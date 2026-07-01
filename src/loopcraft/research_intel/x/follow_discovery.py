from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import IntelConfig


@dataclass
class Candidate:
    username: str
    score: int = 0
    reasons: set[str] = field(default_factory=set)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=dict)


def load_latest_digest_json(digest_dir: Path) -> Path:
    paths = sorted(digest_dir.glob("*.json"))
    if not paths:
        raise FileNotFoundError(f"No digest JSON files found in {digest_dir}")
    return paths[-1]


def discover_candidates(
    digest: dict[str, Any],
    config: IntelConfig,
    *,
    followed_handles: set[str],
    hydrated_profiles: list[dict[str, Any]] | None = None,
    top_n: int = 25,
) -> list[dict[str, Any]]:
    candidates: dict[str, Candidate] = {}
    own_handle = _source_handle(config.sources.following_snapshot)
    excluded = {handle.lower().lstrip("@") for handle in followed_handles}
    if own_handle:
        excluded.add(own_handle)

    for post in digest.get("top_posts", []):
        _add_author_candidate(candidates, post)
        _add_mention_candidates(candidates, post)
        _add_linked_x_candidates(candidates, post)

    for handle in list(candidates):
        if handle in excluded:
            del candidates[handle]

    profiles_by_handle = {
        str(profile.get("username", "")).lower(): profile for profile in hydrated_profiles or [] if profile.get("username")
    }
    for handle, candidate in candidates.items():
        profile = profiles_by_handle.get(handle)
        if profile:
            candidate.profile = profile
            candidate.score += _profile_score(profile, config)

    filtered = [
        candidate
        for candidate in candidates.values()
        if _candidate_is_relevant(candidate, config)
    ]
    ranked = sorted(filtered, key=lambda item: (item.score, len(item.evidence), item.username), reverse=True)
    return [_candidate_to_dict(candidate) for candidate in ranked[:top_n]]


def followed_handles_from_snapshot(path: Path | None) -> set[str]:
    if not path or not path.exists():
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    handles = {str(user.get("username", "")).lower() for user in raw.get("users", []) if user.get("username")}
    source = raw.get("source_user", {}).get("username")
    if source:
        handles.add(str(source).lower())
    return handles


def render_follow_candidates(candidates: list[dict[str, Any]], *, generated_at: datetime, digest_path: Path) -> str:
    lines = [
        f"# X Follow Candidates - {generated_at.date().isoformat()}",
        "",
        f"Generated at `{generated_at.isoformat()}` from `{digest_path}`.",
        f"Recommended candidates: `{len(candidates)}`.",
        "",
    ]
    if not candidates:
        lines.append("No new follow candidates found.")
        lines.append("")
        return "\n".join(lines)

    for candidate in candidates:
        profile = candidate.get("profile") or {}
        username = candidate["username"]
        name = profile.get("name") or username
        description = " ".join(str(profile.get("description", "")).split())
        reasons = ", ".join(candidate.get("reasons", []))
        lines.append(f"- **{name} (@{username})** - score `{candidate['score']}`")
        lines.append(f"  - Profile: https://x.com/{username}")
        if description:
            lines.append(f"  - Bio: {description[:240]}")
        if reasons:
            lines.append(f"  - Reasons: {reasons}")
        for item in candidate.get("evidence", [])[:3]:
            lines.append(f"  - Evidence: {item['kind']} via [{item['source_author']}](https://x.com/{item['source_author']}/status/{item['source_post_id']})")
        lines.append("")
    return "\n".join(lines)


def _add_author_candidate(candidates: dict[str, Candidate], post: dict[str, Any]) -> None:
    author = post.get("author") or {}
    username = str(author.get("username", "")).lower()
    if username:
        _add_evidence(candidates, username, post, "high-signal author", 25)


def _add_mention_candidates(candidates: dict[str, Candidate], post: dict[str, Any]) -> None:
    for mention in (post.get("entities") or {}).get("mentions") or []:
        username = str(mention.get("username", "")).lower()
        if username:
            _add_evidence(candidates, username, post, "mentioned in high-signal post", 15)


def _add_linked_x_candidates(candidates: dict[str, Candidate], post: dict[str, Any]) -> None:
    for item in (post.get("entities") or {}).get("urls") or []:
        expanded = item.get("expanded_url") or item.get("unwound_url") or item.get("url")
        username = _handle_from_x_url(str(expanded or ""))
        if username:
            _add_evidence(candidates, username, post, "linked X source", 30)


def _add_evidence(candidates: dict[str, Candidate], username: str, post: dict[str, Any], kind: str, weight: int) -> None:
    username = username.lower().lstrip("@")
    author = post.get("author") or {}
    candidate = candidates.setdefault(username, Candidate(username=username))
    candidate.score += weight + min(int(post.get("score", 0)) // 10, 12)
    candidate.reasons.add(kind)
    candidate.evidence.append(
        {
            "kind": kind,
            "source_post_id": post.get("id"),
            "source_author": author.get("username", "unknown"),
            "source_score": post.get("score", 0),
            "source_text": " ".join(str(post.get("text", "")).split())[:280],
        }
    )


def _profile_score(profile: dict[str, Any], config: IntelConfig) -> int:
    score = 0
    description = str(profile.get("description", "")).lower()
    name = str(profile.get("name", "")).lower()
    text = f"{name} {description}"
    for keyword, weight in config.ranking.keywords.items():
        if _contains_term(text, keyword):
            score += min(weight, 20)
    if _profile_has_discovery_context(profile, config):
        score += 10
    else:
        return score
    metrics = profile.get("public_metrics") or {}
    followers = int(metrics.get("followers_count", 0))
    score += min(followers // 25_000, 20)
    if profile.get("verified"):
        score += 5
    return score


def _candidate_is_relevant(candidate: Candidate, config: IntelConfig) -> bool:
    if "linked X source" in candidate.reasons or "high-signal author" in candidate.reasons:
        return True
    if len(candidate.evidence) >= 2:
        return True
    return _profile_has_discovery_context(candidate.profile, config)


def _profile_has_discovery_context(profile: dict[str, Any], config: IntelConfig) -> bool:
    if not profile:
        return False
    description = str(profile.get("description", "")).lower()
    name = str(profile.get("name", "")).lower()
    text = f"{name} {description}"
    discovery_terms = {
        "ai",
        "ml",
        "llm",
        "agent",
        "agents",
        "model",
        "models",
        "research",
        "science",
        "technology",
        "software",
        "engineering",
        "data",
        "robotics",
        "frontier",
        "eval",
        "evals",
    }
    return any(_contains_term(text, term) for term in discovery_terms | set(config.ranking.keywords))


def _candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
    return {
        "username": candidate.username,
        "score": candidate.score,
        "reasons": sorted(candidate.reasons),
        "evidence": candidate.evidence,
        "profile": candidate.profile,
    }


def _handle_from_x_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    handle = parts[0].lower()
    if handle in {"i", "intent", "share", "search", "home", "hashtag"}:
        return None
    if not re.match(r"^[a-z0-9_]{1,15}$", handle):
        return None
    return handle


def _source_handle(path: Path | None) -> str | None:
    if not path or not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    username = raw.get("source_user", {}).get("username")
    return str(username).lower() if username else None


def _contains_term(text: str, term: str) -> bool:
    if " " in term or "-" in term:
        return term in text
    return re.search(rf"\b{re.escape(term)}\b", text) is not None
