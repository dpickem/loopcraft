from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
class OutputConfig:
    seen_path: Path
    papers_path: Path
    digest_dir: Path
    latest_markdown: Path
    latest_json: Path


@dataclass(frozen=True)
class ArxivIntelConfig:
    sources: SourcesConfig
    ranking: RankingConfig
    output: OutputConfig

    @classmethod
    def load(cls, path: Path) -> "ArxivIntelConfig":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ArxivIntelConfig":
        sources = raw.get("sources", {})
        ranking = raw.get("ranking", {})
        output = raw.get("output", {})
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
            output=OutputConfig(
                seen_path=Path(output.get("seen_path", "state/research/arxiv/seen.json")),
                papers_path=Path(output.get("papers_path", "state/research/arxiv/papers.jsonl")),
                digest_dir=Path(output.get("digest_dir", "state/research/arxiv/digests")),
                latest_markdown=Path(output.get("latest_markdown", "state/research/arxiv/latest.md")),
                latest_json=Path(output.get("latest_json", "state/research/arxiv/latest.json")),
            ),
        )

