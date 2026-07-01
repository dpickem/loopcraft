from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from loopcraft.config import LoopcraftConfig, is_state_path

from .client import ArxivApiError, ArxivClient
from .config import ArxivIntelConfig
from .digest import render_digest
from .ranking import rank_papers
from .store import ArxivStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loopcraft-arxiv-intel")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Fetch, rank, and write a daily arXiv digest.")
    run_parser.add_argument("--config", default="config/arxiv_intel.json", help="Path to JSON config.")
    run_parser.add_argument("--dry-run", action="store_true", help="Fetch and rank without writing seen state.")
    run_parser.add_argument("--include-seen", action="store_true", help="Include papers already seen in prior runs.")

    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.config, dry_run=args.dry_run, include_seen=args.include_seen)
    return 2


def run(config_path: str, *, dry_run: bool = False, include_seen: bool = False) -> int:
    loopcraft = LoopcraftConfig.load()
    config = ArxivIntelConfig.load(Path(config_path))
    store = ArxivStore(
        seen_path=_resolve_path(loopcraft, config.output.seen_path),
        papers_path=_resolve_path(loopcraft, config.output.papers_path),
    )
    client = ArxivClient()
    errors: list[str] = []
    raw_papers: list[dict] = []

    try:
        raw_papers = client.search_recent(config)
    except ArxivApiError as exc:
        errors.append(str(exc))

    candidate_papers = raw_papers
    if not include_seen:
        seen = store.seen_ids()
        candidate_papers = [paper for paper in raw_papers if paper.get("id") not in seen]

    ranked = rank_papers(candidate_papers, config)
    top_papers = ranked[: config.ranking.top_papers]
    now = datetime.now(UTC)

    if not dry_run:
        store.save_papers(raw_papers)
        store.mark_seen([paper["id"] for paper in raw_papers if paper.get("id")])

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

    markdown = render_digest(top_papers, raw_papers, errors, generated_at=now)
    payload = json.dumps(
        {
            "generated_at": now.isoformat(),
            "paper_count": len(raw_papers),
            "ranked_count": len(ranked),
            "top_papers": top_papers,
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

    print(f"ARXIV_DIGEST_MARKDOWN={markdown_path}")
    print(f"ARXIV_DIGEST_JSON={json_path}")
    if errors:
        print("ERROR_SUMMARY=" + " | ".join(errors), file=sys.stderr)
        return 1
    return 0


def _resolve_path(loopcraft: LoopcraftConfig, path: Path) -> Path:
    """Resolve config paths through the loopcraft memory tree when they use state/."""
    raw = path.as_posix()
    if is_state_path(raw):
        return loopcraft.resolve_state_path(raw)
    return path


if __name__ == "__main__":
    raise SystemExit(main())
