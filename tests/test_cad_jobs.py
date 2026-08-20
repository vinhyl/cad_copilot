"""R5 -- cad_jobs.JobManager tests (lifecycle / progress / cancel / GC)."""
from __future__ import annotations

import threading
import time

import cad_jobs


def test_submit_runs_and_completes():
    mgr = cad_jobs.JobManager()
    jid = mgr.submit("fea", lambda ctx: {"answer": 42})
    snap = mgr.wait(jid, timeout_s=5)
    assert snap["status"] == "done"
    assert snap["result"] == {"answer": 42}
    assert snap["progress"]["percent"] == 100
    assert snap["kind"] == "fea"


def test_error_captured_with_kind():
    mgr = cad_jobs.JobManager()

    def boom(ctx):
        raise RuntimeError("solver exploded")

    jid = mgr.submit("fea", boom)
    snap = mgr.wait(jid, timeout_s=5)
    assert snap["status"] == "error"
    assert "solver exploded" in snap["error"]
    assert snap["error_kind"] is None


def test_cancelled_exception_kind_maps_to_cancelled():
    class FakePluginError(RuntimeError):
        kind = "cancelled"

    mgr = cad_jobs.JobManager()

    def boom(ctx):
        raise FakePluginError("已取消")

    jid = mgr.submit("render", boom)
    snap = mgr.wait(jid, timeout_s=5)
    assert snap["status"] == "cancelled"
    assert snap["error_kind"] == "cancelled"


def test_progress_reporting_visible_in_snapshot():
    mgr = cad_jobs.JobManager()

    def work(ctx):
        ctx.report("solve", 75, "CalculiX 迭代中")
        return {"ok": True}

    jid = mgr.submit("fea", work)
    mgr.wait(jid, timeout_s=5)
    # final snapshot shows done/100 (last report wins before completion)
    assert mgr.get(jid)["status"] == "done"


def test_cooperative_cancel_wins_over_result():
    mgr = cad_jobs.JobManager()

    def work(ctx):
        for _ in range(200):          # ~2s max
            if ctx.should_cancel():
                return {"partial": True}   # raced a natural finish
            time.sleep(0.01)
        return {"full": True}

    jid = mgr.submit("fea", work)
    time.sleep(0.05)
    snap = mgr.cancel(jid)
    assert snap["cancel_requested"] is True
    final = mgr.wait(jid, timeout_s=5)
    # user asked for cancel -> cancelled even though fn returned a result
    assert final["status"] == "cancelled"
    assert final["error_kind"] == "cancelled"


def test_cancel_fires_terminate_hook():
    mgr = cad_jobs.JobManager()
    fired = threading.Event()

    def work(ctx):
        ctx.on_terminate(fired.set)
        while not ctx.should_cancel():
            time.sleep(0.01)
        raise RuntimeError("killed by hook")

    jid = mgr.submit("render", work)
    time.sleep(0.05)
    mgr.cancel(jid)
    assert fired.wait(timeout=2)       # backstop hook fired immediately
    snap = mgr.wait(jid, timeout_s=5)
    assert snap["status"] == "error"


def test_cancel_unknown_job_returns_none():
    mgr = cad_jobs.JobManager()
    assert mgr.cancel("deadbeef") is None
    assert mgr.get("deadbeef") is None


def test_cancel_finished_job_is_idempotent():
    mgr = cad_jobs.JobManager()
    jid = mgr.submit("fea", lambda ctx: 1)
    mgr.wait(jid, timeout_s=5)
    snap = mgr.cancel(jid)
    assert snap["status"] == "done"    # no hook -> status untouched


def test_snapshot_has_no_terminate_hook():
    mgr = cad_jobs.JobManager()

    def work(ctx):
        ctx.on_terminate(lambda: None)
        return {}

    jid = mgr.submit("fea", work)
    snap = mgr.wait(jid, timeout_s=5)
    assert "terminate_hook" not in snap or snap["terminate_hook"] is None


def test_list_and_gc_keeps_newest_finished():
    mgr = cad_jobs.JobManager(max_finished=2)
    for i in range(4):
        jid = mgr.submit("fea", lambda ctx, i=i: i)
        mgr.wait(jid, timeout_s=5)
        time.sleep(0.02)   # distinct finished timestamps
    jobs = mgr.list()
    assert len(jobs) == 2          # GC kept only the newest finished ones
    assert all(j["status"] == "done" for j in jobs)
    assert [j["result"] for j in jobs] == [2, 3]
