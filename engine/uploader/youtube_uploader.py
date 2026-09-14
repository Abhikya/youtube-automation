import os
import errno
import http.client
import logging
import socket
import ssl
import time

import httplib2
from googleapiclient.errors import HttpError
import pickle

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config.settings import (
    YOUTUBE_CLIENT_SECRET_FILE,
    YOUTUBE_TOKEN_FILE,
    YOUTUBE_PRIVACY_STATUS,
)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
MAX_UPLOAD_RETRIES = 5
logger = logging.getLogger(__name__)


def is_transient_upload_error(exc):
    if isinstance(exc, HttpError):
        return exc.resp.status in {408, 429, 500, 502, 503, 504}
    if isinstance(exc, (ssl.SSLEOFError, ssl.SSLZeroReturnError, TimeoutError,
                        ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                        http.client.RemoteDisconnected, http.client.IncompleteRead,
                        httplib2.ServerNotFoundError)):
        return True
    if isinstance(exc, socket.gaierror):
        return exc.errno == socket.EAI_AGAIN
    return isinstance(exc, OSError) and (
        exc.errno in {errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT,
                      errno.EPIPE, errno.ENETDOWN, errno.ENETUNREACH,
                      errno.EHOSTUNREACH, errno.EINTR, errno.EAGAIN}
        or getattr(exc, "winerror", None) in {10053, 10054, 10060, 10051, 10065}
    )


def complete_resumable_upload(request):
    """Retry the SAME request/session; five retries total, not per chunk."""
    response = None
    retries = 0
    while response is None:
        try:
            # Disable Google's nested retries so this is the single retry budget.
            status, response = request.next_chunk(num_retries=0)
        except Exception as exc:
            if not is_transient_upload_error(exc):
                logger.error("YouTube upload failed: non-retriable %s", type(exc).__name__)
                raise
            if retries >= MAX_UPLOAD_RETRIES:
                logger.error("YouTube upload failed: retry limit exhausted (%s)", type(exc).__name__)
                raise RuntimeError(
                    f"YouTube resumable upload failed after {MAX_UPLOAD_RETRIES} retries "
                    f"({type(exc).__name__}); inspect the outcome before starting another upload."
                ) from exc
            delay = 2 ** retries
            retries += 1
            logger.warning("YouTube transient upload retry %s/%s in %ss (%s)",
                           retries, MAX_UPLOAD_RETRIES, delay, type(exc).__name__)
            time.sleep(delay)
            continue
        if status is not None:
            logger.info("YouTube upload progress: %.1f%%", status.progress() * 100)
    if not isinstance(response, dict) or not response.get("id"):
        logger.error("YouTube upload failed: no video ID returned")
        raise RuntimeError("YouTube returned no video ID; upload success is unconfirmed.")
    logger.info("YouTube upload completed: video ID %s", response["id"])
    return response


def get_authenticated_service(*, allow_interactive=False):
    creds = None

    if os.path.isfile(YOUTUBE_TOKEN_FILE):
        with open(YOUTUBE_TOKEN_FILE, "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid or (allow_interactive and not creds.refresh_token):
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not allow_interactive:
                raise RuntimeError(
                    "YouTube authorization required. Run: python -m engine.uploader.youtube_uploader --authorize"
                )
            if not os.path.isfile(YOUTUBE_CLIENT_SECRET_FILE):
                raise FileNotFoundError(
                    "YouTube credentials unavailable: provide client_secret.json "
                    "in the project root or a valid token.pickle."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                YOUTUBE_CLIENT_SECRET_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

        with open(YOUTUBE_TOKEN_FILE, "wb") as token:
            pickle.dump(creds, token)

    return build("youtube", "v3", credentials=creds)


def upload_video(video_path, title, description, tags):
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Rendered video not found: {video_path}")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Generated YouTube title is required.")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Generated YouTube description is required.")
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("Generated hashtags must be a list of strings.")
    if YOUTUBE_PRIVACY_STATUS not in {"private", "unlisted", "public"}:
        raise ValueError("YOUTUBE_PRIVACY_STATUS must be private, unlisted, or public.")

    youtube = get_authenticated_service()
    logger.info("YouTube resumable upload started (%s privacy)", YOUTUBE_PRIVACY_STATUS)
    media = MediaFileUpload(video_path, mimetype="video/mp4", chunksize=UPLOAD_CHUNK_SIZE, resumable=True)
    try:
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": tags,
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": YOUTUBE_PRIVACY_STATUS,
                    "selfDeclaredMadeForKids": False,
                },
            },
            media_body=media,
        )
        return complete_resumable_upload(request)
    finally:
        media.stream().close()


def upload_generated_video(video_path, metadata):
    """Upload this render's metadata; retain the local result on upload failure."""
    try:
        response = upload_video(
            video_path=video_path,
            title=metadata["Title"],
            description=metadata["Description"],
            tags=metadata["Hashtags"],
        )
        video_id = response["id"]
        return {
            "success": True,
            "status": "uploaded",
            "video_id": video_id,
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
            "title": metadata["Title"],
            "privacy_status": response.get("status", {}).get(
                "privacyStatus", YOUTUBE_PRIVACY_STATUS
            ),
        }
    except Exception as exc:
        logging.getLogger(__name__).exception("YouTube upload failed for %s", video_path)
        return {
            "success": False,
            "status": "failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "privacy_status": YOUTUBE_PRIVACY_STATUS,
        }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Authorize the existing YouTube uploader; does not upload a video.")
    parser.add_argument("--authorize", action="store_true", required=True)
    parser.parse_args()
    service = get_authenticated_service(allow_interactive=True)
    service.close()
    print("YouTube authorization complete; token.pickle saved for subsequent uploads.")
