"""Tests for fleet pre-deploy validation, planning, and unit install (M2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopcraft import deploy
from loopcraft.config import LoopcraftConfig
from loopcraft.runners.base import PreflightReport


class _OkRunner:
    """Stub adapter whose preflight always passes."""

    vendor = "okvendor"

    def preflight(self, loop, config):  # noqa: ANN001
        """Report a passing preflight."""
        return PreflightReport(vendor=self.vendor, ok=True, problems=[])


class _FailRunner:
    """Stub adapter whose preflight always fails."""

    vendor = "failvendor"

    def preflight(self, loop, config):  # noqa: ANN001
        """Report a failing preflight."""
        return PreflightReport(vendor=self.vendor, ok=False, problems=["missing token"])


def _source(tmp_path: Path, manifest_text: str, filename: str = "demo.yaml") -> LoopcraftConfig:
    """Build a temp source tree with one skill + manifest and return its config."""
    source = tmp_path / "src"
    (source / "loops").mkdir(parents=True)
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("body", encoding="utf-8")
    (source / "loops" / filename).write_text(manifest_text, encoding="utf-8")
    return LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")


_CRON_MANIFEST = (
    "id: demo\n"
    "name: Demo\n"
    "cadence: {type: cron, at: '0 9 * * 1-5'}\n"
    "logic: {skill: skills/demo/SKILL.md}\n"
)


def test_plan_deployment_ok_with_passing_preflight(monkeypatch, tmp_path: Path) -> None:
    """A valid fleet with passing preflight yields a fully deployable plan."""
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _OkRunner())
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config)
    assert plan.ok
    assert plan.renderable
    assert plan.preflight_ran
    assert len(plan.units) == 1
    assert plan.units[0].loop == "demo"


def test_plan_deployment_reports_preflight_problems(monkeypatch, tmp_path: Path) -> None:
    """Unmet dependencies surface at plan time, and block deployment but not render."""
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _FailRunner())
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config)
    assert not plan.ok
    # Units are still renderable — a missing token does not corrupt unit text.
    assert plan.renderable
    assert plan.preflight_problems == ["demo: missing token"]


def test_plan_deployment_skip_preflight(tmp_path: Path) -> None:
    """--skip-preflight validates structure only and runs no adapter probes."""
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config, run_preflight=False)
    assert plan.ok
    assert not plan.preflight_ran
    assert plan.preflights == []


def test_plan_deployment_captures_manifest_problems(tmp_path: Path) -> None:
    """Structural manifest problems make the plan non-renderable."""
    config = _source(
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: cron, at: '0 9 * * *'}\n"
        "outputs: ['linear:project/Daily']\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    plan = deploy.plan_deployment(config, run_preflight=False)
    assert not plan.ok
    assert not plan.renderable
    assert any("external sink" in p for p in plan.manifest_problems)


def test_plan_deployment_captures_render_problems(tmp_path: Path) -> None:
    """A cadence with no systemd representation is a render problem, not a crash."""
    config = _source(
        tmp_path,
        "id: demo\n"
        "name: Demo\n"
        "cadence: {type: event}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
    )
    plan = deploy.plan_deployment(config, run_preflight=False)
    assert not plan.renderable
    assert any("event triggers land in M8" in p for p in plan.render_problems)


def test_write_units_writes_all_files(tmp_path: Path) -> None:
    """write_units materializes every rendered unit into the target directory."""
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config, run_preflight=False)
    out = tmp_path / "units"
    written = deploy.write_units(plan, out)
    names = sorted(p.name for p in written)
    assert names == ["loop-demo.service", "loop-demo.timer"]
    assert all(p.exists() for p in written)


def test_install_units_requires_systemctl(monkeypatch, tmp_path: Path) -> None:
    """Install fails cleanly (no raise) when systemctl is not on PATH."""
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config, run_preflight=False)
    monkeypatch.setattr(deploy.shutil, "which", lambda name: None)
    result = deploy.install_units(config, plan)
    assert not result.ok
    assert any("systemctl not found" in p for p in result.problems)


def test_install_units_copies_and_enables(monkeypatch, tmp_path: Path) -> None:
    """Install copies units to the scope dir and enables each timer trigger."""
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config, run_preflight=False)

    unit_dir = tmp_path / "systemd"
    monkeypatch.setattr(deploy.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(deploy, "systemd_unit_dir", lambda cfg: unit_dir)
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Completed()

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    result = deploy.install_units(config, plan)

    assert result.ok
    assert (unit_dir / "loop-demo.service").exists()
    assert (unit_dir / "loop-demo.timer").exists()
    assert result.enabled == ["loop-demo.timer"]
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "--now", "loop-demo.timer"] in calls
