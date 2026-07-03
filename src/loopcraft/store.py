"""Sanctioned persistence into the memory tree.

Every loop records a common set of run fields through this store, which owns the
``ledger/runs/`` run records that the harvester reindexes from. Loops may also
persist their own custom, loop-specific state (seen sets, queues, digests) under
the ledger through this same store API (or the ledger-backed helpers built on
top of it); the rule is that nothing stands up an out-of-band side database. So
"single sanctioned persistence path" means one storage model — required run
records plus loop-specific ledger files — not a single file.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from loopcraft.config import LoopcraftConfig


class RunRecord(BaseModel):
    """Durable, per-run telemetry written to the ledger at finish.

    This is the authoritative input the harvester (M4) reindexes from, so the
    derived SQLite DB is always reconstructable from the ledger alone.

    Field semantics are the same for every status (success, failure, failed
    preflight): ``inputs`` and ``declared_outputs`` echo the manifest's declared
    I/O contract at run time, while ``outputs`` lists only files the run actually
    produced or refreshed — empty when execution never started, so a failed run
    can never look like a producer to downstream readers.
    """

    run_id: str
    loop: str
    vendor: str
    model: str | None = None
    status: str
    started_at: str
    ended_at: str | None = None
    duration_s: float | None = None
    exit_code: int | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    iterations: int | None = None
    #: The manifest's declared inputs (the contract, not what was consumed).
    inputs: list[str] = Field(default_factory=list)
    #: Files actually produced/refreshed by this run (provenance).
    outputs: list[str] = Field(default_factory=list)
    #: The manifest's declared outputs (the contract, template form).
    declared_outputs: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    log_path: str | None = None
    problems: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain JSON-serializable dict of this record."""
        return self.model_dump()


class Store:
    """The single sanctioned persistence path into the memory tree.

    Loops never stand up their own side databases. Every run writes a common
    :class:`RunRecord` under ``ledger/runs/``; small/seen/queue state and findings
    go to the ledger (markdown/JSONL); produced files go to the artifact store.
    Loop-specific state is allowed, but only through this ledger-backed storage
    model — never an out-of-band database.
    """

    def __init__(self, config: LoopcraftConfig) -> None:
        """Bind the store to a resolved control-plane config."""
        self.config = config

    # --- run lifecycle ------------------------------------------------------
    @staticmethod
    def new_run_id() -> str:
        """Return a fresh sortable run id (``<UTC timestamp>-<short uuid>``)."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"

    def record_run(self, record: RunRecord) -> Path:
        """Write one run record to ``ledger/runs/`` and return its path.

        The write is atomic (temporary file + rename) so history readers never
        observe a partially written record.
        """
        self.config.runs_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.runs_dir / _run_record_filename(record.loop, record.run_id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(record.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def latest_run(self, loop_id: str) -> RunRecord | None:
        """Return the most recent run record for a loop, or None."""
        runs = self.runs_for(loop_id)
        return runs[-1] if runs else None

    def runs_for(self, loop_id: str) -> list[RunRecord]:
        """Return all run records for a loop, oldest first.

        Handles both current (``<loop>__<run>.json``) and legacy run-record
        filenames. The ledger is durable, hand-editable state that outlives
        schema revisions, so any unreadable or schema-invalid file (bad JSON,
        missing required fields, wrong field types) is skipped rather than
        allowed to take down every history reader for the loop.
        """
        if not self.config.runs_dir.exists():
            return []
        records: list[RunRecord] = []
        for path in sorted(self.config.runs_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict) or data.get("loop") != loop_id:
                continue
            fields = {
                k: data[k] for k in _RUN_FIELDS if k in data and data[k] is not None
            }
            try:
                records.append(RunRecord(**fields))
            except ValidationError:
                continue
        records.sort(key=lambda r: r.started_at)
        return records

    # --- ledger I/O ---------------------------------------------------------
    def write_state(self, declared_path: str, content: str) -> Path:
        """Write text to a ledger file declared as ``state/...`` (or ledger-relative)."""
        path = self.config.resolve_state_path(declared_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read_state(self, declared_path: str) -> str | None:
        """Return the text of a ledger file, or None if it does not exist."""
        path = self.config.resolve_state_path(declared_path)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def state_exists(self, declared_path: str) -> bool:
        """Return whether a declared ledger file exists."""
        return self.config.resolve_state_path(declared_path).exists()

    def append_jsonl(self, declared_path: str, record: dict[str, Any]) -> Path:
        """Append one JSON record as a line to a ledger JSONL file."""
        path = self.config.resolve_state_path(declared_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path


_RUN_FIELDS = tuple(RunRecord.model_fields.keys())


def _run_record_filename(loop_id: str, run_id: str) -> str:
    """Return a readable run-record filename that includes the producing loop."""
    safe_loop = re.sub(r"[^0-9A-Za-z_.-]+", "-", loop_id).strip("-") or "loop"
    return f"{safe_loop}__{run_id}.json"
