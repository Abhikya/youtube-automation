"""Offline observation previews: no live services or persistence."""
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch
from engine.learning.learning_observations import build_observations, preview_observations, ObservationBuildError
from engine.learning.content_analysis import analyze_content
from engine.learning.content_features import extract_features
from test_content_analysis import snapshot
from test_content_features import Repository, NOW


class LearningObservationTests(unittest.TestCase):
    def setUp(self):
        repo=Repository()
        self.pub=repo.publication
        self.feature=extract_features("channel-a",generation=repo.generation,publication=self.pub,extracted_at=NOW)
    def analysis(self,metrics=None,features=None,pubs=None):
        return analyze_content("channel-a",[self.pub] if pubs is None else pubs,
            [snapshot()] if metrics is None else metrics,[self.feature] if features is None else features)
    def test_single_descriptive(self):
        result=build_observations(self.analysis())
        self.assertIn("views=93",result.observations[0].notes)
        self.assertEqual(result.observations[0].sample_count,1)
        self.assertEqual(len(result.observations[0].evidence),2)
    def test_insufficient(self):
        result=build_observations(self.analysis())
        self.assertEqual(result.comparative_evidence,"INSUFFICIENT EVIDENCE")
        self.assertEqual(result.observations[-1].notes,"insufficient evidence for comparative pattern")
    def test_temporal_not_independent(self):
        later=NOW+timedelta(minutes=14)
        result=build_observations(self.analysis(metrics=[snapshot(),snapshot("later",collected_at=later,observation_window={"kind":"cumulative","end_at":later})]))
        temporal=[o for o in result.observations if "counter change" in o.notes]
        self.assertEqual(len(temporal),1)
        self.assertIn('"views": 0',temporal[0].notes)
        self.assertEqual(temporal[0].sample_count,1)
        self.assertEqual(result.observations[-1].sample_count,1)
    def test_versions_not_videos(self):
        old=self.feature.model_copy(update={"feature_record_id":"older","extraction_version":"old"})
        result=build_observations(self.analysis(features=[self.feature,old]))
        self.assertEqual(result.observations[-1].sample_count,1)
        self.assertEqual(len(result.analysis.videos[0].features),2)
    def test_multiple_videos(self):
        other=self.pub.model_copy(update={"published_video_id":"second","youtube_video_id":"secondvideo"})
        result=build_observations(self.analysis(pubs=[self.pub,other],metrics=[snapshot(),snapshot("second",video="secondvideo")]))
        self.assertEqual(result.observations[-1].sample_count,2)
        self.assertEqual(result.comparative_evidence,"INSUFFICIENT EVIDENCE")
    def test_no_comparison_without_comparability(self):
        result=build_observations(self.analysis())
        self.assertIn("comparison design",result.observations[-1].uncertainty_notes)
    def test_missing_metrics(self):
        result=build_observations(self.analysis(metrics=[snapshot(metrics={"views":None,"likes":0})]))
        self.assertIn("views=unknown",result.observations[0].notes)
        self.assertIsNone(result.analysis.videos[0].observations[0].like_to_view_ratio)
    def test_missing_features(self):
        result=build_observations(self.analysis(features=[]))
        self.assertTrue(any("content_features" in o.notes for o in result.observations))
    def test_channel_isolation(self):
        bad=self.feature.model_copy(update={"channel_context_id":"foreign"})
        with self.assertRaises(ObservationBuildError):build_observations(self.analysis(features=[bad]))
    def test_provenance_and_timestamps(self):
        result=build_observations(self.analysis())
        retained=result.analysis.videos[0].observations[0].snapshot
        self.assertEqual(retained.provenance,snapshot().provenance)
        self.assertEqual(retained.collected_at,NOW)
        self.assertEqual(result.analysis.videos[0].features[0].provenance,self.feature.provenance)
    def test_idempotent_and_deterministic(self):
        a=self.analysis()
        before=a.model_dump_json()
        first=build_observations(a)
        self.assertEqual(first,build_observations(a.model_dump(mode="json")))
        self.assertEqual(a.model_dump_json(),before)
        self.assertEqual(len(first.evidence_digest),64)
    def test_conflict(self):
        bad=snapshot(metrics={"views":94})
        with self.assertRaises(ObservationBuildError):build_observations(self.analysis(metrics=[snapshot(),bad]))
    def test_changed_evidence_changes_digest(self):
        a=build_observations(self.analysis())
        b=build_observations(self.analysis(metrics=[snapshot(metrics={"views":94,"likes":6,"comments":0})]))
        self.assertNotEqual(a.evidence_digest,b.evidence_digest)
    def test_no_causal_claims(self):
        result=build_observations(self.analysis())
        text=" ".join(o.notes for o in result.observations).lower()
        for word in ("causes","caused","increases views","improves performance"):
            self.assertNotIn(word,text)
    def test_preview_no_persistence(self):
        repo=Mock()
        with patch("engine.learning.learning_observations.preview_analysis",return_value=self.analysis()) as read:
            result=preview_observations(repo)
        read.assert_called_once_with(repo,page_size=100)
        repo.create_context.assert_not_called()
        repo.create_features.assert_not_called()
        repo.insert_snapshot.assert_not_called()
        self.assertTrue(result.observations)
    def test_forged_ratio_rejected(self):
        a=self.analysis()
        a.videos[0].observations[0].like_to_view_ratio=1.0
        with self.assertRaises(ObservationBuildError):build_observations(a)
    def test_empty_no_invented_evidence(self):
        result=build_observations(self.analysis(pubs=[],metrics=[],features=[]))
        self.assertEqual(result.observations,[])
        self.assertEqual(result.comparative_evidence,"INSUFFICIENT EVIDENCE")
    def test_different_sources_no_temporal_delta(self):
        later=NOW+timedelta(minutes=10)
        result=build_observations(self.analysis(metrics=[snapshot(),snapshot("later",collected_at=later,metric_source="other",observation_window={"kind":"cumulative","end_at":later})]))
        self.assertFalse(any("counter change" in o.notes for o in result.observations))


if __name__ == "__main__":unittest.main()
