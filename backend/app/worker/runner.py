"""Worker process: runs queued jobs, schedules creator scans and watches the inbox folder.

    python -m app.worker.runner            # run forever
    python -m app.worker.runner --once     # process the queue until empty, then exit (tests/cron)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import time
import traceback

from app.config import get_settings
from app.db import SessionLocal
from app.models import Job, JobType
from app.services import queue
from app.services.discovery import due_creators
from app.services.media import scan_inbox
from app.services.queue import JobCancelled, JobContext, JobWaiting, PermanentJobError
from app.services.settings_store import load_settings
from app.worker.tasks import HANDLERS

log = logging.getLogger("viralclip.worker")


class Worker:
    def __init__(self, worker_id: str | None = None):
        s = get_settings()
        self.worker_id = worker_id or s.worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.poll = s.worker_poll_seconds
        self.stopping = False
        self._last_schedule = 0.0
        self._last_inbox = 0.0
        self._last_recover = 0.0

    def stop(self, *_: object) -> None:
        log.info("Stopping after the current job...")
        self.stopping = True

    # periodic duties -----------------------------------------------------------------------
    def schedule_scans(self) -> int:
        with SessionLocal() as db:
            rs = load_settings(db)
            n = 0
            for creator in due_creators(db, rs):
                queue.enqueue(
                    db, JobType.SCAN_CREATOR, creator_id=creator.id, priority=70,
                    title=f"Scan {creator.name}", commit=False,
                )
                n += 1
            db.commit()
            return n

    def periodic(self) -> None:
        now = time.monotonic()
        s = get_settings()
        if now - self._last_schedule >= 60:
            self._last_schedule = now
            try:
                n = self.schedule_scans()
                if n:
                    log.info("Scheduled %s creator scans", n)
            except Exception:
                log.exception("Scan scheduling failed")
        if now - self._last_inbox >= s.inbox_scan_seconds:
            self._last_inbox = now
            try:
                with SessionLocal() as db:
                    touched = scan_inbox(db)
                if touched:
                    log.info("Imported inbox media for videos %s", touched)
            except Exception:
                log.exception("Inbox scan failed")
        if now - self._last_recover >= 300:
            self._last_recover = now
            with SessionLocal() as db:
                n = queue.recover_stale(db, s.job_stale_minutes)
                if n:
                    log.warning("Re-queued %s stale jobs", n)

    # jobs ---------------------------------------------------------------------------------------
    def run_job(self, job: Job) -> None:
        handler = HANDLERS.get(job.type)
        ctx = JobContext(job)
        log.info("Job %s (%s) started: %s", job.id, job.type, job.title)
        if handler is None:
            queue.fail(job.id, f"Onbekend jobtype {job.type}")
            return
        try:
            message = handler(ctx)
        except JobWaiting as w:
            queue.mark_waiting(job.id, w.message)
            log.info("Job %s waiting: %s", job.id, w.message)
        except JobCancelled:
            log.info("Job %s cancelled", job.id)
        except PermanentJobError as e:
            log.error("Job %s failed permanently: %s", job.id, e)
            queue.fail(job.id, str(e), retry=False)
        except Exception as e:
            log.error("Job %s failed: %s", job.id, e)
            queue.fail(job.id, f"{e}\n\n{traceback.format_exc(limit=6)}")
        else:
            queue.complete(job.id, message)
            log.info("Job %s done: %s", job.id, message)

    def run_once(self) -> bool:
        with SessionLocal() as db:
            job = queue.claim_next(db, self.worker_id)
        if job is None:
            return False
        self.run_job(job)
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log.info("Worker %s started", self.worker_id)
        while not self.stopping:
            self.periodic()
            if not self.run_once():
                time.sleep(self.poll)
        log.info("Worker %s stopped", self.worker_id)

    def drain(self, max_jobs: int = 1000) -> int:
        n = 0
        while n < max_jobs and self.run_once():
            n += 1
        return n


def main() -> None:
    parser = argparse.ArgumentParser(description="ViralClip AI worker")
    parser.add_argument("--once", action="store_true", help="process queued jobs, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from app.migrate import upgrade_db

    upgrade_db()
    worker = Worker()
    if args.once:
        worker.periodic()
        n = worker.drain()
        log.info("Processed %s jobs", n)
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()
