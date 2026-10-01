"""Repository contracts offline; opt-in PostgreSQL tests use rollback-only schemas."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from engine.learning.database import connect_database
from engine.learning.migrations import apply_migrations
from engine.learning.mapping import to_columns
from engine.learning.repositories import LearningRepository, RepositoryError, RepositoryConflict
from engine.learning.schema import ChannelProfile, GenerationRecord, PublishedVideo, MetricSnapshot, ContentFeatures, LearningContext
from test_learning_schema import fixtures


def records(channel="channel-a"):
    result = {}
    for model, original in fixtures():
        data = deepcopy(original)
        data["channel_context_id"] = channel
        if "youtube_video_id" in data:
            data["youtube_video_id"] = "video-" + channel
        for name in ("generation_reference", "video_reference", "context_reference"):
            if data.get(name):
                data[name]["channel_context_id"] = channel
                data[name]["record_id"] = "gen-1"
        if model is PublishedVideo:
            data["published_at"] = data["created_at"]
        if model is LearningContext:
            for observation in data["observations"]:
                for reference in observation["evidence"]:
                    reference["channel_context_id"] = channel
                    reference["record_id"] = "snapshot-1"
            for reference in data.get("examples") or []:
                reference["channel_context_id"] = channel
                reference["record_id"] = "gen-1"
        result[model] = model.model_validate(data)
    return result


def db_row(record):
    return {key: value.obj if isinstance(value, Jsonb) else value for key, value in to_columns(record).items()}


class RepositoryOfflineTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.repo = LearningRepository(self.connection, "channel-a")

    def test_constructor_and_import_never_connect(self):
        self.assertEqual(self.repo.channel_context_id, "channel-a")
        self.connection.assert_not_called()
        with self.assertRaises(ValidationError):
            LearningRepository(self.connection, "display name")
        code = "import sys; from unittest.mock import patch; import psycopg; guard=patch('psycopg.connect', side_effect=AssertionError('unexpected connection')); guard.start(); import engine.learning.repositories; assert not any(n.startswith(('config.settings','engine.pipeline','engine.scheduling','engine.uploader','engine.approval')) for n in sys.modules)"
        completed = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_all_create_methods_use_existing_mapping_and_return_models(self):
        for model, method in ((ChannelProfile, "create_profile"), (GenerationRecord, "create_generation"), (PublishedVideo, "create_published_video"), (MetricSnapshot, "insert_snapshot"), (ContentFeatures, "create_features"), (LearningContext, "create_context")):
            with self.subTest(model=model.__name__):
                record = records()[model]
                self.cursor.execute.return_value.fetchone.return_value = db_row(record)
                self.assertEqual(getattr(self.repo, method)(record), record)
                query, params = self.cursor.execute.call_args.args
                self.assertIn("INSERT INTO", query.as_string())
                self.assertIn("%s", query.as_string())
                self.assertNotIn("ON CONFLICT", query.as_string())
                self.assertEqual(params[0], "channel-a")
                self.assertEqual(self.connection.cursor.call_args.kwargs["row_factory"].__name__, "dict_row")

    def test_wrong_channel_writes_rejected_before_sql(self):
        for model, method in ((ChannelProfile, "create_profile"), (GenerationRecord, "create_generation"), (PublishedVideo, "create_published_video"), (MetricSnapshot, "insert_snapshot"), (ContentFeatures, "create_features"), (LearningContext, "create_context")):
            with self.subTest(model=model.__name__), self.assertRaises(ValueError):
                getattr(self.repo, method)(records("channel-b")[model])
        self.connection.cursor.assert_not_called()

    def test_all_query_paths_scope_channel_and_return_clean_empty_results(self):
        self.cursor.execute.return_value.fetchall.return_value = []
        calls = [lambda: self.repo.get_profile(), lambda: self.repo.get_generation("same-id"), self.repo.list_generations,
                 self.repo.recent_generations, lambda: self.repo.get_published_video("same-id"),
                 lambda: self.repo.get_published_video_by_youtube_id("video-a"),
                 lambda: self.repo.published_videos_for_generation("same-id"), self.repo.list_published_videos,
                 self.repo.recent_published_videos, lambda: self.repo.get_snapshot("same-id"),
                 self.repo.list_snapshots, lambda: self.repo.list_snapshots(youtube_video_id="video-a"),
                 lambda: self.repo.get_features("same-id"), self.repo.list_features,
                 lambda: self.repo.list_features(generation_id="same-id", published_video_id="same-id"),
                 lambda: self.repo.get_context("same-id"), self.repo.list_contexts, self.repo.latest_context]
        for call in calls:
            self.connection.reset_mock()
            value = call()
            self.assertIn(value, (None, []))
            query, params = self.cursor.execute.call_args.args
            self.assertIn("WHERE channel_context_id = %s", query.as_string())
            self.assertEqual(params[0], "channel-a")
            self.cursor.execute.assert_called_once()

    def test_read_boundary_rejects_wrong_channel_even_if_database_returns_it(self):
        self.cursor.execute.return_value.fetchall.return_value = [db_row(records("channel-b")[GenerationRecord])]
        with self.assertRaises(ValueError):
            self.repo.get_generation("gen-1")

    def test_query_input_validation_and_date_bounds(self):
        for kwargs in ({"limit": True}, {"limit": 0}, {"limit": 1001}, {"offset": -1}, {"offset": False}, {"start_at": "2026-10-02T10:00:00"}):
            with self.assertRaises((ValueError, ValidationError)):
                self.repo.list_generations(**kwargs)
        now = records()[GenerationRecord].generated_at
        with self.assertRaises(ValueError):
            self.repo.list_generations(start_at=now + timedelta(days=1), end_at=now)
        self.cursor.execute.return_value.fetchall.return_value = []
        self.repo.list_generations(start_at=now, end_at=now + timedelta(days=1), limit=5, offset=2)
        query, params = self.cursor.execute.call_args.args
        self.assertIn('"generated_at" >= %s', query.as_string())
        self.assertIn('"generated_at" < %s', query.as_string())
        self.assertEqual(params[-2:], (5, 2))

    def test_upsert_guards_identity_and_stale_updates(self):
        for model, method in ((ChannelProfile, "upsert_profile"), (PublishedVideo, "upsert_published_video")):
            record = records()[model]
            self.cursor.execute.return_value.fetchone.return_value = db_row(record)
            self.assertEqual(getattr(self.repo, method)(record), record)
            query = self.cursor.execute.call_args.args[0].as_string()
            self.assertIn("ON CONFLICT", query)
            self.assertIn("created_at = EXCLUDED.created_at", query)
            self.assertIn("updated_at <= EXCLUDED.updated_at", query)
            if model is PublishedVideo:
                self.assertIn("youtube_video_id = EXCLUDED.youtube_video_id", query)
            self.cursor.execute.return_value.fetchone.return_value = None
            with self.assertRaises(RepositoryConflict):
                getattr(self.repo, method)(record)

    def test_driver_errors_are_sanitized_and_transactions_exit(self):
        for error, expected in ((psycopg.errors.UniqueViolation("private_password"), RepositoryConflict), (psycopg.OperationalError("private_password"), RepositoryError)):
            self.cursor.execute.side_effect = error
            with self.assertRaises(expected) as caught:
                self.repo.create_generation(records()[GenerationRecord])
            self.assertNotIn("private_password", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            self.connection.transaction.return_value.__exit__.assert_called()

    def test_outer_transaction_failure_propagates(self):
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with self.repo.transaction() as same:
                self.assertIs(same, self.repo)
                raise RuntimeError("fixture failure")
        self.connection.transaction.return_value.__exit__.assert_called_once()

    def test_snapshots_and_contexts_have_no_update_api(self):
        self.assertFalse(hasattr(self.repo, "upsert_snapshot"))
        self.assertFalse(hasattr(self.repo, "upsert_context"))
        self.assertFalse(hasattr(self.repo, "delete_context"))
        with self.assertRaises(TypeError):
            self.repo.create_generation(records()[ChannelProfile])


@unittest.skipUnless(os.getenv("RUN_LEARNING_DB_TESTS") == "1", "Explicit PostgreSQL integration opt-in required")
class RepositoryPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.connection = cls.stack.enter_context(connect_database())
        cls.schema = "learning_repo_test_" + uuid4().hex
        cls.transaction_stack = ExitStack()
        cls.addClassCleanup(cls.transaction_stack.close)
        cls.transaction_stack.enter_context(cls.connection.transaction(force_rollback=True))
        apply_migrations(cls.connection, schema=cls.schema)

    @classmethod
    def tearDownClass(cls):
        cls.transaction_stack.close()
        if cls.connection.execute("SELECT to_regnamespace(%s)", (cls.schema,)).fetchone()[0] is not None:
            raise AssertionError("Temporary repository schema was not rolled back")

    def setUp(self):
        self.test_stack = ExitStack()
        self.addCleanup(self.test_stack.close)
        self.test_stack.enter_context(self.connection.transaction(force_rollback=True))
        self.a = LearningRepository(self.connection, "channel-a", schema=self.schema)
        self.b = LearningRepository(self.connection, "channel-b", schema=self.schema)
        self.data_a = records()
        self.data_b = records("channel-b")

    def seed(self, repo, data):
        for model, method in ((ChannelProfile,"create_profile"), (LearningContext,"create_context"), (GenerationRecord,"create_generation"), (PublishedVideo,"create_published_video"), (MetricSnapshot,"insert_snapshot"), (ContentFeatures,"create_features")):
            self.assertEqual(getattr(repo, method)(data[model]), data[model])

    def test_empty_database(self):
        self.assertIsNone(self.a.get_profile())
        self.assertIsNone(self.a.get_generation("missing"))
        self.assertIsNone(self.a.get_published_video("missing"))
        self.assertIsNone(self.a.get_published_video_by_youtube_id("missing"))
        self.assertIsNone(self.a.get_context("missing"))
        self.assertIsNone(self.a.latest_context())
        for method in (self.a.list_generations, self.a.list_published_videos, self.a.list_snapshots, self.a.list_features, self.a.list_contexts):
            self.assertEqual(method(), [])

    def test_all_entities_round_trip_and_channel_isolation(self):
        self.seed(self.a, self.data_a)
        self.seed(self.b, self.data_b)
        for repo, data in ((self.a,self.data_a),(self.b,self.data_b)):
            self.assertEqual(repo.get_profile(), data[ChannelProfile])
            self.assertEqual(repo.get_generation("gen-1"), data[GenerationRecord])
            self.assertEqual(repo.get_published_video("published-1"), data[PublishedVideo])
            self.assertEqual(repo.get_published_video_by_youtube_id(data[PublishedVideo].youtube_video_id), data[PublishedVideo])
            self.assertEqual(repo.published_videos_for_generation("gen-1"), [data[PublishedVideo]])
            self.assertEqual(repo.list_generations(), [data[GenerationRecord]])
            self.assertEqual(repo.list_published_videos(), [data[PublishedVideo]])
            self.assertEqual(repo.list_snapshots(), [data[MetricSnapshot]])
            self.assertEqual(repo.list_features(), [data[ContentFeatures]])
            self.assertEqual(repo.get_features("features-1"), data[ContentFeatures])
            self.assertEqual(repo.list_features(generation_id="gen-1"), [data[ContentFeatures]])
            self.assertEqual(repo.list_contexts(), [data[LearningContext]])
            self.assertEqual(repo.get_context("context-1"), data[LearningContext])
        self.assertIsNone(self.a.get_published_video_by_youtube_id("video-channel-b"))
        self.assertEqual(self.a.list_snapshots(youtube_video_id="video-channel-b"), [])
        only_b = self.data_b[GenerationRecord].model_copy(update={"generation_id":"only-b"})
        self.b.create_generation(only_b)
        self.assertIsNone(self.a.get_generation("only-b"))
        only_features = self.data_b[ContentFeatures].model_copy(update={"feature_record_id":"only-b"})
        self.b.create_features(only_features)
        self.assertIsNone(self.a.get_features("only-b"))
        only_context = self.data_b[LearningContext].model_copy(update={"context_id":"only-b"})
        self.b.create_context(only_context)
        self.assertIsNone(self.a.get_context("only-b"))

    def test_profiles_upsert_and_publication_identity(self):
        self.seed(self.a,self.data_a)
        profile = self.data_a[ChannelProfile]
        updated = profile.model_copy(update={"display_name":"Updated identity", "configuration_version":"2", "updated_at":profile.updated_at + timedelta(hours=1)})
        self.assertEqual(self.a.upsert_profile(updated), updated)
        with self.assertRaises(RepositoryConflict):
            self.a.upsert_profile(profile)
        video = self.data_a[PublishedVideo]
        updated_video = video.model_copy(update={"privacy_status":"public", "updated_at":video.updated_at + timedelta(hours=1)})
        self.assertEqual(self.a.upsert_published_video(updated_video), updated_video)
        with self.assertRaises(RepositoryConflict):
            self.a.upsert_published_video(updated_video.model_copy(update={"youtube_video_id":"different-video"}))
        self.assertEqual(self.a.get_published_video("published-1"), updated_video)
        with self.assertRaises(RepositoryConflict):
            self.a.upsert_profile(updated.model_copy(update={"created_at":profile.created_at - timedelta(days=1)}))
        self.assertEqual(self.b.upsert_profile(self.data_b[ChannelProfile]), self.data_b[ChannelProfile])

    def test_recent_and_date_ranges_and_pagination(self):
        self.seed(self.a,self.data_a)
        original = self.data_a[GenerationRecord]
        later = original.model_copy(update={"generation_id":"gen-2", "generated_at": original.generated_at + timedelta(hours=1)})
        self.a.create_generation(later)
        self.assertEqual(self.a.recent_generations(limit=1), [later])
        self.assertEqual(self.a.list_generations(start_at=original.generated_at,end_at=later.generated_at), [original])
        self.assertEqual(self.a.list_generations(limit=1,offset=1), [original])
        self.assertEqual(self.a.list_generations(start_at=later.generated_at,end_at=later.generated_at), [])
        video = self.data_a[PublishedVideo]
        later_video = video.model_copy(update={"published_video_id":"published-2","youtube_video_id":"video-later", "published_at":video.published_at + timedelta(hours=1)})
        self.a.upsert_published_video(later_video)
        self.assertEqual(self.a.recent_published_videos(limit=1), [later_video])
        self.assertEqual(self.a.list_published_videos(end_at=later_video.published_at), [video])

    def test_snapshot_history_chronology_null_and_zero(self):
        self.seed(self.a,self.data_a)
        snapshot = self.data_a[MetricSnapshot]
        later = snapshot.model_copy(update={"snapshot_id":"snapshot-2", "collected_at":snapshot.collected_at + timedelta(hours=1), "metrics":{"views":None,"likes":0}})
        self.a.insert_snapshot(later)
        self.assertEqual(self.a.list_snapshots(youtube_video_id=snapshot.youtube_video_id), [snapshot,later])
        self.assertEqual(self.a.list_snapshots(start_at=later.collected_at), [later])
        self.assertEqual(self.a.list_snapshots(end_at=later.collected_at), [snapshot])
        self.assertIsNone(self.a.get_snapshot("snapshot-2").metrics["views"])
        self.assertEqual(self.a.get_snapshot("snapshot-2").metrics["likes"], 0)
        with self.assertRaises(RepositoryConflict):
            self.a.insert_snapshot(later.model_copy(update={"metrics":{"views":999}}))
        self.assertEqual(self.a.get_snapshot("snapshot-2"),later)
        self.assertIsNone(self.b.get_snapshot("snapshot-2"))

    def test_feature_annotations_and_video_links(self):
        self.seed(self.a,self.data_a)
        original = self.data_a[ContentFeatures]
        reference = {"channel_context_id":"channel-a", "record_type":"published_video", "record_id":"published-1"}
        new = ContentFeatures.model_validate(dict(original.model_dump(), feature_record_id="features-2", video_reference=reference))
        self.a.create_features(new)
        self.assertEqual(self.a.list_features(published_video_id="published-1"), [new])
        loaded = self.a.get_features("features-2")
        self.assertEqual(loaded.hook_patterns[0].knowledge,"inferred")
        self.assertEqual(loaded.provenance,original.provenance)
        self.assertEqual(loaded.extraction_version,original.extraction_version)
        self.assertEqual(self.b.list_features(published_video_id="published-1"), [])

    def test_immutable_context_versions_and_freshness(self):
        self.seed(self.a,self.data_a)
        original = self.data_a[LearningContext]
        newer = original.model_copy(update={"context_id":"context-2", "context_version":"2", "built_at":original.built_at + timedelta(hours=1), "expires_at":original.built_at + timedelta(hours=2)})
        self.a.create_context(newer)
        self.assertEqual(self.a.latest_context(),newer)
        self.assertEqual(self.a.list_contexts(),[newer,original])
        self.assertEqual(self.a.get_context("context-1"),original)
        self.assertEqual(self.a.latest_context().observations,original.observations)
        self.assertEqual(self.a.latest_context().data_through_at,original.data_through_at)
        with self.assertRaises(RepositoryConflict):
            self.a.create_context(newer.model_copy(update={"context_version":"replacement"}))
        self.assertEqual(self.a.get_context("context-2"),newer)
        self.assertIsNone(self.b.latest_context())

    def test_related_transaction_rolls_back_and_connection_recovers(self):
        self.a.create_profile(self.data_a[ChannelProfile])
        with self.assertRaises(RepositoryConflict):
            with self.a.transaction():
                self.a.create_generation(self.data_a[GenerationRecord])
                self.a.create_generation(self.data_a[GenerationRecord])
        self.assertIsNone(self.a.get_generation("gen-1"))
        self.assertEqual(self.a.create_generation(self.data_a[GenerationRecord]),self.data_a[GenerationRecord])
        # Same-scope nonexistent targets are rejected by PostgreSQL, not just Pydantic.
        invalid = PublishedVideo.model_validate(dict(self.data_a[PublishedVideo].model_dump(), generation_reference={"channel_context_id":"channel-a","record_type":"generation","record_id":"missing"}))
        with self.assertRaises(RepositoryError):
            self.a.create_published_video(invalid)
        self.assertIsNone(self.a.get_published_video("published-1"))
        self.assertEqual(self.a.create_published_video(self.data_a[PublishedVideo]),self.data_a[PublishedVideo])


if __name__ == "__main__":
    unittest.main()
