"""Runs a translation job in a background worker and forwards logs/events.

The worker runs ``translator.job.run_translation`` (the same code path as the
CLI) with a ``progress_cb`` that updates the Job in the registry. A logging
handler captures root-logger records into the job's log buffer so the web UI can
tail ``translation.log`` without file-watching.

The worker runs in a ``threading.Thread`` by default, or a
``multiprocessing.Process`` when ``PYTB_JOB_PROCESS != "0"``. A Process isolates
a hung/slow engine from the web server and lets Stop call ``terminate()`` for an
instant kill. Because Windows uses ``spawn`` (separate memory per process), the
subprocess worker re-opens the registry from disk and shares state via the job's
JSON / ``.log`` files rather than in-memory objects.
"""

import atexit
import logging
import multiprocessing
import os
import threading

from translator.job import run_translation, read_config
from translator.translator import _TranslationStopped


# Default to a subprocess worker (instant kill via terminate()); set
# PYTB_JOB_PROCESS=0 to fall back to an in-process thread (e.g. for tests that
# monkeypatch build_engine in the parent process).
_USE_PROCESS = os.environ.get("PYTB_JOB_PROCESS", "1") != "0"

# job_id -> multiprocessing.Process (only populated in process mode).
_PROCS: dict[str, "multiprocessing.Process"] = {}


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


def _run(registry, job, params, engine=None, should_stop=None):
    """Worker body. Runs in-process (thread path) or is wrapped by ``_run_proc``
    (subprocess path). ``should_stop`` is consulted cooperatively so a job can
    finish the current chapter cleanly before aborting."""
    should_stop = should_stop or (lambda: job.stop_requested)
    job.status = "running"
    registry.save(job)

    handler = JobLogHandler(job, registry)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        params = dict(params)
        config_path = params.pop("config_path", None)
        config = read_config(config_path) if config_path else {}

        def cb(event, data):
            if should_stop():
                raise _StopRequested()
            registry.record_event(job, event, data)

        stats = run_translation(
            config,
            progress_cb=cb,
            should_stop=should_stop,
            engine_obj=engine,
            **params,
        )
        job.result = stats
        job.status = "stopped" if should_stop() else "done"
    except (_StopRequested, _TranslationStopped):
        job.status = "stopped"
    except Exception as exc:  # noqa: BLE001
        job.error = str(exc)
        job.status = "error"
    finally:
        root.removeHandler(handler)
        registry.save(job)


def _run_proc(job_dir: str, job_id: str, params: dict, engine=None):
    """Subprocess entry point. Re-opens the registry from disk (it cannot share
    the parent's in-memory objects under spawn) and reads the cooperative stop
    flag from the persisted JSON, since ``job.stop_requested`` is not shared
    across processes."""
    from webui.jobs import JobRegistry

    registry = JobRegistry(job_dir, relabel_stale=False)
    job = registry.get(job_id)
    if job is None:
        return
    _run(
        registry,
        job,
        params,
        engine,
        should_stop=lambda: bool(
            (registry.load_meta(job_id) or {}).get("stop_requested")
        ),
    )


def start_job(registry, job, engine=None):
    """Start a translation job. Returns the worker (Thread or Process)."""
    if _USE_PROCESS:
        p = multiprocessing.Process(
            target=_run_proc,
            args=(registry.job_dir, job.id, dict(job.params), engine),
            daemon=True,
        )
        p.start()
        _PROCS[job.id] = p
        return p
    t = threading.Thread(
        target=_run,
        args=(registry, job, dict(job.params), engine),
        daemon=True,
    )
    t.start()
    return t


def request_hard_stop(job_id: str):
    """Force-kill a running subprocess worker (no-op in thread mode)."""
    p = _PROCS.get(job_id)
    if p is not None and p.is_alive():
        p.terminate()
    return p


@atexit.register
def _cleanup_procs():
    for p in _PROCS.values():
        try:
            if p.is_alive():
                p.terminate()
        except Exception:  # noqa: BLE001
            pass
