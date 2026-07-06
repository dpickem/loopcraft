"""Tests for LoopcraftConfig, focused on scheduled-environment resolution (M2)."""

from __future__ import annotations

from pathlib import Path

from loopcraft.config import LoopcraftConfig, SchedulerConfig


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
