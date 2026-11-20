"""Nightly numbers for every tracked repo via GraphQL (PLAN.md 6.4).

Only numbers and names are fetched: stars, forks, open issues, last push, archived, and the
current owner/name (repos get renamed). Descriptions, topics and licences come from discover's
search results, which already carry them. GitHub stops a GraphQL query after about 10 seconds;
measured 2026-11-20, 100 repos with this query take 3-5 s while the full metadata query timed
out (502) at 100 and took 10 s at 50. A batch that still fails with 5xx is split in half.
"""

from __future__ import annotations

from collections.abc import Callable

from pipeline.github import GitHubClient, GitHubError

BATCH = 100

QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Repository {
      databaseId
      nameWithOwner
      stargazerCount
      forkCount
      isArchived
      pushedAt
      issues(states: OPEN) { totalCount }
    }
  }
}
"""

# Columns written by db.apply_refresh, besides id.
FIELDS = ("owner", "name", "stars", "forks", "open_issues", "pushed_at", "archived")


def node_to_update(node: dict) -> dict:
    """GraphQL Repository -> the column values db.apply_refresh writes."""
    owner, name = node["nameWithOwner"].split("/", 1)
    return {
        "id": int(node["databaseId"]),
        "owner": owner,
        "name": name,
        "stars": int(node["stargazerCount"]),
        "forks": int(node["forkCount"]),
        "open_issues": int(node["issues"]["totalCount"]),
        "pushed_at": node.get("pushedAt"),
        "archived": bool(node["isArchived"]),
    }


def _fetch_batch(client: GitHubClient, batch: list[tuple[int, str]]) -> list[dict | None]:
    try:
        return client.graphql(QUERY, {"ids": [node_id for _, node_id in batch]}, retries=1)["nodes"]
    except GitHubError as err:
        if err.status < 500 or len(batch) == 1:
            raise
        half = len(batch) // 2
        return _fetch_batch(client, batch[:half]) + _fetch_batch(client, batch[half:])


def fetch(
    client: GitHubClient, targets: list[tuple[int, str]], log: Callable[[str], None] | None = None
) -> tuple[list[dict], list[int]]:
    """Return (updates, missing ids). Missing = deleted, made private, or blocked on GitHub."""
    updates, missing = [], []
    for start in range(0, len(targets), BATCH):
        batch = targets[start : start + BATCH]
        for (rid, _), node in zip(batch, _fetch_batch(client, batch), strict=True):
            if not node or "databaseId" not in node:
                missing.append(rid)
            else:
                updates.append(node_to_update(node))
        if log and (start // BATCH) % 20 == 0:
            log(f"refresh: {min(start + BATCH, len(targets))}/{len(targets)}")
    return updates, missing


def keep_old_name_on_collision(updates: list[dict], rows: list[tuple[int, str, str]]) -> int:
    """Keep a repo's stored name when its new name already belongs to another tracked repo.

    `rows` is (id, owner, name) for every row in `repos`. Returns how many were kept back;
    the clash resolves itself once the other repo disappears from GitHub.
    """
    current = {rid: (owner, name) for rid, owner, name in rows}
    taken = {(owner.lower(), name.lower()): rid for rid, owner, name in rows}
    kept = 0
    for u in updates:
        holder = taken.get((u["owner"].lower(), u["name"].lower()))
        if holder is not None and holder != u["id"] and u["id"] in current:
            u["owner"], u["name"] = current[u["id"]]
            kept += 1
    return kept
