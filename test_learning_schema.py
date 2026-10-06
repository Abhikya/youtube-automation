"""Offline schema contracts: no production imports, files, credentials or APIs."""
import json
import unittest
from pydantic import ValidationError
from engine.learning.schema import (
    ChannelProfile, GenerationRecord, PublishedVideo, MetricSnapshot,
    ContentFeatures, LearningContext, FeatureValue, ObservationWindow,
)

STAMP = "2026-10-02T10:00:00+05:30"
BASE = {"channel_context_id": "channel-a", "provenance": {"source": "offline-fixture", "source_run_id": "run-1"}}


def ref(kind="generation", channel="channel-a"):
    return {"channel_context_id": channel, "record_type": kind, "record_id": "record-1"}


def fixtures():
    return [
        (ChannelProfile, dict(BASE, display_name="Science Stories", identity="Accessible science", content_config={"topics": ["space"]}, metadata_strategy={"language": "en"}, configuration_version="1", created_at=STAMP, updated_at=STAMP)),
        (GenerationRecord, dict(BASE, generation_id="gen-1", idea="How stars form", script="Spoken words", title="Star formation", description="A science story", generated_at=STAMP, model_versions={"script": "model-1"}, prompt_versions={"script": "v1"}, context_version="v1")),
        (PublishedVideo, dict(BASE, published_video_id="published-1", youtube_video_id="video-1", generation_reference=ref(), created_at=STAMP, updated_at=STAMP)),
        (MetricSnapshot, dict(BASE, snapshot_id="snapshot-1", youtube_video_id="video-1", collected_at=STAMP, metric_source="fixture", observation_window={"kind": "cumulative"}, metrics={"views": 0, "likes": None}, collection_status="partial")),
        (ContentFeatures, dict(BASE, feature_record_id="features-1", generation_reference=ref(), hook_patterns=[{"value": "question", "knowledge": "inferred", "notes": "Opening sentence only"}], extraction_method="manual", extraction_version="1", created_at=STAMP)),
        (LearningContext, dict(BASE, context_id="context-1", context_version="1", analysis_period={"kind": "interval", "start_at": STAMP, "end_at": STAMP}, observations=[{"notes": "Observed association only", "evidence": [ref("metric_snapshot")], "sample_count": 1, "uncertainty_notes": "Small sample"}], examples=[ref()], sample_count=1, built_at=STAMP, data_through_at=STAMP)),
    ]


class LearningSchemaTests(unittest.TestCase):
    def test_all_six_valid_entities_and_round_trips(self):
        for model, data in fixtures():
            with self.subTest(entity=model.__name__):
                record = model.model_validate(data)
                self.assertEqual(record, model.model_validate_json(record.model_dump_json()))
                self.assertEqual(record, model.model_validate(record.model_dump()))
                self.assertEqual(json.loads(record.model_dump_json())["channel_context_id"], "channel-a")

    def test_every_required_field_is_enforced(self):
        for model, data in fixtures():
            for field, definition in model.model_fields.items():
                if definition.is_required():
                    with self.subTest(entity=model.__name__, field=field):
                        invalid = dict(data)
                        invalid.pop(field)
                        with self.assertRaises(ValidationError):
                            model.model_validate(invalid)

    def test_timestamps_reject_naive_or_invalid_values(self):
        for model, data in fixtures():
            for field in ("created_at", "updated_at", "generated_at", "collected_at", "built_at", "data_through_at"):
                if field in data:
                    for value in ("2026-10-02T10:00:00", "not-a-date"):
                        with self.subTest(entity=model.__name__, field=field, value=value):
                            with self.assertRaises(ValidationError):
                                model.model_validate(dict(data, **{field: value}))
        with self.assertRaises(ValidationError):
            ObservationWindow(kind="interval", start_at="2026-10-02T10:00:00", end_at=STAMP)

    def test_channel_boundary_rejects_other_channel_for_every_entity(self):
        for model, data in fixtures():
            record = model.model_validate(data)
            self.assertIs(record.require_channel("channel-a"), record)
            with self.subTest(entity=model.__name__), self.assertRaisesRegex(ValueError, "different channel"):
                record.require_channel("channel-b")

    def test_cross_channel_references_rejected(self):
        for model, data, field, kind in [
            (GenerationRecord, fixtures()[1][1], "context_reference", "learning_context"),
            (PublishedVideo, fixtures()[2][1], "generation_reference", "generation"),
            (ContentFeatures, fixtures()[4][1], "generation_reference", "generation"),
            (ContentFeatures, fixtures()[4][1], "video_reference", "published_video"),
            (LearningContext, fixtures()[5][1], "examples", "generation"),
        ]:
            value = ref(kind, "channel-b")
            with self.subTest(entity=model.__name__, field=field), self.assertRaises(ValidationError):
                model.model_validate(dict(data, **{field: [value] if field == "examples" else value}))
        data = fixtures()[5][1]
        with self.assertRaises(ValidationError):
            LearningContext.model_validate(dict(data, observations=[{"notes": "Observation", "evidence": [ref("metric_snapshot", "channel-b")], "sample_count": 1}]))

    def test_unknown_metrics_and_hook_preserved(self):
        record = MetricSnapshot.model_validate(fixtures()[3][1])
        self.assertEqual(record.metrics["views"], 0)
        self.assertIsNone(record.metrics["likes"])
        self.assertNotIn("comments", record.metrics)
        self.assertIsNone(MetricSnapshot.model_validate(dict(fixtures()[3][1], metrics=None)).metrics)
        generation = GenerationRecord.model_validate(fixtures()[1][1])
        self.assertIsNone(generation.hook)
        self.assertIsNone(generation.measured_duration_seconds)
        self.assertIsNone(generation.hashtags)
        published = PublishedVideo.model_validate(fixtures()[2][1])
        self.assertIsNone(published.published_at)
        self.assertIsNone(published.privacy_status)

    def test_inference_and_provenance_versions_preserved(self):
        feature = ContentFeatures.model_validate(fixtures()[4][1])
        self.assertEqual(feature.hook_patterns[0].knowledge, "inferred")
        self.assertEqual(feature.extraction_version, "1")
        generation = GenerationRecord.model_validate_json(GenerationRecord.model_validate(fixtures()[1][1]).model_dump_json())
        self.assertEqual(generation.provenance.source_run_id, "run-1")
        self.assertEqual(generation.model_versions, {"script": "model-1"})
        self.assertEqual(generation.prompt_versions, {"script": "v1"})
        self.assertEqual(generation.context_version, "v1")

    def test_invalid_ids_extra_fields_and_types(self):
        for model, data in fixtures():
            for value in (None, "", "display name", 123):
                with self.subTest(entity=model.__name__, value=value), self.assertRaises(ValidationError):
                    model.model_validate(dict(data, channel_context_id=value))
            with self.assertRaises(ValidationError):
                model.model_validate(dict(data, unexpected=True))
        for value in (True, -1, "10", float("nan"), float("inf")):
            with self.subTest(metric=value), self.assertRaises(ValidationError):
                MetricSnapshot.model_validate(dict(fixtures()[3][1], metrics={"views": value}))
        for value in (True, -1, "40"):
            with self.assertRaises(ValidationError):
                GenerationRecord.model_validate(dict(fixtures()[1][1], measured_duration_seconds=value))
        with self.assertRaises(ValidationError):
            ChannelProfile.model_validate(dict(fixtures()[0][1], content_config={"bad": float("nan")}))

    def test_invalid_links_and_feature_claims(self):
        with self.assertRaises(ValidationError):
            PublishedVideo.model_validate(dict(fixtures()[2][1], generation_reference=ref("metric_snapshot")))
        with self.assertRaises(ValidationError):
            ContentFeatures.model_validate(dict(fixtures()[4][1], generation_reference=None))
        for data in ({"knowledge": "known"}, {"knowledge": "unknown", "value": "question"}):
            with self.assertRaises(ValidationError):
                FeatureValue.model_validate(data)
        self.assertIsNone(FeatureValue(knowledge="unknown").value)

    def test_invalid_time_windows_and_freshness(self):
        later = "2026-10-03T10:00:00+05:30"
        with self.assertRaises(ValidationError):
            ObservationWindow(kind="interval", start_at=later, end_at=STAMP)
        with self.assertRaises(ValidationError):
            ObservationWindow(kind="interval", end_at=STAMP)
        with self.assertRaises(ValidationError):
            ObservationWindow(kind="unknown", end_at=STAMP)
        with self.assertRaises(ValidationError):
            ChannelProfile.model_validate(dict(fixtures()[0][1], created_at=later))
        for updates in ({"data_through_at": later}, {"expires_at": "2026-10-01T10:00:00+05:30"}, {"sample_count": 0}, {"sample_count": True}):
            with self.assertRaises(ValidationError):
                LearningContext.model_validate(dict(fixtures()[5][1], **updates))
        with self.assertRaises(ValidationError):
            MetricSnapshot.model_validate(dict(fixtures()[3][1], observation_window={"kind": "cumulative", "end_at": later}))

    def test_evidence_required_and_empty_context_allowed(self):
        data = fixtures()[5][1]
        with self.assertRaises(ValidationError):
            LearningContext.model_validate(dict(data, observations=[{"notes": "Unsupported claim", "evidence": [], "sample_count": 1}]))
        record = LearningContext.model_validate(dict(data, sample_count=0, observations=[], examples=None))
        self.assertEqual(record.sample_count, 0)
        self.assertIsNone(record.uncertainty_notes)

    def test_no_production_modules_imported(self):
        import subprocess
        import sys
        code = "import sys; import engine.learning.schema; assert not any(name.startswith(('config.settings', 'engine.pipeline', 'engine.scheduling', 'engine.uploader', 'engine.approval')) for name in sys.modules)"
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
