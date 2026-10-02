import os
from datetime import datetime, timezone
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

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
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

    scope_upgrade = allow_interactive and creds and not creds.has_scopes(SCOPES)
    if not creds or not creds.valid or (allow_interactive and not creds.refresh_token) or scope_upgrade:
        if creds and creds.expired and creds.refresh_token and not scope_upgrade:
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


class UploadNotStarted(RuntimeError):
    """Proven failure before constructing or sending an insert request."""


class PublicationExpired(UploadNotStarted):
    """The intended slot elapsed before any insertion request was made."""


def upload_video(video_path, title, description, tags, *, publish_at=None):
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

    status = {"privacyStatus": YOUTUBE_PRIVACY_STATUS, "selfDeclaredMadeForKids": False}
    if publish_at is not None:
        try:
            validate_future_publish_at(publish_at)
        except ValueError:
            raise PublicationExpired("Publication time invalid or expired before authentication; slot retained") from None
        status.update(privacyStatus="private", publishAt=publish_at)
    try:
        youtube = get_authenticated_service()
    except Exception:
        if publish_at is not None:
            raise UploadNotStarted("Scheduled upload authentication failed before insertion") from None
        raise
    logger.info("YouTube resumable upload started (%s privacy)", YOUTUBE_PRIVACY_STATUS)
    media = MediaFileUpload(video_path, mimetype="video/mp4", chunksize=UPLOAD_CHUNK_SIZE, resumable=True)
    try:
        if publish_at is not None:
            try:
                validate_future_publish_at(publish_at)
            except ValueError:
                raise PublicationExpired("Publication time expired before insertion; slot retained") from None
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": tags,
                    "categoryId": "22",
                },
                "status": status,
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


def validate_future_publish_at(value, now=None):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None or stamp <= (now or datetime.now(timezone.utc)):
        raise ValueError("Publication time must be timezone-aware and strictly future")
    return stamp


def preparation_preflight(expected_channel):
    # Scope/channel failures occur before claiming an upload; renders remain recoverable.
    with open(YOUTUBE_TOKEN_FILE, "rb") as token:
        creds = pickle.load(token)
    if not creds.has_scopes(SCOPES):
        raise RuntimeError("Preparation requires youtube.force-ssl; reauthorize locally and replace the Actions token secret")
    service = get_authenticated_service()
    try:
        channels = service.channels().list(part="id", mine=True).execute().get("items", [])
        if [c.get("id") for c in channels] != [expected_channel]:
            raise ValueError("Authenticated YouTube channel does not match configured channel")
    finally:
        service.close()


def upload_prepared_video(path, metadata, publish_at):
    response = upload_video(path, metadata["Title"], metadata["Description"], metadata["Hashtags"],
                            publish_at=publish_at)
    return {"success": True, "status": "uploaded", "video_id": response["id"],
            "video_url": "https://www.youtube.com/watch?v=" + response["id"],
            "privacy_status": "private", "publish_at": publish_at}


def verify_prepared_video(video_id, expected_channel, publish_at):
    validate_future_publish_at(publish_at)
    service = get_authenticated_service()
    try:
        def read():
            values = service.videos().list(id=video_id, part="snippet,status,processingDetails").execute().get("items", [])
            if len(values) != 1 or values[0].get("id") != video_id:
                raise ValueError("Prepared video missing or inaccessible; never reupload blindly")
            video = values[0]
            if video.get("snippet", {}).get("channelId") != expected_channel:
                raise ValueError("Prepared video belongs to another channel")
            status = video.get("status", {})
            if status.get("privacyStatus") != "private" or status.get("uploadStatus") in {"failed", "rejected", "deleted"}:
                raise ValueError("Prepared video is public, unavailable or rejected; repair refused")
            return video
        video = read()
        status = video["status"]
        actual = status.get("publishAt")
        matches = actual and datetime.fromisoformat(actual.replace("Z", "+00:00")) == validate_future_publish_at(publish_at)
        if not matches:
            # A formerly public private video is rejected by YouTube's publishAt semantics.
            # Preserve other mutable status fields rather than clearing audience/license settings.
            mutable = {key: status[key] for key in ("license", "embeddable", "publicStatsViewable",
                       "selfDeclaredMadeForKids", "containsSyntheticMedia") if key in status}
            mutable.update(privacyStatus="private", publishAt=publish_at)
            validate_future_publish_at(publish_at)
            service.videos().update(part="status", body={"id": video_id, "status": mutable}).execute()
            video = read()
            actual = video["status"].get("publishAt")
        matches = bool(actual and datetime.fromisoformat(actual.replace("Z", "+00:00")) == validate_future_publish_at(publish_at))
        return {"verified": matches and video.get("processingDetails", {}).get("processingStatus") == "succeeded",
                "video_id": video_id, "channel_id": expected_channel, "privacy_status": "private",
                "publish_at": actual, "processing_status": video.get("processingDetails", {}).get("processingStatus"),
                "checked_at": datetime.now(timezone.utc).isoformat()}
    finally:
        service.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Authorize the existing YouTube uploader; does not upload a video.")
    parser.add_argument("--authorize", action="store_true", required=True)
    parser.parse_args()
    service = get_authenticated_service(allow_interactive=True)
    service.close()
    print("YouTube authorization complete; token.pickle saved for subsequent uploads.")
