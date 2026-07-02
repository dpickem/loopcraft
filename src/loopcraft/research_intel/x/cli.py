"""Command-line entry point for the X intelligence loop.

The fetch/rank/write and follow-discovery commands share one
:class:`XIntelRunner` so the loopcraft config, content config, X API token, and
store are resolved exactly once per invocation instead of per function.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loopcraft.cli_output import emit
from loopcraft.config import RUN_ID_ENV, LoopcraftConfig
from loopcraft.env import load_dotenv
from loopcraft.research_intel.x.client import XApiClient, XApiError
from loopcraft.research_intel.x.config import IntelConfig, XApiTokens
from loopcraft.research_intel.x.digest import render_digest
from loopcraft.research_intel.x.follow_discovery import (
    discover_candidates,
    followed_handles_from_snapshot,
    render_follow_candidates,
)
from loopcraft.research_intel.x.ranking import rank_posts
from loopcraft.research_intel.x.store import IntelStore

#: Max author handles per combined ``from:`` search (X API query-length limit).
_AUTHOR_BATCH_SIZE = 10

#: Default content-config path for the X intelligence loop.
_DEFAULT_CONFIG = "config/x_intel.yaml"

#: Default private (gitignored) following-snapshot output path.
_DEFAULT_SNAPSHOT_OUTPUT = "config/x_following_snapshot.local.json"


class MissingTokenError(RuntimeError):
    """Raised when no X API token is available for a requested operation."""


def _fail(command: str, rc: int, message: str, *, as_json: bool) -> int:
    """Emit a failure result: a JSON error envelope, or a stderr message."""
    if as_json:
        return emit(command, as_json=True, ok=False, rc=rc, data={"error": message}, lines=[])
    print(f"ERROR: {message}", file=sys.stderr)
    return rc


class XIntelRunner:
    """Coherent X intelligence runner.

    Loads the loopcraft config, X content config, API tokens, and ledger store
    once at construction so command methods never re-resolve them.
    """

    def __init__(self, config_path: str) -> None:
        """Resolve dotenv, loopcraft config, content config, tokens, and store."""
        load_dotenv()
        self.loopcraft = LoopcraftConfig.load()
        self.config = IntelConfig.load(Path(config_path))
        self.tokens = XApiTokens.from_env()
        self.store = IntelStore(self.loopcraft, self.config.output)

    def _client(self, *, require_user_context: bool = False) -> XApiClient:
        """Return an X API client, raising if no suitable token is set.

        Raises:
            MissingTokenError: If the required token is not configured.
        """
        token = self.tokens.token(require_user_context=require_user_context)
        if not token:
            raise MissingTokenError(
                "X_API_OAUTH2_ACCESS_TOKEN is required for user-context access"
                if require_user_context
                else "X_API_BEARER_TOKEN or X_API_OAUTH2_ACCESS_TOKEN is required"
            )
        return XApiClient(token)

    def run(self, *, dry_run: bool = False, as_json: bool = False) -> int:
        """Fetch, rank, and write a daily X digest; return a process exit code."""
        try:
            client = self._client()
        except MissingTokenError as exc:
            return _fail("run", 2, str(exc), as_json=as_json)

        raw_posts, errors = self._collect_raw_posts(client, dry_run=dry_run)

        ranked = rank_posts(raw_posts, self.config)
        top_posts = ranked[: self.config.ranking.top_posts]

        now = datetime.now(UTC)
        if not dry_run:
            self.store.save_posts(raw_posts)
            self.store.mark_seen([post["id"] for post in raw_posts if post.get("id")])
            # Always (re)write source-state.json so this declared output exists and
            # is refreshed even when no source produced a new high-water mark.
            self.store.persist_source_state()

        markdown = render_digest(top_posts, raw_posts, errors, generated_at=now)
        payload = json.dumps(
            {
                "generated_at": now.isoformat(),
                "post_count": len(raw_posts),
                "top_posts": top_posts,
                "errors": errors,
            },
            indent=2,
            ensure_ascii=False,
        )
        run_stamp = self.loopcraft.env_value(RUN_ID_ENV) or now.strftime("%Y%m%dT%H%M%SZ")
        markdown_path, json_path = self.store.write_digest(
            markdown=markdown,
            payload=payload,
            run_stamp=run_stamp,
            date_stamp=now.strftime("%Y-%m-%d"),
        )

        ok = not errors
        data = {
            "markdown_path": str(markdown_path),
            "json_path": str(json_path),
            "post_count": len(raw_posts),
            "errors": errors,
        }
        lines = [f"DIGEST_MARKDOWN={markdown_path}", f"DIGEST_JSON={json_path}"]
        rc = emit("run", as_json=as_json, ok=ok, rc=0 if ok else 1, data=data, lines=lines)
        if not as_json and errors:
            print("ERROR_SUMMARY=" + " | ".join(errors), file=sys.stderr)
        return rc

    def discover_follows(
        self, *, digest_json: str | None, output_dir: str | None, top: int, as_json: bool = False
    ) -> int:
        """Recommend new accounts to follow from the latest digest."""
        try:
            client = self._client()
        except MissingTokenError as exc:
            return _fail("discover-follows", 2, str(exc), as_json=as_json)

        digest_path = Path(digest_json) if digest_json else self.store.latest_digest_json()
        digest = json.loads(digest_path.read_text(encoding="utf-8"))
        followed = followed_handles_from_snapshot(self.config.sources.following_snapshot)
        initial = discover_candidates(digest, self.config, followed_handles=followed, top_n=top * 3)

        try:
            profiles = client.users_by_usernames([c["username"] for c in initial])
        except XApiError as exc:
            return _fail("discover-follows", 1, str(exc), as_json=as_json)

        candidates = discover_candidates(
            digest,
            self.config,
            followed_handles=followed,
            hydrated_profiles=profiles,
            top_n=top,
        )
        generated_at = datetime.now(UTC)
        markdown = render_follow_candidates(
            candidates, generated_at=generated_at, digest_path=digest_path
        )
        payload = json.dumps(
            {
                "generated_at": generated_at.isoformat(),
                "digest_json": str(digest_path),
                "candidate_count": len(candidates),
                "candidates": candidates,
            },
            indent=2,
            ensure_ascii=False,
        )
        markdown_path, json_path = self.store.write_follow_candidates(
            markdown=markdown,
            payload=payload,
            date_stamp=generated_at.strftime("%Y-%m-%d"),
            output_dir=output_dir,
        )
        data = {
            "markdown_path": str(markdown_path),
            "json_path": str(json_path),
            "candidate_count": len(candidates),
        }
        lines = [
            f"FOLLOW_CANDIDATES_MARKDOWN={markdown_path}",
            f"FOLLOW_CANDIDATES_JSON={json_path}",
        ]
        return emit("discover-follows", as_json=as_json, ok=True, rc=0, data=data, lines=lines)

    # --- fetch phases -------------------------------------------------------
    def _collect_raw_posts(
        self, client: XApiClient, *, dry_run: bool
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Run every fetch phase, isolating failures so sources are independent.

        Each phase (queries, lists, following, author handles, snapshot) is
        wrapped individually: an ``XApiError`` in one source (e.g. a rate-limited
        search endpoint) is recorded but does not skip the remaining sources, and
        every error is accumulated rather than only the first.

        Args:
            client: The X API client.
            dry_run: Whether to skip persisting per-source high-water marks.

        Returns:
            A tuple of (accumulated posts, per-source error messages).
        """
        raw_posts: list[dict[str, Any]] = []
        errors: list[str] = []
        for fetch in (
            self._fetch_from_queries,
            self._fetch_from_lists,
            self._fetch_from_following,
            self._fetch_from_author_handles,
            self._fetch_from_snapshot,
        ):
            try:
                raw_posts.extend(fetch(client, dry_run=dry_run))
            except XApiError as exc:
                errors.append(str(exc))
        return raw_posts, errors

    def _fetch_from_queries(self, client: XApiClient, *, dry_run: bool) -> list[dict[str, Any]]:
        """Fetch posts for each configured search query."""
        posts: list[dict[str, Any]] = []
        for query in self.config.sources.search_queries:
            posts.extend(self._fetch_source(client, f"query:{query}", query, dry_run=dry_run))
        return posts

    def _fetch_from_lists(self, client: XApiClient, *, dry_run: bool) -> list[dict[str, Any]]:
        """Fetch posts from each configured X list."""
        posts: list[dict[str, Any]] = []
        for list_id in self.config.sources.list_ids:
            source_key = f"list:{list_id}"
            since_id = self.store.latest_seen_id(source_key)
            fetched = client.list_posts(
                list_id, since_id=since_id, max_results=self.config.ranking.max_posts_per_run
            )
            self._remember(source_key, fetched, dry_run=dry_run)
            posts.extend(fetched)
        return posts

    def _fetch_from_following(self, client: XApiClient, *, dry_run: bool) -> list[dict[str, Any]]:
        """Fetch posts from accounts followed by configured users."""
        handles: list[str] = []
        for user_id in self.config.sources.following_user_ids:
            handles.extend(client.following_handles(user_id))
        return self._fetch_author_batches(client, handles, source_prefix="following", dry_run=dry_run)

    def _fetch_from_author_handles(self, client: XApiClient, *, dry_run: bool) -> list[dict[str, Any]]:
        """Fetch posts from explicitly configured author handles."""
        return self._fetch_author_batches(
            client, self.config.sources.author_handles, source_prefix="authors", dry_run=dry_run
        )

    def _fetch_from_snapshot(self, client: XApiClient, *, dry_run: bool) -> list[dict[str, Any]]:
        """Fetch posts from the handles in the following snapshot, if any."""
        snapshot = self.config.sources.following_snapshot
        if not snapshot:
            return []
        return self._fetch_author_batches(
            client, _snapshot_handles(snapshot), source_prefix="snapshot", dry_run=dry_run
        )

    def _fetch_author_batches(
        self, client: XApiClient, handles: list[str], *, source_prefix: str, dry_run: bool
    ) -> list[dict[str, Any]]:
        """Fetch posts for batches of author handles via combined ``from:`` queries."""
        clean = sorted({h.lower().lstrip("@") for h in handles if h.strip()})
        posts: list[dict[str, Any]] = []
        for index in range(0, len(clean), _AUTHOR_BATCH_SIZE):
            batch = clean[index : index + _AUTHOR_BATCH_SIZE]
            query = "(" + " OR ".join(f"from:{h}" for h in batch) + f") {self.config.ranking.topic_query}"
            source_key = f"{source_prefix}:{','.join(batch)}"
            posts.extend(self._fetch_source(client, source_key, query, dry_run=dry_run))
        return posts

    def _fetch_source(
        self, client: XApiClient, source_key: str, query: str, *, dry_run: bool
    ) -> list[dict[str, Any]]:
        """Run one recent-search fetch and update the source high-water mark."""
        since_id = self.store.latest_seen_id(source_key)
        fetched = client.search_recent(
            query, since_id=since_id, max_results=self.config.ranking.max_posts_per_run
        )
        self._remember(source_key, fetched, dry_run=dry_run)
        return fetched

    def _remember(self, source_key: str, fetched: list[dict[str, Any]], *, dry_run: bool) -> None:
        """Persist the source high-water mark unless this is a dry run."""
        if not dry_run:
            self.store.remember_source_highwater(source_key, fetched)


def snapshot_following(
    output_path: str,
    *,
    user_id: str | None = None,
    username: str | None = None,
    as_json: bool = False,
) -> int:
    """Fetch followed accounts and write a private snapshot for focused searches."""
    load_dotenv()
    tokens = XApiTokens.from_env()
    token = tokens.token(require_user_context=not (user_id or username))
    if not token:
        return _fail(
            "snapshot-following",
            2,
            "X_API_OAUTH2_ACCESS_TOKEN is required for snapshot-following "
            "without --username or --user-id.",
            as_json=as_json,
        )

    client = XApiClient(token)
    try:
        if user_id:
            current_user = {"id": user_id}
        elif username:
            current_user = client.user_by_username(username)
        else:
            current_user = client.current_user()
        followed_users = client.following_users(str(current_user["id"]))
    except XApiError as exc:
        return _fail("snapshot-following", 1, str(exc), as_json=as_json)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_user": current_user,
        "count": len(followed_users),
        "users": sorted(followed_users, key=lambda user: str(user.get("username", "")).lower()),
    }
    output.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    data = {"snapshot_path": str(output), "following_count": len(followed_users)}
    lines = [f"FOLLOWING_SNAPSHOT={output}", f"FOLLOWING_COUNT={len(followed_users)}"]
    return emit("snapshot-following", as_json=as_json, ok=True, rc=0, data=data, lines=lines)


def _snapshot_handles(path: Path) -> list[str]:
    """Return the usernames recorded in a following-snapshot file."""
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [user["username"] for user in raw.get("users", []) if user.get("username")]


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the X intelligence CLI."""
    parser = argparse.ArgumentParser(prog="loopcraft-x-intel")
    parser.add_argument("--json", action="store_true", help="Emit a structured JSON result envelope.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Fetch, rank, and write a daily X digest.")
    run_parser.add_argument("--config", default=_DEFAULT_CONFIG, help="Path to YAML content config.")
    run_parser.add_argument("--dry-run", action="store_true", help="Fetch and rank without writing seen state.")

    snapshot_parser = subparsers.add_parser(
        "snapshot-following",
        help="Fetch followed accounts and write a repo snapshot for focused daily searches.",
    )
    snapshot_parser.add_argument(
        "--output",
        default=_DEFAULT_SNAPSHOT_OUTPUT,
        help="Private snapshot JSON path (gitignored by *.local.*).",
    )
    snapshot_parser.add_argument("--user-id", help="X user ID to snapshot. Defaults to /2/users/me.")
    snapshot_parser.add_argument("--username", help="X username to resolve and snapshot.")

    discover_parser = subparsers.add_parser(
        "discover-follows",
        help="Recommend new X accounts to follow from the latest intelligence digest.",
    )
    discover_parser.add_argument("--config", default=_DEFAULT_CONFIG, help="Path to YAML content config.")
    discover_parser.add_argument("--digest-json", help="Digest JSON path. Defaults to latest digest in config.")
    discover_parser.add_argument("--output-dir", help="Output directory. Defaults to config.output.follow_candidates_dir.")
    discover_parser.add_argument("--top", type=int, default=25, help="Maximum candidates to emit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the requested X intelligence command."""
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return XIntelRunner(args.config).run(dry_run=args.dry_run, as_json=args.json)
    if args.command == "snapshot-following":
        return snapshot_following(
            args.output, user_id=args.user_id, username=args.username, as_json=args.json
        )
    if args.command == "discover-follows":
        return XIntelRunner(args.config).discover_follows(
            digest_json=args.digest_json,
            output_dir=args.output_dir,
            top=args.top,
            as_json=args.json,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
