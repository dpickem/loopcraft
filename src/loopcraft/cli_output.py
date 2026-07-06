"""Shared CLI output helper.

All loopcraft CLIs (``loopctl`` and the research-intel debug CLIs) render results
through :func:`emit` so the structured ``--json`` envelope is identical across
commands: ``{command, ok, exit_code, data}``. Human-readable text remains the
default; JSON is opt-in and agent-friendly.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import BaseModel, Field


class CommandOutcome(BaseModel):
    """One structured command outcome ready for emission.

    Bundles the exit code, JSON payload, and human-readable lines a command
    handler produces, replacing loose ``(rc, data, lines)`` tuples.
    """

    rc: int
    data: dict[str, Any] = Field(default_factory=dict)
    lines: list[str] = Field(default_factory=list)


def emit(
    command: str,
    *,
    as_json: bool,
    ok: bool,
    rc: int,
    data: dict[str, Any],
    lines: list[str],
) -> int:
    """Render a command result and return its exit code.

    In JSON mode a single ``{command, ok, exit_code, data}`` object is printed to
    stdout; otherwise the pre-formatted human ``lines`` are printed. Returning
    ``rc`` lets callers ``return emit(...)`` directly.

    Args:
        command: Dotted command name (e.g. ``run``, ``deps.check``).
        as_json: Whether to emit the JSON envelope instead of text lines.
        ok: Whether the command succeeded.
        rc: The process exit code to return.
        data: Structured payload for the JSON envelope.
        lines: Pre-formatted human-readable output lines.

    Returns:
        The provided ``rc``.
    """
    if as_json:
        envelope = {"command": command, "ok": ok, "exit_code": rc, "data": data}
        print(json.dumps(envelope, indent=2, ensure_ascii=False, default=str))
    else:
        for line in lines:
            print(line)
    return rc


def render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Render an aligned, box-drawn table as a list of text lines.

    Column widths are sized to the widest header or cell so the table stays
    aligned regardless of content. Kept dependency-free (no ``rich``/``tabulate``)
    to match the project's minimal footprint.

    Args:
        headers: Column header labels.
        rows: Rows of already-stringified cells; each row must have one cell per
            header.

    Returns:
        The table's lines (top border, header, separator, rows, bottom border),
        ready to hand to :func:`emit` as ``lines``.
    """
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    def _row(cells: list[str]) -> str:
        return "│ " + " │ ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)) + " │"

    def _rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    lines = [_rule("┌", "┬", "┐"), _row(headers), _rule("├", "┼", "┤")]
    lines += [_row(row) for row in rows]
    lines.append(_rule("└", "┴", "┘"))
    return lines


def fail(command: str, rc: int, message: str, *, as_json: bool) -> int:
    """Emit a failure result: a JSON error envelope, or a stderr message.

    Shared by the direct research CLIs so error paths keep the same
    ``{command, ok, exit_code, data}`` envelope contract as successes.

    Args:
        command: Dotted command name (e.g. ``run``).
        rc: The process exit code to return.
        message: Human-readable failure description.
        as_json: Whether to emit the JSON envelope instead of stderr text.

    Returns:
        The provided ``rc``.
    """
    if as_json:
        return emit(command, as_json=True, ok=False, rc=rc, data={"error": message}, lines=[])
    print(f"ERROR: {message}", file=sys.stderr)
    return rc
