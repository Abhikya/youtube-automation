# Daily mystery Shorts scheduler

Run from the project root with YouTube OAuth credentials and Telegram settings configured:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
$env:YOUTUBE_PRIVACY_STATUS = "private"
.\venv\Scripts\python.exe run_scheduler.py
```

Keep the process running. Ctrl+C stops it. It logs next execution times for the four daily jobs and hourly cleanup on startup.
No real generation/upload is required to run the offline tests:

```powershell
.\venv\Scripts\python.exe -B -m unittest test_resumable_upload test_integration test_cleanup test_scheduler test_telegram -v
```

## Timing and startup

Timezone is explicitly `Asia/Kolkata` (tzdata supplies Windows timezone data).
08:00 generates three videos sequentially, saving each result before continuing.
10:00 uploads only the first slot, 14:00 only the second, and 19:00 only the third.
YouTube completes or fails before Telegram notification is attempted.

All jobs use `max_instances=1`, coalescing, and a 900-second (15-minute) misfire grace.
Startup recovers only today's occurrence within that window: 08:05 may start the batch;
16:00 does not replay morning work; 18:50 waits for 19:00. There is no old-day backlog.
A slot not ready at its execution is skipped, not substituted or automatically retried later.
Generation taking longer than the first upload deadline may therefore miss that slot.

## JSON queue

Files are `data/daily_queue_YYYY-MM-DD.json`, dated in India time:

```json
{
  "date": "2026-09-14",
  "timezone": "Asia/Kolkata",
  "videos": [
    {
      "slot": "10:00",
      "generation_status": "ready",
      "upload_status": "pending",
      "topic": "unsolved mysteries",
      "idea": "The generated idea",
      "script": "The generated script",
      "video_path": "absolute path to the generated video",
      "metadata": {"Title": "Exact title", "Description": "Exact description", "Hashtags": ["#Mystery"]},
      "youtube": null,
      "telegram": null
    }
  ]
}
```

Actual queues contain all three slots from the beginning. Failed generations are recorded as
`generation_failed` with an error; successful files and their original slots remain intact.
A batch is attempted once per date. Existing queues are not regenerated on restart, including
partially completed batches. A crash may leave `pending`/`generating` entries requiring inspection.

Updates use a temporary file in the same directory, flush/fsync, and atomic replacement.
Local file locks serialize JSON edits and prevent concurrent batches across processes.
An upload is atomically claimed as `uploading` before the network call. Uploaded items,
items with a YouTube ID, and any previously attempted upload are skipped on repeated triggers.
Upload failures are saved and retained locally; there are no automatic retries or replacements.

A crash/network interruption can leave an uncertain remote outcome. An `uploading` item
is intentionally NOT retried after restart: check YouTube manually before taking recovery action.
This is conservative duplicate protection, not a guarantee of an exactly-once remote transaction.
YouTube results are saved before Telegram starts; a Telegram failure never causes another upload.

## Existing manual use

`generate_video(publish=False)` is the shared generation-only path used by the batch.
The API and `run_engine_.py` keep their existing immediate-upload behavior (`publish=True`).
Do not run these commands to test the daily queue; use the offline tests above instead.
The existing pipeline uses shared temporary audio, so avoid manual generation during the daily batch.

No database, story deduplication, deployment, or asset changes are included.


## Step 5: conservative 24-hour cleanup

Cleanup runs once per hour at minute 35 in Asia/Kolkata, with max_instances=1,
coalescing, a local process lock, and the existing 15-minute misfire grace.
There is no startup cleanup replay. It checks eight dates: today and the previous seven.
Older queue history is not scanned or deleted; unresolved items outside this window
are retained for manual inspection.

Each successful generation immediately saves its own timezone-aware
`generation_completed_at`, before computing a file fingerprint. The timer starts at that
completion time, not batch start or upload time. Existing `generated_at` is retained for
compatibility but is NOT used to infer eligibility. Old records without the new timestamp
or ownership manifest are retained; there is no automatic migration/backdating.

Deletion requires ALL of:
- `generation_status == "ready"` and a valid timezone-aware completion timestamp;
- at least 24 hours elapsed;
- `upload_status == "uploaded"`, `youtube.success == true`,
  `youtube.status == "uploaded"`, and a nonempty YouTube video ID;
- a matching generation-time file manifest owned by this queue date/slot;
- a canonical absolute path directly inside `output/videos`, with the pipeline's
  `unsolved_mysteries_YYYYMMDDHHMMSS.mp4` naming;
- a matching SHA-256, size, and modification time for an existing file, and no other
  reference to the file in the scanned queues.

Only the final rendered MP4 is tracked/deleted. The current narration path is shared,
and temporary footage is reusable or not owned per video, so NO temporary files are deleted.
Pexels cache/library, music, source code, .env, OAuth files, settings, scheduler files,
queue JSON, and idea history are outside the cleanup allowlist. Symlinks, junctions,
relative/traversal paths, directories, and hard-linked files are rejected.

The queue is locked during validation/deletion/state update. Queue/history fields are
retained, with `cleanup.status`, `cleaned_at`, `deleted_files`, `absent_files`, and
`failed_files` added. A missing eligible file is marked completed/already absent;
completed items are skipped. Permission failures are recorded and may retry next hour;
one failed file does not stop another item. No remote uploads are retried by cleanup.

Telegram outcome does not affect eligibility. Failed/pending/uploading/uncertain YouTube
outcomes or missing video IDs retain local files indefinitely. Cleanup is isolated from
publishing jobs; a queue-read or filesystem error is logged rather than crashing the scheduler.
No production cleanup is performed by the offline tests; all fixtures live in temporary directories.


## Step 6: readiness and ONE private end-to-end test

The full offline regression suite includes queue corruption, missing metadata, uncertain
uploads, disk-write failures, quota failures, OAuth reuse, and exact upload metadata.
The normal production times remain 08:00 / 10:00 / 14:00 / 19:00, with cleanup at :35 IST.

1. Check local configuration without displaying secrets or contacting APIs:

   ```powershell
   .\venv\Scripts\python.exe -B check_readiness.py
   ```

   This checks presence/structure only: it cannot confirm API balances, token revocation,
   Telegram reachability, or which YouTube channel will be selected. Expected variables in
   the root .env: OPENAI_API_KEY, PEXELS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

2. If YouTube is not authorized, enable YouTube Data API v3 for your Google Cloud project,
   configure the OAuth consent screen (add your Google account as a test user if applicable),
   and create/download a Desktop app OAuth client as `client_secret.json` in this project root.
   Authorize explicitly, selecting the intended YouTube account/channel:

   ```powershell
   .\venv\Scripts\python.exe -m engine.uploader.youtube_uploader --authorize
   ```

   This reuses the existing OAuth implementation, opens the browser only for setup, and
   saves `token.pickle`. Scheduled uploads reuse that token and refresh it when expired.
   They never open a browser automatically. A revoked/unrefreshable token requires manual
   authorization again. `token.json` is not supported. No video is uploaded by --authorize.

3. Run exactly one private test (do not use run_engine_.py for this restart-safety test):

   ```powershell
   $env:YOUTUBE_PRIVACY_STATUS = "private"
   .\venv\Scripts\python.exe run_private_test.py --run-id first-private
   ```

   The command also forces private before importing the uploader. It uses the production
   generation-only function, queue upload function, and Telegram notifier. Its isolated
   queue is `data/manual_tests/first-private/daily_queue_YYYY-MM-DD.json`, with exactly one
   item using the existing 10:00 slot identifier. This explicit manual invocation uploads
   immediately; it does not change production times or add a scheduler job.

   It saves generation metadata before uploading, claims the upload before the network
   call, saves the YouTube outcome before Telegram, and prints the video path, exact
   metadata, YouTube ID/URL/privacy and Telegram result. Its root generation lock prevents
   overlap with the scheduled batch. Run this test away from production generation times.

4. Check the printed MP4 path exists. Open the private video in the intended channel's
   YouTube Studio and compare title, description, and tags against the queue's `metadata`.
   Hashtags are sent as YouTube tags; they are not automatically appended to the description.
   Verify the returned video ID/URL and Private visibility. Local mocks prove request
   construction; actual remote metadata/channel verification is manual during this test.

5. Confirm Telegram received that video's preview, generated title, Private status, and
   YouTube link, with no approval buttons. If Telegram failed, the queue still retains the
   successful YouTube result. There is no automatic re-upload or regeneration.

6. Re-run the SAME command with the SAME run ID to verify restart behavior. Completed,
   failed, or uncertain attempts are not uploaded again. A previously generated ready
   item can resume its first upload. Do not choose a new run ID merely to retry an uncertain
   request: first inspect YouTube and the existing JSON. No automatic reconciliation exists.

No cleanup is invoked by the private test. Its nested test queues are intentionally outside
production's daily cleanup scan, so the test MP4/state are retained for inspection.
Production cleanup remains 24 hours after individual generation completion and requires
confirmed upload success plus a video ID.

Malformed/incomplete queues are skipped safely. An `uploading` or `uncertain` state is logged
as manual-review-required and never blindly retried. If a YouTube result cannot be saved,
the existing uploading claim remains; if Telegram state cannot be saved, YouTube state
remains saved. Failed generation records a clear error, including insufficient OpenAI quota,
with no endless retry. Future scheduled dates can still generate normally.


## Resumable uploads and retrying the existing failed private test

Uploads now stream the existing MP4 using MediaFileUpload(resumable=True), 8 MiB chunks,
and next_chunk(num_retries=0). One insert request is created per upload attempt. The same
request/session is reused across up to five transient retries total (1, 2, 4, 8, 16 seconds).
The Google client probes an existing session after chunk errors and resumes from the server's
confirmed offset. OAuth and exact title/description/tags/privacy mapping are unchanged.

Retried errors: SSL EOF/zero-return, socket timeout, connection reset/abort/broken pipe,
remote disconnect/incomplete read, temporary DNS/network errors, and HTTP 408, 429,
500, 502, 503, 504. Permanent authorization/validation/certificate errors are not retried.
Progress, retry count, and final outcome are logged without session URLs or credentials.

For the saved first-private test ONLY, retry the existing failed upload without regeneration:

```powershell
$env:YOUTUBE_PRIVACY_STATUS = "private"
.\venv\Scripts\python.exe run_private_test.py --run-id first-private --retry-failed-upload
```

This reuses `data/manual_tests/first-private/daily_queue_2026-09-14.json` and its stored
MP4 path and metadata. No OpenAI, Pexels, narration, or MoviePy imports/work are needed.
Generation-readiness checks are skipped; the existing uploader validates file/metadata and
uses the existing token. Telegram runs after the YouTube outcome is saved.

Only an explicit command can retry a recorded `failed` result without a video ID. It
atomically claims the item as uploading and preserves the old result in manual_retry_history.
Already uploaded, ID-bearing, uploading, and uncertain entries remain protected. A missing
queue/video never causes replacement generation. Ordinary scheduler triggers still do not
retry failed or uncertain items.

The earlier one-shot upload has no saved resumable session, so this explicit command begins
one new resumable attempt. Within that attempt retries reuse its session. Sessions are not
persisted across process restarts; a crash leaves uploading/uncertain for manual inspection.
If a prior failure may have completed remotely, inspect YouTube before requesting another
manual attempt. Cleanup timing, scheduler slots, clip cache, music and rendering are unchanged.
