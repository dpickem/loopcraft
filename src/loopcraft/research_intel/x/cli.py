from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loopcraft.config import LoopcraftConfig, is_state_path
from loopcraft.env import load_dotenv

from .client import XApiClient, XApiError
from .config import IntelConfig
from .digest import render_digest
from .follow_discovery import (
    discover_candidates,
    followed_handles_from_snapshot,
    load_latest_digest_json,
    render_follow_candidates,
)
from .ranking import rank_posts
from .store import IntelStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loopcraft-x-intel")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Fetch, rank, and write a daily X digest.")
    run_parser.add_argument("--config", default="config/x_intel.yaml", help="Path to YAML content config.")
    run_parser.add_argument("--dry-run", action="store_true", help="Fetch and rank without writing seen state.")

    snapshot_parser = subparsers.add_parser(
        "snapshot-following",
        help="Fetch followed accounts and write a repo snapshot for focused daily searches.",
    )
    snapshot_parser.add_argument(
        "--output",
        default="config/x_following_snapshot.local.json",
        help="Private snapshot JSON path (gitignored by *.local.*).",
    )
    snapshot_parser.add_argument("--user-id", help="X user ID to snapshot. Defaults to /2/users/me.")
    snapshot_parser.add_argument("--username", help="X username to resolve and snapshot.")

    discover_parser = subparsers.add_parser(
        "discover-follows",
        help="Recommend new X accounts to follow from the latest intelligence digest.",
    )
    discover_parser.add_argument("--config", default="config/x_intel.yaml", help="Path to YAML content config.")
    discover_parser.add_argument("--digest-json", help="Digest JSON path. Defaults to latest digest in config.")
    discover_parser.add_argument("--output-dir", help="Output directory. Defaults to config.output.follow_candidates_dir.")
    discover_parser.add_argument("--top", type=int, default=25, help="Maximum candidates to emit.")

    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.config, dry_run=args.dry_run)
    if args.command == "snapshot-following":
        return snapshot_following(args.output, user_id=args.user_id, username=args.username)
    if args.command == "discover-follows":
        return discover_follows(args.config, digest_json=args.digest_json, output_dir=args.output_dir, top=args.top)
    return 2


def run(config_path: str, *, dry_run: bool = False) -> int:
    load_dotenv()
    loopcraft = LoopcraftConfig.load()
    config = IntelConfig.load(Path(config_path))
    token = _api_token()
    if not token:
        print("ERROR: X_API_BEARER_TOKEN is required for official X API access.", file=sys.stderr)
        return 2

    store = IntelStore(
        seen_path=_resolve_path(loopcraft, config.output.seen_path),
        posts_path=_resolve_path(loopcraft, config.output.posts_path),
        source_state_path=_resolve_path(loopcraft, config.output.source_state_path),
    )
    client = XApiClient(token)
    errors: list[str] = []
    raw_posts: list[dict[str, Any]] = []

    try:
        raw_posts.extend(_fetch_from_queries(client, store, config, dry_run=dry_run))
        raw_posts.extend(_fetch_from_lists(client, store, config, dry_run=dry_run))
        raw_posts.extend(_fetch_from_following(client, store, config, dry_run=dry_run))
        raw_posts.extend(_fetch_from_author_handles(client, store, config, dry_run=dry_run))
        raw_posts.extend(_fetch_from_snapshot(client, store, config, dry_run=dry_run))
    except XApiError as exc:
        errors.append(str(exc))

    ranked = rank_posts(raw_posts, config)
    top_posts = ranked[: config.ranking.top_posts]

    now = datetime.now(UTC)
    if not dry_run:
        store.save_posts(raw_posts)
        store.mark_seen([post["id"] for post in raw_posts if post.get("id")])

    digest_dir = _resolve_path(loopcraft, config.output.digest_dir)
    digest_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d")
    run_stamp = now.strftime("%Y%m%dT%H%M%SZ")
    markdown_path = digest_dir / f"{stamp}.md"
    json_path = digest_dir / f"{stamp}.json"
    history_dir = _resolve_path(loopcraft, config.output.history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)
    history_markdown = history_dir / f"{run_stamp}.md"
    history_json = history_dir / f"{run_stamp}.json"
    latest_markdown = _resolve_path(loopcraft, config.output.latest_markdown)
    latest_json = _resolve_path(loopcraft, config.output.latest_json)
    latest_markdown.parent.mkdir(parents=True, exist_ok=True)
    latest_json.parent.mkdir(parents=True, exist_ok=True)

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
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(payload, encoding="utf-8")
    history_markdown.write_text(markdown, encoding="utf-8")
    history_json.write_text(payload, encoding="utf-8")
    latest_markdown.write_text(markdown, encoding="utf-8")
    latest_json.write_text(payload, encoding="utf-8")

    print(f"DIGEST_MARKDOWN={markdown_path}")
    print(f"DIGEST_JSON={json_path}")
    if errors:
        print("ERROR_SUMMARY=" + " | ".join(errors), file=sys.stderr)
        return 1
    return 0


def snapshot_following(output_path: str, *, user_id: str | None = None, username: str | None = None) -> int:
    load_dotenv()
    token = _api_token(require_user_context=not (user_id or username))
    if not token:
        print(
            "ERROR: X_API_OAUTH2_ACCESS_TOKEN is required for snapshot-following without --username or --user-id.",
            file=sys.stderr,
        )
        return 2

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
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_user": current_user,
        "count": len(followed_users),
        "users": sorted(followed_users, key=lambda user: str(user.get("username", "")).lower()),
    }
    output.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"FOLLOWING_SNAPSHOT={output}")
    print(f"FOLLOWING_COUNT={len(followed_users)}")
    return 0


def discover_follows(config_path: str, *, digest_json: str | None, output_dir: str | None, top: int) -> int:
    load_dotenv()
    loopcraft = LoopcraftConfig.load()
    config = IntelConfig.load(Path(config_path))
    token = _api_token()
    if not token:
        print("ERROR: X_API_BEARER_TOKEN or X_API_OAUTH2_ACCESS_TOKEN is required for profile hydration.", file=sys.stderr)
        return 2

    digest_dir = _resolve_path(loopcraft, config.output.digest_dir)
    digest_path = Path(digest_json) if digest_json else load_latest_digest_json(digest_dir)
    digest = json.loads(digest_path.read_text(encoding="utf-8"))
    followed_handles = followed_handles_from_snapshot(config.sources.following_snapshot)
    initial = discover_candidates(digest, config, followed_handles=followed_handles, top_n=top * 3)

    client = XApiClient(token)
    try:
        profiles = client.users_by_usernames([candidate["username"] for candidate in initial])
    except XApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    candidates = discover_candidates(
        digest,
        config,
        followed_handles=followed_handles,
        hydrated_profiles=profiles,
        top_n=top,
    )
    generated_at = datetime.now(UTC)
    out_dir = _resolve_path(loopcraft, Path(output_dir)) if output_dir else _resolve_path(
        loopcraft, config.output.follow_candidates_dir
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.strftime("%Y-%m-%d")
    markdown_path = out_dir / f"{stamp}.md"
    json_path = out_dir / f"{stamp}.json"

    markdown_path.write_text(render_follow_candidates(candidates, generated_at=generated_at, digest_path=digest_path), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "generated_at": generated_at.isoformat(),
                "digest_json": str(digest_path),
                "candidate_count": len(candidates),
                "candidates": candidates,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"FOLLOW_CANDIDATES_MARKDOWN={markdown_path}")
    print(f"FOLLOW_CANDIDATES_JSON={json_path}")
    return 0


def _fetch_from_queries(
    client: XApiClient,
    store: IntelStore,
    config: IntelConfig,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for query in config.sources.search_queries:
        since_id = store.latest_seen_id(f"query:{query}")
        fetched = client.search_recent(query, since_id=since_id, max_results=config.ranking.max_posts_per_run)
        if not dry_run:
            store.remember_source_highwater(f"query:{query}", fetched)
        posts.extend(fetched)
    return posts


def _fetch_from_lists(
    client: XApiClient,
    store: IntelStore,
    config: IntelConfig,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for list_id in config.sources.list_ids:
        since_id = store.latest_seen_id(f"list:{list_id}")
        fetched = client.list_posts(list_id, since_id=since_id, max_results=config.ranking.max_posts_per_run)
        if not dry_run:
            store.remember_source_highwater(f"list:{list_id}", fetched)
        posts.extend(fetched)
    return posts


def _fetch_from_following(
    client: XApiClient,
    store: IntelStore,
    config: IntelConfig,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    handles: list[str] = []
    for user_id in config.sources.following_user_ids:
        handles.extend(client.following_handles(user_id))
    return _fetch_author_batches(client, store, config, handles, source_prefix="following", dry_run=dry_run)


def _fetch_from_author_handles(
    client: XApiClient,
    store: IntelStore,
    config: IntelConfig,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    return _fetch_author_batches(
        client,
        store,
        config,
        config.sources.author_handles,
        source_prefix="authors",
        dry_run=dry_run,
    )


def _fetch_from_snapshot(
    client: XApiClient,
    store: IntelStore,
    config: IntelConfig,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    if not config.sources.following_snapshot:
        return []
    handles = _snapshot_handles(config.sources.following_snapshot)
    return _fetch_author_batches(client, store, config, handles, source_prefix="snapshot", dry_run=dry_run)


def _fetch_author_batches(
    client: XApiClient,
    store: IntelStore,
    config: IntelConfig,
    handles: list[str],
    *,
    source_prefix: str,
    dry_run: bool,
) -> list[dict[str, Any]]:
    clean_handles = sorted({handle.lower().lstrip("@") for handle in handles if handle.strip()})
    posts: list[dict[str, Any]] = []
    for index in range(0, len(clean_handles), 10):
        batch = clean_handles[index : index + 10]
        query = "(" + " OR ".join(f"from:{handle}" for handle in batch) + f") {_topic_query()} lang:en -is:retweet"
        source_key = f"{source_prefix}:{','.join(batch)}"
        since_id = store.latest_seen_id(source_key)
        fetched = client.search_recent(query, since_id=since_id, max_results=config.ranking.max_posts_per_run)
        if not dry_run:
            store.remember_source_highwater(source_key, fetched)
        posts.extend(fetched)
    return posts


def _snapshot_handles(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [user["username"] for user in raw.get("users", []) if user.get("username")]


def _topic_query() -> str:
    return (
        '(AI OR LLM OR "language model" OR "frontier model" OR "foundation model" '
        'OR agent OR agents OR evals OR harness OR "tool use" OR reasoning OR "post-training" OR coding)'
    )


def _api_token(*, require_user_context: bool = False) -> str | None:
    if require_user_context:
        return os.environ.get("X_API_OAUTH2_ACCESS_TOKEN")
    return os.environ.get("X_API_OAUTH2_ACCESS_TOKEN") or os.environ.get("X_API_BEARER_TOKEN")


def _resolve_path(loopcraft: LoopcraftConfig, path: Path) -> Path:
    """Resolve config paths through the loopcraft memory tree when they use state/."""
    raw = path.as_posix()
    if is_state_path(raw):
        return loopcraft.resolve_state_path(raw)
    return path


if __name__ == "__main__":
    raise SystemExit(main())
