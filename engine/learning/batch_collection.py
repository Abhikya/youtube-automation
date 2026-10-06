"""Explicit, channel-scoped collection from stored publications; no startup I/O."""
import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import json

from engine.learning.metric_collector import (
    MetricCollector, YouTubeStatisticsProvider, authenticated_channel_preflight,
)


def collect_publications(repository, provider, *, persist=False, page_size=100, clock=None):
    """Eligible means a stored channel-owned publication with a valid video ID.

    Private/unlisted records and unknown publication times remain observable;
    explicitly future publication times are skipped. No channel discovery/import.
    """
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("page_size must be from 1 to 1000")
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    collector = MetricCollector(repository, provider, clock=clock)
    rows = []
    seen = set()
    offset = 0
    # Read the inventory before collecting; pagination failure never fabricates a
    # complete inventory or starts a partially enumerated batch.
    publications = []
    while True:
        page = repository.list_published_videos(limit=page_size, offset=offset)
        publications.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
    for publication in publications:
        identifier = publication.youtube_video_id
        if identifier in seen:
            rows.append({"video_id": identifier, "status": "skipped", "reason": "DuplicatePublication"})
            continue
        seen.add(identifier)
        try:
            publication.require_channel(repository.channel_context_id)
        except ValueError:
            rows.append({"video_id": identifier, "status": "failed", "error_type": "WrongChannel"})
            continue
        if publication.published_at is not None and publication.published_at > now:
            rows.append({"video_id": identifier, "status": "skipped", "reason": "FuturePublication"})
            continue
        outcome = collector.collect(repository.channel_context_id, identifier, persist=persist)
        row = {"video_id": identifier}
        if outcome["success"]:
            row["status"] = "skipped" if outcome["action"] == "SKIP_ALREADY_EXISTS" else "collected"
            row["action"] = outcome["action"]
            snapshot = outcome["snapshot"]
            row.update(snapshot_id=snapshot.snapshot_id, observed_at=snapshot.collected_at.isoformat(),
                       metrics=snapshot.metrics, collection_status=snapshot.collection_status)
        else:
            row.update(status="retryable" if outcome.get("retryable") else "failed",
                       error_type=outcome["error_type"])
        rows.append(row)
    totals = {state: sum(r["status"] == state for r in rows)
              for state in ("collected", "skipped", "failed", "retryable")}
    return {"channel_context_id": repository.channel_context_id, "mode": "PERSIST" if persist else "PREVIEW",
            "total": len(rows), "totals": totals, "results": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-context-id", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--persist", action="store_true", help="Explicitly insert append-only observations")
    mode.add_argument("--dry-run", action="store_true", help="Default: real API observations, no database writes")
    args = parser.parse_args(argv)
    service = None
    try:
        from engine.learning.database import connect_database
        from engine.learning.repositories import LearningRepository
        from engine.uploader.youtube_uploader import get_authenticated_service
        with connect_database() as connection:
            context = nullcontext() if args.persist else connection.transaction(force_rollback=True)
            with context:
                if not args.persist:
                    connection.execute("SET TRANSACTION READ ONLY")
                repository = LearningRepository(connection, args.channel_context_id)
                profile = repository.get_profile()
                if profile is None:
                    raise ValueError("Profile missing")
                preflight = authenticated_channel_preflight(profile)
                if not preflight["collection_authorized"]:
                    print(json.dumps({"success": False, "preflight": preflight["status"]}))
                    return 1
                service = get_authenticated_service(allow_interactive=False)
                report = collect_publications(repository, YouTubeStatisticsProvider(service), persist=args.persist)
        print(json.dumps(report, sort_keys=True))
        return 1 if report["totals"]["failed"] or report["totals"]["retryable"] else 0
    except Exception:
        print(json.dumps({"success": False, "error_type": "BatchCollectionFailed"}))
        return 1
    finally:
        if service is not None:
            try:
                service.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
