from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath

from .config import LoopcraftConfig, safe_source_relpath
from .manifest import LoopManifest


def _assert_under(root: Path, candidate: Path) -> None:
    """Guard that ``candidate`` stays inside ``root`` (no traversal escape)."""
    root_norm = os.path.normpath(str(root))
    cand_norm = os.path.normpath(str(candidate))
    if cand_norm != root_norm and not cand_norm.startswith(root_norm + os.sep):
        raise ValueError(f"staged path escapes the run worktree: {candidate}")


def stage_loop_assets(
    config: LoopcraftConfig, manifest: LoopManifest, workdir: Path
) -> list[Path]:
    """Copy a minimal source bundle for a loop into its isolated run directory.

    M1 runs headless in an isolated working directory rather than a full git
    worktree. For the loop's own relative asset references to resolve (e.g. the
    skill telling the agent to read ``skills/slack-triage/channels.txt``), the
    skill directory and the manifest are staged into ``workdir`` preserving their
    source-relative paths.

    The ``logic.skill`` reference is validated as a safe source-relative path, and
    every staged destination is confirmed to remain under ``workdir`` so a
    malformed manifest cannot read outside the source tree or write outside the
    run directory.

    Returns the list of staged destination paths.

    Raises:
        SourcePathError: If ``logic.skill`` is absolute, scheme-qualified, or
            traverses outside the source tree.
    """
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []

    skill = manifest.logic.skill
    if skill:
        skill_rel = PurePosixPath(safe_source_relpath(skill))
        skill_src = config.resolve_source_path(skill)
        skill_dir_rel = skill_rel.parent
        skill_dir_src = skill_src.parent
        if str(skill_dir_rel) not in ("", ".") and skill_dir_src.is_dir():
            dest = (workdir / skill_dir_rel).resolve()
            _assert_under(workdir, dest)
            shutil.copytree(skill_dir_src, dest, dirs_exist_ok=True)
            staged.append(dest)
        elif skill_src.is_file():
            dest = (workdir / skill_rel).resolve()
            _assert_under(workdir, dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(skill_src, dest)
            staged.append(dest)

    if manifest.source_path and manifest.source_path.is_file():
        dest = (workdir / "loops" / manifest.source_path.name).resolve()
        _assert_under(workdir, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest.source_path, dest)
        staged.append(dest)

    return staged
