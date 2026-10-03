# Phase 2.7 metric collection foundation

The collector is independent of production scheduling, uploads, queues and Telegram.
It borrows LearningRepository and an existing authenticated YouTube service. Importing
it opens no connections. No new database, migrations or dependencies are required.

## Available statistics

The adapter uses videos.list(part=snippet,statistics) for viewCount, likeCount and
commentCount. Missing counters stay null; real zero stays zero. Invalid counters,
unavailable videos and request errors are explicit per-video failures, not fabricated
metrics. Missing optional counters yield partial snapshots; all absent yields unavailable.

Sources: https://developers.google.com/youtube/v3/docs/videos/list and
https://developers.google.com/youtube/v3/docs/videos.

## Ownership and timestamps

Call collect(channel_context_id, youtube_video_id) or collect_batch(channel_context_id,
video_ids). The repository context, stored publication and profile must match. A verified
ChannelProfile.youtube_channel_id is required and must equal returned snippet.channelId.
The current imported profile lacks that mapping: collection fails ChannelMappingRequired
until the owner supplies/verifies it through a separately authorized profile update.
No channel IDs are guessed or automatically saved.

collected_at is the UTC instant immediately after a successful response. These are
current cumulative counters, not reconstructions of upload-time statistics. Counters
may lag activity. video age is populated only when an exact stored publication time
exists. An explicit observation timestamp is supported for recorded/offline providers;
it is forbidden with the live adapter to prevent backdating live statistics.

UUID5 snapshot identity uses source, channel, video and normalized observation instant.
Different instants append records. Same identity plus identical content skips insertion;
changed content conflicts. insert_snapshot is append-only. Each observation uses the
repository transaction; database uniqueness rejects concurrent duplicate writers safely.
No silent overwrite or automatic retry of conflicting observations. Batch results retain
each failure, and a later video can succeed. API failures return no metric snapshot.

```python
from engine.learning.metric_collector import MetricCollector, YouTubeStatisticsProvider
# repo: existing channel-scoped LearningRepository; service: existing YouTube service
collector = MetricCollector(repo, YouTubeStatisticsProvider(service))
results = collector.collect_batch(repo.channel_context_id, video_ids)
```

Persisting is explicit at invocation (persist=True, default). persist=False validates
without writing snapshots, but repository ownership reads still require a connection.
No collector invocation or snapshot persistence is installed in production.

## Permissions

Production currently declares https://www.googleapis.com/auth/youtube.force-ssl.
This preflight reuses that scope without broadening permissions.
If videos.list rejects existing authorization, read access may require separately approved
OAuth consent with https://www.googleapis.com/auth/youtube.readonly; do not regenerate
or replace production credentials automatically.

Watch time (estimatedMinutesWatched), averageViewDuration and retention reporting require
YouTube Analytics integration with https://www.googleapis.com/auth/yt-analytics.readonly
and channel-owner authorization; Analytics API enablement is also required. Monetary
reports additionally require https://www.googleapis.com/auth/yt-analytics-monetary.readonly.
These fields are not collected yet. A later provider can reuse MetricSnapshot metric_source
and observation_window for interval reports; do not label interval reports cumulative.
Sources: https://developers.google.com/youtube/analytics/reference and
https://developers.google.com/youtube/analytics/metrics.

## Verification

Offline: python -B -m unittest test_metric_collector -v
All offline tests: RUN_LEARNING_DB_TESTS=0 python -B -m unittest discover

Optional live read-only check:
python -B -m engine.learning.metric_collector --verify-video iyrVp9tEMVw

The command uses the existing get_authenticated_service, calls only videos.list and
never opens the database, uploads or notifies. It requires an already-valid local token
and refuses expired/missing credentials, avoiding authentication refresh/token replacement.
No interactive authorization, scope changes or secret output. It reports channel ID,
counters and current observation time for manual verification, not a persisted snapshot.

## Authenticated-channel read-only preflight (Phase 2.6)

`authenticated_channel_preflight(profile)` accepts an existing ChannelProfile and
uses the existing authentication helper with `allow_interactive=False`.
Expired access tokens are refreshed using the existing refresh token, and updated
credentials may be saved locally. Refresh failure reports AUTHENTICATION_FAILED.
Interactive consent remains disabled. Read-only refers to YouTube and learning
data, not local credential storage. Production authentication defaults and OAuth
scopes are unchanged.

Only `channels.list(part="id", mine=True)` is called. Exactly one channel is required;
empty, multiple, paginated or malformed responses fail closed. Results are MATCH,
MISMATCH, MAPPING_REQUIRED, AUTHENTICATION_FAILED or CHANNEL_LOOKUP_FAILED. Only MATCH
sets `collection_authorized=True`. Authentication/lookup failure takes precedence
when the authenticated channel cannot be established.

A missing mapping reports the discovered channel ID for human review but never
assigns it. This function performs no database operations, statistics collection,
uploads or profile mutation. Obtain the channel-scoped profile through existing
repository reads; callers must gate subsequent collector calls on the result.
Existing collector behavior remains unchanged; it does not invoke this preflight
automatically. No live preflight or collection runs at module import.
