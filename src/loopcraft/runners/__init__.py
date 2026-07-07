"""Runner package exports and the vendor runner factory."""

from __future__ import annotations

from loopcraft.runners.base import (
    PreflightReport,
    RunContext,
    Runner,
    RunResult,
    RunStatus,
)
from loopcraft.runners.claude import ClaudeRunner
from loopcraft.runners.codex import CodexRunner
from loopcraft.runners.cursor import CursorRunner

#: Registry of vendor name -> runner adapter class (extended by register_runner).
#: Mirrors the manifest ``Vendor`` vocabulary so every declarable vendor has an
#: adapter — a loop is portable across them by changing one flag.
_RUNNERS: dict[str, type[Runner]] = {
    "codex": CodexRunner,
    "claude": ClaudeRunner,
    "cursor": CursorRunner,
}


def get_runner(vendor: str) -> Runner:
    """Return a runner adapter for the given vendor.

    Codex, Claude, and Cursor adapters ship as of M3; the same manifest runs on
    any of them (vendor is a config value, not a rewrite).
    """
    try:
        runner_cls = _RUNNERS[vendor]
    except KeyError:
        raise ValueError(
            f"no runtime adapter for vendor '{vendor}' "
            f"(available: {sorted(_RUNNERS)})"
        ) from None
    return runner_cls()


def register_runner(vendor: str, runner_cls: type[Runner]) -> None:
    """Register an additional runner adapter (used by later milestones/tests)."""
    _RUNNERS[vendor] = runner_cls


def available_vendors() -> list[str]:
    """Return the sorted vendor names that have a registered runner adapter."""
    return sorted(_RUNNERS)


__all__ = [
    "PreflightReport",
    "RunContext",
    "Runner",
    "RunResult",
    "CodexRunner",
    "ClaudeRunner",
    "CursorRunner",
    "get_runner",
    "register_runner",
    "available_vendors",
    "RunStatus",
]
