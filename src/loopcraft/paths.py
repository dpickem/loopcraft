"""Reusable path validation and containment helpers.

Loopcraft has two important filesystem boundaries: the source tree, which holds
code/manifests/skills, and the memory ledger, which holds private durable state.
This module centralizes the normalization rules for paths that cross those
boundaries so callers do not each invent their own traversal checks.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


def safe_relpath(
    declared: str,
    *,
    prefixes: tuple[str, ...] = (),
    kind: str,
    allow_colon: bool = False,
) -> str:
    """Validate and normalize a POSIX-style relative path.

    Args:
        declared: Path string from config or a manifest.
        prefixes: Optional leading path segments to strip before returning.
        kind: Human-readable label used in exception messages.
        allow_colon: Whether ``:`` is allowed in path segments.

    Returns:
        A normalized POSIX-style relative path with any accepted prefix removed.

    Raises:
        ValueError: If the path is empty, absolute, has ``..`` traversal, has an
            invalid segment, or has a scheme/drive head.
    """
    rel = declared.strip()
    if not rel:
        raise ValueError(f"empty {kind} path")
    if rel.startswith("/") or os.path.isabs(rel) or PurePosixPath(rel).is_absolute():
        raise ValueError(f"absolute {kind} path is not allowed: {declared!r}")
    head = rel.split("/", 1)[0]
    if ":" in head and not allow_colon:
        raise ValueError(f"scheme/drive {kind} path is not allowed: {declared!r}")
    parts = list(PurePosixPath(rel).parts)
    if prefixes and parts and parts[0] in prefixes:
        parts = parts[1:]
    if not parts:
        raise ValueError(f"{kind} path has no file after prefix: {declared!r}")
    if any(part == ".." for part in parts):
        raise ValueError(f"'..' is not allowed in a {kind} path: {declared!r}")
    if any(part in ("", ".") or (":" in part and not allow_colon) for part in parts):
        raise ValueError(f"invalid segment in {kind} path: {declared!r}")
    return "/".join(parts)


def contained_child(root: Path, name: str, *, label: str) -> Path:
    """Compose ``root / name`` and assert the child stays under ``root``.

    Defense in depth for filename components derived from runtime values (run
    ids, date stamps): a traversal or absolute component must not escape the
    already-resolved parent directory.

    Raises:
        ValueError: If the composed child escapes ``root``.
    """
    child = root / name
    assert_under(root, child, label=label)
    return child


def assert_under(root: Path, candidate: Path, *, label: str) -> None:
    """Assert that ``candidate`` is inside ``root``, resolving symlinks.

    Both paths are fully resolved (``Path.resolve``), which follows symlinks in
    every existing component and normalizes the nonexistent tail lexically. A
    symlink inside the tree is therefore allowed only if its target also stays
    under the (resolved) root — an in-tree symlink cannot redirect reads,
    writes, or pruning outside the boundary.

    Args:
        root: Allowed root directory.
        candidate: Path to check.
        label: Human-readable label for errors.

    Raises:
        ValueError: If the resolved candidate escapes the resolved root.
    """
    root_real = root.resolve()
    cand_real = candidate.resolve()
    if cand_real != root_real and root_real not in cand_real.parents:
        raise ValueError(f"{label} escapes allowed root: {candidate}")
