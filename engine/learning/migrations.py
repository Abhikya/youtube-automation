"""Explicit, checksum-verified transactional SQL migrations. No import-time I/O."""
import argparse
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from psycopg import sql
from engine.learning.database import connect_database, DatabaseConfigurationError, DatabaseConnectionError

MIGRATION_DIR = Path(__file__).with_name("sql")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    content: str


def discover_migrations(directory=MIGRATION_DIR):
    result = []
    for path in sorted(Path(directory).glob("*.sql")):
        match = re.fullmatch(r"([0-9]{4})_[a-z0-9_]+\.sql", path.name)
        if not match:
            raise MigrationError("Invalid migration filename.")
        raw = path.read_bytes()
        result.append(Migration(int(match[1]), path.name, hashlib.sha256(raw).hexdigest(), raw.decode("utf-8")))
    if not result or [m.version for m in result] != list(range(1, len(result) + 1)):
        raise MigrationError("Migration versions must be contiguous, starting at 0001.")
    return result


def apply_migrations(connection, *, schema="learning", directory=MIGRATION_DIR):
    """One transaction for the batch; safe reruns, no DROP or startup migrations.

    Schema is explicit so integration tests can use a rollback-only namespace.
    Use a direct connection and a DDL role for deployments.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema) or schema.startswith("pg_") or schema in {"public", "information_schema"}:
        raise MigrationError("Invalid or reserved learning schema name.")
    migrations = discover_migrations(directory)
    try:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("learning-migrations:" + schema,))
            connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            saved = {row[0]: row[1:] for row in connection.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version").fetchall()}
            if sorted(saved) != list(range(1, len(saved) + 1)) or any(v > len(migrations) for v in saved):
                raise MigrationError("Database migration history is incompatible with this checkout.")
            applied = []
            for migration in migrations:
                if migration.version in saved:
                    if saved[migration.version] != (migration.name, migration.checksum):
                        raise MigrationError("Applied migration checksum/name changed; restore the immutable migration.")
                    continue
                connection.execute(migration.content)
                connection.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)", (migration.version, migration.name, migration.checksum))
                applied.append(migration.version)
            return applied
    except MigrationError:
        raise
    except Exception:
        raise MigrationError("Learning migration failed; transaction rolled back. Check database permissions and compatibility.") from None


def main():
    parser = argparse.ArgumentParser(description="Apply learning PostgreSQL migrations explicitly.")
    parser.add_argument("--schema", default="learning")
    args = parser.parse_args()
    try:
        with connect_database() as connection:
            applied = apply_migrations(connection, schema=args.schema)
        print(f"Learning migrations applied: {applied}")
        return 0
    except (DatabaseConfigurationError, DatabaseConnectionError, MigrationError) as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
