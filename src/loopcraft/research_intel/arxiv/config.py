"""Content configuration for the arXiv intelligence loop (sources, ranking)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    """Fixed ledger output paths for direct arXiv CLI runs."""

    seen_path: Path = Path("state/research/arxiv/seen.json")
    papers_path: Path = Path("state/research/arxiv/papers.jsonl")
    digest_dir: Path = Path("state/research/arxiv/digests")
    history_dir: Path = Path("state/research/arxiv/history")
    latest_markdown: Path = Path("state/research/arxiv/latest.md")
    latest_json: Path = Path("state/research/arxiv/latest.json")


class ArxivIntelConfig(_ContentModel):
    """Complete arXiv content definition plus fixed ledger output defaults."""

    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    ranking: RankingConfig = Field(default_factory=RankingConfig)
    output: OutputPaths = Field(default_factory=OutputPaths)

    @classmethod
    def load(cls, path: Path) -> ArxivIntelConfig:
        """Load a YAML content-definition file."""
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ArxivIntelConfig:
        """Build content config from a raw mapping."""
        return cls.model_validate(raw)

