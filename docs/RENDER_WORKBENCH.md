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

## 6. PNG output — two channels

`Project.render()` writes an image file to disk (POV-Ray / LuxCore / etc. all emit
PNG) and returns its path. The worker reads those bytes, so every render is
available **both** ways:

- **`png_base64`** — inline, same shape as today's `render_view`, so the agent can
  *see* the result in-loop (and the `show-in-vscode` skill can open it).
- **`png_path`** — when the caller passes `output_path`, the PNG (and the raw
  renderer scene file, for debugging) is persisted there. Renders are slow and
  worth keeping; base64-only would be wasteful for a "final" pass.

The return payload carries provenance so the agent can reason about cost/quality:
`{png_base64, png_path, renderer, scene, view, samples, elapsed_s, width, height}`.

## 7. The MCP contract — choosing the "rendering situation"

**Design principle: the agent speaks intent, never renderer syntax.** No POV-Ray
finishes or LuxCore node graphs cross the tool boundary. The minimal §5.2
`render_photoreal` is the P0 plumbing proof; the shipping surface is a richer
`render_scene` with three orthogonal knobs plus a discovery tool:

```python
render_scene(
    target,                    # handle OR assembly handle
    scene   = "studio",        # lighting + environment + background
    view    = "iso",           # preset OR {azimuth, elevation, distance, perspective, fov}
    quality = "preview",       # time/samples budget: draft | preview | final
    width=1280, height=720,
    output_path=None,          # also write PNG here
    renderer="auto",           # "auto" picks the best installed engine
    cmf_overrides=None,        # per-component appearance, see §8
)  # -> {png_base64, png_path, renderer, scene, samples, elapsed_s, ...}
```

**`scene`** is the heart of "what situation" — a small set of named, renderer-neutral
setups, each realized as a workbench template + Light objects + groundplane +
environment:

| `scene` | Looks like | Built from |
|---|---|---|
| `studio` | Seamless backdrop, soft 3-point key/fill/rim — product hero shot | template + AreaLights + sweep groundplane |
| `workshop` | Even matte lighting, faint contact shadow — engineering/spec look | template + DistantLight + neutral ground |
| `outdoor` | Sun + sky, horizon ground — context/marketing | SunskyLight + groundplane |
| `hdri` | Image-based lighting from an environment map | ImageLight (+ supplied `.hdr`) |
| `xray` / `section` | Internals visible — semi-transparent or cutaway | transparency override / `section_view` plane |

**`quality`** is the time-budget knob (`draft` → `preview` → `final`, mapping to
samples / denoise / resolution-scale). The returned `samples` / `elapsed_s` let the
agent learn the trade-off and decide whether to re-render larger.

**Discovery — `render_capabilities()`** (mirrors the existing `list_thread_options`
pattern): returns installed renderers, available `scene` names + descriptions,
quality presets, and material-library names. This is *how the agent determines the
situation* — it introspects the menu instead of guessing, then maps user intent →
preset.

Optional sugar (open question): a **`purpose=`** alias
(`"inspection" | "documentation" | "marketing"`) that sets sensible `scene`+`quality`
defaults the agent can still override — lets the model say "nice picture for the
README" without learning the preset taxonomy.

## 8. Per-component CMF (Color / Material / Finish)

Render materials are `App::MaterialObjectPython` objects (`make_material(name, color,
transparency)`) holding a **renderer-neutral dict** — `basecolor` / `metallic` /
`roughness` plus per-renderer `Render.<engine>.*` overrides. Only the
ViewProvider / task-panel is GUI-gated, so **the material object itself is
headless-creatable** — which is what makes per-component CMF possible under
`freecadcmd`.

```python
set_appearance(
    handle,
    material = "aluminum_6061_brushed",   # name from the Render material library (renderer-neutral)
    color    = [0.8, 0.8, 0.82],          # OR generic PBR override
    metallic = 1.0, roughness = 0.35,
    finish   = "brushed",                 # nudges roughness / anisotropy / clearcoat
)
```

- **Color → `basecolor`, Material → a library card** (sets metallic/roughness/
  transmission for steel/ABS/glass/…), **Finish → roughness/clearcoat tweak**
  (matte/satin/gloss/brushed). All land in the renderer-neutral dict; the workbench
  translates per engine, so the *same* CMF renders in POV-Ray or LuxCore.
- **It persists in the `.FCStd`** (material is an App object linked to the part).
  This is the key property for multi-agent: **`merge_assembly` preserves each
  component's CMF automatically** — no central re-skin needed.

Two authoring modes, both supported:

- **(a) Builder-owned** — each component agent calls `set_appearance` on its own
  part; CMF travels in the component file. Most autonomous; fits the "cold builder
  sees only its slice" model in [`MULTI_AGENT.md`](MULTI_AGENT.md).
- **(b) Coordinator art-direction** — `cmf_overrides={component_id: {material,
  color, ...}}` on `render_scene`, applied to the *merged* instances at render time
  without editing source files. Enforces a consistent palette ("all brackets
  anodized black") or re-themes for a different shot.

Wiring into the multi-agent contract: extend the manifest with an optional
`appearance` per component (so `decompose` can assign CMF intent up front); the
coordinator's `cmf_overrides` win at render time. The `merge_assembly` primitive
stays appearance-agnostic — CMF is just object state it carries.

## 9. Phasing

| Phase | Scope |
|---|---|
| **P0** | `render_photoreal` for a single handle: `studio` + `draft`/`final`, POV-Ray, PNG path+base64. Proves the headless camera/light/material plumbing (§5). |
| **P1** | Promote to `render_scene`: full `scene` set + custom camera + `render_capabilities` discovery. |
| **P2** | `set_appearance` + CMF persistence; render whole assemblies. |
| **P3** | `cmf_overrides` + manifest `appearance`; coordinator integration. |

## 10. Prerequisites (none present in dev env today)

1. Install the addon so `freecadcmd` auto-loads it:
   ```bash
   git clone https://github.com/FreeCAD/FreeCAD-render \
     "$HOME/Library/Application Support/FreeCAD/Mod/Render"
   ```
   (or Addon Manager → "Render"). **Pin a commit** rather than tracking `master`.
2. Install one renderer binary (POV-Ray recommended for the first cut).
3. Set its `RenderExecPath` param (§4).

## 11. Risks & open questions for reviewers

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
- **CMF source of truth (§8).** Component file (mode a), manifest + overrides
  (mode b), or both? Lean: both, with `cmf_overrides` winning at render time.
- **Material vocabulary (§8).** Adopt the Render WB's library card names as the
  contract, or define our own renderer-neutral CMF schema and translate? Library
  is faster to ship; our own schema is more stable against the unmaintained
  upstream.
- **`purpose=` auto-preset (§7).** Worth the extra surface area, or keep `scene` +
  `quality` explicit?
- **Assembly rendering (§7).** One combined image, or also per-component contact
  sheets?
