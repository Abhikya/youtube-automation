"""Tomorrow preparation using existing queue, artifacts and YouTube integration."""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from filelock import FileLock
from config.settings import UPLOAD_TIMES
from config.channels import slot_owner
from engine.scheduling.daily_queue import india_now
from run_actions import Production, StaleStateConflict, require
from engine.scheduling.daily_queue import CheckpointError
import copy
import json

TZ = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)
MAX_GENERATION_ATTEMPTS = 5  # One per unfinished slot per invocation, five lifetime attempts.


def target_day(now, explicit=""):
    require(now.utcoffset() is not None, "Preparation clock must be timezone-aware")
    tomorrow = now.astimezone(TZ).date() + timedelta(days=1)
    if explicit:
        from datetime import date
        requested = date.fromisoformat(explicit)
        require(requested in (tomorrow - timedelta(days=1), tomorrow),
                "Target must be tomorrow or an existing current-day batch in Asia/Kolkata")
        return requested
    return tomorrow


def publication_time(day, slot):
    require(slot in UPLOAD_TIMES, "Unknown publication slot")
    hour, minute = map(int, slot.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).astimezone(timezone.utc).isoformat()


def verified_item(item, day, channel):
    evidence = item.get("preparation_verification") or {}
    youtube = item.get("youtube") or {}
    try:
        actual = datetime.fromisoformat(evidence.get("publish_at", "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return (item.get("generation_status") == "ready" and item.get("upload_status") == "uploaded"
            and youtube.get("success") is True and bool(youtube.get("video_id"))
            and evidence.get("verified") is True and evidence.get("video_id") == youtube["video_id"]
            and evidence.get("channel_id") == channel and evidence.get("privacy_status") == "private"
            and evidence.get("processing_status") == "succeeded"
            and actual == datetime.fromisoformat(publication_time(day, item["slot"])))


def validate_preparation(queue):
    from datetime import date
    context = queue["preparation"]
    require(isinstance(context, dict) and context.get("channel_id"), "Missing preparation channel identity")
    day = date.fromisoformat(queue["date"])
    require(date.fromisoformat(context["started_on"]) == day - timedelta(days=1), "Invalid preparation target ownership")
    require(queue.get("batch_status", "PREPARING") in ("PREPARING", "READY"), "Invalid batch status")
    if queue.get("batch_status") == "READY":
        require(all(verified_item(v, day, context["channel_id"]) for v in queue["videos"]),
                "READY requires three verified scheduled videos")
        ids = [v["youtube"]["video_id"] for v in queue["videos"]]
        require(len(set(ids)) == 3, "READY contains duplicate YouTube IDs")
        ready = datetime.fromisoformat(queue["ready_at"])
        require(ready.utcoffset() is not None and ready < datetime.fromisoformat(publication_time(day, UPLOAD_TIMES[-1])),
                "READY receipt must precede final publication")


class Preparation(Production):
    def prepare(self, **kwargs):
        # A rejected stale snapshot never authorizes a side effect. Restart from the head.
        for attempt in range(3):
            try:
                return self._prepare_once(**kwargs)
            except CheckpointError as exc:
                conflict = exc.__cause__
                if not isinstance(conflict, StaleStateConflict) or attempt == 2:
                    raise
                with FileLock(str(self.scratch / "preparation.lock"), timeout=0):
                    self.queue.checkpoint = None
                    local_memory = json.loads(self.memory.read_text()) if self.memory.exists() else {}
                    self.media = {}
                    self.load()
                    self.poisoned = False
                    newest = self.queue.read(self.day)
                    base = conflict.base_state
                    proposed = conflict.proposed_queue
                    def comparable(item):
                        value = copy.deepcopy(item)
                        value.pop("video_path", None)
                        for record in value.get("generated_files", []):
                            record.pop("path", None)
                        return value
                    changed = False
                    for candidate in proposed["videos"]:
                        saved = next(v for v in newest["videos"] if v["slot"] == candidate["slot"])
                        old = next((v for v in (base or {}).get("queue", {}).get("videos", [])
                                    if v["slot"] == candidate["slot"]), None)
                        vid = (candidate.get("youtube") or {}).get("video_id")
                        saved_id = (saved.get("youtube") or {}).get("video_id")
                        require(not vid or not saved_id or vid == saved_id,
                                "Conflicting confirmed IDs; retain checkpoints for manual reconciliation")
                        old_id = (old.get("youtube") or {}).get("video_id") if old else None
                        require(not vid or old_id != vid or saved_id == vid,
                                "Newer checkpoint removed a confirmed ID; manual reconciliation required")
                        important = ((candidate["generation_status"] == "ready" and
                                      (old is None or old["generation_status"] != "ready")) or
                                     (vid and vid != old_id) or
                                     (candidate["upload_status"] == "uncertain" and
                                      (old is None or old["upload_status"] != "uncertain")))
                        if not important or comparable(candidate) == comparable(saved):
                            continue
                        if old is not None and comparable(saved) == comparable(old):
                            require(self.state["generation_run_id"] == base["generation_run_id"],
                                    "Checkpoint generation ownership changed")
                            require(newest.get("preparation") == base["queue"].get("preparation"),
                                    "Checkpoint preparation ownership changed")
                            saved.update(copy.deepcopy(candidate))
                            changed = True
                        else:
                            require((not vid or saved_id == vid) and
                                    (candidate["generation_status"] != "ready" or saved["generation_status"] == "ready") and
                                    (candidate["upload_status"] != "uncertain" or saved["upload_status"] in ("uploading", "uncertain", "uploaded")),
                                    "Completed local work conflicts with newer state; manual reconciliation required")
                    if changed:
                        memory = self.state["idea_memory"]
                        for category, ideas in local_memory.items():
                            memory[category] = list(dict.fromkeys(memory.get(category, []) + ideas))
                        self.memory.write_text(json.dumps(memory), encoding="utf-8")
                        self.queue.checkpoint = self.checkpoint
                        self.queue.update(self.day, lambda _: newest)
                    logger.warning("Reloaded newer checkpoint for target %s; resuming without overwriting its slots", self.day)
        raise AssertionError("Unreachable checkpoint retry state")

    def _prepare_once(self, *, expected_channel, generator=None, uploader=None, verifier=None, notifier=None, now=None, authenticate=None, before_render=None, allow_current_day=False, started_at=None, credential_context=None):
        from config.channels import MINUTE_MYSTERIES
        require(self.channel == MINUTE_MYSTERIES or credential_context is not None, "Non-default preparation requires a credential context")
        if credential_context is not None:
            require(credential_context.channel == self.channel, "Preparation credential/state channel mismatch")
            credential_context.require_channel(expected_channel)
        clock = now or india_now
        initial = started_at or clock()
        require(initial.utcoffset() is not None, "Preparation clock must be timezone-aware")
        execution_day = initial.astimezone(TZ).date()
        # Cross-run serialization remains the shared Actions group; this also protects local callers.
        with FileLock(str(self.scratch / "preparation.lock"), timeout=0):
            self.queue.checkpoint = None
            self.load(required=False)
            require(execution_day in (self.day - timedelta(days=1), self.day),
                    "Past and arbitrary future target batches are blocked")
            require(execution_day != self.day or (allow_current_day and self.state is not None),
                    "Current-day recovery requires an explicit day and an existing batch")
            logger.info("execution_date=%s target_publication_date=%s batch=%s action=restore_target_batch",
                        execution_day, self.day, self.queue.path(self.day).name)
            if self.state:
                saved = self.state["queue"]
                require(saved.get("preparation", {}).get("channel_id") == expected_channel,
                        "Existing target batch is legacy or belongs to another channel; do not overwrite")
                if saved.get("batch_status") == "READY":
                    for item in saved["videos"]:
                        self._log(item, clock, "skip_verified_ready_batch")
                    return self._result(clock, expected_channel, no_op=True)
            def admission():
                instant = clock()
                require(instant.utcoffset() is not None, "Preparation clock must be timezone-aware")
                current = instant.astimezone(TZ)
                require(current.date() == self.day - timedelta(days=1) or
                        (current.date() == self.day and self.state is not None and
                         (allow_current_day or execution_day == self.day - timedelta(days=1))),
                        "Only tomorrow preparation or explicit existing current-day recovery is allowed")
            admission()
            context = {"preparation": {"channel_id": expected_channel, "started_on": str(self.day - timedelta(days=1))},
                       "batch_status": "PREPARING"}
            saved = self.state["queue"] if self.state else None
            if saved is None or any(v["generation_status"] != "ready" and v["upload_status"] == "pending"
                                    and v.get("generation_attempts", 0) < MAX_GENERATION_ATTEMPTS
                                    and self._future(v["slot"], clock) for v in saved["videos"]):
                def render_admission():
                    admission()
                    if before_render is not None:
                        before_render()
                self.generate(generator, max_attempts=MAX_GENERATION_ATTEMPTS, passes=1, complete=False,
                              context=context, before_generation=render_admission,
                              generation_allowed=lambda slot: self._future(slot, clock))
                # Reload durable media identities enriched by the artifact checkpoint.
                self.queue.checkpoint = None
                self.load()
            self.queue.checkpoint = self.checkpoint
            if uploader is None:
                from engine.uploader.youtube_uploader import upload_prepared_video
                if credential_context is not None:
                    from functools import partial
                    uploader = partial(upload_prepared_video, credential_context=credential_context)
                else:
                    uploader = upload_prepared_video
            if verifier is None:
                from engine.uploader.youtube_uploader import verify_prepared_video
                if credential_context is not None:
                    from functools import partial
                    verifier = partial(verify_prepared_video, credential_context=credential_context)
                else:
                    verifier = verify_prepared_video
            if notifier is None:
                from engine.approval.telegram_bot import notify_generated_video
                notifier = notify_generated_video
            problems = []
            authentication_error = None
            candidates = [v for v in self.queue.read(self.day)["videos"]
                          if not verified_item(v, self.day, expected_channel)
                          and v["generation_status"] == "ready" and self._future(v["slot"], clock)
                          and ((v.get("youtube") or {}).get("video_id") or v["upload_status"] == "pending")]
            if candidates and authenticate is not None:
                try:
                    authenticate()
                except Exception as exc:
                    authentication_error = type(exc).__name__
            for item in self.queue.read(self.day)["videos"]:
                slot = item["slot"]
                if verified_item(item, self.day, expected_channel):
                    self._log(item, clock, "skip_verified")
                    continue
                admission()
                if not self._future(slot, clock):
                    self._log(item, clock, "block_elapsed_publish_at")
                    problems.append({"slot": slot, "reason": "publish_at_not_future"})
                    continue
                if item["generation_status"] != "ready":
                    self._log(item, clock, "generation_incomplete")
                    problems.append({"slot": slot, "reason": "generation_incomplete"})
                    continue
                youtube = item.get("youtube") or {}
                if not youtube.get("video_id"):
                    if item["upload_status"] != "pending":
                        self._log(item, clock, "block_uncertain_manual_reconciliation")
                        problems.append({"slot": slot, "reason": "uncertain_upload_manual_reconciliation"})
                        continue
                    if authentication_error:
                        self._log(item, clock, "resume_upload_authentication_blocked")
                        problems.append({"slot": slot, "reason": "authentication_failed_upload_pending",
                                         "error_type": authentication_error})
                        continue
                    self._log(item, clock, "restore_and_resume_upload")
                    self.restore_media([slot])
                    admission()
                    if not self._future(slot, clock):
                        problems.append({"slot": slot, "reason": "publish_at_not_future"})
                        continue
                    self.queue._set(self.day, slot, {"upload_status": "uploading", "upload_owner": slot_owner(self.channel, self.day, slot)})
                    try:
                        youtube = uploader(item["video_path"], item["metadata"], publication_time(self.day, slot))
                        require(isinstance(youtube, dict) and youtube.get("success") is True and youtube.get("video_id"),
                                "Unconfirmed prepared upload")
                    except Exception as exc:
                        from engine.uploader.youtube_uploader import PublicationExpired, UploadNotStarted
                        if isinstance(exc, UploadNotStarted):
                            # Proven pre-insertion failure only; retain its audit receipt.
                            history = list(item.get("upload_not_started_history", []))
                            history.append({"at": clock().isoformat(), "error_type": type(exc).__name__})
                            self.queue._set(self.day, slot, {"upload_status": "pending",
                                "upload_not_started_history": history,
                                "youtube": {"success": False, "status": "not_started", "error_type": type(exc).__name__}})
                            problems.append({"slot": slot, "reason": "publish_at_not_future"
                                             if isinstance(exc, PublicationExpired) else "upload_not_started_retryable"})
                            continue
                        self.queue._set(self.day, slot, {"upload_status": "uncertain",
                            "youtube": {"success": False, "status": "uncertain", "error_type": type(exc).__name__}})
                        problems.append({"slot": slot, "reason": "uncertain_upload_manual_reconciliation"})
                        continue
                    # ID is durable BEFORE readback, repairs or Telegram side effects.
                    self.queue._set(self.day, slot, {"upload_status": "uploaded", "youtube": youtube,
                                                   "successful_upload_at": clock().isoformat()})
                if authentication_error:
                    self._log(item, clock, "verify_existing_id_authentication_blocked")
                    problems.append({"slot": slot, "reason": "authentication_failed_verification_pending",
                                     "error_type": authentication_error})
                    continue
                self._log(item, clock, "verify_existing_id")
                try:
                    admission()
                    if not self._future(slot, clock):
                        problems.append({"slot": slot, "reason": "publish_at_not_future"})
                        continue
                    evidence = verifier(youtube["video_id"], expected_channel, publication_time(self.day, slot))
                    if isinstance(evidence, dict) and evidence.get("processing_status") == "processing":
                        evidence = {**evidence, "verification_status": "pending"}
                    self.queue._set(self.day, slot, {"preparation_verification": evidence})
                except Exception as exc:
                    if self.poisoned:
                        raise
                    self.queue._set(self.day, slot, {"preparation_verification": {"verified": False,
                                                    "error_type": type(exc).__name__}})
                    problems.append({"slot": slot, "reason": "verification_failed"})
                    continue
                current = next(v for v in self.queue.read(self.day)["videos"] if v["slot"] == slot)
                if not verified_item(current, self.day, expected_channel):
                    evidence = current.get("preparation_verification") or {}
                    scheduled = {**current, "preparation_verification": {**evidence,
                                 "verified": True, "processing_status": "succeeded"}}
                    pending = evidence.get("processing_status") == "processing" and verified_item(scheduled, self.day, expected_channel)
                    problems.append({"slot": slot, "reason": "processing_pending" if pending else "processing_or_schedule_unverified"})
                    continue
                try:
                    if not Path(current["video_path"]).is_file():
                        self.restore_media([slot])
                    notification = notifier({**current, "upload": youtube, "preparation": {
                        "target_date": str(self.day), "publish_at": publication_time(self.day, slot), "verified": True}})
                except Exception as exc:
                    notification = {"success": False, "error_type": type(exc).__name__}
                self.queue._set(self.day, slot, {"telegram": notification})
            data = self.queue.read(self.day)
            if all(verified_item(v, self.day, expected_channel) for v in data["videos"]):
                admission()
                self.queue.update(self.day, lambda saved: {**saved, "batch_status": "READY", "ready_at": clock().isoformat()})
                try:
                    notification = notifier({"batch_notice": True, "metadata": {"Title": "Target batch READY"},
                        "upload": {"success": True, "privacy_status": "private", "video_url": ""},
                        "preparation": {"target_date": str(self.day), "batch_status": "READY"}})
                except Exception as exc:
                    notification = {"success": False, "error_type": type(exc).__name__}
                self.queue.update(self.day, lambda saved: {**saved, "batch_telegram": notification})
                return self._result(clock, expected_channel)
            try:
                notification = notifier({"batch_notice": True, "metadata": {"Title": "Preparation recovery"},
                    "upload": {"success": False, "error": "Preparation incomplete; saved work retained"},
                    "preparation": {"target_date": str(self.day), "batch_status": "PREPARING", "problems": problems,
                        "generation_status": self._result(clock, expected_channel)["generation_status"]}})
            except Exception as exc:
                notification = {"success": False, "error_type": type(exc).__name__}
            self.queue.update(self.day, lambda saved: {**saved, "batch_telegram": notification})
            return self._result(clock, expected_channel, problems=problems)

    def reconcile_not_uploaded(self, slot, *, expected_channel, now=None, credential_context=None):
        """Explicit operator attestation only; never infer absence from a missing ID."""
        if credential_context is not None:
            require(credential_context.channel == self.channel, "Reconciliation credential/state channel mismatch")
            credential_context.require_channel(expected_channel)
        clock = now or india_now
        with FileLock(str(self.scratch / "preparation.lock"), timeout=0):
            self.queue.checkpoint = None
            self.load()
            instant = clock()
            require(instant.utcoffset() is not None, "Reconciliation clock must be timezone-aware")
            require(self.day in (instant.astimezone(TZ).date(), instant.astimezone(TZ).date() + timedelta(days=1)),
                    "Reconciliation requires today's or tomorrow's target")
            require(slot in UPLOAD_TIMES and self._future(slot, clock), "Reconciliation slot must still be future")
            data = self.queue.read(self.day)
            require(data.get("preparation", {}).get("channel_id") == expected_channel, "Reconciliation channel mismatch")
            item = next(v for v in data["videos"] if v["slot"] == slot)
            youtube = item.get("youtube") or {}
            require(not youtube.get("video_id") and not youtube.get("success"), "Never reconcile an ID-bearing or confirmed upload for retry")
            require(item["generation_status"] == "ready" and item["upload_status"] in ("uploading", "uncertain"),
                    "Reconciliation only releases an unresolved upload of an existing render")
            # Confirm the original durable render before releasing the protected claim.
            self.restore_media([slot])
            history = list(item.get("upload_reconciliation_history", []))
            history.append({"confirmed_at": instant.isoformat(), "source": "manual_youtube_studio_confirmation",
                            "confirmation": "not_uploaded", "target_date": str(self.day), "slot": slot,
                            "channel_id": expected_channel, "previous_upload_status": item["upload_status"],
                            "previous_youtube": youtube})
            self.queue.checkpoint = self.checkpoint
            self.queue._set(self.day, slot, {"upload_status": "pending", "youtube": None,
                                           "upload_reconciliation_history": history})
            return {"slot": slot, "target_date": str(self.day), "status": "retryable"}

    def recoverable_slots(self, *, expected_channel, now):
        """Inspect a loaded target batch without claiming, generating, or uploading."""
        if not self.state or self.state["queue"].get("batch_status") == "READY":
            return []
        data = self.state["queue"]
        if data.get("preparation", {}).get("channel_id") != expected_channel:
            return []
        return [v["slot"] for v in data["videos"]
                if not verified_item(v, self.day, expected_channel) and self._future(v["slot"], lambda: now)
                and ((v.get("youtube") or {}).get("video_id") or
                     (v["upload_status"] == "pending" and
                      (v["generation_status"] == "ready" or
                       v.get("generation_attempts", 0) < MAX_GENERATION_ATTEMPTS)))]

    def _future(self, slot, clock):
        instant = clock()
        require(instant.utcoffset() is not None, "Preparation clock must be timezone-aware")
        return datetime.fromisoformat(publication_time(self.day, slot)) > instant

    def _log(self, item, clock, action):
        # Log the durable outcome, rather than an earlier in-memory slot snapshot.
        item = next(v for v in self.queue.read(self.day)["videos"] if v["slot"] == item["slot"])
        logger.info("execution_date=%s target_publication_date=%s batch=%s slot=%s "
                    "generation_status=%s upload_status=%s youtube_id=%s publish_at=%s action=%s",
                    clock().astimezone(TZ).date(), self.day, self.queue.path(self.day).name,
                    item["slot"], item["generation_status"], item["upload_status"],
                    (item.get("youtube") or {}).get("video_id"), publication_time(self.day, item["slot"]), action)

    def _result(self, clock, channel, *, no_op=False, problems=None):
        data = self.queue.read(self.day)
        return {"status": data.get("batch_status", "PREPARING"), "target_date": str(self.day),
                "execution_date": str(clock().astimezone(TZ).date()), "batch": self.queue.path(self.day).name,
                "no_op": no_op, "recoverable": data.get("batch_status") != "READY",
                "generation_status": "COMPLETE" if all(v["generation_status"] == "ready" for v in data["videos"]) else "INCOMPLETE",
                "upload_status": "COMPLETE" if all(v["upload_status"] == "uploaded" and
                    (v.get("youtube") or {}).get("success") is True and (v.get("youtube") or {}).get("video_id")
                    for v in data["videos"]) else "INCOMPLETE",
                "verification_pending": bool(problems) and all(p["reason"] == "processing_pending" for p in problems),
                "problems": problems or [],
                "slots": [{"slot": v["slot"], "generation_status": v["generation_status"],
                           "upload_status": v["upload_status"], "youtube_video_id": (v.get("youtube") or {}).get("video_id"),
                           "publish_at": publication_time(self.day, v["slot"]),
                           "verified": verified_item(v, self.day, channel)} for v in data["videos"]]}
