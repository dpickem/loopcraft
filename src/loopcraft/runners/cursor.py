"""Cursor CLI (``cursor-agent``) runtime adapter.

Implements the Cursor-specific preflight and headless command construction on
top of :class:`BaseRunner`, sharing the vendor-neutral prompt and capability
checks. Cursor is cross-provider (a loop can request a gpt/claude/gemini model),
so the model check is intentionally permissive.

M3 scope note: this is a **limited** adapter. Unlike Codex/Claude it does not
grant write access to the loop's declared ledger outputs (they resolve outside
the per-run worktree, and ``cursor-agent`` has no equivalent of ``--add-dir``
here), so preflight reports those loops as unsupported rather than letting a run
silently fail to produce them. Cross-provider sub-agents and role compilation
land in M3.5.
"""

from __future__ import annotations

from loopcraft.config import LoopcraftConfig, is_state_path
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
        capabilities. The model id is not vendor-checked (Cursor is
        cross-provider and its slugs are account/plan-dependent, so the CLI
        validates it). Because this adapter cannot grant write access to ledger
        outputs (see the module note), a loop that declares any ``state/...``
        output is reported as unsupported for Cursor in M3.

        Returns:
            A report listing any problems found (empty when ready to run).
        """
        problems: list[str] = []
        if config.which(_CURSOR_BIN) is None:
            problems.append(f"{_CURSOR_BIN} not found on PATH (install the Cursor CLI)")
        problems += self.check_declared_capabilities(loop, config)
        ledger_outputs = [out for out in loop.outputs if is_state_path(out)]
        if ledger_outputs:
            problems.append(
                "cursor adapter (M3) cannot grant write access to ledger outputs "
                f"outside the run worktree: {ledger_outputs}; use codex/claude for "
                "output-producing loops (Cursor writable-root support is deferred)"
            )
        return PreflightReport(vendor=self.vendor, ok=not problems, problems=problems)

    def build_command(self, loop: LoopManifest, ctx: RunContext) -> list[str]:
        """Build the headless ``cursor-agent -p`` argv for one loop invocation.

        Runs non-interactively (``-p``) reading the prompt from stdin (the base
        runner pipes it), optionally pinning the model. Cursor resolves file
        access from its own workspace/trust settings; loops needing writes to the
        ledger are rejected at preflight (see :meth:`preflight`).

        Returns:
            The command argv; the prompt is supplied on stdin by the base runner.
        """
        cmd = [_CURSOR_BIN, "-p", "--output-format", "text"]
        if loop.runtime.model:
            cmd += ["--model", loop.runtime.model]
        return cmd
