from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SourcesConfig:
    categories: list[str]
    search_terms: list[str]


@dataclass(frozen=True)
class RankingConfig:
    max_results: int
    top_papers: int
    min_score: int
    recency_bonus_days: int
    keywords: dict[str, int]
    negative_keywords: dict[str, int]


@dataclass(frozen=True)
class OutputPaths:
    seen_path: Path
    papers_path: Path
    digest_dir: Path
    history_dir: Path
    latest_markdown: Path
    latest_json: Path


@dataclass(frozen=True)
class ArxivIntelConfig:
    sources: SourcesConfig
    ranking: RankingConfig
    output: OutputPaths = OutputPaths(
        seen_path=Path("state/research/arxiv/seen.json"),
        papers_path=Path("state/research/arxiv/papers.jsonl"),
        digest_dir=Path("state/research/arxiv/digests"),
        history_dir=Path("state/research/arxiv/history"),
        latest_markdown=Path("state/research/arxiv/latest.md"),
        latest_json=Path("state/research/arxiv/latest.json"),
    )

    @classmethod
    def load(cls, path: Path) -> "ArxivIntelConfig":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ArxivIntelConfig":
        sources = raw.get("sources", {})
        ranking = raw.get("ranking", {})
        return cls(
            sources=SourcesConfig(
                categories=[str(value) for value in sources.get("categories", ["cs.AI", "cs.CL", "cs.LG", "stat.ML"])],
                search_terms=[str(value) for value in sources.get("search_terms", [])],
            ),
            ranking=RankingConfig(
                max_results=int(ranking.get("max_results", 150)),
                top_papers=int(ranking.get("top_papers", 10)),
                min_score=int(ranking.get("min_score", 25)),
                recency_bonus_days=int(ranking.get("recency_bonus_days", 3)),
                keywords={str(term).lower(): int(weight) for term, weight in ranking.get("keywords", {}).items()},
                negative_keywords={
                    str(term).lower(): int(weight) for term, weight in ranking.get("negative_keywords", {}).items()
                },
            ),
        )

