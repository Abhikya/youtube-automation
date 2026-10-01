import logging
import asyncio
import os

from dotenv import load_dotenv
from telegram import Bot
from telegram.request import HTTPXRequest

load_dotenv()


async def send_video_notification(video_data):
    """Send a preview after the upload attempt; never upload or request approval."""
    try:
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.")
        if any(char.isspace() or not char.isprintable() for char in token):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN contains whitespace/control characters; "
                "update the GitHub Actions secret."
            )

        metadata = video_data["metadata"]
        upload = video_data["upload"]
        title = metadata["Title"]
        if upload["success"]:
            caption = (
                "Uploaded successfully to YouTube\n\n"
                f"Title: {title}\n"
                f"Privacy: {upload['privacy_status']}\n"
                f"YouTube: {upload['video_url']}"
            )
        else:
            caption = (
                "YouTube upload failed. Video retained locally.\n\n"
                f"Title: {title}\n"
                f"Error: {upload.get('error', 'Unknown upload error')}"
            )

        # Keep captions comfortably below Telegram's limit, including Unicode.
        caption = caption[:450]
        request = HTTPXRequest(
            connect_timeout=20,
            read_timeout=120,
            write_timeout=120,
            pool_timeout=20,
            media_write_timeout=180,
        )
        # A fresh client is initialized and closed within this invocation's loop.
        async with Bot(token=token, request=request) as bot:
            with open(video_data["video_path"], "rb") as video:
                message = await bot.send_video(
                    chat_id=chat_id,
                    video=video,
                    caption=caption,
                    connect_timeout=20,
                    read_timeout=120,
                    write_timeout=180,
                    pool_timeout=20,
                )
        return {"success": True, "status": "sent", "message_id": message.message_id}
    except Exception as exc:
        error = str(exc)
        raw_token = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        for secret in (raw_token, raw_token.strip()):
            if secret:
                error = error.replace(secret, "[REDACTED]")
        # SDK exceptions can contain the request URL, which embeds the token.
        # Log only the sanitized message, without an unsanitized traceback.
        logging.getLogger(__name__).error("Telegram notification failed (%s): %s", type(exc).__name__, error)
        return {
            "success": False,
            "status": "failed",
            "error": error,
            "error_type": type(exc).__name__,
        }


def start_bot():
    """Retain the old launcher without starting an approval listener."""
    print("Telegram previews are sent by the video pipeline; no listener is needed.")


def notify_generated_video(result):
    """Notify about an existing upload outcome without generating or uploading."""
    try:
        return asyncio.run(send_video_notification(result))
    except Exception as exc:
        logging.getLogger(__name__).exception("Telegram notification could not run")
        return {
            "success": False,
            "status": "failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
