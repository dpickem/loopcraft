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

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from loopcraft.config import LoopcraftConfig, SystemdScope
from loopcraft.env import parse_env_file
from loopcraft.manifest import LoopManifest, load_all
from loopcraft.paths import is_lexically_under
from loopcraft.runners import get_runner
from loopcraft.scheduler import LoopUnits, SchedulerError, UnitKind, render_loop_units

#: Systemd unit directory for system scope. User scope is resolved at install
#: time from the XDG config location (see ``systemd_unit_dir``).
_SYSTEM_UNIT_DIR = Path("/etc/systemd/system")

#: Seconds allowed for a single ``systemctl`` invocation during install.
_SYSTEMCTL_TIMEOUT_S = 30

#: Rendered unit-file suffixes loopcraft owns (used when enumerating a loop's
#: units for install-state and removal).
_UNIT_SUFFIXES = frozenset({".service", ".timer", ".path"})


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


def resolve_loopctl_command(config: LoopcraftConfig) -> tuple[list[str] | None, str | None]:
    """Resolve ``scheduler.loopctl_bin`` to an absolute, executable service argv.

    A systemd unit does not inherit the operator's interactive shell PATH, so
    the rendered ``ExecStart`` must name an absolute executable. This splits the
    configured command (supporting a multi-word prefix like ``uv run loopctl``)
    and resolves its first token:

    - an absolute path must be a regular executable file;
    - a bare name is resolved against the **scheduled** PATH (``scheduler.path``),
      not the operator PATH, so ``apply`` cannot resolve a binary the deployed
      service would not find.

    Returns:
        A ``(argv, problem)`` pair: the resolved argv (list) with a None problem
        on success, or ``(None, problem)`` when the executable cannot be resolved
        or is not executable for the systemd context.
    """
    raw = config.scheduler.loopctl_bin.strip()
    if not raw:
        return None, "scheduler.loopctl_bin is empty"
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        return None, f"scheduler.loopctl_bin is not a valid command: {exc}"
    head, *rest = parts
    head = os.path.expanduser(head)
    if os.path.isabs(head):
        if not (Path(head).is_file() and os.access(head, os.X_OK)):
            return None, (
                f"scheduler.loopctl_bin '{head}' is not an executable file"
            )
        resolved: str | None = head
    else:
        resolved = shutil.which(head, path=config.scheduled_path)
        if resolved is None:
            return None, (
                f"scheduler.loopctl_bin '{raw}' cannot be resolved to an executable "
                "on the scheduled PATH (set an absolute path in [scheduler].loopctl_bin "
                "or add its directory to [scheduler].path)"
            )
    return [resolved, *rest], None


def environment_file_health(config: LoopcraftConfig) -> tuple[set[str], list[str]]:
    """Inspect ``scheduler.environment_file``: return its keys and any problems.

    File-level checks only (absolute, placement, symlink, existence, regular
    file, readability); the per-loop "does it contain the declared vars" check
    lives in :func:`validate_environment`. Shared by ``apply`` and ``auth`` so
    both agree on which keys a scheduled service would see. Never raises — a
    misconfigured path is a structured problem, not a traceback.

    Returns:
        A ``(keys, problems)`` pair. ``keys`` is the set of env var names in the
        file (empty when unset or unusable); ``problems`` describes any issue.
    """
    env_file = config.scheduler.environment_file
    if not env_file:
        return set(), []
    path = Path(env_file).expanduser()

    if not path.is_absolute():
        return set(), [f"scheduler.environment_file must be an absolute path: {env_file!r}"]

    problems: list[str] = []
    # Lexical (pre-resolve) containment: a stub inside a git tree is rejected
    # even if its symlink target is outside.
    for label, root in (("source", config.source_path), ("memory", config.memory_path)):
        if is_lexically_under(path, root):
            problems.append(
                f"scheduler.environment_file must live outside the {label} tree "
                f"(secrets stay out of git): {env_file}"
            )
    if path.is_symlink():
        problems.append(f"scheduler.environment_file must not be a symlink: {env_file}")
    if not path.exists():
        problems.append(f"scheduler.environment_file not found: {env_file}")
        return set(), problems
    if not path.is_file():
        problems.append(f"scheduler.environment_file is not a regular file: {env_file}")
        return set(), problems
    try:
        keys = set(parse_env_file(path))
    except OSError as exc:
        problems.append(f"scheduler.environment_file could not be read: {exc}")
        return set(), problems
    return keys, problems


def validate_environment(config: LoopcraftConfig, manifests: list[LoopManifest]) -> list[str]:
    """Validate the scheduled-service environment against declared env vars.

    When ``scheduler.environment_file`` is configured it is treated as the
    authority for a scheduled service's credentials (a service does not see the
    operator's ``.env`` or interactive shell): the file must be an absolute,
    non-symlink regular file outside *both* git trees, and contain every env var
    the deployed loops declare. When it is not configured but loops declare env
    vars, that gap is reported too — otherwise ``apply`` could pass on the
    operator's shell while the service later fails at runtime.

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
    # Only check declared vars once the file itself is healthy; otherwise the
    # file-level problem already explains why nothing can be satisfied.
    if not problems:
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

    # Structural scheduler config problems (unit_prefix, path) make rendering
    # unsafe/ambiguous, as does an unresolvable service command — both are
    # fleet-wide render problems, so nothing is rendered until they are fixed.
    render_problems: list[str] = list(config.scheduler.problems())
    units: list[LoopUnits] = []
    if not render_problems:
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
        # Preflight for deployment must see what the scheduled service will see
        # (its EnvironmentFile), not the operator's shell — so a token only in
        # .env cannot make apply pass, and a token only in the EnvironmentFile
        # is accepted. Direct `loopctl run` keeps using the process env.
        scheduled = config.for_scheduled_preflight()
        preflights = [preflight_loop(scheduled, m) for m in catalog.manifests]

    return DeploymentPlan(
        loops_dir=str(target),
        manifest_problems=catalog.problems,
        render_problems=render_problems,
        env_problems=env_problems,
        preflights=preflights,
        preflight_ran=run_preflight,
        units=units,
    )


def _safe_unit_dest(directory: Path, filename: str) -> Path:
    """Compose ``directory / filename`` after asserting it stays a direct child.

    Defense-in-depth for rendered unit filenames: even though ``unit_prefix`` is
    validated before rendering, a filename must be a single path component so it
    cannot escape the staging / unit directory.

    Examples:
        - allowed: ``loop-slack-triage.service``, ``loop-demo.timer``
        - rejected: ``../escape.service``, ``sub/loop-demo.timer``,
          ``/etc/systemd/system/x.service`` (all contain a path separator or
          ``..``, so they are not a single filename component)

    Raises:
        ValueError: If ``filename`` is not a plain filename.
    """
    if filename != Path(filename).name or os.sep in filename or (os.altsep and os.altsep in filename):
        raise ValueError(f"unsafe unit filename: {filename!r}")
    return directory / filename


def write_units(plan: DeploymentPlan, out_dir: Path) -> list[Path]:
    """Write every rendered unit in ``plan`` to ``out_dir``; return the paths.

    The directory is created if needed. Only call this when ``plan.renderable``
    is True (structurally valid + cleanly rendered).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for loop_units in plan.units:
        for unit in loop_units.units:
            path = _safe_unit_dest(out_dir, unit.filename)
            path.write_text(unit.content, encoding="utf-8")
            written.append(path)
    return written


def systemd_unit_dir(config: LoopcraftConfig) -> Path:
    """Return the real systemd unit directory for the configured scope.

    User scope follows the XDG convention ``systemctl --user`` uses:
    ``$XDG_CONFIG_HOME/systemd/user`` when set, else ``~/.config/systemd/user``.
    """
    if config.scheduler.scope == SystemdScope.USER:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
        return base / "systemd" / "user"
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

    # 1. Write unit files, recording a backup of each one replaced (None when the
    #    file is new) so a later failure can restore or remove exactly what we
    #    touched.
    backups: dict[str, str | None] = {}
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        for unit in units:
            dest = _safe_unit_dest(unit_dir, unit.filename)
            # A symlink here would make read/write operate on its target outside
            # the unit dir (and install may run privileged for system scope).
            if dest.is_symlink():
                return _install_failure(
                    f"refusing to install over a symlinked unit: {dest}",
                    unit_dir, backups, enabled=[], base=base,
                )
            backups[unit.filename] = dest.read_text(encoding="utf-8") if dest.exists() else None
            dest.write_text(unit.content, encoding="utf-8")
    except (OSError, ValueError) as exc:
        return _install_failure(
            f"could not write units to {unit_dir}: {exc}", unit_dir, backups, enabled=[], base=base
        )

    # 2. Reload so systemd picks up the new unit files before we enable them.
    reload_problem = _run_systemctl([*base, "daemon-reload"])
    if reload_problem:
        return _install_failure(reload_problem, unit_dir, backups, enabled=[], base=base)

    # 3. Enable + start each trigger; on the first failure roll back the ones
    #    already enabled (plus the written files).
    enabled: list[str] = []
    for trigger in _enable_targets(plan):
        problem = _run_systemctl([*base, "enable", "--now", trigger])
        if problem:
            return _install_failure(problem, unit_dir, backups, enabled=enabled, base=base)
        enabled.append(trigger)

    return InstallResult(installed=[unit.filename for unit in units], enabled=enabled)


def _install_failure(
    problem: str,
    unit_dir: Path,
    backups: dict[str, str | None],
    *,
    enabled: list[str],
    base: list[str],
) -> InstallResult:
    """Build a failed :class:`InstallResult`, attempting a rollback first.

    ``rolled_back`` reflects whether the rollback actually restored the prior
    state: it is True only when every rollback step succeeded. Any rollback
    problems are appended so the operator is told the fleet may be in a partial
    state and what to clean up, rather than seeing a misleading ``rolled_back``.
    """
    rollback_problems = _rollback_install(unit_dir, backups, enabled=enabled, base=base)
    return InstallResult(
        problems=[problem, *rollback_problems],
        rolled_back=not rollback_problems,
    )


def _rollback_install(
    unit_dir: Path, backups: dict[str, str | None], *, enabled: list[str], base: list[str]
) -> list[str]:
    """Undo a partial install: disable enabled triggers and restore units.

    Best-effort — every step is attempted regardless of the others so one
    failing cleanup does not strand the rest — but each failure is collected and
    returned (rather than swallowed), so the caller can report that the rollback
    itself was incomplete.

    Returns:
        The rollback problems (empty when the prior state was fully restored).
    """
    problems: list[str] = []
    for trigger in enabled:
        problem = _run_systemctl([*base, "disable", "--now", trigger])
        if problem:
            problems.append(f"rollback: disabling {trigger}: {problem}")
    for filename, prior in backups.items():
        dest = unit_dir / filename
        try:
            if prior is None:
                dest.unlink(missing_ok=True)
            else:
                dest.write_text(prior, encoding="utf-8")
        except OSError as exc:
            problems.append(f"rollback: could not restore {dest}: {exc}")
    reload_problem = _run_systemctl([*base, "daemon-reload"])
    if reload_problem:
        problems.append(f"rollback: {reload_problem}")
    return problems


def loop_unit_files(directory: Path, prefix: str, selector: str) -> list[Path]:
    """Return a loop's rendered unit files in ``directory``.

    ``selector`` is a canonical loop id, or ``"*"`` to match every loop. The
    ``{prefix}{selector}.*`` glob matches only that loop's units (the literal
    ``.`` after the id prevents ``loop-demo`` from also matching
    ``loop-demo-extra``), filtered to loopcraft's unit suffixes.
    """
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.glob(f"{prefix}{selector}.*")
        if path.suffix in _UNIT_SUFFIXES and (path.is_file() or path.is_symlink())
    )


class RemovalPlan(BaseModel):
    """The units a ``remove`` would disable/delete, computed without side effects."""

    installed: list[str] = Field(default_factory=list)
    staged: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        """Whether there is nothing to remove."""
        return not (self.installed or self.staged)


class UninstallResult(BaseModel):
    """Outcome of removing (undeploying) a loop's units."""

    disabled: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    removed_staged: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the removal completed without problems."""
        return not self.problems


def plan_removal(config: LoopcraftConfig, selectors: list[str]) -> RemovalPlan:
    """Enumerate the installed + staged units ``remove`` would act on.

    Works purely from the files on disk (not the current manifests), so a loop
    can be undeployed even after its manifest was edited or deleted.
    """
    prefix = config.scheduler.unit_prefix
    unit_dir = systemd_unit_dir(config)
    stage_dir = config.systemd_stage_dir
    installed: list[Path] = []
    staged: list[Path] = []
    for selector in selectors:
        installed += loop_unit_files(unit_dir, prefix, selector)
        staged += loop_unit_files(stage_dir, prefix, selector)
    triggers = [p.name for p in installed if p.suffix in {".timer", ".path"}]
    return RemovalPlan(
        installed=[str(p) for p in installed],
        staged=[str(p) for p in staged],
        triggers=triggers,
    )


def uninstall_units(config: LoopcraftConfig, selectors: list[str]) -> UninstallResult:
    """Undeploy loops: disable their triggers and delete installed + staged units.

    The inverse of ``apply``/``apply --install``. Installed triggers are
    ``disable --now``'d before their files are removed (requires ``systemctl``);
    staged files are always removed. Best-effort: each step's failure is recorded
    as a problem rather than raised, and a final ``daemon-reload`` settles the
    manager after installed units change.
    """
    result = UninstallResult()
    plan = plan_removal(config, selectors)
    unit_dir = systemd_unit_dir(config)
    base = _systemctl_base(config)

    if plan.installed:
        if shutil.which("systemctl") is None:
            result.problems.append(
                "systemctl not found on PATH; installed units left in place "
                f"(remove manually from {unit_dir})"
            )
        else:
            for trigger in plan.triggers:
                problem = _run_systemctl([*base, "disable", "--now", trigger])
                if problem:
                    result.problems.append(problem)
                else:
                    result.disabled.append(trigger)
            for path_str in plan.installed:
                removed = _remove_file(Path(path_str), result)
                if removed:
                    result.removed.append(Path(path_str).name)
            _run_systemctl([*base, "daemon-reload"])

    for path_str in plan.staged:
        if _remove_file(Path(path_str), result):
            result.removed_staged.append(Path(path_str).name)
    return result


def _remove_file(path: Path, result: UninstallResult) -> bool:
    """Delete one unit file, recording a problem on failure; return success."""
    try:
        path.unlink()
    except OSError as exc:
        result.problems.append(f"could not remove {path}: {exc}")
        return False
    return True


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
