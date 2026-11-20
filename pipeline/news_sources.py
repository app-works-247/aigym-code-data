"""Read official news sources into plain items (PLAN.md 3.1, 8.2).

Parsers are pure functions over downloaded text, so they are tested on saved samples. Only a
title, date, link and a short excerpt are kept; full articles are never republished (11.4).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

SOURCES_PATH = Path(__file__).parent / "sources.yaml"
USER_AGENT = "aigym-code-data (+https://github.com/app-works-247/aigym-code-data)"
MAX_EXCERPT = 8000  # database CHECK

_VERSION_HEADING = re.compile(r"(?m)^## (\d+\.\d+\.\d+)\s*$")
_DATE_HEADING = re.compile(r"(?m)^### ([A-Z][a-z]+ \d{1,2}, \d{4})\s*$")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Source:
    id: str
    kind: str
    url: str
    tags: list[str]
    enabled: bool = True
    page_url: str | None = None
    releases_repo: str | None = None
    include: list[str] = field(default_factory=list)
    filter_keywords: bool = False


@dataclass
class SourcesConfig:
    backfill_days: int
    dev_keywords: list[str]
    sources: list[Source]


@dataclass
class NewsItem:
    source_id: str
    url: str
    title: str
    published_at: datetime
    excerpt: str
    kind: str = "news"  # news | release
    version: str | None = None
    dedup_key: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return item_id(self.url)


def load_config(path: Path = SOURCES_PATH) -> SourcesConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("sources.yaml: version must be 1")
    kinds = {"changelog", "dated_markdown", "sitemap", "rss"}
    sources = []
    for raw in data["sources"]:
        src = Source(**raw)
        if src.kind not in kinds:
            raise ValueError(f"sources.yaml: {src.id}: unknown kind {src.kind}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", src.id):
            raise ValueError(f"sources.yaml: bad id {src.id!r}")
        sources.append(src)
    if len({s.id for s in sources}) != len(sources):
        raise ValueError("sources.yaml: duplicate source ids")
    return SourcesConfig(int(data["backfill_days"]), list(data["dev_keywords"]), sources)


# ----- helpers --------------------------------------------------------------------------


def normalize_url(url: str) -> str:
    """Lowercase scheme and host, drop tracking parameters and a trailing slash."""
    parts = urllib.parse.urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query)
        if not k.lower().startswith(("utm_", "ref"))
    ]
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            urllib.parse.urlencode(query),
            parts.fragment,
        )
    )


def item_id(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:12]


def plain(markdown: str) -> str:
    text = _MD_LINK.sub(r"\1", markdown)
    return html.unescape(text).strip()


def first_sentence(text: str, limit: int = 160) -> str:
    line = plain(text).lstrip("*- ").strip().splitlines()[0] if text.strip() else ""
    match = re.match(r"(.+?[.!?])(\s|$)", line)
    sentence = (match.group(1) if match else line).strip()
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def fetch_text(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def mentions_any(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(k.lower())}(?![a-z])", lower) for k in keywords)


# ----- parsers --------------------------------------------------------------------------


def parse_changelog(
    text: str, source: Source, release_dates: dict[str, datetime]
) -> list[NewsItem]:
    """One item per "## x.y.z" section that GitHub Releases dates; undated versions wait."""
    items = []
    headings = list(_VERSION_HEADING.finditer(text))
    repo = source.releases_repo or ""
    for i, heading in enumerate(headings):
        version = heading.group(1)
        published = release_dates.get(version)
        if published is None:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[heading.end() : end].strip()
        items.append(
            NewsItem(
                source_id=source.id,
                url=f"https://github.com/{repo}/releases/tag/v{version}",
                title=f"Claude Code {version}",
                published_at=published,
                excerpt=body[:MAX_EXCERPT],
                kind="release",
                version=version,
                dedup_key=f"claude-code@{version}",
                tags=list(source.tags),
            )
        )
    return items


def parse_dated_markdown(text: str, source: Source) -> list[NewsItem]:
    """One item per "### Month D, YYYY" section (Claude API release notes)."""
    items = []
    headings = list(_DATE_HEADING.finditer(text))
    for i, heading in enumerate(headings):
        label = heading.group(1)
        published = datetime.strptime(label, "%B %d, %Y").replace(tzinfo=UTC)
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[heading.end() : end].strip()
        if not body:
            continue
        anchor = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        items.append(
            NewsItem(
                source_id=source.id,
                url=f"{source.page_url or source.url}#{anchor}",
                title=first_sentence(body),
                published_at=published,
                excerpt=plain(body)[:MAX_EXCERPT],
                tags=list(source.tags),
            )
        )
    return items


def parse_rss(xml: str, source: Source) -> list[NewsItem]:
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):

        def tag(name: str, block: str = block) -> str:
            match = re.search(rf"<{name}>(.*?)</{name}>", block, re.S)
            raw = match.group(1) if match else ""
            raw = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", raw.strip(), flags=re.S)
            return html.unescape(raw).strip()

        link, title, date = tag("link"), tag("title"), tag("pubDate")
        if not (link and title and date):
            continue
        description = re.sub(r"\s+", " ", _TAG.sub(" ", tag("description"))).strip()
        items.append(
            NewsItem(
                source_id=source.id,
                url=link,
                title=title,
                published_at=parsedate_to_datetime(date).astimezone(UTC),
                excerpt=description[:MAX_EXCERPT],
                tags=list(source.tags),
            )
        )
    return items


def sitemap_urls(xml: str, include: list[str]) -> list[tuple[str, datetime | None]]:
    patterns = [re.compile(p) for p in include]
    out = []
    for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
        loc = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", block)
        if not loc or not any(p.search(loc.group(1)) for p in patterns):
            continue
        mod = re.search(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", block)
        lastmod = None
        if mod:
            try:
                lastmod = datetime.fromisoformat(mod.group(1).replace("Z", "+00:00"))
                if lastmod.tzinfo is None:
                    lastmod = lastmod.replace(tzinfo=UTC)
            except ValueError:
                lastmod = None
        out.append((loc.group(1), lastmod))
    return out


def parse_article(page: str, url: str, source: Source) -> NewsItem | None:
    """Title, publish date and a short excerpt from an article page; None when undated."""

    def meta(prop: str) -> str:
        match = re.search(
            rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+content="([^"]*)"', page
        ) or re.search(
            rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(prop)}"', page
        )
        return html.unescape(match.group(1)).strip() if match else ""

    date = None
    for raw in re.findall(r'"datePublished"\s*:\s*"([^"]+)"', page) + [
        meta("article:published_time")
    ]:
        try:
            date = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
            break
        except ValueError:
            continue
    title = (
        meta("og:title")
        or html.unescape((re.search(r"<title>(.*?)</title>", page, re.S) or [None, ""])[1])
        .split(" \\ ")[0]
        .strip()
    )
    if not date or not title:
        return None
    body = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", page)
    paragraphs = [
        re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", p))).strip()
        for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)
    ]
    text = " ".join([meta("og:description")] + [p for p in paragraphs if len(p) > 40])
    return NewsItem(
        source_id=source.id,
        url=url,
        title=title,
        published_at=date,
        excerpt=text[:6000],
        tags=list(source.tags),
    )


def release_dates(releases: list[dict]) -> dict[str, datetime]:
    """GitHub Releases API rows -> {"2.1.329": published_at}."""
    out = {}
    for row in releases:
        tag = str(row.get("tag_name") or "").lstrip("v")
        when = row.get("published_at")
        if re.fullmatch(r"\d+\.\d+\.\d+", tag) and when:
            out[tag] = datetime.fromisoformat(when.replace("Z", "+00:00"))
    return out


def to_json_line(item: NewsItem) -> str:
    """For debugging and the dry-run command."""
    return json.dumps(
        {
            "id": item.id,
            "source": item.source_id,
            "title": item.title,
            "date": item.published_at.isoformat(),
            "url": item.url,
            "excerpt_chars": len(item.excerpt),
        },
        ensure_ascii=False,
    )
