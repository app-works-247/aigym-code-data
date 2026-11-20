"""A fake GitHub search API over an in-memory list of repos, for discover tests."""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping

from pipeline.github import Response

_STARS_RE = re.compile(r"stars:(?:>=(\d+)|(\d+)\.\.(\d+))")
_TOPIC_RE = re.compile(r"topic:([a-z0-9-]+)")


def make_repo(
    repo_id: int,
    stars: int,
    topics: list[str],
    owner: str = "someone",
    name: str | None = None,
    **extra,
) -> dict:
    repo = {
        "id": repo_id,
        "node_id": f"R_{repo_id}",
        "name": name or f"repo-{repo_id}",
        "owner": {"login": owner},
        "description": f"Repo {repo_id}",
        "homepage": "",
        "language": "Python",
        "license": {"spdx_id": "MIT"},
        "topics": topics,
        "default_branch": "main",
        "created_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-11-01T00:00:00Z",
        "archived": False,
        "fork": False,
        "stargazers_count": stars,
        "forks_count": 1,
        "open_issues_count": 0,
    }
    repo.update(extra)
    return repo


class FakeSearch:
    """Answers /search/repositories like GitHub does, including the 1000-result cap."""

    def __init__(self, repos: list[dict], stale_ids: frozenset[int] = frozenset()) -> None:
        self.repos = repos
        self.stale_ids = stale_ids  # returned even if they no longer match (stale index)
        self.queries: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> Response:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        q = params["q"][0]
        page = int(params["page"][0])
        per_page = int(params["per_page"][0])
        self.queries.append(q)

        topic = _TOPIC_RE.search(q).group(1)
        m = _STARS_RE.search(q)
        lo = int(m.group(1) or m.group(2))
        hi = int(m.group(3)) if m.group(3) else None

        def matches(r: dict) -> bool:
            if r["id"] in self.stale_ids:
                return topic in r["topics"]
            in_range = r["stargazers_count"] >= lo and (hi is None or r["stargazers_count"] <= hi)
            return topic in r["topics"] and in_range and not r["fork"] and not r["archived"]

        hits = sorted((r for r in self.repos if matches(r)), key=lambda r: -r["stargazers_count"])
        visible = hits[:1000]
        items = visible[(page - 1) * per_page : page * per_page]
        body = {"total_count": len(hits), "incomplete_results": False, "items": items}
        return Response(200, {}, json.dumps(body).encode())
