"""Keyword-based scoring and ranking of X posts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from loopcraft.research_intel.x.config import IntelConfig


def rank_posts(posts: list[dict[str, Any]], config: IntelConfig) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for post in posts:
        post_id = post.get("id")
        if post_id:
            deduped[str(post_id)] = post

    near_deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for post in deduped.values():
        author = post.get("author") or {}
        key = (str(author.get("username", "")).lower(), _normalize_text(str(post.get("text", ""))))
        existing = near_deduped.get(key)
        if not existing or int(post.get("id", 0)) > int(existing.get("id", 0)):
            near_deduped[key] = post

    ranked = [score_post(post, config) for post in near_deduped.values()]
    ranked = [
        post
        for post in ranked
        if post.get("score", 0) >= config.ranking.min_score or bool(post.get("frontier_lab"))
    ]
    ranked.sort(key=lambda post: (post.get("score", 0), post.get("id", "")), reverse=True)
    return _cap_posts_per_author(ranked, config.ranking.max_posts_per_author)


def score_post(post: dict[str, Any], config: IntelConfig, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    scored = dict(post)
    author = dict(scored.get("author") or {})
    handle = str(author.get("username", "")).lower()
    text = str(scored.get("text", "")).lower()
    reasons: list[str] = []
    score = 0

    frontier_lab = _frontier_lab(handle, author, config)
    high_priority_author = handle in config.frontier_labs.high_priority_handles
    has_ai_context = _has_ai_context(text, config)

    if config.ranking.require_ai_context and not has_ai_context and not frontier_lab and not high_priority_author:
        score -= config.ranking.min_score
        reasons.append("missing AI context")

    if frontier_lab:
        score += config.ranking.frontier_lab_bonus
        reasons.append(f"frontier lab: {frontier_lab}")

    if high_priority_author:
        score += config.ranking.high_priority_author_bonus
        reasons.append("high-priority author")

    for keyword, weight in config.ranking.keywords.items():
        if _contains_term(text, keyword):
            score += weight
            reasons.append(keyword)

    for keyword, penalty in config.ranking.negative_keywords.items():
        if _contains_term(text, keyword):
            score -= penalty
            reasons.append(f"penalty: {keyword}")

    if _is_reply(scored) and not frontier_lab and not high_priority_author:
        score -= config.ranking.reply_penalty
        reasons.append("reply penalty")

    metrics = scored.get("public_metrics") or {}
    score += min(int(metrics.get("like_count", 0)) // 100, 12)
    score += min(int(metrics.get("retweet_count", 0)) // 25, 12)
    score += min(int(metrics.get("reply_count", 0)) // 25, 8)

    created_at = _parse_datetime(scored.get("created_at"))
    if created_at:
        age_hours = (now - created_at).total_seconds() / 3600
        if age_hours <= config.ranking.recency_bonus_hours:
            bonus = max(1, int(config.ranking.recency_bonus_hours - age_hours))
            score += bonus
            reasons.append("recent")

    scored["author"] = author
    scored["frontier_lab"] = frontier_lab
    scored["score"] = score
    scored["score_reasons"] = reasons
    return scored


def _frontier_lab(handle: str, author: dict[str, Any], config: IntelConfig) -> str | None:
    for lab, handles in config.frontier_labs.affiliations.items():
        if handle in handles:
            return lab
    return None


def _cap_posts_per_author(posts: list[dict[str, Any]], max_posts: int) -> list[dict[str, Any]]:
    if max_posts <= 0:
        return posts
    counts: dict[str, int] = {}
    capped: list[dict[str, Any]] = []
    for post in posts:
        author = post.get("author") or {}
        handle = str(author.get("username", "")).lower()
        counts[handle] = counts.get(handle, 0) + 1
        if counts[handle] <= max_posts:
            capped.append(post)
    return capped


def _normalize_text(text: str) -> str:
    no_urls = re.sub(r"https?://\S+", "", text.lower())
    return " ".join(no_urls.split())


def _has_ai_context(text: str, config: IntelConfig) -> bool:
    return any(_contains_term(text, term) for term in config.ranking.ai_context_keywords)


def _contains_term(text: str, term: str) -> bool:
    if " " in term or "-" in term:
        return term in text
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


def _is_reply(post: dict[str, Any]) -> bool:
    return any(ref.get("type") == "replied_to" for ref in post.get("referenced_tweets", []) or [])


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
