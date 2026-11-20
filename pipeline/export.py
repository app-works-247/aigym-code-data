"""Write the static Radar JSON the app reads (/v1, PLAN.md 6.9; field list in docs/json-v1.md).

export only turns a list of RepoRecord into files. Where the records come from is separate:
`records_from_json` reads discover + classify output (works today, before the database);
a database loader will produce the same records once Neon exists.
"""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pipeline.categories import BY_SLUG, CATEGORIES
from pipeline.classify import ACTIVE_CONFIDENCE
from pipeline.seeds import Seeds

SCHEMA_VERSION = 1
TOP_SIZE = 100
TRENDING_SIZE = 50
RELATED_SIZE = 6
HISTORY_DAYS = 90
ACTIVE_WITHIN = timedelta(days=90)  # PLAN.md 6.4
SLOW_WITHIN = timedelta(days=365)
# Files and folders export owns inside the output directory; nothing else is ever deleted.
OWNED = ("meta.json", "trending.json", "top", "repos")


@dataclass
class RepoRecord:
    """Everything export needs about one repo. Mirrors the database tables."""

    github_id: int
    owner: str
    name: str
    description: str | None
    homepage: str | None
    language: str | None
    license_spdx: str | None
    topics: list[str]
    created_at: str
    pushed_at: str | None
    archived: bool
    is_official: bool
    is_docs: bool
    stars: int
    forks: int
    open_issues: int
    categories: list[str]  # slugs, strongest first
    flags: list[str]
    summary: dict[str, str] | None = None  # {"vi": …, "en": …} from repo_i18n
    use_when: dict[str, list[str]] | None = None
    install: list[dict[str, str]] = field(default_factory=list)  # [{"kind", "cmd"}]
    history: list[tuple[date, int]] = field(default_factory=list)  # daily stars, oldest first

    @property
    def id(self) -> str:
        # Owners cannot contain "_", so the first "__" always separates owner from name.
        return f"{self.owner}__{self.name}".lower()


class ExportError(Exception):
    pass


# ----- numbers ---------------------------------------------------------------------------


def star_deltas(record: RepoRecord, today: date) -> dict[str, int | None]:
    """d1/d7/d30 from daily snapshots; None until a snapshot that old exists (shown as "—")."""
    by_day = dict(record.history)
    out: dict[str, int | None] = {}
    for days in (1, 7, 30):
        past = by_day.get(today - timedelta(days=days))
        out[f"d{days}"] = None if past is None else record.stars - past
    return out


def rising(stars: int, d7: int | None) -> float | None:
    """PLAN.md 6.4: stars gained in 7 days / sqrt(stars), so small repos can rise too."""
    if d7 is None or stars <= 0:
        return None
    return round(d7 / math.sqrt(stars), 4)


def activity(record: RepoRecord, now: datetime) -> str:
    """active: push within 90 days · slow: within a year · stale: older or archived."""
    if record.archived or not record.pushed_at:
        return "stale"
    age = now - datetime.fromisoformat(record.pushed_at.replace("Z", "+00:00"))
    if age <= ACTIVE_WITHIN:
        return "active"
    return "slow" if age <= SLOW_WITHIN else "stale"


# ----- documents -------------------------------------------------------------------------


def _list_item(record: RepoRecord, rank: int, deltas: dict, score: float | None, now) -> dict:
    return {
        "rank": rank,
        "id": record.id,
        "owner": record.owner,
        "name": record.name,
        "description": record.description,
        "stars": record.stars,
        **deltas,
        "rising": score,
        "activity": activity(record, now),
        "categories": record.categories,
        "official": record.is_official,
        "summary": record.summary,
    }


def _ranked(records: list[RepoRecord], computed: dict, now: datetime, limit: int, by: str):
    if by == "stars":
        ordered = sorted(records, key=lambda r: (-r.stars, r.id))
    else:
        with_score = [r for r in records if computed[r.id]["rising"] is not None]
        ordered = sorted(with_score, key=lambda r: (-computed[r.id]["rising"], -r.stars, r.id))
    return [
        _list_item(r, i, computed[r.id]["deltas"], computed[r.id]["rising"], now)
        for i, r in enumerate(ordered[:limit], 1)
    ]


def _detail(record: RepoRecord, computed: dict, related: list[RepoRecord], now, today) -> dict:
    cutoff = today - timedelta(days=HISTORY_DAYS)
    return {
        "schema": SCHEMA_VERSION,
        "id": record.id,
        "github_id": record.github_id,
        "owner": record.owner,
        "name": record.name,
        "url": f"https://github.com/{record.owner}/{record.name}",
        "description": record.description,
        "homepage": record.homepage,
        "language": record.language,
        "license": record.license_spdx,
        "topics": record.topics,
        "created_at": record.created_at,
        "pushed_at": record.pushed_at,
        "activity": activity(record, now),
        "official": record.is_official,
        "docs": record.is_docs,
        "stars": record.stars,
        "forks": record.forks,
        "open_issues": record.open_issues,
        **computed[record.id]["deltas"],
        "rising": computed[record.id]["rising"],
        "categories": record.categories,
        "lesson_units": sorted(
            {BY_SLUG[s].lesson_unit for s in record.categories if BY_SLUG[s].lesson_unit}
        ),
        "summary": record.summary,
        "use_when": record.use_when,
        "install": record.install,
        "cautions": record.flags,
        "history": [[int(d.strftime("%Y%m%d")), s] for d, s in record.history if d > cutoff],
        "related": [
            {"id": r.id, "owner": r.owner, "name": r.name, "stars": r.stars} for r in related
        ],
    }


def build(records: Iterable[RepoRecord], now: datetime | None = None) -> dict[str, dict]:
    """Every /v1 Radar document, keyed by its path relative to /v1."""
    now = now or datetime.now(UTC)
    today = now.date()
    records = list(records)
    seen: set[str] = set()
    for r in records:
        if r.id in seen:
            raise ExportError(f"duplicate repo id {r.id}")
        seen.add(r.id)
        unknown = [s for s in r.categories if s not in BY_SLUG]
        if unknown:
            raise ExportError(f"{r.id}: unknown categories {unknown}")
        if not r.categories:
            raise ExportError(f"{r.id}: an active repo needs at least one category")

    computed = {}
    for r in records:
        deltas = star_deltas(r, today)
        computed[r.id] = {"deltas": deltas, "rising": rising(r.stars, deltas["d7"])}

    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    docs: dict[str, dict] = {}
    per_category = {c.slug: [r for r in records if c.slug in r.categories] for c in CATEGORIES}

    docs["meta.json"] = {
        "schema": SCHEMA_VERSION,
        "updated_at": stamp,
        "repo_count": len(records),
        "categories": [
            {
                "slug": c.slug,
                "title": {"vi": c.title_vi, "en": c.title_en},
                "lesson_unit": c.lesson_unit,
                "count": len(per_category[c.slug]),
            }
            for c in CATEGORIES
        ],
    }
    for slug, members in [("all", records), *per_category.items()]:
        docs[f"top/{slug}.json"] = {
            "schema": SCHEMA_VERSION,
            "updated_at": stamp,
            "category": slug,
            "stars": _ranked(members, computed, now, TOP_SIZE, "stars"),
            "rising": _ranked(members, computed, now, TOP_SIZE, "rising"),
        }
    docs["trending.json"] = {
        "schema": SCHEMA_VERSION,
        "updated_at": stamp,
        "items": _ranked(records, computed, now, TRENDING_SIZE, "rising"),
    }
    for r in records:
        primary = r.categories[0]
        related = sorted(
            (o for o in per_category[primary] if o.id != r.id), key=lambda o: (-o.stars, o.id)
        )[:RELATED_SIZE]
        docs[f"repos/{r.id}.json"] = _detail(r, computed, related, now, today)
    return docs


def write(docs: dict[str, dict], out_dir: Path) -> None:
    """Replace export's own files in out_dir. Refuses a non-empty folder export did not make."""
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = list(out_dir.iterdir())
    if existing and not (out_dir / "meta.json").exists():
        raise ExportError(f"{out_dir} is not empty and holds no previous export; refusing")
    for name in OWNED:
        target = out_dir / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for rel, doc in sorted(docs.items()):
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        path.write_text(text, encoding="utf-8")


# ----- loading from JSON files (before the database exists) ------------------------------


def records_from_json(candidates: dict, classified: list[dict], seeds: Seeds) -> list[RepoRecord]:
    """Active repos from classify output, joined with discover data. Excluded repos are dropped."""
    by_name = {f"{c['owner']}/{c['name']}".lower(): c for c in candidates["repos"]}
    records = []
    for result in classified:
        if result["status"] != "active":
            continue
        c = by_name.get(result["full_name"].lower())
        if c is None or seeds.is_excluded(c["owner"], c["name"]):
            continue
        strong = [
            m["slug"]
            for m in sorted(
                result["categories"],
                key=lambda m: (-m["confidence"], BY_SLUG[m["slug"]].sort_order),
            )
            if m["confidence"] >= ACTIVE_CONFIDENCE
        ]
        records.append(
            RepoRecord(
                github_id=c["id"],
                owner=c["owner"],
                name=c["name"],
                description=c["description"],
                homepage=c["homepage"],
                language=c["language"],
                license_spdx=c["license_spdx"],
                topics=c["topics"],
                created_at=c["created_at"],
                pushed_at=c["pushed_at"],
                archived=c["archived"],
                is_official=c["is_official"],
                is_docs=result["is_docs"],
                stars=c["stars"],
                forks=c["forks"],
                open_issues=c["open_issues"],
                categories=strong,
                flags=result["flags"],
            )
        )
    return records
