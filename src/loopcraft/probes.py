"""Reusable dependency probe helpers.

Runner adapters use these helpers to perform bounded, read-only capability
checks before starting a headless run. The helpers keep subprocess timeout and
missing-binary behavior consistent across adapters.
"""

from __future__ import annotations

import subprocess


def run_probe(cmd: list[str], *, timeout_s: int) -> int | None:
    """Run a bounded probe command.

    Args:
        cmd: Command argument vector.
        timeout_s: Maximum seconds to wait.

    Returns:
        Process exit code, or None if the binary is missing or the probe timed
        out.
    """
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return completed.returncode
