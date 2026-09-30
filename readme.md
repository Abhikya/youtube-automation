# YouTube mystery automation

The existing pipeline generates unsolved-mystery Shorts, renders them with Pexels
footage, and preserves their generated title, description and tags. No database
or Telegram approval is used.

## GitHub Actions production

**Workflow: `Mystery Shorts production` (`production.yml`). Manual dispatch only.**
The existing `Test single mystery video generation` workflow remains separate and
unchanged. Production operations run only from the default branch.

Prepared, commented schedules use `Asia/Kolkata`:

| Operation | IST |
| --- | --- |
| Generate three assigned videos | 05:00; completion deadline 08:00 |
| Upload only slot-10 | 10:00 |
| Upload only slot-14 | 14:00 |
| Upload only slot-19 | 19:00 |
| Guarded artifact cleanup | Hourly at :35 |

Do not uncomment `schedule` until the manual private end-to-end test passes.
The three-hour generation window is a conservative starting budget based on the
existing single-video workflow's 35-minute generation allowance, not a measured
three-video production runtime. The job limit is 170 minutes. Measure the batch
and adjust the start earlier if necessary. Actions schedules can be delayed;
08:00 completion and exact upload start times are not guaranteed by GitHub.
Scheduled generation reports a missed deadline as failure, preserving completed work.

The local `python run_scheduler.py` remains available; never run it alongside
production Actions against the same channel. Actions runs finite queue operations.
`run_engine_.py` and the API publish immediately and bypass the production artifact
queue: do not use them to retry an Actions slot.

## Secrets and OAuth

Configure repository Actions Secrets:

- `OPENAI_API_KEY`
- `PEXELS_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `YOUTUBE_TOKEN_PICKLE_B64`: base64 of your existing, trusted `token.pickle`.

`GITHUB_TOKEN` is supplied automatically. The workflow needs `contents: read` and
`actions: write` to read cross-run artifacts and delete eligible media artifacts.
OAuth files, environment files and tokens are never included in artifacts.
Generation receives only the generation API secrets; cleanup receives none of the
application secrets. Upload uses no OpenAI or Pexels calls.

Existing noninteractive uploads use `token.pickle`, including its refresh token.
`client_secret.json` is needed locally for initial authorization, not as an
additional Actions secret when the token is valid/refreshable. If necessary, run:

```powershell
.\venv\Scripts\python.exe -m engine.uploader.youtube_uploader --authorize
```

Using an already authenticated GitHub CLI, set the token secret without printing it:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes((Resolve-Path token.pickle))) | gh secret set YOUTUBE_TOKEN_PICKLE_B64
```

Only use your own trusted pickle: loading a pickle executes its serialized code.
Base64 is transport encoding, not encryption. The secret is decoded to the existing
root token path on the ephemeral upload runner with owner-only permissions.
Existing token refresh behavior is retained. If Google revokes/expires authorization,
re-authorize locally and replace the secret; Actions never launches a browser.

Production privacy is explicitly **private** for the first tests and is recorded
with generation state. Do not change privacy between generating and uploading a
batch. Publishing publicly later is a separate deliberate workflow configuration change.

## Manual end-to-end test

After reviewing and making these files available on the default branch yourself:

1. Open Actions -> **Mystery Shorts production** -> Run workflow. Select
   `generate`; leave `day` blank (today in IST). Slot is ignored for this operation.
2. Confirm success means **three ready slots**, then copy `generation_run_id`
   from the summary. This is the original generation run, also when a later run
   recovered an incomplete batch.
3. Dispatch `upload`, slot `10:00`, with that `generation_run` value and the same
   IST date. Confirm exactly one private YouTube video and its Telegram preview.
4. Repeat separately for `14:00` and `19:00`. Manual dispatch uploads immediately;
   it does not wait until the clock reaches the selected slot.
5. Dispatch an already uploaded slot again: it must fail with `already attempted`,
   without another YouTube upload. This intentional failure tests duplicate protection.
6. Dispatch `cleanup` before 24 hours: uploaded media must remain. Dispatch again
   at least 24 hours after each slot's successful upload: eligible media artifacts
   must be removed while their state/history remains. Cleanup scans recent dates;
   leave the input day blank even when cleaning yesterday's uploads.
7. Only after these checks pass, enable the commented schedules. Keep the existing
   single-video feasibility workflow for isolated generation regression tests.

Generation and upload accept only today's IST date, preventing historical artifact
replays. Complete the three upload tests before midnight IST. There is no automatic
catch-up upload on a later date.

## Artifacts, recovery and duplicate protection

`run_actions.py` is an Actions adapter, not a generator. It calls
`DailyQueue.generate_batch()` and `DailyQueue.upload_slot()` and retains the current
YouTube and Telegram implementations.

Artifacts are explicitly named and identified:

```text
mystery-media-YYYY-MM-DD-slot-10-<run>-<unique>/  -> one original MP4
mystery-media-YYYY-MM-DD-slot-14-<run>-<unique>/  -> one original MP4
mystery-media-YYYY-MM-DD-slot-19-<run>-<unique>/  -> one original MP4
mystery-state-YYYY-MM-DD-<run>-<unique>/         -> state.json
```

Each ready slot has one media artifact. `state.json` includes the three-slot queue,
exact metadata, date, original generation run ID, privacy, generation attempts,
file fingerprints/ownership, explicit media artifact IDs and producer run IDs,
YouTube/Telegram results, successful upload timestamps, cleanup results and idea
memory. State artifacts contain no MP4 and no credentials.

Each operation restores the newest checkpoint **for the intended date**, verifies
its production workflow/default-branch origin, and checks the original generation
run for manual uploads. Media is downloaded by its exact artifact ID and producer
run ID. Missing/expired/wrong-date artifacts fail instead of selecting another batch.
Paths are rebased onto the new runner. File hash/size are verified before restoring
recorded nanosecond mtime; the existing cleanup fingerprint checks remain intact.

All production operations share one non-cancelling concurrency group. This is
essential: local file locks do not synchronize runners. Every queue mutation writes
an immutable checkpoint, and stale writers are refused. GitHub can replace pending
runs in a concurrency group; inspect cancelled pending runs and dispatch again when
needed. Never run a second independent uploader or change the concurrency group.

Before YouTube: persist `uploading` remotely and confirm the artifact exists.
After YouTube: persist `uploaded`, video ID and `successful_upload_at` before Telegram.
An unconfirmed transfer is `uncertain`, not proof that no video exists. Crashes or
checkpoint failures leave a durable claim that blocks another upload. There is no
automatic upload retry across jobs; the existing bounded resumable retries within
one request remain unchanged. A missing remote success receipt after an accepted
upload requires manual reconciliation, not regeneration or a second insert.

Generation saves successful slots separately. It allows at most **two lifetime
attempts per slot**, including interrupted attempts and manual reruns. Rerunning
`generate` resumes only unfinished slots in the same batch; ready slots are never
regenerated. Fewer than three usable slots fails the job and identifies failed slots.

Telegram uses the existing preview sender after successful uploading. No buttons,
listener or approval are used. Telegram failure makes the job report failure but
leaves YouTube successful. Do not retry the upload to fix Telegram.

## Cleanup and retention

The existing guarded cleanup now measures **24 hours from `successful_upload_at`**.
Legacy entries lacking that timestamp are retained for manual review; generation
time is never substituted. Ownership, hash/size/mtime, successful-upload and shared
reference checks remain in place. Pexels clips, music and repository assets are not deleted.

Production restores recent queues and eligible media, runs the existing cleanup,
checkpoints its result, then deletes each eligible media artifact by ID. A subsequent
checkpoint records artifact deletion. Both deletion and cleanup are rerun-safe;
a failed checkpoint prevents remote media deletion. State/history is separate.

MP4 artifacts have a **7-day maximum retention** as a fallback for failures or
inactivity; normal explicit deletion is on the first cleanup run after upload +24h.
GitHub retention expires failed/unuploaded media too: investigate/download it within
seven days if it must be kept. It is not permanent storage. Small state snapshots
have **90-day retention**. Do not manually delete production state/run history or
lower retention during an active batch. Artifact expiry/deletion is not proof that
a video was never uploaded. Public-repository artifacts may be accessible to other
GitHub users; only media and noncredential operational state are stored there.

## Investigating failures

Read the failed step and summary, then download the newest dated state artifact.
Inspect the specific slot's generation status, attempts, upload status, YouTube ID,
Telegram result and cleanup status. Do not restore the original morning snapshot.
Missing credentials/OAuth failures occur before claiming/uploading. Restore the
Secrets and rerun the same slot only if its latest state is still pending.

`uploading`/`uncertain` states require checking YouTube Studio and the workflow logs.
There is intentionally no override that silently clears a claim. Reconcile the
actual remote outcome before a reviewed state repair; do not delete artifacts to
force a new attempt. Successful YouTube + failed Telegram is a notification issue.
If cleanup fails, keep state and retry cleanup; do not delete assets manually.

## Offline verification

```powershell
.\venv\Scripts\python.exe -B -m unittest test_actions test_integration test_scheduler test_cleanup test_resumable_upload test_telegram
```

These tests use fake media, mocked services and isolated temporary directories.
They do not generate real videos, contact YouTube or send Telegram messages.
