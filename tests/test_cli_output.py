"""Tests for the shared CLI output envelope helper."""

from __future__ import annotations

import json

from loopcraft.cli_output import emit


def test_emit_text_mode_prints_lines(capsys) -> None:
    """In text mode emit prints the human lines and returns the exit code."""
    rc = emit("run", as_json=False, ok=True, rc=0, data={"x": 1}, lines=["a", "b"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines() == ["a", "b"]


def test_emit_json_mode_emits_consistent_envelope(capsys) -> None:
    """In JSON mode emit prints a {command, ok, exit_code, data} envelope."""
    rc = emit("deps.check", as_json=True, ok=False, rc=1, data={"missing": ["git"]}, lines=["ignored"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload == {
        "command": "deps.check",
        "ok": False,
        "exit_code": 1,
        "data": {"missing": ["git"]},
    }
