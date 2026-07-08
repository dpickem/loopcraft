"""Control-plane configuration and ledger/source path resolution.

``LoopcraftConfig`` is the single object that wires the source tree (code,
manifests, skills) to the memory tree (ledger, artifacts, run DB) and centralizes
environment access. It also exposes the validated state/source path resolvers so
loop I/O can never escape its tree.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from datetime import date, datetime
from enum import IntEnum, StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from loopcraft.env import load_dotenv as _load_dotenv_file, parse_env_file
from loopcraft.paths import assert_under, safe_relpath
from loopcraft.settings import local_override_path

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

#: Command the rendered systemd service invokes to run one loop. Kept as a bare
#: name by default (resolved on the unit's PATH); set an absolute path in
#: ``[scheduler].loopctl_bin`` for a hardened host.
DEFAULT_LOOPCTL_BIN = "loopctl"
#: Project uv-managed ``loopctl`` location under the source tree. When present
#: and ``[scheduler].loopctl_bin`` is unset, config load defaults the scheduled
#: command to this absolute path so a clean ``uv sync`` checkout renders units
#: (e.g. ``make check``) without extra config.
_VENV_LOOPCTL_SUBPATH = (".venv", "bin", "loopctl")
#: Filename prefix for every rendered systemd unit (``loop-<id>.timer`` etc.),
#: so the whole fleet is greppable and ``systemctl`` completion groups it.
DEFAULT_UNIT_PREFIX = "loop-"
#: Subpath (under the memory tree) where ``loopctl apply`` renders units before
#: install, so generated files never land in either git tree.
SYSTEMD_STAGE_SUBPATH = ("var", "systemd")
#: systemd's compiled-in default PATH for a service with no explicit ``PATH=``.
#: Scheduled preflight resolves runtime/tool binaries against this (unless
#: ``[scheduler].path`` overrides it), and the rendered unit sets exactly the
#: same value, so ``apply`` validates the PATH the service actually runs with.
SYSTEMD_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: Safe systemd unit filename prefix: letters, digits, ``_``, ``.``, ``-`` only.
#: A prefix with path separators, ``..``, whitespace, or glob metacharacters
#: could make a rendered unit filename escape the staging / unit directory.
_UNIT_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

#: Operator environment variables a *scheduled* live probe is allowed to inherit.
#: A systemd service only sees a minimal base env plus its own Environment=/
#: EnvironmentFile= lines, so a scheduled probe starts from this allowlist rather
#: than the operator's full environment (proxies, cert paths, etc. must be in the
#: EnvironmentFile to affect a probe, matching what the deployed service sees).
_SCHEDULED_ENV_ALLOWLIST = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM")

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


class SystemdScope(StrEnum):
    """Which systemd manager owns the rendered units.

    - ``system``: system-wide units under ``/etc/systemd/system`` managed by
      ``systemctl`` (root). The design default for the always-on VM.
    - ``user``: per-user units under ``~/.config/systemd/user`` managed by
      ``systemctl --user`` (no root needed; requires a login/lingering session).
    """

    SYSTEM = "system"
    USER = "user"


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


def _abs_path_list_problems(value: str, *, field: str) -> list[str]:
    """Validate a ``PATH``-style string as absolute, non-empty components.

    A relative or empty (``""`` = current directory) component resolves
    differently depending on the process cwd, so ``apply`` (run anywhere) and the
    systemd service (run from ``WorkingDirectory``) would search different
    directories. Requiring absolute components keeps the two aligned.

    Returns:
        A list of problem strings (empty when every component is absolute and
        free of ``..``). ``~`` is expanded before the checks.
    """
    problems: list[str] = []
    for entry in value.split(os.pathsep):
        if entry == "":
            problems.append(f"{field} has an empty component (means current dir): {value!r}")
            continue
        expanded = os.path.expanduser(entry)
        if not os.path.isabs(expanded):
            problems.append(f"{field} entry is not an absolute directory: {entry!r}")
        elif ".." in Path(expanded).parts:
            problems.append(f"{field} entry contains '..': {entry!r}")
    return problems


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


class SchedulerConfig(BaseModel):
    """Host-specific settings for rendering + installing systemd units (M2).

    Loaded from the ``[scheduler]`` table of ``loopcraft.toml``. Everything here
    is a per-host operational choice (which manager owns the units, which user
    runs them, where secrets live) — it never affects loop semantics, so it is
    kept out of the manifests.

    Attributes:
        loopctl_bin: The command the rendered service runs (``ExecStart``).
        unit_prefix: Filename prefix for every rendered unit.
        scope: Which systemd manager (``system`` or ``user``) owns the units.
        user: Optional ``User=`` for system-scope services (ignored for user
            scope, where the units already run as the invoking user).
        environment_file: Optional ``EnvironmentFile=`` path holding the host's
            secrets. Per the security model this lives outside *both* git trees.
        path: Optional ``PATH`` the scheduled service runs with. When set it is
            rendered as ``Environment=PATH=`` and ``apply`` resolves runtime/tool
            binaries against it; when unset, systemd's default service PATH is
            used for both.
    """

    model_config = ConfigDict(frozen=True)

    loopctl_bin: str = DEFAULT_LOOPCTL_BIN
    unit_prefix: str = DEFAULT_UNIT_PREFIX
    scope: SystemdScope = SystemdScope.SYSTEM
    user: str | None = None
    environment_file: str | None = None
    path: str | None = None

    def problems(self) -> list[str]:
        """Return structural (root-independent) scheduler config problems.

        Covers the render-affecting fields — ``unit_prefix`` (must be a safe
        filename prefix so a rendered unit cannot escape its directory) and
        ``path`` (must be absolute components). ``environment_file`` placement is
        validated separately in :func:`loopcraft.deploy.environment_file_health`
        because it needs the source/memory roots.
        """
        out: list[str] = []
        if not _UNIT_PREFIX_RE.fullmatch(self.unit_prefix):
            out.append(
                "scheduler.unit_prefix must be a safe filename prefix "
                f"(letters, digits, '_', '.', '-'): {self.unit_prefix!r}"
            )
        if self.path is not None:
            out += _abs_path_list_problems(self.path, field="scheduler.path")
        return out


class LoopcraftConfig(BaseModel):
    """Resolved control-plane configuration.

    Connects the source tree (manifests, skills, code) to the memory tree
    (ledger + artifacts + run-history DB).

    Attributes:
        source_path: Absolute root of the source tree (loops/, skills/, code).
        memory_path: Absolute root of the memory tree (ledger/, artifacts/, DB).
        default_vendor: Runtime vendor used when a loop does not pin one.
        host: Execution host label (informational; e.g. ``vm``).
        worktree_keep_last: Number of per-loop run worktrees retained by pruning.
        dependencies: Required binary name -> probe target (from pyproject).
        optional_dependencies: Optional/future-runtime binary probes (never fail
            ``deps check``).
        artifact_store: Optional external artifact-store URI (e.g. ``s3://...``).
        scheduler: Host-specific systemd rendering/install settings
            (see :class:`SchedulerConfig`).
        scheduled_env: When True, ``env_value``/``which`` resolve against the
            scheduled service environment (set only on the copy handed to
            ``apply``/``auth`` preflight; see :meth:`for_scheduled_preflight`).
        extra: Any unrecognized top-level ``loopcraft.toml`` keys, preserved.
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
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    #: When True, ``env_value`` resolves against the scheduled service's
    #: ``EnvironmentFile`` instead of the operator's process env. Set only on the
    #: copy handed to ``apply``/``auth`` preflight (see ``for_scheduled_preflight``),
    #: so a direct ``loopctl run`` keeps reading the live process environment.
    scheduled_env: bool = False
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

    @property
    def systemd_stage_dir(self) -> Path:
        """Directory (under the memory tree) where units are rendered pre-install.

        Rendered units are generated files, so they are staged under the memory
        tree's ``var/`` scratch area rather than either git tree; ``loopctl
        apply --install`` copies them into the real systemd unit directory.
        """
        return self.memory_path.joinpath(*SYSTEMD_STAGE_SUBPATH)

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

    def resolve_content_config(self, declared: str) -> Path:
        """Resolve a content-config path to the contained effective file.

        Validates ``declared`` as a source-relative path, prefers a gitignored
        ``*.local.*`` sibling, and asserts the effective file (symlinks resolved)
        stays under the source tree. This gives the direct research CLIs the same
        source-boundary and ``.local`` containment the control-plane preflight and
        worktree staging already enforce, so a ``--config`` (or its ``.local``
        override) cannot read outside the source tree.

        Raises:
            SourcePathError: If ``declared`` is unsafe or the effective file
                escapes the source tree.
        """
        public = self.resolve_source_path(declared)
        effective = local_override_path(public)
        self.assert_source_contained(effective, label="content config")
        return effective

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

    def load_dotenv(self) -> None:
        """Load the source tree's ``.env`` into the process environment.

        Loads ``<source_path>/.env`` (only for variables not already set), so
        credentials/config overrides stay tied to ``LOOPCRAFT_SOURCE`` rather
        than whatever directory a command happened to be invoked from. A no-op
        when the file is absent.
        """
        _load_dotenv_file(self.source_path / ".env")

    def env_value(self, name: str) -> str | None:
        """Return one environment value through the central config object.

        In the default (direct) mode this reads the live process environment
        (which includes any ``.env`` loaded at the CLI boundary). On a config
        marked for scheduled preflight (:meth:`for_scheduled_preflight`) it
        resolves against the scheduled service's ``EnvironmentFile`` instead, so
        ``apply``/``auth`` validate exactly what the systemd unit will see rather
        than the operator's shell.
        """
        if self.scheduled_env:
            return self.scheduled_env_value(name)
        return os.environ.get(name)

    def scheduled_env_value(self, name: str) -> str | None:
        """Return the value a scheduled systemd service would see for ``name``.

        The authority is ``scheduler.environment_file`` — a service does not
        inherit the operator's process env or ``.env``. Returns None when no
        environment file is configured or the key is absent.
        """
        env_file = self.scheduler.environment_file
        if not env_file:
            return None
        return parse_env_file(Path(env_file).expanduser()).get(name)

    def for_scheduled_preflight(self) -> LoopcraftConfig:
        """Return a copy whose ``env_value``/``which`` resolve against the
        scheduled service environment.

        Used by ``apply``/``auth`` so credential *and* binary probes reflect the
        deployed service's ``EnvironmentFile`` and ``PATH`` rather than the
        interactive shell.
        """
        return self.model_copy(update={"scheduled_env": True})

    @property
    def scheduled_path(self) -> str:
        """PATH a scheduled systemd service will use for binary lookup.

        ``scheduler.path`` when set, else systemd's default service PATH, with
        ``~`` expanded per component. The rendered unit sets exactly this as
        ``Environment=PATH=`` and scheduled preflight resolves runtime/tool
        binaries against it, so ``apply`` validates the same PATH the service
        runs with.
        """
        raw = self.scheduler.path or SYSTEMD_DEFAULT_PATH
        return os.pathsep.join(os.path.expanduser(entry) for entry in raw.split(os.pathsep))

    @property
    def rendered_environment_file(self) -> str | None:
        """The ``EnvironmentFile=`` value to render (``~``-expanded absolute).

        Returns None when no environment file is configured. Rendering the
        expanded path (not the raw string) keeps the unit's ``EnvironmentFile=``
        aligned with what ``apply`` validated.
        """
        env_file = self.scheduler.environment_file
        return str(Path(env_file).expanduser()) if env_file else None

    def which(self, binary: str) -> str | None:
        """Resolve a binary on PATH, honoring scheduled vs direct mode.

        Direct mode uses the operator's PATH; a config marked for scheduled
        preflight (:meth:`for_scheduled_preflight`) resolves against the
        scheduled service PATH (see :attr:`scheduled_path`), so ``apply`` cannot
        pass on a runtime/tool binary the systemd service would not find.
        """
        if self.scheduled_env:
            return shutil.which(binary, path=self.scheduled_path)
        return shutil.which(binary)

    def probe_env(self) -> dict[str, str] | None:
        """Environment for a *live* capability probe subprocess (or None).

        Direct mode returns None (the probe inherits the operator process env).
        Scheduled mode builds a **minimal** environment that mirrors what the
        systemd service will see, rather than inheriting the operator's full
        environment: a small base allowlist (``HOME``/``USER``/locale/...), the
        scheduled ``PATH``, ``LOOPCRAFT_SOURCE``/``LOOPCRAFT_MEMORY``, and the
        scheduled ``EnvironmentFile`` values. This prevents an operator-only
        variable (e.g. ``HTTPS_PROXY``, ``SSL_CERT_FILE``) from making a probe
        pass when the deployed service would not have it.
        """
        if not self.scheduled_env:
            return None
        env = {
            name: os.environ[name]
            for name in _SCHEDULED_ENV_ALLOWLIST
            if name in os.environ
        }
        # Overlay the EnvironmentFile first, then re-assert the loopcraft-managed
        # keys so they win. PATH in particular must equal the validated
        # ``scheduled_path`` (the same value ``which()`` checks and the rendered
        # unit sets), so an EnvironmentFile PATH cannot make the probe execute on
        # a different PATH than binary validation used.
        if self.scheduler.environment_file:
            path = Path(self.scheduler.environment_file).expanduser()
            if path.is_file():
                env.update(parse_env_file(path))
        env["PATH"] = self.scheduled_path
        env["LOOPCRAFT_SOURCE"] = str(self.source_path)
        env["LOOPCRAFT_MEMORY"] = str(self.memory_path)
        return env

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

        scheduler_raw = raw.get("scheduler", {})
        scheduler_dict = dict(scheduler_raw) if isinstance(scheduler_raw, dict) else {}
        # Default the scheduled command to the project's uv-managed loopctl when
        # the operator has not pinned one, so a clean `uv sync` checkout renders
        # units (make check) without hidden local config. Deploy/install stay
        # strict: a real host overrides this with an absolute command or a
        # validated [scheduler].path.
        if "loopctl_bin" not in scheduler_dict:
            venv_loopctl = source.joinpath(*_VENV_LOOPCTL_SUBPATH)
            if venv_loopctl.is_file():
                scheduler_dict["loopctl_bin"] = str(venv_loopctl)
        scheduler = SchedulerConfig.model_validate(scheduler_dict)

        known = {
            "default_vendor",
            "host",
            "memory_path",
            "artifact_store",
            "worktree_keep_last",
            "scheduler",
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
            scheduler=scheduler,
            extra=extra,
        )
