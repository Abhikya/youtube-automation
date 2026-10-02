# Phase 2.5 historical and early production learning preview

PHASE 2.5 AUDIT/PREVIEW COMPLETE. Offline only; no import.

## Evidence and performance

Owner confirms all four videos belong to Minute Mysteries. The owner-transcribed Studio observations are dated 2026-10-02. The screenshot itself was not inspected. Exact observation time is unknown.

| Video | Video date | Current views | Title characters / words | Hashtags | Script words | YouTube ID | Local evidence |
|---|---|---:|---|---|---|---|---|
| The Tunguska Enigma | 2026-09-14 | 92 | 19 / 3 | 8 | 83 | iyrVp9tEMVw | CONTENT_COMPLETE_IDENTITY_ASSIGNED |
| The Wow! Signal: A Whisper from the Stars | 2026-10-01 | 400 | 41 / 8 | UNKNOWN | UNKNOWN | UNKNOWN | PERFORMANCE_ONLY |
| The Enigmatic Blaze of Springfield Armory | 2026-10-01 | 4 | 41 / 6 | UNKNOWN | UNKNOWN | UNKNOWN | PERFORMANCE_ONLY |
| The Vanishing of the Beaumont Children | 2026-10-01 | 241 | 38 / 6 | UNKNOWN | UNKNOWN | UNKNOWN | PERFORMANCE_ONLY |

## Provenance and quality

JSON contains every investigated field with KNOWN, INFERRED, DERIVED, UNKNOWN or MISSING labels and provenance. No conflicting known values were found. All non-view metrics remain null. No titles are ranked and no causal conclusions are made.

Tunguska: generation completed 2026-09-14T14:04:34.207231+05:30; logical queue slot 10:00; recorded private successful upload. Actual upload/publication timestamp and current privacy are unknown. Its media hash, size and mtime match. Measured video duration: 33.12 seconds (FFprobe format.duration). Opening sentence is derived, not an explicitly authored hook.

Title/description character counts use Unicode code points; word counts use whitespace splitting. Other videos have title counts only, no inferred scripts or generation topics. Named entities, categories, semantic keywords and scene structure remain unanalysed. Shared narration is not linked.

## Schema candidates

1 ChannelProfile, 1 GenerationRecord, 1 PublishedVideo and 1 ContentFeatures pass existing schema validation. IDs are proposed deterministic storage identities, not recovered historical IDs. Numeric measures stay in the evidence envelope because ContentFeatures lacks dedicated numeric fields. Profile/publication preview timestamps are administrative preview metadata, never upload dates. Empty profile configuration dictionaries mean unspecified strategy, not reconstructed history.

Four MetricSnapshot drafts preserve views and observed_at=2026-10-02 but fail validation by design: collected_at requires an exact aware datetime; three also lack youtube_video_id. No fabricated midnight timestamp or API lookup was used. Zero valid metrics are proposed for immediate import.

LearningContext: NOT READY / INSUFFICIENT EVIDENCE. No object created.

## Inventory and duplicates

2 data JSON files; 2 final MP4s; 30 cached stock MP4s; 30 identical temp copies plus 1 unlinked MP3. Root temp_map.json has 1 partial mapping. Ten idea proposals are excluded as generations. One proposal matches the queue. Previous failed upload and successful retry are one video. No local October 1 record was found. No local CSV/log/JSONL/ZIP evidence was found in the previous inventory; remote artifact availability remains unknown.

## Import safety and next step

No import executed. Reuse channel-scoped schemas and repositories only in a separately authorized future importer. Require source hashes, stable source locators, explicit identity reconciliation, duplicate comparison, conflict rejection, transactional rollback and a dry-run audit manifest. Titles alone cannot establish identity. Current channel ownership is owner-confirmed; platform channel ID remains unknown.

Import dependencies: profile -> generation -> publication -> features -> evidence-backed metric snapshots -> later learning analysis. Resolve October 1 IDs and the exact observation timestamp through owner-provided local evidence. No Phase 2.6 work.

## Validation

Schema round trips, aware timestamps, rejected cross-channel access, provenance, duplicate IDs, known-value conflicts and Tunguska linkage checked locally. No database or production modules imported by preview generation. Socket-connect guard confirms validation requires no network. Offline suite results are reported in the final response; live PostgreSQL tests disabled.

## Safety

ZERO Neon/database connections or writes. ZERO external APIs, uploads or Telegram messages. ZERO Phase 1/workflow/production edits. No commit or push.
