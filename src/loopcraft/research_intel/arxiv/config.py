"""Content configuration for the arXiv intelligence loop (sources, ranking)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from loopcraft.settings import local_override_path


class _ContentModel(BaseModel):
    """Base model for arXiv content-definition data."""

    model_config = ConfigDict(extra="forbid")


class SourcesConfig(_ContentModel):
    """arXiv source categories and search terms."""

    categories: list[str] = Field(default_factory=lambda: ["cs.AI", "cs.CL", "cs.LG", "stat.ML"])
    search_terms: list[str] = Field(default_factory=list)


class RankingConfig(_ContentModel):
    """Ranking knobs for arXiv papers."""

    max_results: int = 150
    top_papers: int = 10
    min_score: int = 25
    recency_bonus_days: int = 3
    keywords: dict[str, int] = Field(default_factory=dict)
    negative_keywords: dict[str, int] = Field(default_factory=dict)

    @field_validator("keywords", "negative_keywords", mode="before")
    @classmethod
    def clean_keyword_weights(cls, values: Any) -> dict[str, int]:
        """Normalize weighted keyword dictionaries."""
        return {str(term).lower(): int(weight) for term, weight in (values or {}).items()}


class OutputPaths(_ContentModel):
    """Fixed ledger output paths for the arXiv workflow.

    These mirror the ``loops/arxiv-intel.yaml`` I/O contract and are fixed in
    code — they are deliberately *not* part of the YAML content-definition
    surface, so no public or gitignored local config can redirect durable
    writes away from the manifest's declared outputs.
    """

    seen_path: Path = Path("state/research/arxiv/seen.json")
    papers_path: Path = Path("state/research/arxiv/papers.jsonl")
    digest_dir: Path = Path("state/research/arxiv/digests")
    history_dir: Path = Path("state/research/arxiv/history")
    latest_markdown: Path = Path("state/research/arxiv/latest.md")
    latest_json: Path = Path("state/research/arxiv/latest.json")


class ArxivIntelConfig(_ContentModel):
    """Complete arXiv content definition (sources and ranking only).

    Durable output locations are not content: they belong to the manifest's
    I/O contract and are fixed in :class:`OutputPaths`.
    """

    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    ranking: RankingConfig = Field(default_factory=RankingConfig)

    @classmethod
    def load(cls, path: Path) -> ArxivIntelConfig:
        """Load a YAML content-definition file, preferring a ``.local.`` override.

        If a gitignored ``<name>.local.yaml`` sibling exists next to ``path`` it
        is loaded instead, so private overrides work whether the CLI is invoked
        directly or via a Makefile target.
        """
        effective = local_override_path(path)
        raw = yaml.safe_load(effective.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ArxivIntelConfig:
        """Build content config from a raw mapping.

        Raises:
            ValueError: If the mapping tries to set ``output`` paths — durable
                outputs are fixed by the loop manifest contract, not content.
        """
        if isinstance(raw, dict) and "output" in raw:
            raise ValueError(
                "content config must not override 'output' paths; durable outputs "
                "are fixed by the loop manifest (loops/arxiv-intel.yaml)"
            )
        return cls.model_validate(raw)

