"""The 9 Radar categories, mirrored from migrations/0002_seed_categories.sql.

The database is the source of truth in production. This copy lets export run from JSON files
before the database exists; a test fails if the two ever disagree.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    slug: str
    title_vi: str
    title_en: str
    lesson_unit: int | None
    sort_order: int


CATEGORIES: tuple[Category, ...] = (
    Category("skills", "Skills", "Skills", 3, 1),
    Category("mcp", "MCP server", "MCP servers", 5, 2),
    Category("subagents", "Subagent", "Subagents", 4, 3),
    Category("hooks", "Hook", "Hooks", 6, 4),
    Category("plugins", "Plugin & marketplace", "Plugins & marketplaces", 8, 5),
    Category("commands", "Lệnh slash", "Slash commands", 3, 6),
    Category("templates", "CLAUDE.md & mẫu", "CLAUDE.md & templates", 2, 7),
    Category("tools", "Công cụ & giao diện", "Tools & GUIs", None, 8),
    Category("learn", "Học thêm", "Learning & awesome lists", None, 9),
)
BY_SLUG = {c.slug: c for c in CATEGORIES}
