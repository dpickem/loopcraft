from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .config import (
    STATE_PREFIX,
    SourcePathError,
    StatePathError,
    is_state_path,
    safe_source_relpath,
    safe_state_relpath,
)

VALID_VENDORS = {"codex", "claude", "cursor"}
VALID_LOCI = {"vm", "cloud", "local"}
VALID_TIERS = {"observe", "propose", "act"}
VALID_CADENCE_TYPES = {"cron", "event", "on-artifact"}

#: Supported duration suffixes for ``budget.max_runtime`` mapped to seconds.
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$", re.IGNORECASE)


def parse_duration(value: str | None) -> int | None:
    """Parse a duration like ``10m``/``30s``/``1h`` into whole seconds.

    Returns None when ``value`` is None. Raises on an unparseable string so a
    malformed ``budget.max_runtime`` surfaces at validation, not at 3am.

    Raises:
        ValueError: If ``value`` is non-empty but not a recognized duration.
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


@dataclass(frozen=True)
class Runtime:
    vendor: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class Cadence:
    type: str = "cron"
    at: str | None = None


@dataclass(frozen=True)
class Budget:
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


@dataclass(frozen=True)
class DependsOn:
    apis: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    auth: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    loops: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Content:
    config: str | None = None


@dataclass(frozen=True)
class Logic:
    skill: str | None = None
    verify: str | None = None


@dataclass(frozen=True)
class Approval:
    required: bool = False


@dataclass(frozen=True)
class LoopManifest:
    id: str
    name: str
    description: str
    runtime: Runtime
    locus: str
    cadence: Cadence
    tier: str
    budget: Budget
    depends_on: DependsOn
    content: Content
    inputs: list[str]
    outputs: list[str]
    artifacts: list[dict[str, Any]]
    logic: Logic
    approval: Approval
    source_path: Path | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source_path: Path | None = None) -> "LoopManifest":
        if not isinstance(raw, dict):
            raise ManifestError("manifest root must be a mapping")

        runtime_raw = raw.get("runtime") or {}
        cadence_raw = raw.get("cadence") or {}
        budget_raw = raw.get("budget") or {}
        deps_raw = raw.get("depends_on") or {}
        content_raw = raw.get("content") or {}
        logic_raw = raw.get("logic") or {}
        approval_raw = raw.get("approval") or {}

        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            description=str(raw.get("description", "")),
            runtime=Runtime(
                vendor=_opt_str(runtime_raw.get("vendor")),
                model=_opt_str(runtime_raw.get("model")),
                reasoning_effort=_opt_str(runtime_raw.get("reasoning_effort")),
            ),
            locus=str(raw.get("locus", "vm")),
            cadence=Cadence(
                type=str(cadence_raw.get("type", "cron")),
                at=_opt_str(cadence_raw.get("at")),
            ),
            tier=str(raw.get("tier", "observe")),
            budget=Budget(
                max_turns=_opt_int(budget_raw.get("max_turns")),
                max_tokens=_opt_int(budget_raw.get("max_tokens")),
                max_runtime=_opt_str(budget_raw.get("max_runtime")),
                max_consecutive_failures=_opt_int(budget_raw.get("max_consecutive_failures")),
            ),
            depends_on=DependsOn(
                apis=_str_list(deps_raw.get("apis")),
                tools=_str_list(deps_raw.get("tools")),
                auth=_str_list(deps_raw.get("auth")),
                env=_str_list(deps_raw.get("env")),
                loops=_str_list(deps_raw.get("loops")),
            ),
            content=Content(config=_opt_str(content_raw.get("config"))),
            inputs=_str_list(raw.get("inputs")),
            outputs=_str_list(raw.get("outputs")),
            artifacts=list(raw.get("artifacts") or []),
            logic=Logic(
                skill=_opt_str(logic_raw.get("skill")),
                verify=_opt_str(logic_raw.get("verify")),
            ),
            approval=Approval(required=bool(approval_raw.get("required", False))),
            source_path=source_path,
        )

    @classmethod
    def load(cls, path: Path | str) -> "LoopManifest":
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:  # pragma: no cover - passthrough
            raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
        return cls.from_dict(raw or {}, source_path=path)

    def effective_vendor(self, default_vendor: str) -> str:
        return self.runtime.vendor or default_vendor

    def validate(self) -> list[str]:
        """Return a list of validation problems (empty == valid)."""
        problems: list[str] = []

        if not self.id:
            problems.append("missing required field: id")
        if not self.name:
            problems.append("missing required field: name")
        if self.runtime.vendor and self.runtime.vendor not in VALID_VENDORS:
            problems.append(
                f"runtime.vendor '{self.runtime.vendor}' not in {sorted(VALID_VENDORS)}"
            )
        if self.locus not in VALID_LOCI:
            problems.append(f"locus '{self.locus}' not in {sorted(VALID_LOCI)}")
        if self.tier not in VALID_TIERS:
            problems.append(f"tier '{self.tier}' not in {sorted(VALID_TIERS)}")
        if self.cadence.type not in VALID_CADENCE_TYPES:
            problems.append(
                f"cadence.type '{self.cadence.type}' not in {sorted(VALID_CADENCE_TYPES)}"
            )
        if self.cadence.type == "cron" and not self.cadence.at:
            problems.append("cadence.type 'cron' requires cadence.at")
        if not self.logic.skill:
            problems.append("missing required field: logic.skill")
        else:
            try:
                safe_source_relpath(self.logic.skill)
            except SourcePathError as exc:
                problems.append(f"logic.skill: {exc}")

        if self.content.config:
            try:
                safe_source_relpath(self.content.config)
            except SourcePathError as exc:
                problems.append(f"content.config: {exc}")

        for label, declared in (
            *(("inputs", p) for p in self.inputs),
            *(("outputs", p) for p in self.outputs),
        ):
            rel = declared.strip()
            if PurePosixPath(rel).is_absolute() or rel.startswith("/"):
                problems.append(f"{label}: absolute path is not allowed: {declared!r}")
                continue
            parts = PurePosixPath(rel).parts
            if parts and parts[0] == STATE_PREFIX:
                # The manifest vocabulary is exactly ``state/...``; it resolves into
                # the ledger and must not escape it.
                try:
                    safe_state_relpath(declared)
                except StatePathError as exc:
                    problems.append(f"{label}: {exc}")
            elif is_state_path(declared):
                # A bare ledger-relative path (e.g. ``slack/out.md`` or
                # ``ledger/...``) is a lower-level Store API, not manifest
                # vocabulary. Require the explicit ``state/`` prefix here.
                problems.append(
                    f"{label}: '{declared}' must use the 'state/...' prefix"
                )
            else:
                # M1 only knows how to resolve ledger ``state/...`` paths. External
                # sinks (e.g. ``linear:project/Daily``) need an artifact/sink
                # abstraction that lands in a later milestone; reject until then so
                # validation and run resolution agree.
                problems.append(
                    f"{label}: external sink '{declared}' is not supported in M1 "
                    f"(only ledger 'state/...' paths)"
                )

        if self.budget.max_runtime is not None:
            try:
                self.budget.max_runtime_s
            except ValueError as exc:
                problems.append(f"budget.max_runtime: {exc}")

        return problems


def _opt_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _opt_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def load_all(loops_dir: Path | str) -> tuple[list[LoopManifest], list[str]]:
    """Load every ``*.yaml`` manifest in a directory.

    Returns ``(manifests, problems)`` where ``problems`` aggregates per-manifest
    validation errors, duplicate ids, unknown upstream loops, and dependency
    cycles across the ``inputs``/``outputs`` + ``depends_on.loops`` graph.
    """
    loops_dir = Path(loops_dir)
    manifests: list[LoopManifest] = []
    problems: list[str] = []

    if not loops_dir.exists():
        return manifests, [f"loops directory not found: {loops_dir}"]

    for path in sorted([*loops_dir.glob("*.yaml"), *loops_dir.glob("*.yml")]):
        try:
            manifest = LoopManifest.load(path)
        except ManifestError as exc:
            problems.append(str(exc))
            continue
        for problem in manifest.validate():
            problems.append(f"{path.name}: {problem}")
        manifests.append(manifest)

    seen_ids: set[str] = set()
    for manifest in manifests:
        if manifest.id in seen_ids:
            problems.append(f"duplicate loop id: {manifest.id}")
        seen_ids.add(manifest.id)

    for manifest in manifests:
        for upstream in manifest.depends_on.loops:
            if upstream not in seen_ids:
                problems.append(
                    f"{manifest.id}: depends_on.loops references unknown loop '{upstream}'"
                )

    problems.extend(_detect_cycles(manifests))
    return manifests, problems


def _detect_cycles(manifests: list[LoopManifest]) -> list[str]:
    """Detect cycles in the loop dependency graph.

    An edge ``A -> B`` means B depends on A: B lists A in ``depends_on.loops``,
    or B reads (``inputs``) something A writes (``outputs``).
    """
    producers: dict[str, str] = {}
    for manifest in manifests:
        for output in manifest.outputs:
            producers.setdefault(_norm(output), manifest.id)

    graph: dict[str, set[str]] = {m.id: set() for m in manifests}
    for manifest in manifests:
        for upstream in manifest.depends_on.loops:
            if upstream in graph:
                graph[upstream].add(manifest.id)
        for input_path in manifest.inputs:
            producer = producers.get(_norm(input_path))
            if producer and producer != manifest.id:
                graph[producer].add(manifest.id)

    problems: list[str] = []
    state: dict[str, int] = {}  # 0=unvisited, 1=in-stack, 2=done

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for neighbor in sorted(graph.get(node, ())):
            if state.get(neighbor, 0) == 0:
                visit(neighbor, stack)
            elif state.get(neighbor) == 1:
                cycle = stack[stack.index(neighbor):] + [neighbor]
                problems.append("dependency cycle: " + " -> ".join(cycle))
        stack.pop()
        state[node] = 2

    for manifest in manifests:
        if state.get(manifest.id, 0) == 0:
            visit(manifest.id, [])

    return problems


def _norm(path: str) -> str:
    parts = Path(path.strip()).parts
    if parts and parts[0] in {"state", "ledger"}:
        parts = parts[1:]
    return str(Path(*parts)) if parts else ""
