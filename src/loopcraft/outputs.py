"""Worktree-local output staging and post-run promotion to the ledger.

Loops declare ``state/...`` outputs that resolve to durable ledger paths *outside*
the isolated run worktree. Rather than granting a headless agent write access to
those out-of-worktree paths (which forces coarse sandbox escapes — especially on
Cursor, which has no per-directory grant), the control plane routes outputs
through the worktree:

1. before the run, each declared output is bound to a **write path** inside the
   worktree (``<worktree>/outputs/<ledger-relative>``) and its final **ledger
   path**;
2. the agent writes only inside its worktree (no write grant needed on any
   vendor);
3. after the run, :func:`promote_outputs` copies the produced files to their
   ledger destinations.

This keeps every adapter's writes confined to the worktree, so the writable-root
grant collapses to nothing for Codex/Claude/Cursor alike.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel

from loopcraft.config import LoopcraftConfig
from loopcraft.paths import assert_under

#: Subdirectory of a run worktree where declared outputs are staged for writing.
OUTPUTS_STAGING_DIR = "outputs"


class OutputBinding(BaseModel):
    """Binds one declared output to its in-worktree write path and ledger dest.

    Attributes:
        declared: The manifest output string (e.g. ``state/x/latest.md``).
        write_path: Absolute path inside the run worktree the agent writes to.
        ledger_path: Absolute durable destination the file is promoted to.
    """

    model_config = {"arbitrary_types_allowed": True}

    declared: str
    write_path: Path
    ledger_path: Path


def plan_output_bindings(
    config: LoopcraftConfig,
    workdir: Path,
    declared_outputs: list[str],
    *,
    run_id: str,
    date: str,
) -> list[OutputBinding]:
    """Bind each declared output to a worktree write path and its ledger dest.

    The write path mirrors the ledger-relative layout under the worktree's
    ``outputs/`` directory, and is asserted to stay inside the worktree.

    Raises:
        StatePathError: If a declared output escapes the ledger.
        ValueError: If the mirrored write path would escape the worktree.
    """
    workdir = workdir.resolve()
    staging_root = workdir / OUTPUTS_STAGING_DIR
    bindings: list[OutputBinding] = []
    for declared in declared_outputs:
        ledger_path = config.resolve_state_template(declared, run_id=run_id, date=date)
        rel = ledger_path.relative_to(config.ledger_dir)
        write_path = (staging_root / rel).resolve()
        assert_under(workdir, write_path, label="output staging path")
        bindings.append(
            OutputBinding(declared=declared, write_path=write_path, ledger_path=ledger_path)
        )
    return bindings


def promote_outputs(bindings: list[OutputBinding]) -> list[Path]:
    """Copy produced worktree outputs to their ledger destinations.

    Only bindings whose ``write_path`` exists are promoted (a run may not produce
    every declared output). Returns the ledger paths actually written.
    """
    promoted: list[Path] = []
    for binding in bindings:
        if not binding.write_path.exists():
            continue
        binding.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binding.write_path, binding.ledger_path)
        promoted.append(binding.ledger_path)
    return promoted
