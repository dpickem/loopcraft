"""Tests for safe, atomic output promotion (M3.5 review 02, finding 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopcraft.outputs import OutputBinding, PromotionError, is_safe_regular_file, promote_outputs


def _binding(worktree: Path, ledger: Path, name: str) -> OutputBinding:
    """Build an output binding for ``name`` under the given worktree/ledger."""
    return OutputBinding(
        declared=f"state/x/{name}",
        write_path=worktree / "outputs" / "x" / name,
        ledger_path=ledger / "x" / name,
    )


def test_is_safe_regular_file_rejects_symlink(tmp_path: Path) -> None:
    """A symlink is never treated as a safe regular file (lstat, no follow)."""
    target = tmp_path / "target.md"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    assert is_safe_regular_file(target) is True
    assert is_safe_regular_file(link) is False


def test_promote_copies_regular_file(tmp_path: Path) -> None:
    """A produced regular file is promoted to its ledger destination."""
    worktree, ledger = tmp_path / "wt", tmp_path / "ledger"
    binding = _binding(worktree, ledger, "out.md")
    binding.write_path.parent.mkdir(parents=True)
    binding.write_path.write_text("hello", encoding="utf-8")

    promoted = promote_outputs([binding], workdir=worktree, ledger_root=ledger)
    assert promoted == [binding.ledger_path]
    assert binding.ledger_path.read_text(encoding="utf-8") == "hello"


def test_promote_rejects_symlink_source(tmp_path: Path) -> None:
    """A declared output that is a symlink is refused (no link-target copy)."""
    worktree, ledger = tmp_path / "wt", tmp_path / "ledger"
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET", encoding="utf-8")
    binding = _binding(worktree, ledger, "out.md")
    binding.write_path.parent.mkdir(parents=True)
    binding.write_path.symlink_to(secret)

    with pytest.raises(PromotionError):
        promote_outputs([binding], workdir=worktree, ledger_root=ledger)
    assert not binding.ledger_path.exists()


def test_promote_prevalidates_all_before_replacing(tmp_path: Path) -> None:
    """A later unsafe output aborts promotion before any destination is written."""
    worktree, ledger = tmp_path / "wt", tmp_path / "ledger"
    good = _binding(worktree, ledger, "good.md")
    bad = _binding(worktree, ledger, "bad.md")
    good.write_path.parent.mkdir(parents=True)
    good.write_path.write_text("new", encoding="utf-8")
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET", encoding="utf-8")
    bad.write_path.symlink_to(secret)
    # A pre-existing canonical value for the good output must not be clobbered
    # when a later binding is unsafe.
    good.ledger_path.parent.mkdir(parents=True)
    good.ledger_path.write_text("OLD", encoding="utf-8")

    with pytest.raises(PromotionError):
        promote_outputs([good, bad], workdir=worktree, ledger_root=ledger)
    assert good.ledger_path.read_text(encoding="utf-8") == "OLD"  # unchanged


def test_promote_skips_unproduced_output(tmp_path: Path) -> None:
    """A binding whose write path was never produced is silently skipped."""
    worktree, ledger = tmp_path / "wt", tmp_path / "ledger"
    binding = _binding(worktree, ledger, "out.md")
    promoted = promote_outputs([binding], workdir=worktree, ledger_root=ledger)
    assert promoted == []
    assert not binding.ledger_path.exists()
