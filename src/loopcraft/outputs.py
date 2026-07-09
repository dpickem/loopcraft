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

import os
import shutil
from pathlib import Path

from pydantic import BaseModel

from loopcraft.config import LoopcraftConfig
from loopcraft.paths import assert_under

#: Subdirectory of a run worktree where declared outputs are staged for writing.
OUTPUTS_STAGING_DIR = "outputs"


class PromotionError(Exception):
    """Raised when a produced output cannot be safely promoted to the ledger."""


def is_safe_regular_file(path: Path) -> bool:
    """Return whether ``path`` is a real regular file (not a symlink/dir/device).

    Uses ``lstat`` so a symlink is never followed: an agent that writes its
    declared output as a symlink cannot trick the control plane into reading or
    copying the link target.
    """
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


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


def promote_outputs(bindings: list[OutputBinding], *, workdir: Path | None = None) -> list[Path]:
    """Atomically copy produced worktree outputs to their ledger destinations.

    Only bindings whose ``write_path`` is a real regular file are promoted (a run
    may not produce every declared output). Each copy goes through a temporary
    file in the destination directory and is then atomically renamed into place,
    so a reader never observes a half-written ledger file and a failed copy
    cannot leave a partial canonical output.

    Args:
        bindings: The output bindings to promote.
        workdir: When given, re-assert every write path stays inside it right
            before reading — defense against a symlinked/relocated staging path.

    Returns:
        The ledger paths actually written.

    Raises:
        PromotionError: If a declared output exists but is not a safe regular
            file (symlink, directory, device, etc.).
    """
    promoted: list[Path] = []
    for binding in bindings:
        write_path = binding.write_path
        if not write_path.exists() and not write_path.is_symlink():
            continue
        if not is_safe_regular_file(write_path):
            raise PromotionError(
                f"declared output is not a regular file (symlink/dir refused): {binding.declared}"
            )
        if workdir is not None:
            assert_under(workdir.resolve(), write_path.resolve(), label="output write path")
        binding.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        # Copy to a temp file in the destination dir, then atomically replace.
        tmp = binding.ledger_path.with_name(binding.ledger_path.name + ".loopcraft.tmp")
        try:
            shutil.copyfile(write_path, tmp)  # copyfile does not follow dest symlinks
            os.replace(tmp, binding.ledger_path)
        finally:
            if tmp.exists():
                tmp.unlink()
        promoted.append(binding.ledger_path)
    return promoted
