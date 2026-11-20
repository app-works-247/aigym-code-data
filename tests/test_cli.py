import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline.__main__ import REQUIRED_ENV, check_env, load_dotenv, main


class CheckEnvTest(unittest.TestCase):
    def test_reports_presence_without_values(self):
        secret = "ghp_should_never_be_printed"
        status = check_env({"GITHUB_TOKEN": secret, "DATABASE_URL": ""})
        self.assertTrue(status["GITHUB_TOKEN"])
        self.assertFalse(status["DATABASE_URL"])  # empty counts as missing
        self.assertNotIn(secret, repr(status))

    def test_check_env_command_never_prints_values(self):
        secret = "ghp_should_never_be_printed"
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": secret}, clear=True):
            with contextlib.redirect_stdout(out):
                code = main(["check-env"])
        self.assertNotIn(secret, out.getvalue())
        self.assertEqual(code, 1)  # other required variables are missing

    def test_check_env_succeeds_when_required_set(self):
        env = {name: "x" for name in REQUIRED_ENV}
        with mock.patch.dict("os.environ", env, clear=True):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["check-env"]), 0)

    def test_no_command_prints_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main([]), 0)
        self.assertIn("check-env", out.getvalue())


class LoadDotenvTest(unittest.TestCase):
    def test_reads_values_and_never_overrides_existing(self):
        text = (
            "# comment\n"
            "GITHUB_TOKEN=from_file\n"
            "DATABASE_URL='postgresql://u:p@h/db?sslmode=require'\n"
            'export BACKUP_PASSPHRASE="two words"\n'
            "GEMINI_API_KEY=\n"
            "not a line\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, ".env")
            path.write_text(text)
            env = {"GITHUB_TOKEN": "from_ci"}
            added = load_dotenv(path, env)
        self.assertEqual(added, 2)
        self.assertEqual(env["GITHUB_TOKEN"], "from_ci")
        self.assertEqual(env["DATABASE_URL"], "postgresql://u:p@h/db?sslmode=require")
        self.assertEqual(env["BACKUP_PASSPHRASE"], "two words")
        self.assertNotIn("GEMINI_API_KEY", env)  # empty values stay unset

    def test_missing_file_is_fine(self):
        self.assertEqual(load_dotenv(Path("/nonexistent/.env"), {}), 0)


if __name__ == "__main__":
    unittest.main()
