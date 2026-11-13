from app.services.github.client import GitHubClient


async def list_issues(owner: str, repo: str, token: str | None = None, limit: int | None = 30, base_url: str | None = None) -> list[dict]:
    client = GitHubClient(token, base_url)
    return await client.get_paginated(
        f"/repos/{owner}/{repo}/issues", {"state": "all"}, limit=limit,
        include=lambda item: "pull_request" not in item,
    )
