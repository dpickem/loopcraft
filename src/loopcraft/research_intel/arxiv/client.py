"""arXiv Atom API client with bounded, retrying HTTP fetches."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from loopcraft.config import HTTP_USER_AGENT
from loopcraft.research_intel.arxiv.config import ArxivIntelConfig

#: XML namespace prefix for Atom feed elements in arXiv responses.
ATOM_NS = "{http://www.w3.org/2005/Atom}"
#: XML namespace prefix for arXiv-specific feed extensions.
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
#: Seconds before an arXiv HTTP request is abandoned.
_REQUEST_TIMEOUT_S = 30
#: Maximum tenacity attempts per arXiv request.
_MAX_RETRY_ATTEMPTS = 3
#: Fixed wait between arXiv retry attempts, in seconds.
_RETRY_WAIT_S = 3
#: Hard cap on ``max_results`` accepted by the arXiv export API.
_MAX_RESULTS_UPPER_BOUND = 2000
#: Fallback search query when the content config declares no categories/terms.
_DEFAULT_SEARCH_QUERY = "cat:cs.AI"
#: Maximum error-body characters included in raised messages.
_ERROR_BODY_LIMIT = 500


class ArxivApiError(RuntimeError):
    """Raised when the arXiv API request or response is unusable."""


class ArxivClient:
    """Thin client over the arXiv Atom export API."""

    def __init__(self, *, base_url: str = "https://export.arxiv.org/api/query") -> None:
        """Store the API base URL used for search requests."""
        self.base_url = base_url

    def search_recent(self, config: ArxivIntelConfig) -> list[dict[str, Any]]:
        """Fetch and parse the most recent papers for the configured query.

        Args:
            config: arXiv content config (categories, search terms, max results).

        Returns:
            Parsed paper metadata dicts, newest submission first.

        Raises:
            ArxivApiError: On an HTTP or network failure.
        """
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
        """Perform the GET request and translate urllib errors to ArxivApiError.

        Raises:
            ArxivApiError: On an HTTP status error or network failure.
        """
        url = f"{self.base_url}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": HTTP_USER_AGENT})
        try:
            return _read_response(request)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ArxivApiError(f"arXiv API HTTP {exc.code}: {body[:_ERROR_BODY_LIMIT]}") from exc
        except URLError as exc:
            raise ArxivApiError(f"arXiv API network error: {exc}") from exc


def build_search_query(config: ArxivIntelConfig) -> str:
    """Build the arXiv ``search_query`` string from categories and terms.

    Args:
        config: arXiv content config providing categories and search terms.

    Returns:
        A combined ``(cat:...) AND (all:...)`` query, or a safe default when the
        config declares neither categories nor terms.
    """
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
    """Parse an arXiv Atom feed into a list of paper metadata dicts.

    Args:
        payload: Raw Atom XML bytes returned by the API.

    Returns:
        One dict per ``<entry>`` with id, title, abstract, links, and metadata.
    """
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
    """Return an ``all:`` field query for one term, quoting multi-word terms."""
    escaped = term.replace('"', "")
    if " " in escaped or "-" in escaped:
        return f'all:"{escaped}"'
    return f"all:{escaped}"


def _links(entry: ET.Element) -> dict[str, str]:
    """Extract the alternate (abstract) and PDF links from an entry."""
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
    """Return the arXiv primary category term, or empty string if absent."""
    primary = entry.find(f"{ARXIV_NS}primary_category")
    return primary.attrib.get("term", "") if primary is not None else ""


def _text(entry: ET.Element, tag: str) -> str:
    """Return the text of an Atom child tag, or empty string if missing."""
    child = entry.find(f"{ATOM_NS}{tag}")
    return child.text or "" if child is not None else ""


def _extension_text(entry: ET.Element, tag: str) -> str | None:
    """Return cleaned text of an arXiv-extension tag, or None if missing."""
    child = entry.find(f"{ARXIV_NS}{tag}")
    if child is None or not child.text:
        return None
    return _clean(child.text)


def _parse_datetime(value: str) -> str | None:
    """Normalize an ISO timestamp to UTC ISO format; pass through on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except ValueError:
        return value


def _clean(value: str) -> str:
    """Collapse runs of whitespace in ``value`` to single spaces."""
    return " ".join(value.split())

