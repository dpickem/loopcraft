"""Tests for `loopctl vendor` and the default-vendor config writer (M3)."""

from __future__ import annotations

import json
from pathlib import Path

from loopcraft import cli
from loopcraft.config import LoopcraftConfig, set_default_vendor


def _source(monkeypatch, tmp_path: Path, toml: str = 'default_vendor = "codex"\n') -> Path:
    """Point loopctl at a temp source tree carrying a loopcraft.toml."""
    source = tmp_path / "src"
    (source / "loops").mkdir(parents=True)
    (source / "loopcraft.toml").write_text(toml, encoding="utf-8")
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(source))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))
    monkeypatch.delenv("LOOPCRAFT_VENDOR", raising=False)
    return source


def test_set_default_vendor_replaces_line(tmp_path: Path) -> None:
    """set_default_vendor rewrites the existing line and preserves other config."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "loopcraft.toml").write_text(
        '# comment\ndefault_vendor = "codex"\nhost = "vm"\n', encoding="utf-8"
    )
    set_default_vendor(source, "claude")
    text = (source / "loopcraft.toml").read_text(encoding="utf-8")
    assert 'default_vendor = "claude"' in text
    assert 'default_vendor = "codex"' not in text
    assert 'host = "vm"' in text  # untouched
    assert "# comment" in text


def test_set_default_vendor_inserts_when_absent(tmp_path: Path) -> None:
    """When the key is absent it is prepended."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "loopcraft.toml").write_text('host = "vm"\n', encoding="utf-8")
    set_default_vendor(source, "cursor")
    text = (source / "loopcraft.toml").read_text(encoding="utf-8")
    assert text.startswith('default_vendor = "cursor"')


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


def test_vendor_set_persists(monkeypatch, tmp_path: Path, capsys) -> None:
    """`vendor set` writes the new default and a reload picks it up."""
    source = _source(monkeypatch, tmp_path)
    rc = cli.main(["vendor", "set", "claude"])
    assert rc == 0
    assert LoopcraftConfig.load(source).default_vendor == "claude"


def test_vendor_set_rejects_unknown(monkeypatch, tmp_path: Path, capsys) -> None:
    """`vendor set` rejects a vendor with no adapter."""
    _source(monkeypatch, tmp_path)
    rc = cli.main(["--json", "vendor", "set", "gemini"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert "unknown vendor" in payload["data"]["error"]


def test_vendor_set_requires_name(monkeypatch, tmp_path: Path, capsys) -> None:
    """`vendor set` with no name is a usage error."""
    _source(monkeypatch, tmp_path)
    rc = cli.main(["--json", "vendor", "set"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert "requires a vendor name" in payload["data"]["error"]


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
