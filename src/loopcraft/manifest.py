"""Loop manifest models, loading, and validation.

Loop manifests define runtime selection, cadence, dependency declarations, and
the ledger I/O contract. The module uses Pydantic models for typed structure and
returns structured validation reports so the CLI and future UI can render issues
without parsing ad hoc strings.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

import networkx as nx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from loopcraft.config import (
    STATE_PREFIX,
    MemoryDir,
    is_state_path,
    safe_source_relpath,
    safe_state_relpath,
)
from loopcraft.paths import assert_under

#: Seconds per supported duration unit in ``budget.max_runtime`` strings.
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}
#: Accepted duration shape: an integer followed by one of s/m/h.
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$", re.IGNORECASE)

#: Canonical loop-id vocabulary: lowercase alphanumeric components separated by
#: single hyphens (e.g. ``slack-triage``). A loop id names its manifest file and
#: its per-loop worktree directory, so it must be exactly one safe path segment —
#: never an absolute path, ``..`` traversal, or anything with separators.
LOOP_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ManifestError(Exception):
    """Raised when a manifest cannot be parsed or fails validation."""


def loop_id_problem(loop_id: str) -> str | None:
    """Return why ``loop_id`` is not a canonical loop id, or None when it is.

    Args:
        loop_id: Candidate id from a manifest or the CLI loop selector.

    Returns:
        A problem message, or None when the id matches :data:`LOOP_ID_RE`.
    """
    if not loop_id:
        return "missing required field"
    if not LOOP_ID_RE.fullmatch(loop_id):
        return (
            "must be lowercase alphanumeric components separated by single "
            f"hyphens (e.g. 'slack-triage'): {loop_id!r}"
        )
    return None


class Vendor(StrEnum):
    """Runtime adapter vendors supported by manifests."""

    CODEX = "codex"
    CLAUDE = "claude"
    CURSOR = "cursor"


class Locus(StrEnum):
    """Execution locations supported by manifests."""

    VM = "vm"
    CLOUD = "cloud"
    LOCAL = "local"


class Tier(StrEnum):
    """Loop autonomy levels."""

    OBSERVE = "observe"
    PROPOSE = "propose"
    ACT = "act"


class CadenceType(StrEnum):
    """Trigger styles supported by manifests."""

    CRON = "cron"
    EVENT = "event"
    ON_ARTIFACT = "on-artifact"


def parse_duration(value: str | None) -> int | None:
    """Parse a duration like ``10m``/``30s``/``1h`` into seconds.

    Args:
        value: Duration string or None.

    Returns:
        Whole seconds, or None when unset.

    Raises:
        ValueError: If the string is not in ``Ns`` / ``Nm`` / ``Nh`` form.
    """
    if value is None:
        return None
    match = _DURATION_RE.match(str(value))
    if not match:
        raise ValueError(f"invalid duration: {value!r} (use forms like 30s, 10m, 1h)")
    amount, unit = match.groups()
    return int(amount) * _DURATION_UNITS[unit.lower()]


class _ManifestModel(BaseModel):
    """Base Pydantic model for manifest structures."""

    model_config = ConfigDict(extra="forbid")


class Runtime(_ManifestModel):
    """Runtime adapter selection."""

    vendor: Vendor | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class Cadence(_ManifestModel):
    """Loop trigger configuration."""

    type: CadenceType = CadenceType.CRON
    at: str | None = None


class Budget(_ManifestModel):
    """Execution budget limits."""

    max_turns: int | None = None
    max_tokens: int | None = None
    max_runtime: str | None = None
    max_consecutive_failures: int | None = None

    @property
    def max_runtime_s(self) -> int | None:
        """``max_runtime`` parsed to seconds, or None when unset.

        Raises:
            ValueError: If ``max_runtime`` is set but unparseable.
        """
        return parse_duration(self.max_runtime)


class DependsOn(_ManifestModel):
    """External and upstream dependencies declared by a loop."""

    apis: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    auth: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    loops: list[str] = Field(default_factory=list)


class Content(_ManifestModel):
    """Content definition references consumed by loop logic."""

    config: str | None = None


class Logic(_ManifestModel):
    """Skill and verification references.

    Both ``skill`` and ``verify`` are source-relative paths to markdown files
    (the ``verify`` file is colocated with the skill and spells out the loop's
    goal/stop conditions). Keeping ``verify`` in a dedicated file lets it define
    completion criteria far more thoroughly than a one-line manifest string.
    """

    skill: str | None = None
    verify: str | None = None


class Approval(_ManifestModel):
    """Human approval policy."""

    required: bool = False


class ValidationIssue(_ManifestModel):
    """One manifest validation problem."""

    scope: str
    message: str
    path: str | None = None

    def render(self) -> str:
        """Render this issue as a concise text message."""
        prefix = f"{self.scope}: " if self.scope else ""
        return f"{prefix}{self.message}"


class ValidationReport(_ManifestModel):
    """Structured validation result."""

    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the validation report has no issues."""
        return not self.issues

    def messages(self) -> list[str]:
        """Return issues as legacy human-readable messages."""
        return [issue.render() for issue in self.issues]


class LoopManifest(_ManifestModel):
    """One loop manifest."""

    id: str
    name: str
    description: str = ""
    runtime: Runtime = Field(default_factory=Runtime)
    locus: Locus = Locus.VM
    cadence: Cadence = Field(default_factory=Cadence)
    tier: Tier = Tier.OBSERVE
    budget: Budget = Field(default_factory=Budget)
    depends_on: DependsOn = Field(default_factory=DependsOn)
    content: Content = Field(default_factory=Content)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    logic: Logic = Field(default_factory=Logic)
    approval: Approval = Field(default_factory=Approval)
    source_path: Path | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source_path: Path | None = None) -> LoopManifest:
        """Parse a manifest from a raw mapping.

        Args:
            raw: Parsed YAML mapping.
            source_path: Optional source file path.

        Returns:
            Parsed manifest model.
        """
        if not isinstance(raw, dict):
            raise ManifestError("manifest root must be a mapping")
        payload = dict(raw)
        payload["source_path"] = source_path
        try:
            return cls.model_validate(payload)
        except Exception as exc:
            raise ManifestError(str(exc)) from exc

    @classmethod
    def load(cls, path: Path | str) -> LoopManifest:
        """Load one YAML manifest from disk."""
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
        return cls.from_dict(raw or {}, source_path=path)

    def effective_vendor(self, default_vendor: str) -> str:
        """Return this manifest's vendor, or the global default."""
        return self.runtime.vendor or default_vendor

    def validation_report(self) -> ValidationReport:
        """Validate this manifest and return structured issues."""
        issues: list[ValidationIssue] = []

        id_problem = loop_id_problem(self.id)
        if id_problem:
            issues.append(ValidationIssue(scope="id", message=id_problem))
        if not self.name:
            issues.append(ValidationIssue(scope="name", message="missing required field"))
        if self.cadence.type == CadenceType.CRON and not self.cadence.at:
            issues.append(ValidationIssue(scope="cadence", message="cron requires cadence.at"))
        if not self.logic.skill:
            issues.append(ValidationIssue(scope="logic.skill", message="missing required field"))
        else:
            try:
                safe_source_relpath(self.logic.skill)
            except Exception as exc:
                issues.append(ValidationIssue(scope="logic.skill", message=str(exc)))

        if self.content.config:
            try:
                safe_source_relpath(self.content.config)
            except Exception as exc:
                issues.append(ValidationIssue(scope="content.config", message=str(exc)))

        if self.logic.verify:
            try:
                safe_source_relpath(self.logic.verify)
            except Exception as exc:
                issues.append(ValidationIssue(scope="logic.verify", message=str(exc)))

        for label, declared in (
            *(("inputs", p) for p in self.inputs),
            *(("outputs", p) for p in self.outputs),
        ):
            rel = declared.strip()
            if PurePosixPath(rel).is_absolute() or rel.startswith("/"):
                issues.append(
                    ValidationIssue(scope=label, message=f"absolute path is not allowed: {declared!r}")
                )
                continue
            parts = PurePosixPath(rel).parts
            if parts and parts[0] == STATE_PREFIX:
                # The manifest vocabulary is exactly ``state/...``; it resolves into
                # the ledger and must not escape it.
                try:
                    safe_state_relpath(declared)
                except Exception as exc:
                    issues.append(ValidationIssue(scope=label, message=str(exc)))
            elif is_state_path(declared):
                # A bare ledger-relative path (e.g. ``slack/out.md`` or
                # ``ledger/...``) is a lower-level Store API, not manifest
                # vocabulary. Require the explicit ``state/`` prefix here.
                issues.append(
                    ValidationIssue(scope=label, message=f"'{declared}' must use the 'state/...' prefix")
                )
            else:
                # M1 only knows how to resolve ledger ``state/...`` paths. External
                # sinks (e.g. ``linear:project/Daily``) need an artifact/sink
                # abstraction that lands in a later milestone; reject until then so
                # validation and run resolution agree.
                issues.append(
                    ValidationIssue(
                        scope=label,
                        message=f"external sink '{declared}' is not supported in M1 (only ledger 'state/...' paths)",
                    )
                )

        if self.budget.max_runtime is not None:
            try:
                self.budget.max_runtime_s
            except ValueError as exc:
                issues.append(ValidationIssue(scope="budget.max_runtime", message=str(exc)))

        return ValidationReport(issues=issues)

    def validate(self) -> list[str]:
        """Return validation problems as human-readable messages."""
        return self.validation_report().messages()


class ManifestEntry(_ManifestModel):
    """One loaded manifest paired with its own validation issues."""

    manifest: LoopManifest
    issues: list[ValidationIssue] = Field(default_factory=list)


class ManifestCatalog(_ManifestModel):
    """Structured result of loading a loops directory.

    Issues stay attached to the manifest they belong to (``entries``); problems
    that have no single loadable manifest — unreadable or escaping files,
    duplicate ids, output collisions, unknown upstream loops, dependency
    cycles — live in ``fleet_issues``.
    """

    entries: list[ManifestEntry] = Field(default_factory=list)
    fleet_issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def manifests(self) -> list[LoopManifest]:
        """Every successfully loaded manifest, in filename order."""
        return [entry.manifest for entry in self.entries]

    @property
    def problems(self) -> list[str]:
        """All issues rendered as messages, per-manifest ones prefixed by loop id."""
        rendered = [
            f"{entry.manifest.id}: {issue.render()}"
            for entry in self.entries
            for issue in entry.issues
        ]
        return rendered + [issue.render() for issue in self.fleet_issues]

    @property
    def ok(self) -> bool:
        """Whether no manifest or fleet issue was found."""
        return not self.problems


def find_manifest(loops_dir: Path | str, loop_id: str) -> LoopManifest | None:
    """Return the manifest for ``loop_id`` from the loops directory, if present.

    The loop selector is an id, not a file path: it must match the canonical
    loop-id vocabulary before any path is constructed, the resolved manifest must
    stay directly under ``loops_dir``, and its ``id`` field must equal the
    filename stem it was looked up by.

    Raises:
        ManifestError: If the selector is not a canonical loop id, the manifest
            cannot be parsed/validated, or its id differs from the filename stem.
    """
    loops_dir = Path(loops_dir)
    problem = loop_id_problem(loop_id)
    if problem is not None:
        raise ManifestError(f"invalid loop id {loop_id!r}: {problem}")
    for ext in (".yaml", ".yml"):
        path = loops_dir / f"{loop_id}{ext}"
        try:
            assert_under(loops_dir, path, label="loop manifest path")
        except ValueError as exc:
            raise ManifestError(str(exc)) from exc
        if path.exists():
            manifest = LoopManifest.load(path)
            if manifest.id != loop_id:
                raise ManifestError(
                    f"{path.name}: manifest id {manifest.id!r} does not match filename stem {loop_id!r}"
                )
            return manifest
    return None


def load_all(loops_dir: Path | str) -> ManifestCatalog:
    """Load every ``*.yaml`` manifest in a directory into a structured catalog.

    Per-manifest issues (schema/custom validation, filename/id mismatch) are
    attached to their :class:`ManifestEntry`; unreadable/escaping files and
    cross-manifest problems (duplicate ids, duplicate/multi-producer outputs,
    unknown upstream loops, and dependency cycles across the
    ``inputs``/``outputs`` + ``depends_on.loops`` graph) are ``fleet_issues``.
    """
    loops_dir = Path(loops_dir)
    entries: list[ManifestEntry] = []
    issues: list[ValidationIssue] = []

    if not loops_dir.exists():
        return ManifestCatalog(
            fleet_issues=[
                ValidationIssue(scope="", message=f"loops directory not found: {loops_dir}")
            ]
        )

    for path in sorted([*loops_dir.glob("*.yaml"), *loops_dir.glob("*.yml")]):
        try:
            # Same containment rule as the single-manifest lookup: a symlinked
            # manifest entry must not resolve outside the loops directory.
            assert_under(loops_dir, path, label="loop manifest path")
        except ValueError as exc:
            issues.append(ValidationIssue(scope=path.name, message=str(exc)))
            continue
        try:
            manifest = LoopManifest.load(path)
        except ManifestError as exc:
            issues.append(ValidationIssue(scope=path.name, message=str(exc)))
            continue
        entry_issues = list(manifest.validation_report().issues)
        if manifest.id != path.stem:
            # The id doubles as the lookup key for `loopctl run <id>`, which
            # resolves to the filename stem; a mismatch would advertise one id
            # while running another.
            entry_issues.append(
                ValidationIssue(
                    scope=path.name,
                    message=f"manifest id {manifest.id!r} does not match filename stem {path.stem!r}",
                )
            )
        entries.append(ManifestEntry(manifest=manifest, issues=entry_issues))

    manifests = [entry.manifest for entry in entries]
    seen_ids: set[str] = set()
    for manifest in manifests:
        if manifest.id in seen_ids:
            issues.append(ValidationIssue(scope=manifest.id, message="duplicate loop id"))
        seen_ids.add(manifest.id)

    # One producing loop per normalized output path: with several producers,
    # dependency inference becomes order-dependent and the loops race on the
    # same durable ledger file. Duplicates within one manifest are flagged too.
    producers: dict[str, list[str]] = {}
    for manifest in manifests:
        declared_norms: set[str] = set()
        for output in manifest.outputs:
            norm = _norm(output)
            if norm in declared_norms:
                issues.append(
                    ValidationIssue(
                        scope=manifest.id,
                        message=f"output declared more than once in this manifest: '{output}'",
                    )
                )
                continue
            declared_norms.add(norm)
            producers.setdefault(norm, []).append(manifest.id)
    for norm, producing_loops in sorted(producers.items()):
        if len(producing_loops) > 1:
            issues.append(
                ValidationIssue(
                    scope="outputs",
                    message=(
                        f"output '{norm}' is declared by multiple loops: "
                        + ", ".join(producing_loops)
                    ),
                )
            )

    for manifest in manifests:
        for upstream in manifest.depends_on.loops:
            if upstream not in seen_ids:
                issues.append(
                    ValidationIssue(
                        scope=manifest.id,
                        message=f"depends_on.loops references unknown loop '{upstream}'",
                    )
                )

    issues.extend(_detect_cycles(manifests).issues)
    return ManifestCatalog(entries=entries, fleet_issues=issues)


def _detect_cycles(manifests: list[LoopManifest]) -> ValidationReport:
    """Detect cycles in the loop dependency graph.

    An edge ``A -> B`` means B depends on A: B lists A in ``depends_on.loops``,
    or B reads (``inputs``) something A writes (``outputs``). Every producer of
    a path contributes edges — fleet validation separately rejects
    multi-producer outputs, so cycle detection must not silently pick the first
    producer and miss a cycle through another.
    """
    producers: dict[str, list[str]] = {}
    for manifest in manifests:
        for output in manifest.outputs:
            producers.setdefault(_norm(output), []).append(manifest.id)

    graph = nx.DiGraph()
    graph.add_nodes_from(manifest.id for manifest in manifests)
    for manifest in manifests:
        for upstream in manifest.depends_on.loops:
            if upstream in graph:
                graph.add_edge(upstream, manifest.id)
        for input_path in manifest.inputs:
            for producer in producers.get(_norm(input_path), []):
                if producer != manifest.id:
                    graph.add_edge(producer, manifest.id)

    issues = [
        ValidationIssue(scope="dependency_graph", message="dependency cycle: " + " -> ".join(cycle + [cycle[0]]))
        for cycle in nx.simple_cycles(graph)
    ]
    return ValidationReport(issues=issues)


def _norm(path: str) -> str:
    """Normalize a declared path for producer/consumer graph matching."""
    parts = Path(path.strip()).parts
    if parts and parts[0] in {STATE_PREFIX, MemoryDir.LEDGER.value}:
        parts = parts[1:]
    return str(Path(*parts)) if parts else ""
