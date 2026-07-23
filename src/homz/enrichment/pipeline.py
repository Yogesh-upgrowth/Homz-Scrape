"""Enrichment pipeline.

Three tiers, run in order, each cheaper than the next is expensive:

  Tier 1  rule extraction   — free, 100% coverage, at ingest (see extractors.py)
  Tier 2  deterministic scores — free, needs locality benchmarks from the ETL
  Tier 3  LLM               — paid, selective, batched

Tier 3 only runs on rows where it adds something rules cannot: Reddit prose,
and listings whose builder/project could not be resolved from the gazetteer.
That keeps the bill proportional to the value rather than to the row count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from homz.common.enums import PossessionStatus, Segment, Sentiment
from homz.enrichment import prompts
from homz.enrichment.extractors import (
    canonical_builder,
    extract_entities,
    extract_topics,
    lexicon_sentiment,
)
from homz.enrichment.llm import ENRICHMENT_VERSION, LLMClient, LLMRequest, LLMResult, estimate_cost
from homz.enrichment.scoring import (
    builder_trust_score,
    investment_score,
    location_score,
    risk_score,
)
from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)


@dataclass
class EnrichmentReport:
    properties_scored: int = 0
    properties_llm: int = 0
    reddit_scored: int = 0
    reddit_llm: int = 0
    builders_scored: int = 0
    llm_failures: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "properties_scored": self.properties_scored,
            "properties_llm": self.properties_llm,
            "reddit_scored": self.reddit_scored,
            "reddit_llm": self.reddit_llm,
            "builders_scored": self.builders_scored,
            "llm_failures": self.llm_failures,
            "usage": self.usage,
            "cost": self.cost,
        }


class EnrichmentPipeline:
    def __init__(
        self,
        session: AsyncSession,
        *,
        llm: LLMClient | None = None,
        use_llm: bool | None = None,
        use_batch: bool | None = None,
    ) -> None:
        self.session = session
        self.use_llm = settings.llm_enabled if use_llm is None else use_llm
        self.use_batch = settings.llm_use_batch if use_batch is None else use_batch
        self._llm = llm
        self.report = EnrichmentReport()

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient()
        return self._llm

    async def aclose(self) -> None:
        if self._llm is not None:
            await self._llm.aclose()

    # ==================================================================
    # Tier 2 — deterministic scores over properties
    # ==================================================================

    async def score_properties(self, *, limit: int = 5_000, force: bool = False) -> int:
        """Recompute location/risk/investment scores for pending listings."""
        where_clause = (
            "TRUE"
            if force
            else f"(p.enriched_at IS NULL OR p.enrichment_version < {ENRICHMENT_VERSION})"
        )
        rows = (
            (
                await self.session.execute(
                    text(
                        f"""
                        SELECT p.id, p.city, p.sector, p.micro_market, p.landmarks,
                               p.latitude, p.longitude, p.possession_status,
                               p.possession_date, p.rera_number, p.price_per_sqft,
                               p.segment, p.bedrooms, p.listing_type, p.builder_id,
                               p.first_seen_at,
                               b.trust_score      AS builder_trust,
                               t.median_price_per_sqft,
                               t.listing_count    AS locality_listing_count,
                               y.rental_yield_pct
                        FROM properties p
                        LEFT JOIN builders b ON b.id = p.builder_id
                        LEFT JOIN LATERAL (
                            SELECT median_price_per_sqft, listing_count
                            FROM mv_locality_price_trends mv
                            WHERE mv.city = p.city
                              AND COALESCE(mv.sector,'') = COALESCE(p.sector,'')
                              AND mv.listing_type = p.listing_type
                            ORDER BY mv.period DESC
                            LIMIT 1
                        ) t ON TRUE
                        LEFT JOIN mv_rental_yield y
                               ON y.city = p.city
                              AND COALESCE(y.sector,'') = COALESCE(p.sector,'')
                              AND y.bedrooms = p.bedrooms
                        WHERE p.is_active AND {where_clause}
                        ORDER BY p.last_seen_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
            )
            .mappings()
            .all()
        )

        if not rows:
            return 0

        updates: list[dict[str, Any]] = []
        now = datetime.now(UTC)

        for row in rows:
            possession = _as_enum(PossessionStatus, row["possession_status"])
            segment = _as_enum(Segment, row["segment"], default=Segment.UNKNOWN)

            loc = location_score(
                micro_market=row["micro_market"],
                landmarks=row["landmarks"] or [],
                has_coordinates=row["latitude"] is not None,
                locality_listing_count=row["locality_listing_count"],
            )
            listing_age = (now - row["first_seen_at"]).days if row["first_seen_at"] else None
            rsk = risk_score(
                possession_status=possession,
                possession_date=row["possession_date"],
                rera_number=row["rera_number"],
                builder_trust=_as_float(row["builder_trust"]),
                price_per_sqft=row["price_per_sqft"],
                locality_median_ppsf=row["median_price_per_sqft"],
                listing_age_days=listing_age,
            )
            inv = investment_score(
                location=loc.value,
                risk=rsk.value,
                rental_yield_pct=_as_float(row["rental_yield_pct"]),
                price_per_sqft=row["price_per_sqft"],
                locality_median_ppsf=row["median_price_per_sqft"],
                possession_status=possession,
                segment=segment,
                builder_trust=_as_float(row["builder_trust"]),
            )

            updates.append(
                {
                    "id": row["id"],
                    "location_score": round(loc.value, 2),
                    "risk_score": round(rsk.value, 2),
                    "investment_score": round(inv.value, 2),
                    "builder_trust_score": _as_float(row["builder_trust"]),
                    "version": ENRICHMENT_VERSION,
                }
            )

        await self.session.execute(
            text(
                """
                UPDATE properties SET
                    location_score      = :location_score,
                    risk_score          = :risk_score,
                    investment_score    = :investment_score,
                    builder_trust_score = COALESCE(:builder_trust_score, builder_trust_score),
                    enriched_at         = NOW(),
                    enrichment_version  = :version
                WHERE id = :id
                """
            ),
            updates,
        )
        await self.session.commit()
        self.report.properties_scored += len(updates)
        log.info("enrich.properties_scored", count=len(updates))
        return len(updates)

    # ==================================================================
    # Tier 2 — builder trust
    # ==================================================================

    async def score_builders(self, *, limit: int = 2_000) -> int:
        rows = (
            (
                await self.session.execute(
                    text(
                        """
                        SELECT b.id, b.name, b.normalized_name, b.established_year,
                               b.rating, b.rating_count, b.total_projects,
                               b.completed_projects, b.ongoing_projects,
                               s.reddit_positive, s.reddit_negative, s.reddit_mentions,
                               COALESCE(d.delay_mentions, 0) AS delay_mentions,
                               COALESCE(d.fraud_mentions, 0) AS fraud_mentions
                        FROM builders b
                        LEFT JOIN mv_builder_scorecard s ON s.builder_id = b.id
                        LEFT JOIN LATERAL (
                            SELECT
                              COUNT(*) FILTER (WHERE 'construction_delay' = ANY(rp.topics)
                                                  OR 'possession_issue'  = ANY(rp.topics))
                                  AS delay_mentions,
                              COUNT(*) FILTER (WHERE 'builder_fraud' = ANY(rp.topics))
                                  AS fraud_mentions
                            FROM reddit_posts rp
                            WHERE rp.detected_builders && ARRAY[b.name]
                        ) d ON TRUE
                        ORDER BY b.updated_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
            )
            .mappings()
            .all()
        )

        if not rows:
            return 0

        updates = []
        for row in rows:
            score = builder_trust_score(
                completed_projects=row["completed_projects"],
                ongoing_projects=row["ongoing_projects"],
                total_projects=row["total_projects"],
                established_year=row["established_year"],
                rating=_as_float(row["rating"]),
                rating_count=row["rating_count"],
                positive_mentions=row["reddit_positive"] or 0,
                negative_mentions=row["reddit_negative"] or 0,
                delay_mentions=row["delay_mentions"] or 0,
                fraud_mentions=row["fraud_mentions"] or 0,
            )
            # Builder risk is the mirror of trust, with complaint weight kept.
            updates.append(
                {
                    "id": row["id"],
                    "trust_score": round(score.value, 2),
                    "risk_score": round(100.0 - score.value, 2),
                    "summary": "; ".join(score.notes[:4]) or None,
                }
            )

        await self.session.execute(
            text(
                """
                UPDATE builders SET
                    trust_score        = :trust_score,
                    risk_score         = :risk_score,
                    reputation_summary = COALESCE(:summary, reputation_summary),
                    enriched_at        = NOW()
                WHERE id = :id
                """
            ),
            updates,
        )
        await self.session.commit()
        self.report.builders_scored += len(updates)
        log.info("enrich.builders_scored", count=len(updates))
        return len(updates)

    # ==================================================================
    # Tier 3 — LLM over Reddit
    # ==================================================================

    async def enrich_reddit(self, *, limit: int | None = None) -> int:
        """Sentiment, entities, topics and claims for unenriched posts."""
        limit = limit or settings.llm_batch_size
        rows = (
            (
                await self.session.execute(
                    text(
                        """
                        SELECT p.id, p.source_id, p.title, p.body,
                               COALESCE(
                                 (SELECT array_agg(c.body ORDER BY c.score DESC)
                                  FROM (SELECT body, score FROM reddit_comments
                                        WHERE post_id = p.id
                                        ORDER BY score DESC LIMIT 15) c),
                                 '{}'
                               ) AS comment_bodies
                        FROM reddit_posts p
                        WHERE p.enriched_at IS NULL
                        ORDER BY p.relevance_score DESC NULLS LAST, p.score DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
            )
            .mappings()
            .all()
        )

        if not rows:
            log.info("enrich.reddit_nothing_pending")
            return 0

        # Always apply the free tier so nothing is left unlabelled if the LLM
        # call fails or is disabled.
        fallback_updates = []
        for row in rows:
            blob = "\n".join(filter(None, [row["title"], row["body"] or ""]))
            blob += "\n" + "\n".join(row["comment_bodies"] or [])
            entities = extract_entities(blob)
            sentiment, score = lexicon_sentiment(blob)
            fallback_updates.append(
                {
                    "id": row["id"],
                    "sentiment": sentiment.value,
                    "sentiment_score": score,
                    "builders": entities.builders,
                    "projects": entities.projects,
                    "sectors": entities.sectors,
                    "city": entities.city.value,
                    "topics": extract_topics(blob),
                    "keywords": entities.keywords,
                    "summary": None,
                }
            )
        await self._apply_reddit_updates(fallback_updates, mark_enriched=not self.use_llm)
        self.report.reddit_scored += len(fallback_updates)

        if not self.use_llm:
            log.info("enrich.reddit_rules_only", count=len(fallback_updates))
            return len(fallback_updates)

        requests = [
            LLMRequest(
                custom_id=f"reddit:{row['id']}",
                system=prompts.REDDIT_SYSTEM_PROMPT,
                user=prompts.reddit_user_prompt(
                    title=row["title"],
                    body=row["body"],
                    comments=list(row["comment_bodies"] or []),
                ),
                schema=prompts.REDDIT_SCHEMA,
                max_tokens=2048,
            )
            for row in rows
        ]

        results = await self._run_llm(requests)
        updates = []
        for result in results:
            if not result.ok:
                self.report.llm_failures += 1
                continue
            data = result.data or {}
            if not data.get("is_relevant", True):
                continue
            post_id = int(result.custom_id.split(":", 1)[1])
            updates.append(
                {
                    "id": post_id,
                    "sentiment": data.get("sentiment", "neutral"),
                    "sentiment_score": _bounded(data.get("sentiment_score"), -1.0, 1.0),
                    "builders": _canonical_list(data.get("builders")),
                    "projects": _string_list(data.get("projects")),
                    "sectors": _string_list(data.get("sectors")),
                    "city": data.get("city", "unknown"),
                    "topics": _string_list(data.get("topics")),
                    "keywords": _string_list(data.get("keywords")),
                    "summary": (data.get("summary") or None),
                }
            )

        if updates:
            await self._apply_reddit_updates(updates, mark_enriched=True)
        self.report.reddit_llm += len(updates)
        log.info(
            "enrich.reddit_llm_done",
            requested=len(requests),
            applied=len(updates),
            failures=self.report.llm_failures,
        )
        return len(updates)

    async def _apply_reddit_updates(
        self, updates: list[dict[str, Any]], *, mark_enriched: bool
    ) -> None:
        if not updates:
            return
        enriched_expr = "NOW()" if mark_enriched else "enriched_at"
        await self.session.execute(
            text(
                f"""
                UPDATE reddit_posts SET
                    sentiment         = CAST(:sentiment AS sentiment_enum),
                    sentiment_score   = :sentiment_score,
                    detected_builders = :builders,
                    detected_projects = :projects,
                    detected_sectors  = :sectors,
                    detected_city     = CAST(:city AS city_enum),
                    topics            = :topics,
                    keywords          = :keywords,
                    summary           = COALESCE(:summary, summary),
                    enriched_at       = {enriched_expr}
                WHERE id = :id
                """
            ),
            updates,
        )
        await self.session.commit()

    # ==================================================================
    # Tier 3 — LLM over listings whose entities rules could not resolve
    # ==================================================================

    async def enrich_properties_llm(self, *, limit: int | None = None) -> int:
        limit = limit or settings.llm_batch_size
        if not self.use_llm:
            return 0

        rows = (
            (
                await self.session.execute(
                    text(
                        """
                        SELECT id, title, description, location_raw, specifications,
                               amenities, price_display
                        FROM properties
                        WHERE is_active
                          AND (builder_name IS NULL OR project_name IS NULL
                               OR ai_summary IS NULL)
                          AND description IS NOT NULL
                          AND length(description) > 120
                        ORDER BY last_seen_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
            )
            .mappings()
            .all()
        )

        if not rows:
            return 0

        requests = [
            LLMRequest(
                custom_id=f"property:{row['id']}",
                system=prompts.PROPERTY_SYSTEM_PROMPT,
                user=prompts.property_user_prompt(
                    title=row["title"],
                    description=row["description"],
                    location_raw=row["location_raw"],
                    specs=row["specifications"],
                    amenities=list(row["amenities"] or []),
                    price_display=row["price_display"],
                ),
                schema=prompts.PROPERTY_SCHEMA,
                max_tokens=1536,
            )
            for row in rows
        ]

        results = await self._run_llm(requests)
        updates = []
        for result in results:
            if not result.ok:
                self.report.llm_failures += 1
                continue
            data = result.data or {}
            property_id = int(result.custom_id.split(":", 1)[1])
            summary_parts = [data.get("summary") or ""]
            if data.get("concerns"):
                summary_parts.append("Verify: " + "; ".join(_string_list(data["concerns"])[:3]))
            updates.append(
                {
                    "id": property_id,
                    "builder_name": canonical_builder(data.get("builder_name")),
                    "project_name": data.get("project_name") or None,
                    "tags": _string_list(data.get("tags")),
                    "keywords": _string_list(data.get("keywords")),
                    "amenities": _string_list(data.get("amenities")),
                    "summary": " ".join(p for p in summary_parts if p).strip() or None,
                }
            )

        if updates:
            await self.session.execute(
                text(
                    """
                    UPDATE properties SET
                        builder_name = COALESCE(builder_name, :builder_name),
                        project_name = COALESCE(project_name, :project_name),
                        tags         = :tags,
                        keywords     = :keywords,
                        amenities    = CASE WHEN cardinality(amenities) = 0
                                            THEN :amenities ELSE amenities END,
                        ai_summary   = COALESCE(:summary, ai_summary)
                    WHERE id = :id
                    """
                ),
                updates,
            )
            await self.session.commit()

        self.report.properties_llm += len(updates)
        log.info("enrich.properties_llm_done", requested=len(requests), applied=len(updates))
        return len(updates)

    # ==================================================================

    async def _run_llm(self, requests: list[LLMRequest]) -> list[LLMResult]:
        if not requests:
            return []
        if self.use_batch and len(requests) >= 20:
            # Batch is 50% cheaper; worth the latency for anything non-trivial.
            log.info("enrich.using_batch_api", count=len(requests))
            results = await self.llm.run_batch(requests)
        else:
            results = await self.llm.complete_many(requests, concurrency=4)

        self.report.usage = self.llm.usage.as_dict()
        self.report.cost = estimate_cost(self.llm.usage)
        cache = self.llm.verify_cache_hits()
        if requests and not cache["caching_effective"]:
            log.debug("enrich.prompt_cache_inactive", **cache)
        return results

    # ==================================================================

    async def run_all(self, *, llm_limit: int | None = None) -> EnrichmentReport:
        await self.score_builders()
        await self.score_properties()
        await self.enrich_reddit(limit=llm_limit)
        await self.enrich_properties_llm(limit=llm_limit)
        # Builder trust depends on Reddit sentiment, so a second pass lets the
        # fresh sentiment flow into the scores.
        await self.score_builders()
        log.info("enrich.complete", **self.report.as_dict())
        return self.report


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _as_enum(enum_cls, value, default=None):
    if value is None:
        return default if default is not None else list(enum_cls)[-1]
    try:
        return enum_cls(value)
    except ValueError:
        return default if default is not None else list(enum_cls)[-1]


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bounded(value: Any, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _string_list(value: Any, *, limit: int = 30) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip()[:200])
    return out[:limit]


def _canonical_list(value: Any) -> list[str]:
    return [canonical_builder(name) or name for name in _string_list(value)]


def sentiment_from_string(value: str | None) -> Sentiment:
    try:
        return Sentiment(value or "neutral")
    except ValueError:
        return Sentiment.NEUTRAL
