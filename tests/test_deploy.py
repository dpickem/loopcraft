"""Tests for fleet pre-deploy validation, planning, and unit install (M2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopcraft import deploy
from loopcraft.config import LoopcraftConfig, SchedulerConfig
from loopcraft.manifest import load_all
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


def _abs_loopctl(tmp_path: Path) -> str:
    """Create a dummy executable and return its absolute path.

    Rendering resolves ``loopctl_bin`` to an absolute path, so tests pin one
    instead of depending on the interactive shell PATH (finding 1).
    """
    binary = tmp_path / "bin" / "loopctl"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return str(binary)


def _source(
    tmp_path: Path,
    manifest_text: str,
    filename: str = "demo.yaml",
    scheduler: SchedulerConfig | None = None,
) -> LoopcraftConfig:
    """Build a temp source tree with one skill + manifest and return its config."""
    source = tmp_path / "src"
    (source / "loops").mkdir(parents=True)
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "SKILL.md").write_text("body", encoding="utf-8")
    (source / "loops" / filename).write_text(manifest_text, encoding="utf-8")
    return LoopcraftConfig(
        source_path=source,
        memory_path=tmp_path / "mem",
        scheduler=scheduler or SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path)),
    )


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


def _fake_systemctl(monkeypatch, unit_dir: Path, *, fail_on: str | None = None) -> list[list[str]]:
    """Wire a fake systemctl + unit dir; optionally fail a matching subcommand.

    Returns the list that records each systemctl argv the code runs.
    """
    monkeypatch.setattr(deploy.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(deploy, "systemd_unit_dir", lambda cfg: unit_dir)
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        failed = fail_on is not None and fail_on in cmd

        class _Completed:
            returncode = 1 if failed else 0
            stdout = ""
            stderr = "boom" if failed else ""

        return _Completed()

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    return calls


def test_install_units_copies_and_enables(monkeypatch, tmp_path: Path) -> None:
    """Install copies units to the scope dir and enables each timer trigger."""
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config, run_preflight=False)
    unit_dir = tmp_path / "systemd"
    calls = _fake_systemctl(monkeypatch, unit_dir)

    result = deploy.install_units(config, plan)

    assert result.ok
    assert (unit_dir / "loop-demo.service").exists()
    assert (unit_dir / "loop-demo.timer").exists()
    assert result.enabled == ["loop-demo.timer"]
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "--now", "loop-demo.timer"] in calls


def test_install_rolls_back_on_enable_failure(monkeypatch, tmp_path: Path) -> None:
    """A failed enable rolls back: units removed, nothing left enabled (finding 5)."""
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config, run_preflight=False)
    unit_dir = tmp_path / "systemd"
    _fake_systemctl(monkeypatch, unit_dir, fail_on="enable")

    result = deploy.install_units(config, plan)

    assert not result.ok
    assert result.rolled_back
    assert result.enabled == []
    # Newly written units are removed on rollback.
    assert not (unit_dir / "loop-demo.service").exists()
    assert not (unit_dir / "loop-demo.timer").exists()


def test_install_rollback_disables_already_enabled_triggers(monkeypatch, tmp_path: Path) -> None:
    """When a later trigger fails, earlier enabled triggers are disabled."""
    config = _source(
        tmp_path,
        "id: a\nname: A\ncadence: {type: cron, at: '0 9 * * *'}\nlogic: {skill: skills/demo/SKILL.md}\n",
        filename="a.yaml",
    )
    # Second loop in the same source tree so two timers are enabled in order.
    (config.source_path / "loops" / "b.yaml").write_text(
        "id: b\nname: B\ncadence: {type: cron, at: '0 10 * * *'}\nlogic: {skill: skills/demo/SKILL.md}\n",
        encoding="utf-8",
    )
    plan = deploy.plan_deployment(config, run_preflight=False)
    unit_dir = tmp_path / "systemd"
    calls = _fake_systemctl(monkeypatch, unit_dir, fail_on="loop-b.timer")

    result = deploy.install_units(config, plan)

    assert result.rolled_back
    # The first trigger enabled before the failure is disabled during rollback.
    assert ["systemctl", "disable", "--now", "loop-a.timer"] in calls
    assert not (unit_dir / "loop-a.timer").exists()


def test_install_restores_replaced_unit_on_failure(monkeypatch, tmp_path: Path) -> None:
    """Rollback restores a unit file the install overwrote."""
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config, run_preflight=False)
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    (unit_dir / "loop-demo.timer").write_text("PRIOR CONTENT\n", encoding="utf-8")
    _fake_systemctl(monkeypatch, unit_dir, fail_on="enable")

    result = deploy.install_units(config, plan)

    assert result.rolled_back
    # The pre-existing unit is restored to its prior content, not left rewritten.
    assert (unit_dir / "loop-demo.timer").read_text(encoding="utf-8") == "PRIOR CONTENT\n"


# --- loopctl command resolution (finding 1) ----------------------------------


def test_resolve_loopctl_command_absolute(tmp_path: Path) -> None:
    """An existing absolute loopctl_bin resolves to itself with no problem."""
    binary = _abs_loopctl(tmp_path)
    config = LoopcraftConfig(
        source_path=tmp_path / "src",
        memory_path=tmp_path / "mem",
        scheduler=SchedulerConfig(loopctl_bin=binary),
    )
    command, problem = deploy.resolve_loopctl_command(config)
    assert command == binary
    assert problem is None


def test_resolve_loopctl_command_unresolvable_is_problem(tmp_path: Path) -> None:
    """A bare name not on PATH is reported, not silently rendered."""
    config = LoopcraftConfig(
        source_path=tmp_path / "src",
        memory_path=tmp_path / "mem",
        scheduler=SchedulerConfig(loopctl_bin="definitely-not-a-real-binary-xyz"),
    )
    command, problem = deploy.resolve_loopctl_command(config)
    assert command is None
    assert "cannot be resolved" in problem


def test_plan_unresolvable_command_blocks_render(tmp_path: Path) -> None:
    """An unresolvable command makes the plan non-renderable with a problem."""
    config = _source(
        tmp_path,
        _CRON_MANIFEST,
        scheduler=SchedulerConfig(loopctl_bin="definitely-not-a-real-binary-xyz"),
    )
    plan = deploy.plan_deployment(config, run_preflight=False)
    assert not plan.renderable
    assert plan.units == []
    assert any("cannot be resolved" in p for p in plan.render_problems)


# --- scheduled environment validation (finding 3) ----------------------------


def _env_manifest(tmp_path: Path, scheduler: SchedulerConfig) -> LoopcraftConfig:
    """A source tree with one loop declaring a DEMO_TOKEN env dependency."""
    return _source(
        tmp_path,
        "id: demo\nname: Demo\ncadence: {type: cron, at: '0 9 * * *'}\n"
        "depends_on: {env: [DEMO_TOKEN]}\nlogic: {skill: skills/demo/SKILL.md}\n",
        scheduler=scheduler,
    )


def test_validate_environment_flags_unconfigured_env_file(tmp_path: Path) -> None:
    """Declared env vars with no environment_file are reported at apply."""

    config = _env_manifest(tmp_path, SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path)))
    problems = deploy.validate_environment(config, load_all(config.loops_dir).manifests)
    assert any("no scheduler.environment_file" in p for p in problems)


def test_validate_environment_flags_missing_var(tmp_path: Path) -> None:
    """A configured env file that lacks a declared var is reported."""

    env_file = tmp_path / "secrets.env"
    env_file.write_text("OTHER=1\n", encoding="utf-8")
    config = _env_manifest(
        tmp_path,
        SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path), environment_file=str(env_file)),
    )
    problems = deploy.validate_environment(config, load_all(config.loops_dir).manifests)
    assert any("missing declared env vars" in p and "DEMO_TOKEN" in p for p in problems)


def test_validate_environment_accepts_complete_env_file(tmp_path: Path) -> None:
    """A configured env file outside the trees with the var passes."""

    env_file = tmp_path / "secrets.env"
    env_file.write_text("DEMO_TOKEN=abc\n", encoding="utf-8")
    config = _env_manifest(
        tmp_path,
        SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path), environment_file=str(env_file)),
    )
    assert deploy.validate_environment(config, load_all(config.loops_dir).manifests) == []


def test_validate_environment_rejects_in_tree_env_file(tmp_path: Path) -> None:
    """An env file inside the memory tree is rejected (secrets stay out of git)."""
    env_file = tmp_path / "mem" / "secrets.env"  # matches _source's memory_path
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("DEMO_TOKEN=abc\n", encoding="utf-8")
    config = _env_manifest(
        tmp_path,
        SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path), environment_file=str(env_file)),
    )
    problems = deploy.validate_environment(config, load_all(config.loops_dir).manifests)
    assert any("outside the memory tree" in p for p in problems)
