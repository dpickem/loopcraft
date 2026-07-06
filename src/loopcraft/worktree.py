"""Staging of loop assets into an isolated per-run worktree.

Copies the manifest, its declared skill, and referenced config into a scratch
worktree, materializing public/private overrides so relative paths resolve during
a headless run without leaking secrets into the source tree.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath

from loopcraft.config import LoopcraftConfig, SourcePathError, safe_source_relpath
from loopcraft.manifest import LoopManifest
from loopcraft.paths import assert_under
from loopcraft.settings import asset_env_var, local_sibling_path, split_env_list

#: Marker that identifies a private override file: ``channels.local.txt``.
_LOCAL_MARKER = ".local."

#: Scratch area (under the memory tree) for per-run worktrees.
_WORKTREES_SUBPATH = ("var", "worktrees")


class StagingError(RuntimeError):
    """Raised when a loop's declared runtime asset cannot be staged."""


def _assert_under(root: Path, candidate: Path) -> None:
    """Guard that ``candidate`` stays inside the run worktree.

    A label-currying convenience over :func:`loopcraft.paths.assert_under` (the
    shared containment primitive) so every staging call site reports the same
    ``run worktree`` boundary.
    """
    assert_under(root, candidate, label="run worktree")


def worktrees_root(config: LoopcraftConfig) -> Path:
    """Root of the per-run worktree scratch area in the memory tree."""
    return config.memory_path.joinpath(*_WORKTREES_SUBPATH)


def worktree_dir(config: LoopcraftConfig, loop_id: str, run_id: str) -> Path:
    """Return the per-run worktree directory for a loop.

    The result is verified to remain under the worktree root, so manifest data
    can never choose an arbitrary staging directory (id validation upstream makes
    this unreachable; the guard is defense in depth).

    Raises:
        ValueError: If ``loop_id``/``run_id`` would escape the worktree root.
    """
    root = worktrees_root(config)
    path = root.joinpath(loop_id, run_id)
    assert_under(root, path, label="run worktree")
    return path


def prune_loop_worktrees(config: LoopcraftConfig, loop_id: str, *, keep_last: int) -> list[Path]:
    """Keep only the newest N per-run worktree directories for one loop.

    The worktree area is scratch/debug state under ``<memory>/var/worktrees``;
    durable run records and outputs live in ``ledger/``. Pruning therefore never
    removes canonical loop state. ``keep_last`` is clamped by config to 0..100.

    Raises:
        ValueError: If ``loop_id`` would make the prune target escape the
            worktree root (defense in depth; ids are validated upstream).
    """
    root = worktrees_root(config)
    loop_dir = root / loop_id
    assert_under(root, loop_dir, label="worktree prune target")
    if not loop_dir.exists():
        return []

    children = [p for p in loop_dir.iterdir() if p.is_dir()]
    children.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    removed: list[Path] = []
    for path in children[keep_last:]:
        shutil.rmtree(path)
        removed.append(path)
    return removed


def _copytree_contained(config: LoopcraftConfig, src_dir: Path, dest: Path, workdir: Path) -> None:
    """Copy a skill directory into the worktree, validating every entry.

    Containment is scoped to the *declared skill directory* (resolved), not
    merely the repository: staging a minimal loop bundle, a directory symlink
    pointing at an unrelated in-repo location (e.g. the source root with its
    gitignored ``.env``) must not pull those files into the bundle. A symlink
    whose target resolves under the skill root is dereference-copied like any
    other entry; anything else aborts staging.

    Cycle detection is ancestry-local (only a directory that reappears in its
    own ancestor chain stops that branch), so two allowed aliases of the same
    directory each materialize their contents.

    Raises:
        SourcePathError: If any staged entry resolves outside the skill root.
        OSError: If an entry cannot be copied (e.g. a dangling symlink).
    """
    config.assert_source_contained(src_dir)
    root = src_dir.resolve()

    def guard(candidate: Path) -> None:
        """Require ``candidate`` (symlinks resolved) to stay under the skill root.

        Raises:
            SourcePathError: If the resolved entry escapes the skill directory.
        """
        try:
            assert_under(root, candidate, label="skill asset")
        except ValueError as exc:
            raise SourcePathError(str(exc)) from exc

    def copy_dir(current: Path, dest_dir: Path, ancestors: tuple[Path, ...]) -> None:
        """Recursively copy one contained directory level into the worktree.

        Args:
            current: Source directory being copied (validated by ``guard``).
            dest_dir: Destination directory inside the run worktree.
            ancestors: Resolved directories on this branch, for cycle detection.
        """
        real = current.resolve()
        if real in ancestors:  # true symlink cycle along this branch: stop here
            return
        guard(current)
        _assert_under(workdir, dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for entry in sorted(current.iterdir()):
            if entry.is_dir():
                copy_dir(entry, (dest_dir / entry.name).resolve(), ancestors + (real,))
            else:
                guard(entry)
                dest_file = (dest_dir / entry.name).resolve()
                _assert_under(workdir, dest_file)
                shutil.copy2(entry, dest_file)

    copy_dir(src_dir, dest, ())


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

    Skills use two file kinds by convention: ``.md`` for prose (SKILL/verify
    text) and ``.txt`` for line-list assets (e.g.
    ``skills/slack-triage/channels.txt``). Only the list assets are
    env-overridable: for each ``.txt`` asset, the env var derived by
    :func:`asset_env_var` (if set) replaces the file contents with the resolved
    list — the highest-precedence source in the public/private split.
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

    Materializes the content config into the run worktree preserving its
    source-relative path (e.g. ``config/x_intel.yaml``). Per the public/private
    split, the declared *public* file must exist as a committed source asset; a
    gitignored ``*.local.*`` sibling is staged alongside it so
    :func:`_apply_local_shadowing` overlays the private override — it shadows
    the public file's values but never replaces its existence contract.

    Args:
        config: Resolved control-plane config used to resolve source paths.
        manifest: The loop manifest whose ``content.config`` is staged.
        workdir: The run worktree root.

    Returns:
        The list of staged destination paths (empty when no content config).

    Raises:
        SourcePathError: If ``content.config`` escapes the source tree.
        StagingError: If the declared public config is missing or not a regular
            file, so the loop's declared dependency cannot be met.
    """
    declared = manifest.content.config
    if not declared:
        return []
    rel = PurePosixPath(safe_source_relpath(declared))
    src = config.resolve_source_path(declared)
    if not src.exists():
        raise StagingError(
            f"content.config not found: {declared} (the public file must exist and be "
            "committed; a *.local.* sibling may shadow its values)"
        )
    if not src.is_file():
        raise StagingError(f"content.config is not a regular file: {declared}")
    dest = (workdir / rel).resolve()
    _assert_under(workdir, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    staged: list[Path] = [dest]
    local_src = local_sibling_path(src)
    if local_src.is_file():
        # A symlinked local override must resolve under the source root too.
        config.assert_source_contained(local_src)
        dest_local = (workdir / rel.parent / local_src.name).resolve()
        _assert_under(workdir, dest_local)
        dest_local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_src, dest_local)
        staged.append(dest_local)
    return staged


def _stage_extra_assets(
    config: LoopcraftConfig, declared_assets: list[str], workdir: Path
) -> list[Path]:
    """Stage extra content-referenced assets verbatim (no local shadowing).

    These are exact source-relative paths referenced by the loop's effective
    content config (e.g. the X following snapshot, itself often a ``*.local.*``
    file). They are staged after the shadowing pass so each keeps the exact
    name the config refers to.

    Raises:
        StagingError: If a declared asset is not a regular file.
        SourcePathError: If an asset escapes the source tree.
    """
    staged: list[Path] = []
    for declared in declared_assets:
        rel = PurePosixPath(safe_source_relpath(declared))
        src = config.resolve_source_path(declared)
        if not src.is_file():
            raise StagingError(f"content asset not found: {declared}")
        config.assert_source_contained(src)
        dest = (workdir / rel).resolve()
        _assert_under(workdir, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        staged.append(dest)
    return staged


def stage_loop_assets(
    config: LoopcraftConfig,
    manifest: LoopManifest,
    workdir: Path,
    environ: dict[str, str] | None = None,
    extra_assets: list[str] | None = None,
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

    The ``logic.skill`` reference is validated as a safe source-relative path,
    every enumerated source entry (including sibling and nested symlinks) must
    resolve under the source root before it is copied, and every staged
    destination is confirmed to remain under ``workdir`` — so a malformed
    manifest or a planted symlink cannot read outside the source tree or write
    outside the run directory.

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
            _copytree_contained(config, skill_dir_src, dest, workdir)
            staged.append(dest)
        elif skill_src.is_file():
            dest = (workdir / skill_rel).resolve()
            _assert_under(workdir, dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(skill_src, dest)
            staged.append(dest)

    if manifest.source_path and manifest.source_path.is_file():
        config.assert_source_contained(manifest.source_path)
        dest = (workdir / "loops" / manifest.source_path.name).resolve()
        _assert_under(workdir, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest.source_path, dest)
        staged.append(dest)

    staged += _stage_content_config(config, manifest, workdir)

    _apply_local_shadowing(workdir)
    _apply_env_overrides(workdir, environ)
    staged += _stage_extra_assets(config, extra_assets or [], workdir)
    return staged
