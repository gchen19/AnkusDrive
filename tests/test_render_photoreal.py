"""
Photoreal render (render_photoreal) toy-problem tests.

Two backends. The FreeCAD Render workbench path (the add-on renderers — the tests
below pin renderer="Povray" etc. explicitly, since the default is now "auto") builds
a Render Project/Camera scene and shells out to an external renderer. The Blender
studio backend (issue #335, the `test_blender_*` tests) tessellates every part and
runs ankusdrive/blender_scene.py inside `blender --background`; those skip when
Blender does not resolve (the not-installed miss dict is the skip signal). They require BOTH the Render addon and a renderer binary to be installed
(see docs/RENDER_WORKBENCH.md). On a machine without them, each test SKIPs — the
worker still boots (Render is imported lazily inside the handler), and the render
call returns AnkusDrive's own install-guidance error, which we detect and treat as a
skip so CI lanes that don't provision a renderer stay green.

Like test_render.py, these assert invariants, never exact pixels — photoreal output
is not bit-reproducible (sampler noise, thread count).

Run:  .venv/bin/python3 tests/test_render_photoreal.py
"""
import base64
import io
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker  # noqa: E402
from ankusdrive.client import WorkerError  # noqa: E402

# Substrings of the worker's OWN guidance messages (ankusdrive/worker.py) when the
# addon or renderer binary is missing — stable because we author them.
_UNAVAILABLE_MARKERS = ("not importable", "could not locate")


class _Skip(Exception):
    """Raised to mark a test skipped (renderer/addon not installed)."""


def _photoreal(w, handle, **kw):
    """Call render_photoreal (the add-on path unless a renderer is named), converting
    'not installed' errors and miss dicts into a skip."""
    kw.setdefault("renderer", "Povray")
    try:
        res = w.call("render_photoreal", handle=handle, _timeout=300.0, **kw)
    except WorkerError as e:
        if any(m in e.remote_message for m in _UNAVAILABLE_MARKERS):
            raise _Skip(e.remote_message.splitlines()[0])
        raise
    if res.get("ok") is False:
        raise _Skip(f"{res.get('renderer')}: {res.get('reason')}")
    return res


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
            w.call("render_photoreal", handle=box["handle"], renderer="Povray",
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


def test_photoreal_ospray_renders():
    """OSPRay Studio renders the scene correctly, but the Render addon's stock
    'ospray_standard.sg' template lights it with a single *dim ambient* light (color
    [0.2,0.2,0.2], no directional/area light), so the image is correct yet flat and
    low-contrast — it does NOT clear the >3 non-blank bar the other renderers clear
    (std-dev ~2.3). This is an addon-template trait, not a problem with the built
    ospStudio binary: the same box renders bright on the other five renderers. See
    docs/RENDERING.md known limitations. So we assert a valid, correctly-sized PNG that
    is demonstrably not blank (std-dev > 1), rather than the full non-blank threshold.
    Still SKIPs when ospStudio is absent (via _photoreal)."""
    with Worker() as w:
        box = _box(w)
        res = _photoreal(w, box["handle"], renderer="Ospray", width=200, height=150)
        assert res["renderer"] == "Ospray"
        png = base64.b64decode(res["png_base64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        assert Image.open(io.BytesIO(png)).size == (200, 150), "wrong size"
        std = float(_img(res).std())
        assert std > 1.0, (
            f"OSPRay render looks blank: std-dev={std:.2f} (expected the dim-but-present "
            "ambient render, ~2.3; see docs/RENDERING.md known limitations)"
        )


def test_photoreal_pbrt_renders():
    _assert_alternate_renderer("Pbrt")


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
            sub = w.call("render_photoreal_submit", handle=box["handle"], renderer="Povray",
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
            sub = w.call("render_photoreal_submit", handle=box["handle"], renderer="Povray",
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
            return w.call("render_photoreal_submit", handle=box["handle"], renderer="Povray",
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


def test_render_capabilities():
    """render_capabilities reports per-renderer availability + addon import status
    WITHOUT rendering, so unlike the render tests it never skips — it's a pure probe
    that simply reports what's installed. We assert the report is self-consistent and
    cross-check each verdict against shutil.which for ground truth (the worker
    inherits this process's PATH, so 'on PATH' agrees both sides)."""
    with Worker() as w:
        caps = w.call("render_capabilities", _timeout=120.0)
        assert isinstance(caps["addon_importable"], bool)
        assert caps["default_renderer"] == "auto" and caps["recommended"] == "Blender"

        rends = caps["renderers"]
        # Blender + the six add-on renderers wired in Phase 1.
        assert "Blender" in rends and "Povray" in rends and len(rends) >= 7, list(rends)
        # `available` is exactly the renderers flagged available, sorted.
        assert caps["available"] == sorted(n for n, i in rends.items() if i["available"])

        for name, info in rends.items():
            assert isinstance(info["available"], bool)
            assert info["binaries"], f"{name} missing binaries"
            if info["backend"] == "render_addon":
                assert info["param_key"], f"{name} missing param_key"
            if info["available"]:
                assert os.path.isfile(info["path"]), f"{name} marked available but path absent"
            else:
                assert info["install_hint"], f"{name} unavailable but no install_hint"
            # Ground truth: a binary on PATH MUST be resolved (Blender may still be
            # unavailable when it is too old to run the studio scene).
            if any(shutil.which(b) for b in info["binaries"]) and name != "Blender":
                assert info["available"], f"{name} is on PATH but reported unavailable"

        b = rends["Blender"]
        assert b["family"] == "studio_render" and "studio" in b["scenes"]
        if b["available"]:
            assert b["version"] and b["device"] and b["supported"] is True
            assert caps["auto_selects"] == "Blender"
            assert "suggestion" not in caps
        else:
            # the upgrade path is handed out, never silently omitted
            assert caps["suggestion"]["renderer"] == "Blender"
            assert caps["suggestion"]["install"] == b["install_hint"]
            addon_ready = [n for n in rends if n != "Blender" and rends[n]["available"]
                           and caps["addon_importable"]]
            assert (caps["auto_selects"] is None) == (not addon_ready), caps["auto_selects"]

        assert "Aluminium" in caps["materials"] and "finish" in caps["appearance_fields"]
        if not caps["addon_importable"]:
            assert caps.get("addon_error"), "addon not importable but no addon_error"


# --- Blender studio backend (issue #335) ------------------------------------------

def _blender(w, **kw):
    """render_photoreal on the Blender backend; SKIP when Blender does not resolve."""
    kw.setdefault("renderer", "Blender")
    kw.setdefault("quality", "draft")
    res = w.call("render_photoreal", _timeout=600.0, **kw)
    if res.get("ok") is False:
        assert res["install"], res                   # a miss always carries the fix
        raise _Skip(f"Blender: {res['reason']}")
    return res


def _two_part_scene(w):
    w.call("new_document", name="blender_scene")
    a = w.call("add_primitive", kind="box", w=30, d=30, h=20)["handle"]
    b = w.call("add_primitive", kind="cylinder", r=12, h=20, placement=[55, 15, 0])["handle"]
    return a, b


def _hue_pixels(arr):
    r, g, b = (arr[..., i].astype(int) for i in range(3))
    return int(((r - b) > 50).sum()), int(((b - r) > 50).sum())


def test_blender_assembly_per_part_appearance():
    """Two parts, two appearances -> ONE image in which both colours are present:
    a clearly red region and a clearly blue region (invariant, not pixels)."""
    with Worker() as w:
        a, b = _two_part_scene(w)
        res = _blender(w, parts=[{"handle": a, "appearance": {"color": "#c62828"}},
                                 {"handle": b, "appearance": {"base": "GlossyPlastic",
                                                              "color": "#1e4fd8"}}],
                       width=320, height=240)
        png = base64.b64decode(res["png_base64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and Image.open(io.BytesIO(png)).size == (320, 240)
        arr = _img(res)
        assert arr.std() > 3, "blank render"
        reds, blues = _hue_pixels(arr)
        assert reds > 200 and blues > 200, f"per-part colours not both visible: red={reds} blue={blues}"
        assert res["renderer"] == "Blender" and len(res["parts"]) == 2
        assert [p["material"] for p in res["parts"]] == ["custom", "GlossyPlastic+custom"]
        assert res["samples"] == 16 and res["quality"] == "draft" and res["elapsed_s"] > 0


def test_blender_assembly_handle_with_appearances():
    """An assembly handle renders every leaf in place; `appearances` is keyed by the
    part names list_assembly_parts reports; an unknown name fails listing the real ones."""
    with Worker() as w:
        a, b = _two_part_scene(w)
        asm = w.call("make_assembly", name="Rig")["handle"]
        w.call("add_part", assembly=asm, source={"handle": a}, name="red_block")
        w.call("add_part", assembly=asm, source={"handle": b}, name="blue_post")
        names = [p["name"] for p in w.call("list_assembly_parts", assembly=asm)]
        assert names == ["red_block", "blue_post"], names
        try:
            _blender(w, handle=asm, appearances={"nope": "Gold"})
        except WorkerError as e:
            assert "red_block" in e.remote_message and "nope" in e.remote_message, e.remote_message
        else:
            raise AssertionError("unknown appearance key should raise")
        res = _blender(w, handle=asm, width=320, height=240,
                       appearances={"red_block": {"color": "#c62828"},
                                    "blue_post": {"color": "#1e4fd8"}})
        reds, blues = _hue_pixels(_img(res))
        assert reds > 200 and blues > 200, f"assembly parts not distinct: red={reds} blue={blues}"
        assert [p["name"] for p in res["parts"]] == ["red_block", "blue_post"]


def test_blender_studio_scene_is_not_flat():
    """scene='studio' with no agent scripting: the backdrop carries a lighting
    gradient and the part casts a contact shadow onto the floor. A saturated red
    part doubles as its own mask, so the checks read only backdrop pixels."""
    with Worker() as w:
        w.call("new_document", name="studio")
        box = w.call("add_primitive", kind="box", w=40, d=40, h=25)["handle"]
        res = _blender(w, handle=box, material={"color": "#d01010", "roughness": 0.6},
                       width=320, height=240)
        assert res["scene"] == "studio"
    a = _img(res).astype(int)
    lum = a.mean(axis=2)
    part = (a[..., 0] - a[..., 2]) > 40
    bg = ~part
    assert part.sum() > 2000 and bg.sum() > 2000, "part not framed"
    p5, p95 = np.percentile(lum[bg], [5, 95])
    assert p95 - p5 > 12, f"backdrop is a flat fill (p5={p5:.1f}, p95={p95:.1f})"
    ys, xs = np.nonzero(part)
    band = np.zeros_like(part)
    band[ys.max() + 1: ys.max() + 7, xs.min(): xs.max() + 1] = True
    band &= bg
    assert band.sum() > 50, "no floor visible below the part"
    below, overall = lum[band].mean(), lum[bg].mean()
    assert below < overall - 4, f"no contact shadow: below={below:.1f} backdrop={overall:.1f}"


def test_blender_blend_file_round_trips():
    """blend_path is saved and reopens in Blender with parts, materials, lights and
    camera intact (checked by loading it in the resolved Blender)."""
    import json
    import subprocess
    import tempfile
    with Worker() as w:
        a, b = _two_part_scene(w)
        out = tempfile.mkdtemp(prefix="blend_rt_")
        res = _blender(w, parts=[{"handle": a, "appearance": "Brass", "name": "block"},
                                 {"handle": b, "appearance": "Glass", "name": "post"}],
                       width=160, height=120, output_dir=out)
        assert res["blend_path"].startswith(out) and os.path.isfile(res["blend_path"])
        blender = w.call("render_capabilities", _timeout=120.0)["renderers"]["Blender"]["path"]
    expr = ("import bpy, json; print('RT ' + json.dumps({"
            "'objects': sorted(o.name for o in bpy.data.objects), "
            "'lights': len(bpy.data.lights), 'camera': bpy.context.scene.camera.name, "
            "'mats': {o.name: o.active_material.name for o in bpy.data.objects "
            "if o.type == 'MESH' and o.active_material}}))")
    proc = subprocess.run([blender, "--background", "--factory-startup", res["blend_path"],
                           "--python-expr", expr], capture_output=True, text=True, timeout=120)
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RT "))
    info = json.loads(line[3:])
    assert {"block", "post", "Camera", "Cyclorama"} <= set(info["objects"]), info
    assert info["lights"] >= 3 and info["camera"] == "Camera", info
    assert info["mats"]["block"] == "block" and info["mats"]["post"] == "post", info


def test_blender_auto_prefers_blender():
    """renderer='auto' (the default) picks Blender when it resolves, and says so."""
    with Worker() as w:
        w.call("new_document", name="auto")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]
        caps = w.call("render_capabilities", _timeout=120.0)
        if not caps["renderers"]["Blender"]["available"]:
            raise _Skip("Blender not installed")
        res = w.call("render_photoreal", handle=box, quality="draft", width=96, height=72,
                     _timeout=600.0)
        assert res["renderer"] == "Blender" and res["auto_selected"] is True, res.get("renderer")
        assert "suggestion" not in res


def test_blender_bad_appearance_fails_fast():
    """A typo'd card or finish raises listing the valid names — before Blender runs."""
    with Worker() as w:
        w.call("new_document", name="bad")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]
        if not w.call("render_capabilities", _timeout=120.0)["renderers"]["Blender"]["available"]:
            raise _Skip("Blender not installed")
        for kw, needle in (({"handle": box, "material": "Unobtanium"}, "Aluminium"),
                           ({"parts": [{"handle": box, "appearance": {"finish": "knurl"}}]},
                            "fdm_layers"),
                           ({"handle": box, "quality": "ultra"}, "preview")):
            try:
                w.call("render_photoreal", renderer="Blender", _timeout=120.0, **kw)
            except WorkerError as e:
                assert needle in e.remote_message, (kw, e.remote_message)
            else:
                raise AssertionError(f"expected an error for {kw}")


def test_blender_async_job():
    """render_photoreal_submit on Blender returns at once, the worker stays responsive,
    and render_job's 'done' carries the PNG plus blend_path."""
    with Worker() as w:
        w.call("new_document", name="async_blender")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]
        sub = w.call("render_photoreal_submit", handle=box, renderer="Blender",
                     quality="draft", width=120, height=90, _timeout=120.0)
        if sub.get("ok") is False:
            raise _Skip(f"Blender: {sub['reason']}")
        assert sub["status"] == "running" and sub["renderer"] == "Blender"
        assert w.call("ping") == "pong"
        res = _drain_job(w, sub["job_id"], timeout=300.0)
        assert res["status"] == "done", res
        assert base64.b64decode(res["png_base64"])[:8] == b"\x89PNG\r\n\x1a\n"
        assert os.path.isfile(res["blend_path"]) and res["samples"] == 16


def test_addon_renderer_reports_ignored_blender_args():
    """A named add-on renderer cannot honour Blender-only arguments: it renders what
    it can and names what it ignored, with the Blender install as the suggestion."""
    with Worker() as w:
        box = _box(w)
        res = _photoreal(w, box["handle"], renderer="Povray", scene="studio",
                         quality="final", width=96, height=72)
        assert res["renderer"] == "Povray"
        assert set(res["ignored"]) == {"scene", "quality"}, res.get("ignored")
        assert res["suggestion"]["renderer"] == "Blender" and res["suggestion"]["install"]


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
