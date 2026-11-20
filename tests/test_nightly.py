import json
import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pipeline import refresh
from pipeline.github import GitHubClient, GitHubError, Response
from pipeline.seeds import parse
from tests.fake_github import FakeSearch, make_repo

NOW = datetime(2026, 11, 21, 2, 15, tzinfo=UTC)


def gql_node(repo: dict, **changes) -> dict:
    node = {
        "databaseId": repo["id"],
        "nameWithOwner": f"{repo['owner']['login']}/{repo['name']}",
        "description": repo["description"],
        "homepageUrl": "",
        "stargazerCount": repo["stargazers_count"],
        "forkCount": repo["forks_count"],
        "isArchived": False,
        "pushedAt": repo["pushed_at"],
        "createdAt": repo["created_at"],
        "licenseInfo": {"spdxId": "MIT"},
        "primaryLanguage": {"name": "Python"},
        "repositoryTopics": {"nodes": [{"topic": {"name": t}} for t in repo["topics"]]},
        "defaultBranchRef": {"name": "main"},
        "issues": {"totalCount": 3},
    }
    node.update(changes)
    return node


class FakeGitHub:
    """Search, trees and GraphQL over one in-memory list of repos."""

    def __init__(self, repos, trees=None, gone=(), star_bump=0):
        self.repos = {r["node_id"]: r for r in repos}
        self.search = FakeSearch(repos)
        self.trees = trees or {}
        self.gone = set(gone)
        self.star_bump = star_bump
        self.graphql_calls = 0

    def __call__(self, url, headers, body=None):
        if url.endswith("/graphql"):
            self.graphql_calls += 1
            ids = json.loads(body)["variables"]["ids"]
            nodes = []
            for node_id in ids:
                repo = self.repos.get(node_id)
                if repo is None or repo["id"] in self.gone:
                    nodes.append(None)
                else:
                    nodes.append(
                        gql_node(repo, stargazerCount=repo["stargazers_count"] + self.star_bump)
                    )
            errors = [{"type": "NOT_FOUND", "message": "gone"}] if None in nodes else []
            payload = {"data": {"nodes": nodes}, "errors": errors}
            return Response(200, {}, json.dumps(payload).encode())
        if "/git/trees/" in url:
            name = url.split("/repos/", 1)[1].split("/git/", 1)[0]
            paths = self.trees.get(name, ["README.md"])
            tree = {"tree": [{"path": p, "type": "blob"} for p in paths], "truncated": False}
            return Response(200, {}, json.dumps(tree).encode())
        return self.search(url, headers)


class RefreshUnitTest(unittest.TestCase):
    def test_node_to_update(self):
        repo = make_repo(7, 120, ["claude-code"], owner="Acme", name="Tool")
        u = refresh.node_to_update(gql_node(repo, isArchived=True))
        self.assertEqual(set(u), {"id", *refresh.FIELDS})
        self.assertEqual((u["id"], u["owner"], u["name"], u["stars"]), (7, "Acme", "Tool", 120))
        self.assertTrue(u["archived"])
        self.assertEqual(u["open_issues"], 3)

    def test_failing_batch_is_split_until_it_fits(self):
        repos = [make_repo(i, 60 + i, ["claude-code"]) for i in range(1, 101)]
        fake = FakeGitHub(repos)
        sizes = []

        def too_big_over_30(url, headers, body=None):
            if url.endswith("/graphql"):
                n = len(json.loads(body)["variables"]["ids"])
                sizes.append(n)
                if n > 30:
                    return Response(502, {}, b"<html>502 Bad Gateway</html>")
            return fake(url, headers, body)

        client = GitHubClient(transport=too_big_over_30, sleep=lambda s: None)
        updates, missing = refresh.fetch(client, [(r["id"], r["node_id"]) for r in repos])
        self.assertEqual(len(updates), 100)
        self.assertEqual(missing, [])
        self.assertEqual([u["id"] for u in updates], list(range(1, 101)))  # order kept
        self.assertEqual(sorted({n for n in sizes if n <= 30}), [25])

    def test_graphql_timeout_message_counts_as_server_error(self):
        def transport(url, headers, body=None):
            payload = {"data": None, "errors": [{"message": "This may be the result of a timeout"}]}
            return Response(200, {}, json.dumps(payload).encode())

        with self.assertRaises(GitHubError) as ctx:
            GitHubClient(transport=transport, sleep=lambda s: None).graphql("q", {}, retries=0)
        self.assertEqual(ctx.exception.status, 504)

    def test_fetch_batches_and_reports_missing(self):
        repos = [make_repo(i, 60 + i, ["claude-code"]) for i in range(1, 251)]
        fake = FakeGitHub(repos, gone={5, 200})
        client = GitHubClient(transport=fake, sleep=lambda s: None)
        updates, missing = refresh.fetch(client, [(r["id"], r["node_id"]) for r in repos])
        self.assertEqual(fake.graphql_calls, 3)  # 100 + 100 + 50
        self.assertEqual(sorted(missing), [5, 200])
        self.assertEqual(len(updates), 248)

    def test_graphql_errors_other_than_not_found_fail(self):
        def transport(url, headers, body=None):
            payload = {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}
            return Response(200, {}, json.dumps(payload).encode())

        with self.assertRaises(GitHubError):
            GitHubClient(transport=transport).graphql("query{x}", {})

    def test_rename_onto_a_taken_name_keeps_the_old_name(self):
        updates = [{"id": 1, "owner": "a", "name": "taken"}, {"id": 2, "owner": "b", "name": "new"}]
        rows = [(1, "a", "old"), (2, "b", "x"), (3, "A", "Taken")]
        self.assertEqual(refresh.keep_old_name_on_collision(updates, rows), 1)
        self.assertEqual(updates[0]["name"], "old")
        self.assertEqual(updates[1]["name"], "new")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to a throwaway db")
class NightlyDatabaseTest(unittest.TestCase):
    """End to end against a throwaway Postgres: never point this at Neon."""

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
        self.seeds = parse(
            {
                "version": 1,
                "min_stars": 50,
                "topics": ["claude-code", "mcp-server"],
                "exclude": {"repos": ["spam/bad"]},
            }
        )
        self.repos = [
            make_repo(1, 500, ["claude-code"], owner="acme", name="skills"),
            make_repo(2, 300, ["mcp-server"], owner="acme", name="generic"),
            make_repo(3, 200, ["claude-code"], owner="solo", name="idea"),
            make_repo(4, 900, ["claude-code"], owner="spam", name="bad"),
        ]
        self.trees = {"acme/skills": ["skills/pdf/SKILL.md", ".claude-plugin/plugin.json"]}

    def connect(self):
        import psycopg

        return psycopg.connect(self.url)

    def run_night(self, fake, now, out):
        from pipeline import nightly

        opened = []

        def transport(url, headers, body=None):
            # The point of nightly's design: never wait on GitHub with a connection open.
            self.assertTrue(all(c.closed for c in opened), "connection open during GitHub call")
            return fake(url, headers, body)

        client = GitHubClient(transport=transport, sleep=lambda s: None)

        def connect():
            conn = self.connect()
            opened.append(conn)
            return conn

        try:
            return nightly.run(connect, client, self.seeds, out, now=now, log=lambda m: None)
        finally:
            # No connection may be left open: Neon would drop it while GitHub work runs.
            self.assertTrue(all(c.closed for c in opened))

    def test_two_nights_build_history_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "v1")
            first = self.run_night(FakeGitHub(self.repos, self.trees), NOW - timedelta(days=1), out)
            self.assertEqual(first["new_repos"], 3)  # spam/bad excluded by discover
            self.assertEqual(first["classified"], 3)
            self.assertEqual(first["classify_status"], {"active": 1, "hidden": 1, "pending": 1})
            self.assertEqual(first["exported"], 1)

            second = self.run_night(FakeGitHub(self.repos, self.trees, star_bump=25), NOW, out)
            self.assertEqual(second["new_repos"], 0)
            self.assertEqual(second["classified"], 0)  # never re-classified nightly
            self.assertEqual(second["snapshot_rows"], 3)

            detail = json.loads(Path(out, "repos/acme__skills.json").read_text())
            self.assertEqual(detail["stars"], 525)
            self.assertEqual(detail["d1"], 25)
            self.assertIsNone(detail["d7"])
            self.assertEqual(detail["categories"][0], "skills")
            self.assertEqual(
                detail["history"], [[int((NOW - timedelta(days=1)).strftime("%Y%m%d")), 500]]
            )

        with self.connect() as conn:
            runs = conn.execute("SELECT status, stats FROM pipeline_runs ORDER BY id").fetchall()
            self.assertEqual([r[0] for r in runs], ["ok", "ok"])
            self.assertEqual(runs[1][1]["refreshed"], 3)
            days = conn.execute("SELECT count(DISTINCT day) FROM repo_snapshots").fetchone()[0]
            self.assertEqual(days, 2)

    def test_same_day_rerun_does_not_duplicate_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "v1")
            self.run_night(FakeGitHub(self.repos, self.trees), NOW, out)
            stats = self.run_night(FakeGitHub(self.repos, self.trees, star_bump=1), NOW, out)
        with self.connect() as conn:
            rows = conn.execute("SELECT count(*), max(stars) FROM repo_snapshots WHERE repo_id = 1")
            self.assertEqual(rows.fetchone(), (1, 501))
        self.assertEqual(stats["snapshot_rows"], 3)

    def test_deleted_repo_becomes_hidden_and_excluded_repo_removed(self):
        from pipeline import db

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "v1")
            self.run_night(FakeGitHub(self.repos, self.trees), NOW - timedelta(days=1), out)
            with self.connect() as conn:  # someone loads spam/bad by hand
                db.upsert_repos(conn, [_candidate(self.repos[3])])
                conn.commit()
            stats = self.run_night(FakeGitHub(self.repos, self.trees, gone={3}), NOW, out)
        self.assertEqual(stats["excluded"], 1)
        self.assertEqual(stats["missing"], 1)
        with self.connect() as conn:
            status = dict(conn.execute("SELECT id, status FROM repos").fetchall())
        self.assertEqual(status[3], "hidden")
        self.assertEqual(status[4], "removed")

    def test_failed_run_is_logged_and_keeps_earlier_steps(self):
        class Broken(FakeGitHub):
            def __call__(self, url, headers, body=None):
                if url.endswith("/graphql"):
                    return Response(401, {}, b'{"message":"Bad credentials"}')
                return super().__call__(url, headers, body)

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(GitHubError):
            self.run_night(Broken(self.repos, self.trees), NOW, Path(tmp, "v1"))
        with self.connect() as conn:
            status, stats = conn.execute("SELECT status, stats FROM pipeline_runs").fetchone()
            repos = conn.execute("SELECT count(*) FROM repos").fetchone()[0]
        self.assertEqual(status, "failed")
        self.assertIn("Bad credentials", stats["error"])
        self.assertEqual(repos, 3)  # discover's work was committed before refresh failed

    def test_export_from_db_matches_classification_order(self):
        from pipeline import db

        with tempfile.TemporaryDirectory() as tmp:
            self.run_night(FakeGitHub(self.repos, self.trees), NOW, Path(tmp, "v1"))
        with self.connect() as conn:
            records, skipped = db.records_for_export(conn, date(2026, 11, 21))
        self.assertEqual([r.id for r in records], ["acme__skills"])
        self.assertEqual(records[0].categories, ["skills", "plugins"])
        self.assertEqual(skipped, [])


def _candidate(item: dict) -> dict:
    from dataclasses import asdict

    from pipeline.discover import candidate_from_item

    return asdict(candidate_from_item(item))


if __name__ == "__main__":
    unittest.main()
