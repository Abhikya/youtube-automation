"""Offline descriptive analysis fixtures; no network or live database."""
from datetime import timedelta
from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch
from engine.learning.schema import MetricSnapshot
from engine.learning.content_analysis import analyze_content, preview_analysis
from engine.learning.content_features import extract_features
from test_content_features import Repository, NOW


def snapshot(identifier="snap-1", video="abcdefghijk", **changes):
    data=dict(channel_context_id="channel-a",provenance={"source":"fixture"},snapshot_id=identifier,
              youtube_video_id=video,collected_at=NOW,metric_source="fixture",observation_window={"kind":"cumulative","end_at":NOW},
              metrics={"views":93,"likes":6,"comments":0},collection_status="success")
    data.update(changes)
    return MetricSnapshot(**data)


class ContentAnalysisTests(unittest.TestCase):
    def setUp(self):
        repo=Repository()
        self.pub=repo.publication
        self.feature=extract_features("channel-a",generation=repo.generation,publication=self.pub,extracted_at=NOW)
    def analyze(self, metrics=None, features=None, publications=None):
        return analyze_content("channel-a",[self.pub] if publications is None else publications,
                               [snapshot()] if metrics is None else metrics,[self.feature] if features is None else features)
    def test_single_video_join_and_descriptive_ratio(self):
        report=self.analyze()
        self.assertEqual((report.video_count,report.observation_count),(1,1))
        observation=report.videos[0].observations[0]
        self.assertAlmostEqual(observation.like_to_view_ratio,6/93)
        self.assertEqual(observation.comment_to_view_ratio,0)
        self.assertIn("views=93",observation.observation.notes)
        self.assertEqual(report.videos[0].features,[self.feature])
        self.assertEqual(report.comparative_evidence,"insufficient evidence for comparative pattern")
    def test_multiple_videos_separate(self):
        second=self.pub.model_copy(update={"published_video_id":"pub-2","youtube_video_id":"secondvideo"})
        report=self.analyze(publications=[self.pub,second],metrics=[snapshot(),snapshot("snap-2","secondvideo")])
        self.assertEqual(report.video_count,2)
        self.assertEqual(report.observation_count,2)
        self.assertTrue(all(len(v.observations)==1 for v in report.videos))
    def test_multiple_snapshots_chronological_not_summed(self):
        later=snapshot("later",collected_at=NOW+timedelta(hours=1),metrics={"views":100,"likes":7,"comments":1})
        report=self.analyze(metrics=[later,snapshot()])
        self.assertEqual(report.video_count,1)
        self.assertEqual([o.snapshot.metrics["views"] for o in report.videos[0].observations],[93,100])
        self.assertEqual(report.comparative_evidence,"insufficient evidence for comparative pattern")
    def test_missing_metrics_preserved(self):
        report=self.analyze(metrics=[snapshot(metrics={"views":None,"likes":6},collection_status="partial")])
        observation=report.videos[0].observations[0]
        self.assertEqual(observation.missing_metrics,["views","comments"])
        self.assertIsNone(observation.like_to_view_ratio)
        self.assertIsNone(observation.comment_to_view_ratio)
        self.assertIsNone(observation.snapshot.metrics["views"])
    def test_zero_likes_comments_are_real_zero(self):
        observation=self.analyze(metrics=[snapshot(metrics={"views":93,"likes":0,"comments":0})]).videos[0].observations[0]
        self.assertEqual(observation.like_to_view_ratio,0)
        self.assertEqual(observation.comment_to_view_ratio,0)
        self.assertEqual(observation.missing_metrics,[])
    def test_zero_views_has_no_ratio(self):
        observation=self.analyze(metrics=[snapshot(metrics={"views":0,"likes":0,"comments":0})]).videos[0].observations[0]
        self.assertIsNone(observation.like_to_view_ratio)
        self.assertIsNone(observation.comment_to_view_ratio)
    def test_missing_features_metrics_and_publication_time_reported(self):
        report=self.analyze(features=[],metrics=[])
        self.assertEqual(report.videos[0].missing_data,["content_features","metric_snapshots","publication_timestamp"])
    def test_cross_channel_records_rejected(self):
        for kind in ("publications","features","metrics"):
            value={"publications":self.pub,"features":self.feature,"metrics":snapshot()}[kind]
            report=self.analyze(**{kind:[value.model_copy(update={"channel_context_id":"foreign"})]})
            self.assertTrue(any("InvalidOrWrongChannel" in issue for issue in report.issues))
            self.assertNotIn("foreign",report.model_dump_json())
    def test_missing_publication_orphan_evidence_not_joined(self):
        report=self.analyze(publications=[])
        self.assertEqual(report.video_count,0)
        self.assertEqual(report.observation_count,0)
        self.assertIn("MetricSnapshot:MissingOrAmbiguousPublication",report.issues)
        self.assertIn("ContentFeatures:MissingOrInvalidPublicationLink",report.issues)
    def test_malformed_record_reported_without_payload(self):
        report=self.analyze(metrics=[{"secret":"sensitive-text"},snapshot()])
        self.assertEqual(report.observation_count,1)
        self.assertNotIn("sensitive-text",report.model_dump_json())
        self.assertTrue(report.issues)
    def test_deterministic_order_and_non_mutating(self):
        later=snapshot("later",collected_at=NOW+timedelta(hours=1))
        original=self.feature.model_dump()
        self.assertEqual(self.analyze(metrics=[snapshot(),later]),self.analyze(metrics=[later,snapshot()]))
        self.assertEqual(self.feature.model_dump(),original)
    def test_duplicate_conflicting_snapshot_quarantined(self):
        changed=snapshot(metrics={"views":200})
        report=self.analyze(metrics=[snapshot(),changed])
        self.assertEqual(report.observation_count,0)
        self.assertIn("MetricSnapshot:ConflictingIdentity",report.issues)
    def test_failed_snapshot_cannot_supply_ratio(self):
        observation=self.analyze(metrics=[snapshot(collection_status="failed")]).videos[0].observations[0]
        self.assertIsNone(observation.like_to_view_ratio)
    def test_generated_observations_make_no_causal_claim(self):
        note=self.analyze().videos[0].observations[0].observation.notes.lower()
        for word in ("causes","caused","increases","improves","successful","best"):
            self.assertNotIn(word,note)
    def test_feature_versions_kept_separate_and_generation_only_link_supported(self):
        old=self.feature.model_copy(update={"feature_record_id":"old","video_reference":None,"extraction_version":"preview-v1"})
        report=self.analyze(features=[self.feature,old])
        self.assertEqual(len(report.videos[0].features),2)
    def test_wrong_generation_reference_rejected(self):
        wrong=self.feature.model_copy(update={"generation_reference":self.feature.generation_reference.model_copy(update={"record_id":"wrong"})})
        report=self.analyze(features=[wrong])
        self.assertEqual(report.videos[0].features,[])
        self.assertIn("ContentFeatures:GenerationLinkMismatch",report.issues)
    def test_preview_pages_readonly_without_writes(self):
        from psycopg.pq import TransactionStatus
        class Repo:
            channel_context_id="channel-a"
            _connection=Mock()
            @contextmanager
            def transaction(self):yield
        repo=Repo()
        repo._connection.info.transaction_status=TransactionStatus.IDLE
        repo.list_published_videos=Mock(side_effect=[[self.pub],[]])
        repo.list_snapshots=Mock(side_effect=[[snapshot()],[]])
        repo.list_features=Mock(side_effect=[[self.feature],[]])
        with patch("psycopg.connect",side_effect=AssertionError("live connection forbidden")):
            report=preview_analysis(repo,page_size=1)
        self.assertEqual(report.video_count,1)
        repo._connection.execute.assert_called_once_with("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    def test_inventory_failure_is_not_partial_success(self):
        repo=Mock(channel_context_id="channel-a")
        del repo._connection
        repo.transaction.return_value.__enter__=Mock()
        repo.transaction.return_value.__exit__=Mock(return_value=False)
        repo.list_published_videos.side_effect=RuntimeError("inventory unavailable")
        with self.assertRaises(RuntimeError):preview_analysis(repo)


if __name__=="__main__":unittest.main()
