"""Dedicated container process; existing worker/leases, bounded shutdown and heartbeat."""
import json
import os
import signal
import time
from threading import Event

from app.core.config import get_settings
from app.db.session import engine
from app.services.automatic_indexing import AutomaticIndexing


def main():
    settings = get_settings()
    if not settings.embedding_worker_enabled:
        raise SystemExit('Worker disabled; refusing a misleading healthy process')
    stop = Event()
    for code in (signal.SIGINT, signal.SIGTERM):
        signal.signal(code, lambda *args: stop.set())
    worker = AutomaticIndexing(engine, settings)
    path = settings.worker_status_path
    path.parent.mkdir(parents=True, exist_ok=True)
    worker.start()
    try:
        while not stop.is_set():
            state = {'status': worker.status, 'updated_at': time.time()}
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state))
            os.replace(temporary, path)
            if worker.status == 'error' or (worker.thread and not worker.thread.is_alive()):
                raise RuntimeError('Worker stopped; pending work retained')
            stop.wait(5)
    finally:
        worker.stop()
        path.write_text(json.dumps({'status': 'stopped', 'updated_at': time.time()}))


if __name__ == '__main__':
    main()
