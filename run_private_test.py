"""Explicit one-video private test using the production JSON queue and pipeline."""
import argparse
from datetime import date
import json
import logging
import os
from pathlib import Path
import re


def run_test(run_id, *, base_dir=None, generator=None, uploader=None, notifier=None, retry_failed_upload=False):
    from config.settings import DATA_DIR, UPLOAD_TIMES, YOUTUBE_PRIVACY_STATUS
    from engine.scheduling.daily_queue import DailyQueue, india_now
    from filelock import FileLock
    if YOUTUBE_PRIVACY_STATUS != "private":
        raise ValueError("Private test refuses to run unless YOUTUBE_PRIVACY_STATUS is private")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", run_id):
        raise ValueError("Use a simple run ID containing letters, digits, underscores or hyphens")
    base = Path(base_dir or DATA_DIR)
    queue = DailyQueue(base / "manual_tests" / run_id)
    existing = list(queue.data_dir.glob("daily_queue_????-??-??.json"))
    if retry_failed_upload and not existing:
        raise ValueError("Upload retry requires an existing test queue; generation is disabled")
    if len(existing) > 1:
        raise ValueError("Multiple queues in this test run; inspect manually")
    day = date.fromisoformat(existing[0].stem.removeprefix("daily_queue_")) if existing else india_now().date()
    slot = UPLOAD_TIMES[0]
    # The same root lock as production prevents overlapping use of shared audio.
    attempt = None
    with FileLock(str(base / "daily_generation.lock"), timeout=0):
        if not existing:
            queue.generate_batch(day, generator, slots=[slot])
        data = queue.read(day)
        if data is None:
            raise RuntimeError("Test queue could not be saved; no upload attempted")
        item = next((item for item in data["videos"] if item["slot"] == slot), None)
        if item is None or len(data["videos"]) != 1:
            raise ValueError("Test queue must contain exactly one assigned item")
        if item.get("generation_status") == "ready":
            attempt = queue.upload_slot(slot, day, uploader, notifier, retry_failed=retry_failed_upload)
        data = queue.read(day)
    item = data["videos"][0]
    return {"queue_file": str(queue.path(day)), "video_path": item.get("video_path"),
            "generation_status": item.get("generation_status"), "generation_error": item.get("generation_error"),
            "metadata": item.get("metadata"), "upload_status": item.get("upload_status"),
            "upload": item.get("youtube") or (attempt or {}).get("upload"),
            "telegram": item.get("telegram") or (attempt or {}).get("telegram"),
            "state": (attempt or {}).get("state", {"success": True})}


def main():
    parser = argparse.ArgumentParser(description="Generate ONE video, upload privately, notify Telegram, and save restart-safe state.")
    parser.add_argument("--run-id", required=True, help="Reuse the SAME ID to inspect/restart this test without a duplicate upload")
    parser.add_argument("--retry-failed-upload", action="store_true", help="Retry only a saved failed upload; NEVER generate a new video")
    args = parser.parse_args()
    # Set before importing settings/uploader; overrides .env or inherited public settings.
    os.environ["YOUTUBE_PRIVACY_STATUS"] = "private"
    from check_readiness import check_readiness
    if not args.retry_failed_upload and not check_readiness():
        return 1
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # HTTPX info logs include Telegram request URLs; never print bot tokens.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        result = run_test(args.run_id, retry_failed_upload=args.retry_failed_upload)
    except Exception as exc:
        print(f"Private test stopped safely: {type(exc).__name__}; inspect configuration/queue before retrying.")
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if (result.get("upload") or {}).get("success") and result["state"]["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
