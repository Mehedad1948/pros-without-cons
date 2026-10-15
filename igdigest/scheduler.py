"""APScheduler jobs.

- Check accounts every IGD_CHECK_INTERVAL_HOURS; afterwards write an updates report (no LLM)
  if there are new posts or failed checks.
- Daily LLM digest at IGD_DIGEST_TIME, registered only when an LLM provider is configured.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .collector import run_collection
from .config import Settings
from .db import Database
from .updates import UpdatesBusy, build_updates_report

log = logging.getLogger(__name__)


def scheduled_collect(db: Database, settings: Settings) -> None:
    try:
        summary = run_collection(db, settings, trigger="scheduled")
        log.info("Scheduled collection: %s (%s)", summary.status, summary.message)
    except Exception:
        log.exception("Scheduled collection crashed")
        return
    if settings.updates_enabled and settings.updates_after_check and summary.status != "skipped_overlap":
        try:
            report_id = build_updates_report(db, settings, trigger="scheduled", skip_if_empty=True)
            if report_id:
                log.info("Updates report #%s created", report_id)
        except UpdatesBusy as e:
            log.warning("Updates report skipped: %s", e)
        except Exception:
            log.exception("Updates report crashed")


def scheduled_digest(db: Database, settings: Settings) -> None:
    from .digest import DigestBusy, DigestUnavailable, build_digest

    try:
        if settings.collect_before_digest:
            summary = run_collection(db, settings, trigger="scheduled")
            log.info("Pre-digest collection: %s (%s)", summary.status, summary.message)
        digest_id = build_digest(db, settings, kind="scheduled", trigger="scheduled")
        log.info("Scheduled digest #%s created", digest_id)
    except (DigestBusy, DigestUnavailable) as e:
        log.warning("Scheduled digest skipped: %s", e)
    except Exception:
        log.exception("Scheduled digest crashed")


def create_scheduler(db: Database, settings: Settings) -> BackgroundScheduler:
    tz = settings.tz
    sched = BackgroundScheduler(
        timezone=tz,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    sched.add_job(
        scheduled_collect,
        IntervalTrigger(hours=settings.check_interval_hours, timezone=tz),
        id="collect", name="Check accounts (+ updates report)" if settings.updates_enabled else "Check accounts",
        args=[db, settings],
    )
    if settings.digest_available:
        hour, minute = settings.digest_hour_minute
        sched.add_job(
            scheduled_digest,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            id="digest", name="Daily LLM digest", args=[db, settings],
        )
    else:
        log.info("LLM digest not scheduled (no LLM provider configured or IGD_DIGEST_ENABLED=false)")
    return sched
