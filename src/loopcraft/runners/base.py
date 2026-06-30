from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..config import LoopcraftConfig
from ..manifest import LoopManifest

STATUS_DONE = "done"
STATUS_STALLED = "stalled"
STATUS_FAILED = "failed"
STATUS_NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class PreflightReport:
    """Result of checking that a vendor can satisfy a loop before running it."""

    vendor: str
    ok: bool
    problems: list[str] = field(default_factory=list)


@dataclass
class RunContext:
    """Everything a runner needs to execute one loop, isolated from others."""

    config: LoopcraftConfig
    workdir: Path
    log_path: Path
    resolved_outputs: list[Path] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class RunResult:
    """Normalized outcome of a headless run, across vendors."""

    status: str
    exit_code: int | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    iterations: int | None = None
    log_path: Path | None = None
    outputs: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@runtime_checkable
class Runner(Protocol):
    """The one narrow seam that delivers vendor portability.

    A runner translates a normalized loop invocation into a headless run on a
    specific vendor, then returns a normalized result.
    """

    vendor: str

    def preflight(self, loop: LoopManifest, config: LoopcraftConfig) -> PreflightReport:
        """Check this vendor can satisfy the loop (binary, auth, env, skill)."""
        ...

    def run(self, loop: LoopManifest, ctx: RunContext) -> RunResult:
        """Execute the loop headless in an isolated worktree; return the result."""
        ...
