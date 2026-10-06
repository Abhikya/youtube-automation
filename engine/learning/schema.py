"""Channel-neutral learning records; no storage, API calls or production imports.

Use model_dump_json/model_validate_json for persistence. IDs are caller-assigned
opaque identifiers, never derived from display names. require_channel must be used
at a channel-scoped boundary; references also validate channel ownership locally.
A reference cannot verify a remote record's existence without a future store.
"""
from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StrictFloat,
    StrictInt, StringConstraints, model_validator,
)

Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"^\S+$")]
Count = Annotated[StrictInt, Field(ge=0)]
Seconds = Annotated[StrictInt | StrictFloat, Field(ge=0)]
MetricValue = Annotated[StrictInt | StrictFloat, Field(ge=0)] | None


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def finite_json(self):
        def check(value):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Non-finite numbers are not serializable learning data")
            if isinstance(value, dict):
                for child in value.values():
                    check(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    check(child)
        check(self.model_dump())
        return self


class Provenance(Schema):
    source: Text
    source_record_id: Identifier | None = None
    source_run_id: Identifier | None = None
    captured_at: AwareDatetime | None = None
    notes: Text | None = None


class RecordReference(Schema):
    channel_context_id: Identifier
    record_type: Literal["channel_profile", "generation", "published_video", "metric_snapshot", "content_features", "learning_context"]
    record_id: Identifier


class ObservationWindow(Schema):
    """Explicitly separates cumulative measurements from interval reports."""
    kind: Literal["cumulative", "interval", "unknown"]
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_bounds(self):
        if self.kind == "interval" and (self.start_at is None or self.end_at is None):
            raise ValueError("Interval windows require both bounds")
        if self.kind == "unknown" and (self.start_at is not None or self.end_at is not None):
            raise ValueError("Unknown windows cannot claim known bounds")
        if self.start_at is not None and self.end_at is not None and self.start_at > self.end_at:
            raise ValueError("Window start must not follow end")
        return self


class ChannelRecord(Schema):
    channel_context_id: Identifier
    provenance: Provenance

    def require_channel(self, channel_context_id: str):
        """Reject use of this record in another channel's operation."""
        if self.channel_context_id != channel_context_id:
            raise ValueError("Record belongs to a different channel_context_id")
        return self

    @model_validator(mode="after")
    def validate_channel_and_times(self):
        def check(value):
            if isinstance(value, RecordReference):
                if value.channel_context_id != self.channel_context_id:
                    raise ValueError("Cross-channel reference rejected")
            elif isinstance(value, BaseModel):
                for name in type(value).model_fields:
                    check(getattr(value, name))
            elif isinstance(value, list):
                for child in value:
                    check(child)
        for name in type(self).model_fields:
            check(getattr(self, name))
        created = getattr(self, "created_at", None)
        updated = getattr(self, "updated_at", None)
        if created is not None and updated is not None and updated < created:
            raise ValueError("updated_at must not precede created_at")
        return self


def require_reference_type(reference, expected):
    if reference is not None and reference.record_type != expected:
        raise ValueError(f"Expected {expected} reference")


class ChannelProfile(ChannelRecord):
    youtube_channel_id: Identifier | None = None
    display_name: Text
    identity: Text
    content_config: dict[Text, JsonValue]
    metadata_strategy: dict[Text, JsonValue]
    configuration_version: Identifier
    created_at: AwareDatetime
    updated_at: AwareDatetime


class GenerationRecord(ChannelRecord):
    generation_id: Identifier
    run_id: Identifier | None = None
    idea: Text
    topic: Text | None = None
    script: Text
    hook: Text | None = None  # Explicitly supplied only; never extracted implicitly.
    title: Text
    description: Text
    hashtags: list[Text] | None = None
    measured_duration_seconds: Seconds | None = None
    model_versions: dict[Text, Text] | None = None
    prompt_versions: dict[Text, Text] | None = None
    context_version: Identifier | None = None
    context_reference: RecordReference | None = None
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def context_link(self):
        require_reference_type(self.context_reference, "learning_context")
        return self


class PublishedVideo(ChannelRecord):
    published_video_id: Identifier
    youtube_video_id: Identifier
    generation_reference: RecordReference | None = None
    published_at: AwareDatetime | None = None
    privacy_status: Literal["public", "private", "unlisted"] | None = None
    upload_metadata: dict[Text, JsonValue] | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def generation_link(self):
        require_reference_type(self.generation_reference, "generation")
        return self


class MetricSnapshot(ChannelRecord):
    snapshot_id: Identifier
    youtube_video_id: Identifier
    collected_at: AwareDatetime
    metric_source: Text
    observation_window: ObservationWindow
    video_age_seconds: Seconds | None = None
    metrics: dict[Text, MetricValue] | None = None
    collection_status: Literal["success", "partial", "failed", "unavailable"]
    error_type: Text | None = None
    error_message: Text | None = None

    @model_validator(mode="after")
    def collection_consistency(self):
        if self.collection_status == "success" and (self.error_type is not None or self.error_message is not None):
            raise ValueError("Successful collection cannot carry an error")
        if self.observation_window.end_at is not None and self.observation_window.end_at > self.collected_at:
            raise ValueError("Observation window cannot end after collection")
        return self


class FeatureValue(Schema):
    value: Text | None = None
    knowledge: Literal["known", "inferred", "unknown"]
    notes: Text | None = None

    @model_validator(mode="after")
    def knowledge_consistency(self):
        if (self.knowledge == "unknown") != (self.value is None):
            raise ValueError("Unknown features require None; known/inferred features require a value")
        return self


class ContentFeatures(ChannelRecord):
    feature_record_id: Identifier
    generation_reference: RecordReference | None = None
    video_reference: RecordReference | None = None
    topics: list[FeatureValue] | None = None
    categories: list[FeatureValue] | None = None
    title_patterns: list[FeatureValue] | None = None
    hook_patterns: list[FeatureValue] | None = None
    keywords: list[FeatureValue] | None = None
    hashtags: list[FeatureValue] | None = None
    content_structure: list[FeatureValue] | None = None
    extraction_method: Text
    extraction_version: Identifier
    created_at: AwareDatetime

    @model_validator(mode="after")
    def links(self):
        if self.generation_reference is None and self.video_reference is None:
            raise ValueError("Features require a generation or published-video reference")
        require_reference_type(self.generation_reference, "generation")
        require_reference_type(self.video_reference, "published_video")
        return self


class LearningObservation(Schema):
    """Descriptive observation, not a causal claim or an AI confidence score."""
    notes: Text
    evidence: Annotated[list[RecordReference], Field(min_length=1)]
    sample_count: Annotated[StrictInt, Field(gt=0)]
    uncertainty_notes: Text | None = None


class LearningContext(ChannelRecord):
    context_id: Identifier
    context_version: Identifier
    analysis_period: ObservationWindow
    observations: list[LearningObservation]
    examples: list[RecordReference] | None = None
    sample_count: Count
    uncertainty_notes: Text | None = None
    built_at: AwareDatetime
    data_through_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def freshness_and_samples(self):
        if self.data_through_at is not None and self.data_through_at > self.built_at:
            raise ValueError("Data freshness cannot be in the future")
        if self.expires_at is not None and self.expires_at < self.built_at:
            raise ValueError("Context cannot expire before it is built")
        if self.analysis_period.end_at is not None and self.analysis_period.end_at > self.built_at:
            raise ValueError("Analysis period cannot end after context creation")
        if any(observation.sample_count > self.sample_count for observation in self.observations):
            raise ValueError("Observation sample count exceeds context sample count")
        return self
