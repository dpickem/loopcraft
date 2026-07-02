"""Shared CLI output helper.

All loopcraft CLIs (``loopctl`` and the research-intel debug CLIs) render results
through :func:`emit` so the structured ``--json`` envelope is identical across
commands: ``{command, ok, exit_code, data}``. Human-readable text remains the
default; JSON is opt-in and agent-friendly.
"""

from __future__ import annotations

import json
from typing import Any


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
