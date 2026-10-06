# Deterministic stored-content features

`engine.learning.content_features.extract_features(channel_context_id, generation=...,
publication=..., extracted_at=...)` reuses ContentFeatures. Extraction time is the
actual aware administrative extraction timestamp, never a fabricated historical time.
Sources are validated schemas with same-channel linkage. No YouTube or GPT calls occur.

Known topics, hashtags and explicitly stored hooks/duration retain known labels.
An opening sentence from conservative textual boundary heuristics may be recorded
as inferred, never as a verified spoken/authored hook. Common title abbreviations
do not terminate the sentence. Initials, ellipses, repeated punctuation, decimals
and other ambiguous boundaries remain unknown. Short or incomplete openings remain unknown. No
speech-rate duration, categories or keywords are inferred. Script absence is supported
for publication-only evidence; GenerationRecord itself requires a script.

Title measurements and structural counts use labeled text FeatureValues because the
existing model has no numeric columns. Unicode character and whitespace word counts
are marked inferred (deterministically derived), with calculation notes. Missing tags
stay unknown; an explicit empty list has count zero. Generation hashtags take priority
when present (including an explicit empty list); otherwise publication hashtags
are used. Each known hashtag identifies its source in its notes. Publication-only metadata recognizes
exact Title, Description and Hashtags keys; malformed known values fail safely.

`extract_stored(repository, generation_id=..., published_video_id=..., persist=False)`
loads existing records, resolves generation links and previews by default. Set persist=True
only for explicitly authorized extraction. A source/channel/version UUID5 is stable;
source evidence SHA256 is recorded in provenance. Same evidence skips the existing record
while preserving its first extraction time; changed evidence at the same version conflicts
rather than overwriting. Use a new extraction version for future algorithms. Existing
historical preview-v1 records are preserved. Writes use repository transactions.

`extract_publications(repository, persist=False, page_size=100)` paginates the channel's
stored publications. It requires an idle real repository connection and enumerates
all pages in a repeatable-read, read-only transaction using the existing deterministic
publication time/ID ordering. Extraction/persistence begins after inventory collection.
Concurrent delete/reorder cannot shift the inventory offsets. A source deleted after
inventory is reported as an extraction failure rather than silently omitted.
Malformed rows are isolated; enumeration errors preserve the already-read inventory
and are reported as PublicationInventoryFailed. The return value is a dictionary with
results, collected/skipped/failed totals, complete (inventory traversal) and success
(complete with no failures). Partial batches are never reported as successful.
Standalone generations use extract_stored directly. No CLI or
production integration is installed. No database schema changes are required. Context
building, performance pattern analysis and prompt integration remain future work.
