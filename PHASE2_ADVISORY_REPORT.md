# Read-only Phase 2 advisory reporting

The reporting entry point has no persistence, generation, upload, scheduling,
Learning Context or Reporting-job execution path. Existing administrative tools
remain separate. No database changes or new infrastructure are required.

## Explicit inventory

Provide a JSON list of accepted source records. Each includes youtube_video_id,
channel_context_id, youtube_channel_id, classification (PRODUCTION/TEST/UNKNOWN),
accepted_production (boolean), and evidence (source references). Optional content:
title, topic, script, description, hashtags, measured_duration_seconds.
These are report inputs, not invented database GenerationRecord IDs. Evidence is
an operator-approved provenance assertion; the reporter cannot prove an arbitrary
assertion true. Keep source evidence available for review.

Duplicate identical inputs count once; conflicting identities fail closed.
Only accepted PRODUCTION with evidence and matching expected channel is considered.
The authenticated channel must match. Live video ownership, public privacy and
publishedAt are checked before Analytics. Tests, unknown provenance, unlisted,
private/scheduled drafts and future videos are excluded with reasons. Discovery
counts refer to the explicit input inventory, not a channel-wide API enumeration.

## Explicit live read command (not run during implementation)

python -B -m engine.learning.advisory_report --inventory <accepted-inventory.json> --channel-context-id <context> --youtube-channel-id <channel> --start-date <YYYY-MM-DD> --end-date-exclusive <YYYY-MM-DD> --live

Without --live, the CLI refuses to authenticate or read services. It prints JSON
to stdout; no output file is written automatically. Optional --read-learning reads
profile, publications, generations and feature versions through an enforced
repeatable-read/read-only transaction. It never invokes persistence methods.
Missing native generation references/conflicting scripts are reported as failures.
Native feature versions are retained, not counted as separate videos.

Phase 2 authentication loads the existing configured token pickle directly, without
invoking or changing production authentication. An expired access token may refresh
using its existing refresh token. Refresh failure fails closed; no browser consent,
scope upgrade or new credentials are created. Refreshed credentials stay in memory:
**token.pickle is not rewritten**. OAuth refresh is credential maintenance, not a
YouTube content mutation. Both services use the same credential object. Channel
identity validation remains mandatory before video/Analytics reads.

Accepted input: `phase2/reporting/accepted_production_inventory.json` contains the
14 public video IDs explicitly operator-approved in the historical production
dataset. Its provenance references that approval, not invented artifact IDs or
native generation relationships. The 2 tests, 4 unresolved videos and 3 private/
future production candidates are intentionally omitted. Live privacy, publication
and channel checks remain authoritative if a candidate's status changes. Missing
scripts/content remain unknown; the inventory does not invent recovered content.

## Metrics and report semantics

Data API views, likes and comments remain cumulative, with observation timestamp.
Interval Analytics requests views, estimatedMinutesWatched, averageViewDuration,
averageViewPercentage, subscribersGained and subscribersLost. Separate reports
validate insightTrafficSourceType views/watch time and elapsedVideoTimeRatio /
audienceWatchRatio curves. Retention above 1 can represent repeat viewing; only
actual returned points are compared, with no interpolation. Traffic sources are
returned as supplied; absent SHORTS rows are not interpreted as zero Shorts traffic.
Impressions/CTR are not collected. No Reporting API jobs are created.

Analytics windows are Pacific calendar days, end-exclusive at the CLI and converted
to the API inclusive end date. Unfinished calendar days are rejected. No daily rows
means NO_DATA, not zero. All requested days with validated required metrics must
be present before comparisons. This conservative coverage gate does not guarantee
that YouTube reports are final; low-traffic omitted days remain unknown.

Comparisons require identical source/window, full daily coverage, publication before
the window and publication ages differing by at most --max-age-difference-hours
(default 24; 0 enforces identical age). This explicit policy is not proof of matched
exposure or removal of topic/distribution confounding. Cumulative counters are not
used as interval performance comparisons. Missing values remain unknown. Findings
link metrics and content characteristics descriptively without causal claims.

Recommendations require at least three distinct mutually comparable videos in a
common window. This minimum is an exploratory review gate, not statistical
significance. Unknown content or tied metrics do not produce recommendations. Confidence is always low in this version. Every recommendation has
observation, evidence, confidence, recommendation, action=MANUAL_REVIEW_REQUIRED.
No recommendation is emitted below the gate. No recommendation modifies production.

JSON sections: report_metadata, coverage, videos (including structured descriptive
observations), metrics, content_features, comparative_findings, evidence_status,
limitations and recommendations. Coverage includes exclusions and per-video failures.

HTTP retries reuse the existing bounded transient/rate-limit policy, respecting
Retry-After; waits over 60 seconds defer. Authorization/permanent/malformed failures
are sanitized. Offline services are injected; implementation tests make no live calls.

Validation: python -B -m unittest test_advisory_report -v
