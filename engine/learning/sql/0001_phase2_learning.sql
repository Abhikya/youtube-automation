-- 0001: isolated Phase 2 learning foundation. No operational queue changes.
-- Domain IDs remain TEXT; row_id is an internal UUID. No metric defaults.

CREATE TABLE channel_profiles (
    row_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_context_id TEXT NOT NULL CHECK (channel_context_id ~ '^\S+$'),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance) = 'object'),
    display_name TEXT NOT NULL CHECK (display_name ~ '\S'),
    identity TEXT NOT NULL CHECK (identity ~ '\S'),
    youtube_channel_id TEXT,
    content_config JSONB NOT NULL CHECK (jsonb_typeof(content_config) = 'object'),
    metadata_strategy JSONB NOT NULL CHECK (jsonb_typeof(metadata_strategy) = 'object'),
    configuration_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL CHECK (updated_at >= created_at),
    UNIQUE (channel_context_id)
);

CREATE TABLE learning_contexts (
    row_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_context_id TEXT NOT NULL CHECK (channel_context_id ~ '^\S+$'),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance) = 'object'),
    context_id TEXT NOT NULL, context_version TEXT NOT NULL,
    analysis_period_kind TEXT NOT NULL CHECK (analysis_period_kind IN ('cumulative', 'interval', 'unknown')),
    analysis_period_start_at TIMESTAMPTZ, analysis_period_end_at TIMESTAMPTZ,
    observations JSONB NOT NULL CHECK (jsonb_typeof(observations) = 'array'),
    examples JSONB, sample_count BIGINT NOT NULL CHECK (sample_count >= 0), uncertainty_notes TEXT,
    built_at TIMESTAMPTZ NOT NULL, data_through_at TIMESTAMPTZ, expires_at TIMESTAMPTZ,
    CHECK (data_through_at IS NULL OR data_through_at <= built_at),
    CHECK (expires_at IS NULL OR expires_at >= built_at),
    CHECK (analysis_period_end_at IS NULL OR analysis_period_end_at <= built_at),
    UNIQUE (channel_context_id, context_id),
    FOREIGN KEY (channel_context_id) REFERENCES channel_profiles (channel_context_id),
    CHECK (analysis_period_kind <> 'interval' OR (analysis_period_start_at IS NOT NULL AND analysis_period_end_at IS NOT NULL)),
    CHECK (analysis_period_kind <> 'unknown' OR (analysis_period_start_at IS NULL AND analysis_period_end_at IS NULL)),
    CHECK (analysis_period_start_at IS NULL OR analysis_period_end_at IS NULL OR analysis_period_start_at <= analysis_period_end_at)
);

CREATE TABLE generation_records (
    row_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_context_id TEXT NOT NULL CHECK (channel_context_id ~ '^\S+$'),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance) = 'object'),
    generation_id TEXT NOT NULL,
    run_id TEXT, idea TEXT NOT NULL, topic TEXT, script TEXT NOT NULL, hook TEXT,
    title TEXT NOT NULL, description TEXT NOT NULL, hashtags TEXT[],
    measured_duration_seconds NUMERIC CHECK (measured_duration_seconds >= 0),
    model_versions JSONB, prompt_versions JSONB, context_version TEXT,
    context_reference JSONB,
    linked_context_id TEXT GENERATED ALWAYS AS (context_reference->>'record_id') STORED,
    generated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (channel_context_id, generation_id),
    FOREIGN KEY (channel_context_id) REFERENCES channel_profiles (channel_context_id),
    CHECK ((context_reference IS NULL OR (jsonb_typeof(context_reference) = 'object' AND context_reference ?& ARRAY['record_id','record_type','channel_context_id'] AND context_reference->>'record_id' IS NOT NULL AND context_reference->>'record_type' = 'learning_context' AND context_reference->>'channel_context_id' = channel_context_id)) IS TRUE),
    FOREIGN KEY (channel_context_id, linked_context_id) REFERENCES learning_contexts (channel_context_id, context_id)
);

CREATE TABLE published_videos (
    row_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_context_id TEXT NOT NULL CHECK (channel_context_id ~ '^\S+$'),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance) = 'object'),
    published_video_id TEXT NOT NULL, youtube_video_id TEXT NOT NULL UNIQUE,
    generation_reference JSONB,
    linked_generation_id TEXT GENERATED ALWAYS AS (generation_reference->>'record_id') STORED,
    published_at TIMESTAMPTZ,
    privacy_status TEXT CHECK (privacy_status IN ('public', 'private', 'unlisted')),
    upload_metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL CHECK (updated_at >= created_at),
    UNIQUE (channel_context_id, published_video_id), UNIQUE (channel_context_id, youtube_video_id),
    FOREIGN KEY (channel_context_id) REFERENCES channel_profiles (channel_context_id),
    CHECK ((generation_reference IS NULL OR (jsonb_typeof(generation_reference) = 'object' AND generation_reference ?& ARRAY['record_id','record_type','channel_context_id'] AND generation_reference->>'record_id' IS NOT NULL AND generation_reference->>'record_type' = 'generation' AND generation_reference->>'channel_context_id' = channel_context_id)) IS TRUE),
    FOREIGN KEY (channel_context_id, linked_generation_id) REFERENCES generation_records (channel_context_id, generation_id)
);

CREATE TABLE metric_snapshots (
    row_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_context_id TEXT NOT NULL CHECK (channel_context_id ~ '^\S+$'),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance) = 'object'),
    snapshot_id TEXT NOT NULL, youtube_video_id TEXT NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL, metric_source TEXT NOT NULL,
    observation_window_kind TEXT NOT NULL CHECK (observation_window_kind IN ('cumulative', 'interval', 'unknown')),
    observation_window_start_at TIMESTAMPTZ, observation_window_end_at TIMESTAMPTZ,
    video_age_seconds NUMERIC CHECK (video_age_seconds >= 0),
    metrics JSONB CHECK (jsonb_typeof(metrics) = 'object'),
    collection_status TEXT NOT NULL CHECK (collection_status IN ('success', 'partial', 'failed', 'unavailable')),
    error_type TEXT, error_message TEXT,
    CHECK (collection_status <> 'success' OR (error_type IS NULL AND error_message IS NULL)),
    CHECK (observation_window_end_at IS NULL OR observation_window_end_at <= collected_at),
    UNIQUE (channel_context_id, snapshot_id),
    FOREIGN KEY (channel_context_id) REFERENCES channel_profiles (channel_context_id),
    FOREIGN KEY (channel_context_id, youtube_video_id) REFERENCES published_videos (channel_context_id, youtube_video_id),
    CHECK (observation_window_kind <> 'interval' OR (observation_window_start_at IS NOT NULL AND observation_window_end_at IS NOT NULL)),
    CHECK (observation_window_kind <> 'unknown' OR (observation_window_start_at IS NULL AND observation_window_end_at IS NULL)),
    CHECK (observation_window_start_at IS NULL OR observation_window_end_at IS NULL OR observation_window_start_at <= observation_window_end_at)
);

CREATE TABLE content_features (
    row_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_context_id TEXT NOT NULL CHECK (channel_context_id ~ '^\S+$'),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance) = 'object'),
    feature_record_id TEXT NOT NULL,
    generation_reference JSONB, video_reference JSONB,
    linked_generation_id TEXT GENERATED ALWAYS AS (generation_reference->>'record_id') STORED,
    linked_video_id TEXT GENERATED ALWAYS AS (video_reference->>'record_id') STORED,
    topics JSONB, categories JSONB, title_patterns JSONB, hook_patterns JSONB,
    keywords JSONB, hashtags JSONB, content_structure JSONB,
    extraction_method TEXT NOT NULL, extraction_version TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
    CHECK (generation_reference IS NOT NULL OR video_reference IS NOT NULL),
    UNIQUE (channel_context_id, feature_record_id),
    FOREIGN KEY (channel_context_id) REFERENCES channel_profiles (channel_context_id),
    CHECK ((generation_reference IS NULL OR (jsonb_typeof(generation_reference) = 'object' AND generation_reference ?& ARRAY['record_id','record_type','channel_context_id'] AND generation_reference->>'record_id' IS NOT NULL AND generation_reference->>'record_type' = 'generation' AND generation_reference->>'channel_context_id' = channel_context_id)) IS TRUE),
    FOREIGN KEY (channel_context_id, linked_generation_id) REFERENCES generation_records (channel_context_id, generation_id),
    CHECK ((video_reference IS NULL OR (jsonb_typeof(video_reference) = 'object' AND video_reference ?& ARRAY['record_id','record_type','channel_context_id'] AND video_reference->>'record_id' IS NOT NULL AND video_reference->>'record_type' = 'published_video' AND video_reference->>'channel_context_id' = channel_context_id)) IS TRUE),
    FOREIGN KEY (channel_context_id, linked_video_id) REFERENCES published_videos (channel_context_id, published_video_id)
);

CREATE INDEX generation_records_lookup_7 ON generation_records (channel_context_id, generated_at DESC, generation_id);

CREATE INDEX published_videos_lookup_8 ON published_videos (channel_context_id, published_at DESC, published_video_id);

CREATE INDEX metric_snapshots_lookup_9 ON metric_snapshots (channel_context_id, youtube_video_id, collected_at, snapshot_id);

CREATE INDEX metric_snapshots_lookup_10 ON metric_snapshots (channel_context_id, collected_at);

CREATE INDEX content_features_lookup_11 ON content_features (channel_context_id, linked_generation_id);

CREATE INDEX content_features_lookup_12 ON content_features (channel_context_id, linked_video_id);

CREATE INDEX learning_contexts_lookup_13 ON learning_contexts (channel_context_id, built_at DESC, context_id);

CREATE INDEX published_videos_lookup_14 ON published_videos (channel_context_id, linked_generation_id);
