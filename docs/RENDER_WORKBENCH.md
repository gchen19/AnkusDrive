# Photoreal rendering via the FreeCAD Render workbench (proposal)

A proposal — for review with other contributors — to give DriftPin a
*photorealistic* render path by stitching in the third-party
[FreeCAD Render workbench](https://github.com/FreeCAD/FreeCAD-render) and exposing
it as an MCP tool (`render_photoreal`) alongside the existing software-rasterized
[`render_view`](../driftpin/render.py).

**Status:** plan only. Nothing in this document is implemented. The addon and a
renderer binary are *not* installed in the current dev environment. This doc
exists so we can decide together whether to take it on, given the maintenance
risk below.

---

## 1. Why, and what we have today

DriftPin's current render path is a deliberate "can the agent *see* what it just
built?" tool, not a presentation tool:

- `render_view` / `render_views` (MCP) → worker `tessellate` handler returns
  vertices + triangles → host-side [`render.py`](../driftpin/render.py)
  rasterizes in pure NumPy + Pillow.
- Flat Lambertian shading, orthographic, fixed light, painter's/z-buffer hybrid.
  No materials, no GI, no perspective. Fast, deterministic, zero external deps.

That's the right tool for closed-loop reliability checks (Layer A/D). It is the
wrong tool for "show me a nice picture of the part." Photoreal output (materials,
lighting, global illumination, perspective camera) is a separate concern and
should be a separate tool — not a flag on `render_view`.

## 2. Maintenance status of the upstream workbench — read this first

The Render workbench is **frozen / in maintenance limbo**:

- The maintainer **Howetuft announced discontinuation on 2025-11-12**
  ("I am discontinuing maintenance of this workbench as of now") and is seeking a
  successor. None has stepped up.
- As of mid-2026: ~226★, ~3,900 commits, **15 open issues, 2 open PRs**, issues
  still being filed (people use it; nobody merges). Still listed in the official
  FreeCAD Addon Manager and still functional.

**Implication for us:** it is safe to *consume* (we depend on a stable Python
API, not on a roadmap) but risky *long-term*. The concrete failure mode is
FreeCAD 1.1 / 2.0 eventually drifting out from under it with nobody upstream to
patch — the same class of breakage we already track in
`docs/`-adjacent notes on FreeCAD API drift. If we adopt it, we should pin a
known-good commit and treat the integration as something we may have to fork or
re-host.

## 3. How the workbench works (architecture)

It is a **pure-Python workbench**. The entire scene — `Project`, `Camera`,
`View`, lights, materials — is a graph of FreeCAD `App` document objects. On
render it serializes that graph into a renderer-specific scene file and
**shells out to an external renderer binary**:

| Renderer | macOS install | Notes |
|---|---|---|
| POV-Ray | `brew install povray` | Single CLI binary, deterministic, easiest. Lower photoreal ceiling. |
| LuxCoreRender | hand-fetched build | Highest quality + PBR materials; heavier/slower headless. |
| Appleseed | hand-fetched build | `appleseed.cli` headless renderer. |
| Cycles (standalone) | hand-fetched build | Blender's engine; fiddliest to wire on macOS. |
| Ospray / pbrt-v4 | hand-fetched | pbrt-v4 marked experimental upstream. |

The workbench itself rasterizes nothing — **it's a scene exporter + process
launcher.** That is exactly what makes it embeddable in DriftPin's worker, *if*
we can drive it without the GUI.

Public Python API (read from upstream `project.py` / `commands.py`):

```python
proj = Render.Project.create(doc, renderer="Povray", template=...)  # template optional
cam  = Render.Camera.create(doc)
proj.Proxy.add_views([cam, obj])              # camera + part are both "views"
out  = proj.Proxy.render(wait_for_completion=True, skip_meshing=False)  # -> image path
```

## 4. The headless question (the crux)

DriftPin's worker runs inside `freecadcmd` — **no GUI, `App.GuiUp == False`**.
The upstream render path is gated on `App.GuiUp`, and three behaviors change
when there is no GUI. None is a blocker; each just means we build scene state as
`App` objects instead of borrowing it from a viewport:

| Concern | With GUI | Headless — what DriftPin must do |
|---|---|---|
| **Camera** | grabs `Gui.ActiveDocument.ActiveView.getCamera()` | No viewport → **add an explicit `Camera` object and set its `Placement`.** Reuse `render.py`'s `_VIEWS` / `_camera_basis` + bbox auto-fit to aim it. (Same discipline as the post-save camera injection we already do for headless saves.) |
| **Visibility filter** | renders only views with `ViewObject.Visibility` | Falls back to **all** views. No action needed. |
| **Materials / colors** | reads `ViewObject.ShapeColor` etc. | No ViewObject colors → falls back to **default material**. Real materials require creating Render `Material` `App`-objects explicitly. Acceptable for a first cut. |

Set the renderer binary path via FreeCAD params (no prefs UI needed):

```python
App.ParamGet("User parameter:BaseApp/Preferences/Mod/Render/PovRay")\
   .SetString("RenderExecPath", "/opt/homebrew/bin/povray")
```

## 5. Proposed integration

Photoreal rendering must run **inside the FreeCAD process** (it needs the live
document + the `Render` package), unlike the host-side NumPy rasterizer. So it
is a new worker handler, not a change to `render.py`.

### 5.1 New worker handler — `driftpin/worker.py`

```python
@handler("render_photoreal")
def _h_render_photoreal(p):
    import Render
    doc = _active_doc()
    obj = _resolve(p["handle"])                      # existing handle -> object resolve
    proj = Render.Project.create(doc, renderer=p.get("renderer", "Povray"))
    cam = Render.Camera.create(doc)
    cam.Placement = _placement_from_view(p, obj)     # reuse render.py view dirs + bbox fit
    proj.Proxy.add_views([cam, obj])                 # camera + part as views
    # optional: Render.SunskyLight.create(doc) if the template lacks lighting
    out = proj.Proxy.render(wait_for_completion=True)
    with open(out, "rb") as f:
        return {"png_path": out, "png_base64": base64.b64encode(f.read()).decode()}
```

### 5.2 New MCP tool — `driftpin/mcp_server.py`

Mirror `render_view`'s signature/return shape so it's a drop-in for agents:

```python
@mcp.tool()
def render_photoreal(handle, renderer="Povray", view="iso", width=800, height=600):
    """Photorealistic render of a shaped object via the FreeCAD Render workbench
    (external renderer). Returns {png_base64, renderer, view}."""
    return _call("render_photoreal", handle=handle, renderer=renderer,
                 view=view, width=width, height=height)
```

## 6. Prerequisites (none present in dev env today)

1. Install the addon so `freecadcmd` auto-loads it:
   ```bash
   git clone https://github.com/FreeCAD/FreeCAD-render \
     "$HOME/Library/Application Support/FreeCAD/Mod/Render"
   ```
   (or Addon Manager → "Render"). **Pin a commit** rather than tracking `master`.
2. Install one renderer binary (POV-Ray recommended for the first cut).
3. Set its `RenderExecPath` param (§4).

## 7. Risks & open questions for reviewers

- **Upstream is unmaintained.** Do we pin + vendor, or fork under FreeCAD-org or
  our own org? What FreeCAD version do we commit to supporting?
- **Determinism.** Photoreal renders are not bit-reproducible (sampler noise,
  thread count). This tool must therefore stay *out* of the reliability golden
  tests — it is presentation-only. Agreed?
- **CI.** No renderer binary in CI → this tool can't be smoke-tested the way
  producers are. Do we gate it behind a nightly/optional lane, or leave it
  un-CI'd and manually verified?
- **Worker blocking.** `wait_for_completion=True` blocks the worker for the
  duration of an external render (seconds to minutes). Do we need an async/job
  variant so the worker isn't held hostage?
- **Renderer choice.** Start POV-Ray-only, or design the handler renderer-agnostic
  from day one (it nearly is — `renderer=` is already a param)?
- **Materials.** First cut uses default material headless. Is that good enough to
  ship, with explicit Render `Material` objects as a follow-up?
