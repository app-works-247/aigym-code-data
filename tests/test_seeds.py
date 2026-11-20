import unittest

from pipeline import seeds


def base(**overrides):
    data = {"version": 1, "min_stars": 50, "topics": ["claude-code"]}
    data.update(overrides)
    return data


class SeedsTest(unittest.TestCase):
    def test_repo_seeds_file_is_valid(self):
        s = seeds.load()
        self.assertEqual(s.min_stars, 50)  # PLAN.md 6.1
        self.assertIn("claude-code", s.topics)
        self.assertIn("agent-skills", s.topics)

    def test_exclusion_is_case_insensitive(self):
        s = seeds.parse(base(exclude={"repos": ["acme/tool"], "owners": ["spam"]}))
        self.assertTrue(s.is_excluded("ACME", "Tool"))
        self.assertTrue(s.is_excluded("Spam", "anything"))
        self.assertFalse(s.is_excluded("acme", "other"))

    def test_rejects_bad_files(self):
        bad_cases = [
            "not a mapping",
            base(version=2),
            base(min_stars=0),
            base(min_stars=True),
            base(topics=[]),
            base(topics=["Claude Code"]),
            base(topics=["claude-code", "claude-code"]),
            base(keywords="claude"),
            base(exclude={"repos": ["no-slash"]}),
            base(exclude={"repos": ["Upper/Case"]}),
            base(exclude={"owners": ["Upper"]}),
        ]
        for data in bad_cases:
            with self.subTest(data=data), self.assertRaises(seeds.SeedsError):
                seeds.parse(data)


if __name__ == "__main__":
    unittest.main()
