"""Find Radar candidate repos on GitHub (PLAN.md 6.1 and 6.3).

For every topic and keyword in seeds.yaml we search repositories with at least `min_stars`
stars. GitHub returns at most 1000 results per query, so when a query has more we split it
by star range until each piece fits. Results are merged by GitHub id (stable across renames).

Discover only collects candidates. Deciding which category a repo belongs to is classify's job.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from pipeline.github import SEARCH_PAGE_SIZE, SEARCH_RESULT_CAP, GitHubClient
from pipeline.seeds import Seeds

OFFICIAL_OWNERS = frozenset({"anthropics"})


@dataclass
class Candidate:
    """One repo, with the columns of the `repos` table that search already gives us."""

    id: int
    node_id: str
    owner: str
    name: str
    description: str | None
    homepage: str | None
    language: str | None
    license_spdx: str | None
    topics: list[str]
    default_branch: str | None
    created_at: str
    pushed_at: str | None
    archived: bool
    stars: int
    forks: int
    open_issues: int
    is_official: bool
    matched_by: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class Report:
    requests: int = 0
    per_query: dict[str, int] = field(default_factory=dict)  # label -> repos kept
    truncated: list[str] = field(default_factory=list)  # still > 1000 after splitting
    incomplete: list[str] = field(default_factory=list)  # GitHub timed out part of the search
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count


def candidate_from_item(item: dict) -> Candidate:
    license_info = item.get("license") or {}
    owner = item["owner"]["login"]
    return Candidate(
        id=int(item["id"]),
        node_id=item["node_id"],
        owner=owner,
        name=item["name"],
        description=item.get("description") or None,
        homepage=item.get("homepage") or None,
        language=item.get("language") or None,
        license_spdx=license_info.get("spdx_id") or None,
        topics=sorted(item.get("topics") or []),
        default_branch=item.get("default_branch") or None,
        created_at=item["created_at"],
        pushed_at=item.get("pushed_at"),
        archived=bool(item.get("archived")),
        stars=int(item["stargazers_count"]),
        forks=int(item["forks_count"]),
        open_issues=int(item.get("open_issues_count") or 0),
        is_official=owner.lower() in OFFICIAL_OWNERS,
    )


def base_queries(seeds: Seeds) -> list[tuple[str, str]]:
    """(label, query) pairs. The label is what ends up in Candidate.matched_by."""
    queries = [(f"topic:{t}", f"topic:{t}") for t in seeds.topics]
    queries += [(f"keyword:{k}", f"{k} in:name,description") for k in seeds.keywords]
    return queries


def star_qualifier(lo: int, hi: int | None) -> str:
    return f"stars:>={lo}" if hi is None else f"stars:{lo}..{hi}"


def split_range(lo: int, hi: int | None) -> list[tuple[int, int | None]] | None:
    """Halve a star range. None when it cannot be split any further."""
    if hi is None:
        return [(lo, lo * 2 - 1), (lo * 2, None)]
    if hi > lo:
        mid = (lo + hi) // 2
        return [(lo, mid), (mid + 1, hi)]
    return None


def iter_search(
    client: GitHubClient, query: str, lo: int, hi: int | None, report: Report
) -> Iterator[dict]:
    full = f"{query} {star_qualifier(lo, hi)} fork:false archived:false"
    first = client.search_repositories(full, page=1)
    total = int(first["total_count"])
    if total > SEARCH_RESULT_CAP:
        parts = split_range(lo, hi)
        if parts:
            for part_lo, part_hi in parts:
                yield from iter_search(client, query, part_lo, part_hi, report)
            return
        report.truncated.append(full)
    if first.get("incomplete_results"):
        report.incomplete.append(full)
    yield from first["items"]
    pages = math.ceil(min(total, SEARCH_RESULT_CAP) / SEARCH_PAGE_SIZE)
    for page in range(2, pages + 1):
        items = client.search_repositories(full, page=page)["items"]
        if not items:
            break
        yield from items


def _skip_reason(item: dict, seeds: Seeds) -> str | None:
    # Search qualifiers already filter these; checking again guards against stale index data.
    if item.get("fork"):
        return "fork"
    if item.get("archived"):
        return "archived"
    if int(item["stargazers_count"]) < seeds.min_stars:
        return "below_min_stars"
    if seeds.is_excluded(item["owner"]["login"], item["name"]):
        return "excluded"
    return None


def discover(
    client: GitHubClient, seeds: Seeds, only: set[str] | None = None, log=None
) -> tuple[dict[int, Candidate], Report]:
    """Run every seed query and merge results. `only` limits the run to some labels."""
    report = Report()
    found: dict[int, Candidate] = {}
    for label, query in base_queries(seeds):
        if only and label not in only:
            continue
        kept = 0
        for item in iter_search(client, query, seeds.min_stars, None, report):
            reason = _skip_reason(item, seeds)
            if reason:
                report.skipped[reason] = report.skipped.get(reason, 0) + 1
                continue
            candidate = found.get(int(item["id"]))
            if candidate is None:
                candidate = found[int(item["id"])] = candidate_from_item(item)
            if label not in candidate.matched_by:
                candidate.matched_by.append(label)
                kept += 1
        report.per_query[label] = kept
        if log:
            log(f"{label}: {kept} repos ({len(found)} unique so far)")
    report.requests = client.request_count
    return found, report


def to_json(found: dict[int, Candidate], report: Report, seeds: Seeds) -> dict:
    repos = sorted(found.values(), key=lambda c: (-c.stars, c.full_name.lower()))
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "min_stars": seeds.min_stars,
        "count": len(repos),
        "report": asdict(report),
        "repos": [asdict(c) for c in repos],
    }


def stderr_log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
