from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import ArxivIntelConfig


ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


class ArxivApiError(RuntimeError):
    pass


class ArxivClient:
    def __init__(self, *, base_url: str = "https://export.arxiv.org/api/query") -> None:
        self.base_url = base_url

    def search_recent(self, config: ArxivIntelConfig) -> list[dict[str, Any]]:
        params = {
            "search_query": build_search_query(config),
            "start": 0,
            "max_results": max(1, min(config.ranking.max_results, 2000)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        payload = self._get(params)
        return parse_feed(payload)

    def _get(self, params: dict[str, Any]) -> bytes:
        url = f"{self.base_url}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "loopcraft-arxiv-intel/0.1"})
        for attempt in range(3):
            try:
                with urlopen(request, timeout=30) as response:
                    return response.read()
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if 500 <= exc.code < 600 and attempt < 2:
                    time.sleep(3)
                    continue
                raise ArxivApiError(f"arXiv API HTTP {exc.code}: {body[:500]}") from exc
            except URLError as exc:
                if attempt < 2:
                    time.sleep(3)
                    continue
                raise ArxivApiError(f"arXiv API network error: {exc}") from exc
        raise ArxivApiError("arXiv API request failed after retries")


def build_search_query(config: ArxivIntelConfig) -> str:
    category_query = " OR ".join(f"cat:{category}" for category in config.sources.categories)
    term_query = " OR ".join(_term_query(term) for term in config.sources.search_terms)
    if category_query and term_query:
        return f"({category_query}) AND ({term_query})"
    return category_query or term_query or "cat:cs.AI"


def parse_feed(payload: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    papers: list[dict[str, Any]] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        paper_id = _text(entry, "id").rsplit("/", 1)[-1]
        links = _links(entry)
        papers.append(
            {
                "id": paper_id,
                "title": _clean(_text(entry, "title")),
                "abstract": _clean(_text(entry, "summary")),
                "published": _parse_datetime(_text(entry, "published")),
                "updated": _parse_datetime(_text(entry, "updated")),
                "authors": [_text(author, "name") for author in entry.findall(f"{ATOM_NS}author")],
                "categories": [category.attrib.get("term", "") for category in entry.findall(f"{ATOM_NS}category")],
                "primary_category": _primary_category(entry),
                "abstract_url": links.get("alternate", f"https://arxiv.org/abs/{paper_id}"),
                "pdf_url": links.get("pdf", f"https://arxiv.org/pdf/{paper_id}"),
                "comment": _extension_text(entry, "comment"),
                "journal_ref": _extension_text(entry, "journal_ref"),
                "doi": _extension_text(entry, "doi"),
            }
        )
    return papers


def _term_query(term: str) -> str:
    escaped = term.replace('"', "")
    if " " in escaped or "-" in escaped:
        return f'all:"{escaped}"'
    return f"all:{escaped}"


def _links(entry: ET.Element) -> dict[str, str]:
    links: dict[str, str] = {}
    for link in entry.findall(f"{ATOM_NS}link"):
        rel = link.attrib.get("rel", "")
        title = link.attrib.get("title", "")
        href = link.attrib.get("href", "")
        if rel == "alternate":
            links["alternate"] = href
        if title == "pdf":
            links["pdf"] = href
    return links


def _primary_category(entry: ET.Element) -> str:
    primary = entry.find(f"{ARXIV_NS}primary_category")
    return primary.attrib.get("term", "") if primary is not None else ""


def _text(entry: ET.Element, tag: str) -> str:
    child = entry.find(f"{ATOM_NS}{tag}")
    return child.text or "" if child is not None else ""


def _extension_text(entry: ET.Element, tag: str) -> str | None:
    child = entry.find(f"{ARXIV_NS}{tag}")
    if child is None or not child.text:
        return None
    return _clean(child.text)


def _parse_datetime(value: str) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except ValueError:
        return value


def _clean(value: str) -> str:
    return " ".join(value.split())

