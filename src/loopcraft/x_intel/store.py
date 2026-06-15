from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


class IntelStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def latest_seen_id(self, source_key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT latest_id FROM source_state WHERE source_key = ?", (source_key,)).fetchone()
        return str(row[0]) if row and row[0] else None

    def remember_source_highwater(self, source_key: str, posts: list[dict[str, Any]]) -> None:
        ids = [int(post["id"]) for post in posts if str(post.get("id", "")).isdigit()]
        if not ids:
            return
        latest_id = str(max(ids))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO source_state(source_key, latest_id)
                VALUES(?, ?)
                ON CONFLICT(source_key) DO UPDATE SET latest_id = excluded.latest_id
                """,
                (source_key, latest_id),
            )

    def save_posts(self, posts: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            for post in posts:
                post_id = post.get("id")
                if not post_id:
                    continue
                author = post.get("author") or {}
                conn.execute(
                    """
                    INSERT INTO posts(id, author_id, author_username, created_at, text, raw_json)
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      author_id = excluded.author_id,
                      author_username = excluded.author_username,
                      created_at = excluded.created_at,
                      text = excluded.text,
                      raw_json = excluded.raw_json
                    """,
                    (
                        str(post_id),
                        post.get("author_id"),
                        author.get("username"),
                        post.get("created_at"),
                        post.get("text"),
                        json.dumps(post, ensure_ascii=False),
                    ),
                )

    def mark_seen(self, post_ids: Iterable[str]) -> None:
        with self._connect() as conn:
            for post_id in post_ids:
                conn.execute("INSERT OR IGNORE INTO seen_posts(id) VALUES(?)", (str(post_id),))

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS posts (
                  id TEXT PRIMARY KEY,
                  author_id TEXT,
                  author_username TEXT,
                  created_at TEXT,
                  text TEXT,
                  raw_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seen_posts (
                  id TEXT PRIMARY KEY,
                  seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS source_state (
                  source_key TEXT PRIMARY KEY,
                  latest_id TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

