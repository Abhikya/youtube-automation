# Phase 1: preparation and scheduled publication

DAY 1: Actions attempts preparation at 05:05, 09:05, 13:05, 17:05 and 21:05 Asia/Kolkata. All attempts address the same tomorrow batch. Existing renders, IDs and verified slots are preserved. Generate missing/retryable slots, upload PRIVATE with publishAt, persist IDs, read back ownership/privacy/schedule/processing, then durably mark READY only after all three pass. READY is a successful no-op on later attempts.

DAY 2: YouTube publishes at 10:00, 14:00 and 19:00 IST (04:30, 08:30 and 13:30 UTC), independently of Actions. Timezone-aware conversion uses the target date; Actions crons are recovery opportunities, not publication timers.

## Configuration and rollout

Set the non-secret repository variable YOUTUBE_CHANNEL_ID to Minute Mysteries' actual channel ID. Preparation checks the authenticated channel before generating media. No channel identity is guessed. Reauthorize locally with `python -m engine.uploader.youtube_uploader --authorize`, then replace YOUTUBE_TOKEN_PICKLE_B64 using the existing secure secret procedure. The OAuth scope changes from youtube.upload to youtube.force-ssl, required for owner readback and videos.update schedule repair. Existing insufficient tokens fail before generation; Actions never starts an interactive browser. No authorization or secret change is performed by this implementation.

Normal scheduling is part of videos.insert. Repair uses the existing ID, only while private; YouTube rejects publishAt for formerly published videos. A wrong channel/public/rejected video blocks that slot without reupload. Processing must succeed before verification. Owner readback is required. An API project restricted to private viewing needs its existing YouTube API compliance restriction resolved before production publication can be relied on.

Manual dispatch supports prepare with blank target (tomorrow) or an explicit tomorrow date. Past/today/arbitrary future dates are blocked. A run freezes its target and stops starting new work if its preparation date ends; it never silently relabels the batch. There is no manual per-slot public upload operation. Legacy helper methods/state remain readable for historical safeguards, but the production CLI only offers prepare/cleanup.

Each unfinished slot gets at most one generation attempt per invocation and five lifetime attempts per target batch. Manual reruns do not reset the budget. Attempt history and previous errors remain recorded. Upload claims are checkpointed before insertion; IDs are persisted before verification or Telegram. Unknown upload outcomes stay protected for manual reconciliation; there is no safe existing owner lookup/session checkpoint that proves a retry cannot duplicate an accepted upload.

The global mystery-production-state concurrency group retains cancel-in-progress=false and adds queue=max (100 pending runs). State remains authoritative regardless of waiting order; overflow and dropped Actions events remain platform limits.

Cleanup runs after a READY preparation opportunity or explicit cleanup dispatch. It preserves tomorrow/current target-date queues and media, requires READY for preparation batches, confirmed upload and 24-hour age, ownership/fingerprints, safe paths and shared-reference checks. Consequently cleanup is deferred rather than forcing immediate DAY 1 deletion. Metadata, IDs, READY evidence and deletion receipts survive cleanup. Telegram failure does not revoke verified preparation or READY.

The old 05:00 cleanup and 10:05/14:05/19:05 upload crons are removed. The separate local scheduler is unchanged and is not used by production Actions. Phase 2 is unchanged. During rollout, do not combine old same-day public operations with the new tomorrow preparation workflow; a legacy queue occupying the target date is rejected for explicit reconciliation.
