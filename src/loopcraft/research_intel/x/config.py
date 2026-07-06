"""Content configuration and API-token resolution for the X intelligence loop."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from loopcraft.config import safe_source_relpath
from loopcraft.settings import local_override_path


class _ContentModel(BaseModel):
    """Base model for X content-definition data."""

    model_config = ConfigDict(extra="forbid")


class SourcesConfig(_ContentModel):
    """Public X API sources to fetch from."""

    list_ids: list[str] = Field(default_factory=list)
    following_user_ids: list[str] = Field(default_factory=list)
    following_snapshot: Path | None = None
    search_queries: list[str] = Field(default_factory=list)
    author_handles: list[str] = Field(default_factory=list)

    @field_validator("following_snapshot", mode="before")
    @classmethod
    def validate_following_snapshot(cls, value: Any) -> Path | None:
        """Require a safe source-relative snapshot path.

        The snapshot is a declared content asset read by the loop, so an
        absolute or traversing path would introduce an undeclared read outside
        the source/worktree boundary.
        """
        if value in (None, ""):
            return None
        return Path(safe_source_relpath(str(value)))


class FrontierLabsConfig(_ContentModel):
    """Frontier-lab attribution and high-priority author settings."""

    high_priority_handles: set[str] = Field(default_factory=set)
    affiliations: dict[str, set[str]] = Field(default_factory=dict)

    @field_validator("high_priority_handles", mode="before")
    @classmethod
    def clean_high_priority_handles(cls, values: Any) -> set[str]:
        """Normalize configured handles."""
        return {_clean_handle(value) for value in values or []}

    @field_validator("affiliations", mode="before")
    @classmethod
    def clean_affiliations(cls, values: Any) -> dict[str, set[str]]:
        """Normalize affiliation handle maps."""
        return {
            str(name): {_clean_handle(handle) for handle in handles}
            for name, handles in (values or {}).items()
        }


class RankingConfig(_ContentModel):
    """Ranking configuration for fetched posts."""

    max_posts_per_run: int = 100
    top_posts: int = 25
    frontier_lab_bonus: int = 35
    high_priority_author_bonus: int = 25
    recency_bonus_hours: int = 18
    min_score: int = 35
    max_posts_per_author: int = 3
    reply_penalty: int = 20
    require_ai_context: bool = True
    topic_query: str = ""
    discovery_context_terms: set[str] = Field(default_factory=set)
    ai_context_keywords: set[str] = Field(default_factory=set)
    negative_keywords: dict[str, int] = Field(default_factory=dict)
    keywords: dict[str, int] = Field(default_factory=dict)

    @field_validator("ai_context_keywords", mode="before")
    @classmethod
    def lower_context_keywords(cls, values: Any) -> set[str]:
        """Normalize context keywords to lowercase."""
        return {str(value).lower() for value in values or []}

    @field_validator("discovery_context_terms", mode="before")
    @classmethod
    def lower_discovery_terms(cls, values: Any) -> set[str]:
        """Normalize profile discovery terms to lowercase."""
        return {str(value).lower() for value in values or []}

    @field_validator("negative_keywords", "keywords", mode="before")
    @classmethod
    def clean_keyword_weights(cls, values: Any) -> dict[str, int]:
        """Normalize weighted keyword dictionaries."""
        return {str(term).lower(): int(weight) for term, weight in (values or {}).items()}


class OutputPaths(_ContentModel):
    """Fixed ledger output paths for the X workflow.

    These mirror the ``loops/x-intel.yaml`` I/O contract and are fixed in code —
    they are deliberately *not* part of the YAML content-definition surface, so
    no public or gitignored local config can redirect durable writes away from
    the manifest's declared outputs.
    """

    seen_path: Path = Path("state/research/x/seen.json")
    posts_path: Path = Path("state/research/x/posts.jsonl")
    source_state_path: Path = Path("state/research/x/source-state.json")
    digest_dir: Path = Path("state/research/x/digests")
    history_dir: Path = Path("state/research/x/history")
    latest_markdown: Path = Path("state/research/x/latest.md")
    latest_json: Path = Path("state/research/x/latest.json")
    follow_candidates_dir: Path = Path("state/research/x/follow-candidates")


class XApiTokens(_ContentModel):
    """Resolved X API token values from the environment."""

    bearer_token: str | None = None
    oauth2_access_token: str | None = None

    @classmethod
    def from_env(cls) -> XApiTokens:
        """Resolve X API tokens from environment variables."""
        return cls(
            bearer_token=os.environ.get("X_API_BEARER_TOKEN"),
            oauth2_access_token=os.environ.get("X_API_OAUTH2_ACCESS_TOKEN"),
        )

    def token(self, *, require_user_context: bool = False) -> str | None:
        """Return the token appropriate for an X API operation.

        Args:
            require_user_context: Whether app-only bearer auth is insufficient.

        Returns:
            OAuth2 access token when user context is required, otherwise OAuth2
            access token if present or app bearer token as a fallback.
        """
        if require_user_context:
            return self.oauth2_access_token
        return self.oauth2_access_token or self.bearer_token


class IntelConfig(_ContentModel):
    """Complete X content definition (sources, frontier labs, ranking only).

    Durable output locations are not content: they belong to the manifest's
    I/O contract and are fixed in :class:`OutputPaths`.
    """

    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    frontier_labs: FrontierLabsConfig = Field(default_factory=FrontierLabsConfig)
    ranking: RankingConfig = Field(default_factory=RankingConfig)

    @classmethod
    def load(cls, path: Path) -> IntelConfig:
        """Load a YAML content-definition file, preferring a ``.local.`` override.

        If a gitignored ``<name>.local.yaml`` sibling exists next to ``path`` it
        is loaded instead, so private overrides work whether the CLI is invoked
        directly or via a Makefile target.
        """
        effective = local_override_path(path)
        raw = yaml.safe_load(effective.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> IntelConfig:
        """Build content config from a raw mapping.

        Raises:
            ValueError: If the mapping tries to set ``output`` paths — durable
                outputs are fixed by the loop manifest contract, not content.
        """
        if isinstance(raw, dict) and "output" in raw:
            raise ValueError(
                "content config must not override 'output' paths; durable outputs "
                "are fixed by the loop manifest (loops/x-intel.yaml)"
            )
        return cls.model_validate(raw)


def _clean_handle(value: str) -> str:
    """Normalize an X handle to lowercase without a leading ``@``."""
    return str(value).lower().lstrip("@")
