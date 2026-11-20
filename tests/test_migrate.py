import os
import tempfile
import unittest
from pathlib import Path

from pipeline import migrate


class FakeDatabase:
    def __init__(self, applied=None):
        self.rows = dict(applied or {})
        self.applied_names = []

    def applied(self):
        return dict(self.rows)

    def apply(self, migration):
        self.rows[migration.version] = migration.checksum
        self.applied_names.append(migration.name)


def m(version, sql="SELECT 1;"):
    return migrate.Migration(version=version, name=f"{version}_x.sql", sql=sql)


class LoadTest(unittest.TestCase):
    def test_repo_migrations_load_in_order(self):
        names = [x.name for x in migrate.load_migrations()]
        self.assertEqual(names[:2], ["0001_init.sql", "0002_seed_categories.sql"])

    def test_rejects_bad_names_duplicates_and_empty_files(self):
        cases = [
            {"1_init.sql": "SELECT 1;"},
            {"0001_Init.sql": "SELECT 1;"},
            {"0001_a.sql": "SELECT 1;", "0001_b.sql": "SELECT 1;"},
            {"0001_a.sql": "   \n"},
        ]
        for files in cases:
            with self.subTest(files=list(files)), tempfile.TemporaryDirectory() as tmp:
                for name, sql in files.items():
                    Path(tmp, name).write_text(sql)
                with self.assertRaises(migrate.MigrationError):
                    migrate.load_migrations(Path(tmp))


class PlanTest(unittest.TestCase):
    def test_fresh_database_gets_everything(self):
        files = [m("0001"), m("0002")]
        self.assertEqual(migrate.plan(files, {}), files)

    def test_only_new_files_are_pending(self):
        files = [m("0001"), m("0002")]
        self.assertEqual(migrate.plan(files, {"0001": files[0].checksum}), [files[1]])

    def test_edited_applied_file_is_refused(self):
        with self.assertRaisesRegex(migrate.MigrationError, "changed after it ran"):
            migrate.plan([m("0001", "SELECT 2;")], {"0001": m("0001").checksum})

    def test_deleted_applied_file_is_refused(self):
        with self.assertRaisesRegex(migrate.MigrationError, "file is gone"):
            migrate.plan([m("0002")], {"0001": "a" * 64})

    def test_out_of_order_file_is_refused(self):
        files = [m("0001"), m("0002"), m("0003")]
        applied = {"0001": files[0].checksum, "0003": files[2].checksum}
        with self.assertRaisesRegex(migrate.MigrationError, "sort before"):
            migrate.plan(files, applied)


class RunTest(unittest.TestCase):
    def test_applies_in_order_and_is_idempotent(self):
        db = FakeDatabase()
        files = [m("0001"), m("0002")]
        migrate.run(db, files)
        self.assertEqual(db.applied_names, ["0001_x.sql", "0002_x.sql"])
        self.assertEqual(migrate.run(db, files), [])

    def test_dry_run_changes_nothing(self):
        db = FakeDatabase()
        pending = migrate.run(db, [m("0001")], dry_run=True)
        self.assertEqual(len(pending), 1)
        self.assertEqual(db.rows, {})


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to a throwaway db")
class PostgresTest(unittest.TestCase):
    """Runs the real migrations against a throwaway Postgres. Never point this at Neon main."""

    def setUp(self):
        import psycopg

        self.url = os.environ["TEST_DATABASE_URL"]
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    def test_migrations_apply_and_constraints_hold(self):
        import psycopg

        db = migrate.PsycopgDatabase(self.url)
        try:
            done = migrate.run(db, migrate.load_migrations())
            self.assertEqual(len(done), len(migrate.load_migrations()))
            self.assertEqual(migrate.run(db, migrate.load_migrations()), [])
        finally:
            db.close()

        with psycopg.connect(self.url, autocommit=True) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                ).fetchall()
            }
            self.assertEqual(len(tables - {"schema_migrations"}), 15)
            slugs = [r[0] for r in conn.execute("SELECT slug FROM categories ORDER BY sort_order")]
            self.assertEqual(
                slugs,
                [
                    "skills",
                    "mcp",
                    "subagents",
                    "hooks",
                    "plugins",
                    "commands",
                    "templates",
                    "tools",
                    "learn",
                ],
            )
            conn.execute(
                "INSERT INTO repos (id, node_id, owner, name, created_at, stars, forks) "
                "VALUES (1, 'R_1', 'Anthropics', 'Skills', now(), 10, 1)"
            )
            bad_statements = [
                # same repo, different case
                "INSERT INTO repos (id, node_id, owner, name, created_at, stars, forks) "
                "VALUES (2, 'R_2', 'anthropics', 'skills', now(), 10, 1)",
                # only vi/en
                "INSERT INTO repo_i18n (repo_id, lang, summary, provider, model, source_sha) "
                "VALUES (1, 'ja', 'x', 'p', 'm', 's')",
                # negative stars
                "INSERT INTO repo_snapshots VALUES (1, current_date, -1, 0)",
                # unknown category
                "INSERT INTO repo_categories VALUES (1, 'nope', 'topic', 0.5)",
            ]
            for sql in bad_statements:
                with self.subTest(sql=sql[:40]), self.assertRaises(psycopg.errors.IntegrityError):
                    conn.execute(sql)
            # Deleting a repo removes its children.
            conn.execute("INSERT INTO repo_snapshots VALUES (1, current_date, 10, 1)")
            conn.execute("DELETE FROM repos WHERE id = 1")
            self.assertEqual(conn.execute("SELECT count(*) FROM repo_snapshots").fetchone()[0], 0)

    def test_roles_can_do_only_their_job(self):

        db = migrate.PsycopgDatabase(self.url)
        try:
            migrate.run(db, migrate.load_migrations())
        finally:
            db.close()
        check_role_rights(self, self.url)


def check_role_rights(test: unittest.TestCase, url: str) -> None:
    """pipeline_rw writes rows but never structure; readonly only reads. Leaves no trace."""
    import psycopg

    denied = psycopg.errors.InsufficientPrivilege
    insert_repo = (
        "INSERT INTO repos (id, node_id, owner, name, created_at, stars, forks) "
        "VALUES (-1, 'R_test', 'role-test', 'role-test', now(), 1, 1)"
    )
    with psycopg.connect(url) as conn:  # one transaction, rolled back at the end
        conn.execute("SET ROLE pipeline_rw")
        conn.execute(insert_repo)
        conn.execute("UPDATE repos SET stars = 2 WHERE id = -1")
        test.assertEqual(conn.execute("SELECT stars FROM repos WHERE id = -1").fetchone()[0], 2)
        conn.execute("INSERT INTO pipeline_runs (job) VALUES ('nightly')")  # needs the sequence
        conn.execute("DELETE FROM repos WHERE id = -1")
        conn.rollback()
    forbidden_for_rw = [
        "CREATE TABLE nope (x int)",
        "DROP TABLE repos",
        "ALTER TABLE repos ADD COLUMN nope int",
        "INSERT INTO schema_migrations (version, name, checksum) "
        "VALUES ('9999', 'x', repeat('a', 64))",
        "TRUNCATE repos",
    ]
    forbidden_for_readonly = [insert_repo, "DELETE FROM categories", "CREATE TABLE nope (x int)"]
    for role, statements in (
        ("pipeline_rw", forbidden_for_rw),
        ("readonly", forbidden_for_readonly),
    ):
        for sql in statements:
            with test.subTest(role=role, sql=sql[:30]), psycopg.connect(url) as conn:
                conn.execute(f"SET ROLE {role}")
                with test.assertRaises(denied):
                    conn.execute(sql)
                conn.rollback()
    with psycopg.connect(url) as conn:
        conn.execute("SET ROLE readonly")
        test.assertEqual(conn.execute("SELECT count(*) FROM categories").fetchone()[0], 9)
        conn.rollback()


if __name__ == "__main__":
    unittest.main()
