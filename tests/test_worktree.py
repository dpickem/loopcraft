from __future__ import annotations

from pathlib import Path

import pytest

from loopcraft.config import LoopcraftConfig, SourcePathError
from loopcraft.manifest import LoopManifest
from loopcraft.worktree import stage_loop_assets


def _source_tree(tmp_path: Path) -> Path:
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

    staged = stage_loop_assets(config, manifest, workdir)

    # The skill's sibling asset must resolve relative to the run directory.
    channels = workdir / "skills" / "slack-triage" / "channels.txt"
    assert channels.exists()
    assert channels.read_text(encoding="utf-8").startswith("team-a")
    assert (workdir / "skills" / "slack-triage" / "SKILL.md").exists()
    assert (workdir / "loops" / "slack-triage.yaml").exists()
    assert (workdir / "skills" / "slack-triage").resolve() in staged


def test_all_staged_paths_stay_under_workdir(tmp_path: Path) -> None:
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
    staged = stage_loop_assets(config, manifest, workdir)
    for dest in staged:
        assert str(dest.resolve()).startswith(str(workdir))


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
