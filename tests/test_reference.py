import tempfile
import unittest
from datetime import date
from pathlib import Path

from pipeline import __main__ as cli
from pipeline import docs, export, reference, reference_draft
from pipeline.llm import LLMError

PAGE = """# Commands

## Commands across a typical workflow

| Step | Command |
| :- | :- |
| Start | `/init` |

## All commands

| Command | Purpose |
| :- | :- |
| `/add-dir <path>` | Add a working directory. See [the guide](https://x.y/z). |
| `/advisor [model\\|off]` | Turn the [advisor](https://a.b) on or off |
| Not a command | ignored row |
| `/add-dir <path>` | duplicate row is ignored |

## See also
"""

SETTINGS = """## Settings index

| Key | Description | Topic | Scope |
| :- | :- | :- | :- |
| [`autoUpdates`](#autoupdates) | Turn automatic updates on or off | General | Any file |
"""

CLI = """## CLI flags

| Flag | Description | Example |
| :- | :- | :- |
| `--model` | Pick the model | `claude --model fable` |
| `--add-dir <path>`, `-d` | Add a folder | `claude --add-dir ../lib` |

## CLI commands

| Command | Description | Example |
| :- | :- | :- |
| `claude mcp` | Manage MCP servers | `claude mcp list` |
"""


def item(name="/add-dir", text="Add a working directory for file access in this session"):
    return docs.DocItem("commands", name, name + " <path>", text, "All commands")


class DocsParserTest(unittest.TestCase):
    def test_commands_table(self):
        found = docs.items("commands", PAGE)
        self.assertEqual([i.name for i in found], ["/add-dir", "/advisor"])
        self.assertEqual(found[1].syntax, "/advisor [model|off]")
        self.assertEqual(found[0].docs_text, "Add a working directory. See the guide.")
        self.assertEqual(found[0].url, "https://code.claude.com/docs/en/commands")

    def test_settings_keep_scope_and_anchor(self):
        (key,) = docs.items("settings", SETTINGS)
        self.assertEqual((key.name, dict(key.extra)["Scope"]), ("autoUpdates", "Any file"))
        self.assertTrue(key.url.endswith("/settings-reference#autoupdates"))

    def test_cli_flags_and_commands(self):
        names = [i.name for i in docs.items("cli", CLI)]
        self.assertEqual(names, ["--model", "--add-dir", "claude mcp"])

    def test_cli_groups_match_parser_groups(self):
        self.assertEqual(set(cli._REFERENCE_GROUPS), set(docs.GROUPS))
        self.assertEqual(set(reference.TITLES), set(docs.GROUPS))


class ReferenceFileTest(unittest.TestCase):
    def entry(self, name="/add-dir", **summary):
        summary = summary or {"vi": "Thêm một thư mục làm việc", "en": "Adds a working folder"}
        return reference.entry_from_doc(item(name), summary, date(2026, 11, 20))

    def test_round_trip_and_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            reference.save("commands", [self.entry("/b"), self.entry("/a")], Path(tmp))
            loaded = reference.load("commands", Path(tmp))
            text = Path(tmp, "commands.yaml").read_text()
        self.assertEqual([e.name for e in loaded], ["/a", "/b"])  # sorted by name
        self.assertEqual(loaded[0].id, "commands-a")
        self.assertFalse(loaded[0].reviewed)
        self.assertIn("Thêm một thư mục", text)  # stored as readable UTF-8

    def test_validation(self):
        bad = [
            [self.entry(), self.entry()],
            [self.entry(vi="ngắn", en="Adds a working folder")],
        ]
        broken = self.entry()
        broken.docs = "https://example.com"
        bad.append([broken])
        for entries in bad:
            with self.subTest(entries=entries), self.assertRaises(reference.ReferenceDataError):
                reference.validate("commands", entries)

    def test_missing_and_gone(self):
        entries = [self.entry("/old"), self.entry("/add-dir")]
        missing, gone = reference.missing_and_gone(entries, [item("/add-dir"), item("/new")])
        self.assertEqual(([m.name for m in missing], [g.name for g in gone]), (["/new"], ["/old"]))

    def test_json_hides_internal_fields(self):
        out = reference.to_json("commands", [self.entry()], "2026-11-20T00:00:00Z")
        self.assertEqual(out["title"]["vi"], "Lệnh slash")
        self.assertNotIn("reviewed", out["items"][0])
        self.assertNotIn("group", out["items"][0])

    def test_repo_content_files_are_valid(self):
        for group in reference.TITLES:
            with self.subTest(group=group):
                reference.load(group)  # raises if a committed file is broken
        self.assertIsInstance(export.reference_docs("2026-11-20T00:00:00Z"), dict)


class Provider:
    name, model = "fake", "fake-1"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def generate_json(self, system, user, schema):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class DraftTest(unittest.TestCase):
    def test_copied_phrase(self):
        docs_text = "Add a working directory for file access in this session"
        self.assertEqual(
            reference_draft.copied_phrase(
                "Lets you add a working directory for file access", docs_text
            ),
            "add a working directory for file",
        )
        self.assertIsNone(
            reference_draft.copied_phrase("Adds another folder Claude can read", docs_text)
        )

    def test_copies_and_missing_items_are_retried_alone(self):
        items = [item("/a"), item("/b"), item("/c")]
        first = {
            "items": [
                {"name": "/a", "en": "Adds another folder Claude can read", "vi": "Thêm thư mục"},
                {
                    "name": "/b",
                    "en": "Add a working directory for file access",
                    "vi": "Thêm thư mục",
                },
            ]
        }
        retry_b = {
            "items": [
                {
                    "name": "/b",
                    "en": "Grants access to one more folder",
                    "vi": "Cho phép thêm thư mục",
                }
            ]
        }
        provider = Provider([first, retry_b, LLMError("quota")])
        logs = []
        done, failed = reference_draft.draft(provider, items, date(2026, 11, 20), logs.append)
        self.assertEqual(sorted(e.name for e in done), ["/a", "/b"])
        self.assertEqual(failed, ["/c"])
        self.assertTrue(any("copies the docs" in m for m in logs))
        self.assertEqual(provider.calls, 3)


if __name__ == "__main__":
    unittest.main()
