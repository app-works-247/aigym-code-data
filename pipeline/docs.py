"""Official Claude Code docs as structured items (PLAN.md 8.4).

Each reference group maps to one docs page and to the table(s) on it that list every item.
The parser returns names exactly as the docs write them, plus the docs' own one-line text,
which is used only to detect changes and to brief whoever writes our description. It is
never published: our descriptions are written in our own words.
"""

from __future__ import annotations

import hashlib
import re
import urllib.request
from dataclasses import dataclass

DOCS_ROOT = "https://code.claude.com/docs/en"


@dataclass(frozen=True)
class Group:
    page: str  # docs page, without .md
    sections: tuple[str, ...]  # headings whose tables list the items
    first_column: str  # header of the column holding the item


GROUPS: dict[str, Group] = {
    "commands": Group("commands", ("All commands",), "Command"),
    "cli": Group("cli-reference", ("CLI commands", "CLI flags", "System prompt flags"), ""),
    "settings": Group("settings-reference", ("Settings index",), "Key"),
    "hooks": Group("hooks", ("Hook lifecycle",), "Event"),
    "env": Group("env-vars", ("Variables",), "Variable"),
}


@dataclass(frozen=True)
class DocItem:
    group: str
    name: str  # stable identifier: /compact, --model, autoUpdates, PreToolUse, CLAUDE_CODE_...
    syntax: str  # as written in the docs' first column, e.g. "/add-dir <path>"
    docs_text: str  # the docs' description, links removed (never published)
    section: str
    extra: tuple[tuple[str, str], ...] = ()  # other columns, e.g. (("Scope", "Any file"),)

    @property
    def url(self) -> str:
        page = GROUPS[self.group].page
        if self.group == "settings":
            return f"{DOCS_ROOT}/{page}#{self.name.lower()}"
        return f"{DOCS_ROOT}/{page}"


_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CODE = re.compile(r"`([^`]+)`")


def fetch(page: str) -> str:
    request = urllib.request.Request(f"{DOCS_ROOT}/{page}.md", headers={"User-Agent": "aigym"})
    with urllib.request.urlopen(request, timeout=60) as resp:
        return resp.read().decode("utf-8")


def page_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plain(cell: str) -> str:
    """Markdown cell -> plain text: links keep their text, escaped pipes are restored."""
    return _LINK.sub(r"\1", cell).replace("\\|", "|").strip()


def _cells(line: str) -> list[str]:
    inner = line.strip()[1:-1] if line.strip().endswith("|") else line.strip()[1:]
    return [c.strip() for c in re.split(r"(?<!\\)\|", inner)]


def tables(md: str) -> list[tuple[str, list[str], list[list[str]]]]:
    """Every pipe table as (nearest heading, header cells, rows)."""
    out = []
    heading = ""
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if match:
            heading = plain(_CODE.sub(r"\1", match.group(1)))
        if (
            line.startswith("|")
            and i + 1 < len(lines)
            and re.match(r"^\|\s*:?-+:?\s*\|", lines[i + 1])
        ):
            header = [plain(c) for c in _cells(line)]
            rows = []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append((heading, header, rows))
            continue
        i += 1
    return out


def _name(group: str, first_cell: str) -> tuple[str, str] | None:
    """(name, syntax) from the first cell, or None when the row is not an item."""
    codes = _CODE.findall(first_cell)
    if not codes:
        return None
    syntax = codes[0].replace("\\|", "|").strip()
    if group == "commands":
        name = syntax.split()[0]
        return (name, syntax) if name.startswith("/") else None
    if group == "cli":
        if syntax.startswith("-"):
            return syntax.split()[0].rstrip(","), syntax
        return (syntax, syntax) if syntax.startswith("claude") else None
    return syntax, syntax


def items(group: str, md: str) -> list[DocItem]:
    spec = GROUPS[group]
    found: dict[str, DocItem] = {}
    for heading, header, rows in tables(md):
        if heading not in spec.sections or len(header) < 2:
            continue
        if spec.first_column and header[0] != spec.first_column:
            continue
        for row in rows:
            if len(row) < 2:
                continue
            named = _name(group, row[0])
            if named is None:
                continue
            name, syntax = named
            extra = tuple((h, plain(c)) for h, c in zip(header[2:], row[2:], strict=False))
            found.setdefault(
                name,
                DocItem(group, name, syntax, plain(_CODE.sub(r"\1", row[1])), heading, extra),
            )
    return list(found.values())
