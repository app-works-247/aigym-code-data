# aigym-code-data

Public data pipeline behind **AIGym Code**, an iOS app that teaches Claude Code to developers (Vietnamese and English).

It collects public information about the Claude ecosystem and publishes static JSON for the app:

- **Radar**: GitHub repositories for Skills, MCP servers, subagents, hooks and plugins, ranked by stars and growth.
- **Today**: Claude Code releases, Claude API release notes and official Anthropic news, summarized in vi/en.
- **Reference**: Claude Code commands, flags, settings, hook events and environment variables.

Not affiliated with Anthropic.

## Status

P1 in progress: database migrations, discovery seeds, `discover`, rule-based `classify`, the Radar part of `export`, the database layer, `refresh` and the nightly job are written and tested; the LLM step of classify and enrich come next. The roadmap lives in the private app repository.

## Run locally

Requires Python 3.12 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env            # fill in only what you need; never commit .env
.venv/bin/python -m pipeline --version
.venv/bin/python -m pipeline check-env   # shows which variables are set, never their values
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m unittest discover -s tests -v
```

## Commands

```bash
python -m pipeline seeds-check              # validate pipeline/seeds.yaml
python -m pipeline migrate --check          # validate migration files, no database needed
python -m pipeline migrate --dry-run        # list pending migrations (DATABASE_URL_MIGRATE)
python -m pipeline migrate                  # apply them, each in its own transaction
python -m pipeline discover --out repos.json   # search GitHub for Radar candidates
python -m pipeline classify --in repos.json --out classified.json --limit 50
python -m pipeline export --candidates repos.json --classified classified.json --out out/v1
python -m pipeline load --candidates repos.json --classified classified.json   # into the database
python -m pipeline refresh                  # today's numbers for every repo + star snapshot
python -m pipeline nightly --out site/v1    # the whole nightly job (see below)
python -m pipeline official --out site/v1   # official news + full export (every 3 hours)
```

`discover` uses `GITHUB_TOKEN` when set (30 searches/min); without it GitHub allows 10/min and a full run takes about half an hour. It writes JSON only; storing candidates in the database arrives with refresh.

`classify` reads each repo's file list (1 request) and, for MCP-looking repos, up to 4 dependency files. A repo becomes `active` only with real evidence: `SKILL.md`, `.claude-plugin/` manifests, plugin agents/commands/hooks, or an MCP SDK dependency. Files inside hidden tool folders (`.claude/`, `.gemini/`…), examples and tests count as weak evidence. Repos tagged for Claude without evidence stay `pending` for the LLM step; repos with only a generic MCP or agent-skills topic are `hidden`.

## Nightly job

`.github/workflows/nightly.yml` runs at 02:00 UTC (and by hand from the Actions tab): discover, classify new repos (500 a night at most), refresh every repo's numbers through GraphQL (100 per request), store the day's star snapshot, export `/v1` and deploy it to Cloudflare Pages at `aigym-code.pages.dev`, then checks the live `meta.json` shows the new run. Each run is logged in the `pipeline_runs` table. It needs three repository secrets: `DATABASE_URL` (role `pipeline_rw`), `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`. GitHub calls use the workflow's own `GITHUB_TOKEN`.

No database connection is held open while waiting on GitHub: Neon's free plan suspends after 5 idle minutes and drops open connections, so every step opens a short connection of its own.

The Postgres test in `tests/test_migrate.py` runs only when `TEST_DATABASE_URL` points to a throwaway database (CI starts one), as do the end-to-end tests in `tests/test_nightly.py`. They drop and recreate the `public` schema, so never point them at a real database.

## Official news job

`.github/workflows/official.yml` runs every 3 hours (half past) and by hand: it reads the sources in `pipeline/sources.yaml`, stores what is new, asks Gemini (`pipeline/ai.yaml`, secret `GEMINI_API_KEY`) for vi/en titles and summaries of up to 30 items, then exports and deploys the whole site, since a Pages deploy replaces every file. A summary that fails the safety net is kept as title only. Both jobs share one concurrency group, so deploys never overlap. Why summaries were skipped or rejected is kept in `pipeline_runs.stats`.

`/v1/status.json` is a Cloudflare Pages Function (`functions/`), tested with `node --test functions/status.test.mjs`.

## Layout

```
pipeline/            Python package (python -m pipeline …)
  migrations/        numbered SQL files; never edit one that has run
  seeds.yaml         topics and keywords discover searches for
tests/               unit tests (standard library unittest)
content/             public reference, templates and model table (see content/README.md)
docs/database.md     database design: tables, roles, migrations, backups
docs/json-v1.md      the static JSON the app reads (fields and rules)
.github/workflows/   ci.yml (lint + tests), nightly.yml (data job)
deploy/_headers      cache and CORS headers for Cloudflare Pages
```

## Data and licensing

- Code: MIT (see `LICENSE`).
- Repository data comes from the public GitHub API. Summaries are generated and link back to each source; full READMEs and articles are never republished.
- Repository owners can ask for a listing to be corrected or removed by opening an issue.

## Vietnamese summary

Repo công khai chứa pipeline dữ liệu cho app AIGym Code: Radar repo, tin Hôm nay và Tra cứu Claude Code. Mã theo giấy phép MIT.
