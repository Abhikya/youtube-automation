"""Channel-scoped statistics collection; no scheduler or import-time connections."""
import argparse
import copy
from datetime import datetime, timezone
import json
import re
import uuid
import time
from email.utils import parsedate_to_datetime

from engine.learning.schema import MetricSnapshot

SOURCE = "youtube-data-api-v3/videos.list"


class CollectionError(RuntimeError):
    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def video_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise CollectionError("InvalidVideoId")
    return value



def authenticated_channel_preflight(profile, *, service_factory=None):
    """Inspect an existing profile and channels.list(mine=True); never persist.

    Missing/invalid profiles are caller errors. Authentication and lookup failures
    return fixed codes, without exception text or credentials. Caller may collect
    only when collection_authorized is true; this function never collects metrics.
    Existing noninteractive authentication may refresh/save local credentials.
    """
    from engine.learning.schema import ChannelProfile
    if not isinstance(profile, ChannelProfile):
        raise ValueError("A learning ChannelProfile is required")
    result = {"status": "AUTHENTICATION_FAILED", "collection_authorized": False,
              "authenticated_channel_id": None, "stored_channel_id": profile.youtube_channel_id}
    service = None
    try:
        if service_factory is None:
            from engine.uploader.youtube_uploader import get_authenticated_service
            service_factory = get_authenticated_service
        service = service_factory(allow_interactive=False)
    except Exception:
        return result
    try:
        response = service.channels().list(part="id", mine=True, fields="items/id,nextPageToken").execute()
        items = response.get("items") if isinstance(response, dict) else None
        if (not isinstance(items, list) or len(items) != 1 or response.get("nextPageToken")
                or not isinstance(items[0], dict) or not isinstance(items[0].get("id"), str)
                or not items[0]["id"].strip()):
            result["status"] = "CHANNEL_LOOKUP_FAILED"
            return result
        discovered = items[0]["id"]
        result["authenticated_channel_id"] = discovered
        if not profile.youtube_channel_id:
            result["status"] = "MAPPING_REQUIRED"
        elif discovered != profile.youtube_channel_id:
            result["status"] = "MISMATCH"
        else:
            result.update(status="MATCH", collection_authorized=True)
    except Exception:
        result["status"] = "CHANNEL_LOOKUP_FAILED"
    finally:
        if service is not None:
            try:
                service.close()
            except Exception:
                pass
    return result


class YouTubeStatisticsProvider:
    """Borrow the existing authenticated YouTube service. Only videos.list is used."""
    def __init__(self, service, *, max_retries=3, sleep=None, clock=None):
        if type(max_retries) is not int or not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be between zero and five")
        self.service = service
        self.max_retries = max_retries
        self.sleep = sleep or time.sleep
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self, identifier):
        video_id(identifier)
        for attempt in range(self.max_retries + 1):
            try:
                response = self.service.videos().list(
                    part="snippet,statistics", id=identifier,
                    fields="items(id,snippet/channelId,statistics(viewCount,likeCount,commentCount))",
                ).execute(num_retries=0)
                break
            except Exception as exc:
                transient, delay = self._retry_policy(exc, attempt)
                if not transient:
                    raise CollectionError("YouTubeRequestFailed") from None
                # Long server delays are deferred, never shortened or slept through.
                if attempt == self.max_retries or delay > 60:
                    raise CollectionError("YouTubeRetryDeferred", retryable=True) from None
                self.sleep(delay)
        return parse_response(response, identifier)

    def _retry_policy(self, exc, attempt):
        from googleapiclient.errors import HttpError
        transient = isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError))
        headers = {}
        if isinstance(exc, HttpError):
            headers = exc.resp
            transient = exc.resp.status in {408, 429, 500, 502, 503, 504}
            if exc.resp.status == 403:
                try:
                    errors = json.loads(exc.content).get("error", {}).get("errors", [])
                    reasons = {error.get("reason") for error in errors}
                    transient = bool(reasons) and reasons <= {"rateLimitExceeded", "userRateLimitExceeded"}
                except Exception:
                    transient = False
        delay = 2 ** attempt
        value = headers.get("retry-after") or headers.get("Retry-After")
        if value:
            try:
                supplied = float(value) if str(value).isdigit() else (parsedate_to_datetime(value) - self.clock()).total_seconds()
                delay = max(delay, supplied)
            except (ValueError, TypeError, OverflowError):
                pass
        return transient, delay



def parse_response(response, identifier):
    video_id(identifier)
    if not isinstance(response, dict) or not isinstance(response.get("items"), list):
        raise CollectionError("MalformedResponse")
    items = response["items"]
    if not items:
        raise CollectionError("VideoUnavailable")
    if len(items) != 1 or not isinstance(items[0], dict) or items[0].get("id") != identifier:
        raise CollectionError("ResponseVideoMismatch")
    item = items[0]
    snippet = item.get("snippet")
    if not isinstance(snippet, dict) or not isinstance(snippet.get("channelId"), str) or not snippet["channelId"].strip():
        raise CollectionError("MissingResponseChannel")
    stats = item.get("statistics")
    if not isinstance(stats, dict):
        raise CollectionError("MissingStatistics")
    metrics = {}
    for source, target in (("viewCount", "views"), ("likeCount", "likes"), ("commentCount", "comments")):
        value = stats.get(source)
        if value is None:
            metrics[target] = None
        elif type(value) is int and value >= 0:
            metrics[target] = value
        elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            metrics[target] = int(value)
        else:
            raise CollectionError("InvalidMetric")
    return {"youtube_channel_id": snippet["channelId"], "metrics": metrics}


class FakeYouTubeProvider:
    """Offline provider using raw API fixtures or explicit exceptions."""
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, identifier):
        self.calls.append(identifier)
        response = self.responses[identifier]
        if isinstance(response, Exception):
            raise response
        return parse_response(copy.deepcopy(response), identifier)


class MetricCollector:
    def __init__(self, repository, provider, *, clock=None):
        self.repository = repository
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def collect(self, channel_context_id, identifier, *, observed_at=None, persist=True):
        snapshot = None
        try:
            video_id(identifier)
            if channel_context_id != self.repository.channel_context_id:
                raise CollectionError("WrongChannel")
            published = self.repository.get_published_video_by_youtube_id(identifier)
            if published is None:
                raise CollectionError("PublishedVideoNotFound")
            published.require_channel(channel_context_id)
            profile = self.repository.get_profile()
            if profile is None:
                raise CollectionError("ChannelProfileNotFound")
            profile.require_channel(channel_context_id)
            if not profile.youtube_channel_id:
                raise CollectionError("ChannelMappingRequired")
            # An explicit timestamp is for replaying a recorded provider observation,
            # never backdating a newly fetched live response.
            if observed_at is not None and isinstance(self.provider, YouTubeStatisticsProvider):
                raise CollectionError("LiveTimestampOverrideForbidden")
            data = self.provider.fetch(identifier)
            if data["youtube_channel_id"] != profile.youtube_channel_id:
                raise CollectionError("ResponseChannelMismatch")
            stamp = observed_at if observed_at is not None else self.clock()
            if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
                raise CollectionError("NaiveObservationTime")
            stamp = stamp.astimezone(timezone.utc)
            snapshot_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                              f"{SOURCE}/{channel_context_id}/{identifier}/{stamp.isoformat()}"))
            metrics = data["metrics"]
            status = "success" if all(v is not None for v in metrics.values()) else "partial"
            if all(v is None for v in metrics.values()):
                status = "unavailable"
            age = None
            if published.published_at is not None and stamp >= published.published_at:
                age = (stamp - published.published_at).total_seconds()
            snapshot = MetricSnapshot(channel_context_id=channel_context_id,
                provenance={"source": SOURCE, "source_record_id": identifier,
                            "captured_at": stamp, "notes": "Returned snippet.channelId matched the verified channel profile; statistics may lag real activity."},
                snapshot_id=snapshot_id, youtube_video_id=identifier, collected_at=stamp,
                metric_source=SOURCE, observation_window={"kind": "cumulative", "end_at": stamp},
                video_age_seconds=age, metrics=metrics, collection_status=status)
            if not persist:
                return {"video_id": identifier, "success": True, "action": "PREVIEW", "snapshot": snapshot}
            # Repository operations nest as savepoints; a per-video failure does not
            # poison the connection or overwrite any prior observation.
            with self.repository.transaction():
                prior = self.repository.get_snapshot(snapshot_id)
                if prior is not None:
                    prior.require_channel(channel_context_id)
                    if prior != snapshot:
                        raise CollectionError("ObservationConflict")
                    action = "SKIP_ALREADY_EXISTS"
                else:
                    self.repository.insert_snapshot(snapshot)
                    action = "INSERT"
            return {"video_id": identifier, "success": True, "action": action, "snapshot": snapshot}
        except Exception as exc:
            # Never return provider/driver exception text: it may contain secrets.
            return {"video_id": identifier if isinstance(identifier, str) else None,
                    "success": False, "error_type": exc.code if isinstance(exc, CollectionError) else type(exc).__name__,
                    "snapshot": snapshot, "persisted": False,
                    "retryable": isinstance(exc, CollectionError) and exc.retryable}

    def collect_batch(self, channel_context_id, identifiers, *, persist=True):
        return [self.collect(channel_context_id, identifier, persist=persist) for identifier in identifiers]


def main():
    parser = argparse.ArgumentParser(description="Optional read-only YouTube statistics verification; no database writes")
    parser.add_argument("--verify-video", required=True)
    args = parser.parse_args()
    service = None
    try:
        video_id(args.verify_video)
        # Existing authentication can refresh and rewrite expired credentials.
        # Refuse that path in this strictly read-only verification command.
        import pickle
        from config.settings import YOUTUBE_TOKEN_FILE
        with open(YOUTUBE_TOKEN_FILE, "rb") as stream:
            credentials = pickle.load(stream)
        if not credentials.valid:
            raise CollectionError("LiveVerificationUnavailableTokenNotValid")
        from engine.uploader.youtube_uploader import get_authenticated_service
        service = get_authenticated_service(allow_interactive=False)
        data = YouTubeStatisticsProvider(service).fetch(args.verify_video)
        data.update(video_id=args.verify_video, observed_at=datetime.now(timezone.utc).isoformat(),
                    metric_source=SOURCE, persisted=False)
        print(json.dumps(data, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error_type": exc.code if isinstance(exc, CollectionError) else type(exc).__name__, "persisted": False}))
        return 1
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
