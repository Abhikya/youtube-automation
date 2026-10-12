"""Conservative cleanup of fingerprinted final renders, never shared assets."""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import logging
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout
from config.settings import (OUTPUT_VIDEO_DIR, SCHEDULER_TIMEZONE,
                             CLEANUP_AFTER_HOURS, CLEANUP_SCAN_DAYS, UPLOAD_TIMES)

from config.channels import MINUTE_MYSTERIES, RuntimePaths, slot_owner, valid_slot_owner, validate_rendered_media_filename

logger = logging.getLogger(__name__)


def _plain_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Relative cleanup paths are not allowed")
    # Reject links/junctions and traversal instead of following them to a target.
    if path.resolve() != path or any(
        part.is_symlink() or part.is_junction() for part in (path, *path.parents)
    ):
        raise ValueError("Linked or noncanonical cleanup path rejected")
    return path


def _fingerprint(path):
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("Cleanup target must be a regular, unshared file")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Generated file changed during fingerprinting")
    return {"sha256": digest, "size_bytes": after.st_size, "mtime_ns": after.st_mtime_ns}


def record_generated_file(video_path, day, slot, *, channel=MINUTE_MYSTERIES):
    path = _plain_path(video_path)
    return {"kind": "rendered_video", "owner": slot_owner(channel, day, slot), "channel_key": channel.channel_key,
            "path": str(path), **_fingerprint(path)}


def _eligible(item, now):
    if item.get("generation_status") != "ready":
        return False
    timestamp = item.get("successful_upload_at")
    try:
        completed = datetime.fromisoformat(timestamp)
        if completed.tzinfo is None or completed.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone")
    except (TypeError, ValueError):
        logger.info("%s: cleanup skipped; missing/invalid successful upload time", item.get("slot"))
        return False
    if now - completed < timedelta(hours=CLEANUP_AFTER_HOURS):
        logger.info("%s: cleanup skipped; item too young", item.get("slot"))
        return False
    youtube = item.get("youtube") or {}
    if (item.get("upload_status") != "uploaded" or youtube.get("success") is not True
            or youtube.get("status") != "uploaded"
            or not isinstance(youtube.get("video_id"), str) or not youtube["video_id"].strip()):
        logger.info("%s: cleanup skipped; YouTube upload not confirmed", item.get("slot"))
        return False
    return True


def _validated_target(item, day, approved_dir, references, *, channel=MINUTE_MYSTERIES):
    files = item.get("generated_files")
    if not isinstance(files, list) or len(files) != 1:
        raise ValueError("Exactly one recorded final-render ownership entry is required")
    record = files[0]
    if (record.get("kind") != "rendered_video" or item.get("slot") not in UPLOAD_TIMES
            or not valid_slot_owner(record.get("owner"), channel, day, item["slot"])):
        raise ValueError("Generated-file owner does not match queue date/slot")
    if any(value.get("channel_key", channel.channel_key) != channel.channel_key for value in (item, record)):
        raise ValueError("Cleanup channel ownership mismatch")
    path = _plain_path(record["path"])
    if path != _plain_path(item["video_path"]):
        raise ValueError("Generated-file record does not match this video's path")
    root = _plain_path(str(approved_dir))
    if channel != MINUTE_MYSTERIES and root.parts[-len(Path(channel.output_namespace).parts):] != Path(channel.output_namespace).parts:
        raise ValueError("Cleanup root does not match channel output namespace")
    # Only direct-child final MP4s matching this pipeline's naming are eligible.
    # No temp directories, assets, arbitrary extensions, or recursive deletion.
    validate_rendered_media_filename(path.name, channel)
    if path.parent != root:
        raise ValueError("Path is outside the approved final-render location/naming")
    if references.get(str(path), 0) != 1:
        raise ValueError("Generated file is referenced by another queue item")
    if (not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))
            or type(record.get("size_bytes")) is not int or record["size_bytes"] < 1
            or type(record.get("mtime_ns")) is not int):
        raise ValueError("Missing or invalid generation-time file fingerprint")
    if path.exists():
        actual = _fingerprint(path)
        if any(actual[key] != record[key] for key in actual):
            raise ValueError("File no longer matches its generation-time fingerprint")
    return path


def _clean_item(item, day, now, approved_dir, references, *, channel=MINUTE_MYSTERIES):
    previous = item.get("cleanup") or {}
    if previous.get("status") == "completed" or not _eligible(item, now):
        return False
    stamp = now.isoformat()
    try:
        path = _validated_target(item, day, approved_dir, references, channel=channel)
    except Exception as exc:
        logger.warning("Unsafe cleanup path rejected (%s): %r: %s", item.get("slot"), item.get("video_path"), exc)
        item["cleanup"] = {"status": "rejected", "last_attempt_at": stamp,
                           "error": str(exc), "deleted_files": previous.get("deleted_files", [])}
        return True
    logger.info("%s: cleanup eligible: %s", item["slot"], path)
    deleted = list(previous.get("deleted_files", []))
    absent = list(previous.get("absent_files", []))
    try:
        # Recheck containment/link state directly before unlinking this one file.
        _plain_path(str(path))
        if not path.exists():
            if str(path) not in absent:
                absent.append(str(path))
            logger.info("Cleanup file already absent: %s", path)
        else:
            path.unlink()
            if str(path) not in deleted:
                deleted.append(str(path))
            logger.info("Cleanup file deleted: %s", path)
        item["cleanup"] = {"status": "completed", "cleaned_at": stamp,
                           "deleted_files": deleted, "absent_files": absent, "failed_files": []}
        logger.info("%s: cleanup completed", item["slot"])
    except FileNotFoundError:
        item["cleanup"] = {"status": "completed", "cleaned_at": stamp, "deleted_files": deleted,
                           "absent_files": list(dict.fromkeys([*absent, str(path)])), "failed_files": []}
    except Exception as exc:
        logger.exception("Partial cleanup failure: %s", path)
        item["cleanup"] = {"status": "partial" if deleted else "failed", "last_attempt_at": stamp,
                           "deleted_files": deleted, "absent_files": absent,
                           "failed_files": [{"path": str(path), "error": str(exc)}]}
    return True


def cleanup_recent(queue, now=None, approved_dir=OUTPUT_VIDEO_DIR):
    if queue.channel != MINUTE_MYSTERIES and approved_dir == OUTPUT_VIDEO_DIR:
        from config.settings import BASE_DIR
        approved_dir = RuntimePaths(queue.channel, BASE_DIR).output
    now = now or datetime.now(ZoneInfo(SCHEDULER_TIMEZONE))
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Cleanup time must be timezone-aware")
    now = now.astimezone(ZoneInfo(SCHEDULER_TIMEZONE))
    logger.info("Cleanup scan started: last %s India-time dates", CLEANUP_SCAN_DAYS)
    try:
        with FileLock(str(queue.data_dir / "cleanup.lock"), timeout=0):
            days = [now.date() - timedelta(days=offset) for offset in range(CLEANUP_SCAN_DAYS)]
            snapshots = {}
            references = Counter()
            for day in days:
                path = queue.path(day)
                if not path.exists():
                    continue
                _plain_path(str(path))
                data = queue.read(day)
                logger.info("Cleanup queue scanned: %s", path)
                snapshots[day] = data
                for item in data["videos"]:
                    if item.get("video_path"):
                        references[str(Path(item["video_path"]).resolve())] += 1
            changed = 0
            for day in snapshots:
                if day >= now.date() or (snapshots[day].get("preparation") and snapshots[day].get("batch_status") != "READY"):
                    continue
                # Queue lock covers validation, unlink, and state writes; keep upload state intact.
                try:
                    with FileLock(str(queue.path(day)) + ".lock", timeout=10):
                        data = queue._read(day)
                        for item in data["videos"]:
                            try:
                                if _clean_item(item, day, now, approved_dir, references, channel=queue.channel):
                                    queue._write(day, data)
                                    changed += 1
                            except Exception:
                                logger.exception("Cleanup item failed; continuing with remaining items")
                except Exception:
                    logger.exception("Cleanup queue failed; continuing with remaining queues")
            return {"status": "completed", "queues_scanned": len(snapshots), "items_updated": changed}
    except Timeout:
        logger.info("Cleanup already running; skipping overlap")
        return {"status": "skipped", "reason": "lock_busy"}
