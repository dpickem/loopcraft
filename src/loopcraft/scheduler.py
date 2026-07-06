"""Render loop manifests into systemd units (the M2 scheduler).

``loopctl apply`` turns each loop's ``cadence`` into deployable systemd units so
the fleet fires on the always-on host whether or not the operator's laptop is
open. A ``cron`` cadence renders a ``.timer`` + ``.service`` pair (with
``Persistent=true`` so a trigger missed while the VM was down is caught up); an
``on-artifact`` cadence renders a ``.path`` + ``.service`` pair that wakes the
loop when an upstream output changes. ``event`` cadence has no unattended systemd
representation yet and is reported as a deployment problem (it lands in M8).

The cron translation and unit rendering here are pure string transforms with no
filesystem or subprocess effects, so they are fully unit-testable offline;
writing and installing the rendered units lives in :mod:`loopcraft.deploy`.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from loopcraft.config import LoopcraftConfig, StatePathError, SystemdScope, is_state_path
from loopcraft.manifest import CadenceType, LoopManifest

#: Inclusive value bounds for each cron time/date field, used to reject
#: out-of-range components before they reach a rendered ``OnCalendar`` line.
_CRON_BOUNDS: dict[str, tuple[int, int]] = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 7),
}

#: cron day-of-week number -> systemd weekday abbreviation. cron treats both 0
#: and 7 as Sunday; systemd uses the ``Sun..Sat`` names.
_CRON_DOW_TO_NAME: dict[int, str] = {
    0: "Sun",
    1: "Mon",
    2: "Tue",
    3: "Wed",
    4: "Thu",
    5: "Fri",
    6: "Sat",
    7: "Sun",
}

#: Accepted lowercase cron day-of-week names -> canonical cron number, so
#: ``mon-fri`` is handled identically to ``1-5``.
_CRON_DOW_NAMES: dict[str, int] = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

#: A single numeric cron token (a plain integer component of a field).
_INT_RE = re.compile(r"^\d+$")


class UnitKind(StrEnum):
    """The systemd unit types loopcraft renders."""

    SERVICE = "service"
    TIMER = "timer"
    PATH = "path"


class SchedulerError(ValueError):
    """Raised when a manifest's cadence cannot be rendered into systemd units."""


class RenderedUnit(BaseModel):
    """One rendered systemd unit file (name + full text content)."""

    kind: UnitKind
    filename: str
    content: str


class LoopUnits(BaseModel):
    """The rendered systemd units for a single loop, plus a schedule summary.

    Attributes:
        loop: The loop id the units belong to.
        units: The rendered unit files (always a ``.service`` plus its trigger).
        trigger: Human-readable trigger summary (the ``OnCalendar`` expression
            or the watched paths) for display in ``apply`` output.
    """

    loop: str
    units: list[RenderedUnit] = Field(default_factory=list)
    trigger: str


def _cron_int(token: str, *, field: str) -> int:
    """Parse one integer cron token and bounds-check it for ``field``.

    Raises:
        SchedulerError: If the token is not an integer or is out of range.
    """
    if not _INT_RE.fullmatch(token):
        raise SchedulerError(f"unsupported {field} value in cron: {token!r}")
    value = int(token)
    lo, hi = _CRON_BOUNDS[field]
    if not lo <= value <= hi:
        raise SchedulerError(f"{field} value {value} out of range {lo}-{hi} in cron")
    return value


def _render_numeric_field(spec: str, *, field: str, width: int) -> str:
    """Translate a numeric cron field (minute/hour/dom/month) to systemd syntax.

    Supports ``*``, single integers, comma lists, inclusive ranges (``a-b`` ->
    ``a..b``), and step values (``*/n`` -> ``0/n``, ``a-b/n`` -> ``a..b/n``).
    Components are zero-padded to ``width`` so rendered lines read like the
    design's ``07:00:00``.

    Raises:
        SchedulerError: If the field uses an unsupported construct.
    """
    spec = spec.strip()
    if spec == "*":
        return "*"

    def pad(value: int) -> str:
        return f"{value:0{width}d}"

    def component(token: str) -> str:
        base, sep, step = token.partition("/")
        step_suffix = ""
        if sep:
            if not _INT_RE.fullmatch(step) or int(step) < 1:
                raise SchedulerError(f"unsupported {field} step in cron: {token!r}")
            step_suffix = f"/{int(step)}"
        if base == "*":
            # cron ``*/n`` starts at the field minimum; systemd spells that
            # ``<min>/n`` (``0/n`` for time fields, ``1/n`` for dom/month).
            start = _CRON_BOUNDS[field][0]
            return f"{pad(start)}{step_suffix}" if step_suffix else "*"
        if "-" in base:
            lo_s, _, hi_s = base.partition("-")
            lo = _cron_int(lo_s, field=field)
            hi = _cron_int(hi_s, field=field)
            if lo > hi:
                raise SchedulerError(f"descending {field} range in cron: {token!r}")
            return f"{pad(lo)}..{pad(hi)}{step_suffix}"
        return f"{pad(_cron_int(base, field=field))}{step_suffix}"

    return ",".join(component(token) for token in spec.split(","))


def _render_dow_field(spec: str) -> str:
    """Translate a cron day-of-week field to a systemd weekday prefix.

    Returns an empty string for ``*`` (every day, so systemd omits the weekday),
    otherwise a comma list of ``Mon``/``Mon..Fri`` style tokens.

    Raises:
        SchedulerError: If the field uses an unsupported construct (e.g. steps).
    """
    spec = spec.strip()
    if spec == "*":
        return ""

    def name(token: str) -> str:
        key = token.strip().lower()
        if key in _CRON_DOW_NAMES:
            return _CRON_DOW_TO_NAME[_CRON_DOW_NAMES[key]]
        return _CRON_DOW_TO_NAME[_cron_int(token, field="dow")]

    def component(token: str) -> str:
        if "/" in token:
            raise SchedulerError(f"step day-of-week is not supported in cron: {token!r}")
        if "-" in token and token.lower() not in _CRON_DOW_NAMES:
            lo_s, _, hi_s = token.partition("-")
            return f"{name(lo_s)}..{name(hi_s)}"
        return name(token)

    return ",".join(component(token) for token in spec.split(","))


def cron_to_oncalendar(expr: str) -> str:
    """Convert a 5-field cron expression into a systemd ``OnCalendar`` value.

    Supports the constructs the initial fleet uses — fixed times, comma lists,
    ranges, ``*/n`` steps, and ``a-b`` day-of-week ranges — and raises for
    anything it cannot faithfully translate, so ``apply`` reports the problem
    instead of silently deploying a wrong schedule.

    Args:
        expr: A standard 5-field cron string
            (``minute hour day-of-month month day-of-week``).

    Returns:
        The equivalent systemd ``OnCalendar`` expression, e.g. ``0 9,13,17 * *
        1-5`` -> ``Mon..Fri *-*-* 09,13,17:00:00``.

    Raises:
        SchedulerError: If the expression is not 5 fields or uses an
            unsupported construct.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise SchedulerError(
            f"cron expression must have 5 fields (got {len(fields)}): {expr!r}"
        )
    minute, hour, dom, month, dow = fields

    minute_c = _render_numeric_field(minute, field="minute", width=2)
    hour_c = _render_numeric_field(hour, field="hour", width=2)
    dom_c = _render_numeric_field(dom, field="dom", width=2)
    month_c = _render_numeric_field(month, field="month", width=2)
    weekday = _render_dow_field(dow)

    date = "*-*-*" if dom_c == "*" and month_c == "*" else f"*-{month_c}-{dom_c}"
    time = f"{hour_c}:{minute_c}:00"
    calendar = f"{date} {time}"
    return f"{weekday} {calendar}" if weekday else calendar


def unit_name(config: LoopcraftConfig, loop_id: str, kind: UnitKind) -> str:
    """Return the systemd unit filename for a loop and unit kind."""
    return f"{config.scheduler.unit_prefix}{loop_id}.{kind.value}"


def _render_service(
    config: LoopcraftConfig, manifest: LoopManifest, loopctl_command: str
) -> RenderedUnit:
    """Render the ``.service`` unit that executes one loop headless.

    The service is ``Type=oneshot`` (a loop run starts, does its work, and
    exits) and runs from the source tree; ``loopctl run`` creates its own
    per-run worktree under the memory tree. Both tree roots are passed through
    the environment so the unit does not depend on the invoking shell, and the
    host's out-of-tree secrets file is referenced (never inlined) when set.

    Args:
        loopctl_command: The command prefix for ``ExecStart`` (before
            ``run <loop>``). Callers pass a resolved, absolute command so a
            scheduled service does not depend on systemd's PATH.
    """
    scheduler = config.scheduler
    lines = [
        "[Unit]",
        f"Description=Loopcraft loop: {manifest.name} ({manifest.id})",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        f"WorkingDirectory={config.source_path}",
        # PATH is set explicitly to exactly the PATH apply resolved runtime/tool
        # binaries against, so the service finds `codex`/`nv-tools`/etc. instead
        # of relying on systemd's ambient (often smaller) default.
        f"Environment=PATH={config.scheduled_path}",
        f"Environment=LOOPCRAFT_SOURCE={config.source_path}",
        f"Environment=LOOPCRAFT_MEMORY={config.memory_path}",
    ]
    if scheduler.environment_file:
        lines.append(f"EnvironmentFile={scheduler.environment_file}")
    if scheduler.scope == SystemdScope.SYSTEM and scheduler.user:
        lines.append(f"User={scheduler.user}")
    lines.append(f"ExecStart={loopctl_command} run {manifest.id}")
    lines.append("")
    return RenderedUnit(
        kind=UnitKind.SERVICE,
        filename=unit_name(config, manifest.id, UnitKind.SERVICE),
        content="\n".join(lines),
    )


def install_target(scope: SystemdScope) -> str:
    """Return the ``[Install] WantedBy=`` target appropriate for a scope.

    ``multi-user.target`` is a system-manager target; user-scope units are
    enabled under ``default.target``. Timers accept ``timers.target`` in both
    scopes, but path units must not point at a system target under a user
    manager or they never enable.
    """
    return "default.target" if scope == SystemdScope.USER else "multi-user.target"


def _render_timer(config: LoopcraftConfig, manifest: LoopManifest, oncalendar: str) -> RenderedUnit:
    """Render the ``.timer`` unit for a cron-cadence loop."""
    service = unit_name(config, manifest.id, UnitKind.SERVICE)
    lines = [
        "[Unit]",
        f"Description=Loopcraft timer for {manifest.id}",
        "",
        "[Timer]",
        f"OnCalendar={oncalendar}",
        # Catch up a trigger missed while the VM was down, then coalesce so a
        # long outage fires the loop once rather than replaying every missed tick.
        "Persistent=true",
        f"Unit={service}",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ]
    return RenderedUnit(
        kind=UnitKind.TIMER,
        filename=unit_name(config, manifest.id, UnitKind.TIMER),
        content="\n".join(lines),
    )


def _render_path(config: LoopcraftConfig, manifest: LoopManifest, watched: list[str]) -> RenderedUnit:
    """Render the ``.path`` unit that wakes an on-artifact loop on input change."""
    service = unit_name(config, manifest.id, UnitKind.SERVICE)
    lines = [
        "[Unit]",
        f"Description=Loopcraft artifact trigger for {manifest.id}",
        "",
        "[Path]",
        *[f"PathModified={path}" for path in watched],
        f"Unit={service}",
        "",
        "[Install]",
        f"WantedBy={install_target(config.scheduler.scope)}",
        "",
    ]
    return RenderedUnit(
        kind=UnitKind.PATH,
        filename=unit_name(config, manifest.id, UnitKind.PATH),
        content="\n".join(lines),
    )


def _watched_inputs(config: LoopcraftConfig, manifest: LoopManifest) -> list[str]:
    """Resolve the ledger inputs an on-artifact loop should watch.

    Only ledger ``state/...`` inputs are watchable files; a self-referential
    cursor the loop also *writes* is excluded so the loop does not retrigger
    itself.

    Raises:
        SchedulerError: If the loop declares no watchable ledger input.
    """
    declared_outputs = {out.strip() for out in manifest.outputs}
    watched: list[str] = []
    for declared in manifest.inputs:
        if declared.strip() in declared_outputs or not is_state_path(declared):
            continue
        try:
            watched.append(str(config.resolve_state_path(declared)))
        except StatePathError:
            continue
    if not watched:
        raise SchedulerError(
            f"on-artifact loop '{manifest.id}' declares no watchable ledger input "
            "(needs a state/... input produced by an upstream loop)"
        )
    return watched


def render_loop_units(
    config: LoopcraftConfig, manifest: LoopManifest, *, loopctl_command: str | None = None
) -> LoopUnits:
    """Render every systemd unit for one loop from its cadence.

    Args:
        config: Resolved control-plane config (supplies host/scheduler settings).
        manifest: The loop manifest to render.
        loopctl_command: Resolved command prefix for the service ``ExecStart``.
            Defaults to the raw ``scheduler.loopctl_bin``; the deploy planner
            passes an absolute-resolved command so a scheduled service does not
            depend on systemd's PATH.

    Returns:
        The rendered service plus its trigger unit and a schedule summary.

    Raises:
        SchedulerError: If the cadence cannot be represented as systemd units
            (missing cron expression, unsupported cron construct, no watchable
            input for on-artifact, or the not-yet-supported ``event`` cadence).
    """
    command = loopctl_command or config.scheduler.loopctl_bin
    service = _render_service(config, manifest, command)
    cadence = manifest.cadence

    if cadence.type == CadenceType.CRON:
        if not cadence.at:
            raise SchedulerError(f"cron loop '{manifest.id}' is missing cadence.at")
        oncalendar = cron_to_oncalendar(cadence.at)
        timer = _render_timer(config, manifest, oncalendar)
        return LoopUnits(loop=manifest.id, units=[service, timer], trigger=f"OnCalendar={oncalendar}")

    if cadence.type == CadenceType.ON_ARTIFACT:
        watched = _watched_inputs(config, manifest)
        path_unit = _render_path(config, manifest, watched)
        return LoopUnits(
            loop=manifest.id,
            units=[service, path_unit],
            trigger="watch " + ", ".join(watched),
        )

    raise SchedulerError(
        f"cadence '{cadence.type}' for loop '{manifest.id}' has no systemd "
        "representation yet (event triggers land in M8)"
    )
