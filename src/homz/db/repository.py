"""Persistence layer: idempotent UPSERTs keyed on natural identity.

Every write here is `INSERT ... ON CONFLICT DO UPDATE` on `(source, source_id)`,
so re-running a scraper is safe and cheap. The `COALESCE(EXCLUDED.x, table.x)`
pattern is deliberate: a thin re-scrape (search-card only) must never blank out
a field that a richer detail-page scrape already filled.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from homz.common.parsing import normalize_name
from homz.common.schema import (
    BuilderRecord,
    MarketInsightRecord,
    ProjectRecord,
    PropertyRecord,
    RedditPostRecord,
)
from homz.common.schema import (
    Location as LocationSchema,
)
from homz.db import models as m
from homz.logging_setup import get_logger

log = get_logger(__name__)


def _jsonable(value: Any) -> Any:
    """Pydantic sub-documents → plain JSON for JSONB columns."""
    import orjson

    return orjson.loads(orjson.dumps(value, default=str))


def _url_hash(url: str) -> str:
    return hashlib.sha1(url.split("?")[0].encode("utf-8")).hexdigest()


def infer_builder_from_project(project_name: str | None) -> str | None:
    """Resolve a developer from a project name using the NCR gazetteer.

    "Godrej Aristocrat" → "Godrej Properties". Returns None when the name does
    not resolve to exactly one known developer, so an unrelated or ambiguous
    project is never misattributed.
    """
    if not project_name:
        return None
    from homz.enrichment.extractors import extract_builders

    matches = extract_builders(project_name)
    return matches[0] if len(matches) == 1 else None


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._location_cache: dict[str, int] = {}
        self._builder_cache: dict[str, int] = {}

    # ------------------------------------------------------------------ locations

    async def upsert_location(self, location: LocationSchema) -> int | None:
        if location.city.value == "unknown" and not (location.locality or location.sector):
            return None

        slug = location.slug()
        if slug in self._location_cache:
            return self._location_cache[slug]

        values = {
            "slug": slug,
            "city": location.city.value,
            "state": location.state,
            "locality": location.locality,
            "sector": location.sector,
            "sub_locality": location.sub_locality,
            "micro_market": location.micro_market,
            "pincode": location.pincode,
            "latitude": location.geo.latitude if location.geo else None,
            "longitude": location.geo.longitude if location.geo else None,
        }
        stmt = (
            insert(m.Location)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[m.Location.slug],
                set_={
                    # Keep the first non-null we ever saw for coordinates —
                    # a card-level scrape often has none.
                    "locality": func.coalesce(text("EXCLUDED.locality"), m.Location.locality),
                    "sector": func.coalesce(text("EXCLUDED.sector"), m.Location.sector),
                    "micro_market": func.coalesce(
                        text("EXCLUDED.micro_market"), m.Location.micro_market
                    ),
                    "pincode": func.coalesce(text("EXCLUDED.pincode"), m.Location.pincode),
                    "latitude": func.coalesce(text("EXCLUDED.latitude"), m.Location.latitude),
                    "longitude": func.coalesce(text("EXCLUDED.longitude"), m.Location.longitude),
                    "updated_at": func.now(),
                },
            )
            .returning(m.Location.id)
        )
        location_id = (await self.session.execute(stmt)).scalar_one()
        self._location_cache[slug] = location_id
        return location_id

    # ------------------------------------------------------------------ builders

    async def resolve_builder(self, name: str | None, source: str) -> int | None:
        """Find-or-create a builder from just a name (as seen on a listing)."""
        normalized = normalize_name(name)
        if not normalized:
            return None
        if normalized in self._builder_cache:
            return self._builder_cache[normalized]

        existing = (
            await self.session.execute(
                select(m.Builder.id)
                .where(m.Builder.normalized_name == normalized)
                .order_by(m.Builder.total_projects.desc().nullslast())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            self._builder_cache[normalized] = existing
            return existing

        stmt = (
            insert(m.Builder)
            .values(
                source=source,
                source_id=f"derived:{normalized}",
                name=name.strip(),
                normalized_name=normalized,
            )
            .on_conflict_do_update(
                index_elements=[m.Builder.source, m.Builder.source_id],
                set_={"updated_at": func.now()},
            )
            .returning(m.Builder.id)
        )
        builder_id = (await self.session.execute(stmt)).scalar_one()
        self._builder_cache[normalized] = builder_id
        return builder_id

    async def upsert_builder(self, record: BuilderRecord) -> int:
        normalized = normalize_name(record.name) or record.name.lower()
        contact = record.contact
        values = {
            "source": record.source.value,
            "source_id": record.source_id,
            "profile_url": record.profile_url,
            "name": record.name,
            "normalized_name": normalized,
            "description": record.description,
            "established_year": record.established_year,
            "headquarters": record.headquarters,
            "website": record.website,
            "total_projects": record.total_projects,
            "ongoing_projects": record.ongoing_projects,
            "completed_projects": record.completed_projects,
            "upcoming_projects": record.upcoming_projects,
            "rating": record.rating,
            "rating_count": record.rating_count,
            "review_count": record.review_count,
            "reviews": _jsonable(record.reviews),
            "cities": record.cities,
            "contact_name": contact.name if contact else None,
            "contact_phone": contact.phone if contact else None,
            "contact_email": contact.email if contact else None,
            "raw_html_key": record.raw_html_key,
            "raw": _jsonable(record.raw),
            "scraped_at": record.scraped_at,
        }
        preserve = (
            "profile_url", "description", "established_year", "headquarters", "website",
            "total_projects", "ongoing_projects", "completed_projects", "upcoming_projects",
            "rating", "rating_count", "review_count", "contact_name", "contact_phone",
            "contact_email",
        )
        set_ = {col: func.coalesce(text(f"EXCLUDED.{col}"), getattr(m.Builder, col))
                for col in preserve}
        set_.update(
            {
                "name": text("EXCLUDED.name"),
                "normalized_name": text("EXCLUDED.normalized_name"),
                "reviews": text("EXCLUDED.reviews"),
                "cities": text("EXCLUDED.cities"),
                "raw_html_key": text("EXCLUDED.raw_html_key"),
                "raw": text("EXCLUDED.raw"),
                "scraped_at": text("EXCLUDED.scraped_at"),
                "updated_at": func.now(),
            }
        )
        stmt = (
            insert(m.Builder)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[m.Builder.source, m.Builder.source_id], set_=set_
            )
            .returning(m.Builder.id)
        )
        builder_id = (await self.session.execute(stmt)).scalar_one()
        self._builder_cache[normalized] = builder_id
        return builder_id

    # ------------------------------------------------------------------ projects

    async def upsert_project(self, record: ProjectRecord) -> int:
        location_id = await self.upsert_location(record.location)
        builder_name = record.builder_name or infer_builder_from_project(record.name)
        builder_id = await self.resolve_builder(builder_name, record.source.value)

        values = {
            "source": record.source.value,
            "source_id": record.source_id,
            "project_url": record.project_url,
            "name": record.name,
            "normalized_name": normalize_name(record.name) or record.name.lower(),
            "builder_id": builder_id,
            "builder_name": builder_name,
            "location_id": location_id,
            "status": record.status.value,
            "launch_date": record.launch_date,
            "possession_date": record.possession_date,
            "rera_number": record.rera_number,
            "price_min": record.price_min,
            "price_max": record.price_max,
            "price_per_sqft": record.price_per_sqft,
            "total_units": record.total_units,
            "total_towers": record.total_towers,
            "project_area_acres": record.project_area_acres,
            "configurations": _jsonable([c.model_dump() for c in record.configurations]),
            "amenities": record.amenities,
            "specifications": _jsonable(record.specifications),
            "landmarks": _jsonable([lm.model_dump() for lm in record.landmarks]),
            "construction_updates": _jsonable(record.construction_updates),
            "description": record.description,
            "raw_html_key": record.raw_html_key,
            "raw": _jsonable(record.raw),
            "scraped_at": record.scraped_at,
        }
        preserve = (
            "builder_id", "builder_name", "location_id", "launch_date", "possession_date",
            "rera_number", "price_min", "price_max", "price_per_sqft", "total_units",
            "total_towers", "project_area_acres", "description",
        )
        set_ = {col: func.coalesce(text(f"EXCLUDED.{col}"), getattr(m.Project, col))
                for col in preserve}
        set_.update(
            {
                "name": text("EXCLUDED.name"),
                "normalized_name": text("EXCLUDED.normalized_name"),
                "project_url": text("EXCLUDED.project_url"),
                "status": text("EXCLUDED.status"),
                "configurations": text("EXCLUDED.configurations"),
                "amenities": text("EXCLUDED.amenities"),
                "specifications": text("EXCLUDED.specifications"),
                "landmarks": text("EXCLUDED.landmarks"),
                "construction_updates": text("EXCLUDED.construction_updates"),
                "raw_html_key": text("EXCLUDED.raw_html_key"),
                "raw": text("EXCLUDED.raw"),
                "scraped_at": text("EXCLUDED.scraped_at"),
                "updated_at": func.now(),
            }
        )
        stmt = (
            insert(m.Project)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[m.Project.source, m.Project.source_id], set_=set_
            )
            .returning(m.Project.id)
        )
        project_id = (await self.session.execute(stmt)).scalar_one()
        await self._upsert_images(record.images, project_id=project_id)
        return project_id

    # ------------------------------------------------------------------ properties

    async def upsert_property(self, record: PropertyRecord) -> tuple[int, bool]:
        """Returns (property_id, is_new)."""
        if record.content_hash is None:
            record.finalize()

        location_id = await self.upsert_location(record.location)

        # Most listings name the project but not the developer. "Godrej
        # Aristocrat" implies Godrej Properties, and without this inference the
        # builders table stays near-empty and every builder-trust feature has
        # nothing to score.
        builder_name = record.builder_name or record.developer_name
        if not builder_name:
            builder_name = infer_builder_from_project(
                record.project_name or record.society_name or record.title
            )
            if builder_name:
                record.builder_name = builder_name

        builder_id = await self.resolve_builder(builder_name, record.source.value)
        project_id = await self._resolve_project(record, builder_id)
        contact = record.contact
        geo = record.location.geo

        values = {
            "source": record.source.value,
            "source_id": record.source_id,
            "listing_url": record.listing_url,
            "title": record.title,
            "description": record.description,
            "project_id": project_id,
            "project_name": record.project_name,
            "builder_id": builder_id,
            "builder_name": record.builder_name,
            "developer_name": record.developer_name,
            "society_name": record.society_name,
            "listing_type": record.listing_type.value,
            "property_type": record.property_type.value,
            "property_type_raw": record.property_type_raw,
            "segment": record.segment.value,
            "is_commercial": record.is_commercial,
            "is_luxury": record.is_luxury,
            "is_affordable": record.is_affordable,
            "configuration": record.configuration,
            "bedrooms": record.bedrooms,
            "bathrooms": record.bathrooms,
            "balconies": record.balconies,
            "floor_number": record.floor_number,
            "total_floors": record.total_floors,
            "facing": record.facing,
            "furnishing": record.furnishing,
            "age_years": record.age_years,
            "price": record.price,
            "price_max": record.price_max,
            "price_display": record.price_display,
            "price_per_sqft": record.price_per_sqft,
            "booking_amount": record.booking_amount,
            "maintenance_charge": record.maintenance_charge,
            "rent_monthly": record.rent_monthly,
            "security_deposit": record.security_deposit,
            "is_price_on_request": record.is_price_on_request,
            "area_value": record.area_value,
            "area_unit": record.area_unit.value if record.area_unit else None,
            "area_sqft": record.area_sqft,
            "carpet_area_sqft": record.carpet_area_sqft,
            "built_up_area_sqft": record.built_up_area_sqft,
            "super_built_up_area_sqft": record.super_built_up_area_sqft,
            "plot_area_sqft": record.plot_area_sqft,
            "location_id": location_id,
            "location_raw": record.location.raw,
            "city": record.location.city.value,
            "sector": record.location.sector,
            "locality": record.location.locality,
            "micro_market": record.location.micro_market,
            "latitude": geo.latitude if geo else None,
            "longitude": geo.longitude if geo else None,
            "possession_status": record.possession_status.value,
            "possession_date": record.possession_date,
            "possession_raw": record.possession_raw,
            "rera_number": record.rera_number,
            "rera_status": record.rera_status,
            "total_units": record.total_units,
            "project_area_acres": record.project_area_acres,
            "launch_date": record.launch_date,
            "amenities": record.amenities,
            "specifications": _jsonable(record.specifications),
            "unit_configurations": _jsonable(
                [c.model_dump() for c in record.unit_configurations]
            ),
            "landmarks": _jsonable([lm.model_dump() for lm in record.landmarks]),
            "contact_name": contact.name if contact else None,
            "contact_seller_type": contact.seller_type.value if contact else "unknown",
            "contact_company": contact.company if contact else None,
            "contact_phone": contact.phone if contact else None,
            "contact_email": contact.email if contact else None,
            "listed_at": record.listed_at,
            "listing_date_raw": record.listing_date_raw,
            "updated_at_source": record.updated_at_source,
            "scraped_at": record.scraped_at,
            "last_seen_at": record.scraped_at,
            "is_active": True,
            "delisted_at": None,
            "content_hash": record.content_hash,
            "dedupe_key": record.dedupe_key,
            "raw_html_key": record.raw_html_key,
            "raw": _jsonable(record.raw),
        }

        # Fields a partial re-scrape must not erase.
        preserve = (
            "title", "description", "project_id", "project_name", "builder_id", "builder_name",
            "developer_name", "society_name", "configuration", "bedrooms", "bathrooms",
            "balconies", "floor_number", "total_floors", "facing", "furnishing", "age_years",
            "price_max", "price_display", "booking_amount", "maintenance_charge",
            "security_deposit", "area_value", "area_unit", "area_sqft", "carpet_area_sqft",
            "built_up_area_sqft", "super_built_up_area_sqft", "plot_area_sqft", "location_id",
            "location_raw", "sector", "locality", "micro_market", "latitude", "longitude",
            "possession_date", "possession_raw", "rera_number", "rera_status", "total_units",
            "project_area_acres", "launch_date", "contact_name", "contact_company",
            "contact_phone", "contact_email", "listed_at", "listing_date_raw",
            "updated_at_source", "property_type_raw",
        )
        set_ = {col: func.coalesce(text(f"EXCLUDED.{col}"), getattr(m.Property, col))
                for col in preserve}
        # Always-overwrite fields: current state of the listing.
        for col in (
            "listing_url", "listing_type", "property_type", "segment", "is_commercial",
            "is_luxury", "is_affordable", "price", "price_per_sqft", "rent_monthly",
            "is_price_on_request", "city", "possession_status", "amenities", "specifications",
            "unit_configurations", "landmarks", "contact_seller_type", "scraped_at",
            "last_seen_at", "is_active", "delisted_at", "content_hash", "dedupe_key",
            "raw_html_key", "raw",
        ):
            set_[col] = text(f"EXCLUDED.{col}")
        set_["updated_at"] = func.now()

        stmt = (
            insert(m.Property)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[m.Property.source, m.Property.source_id], set_=set_
            )
            .returning(m.Property.id, text("(xmax = 0) AS inserted"))
        )
        row = (await self.session.execute(stmt)).one()
        property_id, is_new = int(row[0]), bool(row[1])

        await self._upsert_images(record.images, property_id=property_id)
        return property_id, is_new

    async def _resolve_project(
        self, record: PropertyRecord, builder_id: int | None
    ) -> int | None:
        """Link a listing to a project row when we can match one confidently."""
        normalized = normalize_name(record.project_name)
        if not normalized:
            return None
        stmt = select(m.Project.id).where(m.Project.normalized_name == normalized)
        if record.location.city.value != "unknown":
            stmt = stmt.join(
                m.Location, m.Project.location_id == m.Location.id, isouter=True
            ).where(
                (m.Location.city == record.location.city.value) | (m.Project.location_id.is_(None))
            )
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    async def _upsert_images(
        self, images: list, *, property_id: int | None = None, project_id: int | None = None
    ) -> int:
        if not images:
            return 0
        rows = []
        seen: set[str] = set()
        for position, image in enumerate(images[:60]):
            digest = _url_hash(image.url)
            if digest in seen:
                continue
            seen.add(digest)
            rows.append(
                {
                    "property_id": property_id,
                    "project_id": project_id,
                    "url": image.url,
                    "url_hash": digest,
                    "caption": image.caption,
                    "is_primary": image.is_primary or position == 0,
                    "width": image.width,
                    "height": image.height,
                    "position": position,
                }
            )
        if not rows:
            return 0

        index_elements = (
            [m.PropertyImage.property_id, m.PropertyImage.url_hash]
            if property_id is not None
            else [m.PropertyImage.project_id, m.PropertyImage.url_hash]
        )
        index_where = (
            m.PropertyImage.property_id.isnot(None)
            if property_id is not None
            else m.PropertyImage.project_id.isnot(None)
        )
        stmt = (
            insert(m.PropertyImage)
            .values(rows)
            .on_conflict_do_update(
                index_elements=index_elements,
                index_where=index_where,
                set_={
                    "caption": text("EXCLUDED.caption"),
                    "is_primary": text("EXCLUDED.is_primary"),
                    "position": text("EXCLUDED.position"),
                },
            )
        )
        await self.session.execute(stmt)
        return len(rows)

    # ------------------------------------------------------------------ reddit

    async def upsert_reddit_post(self, record: RedditPostRecord) -> int:
        values = {
            "source_id": record.source_id,
            "subreddit": record.subreddit,
            "url": record.url,
            "permalink": record.permalink,
            "title": record.title,
            "body": record.body,
            "author": record.author,
            "created_utc": record.created_utc,
            "score": record.score,
            "upvote_ratio": record.upvote_ratio,
            "num_comments": record.num_comments,
            "flair": record.flair,
            "is_self": record.is_self,
            "over_18": record.over_18,
            "detected_builders": record.detected_builders,
            "detected_projects": record.detected_projects,
            "detected_sectors": record.detected_sectors,
            "detected_city": (record.detected_city.value if record.detected_city else "unknown"),
            "topics": record.topics,
            "keywords": record.keywords,
            "relevance_score": record.relevance_score,
            "sentiment": record.sentiment.value if record.sentiment else None,
            "sentiment_score": record.sentiment_score,
            "summary": record.summary,
            "raw": _jsonable(record.raw),
            "scraped_at": record.scraped_at,
        }
        set_ = {
            # Score and comment count move constantly — always take the newest.
            "score": text("EXCLUDED.score"),
            "upvote_ratio": text("EXCLUDED.upvote_ratio"),
            "num_comments": text("EXCLUDED.num_comments"),
            "body": func.coalesce(text("EXCLUDED.body"), m.RedditPost.body),
            "flair": func.coalesce(text("EXCLUDED.flair"), m.RedditPost.flair),
            "detected_builders": text("EXCLUDED.detected_builders"),
            "detected_projects": text("EXCLUDED.detected_projects"),
            "detected_sectors": text("EXCLUDED.detected_sectors"),
            "detected_city": text("EXCLUDED.detected_city"),
            "topics": text("EXCLUDED.topics"),
            "keywords": text("EXCLUDED.keywords"),
            "relevance_score": text("EXCLUDED.relevance_score"),
            # Enrichment output is only overwritten when the new value is set,
            # so a plain re-scrape doesn't wipe an LLM pass.
            "sentiment": func.coalesce(text("EXCLUDED.sentiment"), m.RedditPost.sentiment),
            "sentiment_score": func.coalesce(
                text("EXCLUDED.sentiment_score"), m.RedditPost.sentiment_score
            ),
            "summary": func.coalesce(text("EXCLUDED.summary"), m.RedditPost.summary),
            "raw": text("EXCLUDED.raw"),
            "scraped_at": text("EXCLUDED.scraped_at"),
            "updated_at": func.now(),
        }
        stmt = (
            insert(m.RedditPost)
            .values(**values)
            .on_conflict_do_update(index_elements=[m.RedditPost.source_id], set_=set_)
            .returning(m.RedditPost.id)
        )
        post_id = (await self.session.execute(stmt)).scalar_one()

        if record.comments:
            await self._upsert_reddit_comments(post_id, record)
        return post_id

    async def _upsert_reddit_comments(self, post_id: int, record: RedditPostRecord) -> int:
        rows = [
            {
                "comment_id": c.comment_id,
                "post_id": post_id,
                "post_source_id": record.source_id,
                "parent_id": c.parent_id,
                "author": c.author,
                "body": c.body,
                "score": c.score,
                "depth": c.depth,
                "is_submitter": c.is_submitter,
                "created_utc": c.created_utc,
                "permalink": c.permalink,
                "sentiment": c.sentiment.value if c.sentiment else None,
                "sentiment_score": c.sentiment_score,
                "detected_builders": c.detected_builders,
                "detected_projects": c.detected_projects,
                "detected_sectors": c.detected_sectors,
                "topics": c.topics,
                "keywords": c.keywords,
            }
            for c in record.comments
        ]
        if not rows:
            return 0
        stmt = (
            insert(m.RedditCommentRow)
            .values(rows)
            .on_conflict_do_update(
                index_elements=[m.RedditCommentRow.comment_id],
                set_={
                    "score": text("EXCLUDED.score"),
                    "body": func.coalesce(text("EXCLUDED.body"), m.RedditCommentRow.body),
                    "sentiment": func.coalesce(
                        text("EXCLUDED.sentiment"), m.RedditCommentRow.sentiment
                    ),
                    "sentiment_score": func.coalesce(
                        text("EXCLUDED.sentiment_score"), m.RedditCommentRow.sentiment_score
                    ),
                    "detected_builders": text("EXCLUDED.detected_builders"),
                    "detected_projects": text("EXCLUDED.detected_projects"),
                    "detected_sectors": text("EXCLUDED.detected_sectors"),
                    "topics": text("EXCLUDED.topics"),
                    "keywords": text("EXCLUDED.keywords"),
                },
            )
        )
        await self.session.execute(stmt)
        return len(rows)

    # ------------------------------------------------------------------ insights

    async def upsert_market_insight(self, record: MarketInsightRecord) -> int:
        values = {
            "source": record.source.value,
            "source_id": record.source_id,
            "metric": record.metric,
            "city": record.city.value,
            "locality": record.locality,
            "sector": record.sector,
            "property_type": record.property_type.value if record.property_type else None,
            "period_start": record.period_start,
            "period_end": record.period_end,
            "value": record.value,
            "unit": record.unit,
            "change_pct": record.change_pct,
            "sample_size": record.sample_size,
            "source_url": record.source_url,
            "notes": record.notes,
            "raw": _jsonable(record.raw),
            "scraped_at": record.scraped_at,
        }
        stmt = (
            insert(m.MarketInsight)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[m.MarketInsight.source, m.MarketInsight.source_id],
                set_={
                    "value": text("EXCLUDED.value"),
                    "change_pct": text("EXCLUDED.change_pct"),
                    "sample_size": text("EXCLUDED.sample_size"),
                    "scraped_at": text("EXCLUDED.scraped_at"),
                },
            )
            .returning(m.MarketInsight.id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    # ------------------------------------------------------------------ ops

    async def record_run(self, report: dict[str, Any], *, inserted: int = 0, updated: int = 0):
        stmt = insert(m.ScrapeRun).values(
            source=report["source"],
            job=report["job"],
            status=report["status"],
            started_at=report["started_at"],
            finished_at=report["finished_at"],
            duration_s=report["duration_s"],
            discovered=report["discovered"],
            fetched=report["fetched"],
            parsed=report["parsed"],
            inserted=inserted,
            updated=updated,
            skipped_known=report["skipped_known"],
            skipped_robots=report["skipped_robots"],
            errors=report["errors"],
            blocked=report["blocked"],
            details=_jsonable(
                {
                    "error_samples": report.get("error_samples", []),
                    "fetcher_stats": report.get("fetcher_stats", {}),
                }
            ),
        )
        await self.session.execute(stmt)

    async def mark_stale_inactive(self, source: str, *, older_than_days: int = 21) -> int:
        """A listing we have not seen in N days is treated as delisted.

        This is what makes `mv_supply_demand.avg_days_on_market` meaningful.
        """
        stmt = (
            update(m.Property)
            .where(
                m.Property.source == source,
                m.Property.is_active.is_(True),
                m.Property.last_seen_at
                < func.now() - text(f"INTERVAL '{int(older_than_days)} days'"),
            )
            .values(is_active=False, delisted_at=func.now())
        )
        result = await self.session.execute(stmt)
        count = result.rowcount or 0
        if count:
            log.info("repo.marked_delisted", source=source, count=count, days=older_than_days)
        return count

    async def link_duplicate(
        self, canonical_id: int, duplicate_id: int, score: float, reason: str
    ) -> None:
        if canonical_id == duplicate_id:
            return
        await self.session.execute(
            insert(m.PropertyDuplicate)
            .values(
                canonical_id=canonical_id,
                duplicate_id=duplicate_id,
                score=score,
                reason=reason[:500],
            )
            .on_conflict_do_nothing(
                index_elements=[m.PropertyDuplicate.canonical_id, m.PropertyDuplicate.duplicate_id]
            )
        )
        await self.session.execute(
            update(m.Property)
            .where(m.Property.id == duplicate_id)
            .values(canonical_property_id=canonical_id)
        )
        await self.session.execute(
            update(m.Property)
            .where(m.Property.id == canonical_id)
            .values(duplicate_count=m.Property.duplicate_count + 1)
        )

    async def enqueue_enrichment(
        self, entity_type: str, entity_ids: list[int], *, priority: int = 5
    ) -> int:
        if not entity_ids:
            return 0
        rows = [
            {"entity_type": entity_type, "entity_id": eid, "priority": priority}
            for eid in entity_ids
        ]
        await self.session.execute(
            insert(m.EnrichmentQueue)
            .values(rows)
            .on_conflict_do_update(
                index_elements=[m.EnrichmentQueue.entity_type, m.EnrichmentQueue.entity_id],
                set_={"processed_at": None, "priority": text("EXCLUDED.priority")},
            )
        )
        return len(rows)

    async def refresh_market_views(self, *, concurrent: bool = True) -> None:
        """Materialized views cannot refresh inside a transaction block when
        CONCURRENTLY is used, so commit first."""
        await self.session.commit()
        await self.session.execute(text("SELECT refresh_market_views(:c)"), {"c": concurrent})
        await self.session.commit()
        log.info("repo.market_views_refreshed", concurrent=concurrent)

    async def counts(self) -> dict[str, int]:
        rows = await self.session.execute(
            text(
                """
                SELECT 'properties' AS t, COUNT(*) FROM properties
                UNION ALL SELECT 'properties_active', COUNT(*) FROM properties WHERE is_active
                UNION ALL SELECT 'projects', COUNT(*) FROM projects
                UNION ALL SELECT 'builders', COUNT(*) FROM builders
                UNION ALL SELECT 'reddit_posts', COUNT(*) FROM reddit_posts
                UNION ALL SELECT 'reddit_comments', COUNT(*) FROM reddit_comments
                UNION ALL SELECT 'price_history', COUNT(*) FROM price_history
                UNION ALL SELECT 'locations', COUNT(*) FROM locations
                """
            )
        )
        return {row[0]: int(row[1]) for row in rows}


def utcnow() -> datetime:
    return datetime.now(UTC)
