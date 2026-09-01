"""Standalone background worker process for SJ88 SubCut Studio."""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from backend.config import (  # noqa: E402
    DB_PATH,
    WORKER_MAX_CONCURRENCY,
    WORKER_POLL_INTERVAL,
)
from backend.services.job_store import JobStore  # noqa: E402
from backend.services.subcut_worker import SubCutWorker  # noqa: E402


def main() -> int:
    """Start worker threads and block until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("sj88.subcut.worker")
    store = JobStore(DB_PATH)
    recovered = store.recover_expired_leases()
    if recovered:
        logger.warning("Recovered %d expired lease(s)", len(recovered))

    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    workers = [
        SubCutWorker(
            store,
            poll_interval=WORKER_POLL_INTERVAL,
            worker_id=f"subcut-worker:{index + 1}",
        )
        for index in range(max(1, WORKER_MAX_CONCURRENCY))
    ]
    for worker in workers:
        worker.start()
    logger.info("Started %d worker(s)", len(workers))

    try:
        while not stopping:
            if any(not worker.is_alive() for worker in workers):
                logger.error("A worker thread exited unexpectedly")
                return 1
            time.sleep(0.5)
    finally:
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(timeout=15)
        logger.info("Workers stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
