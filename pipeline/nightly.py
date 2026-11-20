"""The nightly job (PLAN.md 6.10), run by .github/workflows/nightly.yml.

Order: exclusions -> discover -> classify new repos -> refresh -> snapshot -> summaries
-> export. Summaries are optional: when the AI quota runs out the step stops for the night
and the rest of the job carries on.
Each step commits on its own, so a late failure never loses the day's star snapshot.
Every run is logged in `pipeline_runs`; "7 nights in a row without error" is read from there.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from pipeline import classify, db, discover, enrich, export, news, refresh
from pipeline import news_sources as ns
from pipeline.classify import Match, Result
from pipeline.github import GitHubClient, GitHubError
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


def enrich_new(
    connect: Callable,
    client: GitHubClient,
    provider,
    limit: int,
    max_readme_chars: int,
    log: Callable[[str], None],
    give_up_after: int = 3,
) -> dict:
    """Summaries for active repos that have none. Each result is saved as soon as it exists."""
    repos = in_transaction(connect, db.repos_to_enrich, limit)
    done = no_readme = failed = in_a_row = 0
    stopped = False
    for repo in repos:
        name = f"{repo['owner']}/{repo['name']}"
        try:
            readme = enrich.fetch_readme(client, repo["owner"], repo["name"])
        except GitHubError as err:  # one unreadable README never stops the night
            failed += 1
            log(f"enrich: {name} README unavailable: {err}")
            continue
        if readme is None or not readme.text.strip():
            in_transaction(connect, db.mark_no_readme, repo["id"])
            no_readme += 1
            continue
        try:
            result = enrich.enrich(provider, repo, readme, max_readme_chars)
        except enrich.EnrichError as err:
            failed += 1
            in_a_row += 1
            log(f"enrich: {name} failed: {err}")
            if in_a_row >= give_up_after:  # most likely the daily AI quota: try tomorrow
                stopped = True
                log(f"enrich: {give_up_after} failures in a row, stopping for tonight")
                break
            continue
        in_a_row = 0
        in_transaction(connect, db.save_enrichment, repo["id"], result)
        done += 1
    return {
        "enriched": done,
        "enrich_failed": failed,
        "enrich_no_readme": no_readme,
        "enrich_stopped_early": stopped,
    }


def news_step(
    connect: Callable,
    client: GitHubClient,
    provider,
    now: datetime,
    log: Callable[[str], None],
    summary_limit: int = 30,
    fetch_text: Callable[[str], str] = ns.fetch_text,
) -> dict:
    """Fetch official news, store what is new, summarise pending items (PLAN.md 8.2)."""
    config = ns.load_config()

    def releases(repo: str) -> list[dict]:
        return client.get(f"/repos/{repo}/releases", {"per_page": 30})

    def prepare(conn):
        news.sync_sources(conn, config)
        return news.known_urls(conn)

    known = in_transaction(connect, prepare)
    fresh, ignored, runs = news.fetch_new(config, known, fetch_text, releases, now, log)

    def store(conn):
        added = news.store_items(conn, fresh, "pending")
        news.store_items(conn, ignored, "hidden")
        news.record_runs(conn, runs, now)
        return added

    stats = {
        "news_new": in_transaction(connect, store),
        "news_failed_sources": [r.source_id for r in runs if not r.ok],
    }
    summarised = title_only = failed = 0
    rejected: dict[str, list[str]] = {}
    if provider is not None and summary_limit > 0:
        for nid, title, excerpt, dedup, tags in in_transaction(
            connect, news.pending, summary_limit
        ):
            try:
                result = news.summarise(provider, title, "release" if dedup else "news", excerpt)
            except Exception as err:  # quota or a bad answer: the item stays pending for later
                failed += 1
                log(f"news: {nid} not summarised: {err}")
                if failed >= 3:
                    break
                continue
            in_transaction(connect, news.save_summary, nid, result, provider, list(tags))
            summarised += 1
            title_only += 0 if result.check_passed else 1
            if not result.check_passed:
                rejected[nid] = result.problems[:3]
                log(f"news: {nid} title only, safety net: {result.problems[:3]}")
    stats.update(news_summarised=summarised, news_title_only=title_only, news_summary_failed=failed)
    if rejected:  # kept in pipeline_runs.stats so a hand check can see why
        stats["news_rejected"] = rejected
    return stats


def export_all(connect: Callable, out_dir: Path, now: datetime) -> dict:
    """Radar, reference, templates and news from the database, as one site (Pages replaces the
    whole site on every deploy, so every job writes every file)."""
    records, skipped = in_transaction(connect, db.records_for_export, now.date())
    docs = export.build(records, now=now)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    docs.update(export.reference_docs(stamp))
    trending = docs["trending.json"]["items"]
    docs.update(in_transaction(connect, news.news_docs, stamp, trending))
    export.write(docs, out_dir)
    return {"exported": len(records), "export_files": len(docs), "export_skipped": len(skipped)}


def run_official(
    connect: Callable,
    client: GitHubClient,
    out_dir: Path,
    *,
    provider=None,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
    summaries_skipped: str | None = None,
) -> dict:
    """The 3-hourly job (official.yml): news first, then re-export everything."""
    now = now or datetime.now(UTC)
    stats: dict = {"summaries_skipped": summaries_skipped} if summaries_skipped else {}
    run_id = in_transaction(connect, db.start_run, "official")
    try:
        stats.update(news_step(connect, client, provider, now, log))
        stats.update(export_all(connect, out_dir, now))
        stats["github_requests"] = client.request_count
        in_transaction(connect, db.finish_run, run_id, True, stats)
        return stats
    except Exception as err:
        stats["error"] = "".join(traceback.format_exception_only(err)).strip()[:500]
        try:
            in_transaction(connect, db.finish_run, run_id, False, stats)
        except Exception as log_err:
            log(f"could not record the failed run: {log_err}")
        raise


def run(
    connect: Callable,
    client: GitHubClient,
    seeds: Seeds,
    out_dir: Path,
    *,
    do_discover: bool = True,
    classify_limit: int = 500,
    provider=None,
    enrich_limit: int = 150,
    max_readme_chars: int = 30000,
    do_news: bool = True,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
    summaries_skipped: str | None = None,
) -> dict:
    """`connect` returns a new database connection each time it is called."""
    now = now or datetime.now(UTC)
    stats: dict = {"summaries_skipped": summaries_skipped} if summaries_skipped else {}
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

        if provider is not None and enrich_limit > 0:
            stats.update(enrich_new(connect, client, provider, enrich_limit, max_readme_chars, log))
            log(f"enrich: {stats['enriched']} summaries")

        if do_news:
            stats.update(news_step(connect, client, provider, now, log))
            log(f"news: {stats['news_new']} new, {stats['news_summarised']} summarised")

        stats.update(export_all(connect, out_dir, now))
        log(f"export: {stats['exported']} repos, {stats['export_files']} files")

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
