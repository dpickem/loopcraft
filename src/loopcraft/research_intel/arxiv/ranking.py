"""Keyword-based scoring and ranking of arXiv papers."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from loopcraft.research_intel.arxiv.config import ArxivIntelConfig


def rank_papers(papers: list[dict[str, Any]], config: ArxivIntelConfig) -> list[dict[str, Any]]:
    ranked = [score_paper(paper, config) for paper in _dedupe(papers)]
    ranked = [paper for paper in ranked if paper.get("score", 0) >= config.ranking.min_score]
    ranked.sort(key=lambda paper: (paper.get("score", 0), paper.get("published") or ""), reverse=True)
    return ranked


def score_paper(paper: dict[str, Any], config: ArxivIntelConfig, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    scored = dict(paper)
    text = f"{paper.get('title', '')} {paper.get('abstract', '')}".lower()
    reasons: list[str] = []
    score = 0

    for keyword, weight in config.ranking.keywords.items():
        if _contains_term(text, keyword):
            score += weight
            reasons.append(keyword)

    for keyword, penalty in config.ranking.negative_keywords.items():
        if _contains_term(text, keyword):
            score -= penalty
            reasons.append(f"penalty: {keyword}")

    categories = set(paper.get("categories", []))
    if categories.intersection({"cs.AI", "cs.CL", "cs.LG", "stat.ML"}):
        score += 8
        reasons.append("target category")

    published = _parse_datetime(paper.get("published"))
    if published:
        age_days = (now - published).total_seconds() / 86400
        if age_days <= config.ranking.recency_bonus_days:
            score += max(1, int(config.ranking.recency_bonus_days - age_days + 1))
            reasons.append("recent")

    scored["score"] = score
    scored["score_reasons"] = reasons
    return scored


def _dedupe(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for paper in papers:
        paper_id = str(paper.get("id", ""))
        base_id = paper_id.split("v", 1)[0]
        if base_id:
            deduped[base_id] = paper
    return list(deduped.values())


def _contains_term(text: str, term: str) -> bool:
    if " " in term or "-" in term:
        return term in text
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None

