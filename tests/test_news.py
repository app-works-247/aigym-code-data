"""News pipeline tests. Samples are written by hand in the same structure as the real sources
(checked live on 2026-11-20); no source text is copied into this public repo (PLAN.md 11.4)."""

import os
import unittest
from datetime import UTC, datetime, timedelta

from pipeline import news
from pipeline import news_sources as ns
from pipeline.llm import LLMError

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

CHANGELOG = """# Changelog

## 2.1.329

- Added a `/tasks` row for scheduled loops
- Fixed `--resume` losing the selected model

## 2.1.328

- Added the `CLAUDE_CODE_FOO_BAR` environment variable

## 2.1.200

- Very old release without a GitHub release date in this sample
"""

API_NOTES = """# Claude API release notes

### November 17, 2026

* We launched [linked billing](https://platform.claude.com/x) in beta.
  Set `billing_policy` on each request.

### November 12, 2026

* Self-hosted managed agents are available on request. Pricing stays at $5 per session-hour.
"""

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>A predictable release cycle</title><link>https://blog.example.org/posts/cycle/</link>
<pubDate>Wed, 11 Nov 2026 09:00:00 +0000</pubDate>
<description>&lt;p&gt;Spec releases move to a fixed schedule.&lt;/p&gt;</description></item>
<item><title>No date here</title><link>https://blog.example.org/posts/x/</link></item>
</channel></rss>"""

SITEMAP = """<?xml version="1.0"?><urlset>
<url><loc>https://www.anthropic.com/news/new-model</loc><lastmod>2026-11-18T10:00:00.000Z</lastmod></url>
<url><loc>https://www.anthropic.com/news/old-story</loc><lastmod>2024-01-01T00:00:00.000Z</lastmod></url>
<url><loc>https://www.anthropic.com/careers</loc></url>
</urlset>"""

ARTICLE = """<html><head><title>Ignored \\ Anthropic</title>
<meta property="og:title" content="Introducing a new model for developers">
<meta property="og:description" content="A faster model is available in the Claude API today.">
<script type="application/ld+json">{"datePublished":"2026-11-18T00:00:00.000Z"}</script>
</head><body><nav>menu</nav>
<p>The model is available to every developer through the Claude API and Claude Code.</p>
<p>short</p></body></html>"""

COMPANY_ARTICLE = """<html><head>
<meta property="og:title" content="We opened a new office">
<meta property="og:description" content="Our team in a new city will grow next year.">
<script type="application/ld+json">{"datePublished":"2026-11-18T00:00:00.000Z"}</script>
</head><body>
<p>The new office opens with room for many new hires across the company.</p></body></html>"""


def config(**overrides):
    cfg = ns.load_config()
    for src in cfg.sources:
        src.enabled = src.id in overrides.get("enabled", {s.id for s in cfg.sources if s.enabled})
    return cfg


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.sources = {s.id: s for s in ns.load_config().sources}

    def test_config(self):
        cfg = ns.load_config()
        self.assertTrue(any(s.kind == "changelog" for s in cfg.sources))
        self.assertFalse(self.sources["claude-blog"].enabled)  # claude.com has no blog

    def test_changelog_waits_for_a_release_date(self):
        dates = ns.release_dates(
            [
                {"tag_name": "v2.1.329", "published_at": "2026-11-19T22:14:41Z"},
                {"tag_name": "v2.1.328", "published_at": "2026-11-19T01:14:04Z"},
                {"tag_name": "nightly", "published_at": "2026-11-19T00:00:00Z"},
            ]
        )
        items = ns.parse_changelog(CHANGELOG, self.sources["claude-code-changelog"], dates)
        self.assertEqual([i.version for i in items], ["2.1.329", "2.1.328"])
        self.assertEqual(items[0].dedup_key, "claude-code@2.1.329")
        self.assertEqual(
            items[0].url, "https://github.com/anthropics/claude-code/releases/tag/v2.1.329"
        )
        self.assertIn("`/tasks`", items[0].excerpt)
        self.assertNotIn("2.1.328", items[0].excerpt)

    def test_dated_markdown(self):
        items = ns.parse_dated_markdown(API_NOTES, self.sources["claude-api-release-notes"])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].published_at, datetime(2026, 11, 17, tzinfo=UTC))
        self.assertTrue(items[0].url.endswith("/overview#november-17-2026"))
        self.assertEqual(items[0].title, "We launched linked billing in beta.")
        self.assertNotIn("](", items[0].excerpt)

    def test_rss(self):
        items = ns.parse_rss(RSS, self.sources["mcp-blog"])
        self.assertEqual(len(items), 1)  # the undated item is skipped
        self.assertEqual(items[0].excerpt, "Spec releases move to a fixed schedule.")

    def test_sitemap_and_article(self):
        urls = ns.sitemap_urls(SITEMAP, self.sources["anthropic-news"].include)
        self.assertEqual(
            [u for u, _ in urls],
            [
                "https://www.anthropic.com/news/new-model",
                "https://www.anthropic.com/news/old-story",
            ],
        )
        item = ns.parse_article(ARTICLE, urls[0][0], self.sources["anthropic-news"])
        self.assertEqual(item.title, "Introducing a new model for developers")
        self.assertEqual(item.published_at, datetime(2026, 11, 18, tzinfo=UTC))
        self.assertIn("every developer", item.excerpt)
        self.assertNotIn("menu", item.excerpt)
        self.assertIsNone(
            ns.parse_article("<html><title>x</title></html>", "u", self.sources["anthropic-news"])
        )

    def test_url_normalization_and_ids(self):
        a = ns.normalize_url("HTTPS://Example.com/Post/?utm_source=x&id=2")
        self.assertEqual(a, "https://example.com/Post?id=2")
        self.assertEqual(
            ns.item_id("https://example.com/Post/"), ns.item_id("https://EXAMPLE.com/Post")
        )
        self.assertRegex(ns.item_id("x"), r"^[0-9a-f]{12}$")


class FetchTest(unittest.TestCase):
    def pages(self):
        return {
            "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md": CHANGELOG,
            "https://platform.claude.com/docs/en/release-notes/overview.md": API_NOTES,
            "https://www.anthropic.com/sitemap.xml": SITEMAP,
            "https://www.anthropic.com/news/new-model": ARTICLE,
            "https://blog.modelcontextprotocol.io/index.xml": RSS.replace(
                "blog.example.org", "blog.modelcontextprotocol.io"
            ),
        }

    def releases(self, repo):
        return [{"tag_name": "v2.1.329", "published_at": "2026-11-19T22:14:41Z"}]

    def test_first_run_then_nothing_new(self):
        cfg = ns.load_config()
        known = set()
        fresh, ignored, runs = news.fetch_new(
            cfg, known, self.pages().__getitem__, self.releases, NOW, log=lambda m: None
        )
        self.assertEqual(
            sorted(r.source_id for r in runs if r.ok),
            ["anthropic-news", "claude-api-release-notes", "claude-code-changelog", "mcp-blog"],
        )
        titles = [i.title for i in fresh]
        self.assertIn("Claude Code 2.1.329", titles)
        self.assertIn("Introducing a new model for developers", titles)
        self.assertEqual(len(fresh), 5)  # 1 release, 2 API notes, 1 article, 1 MCP post
        again, _, _ = news.fetch_new(
            cfg, known, self.pages().__getitem__, self.releases, NOW, log=lambda m: None
        )
        self.assertEqual(again, [])

    def test_company_news_is_remembered_but_not_shown(self):
        pages = self.pages()
        pages["https://www.anthropic.com/news/new-model"] = COMPANY_ARTICLE
        fresh, ignored, _ = news.fetch_new(
            ns.load_config(), set(), pages.__getitem__, self.releases, NOW, log=lambda m: None
        )
        self.assertNotIn("anthropic-news", {i.source_id for i in fresh})
        self.assertIn("anthropic-news", {i.source_id for i in ignored})

    def test_old_items_are_not_backfilled_beyond_the_window(self):
        fresh, ignored, _ = news.fetch_new(
            ns.load_config(),
            set(),
            self.pages().__getitem__,
            self.releases,
            NOW + timedelta(days=60),
            log=lambda m: None,
        )
        self.assertEqual(fresh, [])
        self.assertTrue(ignored)

    def test_a_broken_source_does_not_stop_the_others(self):
        pages = self.pages()
        del pages["https://platform.claude.com/docs/en/release-notes/overview.md"]
        fresh, _, runs = news.fetch_new(
            ns.load_config(), set(), pages.__getitem__, self.releases, NOW, log=lambda m: None
        )
        failed = [r.source_id for r in runs if not r.ok]
        self.assertEqual(failed, ["claude-api-release-notes"])
        self.assertTrue(fresh)


class SafetyNetTest(unittest.TestCase):
    SOURCE = (
        "Claude Code 2.1.329: Added a `/tasks` row; fixed `--resume`. Claude Opus 4.6 is faster. "
        "Set CLAUDE_CODE_FOO_BAR=1. Pricing is $5 per hour. Shipped on November 17, 2026. "
        "Up to 1,000 requests a minute."
    )

    def test_faithful_summaries_pass(self):
        for summary in [
            "Claude Code 2.1.329 thêm dòng `/tasks` và sửa lỗi `--resume`.",
            "Claude Opus 4.6 nhanh hơn; đặt CLAUDE_CODE_FOO_BAR để bật. Giá $5 mỗi giờ.",
            "Phát hành ngày 17/11/2026.",
            "Phát hành ngày 17 tháng 11.",
            "See https://platform.claude.com/docs/en/thing for details.",
            "Giới hạn 1.000 yêu cầu mỗi phút.",  # Vietnamese thousands, source writes 1,000
        ]:
            with self.subTest(summary=summary):
                self.assertEqual(news.facts_missing(summary, self.SOURCE), [])

    def test_invented_facts_are_caught(self):
        cases = {
            "Claude Code 2.1.330 adds tasks.": "2.1.330",
            "Claude Opus 4.8 is faster.": "Claude Opus 4.8",
            "Run `/loop` now.": "/loop",
            "Use the --worktree flag.": "--worktree",
            "Set CLAUDE_CODE_OTHER=1.": "CLAUDE_CODE_OTHER",
            "It costs $15 per hour.": "$15",
            "Released on 18/11/2026.": "date 11/18",
            "Released on November 19, 2026.": "date 11/19",
        }
        for summary, fact in cases.items():
            with self.subTest(summary=summary):
                self.assertIn(fact, news.facts_missing(summary, self.SOURCE))


class Provider:
    name, model = "fake", "fake-1"

    def __init__(self, answers):
        self.answers = list(answers)

    def generate_json(self, system, user, schema):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


GOOD = {
    "title_en": "Claude Code 2.1.329 adds a tasks row",
    "title_vi": "Claude Code 2.1.329 thêm dòng tasks",
    "summary_en": "Added a `/tasks` row\nFixed `--resume`\nOpus 4.6 is faster",
    "summary_vi": "Thêm dòng `/tasks`\nSửa lỗi `--resume`\nOpus 4.6 nhanh hơn",
    "tags": ["claude-code", "not-a-tag"],
    "importance": 7,
}


class SummariseTest(unittest.TestCase):
    def test_good_summary(self):
        s = news.summarise(Provider([GOOD]), "Claude Code 2.1.329", "release", SafetyNetTest.SOURCE)
        self.assertTrue(s.check_passed, s.problems)
        self.assertEqual(s.tags, ["claude-code"])
        self.assertEqual(s.importance, 3)  # clamped

    def test_invented_fact_downgrades_to_title_only(self):
        bad = {**GOOD, "summary_vi": "Thêm `/loop` và hỗ trợ Claude Opus 4.8"}
        s = news.summarise(Provider([bad]), "Claude Code 2.1.329", "release", SafetyNetTest.SOURCE)
        self.assertFalse(s.check_passed)
        self.assertTrue(any("/loop" in p for p in s.problems))

    def test_empty_answers_raise(self):
        with self.assertRaises(LLMError):
            news.summarise(Provider([{**GOOD, "title_vi": ""}]), "t", "news", "source text")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to a throwaway db")
class NewsDatabaseTest(unittest.TestCase):
    def setUp(self):
        import psycopg

        from pipeline import migrate

        self.url = os.environ["TEST_DATABASE_URL"]
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        mdb = migrate.PsycopgDatabase(self.url)
        try:
            migrate.run(mdb, migrate.load_migrations())
        finally:
            mdb.close()

    def test_store_summarise_and_export(self):
        import psycopg

        cfg = ns.load_config()
        fetch = FetchTest()
        fresh, ignored, runs = news.fetch_new(
            cfg, set(), fetch.pages().__getitem__, fetch.releases, NOW, log=lambda m: None
        )
        with psycopg.connect(self.url) as conn:
            news.sync_sources(conn, cfg)
            self.assertEqual(news.store_items(conn, fresh, "pending"), 5)
            news.store_items(conn, ignored, "hidden")
            self.assertEqual(news.store_items(conn, fresh, "pending"), 0)  # never twice
            news.record_runs(conn, runs, NOW)
            todo = news.pending(conn, 10)
            self.assertEqual(len(todo), 5)
            release = next(row for row in todo if row[3] == "claude-code@2.1.329")
            news.save_summary(
                conn,
                release[0],
                news.summarise(
                    Provider([GOOD]), release[1], "release", release[2] + SafetyNetTest.SOURCE
                ),
                Provider([]),
                list(release[4]),
            )
            other = next(row for row in todo if row[3] is None)
            bad = {**GOOD, "summary_en": "Mentions Claude Opus 9.9", "importance": 2}
            news.save_summary(
                conn,
                other[0],
                news.summarise(Provider([bad]), other[1], "news", other[2]),
                Provider([]),
                list(other[4]),
            )
            conn.commit()
            docs = news.news_docs(conn, "2026-11-20T12:00:00Z", [{"id": "a__b"}])
            status = dict(conn.execute("SELECT id, status FROM news_items").fetchall())
            state = conn.execute(
                "SELECT source_id, last_ok_at IS NOT NULL FROM source_state"
            ).fetchall()
        self.assertEqual(status[release[0]], "published")
        self.assertEqual(status[other[0]], "title_only")
        today = docs["today.json"]
        self.assertEqual(today["claude_code"]["version"], "2.1.329")
        self.assertEqual(today["claude_code"]["summary"]["vi"].count("\n"), 2)
        self.assertEqual(today["rising"], [{"id": "a__b"}])
        title_only = docs[f"news/item/{other[0]}.json"]
        self.assertIsNone(title_only["summary"])
        self.assertFalse(title_only["ai_summary"])
        self.assertEqual(title_only["title"]["vi"], other[1])  # the original title, untouched
        self.assertIn("news/page/1.json", docs)
        self.assertTrue(all(ok for sid, ok in state if sid != "claude-blog"))


if __name__ == "__main__":
    unittest.main()
