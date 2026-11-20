import json
import unittest

from pipeline import discover
from pipeline.github import GitHubClient, GitHubError, Response
from pipeline.seeds import parse
from tests.fake_github import FakeSearch, make_repo


def seeds_for(topics, min_stars=50, exclude=None):
    return parse(
        {
            "version": 1,
            "min_stars": min_stars,
            "topics": topics,
            "exclude": exclude or {},
        }
    )


class StarRangeTest(unittest.TestCase):
    def test_qualifier(self):
        self.assertEqual(discover.star_qualifier(50, None), "stars:>=50")
        self.assertEqual(discover.star_qualifier(50, 99), "stars:50..99")

    def test_split(self):
        self.assertEqual(discover.split_range(50, None), [(50, 99), (100, None)])
        self.assertEqual(discover.split_range(50, 99), [(50, 74), (75, 99)])
        self.assertEqual(discover.split_range(50, 51), [(50, 50), (51, 51)])
        self.assertIsNone(discover.split_range(60, 60))


class DiscoverTest(unittest.TestCase):
    def test_splits_queries_over_1000_and_finds_everything(self):
        # 2600 repos spread over 50..2649 stars: one query cannot return them all.
        repos = [make_repo(i, 50 + i, ["claude-code"]) for i in range(2600)]
        fake = FakeSearch(repos)
        found, report = discover.discover(GitHubClient(transport=fake), seeds_for(["claude-code"]))
        self.assertEqual(len(found), 2600)
        self.assertEqual(report.truncated, [])
        self.assertEqual(report.per_query["topic:claude-code"], 2600)
        self.assertEqual(report.requests, len(fake.queries))
        # Every query that was actually paged stayed under the cap.
        self.assertTrue(all("fork:false archived:false" in q for q in fake.queries))

    def test_reports_ranges_that_cannot_be_split(self):
        repos = [make_repo(i, 77, ["claude-code"]) for i in range(1200)]
        found, report = discover.discover(
            GitHubClient(transport=FakeSearch(repos)), seeds_for(["claude-code"])
        )
        self.assertEqual(len(found), 1000)  # GitHub's hard cap for one query
        self.assertEqual(
            report.truncated, ["topic:claude-code stars:77..77 fork:false archived:false"]
        )

    def test_merges_topics_by_id_and_records_matches(self):
        repos = [
            make_repo(1, 500, ["claude-code", "mcp-server"]),
            make_repo(2, 300, ["mcp-server"]),
        ]
        found, _ = discover.discover(
            GitHubClient(transport=FakeSearch(repos)), seeds_for(["claude-code", "mcp-server"])
        )
        self.assertEqual(found[1].matched_by, ["topic:claude-code", "topic:mcp-server"])
        self.assertEqual(found[2].matched_by, ["topic:mcp-server"])

    def test_skips_stale_forks_low_stars_and_excluded(self):
        repos = [
            make_repo(1, 100, ["claude-code"], fork=True),
            make_repo(2, 10, ["claude-code"]),
            make_repo(3, 100, ["claude-code"], owner="Blocked"),
            make_repo(4, 100, ["claude-code"], owner="ok", name="Hidden-Repo"),
            make_repo(5, 100, ["claude-code"]),
        ]
        fake = FakeSearch(repos, stale_ids=frozenset({1, 2}))
        seeds = seeds_for(
            ["claude-code"], exclude={"owners": ["blocked"], "repos": ["ok/hidden-repo"]}
        )
        found, report = discover.discover(GitHubClient(transport=fake), seeds)
        self.assertEqual(list(found), [5])
        self.assertEqual(report.skipped, {"fork": 1, "below_min_stars": 1, "excluded": 2})

    def test_candidate_fields(self):
        item = make_repo(
            9,
            1234,
            ["mcp", "claude-code"],
            owner="anthropics",
            name="skills",
            homepage="",
            license=None,
            description=None,
        )
        c = discover.candidate_from_item(item)
        self.assertTrue(c.is_official)
        self.assertIsNone(c.homepage)
        self.assertIsNone(c.license_spdx)
        self.assertIsNone(c.description)
        self.assertEqual(c.topics, ["claude-code", "mcp"])
        self.assertEqual(c.full_name, "anthropics/skills")

    def test_only_runs_selected_labels(self):
        fake = FakeSearch([make_repo(1, 100, ["mcp-server"])])
        found, _ = discover.discover(
            GitHubClient(transport=fake),
            seeds_for(["claude-code", "mcp-server"]),
            only={"topic:mcp-server"},
        )
        self.assertEqual(list(found), [1])
        self.assertTrue(all("topic:mcp-server" in q for q in fake.queries))

    def test_json_output_sorted_by_stars(self):
        repos = [make_repo(1, 60, ["claude-code"]), make_repo(2, 900, ["claude-code"])]
        seeds = seeds_for(["claude-code"])
        found, report = discover.discover(GitHubClient(transport=FakeSearch(repos)), seeds)
        out = discover.to_json(found, report, seeds)
        json.dumps(out)  # must be serialisable
        self.assertEqual([r["id"] for r in out["repos"]], [2, 1])
        self.assertEqual(out["count"], 2)


class Scripted:
    """Transport that replays a fixed list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.headers_seen = []

    def __call__(self, url, headers):
        self.headers_seen.append(dict(headers))
        return self.responses.pop(0)


def ok(body=None, **headers):
    return Response(200, headers, json.dumps(body or {"ok": True}).encode())


class GitHubClientTest(unittest.TestCase):
    def setUp(self):
        self.slept = []

    def client(self, transport, token=None):
        return GitHubClient(
            token=token, transport=transport, sleep=self.slept.append, now=lambda: 1000.0
        )

    def test_waits_for_reset_on_primary_rate_limit(self):
        limited = Response(
            403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}, b'{"message":"x"}'
        )
        result = self.client(Scripted([limited, ok()])).get("/x")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.slept, [31.0])

    def test_honours_retry_after_on_secondary_limit(self):
        limited = Response(429, {"retry-after": "7"}, b"{}")
        self.client(Scripted([limited, ok()])).get("/x")
        self.assertEqual(self.slept, [7.0])

    def test_pauses_before_next_call_when_quota_is_used_up(self):
        self.client(
            Scripted([ok(**{"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1010"})])
        ).get("/x")
        self.assertEqual(self.slept, [11.0])

    def test_retries_server_errors_with_backoff(self):
        err = Response(502, {}, b"bad gateway")
        self.client(Scripted([err, err, ok()])).get("/x")
        self.assertEqual(self.slept, [2.0, 4.0])

    def test_network_errors_are_retried(self):
        import urllib.error

        from pipeline import github

        calls = []

        def flaky_urlopen(request, timeout):
            calls.append(1)
            raise urllib.error.URLError("timed out")

        original = github.urllib.request.urlopen
        github.urllib.request.urlopen = flaky_urlopen
        try:
            resp = github.urllib_transport("https://api.github.com/x", {})
        finally:
            github.urllib.request.urlopen = original
        self.assertEqual(resp.status, github.NETWORK_ERROR_STATUS)

        self.client(Scripted([resp, resp, ok()])).get("/x")
        self.assertEqual(self.slept, [2.0, 4.0])

    def test_body_cut_mid_read_is_a_network_error(self):
        import http.client

        from pipeline import github

        def cut_urlopen(request, timeout):
            raise http.client.IncompleteRead(b"partial", 100)

        original = github.urllib.request.urlopen
        github.urllib.request.urlopen = cut_urlopen
        try:
            resp = github.urllib_transport("https://api.github.com/x", {})
        finally:
            github.urllib.request.urlopen = original
        self.assertEqual(resp.status, github.NETWORK_ERROR_STATUS)

    def test_permission_error_is_not_retried_and_hides_token(self):
        denied = Response(403, {"x-ratelimit-remaining": "4000"}, b'{"message":"Forbidden"}')
        transport = Scripted([denied])
        with self.assertRaises(GitHubError) as ctx:
            self.client(transport, token="ghp_secret123").get("/x")
        self.assertEqual(ctx.exception.status, 403)
        self.assertNotIn("ghp_secret123", str(ctx.exception))
        self.assertEqual(transport.headers_seen[0]["Authorization"], "Bearer ghp_secret123")
        self.assertEqual(self.slept, [])

    def test_refuses_absurdly_long_waits(self):
        limited = Response(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99999"}, b"{}")
        with self.assertRaises(GitHubError):
            self.client(Scripted([limited])).get("/x")

    def test_no_auth_header_without_token(self):
        transport = Scripted([ok()])
        self.client(transport).get("/x")
        self.assertNotIn("Authorization", transport.headers_seen[0])


if __name__ == "__main__":
    unittest.main()
