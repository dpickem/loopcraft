"""Tests for reusable path-containment helpers."""

from __future__ import annotations

from pathlib import Path

from loopcraft.paths import is_lexically_under


def test_is_lexically_under_direct_child(tmp_path: Path) -> None:
    """A path textually inside the root is under it."""
    assert is_lexically_under(tmp_path / "a" / "b.txt", tmp_path)


def test_is_lexically_under_outside(tmp_path: Path) -> None:
    """A sibling path is not under the root."""
    assert not is_lexically_under(tmp_path.parent / "elsewhere.txt", tmp_path)


def test_is_lexically_under_ignores_symlink_target(tmp_path: Path) -> None:
    """A symlink *inside* the root counts as under it even if it points outside.

    This is the point of lexical (non-resolving) containment: a tracked stub in a
    git tree must be rejected regardless of where its target lives.
    """
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    root = tmp_path / "tree"
    root.mkdir()
    stub = root / "link.txt"
    stub.symlink_to(outside)
    assert is_lexically_under(stub, root)


def test_is_lexically_under_normalizes_traversal(tmp_path: Path) -> None:
    """A ``..`` that climbs out of the root is not under it."""
    assert not is_lexically_under(tmp_path / "a" / ".." / ".." / "x", tmp_path)