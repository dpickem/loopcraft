"""Tests for the X intelligence ranking, digest, follow discovery, and store."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from loopcraft.env import load_dotenv
from loopcraft.config import LoopcraftConfig
from loopcraft.research_intel.x.cli import XIntelRunner
from loopcraft.research_intel.x.client import XApiError
from loopcraft.research_intel.x.config import IntelConfig, OutputPaths
from loopcraft.research_intel.x.digest import render_digest
from loopcraft.research_intel.x.follow_discovery import discover_candidates, render_follow_candidates
from loopcraft.research_intel.x.ranking import rank_posts, score_post
from loopcraft.research_intel.x.store import IntelStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _config() -> IntelConfig:
    """Return an X content config with representative frontier/ranking data."""
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
                "ai_context_keywords": ["loopcraft", "evals", "ai", "agent"],
                "keywords": {"loopcraft": 40, "evals": 10},
            },
        }
    )


def test_score_post_weights_frontier_lab_and_keywords() -> None:
    """score_post credits frontier-lab affiliation and keyword matches."""
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
    """rank_posts collapses duplicate ids and ranks by score."""
    posts = [
        {"id": "1", "text": "boring", "author": {"username": "nobody"}},
        {"id": "1", "text": "loopcraft", "author": {"username": "nobody"}},
        {"id": "2", "text": "evals", "author": {"username": "frontier"}},
    ]

    ranked = rank_posts(posts, _config())

    assert len(ranked) == 2
    assert ranked[0]["id"] == "2"


def test_render_digest_includes_frontier_section() -> None:
    """The digest renders a frontier-lab highlights section with post links."""
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
    """The digest surfaces expanded external links from post entities."""
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
    """load_dotenv sets unset vars but never overwrites existing ones."""
    env_path = tmp_path / ".env"
    env_path.write_text("X_API_BEARER_TOKEN='local-token'\nOPENAI_MODEL=gpt-5.4\n", encoding="utf-8")
    monkeypatch.delenv("X_API_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "already-set")

    load_dotenv(env_path)

    assert os.environ["X_API_BEARER_TOKEN"] == "local-token"
    assert os.environ["OPENAI_MODEL"] == "already-set"


def test_discover_candidates_filters_followed_and_scores_linked_sources() -> None:
    """Discovery excludes followed handles and scores linked-source accounts."""
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
    """The follow-candidate markdown links profiles and evidence posts."""
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
    """Mention-only candidates without discovery context are dropped."""
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


def test_collect_raw_posts_isolates_failing_sources() -> None:
    """One source raising XApiError does not skip the others; all errors collect."""
    runner = XIntelRunner.__new__(XIntelRunner)

    def ok(client, *, dry_run):  # noqa: ANN001
        """Healthy fetch phase that returns one post."""
        return [{"id": "ok"}]

    def boom_rate_limit(client, *, dry_run):  # noqa: ANN001
        """Fetch phase that fails with a rate-limit error."""
        raise XApiError("rate limit on search")

    def boom_lists(client, *, dry_run):  # noqa: ANN001
        """Fetch phase that fails with a list-access error."""
        raise XApiError("list access denied")

    runner._fetch_from_queries = boom_rate_limit
    runner._fetch_from_lists = boom_lists
    runner._fetch_from_following = ok
    runner._fetch_from_author_handles = ok
    runner._fetch_from_snapshot = ok

    posts, errors = runner._collect_raw_posts(client=None, dry_run=True)

    # The three healthy sources still contribute posts...
    assert len(posts) == 3
    # ...and both independent failures are accumulated, not just the first.
    assert errors == ["rate limit on search", "list access denied"]


def test_x_run_writes_declared_outputs_with_run_id(tmp_path, monkeypatch) -> None:
    """A default-config X run writes every declared output, keyed by run id.

    Covers two findings at once: the run-scoped history archives use the
    control-plane run id (LOOPCRAFT_RUN_ID), and source-state.json is always
    written even with the shipped empty-source config.
    """
    run_id = "20260101T000000Z-cafef00d"
    monkeypatch.setenv("LOOPCRAFT_RUN_ID", run_id)

    runner = XIntelRunner.__new__(XIntelRunner)
    runner.loopcraft = LoopcraftConfig(source_path=tmp_path / "src", memory_path=tmp_path / "mem")
    runner.config = IntelConfig.from_dict({})
    runner.store = IntelStore(runner.loopcraft, OutputPaths())
    runner._client = lambda **kwargs: object()  # empty sources => never used

    rc = runner.run(dry_run=False)
    assert rc == 0

    base = tmp_path / "mem" / "ledger" / "research" / "x"
    assert (base / "history" / f"{run_id}.md").exists()
    assert (base / "history" / f"{run_id}.json").exists()
    assert (base / "source-state.json").exists()
    assert (base / "seen.json").exists()
    assert (base / "posts.jsonl").exists()
    assert (base / "latest.md").exists()

    # Finding 3 (review 06): the run writes exactly the manifest-declared set.
    from loopcraft.manifest import LoopManifest

    manifest = LoopManifest.load(REPO_ROOT / "loops" / "x-intel.yaml")
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    for declared in manifest.outputs:
        resolved = runner.loopcraft.resolve_state_template(declared, run_id=run_id, date=date)
        assert resolved.exists(), f"missing declared output: {declared} -> {resolved}"


def test_x_persist_source_state_initializes_empty_file(tmp_path) -> None:
    """persist_source_state creates source-state.json even with no high-water marks."""
    config = LoopcraftConfig(source_path=tmp_path / "src", memory_path=tmp_path / "mem")
    store = IntelStore(config, OutputPaths(source_state_path=Path("state/source-state.json")))
    path = store.persist_source_state()
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip() == "{}"


def test_x_config_load_prefers_local_override(tmp_path) -> None:
    """IntelConfig.load reads a gitignored .local. sibling when present."""
    (tmp_path / "x_intel.yaml").write_text("ranking: {top_posts: 5}\n", encoding="utf-8")
    (tmp_path / "x_intel.local.yaml").write_text("ranking: {top_posts: 42}\n", encoding="utf-8")
    config = IntelConfig.load(tmp_path / "x_intel.yaml")
    assert config.ranking.top_posts == 42


def test_x_output_defaults_are_memory_state_paths() -> None:
    """Fixed X output paths point at the memory ledger state tree."""
    output = OutputPaths()
    assert output.seen_path.as_posix() == "state/research/x/seen.json"
    assert output.posts_path.as_posix() == "state/research/x/posts.jsonl"
    assert output.source_state_path.as_posix() == "state/research/x/source-state.json"
    assert output.history_dir.as_posix() == "state/research/x/history"
    assert output.latest_markdown.as_posix() == "state/research/x/latest.md"


def test_x_config_rejects_output_override(tmp_path) -> None:
    """Finding 3 (review 06): content config cannot redirect durable outputs.

    Neither the public content config nor a gitignored local override may set
    ``output`` paths — the manifest's declared outputs are the source of truth.
    """
    import pytest

    with pytest.raises(ValueError, match="must not override 'output'"):
        IntelConfig.from_dict({"output": {"latest_json": "state/research/x/custom.json"}})

    (tmp_path / "x_intel.yaml").write_text("ranking: {top_posts: 5}\n", encoding="utf-8")
    (tmp_path / "x_intel.local.yaml").write_text(
        "output: {latest_json: state/research/x/custom.json}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="must not override 'output'"):
        IntelConfig.load(tmp_path / "x_intel.yaml")


def test_x_fixed_outputs_match_manifest_contract() -> None:
    """Finding 3 (review 06): the fixed write set equals the declared outputs.

    ``follow_candidates_dir`` is excluded: it belongs to the separate operator
    ``discover-follows`` command, not the ``run`` workflow this loop schedules.
    """
    from loopcraft.manifest import LoopManifest

    manifest = LoopManifest.load(REPO_ROOT / "loops" / "x-intel.yaml")
    out = OutputPaths()
    expected = {
        out.seen_path.as_posix(),
        out.posts_path.as_posix(),
        out.source_state_path.as_posix(),
        f"{out.digest_dir.as_posix()}/{{{{date}}}}.md",
        f"{out.digest_dir.as_posix()}/{{{{date}}}}.json",
        f"{out.history_dir.as_posix()}/{{{{run_id}}}}.md",
        f"{out.history_dir.as_posix()}/{{{{run_id}}}}.json",
        out.latest_markdown.as_posix(),
        out.latest_json.as_posix(),
    }
    assert set(manifest.outputs) == expected


def test_x_loads_yaml_content_config() -> None:
    """The shipped X YAML content config loads with expected values."""
    config = IntelConfig.load(REPO_ROOT / "config" / "x_intel.yaml")
    assert config.sources.following_snapshot is None
    assert "sama" in config.frontier_labs.high_priority_handles
    assert "loopcraft" in config.ranking.keywords


def test_x_store_uses_json_ledger_files(tmp_path) -> None:
    """The X store tracks high-water marks and posts/seen ids in the ledger."""
    config = LoopcraftConfig(source_path=tmp_path / "src", memory_path=tmp_path / "mem")
    output = OutputPaths(
        seen_path=Path("state/seen.json"),
        posts_path=Path("state/posts.jsonl"),
        source_state_path=Path("state/source-state.json"),
    )
    store = IntelStore(config, output)
    assert store.latest_seen_id("query:test") is None
    store.remember_source_highwater(
        "query:test",
        [{"id": "10", "text": "old"}, {"id": "12", "text": "new"}],
    )
    assert store.latest_seen_id("query:test") == "12"

    store.save_posts([{"id": "10", "text": "old"}, {"id": "10", "text": "updated"}])
    store.mark_seen(["10", "12"])
    assert store.seen_ids() == {"10", "12"}
    assert len((tmp_path / "mem" / "ledger" / "posts.jsonl").read_text(encoding="utf-8").splitlines()) == 1
