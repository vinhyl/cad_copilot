"""R5 job/progress async protocol -- in-memory job manager.

Long-running plugin tasks (FEA solve, Blender render) run as JOBS:

  * ``submit(kind, fn)`` returns a job id immediately; ``fn(ctx)`` runs on
    a daemon worker thread;
  * ``ctx.report(phase, percent, detail)`` streams progress into the job
    record (percent=None -> indeterminate, the UI shows a spinner);
  * ``ctx.should_cancel()`` + ``ctx.on_terminate(hook)`` let the running
    code kill its subprocess cooperatively (the hook is also invoked by
    cancel() as a backstop);
  * lifecycle: queued -> running -> done | error | cancelled.

No persistence (R12: workspace data stays out of git; job history is
ephemeral by design) and a capped history: the newest ``max_finished``
finished jobs are kept, older ones are GC'd.

The service exposes this over ``GET /api/jobs/{id}``,
``POST /api/jobs/{id}/cancel`` and ``GET /api/jobs``; the frontend polls
(WebSocket push can reuse the same snapshots later).
"""
from __future__ import annotations

import secrets
import threading
import time

ACTIVE = ("queued", "running")
FINISHED = ("done", "error", "cancelled")


class _JobCtx:
    """Handle the running function uses to report progress / poll cancel."""

    def __init__(self, job: dict, lock: threading.Lock):
        self._job = job
        self._lock = lock

    def report(self, phase: str, percent: float | None = None,
               detail: str | None = None) -> None:
        with self._lock:
            self._job["progress"] = {
                "phase": str(phase), "percent": percent, "detail": detail}

    def should_cancel(self) -> bool:
        with self._lock:
            return bool(self._job["cancel_requested"])

    def on_terminate(self, hook) -> None:
        """Register a callable invoked by cancel() (e.g. proc.terminate)."""
        with self._lock:
            self._job["terminate_hook"] = hook


class JobManager:
    """Thread-safe job registry with progress + cooperative cancel."""

    def __init__(self, max_finished: int = 50):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._max_finished = max(1, int(max_finished))

    # ---------------------------------------------------------------- api

    def submit(self, kind: str, fn, meta: dict | None = None) -> str:
        """Run fn(ctx) on a worker thread; returns the job id immediately."""
        jid = secrets.token_hex(8)
        job = {
            "id": jid,
            "kind": str(kind),
            "status": "queued",
            "progress": {"phase": "queued", "percent": None, "detail": None},
            "meta": meta or {},
            "result": None,
            "error": None,
            "error_kind": None,
            "created": _now(),
            "started": None,
            "finished": None,
            "cancel_requested": False,
            "terminate_hook": None,
        }
        with self._lock:
            self._jobs[jid] = job
        threading.Thread(target=self._run, args=(job, fn),
                         daemon=True, name=f"job-{jid}").start()
        return jid

    def get(self, jid: str) -> dict | None:
        """Snapshot of the job record (safe to serialize)."""
        with self._lock:
            job = self._jobs.get(jid)
            return _snapshot(job) if job else None

    def list(self) -> list[dict]:
        with self._lock:
            return [_snapshot(j) for j in self._jobs.values()]

    def cancel(self, jid: str) -> dict | None:
        """Request cancellation. Returns the snapshot, or None if unknown.

        The terminate hook (if registered) fires immediately so a stuck
        subprocess dies even if its polling loop is blocked.
        """
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return None
            job["cancel_requested"] = True
            hook = job["terminate_hook"]
            active = job["status"] in ACTIVE
        if hook is not None and active:
            try:
                hook()
            except Exception:  # noqa: BLE001 -- best-effort backstop
                pass
        return _snapshot(job)

    def wait(self, jid: str, timeout_s: float = 30.0) -> dict | None:
        """Block until the job finishes (test/CLI convenience)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            snap = self.get(jid)
            if snap is None or snap["status"] in FINISHED:
                return snap
            time.sleep(0.02)
        return self.get(jid)

    # ------------------------------------------------------------ internal

    def _run(self, job: dict, fn) -> None:
        with self._lock:
            job["status"] = "running"
            job["started"] = _now()
            job["progress"] = {"phase": "running", "percent": None,
                               "detail": None}
        try:
            result = fn(_JobCtx(job, self._lock))
        except Exception as e:  # noqa: BLE001 -- job errors never kill workers
            with self._lock:
                job["status"] = "cancelled" if getattr(e, "kind", None) == \
                    "cancelled" else "error"
                job["error"] = str(e)
                job["error_kind"] = getattr(e, "kind", None)
                job["finished"] = _now()
                job["terminate_hook"] = None
            self._gc()
            return
        with self._lock:
            # a cancel that raced the natural finish still wins (the user
            # asked for it; a half-killed subprocess result is not trusted)
            if job["cancel_requested"]:
                job["status"] = "cancelled"
                job["error"] = "已取消"
                job["error_kind"] = "cancelled"
            else:
                job["status"] = "done"
                job["result"] = result
                job["progress"] = {"phase": "done", "percent": 100,
                                   "detail": None}
            job["finished"] = _now()
            job["terminate_hook"] = None
        self._gc()

    def _gc(self) -> None:
        """Keep at most max_finished finished jobs (drop oldest)."""
        with self._lock:
            finished = sorted(
                (j for j in self._jobs.values() if j["status"] in FINISHED),
                key=lambda j: j["finished"] or "")
            for j in finished[:-self._max_finished]:
                self._jobs.pop(j["id"], None)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _snapshot(job: dict) -> dict:
    """Copy without the unpicklable terminate_hook."""
    snap = dict(job)
    snap["terminate_hook"] = None
    return snap
