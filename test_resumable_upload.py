"""Offline resumable transfer and explicit existing-video retry tests."""
import builtins
from datetime import date
import json
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import httplib2
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, MediaFileUpload
from engine.uploader import youtube_uploader as uploader
from engine.scheduling.daily_queue import DailyQueue
from run_private_test import run_test


def http_error(status):
    return HttpError(httplib2.Response({"status": str(status)}), b'{"error":{"message":"fixture"}}')


class ResumableTests(unittest.TestCase):
    def test_ssl_eof_resumes_real_google_request_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "video.mp4"
            path.write_bytes(b"0123456789")
            media = MediaFileUpload(str(path), chunksize=uploader.UPLOAD_CHUNK_SIZE, resumable=True)
            transport = Mock()
            transport.request.side_effect = [
                (httplib2.Response({"status": "200", "location": "https://example.invalid/session"}), b""),
                ssl.SSLEOFError("fixture EOF"),
                (httplib2.Response({"status": "308", "range": "bytes=0-1"}), b""),
                (httplib2.Response({"status": "200"}), b'{"id":"same-session-id"}'),
            ]
            request = HttpRequest(transport, lambda resp, body: json.loads(body),
                                  "https://example.invalid/insert", method="POST", body="{}", resumable=media)
            try:
                with patch.object(uploader.time, "sleep") as sleep, self.assertLogs(uploader.logger, level="WARNING"):
                    result = uploader.complete_resumable_upload(request)
                self.assertEqual(result["id"], "same-session-id")
                self.assertEqual(transport.request.call_count, 4)
                calls = transport.request.call_args_list
                self.assertEqual(calls[0].args[0], "https://example.invalid/insert")
                self.assertTrue(all(c.args[0] == "https://example.invalid/session" for c in calls[1:]))
                self.assertEqual(calls[2].kwargs["headers"]["Content-Range"], "bytes */10")
                self.assertEqual(calls[3].kwargs["headers"]["Content-Range"], "bytes 2-9/10")
                sleep.assert_called_once_with(1)
            finally:
                media.stream().close()

    def test_transient_http_and_network_errors_retry(self):
        errors = [ssl.SSLEOFError("EOF"), socket.timeout("timeout"), ConnectionResetError(),
                  ConnectionAbortedError(), http_error(429), http_error(500), http_error(502),
                  http_error(503), http_error(504)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                request = Mock()
                request.next_chunk.side_effect = [error, (None, {"id": "id"})]
                with patch.object(uploader.time, "sleep"), self.assertLogs(uploader.logger, level="WARNING"):
                    self.assertEqual(uploader.complete_resumable_upload(request)["id"], "id")
                self.assertEqual(request.next_chunk.call_args_list, [call(num_retries=0), call(num_retries=0)])
                request.execute.assert_not_called()

    def test_progress_chunks_continue_until_final_id(self):
        request = Mock()
        progress = Mock()
        progress.progress.return_value = 0.5
        request.next_chunk.side_effect = [(progress, None), (None, {"id": "final"})]
        with self.assertLogs(uploader.logger, level="INFO") as logs:
            result = uploader.complete_resumable_upload(request)
        self.assertEqual(result["id"], "final")
        self.assertIn("50.0%", " ".join(logs.output))

    def test_five_retries_total_and_clear_exhaustion(self):
        request = Mock()
        request.next_chunk.side_effect = ssl.SSLEOFError("EOF")
        with patch.object(uploader.time, "sleep") as sleep, self.assertLogs(uploader.logger, level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "after 5 retries"):
                uploader.complete_resumable_upload(request)
        self.assertEqual(request.next_chunk.call_count, 6)
        self.assertEqual(sleep.call_args_list, [call(1), call(2), call(4), call(8), call(16)])

    def test_nontransient_errors_do_not_retry(self):
        for error in [http_error(400), http_error(401), http_error(403), http_error(404), ssl.SSLCertVerificationError("certificate")]:
            request = Mock()
            request.next_chunk.side_effect = error
            with patch.object(uploader.time, "sleep") as sleep, self.assertLogs(uploader.logger, level="ERROR"):
                with self.assertRaises(type(error)):
                    uploader.complete_resumable_upload(request)
            request.next_chunk.assert_called_once()
            sleep.assert_not_called()

    def test_metadata_unchanged_and_only_one_insert_across_retries(self):
        service = Mock()
        request = service.videos.return_value.insert.return_value
        request.next_chunk.side_effect = [ssl.SSLEOFError(), http_error(503), (None, {"id": "id"})]
        with patch.object(uploader.os.path, "isfile", return_value=True), \
             patch.object(uploader, "get_authenticated_service", return_value=service) as auth, \
             patch.object(uploader, "MediaFileUpload") as media, patch.object(uploader.time, "sleep"), \
             patch.object(uploader, "YOUTUBE_PRIVACY_STATUS", "private"), self.assertLogs(uploader.logger, level="WARNING"):
            result = uploader.upload_video("existing.mp4", "Exact title", "Exact description", ["#Original"])
        auth.assert_called_once_with()
        service.videos.return_value.insert.assert_called_once()
        body = service.videos.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["snippet"], {"title": "Exact title", "description": "Exact description", "tags": ["#Original"], "categoryId": "22"})
        self.assertEqual(body["status"]["privacyStatus"], "private")
        media.assert_called_once_with("existing.mp4", mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        media.return_value.stream.return_value.close.assert_called_once()
        request.execute.assert_not_called()
        self.assertEqual(result["id"], "id")


class ExistingVideoRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = DailyQueue(self.root / "manual_tests" / "first-private")
        self.day = date(2026, 9, 14)
        self.path = self.root / "unsolved_mysteries_20260914134353.mp4"
        self.path.write_bytes(b"existing fixture video")
        self.metadata = {"Title": "Saved title", "Description": "Saved description", "Hashtags": ["#Saved"]}
        self.item = {"slot": "10:00", "generation_status": "ready", "upload_status": "failed",
                     "video_path": str(self.path), "metadata": self.metadata,
                     "youtube": {"success": False, "status": "failed", "error": "SSL EOF"},
                     "telegram": {"success": True}, "idea": "Saved idea", "script": "Saved script"}
        self.save()

    def save(self):
        self.queue.update(self.day, lambda _: {"date": self.day.isoformat(), "timezone": "Asia/Kolkata", "videos": [self.item.copy()]})

    def test_explicit_retry_existing_video_metadata_no_generation_imports(self):
        service = Mock()
        service.videos.return_value.insert.return_value.next_chunk.return_value = (None, {"id": "retry-id"})
        generation = Mock(side_effect=AssertionError("No regeneration allowed"))
        original_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name.startswith(("openai", "moviepy", "engine.pipeline", "engine.generators", "engine.video", "engine.media")):
                raise AssertionError("Generation dependency imported: " + name)
            return original_import(name, *args, **kwargs)
        notifier = Mock(return_value={"success": True, "status": "sent"})
        with patch.object(uploader, "get_authenticated_service", return_value=service), patch("builtins.__import__", side_effect=guarded_import):
            result = run_test("first-private", base_dir=self.root, generator=generation,
                              notifier=notifier, retry_failed_upload=True)
        generation.assert_not_called()
        self.assertEqual(result["upload"]["video_id"], "retry-id")
        self.assertEqual(result["video_path"], str(self.path))
        self.assertEqual(result["metadata"], self.metadata)
        self.assertEqual(notifier.call_args.args[0]["upload"]["video_url"], "https://www.youtube.com/watch?v=retry-id")
        stored = self.queue.read(self.day)["videos"][0]
        self.assertEqual(stored["upload_status"], "uploaded")
        self.assertEqual(stored["manual_retry_history"][0]["previous_youtube"]["error"], "SSL EOF")
        # Repeating the explicit command after success must still skip upload.
        second_uploader = Mock()
        run_test("first-private", base_dir=self.root, uploader=second_uploader, retry_failed_upload=True)
        second_uploader.assert_not_called()

    def test_normal_runs_still_do_not_retry_failed_uploads(self):
        upload = Mock()
        run_test("first-private", base_dir=self.root, uploader=upload)
        upload.assert_not_called()

    def test_explicit_retry_refuses_uncertain_uploaded_and_id_bearing_states(self):
        for status, youtube in [("uploading", self.item["youtube"]), ("uncertain", self.item["youtube"]),
                                ("uploaded", {"success": True, "status": "uploaded", "video_id": "known"}),
                                ("failed", {"success": False, "status": "failed", "video_id": "known"})]:
            with self.subTest(status=status):
                self.item.update(upload_status=status, youtube=youtube)
                self.save()
                upload = Mock()
                run_test("first-private", base_dir=self.root, uploader=upload, retry_failed_upload=True)
                upload.assert_not_called()

    def test_missing_queue_does_not_generate(self):
        generate = Mock()
        with self.assertRaisesRegex(ValueError, "existing test queue"):
            run_test("missing-run", base_dir=self.root, generator=generate, retry_failed_upload=True)
        generate.assert_not_called()

    def test_missing_video_does_not_generate_or_upload(self):
        self.path.unlink()
        generate, upload = Mock(), Mock()
        run_test("first-private", base_dir=self.root, generator=generate, uploader=upload, retry_failed_upload=True)
        generate.assert_not_called()
        upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
