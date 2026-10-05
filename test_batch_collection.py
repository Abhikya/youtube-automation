"""Offline operational batch regressions; no real network or database."""
import copy
import json
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch
import httplib2
from googleapiclient.errors import HttpError
from engine.learning.batch_collection import collect_publications, main
from engine.learning.metric_collector import YouTubeStatisticsProvider, CollectionError, FakeYouTubeProvider
from test_metric_collector import Repository, NOW, VIDEO, response

SECOND = "abcdefghijk"


def http_error(status, retry_after=None, reason=None):
    headers = {"status": str(status)}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return HttpError(httplib2.Response(headers), json.dumps({"error": {"errors": [{"reason": reason}]}}).encode())


class BatchCollectionTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository()
        second = self.repo.publication.model_copy(update={"youtube_video_id": SECOND, "published_video_id": "pub-2"})
        self.publications = [self.repo.publication, second]
        self.repo.list_published_videos = Mock(side_effect=lambda limit, offset: self.publications[offset:offset+limit])
        self.repo.get_published_video_by_youtube_id = lambda identifier: next((p for p in self.publications if p.youtube_video_id == identifier), None)
        second_response = response()
        second_response["items"][0]["id"] = SECOND
        self.provider = FakeYouTubeProvider({VIDEO: response(), SECOND: second_response})

    def collect(self, persist=True):
        return collect_publications(self.repo, self.provider, persist=persist, page_size=1, clock=lambda: NOW)

    def test_multiple_videos_paginated_success_summary(self):
        report = self.collect()
        self.assertEqual(report["totals"], {"collected": 2, "skipped": 0, "failed": 0, "retryable": 0})
        self.assertEqual(report["total"], 2)
        self.assertEqual(len(self.repo.snapshots), 2)
        self.assertEqual(self.repo.list_published_videos.call_count, 3)

    def test_failure_continues_and_is_sanitized(self):
        self.provider.responses[VIDEO] = RuntimeError("secret-credential")
        report = self.collect()
        self.assertEqual([r["status"] for r in report["results"]], ["failed", "collected"])
        self.assertNotIn("secret-credential", json.dumps(report))

    def test_idempotent_observation_and_changed_conflict(self):
        self.collect()
        self.assertEqual(self.collect()["totals"]["skipped"], 2)
        self.provider.responses[VIDEO] = response("999")
        report = self.collect()
        self.assertEqual(report["results"][0]["error_type"], "ObservationConflict")
        self.assertEqual(len(self.repo.snapshots), 2)

    def test_dry_run_default_does_not_persist(self):
        report = collect_publications(self.repo, self.provider, clock=lambda: NOW)
        self.assertEqual(self.repo.snapshots, {})
        self.assertEqual(report["mode"], "PREVIEW")
        self.assertTrue(all(r["action"] == "PREVIEW" for r in report["results"]))

    def test_channel_validation_and_future_skip(self):
        self.publications[0] = self.publications[0].model_copy(update={"channel_context_id": "foreign"})
        self.publications[1] = self.publications[1].model_copy(update={"published_at": NOW + timedelta(days=1)})
        report = self.collect()
        self.assertEqual(report["totals"]["failed"], 1)
        self.assertEqual(report["totals"]["skipped"], 1)
        self.assertEqual(self.provider.calls, [])

    def test_mapping_and_response_ownership(self):
        self.repo.profile.youtube_channel_id = None
        self.assertEqual(self.collect()["totals"]["failed"], 2)
        self.assertEqual(self.provider.calls, [])
        self.repo.profile.youtube_channel_id = "wrong-channel"
        self.assertEqual(self.collect()["totals"]["failed"], 2)
        self.assertEqual(self.repo.snapshots, {})

    def test_duplicate_inventory_skipped(self):
        self.publications.append(self.publications[0])
        self.assertEqual(self.collect()["totals"]["skipped"], 1)
        self.assertEqual(len(self.repo.snapshots), 2)

    def test_retryable_failure_continues(self):
        self.provider.responses[VIDEO] = CollectionError("YouTubeRetryDeferred", retryable=True)
        report = self.collect()
        self.assertEqual([r["status"] for r in report["results"]], ["retryable", "collected"])

    def test_cli_preview_uses_readonly_transaction_and_preflight(self):
        connection = Mock()
        connection.transaction.return_value.__enter__ = Mock()
        connection.transaction.return_value.__exit__ = Mock(return_value=False)
        from contextlib import contextmanager
        @contextmanager
        def database():
            yield connection
        service = Mock()
        summary = {"totals": {"failed": 0, "retryable": 0}}
        with patch("engine.learning.database.connect_database", database), \
             patch("engine.learning.repositories.LearningRepository", return_value=self.repo), \
             patch("engine.learning.batch_collection.authenticated_channel_preflight", return_value={"collection_authorized": True}), \
             patch("engine.uploader.youtube_uploader.get_authenticated_service", return_value=service), \
             patch("engine.learning.batch_collection.collect_publications", return_value=summary) as collect, \
             patch("builtins.print"):
            self.assertEqual(main(["--channel-context-id", "context-a", "--dry-run"]), 0)
        connection.execute.assert_called_once_with("SET TRANSACTION READ ONLY")
        self.assertFalse(collect.call_args.kwargs["persist"])
        service.close.assert_called_once()


class StatisticsRetryTests(unittest.TestCase):
    def provider(self, outcomes):
        self.service = Mock()
        self.service.videos.return_value.list.return_value.execute.side_effect = outcomes
        self.sleep = Mock()
        return YouTubeStatisticsProvider(self.service, sleep=self.sleep, clock=lambda: NOW)

    def test_transient_then_success(self):
        provider = self.provider([TimeoutError(), response()])
        self.assertEqual(provider.fetch(VIDEO)["metrics"]["views"], 92)
        self.sleep.assert_called_once_with(1)
        self.service.videos.return_value.list.return_value.execute.assert_called_with(num_retries=0)

    def test_429_respects_retry_after(self):
        self.provider([http_error(429, "7"), response()]).fetch(VIDEO)
        self.sleep.assert_called_once_with(7)

    def test_retry_after_http_date(self):
        from email.utils import format_datetime
        self.provider([http_error(503, format_datetime(NOW+timedelta(seconds=12))), response()]).fetch(VIDEO)
        self.sleep.assert_called_once_with(12)

    def test_long_retry_after_deferred_without_sleep(self):
        with self.assertRaises(CollectionError) as caught:
            self.provider([http_error(429, "120")]).fetch(VIDEO)
        self.assertTrue(caught.exception.retryable)
        self.sleep.assert_not_called()

    def test_permanent_error_not_retried(self):
        for status in (400, 401, 404):
            with self.assertRaises(CollectionError) as caught:
                self.provider([http_error(status)]).fetch(VIDEO)
            self.assertFalse(caught.exception.retryable)
            self.sleep.assert_not_called()

    def test_quota_exhaustion_not_retried_but_rate_limit_is(self):
        with self.assertRaises(CollectionError):
            self.provider([http_error(403, reason="quotaExceeded")]).fetch(VIDEO)
        self.sleep.assert_not_called()
        self.provider([http_error(403, reason="rateLimitExceeded"), response()]).fetch(VIDEO)
        self.sleep.assert_called_once()

    def test_bounded_retries_and_sanitization(self):
        with self.assertRaises(CollectionError) as caught:
            self.provider([http_error(500)]*4).fetch(VIDEO)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.sleep.call_count, 3)
        self.assertEqual(str(caught.exception), "YouTubeRetryDeferred")


if __name__ == "__main__":
    unittest.main()
