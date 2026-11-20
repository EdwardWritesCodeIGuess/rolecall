"""Stage runner shared by the n8n API (/api/v1/run/<stage>) and the built-in worker.

Stages
  watch    find new finished VODs on authorized channels + scan the inbox folder, ingest them
  process  cut & edit the next new source: transcribe, find moments, 9:16 render, checks, copy
  publish  post whatever is approved and due (caps, spacing, posting window, retries)

watch and process share one "heavy" lock so the machine never transcodes two things at once;
publish has its own lock, so posting keeps going while a long VOD is being processed.

Who drives: setting `orchestrator` = auto | n8n | builtin. With auto, n8n leads while it keeps
calling /api/v1/run/*; if it goes quiet for `n8n_grace_minutes` the built-in worker takes over,
so a dead n8n never stalls the channel.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from . import db, events, pipeline, watcher

_locks = {"heavy": threading.Lock(), "publish": threading.Lock()}
_running: dict[str, dict] = {}
_jobs: OrderedDict[str, dict] = OrderedDict()
_jobs_guard = threading.Lock()
MAX_JOBS = 100


class Busy(Exception):
    def __init__(self, job: dict | None):
        super().__init__("busy")
        self.job = job


# ------------------------------------------------------------------ stages
def stage_watch(creator: str | None = None, **_) -> dict:
    return watcher.watch(creator)


def stage_process(source_id: int | None = None, **_) -> dict:
    if source_id is None:
        row = db.q1("SELECT id FROM sources WHERE status='new' ORDER BY id LIMIT 1")
        if not row:
            return {"source_id": None, "clips": [], "message": "nothing new to process"}
        source_id = row["id"]
    clip_ids = pipeline.process_source(int(source_id))
    clips = [events.clip_summary(c) for c in clip_ids]
    return {
        "source_id": source_id,
        "clips": [{k: c.get(k) for k in ("clip_id", "title", "review_status", "duration", "score")}
                  | {"flags": [f["code"] for f in c.get("flags", [])]} for c in clips],
        "needs_review": sum(1 for c in clips if c.get("review_status") == "needs_review"),
        "auto_approved": sum(1 for c in clips if c.get("review_status") == "auto_approved"),
        "remaining": db.q1("SELECT COUNT(*) n FROM sources WHERE status='new'")["n"],
    }


def stage_publish(**_) -> dict:
    from . import scheduler  # scheduler imports this module

    return {"posts": scheduler.publish_due()}


STAGES = {"watch": (stage_watch, "heavy"), "process": (stage_process, "heavy"), "publish": (stage_publish, "publish")}


# ------------------------------------------------------------------ jobs
def public(job: dict | None) -> dict | None:
    if job is None:
        return None
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _remember(job: dict) -> None:
    with _jobs_guard:
        _jobs[job["id"]] = job
        while len(_jobs) > MAX_JOBS:
            _jobs.popitem(last=False)


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def running() -> list[dict]:
    return [public(j) for j in list(_running.values())]


def _execute(job: dict, fn, lock_name: str, kw: dict) -> None:
    try:
        job["result"] = fn(**kw)
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 - reported on the job and as an event
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"[:2000]
        db.log_event("error", "autopilot", f"{job['stage']} failed: {job['error']}")
        events.emit("job.failed", public(job))
    finally:
        job["finished_at"] = db.now_iso()
        job["seconds"] = round(time.time() - job["_t0"], 1)
        _running.pop(lock_name, None)
        _locks[lock_name].release()
        job["_done"].set()


def start(stage: str, by: str = "api", background: bool = True, **kw) -> dict:
    """Start a stage. Raises Busy (with the running job) if its lock is held. Returns the job dict."""
    if stage not in STAGES:
        raise KeyError(stage)
    fn, lock_name = STAGES[stage]
    if not _locks[lock_name].acquire(blocking=False):
        raise Busy(_running.get(lock_name))
    job = {"id": uuid.uuid4().hex[:12], "stage": stage, "by": by, "status": "running", "args": kw,
           "started_at": db.now_iso(), "finished_at": None, "result": None, "error": None,
           "_t0": time.time(), "_done": threading.Event()}
    _running[lock_name] = job
    _remember(job)
    if background:
        threading.Thread(target=_execute, args=(job, fn, lock_name, kw), daemon=True,
                         name=f"rolecall-{stage}").start()
    else:
        _execute(job, fn, lock_name, kw)
    return job


def run_inline(stage: str, **kw) -> dict | None:
    """Built-in worker: run a stage in this thread, or skip quietly if it is already running."""
    try:
        return start(stage, by="worker", background=False, **kw)
    except Busy:
        return None


# ------------------------------------------------------------------ who drives
def note_orchestrator_call() -> None:
    db.set_setting("n8n_last_seen", db.now_iso())


def builtin_should_run(now: datetime | None = None) -> bool:
    mode = db.get_setting("orchestrator") or "auto"
    if mode == "builtin":
        return True
    if mode == "n8n":
        return False
    last = db.get_setting("n8n_last_seen")
    if not last:
        return True
    now = now or datetime.now(timezone.utc)
    seen = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return now - seen > timedelta(minutes=int(db.get_setting("n8n_grace_minutes") or 90))


def driver() -> str:
    return "builtin worker" if builtin_should_run() else "n8n"
