import json
import re
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pipeline import export
from pipeline.categories import CATEGORIES
from pipeline.migrate import MIGRATIONS_DIR
from pipeline.seeds import parse

NOW = datetime(2026, 11, 20, 2, 30, tzinfo=UTC)
TODAY = NOW.date()


def record(owner="acme", name="tool", stars=100, categories=("skills",), **extra):
    data = dict(
        github_id=abs(hash((owner, name))) % 10**9,
        owner=owner,
        name=name,
        description="d",
        homepage=None,
        language="Python",
        license_spdx="MIT",
        topics=["claude-code"],
        created_at="2025-01-01T00:00:00Z",
        pushed_at="2026-11-01T00:00:00Z",
        archived=False,
        is_official=False,
        is_docs=False,
        stars=stars,
        forks=1,
        open_issues=0,
        categories=list(categories),
        flags=[],
    )
    data.update(extra)
    return export.RepoRecord(**data)


def history(stars_now, gained_per_day, days):
    return [(TODAY - timedelta(days=n), stars_now - n * gained_per_day) for n in range(days, 0, -1)]


class CategoriesMirrorTest(unittest.TestCase):
    def test_python_copy_matches_seed_migration(self):
        sql = (MIGRATIONS_DIR / "0002_seed_categories.sql").read_text(encoding="utf-8")
        rows = re.findall(r"\('([a-z-]+)',\s*'([^']*)',\s*'([^']*)',\s*(\d+|NULL),\s*(\d+)\)", sql)
        from_sql = [
            (s, vi, en, None if u == "NULL" else int(u), int(o)) for s, vi, en, u, o in rows
        ]
        from_py = [
            (c.slug, c.title_vi, c.title_en, c.lesson_unit, c.sort_order) for c in CATEGORIES
        ]
        self.assertEqual(from_py, from_sql)


class NumbersTest(unittest.TestCase):
    def test_deltas_need_old_enough_snapshots(self):
        r = record(stars=200, history=history(200, 2, 8))  # 8 days of data
        self.assertEqual(export.star_deltas(r, TODAY), {"d1": 2, "d7": 14, "d30": None})

    def test_no_history_means_unknown_not_zero(self):
        self.assertEqual(export.star_deltas(record(), TODAY), {"d1": None, "d7": None, "d30": None})

    def test_rising_favours_fast_small_repos(self):
        self.assertGreater(export.rising(100, 50), export.rising(10_000, 50))
        self.assertIsNone(export.rising(100, None))

    def test_activity(self):
        self.assertEqual(export.activity(record(pushed_at="2026-10-01T00:00:00Z"), NOW), "active")
        self.assertEqual(export.activity(record(pushed_at="2026-03-01T00:00:00Z"), NOW), "slow")
        self.assertEqual(export.activity(record(pushed_at="2025-01-01T00:00:00Z"), NOW), "stale")
        self.assertEqual(export.activity(record(archived=True), NOW), "stale")


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            record("anthropics", "skills", 197_000, ["skills", "plugins"], is_official=True),
            record("small", "fast-skill", 120, ["skills"], history=history(120, 10, 10)),
            record("mid", "mcp-thing", 5_000, ["mcp"], history=history(5_000, 20, 10)),
            record("Some_Org-x", "My_Repo.js", 300, ["skills"]),
        ]
        self.docs = export.build(self.records, now=NOW)

    def test_file_set(self):
        expected = {"meta.json", "trending.json", "top/all.json"}
        expected |= {f"top/{c.slug}.json" for c in CATEGORIES}
        expected |= {f"repos/{r.id}.json" for r in self.records}
        self.assertEqual(set(self.docs), expected)

    def test_meta_counts_per_category(self):
        meta = self.docs["meta.json"]
        counts = {c["slug"]: c["count"] for c in meta["categories"]}
        self.assertEqual(meta["repo_count"], 4)
        self.assertEqual(counts["skills"], 3)
        self.assertEqual(counts["templates"], 0)
        self.assertEqual(meta["categories"][0]["title"], {"vi": "Skills", "en": "Skills"})

    def test_top_lists(self):
        top = self.docs["top/skills.json"]
        self.assertEqual(
            [i["id"] for i in top["stars"]][:2], ["anthropics__skills", "some_org-x__my_repo.js"]
        )
        self.assertEqual([i["rank"] for i in top["stars"]], [1, 2, 3])
        # Only repos with 7 days of history can be ranked by rising.
        self.assertEqual([i["id"] for i in top["rising"]], ["small__fast-skill"])

    def test_trending_ranks_small_fast_repo_first(self):
        ids = [i["id"] for i in self.docs["trending.json"]["items"]]
        self.assertEqual(ids, ["small__fast-skill", "mid__mcp-thing"])

    def test_detail(self):
        d = self.docs["repos/anthropics__skills.json"]
        self.assertEqual(d["url"], "https://github.com/anthropics/skills")
        self.assertEqual(d["lesson_units"], [3, 8])
        self.assertIsNone(d["d7"])
        self.assertIsNone(d["summary"])
        self.assertEqual(
            [r["id"] for r in d["related"]], ["some_org-x__my_repo.js", "small__fast-skill"]
        )
        fast = self.docs["repos/small__fast-skill.json"]
        self.assertEqual(len(fast["history"]), 10)
        self.assertEqual(
            fast["history"][-1], [int((TODAY - timedelta(days=1)).strftime("%Y%m%d")), 110]
        )

    def test_history_keeps_only_90_days(self):
        r = record(history=history(1_000, 1, 120))
        d = export.build([r], now=NOW)[f"repos/{r.id}.json"]
        self.assertEqual(len(d["history"]), 89)  # strictly newer than 90 days ago, up to yesterday
        self.assertGreater(d["history"][0][0], int((TODAY - timedelta(days=90)).strftime("%Y%m%d")))

    def test_rejects_bad_records(self):
        for bad in (
            [record(), record()],
            [record(categories=["nope"])],
            [record(categories=[])],
        ):
            with self.subTest(bad=bad), self.assertRaises(export.ExportError):
                export.build(bad, now=NOW)


class WriteTest(unittest.TestCase):
    def test_writes_compact_json_and_replaces_previous_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "v1"
            export.write(export.build([record("a", "one")], now=NOW), out)
            (out / "status.json").write_text("{}")  # someone else's file: must survive
            export.write(export.build([record("b", "two")], now=NOW), out)
            self.assertFalse((out / "repos/a__one.json").exists())
            self.assertTrue((out / "repos/b__two.json").exists())
            self.assertTrue((out / "status.json").exists())
            text = (out / "meta.json").read_text(encoding="utf-8")
            self.assertNotIn("\n", text)
            self.assertEqual(json.loads(text)["repo_count"], 1)

    def test_refuses_unrelated_non_empty_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "important.txt").write_text("keep me")
            with self.assertRaises(export.ExportError):
                export.write({}, Path(tmp))
            self.assertTrue(Path(tmp, "important.txt").exists())


class FromJsonTest(unittest.TestCase):
    def test_joins_active_results_and_drops_excluded(self):
        def cand(i, owner, name):
            return {
                "id": i,
                "node_id": f"R{i}",
                "owner": owner,
                "name": name,
                "description": None,
                "homepage": None,
                "language": None,
                "license_spdx": None,
                "topics": [],
                "default_branch": "main",
                "created_at": "2026-01-01T00:00:00Z",
                "pushed_at": None,
                "archived": False,
                "stars": 60,
                "forks": 0,
                "open_issues": 0,
                "is_official": False,
                "matched_by": [],
            }

        def res(full, status, cats):
            return {
                "full_name": full,
                "status": status,
                "is_docs": False,
                "flags": ["no_license"],
                "categories": [{"slug": s, "confidence": c} for s, c in cats],
            }

        candidates = {"repos": [cand(1, "a", "x"), cand(2, "b", "y"), cand(3, "Gone", "z")]}
        classified = [
            res("a/x", "active", [("skills", 0.95), ("mcp", 0.6)]),
            res("b/y", "pending", [("mcp", 0.6)]),
            res("gone/z", "active", [("skills", 0.95)]),
        ]
        seeds = parse(
            {"version": 1, "min_stars": 50, "topics": ["x"], "exclude": {"owners": ["gone"]}}
        )
        records = export.records_from_json(candidates, classified, seeds)
        self.assertEqual([r.id for r in records], ["a__x"])
        self.assertEqual(records[0].categories, ["skills"])  # weak mcp match left out
        self.assertEqual(records[0].flags, ["no_license"])


if __name__ == "__main__":
    unittest.main()
