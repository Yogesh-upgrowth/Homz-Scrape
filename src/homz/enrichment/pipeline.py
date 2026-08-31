"""Enrichment pipeline.

Three tiers, run in order, each cheaper than the next is expensive:

  Tier 1  rule extraction    — free, 100% coverage, at ingest (extractors.py)
  Tier 2  deterministic scores — free, needs locality benchmarks from the ETL
  Tier 3  LLM                — paid, selective, batched

Tier 3 only runs where it adds something rules cannot: Reddit prose, and
listings whose builder/project the gazetteer missed. That keeps the bill
proportional to the value rather than to the row count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne

from homz.common.enums import PossessionStatus, Segment, Sentiment
from homz.db import documents as D
from homz.db.codecs import as_float
from homz.enrichment import prompts
from homz.enrichment.extractors import (
    canonical_builder,
    extract_entities,
    extract_topics,
    lexicon_sentiment,
)
from homz.enrichment.llm import ENRICHMENT_VERSION, LLMClient, LLMRequest, LLMResult, estimate_cost
from homz.enrichment.rera_matching import HIGH, RERA_NUMBER_SHAPE_RE, match_hrera
from homz.enrichment.scoring import (
    builder_trust_score,
    investment_score,
    location_score,
    risk_score,
)
from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

# hrera_registry (see homz.scrapers.hrera) only covers Gurugram district today
# — Faridabad and the rest of Haryana fall under the separate HRERA Panchkula
# authority, not yet scraped. Matching against Faridabad docs here would just
# produce a wall of false "no_match" results, so it's excluded rather than
# silently attempted.
_RERA_CITY_DISTRICT = {"gurgaon": "GURUGRAM"}


@dataclass
class EnrichmentReport:
    properties_scored: int = 0
    properties_llm: int = 0
    reddit_scored: int = 0
    reddit_llm: int = 0
    builders_scored: int = 0
    llm_failures: int = 0
    rera_auto_matched: int = 0
    rera_needs_review: int = 0
    rera_no_match: int = 0
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
            "rera_auto_matched": self.rera_auto_matched,
            "rera_needs_review": self.rera_needs_review,
            "rera_no_match": self.rera_no_match,
            "usage": self.usage,
            "cost": self.cost,
        }


class EnrichmentPipeline:
    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        *,
        llm: LLMClient | None = None,
        use_llm: bool | None = None,
        use_batch: bool | None = None,
    ) -> None:
        self.db = db
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
        """Recompute location/risk/investment scores for pending listings.

        The locality benchmark and builder trust come from `$lookup` against
        the rollup collections — the direct equivalent of the SQL version's
        LATERAL joins.
        """
        match: dict[str, Any] = {"is_active": True}
        if not force:
            match["$or"] = [
                {"enriched_at": None},
                {"enrichment_version": {"$lt": ENRICHMENT_VERSION}},
            ]

        pipeline: list[dict[str, Any]] = [
            {"$match": match},
            {"$sort": {"last_seen_at": -1}},
            {"$limit": limit},
            {"$lookup": {
                "from": D.BUILDERS, "localField": "builder_id", "foreignField": "_id",
                "pipeline": [{"$project": {"trust_score": 1}}], "as": "_builder",
            }},
            {"$lookup": {
                "from": D.MV_LOCALITY_TRENDS,
                "let": {"c": "$city", "s": "$sector", "lt": "$listing_type"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$city", "$$c"]},
                        {"$eq": [{"$ifNull": ["$sector", ""]}, {"$ifNull": ["$$s", ""]}]},
                        {"$eq": ["$listing_type", "$$lt"]},
                    ]}}},
                    {"$sort": {"period": -1}},
                    {"$limit": 1},
                    {"$project": {"median_price_per_sqft": 1, "listing_count": 1}},
                ],
                "as": "_trend",
            }},
            {"$lookup": {
                "from": D.MV_RENTAL_YIELD,
                "let": {"c": "$city", "s": "$sector", "b": "$bedrooms"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$city", "$$c"]},
                        {"$eq": [{"$ifNull": ["$sector", ""]}, {"$ifNull": ["$$s", ""]}]},
                        {"$eq": ["$bedrooms", "$$b"]},
                    ]}}},
                    {"$limit": 1},
                    {"$project": {"rental_yield_pct": 1}},
                ],
                "as": "_yield",
            }},
            {"$project": {
                "city": 1, "sector": 1, "micro_market": 1, "landmarks": 1,
                "latitude": 1, "possession_status": 1, "possession_date": 1,
                "rera_number": 1, "price_per_sqft": 1, "segment": 1,
                "bedrooms": 1, "listing_type": 1, "first_seen_at": 1,
                "builder_trust": {"$first": "$_builder.trust_score"},
                "median_ppsf": {"$first": "$_trend.median_price_per_sqft"},
                "locality_listing_count": {"$first": "$_trend.listing_count"},
                "rental_yield_pct": {"$first": "$_yield.rental_yield_pct"},
            }},
        ]

        rows = await self.db[D.PROPERTIES].aggregate(
            pipeline, allowDiskUse=True
        ).to_list(length=limit)
        if not rows:
            return 0

        now = datetime.now(UTC)
        operations: list[UpdateOne] = []

        for row in rows:
            possession = _as_enum(PossessionStatus, row.get("possession_status"))
            segment = _as_enum(Segment, row.get("segment"), default=Segment.UNKNOWN)
            builder_trust = as_float(row.get("builder_trust"))
            median_ppsf = as_float(row.get("median_ppsf"))

            loc = location_score(
                micro_market=row.get("micro_market"),
                landmarks=row.get("landmarks") or [],
                has_coordinates=row.get("latitude") is not None,
                locality_listing_count=row.get("locality_listing_count"),
            )
            first_seen = row.get("first_seen_at")
            listing_age = (now - first_seen).days if first_seen else None

            rsk = risk_score(
                possession_status=possession,
                possession_date=_as_date(row.get("possession_date")),
                rera_number=row.get("rera_number"),
                builder_trust=builder_trust,
                price_per_sqft=as_float(row.get("price_per_sqft")),
                locality_median_ppsf=median_ppsf,
                listing_age_days=listing_age,
            )
            inv = investment_score(
                location=loc.value,
                risk=rsk.value,
                rental_yield_pct=as_float(row.get("rental_yield_pct")),
                price_per_sqft=as_float(row.get("price_per_sqft")),
                locality_median_ppsf=median_ppsf,
                possession_status=possession,
                segment=segment,
                builder_trust=builder_trust,
            )

            update: dict[str, Any] = {
                "location_score": round(loc.value, 2),
                "risk_score": round(rsk.value, 2),
                "investment_score": round(inv.value, 2),
                "enriched_at": now,
                "enrichment_version": ENRICHMENT_VERSION,
            }
            if builder_trust is not None:
                update["builder_trust_score"] = builder_trust

            operations.append(UpdateOne({"_id": row["_id"]}, {"$set": update}))

        await self._bulk(D.PROPERTIES, operations)
        self.report.properties_scored += len(operations)
        log.info("enrich.properties_scored", count=len(operations))
        return len(operations)

    # ==================================================================
    # Tier 2 — builder trust
    # ==================================================================

    async def score_builders(self, *, limit: int = 2_000) -> int:
        rows = await self.db[D.MV_BUILDER_SCORECARD].find().limit(limit).to_list(length=limit)
        if not rows:
            # Scorecard not built yet — fall back to the builders themselves so
            # a first run still produces scores.
            rows = await self.db[D.BUILDERS].find(
                projection={"name": 1, "established_year": 1, "rating": 1,
                            "rating_count": 1, "total_projects": 1,
                            "completed_projects": 1, "ongoing_projects": 1},
            ).limit(limit).to_list(length=limit)
        if not rows:
            return 0

        operations: list[UpdateOne] = []
        for row in rows:
            score = builder_trust_score(
                completed_projects=row.get("completed_count") or row.get("completed_projects"),
                ongoing_projects=row.get("ongoing_count") or row.get("ongoing_projects"),
                total_projects=row.get("project_count") or row.get("total_projects"),
                established_year=row.get("established_year"),
                rating=as_float(row.get("rating")),
                rating_count=row.get("rating_count"),
                positive_mentions=row.get("reddit_positive") or 0,
                negative_mentions=row.get("reddit_negative") or 0,
                delay_mentions=row.get("delay_mentions") or 0,
                fraud_mentions=row.get("fraud_mentions") or 0,
            )
            operations.append(UpdateOne(
                {"_id": row["_id"]},
                {"$set": {
                    "trust_score": round(score.value, 2),
                    "risk_score": round(100.0 - score.value, 2),
                    "reputation_summary": "; ".join(score.notes[:4]) or None,
                    "enriched_at": datetime.now(UTC),
                }},
            ))

        await self._bulk(D.BUILDERS, operations)
        self.report.builders_scored += len(operations)
        log.info("enrich.builders_scored", count=len(operations))
        return len(operations)

    # ==================================================================
    # Tier 2 — RERA number backfill from the scraped HRERA registry
    # ==================================================================

    async def attach_rera_numbers(self, *, limit: int = 50_000) -> dict[str, int]:
        """Fill in `rera_number` for Haryana properties/projects missing it,
        by fuzzy-matching against `hrera_registry` (see `homz.scrapers.hrera`
        and `homz.enrichment.rera_matching`).

        Only fills blanks — never touches a document that already has a
        `rera_number` (even a possibly-wrong one scraped from a listing
        portal; cross-checking existing values is a separate follow-up, not
        this pass). Only auto-writes matches scoring >= HIGH; everything in
        [LOW, HIGH) is stored as a review candidate, never as the live value.

        Excludes documents that already have a `rera_match_status` from a
        prior run — without this, a run that hits `limit` would keep
        re-deciding the same leading batch forever (Mongo's default find()
        order is stable) instead of ever reaching the rest of the backlog.
        `limit` is intentionally high (unlike the LLM-tier steps): this is
        cheap in-memory string matching, not a paid or rate-limited call, so
        there's little reason to cap a run short of the actual backlog size.
        """
        missing = {
            "$or": [{"rera_number": None}, {"rera_number": {"$exists": False}}],
            "rera_match_status": {"$exists": False},
        }
        totals = {"auto_matched": 0, "needs_review": 0, "no_match": 0}

        for city, district in _RERA_CITY_DISTRICT.items():
            candidates = await self.db[D.HRERA_REGISTRY].find(
                {"district": district}
            ).to_list(length=None)
            if not candidates:
                continue

            property_ops: list[UpdateOne] = []
            rows = await self.db[D.PROPERTIES].find(
                {"city": city, **missing},
                projection={"project_name": 1, "builder_name": 1},
            ).limit(limit).to_list(length=limit)
            for row in rows:
                op = self._rera_update(row, candidates, totals)
                if op is not None:
                    property_ops.append(op)
            await self._bulk(D.PROPERTIES, property_ops)

            project_ops: list[UpdateOne] = []
            rows = await self.db[D.PROJECTS].find(
                {"city": city, **missing},
                projection={"name": 1, "builder_name": 1},
            ).limit(limit).to_list(length=limit)
            for row in rows:
                op = self._rera_update(row, candidates, totals, name_field="name")
                if op is not None:
                    project_ops.append(op)
            await self._bulk(D.PROJECTS, project_ops)

        self.report.rera_auto_matched += totals["auto_matched"]
        self.report.rera_needs_review += totals["needs_review"]
        self.report.rera_no_match += totals["no_match"]
        log.info("enrich.rera_done", **totals)
        return totals

    @staticmethod
    def _rera_update(
        row: dict[str, Any],
        candidates: list[dict[str, Any]],
        totals: dict[str, int],
        *,
        name_field: str = "project_name",
    ) -> UpdateOne | None:
        match = match_hrera(row.get(name_field), row.get("builder_name"), candidates)
        now = datetime.now(UTC)

        if match is None:
            totals["no_match"] += 1
            return UpdateOne({"_id": row["_id"]}, {"$set": {"rera_match_status": "no_match"}})

        if match.score >= HIGH and match.builder_corroborated:
            totals["auto_matched"] += 1
            candidate = match.candidate
            return UpdateOne(
                {"_id": row["_id"]},
                {"$set": {
                    "rera_number": candidate["rera_number"],
                    "rera_certificate_number": candidate.get("certificate_number"),
                    "rera_registered_with": candidate.get("registered_with"),
                    "rera_valid_upto": candidate.get("registration_upto"),
                    "rera_certificate_url": candidate.get("certificate_url"),
                    "rera_match_status": "auto_matched",
                    "rera_match_confidence": round(match.score, 3),
                    "rera_matched_at": now,
                }},
            )

        # LOW <= score < HIGH: a candidate exists but isn't confident enough
        # to become the live rera_number — surface it for manual review only.
        totals["needs_review"] += 1
        return UpdateOne(
            {"_id": row["_id"]},
            {"$set": {
                "rera_match_status": "needs_review",
                "rera_candidate_number": match.candidate["rera_number"],
                "rera_candidate_confidence": round(match.score, 3),
                "rera_matched_at": now,
            }},
        )

    # ==================================================================
    # Tier 2 — flag existing rera_number values that don't look like a real
    # HRERA Project ID, without touching the live value
    # ==================================================================

    async def verify_existing_rera_numbers(self, *, limit: int = 50_000) -> dict[str, int]:
        """Audit documents that already HAVE a `rera_number` whose shape
        doesn't match a real HRERA Project ID (`RERA_NUMBER_SHAPE_RE`) —
        confirmed live 2026-08-31: Ireo Skyon was stored as rera_number
        "367" while the registry's real ID is "RERA-GRG-1789-2024".

        Never overwrites the live `rera_number` — this is audit-only.
        Three-tier, same shape as attach_rera_numbers: a HIGH-confidence,
        builder-corroborated match is stored as `rera_suggested_number` for a
        human to promote; a weaker [LOW, HIGH) match is stored as
        `rera_candidate_number`/`rera_candidate_confidence` for manual
        review rather than discarded — earlier versions of this method
        collapsed both into one "unverifiable" bucket, losing the weak-signal
        cases that are exactly the ones worth a human glance; genuinely
        below LOW is `unverifiable` (it may still be a real, valid
        pre-HRERA-ID format this registry simply can't confirm — absence of
        proof isn't proof of wrongness).
        """
        suspect = {
            "rera_number": {"$exists": True, "$ne": None},
            "rera_verify_status": {"$exists": False},
        }
        totals = {"mismatch_flagged": 0, "needs_review": 0, "unverifiable": 0}

        for city, district in _RERA_CITY_DISTRICT.items():
            candidates = await self.db[D.HRERA_REGISTRY].find(
                {"district": district}
            ).to_list(length=None)
            if not candidates:
                continue

            for collection, name_field in ((D.PROPERTIES, "project_name"), (D.PROJECTS, "name")):
                rows = await self.db[collection].find(
                    {"city": city, **suspect},
                    projection={name_field: 1, "builder_name": 1, "rera_number": 1},
                ).limit(limit).to_list(length=limit)
                shaped_out = [r for r in rows if not RERA_NUMBER_SHAPE_RE.match(r["rera_number"])]
                ops = [
                    self._rera_verify_update(row, candidates, totals, name_field=name_field)
                    for row in shaped_out
                ]
                await self._bulk(collection, ops)

        log.info("enrich.rera_verify_done", **totals)
        return totals

    @staticmethod
    def _rera_verify_update(
        row: dict[str, Any],
        candidates: list[dict[str, Any]],
        totals: dict[str, int],
        *,
        name_field: str,
    ) -> UpdateOne:
        match = match_hrera(row.get(name_field), row.get("builder_name"), candidates)
        now = datetime.now(UTC)

        if match is not None and match.score >= HIGH and match.builder_corroborated:
            totals["mismatch_flagged"] += 1
            return UpdateOne(
                {"_id": row["_id"]},
                {"$set": {
                    "rera_verify_status": "mismatch_flagged",
                    "rera_existing_number": row["rera_number"],
                    "rera_suggested_number": match.candidate["rera_number"],
                    "rera_suggested_confidence": round(match.score, 3),
                    "rera_verified_at": now,
                }},
            )

        if match is not None:
            # LOW <= score < HIGH (or below HIGH without builder
            # corroboration) — a candidate exists but isn't confident enough
            # to suggest as THE fix; still worth a human glance instead of
            # silently discarding it.
            totals["needs_review"] += 1
            return UpdateOne(
                {"_id": row["_id"]},
                {"$set": {
                    "rera_verify_status": "needs_review",
                    "rera_existing_number": row["rera_number"],
                    "rera_candidate_number": match.candidate["rera_number"],
                    "rera_candidate_confidence": round(match.score, 3),
                    "rera_verified_at": now,
                }},
            )

        totals["unverifiable"] += 1
        return UpdateOne(
            {"_id": row["_id"]},
            {"$set": {"rera_verify_status": "unverifiable", "rera_verified_at": now}},
        )

    async def promote_verified_rera_corrections(
        self,
        *,
        limit: int = 50_000,
        also_promote_exact_needs_review: bool = False,
        promote_needs_review_with_builder_at: float | None = None,
    ) -> int:
        """Overwrite `rera_number` with `rera_suggested_number` for every
        `mismatch_flagged` document — the one case in this whole RERA
        pipeline where an *existing* live value is intentionally replaced,
        because these already passed the same HIGH-confidence,
        builder-corroborated bar as every other auto-write here (see
        `verify_existing_rera_numbers`). `rera_existing_number` (the old
        value) stays on the document as an audit trail.

        Does not touch plain `unverifiable` documents — there is no
        confirmed correct value for those, only a shape that looks wrong.

        `also_promote_exact_needs_review=True` is a deliberately higher-risk,
        explicitly-opted-into mode: it additionally promotes `needs_review`
        documents whose `rera_candidate_confidence == 1.0` even without
        builder corroboration. Many of these are missing `builder_name`
        entirely on our side (not an ambiguous match, just nothing to
        cross-check against) — but a perfect project-name-only score is
        exactly the failure mode that produced the earlier false positive
        ("The Residences" matching an unrelated "The Estate Residences" at
        score 1.0), so this must never be the default.

        `promote_needs_review_with_builder_at` additionally promotes
        needs_review documents scoring at or above this threshold, but only
        when our own document HAS a builder_name (i.e. a builder comparison
        actually happened, even if it scored low). Verified live 2026-08-31:
        the entire 0.70-1.0 band with a builder present was inspected by
        hand — 39/40 sampled were exact project-name matches where the score
        was merely dragged down by a brand-vs-SPV builder-name mismatch
        (e.g. "M3M" vs the project's actual "Roshni Builders Pvt. Ltd."), a
        real and common pattern in Indian real estate, not a false positive.
        The unscoped 0.5-1.0 band was explicitly rejected: confirmed wrong
        matches there (e.g. "Signature Global Tonino Lamborghini Residences"
        -> "Signature Global SCO-37D", matched on shared builder-brand
        tokens alone) came from PARTIAL project-name overlap combined with a
        same-family builder — exactly what this threshold must stay above.
        """
        promoted = 0
        for collection in (D.PROPERTIES, D.PROJECTS):
            match: dict[str, Any] = {"rera_verify_status": "mismatch_flagged"}
            extra_or: list[dict[str, Any]] = []
            if also_promote_exact_needs_review:
                extra_or.append({"rera_verify_status": "needs_review", "rera_candidate_confidence": 1.0})
            if promote_needs_review_with_builder_at is not None:
                extra_or.append({
                    "rera_verify_status": "needs_review",
                    "rera_candidate_confidence": {"$gte": promote_needs_review_with_builder_at},
                    "builder_name": {"$ne": None},
                })
            if extra_or:
                match = {"$or": [match, *extra_or]}

            rows = await self.db[collection].find(
                match,
                projection={"rera_suggested_number": 1, "rera_candidate_number": 1},
            ).limit(limit).to_list(length=limit)
            if not rows:
                continue

            def target_number(row: dict[str, Any]) -> str | None:
                return row.get("rera_suggested_number") or row.get("rera_candidate_number")

            target_numbers = list({n for r in rows if (n := target_number(r))})
            registry_rows = await self.db[D.HRERA_REGISTRY].find(
                {"rera_number": {"$in": target_numbers}}
            ).to_list(length=None)
            registry_by_number = {r["rera_number"]: r for r in registry_rows}

            now = datetime.now(UTC)
            ops = []
            for row in rows:
                candidate = registry_by_number.get(target_number(row) or "")
                if candidate is None:
                    continue
                ops.append(UpdateOne(
                    {"_id": row["_id"]},
                    {"$set": {
                        "rera_number": candidate["rera_number"],
                        "rera_certificate_number": candidate.get("certificate_number"),
                        "rera_registered_with": candidate.get("registered_with"),
                        "rera_valid_upto": candidate.get("registration_upto"),
                        "rera_certificate_url": candidate.get("certificate_url"),
                        "rera_verify_status": "corrected",
                        "rera_corrected_at": now,
                    }},
                ))
            await self._bulk(collection, ops)
            promoted += len(ops)

        log.info("enrich.rera_corrections_promoted", count=promoted)
        return promoted

    # ==================================================================
    # Tier 3 — LLM over Reddit
    # ==================================================================

    async def enrich_reddit(self, *, limit: int | None = None) -> int:
        limit = limit or settings.llm_batch_size
        rows = await self.db[D.REDDIT_POSTS].find(
            {"enriched_at": None},
            projection={"title": 1, "body": 1, "top_comments": 1},
        ).sort([("relevance_score", -1), ("score", -1)]).limit(limit).to_list(length=limit)

        if not rows:
            log.info("enrich.reddit_nothing_pending")
            return 0

        # Always apply the free tier so nothing is left unlabelled if the LLM
        # call fails or is disabled.
        fallback: list[UpdateOne] = []
        prompts_by_id: dict[str, str] = {}
        for row in rows:
            comments = [c.get("body") or "" for c in (row.get("top_comments") or [])]
            blob = "\n".join(filter(None, [row.get("title"), row.get("body") or "", *comments]))
            entities = extract_entities(blob)
            sentiment, score = lexicon_sentiment(blob)
            prompts_by_id[row["_id"]] = prompts.reddit_user_prompt(
                title=row.get("title") or "", body=row.get("body"), comments=comments
            )
            fallback.append(UpdateOne({"_id": row["_id"]}, {"$set": {
                "sentiment": sentiment.value,
                "sentiment_score": score,
                "detected_builders": entities.builders,
                "detected_projects": entities.projects,
                "detected_sectors": entities.sectors,
                "detected_city": entities.city.value,
                "topics": extract_topics(blob),
                "keywords": entities.keywords,
                **({} if self.use_llm else {"enriched_at": datetime.now(UTC)}),
            }}))

        await self._bulk(D.REDDIT_POSTS, fallback)
        self.report.reddit_scored += len(fallback)

        if not self.use_llm:
            log.info("enrich.reddit_rules_only", count=len(fallback))
            return len(fallback)

        requests = [
            LLMRequest(
                custom_id=f"reddit:{post_id}",
                system=prompts.REDDIT_SYSTEM_PROMPT,
                user=user_prompt,
                schema=prompts.REDDIT_SCHEMA,
                max_tokens=2048,
            )
            for post_id, user_prompt in prompts_by_id.items()
        ]

        results = await self._run_llm(requests)
        operations: list[UpdateOne] = []
        for result in results:
            if not result.ok:
                self.report.llm_failures += 1
                continue
            data = result.data or {}
            if not data.get("is_relevant", True):
                continue
            post_id = result.custom_id.split(":", 1)[1]
            update = {
                "sentiment": data.get("sentiment", "neutral"),
                "sentiment_score": _bounded(data.get("sentiment_score"), -1.0, 1.0),
                "detected_builders": _canonical_list(data.get("builders")),
                "detected_projects": _string_list(data.get("projects")),
                "detected_sectors": _string_list(data.get("sectors")),
                "detected_city": data.get("city", "unknown"),
                "topics": _string_list(data.get("topics")),
                "keywords": _string_list(data.get("keywords")),
                "claims": _claims(data.get("claims")),
                "enriched_at": datetime.now(UTC),
            }
            if data.get("summary"):
                update["summary"] = data["summary"]
            operations.append(UpdateOne({"_id": post_id}, {"$set": update}))

        await self._bulk(D.REDDIT_POSTS, operations)
        self.report.reddit_llm += len(operations)
        log.info(
            "enrich.reddit_llm_done",
            requested=len(requests), applied=len(operations),
            failures=self.report.llm_failures,
        )
        return len(operations)

    # ==================================================================
    # Tier 3 — LLM over listings the gazetteer could not resolve
    # ==================================================================

    async def enrich_properties_llm(self, *, limit: int | None = None) -> int:
        limit = limit or settings.llm_batch_size
        if not self.use_llm:
            return 0

        rows = await self.db[D.PROPERTIES].find(
            {
                "is_active": True,
                "description": {"$ne": None, "$exists": True},
                "$or": [
                    {"builder_name": None}, {"project_name": None}, {"ai_summary": None},
                ],
            },
            projection={"title": 1, "description": 1, "location_raw": 1,
                        "specifications": 1, "amenities": 1, "price_display": 1},
        ).sort("last_seen_at", -1).limit(limit).to_list(length=limit)

        rows = [r for r in rows if len(r.get("description") or "") > 120]
        if not rows:
            return 0

        requests = [
            LLMRequest(
                custom_id=f"property:{row['_id']}",
                system=prompts.PROPERTY_SYSTEM_PROMPT,
                user=prompts.property_user_prompt(
                    title=row.get("title"),
                    description=row.get("description"),
                    location_raw=row.get("location_raw"),
                    specs=row.get("specifications"),
                    amenities=list(row.get("amenities") or []),
                    price_display=row.get("price_display"),
                ),
                schema=prompts.PROPERTY_SCHEMA,
                max_tokens=1536,
            )
            for row in rows
        ]

        results = await self._run_llm(requests)
        operations: list[UpdateOne] = []
        for result in results:
            if not result.ok:
                self.report.llm_failures += 1
                continue
            data = result.data or {}
            property_id = result.custom_id.split(":", 1)[1]

            summary_parts = [data.get("summary") or ""]
            if data.get("concerns"):
                summary_parts.append("Verify: " + "; ".join(_string_list(data["concerns"])[:3]))
            summary = " ".join(p for p in summary_parts if p).strip()

            update: dict[str, Any] = {
                "tags": _string_list(data.get("tags")),
                "keywords": _string_list(data.get("keywords")),
            }
            if summary:
                update["ai_summary"] = summary
            # Only fill what rules missed — never overwrite a scraped value.
            builder = canonical_builder(data.get("builder_name"))
            operations.append(UpdateOne(
                {"_id": property_id},
                [{"$set": {
                    **update,
                    "builder_name": {"$ifNull": ["$builder_name", builder]},
                    "project_name": {
                        "$ifNull": ["$project_name", data.get("project_name")]
                    },
                    "amenities": {"$cond": [
                        {"$eq": [{"$size": {"$ifNull": ["$amenities", []]}}, 0]},
                        _string_list(data.get("amenities")),
                        "$amenities",
                    ]},
                }}],
            ))

        await self._bulk(D.PROPERTIES, operations)
        self.report.properties_llm += len(operations)
        log.info("enrich.properties_llm_done", requested=len(requests),
                 applied=len(operations))
        return len(operations)

    # ==================================================================

    async def _bulk(self, collection: str, operations: list[UpdateOne]) -> None:
        if not operations:
            return
        # Chunked so one oversized batch cannot exceed the 16 MB command limit.
        for start in range(0, len(operations), 500):
            await self.db[collection].bulk_write(
                operations[start : start + 500], ordered=False
            )

    async def _run_llm(self, requests: list[LLMRequest]) -> list[LLMResult]:
        if not requests:
            return []
        if self.use_batch and len(requests) >= 20:
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
        await self.attach_rera_numbers()
        await self.verify_existing_rera_numbers()
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


def _as_date(value: Any):
    from homz.db.codecs import as_date

    return as_date(value)


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


def _claims(value: Any, *, limit: int = 12) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        if isinstance(item, dict) and item.get("claim"):
            out.append({
                "claim": str(item["claim"])[:500],
                "subject": str(item.get("subject", ""))[:200],
                "polarity": str(item.get("polarity", "neutral")),
            })
    return out


def sentiment_from_string(value: str | None) -> Sentiment:
    try:
        return Sentiment(value or "neutral")
    except ValueError:
        return Sentiment.NEUTRAL
