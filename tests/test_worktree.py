"""Tests for staging loop assets into an isolated run worktree."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopcraft.config import LoopcraftConfig, SourcePathError
from loopcraft.manifest import LoopManifest
from loopcraft.worktree import stage_loop_assets


def _source_tree(tmp_path: Path) -> Path:
    """Build a minimal source tree (skill dir + manifest) and return its root."""
    source = tmp_path / "src"
    skill_dir = source / "skills" / "slack-triage"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("skill body", encoding="utf-8")
    (skill_dir / "channels.txt").write_text("team-a\nteam-b\n", encoding="utf-8")
    loops = source / "loops"
    loops.mkdir()
    (loops / "slack-triage.yaml").write_text("id: slack-triage\n", encoding="utf-8")
    return source


def test_stage_loop_assets_copies_skill_dir_and_manifest(tmp_path: Path) -> None:
    """Staging copies the skill directory and manifest into the worktree."""
    source = _source_tree(tmp_path)
    config = LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")
    manifest = LoopManifest.from_dict(
        {
            "id": "slack-triage",
            "name": "Slack triage",
            "logic": {"skill": "skills/slack-triage/SKILL.md"},
            "cadence": {"type": "cron", "at": "0 9 * * *"},
        },
        source_path=source / "loops" / "slack-triage.yaml",
    )
    workdir = tmp_path / "wt"

    staged = stage_loop_assets(config, manifest, workdir, environ={})

    # The skill's sibling asset must resolve relative to the run directory.
    channels = workdir / "skills" / "slack-triage" / "channels.txt"
    assert channels.exists()
    assert channels.read_text(encoding="utf-8").startswith("team-a")
    assert (workdir / "skills" / "slack-triage" / "SKILL.md").exists()
    assert (workdir / "loops" / "slack-triage.yaml").exists()
    assert (workdir / "skills" / "slack-triage").resolve() in staged


def test_all_staged_paths_stay_under_workdir(tmp_path: Path) -> None:
    """Every staged path is contained within the run worktree."""
    source = _source_tree(tmp_path)
    config = LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")
    manifest = LoopManifest.from_dict(
        {
            "id": "slack-triage",
            "name": "Slack triage",
            "logic": {"skill": "skills/slack-triage/SKILL.md"},
            "cadence": {"type": "cron", "at": "0 9 * * *"},
        },
        source_path=source / "loops" / "slack-triage.yaml",
    )
    workdir = (tmp_path / "wt").resolve()
    staged = stage_loop_assets(config, manifest, workdir, environ={})
    for dest in staged:
        assert str(dest.resolve()).startswith(str(workdir))


def test_env_var_overrides_staged_channels(tmp_path: Path) -> None:
    """Public/private split: an env var replaces the staged public list asset."""
    source = _source_tree(tmp_path)
    (source / "skills" / "slack-triage" / "channels.txt").write_text(
        "# public placeholder only\n", encoding="utf-8"
    )
    config = LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")
    manifest = LoopManifest.from_dict(
        {
            "id": "slack-triage",
            "name": "Slack triage",
            "logic": {"skill": "skills/slack-triage/SKILL.md"},
            "cadence": {"type": "cron", "at": "0 9 * * *"},
        },
        source_path=source / "loops" / "slack-triage.yaml",
    )
    workdir = tmp_path / "wt"
    stage_loop_assets(
        config,
        manifest,
        workdir,
        environ={"LOOPCRAFT_SLACK_TRIAGE_CHANNELS": "secret-a, secret-b"},
    )
    staged = (workdir / "skills" / "slack-triage" / "channels.txt").read_text(encoding="utf-8")
    assert "secret-a" in staged and "secret-b" in staged
    assert "public placeholder" not in staged


def test_local_file_shadows_public(tmp_path: Path) -> None:
    """A *.local.* override replaces the public file and is removed from the worktree."""
    source = _source_tree(tmp_path)
    skill_dir = source / "skills" / "slack-triage"
    (skill_dir / "channels.txt").write_text("# placeholder\n", encoding="utf-8")
    (skill_dir / "channels.local.txt").write_text("private-chan\n", encoding="utf-8")
    config = LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")
    manifest = LoopManifest.from_dict(
        {
            "id": "slack-triage",
            "name": "Slack triage",
            "logic": {"skill": "skills/slack-triage/SKILL.md"},
            "cadence": {"type": "cron", "at": "0 9 * * *"},
        },
        source_path=source / "loops" / "slack-triage.yaml",
    )
    workdir = tmp_path / "wt"
    stage_loop_assets(config, manifest, workdir, environ={})

    channels = workdir / "skills" / "slack-triage" / "channels.txt"
    assert channels.read_text(encoding="utf-8").strip() == "private-chan"
    assert not (workdir / "skills" / "slack-triage" / "channels.local.txt").exists()


def test_stage_loop_assets_stages_content_config_with_local_override(tmp_path: Path) -> None:
    """Staging materializes manifest.content.config and overlays its .local. sibling."""
    source = _source_tree(tmp_path)
    (source / "config").mkdir()
    (source / "config" / "x_intel.yaml").write_text("ranking:\n  top_posts: 5\n", encoding="utf-8")
    (source / "config" / "x_intel.local.yaml").write_text("ranking:\n  top_posts: 99\n", encoding="utf-8")
    config = LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")
    manifest = LoopManifest.from_dict(
        {
            "id": "x-intel",
            "name": "X",
            "logic": {"skill": "skills/slack-triage/SKILL.md"},
            "content": {"config": "config/x_intel.yaml"},
            "cadence": {"type": "cron", "at": "0 9 * * *"},
        },
        source_path=source / "loops" / "x-intel.yaml",
    )
    workdir = tmp_path / "wt"

    stage_loop_assets(config, manifest, workdir, environ={})

    staged_config = workdir / "config" / "x_intel.yaml"
    assert staged_config.exists()
    # The private override shadows the public file, and the .local. copy is removed.
    assert "99" in staged_config.read_text(encoding="utf-8")
    assert not (workdir / "config" / "x_intel.local.yaml").exists()


def test_stage_rejects_traversing_skill(tmp_path: Path) -> None:
    """Finding 2 (review 02): an escaping logic.skill is refused at staging."""
    source = _source_tree(tmp_path)
    config = LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")
    manifest = LoopManifest.from_dict(
        {
            "id": "evil",
            "name": "Evil",
            "logic": {"skill": "../../outside/SKILL.md"},
            "cadence": {"type": "cron", "at": "0 9 * * *"},
        }
    )
    with pytest.raises(SourcePathError):
        stage_loop_assets(config, manifest, tmp_path / "wt")
