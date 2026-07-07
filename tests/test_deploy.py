"""Tests for fleet pre-deploy validation, planning, and unit install (M2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopcraft import deploy
from loopcraft.config import LoopcraftConfig, SchedulerConfig, SystemdScope
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


class _CapabilityRunner:
    """Stub adapter whose preflight runs the shared runtime-neutral checks.

    Lets scheduled-credential tests exercise the real env/auth resolution
    (against whatever config preflight receives) without a vendor binary.
    """

    vendor = "capvendor"

    def preflight(self, loop, config):  # noqa: ANN001
        """Delegate to the shared capability check on the given config."""
        from loopcraft.runners.capabilities import check_declared_capabilities

        problems = check_declared_capabilities(loop, config)
        return PreflightReport(vendor=self.vendor, ok=not problems, problems=problems)


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


def test_write_units_refuses_escaping_filename(tmp_path: Path) -> None:
    """write_units's defense-in-depth rejects a non-plain unit filename (finding 5)."""
    import pytest

    from loopcraft.scheduler import LoopUnits, RenderedUnit, UnitKind

    plan = deploy.DeploymentPlan(
        loops_dir="x",
        units=[
            LoopUnits(
                loop="demo",
                trigger="t",
                units=[RenderedUnit(kind=UnitKind.SERVICE, filename="../escape.service", content="x")],
            )
        ],
    )
    with pytest.raises(ValueError, match="unsafe unit filename"):
        deploy.write_units(plan, tmp_path / "out")


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


def test_install_reports_partial_state_when_rollback_fails(monkeypatch, tmp_path: Path) -> None:
    """If the rollback itself fails, rolled_back is False and the problem surfaces."""
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config, run_preflight=False)
    unit_dir = tmp_path / "systemd"
    # daemon-reload fails: it breaks the install (step 2) and also the rollback's
    # own final daemon-reload, so the rollback cannot fully complete.
    _fake_systemctl(monkeypatch, unit_dir, fail_on="daemon-reload")

    result = deploy.install_units(config, plan)

    assert not result.ok
    assert result.rolled_back is False
    assert any("rollback" in p for p in result.problems)


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
    assert command == [binary]
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


# --- scheduled binary resolution in preflight (review 03, finding 1) ----------

_TOOL_MANIFEST = (
    "id: demo\nname: Demo\ncadence: {type: cron, at: '0 9 * * *'}\n"
    "depends_on: {tools: [mytool]}\nlogic: {skill: skills/demo/SKILL.md}\n"
)


def _make_tool(tmp_path: Path, name: str = "mytool") -> Path:
    """Create an executable stub tool and return its directory."""
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir(parents=True, exist_ok=True)
    binary = tool_dir / name
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return tool_dir


def test_scheduled_preflight_tool_missing_from_scheduled_path(monkeypatch, tmp_path: Path) -> None:
    """A tool only on the operator PATH does not satisfy scheduled preflight."""
    tool_dir = _make_tool(tmp_path)
    monkeypatch.setenv("PATH", str(tool_dir))  # visible to the operator, not the service
    config = _source(
        tmp_path, _TOOL_MANIFEST, scheduler=SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path))
    )
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _CapabilityRunner())
    plan = deploy.plan_deployment(config, run_preflight=True)
    assert any("mytool" in p for p in plan.preflight_problems)


def test_scheduled_preflight_tool_found_on_configured_path(monkeypatch, tmp_path: Path) -> None:
    """The same tool satisfies preflight when scheduler.path includes its dir."""
    tool_dir = _make_tool(tmp_path)
    monkeypatch.delenv("PATH", raising=False)  # not on operator PATH at all
    config = _source(
        tmp_path,
        _TOOL_MANIFEST,
        scheduler=SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path), path=str(tool_dir)),
    )
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _CapabilityRunner())
    plan = deploy.plan_deployment(config, run_preflight=True)
    assert not any("mytool" in p for p in plan.preflight_problems)


# --- remove / undeploy (inverse of apply) ------------------------------------


def test_plan_removal_enumerates_staged_units(tmp_path: Path) -> None:
    """plan_removal lists a loop's staged units without touching disk."""
    config = _source(tmp_path, _CRON_MANIFEST)
    deploy.write_units(deploy.plan_deployment(config, run_preflight=False), config.systemd_stage_dir)
    plan = deploy.plan_removal(config, ["demo"])
    assert sorted(Path(p).name for p in plan.staged) == ["loop-demo.service", "loop-demo.timer"]
    assert plan.installed == []
    assert not plan.empty


def test_uninstall_removes_installed_and_staged(monkeypatch, tmp_path: Path) -> None:
    """uninstall_units disables triggers and deletes installed + staged units."""
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config, run_preflight=False)
    unit_dir = tmp_path / "systemd"
    calls = _fake_systemctl(monkeypatch, unit_dir)
    deploy.install_units(config, plan)
    deploy.write_units(plan, config.systemd_stage_dir)

    result = deploy.uninstall_units(config, ["demo"])

    assert result.ok
    assert result.disabled == ["loop-demo.timer"]
    assert set(result.removed) == {"loop-demo.service", "loop-demo.timer"}
    assert set(result.removed_staged) == {"loop-demo.service", "loop-demo.timer"}
    assert not (unit_dir / "loop-demo.timer").exists()
    assert not (config.systemd_stage_dir / "loop-demo.service").exists()
    assert ["systemctl", "disable", "--now", "loop-demo.timer"] in calls


def test_uninstall_without_systemctl_keeps_installed(monkeypatch, tmp_path: Path) -> None:
    """Without systemctl, installed units are left in place with a clear problem."""
    from loopcraft.scheduler import MANAGED_MARKER

    config = _source(tmp_path, _CRON_MANIFEST)
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    (unit_dir / "loop-demo.service").write_text(f"{MANAGED_MARKER}\n[Unit]\n", encoding="utf-8")
    (unit_dir / "loop-demo.timer").write_text(f"{MANAGED_MARKER}\n[Timer]\n", encoding="utf-8")
    monkeypatch.setattr(deploy, "systemd_unit_dir", lambda cfg: unit_dir)
    monkeypatch.setattr(deploy.shutil, "which", lambda name: None)

    result = deploy.uninstall_units(config, ["demo"])

    assert not result.ok
    assert any("systemctl not found" in p for p in result.problems)
    assert (unit_dir / "loop-demo.timer").exists()


def test_remove_skips_foreign_unmanaged_units(monkeypatch, tmp_path: Path) -> None:
    """A prefix-matching but non-loopcraft unit is skipped, never removed."""
    config = _source(tmp_path, _CRON_MANIFEST)
    stage_dir = config.systemd_stage_dir
    stage_dir.mkdir(parents=True)
    # A foreign unit that happens to match the loop- prefix but lacks the marker.
    foreign = stage_dir / "loop-demo.service"
    foreign.write_text("[Unit]\nDescription=someone else's unit\n", encoding="utf-8")

    plan = deploy.plan_removal(config, ["demo"])
    assert plan.staged == []
    assert str(foreign) in plan.skipped

    result = deploy.uninstall_units(config, ["demo"])
    assert result.removed_staged == []
    assert foreign.exists()  # left untouched


def test_loop_unit_files_matches_only_the_loop(tmp_path: Path) -> None:
    """The glob matches loop-demo.* but not loop-demo-extra.*."""
    d = tmp_path / "units"
    d.mkdir()
    for name in ("loop-demo.service", "loop-demo.timer", "loop-demo-extra.service", "other.txt"):
        (d / name).write_text("x", encoding="utf-8")
    names = sorted(p.name for p in deploy.loop_unit_files(d, "loop-", "demo"))
    assert names == ["loop-demo.service", "loop-demo.timer"]


# --- path-boundary hardening (review 05) -------------------------------------


def test_resolve_loopctl_command_rejects_non_executable(tmp_path: Path) -> None:
    """An absolute but non-executable loopctl_bin is rejected (finding 1)."""
    binary = tmp_path / "not-exec"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o644)
    config = LoopcraftConfig(
        source_path=tmp_path / "src",
        memory_path=tmp_path / "mem",
        scheduler=SchedulerConfig(loopctl_bin=str(binary)),
    )
    command, problem = deploy.resolve_loopctl_command(config)
    assert command is None
    assert "not an executable file" in problem


def test_resolve_loopctl_command_rejects_directory(tmp_path: Path) -> None:
    """An absolute loopctl_bin pointing at a directory is rejected (finding 1)."""
    directory = tmp_path / "dir"
    directory.mkdir()
    config = LoopcraftConfig(
        source_path=tmp_path / "src",
        memory_path=tmp_path / "mem",
        scheduler=SchedulerConfig(loopctl_bin=str(directory)),
    )
    command, problem = deploy.resolve_loopctl_command(config)
    assert command is None
    assert "not an executable file" in problem


def test_resolve_loopctl_bare_uses_scheduled_path(monkeypatch, tmp_path: Path) -> None:
    """A bare loopctl_bin only on the operator PATH does not resolve (finding 7)."""
    bindir = tmp_path / "opbin"
    bindir.mkdir()
    binary = bindir / "loopctl"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))  # operator PATH only
    config = LoopcraftConfig(
        source_path=tmp_path / "src",
        memory_path=tmp_path / "mem",
        scheduler=SchedulerConfig(loopctl_bin="loopctl"),  # no scheduler.path
    )
    command, problem = deploy.resolve_loopctl_command(config)
    assert command is None
    assert "scheduled PATH" in problem
    # On the scheduled PATH it resolves.
    config2 = config.model_copy(
        update={"scheduler": SchedulerConfig(loopctl_bin="loopctl", path=str(bindir))}
    )
    command2, problem2 = deploy.resolve_loopctl_command(config2)
    assert command2 == [str(binary)]
    assert problem2 is None


def test_plan_flags_bad_unit_prefix(tmp_path: Path) -> None:
    """A bad unit_prefix makes the plan non-renderable (finding 5)."""
    config = _source(
        tmp_path, _CRON_MANIFEST, scheduler=SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path), unit_prefix="../x-")
    )
    plan = deploy.plan_deployment(config, run_preflight=False)
    assert not plan.renderable
    assert any("unit_prefix" in p for p in plan.render_problems)


def test_environment_file_directory_is_problem_not_crash(tmp_path: Path) -> None:
    """A directory environment_file is a structured problem, never a crash (finding 2)."""
    env_dir = tmp_path / "envdir"
    env_dir.mkdir()
    config = _source(
        tmp_path,
        "id: demo\nname: Demo\ncadence: {type: cron, at: '0 9 * * *'}\n"
        "depends_on: {env: [DEMO_TOKEN]}\nlogic: {skill: skills/demo/SKILL.md}\n",
        scheduler=SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path), environment_file=str(env_dir)),
    )
    plan = deploy.plan_deployment(config, run_preflight=False)
    assert any("not a regular file" in p for p in plan.env_problems)


def test_environment_file_relative_is_rejected(tmp_path: Path) -> None:
    """A relative environment_file is rejected (finding 3)."""
    config = _source(
        tmp_path,
        _CRON_MANIFEST,
        scheduler=SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path), environment_file="secrets.env"),
    )
    _keys, problems = deploy.environment_file_health(config)
    assert any("must be an absolute path" in p for p in problems)


def test_environment_file_in_tree_symlink_rejected(tmp_path: Path) -> None:
    """An in-tree env_file symlink to an outside secret is rejected (finding 8)."""
    outside = tmp_path / "real-secrets.env"
    outside.write_text("DEMO_TOKEN=x\n", encoding="utf-8")
    config = _source(tmp_path, _CRON_MANIFEST, scheduler=SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path)))
    stub = config.memory_path / "secrets.env"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.symlink_to(outside)
    config = config.model_copy(
        update={"scheduler": SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path), environment_file=str(stub))}
    )
    _keys, problems = deploy.environment_file_health(config)
    assert any("outside the memory tree" in p for p in problems)
    assert any("must not be a symlink" in p for p in problems)


def test_systemd_unit_dir_honors_xdg(monkeypatch, tmp_path: Path) -> None:
    """User-scope unit dir follows XDG_CONFIG_HOME (finding 9)."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    config = LoopcraftConfig(
        source_path=tmp_path / "src",
        memory_path=tmp_path / "mem",
        scheduler=SchedulerConfig(scope=SystemdScope.USER),
    )
    assert deploy.systemd_unit_dir(config) == xdg / "systemd" / "user"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert deploy.systemd_unit_dir(config) == tmp_path / ".config" / "systemd" / "user"


def test_install_refuses_symlinked_unit(monkeypatch, tmp_path: Path) -> None:
    """Install refuses to write over a symlinked unit and rolls back (finding 12)."""
    config = _source(tmp_path, _CRON_MANIFEST)
    plan = deploy.plan_deployment(config, run_preflight=False)
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    outside = tmp_path / "outside.service"
    outside.write_text("HIJACK\n", encoding="utf-8")
    (unit_dir / "loop-demo.service").symlink_to(outside)
    _fake_systemctl(monkeypatch, unit_dir)
    result = deploy.install_units(config, plan)
    assert not result.ok
    assert result.rolled_back
    assert any("symlink" in p for p in result.problems)
    # The symlink target is untouched.
    assert outside.read_text(encoding="utf-8") == "HIJACK\n"


# --- live probes execute on the scheduled PATH (review 04, finding 1) ---------


def test_scheduled_slack_probe_runs_on_scheduled_path(monkeypatch, tmp_path: Path) -> None:
    """A Slack loop passes scheduled preflight when the fake nv-tools lives on
    scheduler.path and the operator PATH is empty — the live probe executes the
    scheduled binary, not a bare command on the operator PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("loopctl", "codex", "nv-tools"):
        binary = bindir / name
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", "")  # nothing on the operator PATH

    config = _source(
        tmp_path,
        "id: demo\nname: Demo\ncadence: {type: cron, at: '0 9 * * *'}\n"
        "depends_on: {tools: [nv-tools], auth: [nv-tools], apis: [slack]}\n"
        "logic: {skill: skills/demo/SKILL.md}\n",
        scheduler=SchedulerConfig(loopctl_bin=str(bindir / "loopctl"), path=str(bindir)),
    )
    plan = deploy.plan_deployment(config, run_preflight=True)
    assert plan.preflight_problems == []
    assert plan.ok


# --- scheduled credential model in preflight (review 02, finding 1) -----------

_X_API_MANIFEST = (
    "id: demo\nname: Demo\ncadence: {type: cron, at: '0 9 * * *'}\n"
    "depends_on: {auth: [x-api]}\nlogic: {skill: skills/demo/SKILL.md}\n"
)


def test_scheduled_preflight_accepts_token_from_env_file(monkeypatch, tmp_path: Path) -> None:
    """A token only in the environment_file satisfies apply preflight (no x-api problem)."""
    monkeypatch.delenv("X_API_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("X_API_OAUTH2_ACCESS_TOKEN", raising=False)
    env_file = tmp_path / "secrets.env"
    env_file.write_text("X_API_BEARER_TOKEN=from-file\n", encoding="utf-8")
    config = _source(
        tmp_path,
        _X_API_MANIFEST,
        scheduler=SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path), environment_file=str(env_file)),
    )
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _CapabilityRunner())
    plan = deploy.plan_deployment(config, run_preflight=True)
    assert not any("x-api" in p for p in plan.preflight_problems)


def test_scheduled_preflight_ignores_token_only_in_process_env(monkeypatch, tmp_path: Path) -> None:
    """A token only in the operator's shell does NOT satisfy scheduled preflight."""
    monkeypatch.setenv("X_API_BEARER_TOKEN", "from-shell")
    config = _source(
        tmp_path,
        _X_API_MANIFEST,
        scheduler=SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path)),  # no environment_file
    )
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _CapabilityRunner())
    plan = deploy.plan_deployment(config, run_preflight=True)
    assert any("x-api" in p for p in plan.preflight_problems)


def test_scheduled_preflight_satisfies_declared_env_from_env_file(monkeypatch, tmp_path: Path) -> None:
    """A depends_on.env var is satisfied by the environment_file during scheduled preflight."""
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    env_file = tmp_path / "secrets.env"
    env_file.write_text("DEMO_TOKEN=from-file\n", encoding="utf-8")
    config = _source(
        tmp_path,
        "id: demo\nname: Demo\ncadence: {type: cron, at: '0 9 * * *'}\n"
        "depends_on: {env: [DEMO_TOKEN]}\nlogic: {skill: skills/demo/SKILL.md}\n",
        scheduler=SchedulerConfig(loopctl_bin=_abs_loopctl(tmp_path), environment_file=str(env_file)),
    )
    monkeypatch.setattr(deploy, "get_runner", lambda vendor: _CapabilityRunner())
    plan = deploy.plan_deployment(config, run_preflight=True)
    assert not any("DEMO_TOKEN" in p for p in plan.preflight_problems)
