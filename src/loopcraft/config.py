from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

CONFIG_FILENAME = "loopcraft.toml"

# Loop-facing state paths are declared with this prefix (e.g. ``state/slack/x.md``)
# and resolve into the ledger directory of the memory tree.
STATE_PREFIX = "state"
LEDGER_DIRNAME = "ledger"
ARTIFACTS_DIRNAME = "artifacts"
RUNS_DIRNAME = "runs"
DB_FILENAME = "loopcraft.db"

#: Prefixes that mark a declared path as a ledger/state file the store owns.
#: Anything else (``linear:...``, ``s3://...``) is a non-file target the store
#: does not resolve, so it is exempt from state-path validation.
_STATE_PATH_PREFIXES = (STATE_PREFIX, LEDGER_DIRNAME)


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
    rel = declared.strip()
    if not rel:
        raise SourcePathError("empty source path")
    if rel.startswith("/") or os.path.isabs(rel) or PurePosixPath(rel).is_absolute():
        raise SourcePathError(f"absolute source path is not allowed: {declared!r}")
    head = rel.split("/", 1)[0]
    if ":" in head:
        raise SourcePathError(f"scheme/drive source path is not allowed: {declared!r}")
    parts = list(PurePosixPath(rel).parts)
    if any(part == ".." for part in parts):
        raise SourcePathError(f"'..' is not allowed in a source path: {declared!r}")
    if any(":" in part or part in ("", ".") for part in parts):
        raise SourcePathError(f"invalid segment in source path: {declared!r}")
    return "/".join(parts)


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
    rel = declared.strip()
    if not rel:
        raise StatePathError("empty state path")
    if rel.startswith("/") or os.path.isabs(rel) or PurePosixPath(rel).is_absolute():
        raise StatePathError(f"absolute state path is not allowed: {declared!r}")

    parts = list(PurePosixPath(rel).parts)
    if parts and parts[0] in _STATE_PATH_PREFIXES:
        parts = parts[1:]
    if not parts:
        raise StatePathError(f"state path has no file after prefix: {declared!r}")
    if any(part == ".." for part in parts):
        raise StatePathError(f"'..' is not allowed in a state path: {declared!r}")
    # Defense in depth: a Windows drive or UNC head would survive the checks above.
    if any(":" in part or part in ("", ".") for part in parts):
        raise StatePathError(f"invalid segment in state path: {declared!r}")
    return "/".join(parts)


@dataclass(frozen=True)
class LoopcraftConfig:
    """Resolved control-plane configuration.

    Connects the source tree (manifests, skills, code) to the memory tree
    (ledger + artifacts + run-history DB).
    """

    source_path: Path
    memory_path: Path
    default_vendor: str = "codex"
    host: str = "vm"
    artifact_store: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    # --- source-tree locations ---------------------------------------------
    @property
    def loops_dir(self) -> Path:
        return self.source_path / "loops"

    @property
    def skills_dir(self) -> Path:
        return self.source_path / "skills"

    @property
    def agents_dir(self) -> Path:
        return self.source_path / "agents"

    # --- memory-tree locations ---------------------------------------------
    @property
    def ledger_dir(self) -> Path:
        return self.memory_path / LEDGER_DIRNAME

    @property
    def artifacts_dir(self) -> Path:
        return self.memory_path / ARTIFACTS_DIRNAME

    @property
    def runs_dir(self) -> Path:
        return self.ledger_dir / RUNS_DIRNAME

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
        # Defense in depth: confirm the normalized path stays inside the ledger.
        ledger_root = os.path.normpath(str(self.ledger_dir))
        candidate = os.path.normpath(str(resolved))
        if candidate != ledger_root and not candidate.startswith(ledger_root + os.sep):
            raise StatePathError(f"state path escapes the ledger tree: {declared!r}")
        return resolved

    def resolve_source_path(self, declared: str) -> Path:
        """Resolve a source-relative path under the source tree, safely.

        Raises:
            SourcePathError: If the declared path is unsafe or escapes the tree.
        """
        rel = safe_source_relpath(declared)
        resolved = self.source_path / rel
        src_root = os.path.normpath(str(self.source_path))
        candidate = os.path.normpath(str(resolved))
        if candidate != src_root and not candidate.startswith(src_root + os.sep):
            raise SourcePathError(f"source path escapes the source tree: {declared!r}")
        return resolved

    @classmethod
    def load(cls, source_path: Path | str | None = None) -> "LoopcraftConfig":
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
            raw.get("memory_path", "~/workspace/loopcraft_memory")
        )
        memory = Path(memory_raw).expanduser().resolve()

        known = {"default_vendor", "host", "memory_path", "artifact_store"}
        extra = {k: v for k, v in raw.items() if k not in known}

        return cls(
            source_path=source,
            memory_path=memory,
            default_vendor=os.environ.get("LOOPCRAFT_VENDOR")
            or str(raw.get("default_vendor", "codex")),
            host=str(raw.get("host", "vm")),
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
