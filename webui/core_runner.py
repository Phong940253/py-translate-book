"""Runs a translation job in a background thread and forwards logs/events.

The thread calls ``translator.job.run_translation`` (the same code path as the
CLI) with a ``progress_cb`` that updates the Job in the registry. A logging
handler captures root-logger records into the job's log buffer so the web UI
can tail ``translation.log`` without file-watching.
"""

import logging
import threading

from translator.job import run_translation, read_config


class _StopRequested(Exception):
    pass


class JobLogHandler(logging.Handler):
    def __init__(self, job, registry):
        super().__init__()
        self.job = job
        self.registry = registry
        self.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        )

    def emit(self, record):
        try:
            self.registry.append_log(self.job, self.format(record))
        except Exception:  # noqa: BLE001
            pass


def _run(registry, job):
    job.status = "running"
    registry.save(job)

    handler = JobLogHandler(job, registry)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        params = dict(job.params)
        config_path = params.pop("config_path", None)
        config = read_config(config_path) if config_path else {}

        def cb(event, data):
            if job.stop_requested:
                raise _StopRequested()
            registry.record_event(job, event, data)

        stats = run_translation(config, progress_cb=cb, **params)
        job.result = stats
        job.status = "stopped" if job.stop_requested else "done"
    except _StopRequested:
        job.status = "stopped"
    except Exception as exc:  # noqa: BLE001
        job.error = str(exc)
        job.status = "error"
    finally:
        root.removeHandler(handler)
        registry.save(job)


def start_job(registry, job) -> threading.Thread:
    t = threading.Thread(target=_run, args=(registry, job), daemon=True)
    t.start()
    return t
