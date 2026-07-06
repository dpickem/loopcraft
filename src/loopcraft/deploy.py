"""Pre-deploy validation and systemd deployment planning (the M2 ``apply``).

This module is the gate the design's exit criterion names: *an unmet dependency
is reported at ``apply``, not at runtime*. :func:`plan_deployment` validates the
whole fleet before anything is written — manifest schema, the cross-loop DAG
(duplicate ids, multi-producer outputs, unknown upstream loops, cycles), and
each loop's adapter preflight (tools/auth/env/api) — and renders each manifest's
cadence into systemd units. Writing and installing those units is separated into
:func:`write_units` / :func:`install_units` so validation stays free of side
effects and easy to test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from loopcraft.config import LoopcraftConfig, SystemdScope
from loopcraft.manifest import LoopManifest, load_all
from loopcraft.runners import get_runner
from loopcraft.scheduler import LoopUnits, SchedulerError, UnitKind, render_loop_units

#: Systemd unit directory per scope, relative to the manager's root.
_SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
#: User-scope units live under the user's XDG config; resolved at install time.
_USER_UNIT_SUBPATH = (".config", "systemd", "user")

#: Seconds allowed for a single ``systemctl`` invocation during install.
_SYSTEMCTL_TIMEOUT_S = 30


class LoopPreflight(BaseModel):
    """One loop's adapter preflight result, flattened for aggregate reporting."""

    loop: str
    vendor: str
    ok: bool
    problems: list[str] = Field(default_factory=list)


class DeploymentPlan(BaseModel):
    """The result of validating + rendering the fleet, without side effects.

    Attributes:
        loops_dir: The loops directory that was planned.
        manifest_problems: Structural manifest/DAG problems (from ``load_all``).
        render_problems: Cadences that could not be rendered into units.
        preflights: Per-loop adapter preflight results (empty when skipped).
        preflight_ran: Whether adapter preflight was executed.
        units: The rendered units per loop (only for cleanly rendered loops).
    """

    loops_dir: str
    manifest_problems: list[str] = Field(default_factory=list)
    render_problems: list[str] = Field(default_factory=list)
    preflights: list[LoopPreflight] = Field(default_factory=list)
    preflight_ran: bool = False
    units: list[LoopUnits] = Field(default_factory=list)

    @property
    def preflight_problems(self) -> list[str]:
        """Every preflight problem, prefixed by its loop id."""
        return [
            f"{pf.loop}: {problem}"
            for pf in self.preflights
            for problem in pf.problems
        ]

    @property
    def problems(self) -> list[str]:
        """All blocking problems across manifest, render, and preflight phases."""
        return self.manifest_problems + self.render_problems + self.preflight_problems

    @property
    def renderable(self) -> bool:
        """Whether units can be safely written (manifests + rendering are clean).

        Preflight problems (missing auth/tools) do not block *rendering* the
        unit files — they are just text — but they do block ``--install``.
        """
        return not self.manifest_problems and not self.render_problems

    @property
    def ok(self) -> bool:
        """Whether the fleet is fully deployable (no problem in any phase)."""
        return not self.problems


def preflight_loop(config: LoopcraftConfig, manifest: LoopManifest) -> LoopPreflight:
    """Run one loop's adapter preflight, normalizing faults to a failed result.

    An unknown vendor or a raising adapter becomes a failed :class:`LoopPreflight`
    rather than an exception, so aggregate validation never aborts on one loop.
    """
    vendor = manifest.effective_vendor(config.default_vendor)
    try:
        runner = get_runner(vendor)
    except ValueError as exc:
        return LoopPreflight(loop=manifest.id, vendor=vendor, ok=False, problems=[str(exc)])
    try:
        report = runner.preflight(manifest, config)
    except Exception as exc:  # noqa: BLE001 — a faulty adapter must not abort the plan
        return LoopPreflight(
            loop=manifest.id,
            vendor=vendor,
            ok=False,
            problems=[f"preflight raised {type(exc).__name__}: {exc}"],
        )
    return LoopPreflight(loop=manifest.id, vendor=vendor, ok=report.ok, problems=report.problems)


def plan_deployment(
    config: LoopcraftConfig,
    *,
    loops_dir: Path | None = None,
    run_preflight: bool = True,
) -> DeploymentPlan:
    """Validate the fleet and render systemd units, with no side effects.

    Args:
        config: Resolved control-plane config.
        loops_dir: Override for the loops directory (defaults to the config's).
        run_preflight: When True, run each loop's adapter preflight so unmet
            auth/tool/env dependencies are reported here at ``apply`` time.

    Returns:
        A :class:`DeploymentPlan` capturing every problem and the units that
        would be written for cleanly rendered loops.
    """
    target = loops_dir or config.loops_dir
    catalog = load_all(target)

    render_problems: list[str] = []
    units: list[LoopUnits] = []
    for manifest in catalog.manifests:
        try:
            units.append(render_loop_units(config, manifest))
        except SchedulerError as exc:
            render_problems.append(f"{manifest.id}: {exc}")

    preflights: list[LoopPreflight] = []
    if run_preflight:
        preflights = [preflight_loop(config, m) for m in catalog.manifests]

    return DeploymentPlan(
        loops_dir=str(target),
        manifest_problems=catalog.problems,
        render_problems=render_problems,
        preflights=preflights,
        preflight_ran=run_preflight,
        units=units,
    )


def write_units(plan: DeploymentPlan, out_dir: Path) -> list[Path]:
    """Write every rendered unit in ``plan`` to ``out_dir``; return the paths.

    The directory is created if needed. Only call this when ``plan.renderable``
    is True (structurally valid + cleanly rendered).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for loop_units in plan.units:
        for unit in loop_units.units:
            path = out_dir / unit.filename
            path.write_text(unit.content, encoding="utf-8")
            written.append(path)
    return written


def systemd_unit_dir(config: LoopcraftConfig) -> Path:
    """Return the real systemd unit directory for the configured scope."""
    if config.scheduler.scope == SystemdScope.USER:
        return Path.home().joinpath(*_USER_UNIT_SUBPATH)
    return _SYSTEM_UNIT_DIR


def _systemctl_base(config: LoopcraftConfig) -> list[str]:
    """Return the ``systemctl`` argv prefix for the configured scope."""
    base = ["systemctl"]
    if config.scheduler.scope == SystemdScope.USER:
        base.append("--user")
    return base


def _enable_targets(plan: DeploymentPlan) -> list[str]:
    """Return the trigger unit names (timers/paths) to enable per loop.

    The service is activated by its trigger, so only the ``.timer`` / ``.path``
    units are enabled.
    """
    return [
        unit.filename
        for loop_units in plan.units
        for unit in loop_units.units
        if unit.kind in (UnitKind.TIMER, UnitKind.PATH)
    ]


class InstallResult(BaseModel):
    """Outcome of installing rendered units into systemd."""

    installed: list[str] = Field(default_factory=list)
    enabled: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the install completed without problems."""
        return not self.problems


def install_units(config: LoopcraftConfig, plan: DeploymentPlan) -> InstallResult:
    """Copy rendered units into the systemd unit dir and enable their triggers.

    Requires ``systemctl`` on PATH. Copies every rendered unit to the scope's
    unit directory, runs ``daemon-reload``, then ``enable --now`` on each timer/
    path trigger. Any failing step is captured as a problem rather than raising,
    so the CLI can report a partial install cleanly.
    """
    if shutil.which("systemctl") is None:
        return InstallResult(problems=["systemctl not found on PATH; cannot install units"])

    unit_dir = systemd_unit_dir(config)
    result = InstallResult()
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        for loop_units in plan.units:
            for unit in loop_units.units:
                (unit_dir / unit.filename).write_text(unit.content, encoding="utf-8")
                result.installed.append(unit.filename)
    except OSError as exc:
        result.problems.append(f"could not write units to {unit_dir}: {exc}")
        return result

    base = _systemctl_base(config)
    reload_problem = _run_systemctl([*base, "daemon-reload"])
    if reload_problem:
        result.problems.append(reload_problem)
        return result

    for trigger in _enable_targets(plan):
        problem = _run_systemctl([*base, "enable", "--now", trigger])
        if problem:
            result.problems.append(problem)
        else:
            result.enabled.append(trigger)
    return result


def _run_systemctl(cmd: list[str]) -> str | None:
    """Run one ``systemctl`` command; return a problem string or None on success."""
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_SYSTEMCTL_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"{' '.join(cmd)} failed: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return f"{' '.join(cmd)} exited {completed.returncode}: {detail}"
    return None
