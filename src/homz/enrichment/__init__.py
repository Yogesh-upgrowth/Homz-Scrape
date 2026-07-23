"""Enrichment package.

Tier 1 (rule extraction) and Tier 2 (deterministic scoring) have no third-party
dependencies beyond the standard library. Tier 3 needs the `anthropic` SDK, so
`LLMClient` and friends are exported lazily — importing `homz.enrichment` for
scoring must not require the SDK to be installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homz.enrichment.extractors import (
    ExtractedEntities,
    canonical_builder,
    extract_builders,
    extract_entities,
    extract_keywords,
    extract_projects,
    extract_sectors,
    extract_topics,
    lexicon_sentiment,
)
from homz.enrichment.scoring import (
    Score,
    builder_trust_score,
    investment_score,
    location_score,
    risk_score,
)

if TYPE_CHECKING:  # pragma: no cover
    pass

_LAZY: dict[str, str] = {
    "ENRICHMENT_VERSION": "homz.enrichment.llm",
    "LLMClient": "homz.enrichment.llm",
    "LLMRequest": "homz.enrichment.llm",
    "LLMResult": "homz.enrichment.llm",
    "estimate_cost": "homz.enrichment.llm",
    "EnrichmentPipeline": "homz.enrichment.pipeline",
    "EnrichmentReport": "homz.enrichment.pipeline",
}


def __getattr__(name: str) -> Any:
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "ENRICHMENT_VERSION",
    "EnrichmentPipeline",
    "EnrichmentReport",
    "ExtractedEntities",
    "LLMClient",
    "LLMRequest",
    "LLMResult",
    "Score",
    "builder_trust_score",
    "canonical_builder",
    "estimate_cost",
    "extract_builders",
    "extract_entities",
    "extract_keywords",
    "extract_projects",
    "extract_sectors",
    "extract_topics",
    "investment_score",
    "lexicon_sentiment",
    "location_score",
    "risk_score",
]
