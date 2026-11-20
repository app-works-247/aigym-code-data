"""Claude Code reference in content/reference/<group>.yaml (PLAN.md 8.4).

Names, syntax and docs links come from the official docs (docs.py) and are never edited by
hand. Descriptions are our own words in vi and en: drafted, then reviewed by the owner, who
sets `reviewed: true`. Tests check that every entry still exists in the docs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import yaml

from pipeline.docs import GROUPS, DocItem

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content" / "reference"

TITLES = {
    "commands": {"vi": "Lệnh slash", "en": "Slash commands"},
    "cli": {"vi": "Lệnh và cờ CLI", "en": "CLI commands and flags"},
    "settings": {"vi": "Settings", "en": "Settings"},
    "hooks": {"vi": "Sự kiện hook", "en": "Hook events"},
    "env": {"vi": "Biến môi trường", "en": "Environment variables"},
}
MAX_SUMMARY = 200
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class ReferenceDataError(ValueError):
    pass


@dataclass
class Entry:
    id: str
    group: str
    name: str
    syntax: str
    summary: dict[str, str]
    docs: str
    verified_at: str  # the day the entry was checked against the docs (YYYY-MM-DD)
    reviewed: bool = False  # the owner read and approved the vi/en text
    example: str | None = None
    scope: str | None = None  # settings only: which files the key goes in
    since: str | None = None  # first Claude Code version, when known
    lessons: list[str] = field(default_factory=list)
    # Fingerprint of the docs row the summary was written from (docs_watch.py). A change
    # means the docs changed and the summary needs another look.
    docs_hash: str | None = None


def make_id(group: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{group}-{slug}"[:80].rstrip("-")


def docs_hash(item: DocItem) -> str:
    """12 hex characters over what the docs say about one item."""
    text = "\n".join([item.syntax, item.docs_text, *(f"{k}: {v}" for k, v in item.extra)])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def entry_from_doc(item: DocItem, summary: dict[str, str], today: date) -> Entry:
    extra = dict(item.extra)
    example = extra.get("Example")
    return Entry(
        id=make_id(item.group, item.name),
        group=item.group,
        name=item.name,
        syntax=item.syntax,
        summary=summary,
        docs=item.url,
        verified_at=today.isoformat(),
        example=example.strip("`") if example else None,
        scope=extra.get("Scope"),
        docs_hash=docs_hash(item),
    )


def path_for(group: str, directory: Path = CONTENT_DIR) -> Path:
    return directory / f"{group}.yaml"


def load(group: str, directory: Path = CONTENT_DIR) -> list[Entry]:
    path = path_for(group, directory)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    entries = [Entry(**row) for row in data]
    validate(group, entries)
    return entries


def save(group: str, entries: list[Entry], directory: Path = CONTENT_DIR) -> None:
    validate(group, entries)
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for e in sorted(entries, key=lambda e: e.name.lower()):
        row = {k: v for k, v in asdict(e).items() if v not in (None, [])}
        rows.append(row)
    header = (
        f"# {TITLES[group]['en']}: names and links from {GROUPS[group].page} in the official\n"
        "# Claude Code docs; descriptions are our own words. Set reviewed: true after reading.\n"
    )
    text = yaml.safe_dump(rows, allow_unicode=True, sort_keys=False, width=100)
    path_for(group, directory).write_text(header + text, encoding="utf-8")


def validate(group: str, entries: list[Entry]) -> None:
    problems = []
    seen_ids, seen_names = set(), set()
    for e in entries:
        where = f"{group}/{e.name}"
        if e.group != group:
            problems.append(f"{where}: group is {e.group}")
        if not _ID_RE.match(e.id) or e.id in seen_ids:
            problems.append(f"{where}: bad or duplicate id {e.id!r}")
        if e.name in seen_names:
            problems.append(f"{where}: duplicate name")
        seen_ids.add(e.id)
        seen_names.add(e.name)
        for lang in ("vi", "en"):
            text = (e.summary or {}).get(lang, "")
            if not 10 <= len(text) <= MAX_SUMMARY:
                problems.append(f"{where}: summary.{lang} length {len(text)}")
        if not e.docs.startswith("https://code.claude.com/docs/"):
            problems.append(f"{where}: docs link must point to code.claude.com/docs")
        try:
            date.fromisoformat(e.verified_at)
        except ValueError:
            problems.append(f"{where}: verified_at {e.verified_at!r} is not YYYY-MM-DD")
    if problems:
        raise ReferenceDataError("; ".join(problems[:20]))


def missing_and_gone(entries: list[Entry], doc_items: list[DocItem]) -> tuple[list, list]:
    """(doc items with no entry yet, entries whose name the docs no longer list)."""
    have = {e.name for e in entries}
    listed = {d.name for d in doc_items}
    return [d for d in doc_items if d.name not in have], [
        e for e in entries if e.name not in listed
    ]


def to_json(group: str, entries: list[Entry], generated_at: str) -> dict:
    """/v1/reference/{group}.json."""
    return {
        "schema": 1,
        "updated_at": generated_at,
        "group": group,
        "title": TITLES[group],
        "items": [
            {
                k: v
                for k, v in asdict(e).items()
                if k not in ("group", "reviewed") and v not in (None, [])
            }
            for e in sorted(entries, key=lambda e: e.name.lower())
        ],
    }
