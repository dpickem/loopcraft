"""Runner package exports and the vendor runner factory."""

from __future__ import annotations

from loopcraft.runners.base import (
    PreflightReport,
    RunContext,
    Runner,
    RunResult,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NEEDS_APPROVAL,
    STATUS_STALLED,
)
from loopcraft.runners.codex import CodexRunner

_RUNNERS: dict[str, type[Runner]] = {
    "codex": CodexRunner,
}


def get_runner(vendor: str) -> Runner:
    """Return a runner adapter for the given vendor.

    Only the Codex adapter ships in M1; Claude and Cursor arrive in M3.
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


__all__ = [
    "PreflightReport",
    "RunContext",
    "Runner",
    "RunResult",
    "CodexRunner",
    "get_runner",
    "register_runner",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_NEEDS_APPROVAL",
    "STATUS_STALLED",
]
