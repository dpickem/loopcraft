"""Tests for the arXiv intelligence client, ranking, digest, and store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from loopcraft.config import LoopcraftConfig
from loopcraft.research_intel.arxiv.client import build_search_query, parse_feed
from loopcraft.research_intel.arxiv.config import ArxivIntelConfig, OutputPaths
from loopcraft.research_intel.arxiv.digest import render_digest
from loopcraft.research_intel.arxiv.ranking import rank_papers, score_paper
from loopcraft.research_intel.arxiv.store import ArxivStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _config() -> ArxivIntelConfig:
    """Return an arXiv content config with representative sources/ranking."""
    return ArxivIntelConfig.from_dict(
        {
            "sources": {
                "categories": ["cs.AI", "cs.CL"],
                "search_terms": ["agent", "recursive self-improvement"],
            },
            "ranking": {
                "max_results": 10,
                "top_papers": 5,
                "min_score": 10,
                "recency_bonus_days": 7,
                "keywords": {"agent": 20, "recursive self-improvement": 60, "harness": 20},
                "negative_keywords": {"wireless": 25},
            },
        }
    )


def test_build_search_query_uses_categories_and_terms() -> None:
    """The search query combines categories and quoted multi-word terms."""
    query = build_search_query(_config())

    assert "cat:cs.AI" in query
    assert 'all:"recursive self-improvement"' in query
    assert "AND" in query


def test_parse_feed_extracts_abstract_metadata() -> None:
    """parse_feed extracts id, title, authors, and primary category."""
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2606.12345v1</id>
        <updated>2026-06-15T12:00:00Z</updated>
        <published>2026-06-15T12:00:00Z</published>
        <title>Self-Improving Agent Harnesses</title>
        <summary>We study recursive self-improvement for agent harnesses.</summary>
        <author><name>Ada Lovelace</name></author>
        <category term="cs.AI" />
        <arxiv:primary_category term="cs.AI" />
        <link href="http://arxiv.org/abs/2606.12345v1" rel="alternate" type="text/html" />
        <link title="pdf" href="http://arxiv.org/pdf/2606.12345v1" rel="related" type="application/pdf" />
      </entry>
    </feed>"""

    papers = parse_feed(payload)

    assert papers[0]["id"] == "2606.12345v1"
    assert papers[0]["title"] == "Self-Improving Agent Harnesses"
    assert papers[0]["authors"] == ["Ada Lovelace"]
    assert papers[0]["primary_category"] == "cs.AI"


def test_score_paper_weights_loopcraft_topics() -> None:
    """score_paper rewards configured loopcraft topic keywords."""
    paper = {
        "id": "2606.12345v1",
        "title": "Recursive Self-Improvement for Agent Harnesses",
        "abstract": "We study agent harnesses for recursive self-improvement.",
        "published": "2026-06-15T12:00:00+00:00",
        "categories": ["cs.AI"],
    }

    scored = score_paper(paper, _config(), now=datetime(2026, 6, 15, 13, 0, tzinfo=UTC))

    assert scored["score"] >= 90
    assert "recursive self-improvement" in scored["score_reasons"]


def test_rank_papers_filters_low_score_and_sorts() -> None:
    """rank_papers drops sub-threshold papers and keeps the relevant one."""
    papers = [
        {"id": "1v1", "title": "Wireless Systems", "abstract": "wireless", "categories": ["cs.IT"]},
        {"id": "2v1", "title": "Agent Harness", "abstract": "agent harness", "categories": ["cs.AI"]},
    ]

    ranked = rank_papers(papers, _config())

    assert len(ranked) == 1
    assert ranked[0]["id"] == "2v1"


def test_render_digest_links_abstract_and_pdf() -> None:
    """The digest includes abstract, PDF, and comment code links."""
    markdown = render_digest(
        [
            {
                "id": "2606.12345v1",
                "title": "Agent Harnesses",
                "abstract": "A useful abstract.",
                "authors": ["Ada Lovelace"],
                "categories": ["cs.AI"],
                "published": "2026-06-15T12:00:00+00:00",
                "abstract_url": "https://arxiv.org/abs/2606.12345v1",
                "pdf_url": "https://arxiv.org/pdf/2606.12345v1",
                "comment": "Code: https://github.com/example/agent-harnesses",
                "score": 42,
                "score_reasons": ["agent", "harness"],
            }
        ],
        raw_papers=[],
        errors=[],
        generated_at=datetime(2026, 6, 15, tzinfo=UTC),
    )

    assert "https://arxiv.org/abs/2606.12345v1" in markdown
    assert "https://arxiv.org/pdf/2606.12345v1" in markdown
    assert "https://github.com/example/agent-harnesses" in markdown


def test_arxiv_output_defaults_are_memory_state_paths() -> None:
    """Fixed arXiv output paths point at the memory ledger state tree."""
    output = OutputPaths()
    assert output.seen_path.as_posix() == "state/research/arxiv/seen.json"
    assert output.papers_path.as_posix() == "state/research/arxiv/papers.jsonl"
    assert output.history_dir.as_posix() == "state/research/arxiv/history"
    assert output.latest_markdown.as_posix() == "state/research/arxiv/latest.md"


def test_arxiv_config_rejects_output_override(tmp_path) -> None:
    """Finding 3 (review 06): content config cannot redirect durable outputs.

    Neither the public content config nor a gitignored local override may set
    ``output`` paths — the manifest's declared outputs are the source of truth.
    """
    import pytest

    with pytest.raises(ValueError, match="must not override 'output'"):
        ArxivIntelConfig.from_dict({"output": {"latest_markdown": "state/research/arxiv/custom.md"}})

    (tmp_path / "arxiv_intel.yaml").write_text("ranking: {top_papers: 5}\n", encoding="utf-8")
    (tmp_path / "arxiv_intel.local.yaml").write_text(
        "output: {latest_markdown: state/research/arxiv/custom.md}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="must not override 'output'"):
        ArxivIntelConfig.load(tmp_path / "arxiv_intel.yaml")


def test_arxiv_fixed_outputs_match_manifest_contract() -> None:
    """Finding 3 (review 06): the fixed write set equals the declared outputs."""
    from loopcraft.manifest import LoopManifest

    manifest = LoopManifest.load(REPO_ROOT / "loops" / "arxiv-intel.yaml")
    out = OutputPaths()
    expected = {
        out.seen_path.as_posix(),
        out.papers_path.as_posix(),
        f"{out.digest_dir.as_posix()}/{{{{date}}}}.md",
        f"{out.digest_dir.as_posix()}/{{{{date}}}}.json",
        f"{out.history_dir.as_posix()}/{{{{run_id}}}}.md",
        f"{out.history_dir.as_posix()}/{{{{run_id}}}}.json",
        out.latest_markdown.as_posix(),
        out.latest_json.as_posix(),
    }
    assert set(manifest.outputs) == expected


def test_arxiv_loads_yaml_content_config() -> None:
    """The shipped arXiv YAML content config loads with expected values."""
    config = ArxivIntelConfig.load(REPO_ROOT / "config" / "arxiv_intel.yaml")
    assert "cs.AI" in config.sources.categories
    assert "recursive self-improvement" in config.ranking.keywords


def test_arxiv_run_produces_manifest_outputs_for_control_plane_run_id(tmp_path, monkeypatch) -> None:
    """The direct arXiv CLI writes exactly the manifest outputs for a given run id.

    Simulates the control plane handing the loop its run id via LOOPCRAFT_RUN_ID
    and asserts every declared `arxiv-intel` output (including the run-scoped
    history archives) resolves to a file the direct workflow actually wrote.
    """
    from loopcraft.manifest import LoopManifest
    from loopcraft.research_intel.arxiv import cli as arxiv_cli

    run_id = "20260101T000000Z-deadbeef"
    # Finding 3 (review 07): the control plane hands down its resolved run date;
    # it deliberately differs from the real clock date here, simulating a run
    # that crosses UTC midnight after outputs were resolved.
    run_date = "2026-01-01"
    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(REPO_ROOT))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPCRAFT_RUN_ID", run_id)
    monkeypatch.setenv("LOOPCRAFT_RUN_DATE", run_date)
    monkeypatch.setattr(arxiv_cli.ArxivClient, "search_recent", lambda self, config: [])

    rc = arxiv_cli.run(str(REPO_ROOT / "config" / "arxiv_intel.yaml"))
    assert rc == 0

    loopcraft = LoopcraftConfig.load(REPO_ROOT)
    manifest = LoopManifest.load(REPO_ROOT / "loops" / "arxiv-intel.yaml")
    for declared in manifest.outputs:
        resolved = loopcraft.resolve_state_template(declared, run_id=run_id, date=run_date)
        assert resolved.exists(), f"missing declared output: {declared} -> {resolved}"


def test_arxiv_cli_reports_invalid_config_as_json_envelope(tmp_path, monkeypatch, capsys) -> None:
    """Finding 4 (review 07): a malformed config yields the JSON envelope, not a traceback."""
    import json

    from loopcraft.research_intel.arxiv import cli as arxiv_cli

    monkeypatch.setenv("LOOPCRAFT_SOURCE", str(tmp_path / "src"))
    monkeypatch.setenv("LOOPCRAFT_MEMORY", str(tmp_path / "mem"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("sources: [unclosed\n", encoding="utf-8")

    rc = arxiv_cli.main(["--json", "run", "--config", str(bad)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["command"] == "run"
    assert payload["ok"] is False
    assert "invalid or unreadable content config" in payload["data"]["error"]


def test_arxiv_config_load_prefers_local_override(tmp_path) -> None:
    """ArxivIntelConfig.load reads a gitignored .local. sibling when present."""
    (tmp_path / "arxiv_intel.yaml").write_text("ranking: {top_papers: 5}\n", encoding="utf-8")
    (tmp_path / "arxiv_intel.local.yaml").write_text("ranking: {top_papers: 42}\n", encoding="utf-8")
    config = ArxivIntelConfig.load(tmp_path / "arxiv_intel.yaml")
    assert config.ranking.top_papers == 42


def test_arxiv_store_uses_json_ledger_files(tmp_path) -> None:
    """The arXiv store persists papers/seen ids as JSON(L) in the ledger."""
    config = LoopcraftConfig(source_path=tmp_path / "src", memory_path=tmp_path / "mem")
    output = OutputPaths(
        seen_path=Path("state/seen.json"),
        papers_path=Path("state/papers.jsonl"),
    )
    store = ArxivStore(config, output)
    store.save_papers(
        [
            {"id": "1", "title": "Old"},
            {"id": "2", "title": "New"},
        ]
    )
    store.save_papers([{"id": "1", "title": "Updated"}])
    store.mark_seen(["1", "2"])

    assert store.seen_ids() == {"1", "2"}
    lines = (tmp_path / "mem" / "ledger" / "papers.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "Updated" in "\n".join(lines)
