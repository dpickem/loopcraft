from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath

from .config import LoopcraftConfig, safe_source_relpath
from .manifest import LoopManifest
from .settings import asset_env_var, split_env_list

#: Marker that identifies a private override file: ``channels.local.txt``.
_LOCAL_MARKER = ".local."


def _assert_under(root: Path, candidate: Path) -> None:
    """Guard that ``candidate`` stays inside ``root`` (no traversal escape)."""
    root_norm = os.path.normpath(str(root))
    cand_norm = os.path.normpath(str(candidate))
    if cand_norm != root_norm and not cand_norm.startswith(root_norm + os.sep):
        raise ValueError(f"staged path escapes the run worktree: {candidate}")


def _apply_local_shadowing(workdir: Path) -> None:
    """Overlay any staged ``*.local.*`` file onto its public counterpart.

    A private override (e.g. ``channels.local.txt``) replaces the public file
    (``channels.txt``) in the run worktree, then the ``.local`` copy is removed so
    the agent only ever sees the resolved file.
    """
    for local_file in sorted(workdir.rglob(f"*{_LOCAL_MARKER}*")):
        if not local_file.is_file():
            continue
        base = local_file.with_name(local_file.name.replace(_LOCAL_MARKER, ".", 1))
        shutil.copy2(local_file, base)
        local_file.unlink()


def _apply_env_overrides(workdir: Path, environ: dict[str, str]) -> None:
    """Override staged skill list assets (``skills/**/*.txt``) from the environment.

    For each ``.txt`` asset, the env var derived by :func:`asset_env_var` (if set)
    replaces the file contents with the resolved list — the highest-precedence
    source in the public/private split.
    """
    skills_root = workdir / "skills"
    if not skills_root.is_dir():
        return
    for asset in sorted(skills_root.rglob("*.txt")):
        if not asset.is_file():
            continue
        env_var = asset_env_var(asset.relative_to(workdir).as_posix())
        value = environ.get(env_var)
        if not value or not value.strip():
            continue
        entries = split_env_list(value)
        header = f"# Resolved from ${env_var} at run time — do not edit.\n"
        asset.write_text(header + "\n".join(entries) + "\n", encoding="utf-8")


def stage_loop_assets(
    config: LoopcraftConfig,
    manifest: LoopManifest,
    workdir: Path,
    environ: dict[str, str] | None = None,
) -> list[Path]:
    """Copy a minimal source bundle for a loop into its isolated run directory.

    M1 runs headless in an isolated working directory rather than a full git
    worktree. For the loop's own relative asset references to resolve (e.g. the
    skill telling the agent to read ``skills/slack-triage/channels.txt``), the
    skill directory and the manifest are staged into ``workdir`` preserving their
    source-relative paths.

    After staging, the public/private config split is applied so confidential
    values never need to live in the source repo: any ``*.local.*`` override
    shadows its public counterpart, then a matching ``LOOPCRAFT_*`` environment
    variable (see :func:`loopcraft.settings.asset_env_var`) overrides a skill list
    asset outright. Precedence is env var > ``*.local.*`` file > public file.

    The ``logic.skill`` reference is validated as a safe source-relative path, and
    every staged destination is confirmed to remain under ``workdir`` so a
    malformed manifest cannot read outside the source tree or write outside the
    run directory.

    Returns the list of staged destination paths.

    Raises:
        SourcePathError: If ``logic.skill`` is absolute, scheme-qualified, or
            traverses outside the source tree.
    """
    environ = os.environ if environ is None else environ
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

    _apply_local_shadowing(workdir)
    _apply_env_overrides(workdir, environ)
    return staged
