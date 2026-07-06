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

import shlex
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from loopcraft.config import LoopcraftConfig, SystemdScope
from loopcraft.env import parse_env_file
from loopcraft.manifest import LoopManifest, load_all
from loopcraft.paths import assert_under
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
        render_problems: Cadences (or an unresolvable ``loopctl`` command) that
            could not be rendered into correct units.
        env_problems: Scheduled-environment problems (a missing/misplaced
            ``EnvironmentFile`` or declared env vars absent from it).
        preflights: Per-loop adapter preflight results (empty when skipped).
        preflight_ran: Whether adapter preflight was executed.
        units: The rendered units per loop (only for cleanly rendered loops).
    """

    loops_dir: str
    manifest_problems: list[str] = Field(default_factory=list)
    render_problems: list[str] = Field(default_factory=list)
    env_problems: list[str] = Field(default_factory=list)
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
        """All blocking problems across every validation phase."""
        return (
            self.manifest_problems
            + self.render_problems
            + self.env_problems
            + self.preflight_problems
        )

    @property
    def renderable(self) -> bool:
        """Whether units can be safely rendered (manifests + rendering are clean).

        Environment and preflight problems (missing auth/tools/secrets) do not
        corrupt the unit *text*, so they don't block rendering — but they do
        block a default ``apply`` write and any ``--install`` (see ``ok``).
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


def resolve_loopctl_command(config: LoopcraftConfig) -> tuple[str | None, str | None]:
    """Resolve ``scheduler.loopctl_bin`` to an absolute service command.

    A systemd unit does not inherit the operator's interactive shell PATH, so
    the rendered ``ExecStart`` must name an absolute executable. This splits the
    configured command (supporting a multi-word prefix like ``uv run loopctl``),
    resolves its first token to an absolute path (via PATH when not already
    absolute), and returns the reassembled command.

    Returns:
        A ``(command, problem)`` pair: the resolved absolute command with a
        None problem on success, or ``(None, problem)`` when the executable
        cannot be resolved for the systemd context.
    """
    raw = config.scheduler.loopctl_bin.strip()
    if not raw:
        return None, "scheduler.loopctl_bin is empty"
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        return None, f"scheduler.loopctl_bin is not a valid command: {exc}"
    head, *rest = parts
    if Path(head).is_absolute():
        resolved = head if Path(head).exists() else None
    else:
        resolved = shutil.which(head)
    if resolved is None:
        return None, (
            f"scheduler.loopctl_bin '{raw}' cannot be resolved to an absolute "
            "executable for the systemd context (set an absolute path in "
            "[scheduler].loopctl_bin)"
        )
    return " ".join([resolved, *rest]), None


def environment_file_health(config: LoopcraftConfig) -> tuple[set[str], list[str]]:
    """Inspect ``scheduler.environment_file``: return its keys and any problems.

    File-level checks only (existence + placement outside both git trees); the
    per-loop "does it contain the declared vars" check lives in
    :func:`validate_environment`. Shared by ``apply`` and ``auth`` so both agree
    on which keys a scheduled service would see.

    Returns:
        A ``(keys, problems)`` pair. ``keys`` is the set of env var names in the
        file (empty when unset or missing); ``problems`` describes a missing or
        misplaced file.
    """
    env_file = config.scheduler.environment_file
    if not env_file:
        return set(), []
    path = Path(env_file).expanduser()
    if not path.exists():
        return set(), [f"scheduler.environment_file not found: {env_file}"]
    problems: list[str] = []
    for label, root in (("source", config.source_path), ("memory", config.memory_path)):
        try:
            assert_under(root, path, label="environment file")
        except ValueError:
            continue  # good: the secrets file is outside this tree
        problems.append(
            f"scheduler.environment_file must live outside the {label} tree "
            f"(secrets stay out of git): {env_file}"
        )
    return set(parse_env_file(path)), problems


def validate_environment(config: LoopcraftConfig, manifests: list[LoopManifest]) -> list[str]:
    """Validate the scheduled-service environment against declared env vars.

    When ``scheduler.environment_file`` is configured it is treated as the
    authority for a scheduled service's credentials (a service does not see the
    operator's ``.env`` or interactive shell): the file must exist, live outside
    *both* git trees, and contain every env var the deployed loops declare. When
    it is not configured but loops declare env vars, that gap is reported too —
    otherwise ``apply`` could pass on the operator's shell while the service
    later fails at runtime.

    Returns:
        A list of problem strings (empty when the scheduled environment can
        satisfy every declared env var).
    """
    required = sorted({var for m in manifests for var in m.depends_on.env})
    if not config.scheduler.environment_file:
        if required:
            return [
                "no scheduler.environment_file is configured, but loops declare "
                f"env vars {required}; a scheduled service will not inherit them "
                "from .env or the interactive shell"
            ]
        return []

    keys, problems = environment_file_health(config)
    # A missing file already fails clearly; don't also list every var as absent.
    if not any("not found" in problem for problem in problems):
        missing = [var for var in required if var not in keys]
        if missing:
            problems.append(f"scheduler.environment_file is missing declared env vars: {missing}")
    return problems


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
    # A single unresolvable service command breaks every rendered unit, so it is
    # a fleet-wide render problem and nothing is rendered until it is fixed.
    command, command_problem = resolve_loopctl_command(config)
    if command_problem:
        render_problems.append(command_problem)
    else:
        for manifest in catalog.manifests:
            try:
                units.append(render_loop_units(config, manifest, loopctl_command=command))
            except SchedulerError as exc:
                render_problems.append(f"{manifest.id}: {exc}")

    env_problems = validate_environment(config, catalog.manifests)

    preflights: list[LoopPreflight] = []
    if run_preflight:
        preflights = [preflight_loop(config, m) for m in catalog.manifests]

    return DeploymentPlan(
        loops_dir=str(target),
        manifest_problems=catalog.problems,
        render_problems=render_problems,
        env_problems=env_problems,
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
    """Outcome of installing rendered units into systemd.

    Attributes:
        installed: Unit filenames left in place after the call.
        enabled: Trigger units enabled and left running after the call.
        problems: Failures encountered (empty on success).
        rolled_back: Whether a failure triggered a rollback to the prior state.
    """

    installed: list[str] = Field(default_factory=list)
    enabled: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    rolled_back: bool = False

    @property
    def ok(self) -> bool:
        """Whether the install completed without problems."""
        return not self.problems


def install_units(config: LoopcraftConfig, plan: DeploymentPlan) -> InstallResult:
    """Transactionally install rendered units and enable their triggers.

    Requires ``systemctl`` on PATH. Copies every rendered unit into the scope's
    unit directory (backing up any unit it overwrites), runs ``daemon-reload``,
    then ``enable --now`` on each timer/path trigger. If any step fails, the
    invocation rolls back — disabling the triggers it enabled and restoring or
    removing the units it wrote — so a failed ``apply --install`` leaves the
    fleet as it was rather than half-deployed.
    """
    if shutil.which("systemctl") is None:
        return InstallResult(problems=["systemctl not found on PATH; cannot install units"])

    unit_dir = systemd_unit_dir(config)
    base = _systemctl_base(config)
    units = [unit for loop_units in plan.units for unit in loop_units.units]

    # filename -> prior content (None when the file did not exist), so rollback
    # can restore a replaced unit or remove a newly written one.
    backups: dict[str, str | None] = {}
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        for unit in units:
            dest = unit_dir / unit.filename
            backups[unit.filename] = dest.read_text(encoding="utf-8") if dest.exists() else None
            dest.write_text(unit.content, encoding="utf-8")
    except OSError as exc:
        _rollback_install(unit_dir, backups, enabled=[], base=base)
        return InstallResult(problems=[f"could not write units to {unit_dir}: {exc}"], rolled_back=True)

    reload_problem = _run_systemctl([*base, "daemon-reload"])
    if reload_problem:
        _rollback_install(unit_dir, backups, enabled=[], base=base)
        return InstallResult(problems=[reload_problem], rolled_back=True)

    enabled: list[str] = []
    for trigger in _enable_targets(plan):
        problem = _run_systemctl([*base, "enable", "--now", trigger])
        if problem:
            _rollback_install(unit_dir, backups, enabled=enabled, base=base)
            return InstallResult(problems=[problem], rolled_back=True)
        enabled.append(trigger)

    return InstallResult(installed=[unit.filename for unit in units], enabled=enabled)


def _rollback_install(
    unit_dir: Path, backups: dict[str, str | None], *, enabled: list[str], base: list[str]
) -> None:
    """Undo a partial install: disable enabled triggers and restore units.

    Best-effort — each step is attempted regardless of the others so one
    failing cleanup command does not strand the rest — then a final
    ``daemon-reload`` settles the manager.
    """
    for trigger in enabled:
        _run_systemctl([*base, "disable", "--now", trigger])
    for filename, prior in backups.items():
        dest = unit_dir / filename
        if prior is None:
            dest.unlink(missing_ok=True)
        else:
            dest.write_text(prior, encoding="utf-8")
    _run_systemctl([*base, "daemon-reload"])


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
