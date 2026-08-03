"""Async job registry — run a long solve off the request thread, poll for it.

Pure-Python, FreeCAD-free. The reusable async/long-solve primitive: a tool that
would otherwise block the MCP channel (a CFD or FEM solve, a big Monte-Carlo
sweep, an external slicer) hands a callable to :func:`submit`, gets a ``job_id``
back immediately, and the caller polls :func:`status` / :func:`result`. This
generalizes the worker's existing render-job pattern (``render_photoreal_submit``
+ ``render_job``) into one facility every async tool can share — so there is a
single poll surface (``job_status`` / ``job_result`` / ``job_list``) no matter
which solve produced the job.

THREADING CONTRACT — read before wiring a real solve:
    The submitted callable runs in a **background thread**, so it MUST NOT touch
    FreeCAD (FreeCAD's document API is not thread-safe). Two safe shapes:
      1. pure-Python computation (tolerance Monte-Carlo, a meshless estimate);
      2. *polling an external subprocess* (ccx / OpenFOAM / a slicer CLI) whose
         FreeCAD-side setup + export already ran on the calling thread before
         submit — exactly how the renderer executor backgrounds only its
         subprocess. Read results back on the main thread after the job is done.

Content-hash caching: pass a ``key`` (build it with :func:`content_key`) and a
re-submit of identical work returns the prior completed job instead of recomputing
— the same "don't re-solve an unchanged part" idea the multi-agent lockfile uses.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

# Cap on retained jobs so abandoned results don't accumulate for the worker
# session. Only terminal (done/failed) jobs are evicted; a running job is never
# dropped, and neither is a PINNED one. Oldest-first by submit order.
#
# The cap was 32, which a fan-out study (#227) blows through trivially: a 5x5 grid
# with two responses is 50 children plus a collector, and eviction would drop the
# points that finished FIRST — so the collector's own jobs.result() would raise
# JobNotFound mid-join and the study would lose exactly the cheapest points. Two
# defences, because a cap alone is only ever "big enough until it isn't":
# pin()/unpin() makes a dependency explicit, and the cap is high enough that
# unpinned interactive work is never the reason a study fails.
_MAX_JOBS = 256

_lock = threading.Lock()
_jobs: dict = {}          # job_id -> job record
_cache: dict = {}         # content key -> job_id of a completed, cacheable job
_counter = {"n": 0}


class JobNotFound(KeyError):
    """Raised by status()/result() when no job matches the id."""


# --- helpers ------------------------------------------------------------------

def content_key(kind: str, payload) -> str:
    """Stable content hash of (kind, payload) for cache lookups. Canonical JSON
    (sort_keys) keeps it reproducible across runs; non-JSON values fall back to
    their str(). Returns a short hex digest (same blake2b scheme as the worker's
    geometry tags / lockfile)."""
    blob = json.dumps({"kind": kind, "payload": payload},
                      sort_keys=True, default=str).encode()
    return hashlib.blake2b(blob, digest_size=12).hexdigest()


def _new_id() -> str:
    _counter["n"] += 1
    return f"job_{_counter['n']}"


def _elapsed(job) -> float:
    end = job["t_finish"] if job["t_finish"] is not None else time.monotonic()
    return round(end - job["t_submit"], 3)


def _evict_locked() -> None:
    """Drop oldest terminal jobs while over the cap. Caller holds _lock. Running
    jobs are never evicted (the thread still writes into the record), and neither
    are pinned ones (another job is waiting to read their result)."""
    while len(_jobs) > _MAX_JOBS:
        victim = next(
            (jid for jid, j in _jobs.items()
             if j["status"] != "running" and not j.get("pins")), None
        )
        if victim is None:
            break                                     # all running -> nothing to free
        j = _jobs.pop(victim)
        if j.get("key") and _cache.get(j["key"]) == victim:
            _cache.pop(j["key"], None)


def _run(job_id: str, fn) -> None:
    """Background-thread target: run fn, store result/error. Writes the result
    BEFORE flipping status to done, so a reader that sees 'done' always sees the
    result (no torn read). Captures BaseException so a solver hard-crash surfaces
    as a failed job rather than killing the thread silently."""
    try:
        r = fn()
        with _lock:
            job = _jobs.get(job_id)
            if job is None:
                return                                # discarded mid-run
            job["result"] = r
            job["t_finish"] = time.monotonic()
            job["status"] = "done"
            if job.get("key"):
                _cache[job["key"]] = job_id
    except BaseException as e:                          # noqa: BLE001 - report any crash
        with _lock:
            job = _jobs.get(job_id)
            if job is None:
                return
            job["error"] = f"{type(e).__name__}: {e}"
            job["t_finish"] = time.monotonic()
            job["status"] = "failed"


# --- public API ---------------------------------------------------------------

def submit(kind: str, fn, key: str | None = None, meta: dict | None = None) -> dict:
    """Run ``fn()`` on a background thread; return immediately.

    ``kind`` labels the job class (e.g. 'fem_run', 'async_demo'). If ``key`` is
    given and a *completed* job with that key is still retained, this is a cache
    hit: the prior job_id comes back and fn is NOT run again. ``meta`` is opaque
    descriptive data echoed back in polls.

    Returns {job_id, status, cache_hit}. fn must not touch FreeCAD (see the module
    threading contract)."""
    with _lock:
        if key is not None:
            cached_id = _cache.get(key)
            if cached_id in _jobs and _jobs[cached_id]["status"] == "done":
                return {"job_id": cached_id, "status": "done", "cache_hit": True}
        job_id = _new_id()
        _jobs[job_id] = {
            "id": job_id, "kind": kind, "status": "running",
            "result": None, "error": None, "key": key, "meta": meta or {},
            "t_submit": time.monotonic(), "t_finish": None, "pins": 0,
        }
        _evict_locked()
    threading.Thread(target=_run, args=(job_id, fn), daemon=True,
                     name=f"job-{job_id}").start()
    return {"job_id": job_id, "status": "running", "cache_hit": False}


def pin(job_id: str) -> dict:
    """Protect a job from cap eviction while something else depends on its result.

    A fan-out driver (a study, a performance verification) submits N children and then
    joins them from ONE collector job. Without a pin, submitting the last child can evict
    the first — terminal, cheap, and therefore first in line — and the collector's
    ``result()`` raises JobNotFound for work that actually succeeded. Pinning makes that
    dependency explicit rather than relying on the cap being generous.

    Pins nest (a job pinned twice needs two unpins). Returns {job_id, pins}. Raises
    JobNotFound on an unknown id — pinning something already gone is a bug worth
    hearing about, unlike unpinning."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"unknown job: {job_id!r}")
        job["pins"] = job.get("pins", 0) + 1
        return {"job_id": job_id, "pins": job["pins"]}


def unpin(job_id: str) -> dict:
    """Release one pin taken by :func:`pin`, making the job evictable again once no pins
    remain. Safe on an unknown or unpinned id (it no-ops), so a collector can unpin in a
    ``finally`` without guarding. Returns {job_id, pins}."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "pins": 0}
        job["pins"] = max(0, job.get("pins", 0) - 1)
        return {"job_id": job_id, "pins": job["pins"]}


def status(job_id: str) -> dict:
    """Lightweight poll: {job_id, kind, status, elapsed_s, meta} (+ error when
    failed), WITHOUT the result payload. Raises JobNotFound on an unknown id."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"unknown job: {job_id!r}")
        out = {"job_id": job_id, "kind": job["kind"], "status": job["status"],
               "elapsed_s": _elapsed(job), "meta": job["meta"]}
        if job["status"] == "failed":
            out["error"] = job["error"]
        return out


def result(job_id: str, discard: bool = False) -> dict:
    """Fetch a job's outcome: {job_id, kind, status, elapsed_s, result|error}.
    ``result`` is present only when done, ``error`` only when failed; while running
    neither is set. ``discard=True`` frees a terminal job (and its cache entry);
    it is ignored while the job is still running. Raises JobNotFound on an unknown
    id."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"unknown job: {job_id!r}")
        out = {"job_id": job_id, "kind": job["kind"], "status": job["status"],
               "elapsed_s": _elapsed(job)}
        if job["status"] == "done":
            out["result"] = job["result"]
        elif job["status"] == "failed":
            out["error"] = job["error"]
        if discard and job["status"] != "running":
            _jobs.pop(job_id, None)
            if job.get("key") and _cache.get(job["key"]) == job_id:
                _cache.pop(job["key"], None)
        return out


def list_jobs() -> dict:
    """Summarize every retained job: {count, jobs:[{job_id, kind, status,
    elapsed_s}]} in submit order."""
    with _lock:
        jobs = [
            {"job_id": j["id"], "kind": j["kind"], "status": j["status"],
             "elapsed_s": _elapsed(j)}
            for j in _jobs.values()
        ]
    return {"count": len(jobs), "jobs": jobs}


def reset() -> None:
    """Clear the whole registry (tests / worker restart). Does not stop threads
    already running — they no-op when they find their record gone."""
    with _lock:
        _jobs.clear()
        _cache.clear()
        _counter["n"] = 0
