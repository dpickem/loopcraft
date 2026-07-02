"""Control-plane configuration and ledger/source path resolution.

``LoopcraftConfig`` is the single object that wires the source tree (code,
manifests, skills) to the memory tree (ledger, artifacts, run DB) and centralizes
environment access. It also exposes the validated state/source path resolvers so
loop I/O can never escape its tree.
"""

from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from loopcraft.paths import assert_under, safe_relpath

CONFIG_FILENAME = "loopcraft.toml"
PYPROJECT_FILENAME = "pyproject.toml"


class SourceDir(StrEnum):
    """Closed vocabulary of top-level directories in the source tree."""

    LOOPS = "loops"
    SKILLS = "skills"
    AGENTS = "agents"


class MemoryDir(StrEnum):
    """Closed vocabulary of top-level directories in the memory tree."""

    LEDGER = "ledger"
    ARTIFACTS = "artifacts"
    RUNS = "runs"


# Loop-facing state paths are declared with this prefix (e.g. ``state/slack/x.md``)
# and resolve into the ledger directory of the memory tree.
STATE_PREFIX = "state"
DB_FILENAME = "loopcraft.db"
DEFAULT_WORKTREE_KEEP_LAST = 100
MAX_WORKTREE_KEEP_LAST = 100

#: Global defaults for control-plane settings (overridable via env/toml).
DEFAULT_VENDOR = "codex"
DEFAULT_HOST = "vm"
DEFAULT_MEMORY_PATH = "~/workspace/loopcraft_memory"

#: Prefixes that mark a declared path as a ledger/state file the store owns.
#: Anything else (``linear:...``, ``s3://...``) is a non-file target the store
#: does not resolve, so it is exempt from state-path validation.
_STATE_PATH_PREFIXES = (STATE_PREFIX, MemoryDir.LEDGER.value)


class StatePathError(ValueError):
    """Raised when a declared state path would escape the ledger tree."""


class SourcePathError(ValueError):
    """Raised when a declared source-relative path would escape the source tree."""


def safe_source_relpath(declared: str) -> str:
    """Validate and normalize a source-relative path (e.g. ``logic.skill``).

    Requires a non-empty, source-relative path with no absolute root, drive/UNC
    or scheme head, and no ``..`` traversal, so staging a loop's assets can never
    read outside the source tree or write outside the run workdir.

    Raises:
        SourcePathError: If the path is empty, absolute, scheme-qualified, or
            contains a ``..`` segment.
    """
    try:
        return safe_relpath(declared, kind="source")
    except ValueError as exc:
        raise SourcePathError(str(exc)) from exc


def is_state_path(declared: str) -> bool:
    """Return True if ``declared`` is a ledger/state path the store resolves.

    Scheme-style targets such as ``linear:project/Daily`` or ``s3://bucket/k``
    are external sinks, not ledger files, so they are not state paths.
    """
    rel = declared.strip()
    if not rel:
        return False
    first = PurePosixPath(rel).parts[0] if PurePosixPath(rel).parts else ""
    if first in _STATE_PATH_PREFIXES:
        return True
    # A scheme target (``name:rest``) is never a ledger file.
    head = rel.split("/", 1)[0]
    return ":" not in head


def safe_state_relpath(declared: str) -> str:
    """Validate and normalize a declared state path to a ledger-relative path.

    Strips an optional leading ``state/`` (or ``ledger/``) segment and returns
    the remainder as a POSIX-style relative path. Rejects empty paths, absolute
    paths, drive-qualified paths, and any ``..`` traversal so a manifest can
    never write outside the ledger tree.

    Raises:
        StatePathError: If the path is empty, absolute, or escapes the ledger.
    """
    try:
        return safe_relpath(declared, prefixes=_STATE_PATH_PREFIXES, kind="state")
    except ValueError as exc:
        raise StatePathError(str(exc)) from exc


class LoopcraftConfig(BaseModel):
    """Resolved control-plane configuration.

    Connects the source tree (manifests, skills, code) to the memory tree
    (ledger + artifacts + run-history DB).
    """

    model_config = ConfigDict(frozen=True)

    source_path: Path
    memory_path: Path
    default_vendor: str = DEFAULT_VENDOR
    host: str = DEFAULT_HOST
    worktree_keep_last: int = DEFAULT_WORKTREE_KEEP_LAST
    dependencies: dict[str, str] = Field(default_factory=dict)
    artifact_store: str | None = None
    extra: dict[str, object] = Field(default_factory=dict)

    # --- source-tree locations ---------------------------------------------
    @property
    def loops_dir(self) -> Path:
        return self.source_path / SourceDir.LOOPS

    @property
    def skills_dir(self) -> Path:
        return self.source_path / SourceDir.SKILLS

    @property
    def agents_dir(self) -> Path:
        return self.source_path / SourceDir.AGENTS

    # --- memory-tree locations ---------------------------------------------
    @property
    def ledger_dir(self) -> Path:
        return self.memory_path / MemoryDir.LEDGER

    @property
    def artifacts_dir(self) -> Path:
        return self.memory_path / MemoryDir.ARTIFACTS

    @property
    def runs_dir(self) -> Path:
        return self.ledger_dir / MemoryDir.RUNS

    @property
    def db_path(self) -> Path:
        return self.memory_path / DB_FILENAME

    def resolve_state_path(self, declared: str) -> Path:
        """Map a loop-declared output path to a concrete file in the ledger.

        ``state/slack/triage-latest.md`` -> ``<memory>/ledger/slack/triage-latest.md``.
        Paths without the ``state/`` prefix are treated as ledger-relative. The
        path is validated so it can never escape the ledger tree.

        Raises:
            StatePathError: If the declared path is absolute or escapes the ledger.
        """
        rel = safe_state_relpath(declared)
        resolved = self.ledger_dir / rel
        try:
            assert_under(self.ledger_dir, resolved, label="state path")
        except ValueError as exc:
            raise StatePathError(str(exc)) from exc
        return resolved

    def resolve_state_template(self, declared: str, *, run_id: str, date: str) -> Path:
        """Resolve a state path that may contain run/date template variables.

        Supported variables:
        - ``{{run_id}}`` — durable run identifier (timestamp + short UUID)
        - ``{{date}}`` — UTC date as ``YYYY-MM-DD``
        """
        rendered = declared.replace("{{run_id}}", run_id).replace("{{date}}", date)
        return self.resolve_state_path(rendered)

    def resolve_source_path(self, declared: str) -> Path:
        """Resolve a source-relative path under the source tree, safely.

        Raises:
            SourcePathError: If the declared path is unsafe or escapes the tree.
        """
        rel = safe_source_relpath(declared)
        resolved = self.source_path / rel
        try:
            assert_under(self.source_path, resolved, label="source path")
        except ValueError as exc:
            raise SourcePathError(str(exc)) from exc
        return resolved

    def env_value(self, name: str) -> str | None:
        """Return one environment value through the central config object."""
        return os.environ.get(name)

    @classmethod
    def load(cls, source_path: Path | str | None = None) -> LoopcraftConfig:
        """Load configuration from ``loopcraft.toml`` in the source tree.

        Environment overrides (handy for tests and per-host tweaks):
          - ``LOOPCRAFT_SOURCE``  -> source tree root
          - ``LOOPCRAFT_MEMORY``  -> memory tree root
          - ``LOOPCRAFT_VENDOR``  -> default runtime vendor
        """
        source = Path(
            source_path
            or os.environ.get("LOOPCRAFT_SOURCE")
            or _find_source_root()
        ).expanduser().resolve()

        raw: dict[str, object] = {}
        config_file = source / CONFIG_FILENAME
        if config_file.exists():
            raw = tomllib.loads(config_file.read_text(encoding="utf-8"))

        memory_raw = os.environ.get("LOOPCRAFT_MEMORY") or str(
            raw.get("memory_path", DEFAULT_MEMORY_PATH)
        )
        memory = Path(memory_raw).expanduser().resolve()

        keep_last_raw = os.environ.get("LOOPCRAFT_WORKTREE_KEEP_LAST") or str(
            raw.get("worktree_keep_last", DEFAULT_WORKTREE_KEEP_LAST)
        )
        try:
            keep_last = int(keep_last_raw)
        except ValueError:
            keep_last = DEFAULT_WORKTREE_KEEP_LAST
        keep_last = max(0, min(keep_last, MAX_WORKTREE_KEEP_LAST))

        dependencies = _load_project_dependencies(source)

        known = {
            "default_vendor",
            "host",
            "memory_path",
            "artifact_store",
            "worktree_keep_last",
        }
        extra = {k: v for k, v in raw.items() if k not in known}

        return cls(
            source_path=source,
            memory_path=memory,
            default_vendor=os.environ.get("LOOPCRAFT_VENDOR")
            or str(raw.get("default_vendor", DEFAULT_VENDOR)),
            host=str(raw.get("host", DEFAULT_HOST)),
            worktree_keep_last=keep_last,
            dependencies=dependencies,
            artifact_store=(
                str(raw["artifact_store"]) if raw.get("artifact_store") else None
            ),
            extra=extra,
        )


def _find_source_root() -> Path:
    """Walk upward from CWD looking for a ``loopcraft.toml``; fall back to CWD."""
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / CONFIG_FILENAME).exists():
            return candidate
    return here


def _load_project_dependencies(source: Path) -> dict[str, str]:
    """Load external Loopcraft binary dependencies from pyproject.toml."""
    pyproject_file = source / PYPROJECT_FILENAME
    if not pyproject_file.exists():
        return {}
    raw = tomllib.loads(pyproject_file.read_text(encoding="utf-8"))
    declared = (
        raw.get("tool", {})
        .get("loopcraft", {})
        .get("dependencies", {})
    )
    if isinstance(declared, dict):
        return {str(k): str(v) for k, v in declared.items()}
    return {}
