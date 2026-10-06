"""Offline foundation tests plus explicitly gated, rollback-only PostgreSQL tests."""
from copy import deepcopy
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import ValidationError
from psycopg import IntegrityError, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from engine.learning.database import database_url, connect_database, DatabaseConfigurationError, DatabaseConnectionError
from engine.learning.mapping import TABLES, to_columns, from_row
from engine.learning.migrations import discover_migrations, apply_migrations, MigrationError
from engine.learning.schema import ChannelProfile, GenerationRecord, PublishedVideo, MetricSnapshot, ContentFeatures, LearningContext
from test_learning_schema import fixtures

URL = "postgresql://fixture:private_password@localhost/fixture?sslmode=require"


class DatabaseFoundationTests(unittest.TestCase):
    def test_missing_url_does_not_connect(self):
        with patch.dict(os.environ, {}, clear=True), patch("engine.learning.database.psycopg.connect") as connect:
            with self.assertRaisesRegex(DatabaseConfigurationError, "DATABASE_URL is required"):
                with connect_database(load_local_env=False):
                    pass
            connect.assert_not_called()

    def test_invalid_config_does_not_expose_credentials(self):
        for value in ("sqlite:///private_password", "postgresql://fixture:private_password@host/db?bad_option=x"):
            with patch.dict(os.environ, {"DATABASE_URL": value}), self.assertRaises(DatabaseConfigurationError) as caught:
                database_url(load_local_env=False)
            self.assertNotIn("private_password", str(caught.exception))
            self.assertNotIn(value, str(caught.exception))

    def test_neon_ssl_required_and_parameters_preserved(self):
        base = "postgresql://fixture:private_password@ep-fixture.neon.tech/db"
        with patch.dict(os.environ, {"DATABASE_URL": base}), self.assertRaises(DatabaseConfigurationError):
            database_url(load_local_env=False)
        secure = base + "?sslmode=require&channel_binding=require"
        with patch.dict(os.environ, {"DATABASE_URL": secure}):
            self.assertEqual(database_url(load_local_env=False), secure)

    def test_connection_error_is_sanitized(self):
        with patch.dict(os.environ, {"DATABASE_URL": URL}), patch("engine.learning.database.psycopg.connect", side_effect=RuntimeError(URL)):
            with self.assertRaises(DatabaseConnectionError) as caught:
                with connect_database(load_local_env=False):
                    pass
        self.assertNotIn("private_password", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_connection_lifecycle_and_no_import_time_connection(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"DATABASE_URL": URL}), patch("engine.learning.database.psycopg.connect", return_value=connection) as connect:
            with connect_database(load_local_env=False) as value:
                self.assertIs(value, connection)
            connect.assert_called_once_with(URL, connect_timeout=15)
            connection.__exit__.assert_called_once_with(None, None, None)

    def test_schema_mapping_round_trips_every_field(self):
        for model, data in fixtures():
            with self.subTest(model=model.__name__):
                original = model.model_validate(data)
                row = to_columns(original)
                row = {key: value.obj if isinstance(value, Jsonb) else value for key, value in row.items()}
                self.assertEqual(original, from_row(model, row, channel_context_id="channel-a"))
                with self.assertRaises(ValueError):
                    from_row(model, row, channel_context_id="channel-b")

    def test_mapping_revalidates_and_preserves_unknowns_and_numeric_duration(self):
        model, data = fixtures()[1]
        record = model.model_validate(dict(data, measured_duration_seconds=1.25))
        row = to_columns(record)
        row = {key: value.obj if isinstance(value, Jsonb) else value for key, value in row.items()}
        row["measured_duration_seconds"] = Decimal("1.25")
        loaded = from_row(model, row, channel_context_id="channel-a")
        self.assertEqual(loaded.measured_duration_seconds, 1.25)
        self.assertIsNone(loaded.hook)
        with self.assertRaises(ValidationError):
            to_columns(record.model_copy(update={"title": None}))
        snapshot = MetricSnapshot.model_validate(fixtures()[3][1])
        self.assertEqual(to_columns(snapshot)["metrics"].obj, {"views": 0, "likes": None})

    def test_discovery_is_deterministic(self):
        first = discover_migrations()
        self.assertEqual(first, discover_migrations())
        self.assertEqual([m.version for m in first], [1, 2])
        self.assertEqual(len(first[0].checksum), 64)

    def test_discovery_rejects_gaps_duplicates_and_invalid_names(self):
        for names in (("0002_gap.sql",), ("0001_a.sql", "0001_b.sql"), ("bad.sql",)):
            with tempfile.TemporaryDirectory() as directory:
                for name in names:
                    Path(directory, name).write_text("SELECT 1;", encoding="utf-8")
                with self.assertRaises(MigrationError):
                    discover_migrations(directory)

    def migration_connection(self, saved):
        connection = Mock()
        transaction = Mock()
        transaction.__enter__ = Mock()
        transaction.__exit__ = Mock(return_value=False)
        connection.transaction.return_value = transaction
        connection.execute.return_value.fetchall.return_value = saved
        return connection

    def test_migrations_apply_once_and_verify_history(self):
        migration = discover_migrations()[0]
        connection = self.migration_connection([])
        self.assertEqual(apply_migrations(connection), [1, 2])
        self.assertTrue(any(call.args[0] == migration.content for call in connection.execute.call_args_list))
        connection = self.migration_connection([(m.version, m.name, m.checksum) for m in discover_migrations()])
        self.assertEqual(apply_migrations(connection), [])
        self.assertFalse(any(call.args[0] == migration.content for call in connection.execute.call_args_list))
        for saved in ([(1, migration.name, "wrong")], [(2, "future", "hash")]):
            with self.assertRaises(MigrationError):
                apply_migrations(self.migration_connection(saved))

    def test_migration_errors_and_schema_names_are_safe(self):
        for name in ("public", "pg_catalog", "bad;DROP", "information_schema"):
            with self.assertRaises(MigrationError):
                apply_migrations(Mock(), schema=name)
        connection = self.migration_connection([])
        connection.execute.side_effect = RuntimeError(URL)
        with self.assertRaises(MigrationError) as caught:
            apply_migrations(connection)
        self.assertNotIn("private_password", str(caught.exception))
        connection.transaction.return_value.__exit__.assert_called_once()


@unittest.skipUnless(os.getenv("RUN_LEARNING_DB_TESTS") == "1", "Explicit PostgreSQL integration opt-in required")
class PostgreSQLIntegrationTests(unittest.TestCase):
    def test_migration_round_trips_constraints_isolation_and_rollback(self):
        schema = "learning_test_" + uuid4().hex
        with connect_database() as connection:
            with connection.transaction(force_rollback=True):
                self.assertEqual(apply_migrations(connection, schema=schema), [1, 2])
                self.assertEqual(apply_migrations(connection, schema=schema), [])
                names = {row[0] for row in connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)).fetchall()}
                self.assertEqual(names, set(TABLES.values()) | {"schema_migrations", "learning_observations", "learning_evidence"})
                indexes = connection.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = %s", (schema,)).fetchall()
                self.assertTrue(any("collected_at" in row[0] for row in indexes))
                self.assertTrue(any("built_at DESC" in row[0] for row in indexes))
                foreign_keys = connection.execute("SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname=%s AND c.contype='f'", (schema,)).fetchone()[0]
                self.assertGreaterEqual(foreign_keys, 9)
                originals = []
                order = [0, 1, 2, 3, 4, 5]
                for channel in ("channel-a", "channel-b"):
                    for i in order:
                        model, data = fixtures()[i]
                        data = deepcopy(data)
                        data["channel_context_id"] = channel
                        for key in ("generation_reference", "video_reference", "context_reference"):
                            if data.get(key):
                                data[key]["channel_context_id"] = channel
                                data[key]["record_id"] = "gen-1"
                        if model is LearningContext:
                            for observation in data["observations"]:
                                for reference in observation["evidence"]:
                                    reference["channel_context_id"] = channel
                                    reference["record_id"] = "snapshot-1"
                            for reference in data.get("examples") or []:
                                reference["channel_context_id"] = channel
                                reference["record_id"] = "gen-1"
                        if "youtube_video_id" in data:
                            data["youtube_video_id"] = "video-" + channel
                        if model is GenerationRecord:
                            data["measured_duration_seconds"] = 40.5
                        original = model.model_validate(data)
                        if model is LearningContext:
                            from engine.learning.repositories import LearningRepository
                            repo = LearningRepository(connection,channel,schema=schema)
                            self.assertEqual(repo.create_context(original),original)
                            self.assertEqual(repo.get_context(original.context_id),original)
                            originals.append(original)
                            continue
                        columns = to_columns(original)
                        statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) RETURNING *").format(sql.Identifier(schema), sql.Identifier(TABLES[model]), sql.SQL(", ").join(map(sql.Identifier, columns)), sql.SQL(", ").join(sql.Placeholder() for _ in columns))
                        with connection.cursor(row_factory=dict_row) as cursor:
                            row = cursor.execute(statement, tuple(columns.values())).fetchone()
                        self.assertEqual(original, from_row(model, row, channel_context_id=channel))
                        originals.append(original)
                self.assertEqual(apply_migrations(connection, schema=schema), [])
                # Repeated measurements and context builds retain history, not overwrites.
                connection.execute(sql.SQL("INSERT INTO {}.metric_snapshots (channel_context_id, provenance, snapshot_id, youtube_video_id, collected_at, metric_source, observation_window_kind, metrics, collection_status) SELECT channel_context_id, provenance, 'snapshot-2', youtube_video_id, collected_at + interval '1 hour', metric_source, observation_window_kind, metrics, collection_status FROM {}.metric_snapshots WHERE channel_context_id='channel-a'").format(sql.Identifier(schema), sql.Identifier(schema)))
                metrics = connection.execute(sql.SQL("SELECT snapshot_id, metrics FROM {}.metric_snapshots WHERE channel_context_id='channel-a' ORDER BY collected_at").format(sql.Identifier(schema))).fetchall()
                self.assertEqual([row[0] for row in metrics], ['snapshot-1', 'snapshot-2'])
                self.assertEqual(metrics[1][1], {"views": 0, "likes": None})
                connection.execute(sql.SQL("DELETE FROM {}.metric_snapshots WHERE channel_context_id='channel-a' AND snapshot_id='snapshot-2'").format(sql.Identifier(schema)))
                for model, _ in fixtures():
                    rows = connection.execute(sql.SQL("SELECT channel_context_id FROM {}.{} WHERE channel_context_id=%s").format(sql.Identifier(schema), sql.Identifier(TABLES[model])), ("channel-a",)).fetchall()
                    self.assertEqual(rows, [("channel-a",)])
                with self.assertRaises(IntegrityError), connection.transaction():
                    connection.execute(sql.SQL("INSERT INTO {}.metric_snapshots (snapshot_id, channel_context_id, provenance, youtube_video_id, collected_at, metric_source, observation_window_kind, collection_status) VALUES ('invalid', 'channel-a', '{{}}', 'video-channel-b', CURRENT_TIMESTAMP, 'fixture', 'unknown', 'unavailable')").format(sql.Identifier(schema)))
                with self.assertRaises(IntegrityError), connection.transaction():
                    connection.execute(sql.SQL("UPDATE {}.published_videos SET generation_reference=%s WHERE channel_context_id='channel-a'").format(sql.Identifier(schema)), (Jsonb({"channel_context_id": "channel-b", "record_type": "generation", "record_id": "gen-1"}),))
                with self.assertRaises(IntegrityError), connection.transaction():
                    connection.execute(sql.SQL("UPDATE {}.published_videos SET generation_reference=%s WHERE channel_context_id='channel-a'").format(sql.Identifier(schema)), (Jsonb({"channel_context_id": None, "record_type": "generation", "record_id": "gen-1"}),))
                # Failed related writes cannot retain the first insert.
                with self.assertRaises(IntegrityError), connection.transaction():
                    connection.execute(sql.SQL("UPDATE {}.channel_profiles SET display_name='should rollback' WHERE channel_context_id='channel-a'").format(sql.Identifier(schema)))
                    connection.execute(sql.SQL("UPDATE {}.published_videos SET privacy_status='invalid' WHERE channel_context_id='channel-a'").format(sql.Identifier(schema)))
                name = connection.execute(sql.SQL("SELECT display_name FROM {}.channel_profiles WHERE channel_context_id='channel-a'").format(sql.Identifier(schema))).fetchone()[0]
                self.assertEqual(name, "Science Stories")
                # Actual SQL failure rolls back the entire migration, including namespace/ledger.
                with tempfile.TemporaryDirectory() as directory:
                    Path(directory, "0001_broken.sql").write_text("CREATE TABLE partial_write (id INTEGER); SELECT nonexistent_column;", encoding="utf-8")
                    with self.assertRaises(MigrationError):
                        apply_migrations(connection, schema=schema + "x", directory=directory)
                    self.assertIsNone(connection.execute("SELECT to_regnamespace(%s)", (schema + "x",)).fetchone()[0])
            self.assertIsNone(connection.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
