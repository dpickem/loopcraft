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

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$", re.IGNORECASE)


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


class ManifestError(Exception):
    """Raised when a manifest cannot be parsed or fails validation."""


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
        except yaml.YAMLError as exc:  # pragma: no cover - passthrough
            raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
        return cls.from_dict(raw or {}, source_path=path)

    def effective_vendor(self, default_vendor: str) -> str:
        """Return this manifest's vendor, or the global default."""
        return self.runtime.vendor or default_vendor

    def validation_report(self) -> ValidationReport:
        """Validate this manifest and return structured issues."""
        issues: list[ValidationIssue] = []

        if not self.id:
            issues.append(ValidationIssue(scope="id", message="missing required field"))
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


def load_all(loops_dir: Path | str) -> tuple[list[LoopManifest], list[str]]:
    """Load every ``*.yaml`` manifest in a directory.

    Returns ``(manifests, problems)`` where ``problems`` aggregates per-manifest
    validation errors, duplicate ids, unknown upstream loops, and dependency
    cycles across the ``inputs``/``outputs`` + ``depends_on.loops`` graph.
    """
    loops_dir = Path(loops_dir)
    manifests: list[LoopManifest] = []
    issues: list[ValidationIssue] = []

    if not loops_dir.exists():
        return manifests, [f"loops directory not found: {loops_dir}"]

    for path in sorted([*loops_dir.glob("*.yaml"), *loops_dir.glob("*.yml")]):
        try:
            manifest = LoopManifest.load(path)
        except ManifestError as exc:
            issues.append(ValidationIssue(scope=path.name, message=str(exc)))
            continue
        for problem in manifest.validate():
            issues.append(ValidationIssue(scope=path.name, message=problem))
        manifests.append(manifest)

    seen_ids: set[str] = set()
    for manifest in manifests:
        if manifest.id in seen_ids:
            issues.append(ValidationIssue(scope=manifest.id, message="duplicate loop id"))
        seen_ids.add(manifest.id)

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
    return manifests, [issue.render() for issue in issues]


def _detect_cycles(manifests: list[LoopManifest]) -> ValidationReport:
    """Detect cycles in the loop dependency graph.

    An edge ``A -> B`` means B depends on A: B lists A in ``depends_on.loops``,
    or B reads (``inputs``) something A writes (``outputs``).
    """
    producers: dict[str, str] = {}
    for manifest in manifests:
        for output in manifest.outputs:
            producers.setdefault(_norm(output), manifest.id)

    graph = nx.DiGraph()
    graph.add_nodes_from(manifest.id for manifest in manifests)
    for manifest in manifests:
        for upstream in manifest.depends_on.loops:
            if upstream in graph:
                graph.add_edge(upstream, manifest.id)
        for input_path in manifest.inputs:
            producer = producers.get(_norm(input_path))
            if producer and producer != manifest.id:
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
