# Static JSON /v1: Radar, Today and status files

The app reads these files from Cloudflare Pages and ships a copy inside the app for offline
use. `python -m pipeline export` writes them; `pipeline/export.py` is the reference.

Rules for every file:

- UTF-8, compact JSON, keys sorted. Every document has `"schema": 1`. A breaking change
  means a new folder (`/v2`), never a silent change to `/v1`.
- Times are UTC ISO 8601 (`2026-11-20T02:30:00Z`). Days in `history` are integers `YYYYMMDD`.
- `null` means unknown, not zero. The app shows it as "—". Example: `d7` stays `null` until
  the pipeline has a snapshot from 7 days ago; `summary` stays `null` until enrich runs.
- A repo `id` is `owner__name` in lowercase (owners cannot contain `_`, so the first `__`
  splits it). Use `owner` and `name` for display; they keep GitHub's capitalisation.
- Only repos whose status is `active` (decision 24) appear anywhere.

## /v1/meta.json

| Field | Type | Meaning |
|---|---|---|
| `updated_at` | string | When export ran |
| `repo_count` | int | Active repos |
| `categories[]` | array | The 9 categories in display order |
| `categories[].slug` | string | `skills`, `mcp`, `subagents`, `hooks`, `plugins`, `commands`, `templates`, `tools`, `learn` |
| `categories[].title` | `{vi, en}` | Display name |
| `categories[].lesson_unit` | int or null | Related course level; null = further reading |
| `categories[].count` | int | Active repos in this category |

## /v1/top/{slug}.json

One file per category plus `all`. Two ranked lists of up to 100 **list items**:

| Field | Meaning |
|---|---|
| `category` | The slug, or `all` |
| `stars[]` | Most stars first ("Nhiều sao" tab) |
| `rising[]` | Highest `rising` first ("Đang lên (7 ngày)" tab). Empty until 7 days of snapshots exist |

**List item** (also used by `trending.json`):

| Field | Type | Meaning |
|---|---|---|
| `rank` | int | 1-based position in this list |
| `id`, `owner`, `name` | string | See rules above |
| `description` | string or null | GitHub description, original language |
| `stars` | int | Current stars |
| `d1`, `d7`, `d30` | int or null | Stars gained in 1, 7, 30 days |
| `rising` | number or null | `d7 / sqrt(stars)` (PLAN.md 6.4) |
| `activity` | string | `active` (push within 90 days), `slow` (within a year), `stale` (older or archived) |
| `categories` | string[] | Slugs, primary first |
| `official` | bool | Owned by `anthropics` |
| `summary` | `{vi, en}` or null | Short AI summary |

## /v1/trending.json

`items[]`: up to 50 list items across all categories, highest `rising` first.

## /v1/repos/{id}.json

Everything in a list item except `rank`, plus:

| Field | Type | Meaning |
|---|---|---|
| `github_id` | int | GitHub's numeric id, stable across renames |
| `url` | string | `https://github.com/{owner}/{name}` |
| `homepage`, `language`, `license` | string or null | `license` is an SPDX id; null = none detected |
| `topics` | string[] | GitHub topics |
| `created_at`, `pushed_at` | string or null | From GitHub |
| `docs` | bool | Awesome list or guide |
| `forks`, `open_issues` | int | From GitHub |
| `lesson_units` | int[] | Course levels related to its categories |
| `use_when` | `{vi: [], en: []}` or null | "Dùng khi" bullet points, from enrich |
| `install[]` | `{kind, cmd}` | Commands quoted verbatim from the README; `kind` is `plugin`, `mcp`, `skill`, `npm`, `pip` or `other` |
| `cautions` | string[] | Any of `has_shell_hooks`, `needs_token`, `no_license`, `inactive`, `very_new` |
| `history` | `[[YYYYMMDD, stars], …]` | Daily stars for the last 90 days, oldest first |
| `related[]` | `{id, owner, name, stars}` | Up to 6 other repos from its primary category, most stars first |

## /v1/today.json

Written by `pipeline/news.py` (`news_docs`) in the nightly and 3-hourly `official` jobs.

| Field | Type | Meaning |
|---|---|---|
| `updated_at` | time | When the job wrote the file |
| `claude_code` | news item or `null` | The newest Claude Code release with a verified summary |
| `news` | news item[] | Up to 20 items from the last 21 days with `importance` 2 or more, newest first |
| `rising` | repo[] | The first 5 entries of `trending.json` (empty until 7 days of star history exist) |
| `tag_titles` | `{tag: {vi, en}}` | Display names for the tags below |

## /v1/news/page/{n}.json and /v1/news/item/{id}.json

Pages hold 30 items each, newest first: `{schema, updated_at, page, pages, items}`. An item file
is one news item plus `schema` and `updated_at`.

A **news item**:

| Field | Type | Meaning |
|---|---|---|
| `id` | string | 12 hex characters, from the source URL |
| `source` | string | `claude-code-changelog`, `claude-api-release-notes`, `anthropic-news`, `mcp-blog` (`pipeline/sources.yaml`) |
| `url` | string | The official page; the app always links to it |
| `published_at` | time | |
| `kind` | `release` or `news` | A release is one Claude Code version |
| `version` | string or `null` | Set for releases |
| `tags` | string[] | `claude-code`, `api-sdk`, `model`, `mcp`, `skills-plugins`, `anthropic` |
| `importance` | 0–3 | 3 = new model, breaking change, price change or deprecation |
| `title` | `{vi, en}` | AI title, or the original title (same in both) when `ai_summary` is false |
| `summary` | `{vi, en}` or `null` | AI summary. Releases: up to 3 lines separated by `\n`. `null` when the safety net rejected it |
| `ai_summary` | bool | false: show the original title and the link only |

The safety net (`news.facts_missing`) rejects a summary when any version, Claude model name,
`code` span, `/command`, `--flag`, CONSTANT_CASE name, `$` price or date in it is missing from
the source, or when it is longer than the source. Summaries are plain text; only `` `code` ``
spans may be styled. Text from sources is never rendered as HTML.

## /v1/status.json

Served live by the Pages Function `functions/v1/status.json.js` from
`status.claude.com/api/v2`, cached 5 minutes. Not part of the exported files and never bundled in
the app: an old status would mislead.

| Field | Type | Meaning |
|---|---|---|
| `indicator` | string | `none`, `minor`, `major`, `critical`, `maintenance`, or `unknown` |
| `description` | string | e.g. "All Systems Operational" |
| `fetched_at`, `updated_at` | time | When we asked; when the status page last changed |
| `components[]` | `{id, name, status}` | Group headers are left out |
| `active_incidents[]`, `recent_incidents[]` | `{id, name, status, impact, started_at, resolved_at, url}` | Recent: the last 10 |
| `page` | string | `https://status.claude.com` |

When status.claude.com cannot be reached the function answers 502 with
`{"schema": 1, "indicator": "unknown", "error": "…"}` and a 60-second cache.

## Not yet documented here

`reference/{group}.json` and `templates.json` (P3, see `pipeline/reference.py` and
`pipeline/templates.py`), `releases…` and `models.json` (later).
