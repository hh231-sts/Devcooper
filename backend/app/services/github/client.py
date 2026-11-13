from collections.abc import Callable
from typing import Any

import httpx

from app.core.config import settings


class PaginatedItems(list[dict]):
    """List-compatible result with coverage information for repository sync."""

    def __init__(self, items: list[dict], *, complete: bool, pages: int) -> None:
        super().__init__(items)
        self.complete = complete
        self.pages = pages


class GitHubClient:
    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        self.token = token or settings.github_token
        self.base_url = (base_url or settings.github_api_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
            response.raise_for_status()
            return response.json()

    async def get_paginated(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        item_key: str | None = None,
        limit: int | None = None,
        include: Callable[[dict], bool] | None = None,
    ) -> PaginatedItems:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive or None")
        query = dict(params or {})
        query["per_page"] = min(limit or 100, 100)
        url = f"{self.base_url}{path}"
        items: list[dict] = []
        visited: set[str] = set()
        pages = 0
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            while url:
                if url in visited:
                    raise ValueError("GitHub pagination returned a repeated next page")
                # Never forward the repository token to a different API origin.
                if httpx.URL(url).scheme != httpx.URL(self.base_url).scheme or httpx.URL(url).netloc != httpx.URL(self.base_url).netloc:
                    raise ValueError("GitHub pagination returned a different API origin")
                visited.add(url)
                response = await client.get(url, headers=self._headers(), params=query)
                response.raise_for_status()
                payload = response.json()
                batch = payload.get(item_key) if item_key and isinstance(payload, dict) else payload
                if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                    raise ValueError("GitHub list response has an unexpected shape")
                pages += 1
                accepted = [item for item in batch if include is None or include(item)]
                next_url = response.links.get("next", {}).get("url")
                if limit is not None and len(items) + len(accepted) >= limit:
                    remaining = limit - len(items)
                    items.extend(accepted[:remaining])
                    return PaginatedItems(items, complete=not next_url and len(accepted) <= remaining, pages=pages)
                items.extend(accepted)
                url = str(response.url.join(next_url)) if next_url else ""
                query = None  # The Link URL already contains its pagination parameters.
        return PaginatedItems(items, complete=True, pages=pages)

    async def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
            response.raise_for_status()
            return response.content

    async def post(self, path: str, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.post(f"{self.base_url}{path}", headers=self._headers(), json=payload)
            response.raise_for_status()
            return response.json()

    async def patch(self, path: str, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.patch(f"{self.base_url}{path}", headers=self._headers(), json=payload)
            response.raise_for_status()
            return response.json()

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return await self.get(f"/repos/{owner}/{repo}")

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues/{issue_number}/comments", {"body": body})

    async def create_issue(self, owner: str, repo: str, title: str, body: str) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues", {"title": title, "body": body})

    async def close_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        return await self.patch(f"/repos/{owner}/{repo}/issues/{issue_number}", {"state": "closed"})

    async def add_issue_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues/{issue_number}/labels", {"labels": labels})
