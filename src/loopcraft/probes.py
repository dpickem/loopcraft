"""Reusable dependency probe helpers.

Runner adapters use these helpers to perform bounded, read-only capability
checks before starting a headless run. The helpers keep subprocess timeout and
missing-binary behavior consistent across adapters, and refuse to execute
anything outside a small allowlist so a probe can never become an arbitrary
command runner.
"""

from __future__ import annotations

import subprocess
from pathlib import PurePath

#: Binaries a capability probe may invoke. Probes are bounded, read-only
#: checks (e.g. ``nv-tools slack list-channels --limit 1``); anything else is
#: refused so probe plumbing cannot be repurposed to run arbitrary commands.
PROBE_ALLOWED_BINARIES = frozenset({"nv-tools"})


def run_probe(cmd: list[str], *, timeout_s: int, env: dict[str, str] | None = None) -> int | None:
    """Run a bounded probe command from the probe allowlist.

    The allowlist is checked against ``cmd[0]``'s basename, so an absolute path
    to an allowlisted binary (e.g. a scheduled-PATH-resolved ``nv-tools``) is
    accepted — the caller should pass the resolved executable so the probe
    executes the same binary it validated.

    Args:
        cmd: Command argument vector; ``cmd[0]``'s basename must be allowlisted.
        timeout_s: Maximum seconds to wait.
        env: Optional environment for the probe process (e.g. the scheduled
            service PATH). When None, the current process environment is used.

    Returns:
        Process exit code, or None if the binary is missing or the probe timed
        out.

    Raises:
        ValueError: If the command is empty or its binary is not allowlisted.
    """
    if not cmd or PurePath(cmd[0]).name not in PROBE_ALLOWED_BINARIES:
        raise ValueError(
            f"probe binary not allowlisted: {cmd[:1] or '(empty)'} "
            f"(allowed: {sorted(PROBE_ALLOWED_BINARIES)})"
        )
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return completed.returncode
