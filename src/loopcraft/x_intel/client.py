from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class XApiError(RuntimeError):
    pass


class XApiClient:
    def __init__(self, bearer_token: str, *, base_url: str = "https://api.x.com/2") -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url.rstrip("/")

    def search_recent(self, query: str, *, since_id: str | None, max_results: int) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "query": query,
            "max_results": min(max(max_results, 10), 100),
            "tweet.fields": "author_id,created_at,public_metrics,conversation_id,referenced_tweets,entities",
            "expansions": "author_id",
            "user.fields": "username,name,description,verified,verified_type,affiliation,public_metrics",
        }
        if since_id:
            params["since_id"] = since_id
        return self._get_posts("/tweets/search/recent", params)

    def list_posts(self, list_id: str, *, since_id: str | None, max_results: int) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "max_results": min(max(max_results, 5), 100),
            "tweet.fields": "author_id,created_at,public_metrics,conversation_id,referenced_tweets,entities",
            "expansions": "author_id",
            "user.fields": "username,name,description,verified,verified_type,affiliation,public_metrics",
        }
        if since_id:
            params["since_id"] = since_id
        return self._get_posts(f"/lists/{list_id}/tweets", params)

    def following_handles(self, user_id: str) -> list[str]:
        return [user["username"] for user in self.following_users(user_id) if user.get("username")]

    def current_user(self) -> dict[str, Any]:
        payload = self._get_json("/users/me", {"user.fields": "username,name,description,verified,verified_type"})
        user = payload.get("data")
        if not user:
            raise XApiError("X API did not return a current user for /users/me.")
        return user

    def user_by_username(self, username: str) -> dict[str, Any]:
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
        users: list[dict[str, Any]] = []
        clean_usernames = sorted({username.lower().lstrip("@") for username in usernames if username.strip()})
        for index in range(0, len(clean_usernames), 100):
            batch = clean_usernames[index : index + 100]
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
        users: list[dict[str, Any]] = []
        pagination_token: str | None = None
        while True:
            params: dict[str, str | int] = {
                "max_results": 1000,
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
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "User-Agent": "loopcraft-x-intel/0.1",
            },
        )
        for attempt in range(3):
            try:
                with urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429:
                    raise XApiError("X API rate limit hit: " + body[:500]) from exc
                if 500 <= exc.code < 600 and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise XApiError(f"X API HTTP {exc.code}: {body[:500]}") from exc
            except URLError as exc:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise XApiError(f"X API network error: {exc}") from exc
        raise XApiError("X API request failed after retries")
