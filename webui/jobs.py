"""In-memory + on-disk registry of translation jobs.

Logs are appended to ``<id>.log`` (text) while metadata (status, params,
progress, result, error) lives in ``<id>.json`` so jobs survive a server
reload. A small per-job lock guards the in-memory log buffer.
"""

import json
import os
import threading
import time
import uuid

JOB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs")


class Job:
    def __init__(self, job_id: str, params: dict):
        self.id = job_id
        self.created_at = time.time()
        self.status = "queued"  # queued | running | done | error | stopped
        self.params = params
        self.log_lines: list[str] = []
        self.events: list = []
        self.progress: dict = {}
        self.result = None
        self.error = None
        self.stop_requested = False
        self._lock = threading.Lock()

    def meta(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "status": self.status,
            "params": self.params,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "stop_requested": self.stop_requested,
        }


class JobRegistry:
    def __init__(self, job_dir: str = JOB_DIR):
        self.job_dir = job_dir
        os.makedirs(job_dir, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._load_all()

    def _meta_path(self, jid: str) -> str:
        return os.path.join(self.job_dir, jid + ".json")

    def _log_path(self, jid: str) -> str:
        return os.path.join(self.job_dir, jid + ".log")

    def _load_all(self) -> None:
        for fn in os.listdir(self.job_dir):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.job_dir, fn), "r", encoding="utf-8") as f:
                    m = json.load(f)
                job = Job(m["id"], m.get("params", {}))
                job.created_at = m.get("created_at", job.created_at)
                job.status = m.get("status", "queued")
                job.progress = m.get("progress", {}) or {}
                job.result = m.get("result")
                job.error = m.get("error")
                job.stop_requested = m.get("stop_requested", False)
                self.jobs[job.id] = job
            except Exception:  # noqa: BLE001
                pass

    def create(self, params: dict) -> Job:
        jid = uuid.uuid4().hex[:8]
        job = Job(jid, params)
        with self._lock:
            self.jobs[jid] = job
        self.save(job)
        return job

    def get(self, jid: str):
        with self._lock:
            return self.jobs.get(jid)

    def all(self) -> list:
        with self._lock:
            return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)

    def save(self, job: Job) -> None:
        try:
            with open(self._meta_path(job.id), "w", encoding="utf-8") as f:
                json.dump(job.meta(), f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    def append_log(self, job: Job, line: str) -> None:
        with job._lock:
            job.log_lines.append(line)
            if len(job.log_lines) > 3000:
                job.log_lines.pop(0)
        try:
            with open(self._log_path(job.id), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

    def record_event(self, job: Job, event: str, data: dict) -> None:
        with job._lock:
            job.events.append((event, data))
            if isinstance(job.progress, dict):
                if event == "job_started":
                    job.progress = {
                        "total_chapters": data.get("total_chapters"),
                        "start": data.get("start"),
                        "end": data.get("end"),
                        "effective_start": data.get("effective_start"),
                        "current_chapter": None,
                    }
                elif event == "chapter_start":
                    job.progress["current_chapter"] = data.get("chapter_number")
                    job.progress["current_title"] = data.get("title")
                    job.progress["index"] = data.get("index")
                    job.progress["total"] = data.get("total")
                elif event == "chapter_done":
                    job.progress["last_completed"] = data.get("chapter_number")
                elif event == "job_done":
                    job.progress["done"] = True
        self.save(job)

    def request_stop(self, job: Job) -> None:
        job.stop_requested = True
        self.save(job)
