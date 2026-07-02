"""Markdown/JSON digest rendering for the arXiv intelligence loop."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def render_digest(
    top_papers: list[dict[str, Any]],
    raw_papers: list[dict[str, Any]],
    errors: list[str],
    *,
    generated_at: datetime,
) -> str:
    lines = [
        f"# Daily arXiv Intelligence - {generated_at.date().isoformat()}",
        "",
        f"Generated at `{generated_at.isoformat()}`.",
        f"Fetched `{len(raw_papers)}` papers; ranked `{len(top_papers)}`.",
        "",
    ]
    if errors:
        lines.extend(["## Errors", ""])
        lines.extend(f"- {error}" for error in errors)
        lines.append("")

    lines.extend(["## Top Papers", ""])
    if not top_papers:
        lines.append("No matching papers were fetched.")
    else:
        lines.extend(_render_paper(paper) for paper in top_papers)
    lines.append("")
    return "\n".join(lines)


def _render_paper(paper: dict[str, Any]) -> str:
    title = paper.get("title", "Untitled")
    authors = ", ".join(paper.get("authors", [])[:6])
    if len(paper.get("authors", [])) > 6:
        authors += ", et al."
    published = paper.get("published", "unknown date")
    categories = ", ".join(paper.get("categories", []))
    abstract = " ".join(str(paper.get("abstract", "")).split())
    if len(abstract) > 900:
        abstract = abstract[:897].rstrip() + "..."
    reasons = ", ".join(paper.get("score_reasons", []))
    comment = paper.get("comment")
    comment_line = f"  - Comment: {comment}\n" if comment else ""
    return (
        f"- **{title}** - score `{paper.get('score', 0)}` - {published}\n"
        f"  - Authors: {authors}\n"
        f"  - Categories: {categories}\n"
        f"  - Abstract: {abstract}\n"
        f"{comment_line}"
        f"  - arXiv: {paper.get('abstract_url')}\n"
        f"  - PDF: {paper.get('pdf_url')}\n"
        f"  - Reasons: {reasons}"
    )
