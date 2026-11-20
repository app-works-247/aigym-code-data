"""Command line entry point: `python -m pipeline <command>`.

Commands so far: check-env, seeds-check, migrate, discover, classify, export.
More jobs (refresh, the LLM step of classify, enrich…) arrive during P1.
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

    candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    classified = json.loads(Path(args.classified).read_text(encoding="utf-8"))
    records = export.records_from_json(candidates, classified, seeds.load())
    docs = export.build(records)
    export.write(docs, Path(args.out))
    print(f"wrote {len(docs)} files for {len(records)} active repos to {args.out}")
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
    exp.add_argument("--candidates", required=True, help="JSON written by discover")
    exp.add_argument("--classified", required=True, help="JSON written by classify")
    exp.add_argument("--out", required=True, help="output folder, e.g. out/v1")
    exp.set_defaults(func=_cmd_export)
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
