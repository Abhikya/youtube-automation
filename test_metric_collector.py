"""Offline provider and channel-scoped collector tests."""
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock
from engine.learning.metric_collector import MetricCollector, FakeYouTubeProvider, YouTubeStatisticsProvider, CollectionError, parse_response
from engine.learning.schema import ChannelProfile, PublishedVideo

VIDEO = "iyrVp9tEMVw"
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def response(views="92", likes="0", comments="2"):
    stats = {k:v for k,v in {"viewCount":views,"likeCount":likes,"commentCount":comments}.items() if v is not None}
    return {"items":[{"id":VIDEO,"snippet":{"channelId":"platform-channel"},"statistics":stats}]}


class Repository:
    channel_context_id = "context-a"
    def __init__(self):
        self.profile = ChannelProfile(channel_context_id=self.channel_context_id, provenance={"source":"fixture"}, display_name="Test", identity="Test", youtube_channel_id="platform-channel", content_config={},metadata_strategy={},configuration_version="v1",created_at=NOW,updated_at=NOW)
        self.publication = PublishedVideo(channel_context_id=self.channel_context_id, provenance={"source":"fixture"},published_video_id="pub-1",youtube_video_id=VIDEO,created_at=NOW,updated_at=NOW)
        self.snapshots = {}
    def get_profile(self):return self.profile
    def get_published_video_by_youtube_id(self, identifier):return self.publication if identifier==VIDEO else None
    def get_snapshot(self, identifier):return self.snapshots.get(identifier)
    def insert_snapshot(self, snapshot):
        snapshot.require_channel(self.channel_context_id)
        if snapshot.snapshot_id in self.snapshots:raise ValueError("duplicate")
        self.snapshots[snapshot.snapshot_id]=snapshot
    @contextmanager
    def transaction(self):
        before=copy.deepcopy(self.snapshots)
        try:yield
        except Exception:
            self.snapshots=before
            raise


class MetricCollectorTests(unittest.TestCase):
    def setUp(self):
        self.repo=Repository()
        self.provider=FakeYouTubeProvider({VIDEO:response()})
        self.collector=MetricCollector(self.repo,self.provider,clock=lambda:NOW)
    def collect(self, **kwargs):return self.collector.collect("context-a",VIDEO,**kwargs)
    def test_success_and_repository_persistence(self):
        result=self.collect()
        self.assertTrue(result["success"])
        self.assertEqual(result["snapshot"].metrics,{"views":92,"likes":0,"comments":2})
        self.assertEqual(len(self.repo.snapshots),1)
    def test_missing_optional_metrics_are_null(self):
        self.provider.responses[VIDEO]=response(likes=None,comments=None)
        result=self.collect()
        self.assertEqual(result["snapshot"].metrics,{"views":92,"likes":None,"comments":None})
        self.assertEqual(result["snapshot"].collection_status,"partial")
    def test_all_missing_is_unavailable(self):
        self.provider.responses[VIDEO]=response(None,None,None)
        self.assertEqual(self.collect()["snapshot"].collection_status,"unavailable")
    def test_invalid_missing_video_ids(self):
        for identifier in (None,"","bad",[VIDEO]):
            self.assertFalse(self.collector.collect("context-a",identifier)["success"])
        self.assertEqual(self.provider.calls,[])
    def test_wrong_context(self):
        self.assertEqual(self.collector.collect("other",VIDEO)["error_type"],"WrongChannel")
        self.assertEqual(self.provider.calls,[])
    def test_publication_not_found(self):
        self.repo.publication=None
        self.assertEqual(self.collect()["error_type"],"PublishedVideoNotFound")
    def test_foreign_publication(self):
        self.repo.publication.channel_context_id="other"
        self.assertFalse(self.collect()["success"])
        self.assertEqual(self.provider.calls,[])
    def test_missing_platform_mapping(self):
        self.repo.profile.youtube_channel_id=None
        self.assertEqual(self.collect()["error_type"],"ChannelMappingRequired")
    def test_response_wrong_channel(self):
        self.provider.responses[VIDEO]["items"][0]["snippet"]["channelId"]="other"
        self.assertEqual(self.collect()["error_type"],"ResponseChannelMismatch")
        self.assertEqual(self.repo.snapshots,{})
    def test_api_failure_does_not_leak_secret(self):
        self.provider.responses[VIDEO]=RuntimeError("secret-token")
        result=self.collect()
        self.assertFalse(result["success"])
        self.assertNotIn("secret-token",str(result))
        self.assertIsNone(result["snapshot"])
    def test_malformed_responses(self):
        for raw in (None,{}, {"items":None},{"items":[{}]}, {"items":[]}):
            self.provider.responses[VIDEO]=raw
            self.assertFalse(self.collect()["success"])
        self.assertEqual(self.repo.snapshots,{})
    def test_invalid_metrics(self):
        for value in (-1,True,"-1","1.5",1.5):
            self.provider.responses[VIDEO]=response(value)
            self.assertEqual(self.collect()["error_type"],"InvalidMetric")
    def test_timezone_and_naive_rejection(self):
        self.assertEqual(self.collect()["snapshot"].collected_at,NOW)
        self.assertEqual(self.collect(observed_at=NOW.replace(tzinfo=None))["error_type"],"NaiveObservationTime")
    def test_identical_observation_skipped(self):
        self.collect()
        self.assertEqual(self.collect()["action"],"SKIP_ALREADY_EXISTS")
        self.assertEqual(len(self.repo.snapshots),1)
    def test_conflicting_observation_never_overwritten(self):
        self.collect();self.provider.responses[VIDEO]=response("100")
        self.assertEqual(self.collect()["error_type"],"ObservationConflict")
        self.assertEqual(next(iter(self.repo.snapshots.values())).metrics["views"],92)
    def test_new_timestamp_is_new_observation(self):
        self.collect();self.collect(observed_at=NOW+timedelta(seconds=1))
        self.assertEqual(len(self.repo.snapshots),2)
    def test_equivalent_timezone_is_same_identity(self):
        self.collect()
        stamp=NOW.astimezone(timezone(timedelta(hours=5,minutes=30)))
        self.assertEqual(self.collect(observed_at=stamp)["action"],"SKIP_ALREADY_EXISTS")
    def test_batch_partial_failure(self):
        results=self.collector.collect_batch("context-a",["not-valid",VIDEO])
        self.assertEqual([r["success"] for r in results],[False,True])
    def test_preview_does_not_persist(self):
        self.assertEqual(self.collect(persist=False)["action"],"PREVIEW")
        self.assertEqual(self.repo.snapshots,{})
    def test_real_repository_adapter_with_borrowed_mock_connection(self):
        from unittest.mock import MagicMock, patch
        from engine.learning.repositories import LearningRepository
        candidate = self.collect(persist=False)["snapshot"]
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        database_row = candidate.model_dump(mode="json")
        window = database_row.pop("observation_window")
        database_row.update({"observation_window_" + key: value for key, value in window.items()})
        cursor.execute.return_value.fetchone.return_value = database_row
        repository = LearningRepository(connection, "context-a")
        with patch.object(repository, "get_profile", return_value=self.repo.profile), \
             patch.object(repository, "get_published_video_by_youtube_id", return_value=self.repo.publication), \
             patch.object(repository, "get_snapshot", return_value=None):
            result = MetricCollector(repository, self.provider, clock=lambda: NOW).collect("context-a", VIDEO)
        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "INSERT")
        self.assertIn('"metric_snapshots"', cursor.execute.call_args.args[0].as_string())
        self.assertTrue(connection.transaction.called)

    def test_repository_failure_reported_without_secret(self):
        from unittest.mock import patch
        with patch.object(self.repo, "insert_snapshot", side_effect=RuntimeError("secret-password")):
            result = self.collect()
        self.assertFalse(result["success"])
        self.assertEqual(self.repo.snapshots, {})
        self.assertNotIn("secret-password", str(result))

    def test_missing_or_foreign_profile_rejected(self):
        self.repo.profile.channel_context_id = "other"
        self.assertFalse(self.collect()["success"])
        self.repo.profile = None
        self.assertEqual(self.collect()["error_type"], "ChannelProfileNotFound")
        self.assertEqual(self.provider.calls, [])

    def test_live_timestamp_override_is_forbidden(self):
        collector = MetricCollector(self.repo, YouTubeStatisticsProvider(Mock()))
        result = collector.collect("context-a", VIDEO, observed_at=NOW)
        self.assertEqual(result["error_type"], "LiveTimestampOverrideForbidden")

    def test_provider_only_reads_and_sanitizes_error(self):
        service=Mock();service.videos.return_value.list.return_value.execute.return_value=response()
        self.assertEqual(YouTubeStatisticsProvider(service).fetch(VIDEO)["metrics"]["views"],92)
        service.videos.return_value.list.assert_called_once()
        service.videos.return_value.insert.assert_not_called()
        service.videos.return_value.list.return_value.execute.side_effect=RuntimeError("secret")
        with self.assertRaisesRegex(CollectionError,"YouTubeRequestFailed"):
            YouTubeStatisticsProvider(service).fetch(VIDEO)



class AuthenticatedChannelPreflightTests(unittest.TestCase):
    def setUp(self):
        self.profile = Repository().profile
        self.service = Mock()
        self.service.channels.return_value.list.return_value.execute.return_value = {"items": [{"id": "platform-channel"}]}
        self.factory = Mock(return_value=self.service)

    def preflight(self):
        from engine.learning.metric_collector import authenticated_channel_preflight
        before = self.profile.model_dump()
        result = authenticated_channel_preflight(self.profile, service_factory=self.factory)
        self.assertEqual(self.profile.model_dump(), before)
        self.service.videos.assert_not_called()
        return result

    def test_missing_mapping_requires_human_confirmation(self):
        self.profile.youtube_channel_id = None
        result = self.preflight()
        self.assertEqual(result["status"], "MAPPING_REQUIRED")
        self.assertFalse(result["collection_authorized"])
        self.assertEqual(result["authenticated_channel_id"], "platform-channel")
        self.assertIsNone(self.profile.youtube_channel_id)

    def test_match_uses_mine_lookup_and_readonly_auth(self):
        result = self.preflight()
        self.assertEqual(result["status"], "MATCH")
        self.assertTrue(result["collection_authorized"])
        self.factory.assert_called_once_with(allow_interactive=False, read_only=True)
        self.service.channels.return_value.list.assert_called_once_with(part="id", mine=True, fields="items/id,nextPageToken")
        self.service.close.assert_called_once()

    def test_mismatch_does_not_collect(self):
        self.profile.youtube_channel_id = "another-channel"
        result = self.preflight()
        self.assertEqual(result["status"], "MISMATCH")
        self.assertFalse(result["collection_authorized"])

    def test_authentication_failure_sanitized(self):
        self.factory.side_effect = RuntimeError("secret-token")
        result = self.preflight()
        self.assertEqual(result["status"], "AUTHENTICATION_FAILED")
        self.assertFalse(result["collection_authorized"])
        self.assertNotIn("secret-token", str(result))
        self.service.channels.assert_not_called()

    def test_lookup_failure_sanitized_and_service_closed(self):
        self.service.channels.return_value.list.return_value.execute.side_effect = RuntimeError("secret-token")
        result = self.preflight()
        self.assertEqual(result["status"], "CHANNEL_LOOKUP_FAILED")
        self.assertFalse(result["collection_authorized"])
        self.assertNotIn("secret-token", str(result))
        self.service.close.assert_called_once()

    def test_ambiguous_missing_and_malformed_channels_fail_closed(self):
        for response in ({}, None, {"items": []}, {"items": [{"id": "a"}, {"id": "b"}]},
                         {"items": [{}]}, {"items": [{"id": ""}]},
                         {"items": [{"id": "platform-channel"}], "nextPageToken": "next"}):
            self.service.channels.return_value.list.return_value.execute.return_value = response
            result = self.preflight()
            self.assertEqual(result["status"], "CHANNEL_LOOKUP_FAILED")
            self.assertFalse(result["collection_authorized"])

    def test_successful_preflight_allows_existing_collector_preview(self):
        repo = Repository()
        result = self.preflight()
        if result["collection_authorized"]:
            provider = FakeYouTubeProvider({VIDEO: response()})
            collected = MetricCollector(repo, provider, clock=lambda: NOW).collect("context-a", VIDEO, persist=False)
            self.assertTrue(collected["success"])
            self.assertEqual(provider.calls, [VIDEO])
        else:
            self.fail("Expected authorized preflight")
        self.assertEqual(repo.snapshots, {})

    def test_no_database_or_statistics_operations_in_preflight(self):
        from unittest.mock import patch
        with patch("psycopg.connect", side_effect=AssertionError("database forbidden")):
            self.preflight()
        self.service.channels.return_value.insert.assert_not_called()
        self.service.channels.return_value.update.assert_not_called()
        self.service.channels.return_value.delete.assert_not_called()

    def test_existing_auth_readonly_rejects_missing_or_expired_token(self):
        from unittest.mock import patch, mock_open
        from engine.uploader import youtube_uploader as auth
        for exists, valid in ((False, False), (True, False)):
            credentials = Mock(valid=valid, expired=True, refresh_token="not-printed")
            with patch.object(auth.os.path, "isfile", return_value=exists), \
                 patch("builtins.open", mock_open()) as opened, \
                 patch.object(auth.pickle, "load", return_value=credentials), \
                 patch.object(auth, "build") as build, patch.object(auth.pickle, "dump") as dump:
                with self.assertRaises(RuntimeError):
                    auth.get_authenticated_service(read_only=True)
                credentials.refresh.assert_not_called()
                build.assert_not_called()
                dump.assert_not_called()
                self.assertTrue(all(call.args[1] == "rb" for call in opened.call_args_list))

    def test_existing_auth_readonly_blocks_http_refresh_and_token_writes(self):
        from unittest.mock import patch, mock_open
        from engine.uploader import youtube_uploader as auth
        credentials = Mock(valid=True)
        with patch.object(auth.os.path, "isfile", return_value=True), \
             patch("builtins.open", mock_open()) as opened, \
             patch.object(auth.pickle, "load", return_value=credentials), \
             patch.object(auth.pickle, "dump") as dump, \
             patch.object(auth, "build", return_value=self.service) as build:
            self.assertIs(auth.get_authenticated_service(read_only=True), self.service)
            with self.assertRaises(RuntimeError):
                credentials.refresh(Mock())
            dump.assert_not_called()
            opened.assert_called_once_with(auth.YOUTUBE_TOKEN_FILE, "rb")
            build.assert_called_once_with("youtube", "v3", credentials=credentials)


if __name__ == "__main__":unittest.main()
