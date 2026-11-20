# Static JSON /v1: Radar files

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

## Not yet produced

`today.json`, `news/…`, `releases…` (P2), `reference/…`, `templates.json`, `models.json` (P3)
and `status.json` (Cloudflare Worker, P2). Their shapes will be added here when built.
