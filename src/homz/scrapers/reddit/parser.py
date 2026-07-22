"""Reddit JSON → normalized records.

Reddit's official API returns well-typed JSON, so there is no HTML parsing
here — just shape mapping plus the cheap rule-based entity/topic detection that
runs on every post before the (optional, paid) LLM pass.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from homz.common.enums import City, Source
from homz.common.geo import detect_city
from homz.common.parsing import clean_text
from homz.common.schema import RedditComment, RedditPostRecord

REDDIT_BASE = "https://www.reddit.com"

# A post has to look like real-estate talk before we store it — r/gurgaon is a
# general city sub and most of it is traffic complaints and restaurant recs.
_RELEVANCE_TERMS = (
    r"\bproperty\b", r"\bflat\b", r"\bapartment\b", r"\bbuilder\b", r"\bbuilders\b",
    r"\brera\b", r"\bpossession\b", r"\bsociety\b", r"\bbroker\b", r"\brent(al)?\b",
    r"\blandlord\b", r"\btenant\b", r"\bsector\s*\d+", r"\bbhk\b", r"\bcarpet area\b",
    r"\bhome ?loan\b", r"\bstamp duty\b", r"\bregistry\b", r"\bregistration\b",
    r"\bmaintenance\b", r"\bdlf\b", r"\bm3m\b", r"\bgodrej\b", r"\bsobha\b",
    r"\bsignaturre?\b", r"\bemaar\b", r"\bexpressway\b", r"\bproject\b",
    r"\binvest(ment|ing)?\b", r"\bresale\b", r"\bplot\b", r"\bvilla\b",
    r"\bfloor\b", r"\breal ?estate\b", r"\bhousing\b", r"\bsq\.? ?ft\b",
)
_RELEVANCE_RE = re.compile("|".join(_RELEVANCE_TERMS), re.I)


def relevance_score(title: str, body: str | None) -> float:
    """0-100. Weighted term hits, title counting double.

    Deliberately generous: a false positive costs one row, a false negative
    loses a builder-fraud thread we can never recover.
    """
    title_hits = len(_RELEVANCE_RE.findall(title or ""))
    body_hits = len(_RELEVANCE_RE.findall((body or "")[:5000]))
    raw = title_hits * 2 + body_hits
    return float(min(100, raw * 12))


def is_relevant(title: str, body: str | None, *, threshold: float = 12.0) -> bool:
    return relevance_score(title, body) >= threshold


def _to_datetime(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (ValueError, OSError, TypeError):
        return None


def parse_post(payload: dict[str, Any], *, enrich: bool = True) -> RedditPostRecord | None:
    """One `t3` listing child → RedditPostRecord."""
    data = payload.get("data", payload)
    source_id = data.get("id")
    if not source_id:
        return None

    title = clean_text(data.get("title")) or ""
    body = clean_text(data.get("selftext")) or None
    permalink = data.get("permalink") or f"/comments/{source_id}/"

    record = RedditPostRecord(
        source=Source.REDDIT,
        source_id=str(source_id),
        subreddit=str(data.get("subreddit") or "unknown").lower(),
        url=data.get("url") or f"{REDDIT_BASE}{permalink}",
        permalink=f"{REDDIT_BASE}{permalink}",
        title=title,
        body=body,
        author=_author(data),
        created_utc=_to_datetime(data.get("created_utc")),
        score=int(data.get("score") or 0),
        upvote_ratio=_ratio(data.get("upvote_ratio")),
        num_comments=int(data.get("num_comments") or 0),
        flair=clean_text(data.get("link_flair_text")),
        is_self=bool(data.get("is_self", True)),
        over_18=bool(data.get("over_18", False)),
        relevance_score=relevance_score(title, body),
        raw={
            "domain": data.get("domain"),
            "num_crossposts": data.get("num_crossposts"),
            "total_awards_received": data.get("total_awards_received"),
            "stickied": data.get("stickied"),
        },
    )
    if enrich:
        apply_rule_extraction(record)
    return record


def parse_comments(
    payload: Any, post_source_id: str, *, limit: int = 50, min_score: int = 1
) -> list[RedditComment]:
    """Flatten the `/comments/{id}` tree.

    Reddit returns `[post_listing, comment_listing]`; comments nest via
    `replies`. Low-scoring and deleted comments are dropped — they add noise to
    sentiment without adding signal.
    """
    listings = payload if isinstance(payload, list) else [payload]
    comments: list[RedditComment] = []

    def walk(children: list[dict[str, Any]], depth: int) -> None:
        for child in children:
            if len(comments) >= limit:
                return
            if child.get("kind") != "t1":
                continue
            data = child.get("data") or {}
            body = clean_text(data.get("body"))
            if not body or body in {"[deleted]", "[removed]"}:
                pass
            elif int(data.get("score") or 0) >= min_score:
                comments.append(
                    RedditComment(
                        comment_id=str(data.get("id")),
                        post_id=post_source_id,
                        parent_id=data.get("parent_id"),
                        author=_author(data),
                        body=body[:10_000],
                        score=int(data.get("score") or 0),
                        created_utc=_to_datetime(data.get("created_utc")),
                        permalink=(
                            f"{REDDIT_BASE}{data['permalink']}" if data.get("permalink") else None
                        ),
                        depth=depth,
                        is_submitter=bool(data.get("is_submitter", False)),
                    )
                )
            replies = data.get("replies")
            if isinstance(replies, dict) and depth < 4:
                walk((replies.get("data") or {}).get("children") or [], depth + 1)

    for listing in listings:
        if not isinstance(listing, dict):
            continue
        children = (listing.get("data") or {}).get("children") or []
        walk(children, 0)

    comments.sort(key=lambda c: c.score, reverse=True)
    return comments[:limit]


def apply_rule_extraction(record: RedditPostRecord) -> RedditPostRecord:
    """Cheap deterministic pass: builders, projects, sectors, city, topics.

    Runs on every record at scrape time so the data is queryable even when the
    LLM enrichment stage is disabled or backlogged.
    """
    from homz.enrichment.extractors import extract_entities, extract_topics

    text = "\n".join(filter(None, [record.title, record.body]))
    comment_text = "\n".join(c.body or "" for c in record.comments[:20])
    combined = f"{text}\n{comment_text}"

    entities = extract_entities(combined)
    record.detected_builders = entities.builders
    record.detected_projects = entities.projects
    record.detected_sectors = entities.sectors
    # A post in r/gurgaon that never names the city is still about Gurgaon.
    # Without this fallback most subreddit posts land in `unknown` and drop out
    # of every city-filtered query.
    record.detected_city = (
        entities.city if entities.city != City.UNKNOWN else detect_city(record.subreddit)
    )
    record.topics = extract_topics(combined)
    record.keywords = entities.keywords

    for comment in record.comments:
        if not comment.body:
            continue
        comment_entities = extract_entities(comment.body)
        comment.detected_builders = comment_entities.builders
        comment.detected_projects = comment_entities.projects
        comment.detected_sectors = comment_entities.sectors
        comment.detected_city = comment_entities.city
        comment.topics = extract_topics(comment.body)
    return record


def _author(data: dict[str, Any]) -> str | None:
    author = data.get("author")
    if not author or author in {"[deleted]", "AutoModerator"}:
        return None
    return str(author)


def _ratio(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None
