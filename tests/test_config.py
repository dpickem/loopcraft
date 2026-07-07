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


# --- default uv-managed loopctl (review 06) ----------------------------------


def _venv_loopctl(source: Path) -> Path:
    """Create a fake uv-managed loopctl under ``source/.venv/bin`` and return it."""
    venv_bin = source / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    binary = venv_bin / "loopctl"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_config_defaults_loopctl_to_venv(tmp_path: Path) -> None:
    """A clean uv checkout defaults loopctl_bin to .venv/bin/loopctl (finding 1)."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "loopcraft.toml").write_text("", encoding="utf-8")
    binary = _venv_loopctl(source)
    config = LoopcraftConfig.load(source)
    assert config.scheduler.loopctl_bin == str(binary)


def test_config_respects_explicit_loopctl_bin(tmp_path: Path) -> None:
    """An explicit [scheduler].loopctl_bin is not overridden by the venv default."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "loopcraft.toml").write_text(
        '[scheduler]\nloopctl_bin = "/opt/loopctl"\n', encoding="utf-8"
    )
    _venv_loopctl(source)
    config = LoopcraftConfig.load(source)
    assert config.scheduler.loopctl_bin == "/opt/loopctl"


def test_config_loopctl_default_stays_bare_without_venv(tmp_path: Path) -> None:
    """Without a project venv, the default stays the bare name (strict on a host)."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "loopcraft.toml").write_text("", encoding="utf-8")
    config = LoopcraftConfig.load(source)
    assert config.scheduler.loopctl_bin == "loopctl"


# --- scheduler config validation (review 05) ---------------------------------


def test_scheduler_problems_flag_bad_unit_prefix(tmp_path: Path) -> None:
    """A unit_prefix with path separators/traversal is rejected (finding 5)."""
    problems = SchedulerConfig(unit_prefix="../../escape-").problems()
    assert any("unit_prefix" in p for p in problems)


def test_scheduler_problems_flag_relative_path_entry(tmp_path: Path) -> None:
    """A relative or empty scheduler.path entry is rejected (finding 4)."""
    assert any("not an absolute" in p for p in SchedulerConfig(path="tools").problems())
    assert any("empty component" in p for p in SchedulerConfig(path="/usr/bin:").problems())
    assert SchedulerConfig(path="/usr/bin:/bin").problems() == []


def test_scheduled_path_expands_user(monkeypatch, tmp_path: Path) -> None:
    """scheduled_path expands ~ per component for both which() and rendering."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = _config(tmp_path, SchedulerConfig(path="~/bin:/usr/bin"))
    assert config.scheduled_path == f"{tmp_path}/bin:/usr/bin"


def test_probe_env_excludes_operator_only_vars(monkeypatch, tmp_path: Path) -> None:
    """Scheduled probe_env starts from a minimal baseline (finding 13)."""
    monkeypatch.setenv("HTTPS_PROXY", "http://evil:8080")
    monkeypatch.setenv("HOME", str(tmp_path))
    env = _config(tmp_path).for_scheduled_preflight().probe_env()
    assert env is not None
    assert "HTTPS_PROXY" not in env
    assert env["PATH"] == _config(tmp_path).scheduled_path
    assert env["HOME"] == str(tmp_path)


# --- content-config containment (review 05, findings 16/17) ------------------


def _source_config(tmp_path: Path) -> LoopcraftConfig:
    """A config whose source tree has a config/ directory."""
    source = tmp_path / "src"
    (source / "config").mkdir(parents=True)
    return LoopcraftConfig(source_path=source, memory_path=tmp_path / "mem")


def test_resolve_content_config_rejects_absolute(tmp_path: Path) -> None:
    """An absolute --config path is rejected (finding 16)."""
    import pytest

    from loopcraft.config import SourcePathError

    config = _source_config(tmp_path)
    with pytest.raises(SourcePathError):
        config.resolve_content_config("/etc/passwd")


def test_resolve_content_config_rejects_traversal(tmp_path: Path) -> None:
    """A traversing --config path is rejected (finding 16)."""
    import pytest

    from loopcraft.config import SourcePathError

    config = _source_config(tmp_path)
    with pytest.raises(SourcePathError):
        config.resolve_content_config("../../etc/passwd")


def test_resolve_content_config_prefers_local(tmp_path: Path) -> None:
    """A .local sibling under source is preferred and returned."""
    config = _source_config(tmp_path)
    (config.source_path / "config" / "c.yaml").write_text("a: 1\n", encoding="utf-8")
    local = config.source_path / "config" / "c.local.yaml"
    local.write_text("a: 2\n", encoding="utf-8")
    assert config.resolve_content_config("config/c.yaml") == local


def test_resolve_content_config_rejects_local_symlink_escape(tmp_path: Path) -> None:
    """A .local override symlinked outside the source tree is rejected (finding 17)."""
    import pytest

    from loopcraft.config import SourcePathError

    config = _source_config(tmp_path)
    (config.source_path / "config" / "c.yaml").write_text("a: 1\n", encoding="utf-8")
    outside = tmp_path / "outside.yaml"
    outside.write_text("a: 9\n", encoding="utf-8")
    (config.source_path / "config" / "c.local.yaml").symlink_to(outside)
    with pytest.raises(SourcePathError):
        config.resolve_content_config("config/c.yaml")
