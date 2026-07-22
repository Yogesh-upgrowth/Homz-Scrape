"""Deduplication.

Three layers, cheapest first:

1. **Exact identity** — `(source, source_id)`. Enforced by a unique index; the
   repository upserts on it, so re-scraping the same listing updates rather
   than duplicates.
2. **Content hash** — sha256 of the volatile business fields. Unchanged hash
   means nothing worth writing changed, so ETL skips the row entirely. This is
   what makes incremental runs cheap.
3. **Cross-source near-duplicate** — the same flat listed on MagicBricks and
   Housing. Blocked by `(city, sector, config)` then scored on title simhash +
   area/price proximity, so we never compare all-pairs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from homz.common.schema import PropertyRecord

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_HASH_BITS = 64
_MASK = (1 << _HASH_BITS) - 1


def tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def simhash(text: str | None, *, bits: int = _HASH_BITS) -> int:
    """Charikar simhash over word tokens — near-identical titles hash close."""
    tokens = tokenize(text)
    if not tokens:
        return 0
    weights = defaultdict(int)
    for token in tokens:
        weights[token] += 1

    vector = [0] * bits
    for token, weight in weights.items():
        h = _stable_hash(token)
        for i in range(bits):
            vector[i] += weight if (h >> i) & 1 else -weight

    fingerprint = 0
    for i in range(bits):
        if vector[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def _stable_hash(token: str) -> int:
    """FNV-1a — deterministic across processes, unlike Python's hash()."""
    h = 0xCBF29CE484222325
    for byte in token.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & _MASK
    return h


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# near-duplicate detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateMatch:
    left: str  # natural_key
    right: str
    score: float
    reason: str


def blocking_key(record: PropertyRecord) -> str:
    """Records only get compared within the same block. Keeps this O(n) in
    practice instead of O(n²) across a 200k-row table."""
    return "|".join(
        [
            record.location.city.value,
            (record.location.sector or record.location.locality or "?").lower(),
            (record.configuration or "?").lower().replace(" ", ""),
            record.listing_type.value,
        ]
    )


def _relative_close(a: float | Decimal | None, b: float | Decimal | None, tolerance: float) -> bool:
    if a is None or b is None:
        return False
    fa, fb = float(a), float(b)
    if fa <= 0 or fb <= 0:
        return False
    return abs(fa - fb) / max(fa, fb) <= tolerance


def similarity(left: PropertyRecord, right: PropertyRecord) -> tuple[float, str]:
    """Weighted similarity in [0, 1] plus a human-readable reason."""
    if left.source == right.source and left.source_id == right.source_id:
        return 1.0, "same source id"

    reasons: list[str] = []
    score = 0.0

    # Project/society name agreement is the strongest signal available.
    left_name = (left.project_name or left.society_name or "").lower().strip()
    right_name = (right.project_name or right.society_name or "").lower().strip()
    if left_name and right_name:
        name_sim = jaccard(set(tokenize(left_name)), set(tokenize(right_name)))
        score += 0.35 * name_sim
        if name_sim > 0.6:
            reasons.append(f"project~{name_sim:.2f}")

    if _relative_close(left.area_sqft, right.area_sqft, 0.03):
        score += 0.20
        reasons.append("area±3%")

    price_left = left.price or left.rent_monthly
    price_right = right.price or right.rent_monthly
    if _relative_close(price_left, price_right, 0.02):
        score += 0.20
        reasons.append("price±2%")

    if left.configuration and left.configuration == right.configuration:
        score += 0.10
        reasons.append("same config")

    if (
        left.location.sector
        and left.location.sector == right.location.sector
        and left.location.city == right.location.city
    ):
        score += 0.05
        reasons.append("same sector")

    title_distance = hamming(simhash(left.title), simhash(right.title))
    if title_distance <= 8:
        score += 0.10 * (1 - title_distance / 8)
        reasons.append(f"title_hd={title_distance}")

    # A shared image URL is conclusive — portals syndicate the same photos.
    left_images = {i.url.split("?")[0] for i in left.images}
    right_images = {i.url.split("?")[0] for i in right.images}
    if left_images & right_images:
        score = max(score, 0.95)
        reasons.append("shared image")

    return min(score, 1.0), ", ".join(reasons) or "weak"


def find_duplicates(
    records: list[PropertyRecord], *, threshold: float = 0.75
) -> list[DuplicateMatch]:
    """Blocked pairwise comparison. Returns matches above `threshold`."""
    blocks: dict[str, list[PropertyRecord]] = defaultdict(list)
    for record in records:
        blocks[blocking_key(record)].append(record)

    matches: list[DuplicateMatch] = []
    for bucket in blocks.values():
        if len(bucket) < 2:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                score, reason = similarity(bucket[i], bucket[j])
                if score >= threshold:
                    matches.append(
                        DuplicateMatch(
                            left=bucket[i].natural_key,
                            right=bucket[j].natural_key,
                            score=round(score, 3),
                            reason=reason,
                        )
                    )
    return matches


def choose_canonical(records: list[PropertyRecord]) -> PropertyRecord:
    """Pick the richest record from a duplicate cluster.

    Completeness beats recency — a fully-populated week-old record is more
    useful than a bare listing scraped an hour ago.
    """

    def completeness(record: PropertyRecord) -> tuple[int, float]:
        filled = sum(
            1
            for value in (
                record.title,
                record.price or record.rent_monthly,
                record.area_sqft,
                record.configuration,
                record.project_name,
                record.builder_name,
                record.description,
                record.rera_number,
                record.location.sector,
                record.location.geo,
            )
            if value
        )
        filled += min(len(record.images), 10) // 2
        filled += min(len(record.amenities), 20) // 5
        return filled, record.scraped_at.timestamp()

    return max(records, key=completeness)
