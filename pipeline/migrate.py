"""Apply numbered SQL migrations from pipeline/migrations/ (docs/database.md section 5).

Rules enforced here:
- files are named NNNN_name.sql and run in version order;
- each file runs in its own transaction, so a failure leaves nothing half-applied;
- a file that already ran must never change: its checksum is stored and compared;
- a new file whose version sorts before an applied one is refused (it would run out of order).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# Arbitrary constant so two migration runs can never overlap.
_ADVISORY_LOCK_KEY = 4_247_001

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT PRIMARY KEY CHECK (version ~ '^[0-9]{4}$'),
  name        TEXT        NOT NULL,
  checksum    TEXT        NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationError(Exception):
    """The migration files or the database are in a state we refuse to touch."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read every migration file, sorted by version. Rejects bad names and duplicate versions."""
    migrations: dict[str, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(f"bad migration file name: {path.name} (expected NNNN_name.sql)")
        version = match.group(1)
        if version in migrations:
            raise MigrationError(
                f"two migration files share version {version}: "
                f"{migrations[version].name} and {path.name}"
            )
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            raise MigrationError(f"migration file is empty: {path.name}")
        migrations[version] = Migration(version=version, name=path.name, sql=sql)
    return [migrations[v] for v in sorted(migrations)]


def plan(migrations: list[Migration], applied: dict[str, str]) -> list[Migration]:
    """Return the migrations still to run.

    `applied` maps version -> checksum as stored in schema_migrations.
    """
    by_version = {m.version: m for m in migrations}
    for version, checksum in sorted(applied.items()):
        current = by_version.get(version)
        if current is None:
            raise MigrationError(f"migration {version} ran on the database but its file is gone")
        if current.checksum != checksum:
            raise MigrationError(
                f"{current.name} changed after it ran (checksum differs). "
                "Never edit an applied migration: add a new numbered file instead."
            )
    pending = [m for m in migrations if m.version not in applied]
    if applied and pending:
        latest = max(applied)
        early = [m.name for m in pending if m.version < latest]
        if early:
            raise MigrationError(
                f"new migration(s) {', '.join(early)} sort before already-applied {latest}; "
                "renumber them after it"
            )
    return pending


class Database(Protocol):
    """The few operations migrate needs. Real implementation: PsycopgDatabase."""

    def applied(self) -> dict[str, str]: ...

    def apply(self, migration: Migration) -> None: ...


class PsycopgDatabase:
    """Postgres access through psycopg 3. One instance holds the advisory lock while open."""

    def __init__(self, url: str) -> None:
        import psycopg  # imported here so the rest of the pipeline works without it

        self._conn = psycopg.connect(url, autocommit=True)
        self._conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        self._conn.execute(_CREATE_TABLE)

    def applied(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        return {version: checksum for version, checksum in rows}

    def apply(self, migration: Migration) -> None:
        with self._conn.transaction():
            # No parameters, so psycopg sends the file as one multi-statement query.
            self._conn.execute(migration.sql)
            self._conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )

    def close(self) -> None:
        self._conn.close()  # closing the session also releases the advisory lock


def run(db: Database, migrations: list[Migration], dry_run: bool = False) -> list[Migration]:
    """Apply pending migrations in order and return them (only list them when dry_run)."""
    pending = plan(migrations, db.applied())
    if not dry_run:
        for migration in pending:
            db.apply(migration)
    return pending
