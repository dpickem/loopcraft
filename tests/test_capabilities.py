"""Tests for the runtime-neutral capability probe registry."""

from __future__ import annotations

from pathlib import Path

from loopcraft.config import LoopcraftConfig
from loopcraft.manifest import LoopManifest
from loopcraft.runners import capabilities


def _config(tmp_path: Path) -> LoopcraftConfig:
    """Return a config with a demo skill + verify file staged under tmp_path."""
    source = tmp_path / "src"
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("body", encoding="utf-8")
    (source / "skills" / "demo" / "verify.md").write_text("done", encoding="utf-8")
    return LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")


def _manifest(**overrides) -> LoopManifest:
    """Return a demo manifest whose skill/verify assets exist under _config."""
    base = {
        "id": "demo",
        "name": "Demo",
        "cadence": {"type": "cron", "at": "0 9 * * *"},
        "logic": {"skill": "skills/demo/SKILL.md", "verify": "skills/demo/verify.md"},
    }
    base.update(overrides)
    return LoopManifest.from_dict(base)


def test_capabilities_pass_for_valid_loop(tmp_path: Path) -> None:
    """A loop with existing assets and no external deps reports no problems."""
    assert capabilities.check_declared_capabilities(_manifest(), _config(tmp_path)) == []


def test_capabilities_flag_missing_skill(tmp_path: Path) -> None:
    """A missing skill asset is reported by the shared capability check."""
    manifest = _manifest(logic={"skill": "skills/ghost/SKILL.md"})
    problems = capabilities.check_declared_capabilities(manifest, _config(tmp_path))
    assert any("skill not found" in p for p in problems)


def test_capabilities_flag_unknown_auth_and_api(tmp_path: Path) -> None:
    """Auth bundles/APIs without a registered probe are reported."""
    manifest = _manifest(depends_on={"auth": ["mystery"], "apis": ["ghost-api"]})
    problems = capabilities.check_declared_capabilities(manifest, _config(tmp_path))
    assert any("mystery" in p for p in problems)
    assert any("ghost-api" in p for p in problems)


def test_capabilities_probe_registry_is_injectable(tmp_path: Path, monkeypatch) -> None:
    """A registered probe's failure message flows through the check."""
    monkeypatch.setitem(capabilities.API_PROBES, "slack", lambda config: "api 'slack': boom")
    manifest = _manifest(depends_on={"apis": ["slack"]})
    problems = capabilities.check_declared_capabilities(manifest, _config(tmp_path))
    assert any("boom" in p for p in problems)
