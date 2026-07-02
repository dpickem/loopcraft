"""Ledger-backed state and digest storage for the X intelligence loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from loopcraft.config import LoopcraftConfig, is_state_path
from loopcraft.jsonl import read_jsonl, write_jsonl
from loopcraft.research_intel.x.config import OutputPaths


class IntelStore:
    """Ledger-backed X intelligence state.

    High-water marks, seen ids, and raw posts are stored as JSON/JSONL in the
    loopcraft memory tree instead of a per-tool SQLite database.
    """

    def __init__(self, loopcraft: LoopcraftConfig, output: OutputPaths) -> None:
        """Resolve ledger paths from config and ensure their parents exist.

        Args:
            loopcraft: Control-plane config used to resolve ``state/`` paths.
            output: Configured X output paths (seen/posts/source-state/digests).
        """
        self.loopcraft = loopcraft
        self.output = output
        self.seen_path = self.resolve(output.seen_path)
        self.posts_path = self.resolve(output.posts_path)
        self.source_state_path = self.resolve(output.source_state_path)
        for path in (self.seen_path, self.posts_path, self.source_state_path):
            path.parent.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: Path) -> Path:
        """Resolve a configured path through the memory ledger when needed."""
        raw = path.as_posix()
        if is_state_path(raw):
            return self.loopcraft.resolve_state_path(raw)
        return path

    def write_digest(self, *, markdown: str, payload: str, run_stamp: str, date_stamp: str) -> tuple[Path, Path]:
        """Write dated, history, and latest X digest files.

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

    def follow_candidates_dir(self, output_dir: str | None) -> Path:
        """Return the resolved follow-candidate output directory."""
        if output_dir:
            return self.resolve(Path(output_dir))
        return self.resolve(self.output.follow_candidates_dir)

    def latest_digest_json(self) -> Path:
        """Return the newest digest JSON file in the resolved digest directory.

        Raises:
            FileNotFoundError: If no digest JSON files exist yet.
        """
        digest_dir = self.resolve(self.output.digest_dir)
        candidates = sorted(digest_dir.glob("*.json"))
        if not candidates:
            raise FileNotFoundError(f"no digest JSON files under {digest_dir}")
        return candidates[-1]

    def write_follow_candidates(
        self, *, markdown: str, payload: str, date_stamp: str, output_dir: str | None
    ) -> tuple[Path, Path]:
        """Write dated follow-candidate markdown/JSON and return their paths."""
        out_dir = self.follow_candidates_dir(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = out_dir / f"{date_stamp}.md"
        json_path = out_dir / f"{date_stamp}.json"
        markdown_path.write_text(markdown, encoding="utf-8")
        json_path.write_text(payload, encoding="utf-8")
        return markdown_path, json_path

    def latest_seen_id(self, source_key: str) -> str | None:
        """Return the stored high-water post id for a source key, if any."""
        state = self._source_state()
        value = state.get(source_key)
        return str(value) if value else None

    def remember_source_highwater(self, source_key: str, posts: list[dict[str, Any]]) -> None:
        """Persist the max post id seen for a source key as its high-water mark."""
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
        """Merge ``posts`` into the JSONL post store, deduped by id."""
        existing: dict[str, dict[str, Any]] = {}
        for record in read_jsonl(self.posts_path):
            if record.get("id"):
                existing[str(record["id"])] = record
        for post in posts:
            post_id = post.get("id")
            if post_id:
                existing[str(post_id)] = post
        write_jsonl(self.posts_path, existing.values())

    def mark_seen(self, post_ids: Iterable[str]) -> None:
        """Add ``post_ids`` to the persisted seen-id set."""
        seen = self.seen_ids()
        seen.update(str(post_id) for post_id in post_ids)
        self.seen_path.write_text(
            json.dumps(sorted(seen), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def seen_ids(self) -> set[str]:
        """Return the set of post ids already seen in prior runs."""
        if not self.seen_path.exists():
            return set()
        raw = json.loads(self.seen_path.read_text(encoding="utf-8"))
        return {str(value) for value in raw}

    def _source_state(self) -> dict[str, str]:
        """Return the per-source high-water mark map from the ledger."""
        if not self.source_state_path.exists():
            return {}
        raw = json.loads(self.source_state_path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in raw.items()}

