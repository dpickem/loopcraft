"""X (Twitter) API client with bounded, retrying HTTP fetches."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from loopcraft.config import HTTP_USER_AGENT

#: Seconds before an X API HTTP request is abandoned.
_REQUEST_TIMEOUT_S = 30
#: Maximum tenacity attempts per X API request.
_MAX_RETRY_ATTEMPTS = 3
#: X API lower bound for ``max_results`` on recent-search requests.
_MIN_POSTS_PER_SEARCH = 10
#: X API lower bound for ``max_results`` on list-timeline requests.
_MIN_POSTS_PER_LIST = 5
#: X API upper bound for posts returned by one request.
_MAX_POSTS_PER_REQUEST = 100
#: X API upper bound for followed accounts returned by one request.
_MAX_FOLLOWING_PER_REQUEST = 1000
#: X API upper bound for usernames per bulk user lookup.
_MAX_USERS_PER_LOOKUP = 100
#: Maximum error-body characters included in raised messages.
_ERROR_BODY_LIMIT = 500


class XApiError(RuntimeError):
    """Raised when the X API request or response is unusable."""


class XApiClient:
    """Client for the subset of the X v2 API used by the intelligence loop."""

    def __init__(self, bearer_token: str, *, base_url: str = "https://api.x.com/2") -> None:
        """Store the bearer token and API base URL for subsequent requests."""
        self.bearer_token = bearer_token
        self.base_url = base_url.rstrip("/")

    def search_recent(self, query: str, *, since_id: str | None, max_results: int) -> list[dict[str, Any]]:
        """Return recent posts matching ``query`` (author-hydrated).

        Args:
            query: X recent-search query string.
            since_id: Only return posts newer than this id, if given.
            max_results: Desired page size (clamped to the API's limits).

        Returns:
            Post dicts with an ``author`` sub-dict attached.
        """
        params: dict[str, str | int] = {
            "query": query,
            "max_results": min(max(max_results, _MIN_POSTS_PER_SEARCH), _MAX_POSTS_PER_REQUEST),
            "tweet.fields": "author_id,created_at,public_metrics,conversation_id,referenced_tweets,entities",
            "expansions": "author_id",
            "user.fields": "username,name,description,verified,verified_type,affiliation,public_metrics",
        }
        if since_id:
            params["since_id"] = since_id
        return self._get_posts("/tweets/search/recent", params)

    def list_posts(self, list_id: str, *, since_id: str | None, max_results: int) -> list[dict[str, Any]]:
        """Return recent posts from an X list (author-hydrated).

        Args:
            list_id: The X list id to read.
            since_id: Only return posts newer than this id, if given.
            max_results: Desired page size (clamped to the API's limits).

        Returns:
            Post dicts with an ``author`` sub-dict attached.
        """
        params: dict[str, str | int] = {
            "max_results": min(max(max_results, _MIN_POSTS_PER_LIST), _MAX_POSTS_PER_REQUEST),
            "tweet.fields": "author_id,created_at,public_metrics,conversation_id,referenced_tweets,entities",
            "expansions": "author_id",
            "user.fields": "username,name,description,verified,verified_type,affiliation,public_metrics",
        }
        if since_id:
            params["since_id"] = since_id
        return self._get_posts(f"/lists/{list_id}/tweets", params)

    def following_handles(self, user_id: str) -> list[str]:
        """Return the usernames a user follows."""
        return [user["username"] for user in self.following_users(user_id) if user.get("username")]

    def current_user(self) -> dict[str, Any]:
        """Return the authenticated user (``/users/me``).

        Raises:
            XApiError: If the API returns no user (e.g. app-only auth).
        """
        payload = self._get_json("/users/me", {"user.fields": "username,name,description,verified,verified_type"})
        user = payload.get("data")
        if not user:
            raise XApiError("X API did not return a current user for /users/me.")
        return user

    def user_by_username(self, username: str) -> dict[str, Any]:
        """Return one user profile by handle.

        Raises:
            XApiError: If the API returns no user for the handle.
        """
        clean_username = username.lstrip("@")
        payload = self._get_json(
            f"/users/by/username/{clean_username}",
            {"user.fields": "username,name,description,verified,verified_type"},
        )
        user = payload.get("data")
        if not user:
            raise XApiError(f"X API did not return a user for @{clean_username}.")
        return user

    def users_by_usernames(self, usernames: list[str]) -> list[dict[str, Any]]:
        """Return profiles for many handles, batched to the API lookup limit."""
        users: list[dict[str, Any]] = []
        clean_usernames = sorted({username.lower().lstrip("@") for username in usernames if username.strip()})
        for index in range(0, len(clean_usernames), _MAX_USERS_PER_LOOKUP):
            batch = clean_usernames[index : index + _MAX_USERS_PER_LOOKUP]
            if not batch:
                continue
            payload = self._get_json(
                "/users/by",
                {
                    "usernames": ",".join(batch),
                    "user.fields": "username,name,description,verified,verified_type,affiliation,public_metrics",
                },
            )
            users.extend(payload.get("data", []))
        return users

    def following_users(self, user_id: str) -> list[dict[str, Any]]:
        """Return all users ``user_id`` follows, following pagination to the end."""
        users: list[dict[str, Any]] = []
        pagination_token: str | None = None
        while True:
            params: dict[str, str | int] = {
                "max_results": _MAX_FOLLOWING_PER_REQUEST,
                "user.fields": "username,name,description,verified,verified_type,affiliation,public_metrics",
            }
            if pagination_token:
                params["pagination_token"] = pagination_token
            payload = self._get_json(f"/users/{user_id}/following", params)
            users.extend(payload.get("data", []))
            pagination_token = payload.get("meta", {}).get("next_token")
            if not pagination_token:
                return users

    def _get_posts(self, path: str, params: dict[str, str | int]) -> list[dict[str, Any]]:
        """Fetch posts and attach each post's expanded author profile."""
        payload = self._get_json(path, params)
        users = {user["id"]: user for user in payload.get("includes", {}).get("users", []) if user.get("id")}
        posts: list[dict[str, Any]] = []
        for post in payload.get("data", []):
            author = users.get(post.get("author_id"), {})
            enriched = dict(post)
            enriched["author"] = author
            posts.append(enriched)
        return posts

    def _get_json(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        """Perform a bearer-authenticated GET and return the decoded JSON.

        Raises:
            XApiError: On rate limiting, HTTP status errors, or network errors.
        """
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "User-Agent": HTTP_USER_AGENT,
            },
        )
        try:
            return json.loads(_read_response(request).decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                raise XApiError("X API rate limit hit: " + body[:_ERROR_BODY_LIMIT]) from exc
            raise XApiError(f"X API HTTP {exc.code}: {body[:_ERROR_BODY_LIMIT]}") from exc
        except URLError as exc:
            raise XApiError(f"X API network error: {exc}") from exc


def _is_retryable_url_error(exc: BaseException) -> bool:
    """Return whether a lower-level urllib exception should be retried."""
    if isinstance(exc, HTTPError):
        return 500 <= exc.code < 600
    return isinstance(exc, URLError)


@retry(
    retry=retry_if_exception(_is_retryable_url_error),
    stop=stop_after_attempt(_MAX_RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _read_response(request: Request) -> bytes:
    """Read an HTTP response body with retry around transient failures."""
    with urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
        return response.read()
