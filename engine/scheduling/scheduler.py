"""Four daily IST jobs and conservative hourly cleanup; no startup backlog."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_SCHEDULER_STARTED
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from config.settings import (SCHEDULER_TIMEZONE, BATCH_GENERATION_TIME,
                             MISFIRE_GRACE_SECONDS, UPLOAD_TIMES, CLEANUP_MINUTE)
from engine.scheduling.daily_queue import DailyQueue

logger = logging.getLogger(__name__)


def create_scheduler(now=None, queue=None):
    timezone = ZoneInfo(SCHEDULER_TIMEZONE)
    now = now or datetime.now(timezone)
    if now.tzinfo is None:
        raise ValueError("Scheduler time must be timezone-aware")
    now = now.astimezone(timezone)
    queue = queue or DailyQueue()
    scheduler = BlockingScheduler(timezone=timezone)
    jobs = [("generate_daily", BATCH_GENERATION_TIME, queue.generate_batch, [])]
    jobs.extend((f"upload_{slot.replace(':', '')}", slot, queue.upload_slot, [slot]) for slot in UPLOAD_TIMES)
    for job_id, clock, function, args in jobs:
        hour, minute = map(int, clock.split(":"))
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Memory job stores do not recover missed jobs on restart by themselves.
        # Explicitly recover ONLY today's occurrence within the 15-minute window.
        options = {}
        if 0 <= (now - scheduled).total_seconds() <= MISFIRE_GRACE_SECONDS:
            options["next_run_time"] = scheduled
        scheduler.add_job(
            function, CronTrigger(hour=hour, minute=minute, timezone=timezone),
            args=args, id=job_id, max_instances=1, coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS, **options,
        )

    scheduler.add_job(
        queue.cleanup_recent, CronTrigger(minute=CLEANUP_MINUTE, timezone=timezone),
        id="cleanup_hourly", max_instances=1, coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )

    def log_jobs(event):
        logger.info("Scheduler started: timezone=%s, misfire grace=%ss", SCHEDULER_TIMEZONE, MISFIRE_GRACE_SECONDS)
        for job in scheduler.get_jobs():
            logger.info("%s next execution: %s", job.id, job.next_run_time)
    scheduler.add_listener(log_jobs, EVENT_SCHEDULER_STARTED)
    return scheduler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    scheduler = create_scheduler()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
