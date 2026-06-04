# Photoreal rendering via the FreeCAD Render workbench

DriftPin grows a *photorealistic* render path by stitching in the third-party
[FreeCAD Render workbench](https://github.com/FreeCAD/FreeCAD-render) and exposing
it as an MCP tool (`render_photoreal`) alongside the existing software-rasterized
[`render_view`](../driftpin/render.py).

**Status: Phase 1 implemented.** `render_photoreal` is wired end-to-end through the
worker and the MCP server, verified on Linux with FreeCAD 1.1.0 + POV-Ray 3.7.
The addon and a renderer binary are still *optional at runtime* — DriftPin boots
and runs without them, and the tool returns clear install guidance if they are
absent (the renderer-gated tests skip rather than fail). What it does **not** yet
do — real materials, renderers other than POV-Ray, and an async/job variant for
very long renders — is tracked in §7 as follow-ups. This doc doubles as the design
record and the operator's install guide for all three platforms.

---

## 1. Why, and what we have today

DriftPin's original render path is a deliberate "can the agent *see* what it just
built?" tool, not a presentation tool:

- `render_view` / `render_views` (MCP) → worker `tessellate` handler returns
  vertices + triangles → host-side [`render.py`](../driftpin/render.py)
  rasterizes in pure NumPy + Pillow.
- Flat Lambertian shading, orthographic, fixed light, per-pixel z-buffer. No
  materials, no GI, no perspective. Fast, deterministic, zero external deps.

That's the right tool for closed-loop reliability checks (Layer A/D). It is the
wrong tool for "show me a nice picture of the part." Photoreal output (materials,
lighting, global illumination, perspective camera) is a separate concern and is a
separate tool — `render_photoreal`, **not** a flag on `render_view`.

## 2. Maintenance status of the upstream workbench — read this first

The Render workbench is **frozen / in maintenance limbo**:

- The maintainer **Howetuft announced discontinuation on 2025-11-12**
  ("I am discontinuing maintenance of this workbench as of now") and is seeking a
  successor. None has stepped up.
- As of mid-2026: ~226★, ~3,900 commits, issues still filed (people use it; nobody
  merges). Still listed in the official FreeCAD Addon Manager and still functional.

**Implication for us:** it is safe to *consume* (we depend on a stable Python API,
not on a roadmap) but risky *long-term*. The concrete failure mode is FreeCAD 1.1 /
2.0 eventually drifting out from under it with nobody upstream to patch — the same
class of breakage we already track for FreeCAD API drift. We therefore **pin a
known-good commit** rather than tracking `master`, and treat the integration as
something we may have to fork or re-host.

> **Pinned commit:** `08be2fe94b8a998323c8a5443f7f0afd0d05bed5` (2026-05-16),
> verified against FreeCAD 1.1.0. Bump deliberately, re-run the photoreal tests.

## 3. How the workbench works (architecture)

It is a **pure-Python workbench**. The entire scene — `Project`, `Camera`, `View`,
lights, materials — is a graph of FreeCAD `App` document objects. On render it
serializes that graph into a renderer-specific scene file and **shells out to an
external renderer binary**:

| Renderer | Install (Linux / macOS / Windows) | Notes |
|---|---|---|
| POV-Ray | `apt install povray` / `brew install povray` / official installer | **Phase 1 default.** Single CLI binary, deterministic-ish, easiest. Lower photoreal ceiling. |
| LuxCoreRender | hand-fetched build (all OSes) | Highest quality + PBR materials; heavier/slower headless. |
| Appleseed | hand-fetched build | `appleseed.cli` headless renderer. |
| Cycles (standalone) | hand-fetched build | Blender's engine; fiddliest to wire. |
| Ospray / pbrt-v4 | hand-fetched | pbrt-v4 marked experimental upstream. |

The workbench itself rasterizes nothing — **it's a scene exporter + process
launcher.** That is exactly what makes it embeddable in DriftPin's worker, given
that we can drive it without the GUI.

**Public Python API — as actually observed (the original proposal mis-stated
several of these; corrected here):**

```python
# create() returns a 3-TUPLE (proxy, fpo, viewprovider) — NOT the object.
proj_proxy, proj, _ = Render.Project.create(doc, renderer="Povray",
                                             template="povray_standard.pov")
proj.RenderWidth, proj.RenderHeight = 800, 600   # REQUIRED: render() aborts if <= 0

cam_proxy, cam, _ = Render.Camera.create(doc)    # also a 3-tuple
cam.Projection = "Perspective"
cam.Placement  = ...                              # camera->world placement

proj_proxy.add_views([cam, part])                 # camera + part are both "views"
img = proj.Proxy.render(wait_for_completion=True) # -> output image path (or None)
```

Corrections vs. the original proposal:

- **`Project.create` / `Camera.create` return `(proxy, fpo, viewprovider)`**, so you
  must unpack. `proj.Proxy.render(...)` (equivalently `proj_proxy.render(...)`) is
  the render entry point.
- **A template is effectively required.** With no `template=`, `Project.Template`
  is empty and `render()` fails (`Is a directory: '.../templates/'`). We pass a
  shipped template (`povray_standard.pov`). `Template` is a relative filename
  resolved against the addon's `templates/` dir.
- **`RenderWidth` / `RenderHeight` must be set** on the project fpo — resolution is
  *not* an argument to `render()`.
- The output path defaults to `{Document.TransientDir}/{Name}_output.png`; `render()`
  returns it. (We never set `OutputImage`.)

## 4. The headless question (resolved)

DriftPin's worker runs inside `freecadcmd` — **no GUI, `App.GuiUp == False`**.
Photoreal rendering works headless; the three GUI-coupled behaviors are handled by
building scene state as `App` objects instead of borrowing it from a viewport:

| Concern | With GUI | Headless — what DriftPin does |
|---|---|---|
| **Camera** | grabs `Gui.ActiveDocument.ActiveView.getCamera()` | No viewport → **adds an explicit `Camera` object and sets its `Placement`** via [`_placement_from_view`](../driftpin/worker.py) (pure `App.Vector` math mirroring `render.py`'s `_camera_basis`; see below). |
| **Visibility filter** | renders only views with `ViewObject.Visibility` | Falls back to **all** views. No action needed. |
| **Materials / colors** | reads `ViewObject.ShapeColor` etc. | No ViewObject colors → falls back to **default material**. Acceptable for Phase 1; real Render `Material` objects are a follow-up (§7). |

Two harmless headless artifacts worth knowing:

- **"More than one camera" POV-Ray warning.** The workbench always injects a default
  camera *and* emits our explicit camera; POV-Ray uses the last one, which is ours
  (it lands in `RaytracingContent`, after the template's `RaytracingCamera` line), so
  our `view=` wins. Verified by rendering the same part from `iso`/`front`/`top` and
  confirming the images differ.
- **SSL / virtualenv traceback on import.** On `import Render`, a background thread
  tries to bootstrap a Python venv over the network (for optional features). In a
  sandboxed/offline environment it fails with an SSL error and the thread dies — it
  is non-fatal and never reaches stdout (worker stderr is discarded), so the JSON
  protocol is unaffected.

### Reuse note (corrects the proposal)

The proposal said to "reuse `render.py`'s `_VIEWS` / `_camera_basis`." That is **not
importable** from the worker: `render.py` is a host-side NumPy/Pillow module, and the
`freecadcmd` worker imports only `FreeCAD`/`Part`/`ObjectsFem`/`Sketcher`. So the
worker carries its own pure-`App.Vector` copy of the view table (`_RENDER_VIEWS`) and
camera math (`_placement_from_view`), kept deliberately identical to `render.py`'s
basis so the two render paths frame a part the same way.

## 5. Implemented integration

Photoreal rendering runs **inside the FreeCAD process** (it needs the live document
and the `Render` package), so it is a worker handler, not a change to `render.py`.

### 5.1 Worker handler — [`driftpin/worker.py`](../driftpin/worker.py)

`@handler("render_photoreal")` (plus helpers `_placement_from_view`,
`_resolve_renderer_exec`, and the `_RENDER_VIEWS` / `_RENDERERS` tables). Key design
decisions beyond the API corrections in §3:

- **Temp-document isolation.** The handler copies the target shape into a throwaway
  `App` document, builds the Project/Camera/View graph *there*, renders, reads the
  PNG, and closes the temp doc — restoring the previously active document. The user's
  live model is never mutated and no Render objects leak into their saved `.FCStd`.
  (Trade-off: it renders the *shape*, so ViewObject colors/materials are dropped —
  which headless has none of anyway, per §4.)
- **Cross-platform renderer resolution.** `_resolve_renderer_exec` finds the binary
  via, in order: `DRIFTPIN_<RENDERER>_PATH` env override → path already set in FreeCAD
  prefs → `PATH` (`shutil.which`, which honors Windows `PATHEXT`) → common per-OS
  install dirs (`platform.system()`-keyed). It then writes the path into the FreeCAD
  param the plugin reads. **The param key is `PovRayPath` in group
  `User parameter:BaseApp/Preferences/Mod/Render`** — *not* `RenderExecPath` as the
  proposal claimed, and the plugin does *not* fall back to `PATH`, so DriftPin must
  set it.
- Returns `{png_base64, png_path, renderer, view, width, height}`.

### 5.2 MCP tool — [`driftpin/mcp_server.py`](../driftpin/mcp_server.py)

```python
@mcp.tool()
def render_photoreal(handle, renderer="Povray", view="iso", width=800, height=600):
    """Photorealistic render via the FreeCAD Render workbench (external renderer).
    Returns {png_base64, png_path, renderer, view, width, height}."""
    return _call("render_photoreal", _timeout=600.0,
                 handle=handle, renderer=renderer, view=view,
                 width=width, height=height)
```

- **Worker-call timeout raised to 600 s for this call.** `Worker.call` defaults to a
  120 s timeout (`client.py`); an external render legitimately takes seconds to
  minutes, and hitting that default raises `WorkerDied` and respawns a fresh worker,
  destroying the live document. `_call` now threads an optional `_timeout`, and
  `render_photoreal` passes 600 s.

## 6. Prerequisites (cross-platform)

1. **Install the addon** into the Mod dir FreeCAD actually reads. Don't hardcode it —
   derive it from `App.getUserAppDataDir()` (it honors `$XDG_DATA_HOME` on Linux), so
   the path varies by platform and even by shell sandbox:
   - Linux: `~/.local/share/FreeCAD/Mod/Render` (or `$XDG_DATA_HOME/FreeCAD/Mod/Render`)
   - macOS: `~/Library/Application Support/FreeCAD/Mod/Render`
   - Windows: `%APPDATA%\FreeCAD\Mod\Render`
   ```bash
   git clone https://github.com/FreeCAD/FreeCAD-render \
     "$(<derived Mod dir>)/Render"
   git -C "<…>/Render" checkout 08be2fe94b8a998323c8a5443f7f0afd0d05bed5   # pin
   ```
   (or Addon Manager → "Render", then check out the pinned commit.)
2. **Install one renderer binary** — POV-Ray for Phase 1:
   `apt install povray` (Linux) · `brew install povray` (macOS) · official installer (Windows).
3. **Nothing else** — DriftPin resolves the binary and sets `PovRayPath` itself (§5.1).
   Override with `DRIFTPIN_POVRAY_PATH=/full/path/to/povray` if it lives somewhere odd.

## 7. Resolved decisions & remaining follow-ups

Resolved (were open questions in the proposal):

- **Determinism.** Photoreal renders are not bit-reproducible, so `render_photoreal`
  stays *out* of the reliability/golden tests — it is presentation-only. Its tests
  ([`tests/test_render_photoreal.py`](../tests/test_render_photoreal.py)) assert
  invariants (valid PNG, non-blank, `view=` changes the image, live doc untouched).
- **CI.** The tests **skip** when the addon/binary is absent (exit 0), so they're safe
  everywhere; they only do real work on a box that provisions a renderer. They run in
  the self-hosted suite (`tests/run_all.sh`), alongside `test_render.py` — *not* the
  hosted nightly, which has neither Pillow nor a renderer.
- **Worker blocking.** Mitigated for normal use by the 600 s call timeout (§5.2). A
  true async/job variant remains a follow-up below.
- **Renderer choice.** POV-Ray-first, but the handler is renderer-agnostic: adding one
  is a single entry in the `_RENDERERS` registry (param key, default template, binary
  names, per-OS dirs). Unknown renderers raise with clear guidance.

Remaining follow-ups (not in Phase 1):

- **Real materials.** Phase 1 uses the default material. A follow-up would create
  Render `Material` `App`-objects and map DriftPin material metadata onto them.
- **Other renderers.** Only POV-Ray is verified end-to-end. LuxCore (PBR) is the
  natural next target for quality.
- **Async/job variant.** For renders that exceed even 600 s, a non-blocking job API so
  the worker isn't held hostage.
- **Upstream risk.** Decide if/when to fork or re-host the unmaintained addon, and what
  FreeCAD version range we commit to supporting (§2).
