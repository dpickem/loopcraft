"""Ledger-backed state and digest storage for the arXiv intelligence loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from loopcraft.config import LoopcraftConfig, is_state_path
from loopcraft.jsonl import read_jsonl, write_jsonl
from loopcraft.research_intel.arxiv.config import OutputPaths


class ArxivStore:
    """Ledger-backed arXiv state (seen ids + raw paper records).

    Small durable state is kept as JSON/JSONL in the loopcraft memory tree,
    rather than in a per-tool SQLite database.
    """

    def __init__(self, loopcraft: LoopcraftConfig, output: OutputPaths) -> None:
        self.loopcraft = loopcraft
        self.output = output
        self.seen_path = self.resolve(output.seen_path)
        self.papers_path = self.resolve(output.papers_path)
        for path in (self.seen_path, self.papers_path):
            path.parent.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: Path) -> Path:
        """Resolve a configured path through the memory ledger when needed."""
        raw = path.as_posix()
        if is_state_path(raw):
            return self.loopcraft.resolve_state_path(raw)
        return path

    def write_digest(self, *, markdown: str, payload: str, run_stamp: str, date_stamp: str) -> tuple[Path, Path]:
        """Write dated, history, and latest arXiv digest files.

        Args:
            markdown: Rendered digest markdown.
            payload: Rendered digest JSON string.
            run_stamp: Timestamp used for immutable history filenames.
            date_stamp: Date used for daily digest filenames.

        Returns:
            The daily markdown and JSON paths printed by the direct CLI.
        """
        digest_dir = self.resolve(self.output.digest_dir)
        history_dir = self.resolve(self.output.history_dir)
        latest_markdown = self.resolve(self.output.latest_markdown)
        latest_json = self.resolve(self.output.latest_json)
        for path in (digest_dir, history_dir, latest_markdown.parent, latest_json.parent):
            path.mkdir(parents=True, exist_ok=True)
        markdown_path = digest_dir / f"{date_stamp}.md"
        json_path = digest_dir / f"{date_stamp}.json"
        writes = {
            markdown_path: markdown,
            json_path: payload,
            history_dir / f"{run_stamp}.md": markdown,
            history_dir / f"{run_stamp}.json": payload,
            latest_markdown: markdown,
            latest_json: payload,
        }
        for path, text in writes.items():
            path.write_text(text, encoding="utf-8")
        return markdown_path, json_path

    def save_papers(self, papers: list[dict[str, Any]]) -> None:
        existing: dict[str, dict[str, Any]] = {}
        for record in read_jsonl(self.papers_path):
            if record.get("id"):
                existing[str(record["id"])] = record
        for paper in papers:
            paper_id = paper.get("id")
            if paper_id:
                existing[str(paper_id)] = paper
        write_jsonl(self.papers_path, existing.values())

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

