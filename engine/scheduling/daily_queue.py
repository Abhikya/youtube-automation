"""Daily JSON queue with atomic updates and local process locks."""
import json
import logging
import os
from pathlib import Path
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout
from config.settings import DATA_DIR, SCHEDULER_TIMEZONE, UPLOAD_TIMES

from config.channels import MINUTE_MYSTERIES, RuntimePaths, channel_identity, validate_channel_identity, slot_owner, valid_slot_owner, validate_channel_scope

logger = logging.getLogger(__name__)


def generation_error(exc):
    code = str(getattr(exc, "code", "") or "").lower()
    detail = str(exc).lower()
    if any(term in code or term in detail for term in
           ("insufficient_quota", "credit_balance_exhausted", "insufficient credits")):
        return "OpenAI generation failed: insufficient API quota; check billing/credits."
    return f"{type(exc).__name__}: {exc}"


def validate_metadata(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("Generated metadata must be an object")
    for key in ("Title", "Description"):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError(f"Generated {key} is missing")
    tags = metadata.get("Hashtags")
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("Generated Hashtags must be a list of strings")


def india_now():
    return datetime.now(ZoneInfo(SCHEDULER_TIMEZONE))


def generate_only(*, channel=MINUTE_MYSTERIES, runtime_paths=None):
    from engine.pipeline.video_pipeline import generate_video
    from config.settings import BASE_DIR
    return generate_video(publish=False, runtime_paths=runtime_paths or RuntimePaths(channel, BASE_DIR))


def upload_existing(path, metadata):
    from engine.uploader.youtube_uploader import upload_generated_video
    return upload_generated_video(path, metadata)


def notify_existing(result):
    from engine.approval.telegram_bot import notify_generated_video
    return notify_generated_video(result)


class CheckpointError(RuntimeError):
    """Remote state was not confirmed; stop before any further side effects."""


class DailyQueue:
    def __init__(self, data_dir=DATA_DIR, *, checkpoint=None, channel=MINUTE_MYSTERIES):
        validate_channel_scope(channel)
        self.channel = channel
        self.checkpoint = checkpoint
        self.data_dir = Path(data_dir) if channel == MINUTE_MYSTERIES else Path(data_dir) / channel.channel_key
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def path(self, day):
        return self.data_dir / f"daily_queue_{day.isoformat()}.json"

    def _read(self, day):
        path = self.path(day)
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("date") != day.isoformat() or data.get("timezone") != SCHEDULER_TIMEZONE:
            raise ValueError("Queue date/timezone mismatch")
        validate_channel_identity(data, self.channel, allow_legacy=self.channel == MINUTE_MYSTERIES)
        videos = data.get("videos")
        if not isinstance(videos, list) or any(not isinstance(item, dict) for item in videos):
            raise ValueError("Queue videos must be a list of objects")
        slots = [item.get("slot") for item in videos]
        if any(slot not in UPLOAD_TIMES for slot in slots) or len(slots) != len(set(slots)):
            raise ValueError("Queue contains invalid or duplicate slots")
        for item in videos:
            if "channel_key" in item and item["channel_key"] != self.channel.channel_key:
                raise ValueError("Queue item channel mismatch")
            if "upload_owner" in item and not valid_slot_owner(item["upload_owner"], self.channel, day, item["slot"]):
                raise ValueError("Upload claim ownership mismatch")
        return data

    def read(self, day):
        with FileLock(str(self.path(day)) + ".lock", timeout=10):
            return self._read(day)

    def _write(self, day, data):
        validate_channel_identity(data, self.channel, allow_legacy=self.channel == MINUTE_MYSTERIES)
        data.update(channel_identity(self.channel))
        for item in data["videos"]:
            if item.get("channel_key", self.channel.channel_key) != self.channel.channel_key:
                raise ValueError("Queue item channel mismatch")
            if "upload_owner" in item and not valid_slot_owner(item["upload_owner"], self.channel, day, item["slot"]):
                raise ValueError("Upload claim ownership mismatch")
            item["channel_key"] = self.channel.channel_key
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.data_dir,
                                             prefix=".queue-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path(day))
            logger.info("Queue saved: %s", self.path(day))
            if self.checkpoint is not None:
                try:
                    self.checkpoint(day, data)
                except Exception as exc:
                    raise CheckpointError("Durable queue checkpoint failed; manual review may be required") from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def update(self, day, change):
        with FileLock(str(self.path(day)) + ".lock", timeout=10):
            data = self._read(day)
            changed = change(data)
            if changed is not None:
                self._write(day, changed)
            return changed

    def generate_batch(self, day=None, generator=None, *, slots=None, resume=False, max_attempts=2, context=None, before_generation=None, generation_allowed=None):
        day = day or india_now().date()
        if generator is None:
            from functools import partial
            generator = partial(generate_only, channel=self.channel)
        slots = list(UPLOAD_TIMES if slots is None else slots)
        if not slots or any(slot not in UPLOAD_TIMES for slot in slots) or len(set(slots)) != len(slots):
            raise ValueError("Generation slots must be unique existing publishing slots")
        try:
            # Protect batches across threads AND independent scheduler processes.
            with FileLock(str(self.data_dir / "daily_generation.lock"), timeout=0):
                existing = self.read(day)
                if existing is not None and not resume:
                    logger.info("Batch already recorded for %s; skipping", day)
                    return existing
                if existing is not None and [v["slot"] for v in existing["videos"]] != slots:
                    raise ValueError("Existing batch slots do not match requested slots")
                data = {"date": day.isoformat(), "timezone": SCHEDULER_TIMEZONE, "videos": [
                    {"slot": slot, "generation_status": "pending", "upload_status": "pending",
                     "video_path": None, "metadata": None, "idea": None, "script": None,
                     "youtube": None, "telegram": None, "generation_completed_at": None,
                     "generated_files": [], "cleanup": {"status": "pending"}} for slot in slots
                ], **channel_identity(self.channel), **(context or {})}
                if existing is None:
                    self.update(day, lambda existing: data if existing is None else None)
                logger.info("Daily batch started for %s", day)
                for index, slot in enumerate(slots, 1):
                    current = next(v for v in self.read(day)["videos"] if v["slot"] == slot)
                    if current["generation_status"] == "ready" or current["upload_status"] != "pending":
                        continue
                    attempts = current.get("generation_attempts", 0)
                    if attempts >= max_attempts:
                        continue
                    if generation_allowed is not None and not generation_allowed(slot):
                        logger.info("%s: target publication time elapsed; preserve slot", slot)
                        continue
                    if before_generation is not None:
                        before_generation()
                    history = list(current.get("generation_attempt_history", []))
                    history.append({"attempt": attempts + 1, "started_at": india_now().isoformat() if context else None,
                                    "previous_status": current["generation_status"],
                                    "previous_error": current.get("generation_error")})
                    self._set(day, slot, {"generation_status": "generating", "generation_attempts": attempts + 1,
                                          "generation_attempt_history": history})
                    try:
                        result = generator()
                        completed_at = india_now().isoformat()
                        if not Path(result["video_path"]).is_file():
                            raise FileNotFoundError(result["video_path"])
                        metadata = result["metadata"]
                        validate_metadata(metadata)
                        fields = {key: result.get(key) for key in ("topic", "idea", "script", "video_path", "metadata")}
                        fields.update(generation_status="ready", generated_at=completed_at,
                                      generation_completed_at=completed_at)
                        # Persist completion before fingerprinting; failures here must not
                        # turn a successfully generated video into a failed generation.
                        if self.checkpoint is None:
                            self._set(day, slot, fields)
                        try:
                            from engine.scheduling.cleanup import record_generated_file
                            fields["generated_files"] = [record_generated_file(result["video_path"], day, slot, channel=self.channel)]
                        except Exception as exc:
                            logger.exception("Video %s: cleanup ownership could not be recorded", index)
                            fields["cleanup"] = {"status": "rejected", "error": str(exc)}
                    except CheckpointError:
                        raise
                    except Exception as exc:
                        error = generation_error(exc)
                        logger.error("Video %s (%s) generation failed: %s", index, slot, error)
                        fields = {"generation_status": "generation_failed", "generation_error": error}
                    self._set(day, slot, fields)
                    logger.info("Video %s (%s): %s", index, slot, fields["generation_status"])
                return self.read(day)
        except CheckpointError:
            raise
        except Timeout:
            logger.warning("Generation already running; skipping batch")
            return None
        except Exception as exc:
            logger.error("Batch could not complete safely: %s; future jobs remain scheduled", type(exc).__name__)
            return None

    def _set(self, day, slot, fields):
        def change(data):
            item = next(item for item in data["videos"] if item["slot"] == slot)
            item.update(fields)
            return data
        return self.update(day, change)

    def upload_slot(self, slot, day=None, uploader=None, notifier=None, *, retry_failed=False):
        try:
            return self._upload_slot(slot, day, uploader, notifier, retry_failed=retry_failed)
        except Exception as exc:
            logger.error("%s: upload job skipped/stopped safely (%s); inspect queue before any retry",
                         slot, type(exc).__name__)
            return {"status": "skipped", "reason": "queue_or_state_error", "error_type": type(exc).__name__}

    def _upload_slot(self, slot, day=None, uploader=None, notifier=None, *, retry_failed=False):
        if slot not in UPLOAD_TIMES:
            raise ValueError(f"Unknown publishing slot: {slot}")
        if self.channel != MINUTE_MYSTERIES and uploader is None:
            raise ValueError("Non-default legacy upload requires an explicitly bound uploader")
        day = day or india_now().date()
        uploader = uploader or upload_existing
        notifier = notifier or notify_existing
        claimed = []
        def claim(data):
            if data is None:
                logger.warning("%s: no queue for %s", slot, day)
                return None
            item = next((item for item in data["videos"] if item["slot"] == slot), None)
            if item is None:
                logger.warning("%s: missing slot", slot)
                return None
            youtube = item.get("youtube") or {}
            if not isinstance(youtube, dict):
                logger.error("%s: malformed YouTube state; skipping", slot)
                return None
            if item.get("upload_status") in {"uploading", "uncertain"}:
                logger.warning("%s: uploading/uncertain - manual review required; do not retry automatically", slot)
                return None
            manual_retry = (retry_failed and item.get("upload_status") == "failed"
                            and youtube.get("success") is False and youtube.get("status") == "failed"
                            and not youtube.get("video_id"))
            if retry_failed and not manual_retry:
                logger.warning("%s: explicit retry refused; only a recorded failed upload without a video ID can be retried", slot)
                return None
            if youtube.get("success") or youtube.get("video_id") or (item.get("upload_status") != "pending" and not manual_retry):
                logger.info("%s: already uploaded/attempted (%s); skipping", slot, item.get("upload_status"))
                return None
            if item.get("generation_status") != "ready":
                logger.warning("%s: video not ready; skipping", slot)
                return None
            try:
                validate_metadata(item.get("metadata"))
                if not isinstance(item.get("video_path"), str) or not Path(item["video_path"]).is_absolute():
                    raise ValueError("Missing or nonabsolute video path")
            except ValueError as exc:
                logger.error("%s: invalid queue entry: %s; skipping", slot, exc)
                return None
            if not Path(item["video_path"]).is_file():
                item["upload_status"] = "missing_file"
                logger.error("%s: local video missing; skipping", slot)
                return data
            if manual_retry:
                item.setdefault("manual_retry_history", []).append({
                    "requested_at": india_now().isoformat(), "previous_youtube": youtube,
                    "previous_telegram": item.get("telegram"),
                })
                # New explicit attempt; preserve the prior failure in history.
                item["youtube"] = None
                item["telegram"] = None
                logger.info("%s: explicit manual retry claimed for the existing rendered video", slot)
            item["upload_owner"] = slot_owner(self.channel, day, slot)
            item["upload_status"] = "uploading"
            claimed.append(dict(item))
            return data
        # Persist the claim before contacting YouTube. Interrupted claims are never retried automatically.
        self.update(day, claim)
        if not claimed:
            return None
        item = claimed[0]
        logger.info("%s: YouTube upload started", slot)
        try:
            youtube = uploader(item["video_path"], item["metadata"])
            if (not isinstance(youtube, dict) or type(youtube.get("success")) is not bool
                    or (youtube["success"] and not youtube.get("video_id"))):
                youtube = {"success": False, "status": "uncertain", "error": "Upload response is incomplete; manual review required."}
        except Exception as exc:
            logger.exception("%s: YouTube upload failed", slot)
            youtube = {"success": False, "status": "failed", "error": str(exc), "error_type": type(exc).__name__}
        upload_status = "uploaded" if youtube["success"] else ("uncertain" if youtube.get("status") == "uncertain" else "failed")
        try:
            fields = {"youtube": youtube, "upload_status": upload_status}
            if youtube["success"]:
                fields["successful_upload_at"] = india_now().isoformat()
            self._set(day, slot, fields)
        except Exception as exc:
            logger.error("%s: YouTube outcome could not be saved; persisted uploading state requires manual review", slot)
            return {"upload": youtube, "telegram": {"success": False, "status": "not_attempted"},
                    "state": {"success": False, "error_type": type(exc).__name__}}

        logger.info("%s: YouTube success=%s", slot, youtube["success"])
        # Save YouTube outcome BEFORE Telegram. Notification failures cannot cause re-upload.
        try:
            telegram = notifier({**item, "upload": youtube})
        except Exception as exc:
            logger.exception("%s: Telegram notification failed", slot)
            telegram = {"success": False, "status": "failed", "error": str(exc), "error_type": type(exc).__name__}
        try:
            self._set(day, slot, {"telegram": telegram})
        except Exception as exc:
            logger.error("%s: Telegram outcome could not be saved; YouTube outcome remains saved", slot)
            return {"upload": youtube, "telegram": telegram,
                    "state": {"success": False, "error_type": type(exc).__name__}}

        logger.info("%s: completed; Telegram success=%s", slot, telegram["success"])
        return {"upload": youtube, "telegram": telegram}


    def cleanup_recent(self, now=None):
        """Isolate hourly cleanup failures from generation and publishing jobs."""
        try:
            from engine.scheduling.cleanup import cleanup_recent
            return cleanup_recent(self, now=now)
        except Exception as exc:
            logger.exception("Hourly cleanup failed; publishing scheduler remains active")
            return {"status": "failed", "error": str(exc)}
