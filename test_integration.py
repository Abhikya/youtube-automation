"""Remaining integration edge cases; no external requests or production cleanup."""
import copy
from datetime import date, datetime, timedelta
import importlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, mock_open, patch
from zoneinfo import ZoneInfo

import httpx
from openai import RateLimitError
from engine.scheduling.daily_queue import DailyQueue
from engine.scheduling.scheduler import create_scheduler
from run_private_test import run_test

DAY = date(2026, 9, 14)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = DailyQueue(self.root / "queues")
        self.video = self.root / "unsolved_mysteries_20260914083000.mp4"
        self.video.write_bytes(b"fake render")
        self.metadata = {"Title": "A documented mystery", "Description": "The explanation is uncertain.", "Hashtags": ["#Mystery", "#Shorts"]}
        self.generated = {"video_path": str(self.video), "metadata": self.metadata, "idea": "Mystery", "script": "Script"}
        self.uploaded = {"success": True, "status": "uploaded", "video_id": "id123", "video_url": "https://www.youtube.com/watch?v=id123", "privacy_status": "private"}

    def ready_queue(self):
        return self.queue.generate_batch(DAY, Mock(return_value=self.generated), slots=["10:00"])

    def test_private_test_exactly_one_and_restart_no_second_upload(self):
        order = []
        def generate():
            order.append("generate")
            return self.generated
        def upload(path, metadata):
            order.append("youtube")
            self.assertEqual(path, str(self.video))
            self.assertEqual(metadata, self.metadata)
            return self.uploaded
        def notify(result):
            order.append("telegram")
            self.assertEqual(result["upload"], self.uploaded)
            return {"success": True, "status": "sent"}
        result = run_test("one-private", base_dir=self.root, generator=generate, uploader=upload, notifier=notify)
        self.assertEqual(order, ["generate", "youtube", "telegram"])
        state = json.loads(Path(result["queue_file"]).read_text())
        self.assertEqual(len(state["videos"]), 1)
        self.assertEqual(state["videos"][0]["metadata"], self.metadata)
        restarted = run_test("one-private", base_dir=self.root, generator=Mock(side_effect=AssertionError("regenerated")),
                             uploader=Mock(side_effect=AssertionError("reuploaded")), notifier=Mock())
        self.assertEqual(restarted["upload"], self.uploaded)
        self.assertTrue(self.video.exists())

    def test_private_test_retains_known_upload_result_when_state_save_fails(self):
        original = DailyQueue._set
        def save(queue, day, slot, fields):
            if "youtube" in fields:
                raise OSError("disk error")
            return original(queue, day, slot, fields)
        with patch.object(DailyQueue, "_set", save), self.assertLogs("engine.scheduling.daily_queue", level="ERROR"):
            result = run_test("state-failure", base_dir=self.root, generator=Mock(return_value=self.generated),
                              uploader=Mock(return_value=self.uploaded), notifier=Mock())
        self.assertEqual(result["upload"]["video_id"], "id123")
        self.assertFalse(result["state"]["success"])
        self.assertEqual(result["upload_status"], "uploading")

    def test_private_test_refuses_public_configuration(self):
        with patch("config.settings.YOUTUBE_PRIVACY_STATUS", "public"):
            with self.assertRaises(ValueError):
                run_test("must-be-private", base_dir=self.root, generator=Mock())

    def test_youtube_failure_is_sent_to_telegram_and_video_retained(self):
        self.ready_queue()
        failure = {"success": False, "status": "failed", "error": "Quota exceeded"}
        notifier = Mock(return_value={"success": True})
        result = self.queue.upload_slot("10:00", DAY, Mock(return_value=failure), notifier)
        self.assertEqual(notifier.call_args.args[0]["upload"], failure)
        self.assertFalse(result["upload"]["success"])
        self.assertTrue(self.video.exists())

    def test_success_with_telegram_failure_is_persisted(self):
        self.ready_queue()
        with self.assertLogs("engine.scheduling.daily_queue", level="ERROR"):
            result = self.queue.upload_slot("10:00", DAY, Mock(return_value=self.uploaded), Mock(side_effect=TimeoutError("Telegram")))
        self.assertTrue(result["upload"]["success"])
        self.assertFalse(result["telegram"]["success"])
        uploader = Mock()
        DailyQueue(self.queue.data_dir).upload_slot("10:00", DAY, uploader, Mock())
        uploader.assert_not_called()

    def test_uncertain_state_logs_manual_review_without_retry(self):
        self.ready_queue()
        self.queue._set(DAY, "10:00", {"upload_status": "uploading"})
        uploader = Mock()
        with self.assertLogs("engine.scheduling.daily_queue", level="WARNING") as logs:
            DailyQueue(self.queue.data_dir).upload_slot("10:00", DAY, uploader, Mock())
        self.assertIn("manual review", " ".join(logs.output))
        uploader.assert_not_called()

    def test_malformed_and_incomplete_queue_skip(self):
        examples = ["{", "[]", json.dumps({"date": DAY.isoformat(), "timezone": "Asia/Kolkata"}),
                    json.dumps({"date": DAY.isoformat(), "timezone": "Asia/Kolkata", "videos": [None]}),
                    json.dumps({"date": DAY.isoformat(), "timezone": "Asia/Kolkata", "videos": [{"slot": "10:00"}, {"slot": "10:00"}]})]
        for raw in examples:
            with self.subTest(raw=raw):
                self.queue.path(DAY).write_text(raw)
                uploader = Mock()
                with self.assertLogs("engine.scheduling.daily_queue", level="ERROR"):
                    self.queue.upload_slot("10:00", DAY, uploader, Mock())
                uploader.assert_not_called()
                self.assertEqual(self.queue.path(DAY).read_text(), raw)

    def test_missing_metadata_and_incomplete_items_never_upload(self):
        original = self.ready_queue()
        for fields in [{"metadata": None}, {"metadata": {}}, {"metadata": {"Title": "Title"}},
                       {"metadata": {"Title": "Title", "Description": "Description", "Hashtags": "bad"}},
                       {"video_path": None}, {"generation_status": None}]:
            with self.subTest(fields=fields):
                data = copy.deepcopy(original)
                data["videos"][0].update(fields)
                self.queue.update(DAY, lambda existing: data)
                uploader = Mock()
                self.queue.upload_slot("10:00", DAY, uploader, Mock())
                uploader.assert_not_called()

    def test_quota_error_recorded_and_next_day_can_run(self):
        response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/test"))
        error = RateLimitError("credit_balance_exhausted", response=response, body={"code": "credit_balance_exhausted"})
        generator = Mock(side_effect=error)
        with self.assertLogs("engine.scheduling.daily_queue", level="ERROR") as logs:
            result = self.queue.generate_batch(DAY, generator)
        self.assertEqual(generator.call_count, 3)
        self.assertTrue(all(item["generation_status"] == "generation_failed" for item in result["videos"]))
        self.assertIn("insufficient API quota", " ".join(logs.output))
        future = self.queue.generate_batch(DAY + timedelta(days=1), Mock(return_value=self.generated))
        self.assertTrue(all(item["generation_status"] == "ready" for item in future["videos"]))

    def test_failed_state_save_after_upload_keeps_claim_uncertain(self):
        self.ready_queue()
        uploader = Mock(return_value=self.uploaded)
        with patch.object(self.queue, "_set", side_effect=OSError("disk error")), self.assertLogs("engine.scheduling.daily_queue", level="ERROR"):
            result = self.queue.upload_slot("10:00", DAY, uploader, Mock())
        self.assertTrue(result["upload"]["success"])
        self.assertFalse(result["state"]["success"])
        self.assertEqual(self.queue.read(DAY)["videos"][0]["upload_status"], "uploading")
        self.queue.upload_slot("10:00", DAY, uploader, Mock())
        uploader.assert_called_once()

    def test_failed_claim_save_never_calls_youtube(self):
        self.ready_queue()
        uploader = Mock()
        with patch.object(self.queue, "_write", side_effect=OSError("disk error")), self.assertLogs("engine.scheduling.daily_queue", level="ERROR"):
            self.queue.upload_slot("10:00", DAY, uploader, Mock())
        uploader.assert_not_called()

    def test_production_schedule_and_restart_windows_unchanged(self):
        timezone = ZoneInfo("Asia/Kolkata")
        for hour, minute, recovery in [(7, 0, []), (8, 5, ["generate_daily"]), (16, 0, []), (18, 50, [])]:
            scheduler = create_scheduler(datetime(2026, 9, 14, hour, minute, tzinfo=timezone), Mock())
            self.assertEqual([job.id for job in scheduler.get_jobs() if hasattr(job, "next_run_time")], recovery)
            expected = {"generate_daily": ("8", "0"), "upload_1000": ("10", "0"), "upload_1400": ("14", "0"),
                        "upload_1900": ("19", "0"), "cleanup_hourly": ("*", "35")}
            for job in scheduler.get_jobs():
                fields = {field.name: str(field) for field in job.trigger.fields}
                self.assertEqual((fields["hour"], fields["minute"]), expected[job.id])
                self.assertEqual(str(job.trigger.timezone), "Asia/Kolkata")


class ServiceBoundaryTests(unittest.TestCase):
    def test_render_openai_and_pexels_failures_never_upload(self):
        with patch("openai.OpenAI", return_value=Mock()):
            pipeline = importlib.import_module("engine.pipeline.video_pipeline")
        replacements = {name: Mock(return_value=value) for name, value in [
            ("generate_video_ideas", ["Mystery"]), ("generate_script", "Script"), ("generate_voice", None),
            ("generate_scenes", ["sea"]), ("download_pexels_clips", ["clip.mp4"]), ("generate_subtitles", ["SCRIPT"]),
            ("select_background_music", None), ("build_short_video", None),
            ("generate_metadata", {"Title": "Title", "Description": "Description", "Hashtags": []}),
            ("upload_generated_video", None), ("notify_generated_video", None)]}
        with patch.multiple(pipeline, **replacements):
            for stage in ("generate_script", "download_pexels_clips", "build_short_video", "generate_metadata"):
                replacements[stage].side_effect = RuntimeError("service/render error")
                with self.assertRaises(RuntimeError):
                    pipeline.generate_video()
                replacements[stage].side_effect = None
            replacements["upload_generated_video"].assert_not_called()
            replacements["notify_generated_video"].assert_not_called()

    def test_uploader_uses_exact_metadata_and_private_payload(self):
        from engine.uploader import youtube_uploader as uploader
        service = Mock()
        service.videos.return_value.insert.return_value.next_chunk.return_value = (None, {"id": "id123"})
        with patch.object(uploader.os.path, "isfile", return_value=True), patch.object(uploader, "get_authenticated_service", return_value=service), \
             patch.object(uploader, "MediaFileUpload"), patch.object(uploader, "YOUTUBE_PRIVACY_STATUS", "private"):
            uploader.upload_video("video.mp4", "Generated title", "Generated description", ["#Mystery"])
        body = service.videos.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["title"], "Generated title")
        self.assertEqual(body["snippet"]["description"], "Generated description")
        self.assertEqual(body["snippet"]["tags"], ["#Mystery"])
        self.assertEqual(body["status"]["privacyStatus"], "private")

    def test_missing_oauth_does_not_open_browser_during_upload(self):
        from engine.uploader import youtube_uploader as uploader
        with patch.object(uploader.os.path, "isfile", return_value=False), patch.object(uploader, "InstalledAppFlow") as flow:
            with self.assertRaisesRegex(RuntimeError, "authorization required"):
                uploader.get_authenticated_service()
        flow.from_client_secrets_file.assert_not_called()

    def test_valid_oauth_token_reused_without_browser(self):
        from engine.uploader import youtube_uploader as uploader
        creds = SimpleNamespace(valid=True, expired=False, refresh_token="fixture")
        with patch.object(uploader.os.path, "isfile", return_value=True), patch("builtins.open", mock_open()), \
             patch.object(uploader.pickle, "load", return_value=creds), patch.object(uploader, "build") as build, \
             patch.object(uploader, "InstalledAppFlow") as flow:
            uploader.get_authenticated_service()
        flow.from_client_secrets_file.assert_not_called()
        self.assertIs(build.call_args.kwargs["credentials"], creds)

    def test_expired_oauth_token_refreshes_without_browser(self):
        from engine.uploader import youtube_uploader as uploader
        creds = Mock(valid=False, expired=True, refresh_token="fixture")
        with patch.object(uploader.os.path, "isfile", return_value=True), patch("builtins.open", mock_open()), \
             patch.object(uploader.pickle, "load", return_value=creds), patch.object(uploader.pickle, "dump") as dump, \
             patch.object(uploader, "build"), patch.object(uploader, "InstalledAppFlow") as flow, patch.object(uploader, "Request"):
            uploader.get_authenticated_service()
        creds.refresh.assert_called_once()
        dump.assert_called_once()
        flow.from_client_secrets_file.assert_not_called()

    def test_diagnostics_never_print_secret_values(self):
        from check_readiness import check_readiness
        with tempfile.TemporaryDirectory() as temporary:
            output = []
            fixture = {name: "secret-fixture-DO-NOT-PRINT" for name in ("OPENAI_API_KEY", "PEXELS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
            with patch.dict(os.environ, fixture), patch("check_readiness.subprocess.run", return_value=SimpleNamespace(returncode=0)):
                self.assertFalse(check_readiness(Path(temporary), output.append))
            self.assertNotIn("secret-fixture-DO-NOT-PRINT", "\n".join(output))
            self.assertIn("OPENAI_API_KEY: configured", output)


if __name__ == "__main__":
    unittest.main()
