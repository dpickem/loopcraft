"""Tests for the shared CLI output envelope helper."""

from __future__ import annotations

import json

from loopcraft.cli_output import emit, render_table


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


def test_render_table_aligns_columns() -> None:
    """render_table sizes columns to the widest cell and box-draws the grid."""
    lines = render_table(["LOOP", "TIER"], [["slack-triage", "observe"], ["x", "act"]])
    # Header + both rows padded to the widest cell in each column.
    assert "│ LOOP         │ TIER    │" in lines
    assert "│ slack-triage │ observe │" in lines
    assert "│ x            │ act     │" in lines
    # Borders open and close the table.
    assert lines[0].startswith("┌") and lines[0].endswith("┐")
    assert lines[-1].startswith("└") and lines[-1].endswith("┘")


def test_render_table_with_no_rows_is_header_only() -> None:
    """A table with no data rows still renders its header between borders."""
    lines = render_table(["A", "B"], [])
    assert len(lines) == 4  # top, header, separator, bottom
    assert "│ A │ B │" in lines
