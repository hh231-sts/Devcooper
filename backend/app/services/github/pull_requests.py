from app.services.github.client import GitHubClient


async def list_pull_requests(owner: str, repo: str, token: str | None = None, limit: int | None = 30, base_url: str | None = None) -> list[dict]:
    client = GitHubClient(token, base_url)
    return await client.get_paginated(f"/repos/{owner}/{repo}/pulls", {"state": "all"}, limit=limit)


async def list_pull_request_files(owner: str, repo: str, number: int, token: str | None = None, base_url: str | None = None) -> list[dict]:
    client = GitHubClient(token, base_url)
    return await client.get_paginated(f"/repos/{owner}/{repo}/pulls/{number}/files")


async def list_pull_request_review_comments(owner: str, repo: str, number: int, token: str | None = None, base_url: str | None = None) -> list[dict]:
    client = GitHubClient(token, base_url)
    return await client.get_paginated(f"/repos/{owner}/{repo}/pulls/{number}/comments")
