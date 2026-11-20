"""The nightly job (PLAN.md 6.10), run by .github/workflows/nightly.yml.

Order: exclusions -> discover -> classify new repos -> refresh -> snapshot -> export.
Each step commits on its own, so a late failure never loses the day's star snapshot.
Every run is logged in `pipeline_runs`; "7 nights in a row without error" is read from there.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from pipeline import classify, db, discover, export, refresh
from pipeline.classify import Match, Result
from pipeline.github import GitHubClient
from pipeline.seeds import Seeds


def result_from_dict(data: dict) -> Result:
    return Result(
        full_name=data["full_name"],
        status=data["status"],
        reason=data["reason"],
        categories=[Match(**m) for m in data["categories"]],
        flags=list(data["flags"]),
        is_docs=data["is_docs"],
        tree_truncated=data.get("tree_truncated", False),
    )


def in_transaction(connect: Callable, fn: Callable, *args):
    """Run fn(conn, *args) on a fresh connection and commit.

    Neon's free plan suspends the database after 5 idle minutes and drops open connections,
    while GitHub calls can take 10 minutes. So no connection is ever held across GitHub work.
    """
    conn = connect()
    try:
        result = fn(conn, *args)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def classify_repos(
    client: GitHubClient, repos: list[dict], log: Callable[[str], None]
) -> tuple[list[tuple[int, Result]], int]:
    done, errors = [], 0
    for repo in repos:
        try:
            paths, deps, truncated = classify.gather_signals(client, repo)
        except Exception as err:  # one bad repo must not stop the night; retried tomorrow
            errors += 1
            log(f"classify: {repo['owner']}/{repo['name']} skipped: {err}")
            continue
        done.append((repo["id"], classify.classify(repo, paths, deps, truncated)))
    return done, errors


def refresh_all(connect: Callable, client: GitHubClient, log: Callable[[str], None]) -> dict:
    """Refresh every tracked repo; GitHub work happens with no database connection open."""
    targets = in_transaction(connect, db.refresh_targets)
    updates, missing = refresh.fetch(client, targets, log=log)

    def write(conn):
        rows = conn.execute("SELECT id, owner, name FROM repos").fetchall()
        kept = refresh.keep_old_name_on_collision(updates, rows)
        db.apply_refresh(conn, updates, missing)
        return kept

    kept = in_transaction(connect, write)
    return {"refreshed": len(updates), "missing": len(missing), "rename_kept": kept}


def run(
    connect: Callable,
    client: GitHubClient,
    seeds: Seeds,
    out_dir: Path,
    *,
    do_discover: bool = True,
    classify_limit: int = 500,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    """`connect` returns a new database connection each time it is called."""
    now = now or datetime.now(UTC)
    stats: dict = {}
    run_id = in_transaction(connect, db.start_run, "nightly")
    try:
        stats["excluded"] = in_transaction(connect, db.apply_exclusions, seeds)

        if do_discover:
            found, report = discover.discover(client, seeds, log=log)
            inserted, _, skipped = in_transaction(
                connect, db.upsert_repos, [asdict(c) for c in found.values()]
            )
            stats.update(
                discovered=len(found),
                new_repos=inserted,
                name_clashes=len(skipped),
                discover_truncated=len(report.truncated),
            )
            log(f"discover: {len(found)} found, {inserted} new")

        repos = in_transaction(connect, db.repos_to_classify, classify_limit)
        done, errors = classify_repos(client, repos, log)
        in_transaction(connect, db.save_classification, done)
        statuses: dict[str, int] = {}
        for _, res in done:
            statuses[res.status] = statuses.get(res.status, 0) + 1
        stats.update(classified=len(done), classify_errors=errors, classify_status=statuses)
        log(f"classify: {len(done)} repos")

        stats.update(refresh_all(connect, client, log))
        log(f"refresh: {stats['refreshed']} updated, {stats['missing']} missing")

        stats["snapshot_rows"] = in_transaction(connect, db.snapshot, now.date())

        records, skipped = in_transaction(connect, db.records_for_export, now.date())
        docs = export.build(records, now=now)
        export.write(docs, out_dir)
        stats.update(exported=len(records), export_files=len(docs), export_skipped=len(skipped))
        log(f"export: {len(records)} repos, {len(docs)} files")

        stats["github_requests"] = client.request_count
        in_transaction(connect, db.finish_run, run_id, True, stats)
        return stats
    except Exception as err:
        stats["error"] = "".join(traceback.format_exception_only(err)).strip()[:500]
        stats["github_requests"] = client.request_count
        try:
            in_transaction(connect, db.finish_run, run_id, False, stats)
        except Exception as log_err:  # never hide the original failure
            log(f"could not record the failed run: {log_err}")
        raise
