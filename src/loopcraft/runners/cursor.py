"""Cursor CLI (``cursor-agent``) runtime adapter.

Implements the Cursor-specific preflight and headless command construction on
top of :class:`BaseRunner`, sharing the vendor-neutral prompt and capability
checks. Cursor is cross-provider (a loop can request a gpt/claude/gemini model),
so the model check is intentionally permissive.

M3.5 writable-root grant: unlike Codex/Claude, ``cursor-agent`` has no per-dir
``--add-dir`` flag, so a loop's declared ledger outputs (which resolve outside
the per-run worktree) are granted by running with the sandbox disabled and
commands force-allowed (``--sandbox disabled --force --trust``). This is a
coarser grant than the scoped Codex/Claude writable roots — it is whole-machine
rather than per-directory — and is applied only in headless ``--print`` mode.
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
        capabilities. The model id is not vendor-checked (Cursor is
        cross-provider and its slugs are account/plan-dependent, so the CLI
        validates it). As of M3.5 the adapter grants write access to declared
        ledger outputs (see the module note), so output-producing loops are
        supported.

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
        runner pipes it), optionally pinning the model. When the loop declares
        outputs that resolve outside the worktree (ledger paths), the sandbox is
        disabled and commands are force-allowed so those writes succeed — the
        Cursor writable-root grant. A loop with no external outputs keeps the
        default (sandboxed) behavior.

        Returns:
            The command argv; the prompt is supplied on stdin by the base runner.
        """
        cmd = [_CURSOR_BIN, "-p", "--output-format", "text", "--trust"]
        if self.writable_roots(ctx):
            # cursor-agent has no per-dir grant; disabling the sandbox and
            # force-allowing commands is the available mechanism to let a run
            # write its declared ledger outputs outside the worktree.
            cmd += ["--force", "--sandbox", "disabled"]
        if loop.runtime.model:
            cmd += ["--model", loop.runtime.model]
        return cmd
