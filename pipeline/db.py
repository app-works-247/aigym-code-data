"""Reading and writing the pipeline database (docs/database.md).

Every function takes an open psycopg connection and leaves committing to the caller, so a job
can group its writes into one transaction. Jobs connect with DATABASE_URL (role pipeline_rw:
rows only, never structure).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from psycopg.types.json import Jsonb

from pipeline.categories import BY_SLUG
from pipeline.classify import ACTIVE_CONFIDENCE, Result
from pipeline.export import HISTORY_DAYS, RepoRecord
from pipeline.seeds import Seeds

# Columns that discover and refresh both know; status, first_seen and is_docs are not here
# because they belong to classify and to the database itself.
_REPO_COLUMNS = (
    "id",
    "node_id",
    "owner",
    "name",
    "description",
    "homepage",
    "language",
    "license_spdx",
    "topics",
    "default_branch",
    "created_at",
    "pushed_at",
    "archived",
    "is_official",
    "stars",
    "forks",
    "open_issues",
)


def connect(url: str):
    import psycopg

    return psycopg.connect(url)


def upsert_repos(conn, repos: list[dict]) -> tuple[int, int, list[str]]:
    """Insert new repos (status pending) and refresh the GitHub fields of known ones.

    Returns (inserted, updated, skipped). A repo is skipped when another id already holds its
    name, which happens when a repo was deleted and a new one created under the same name;
    the old row is resolved by refresh (it disappears from GitHub) before the new one enters.
    """
    taken = {
        (owner.lower(), name.lower()): rid
        for rid, owner, name in conn.execute("SELECT id, owner, name FROM repos")
    }
    known_ids = set(taken.values())
    rows, skipped = [], []
    for r in repos:
        holder = taken.get((r["owner"].lower(), r["name"].lower()))
        if holder is not None and holder != r["id"]:
            skipped.append(f"{r['owner']}/{r['name']}")
            continue
        rows.append(tuple(r[c] for c in _REPO_COLUMNS))
    columns = ", ".join(_REPO_COLUMNS)
    placeholders = ", ".join(["%s"] * len(_REPO_COLUMNS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _REPO_COLUMNS if c != "id")
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO repos ({columns}, last_refreshed) VALUES ({placeholders}, now()) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}, last_refreshed = now()",
            rows,
        )
    inserted = sum(1 for row in rows if row[0] not in known_ids)
    return inserted, len(rows) - inserted, skipped


def save_classification(conn, results: list[tuple[int, Result]]) -> None:
    """Store classify output. Manual categories and flags set by enrich are kept."""
    if not results:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE repos SET status = %s, is_docs = %s, classified_at = now() "
            "WHERE id = %s AND status <> 'removed'",
            [(res.status, res.is_docs, rid) for rid, res in results],
        )
        ids = [rid for rid, _ in results]
        cur.execute(
            "DELETE FROM repo_categories WHERE repo_id = ANY(%s) AND source <> 'manual'", (ids,)
        )
        cur.executemany(
            "INSERT INTO repo_categories (repo_id, slug, source, confidence) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (repo_id, slug) DO NOTHING",
            [(rid, m.slug, m.source, m.confidence) for rid, res in results for m in res.categories],
        )
        cur.execute(
            "DELETE FROM repo_flags WHERE repo_id = ANY(%s) AND flag <> 'needs_token'", (ids,)
        )
        cur.executemany(
            "INSERT INTO repo_flags (repo_id, flag) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [(rid, flag) for rid, res in results for flag in res.flags],
        )


def apply_exclusions(conn, seeds: Seeds) -> int:
    """Mark repos listed in seeds.yaml `exclude` as removed. Returns how many changed."""
    changed = 0
    for rid, owner, name in conn.execute(
        "SELECT id, owner, name FROM repos WHERE status <> 'removed'"
    ).fetchall():
        if seeds.is_excluded(owner, name):
            conn.execute("UPDATE repos SET status = 'removed' WHERE id = %s", (rid,))
            changed += 1
    return changed


def repos_to_classify(conn, limit: int) -> list[dict]:
    """Repos classify has never looked at, oldest first, as discover-shaped dicts."""
    cur = conn.execute(
        f"SELECT {', '.join(_REPO_COLUMNS)} FROM repos "
        "WHERE classified_at IS NULL AND status <> 'removed' "
        "ORDER BY first_seen, stars DESC LIMIT %s",
        (limit,),
    )
    names = [d.name for d in cur.description]
    out = []
    for row in cur.fetchall():
        repo = dict(zip(names, row, strict=True))
        for key in ("created_at", "pushed_at"):
            if repo[key] is not None:
                repo[key] = _iso(repo[key])
        repo["matched_by"] = []  # topics carry the same signal for classify
        out.append(repo)
    return out


def refresh_targets(conn) -> list[tuple[int, str]]:
    """(id, node_id) of every repo that still needs daily numbers."""
    return conn.execute(
        "SELECT id, node_id FROM repos WHERE status <> 'removed' ORDER BY id"
    ).fetchall()


def apply_refresh(conn, updates: list[dict], missing: list[int]) -> None:
    """Write refreshed GitHub fields; repos GitHub no longer returns become hidden."""
    from pipeline.refresh import FIELDS as fields

    with conn.cursor() as cur:
        cur.executemany(
            f"UPDATE repos SET {', '.join(f'{c} = %({c})s' for c in fields)}, "
            "last_refreshed = now() WHERE id = %(id)s",
            updates,
        )
        if missing:
            cur.execute(
                "UPDATE repos SET status = 'hidden' WHERE id = ANY(%s) AND status <> 'removed'",
                (missing,),
            )


def snapshot(conn, day: date) -> int:
    """Today's stars and forks for every tracked repo. Safe to run twice the same day."""
    cur = conn.execute(
        "INSERT INTO repo_snapshots (repo_id, day, stars, forks) "
        "SELECT id, %s, stars, forks FROM repos WHERE status <> 'removed' "
        "ON CONFLICT (repo_id, day) DO UPDATE SET stars = EXCLUDED.stars, forks = EXCLUDED.forks",
        (day,),
    )
    return cur.rowcount


def records_for_export(conn, today: date) -> tuple[list[RepoRecord], list[str]]:
    """Active repos with at least one strong category. Returns (records, skipped names)."""
    rows = conn.execute(
        """
        SELECT r.id, r.owner, r.name, r.description, r.homepage, r.language, r.license_spdx,
               r.topics, r.created_at, r.pushed_at, r.archived, r.is_official, r.is_docs,
               r.stars, r.forks, r.open_issues,
               ARRAY(SELECT rc.slug FROM repo_categories rc WHERE rc.repo_id = r.id
                     AND rc.confidence >= %s) AS cats,
               ARRAY(SELECT f.flag FROM repo_flags f WHERE f.repo_id = r.id ORDER BY f.flag)
        FROM repos r WHERE r.status = 'active'
        """,
        (ACTIVE_CONFIDENCE,),
    ).fetchall()
    confidence = {
        (rid, slug): conf
        for rid, slug, conf in conn.execute("SELECT repo_id, slug, confidence FROM repo_categories")
    }
    ids = [r[0] for r in rows]
    history: dict[int, list[tuple[date, int]]] = {}
    for rid, day, stars in conn.execute(
        "SELECT repo_id, day, stars FROM repo_snapshots "
        "WHERE repo_id = ANY(%s) AND day > %s AND day < %s ORDER BY repo_id, day",
        (ids, today - timedelta(days=HISTORY_DAYS), today),
    ):
        history.setdefault(rid, []).append((day, stars))
    summaries: dict[int, dict] = {}
    use_when: dict[int, dict] = {}
    for rid, lang, summary, when in conn.execute(
        "SELECT repo_id, lang, summary, use_when FROM repo_i18n WHERE repo_id = ANY(%s)", (ids,)
    ):
        summaries.setdefault(rid, {})[lang] = summary
        use_when.setdefault(rid, {})[lang] = when
    installs: dict[int, list[dict]] = {}
    for rid, kind, cmd in conn.execute(
        "SELECT repo_id, kind, cmd FROM repo_installs "
        "WHERE repo_id = ANY(%s) ORDER BY repo_id, ord",
        (ids,),
    ):
        installs.setdefault(rid, []).append({"kind": kind, "cmd": cmd})

    records, skipped = [], []
    for row in rows:
        rid, owner, name = row[0], row[1], row[2]
        cats = sorted(row[16], key=lambda s: (-confidence[(rid, s)], BY_SLUG[s].sort_order))
        if not cats:
            skipped.append(f"{owner}/{name}")
            continue
        records.append(
            RepoRecord(
                github_id=rid,
                owner=owner,
                name=name,
                description=row[3],
                homepage=row[4],
                language=row[5],
                license_spdx=row[6],
                topics=list(row[7]),
                created_at=_iso(row[8]),
                pushed_at=_iso(row[9]) if row[9] else None,
                archived=row[10],
                is_official=row[11],
                is_docs=row[12],
                stars=row[13],
                forks=row[14],
                open_issues=row[15],
                categories=cats,
                flags=list(row[17]),
                summary=summaries.get(rid) if len(summaries.get(rid, {})) == 2 else None,
                use_when=use_when.get(rid) if len(use_when.get(rid, {})) == 2 else None,
                install=installs.get(rid, []),
                history=history.get(rid, []),
            )
        )
    return records, skipped


NO_README = "none"  # repos.readme_sha for repos without a README: nothing to summarise


def repos_to_enrich(conn, limit: int) -> list[dict]:
    """Active repos with no summary yet, most stars first (they are seen first in the app)."""
    rows = conn.execute(
        """
        SELECT r.id, r.owner, r.name, r.description, r.topics,
               ARRAY(SELECT rc.slug FROM repo_categories rc
                     WHERE rc.repo_id = r.id AND rc.confidence >= %s
                     ORDER BY rc.confidence DESC, rc.slug)
        FROM repos r
        WHERE r.status = 'active' AND r.readme_sha IS DISTINCT FROM %s
          AND NOT EXISTS (SELECT 1 FROM repo_i18n i WHERE i.repo_id = r.id)
        ORDER BY r.stars DESC, r.id LIMIT %s
        """,
        (ACTIVE_CONFIDENCE, NO_README, limit),
    ).fetchall()
    return [
        dict(id=i, owner=o, name=n, description=d, topics=list(t), categories=list(c))
        for i, o, n, d, t, c in rows
    ]


def save_enrichment(conn, repo_id: int, enriched) -> None:
    """Store one repo's summary (vi + en), install commands and needs_token flag."""
    for lang in ("vi", "en"):
        conn.execute(
            """
            INSERT INTO repo_i18n (repo_id, lang, summary, use_when, provider, model, source_sha)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (repo_id, lang) DO UPDATE SET summary = EXCLUDED.summary,
              use_when = EXCLUDED.use_when, provider = EXCLUDED.provider,
              model = EXCLUDED.model, source_sha = EXCLUDED.source_sha, generated_at = now()
            """,
            (
                repo_id,
                lang,
                enriched.summary[lang],
                Jsonb(enriched.use_when[lang]),
                enriched.provider,
                enriched.model,
                enriched.source_sha,
            ),
        )
    conn.execute("DELETE FROM repo_installs WHERE repo_id = %s", (repo_id,))
    for ord_, item in enumerate(enriched.installs):
        conn.execute(
            "INSERT INTO repo_installs (repo_id, ord, kind, cmd) VALUES (%s, %s, %s, %s)",
            (repo_id, ord_, item["kind"], item["cmd"]),
        )
    if enriched.needs_token:
        conn.execute(
            "INSERT INTO repo_flags (repo_id, flag) VALUES (%s, 'needs_token') "
            "ON CONFLICT DO NOTHING",
            (repo_id,),
        )
    else:
        conn.execute(
            "DELETE FROM repo_flags WHERE repo_id = %s AND flag = 'needs_token'", (repo_id,)
        )
    conn.execute("UPDATE repos SET readme_sha = %s WHERE id = %s", (enriched.source_sha, repo_id))


def mark_no_readme(conn, repo_id: int) -> None:
    conn.execute("UPDATE repos SET readme_sha = %s WHERE id = %s", (NO_README, repo_id))


def start_run(conn, job: str) -> int:
    return conn.execute(
        "INSERT INTO pipeline_runs (job) VALUES (%s) RETURNING id", (job,)
    ).fetchone()[0]


def finish_run(conn, run_id: int, ok: bool, stats: dict) -> None:
    conn.execute(
        "UPDATE pipeline_runs SET finished_at = now(), status = %s, stats = %s WHERE id = %s",
        ("ok" if ok else "failed", Jsonb(stats), run_id),
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
