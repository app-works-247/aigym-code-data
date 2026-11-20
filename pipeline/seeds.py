"""Load and validate pipeline/seeds.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SEEDS_PATH = Path(__file__).parent / "seeds.yaml"

_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")  # GitHub topic rules
_NAME_RE = re.compile(r"^[a-z0-9_.-]+$")


class SeedsError(ValueError):
    """seeds.yaml is missing a field or holds a value of the wrong shape."""


@dataclass(frozen=True)
class Seeds:
    min_stars: int
    topics: tuple[str, ...]
    keywords: tuple[str, ...]
    code_search: tuple[str, ...]
    exclude_repos: frozenset[str]
    exclude_owners: frozenset[str]

    def is_excluded(self, owner: str, name: str) -> bool:
        owner = owner.lower()
        return owner in self.exclude_owners or f"{owner}/{name.lower()}" in self.exclude_repos


def _str_list(data: dict, key: str, required: bool = True) -> list[str]:
    value = data.get(key)
    if value is None:
        if required:
            raise SeedsError(f"seeds: '{key}' is required")
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise SeedsError(f"seeds: '{key}' must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise SeedsError(f"seeds: '{key}' has duplicates")
    return value


def parse(data: object) -> Seeds:
    if not isinstance(data, dict):
        raise SeedsError("seeds: top level must be a mapping")
    if data.get("version") != 1:
        raise SeedsError("seeds: 'version' must be 1")

    min_stars = data.get("min_stars")
    if not isinstance(min_stars, int) or isinstance(min_stars, bool) or min_stars < 1:
        raise SeedsError("seeds: 'min_stars' must be a positive integer")

    topics = _str_list(data, "topics")
    if not topics:
        raise SeedsError("seeds: 'topics' must not be empty")
    bad = [t for t in topics if not _TOPIC_RE.match(t)]
    if bad:
        raise SeedsError(f"seeds: invalid topic name(s): {', '.join(bad)}")

    exclude = data.get("exclude") or {}
    if not isinstance(exclude, dict):
        raise SeedsError("seeds: 'exclude' must be a mapping")
    repos = _str_list(exclude, "repos", required=False)
    owners = _str_list(exclude, "owners", required=False)
    for item in repos:
        parts = item.split("/")
        if len(parts) != 2 or not all(_NAME_RE.match(p) for p in parts):
            raise SeedsError(f"seeds: exclude.repos entry must be lowercase owner/name: {item!r}")
    for item in owners:
        if not _NAME_RE.match(item):
            raise SeedsError(f"seeds: exclude.owners entry must be lowercase: {item!r}")

    return Seeds(
        min_stars=min_stars,
        topics=tuple(topics),
        keywords=tuple(_str_list(data, "keywords", required=False)),
        code_search=tuple(_str_list(data, "code_search", required=False)),
        exclude_repos=frozenset(repos),
        exclude_owners=frozenset(owners),
    )


def load(path: Path = SEEDS_PATH) -> Seeds:
    with path.open(encoding="utf-8") as fh:
        return parse(yaml.safe_load(fh))
