"""Tests for LoopcraftConfig, focused on scheduled-environment resolution (M2)."""

from __future__ import annotations

from pathlib import Path

from loopcraft.config import SYSTEMD_DEFAULT_PATH, LoopcraftConfig, SchedulerConfig


def _make_executable(directory: Path, name: str) -> None:
    """Create an executable stub named ``name`` in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)


def _config(tmp_path: Path, scheduler: SchedulerConfig | None = None) -> LoopcraftConfig:
    """Build a config rooted at a temp tree with an optional scheduler block."""
    return LoopcraftConfig(
        source_path=tmp_path / "src",
        memory_path=tmp_path / "mem",
        scheduler=scheduler or SchedulerConfig(),
    )


def test_env_value_reads_process_env_by_default(monkeypatch, tmp_path: Path) -> None:
    """The default (direct) config reads the live process environment."""
    monkeypatch.setenv("DEMO_TOKEN", "from-shell")
    config = _config(tmp_path)
    assert config.env_value("DEMO_TOKEN") == "from-shell"
    assert config.scheduled_env is False


def test_scheduled_env_value_reads_environment_file(monkeypatch, tmp_path: Path) -> None:
    """scheduled_env_value resolves against the EnvironmentFile, not the shell."""
    monkeypatch.setenv("DEMO_TOKEN", "from-shell")
    env_file = tmp_path / "secrets.env"
    env_file.write_text("DEMO_TOKEN=from-file\n", encoding="utf-8")
    config = _config(tmp_path, SchedulerConfig(environment_file=str(env_file)))
    assert config.scheduled_env_value("DEMO_TOKEN") == "from-file"
    assert config.scheduled_env_value("MISSING") is None


def test_scheduled_env_value_none_when_unconfigured(tmp_path: Path) -> None:
    """With no environment_file, the scheduled service sees no credentials."""
    config = _config(tmp_path)
    assert config.scheduled_env_value("ANYTHING") is None


def test_for_scheduled_preflight_switches_env_source(monkeypatch, tmp_path: Path) -> None:
    """The scheduled copy's env_value reads the file; the original still reads the shell."""
    monkeypatch.setenv("DEMO_TOKEN", "from-shell")
    env_file = tmp_path / "secrets.env"
    env_file.write_text("DEMO_TOKEN=from-file\n", encoding="utf-8")
    config = _config(tmp_path, SchedulerConfig(environment_file=str(env_file)))

    scheduled = config.for_scheduled_preflight()
    assert scheduled.scheduled_env is True
    assert scheduled.env_value("DEMO_TOKEN") == "from-file"
    # The original is untouched — direct `loopctl run` still sees the shell.
    assert config.env_value("DEMO_TOKEN") == "from-shell"


def test_scheduled_path_defaults_to_systemd_default(tmp_path: Path) -> None:
    """Unset scheduler.path falls back to systemd's default service PATH."""
    assert _config(tmp_path).scheduled_path == SYSTEMD_DEFAULT_PATH


def test_scheduled_path_uses_configured_value(tmp_path: Path) -> None:
    """A configured scheduler.path is the scheduled service PATH."""
    config = _config(tmp_path, SchedulerConfig(path="/opt/bin:/usr/bin"))
    assert config.scheduled_path == "/opt/bin:/usr/bin"


def test_which_scheduled_resolves_against_scheduled_path(monkeypatch, tmp_path: Path) -> None:
    """Scheduled which() finds a binary only when the scheduled PATH includes it."""
    tool_dir = tmp_path / "tools"
    _make_executable(tool_dir, "mytool")
    # Even if the binary is on the operator PATH, scheduled resolution must not
    # see it unless the scheduled PATH includes its directory.
    monkeypatch.setenv("PATH", str(tool_dir))

    default_scheduled = _config(tmp_path).for_scheduled_preflight()
    assert default_scheduled.which("mytool") is None

    path_scheduled = _config(
        tmp_path, SchedulerConfig(path=str(tool_dir))
    ).for_scheduled_preflight()
    assert path_scheduled.which("mytool") == str(tool_dir / "mytool")


def test_which_direct_uses_process_path(monkeypatch, tmp_path: Path) -> None:
    """Direct which() resolves against the operator's process PATH."""
    tool_dir = tmp_path / "tools"
    _make_executable(tool_dir, "mytool")
    monkeypatch.setenv("PATH", str(tool_dir))
    assert _config(tmp_path).which("mytool") == str(tool_dir / "mytool")
