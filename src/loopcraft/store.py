from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import LoopcraftConfig


@dataclass
class RunRecord:
    """Durable, per-run telemetry written to the ledger at finish.

    This is the authoritative input the harvester (M4) reindexes from, so the
    derived SQLite DB is always reconstructable from the ledger alone.
    """

    run_id: str
    loop: str
    vendor: str
    model: str | None
    status: str
    started_at: str
    ended_at: str | None = None
    duration_s: float | None = None
    exit_code: int | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    iterations: int | None = None
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    log_path: str | None = None
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Store:
    """The single sanctioned persistence path into the memory tree.

    Loops never stand up their own side databases. Small/seen/queue state and
    findings go to the ledger (markdown/JSONL); produced files go to the
    artifact store; run metadata goes to per-run records under ``ledger/runs/``.
    """

    def __init__(self, config: LoopcraftConfig) -> None:
        self.config = config

    # --- run lifecycle ------------------------------------------------------
    @staticmethod
    def new_run_id() -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"

    def record_run(self, record: RunRecord) -> Path:
        self.config.runs_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.runs_dir / f"{record.run_id}.json"
        path.write_text(
            json.dumps(record.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def latest_run(self, loop_id: str) -> RunRecord | None:
        runs = self.runs_for(loop_id)
        return runs[-1] if runs else None

    def runs_for(self, loop_id: str) -> list[RunRecord]:
        if not self.config.runs_dir.exists():
            return []
        records: list[RunRecord] = []
        for path in sorted(self.config.runs_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("loop") != loop_id:
                continue
            records.append(RunRecord(**{k: data.get(k) for k in _RUN_FIELDS}))
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
        path = self.config.resolve_state_path(declared_path)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def state_exists(self, declared_path: str) -> bool:
        return self.config.resolve_state_path(declared_path).exists()

    def append_jsonl(self, declared_path: str, record: dict[str, Any]) -> Path:
        path = self.config.resolve_state_path(declared_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path


_RUN_FIELDS = tuple(RunRecord.__dataclass_fields__.keys())
