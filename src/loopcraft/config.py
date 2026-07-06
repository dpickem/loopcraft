"""Control-plane configuration and ledger/source path resolution.

``LoopcraftConfig`` is the single object that wires the source tree (code,
manifests, skills) to the memory tree (ledger, artifacts, run DB) and centralizes
environment access. It also exposes the validated state/source path resolvers so
loop I/O can never escape its tree.
"""

from __future__ import annotations

import os
import re
import tomllib
from datetime import date, datetime
from enum import IntEnum, StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from loopcraft.paths import assert_under, safe_relpath

# --- globals -------------------------------------------------------------------

#: Filename of the control-plane config at the source tree root.
CONFIG_FILENAME = "loopcraft.toml"
#: Filename of the Python project config holding the binary-dependency tables.
PYPROJECT_FILENAME = "pyproject.toml"

#: Loop-facing state paths are declared with this prefix (e.g. ``state/slack/x.md``)
#: and resolve into the ledger directory of the memory tree.
STATE_PREFIX = "state"
#: Filename of the derived run-history database in the memory tree.
DB_FILENAME = "loopcraft.db"
#: Default number of per-loop run worktrees kept by pruning.
DEFAULT_WORKTREE_KEEP_LAST = 100
#: Upper clamp for the worktree retention setting.
MAX_WORKTREE_KEEP_LAST = 100

#: Default runtime vendor when neither env nor toml overrides it.
DEFAULT_VENDOR = "codex"
#: Default execution host label.
DEFAULT_HOST = "vm"
#: Default memory-tree root (expanded at load time).
DEFAULT_MEMORY_PATH = "~/workspace/loopcraft_memory"

#: User agent sent by loopcraft HTTP clients (arXiv, X).
HTTP_USER_AGENT = "loopcraft/0.1"

#: Env var the control plane sets so a loop's direct CLI names its run-scoped
#: history archives with the same run id the manifest ``{{run_id}}`` outputs use.
RUN_ID_ENV = "LOOPCRAFT_RUN_ID"

#: Env var the control plane sets so a loop's direct CLI stamps its dated
#: outputs with the same UTC date the manifest ``{{date}}`` outputs resolved
#: to — a run crossing 00:00 UTC must not write the next day's filename.
RUN_DATE_ENV = "LOOPCRAFT_RUN_DATE"

#: Env var the control plane sets to the id of the loop currently executing.
#: ``loopctl run`` refuses to re-enter the same loop when it is set, so a skill
#: that (incorrectly) invokes the control plane for its own loop cannot recurse.
ACTIVE_LOOP_ENV = "LOOPCRAFT_ACTIVE_LOOP"

#: Canonical control-plane run-id shape (see ``Store.new_run_id``).
_RUN_ID_STAMP_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
#: ISO calendar-date shape for the handed-down run date.
_RUN_DATE_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# --- exceptions ------------------------------------------------------------------


class StatePathError(ValueError):
    """Raised when a declared state path would escape the ledger tree."""


class SourcePathError(ValueError):
    """Raised when a declared source-relative path would escape the source tree."""


# --- enums -----------------------------------------------------------------------


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


class ExitCode(IntEnum):
    """Closed vocabulary of process exit codes across all loopcraft CLIs.

    Every command returns one of these named values instead of a bare integer:

    - ``OK``: the command succeeded.
    - ``FAILURE``: the command ran but the operation failed (failed run,
      failed preflight/check, degraded fleet view, missing log).
    - ``INVALID``: the request itself was unusable (unknown loop, invalid
      manifest/config, bad protocol value, unknown vendor, usage errors).
    """

    OK = 0
    FAILURE = 1
    INVALID = 2


#: Prefixes that mark a declared path as a ledger/state file the store owns.
#: Anything else (``linear:...``, ``s3://...``) is a non-file target the store
#: does not resolve, so it is exempt from state-path validation. (Derived from
#: the enum above, so it lives directly after the enum definitions.)
_STATE_PATH_PREFIXES = (STATE_PREFIX, MemoryDir.LEDGER.value)

# --- private functions -------------------------------------------------------------


def _find_source_root() -> Path:
    """Walk upward from CWD looking for a ``loopcraft.toml``; fall back to CWD."""
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / CONFIG_FILENAME).exists():
            return candidate
    return here


def _load_project_dependencies(source: Path, table: str) -> dict[str, str]:
    """Load an external Loopcraft binary-dependency table from pyproject.toml.

    Args:
        source: Source tree root containing ``pyproject.toml``.
        table: Sub-table name under ``[tool.loopcraft]`` (``dependencies`` for
            required M1 binaries or ``optional-dependencies`` for future runtimes).

    Returns:
        A mapping of dependency name to the binary probed on PATH (empty when the
        file or table is absent).
    """
    pyproject_file = source / PYPROJECT_FILENAME
    if not pyproject_file.exists():
        return {}
    raw = tomllib.loads(pyproject_file.read_text(encoding="utf-8"))
    declared = raw.get("tool", {}).get("loopcraft", {}).get(table, {})
    if isinstance(declared, dict):
        return {str(k): str(v) for k, v in declared.items()}
    return {}


# --- public functions ---------------------------------------------------------------


def validate_run_id_stamp(value: str) -> str:
    """Validate a ``LOOPCRAFT_RUN_ID`` protocol value.

    Direct CLIs inherit arbitrary caller environments, so the handed-down run
    id must match the canonical control-plane format — one safe filename
    component — before it is used to name ledger files.

    Raises:
        ValueError: If the value is not a canonical control-plane run id.
    """
    if not _RUN_ID_STAMP_RE.fullmatch(value):
        raise ValueError(
            f"invalid {RUN_ID_ENV} value {value!r} (expected <YYYYMMDDTHHMMSSZ>-<hex8>)"
        )
    return value


def validate_run_date_stamp(value: str) -> str:
    """Validate a ``LOOPCRAFT_RUN_DATE`` protocol value as a real ISO date.

    Raises:
        ValueError: If the value is not a valid ``YYYY-MM-DD`` calendar date.
    """
    if not _RUN_DATE_STAMP_RE.fullmatch(value):
        raise ValueError(f"invalid {RUN_DATE_ENV} value {value!r} (expected YYYY-MM-DD)")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {RUN_DATE_ENV} value {value!r}: {exc}") from exc
    return value


def resolve_run_stamps(config: LoopcraftConfig, now: datetime) -> tuple[str, str]:
    """Return validated ``(run_stamp, date_stamp)`` for a loop workflow.

    Uses the control plane's handed-down ``LOOPCRAFT_RUN_ID`` /
    ``LOOPCRAFT_RUN_DATE`` when set (validated as protocol values), falling back
    to ``now`` only for standalone direct-CLI runs.

    Raises:
        ValueError: If an inherited protocol value is malformed.
    """
    run_id = config.env_value(RUN_ID_ENV)
    run_date = config.env_value(RUN_DATE_ENV)
    run_stamp = validate_run_id_stamp(run_id) if run_id else now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = validate_run_date_stamp(run_date) if run_date else now.strftime("%Y-%m-%d")
    return run_stamp, date_stamp


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
    optional_dependencies: dict[str, str] = Field(default_factory=dict)
    artifact_store: str | None = None
    extra: dict[str, object] = Field(default_factory=dict)

    # --- source-tree locations ---------------------------------------------
    @property
    def loops_dir(self) -> Path:
        """Directory holding loop manifests in the source tree."""
        return self.source_path / SourceDir.LOOPS

    @property
    def skills_dir(self) -> Path:
        """Directory holding skills in the source tree."""
        return self.source_path / SourceDir.SKILLS

    @property
    def agents_dir(self) -> Path:
        """Directory holding agent definitions in the source tree."""
        return self.source_path / SourceDir.AGENTS

    # --- memory-tree locations ---------------------------------------------
    @property
    def ledger_dir(self) -> Path:
        """Ledger root in the memory tree (durable loop state and outputs)."""
        return self.memory_path / MemoryDir.LEDGER

    @property
    def artifacts_dir(self) -> Path:
        """Artifact store root in the memory tree."""
        return self.memory_path / MemoryDir.ARTIFACTS

    @property
    def runs_dir(self) -> Path:
        """Directory holding per-run records under the ledger."""
        return self.ledger_dir / MemoryDir.RUNS

    @property
    def db_path(self) -> Path:
        """Path to the derived run-history database in the memory tree."""
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

    def assert_source_contained(self, candidate: Path, *, label: str = "source asset") -> None:
        """Guard that ``candidate`` (symlinks resolved) stays under the source root.

        Args:
            candidate: Path to check.
            label: Human-readable label used in the error message.

        Raises:
            SourcePathError: If the resolved candidate escapes the source tree.
        """
        try:
            assert_under(self.source_path, candidate, label=label)
        except ValueError as exc:
            raise SourcePathError(str(exc)) from exc

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

        dependencies = _load_project_dependencies(source, "dependencies")
        optional_dependencies = _load_project_dependencies(source, "optional-dependencies")

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
            optional_dependencies=optional_dependencies,
            artifact_store=(
                str(raw["artifact_store"]) if raw.get("artifact_store") else None
            ),
            extra=extra,
        )
