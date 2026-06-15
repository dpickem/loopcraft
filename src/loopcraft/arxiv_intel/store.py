from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


class ArxivStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def save_papers(self, papers: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            for paper in papers:
                paper_id = paper.get("id")
                if not paper_id:
                    continue
                conn.execute(
                    """
                    INSERT INTO papers(id, title, published, updated, abstract, raw_json)
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      title = excluded.title,
                      published = excluded.published,
                      updated = excluded.updated,
                      abstract = excluded.abstract,
                      raw_json = excluded.raw_json
                    """,
                    (
                        str(paper_id),
                        paper.get("title"),
                        paper.get("published"),
                        paper.get("updated"),
                        paper.get("abstract"),
                        json.dumps(paper, ensure_ascii=False),
                    ),
                )

    def mark_seen(self, paper_ids: Iterable[str]) -> None:
        with self._connect() as conn:
            for paper_id in paper_ids:
                conn.execute("INSERT OR IGNORE INTO seen_papers(id) VALUES(?)", (str(paper_id),))

    def seen_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM seen_papers").fetchall()
        return {str(row[0]) for row in rows}

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS papers (
                  id TEXT PRIMARY KEY,
                  title TEXT,
                  published TEXT,
                  updated TEXT,
                  abstract TEXT,
                  raw_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seen_papers (
                  id TEXT PRIMARY KEY,
                  seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

