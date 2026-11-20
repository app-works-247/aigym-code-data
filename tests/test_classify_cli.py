import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline.__main__ import main
from pipeline.github import GitHubError


def candidate(i):
    return {
        "id": i,
        "owner": "o",
        "name": f"r{i}",
        "description": "",
        "topics": ["claude-code"],
        "matched_by": ["topic:claude-code"],
        "license_spdx": "MIT",
        "archived": False,
        "created_at": "2025-01-01T00:00:00Z",
        "pushed_at": "2026-11-01T00:00:00Z",
        "default_branch": "main",
    }


class ClassifyCommandTest(unittest.TestCase):
    def run_cli(self, tmp, gather):
        src = Path(tmp, "candidates.json")
        src.write_text(json.dumps({"repos": [candidate(i) for i in range(5)]}))
        out = Path(tmp, "classified.json")
        with (
            mock.patch("pipeline.classify.gather_signals", gather),
            mock.patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = main(["classify", "--in", str(src), "--out", str(out), "--save-every", "2"])
        return code, json.loads(out.read_text())

    def test_failed_repos_are_skipped_then_picked_up_on_resume(self):
        seen = []

        def failing_on_r2(client, repo):
            seen.append(repo["name"])
            if repo["name"] == "r2":
                raise GitHubError(502, "bad gateway")
            return ["skills/a/SKILL.md"], {}, False

        with tempfile.TemporaryDirectory() as tmp:
            code, results = self.run_cli(tmp, failing_on_r2)
            self.assertEqual(code, 0)
            self.assertEqual(
                sorted(r["full_name"] for r in results), ["o/r0", "o/r1", "o/r3", "o/r4"]
            )

            seen.clear()
            code, results = self.run_cli(
                tmp, lambda c, r: seen.append(r["name"]) or ([], {}, False)
            )
            self.assertEqual(seen, ["r2"])  # only the missing repo is fetched again
            self.assertEqual(len(results), 5)

    def test_stops_after_many_errors_in_a_row_but_keeps_progress(self):
        def always_fail(client, repo):
            raise GitHubError(403, "forbidden")

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "candidates.json")
            src.write_text(json.dumps({"repos": [candidate(i) for i in range(25)]}))
            out = Path(tmp, "classified.json")
            with (
                mock.patch("pipeline.classify.gather_signals", always_fail),
                mock.patch.dict("os.environ", {}, clear=True),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = main(["classify", "--in", str(src), "--out", str(out)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out.read_text()), [])


if __name__ == "__main__":
    unittest.main()
