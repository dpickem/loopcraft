"""Staging of loop assets into an isolated per-run worktree.

Copies the manifest, its declared skill, and referenced config into a scratch
worktree, materializing public/private overrides so relative paths resolve during
a headless run without leaking secrets into the source tree.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath

from loopcraft.config import LoopcraftConfig, safe_source_relpath
from loopcraft.manifest import LoopManifest
from loopcraft.paths import assert_under
from loopcraft.settings import asset_env_var, local_sibling_path, split_env_list

#: Marker that identifies a private override file: ``channels.local.txt``.
_LOCAL_MARKER = ".local."


class StagingError(RuntimeError):
    """Raised when a loop's declared runtime asset cannot be staged."""


def _assert_under(root: Path, candidate: Path) -> None:
    """Guard that ``candidate`` stays inside ``root`` (no traversal escape)."""
    assert_under(root, candidate, label="run worktree")


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


def _stage_content_config(
    config: LoopcraftConfig, manifest: LoopManifest, workdir: Path
) -> list[Path]:
    """Stage the loop's ``content.config`` file (and its ``.local.`` sibling).

    Materializes the effective content config into the run worktree preserving its
    source-relative path (e.g. ``config/x_intel.yaml``). A gitignored
    ``*.local.*`` sibling is staged alongside it so :func:`_apply_local_shadowing`
    overlays the private override, keeping the public/private split intact.

    Args:
        config: Resolved control-plane config used to resolve source paths.
        manifest: The loop manifest whose ``content.config`` is staged.
        workdir: The run worktree root.

    Returns:
        The list of staged destination paths (empty when no content config).

    Raises:
        SourcePathError: If ``content.config`` escapes the source tree.
        StagingError: If neither the declared public config nor its ``*.local.*``
            override is a regular file, so the loop's declared dependency cannot
            be met.
    """
    declared = manifest.content.config
    if not declared:
        return []
    rel = PurePosixPath(safe_source_relpath(declared))
    src = config.resolve_source_path(declared)
    if src.exists() and not src.is_file():
        raise StagingError(f"content.config is not a regular file: {declared}")
    staged: list[Path] = []
    if src.is_file():
        dest = (workdir / rel).resolve()
        _assert_under(workdir, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        staged.append(dest)
    local_src = local_sibling_path(src)
    if local_src.is_file():
        dest_local = (workdir / rel.parent / local_src.name).resolve()
        _assert_under(workdir, dest_local)
        dest_local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_src, dest_local)
        staged.append(dest_local)
    if not staged:
        raise StagingError(
            f"content.config not found: {declared} (no public file or *.local.* override)"
        )
    return staged


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
        StagingError: If a declared ``content.config`` cannot be staged (see
            :func:`_stage_content_config`).
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

    staged += _stage_content_config(config, manifest, workdir)

    _apply_local_shadowing(workdir)
    _apply_env_overrides(workdir, environ)
    return staged
