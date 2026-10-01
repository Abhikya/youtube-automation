"""Offline notification checks: no network calls, uploads, or real credentials."""
import os
import io
from contextlib import redirect_stdout, redirect_stderr
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, mock_open, patch

from telegram.error import TimedOut
from engine.approval.telegram_bot import send_video_notification


class TelegramNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def notify(self, upload, error=None, *, token="test"):
        data = {
            "video_path": "render.mp4",
            "metadata": {"Title": "The missing ship"},
            "upload": upload,
        }
        bot = AsyncMock()
        bot.__aenter__.return_value = bot
        bot.send_video.return_value = SimpleNamespace(message_id=123)
        bot.send_video.side_effect = error
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": "123"}), \
             patch("engine.approval.telegram_bot.Bot", return_value=bot) as constructor, \
             patch("engine.approval.telegram_bot.HTTPXRequest") as request, \
             patch("builtins.open", mock_open(read_data=b"video")):
            result = await send_video_notification(data)
        constructor.assert_called_once_with(token=token.strip(), request=request.return_value)
        self.assertEqual(bot.send_video.call_args.kwargs["chat_id"], "123")
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

    async def test_trailing_newline_is_normalized(self):
        result, _, _ = await self.notify({'success': False, 'error': 'fixture'},
                                         token='123456:FAKE_TOKEN_FOR_TEST\n')
        self.assertTrue(result['success'])

    async def test_surrounding_spaces_are_normalized(self):
        result, _, _ = await self.notify({'success': False, 'error': 'fixture'},
                                         token='  123456:FAKE_TOKEN_FOR_TEST  ')
        self.assertTrue(result['success'])

    async def test_internal_whitespace_and_control_characters_rejected_without_leak(self):
        for separator in (' ', '\n', '\r', '\t', '\x01', '\x7f', '\u200b'):
            token = '123456:FAKE_' + separator + 'TOKEN_FOR_TEST'
            with self.subTest(separator=repr(separator)):
                output = io.StringIO()
                with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': token, 'TELEGRAM_CHAT_ID': '123'}), \
                     patch('engine.approval.telegram_bot.Bot') as constructor, \
                     self.assertLogs('engine.approval.telegram_bot', level='ERROR') as logs, \
                     redirect_stdout(output), redirect_stderr(output):
                    result = await send_video_notification({})
                constructor.assert_not_called()
                self.assertFalse(result['success'])
                self.assertEqual(result['error_type'], 'ValueError')
                self.assertEqual(result['error'], 'TELEGRAM_BOT_TOKEN contains whitespace/control characters; update the GitHub Actions secret.')
                combined = repr(result) + '\n'.join(logs.output) + output.getvalue()
                self.assertNotIn('FAKE_', combined)
                self.assertNotIn('TOKEN_FOR_TEST', combined)

    async def test_blank_token_is_rejected(self):
        for token in ('', ' \n\t '):
            with self.subTest(token=repr(token)), \
                 patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': token, 'TELEGRAM_CHAT_ID': '123'}), \
                 patch('engine.approval.telegram_bot.Bot') as constructor, \
                 self.assertLogs('engine.approval.telegram_bot', level='ERROR'):
                result = await send_video_notification({})
            constructor.assert_not_called()
            self.assertFalse(result['success'])
            self.assertIn('required', result['error'])

    async def test_sdk_error_token_is_redacted_from_result_and_logs(self):
        token = '123456:FAKE_TOKEN_FOR_TEST'
        output = io.StringIO()
        with self.assertLogs('engine.approval.telegram_bot', level='ERROR') as logs, \
             redirect_stdout(output), redirect_stderr(output):
            result, _, _ = await self.notify({'success': False, 'error': 'fixture'},
                RuntimeError('Request failed: https://api.telegram.org/bot' + token + '/getMe'),
                token='  ' + token + '\n')
        self.assertFalse(result['success'])
        combined = repr(result) + '\n'.join(logs.output) + output.getvalue()
        self.assertNotIn(token, combined)
        self.assertIn('[REDACTED]', result['error'])

    async def test_missing_config_is_a_notification_failure(self):
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "123"}, clear=True), \
             self.assertLogs("engine.approval.telegram_bot", level="ERROR"):
            result = await send_video_notification({})
        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
