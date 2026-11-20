"""docs_watch on hand-made docs rows: a simulated docs change must reach the report (PLAN 12.2)."""

import unittest
from dataclasses import replace
from datetime import date

from pipeline import docs_watch, reference
from pipeline.docs import DocItem

TODAY = date(2026, 11, 21)


def item(name, text, syntax=None, extra=()):
    return DocItem("env", name, syntax or name, text, "Variables", extra)


DOCS = [
    item("DISABLE_COMPACT", "Set to 1 to disable all compaction"),
    item("MCP_TIMEOUT", "Timeout in milliseconds for MCP server startup"),
]


def entries():
    summary = {"vi": "Một mô tả tiếng Việt đủ dài.", "en": "A long enough English summary."}
    return [
        replace(reference.entry_from_doc(d, summary, date(2026, 11, 20)), reviewed=True)
        for d in DOCS
    ]


def fake_draft(items):
    summary = {"vi": "Mô tả nháp do AI viết.", "en": "A draft summary by the AI."}
    return [reference.entry_from_doc(i, summary, TODAY) for i in items], []


class DocsWatchTest(unittest.TestCase):
    def test_no_change(self):
        kept, report = docs_watch.watch("env", entries(), DOCS, TODAY, fake_draft)
        self.assertFalse(report.needs_review)
        self.assertTrue(all(e.reviewed for e in kept))
        self.assertIn("No changes", docs_watch.markdown([report], TODAY))

    def test_simulated_docs_change(self):
        changed = [DOCS[0], item("MCP_TIMEOUT", "Timeout in seconds for MCP server startup")]
        kept, report = docs_watch.watch("env", entries(), changed, TODAY, fake_draft)
        self.assertEqual([n for n, _ in report.changed], ["MCP_TIMEOUT"])
        entry = next(e for e in kept if e.name == "MCP_TIMEOUT")
        self.assertFalse(entry.reviewed)  # back to the owner
        self.assertEqual(entry.summary["en"], "A long enough English summary.")  # kept as is
        self.assertEqual(entry.verified_at, TODAY.isoformat())
        body = docs_watch.markdown([report], TODAY)
        self.assertIn("`MCP_TIMEOUT`: Timeout in seconds", body)

    def test_added_and_removed(self):
        docs = [DOCS[0], item("NEW_VAR", "Set to 1 to try a new thing")]
        kept, report = docs_watch.watch("env", entries(), docs, TODAY, fake_draft)
        self.assertEqual(report.removed, ["MCP_TIMEOUT"])
        self.assertEqual(report.added, ["NEW_VAR"])
        self.assertEqual(sorted(e.name for e in kept), ["DISABLE_COMPACT", "NEW_VAR"])

    def test_without_ai_new_items_are_listed(self):
        docs = [*DOCS, item("NEW_VAR", "Set to 1 to try a new thing")]
        kept, report = docs_watch.watch("env", entries(), docs, TODAY, None)
        self.assertEqual(report.not_drafted, ["NEW_VAR"])
        self.assertEqual(len(kept), 2)

    def test_first_run_only_stores_fingerprints(self):
        old = [replace(e, docs_hash=None) for e in entries()]
        kept, report = docs_watch.watch("env", old, DOCS, TODAY, fake_draft)
        self.assertEqual(report.baselined, 2)
        self.assertFalse(report.needs_review)
        self.assertTrue(all(e.docs_hash for e in kept))

    def test_a_scope_change_counts(self):
        a = item("X_VAR", "Some text", extra=(("Scope", "Any file"),))
        b = item("X_VAR", "Some text", extra=(("Scope", "Managed"),))
        self.assertNotEqual(reference.docs_hash(a), reference.docs_hash(b))


if __name__ == "__main__":
    unittest.main()
