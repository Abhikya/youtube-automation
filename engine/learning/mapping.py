"""Small field adapters using the existing schemas, not a second model layer.

No repository or production integration. Callers must scope SQL by channel;
composite foreign keys enforce ownership of linked records, not broad SELECTs.
"""
from decimal import Decimal
from psycopg.types.json import Jsonb
from engine.learning.schema import (
    ChannelProfile, GenerationRecord, PublishedVideo, MetricSnapshot,
    ContentFeatures, LearningContext,
)

TABLES = {
    ChannelProfile: "channel_profiles", GenerationRecord: "generation_records",
    PublishedVideo: "published_videos", MetricSnapshot: "metric_snapshots",
    ContentFeatures: "content_features", LearningContext: "learning_contexts",
}
JSON_FIELDS = {
    "provenance", "content_config", "metadata_strategy", "model_versions", "prompt_versions",
    "context_reference", "generation_reference", "video_reference", "upload_metadata",
    "metrics", "topics", "categories", "title_patterns", "hook_patterns", "keywords",
    "content_structure", "observations", "examples",
}


def to_columns(record):
    model = type(record)
    if model not in TABLES:
        raise TypeError("Unsupported learning schema.")
    # Revalidate even model_construct() or mutated nested data at the boundary.
    validated = model.model_validate(record.model_dump())
    values = validated.model_dump()
    json_values = validated.model_dump(mode="json")
    for key in values:
        if key in JSON_FIELDS or (model is ContentFeatures and key == "hashtags"):
            values[key] = None if values[key] is None else Jsonb(json_values[key])
    window_name = "observation_window" if model is MetricSnapshot else "analysis_period" if model is LearningContext else None
    if window_name:
        window = values.pop(window_name)
        for key, value in window.items():
            values[f"{window_name}_{key}"] = value
    return values


def from_row(model, row, *, channel_context_id):
    if model not in TABLES:
        raise TypeError("Unsupported learning schema.")
    values = dict(row)
    values.pop("row_id", None)
    for key in ("linked_context_id", "linked_generation_id", "linked_video_id"):
        values.pop(key, None)
    window_name = "observation_window" if model is MetricSnapshot else "analysis_period" if model is LearningContext else None
    if window_name:
        values[window_name] = {key: values.pop(f"{window_name}_{key}") for key in ("kind", "start_at", "end_at")}
    for key in ("measured_duration_seconds", "video_age_seconds"):
        if isinstance(values.get(key), Decimal):
            number = values[key]
            values[key] = int(number) if number == number.to_integral_value() else float(number)
    return model.model_validate(values).require_channel(channel_context_id)
