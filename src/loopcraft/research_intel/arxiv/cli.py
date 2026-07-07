"""Command-line entry point for the arXiv intelligence loop."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from loopcraft.cli_output import emit, fail as _fail
from loopcraft.config import LoopcraftConfig, resolve_run_stamps
from loopcraft.research_intel.arxiv.client import ArxivApiError, ArxivClient
from loopcraft.research_intel.arxiv.config import ArxivIntelConfig, OutputPaths
from loopcraft.research_intel.arxiv.digest import render_digest
from loopcraft.research_intel.arxiv.ranking import rank_papers
from loopcraft.research_intel.arxiv.store import ArxivStore


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch the arXiv intelligence command."""
    parser = argparse.ArgumentParser(prog="loopcraft-arxiv-intel")
    parser.add_argument("--json", action="store_true", help="Emit a structured JSON result envelope.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Fetch, rank, and write a daily arXiv digest.")
    run_parser.add_argument("--config", default="config/arxiv_intel.yaml", help="Path to YAML content config.")
    run_parser.add_argument("--dry-run", action="store_true", help="Fetch and rank without writing seen state.")
    run_parser.add_argument("--include-seen", action="store_true", help="Include papers already seen in prior runs.")

    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.config, dry_run=args.dry_run, include_seen=args.include_seen, as_json=args.json)
    return 2


def run(
    config_path: str,
    *,
    dry_run: bool = False,
    include_seen: bool = False,
    as_json: bool = False,
) -> int:
    """Fetch, rank, and write a daily arXiv digest.

    Args:
        config_path: Path to the YAML content-definition file.
        dry_run: If True, do not persist seen/paper state.
        include_seen: If True, keep papers already seen in prior runs.
        as_json: If True, emit the structured JSON envelope instead of text.

    Returns:
        Process exit code (0 on success, 1 if fetch errors occurred).
    """
    loopcraft = LoopcraftConfig.load()
    try:
        # The content config (and any .local override) must stay under the source
        # tree, matching control-plane preflight/staging.
        config = ArxivIntelConfig.load(loopcraft.resolve_content_config(config_path))
    except Exception as exc:  # noqa: BLE001 — CLI boundary: emit envelope, not traceback
        return _fail("run", 2, f"invalid or unreadable content config {config_path}: {exc}", as_json=as_json)
    # Validate inherited control-plane protocol values before any work: a
    # malformed run id/date must be a structured failure, not a traversal.
    try:
        run_stamp, date_stamp = resolve_run_stamps(loopcraft, datetime.now(UTC))
    except ValueError as exc:
        return _fail("run", 2, str(exc), as_json=as_json)
    # Output locations are fixed in code (mirroring the manifest contract), never
    # read from the content config.
    store = ArxivStore(loopcraft, OutputPaths())
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
    markdown_path, json_path = store.write_digest(
        markdown=markdown,
        payload=payload,
        run_stamp=run_stamp,
        date_stamp=date_stamp,
    )

    ok = not errors
    data = {
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
        "paper_count": len(raw_papers),
        "ranked_count": len(ranked),
        "errors": errors,
    }
    lines = [
        f"ARXIV_DIGEST_MARKDOWN={markdown_path}",
        f"ARXIV_DIGEST_JSON={json_path}",
    ]
    rc = emit("run", as_json=as_json, ok=ok, rc=0 if ok else 1, data=data, lines=lines)
    if not as_json and errors:
        print("ERROR_SUMMARY=" + " | ".join(errors), file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
