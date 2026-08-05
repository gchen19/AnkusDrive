"""Main-thread work queue — let a background job ask for FreeCAD work (issue #260).

Pure-Python, FreeCAD-free. This is the piece that was missing under adaptive
*shape* search, and the reason ``optimize_submit`` (#228) could vary numbers but
not geometry.

THE PROBLEM
-----------
Two constraints meet head-on:

  * An optimizer's search is **sequential and long**. Each step depends on the last
    and a solver-tier budget runs to minutes or hours — far past the client's
    per-call timeout — so it must run as a background job.
  * FreeCAD's document API is **main-thread only** (the ``jobs.py`` threading
    contract). A background job may not build geometry.

A parameter search satisfies both because its evaluations are pure functions of
numbers. A shape search cannot: every candidate needs ``recipe(params)`` to run,
which needs the main thread, which the search does not have. ``study_submit``
(#227) dodges this by knowing all its points up front and building every one on the
main thread before submitting anything — real and useful, and not a search.

THE SHAPE
---------
A background job calls :func:`call` with the work it needs done on the main thread.
That enqueues the callable and blocks the CALLING thread on its result. The worker's
request loop calls :func:`drain` before handling each request, which runs the queued
callables on the main thread and wakes whoever was waiting.

WHY THIS NEEDS NO CHANGE TO THE BLOCKING stdin READ
---------------------------------------------------
The worker's loop is ``for line in sys.stdin`` — it blocks, so a queue serviced only
"when idle" would need a non-blocking rewrite of the one path every request takes.
It doesn't need one: **the client is already polling.** An async tool returns a
``job_id`` and the caller polls ``job_status``/``job_result`` until it is done. Every
one of those polls is a request, and every request drains the queue first. The poll
the client must do anyway IS the main thread's turn.

The corollary is the one real constraint, and it is stated rather than hidden: a
background job that calls :func:`call` makes no progress until the client polls
again. That is why :func:`call` takes a timeout and why its timeout message says so
— a deadlock here must read as "nobody polled", not as a hung solve.

Reentrancy: calling :func:`call` from the main thread itself runs the callable
inline rather than enqueuing it (which would deadlock — the servicing thread would
be the one waiting). So the same evaluation code works both inside a background
search and inline in a synchronous handler.
"""
from __future__ import annotations

import queue
import threading
import time

#: How long :func:`call` waits for the main thread before giving up. Generous,
#: because the gap between client polls is the client's business, not ours — a
#: long-running solve polled every 30 s is normal.
DEFAULT_TIMEOUT_S = 600.0

_queue: queue.Queue = queue.Queue()
_service_thread_id = None            # set by bind(); the thread that runs drain()
_stats = {"queued": 0, "ran": 0, "failed": 0, "drains": 0}
_stats_lock = threading.Lock()


class NotServiced(RuntimeError):
    """Raised by :func:`call` when the main thread never came to run the work."""


class _Task:
    __slots__ = ("fn", "done", "value", "error")

    def __init__(self, fn):
        self.fn = fn
        self.done = threading.Event()
        self.value = None
        self.error = None


def bind(thread_id=None) -> None:
    """Mark the current thread (or ``thread_id``) as the one that services the queue.

    Called once by the worker's request loop at startup. :func:`call` uses it to tell
    "I am the main thread, run inline" from "I am a job thread, enqueue and wait"."""
    global _service_thread_id
    _service_thread_id = threading.get_ident() if thread_id is None else thread_id


def is_service_thread() -> bool:
    """True when the caller is the thread that drains the queue."""
    return _service_thread_id is not None and threading.get_ident() == _service_thread_id


def call(fn, timeout_s: float | None = None):
    """Run ``fn()`` on the main thread and return its result, from any thread.

    On the servicing thread this runs ``fn`` INLINE — enqueuing would deadlock, since
    the only thread that could run it is the one about to wait. Everywhere else it
    enqueues and blocks until :func:`drain` picks it up.

    Raises whatever ``fn`` raised (so a recipe that fails to build fails at the call
    site, with its own traceback), or :class:`NotServiced` if the main thread did not
    come within ``timeout_s`` — which, in a worker, means the client stopped polling."""
    if is_service_thread():
        return fn()
    task = _Task(fn)
    with _stats_lock:
        _stats["queued"] += 1
    _queue.put(task)
    if not task.done.wait(DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s):
        raise NotServiced(
            "the main thread did not run this work within "
            f"{DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s:g}s. The queue is "
            "drained once per incoming request, so a background job only advances "
            "while the client is polling (job_status / job_result) — if polling "
            "stopped, the job is waiting for a turn that will never come")
    if task.error is not None:
        raise task.error
    return task.value


def drain(max_items: int | None = None, max_s: float | None = None) -> int:
    """Run queued callables on the calling thread. Returns how many ran.

    Called by the worker's request loop before each request. Bounded on both axes so
    one greedy job cannot starve the request it is riding on: ``max_items`` caps the
    count, ``max_s`` the wall clock (checked BETWEEN items — a single long build still
    runs to completion, because abandoning it half-way would leave a partial document).

    Never raises: a task's exception is handed to the waiter through its own future,
    because a queued build blowing up must fail that job, not the worker's loop."""
    ran = 0
    t0 = time.monotonic()
    with _stats_lock:
        _stats["drains"] += 1
    while True:
        if max_items is not None and ran >= max_items:
            break
        if max_s is not None and ran and (time.monotonic() - t0) >= max_s:
            break
        try:
            task = _queue.get_nowait()
        except queue.Empty:
            break
        try:
            task.value = task.fn()
        except BaseException as e:                   # noqa: BLE001 - relayed, not swallowed
            task.error = e
            with _stats_lock:
                _stats["failed"] += 1
        else:
            with _stats_lock:
                _stats["ran"] += 1
        finally:
            task.done.set()
            ran += 1
    return ran


def pending() -> int:
    """How many callables are waiting for a main-thread turn."""
    return _queue.qsize()


def stats() -> dict:
    """{queued, ran, failed, drains, pending} — surfaced by ``job_list`` for debugging
    a job that looks stuck: ``pending`` high with ``drains`` climbing means the client
    is polling and the work is simply slow; ``drains`` flat means nobody is polling."""
    with _stats_lock:
        out = dict(_stats)
    out["pending"] = pending()
    return out


def reset() -> None:
    """Drop queued work and zero the counters. Tests only."""
    global _queue
    _queue = queue.Queue()
    with _stats_lock:
        for k in _stats:
            _stats[k] = 0
