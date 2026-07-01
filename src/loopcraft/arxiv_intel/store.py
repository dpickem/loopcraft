from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


class ArxivStore:
    """Ledger-backed arXiv state (seen ids + raw paper records).

    Small durable state is kept as JSON/JSONL in the loopcraft memory tree,
    rather than in a per-tool SQLite database.
    """

    def __init__(self, *, seen_path: Path, papers_path: Path) -> None:
        self.seen_path = seen_path
        self.papers_path = papers_path
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.papers_path.parent.mkdir(parents=True, exist_ok=True)

    def save_papers(self, papers: list[dict[str, Any]]) -> None:
        existing: dict[str, dict[str, Any]] = {}
        if self.papers_path.exists():
            for line in self.papers_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("id"):
                    existing[str(record["id"])] = record
        for paper in papers:
            paper_id = paper.get("id")
            if paper_id:
                existing[str(paper_id)] = paper
        self.papers_path.write_text(
            "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in existing.values()),
            encoding="utf-8",
        )

    def mark_seen(self, paper_ids: Iterable[str]) -> None:
        seen = self.seen_ids()
        seen.update(str(paper_id) for paper_id in paper_ids)
        self.seen_path.write_text(
            json.dumps(sorted(seen), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def seen_ids(self) -> set[str]:
        if not self.seen_path.exists():
            return set()
        raw = json.loads(self.seen_path.read_text(encoding="utf-8"))
        return {str(value) for value in raw}

