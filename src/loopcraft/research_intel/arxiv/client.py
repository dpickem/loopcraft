"""arXiv Atom API client with bounded, retrying HTTP fetches."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from loopcraft.research_intel.arxiv.config import ArxivIntelConfig


ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
_USER_AGENT = "loopcraft-arxiv-intel/0.1"
_REQUEST_TIMEOUT_S = 30
_MAX_RETRY_ATTEMPTS = 3
_RETRY_WAIT_S = 3
_MAX_RESULTS_UPPER_BOUND = 2000
_DEFAULT_SEARCH_QUERY = "cat:cs.AI"
_ERROR_BODY_LIMIT = 500


class ArxivApiError(RuntimeError):
    """Raised when the arXiv API request or response is unusable."""


class ArxivClient:
    def __init__(self, *, base_url: str = "https://export.arxiv.org/api/query") -> None:
        self.base_url = base_url

    def search_recent(self, config: ArxivIntelConfig) -> list[dict[str, Any]]:
        params = {
            "search_query": build_search_query(config),
            "start": 0,
            "max_results": max(1, min(config.ranking.max_results, _MAX_RESULTS_UPPER_BOUND)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        payload = self._get(params)
        return parse_feed(payload)

    def _get(self, params: dict[str, Any]) -> bytes:
        url = f"{self.base_url}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            return _read_response(request)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ArxivApiError(f"arXiv API HTTP {exc.code}: {body[:_ERROR_BODY_LIMIT]}") from exc
        except URLError as exc:
            raise ArxivApiError(f"arXiv API network error: {exc}") from exc


def build_search_query(config: ArxivIntelConfig) -> str:
    category_query = " OR ".join(f"cat:{category}" for category in config.sources.categories)
    term_query = " OR ".join(_term_query(term) for term in config.sources.search_terms)
    if category_query and term_query:
        return f"({category_query}) AND ({term_query})"
    return category_query or term_query or _DEFAULT_SEARCH_QUERY


def _is_retryable_url_error(exc: BaseException) -> bool:
    """Return whether urllib raised a retryable API/network exception."""
    if isinstance(exc, HTTPError):
        return 500 <= exc.code < 600
    return isinstance(exc, URLError)


@retry(
    retry=retry_if_exception(_is_retryable_url_error),
    stop=stop_after_attempt(_MAX_RETRY_ATTEMPTS),
    wait=wait_fixed(_RETRY_WAIT_S),
    reraise=True,
)
def _read_response(request: Request) -> bytes:
    """Read an arXiv response body with bounded retries around transient errors."""
    with urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
        return response.read()


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

