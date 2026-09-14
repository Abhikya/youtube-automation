"""Cleanup safety tests use isolated temporary directories, never project videos."""
import copy
from datetime import datetime, timedelta
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from engine.scheduling.cleanup import cleanup_recent, record_generated_file
from engine.scheduling.daily_queue import DailyQueue
from engine.scheduling.scheduler import create_scheduler

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 16, 10, 35, tzinfo=TZ)
DAY = NOW.date() - timedelta(days=1)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output" / "videos"
        self.output.mkdir(parents=True)
        self.queue = DailyQueue(self.root / "data")
        self.video = self.output / "unsolved_mysteries_20260915082000.mp4"
        self.video.write_bytes(b"unique generated video one")
        self.item = self.make_item(self.video, "10:00")
        self.save([self.item])

    def make_item(self, path, slot):
        return {
            "slot": slot, "generation_status": "ready",
            "generation_completed_at": (NOW - timedelta(hours=26)).isoformat(),
            "upload_status": "uploaded", "video_path": str(path),
            "idea": "Idea", "script": "Script",
            "metadata": {"Title": "Title", "Description": "Description", "Hashtags": ["#Mystery"]},
            "youtube": {"success": True, "status": "uploaded", "video_id": "abc123",
                        "video_url": "https://www.youtube.com/watch?v=abc123"},
            "telegram": {"success": True, "status": "sent"},
            "generated_files": [record_generated_file(str(path), DAY, slot)],
            "cleanup": {"status": "pending"},
        }

    def save(self, items):
        data = {"date": DAY.isoformat(), "timezone": "Asia/Kolkata", "videos": copy.deepcopy(items)}
        self.queue.update(DAY, lambda existing: data)

    def run_cleanup(self):
        return cleanup_recent(self.queue, NOW, self.output)

    def stored(self):
        return self.queue.read(DAY)["videos"]

    def test_23_hours_not_deleted(self):
        self.item["generation_completed_at"] = (NOW - timedelta(hours=23)).isoformat()
        self.save([self.item])
        self.run_cleanup()
        self.assertTrue(self.video.exists())

    def test_older_than_24_hours_deleted_history_preserved(self):
        self.run_cleanup()
        self.assertFalse(self.video.exists())
        self.assertTrue(self.queue.path(DAY).exists())
        saved = self.stored()[0]
        self.assertEqual(saved["cleanup"]["status"], "completed")
        self.assertEqual(saved["cleanup"]["deleted_files"], [str(self.video)])
        for key in ("idea", "script", "metadata", "youtube", "telegram", "generation_completed_at"):
            self.assertEqual(saved[key], self.item[key])

    def test_exact_24_hour_boundary_allowed(self):
        self.item["generation_completed_at"] = (NOW - timedelta(hours=24)).isoformat()
        self.save([self.item])
        self.run_cleanup()
        self.assertFalse(self.video.exists())

    def test_failed_pending_uploading_unknown_not_deleted(self):
        for status in ["failed", "pending", "uploading", "unknown", "not_attempted", None]:
            with self.subTest(status=status):
                self.item["upload_status"] = status
                self.save([self.item])
                self.run_cleanup()
                self.assertTrue(self.video.exists())

    def test_youtube_failure_even_with_uploaded_status_not_deleted(self):
        self.item["youtube"]["success"] = False
        self.save([self.item])
        self.run_cleanup()
        self.assertTrue(self.video.exists())

    def test_missing_video_id_not_deleted(self):
        self.item["youtube"].pop("video_id")
        self.save([self.item])
        self.run_cleanup()
        self.assertTrue(self.video.exists())

    def test_invalid_missing_naive_or_future_timestamp_not_deleted(self):
        for timestamp in [None, "invalid", "2026-09-15T08:00:00", (NOW + timedelta(days=1)).isoformat()]:
            with self.subTest(timestamp=timestamp):
                self.item["generation_completed_at"] = timestamp
                self.save([self.item])
                self.run_cleanup()
                self.assertTrue(self.video.exists())

    def test_telegram_failure_does_not_prevent_cleanup(self):
        self.item["telegram"] = {"success": False, "error": "timeout"}
        self.save([self.item])
        self.run_cleanup()
        self.assertFalse(self.video.exists())
        self.assertFalse(self.stored()[0]["telegram"]["success"])

    def test_missing_local_file_is_recorded_as_absent(self):
        self.video.unlink()
        self.run_cleanup()
        self.assertEqual(self.stored()[0]["cleanup"]["status"], "completed")
        self.assertEqual(self.stored()[0]["cleanup"]["absent_files"], [str(self.video)])

    def test_second_run_is_idempotent(self):
        self.run_cleanup()
        original = self.stored()
        self.assertEqual(self.run_cleanup()["items_updated"], 0)
        self.assertEqual(self.stored(), original)

    def test_assets_external_paths_and_source_files_rejected(self):
        for relative in ["assets/clips_library/unsolved_mysteries_20260915082000.mp4",
                         "assets/music/unsolved_mysteries_20260915082000.mp4",
                         "temp/audio/voice.mp3", "temp/clips/clip.mp4",
                         "outside/unsolved_mysteries_20260915082000.mp4",
                         "output/videos/.env", "output/videos/client_secret.json",
                         "output/videos/token.pickle", "output/videos/code.py"]:
            with self.subTest(path=relative):
                protected = self.root / relative
                protected.parent.mkdir(parents=True, exist_ok=True)
                protected.write_bytes(b"protected fixture")
                item = self.make_item(protected, "10:00")
                self.save([item])
                with self.assertLogs("engine.scheduling.cleanup", level="WARNING"):
                    self.run_cleanup()
                self.assertTrue(protected.exists())
                self.assertEqual(self.stored()[0]["cleanup"]["status"], "rejected")

    def test_previous_day_is_scanned_but_older_than_window_is_not(self):
        self.assertEqual(self.run_cleanup()["queues_scanned"], 1)
        self.assertFalse(self.video.exists())
        self.video.write_bytes(b"old historical fixture")
        old_day = NOW.date() - timedelta(days=8)
        old_item = self.make_item(self.video, "10:00")
        old_item["generated_files"] = [record_generated_file(str(self.video), old_day, "10:00")]
        self.queue.update(old_day, lambda _: {"date": old_day.isoformat(), "timezone": "Asia/Kolkata", "videos": [old_item]})
        self.run_cleanup()
        self.assertTrue(self.video.exists())

    def test_individual_generation_timestamps_are_recorded(self):
        # Use a fresh date, avoiding the existing test queue.
        day = NOW.date()
        results = []
        times = [NOW.replace(hour=8, minute=18), NOW.replace(hour=8, minute=31), NOW.replace(hour=8, minute=45)]
        for index in range(3):
            path = self.output / f"unsolved_mysteries_2026091608{index:02d}00.mp4"
            path.write_bytes(f"video {index}".encode())
            results.append({"video_path": str(path), "metadata": self.item["metadata"], "idea": "Idea", "script": "Script"})
        with patch("engine.scheduling.daily_queue.india_now", side_effect=times):
            data = self.queue.generate_batch(day, Mock(side_effect=results))
        self.assertEqual([item["generation_completed_at"] for item in data["videos"]], [time.isoformat() for time in times])
        self.assertTrue(all(item["generated_files"] for item in data["videos"]))

    def test_permission_failure_does_not_stop_other_item_and_retries(self):
        second = self.output / "unsolved_mysteries_20260915083100.mp4"
        second.write_bytes(b"unique generated video two")
        self.save([self.item, self.make_item(second, "14:00")])
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == self.video:
                raise PermissionError("fixture is busy")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink), self.assertLogs("engine.scheduling.cleanup", level="ERROR"):
            self.run_cleanup()
        self.assertTrue(self.video.exists())
        self.assertFalse(second.exists())
        self.assertEqual(self.stored()[0]["cleanup"]["status"], "failed")
        self.assertEqual(self.stored()[1]["cleanup"]["status"], "completed")
        self.run_cleanup()
        self.assertFalse(self.video.exists())
        self.assertEqual(self.stored()[0]["cleanup"]["status"], "completed")

    def test_missing_ownership_or_changed_file_is_rejected(self):
        for change in ["missing", "owner", "changed"]:
            with self.subTest(change=change):
                item = copy.deepcopy(self.item)
                if change == "missing":
                    item.pop("generated_files")
                elif change == "owner":
                    item["generated_files"][0]["owner"] = f"{DAY}:19:00"
                else:
                    self.video.write_bytes(b"replacement content")
                self.save([item])
                with self.assertLogs("engine.scheduling.cleanup", level="WARNING"):
                    self.run_cleanup()
                self.assertTrue(self.video.exists())
                self.assertEqual(self.stored()[0]["cleanup"]["status"], "rejected")

    def test_shared_reference_is_rejected(self):
        other = self.make_item(self.video, "14:00")
        self.save([self.item, other])
        with self.assertLogs("engine.scheduling.cleanup", level="WARNING"):
            self.run_cleanup()
        self.assertTrue(self.video.exists())

    def test_relative_and_traversal_paths_rejected(self):
        for path in ["output/videos/unsolved_mysteries_20260915082000.mp4",
                     str(self.output / ".." / "videos" / self.video.name)]:
            with self.subTest(path=path):
                item = copy.deepcopy(self.item)
                item["video_path"] = path
                item["generated_files"][0]["path"] = path
                self.save([item])
                with self.assertLogs("engine.scheduling.cleanup", level="WARNING"):
                    self.run_cleanup()
                self.assertTrue(self.video.exists())

    def test_generation_failure_never_deleted(self):
        self.item["generation_status"] = "generation_failed"
        self.save([self.item])
        self.run_cleanup()
        self.assertTrue(self.video.exists())

    def test_cleanup_exception_isolated_from_scheduler(self):
        scheduler = create_scheduler(NOW, self.queue)
        with patch("engine.scheduling.cleanup.cleanup_recent", side_effect=PermissionError("test")), self.assertLogs("engine.scheduling.daily_queue", level="ERROR"):
            result = scheduler.get_job("cleanup_hourly").func()
        self.assertEqual(result["status"], "failed")
        self.assertIsNotNone(scheduler.get_job("generate_daily"))
        self.assertIsNotNone(scheduler.get_job("upload_1900"))

    def test_original_jobs_and_hourly_cleanup_registered(self):
        scheduler = create_scheduler(NOW, self.queue)
        self.assertEqual({job.id for job in scheduler.get_jobs()},
                         {"generate_daily", "upload_1000", "upload_1400", "upload_1900", "cleanup_hourly"})
        job = scheduler.get_job("cleanup_hourly")
        fields = {field.name: str(field) for field in job.trigger.fields}
        self.assertEqual(fields["hour"], "*")
        self.assertEqual(fields["minute"], "35")
        self.assertEqual(str(job.trigger.timezone), "Asia/Kolkata")
        self.assertEqual(job.max_instances, 1)
        self.assertTrue(job.coalesce)


if __name__ == "__main__":
    unittest.main()
