from __future__ import annotations

from datetime import datetime
from typing import Any


def render_digest(
    top_posts: list[dict[str, Any]],
    raw_posts: list[dict[str, Any]],
    errors: list[str],
    *,
    generated_at: datetime,
) -> str:
    lines = [
        f"# Daily X Intelligence - {generated_at.date().isoformat()}",
        "",
        f"Generated at `{generated_at.isoformat()}`.",
        f"Fetched `{len(raw_posts)}` posts; ranked `{len(top_posts)}`.",
        "",
    ]
    if errors:
        lines.extend(["## Errors", ""])
        lines.extend(f"- {error}" for error in errors)
        lines.append("")

    frontier_posts = [post for post in top_posts if post.get("frontier_lab")]
    if frontier_posts:
        lines.extend(["## Frontier-Lab Highlights", ""])
        lines.extend(_render_post(post) for post in frontier_posts[:10])
        lines.append("")

    lines.extend(["## Top Posts", ""])
    if not top_posts:
        lines.append("No matching posts were fetched.")
    else:
        lines.extend(_render_post(post) for post in top_posts)
    lines.append("")
    return "\n".join(lines)


def _render_post(post: dict[str, Any]) -> str:
    author = post.get("author", {})
    username = author.get("username", "unknown")
    name = author.get("name", username)
    lab = f" [{post['frontier_lab']}]" if post.get("frontier_lab") else ""
    score = post.get("score", 0)
    created = post.get("created_at", "unknown time")
    text = " ".join(str(post.get("text", "")).split())
    url = f"https://x.com/{username}/status/{post.get('id')}"
    external_links = _external_links(post)
    reasons = ", ".join(post.get("score_reasons", []))
    links_suffix = ""
    if external_links:
        links = ", ".join(f"[{link['display']}]({link['url']})" for link in external_links)
        links_suffix = f"\n  - External links: {links}"
    reason_suffix = f"\n  - Reasons: {reasons}" if reasons else ""
    return (
        f"- **{name} (@{username}){lab}** - score `{score}` - {created}\n"
        f"  - {text}\n"
        f"  - Post: {url}"
        f"{links_suffix}"
        f"{reason_suffix}"
    )


def _external_links(post: dict[str, Any]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    urls = (post.get("entities") or {}).get("urls") or []
    post_url = f"https://x.com/{(post.get('author') or {}).get('username', '')}/status/{post.get('id')}"
    for item in urls:
        expanded = item.get("expanded_url") or item.get("unwound_url") or item.get("url")
        if not expanded or expanded in seen or expanded == post_url:
            continue
        seen.add(expanded)
        display = item.get("display_url") or expanded.replace("https://", "").replace("http://", "")
        links.append({"display": str(display), "url": str(expanded)})
    return links
