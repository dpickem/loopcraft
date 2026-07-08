"""Tests for the read-only `loopctl vendor` command (M3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopcraft import cli


def _source(monkeypatch, tmp_path: Path, toml: str = 'default_vendor = "codex"\n') -> Path:
    """Point loopctl at a temp source tree carrying a loopcraft.toml."""
    source = tmp_path / "src"
    (source / "loops").mkdir(parents=True)
    (source / "loopcraft.toml").write_text(toml, encoding="utf-8")
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(source))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))
    monkeypatch.delenv("LOOPCRAFT_VENDOR", raising=False)
    return source


def test_vendor_get(monkeypatch, tmp_path: Path, capsys) -> None:
    """`vendor get` reports the configured default."""
    _source(monkeypatch, tmp_path)
    rc = cli.main(["--json", "vendor", "get"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["data"]["default"] == "codex"


def test_vendor_list_marks_default(monkeypatch, tmp_path: Path, capsys) -> None:
    """`vendor list` includes all adapters and reports the default."""
    _source(monkeypatch, tmp_path)
    rc = cli.main(["--json", "vendor", "list"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    # Subset: other test modules may register stub vendors into the shared registry.
    assert {"claude", "codex", "cursor"}.issubset(payload["data"]["vendors"])
    assert payload["data"]["default"] == "codex"


def test_vendor_set_is_rejected(monkeypatch, tmp_path: Path) -> None:
    """`vendor set` is no longer an action; changing the default needs a commit."""
    _source(monkeypatch, tmp_path)
    # argparse rejects the removed choice with a usage error (SystemExit 2).
    with pytest.raises(SystemExit) as exc:
        cli.main(["vendor", "set", "claude"])
    assert exc.value.code == 2


def test_vendor_get_flags_invalid_config_default(monkeypatch, tmp_path: Path, capsys) -> None:
    """A loopcraft.toml default_vendor with no adapter fails vendor get (review 01)."""
    _source(monkeypatch, tmp_path, toml='default_vendor = "gemini"\n')
    rc = cli.main(["--json", "vendor", "get"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["data"]["default"] == "gemini"
    assert payload["data"]["default_ok"] is False


def test_vendor_list_flags_invalid_config_default(monkeypatch, tmp_path: Path, capsys) -> None:
    """vendor list also reports an unregistered default as a failure."""
    _source(monkeypatch, tmp_path, toml='default_vendor = "gemini"\n')
    rc = cli.main(["--json", "vendor", "list"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["data"]["default_ok"] is False


def test_vendor_get_flags_invalid_env_override(monkeypatch, tmp_path: Path, capsys) -> None:
    """An invalid LOOPCRAFT_VENDOR override is flagged too (env wins at load)."""
    _source(monkeypatch, tmp_path)  # toml default is codex
    monkeypatch.setenv("LOOPCRAFT_VENDOR", "gemini")
    rc = cli.main(["--json", "vendor", "get"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["data"]["default"] == "gemini"
    assert payload["data"]["default_ok"] is False


def test_vendor_get_notes_env_override(monkeypatch, tmp_path: Path, capsys) -> None:
    """LOOPCRAFT_VENDOR override is surfaced by `vendor get`."""
    _source(monkeypatch, tmp_path)
    monkeypatch.setenv("LOOPCRAFT_VENDOR", "cursor")
    rc = cli.main(["--json", "vendor", "get"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    # The env var wins at load time, and the override is reported.
    assert payload["data"]["default"] == "cursor"
    assert payload["data"]["override"] == "cursor"
