"""Cursor CLI (``cursor-agent``) runtime adapter.

Implements the Cursor-specific preflight and headless command construction on
top of :class:`BaseRunner`, sharing the vendor-neutral prompt and capability
checks. Cursor is cross-provider (a loop can request a gpt/claude/gemini model,
and a Cursor run can spawn sub-agents on other providers), so the model check is
intentionally permissive.
"""

from __future__ import annotations

from loopcraft.config import LoopcraftConfig
from loopcraft.manifest import LoopManifest
from loopcraft.runners.base import BaseRunner, PreflightReport, RunContext

#: The Cursor headless CLI binary.
_CURSOR_BIN = "cursor-agent"


class CursorRunner(BaseRunner):
    """Runtime adapter that executes a loop headless via the Cursor CLI."""

    vendor = "cursor"

    def preflight(self, loop: LoopManifest, config: LoopcraftConfig) -> PreflightReport:
        """Check the Cursor CLI can satisfy the loop before running it.

        Verifies the ``cursor-agent`` binary and the shared declared
        capabilities. The model id is not vendor-checked here: Cursor is
        cross-provider and its available model slugs are account/plan-dependent,
        so an unknown-looking model is left to the CLI to accept or reject rather
        than rejected locally.

        Returns:
            A report listing any problems found (empty when ready to run).
        """
        problems: list[str] = []
        if config.which(_CURSOR_BIN) is None:
            problems.append(f"{_CURSOR_BIN} not found on PATH (install the Cursor CLI)")
        problems += self.check_declared_capabilities(loop, config)
        return PreflightReport(vendor=self.vendor, ok=not problems, problems=problems)

    def build_command(self, loop: LoopManifest, ctx: RunContext) -> list[str]:
        """Build the headless ``cursor-agent -p`` argv for one loop invocation.

        Runs non-interactively (``-p``) reading the prompt from stdin (the base
        runner pipes it), optionally pinning the model. Cursor resolves file
        access from its own workspace/trust settings, so writable output roots are
        conveyed to the agent through the prompt's I/O contract rather than a
        per-dir flag.

        Returns:
            The command argv; the prompt is supplied on stdin by the base runner.
        """
        cmd = [_CURSOR_BIN, "-p", "--output-format", "text"]
        if loop.runtime.model:
            cmd += ["--model", loop.runtime.model]
        return cmd
