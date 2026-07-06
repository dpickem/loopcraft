"""Tests for the public/private config-split helpers in loopcraft.settings."""

from __future__ import annotations

from pathlib import Path

from loopcraft.settings import (
    asset_env_var,
    clean_lines,
    resolve_overridable_list,
    split_env_list,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_clean_lines_drops_comments_and_blanks() -> None:
    """clean_lines keeps only non-empty, non-comment, stripped lines."""
    text = "# header\n\nalpha\n  beta  \n# c\n"
    assert clean_lines(text) == ["alpha", "beta"]


def test_split_env_list_handles_commas_and_newlines() -> None:
    """split_env_list splits on commas/newlines and drops blanks/comments."""
    assert split_env_list("a, b ,c") == ["a", "b", "c"]
    assert split_env_list("a\nb\n# c\n,d") == ["a", "b", "d"]
    assert split_env_list("   ") == []


def test_asset_env_var_mapping() -> None:
    """asset_env_var derives the LOOPCRAFT_* override name from an asset path."""
    assert asset_env_var("skills/slack-triage/channels.txt") == "LOOPCRAFT_SLACK_TRIAGE_CHANNELS"
    assert asset_env_var("skills/paper-discovery/lists.txt") == "LOOPCRAFT_PAPER_DISCOVERY_LISTS"


def test_resolve_precedence_env_over_local_over_public(tmp_path: Path) -> None:
    """resolve_overridable_list honors env > *.local.* > public precedence."""
    public = tmp_path / "channels.txt"
    public.write_text("# c\npub-a\npub-b\n", encoding="utf-8")
    local = tmp_path / "channels.local.txt"
    local.write_text("loc-a\n", encoding="utf-8")

    # public only
    assert resolve_overridable_list(env_var="X", public_path=public, environ={}) == ["pub-a", "pub-b"]
    # local shadows public
    assert resolve_overridable_list(
        env_var="X", public_path=public, local_path=local, environ={}
    ) == ["loc-a"]
    # env beats both
    assert resolve_overridable_list(
        env_var="X", public_path=public, local_path=local, environ={"X": "e1, e2"}
    ) == ["e1", "e2"]
    # blank env falls through to local
    assert resolve_overridable_list(
        env_var="X", public_path=public, local_path=local, environ={"X": "   "}
    ) == ["loc-a"]


def test_public_channels_file_has_no_confidential_entries() -> None:
    """The committed public channels file must hold only comments/placeholders."""
    text = (REPO_ROOT / "skills" / "slack-triage" / "channels.txt").read_text(encoding="utf-8")
    assert clean_lines(text) == []
