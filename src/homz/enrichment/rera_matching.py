"""Fuzzy-match a property/project's (project_name, builder_name) against the
scraped `hrera_registry` to find its real RERA number.

No exact key exists between our documents and the registry — project names
are typed slightly differently ("M3M Antalya" vs "M3M ANTALYA HILLS PHASE-1"),
so this scores candidates and leaves the write-or-flag decision to the caller
(`EnrichmentPipeline.attach_rera_numbers`), which only auto-writes above
`HIGH` and stores everything in `[LOW, HIGH)` as a review candidate — a wrong
RERA number is a legal-accuracy problem, not just a data-quality one, so
"unsure" must never look identical to "confident" here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homz.common.dedupe import jaccard, tokenize
from homz.common.parsing import normalize_name

# What an actual HRERA "Project ID" looks like (e.g. "RERA-GRG-1789-2024").
# A stored rera_number that doesn't match this shape is not necessarily wrong
# — older interim-RERA-era filings used other formats — but it can't be
# confirmed against this registry either, and several confirmed-wrong values
# (a bare "367", a certificate-number fragment "GGM/2018/05") were found
# stored where a real Project ID belonged. See EnrichmentPipeline.verify_existing_rera_numbers.
RERA_NUMBER_SHAPE_RE = re.compile(r"^RERA-[A-Z]+-\d+-\d{4}$")

# Score >= HIGH: auto-write as the live rera_number.
# LOW <= score < HIGH: store as a review candidate only.
# score < LOW: no match at all.
HIGH = 0.72
LOW = 0.45

# Project name carries more signal than builder name: builder names are
# noisier (a listing's "builder_name" is often inferred, not scraped from an
# official source) and many builders share generic tokens after
# normalize_name() strips corporate suffixes.
_PROJECT_WEIGHT = 0.7
_BUILDER_WEIGHT = 0.3


@dataclass(frozen=True)
class ReraMatch:
    candidate: dict[str, Any]
    score: float
    # False when either side had no builder name to compare, so `score` is
    # project-name-only. Verified live 2026-08-31: a builder-less "The
    # Residences" (no builder_name at all) scored a perfect 1.0 against the
    # registry's "THE ESTATE RESIDENCES" (Anant Raj Limited) — an unrelated
    # project — because normalize_name() strips "estate" as a generic
    # real-estate word, collapsing two different projects to the same
    # normalized name. Project-name-only similarity, however high, is not
    # enough to safely auto-write a legal RERA number; the caller must treat
    # an uncorroborated match as review-only regardless of score.
    builder_corroborated: bool


def _name_score(a: str | None, b: str | None) -> float:
    ta, tb = set(tokenize(a or "")), set(tokenize(b or ""))
    return jaccard(ta, tb)


def score_candidate(
    project_name: str | None, builder_name: str | None, candidate: dict[str, Any]
) -> tuple[float, bool]:
    """Returns (score, builder_corroborated)."""
    project_sim = _name_score(normalize_name(project_name), candidate.get("project_name_normalized"))
    if not builder_name or not candidate.get("builder_name_normalized"):
        return project_sim, False
    builder_sim = _name_score(normalize_name(builder_name), candidate.get("builder_name_normalized"))
    return _PROJECT_WEIGHT * project_sim + _BUILDER_WEIGHT * builder_sim, True


def match_hrera(
    project_name: str | None,
    builder_name: str | None,
    candidates: list[dict[str, Any]],
) -> ReraMatch | None:
    """Best-scoring registry row for this project, or None if nothing scores
    above `LOW` at all (candidates should already be pre-filtered to the
    right district — this does not itself filter by location)."""
    if not project_name or not candidates:
        return None

    best: ReraMatch | None = None
    for candidate in candidates:
        score, corroborated = score_candidate(project_name, builder_name, candidate)
        if best is None or score > best.score:
            best = ReraMatch(candidate=candidate, score=score, builder_corroborated=corroborated)

    if best is None or best.score < LOW:
        return None
    return best


# ---------------------------------------------------------------------------
# display status — consumed by homz.services.feed / listings_feed to build
# the site's "lapsed/expired" badge without the frontend needing to know
# HRERA's data shapes or do its own date math.
# ---------------------------------------------------------------------------

NOT_REGISTERED = "not_registered"  # no rera_number at all
UNVERIFIED = "unverified"  # a rera_number exists but doesn't look like a real Project ID
LAPSED = "lapsed"  # confirmed interim/expired against the registry
ACTIVE = "active"  # real Project ID shape, and nothing says it's lapsed


def rera_badge_status(
    rera_number: str | None,
    *,
    valid_upto: datetime | None = None,
    registered_with: str | None = None,
) -> str:
    """One of NOT_REGISTERED / UNVERIFIED / LAPSED / ACTIVE.

    `valid_upto`/`registered_with` only exist on documents this pipeline has
    actually cross-checked against the HRERA registry (see
    EnrichmentPipeline.attach_rera_numbers / verify_existing_rera_numbers) —
    absent for everything else, including correct-looking pre-existing
    numbers (e.g. Sobha Altus) that were never run through matching because
    they didn't need to be. Missing validity info is not itself a red flag;
    a non-standard-shaped number with no corroborating registry data is
    UNVERIFIED (a bare RERA-shaped id with no data behind it still reads as
    ACTIVE), so this degrades gracefully rather than needing every document
    to have been freshly re-verified to render sensibly.

    Verified live 2026-08-31: Ireo Skyon carries a real, correctly-shaped
    Project ID (RERA-GRG-1789-2024) whose only registry record is a 2017
    INTERIM RERA filing that lapsed in 2018 — a bare "active-looking" number
    without this check would misrepresent it as currently registered.
    """
    if not rera_number:
        return NOT_REGISTERED
    if registered_with and "INTERIM" in registered_with.upper():
        return LAPSED
    if valid_upto is not None:
        # CODEC_OPTIONS (db/codecs.py) reads Mongo dates back tz-aware, but
        # accept a naive datetime too rather than raising on comparison.
        now = datetime.now(UTC) if valid_upto.tzinfo else datetime.now()
        if valid_upto < now:
            return LAPSED
    if RERA_NUMBER_SHAPE_RE.match(rera_number):
        return ACTIVE
    return UNVERIFIED
