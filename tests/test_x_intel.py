from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from loopcraft.env import load_dotenv
from loopcraft.x_intel.config import IntelConfig
from loopcraft.x_intel.digest import render_digest
from loopcraft.x_intel.follow_discovery import discover_candidates, render_follow_candidates
from loopcraft.x_intel.ranking import rank_posts, score_post


def _config() -> IntelConfig:
    return IntelConfig.from_dict(
        {
            "frontier_labs": {
                "high_priority_handles": ["important"],
                "affiliations": {"openai": ["frontier"]},
            },
            "ranking": {
                "frontier_lab_bonus": 35,
                "high_priority_author_bonus": 25,
                "recency_bonus_hours": 24,
                "keywords": {"loopcraft": 40, "evals": 10},
            },
        }
    )


def test_score_post_weights_frontier_lab_and_keywords() -> None:
    post = {
        "id": "10",
        "text": "Loopcraft evals for agent harnesses",
        "created_at": "2026-06-15T12:00:00Z",
        "author": {"username": "frontier", "name": "Frontier Person"},
        "public_metrics": {"like_count": 250, "retweet_count": 50, "reply_count": 25},
    }

    scored = score_post(post, _config(), now=datetime(2026, 6, 15, 13, 0, tzinfo=UTC))

    assert scored["frontier_lab"] == "openai"
    assert scored["score"] >= 100
    assert "loopcraft" in scored["score_reasons"]


def test_rank_posts_dedupes_by_id() -> None:
    posts = [
        {"id": "1", "text": "boring", "author": {"username": "nobody"}},
        {"id": "1", "text": "loopcraft", "author": {"username": "nobody"}},
        {"id": "2", "text": "evals", "author": {"username": "frontier"}},
    ]

    ranked = rank_posts(posts, _config())

    assert len(ranked) == 2
    assert ranked[0]["id"] == "2"


def test_render_digest_includes_frontier_section() -> None:
    markdown = render_digest(
        [
            {
                "id": "10",
                "text": "Loopcraft evals",
                "created_at": "2026-06-15T12:00:00Z",
                "author": {"username": "frontier", "name": "Frontier Person"},
                "frontier_lab": "openai",
                "score": 100,
            }
        ],
        raw_posts=[],
        errors=[],
        generated_at=datetime(2026, 6, 15, tzinfo=UTC),
    )

    assert "Frontier-Lab Highlights" in markdown
    assert "https://x.com/frontier/status/10" in markdown


def test_render_digest_includes_expanded_external_links() -> None:
    markdown = render_digest(
        [
            {
                "id": "11",
                "text": "Read this https://t.co/example",
                "created_at": "2026-06-15T12:00:00Z",
                "author": {"username": "frontier", "name": "Frontier Person"},
                "frontier_lab": "openai",
                "score": 100,
                "entities": {
                    "urls": [
                        {
                            "url": "https://t.co/example",
                            "expanded_url": "https://example.com/blog/agent-harnesses",
                            "display_url": "example.com/blog/agent-harnesses",
                        }
                    ]
                },
            }
        ],
        raw_posts=[],
        errors=[],
        generated_at=datetime(2026, 6, 15, tzinfo=UTC),
    )

    assert "External links:" in markdown
    assert "https://example.com/blog/agent-harnesses" in markdown


def test_load_dotenv_sets_missing_values_without_overwriting(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("X_API_BEARER_TOKEN='local-token'\nOPENAI_MODEL=gpt-5.4\n", encoding="utf-8")
    monkeypatch.delenv("X_API_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "already-set")

    load_dotenv(env_path)

    assert os.environ["X_API_BEARER_TOKEN"] == "local-token"
    assert os.environ["OPENAI_MODEL"] == "already-set"


def test_discover_candidates_filters_followed_and_scores_linked_sources() -> None:
    digest = {
        "top_posts": [
            {
                "id": "99",
                "score": 90,
                "text": "Great agent harness work from @newlab and @alreadyfollowed",
                "author": {"username": "followedauthor"},
                "entities": {
                    "mentions": [
                        {"username": "newlab"},
                        {"username": "alreadyfollowed"},
                    ],
                    "urls": [
                        {
                            "expanded_url": "https://twitter.com/upstream_ai/status/123",
                            "display_url": "x.com/upstream_ai/status/123",
                        }
                    ],
                },
            }
        ]
    }
    profiles = [
        {
            "username": "newlab",
            "name": "New Lab",
            "description": "AI agent evals and harness engineering",
            "verified": True,
            "public_metrics": {"followers_count": 50000},
        },
        {
            "username": "upstream_ai",
            "name": "Upstream AI",
            "description": "Frontier model tooling",
            "verified": False,
            "public_metrics": {"followers_count": 25000},
        },
    ]

    candidates = discover_candidates(
        digest,
        _config(),
        followed_handles={"alreadyfollowed", "followedauthor"},
        hydrated_profiles=profiles,
        top_n=10,
    )

    handles = [candidate["username"] for candidate in candidates]
    assert "alreadyfollowed" not in handles
    assert "followedauthor" not in handles
    assert "newlab" in handles
    assert "upstream_ai" in handles
    upstream = next(candidate for candidate in candidates if candidate["username"] == "upstream_ai")
    assert "linked X source" in upstream["reasons"]


def test_render_follow_candidates_links_profile_and_evidence() -> None:
    markdown = render_follow_candidates(
        [
            {
                "username": "newlab",
                "score": 42,
                "reasons": ["mentioned in high-signal post"],
                "profile": {"name": "New Lab", "description": "Agent harnesses"},
                "evidence": [
                    {
                        "kind": "mentioned in high-signal post",
                        "source_author": "frontier",
                        "source_post_id": "10",
                    }
                ],
            }
        ],
        generated_at=datetime(2026, 6, 15, tzinfo=UTC),
        digest_path=Path("digest.json"),
    )

    assert "https://x.com/newlab" in markdown
    assert "https://x.com/frontier/status/10" in markdown


def test_discover_candidates_drops_irrelevant_mention_only_profiles() -> None:
    digest = {
        "top_posts": [
            {
                "id": "12",
                "score": 90,
                "text": "AI simulation work with @sportsclub",
                "author": {"username": "frontier"},
                "entities": {"mentions": [{"username": "sportsclub"}]},
            }
        ]
    }
    profiles = [
        {
            "username": "sportsclub",
            "name": "Sports Club",
            "description": "Official football club account",
            "verified": True,
            "public_metrics": {"followers_count": 2_000_000},
        }
    ]

    candidates = discover_candidates(
        digest,
        _config(),
        followed_handles={"frontier"},
        hydrated_profiles=profiles,
        top_n=10,
    )

    assert candidates == []
