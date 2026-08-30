# Kickoff — Real 2D Mechanical Drawings: Headless DXF/SVG/PDF + Dimensions

Written 2026-06-14. AnkusDrive could *build* a multi-view TechDraw page but not
**deliver** one: no headless export, no dimensions. This kickoff scoped, then
closed, both gaps.

> **Status (2026-06-14): DONE.** `export_drawing` now emits PDF, SVG, and DXF
> headless. `add_dimension` (auto extents + manual edge/point/diameter/radius,
> values read from the real geometry) and `add_annotation` are wired through the
> worker and MCP. Page composition (template + per-view fragments + dimension
> graphics) + svglib/reportlab rasterise to PDF — pure-Python, no native deps.
> Examples: `artifacts/drawings/{lbracket,plate}_demo.{pdf,svg,dxf}`, regenerate with
> `.venv/bin/python scratch/drawing_demo.py`. Tests in `tests/test_worker.py`
> (`test_export_drawing_three_formats`, `test_auto_and_manual_dimensions`, …).
> **This is scaffolding** — the dimension *palette* a manufacturer needs. The
> open follow-up is the **intelligence layer**: an agent deciding *which*
> measurements a part needs to be reliably reproduced (datums, GD&T, tolerances,
> minimal-complete dimension sets). See "Next layer" below.
>
> Build/env notes: the worker runs FreeCAD's bundled Python 3.11; `svglib`,
> `reportlab`, and a matching `Pillow` were installed into *its* site-packages
> (the host `.venv` is 3.12 — different ABI). `_prefer_self_site_packages()`
> reorders sys.path so reportlab binds the 3.11 PIL. The `.venv` ankusdrive install
> was stale (≈3000 lines behind source); it was refreshed to match HEAD.

## What works today vs. what's missing (confirmed by running it)

A live end-to-end check on 2026-06-14 (box → A4 page → Front/Top/Right/Iso group):

| Capability | Status | Evidence |
|---|---|---|
| `make_drawing_page` | ✅ works | Built A4 landscape page on auto-found template |
| `add_projection_group` | ✅ works | 4 views, third-angle, auto-scaled, persist in `.FCStd` |
| `export_drawing` → PDF/SVG | ❌ **stub** | Raises `NotImplementedError` (`worker.py:5888`) |
| Dimensions | ❌ **absent** | No `add_dimension` handler exists anywhere |

So against the literal goal — *multi-view PDF **with dimensions*** — the answer today
is **no**.

## The stub's premise is wrong — this is wiring, not building a renderer

`_h_export_drawing` assumes the only export path is `TechDrawGui` (GUI-only, blocked
under `freecadcmd`). That's false. The **console `TechDraw` module loads headless** and
exposes the primitives we need. Probed live on FreeCAD 1.1.0:

- `TechDraw.writeDXFPage(page, path)` → **wrote a real 8.2 KB DXF of the full page.** DXF
  export needs *no new dependency and barely any code.*
- `TechDraw.viewPartAsSvg(view)` / `TechDraw.projectToSVG(shape, dir)` → return valid SVG
  fragments per view, headless.
- `TechDraw.makeDistanceDim`, `makeExtentDim`, `makeDistanceDim3d`, `makeLeader` →
  dimension/annotation primitives, **all present headless.**

What's genuinely missing from the box: an **SVG→PDF rasterizer** (the environment has
none — no cairosvg/svglib/reportlab/inkscape/rsvg-convert), and the **page-composition
glue** that `TechDrawGui` normally does (place each view fragment on the template, render
dimension graphics, fill the title block).

## Decisions (locked 2026-06-14)

- **PDF backend: `svglib` + `reportlab`** — pure-Python pip deps, no native system
  libraries. Chosen to honour the cross-platform (Linux/macOS/Windows) constraint; cairosvg
  was rejected for pulling native cairo/pango.
- **Dimensions: both auto + manual** in the first cut.

## Scope

### 1. `export_drawing` — make it real (replace the stub at `worker.py:5888`)

Dispatch on path extension, mirroring `_h_export_shape` (`worker.py:2547`) /
`_h_save_document` (`worker.py:2495`). Return `{path, size, format, views}`.

- **`.dxf`** — direct: `TechDraw.writeDXFPage(page, path)`. Lowest risk; ship first.
- **`.svg`** — compose a full page SVG headless:
  1. Read the page's template SVG (already on disk; path returned by `make_drawing_page`).
  2. For each `DrawProjGroupItem` / `DrawViewPart`: get its fragment via
     `TechDraw.viewPartAsSvg(view)`, wrap in `<g transform="translate(X,Y) scale(s)">`
     using the view's `.X`, `.Y`, `.Scale`. **Watch the Y-axis flip** (FreeCAD page coords
     are Y-up; SVG is Y-down) and the template's mm→px units.
  3. Inject dimension graphics (see §2) and title-block field substitution.
- **`.pdf`** — render the composed page SVG via `svglib.svg2rlg` → `reportlab` canvas →
  PDF. (Fallback if SVG-composition fidelity bites: draw view fragments and dimensions
  directly onto a `reportlab` canvas, skipping full-page SVG.)

### 2. `add_dimension` + `add_annotation` (new handlers)

- `add_dimension(page, view, kind, refs)` — `kind ∈ {distance, horizontal, vertical,
  radius, diameter, angle}`. `refs` are **edge/face tags** resolved through the existing
  tag system: `_h_resolve_edge` / `_h_resolve_face` (`worker.py:2305`, `2286`) map a stable
  `e_…`/`f_…` blake2b tag → `Edge{n}`/`Face{n}`. Build the `DrawViewDimension` via
  `TechDraw.makeDistanceDim` / `makeExtentDim` and attach to the view's `References`.
- **Auto mode** — `add_dimension(page, auto=True)`: for each view, emit bounding-box extent
  dimensions (overall width/height) via `makeExtentDim`. Zero-config "dimensioned drawing."
- `add_annotation(page, text, position)` — `makeLeader` / a `DrawViewAnnotation`.
- **Open question / spike (the real risk):** does `viewPartAsSvg` render attached
  `DrawViewDimension`s, or only part geometry? If only geometry (likely), we render
  dimension graphics ourselves in the composer from each dim's measured value + `.X/.Y`
  label position + reference endpoints. Resolve this **first** — it decides whether §1 SVG
  and §2 are one job or two.

### 3. MCP tools (`mcp_server.py`)

`export_drawing` (1996) already passes through — it'll just stop raising. Add `@mcp.tool()`
wrappers for `add_dimension` / `add_annotation` following the `add_projection_group` pattern
(1979). Pure pass-throughs to `_call(...)`.

### 4. Dependencies (`pyproject.toml:42`)

Add `svglib>=1.5` and `reportlab>=4.0` to `dependencies` (or a `drawings` optional group).
**No `ezdxf`** — DXF goes through FreeCAD's own `writeDXFPage`.

### 5. Tests + artifact

Extend `test_drawing_page_constructed_and_persisted` (`test_worker.py:851`):
- Export `.dxf`/`.svg`/`.pdf`, assert non-empty file + valid magic bytes (`%PDF`, `<svg`,
  DXF `SECTION`).
- Auto-dimension a known box; assert ≥1 `DrawViewDimension` and that the SVG/PDF contains
  the expected extent value.
- Manual dimension via an edge tag from `list_edges`; assert it resolves and renders.
- Land a golden artifact (`artifacts/drawing_<part>.pdf`) like the other validation
  artifacts — "validate the artifact, not the model": open the PDF, confirm the dimensions
  read the real geometry.

## Phasing (de-risked order)

1. **DXF now** — `writeDXFPage`, ~20 lines, no deps, immediate win. Proves the headless
   `TechDraw` console path end-to-end.
2. **Dimension spike** — answer the `viewPartAsSvg`-renders-dimensions question.
3. **SVG composer** — template + view fragments (+ dimension graphics per spike outcome).
4. **PDF** — `svglib`+`reportlab` over the composed SVG.
5. **Auto + manual `add_dimension`**, `add_annotation`, MCP wrappers, tests, golden artifact.

## Next layer — *which* dimensions (the intelligence)

This kickoff delivered the **palette** and the **renderer**: the agent can place
any extent / linear / diameter / radius dimension and get a clean, correctly
oriented, true-valued drawing out. What it does **not** do is decide *which*
dimensions a part needs to be reliably manufactured. That is the next piece of
work, and it sits squarely on top of what's here:

- **Completeness** — every feature constrained, nothing redundant or
  over-dimensioned (a minimal complete set). Today the agent (or `auto=True`)
  just stacks overall extents + whatever it's told.
- **Datums & origin** — dimension *from* functional reference faces/edges, not
  arbitrary corners. The `annotate_face` role system (`AD_FaceRoles`) is the
  natural hook for "this is the datum."
- **Tolerances & GD&T** — fits, position, flatness. `DrawViewDimension`
  supports tolerance fields; `gdt_check`/`tolerance_stackup` already exist to
  lean on.
- **Manufacturability awareness** — a turned part wants Ø + length from a face;
  a sheet part wants hole pattern + edge distances. Different processes →
  different dimension schemes.
- **Validate-the-artifact** — the displayed number already reads the real solid
  (AD_TrueValue). The next gate: does the *set* of dimensions fully reconstruct
  the part? A drawing that under-dimensions is as wrong as a green-but-wrong sim.

The renderer is deliberately a thin, predictable primitive so this layer can
drive it without fighting layout. Known rough edges it may want to improve:
multi-dim collision avoidance is offset-stacking only (no true label packing);
section/auxiliary views aren't composed yet; the SVG title-block fields aren't
filled from BOM/metadata.

## Anchors

- Implemented: `_h_export_drawing`, `_compose_page_svg`, `_dim_to_svg`,
  `_h_add_dimension`, `_h_add_annotation` in `ankusdrive/worker.py`.
- Page/group builders: `worker.py:5819`, `5856`. Handler pattern: `@handler(name)` →
  `HANDLERS` (`worker.py:127`).
- Export templates: `_h_export_shape` `worker.py:2547`, `_h_save_document` `worker.py:2495`.
- Tag resolution for `refs`: `_h_resolve_edge` `worker.py:2305`, `_h_resolve_face` `2286`;
  `list_edges`/`list_faces` `2227`/`2221`.
- MCP layer: `mcp_server.py:1969-1999`. Deps: `pyproject.toml:42`. Test: `test_worker.py:851`.
