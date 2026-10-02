"""Tomorrow preparation using existing queue, artifacts and YouTube integration."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from filelock import FileLock
from config.settings import UPLOAD_TIMES
from engine.scheduling.daily_queue import india_now
from run_actions import Production, require

TZ = ZoneInfo("Asia/Kolkata")
MAX_GENERATION_ATTEMPTS = 5  # One per unfinished slot per invocation, five lifetime attempts.


def target_day(now, explicit=""):
    require(now.utcoffset() is not None, "Preparation clock must be timezone-aware")
    tomorrow = now.astimezone(TZ).date() + timedelta(days=1)
    if explicit:
        from datetime import date
        require(date.fromisoformat(explicit) == tomorrow, "Target must be tomorrow in Asia/Kolkata")
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
        require(ready.utcoffset() is not None and ready < datetime.fromisoformat(publication_time(day, UPLOAD_TIMES[0])),
                "READY receipt must precede first publication")


class Preparation(Production):
    def prepare(self, *, expected_channel, generator=None, uploader=None, verifier=None, notifier=None, now=None):
        clock = now or india_now
        # Cross-run serialization remains the shared Actions group; this also protects local callers.
        with FileLock(str(self.scratch / "preparation.lock"), timeout=0):
            self.queue.checkpoint = None
            self.load(required=False)
            if self.state:
                saved = self.state["queue"]
                require(saved.get("preparation", {}).get("channel_id") == expected_channel,
                        "Existing target batch is legacy or belongs to another channel; do not overwrite")
                if saved.get("batch_status") == "READY":
                    return {"status": "READY", "target_date": str(self.day), "no_op": True}
            def admission():
                instant = clock()
                require(instant.utcoffset() is not None, "Preparation clock must be timezone-aware")
                current = instant.astimezone(TZ)
                require(current.date() == self.day - timedelta(days=1),
                        "Preparation day ended; preserve target batch and do not schedule stale work")
            admission()
            context = {"preparation": {"channel_id": expected_channel, "started_on": str(self.day - timedelta(days=1))},
                       "batch_status": "PREPARING"}
            self.generate(generator, max_attempts=MAX_GENERATION_ATTEMPTS, passes=1, complete=False,
                          context=context, before_generation=admission)
            # Reload durable media identities: checkpoint writes enrich the remote snapshot.
            self.queue.checkpoint = None
            self.load()
            self.queue.checkpoint = self.checkpoint
            if uploader is None:
                from engine.uploader.youtube_uploader import upload_prepared_video
                uploader = upload_prepared_video
            if verifier is None:
                from engine.uploader.youtube_uploader import verify_prepared_video
                verifier = verify_prepared_video
            if notifier is None:
                from engine.approval.telegram_bot import notify_generated_video
                notifier = notify_generated_video
            problems = []
            for item in self.queue.read(self.day)["videos"]:
                slot = item["slot"]
                if verified_item(item, self.day, expected_channel):
                    continue
                admission()
                if item["generation_status"] != "ready":
                    problems.append({"slot": slot, "reason": "generation_incomplete"})
                    continue
                youtube = item.get("youtube") or {}
                if not youtube.get("video_id"):
                    if item["upload_status"] != "pending":
                        problems.append({"slot": slot, "reason": "uncertain_upload_manual_reconciliation"})
                        continue
                    self.restore_media([slot])
                    self.queue._set(self.day, slot, {"upload_status": "uploading"})
                    try:
                        youtube = uploader(item["video_path"], item["metadata"], publication_time(self.day, slot))
                        require(isinstance(youtube, dict) and youtube.get("success") is True and youtube.get("video_id"),
                                "Unconfirmed prepared upload")
                    except Exception as exc:
                        self.queue._set(self.day, slot, {"upload_status": "uncertain",
                            "youtube": {"success": False, "status": "uncertain", "error_type": type(exc).__name__}})
                        problems.append({"slot": slot, "reason": "uncertain_upload_manual_reconciliation"})
                        continue
                    # ID is durable BEFORE readback, repairs or Telegram side effects.
                    self.queue._set(self.day, slot, {"upload_status": "uploaded", "youtube": youtube,
                                                   "successful_upload_at": clock().isoformat()})
                try:
                    admission()
                    evidence = verifier(youtube["video_id"], expected_channel, publication_time(self.day, slot))
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
                    problems.append({"slot": slot, "reason": "processing_or_schedule_unverified"})
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
                    notification = notifier({"batch_notice": True, "metadata": {"Title": "Tomorrow batch READY"},
                        "upload": {"success": True, "privacy_status": "private", "video_url": ""},
                        "preparation": {"target_date": str(self.day), "batch_status": "READY"}})
                except Exception as exc:
                    notification = {"success": False, "error_type": type(exc).__name__}
                self.queue.update(self.day, lambda saved: {**saved, "batch_telegram": notification})
                return {"status": "READY", "target_date": str(self.day), "no_op": False}
            try:
                notification = notifier({"batch_notice": True, "metadata": {"Title": "Preparation recovery"},
                    "upload": {"success": False, "error": "Preparation incomplete; saved work retained"},
                    "preparation": {"target_date": str(self.day), "batch_status": "PREPARING", "problems": problems}})
            except Exception as exc:
                notification = {"success": False, "error_type": type(exc).__name__}
            self.queue.update(self.day, lambda saved: {**saved, "batch_telegram": notification})
            return {"status": "PREPARING", "target_date": str(self.day), "problems": problems}
