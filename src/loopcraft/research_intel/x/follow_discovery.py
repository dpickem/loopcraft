"""X follow-candidate discovery.

Reads the latest X digest and hydrated profiles to recommend new accounts to
follow. Candidate accumulation, filtering, ranking, and markdown rendering live
here; the thresholds and discovery terms come from the X content config.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from enum import StrEnum

from pydantic import BaseModel, Field

from loopcraft.research_intel.x.config import IntelConfig


class EvidenceKind(StrEnum):
    """Closed vocabulary of why an account became a follow candidate."""

    AUTHOR = "high-signal author"
    MENTION = "mentioned in high-signal post"
    LINKED = "linked X source"


#: Base score awarded per evidence kind.
_EVIDENCE_WEIGHT = {
    EvidenceKind.AUTHOR: 25,
    EvidenceKind.MENTION: 15,
    EvidenceKind.LINKED: 30,
}

#: A candidate also inherits a fraction of the source post's score, capped.
_SOURCE_SCORE_DIVISOR = 10
_SOURCE_SCORE_BONUS_CAP = 12

#: Profile (bio/name/metrics) scoring weights and caps.
_PROFILE_KEYWORD_WEIGHT_CAP = 20
_PROFILE_DISCOVERY_CONTEXT_BONUS = 10
_PROFILE_FOLLOWERS_PER_POINT = 25_000
_PROFILE_FOLLOWERS_BONUS_CAP = 20
_PROFILE_VERIFIED_BONUS = 5

#: A candidate with at least this much evidence is kept even without a profile.
_MIN_EVIDENCE_FOR_RELEVANCE = 2

#: Rendering limits for the follow-candidate markdown.
_MAX_EVIDENCE_RENDERED = 3
_MAX_BIO_CHARS = 240
_MAX_EVIDENCE_TEXT_CHARS = 280

#: X URL path prefixes that are not user handles.
_NON_HANDLE_PATH_HEADS = {"i", "intent", "share", "search", "home", "hashtag"}
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
_HANDLE_RE = re.compile(r"^[a-z0-9_]{1,15}$")


class Candidate(BaseModel):
    """Mutable accumulator for one follow candidate."""

    username: str
    score: int = 0
    reasons: set[str] = Field(default_factory=set)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    profile: dict[str, Any] = Field(default_factory=dict)


def load_latest_digest_json(digest_dir: Path) -> Path:
    """Return the newest digest JSON in ``digest_dir``.

    Raises:
        FileNotFoundError: If the directory has no digest JSON files.
    """
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
    """Recommend follow candidates from a digest and optional hydrated profiles.

    Args:
        digest: Parsed digest JSON (uses ``top_posts``).
        config: X content configuration (scoring weights, discovery terms).
        followed_handles: Handles already followed, excluded from results.
        hydrated_profiles: Optional fetched profiles to refine scores.
        top_n: Maximum number of candidates to return.

    Returns:
        Ranked candidate dicts (highest score first).
    """
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
    """Return the set of already-followed handles from a following snapshot."""
    if not path or not path.exists():
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    handles = {str(user.get("username", "")).lower() for user in raw.get("users", []) if user.get("username")}
    source = raw.get("source_user", {}).get("username")
    if source:
        handles.add(str(source).lower())
    return handles


def render_follow_candidates(candidates: list[dict[str, Any]], *, generated_at: datetime, digest_path: Path) -> str:
    """Render the follow-candidate list as markdown."""
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
            lines.append(f"  - Bio: {description[:_MAX_BIO_CHARS]}")
        if reasons:
            lines.append(f"  - Reasons: {reasons}")
        for item in candidate.get("evidence", [])[:_MAX_EVIDENCE_RENDERED]:
            lines.append(f"  - Evidence: {item['kind']} via [{item['source_author']}](https://x.com/{item['source_author']}/status/{item['source_post_id']})")
        lines.append("")
    return "\n".join(lines)


def _add_author_candidate(candidates: dict[str, Candidate], post: dict[str, Any]) -> None:
    """Add the post author as an author-evidence follow candidate."""
    author = post.get("author") or {}
    username = str(author.get("username", "")).lower()
    if username:
        _add_evidence(candidates, username, post, EvidenceKind.AUTHOR)


def _add_mention_candidates(candidates: dict[str, Candidate], post: dict[str, Any]) -> None:
    """Add accounts mentioned in a high-signal post as candidates."""
    for mention in (post.get("entities") or {}).get("mentions") or []:
        username = str(mention.get("username", "")).lower()
        if username:
            _add_evidence(candidates, username, post, EvidenceKind.MENTION)


def _add_linked_x_candidates(candidates: dict[str, Candidate], post: dict[str, Any]) -> None:
    """Add accounts linked from a high-signal post as candidates."""
    for item in (post.get("entities") or {}).get("urls") or []:
        expanded = item.get("expanded_url") or item.get("unwound_url") or item.get("url")
        username = _handle_from_x_url(str(expanded or ""))
        if username:
            _add_evidence(candidates, username, post, EvidenceKind.LINKED)


def _add_evidence(
    candidates: dict[str, Candidate],
    username: str,
    post: dict[str, Any],
    kind: EvidenceKind,
) -> None:
    """Accrue score/evidence for ``username`` from one supporting post."""
    username = username.lower().lstrip("@")
    author = post.get("author") or {}
    candidate = candidates.setdefault(username, Candidate(username=username))
    source_bonus = min(int(post.get("score", 0)) // _SOURCE_SCORE_DIVISOR, _SOURCE_SCORE_BONUS_CAP)
    candidate.score += _EVIDENCE_WEIGHT[kind] + source_bonus
    candidate.reasons.add(str(kind))
    candidate.evidence.append(
        {
            "kind": kind,
            "source_post_id": post.get("id"),
            "source_author": author.get("username", "unknown"),
            "source_score": post.get("score", 0),
            "source_text": " ".join(str(post.get("text", "")).split())[:_MAX_EVIDENCE_TEXT_CHARS],
        }
    )


def _profile_score(profile: dict[str, Any], config: IntelConfig) -> int:
    """Score a hydrated profile by keyword match, discovery context, and reach."""
    score = 0
    description = str(profile.get("description", "")).lower()
    name = str(profile.get("name", "")).lower()
    text = f"{name} {description}"
    for keyword, weight in config.ranking.keywords.items():
        if _contains_term(text, keyword):
            score += min(weight, _PROFILE_KEYWORD_WEIGHT_CAP)
    if _profile_has_discovery_context(profile, config):
        score += _PROFILE_DISCOVERY_CONTEXT_BONUS
    else:
        return score
    metrics = profile.get("public_metrics") or {}
    followers = int(metrics.get("followers_count", 0))
    score += min(followers // _PROFILE_FOLLOWERS_PER_POINT, _PROFILE_FOLLOWERS_BONUS_CAP)
    if profile.get("verified"):
        score += _PROFILE_VERIFIED_BONUS
    return score


def _candidate_is_relevant(candidate: Candidate, config: IntelConfig) -> bool:
    """Return whether a candidate has enough signal to be recommended."""
    strong_reasons = {str(EvidenceKind.LINKED), str(EvidenceKind.AUTHOR)}
    if candidate.reasons & strong_reasons:
        return True
    if len(candidate.evidence) >= _MIN_EVIDENCE_FOR_RELEVANCE:
        return True
    return _profile_has_discovery_context(candidate.profile, config)


def _profile_has_discovery_context(profile: dict[str, Any], config: IntelConfig) -> bool:
    """Return whether the profile text matches configured discovery terms."""
    if not profile:
        return False
    description = str(profile.get("description", "")).lower()
    name = str(profile.get("name", "")).lower()
    text = f"{name} {description}"
    discovery_terms = config.ranking.discovery_context_terms | set(config.ranking.keywords)
    return any(_contains_term(text, term) for term in discovery_terms)


def _candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
    """Serialize a candidate to a JSON-friendly dict with sorted reasons."""
    return {
        "username": candidate.username,
        "score": candidate.score,
        "reasons": sorted(candidate.reasons),
        "evidence": candidate.evidence,
        "profile": candidate.profile,
    }


def _handle_from_x_url(url: str) -> str | None:
    """Extract a bare X handle from an x.com/twitter.com URL, if any."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host not in _X_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    handle = parts[0].lower()
    if handle in _NON_HANDLE_PATH_HEADS:
        return None
    if not _HANDLE_RE.match(handle):
        return None
    return handle


def _source_handle(path: Path | None) -> str | None:
    """Return the snapshot owner's handle (excluded from candidates)."""
    if not path or not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    username = raw.get("source_user", {}).get("username")
    return str(username).lower() if username else None


def _contains_term(text: str, term: str) -> bool:
    """Return whether ``term`` appears in ``text`` (word-boundary aware)."""
    if " " in term or "-" in term:
        return term in text
    return re.search(rf"\b{re.escape(term)}\b", text) is not None
