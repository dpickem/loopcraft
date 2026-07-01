from __future__ import annotations

from datetime import UTC, datetime

from loopcraft.research_intel.arxiv.client import build_search_query, parse_feed
from loopcraft.research_intel.arxiv.config import ArxivIntelConfig
from loopcraft.research_intel.arxiv.digest import render_digest
from loopcraft.research_intel.arxiv.ranking import rank_papers, score_paper
from loopcraft.research_intel.arxiv.store import ArxivStore


def _config() -> ArxivIntelConfig:
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
    query = build_search_query(_config())

    assert "cat:cs.AI" in query
    assert 'all:"recursive self-improvement"' in query
    assert "AND" in query


def test_parse_feed_extracts_abstract_metadata() -> None:
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
    papers = [
        {"id": "1v1", "title": "Wireless Systems", "abstract": "wireless", "categories": ["cs.IT"]},
        {"id": "2v1", "title": "Agent Harness", "abstract": "agent harness", "categories": ["cs.AI"]},
    ]

    ranked = rank_papers(papers, _config())

    assert len(ranked) == 1
    assert ranked[0]["id"] == "2v1"


def test_render_digest_links_abstract_and_pdf() -> None:
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
    config = ArxivIntelConfig.from_dict({})
    assert config.output.seen_path.as_posix() == "state/research/arxiv/seen.json"
    assert config.output.papers_path.as_posix() == "state/research/arxiv/papers.jsonl"
    assert config.output.history_dir.as_posix() == "state/research/arxiv/history"
    assert config.output.latest_markdown.as_posix() == "state/research/arxiv/latest.md"


def test_arxiv_store_uses_json_ledger_files(tmp_path) -> None:
    store = ArxivStore(
        seen_path=tmp_path / "seen.json",
        papers_path=tmp_path / "papers.jsonl",
    )
    store.save_papers(
        [
            {"id": "1", "title": "Old"},
            {"id": "2", "title": "New"},
        ]
    )
    store.save_papers([{"id": "1", "title": "Updated"}])
    store.mark_seen(["1", "2"])

    assert store.seen_ids() == {"1", "2"}
    lines = (tmp_path / "papers.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "Updated" in "\n".join(lines)
