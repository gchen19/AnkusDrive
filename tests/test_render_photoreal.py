"""
Photoreal render (render_photoreal) toy-problem tests.

These exercise the FreeCAD Render workbench path: a worker-side handler that builds
a Render Project/Camera scene and shells out to an external renderer (POV-Ray by
default). They require BOTH the Render addon and a renderer binary to be installed
(see docs/RENDER_WORKBENCH.md). On a machine without them, each test SKIPs — the
worker still boots (Render is imported lazily inside the handler), and the render
call returns DriftPin's own install-guidance error, which we detect and treat as a
skip so CI lanes that don't provision a renderer stay green.

Like test_render.py, these assert invariants, never exact pixels — photoreal output
is not bit-reproducible (sampler noise, thread count).

Run:  .venv/bin/python3 tests/test_render_photoreal.py
"""
import base64
import io
import sys
import time
import traceback
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin.client import WorkerError  # noqa: E402

# Substrings of the worker's OWN guidance messages (driftpin/worker.py) when the
# addon or renderer binary is missing — stable because we author them.
_UNAVAILABLE_MARKERS = ("not importable", "could not locate")


class _Skip(Exception):
    """Raised to mark a test skipped (renderer/addon not installed)."""


def _photoreal(w, handle, **kw):
    """Call render_photoreal, converting 'not installed' errors into a skip."""
    try:
        return w.call("render_photoreal", handle=handle, _timeout=300.0, **kw)
    except WorkerError as e:
        if any(m in e.remote_message for m in _UNAVAILABLE_MARKERS):
            raise _Skip(e.remote_message.splitlines()[0])
        raise


def _box(w, dims=(30, 20, 10)):
    w.call("new_document", name="photoreal")
    return w.call("add_primitive", kind="box", w=dims[0], d=dims[1], h=dims[2])


def _img(res):
    png = base64.b64decode(res["png_base64"])
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))


def _center_rgb(res):
    """Mean RGB of the central third — the part, not the background."""
    a = _img(res)
    h, w, _ = a.shape
    return a[h // 3:2 * h // 3, w // 3:2 * w // 3].reshape(-1, 3).mean(axis=0)


# --- tests --------------------------------------------------------------------

def test_photoreal_returns_valid_png():
    """Output is a parseable PNG of the requested size, with echoed metadata."""
    with Worker() as w:
        box = _box(w)
        res = _photoreal(w, box["handle"], view="iso", width=200, height=150)
        png = base64.b64decode(res["png_base64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        assert Image.open(io.BytesIO(png)).size == (200, 150), "wrong size"
        assert res["renderer"] == "Povray" and res["view"] == "iso"
        assert res["width"] == 200 and res["height"] == 150


def test_photoreal_non_blank():
    """The renderer actually drew the part — a blank canvas has stdev 0."""
    with Worker() as w:
        box = _box(w)
        arr = _img(_photoreal(w, box["handle"], width=200, height=150))
        assert arr.std() > 3, f"image looks blank: stdev={arr.std()}"


def test_photoreal_view_changes_image():
    """The view parameter drives the camera: iso vs front are different images."""
    with Worker() as w:
        box = _box(w, dims=(30, 20, 10))
        a_iso = _img(_photoreal(w, box["handle"], view="iso", width=200, height=150))
        a_front = _img(_photoreal(w, box["handle"], view="front", width=200, height=150))
        diff = float(np.abs(a_iso.astype(int) - a_front.astype(int)).mean())
        assert diff > 2.0, (
            f"iso and front renders nearly identical (mean abs diff={diff:.2f}) — "
            "view parameter may not reach the camera"
        )


def test_photoreal_material_changes_color():
    """A material library card visibly changes the render: 'Gold' is yellow
    (red channel well above blue), while the default material is neutral gray
    (red ~= blue). Also confirms the chosen material is echoed back."""
    with Worker() as w:
        box = _box(w)
        gold_res = _photoreal(w, box["handle"], material="Gold", width=240, height=180)
        assert gold_res["material"] == "Gold"
        gold = _center_rgb(gold_res)
        dflt = _center_rgb(_photoreal(w, box["handle"], width=240, height=180))
        assert gold[0] - gold[2] > 30, f"'Gold' not yellow enough: RGB={gold.round(1)}"
        assert abs(dflt[0] - dflt[2]) < 10, f"default material not neutral: RGB={dflt.round(1)}"


def test_photoreal_unknown_material_errors():
    """An unknown material name fails with a listing of the valid cards (rather
    than rendering something wrong or crashing)."""
    with Worker() as w:
        box = _box(w)
        try:
            w.call("render_photoreal", handle=box["handle"],
                   material="Unobtanium__nope", _timeout=60.0)
        except WorkerError as e:
            if any(m in e.remote_message for m in _UNAVAILABLE_MARKERS):
                raise _Skip(e.remote_message.splitlines()[0])
            assert "unknown render material" in e.remote_message, e.remote_message
            return
        raise AssertionError("expected an error for an unknown material name")


def _assert_alternate_renderer(renderer):
    """Render a box with an alternate (non-default) renderer in batch/console mode.
    Skips when that renderer's binary is absent (they are hand-fetched builds); on a
    box that provides it, produces a non-blank PNG via the same scene-export path as
    POV-Ray."""
    with Worker() as w:
        box = _box(w)
        res = _photoreal(w, box["handle"], renderer=renderer, width=200, height=150)
        assert res["renderer"] == renderer
        assert _img(res).std() > 3, f"{renderer} render looks blank"


def test_photoreal_luxcore_renders():
    _assert_alternate_renderer("Luxcore")


def test_photoreal_appleseed_renders():
    _assert_alternate_renderer("Appleseed")


def test_photoreal_cycles_renders():
    _assert_alternate_renderer("Cycles")


def test_photoreal_isolates_live_document():
    """Rendering must not mutate the caller's document. The handler renders in a
    throwaway temp doc, so the live doc's object list is unchanged afterwards."""
    with Worker() as w:
        box = _box(w)
        before = [o["type"] for o in w.call("list_objects")]
        _photoreal(w, box["handle"], width=120, height=90)
        after = [o["type"] for o in w.call("list_objects")]
        assert after == before, (
            f"render leaked objects into the live document: {before} -> {after}"
        )


def test_photoreal_async_job():
    """render_photoreal_submit returns immediately and the worker stays responsive
    (a ping succeeds mid-render); render_job polls to a valid non-blank PNG; and the
    live document is left untouched (the temp doc is closed on completion)."""
    with Worker() as w:
        box = _box(w)
        try:
            sub = w.call("render_photoreal_submit", handle=box["handle"],
                         width=400, height=300, _timeout=60.0)
        except WorkerError as e:
            if any(m in e.remote_message for m in _UNAVAILABLE_MARKERS):
                raise _Skip(e.remote_message.splitlines()[0])
            raise
        assert sub["status"] == "running" and sub["job_id"]
        assert w.call("ping") == "pong"          # worker responsive while rendering

        res = None
        deadline = time.time() + 90
        while time.time() < deadline:
            res = w.call("render_job", job_id=sub["job_id"], _timeout=30.0)
            if res["status"] != "running":
                break
            time.sleep(0.3)
        assert res and res["status"] == "done", f"job did not finish: {res}"
        png = base64.b64decode(res["png_base64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        assert np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).std() > 3, "blank"
        assert [o["type"] for o in w.call("list_objects")] == ["Part::Box"], \
            "async render leaked objects into the live document"


def test_render_job_unknown():
    """Polling a nonexistent job id errors cleanly (independent of the renderer, so
    this runs even without the addon installed)."""
    with Worker() as w:
        try:
            w.call("render_job", job_id="render_job_does_not_exist", _timeout=30.0)
        except WorkerError as e:
            assert "unknown render job" in e.remote_message, e.remote_message
            return
        raise AssertionError("expected an error for an unknown job id")


def _drain_job(w, job_id, timeout=60.0):
    """Poll a job until it leaves 'running'. Returns the last response."""
    deadline = time.time() + timeout
    res = None
    while time.time() < deadline:
        res = w.call("render_job", job_id=job_id, _timeout=30.0)
        if res["status"] != "running":
            return res
        time.sleep(0.15)
    return res


def test_render_job_discard():
    """discard=True frees a finished job: it returns the result once, then the job
    is gone (closing its temp doc and dropping the cached PNG)."""
    with Worker() as w:
        box = _box(w)
        try:
            sub = w.call("render_photoreal_submit", handle=box["handle"],
                         width=120, height=90, _timeout=60.0)
        except WorkerError as e:
            if any(m in e.remote_message for m in _UNAVAILABLE_MARKERS):
                raise _Skip(e.remote_message.splitlines()[0])
            raise
        jid = sub["job_id"]
        assert _drain_job(w, jid)["status"] == "done"
        res = w.call("render_job", job_id=jid, discard=True, _timeout=30.0)
        assert res["status"] == "done" and "png_base64" in res
        try:
            w.call("render_job", job_id=jid, _timeout=30.0)
        except WorkerError as e:
            assert "unknown render job" in e.remote_message
            return
        raise AssertionError("discarded job should be gone")


def test_render_job_eviction():
    """Finished jobs are bounded by the worker cap (_MAX_RENDER_JOBS = 16): once more
    than the cap have finished, a further submit evicts the oldest (polling it returns
    unknown) while a recent job is retained. Uses tiny images to stay fast."""
    CAP = 16
    with Worker() as w:
        box = _box(w)

        def submit():
            return w.call("render_photoreal_submit", handle=box["handle"],
                          width=48, height=36, _timeout=60.0)["job_id"]

        try:
            ids = [submit()]
        except WorkerError as e:
            if any(m in e.remote_message for m in _UNAVAILABLE_MARKERS):
                raise _Skip(e.remote_message.splitlines()[0])
            raise
        for _ in range(CAP):                         # CAP + 1 jobs submitted
            ids.append(submit())
        for jid in ids:                              # make them all finished
            _drain_job(w, jid)
        submit()                                     # over the cap -> evict oldest finished

        try:
            w.call("render_job", job_id=ids[0], _timeout=30.0)
            evicted = False
        except WorkerError as e:
            evicted = "unknown render job" in e.remote_message
        assert evicted, "oldest finished job should have been evicted past the cap"
        assert w.call("render_job", job_id=ids[-1], _timeout=30.0)["status"] != "running", \
            "a recent job should still be retained"


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    tests = _discover()
    failures = []
    skipped = []
    passed = 0
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except _Skip as s:
            skipped.append(name)
            print(f"  SKIP {name:45s} ({time.time() - t0:.2f}s)  {s}")
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:45s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            passed += 1
            print(f"  PASS {name:45s} ({time.time() - t0:.2f}s)")

    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed, {len(skipped)} skipped  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    if skipped and not passed:
        print(f"== all {len(skipped)} tests skipped (Render addon / renderer not installed)  ({total:.1f}s) ==")
    else:
        print(f"== {passed}/{len(tests)} passed, {len(skipped)} skipped  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
