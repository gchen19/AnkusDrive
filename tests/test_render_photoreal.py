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
