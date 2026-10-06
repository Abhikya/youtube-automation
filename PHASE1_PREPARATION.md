# Phase 1: preparation and scheduled publication

DAY 1: Actions attempts preparation at 05:05, 09:05, 13:05, 17:05 and 21:05 Asia/Kolkata. All attempts address the same tomorrow batch. Existing renders, IDs and verified slots are preserved. Generate missing/retryable slots, upload PRIVATE with publishAt, persist IDs, read back ownership/privacy/schedule/processing, then durably mark READY only after all three pass. READY is a successful no-op on later attempts.

DAY 2: YouTube publishes at 10:00, 14:00 and 19:00 IST (04:30, 08:30 and 13:30 UTC), independently of Actions. Timezone-aware conversion uses the target date; Actions crons are recovery opportunities, not publication timers.

## Configuration and rollout

Set the non-secret repository variable YOUTUBE_CHANNEL_ID to Minute Mysteries' actual channel ID. Preparation durably records generated media, then checks the authenticated channel before claiming any upload. No channel identity is guessed. Reauthorize locally with `python -m engine.uploader.youtube_uploader --authorize`, then replace YOUTUBE_TOKEN_PICKLE_B64 using the existing secure secret procedure. The OAuth scope changes from youtube.upload to youtube.force-ssl, required for owner readback and videos.update schedule repair. Insufficient tokens leave generated slots pending and recoverable; Actions never starts an interactive browser. No authorization or secret change is performed by this implementation.

Normal scheduling is part of videos.insert. Repair uses the existing ID, only while private; YouTube rejects publishAt for formerly published videos. A wrong channel/public/rejected video blocks that slot without reupload. Processing must succeed before verification. Owner readback is required. An API project restricted to private viewing needs its existing YouTube API compliance restriction resolved before production publication can be relied on.

Manual dispatch supports prepare with blank target (tomorrow), an explicit tomorrow date, or explicit current-day recovery of an existing preparation batch. Older dates and arbitrary future dates are blocked; a new current-day batch cannot be created. Recovery and an in-flight midnight rollover retain the original target and only advance unfinished slots whose publication time is strictly future. Elapsed slots are preserved and reported as blocked; already verified slots are skipped. A READY receipt may follow an earlier verified slot's publication, but must precede the final slot. There is no manual per-slot public upload operation. Legacy helper methods/state remain readable for historical safeguards, but the production CLI only offers prepare/cleanup.

Each unfinished slot gets at most one generation attempt per invocation and five lifetime attempts per target batch. Manual reruns do not reset the budget. Attempt history and previous errors remain recorded. Upload claims are checkpointed before insertion; IDs are persisted before verification or Telegram. Unknown upload outcomes stay protected for manual reconciliation; there is no safe existing owner lookup/session checkpoint that proves a retry cannot duplicate an accepted upload.

The global mystery-production-state concurrency group retains cancel-in-progress=false and adds queue=max (100 pending runs). State remains authoritative regardless of waiting order; overflow and dropped Actions events remain platform limits.

Cleanup runs after a READY preparation opportunity or explicit cleanup dispatch. It preserves tomorrow/current target-date queues and media, requires READY for preparation batches, confirmed upload and 24-hour age, ownership/fingerprints, safe paths and shared-reference checks. Consequently cleanup is deferred rather than forcing immediate DAY 1 deletion. Metadata, IDs, READY evidence and deletion receipts survive cleanup. Telegram failure does not revoke verified preparation or READY.

The old 05:00 cleanup and 10:05/14:05/19:05 upload crons are removed. The separate local scheduler is unchanged and is not used by production Actions. Phase 2 is unchanged. During rollout, do not combine old same-day public operations with the new tomorrow preparation workflow; a legacy queue occupying the target date is rejected for explicit reconciliation.

Recovery results distinguish execution date, immutable target date, generation completion, upload completion, and per-slot verification/problems. Incomplete work exits nonzero with a RECOVERABLE summary instead of a generic terminal error. An authentication failure before insertion leaves renders reusable; only a proven pre-insertion failure can release a claim. Older uploading/uncertain claims without reliable IDs remain blocked for reconciliation. Upload-only recovery requires no generation API secrets. No production upload command or new publication job is added.

Default preparation first checks the existing current-day batch and recovers safe unfinished future slots before preparing tomorrow. Expired slots and uncertain uploads without IDs are preserved and skipped; when no safe future work remains, tomorrow preparation proceeds. Explicit --day still addresses only the requested batch. Both batch outcomes appear in the result. The uploader rechecks publishAt after authentication immediately before insertion; expiry is a proven not-started outcome, never an uncertain transfer.

## Explicit confirmation that an unresolved upload was not accepted

Only after manually confirming in the correct channel's YouTube Studio that no uploaded/private/scheduled video exists for the exact slot, dispatch the production workflow with operation=prepare, day=2026-10-03, confirmed_not_uploaded_slot=14:00. Leave this input blank for ordinary dispatches. The equivalent Actions CLI is `python run_actions.py prepare --day 2026-10-03 --confirm-not-uploaded-slot 14:00`.

This records the operator's Studio confirmation and prior unresolved claim in the durable checkpoint, then releases only that generated slot to pending and resumes preparation. It refuses ID-bearing/confirmed uploads, wrong channels, missing renders, non-unresolved states, older target dates and elapsed publication slots. It never treats a missing ID alone as evidence of absence. Retry remains subject to the existing future-publishAt checks; run before Oct 3 14:00 IST. Do not use this confirmation input to repeat a transfer whose acceptance remains unknown. A later retry after an authentication failure should omit the confirmation input because reconciliation is already saved.

## Phase 3 configuration foundation (not enabled)

`config/channels.py` defines the immutable Minute Mysteries configuration and a
validated registry resolver. Production still uses existing settings, prompts and
secret loading; this descriptive configuration does not change runtime behavior.
Preparation times describe the existing external dispatch opportunities, not a
new scheduler. Content instructions summarize existing rules; existing prompt
text remains authoritative and unchanged.

No legacy state is migrated: `daily_queue_DATE.json`, `mystery-state-*`,
`mystery-media-*`, `.actions-work`, `data/idea_memory.json`, `token.pickle`,
`output/videos`, `temp/audio` and `temp/clips` retain their current identities.
Namespace fields record current roots/prefix identity; they do not rename paths.
A later isolation step must explicitly adopt legacy checkpoints and preserve
confirmed IDs, claims, fingerprints and recovery history before enabling another
channel. Credential/Telegram fields contain reference names only, never secrets.

## Phase 3 durable state identity (single channel remains enabled)

New queues/checkpoints record `channel_key`, `youtube_channel_id` and
`state_namespace`. Upload claims and generated-file ownership include channel,
publication date and slot. Artifact selection uses the resolved namespace.
Minute Mysteries keeps its existing `mystery-state-*`/`mystery-media-*`, queue
paths and lock paths. Default-only legacy adoption accepts untagged state after
existing date, slot, fingerprint, artifact and preparation-channel checks; it
never copies a legacy batch into another channel. Partial/mismatched identities
fail closed. Loading a legacy checkpoint preserves IDs, uncertain claims and
ready renders; the next normal write adds identity without resetting progress.
Non-default state/queue roots are independently addressable for offline tests,
but dispatch, OAuth, rendering, Telegram and workflow concurrency are unchanged.
No second channel is enabled. Historical artifacts are not renamed or rewritten.

## Phase 3 operation-local YouTube credentials

Production binds preflight, upload, verification and recovery to one immutable
channel credential context and reuses its authenticated service for the operation.
The context checks the configured YouTube ID and credential reference; unverified
or mismatched contexts fail before insertion/verification. Reconciliation validates
the same state/channel context without making a YouTube request.
Minute Mysteries still restores `YOUTUBE_TOKEN_PICKLE_B64` to legacy `token.pickle`
and uses the existing noninteractive refresh/save behavior and OAuth scope.
Future non-default contexts address `.credentials/<channel_key>/token.pickle`,
never the default token. Production dispatch still enables only Minute Mysteries.
Legacy callers without a context retain their authentication interfaces; Phase 2
read-only authentication and in-memory refresh are unchanged.


### Channel runtime file boundaries

RuntimePaths in config/channels.py resolves narration, clip working copies,
subtitle and render-audio intermediates under temp/<channel_key>. Production
passes the same path plan into the existing renderer and idea generator.
Minute Mysteries retains output/videos and data/idea_memory.json, including
legacy checkpoint restoration. Future configurations use their own output
namespace and data/channels/<channel_key>/idea_memory.json; they cannot adopt
the default memory or output root. No files are moved or deleted.
Stock footage and background music remain shared assets. Stock cache writes
are query-locked and atomic; existing cache files are not overwritten. Render
inputs are independent channel-local copies, never mutable shared cache files.
Cleanup remains limited to fingerprinted final renders in the channel's output
root; it does not delete temp files or stock cache assets.


### Shared workflow channel selection

The existing production workflow accepts channel_key (default minute_mysteries)
and passes --channel-key explicitly. Both workflow configuration resolution and
the runner use resolve_channel_config; only Minute Mysteries is registered.
Unknown keys fail before production work. The resolved config is passed to
artifact storage, preparation/current-day recovery, reconciliation credentials,
generation paths and cleanup without changing process-global channel settings.
The workflow dynamically selects secrets[credential_reference] and supplies that
value under the resolved reference to the child Python process. Missing selected
credentials fail closed; there is no default-secret fallback.
Concurrency is production-<channel_key>, with cancellation disabled and queue:max
preserved. GitHub evaluates this group before Python validation; invalid keys may
get their own group but cannot perform production work. Existing cron-job.org
payloads may omit channel_key. No GitHub cron or new workflow is introduced.
When deploying the changed group name, allow any older in-flight production run
to finish first, since the old global group and new default group are distinct.
