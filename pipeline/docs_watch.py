"""docs_watch (PLAN.md 8.4 and 12.2): keep content/reference in step with the official docs.

Every day the job re-reads the five docs pages and compares each row with our entries:

- new in the docs: drafted by the AI (reviewed: false), or listed for later when it cannot;
- gone from the docs: removed, so the app never shows something Claude Code no longer has;
- changed in the docs (syntax, description or another column): syntax, scope, example and the
  fingerprint are updated from the docs and the entry goes back to reviewed: false, because our
  summary may now be wrong.

Nothing reaches the app on its own: the workflow opens a pull request with the report below,
and the owner merges it after reading.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date

from pipeline import reference
from pipeline.docs import DocItem

# draft(doc_items) -> (new entries, names that could not be drafted)
DraftFn = Callable[[list[DocItem]], tuple[list[reference.Entry], list[str]]]


@dataclass
class GroupReport:
    group: str
    added: list[str] = field(default_factory=list)
    not_drafted: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[tuple[str, str]] = field(default_factory=list)  # (name, new docs text)
    baselined: int = 0  # entries that had no fingerprint yet (first run): stored, not flagged

    @property
    def needs_review(self) -> bool:
        return bool(self.added or self.not_drafted or self.removed or self.changed)


def watch(
    group: str,
    entries: list[reference.Entry],
    doc_items: list[DocItem],
    today: date,
    draft: DraftFn | None,
) -> tuple[list[reference.Entry], GroupReport]:
    report = GroupReport(group)
    by_name = {d.name: d for d in doc_items}
    kept: list[reference.Entry] = []
    for entry in entries:
        item = by_name.get(entry.name)
        if item is None:
            report.removed.append(entry.name)
            continue
        fingerprint = reference.docs_hash(item)
        if entry.docs_hash is None:
            report.baselined += 1
            kept.append(replace(entry, docs_hash=fingerprint))
        elif entry.docs_hash != fingerprint:
            fresh = reference.entry_from_doc(item, entry.summary, today)
            report.changed.append((entry.name, item.docs_text))
            kept.append(replace(fresh, id=entry.id, since=entry.since, lessons=entry.lessons))
        else:
            kept.append(entry)

    have = {e.name for e in kept}
    missing = [d for d in doc_items if d.name not in have]
    if missing:
        if draft is None:
            report.not_drafted = [d.name for d in missing]
        else:
            new, failed = draft(missing)
            kept += new
            report.added = [e.name for e in new]
            report.not_drafted = failed
    return kept, report


def markdown(reports: list[GroupReport], today: date) -> str:
    """The pull request body."""
    lines = [f"# Docs changes found on {today.isoformat()}", ""]
    if not any(r.needs_review for r in reports):
        lines.append("No changes in the official docs.")
        return "\n".join(lines) + "\n"
    lines += [
        "The official Claude Code docs changed. Please read each item below before merging:",
        "every entry listed here is `reviewed: false` until you set it back to `true`.",
        "",
    ]
    for r in reports:
        if not r.needs_review:
            continue
        lines.append(f"## {reference.TITLES[r.group]['en']}")
        if r.changed:
            lines.append("**Changed in the docs** (check that our vi/en summary is still right):")
            for name, text in r.changed:
                short = text if len(text) <= 300 else text[:297] + "…"
                lines.append(f"- `{name}`: {short}")
        if r.added:
            lines.append("**New, summary drafted by AI:** " + ", ".join(f"`{n}`" for n in r.added))
        if r.not_drafted:
            lines.append(
                "**New, not drafted yet** (write by hand or wait for the next run): "
                + ", ".join(f"`{n}`" for n in r.not_drafted)
            )
        if r.removed:
            lines.append(
                "**Removed from the docs, removed here too:** "
                + ", ".join(f"`{n}`" for n in r.removed)
            )
        lines.append("")
    return "\n".join(lines)
