from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourcesConfig:
    list_ids: list[str]
    following_user_ids: list[str]
    following_snapshot: Path | None
    search_queries: list[str]
    author_handles: list[str]


@dataclass(frozen=True)
class FrontierLabsConfig:
    high_priority_handles: set[str]
    affiliations: dict[str, set[str]]


@dataclass(frozen=True)
class RankingConfig:
    max_posts_per_run: int
    top_posts: int
    frontier_lab_bonus: int
    high_priority_author_bonus: int
    recency_bonus_hours: int
    min_score: int
    max_posts_per_author: int
    reply_penalty: int
    require_ai_context: bool
    ai_context_keywords: set[str]
    negative_keywords: dict[str, int]
    keywords: dict[str, int]


@dataclass(frozen=True)
class OutputConfig:
    state_db: Path
    digest_dir: Path


@dataclass(frozen=True)
class IntelConfig:
    sources: SourcesConfig
    frontier_labs: FrontierLabsConfig
    ranking: RankingConfig
    output: OutputConfig

    @classmethod
    def load(cls, path: Path) -> "IntelConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IntelConfig":
        sources = raw.get("sources", {})
        labs = raw.get("frontier_labs", {})
        ranking = raw.get("ranking", {})
        output = raw.get("output", {})

        return cls(
            sources=SourcesConfig(
                list_ids=[str(value) for value in sources.get("list_ids", [])],
                following_user_ids=[str(value) for value in sources.get("following_user_ids", [])],
                following_snapshot=Path(sources["following_snapshot"]) if sources.get("following_snapshot") else None,
                search_queries=[str(value) for value in sources.get("search_queries", [])],
                author_handles=[str(value) for value in sources.get("author_handles", [])],
            ),
            frontier_labs=FrontierLabsConfig(
                high_priority_handles={_clean_handle(value) for value in labs.get("high_priority_handles", [])},
                affiliations={
                    str(name): {_clean_handle(handle) for handle in handles}
                    for name, handles in labs.get("affiliations", {}).items()
                },
            ),
            ranking=RankingConfig(
                max_posts_per_run=int(ranking.get("max_posts_per_run", 100)),
                top_posts=int(ranking.get("top_posts", 25)),
                frontier_lab_bonus=int(ranking.get("frontier_lab_bonus", 35)),
                high_priority_author_bonus=int(ranking.get("high_priority_author_bonus", 25)),
                recency_bonus_hours=int(ranking.get("recency_bonus_hours", 18)),
                min_score=int(ranking.get("min_score", 35)),
                max_posts_per_author=int(ranking.get("max_posts_per_author", 3)),
                reply_penalty=int(ranking.get("reply_penalty", 20)),
                require_ai_context=bool(ranking.get("require_ai_context", True)),
                ai_context_keywords={
                    str(value).lower()
                    for value in ranking.get(
                        "ai_context_keywords",
                        [
                            "ai",
                            "llm",
                            "model",
                            "models",
                            "agent",
                            "agents",
                            "eval",
                            "evals",
                            "tool use",
                            "harness",
                            "loopcraft",
                            "loop engineering",
                            "reasoning",
                            "post-training",
                        ],
                    )
                },
                negative_keywords={
                    str(term).lower(): int(weight) for term, weight in ranking.get("negative_keywords", {}).items()
                },
                keywords={str(term).lower(): int(weight) for term, weight in ranking.get("keywords", {}).items()},
            ),
            output=OutputConfig(
                state_db=Path(output.get("state_db", "var/x_intel/state.sqlite3")),
                digest_dir=Path(output.get("digest_dir", "var/x_intel/digests")),
            ),
        )


def _clean_handle(value: str) -> str:
    return str(value).lower().lstrip("@")
