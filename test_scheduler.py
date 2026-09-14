"""Offline tests: temporary JSON/video fixtures, no API calls or real rendering."""
import importlib
import logging
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from engine.scheduling.daily_queue import DailyQueue, generate_only
from engine.scheduling.scheduler import create_scheduler

DAY = date(2026, 9, 14)
TZ = ZoneInfo("Asia/Kolkata")
SLOTS = ["10:00", "14:00", "19:00"]


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.queue = DailyQueue(self.temp.name)
        self.results = []
        for index in range(3):
            path = Path(self.temp.name) / f"video{index}.mp4"
            path.write_bytes(b"offline fixture")
            self.results.append({"video_path": str(path), "idea": f"Idea {index}",
                                 "script": f"Script {index}", "topic": "unsolved mysteries",
                                 "metadata": {"Title": f"Title {index}", "Description": f"Description {index}", "Hashtags": [f"#tag{index}"]}})

    def batch(self):
        generator = Mock(side_effect=self.results)
        result = self.queue.generate_batch(DAY, generator)
        self.assertEqual(generator.call_count, 3)
        return result

    def test_batch_has_three_ordered_entries_and_exact_metadata(self):
        result = self.batch()
        self.assertEqual([v["slot"] for v in result["videos"]], SLOTS)
        for item, generated in zip(result["videos"], self.results):
            for key in ("video_path", "metadata", "idea", "script"):
                self.assertEqual(item[key], generated[key])
            self.assertEqual(item["generation_status"], "ready")
            self.assertEqual(item["upload_status"], "pending")
            self.assertIsNone(item["youtube"])
            self.assertIsNone(item["telegram"])

    def test_partial_generation_keeps_assigned_slots(self):
        with self.assertLogs("engine.scheduling.daily_queue", level="ERROR"):
            result = self.queue.generate_batch(DAY, Mock(side_effect=[self.results[0], RuntimeError("render failed"), self.results[2]]))
        self.assertEqual([v["generation_status"] for v in result["videos"]], ["ready", "generation_failed", "ready"])
        self.assertEqual(result["videos"][2]["video_path"], self.results[2]["video_path"])
        uploader = Mock()
        self.queue.upload_slot("14:00", DAY, uploader, Mock())
        uploader.assert_not_called()

    def test_each_slot_uploads_only_its_exact_video_once(self):
        self.batch()
        for index, slot in enumerate(SLOTS):
            uploader = Mock(return_value={"success": True, "video_id": f"id{index}"})
            def notification(result):
                saved = self.queue.read(DAY)["videos"][index]
                self.assertTrue(saved["youtube"]["success"])
                self.assertEqual(result["metadata"], self.results[index]["metadata"])
                return {"success": True, "status": "sent"}
            self.queue.upload_slot(slot, DAY, uploader, notification)
            uploader.assert_called_once_with(self.results[index]["video_path"], self.results[index]["metadata"])
            # New queue object simulates a process restart.
            DailyQueue(self.temp.name).upload_slot(slot, DAY, uploader, Mock())
            self.assertEqual(uploader.call_count, 1)

    def test_telegram_timeout_preserves_youtube_and_no_retry(self):
        self.batch()
        uploader = Mock(return_value={"success": True, "video_id": "id"})
        with self.assertLogs("engine.scheduling.daily_queue", level="ERROR"):
            result = self.queue.upload_slot("10:00", DAY, uploader, Mock(side_effect=TimeoutError("timeout")))
        self.assertTrue(result["upload"]["success"])
        self.assertFalse(result["telegram"]["success"])
        self.queue.upload_slot("10:00", DAY, uploader, Mock())
        uploader.assert_called_once()

    def test_upload_failure_retains_local_video_and_is_not_retried(self):
        self.batch()
        uploader = Mock(return_value={"success": False, "error": "quota"})
        self.queue.upload_slot("10:00", DAY, uploader, Mock(return_value={"success": True}))
        self.queue.upload_slot("10:00", DAY, uploader, Mock())
        uploader.assert_called_once()
        self.assertTrue(Path(self.results[0]["video_path"]).exists())
        self.assertEqual(self.queue.read(DAY)["videos"][0]["upload_status"], "failed")

    def test_missing_file_never_substitutes_another_slot(self):
        self.batch()
        Path(self.results[1]["video_path"]).unlink()
        uploader = Mock()
        with self.assertLogs("engine.scheduling.daily_queue", level="ERROR"):
            self.queue.upload_slot("14:00", DAY, uploader, Mock())
        uploader.assert_not_called()
        self.assertEqual(self.queue.read(DAY)["videos"][2]["upload_status"], "pending")

    def test_missing_queue_skips(self):
        uploader = Mock()
        self.queue.upload_slot("10:00", DAY, uploader, Mock())
        uploader.assert_not_called()

    def test_batch_restart_does_not_regenerate(self):
        self.batch()
        generator = Mock()
        self.queue.generate_batch(DAY, generator)
        generator.assert_not_called()

    def test_persisted_uploading_or_video_id_blocks_retry(self):
        self.batch()
        for fields in [{"upload_status": "uploading"}, {"upload_status": "pending", "youtube": {"video_id": "known-id"}}]:
            self.queue._set(DAY, "10:00", fields)
            uploader = Mock()
            self.queue.upload_slot("10:00", DAY, uploader, Mock())
            uploader.assert_not_called()

    def test_concurrent_upload_claim_allows_one_attempt(self):
        self.batch()
        entered, release = threading.Event(), threading.Event()
        errors = []
        def upload(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization timeout")
            return {"success": True, "video_id": "id"}
        uploader = Mock(side_effect=upload)
        def worker():
            try:
                self.queue.upload_slot("10:00", DAY, uploader, Mock(return_value={"success": True}))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            DailyQueue(self.temp.name).upload_slot("10:00", DAY, uploader, Mock())
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        uploader.assert_called_once()

    def test_failed_atomic_replace_preserves_existing_queue(self):
        original = self.batch()
        with patch("engine.scheduling.daily_queue.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.queue._set(DAY, "10:00", {"upload_status": "uploading"})
        self.assertEqual(self.queue.read(DAY), original)


class PipelineSeparationTests(unittest.TestCase):
    def test_generation_only_never_uploads_or_notifies(self):
        # Stub client construction too: no credentials or external calls needed.
        with patch("openai.OpenAI", return_value=Mock()):
            pipeline = importlib.import_module("engine.pipeline.video_pipeline")
        mocks = {name: Mock(return_value=value) for name, value in [
            ("generate_video_ideas", ["Mystery"]), ("generate_script", "Script"),
            ("generate_voice", None), ("generate_scenes", ["sea"]),
            ("download_pexels_clips", ["clip.mp4"]), ("generate_subtitles", ["SCRIPT"]),
            ("select_background_music", None), ("build_short_video", None),
            ("generate_metadata", {"Title": "Title", "Description": "Description", "Hashtags": []}),
            ("upload_generated_video", {"success": True}), ("notify_generated_video", {"success": True}),
        ]}
        with patch.multiple(pipeline, **mocks):
            result = generate_only()
            self.assertNotIn("upload", result)
            mocks["upload_generated_video"].assert_not_called()
            mocks["notify_generated_video"].assert_not_called()
            # Existing manual/API behavior remains immediate upload + notification.
            manual = pipeline.generate_video()
            self.assertTrue(manual["upload"]["success"])
            mocks["upload_generated_video"].assert_called_once()
            mocks["notify_generated_video"].assert_called_once()


class SchedulerTests(unittest.TestCase):
    def test_four_ist_jobs_and_no_immediate_startup_work(self):
        queue = Mock()
        scheduler = create_scheduler(datetime(2026, 9, 14, 16, tzinfo=TZ), queue)
        jobs = scheduler.get_jobs()
        self.assertEqual(len(jobs), 5)
        daily_jobs = [job for job in jobs if job.id != "cleanup_hourly"]
        for job, clock in zip(daily_jobs, ["08:00", *SLOTS]):
            self.assertEqual(str(job.trigger.timezone), "Asia/Kolkata")
            fields = {field.name: str(field) for field in job.trigger.fields}
            self.assertEqual(int(fields["hour"]), int(clock[:2]))
            self.assertEqual(int(fields["minute"]), int(clock[3:]))
            self.assertEqual(job.max_instances, 1)
            self.assertTrue(job.coalesce)
            self.assertEqual(job.misfire_grace_time, 900)
            self.assertFalse(hasattr(job, "next_run_time"))
        queue.generate_batch.assert_not_called()
        queue.upload_slot.assert_not_called()

    def test_startup_recovery_only_inside_grace(self):
        for hour, minute, recovery in [(8, 5, "generate_daily"), (8, 16, None), (10, 5, "upload_1000"), (16, 0, None), (18, 50, None)]:
            scheduler = create_scheduler(datetime(2026, 9, 14, hour, minute, tzinfo=TZ), Mock())
            recovered = [j.id for j in scheduler.get_jobs() if hasattr(j, "next_run_time")]
            self.assertEqual(recovered, [recovery] if recovery else [])

    def test_scheduler_start_logs_jobs_without_running_them(self):
        queue = Mock()
        scheduler = create_scheduler(datetime(2026, 9, 14, 16, tzinfo=TZ), queue)
        with patch.object(scheduler, "_main_loop"), self.assertLogs("engine.scheduling.scheduler", level="INFO"):
            scheduler.start(paused=True)
        scheduler.shutdown(wait=False)
        queue.generate_batch.assert_not_called()
        queue.upload_slot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
