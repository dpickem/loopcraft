"""Tests for cron->OnCalendar translation and systemd unit rendering (M2)."""

from __future__ import annotations

import pytest

from loopcraft.config import LoopcraftConfig, SchedulerConfig, SystemdScope
from loopcraft.manifest import LoopManifest
from loopcraft.scheduler import (
    SchedulerError,
    UnitKind,
    cron_to_oncalendar,
    render_loop_units,
    unit_name,
)


@pytest.mark.parametrize(
    ("cron", "expected"),
    [
        ("0 9,13,17 * * 1-5", "Mon..Fri *-*-* 09,13,17:00:00"),
        ("30 7 * * 1-5", "Mon..Fri *-*-* 07:30:00"),
        ("0 7 * * *", "*-*-* 07:00:00"),
        ("*/15 * * * *", "*-*-* *:00/15:00"),
        ("0 0 1 * *", "*-*-01 00:00:00"),
        ("0 8 * * mon-fri", "Mon..Fri *-*-* 08:00:00"),
        ("0 */4 * * *", "*-*-* 00/4:00:00"),
        ("15 9 1,15 * *", "*-*-01,15 09:15:00"),
        ("0 12 * * 0", "Sun *-*-* 12:00:00"),
        ("0 12 * * 7", "Sun *-*-* 12:00:00"),
        ("0 12 * * 1,3,5", "Mon,Wed,Fri *-*-* 12:00:00"),
    ],
)
def test_cron_to_oncalendar(cron: str, expected: str) -> None:
    """Common cron forms translate to the expected systemd OnCalendar value."""
    assert cron_to_oncalendar(cron) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "0 9 * *",  # only 4 fields
        "0 25 * * *",  # hour out of range
        "99 9 * * *",  # minute out of range
        "0 9 * * 9",  # day-of-week out of range
        "0 9-5 * * *",  # descending range
        "0 9 * * mon/2",  # step day-of-week unsupported
    ],
)
def test_cron_to_oncalendar_rejects_unsupported(bad: str) -> None:
    """Malformed or unsupported cron expressions raise, never mis-translate."""
    with pytest.raises(SchedulerError):
        cron_to_oncalendar(bad)


def _config(tmp_path, scheduler: SchedulerConfig | None = None) -> LoopcraftConfig:
    """Build a config rooted at a temp source/memory tree for rendering."""
    return LoopcraftConfig(
        source_path=tmp_path / "src",
        memory_path=tmp_path / "mem",
        scheduler=scheduler or SchedulerConfig(),
    )


def _cron_manifest() -> LoopManifest:
    """A minimal valid cron loop manifest for rendering tests."""
    return LoopManifest.from_dict(
        {
            "id": "demo",
            "name": "Demo loop",
            "cadence": {"type": "cron", "at": "0 7 * * 1-5"},
            "logic": {"skill": "skills/demo/SKILL.md"},
        }
    )


def test_render_cron_loop_units(tmp_path) -> None:
    """A cron loop renders a service + timer with the expected fields."""
    config = _config(tmp_path)
    units = render_loop_units(config, _cron_manifest())

    assert units.loop == "demo"
    assert units.trigger == "OnCalendar=Mon..Fri *-*-* 07:00:00"
    kinds = {u.kind for u in units.units}
    assert kinds == {UnitKind.SERVICE, UnitKind.TIMER}

    service = next(u for u in units.units if u.kind == UnitKind.SERVICE)
    assert service.filename == "loop-demo.service"
    assert "ExecStart=loopctl run demo" in service.content
    assert f"WorkingDirectory={config.source_path}" in service.content
    assert f"Environment=LOOPCRAFT_MEMORY={config.memory_path}" in service.content

    timer = next(u for u in units.units if u.kind == UnitKind.TIMER)
    assert "OnCalendar=Mon..Fri *-*-* 07:00:00" in timer.content
    assert "Persistent=true" in timer.content
    assert "Unit=loop-demo.service" in timer.content
    assert "WantedBy=timers.target" in timer.content


def test_render_service_honors_scheduler_config(tmp_path) -> None:
    """EnvironmentFile, User, and loopctl_bin flow into the rendered service."""
    scheduler = SchedulerConfig(
        loopctl_bin="/usr/local/bin/loopctl",
        user="loopcraft",
        environment_file="/etc/loopcraft/loopcraft.env",
        scope=SystemdScope.SYSTEM,
    )
    config = _config(tmp_path, scheduler)
    service = next(
        u for u in render_loop_units(config, _cron_manifest()).units if u.kind == UnitKind.SERVICE
    )
    assert "ExecStart=/usr/local/bin/loopctl run demo" in service.content
    assert "EnvironmentFile=/etc/loopcraft/loopcraft.env" in service.content
    assert "User=loopcraft" in service.content


def test_user_scope_omits_user_directive(tmp_path) -> None:
    """User-scope units run as the invoking user, so no User= is emitted."""
    scheduler = SchedulerConfig(scope=SystemdScope.USER, user="ignored")
    config = _config(tmp_path, scheduler)
    service = next(
        u for u in render_loop_units(config, _cron_manifest()).units if u.kind == UnitKind.SERVICE
    )
    assert "User=" not in service.content


def test_render_service_uses_explicit_loopctl_command(tmp_path) -> None:
    """A resolved absolute command flows into ExecStart (finding 1)."""
    config = _config(tmp_path)
    units = render_loop_units(config, _cron_manifest(), loopctl_command="/opt/venv/bin/loopctl")
    service = next(u for u in units.units if u.kind == UnitKind.SERVICE)
    assert "ExecStart=/opt/venv/bin/loopctl run demo" in service.content


def test_render_on_artifact_loop_units(tmp_path) -> None:
    """An on-artifact loop renders a path unit watching its upstream input."""
    config = _config(tmp_path)
    manifest = LoopManifest.from_dict(
        {
            "id": "downstream",
            "name": "Downstream",
            "cadence": {"type": "on-artifact"},
            "inputs": ["state/slack/triage-latest.md", "state/downstream/seen.json"],
            "outputs": ["state/downstream/seen.json", "state/downstream/out.md"],
            "logic": {"skill": "skills/demo/SKILL.md"},
        }
    )
    units = render_loop_units(config, manifest)
    path_unit = next(u for u in units.units if u.kind == UnitKind.PATH)
    resolved = str(config.resolve_state_path("state/slack/triage-latest.md"))
    assert f"PathModified={resolved}" in path_unit.content
    # The self-written cursor (input AND output) must not retrigger the loop.
    cursor = str(config.resolve_state_path("state/downstream/seen.json"))
    assert f"PathModified={cursor}" not in path_unit.content
    assert "Unit=loop-downstream.service" in path_unit.content


def test_on_artifact_path_unit_target_follows_scope(tmp_path) -> None:
    """User-scope path units install under default.target, not multi-user (finding 4)."""
    manifest = LoopManifest.from_dict(
        {
            "id": "downstream",
            "name": "Downstream",
            "cadence": {"type": "on-artifact"},
            "inputs": ["state/slack/triage-latest.md"],
            "outputs": ["state/downstream/out.md"],
            "logic": {"skill": "skills/demo/SKILL.md"},
        }
    )
    system = _config(tmp_path, SchedulerConfig(scope=SystemdScope.SYSTEM))
    user = _config(tmp_path, SchedulerConfig(scope=SystemdScope.USER))
    system_path = next(u for u in render_loop_units(system, manifest).units if u.kind == UnitKind.PATH)
    user_path = next(u for u in render_loop_units(user, manifest).units if u.kind == UnitKind.PATH)
    assert "WantedBy=multi-user.target" in system_path.content
    assert "WantedBy=default.target" in user_path.content


def test_on_artifact_without_watchable_input_raises(tmp_path) -> None:
    """An on-artifact loop with no external ledger input is unrenderable."""
    config = _config(tmp_path)
    manifest = LoopManifest.from_dict(
        {
            "id": "lonely",
            "name": "Lonely",
            "cadence": {"type": "on-artifact"},
            "inputs": ["state/lonely/seen.json"],
            "outputs": ["state/lonely/seen.json"],
            "logic": {"skill": "skills/demo/SKILL.md"},
        }
    )
    with pytest.raises(SchedulerError, match="no watchable ledger input"):
        render_loop_units(config, manifest)


def test_event_cadence_is_not_yet_renderable(tmp_path) -> None:
    """Event cadence has no systemd representation until M8."""
    config = _config(tmp_path)
    manifest = LoopManifest.from_dict(
        {
            "id": "reactive",
            "name": "Reactive",
            "cadence": {"type": "event"},
            "logic": {"skill": "skills/demo/SKILL.md"},
        }
    )
    with pytest.raises(SchedulerError, match="event triggers land in M8"):
        render_loop_units(config, manifest)


def test_unit_name_uses_prefix(tmp_path) -> None:
    """Unit filenames use the configured prefix and unit kind."""
    config = _config(tmp_path, SchedulerConfig(unit_prefix="lc-"))
    assert unit_name(config, "demo", UnitKind.TIMER) == "lc-demo.timer"
