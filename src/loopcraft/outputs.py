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
import stat
import tempfile
from pathlib import Path

from pydantic import BaseModel

from loopcraft.config import LoopcraftConfig
from loopcraft.paths import assert_under

#: Subdirectory of a run worktree where declared outputs are staged for writing.
OUTPUTS_STAGING_DIR = "outputs"

#: Chunk size for streaming a promoted output from its opened descriptor.
_COPY_CHUNK = 1 << 20


class PromotionError(Exception):
    """Raised when a produced output cannot be safely promoted to the ledger."""


def is_safe_regular_file(path: Path) -> bool:
    """Return whether ``path`` is a real regular file (not a symlink/dir/device).

    Uses a single ``lstat`` so a symlink is never followed: an agent that writes
    its declared output as a symlink cannot trick the control plane into reading
    or copying the link target.
    """
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
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


def _open_regular_nofollow(path: Path) -> int:
    """Open ``path`` read-only without following a final symlink; verify regular.

    Returns an open file descriptor. Using ``O_NOFOLLOW`` + ``fstat`` on the
    *descriptor* closes the check-then-copy (TOCTOU) window: if the path is a
    symlink at open time the open fails, and the descriptor we copy from is the
    exact inode we validated — a background swap cannot redirect the read.

    Raises:
        PromotionError: If the path is a symlink or not a regular file.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PromotionError(f"cannot open output for promotion (symlink refused?): {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PromotionError(f"output is not a regular file: {path}")
    except Exception:
        os.close(fd)
        raise
    return fd


def promote_outputs(
    bindings: list[OutputBinding],
    *,
    workdir: Path | None = None,
    ledger_root: Path | None = None,
) -> list[Path]:
    """Atomically copy produced worktree outputs to their ledger destinations.

    Promotion is validated across *all* bindings before any destination is
    replaced, and each file is copied from a no-follow-opened descriptor through
    a uniquely-named same-directory temp file, then ``os.replace``\\d into place:

    - a produced output that is a symlink / directory / device is refused, and no
      destination is written (fail before any replace);
    - the source is opened with ``O_NOFOLLOW`` and copied from that descriptor,
      so a source swapped for a symlink after validation cannot redirect the read;
    - a unique ``mkstemp`` temp file avoids a fixed-name collision/redirect;
    - the destination (and parent) are re-asserted under the ledger before write.

    Only bindings whose ``write_path`` exists are promoted (a run may not produce
    every declared output).

    Args:
        bindings: The output bindings to promote.
        workdir: When given, re-assert every write path stays inside it.
        ledger_root: When given, re-assert every ledger destination stays inside
            it right before writing.

    Returns:
        The ledger paths actually written.

    Raises:
        PromotionError: If any produced output is not a safe regular file.
    """
    # Phase 1: decide which bindings are present and validate every one BEFORE
    # replacing any destination, so a later unsafe/failed output cannot leave a
    # mix of old/new canonical state.
    pending: list[OutputBinding] = []
    for binding in bindings:
        if not is_safe_regular_file(binding.write_path):
            # Distinguish "not produced" (skip) from "produced but unsafe" (fail).
            if binding.write_path.exists() or binding.write_path.is_symlink():
                raise PromotionError(
                    f"declared output is not a regular file (symlink/dir refused): {binding.declared}"
                )
            continue
        if workdir is not None:
            assert_under(workdir.resolve(), binding.write_path, label="output write path")
        if ledger_root is not None:
            assert_under(ledger_root.resolve(), binding.ledger_path, label="ledger output path")
        pending.append(binding)

    # Phase 2: copy each validated source (no-follow) through a unique temp file
    # in the destination directory, then atomically replace.
    promoted: list[Path] = []
    for binding in pending:
        binding.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd = _open_regular_nofollow(binding.write_path)
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=binding.ledger_path.parent, prefix=f".{binding.ledger_path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "rb") as src, os.fdopen(tmp_fd, "wb") as dst:
                while chunk := src.read(_COPY_CHUNK):
                    dst.write(chunk)
            os.replace(tmp_path, binding.ledger_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        promoted.append(binding.ledger_path)
    return promoted
