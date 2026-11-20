"""Command line entry point: `python -m pipeline <command>`.

Commands: check-env, seeds-check, migrate, discover, classify, export, load, refresh, nightly,
official, enrich, enrich-trial, reference-check, reference-draft, docs-watch.
Still to come: the LLM step of classify.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pipeline import __version__

# Names only. Values are never printed, so the output is safe to paste into an issue or CI log.
REQUIRED_ENV = (
    "GITHUB_TOKEN",
    "DATABASE_URL",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
)
OPTIONAL_ENV = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "DATABASE_URL_MIGRATE",
    "BACKUP_PASSPHRASE",
)


def load_dotenv(path: Path, environ: dict[str, str] | None = None) -> int:
    """Read KEY=value lines from a local .env file into the environment.

    Variables already set (for example by GitHub Actions secrets) always win. Returns how many
    were added. Only for local runs; CI never has a .env file.
    """
    env = os.environ if environ is None else environ
    if not path.is_file():
        return 0
    added = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and not env.get(key):
            env[key] = value
            added += 1
    return added


def check_env(environ: dict[str, str] | None = None) -> dict[str, bool]:
    """Report which known variables are set (non-empty). Never returns the values."""
    env = os.environ if environ is None else environ
    return {name: bool(env.get(name)) for name in REQUIRED_ENV + OPTIONAL_ENV}


def _cmd_check_env(_: argparse.Namespace) -> int:
    status = check_env()
    for name in REQUIRED_ENV + OPTIONAL_ENV:
        kind = "required" if name in REQUIRED_ENV else "optional"
        mark = "set" if status[name] else "missing"
        print(f"{name:<24} {kind:<9} {mark}")
    missing = [n for n in REQUIRED_ENV if not status[n]]
    return 1 if missing else 0


def _cmd_seeds_check(_: argparse.Namespace) -> int:
    from pipeline import seeds

    s = seeds.load()
    print(
        f"seeds.yaml OK: min_stars={s.min_stars}, {len(s.topics)} topics, "
        f"{len(s.keywords)} keywords, {len(s.code_search)} code searches, "
        f"{len(s.exclude_repos) + len(s.exclude_owners)} exclusions"
    )
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from pipeline import migrate

    migrations = migrate.load_migrations()
    if args.check:
        for m in migrations:
            print(f"{m.name}  {m.checksum[:12]}")
        print(f"{len(migrations)} migration files OK")
        return 0
    url = os.environ.get("DATABASE_URL_MIGRATE")
    if not url:
        print("DATABASE_URL_MIGRATE is not set (owner connection, used only for migrations)")
        return 1
    db = migrate.PsycopgDatabase(url)
    try:
        done = migrate.run(db, migrations, dry_run=args.dry_run)
    finally:
        db.close()
    verb = "would apply" if args.dry_run else "applied"
    for m in done:
        print(f"{verb} {m.name}")
    print(
        f"{len(done)} migration(s) {verb}; database is up to date"
        if not args.dry_run
        else f"{len(done)} migration(s) pending"
    )
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    from pipeline import discover, seeds
    from pipeline.github import GitHubClient

    s = seeds.load()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        discover.stderr_log("GITHUB_TOKEN not set: using the anonymous limit (10 searches/min)")
    client = GitHubClient(token=token)
    found, report = discover.discover(
        client, s, only=set(args.only) if args.only else None, log=discover.stderr_log
    )
    result = discover.to_json(found, report, s)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    discover.stderr_log(
        f"{result['count']} unique repos written to {args.out} using {report.requests} requests"
    )
    if report.truncated:
        discover.stderr_log(f"warning: {len(report.truncated)} queries still capped at 1000")
    return 0


def _write_json_atomic(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # a crash mid-write never leaves a half-written file


def _cmd_classify(args: argparse.Namespace) -> int:
    from collections import Counter

    from pipeline import classify
    from pipeline.discover import stderr_log
    from pipeline.github import GitHubClient, GitHubError

    repos = json.loads(Path(args.input).read_text(encoding="utf-8"))["repos"]
    if args.repo:
        wanted = {r.lower() for r in args.repo}
        repos = [r for r in repos if f"{r['owner']}/{r['name']}".lower() in wanted]
    if args.limit:
        repos = repos[: args.limit]

    out = Path(args.out)
    done: dict[str, dict] = {}
    if out.exists():  # resume: repos already in the file are not fetched again
        done = {r["full_name"].lower(): r for r in json.loads(out.read_text(encoding="utf-8"))}
        stderr_log(f"resuming: {len(done)} repos already classified in {out}")
    todo = [r for r in repos if f"{r['owner']}/{r['name']}".lower() not in done]

    client = GitHubClient(token=os.environ.get("GITHUB_TOKEN"))
    errors = consecutive = 0
    for i, repo in enumerate(todo, 1):
        name = f"{repo['owner']}/{repo['name']}"
        try:
            paths, deps, truncated = classify.gather_signals(client, repo)
        except GitHubError as err:
            errors += 1
            consecutive += 1
            stderr_log(f"[{i}/{len(todo)}] {name}: skipped, {err} (retried on the next run)")
            if consecutive >= 20:
                stderr_log("20 errors in a row: stopping; progress is saved")
                break
            continue
        consecutive = 0
        result = classify.classify(repo, paths, deps, truncated)
        done[name.lower()] = classify.to_dict(result)
        stderr_log(f"[{i}/{len(todo)}] {name}: {result.status} ({result.reason})")
        if i % args.save_every == 0:
            _write_json_atomic(out, list(done.values()))
    _write_json_atomic(out, list(done.values()))

    results = list(done.values())
    status = Counter(r["status"] for r in results)
    slugs = Counter(c["slug"] for r in results if r["status"] == "active" for c in r["categories"])
    stderr_log(
        f"{len(results)} repos in {out} ({client.request_count} requests this run, "
        f"{errors} skipped): {dict(status)}"
    )
    stderr_log(f"active categories: {dict(slugs)}")
    return 1 if consecutive >= 20 else 0


def _cmd_export(args: argparse.Namespace) -> int:
    from pipeline import export, seeds

    if args.db:
        from datetime import UTC, datetime

        from pipeline import db

        with db.connect(_require_env("DATABASE_URL")) as conn:
            records, skipped = db.records_for_export(conn, datetime.now(UTC).date())
        if skipped:
            print(f"skipped {len(skipped)} active repos without a strong category")
    else:
        if not (args.candidates and args.classified):
            print("give --db, or both --candidates and --classified")
            return 2
        candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
        classified = json.loads(Path(args.classified).read_text(encoding="utf-8"))
        records = export.records_from_json(candidates, classified, seeds.load())
    docs = export.build(records)
    docs.update(export.reference_docs(docs["meta.json"]["updated_at"]))
    export.write(docs, Path(args.out))
    print(f"wrote {len(docs)} files for {len(records)} active repos to {args.out}")
    return 0


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set (see .env.example)")
    return value


def _cmd_load(args: argparse.Namespace) -> int:
    """One-off: put discover (and optionally classify) output into the database."""
    from pipeline import db, nightly, seeds

    candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))["repos"]
    with db.connect(_require_env("DATABASE_URL")) as conn:
        if args.classification_only:
            # Keep the numbers refresh wrote; discover output may be hours or days old.
            print("repos: left as they are (--classification-only)")
        else:
            inserted, updated, skipped = db.upsert_repos(conn, candidates)
            conn.commit()
            print(f"repos: {inserted} new, {updated} updated, {len(skipped)} skipped (name clash)")
        if args.classified:
            ids = {f"{c['owner']}/{c['name']}".lower(): c["id"] for c in candidates}
            results = json.loads(Path(args.classified).read_text(encoding="utf-8"))
            pairs = [
                (ids[r["full_name"].lower()], nightly.result_from_dict(r))
                for r in results
                if r["full_name"].lower() in ids
            ]
            db.save_classification(conn, pairs)
            conn.commit()
            print(f"classification saved for {len(pairs)} repos")
        print(f"excluded: {db.apply_exclusions(conn, seeds.load())}")
        conn.commit()
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from pipeline import db, nightly
    from pipeline.discover import stderr_log
    from pipeline.github import GitHubClient

    url = _require_env("DATABASE_URL")
    client = GitHubClient(token=_require_env("GITHUB_TOKEN"))
    stats = nightly.refresh_all(lambda: db.connect(url), client, stderr_log)
    day = datetime.now(UTC).date()
    count = (
        0 if args.no_snapshot else nightly.in_transaction(lambda: db.connect(url), db.snapshot, day)
    )
    print(f"{stats}, snapshot rows for {day}: {count}, {client.request_count} GitHub requests")
    return 0


def _cmd_nightly(args: argparse.Namespace) -> int:
    from pipeline import db, enrich, nightly, seeds
    from pipeline.discover import stderr_log
    from pipeline.github import GitHubClient
    from pipeline.llm import make_provider

    url = _require_env("DATABASE_URL")
    client = GitHubClient(token=_require_env("GITHUB_TOKEN"))
    config = enrich.load_config()
    provider, skipped = None, None
    if args.enrich_limit > 0:
        try:
            provider = make_provider(config, dict(os.environ))
        except Exception as err:  # no AI key: run everything else, skip summaries
            skipped = str(err)[:200]  # names what is missing, never a key value
            stderr_log(f"summaries skipped: {skipped}")
    stats = nightly.run(
        lambda: db.connect(url),
        client,
        seeds.load(),
        Path(args.out),
        do_discover=not args.no_discover,
        classify_limit=args.classify_limit,
        provider=provider,
        enrich_limit=args.enrich_limit,
        max_readme_chars=int(config["max_readme_chars"]),
        log=stderr_log,
        summaries_skipped=skipped,
    )
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_enrich(args: argparse.Namespace) -> int:
    """Only the summary step of nightly, e.g. to fill the backlog from a laptop."""
    from pipeline import db, enrich, nightly
    from pipeline.discover import stderr_log
    from pipeline.github import GitHubClient
    from pipeline.llm import make_provider

    url = _require_env("DATABASE_URL")
    config = enrich.load_config()
    provider = make_provider(config, dict(os.environ))
    client = GitHubClient(token=os.environ.get("GITHUB_TOKEN"))

    def log(message: str) -> None:
        stderr_log(message)

    stats = nightly.enrich_new(
        lambda: db.connect(url), client, provider, args.limit, int(config["max_readme_chars"]), log
    )
    stats.update(model=provider.model, ai_calls=provider.calls)
    print(json.dumps(stats, sort_keys=True))
    return 0


def _cmd_reference_check(args: argparse.Namespace) -> int:
    """Compare content/reference with the live docs: what is missing, what the docs dropped."""
    from pipeline import docs, reference

    status = 0
    for group, spec in docs.GROUPS.items():
        if args.group and group not in args.group:
            continue
        doc_items = docs.items(group, docs.fetch(spec.page))
        entries = reference.load(group)
        missing, gone = reference.missing_and_gone(entries, doc_items)
        reviewed = sum(1 for e in entries if e.reviewed)
        print(
            f"{group:<9} docs {len(doc_items):>4} | ours {len(entries):>4} "
            f"(reviewed {reviewed}) | missing {len(missing):>4} | gone from docs {len(gone)}"
        )
        for e in gone:
            print(f"  gone: {e.name}")
        if gone:
            status = 1
    return status


def _cmd_reference_draft(args: argparse.Namespace) -> int:
    """Draft descriptions for docs items we do not have yet (reviewed: false)."""
    from datetime import UTC, datetime

    from pipeline import docs, enrich, reference, reference_draft
    from pipeline.discover import stderr_log
    from pipeline.llm import make_provider

    provider = make_provider(enrich.load_config(), dict(os.environ))
    spec = docs.GROUPS[args.group]
    doc_items = docs.items(args.group, docs.fetch(spec.page))
    entries = reference.load(args.group)
    missing, _ = reference.missing_and_gone(entries, doc_items)
    todo = missing[: args.limit] if args.limit else missing
    new, failed = reference_draft.draft(provider, todo, datetime.now(UTC).date(), stderr_log)
    reference.save(args.group, entries + new)
    print(
        f"{args.group}: {len(new)} drafted, {len(failed)} failed, "
        f"{len(missing) - len(todo)} left for later, {provider.calls} AI calls"
    )
    return 0


def _cmd_docs_watch(args: argparse.Namespace) -> int:
    """Compare content/reference with the docs; write the report used as the PR body."""
    from datetime import UTC, datetime

    from pipeline import docs, docs_watch, enrich, reference, reference_draft
    from pipeline.discover import stderr_log
    from pipeline.llm import make_provider

    today = datetime.now(UTC).date()
    provider = None
    if not args.no_draft:
        try:
            provider = make_provider(enrich.load_config(), dict(os.environ))
        except Exception as err:  # names what is missing, never a key value
            stderr_log(f"new items will not be drafted: {err}")

    def draft(items):
        return reference_draft.draft(provider, items, today, stderr_log)

    reports = []
    for group in sorted(_REFERENCE_GROUPS):
        doc_items = docs.items(group, docs.fetch(docs.GROUPS[group].page))
        entries, report = docs_watch.watch(
            group, reference.load(group), doc_items, today, draft if provider else None
        )
        reference.save(group, entries)
        reports.append(report)
        stderr_log(
            f"{group}: {len(report.changed)} changed, {len(report.added)} added, "
            f"{len(report.not_drafted)} not drafted, {len(report.removed)} removed, "
            f"{report.baselined} baselined"
        )
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(docs_watch.markdown(reports, today), encoding="utf-8")
    print(json.dumps({"needs_review": any(r.needs_review for r in reports)}))
    return 0


def _cmd_enrich_trial(args: argparse.Namespace) -> int:
    """Compare models on a sample of active repos. Writes JSON only, never the database."""
    import random
    import time

    from pipeline import db, enrich
    from pipeline.discover import stderr_log
    from pipeline.github import GitHubClient
    from pipeline.llm import GeminiProvider

    config = enrich.load_config()
    with db.connect(_require_env("DATABASE_URL")) as conn:
        rows = conn.execute(
            """
            SELECT r.owner, r.name, r.description, r.topics, r.stars,
                   ARRAY(SELECT rc.slug FROM repo_categories rc
                         WHERE rc.repo_id = r.id AND rc.confidence >= 0.8
                         ORDER BY rc.confidence DESC, rc.slug)
            FROM repos r WHERE r.status = 'active' ORDER BY r.id
            """
        ).fetchall()
    repos = [
        dict(owner=o, name=n, description=d, topics=list(t), stars=s, categories=list(c))
        for o, n, d, t, s, c in rows
        if c
    ]
    rng = random.Random(args.seed)
    rng.shuffle(repos)
    by_primary: dict[str, list[dict]] = {}
    for repo in repos:
        by_primary.setdefault(repo["categories"][0], []).append(repo)
    sample: list[dict] = []
    while len(sample) < min(args.sample, len(repos)):  # round-robin across categories
        for group in by_primary.values():
            if group and len(sample) < args.sample:
                sample.append(group.pop())

    providers = [
        GeminiProvider(
            _require_env("GEMINI_API_KEY"),
            model,
            min_interval=float(config.get("min_seconds_between_calls", 0)),
        )
        for model in args.models.split(",")
    ]
    seconds = {p.model: 0.0 for p in providers}
    client = GitHubClient(token=os.environ.get("GITHUB_TOKEN"))
    results = []
    for i, repo in enumerate(sample, 1):
        name = f"{repo['owner']}/{repo['name']}"
        readme = enrich.fetch_readme(client, repo["owner"], repo["name"])
        entry = {**repo, "readme_chars": len(readme.text) if readme else 0, "models": {}}
        if readme is None:
            entry["error"] = "no README"
            results.append(entry)
            continue
        entry["installs"] = enrich.extract_installs(readme.text)
        entry["needs_token"] = enrich.needs_token(readme.text)
        for provider in providers:
            start = time.monotonic()
            try:
                out = enrich.enrich(provider, repo, readme, config["max_readme_chars"])
                entry["models"][provider.model] = {"summary": out.summary, "use_when": out.use_when}
            except enrich.EnrichError as err:
                entry["models"][provider.model] = {"error": str(err)[:300]}
            seconds[provider.model] += time.monotonic() - start
        results.append(entry)
        ok = sum(1 for m in entry["models"].values() if "summary" in m)
        stderr_log(f"[{i}/{len(sample)}] {name}: {ok}/{len(providers)} models ok")
    usage = {
        p.model: {
            "calls": p.calls,
            "input_tokens": p.input_tokens,
            "output_tokens": p.output_tokens,
            "seconds": round(seconds[p.model], 1),
            "ok": sum(1 for r in results if "summary" in r["models"].get(p.model, {})),
        }
        for p in providers
    }
    Path(args.out).write_text(
        json.dumps({"usage": usage, "repos": results}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(json.dumps(usage, indent=1))
    return 0


_REFERENCE_GROUPS = ("commands", "cli", "settings", "hooks", "env")


def _cmd_official(args: argparse.Namespace) -> int:
    from pipeline import db, enrich, nightly
    from pipeline.discover import stderr_log
    from pipeline.github import GitHubClient
    from pipeline.llm import make_provider

    url = _require_env("DATABASE_URL")
    provider, skipped = None, None
    try:
        provider = make_provider(enrich.load_config(), dict(os.environ))
    except Exception as err:  # the message names what is missing, never a key value
        skipped = str(err)[:200]
        stderr_log(f"news summaries skipped: {skipped}")
    stats = nightly.run_official(
        lambda: db.connect(url),
        GitHubClient(token=_require_env("GITHUB_TOKEN")),
        Path(args.out),
        provider=provider,
        log=stderr_log,
        summaries_skipped=skipped,
    )
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__)
    parser.add_argument("--version", action="version", version=f"aigym-code-data {__version__}")
    sub = parser.add_subparsers(dest="command")
    check = sub.add_parser("check-env", help="show which environment variables are set")
    check.set_defaults(func=_cmd_check_env)

    seeds_check = sub.add_parser("seeds-check", help="validate pipeline/seeds.yaml")
    seeds_check.set_defaults(func=_cmd_seeds_check)

    mig = sub.add_parser("migrate", help="apply pending SQL migrations (DATABASE_URL_MIGRATE)")
    mig.add_argument("--check", action="store_true", help="only validate the files, no database")
    mig.add_argument("--dry-run", action="store_true", help="list pending migrations, apply none")
    mig.set_defaults(func=_cmd_migrate)

    disc = sub.add_parser("discover", help="search GitHub for Radar candidate repos")
    disc.add_argument("--out", required=True, help="write candidates to this JSON file")
    disc.add_argument(
        "--only", action="append", metavar="LABEL", help="run one query, e.g. topic:claude-code"
    )
    disc.set_defaults(func=_cmd_discover)

    cls = sub.add_parser("classify", help="classify discover output by files in each repo")
    cls.add_argument("--in", dest="input", required=True, help="JSON written by discover")
    cls.add_argument("--out", required=True, help="write results to this JSON file")
    cls.add_argument("--limit", type=int, help="only the first N repos (most stars first)")
    cls.add_argument("--repo", action="append", metavar="OWNER/NAME", help="only these repos")
    cls.add_argument(
        "--save-every", type=int, default=100, help="save progress every N repos (default 100)"
    )
    cls.set_defaults(func=_cmd_classify)

    exp = sub.add_parser("export", help="write /v1 Radar JSON from discover + classify output")
    exp.add_argument("--db", action="store_true", help="read from the database (DATABASE_URL)")
    exp.add_argument("--candidates", help="JSON written by discover")
    exp.add_argument("--classified", help="JSON written by classify")
    exp.add_argument("--out", required=True, help="output folder, e.g. out/v1")
    exp.set_defaults(func=_cmd_export)

    load = sub.add_parser("load", help="store discover/classify JSON in the database")
    load.add_argument("--candidates", required=True, help="JSON written by discover")
    load.add_argument("--classified", help="JSON written by classify")
    load.add_argument(
        "--classification-only",
        action="store_true",
        help="store only the classification; never overwrite repo numbers",
    )
    load.set_defaults(func=_cmd_load)

    ref = sub.add_parser("refresh", help="update numbers of all tracked repos and snapshot")
    ref.add_argument("--no-snapshot", action="store_true", help="update repos only")
    ref.set_defaults(func=_cmd_refresh)

    nig = sub.add_parser("nightly", help="the whole nightly job; logs to pipeline_runs")
    nig.add_argument("--out", required=True, help="export folder, e.g. out/v1")
    nig.add_argument("--no-discover", action="store_true", help="skip GitHub search")
    nig.add_argument("--classify-limit", type=int, default=500, help="new repos per night")
    nig.add_argument("--enrich-limit", type=int, default=150, help="summaries per night (0 = none)")
    nig.set_defaults(func=_cmd_nightly)

    rch = sub.add_parser("reference-check", help="compare content/reference with the docs")
    rch.add_argument("--group", action="append", choices=sorted(_REFERENCE_GROUPS))
    rch.set_defaults(func=_cmd_reference_check)

    rdr = sub.add_parser("reference-draft", help="draft descriptions for missing items")
    rdr.add_argument("--group", required=True, choices=sorted(_REFERENCE_GROUPS))
    rdr.add_argument("--limit", type=int, help="draft at most N items")
    rdr.set_defaults(func=_cmd_reference_draft)

    dw = sub.add_parser("docs-watch", help="sync content/reference with the docs, write a report")
    dw.add_argument("--report", default="out/docs-watch.md", help="markdown report (PR body)")
    dw.add_argument("--no-draft", action="store_true", help="never call the AI")
    dw.set_defaults(func=_cmd_docs_watch)

    enr = sub.add_parser("enrich", help="write summaries for active repos that have none")
    enr.add_argument("--limit", type=int, default=50)
    enr.set_defaults(func=_cmd_enrich)

    off = sub.add_parser("official", help="news from official sources + full export (3-hourly)")
    off.add_argument("--out", required=True, help="export folder, e.g. site/v1")
    off.set_defaults(func=_cmd_official)

    tri = sub.add_parser("enrich-trial", help="compare summary models on sample repos (JSON)")
    tri.add_argument("--models", required=True, help="comma-separated exact model names")
    tri.add_argument("--sample", type=int, default=50)
    tri.add_argument("--seed", type=int, default=20261120)
    tri.add_argument("--out", required=True)
    tri.set_defaults(func=_cmd_enrich_trial)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    load_dotenv(Path(".env"))
    sys.exit(main())
