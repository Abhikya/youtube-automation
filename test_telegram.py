"""Offline notification checks: no network calls, uploads, or real credentials."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, mock_open, patch

from telegram.error import TimedOut
from engine.approval.telegram_bot import send_video_notification


class TelegramNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def notify(self, upload, error=None):
        data = {
            "video_path": "render.mp4",
            "metadata": {"Title": "The missing ship"},
            "upload": upload,
        }
        bot = AsyncMock()
        bot.__aenter__.return_value = bot
        bot.send_video.return_value = SimpleNamespace(message_id=123)
        bot.send_video.side_effect = error
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "123"}), \
             patch("engine.approval.telegram_bot.Bot", return_value=bot), \
             patch("engine.approval.telegram_bot.HTTPXRequest") as request, \
             patch("builtins.open", mock_open(read_data=b"video")):
            result = await send_video_notification(data)
        return result, bot.send_video.call_args.kwargs, request.call_args.kwargs

    async def test_success_preview_has_link_and_no_buttons(self):
        result, sent, timeouts = await self.notify({
            "success": True, "privacy_status": "private",
            "video_url": "https://www.youtube.com/watch?v=test",
        })
        self.assertTrue(result["success"])
        self.assertIn("Uploaded successfully", sent["caption"])
        self.assertIn("watch?v=test", sent["caption"])
        self.assertIn("private", sent["caption"])
        self.assertNotIn("reply_markup", sent)
        self.assertEqual(sent["write_timeout"], 180)
        self.assertTrue(all(0 < value <= 180 for value in timeouts.values()))

    async def test_upload_failure_is_reported_truthfully(self):
        result, sent, _ = await self.notify({"success": False, "error": "Quota exceeded"})
        self.assertTrue(result["success"])
        self.assertIn("upload failed", sent["caption"])
        self.assertIn("Quota exceeded", sent["caption"])

    async def test_timeout_does_not_modify_upload(self):
        upload = {"success": True, "privacy_status": "private", "video_url": "url"}
        original = upload.copy()
        with self.assertLogs("engine.approval.telegram_bot", level="ERROR"):
            result, _, _ = await self.notify(upload, TimedOut())
        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "TimedOut")
        self.assertEqual(upload, original)

    async def test_missing_config_is_a_notification_failure(self):
        with patch.dict(os.environ, {}, clear=True), \
             self.assertLogs("engine.approval.telegram_bot", level="ERROR"):
            result = await send_video_notification({})
        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
