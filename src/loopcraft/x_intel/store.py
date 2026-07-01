from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


class IntelStore:
    """Ledger-backed X intelligence state.

    High-water marks, seen ids, and raw posts are stored as JSON/JSONL in the
    loopcraft memory tree instead of a per-tool SQLite database.
    """

    def __init__(self, *, seen_path: Path, posts_path: Path, source_state_path: Path) -> None:
        self.seen_path = seen_path
        self.posts_path = posts_path
        self.source_state_path = source_state_path
        for path in (self.seen_path, self.posts_path, self.source_state_path):
            path.parent.mkdir(parents=True, exist_ok=True)

    def latest_seen_id(self, source_key: str) -> str | None:
        state = self._source_state()
        value = state.get(source_key)
        return str(value) if value else None

    def remember_source_highwater(self, source_key: str, posts: list[dict[str, Any]]) -> None:
        ids = [int(post["id"]) for post in posts if str(post.get("id", "")).isdigit()]
        if not ids:
            return
        latest_id = str(max(ids))
        state = self._source_state()
        state[source_key] = latest_id
        self.source_state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def save_posts(self, posts: list[dict[str, Any]]) -> None:
        existing: dict[str, dict[str, Any]] = {}
        if self.posts_path.exists():
            for line in self.posts_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("id"):
                    existing[str(record["id"])] = record
        for post in posts:
            post_id = post.get("id")
            if post_id:
                existing[str(post_id)] = post
        self.posts_path.write_text(
            "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in existing.values()),
            encoding="utf-8",
        )

    def mark_seen(self, post_ids: Iterable[str]) -> None:
        seen = self.seen_ids()
        seen.update(str(post_id) for post_id in post_ids)
        self.seen_path.write_text(
            json.dumps(sorted(seen), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def seen_ids(self) -> set[str]:
        if not self.seen_path.exists():
            return set()
        raw = json.loads(self.seen_path.read_text(encoding="utf-8"))
        return {str(value) for value in raw}

    def _source_state(self) -> dict[str, str]:
        if not self.source_state_path.exists():
            return {}
        raw = json.loads(self.source_state_path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in raw.items()}

