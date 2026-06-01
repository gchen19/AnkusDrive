# Tier 2 — everyday feature ops

Thin wrappers over FreeCAD APIs the codebase is already adjacent to, closing
obvious symmetry holes (you have `fillet_edges` but no direct chamfer; `thickness`
only for PartDesign bodies; threads referenced everywhere but never cut). Read
[`README.md`](README.md) first. Each packet is one agent. Dispatch all 8 in
parallel.

| # | Command | Anchor (insert handler after) | Difficulty |
|---|---|---|---|
| 2.1 | `chamfer_edges` | `fillet_edges` | low |
| 2.2 | `shell_solid` | `thickness` | low |
| 2.3 | `add_thread` | `draft` | high |
| 2.4 | `engrave_text` | `add_sketch_external` | medium (font file) |
| 2.5 | `add_rib` | `partdesign_chamfer` | medium |
| 2.6 | `transform` | `set_property` | low |
| 2.7 | `scale_shape` | `mirrored` | low |
| 2.8 | `copy_shape` | `set_visibility` | low |

---

## 2.1 `chamfer_edges` — direct-shape chamfer (mirror of `fillet_edges`)

**Signature:** `chamfer_edges(handle: str, edges: list, size: float = 1.0, name: str = "Chamfer") -> dict`

**Worker approach.** Copy `_h_fillet_edges` (worker.py:493) almost verbatim,
swapping `Part::Fillet` for `Part::Chamfer`:
```python
chamfer = doc.addObject("Part::Chamfer", "Chamfer")
chamfer.Base = obj
chamfer.Edges = [(i, size, size) for i in edges]   # (edge_idx, dist1, dist2)
```
Reuse the exact same edge-ref resolution loop (tags `e_*` → index via
`_h_resolve_edge`, `EdgeN` strings, bare ints). `_set_visibility(obj, False)` on
the base like fillet does implicitly via consumption.

**Returns:** `{handle, name, volume, edges}` (same shape as `fillet_edges`).

**Test:** box, chamfer 4 vertical edges by 2 → `volume` < original box volume,
handle starts `chamfer_`. Mirror `test_partdesign_fillet_by_tag` style.

---

## 2.2 `shell_solid` — hollow a direct solid (mirror of `thickness`, for raw shapes)

**Signature:** `shell_solid(handle: str, faces: list, thickness: float, name: str = "Shell") -> dict`
`faces` = tags/indices of faces to **remove** (the openings); `thickness` mm,
positive = shell inward.

**Worker approach.** The existing `thickness` handler is PartDesign-only. For a
direct `Part` solid use the shape method:
```python
obj, shape = _shape_of(p["handle"])
faces_to_remove = [shape.Faces[idx-1] for idx in resolved_indices]  # 1-based tags → 0-based
hollow = shape.makeThickness(faces_to_remove, -abs(thickness), 1e-3)
out = doc.addObject("Part::Feature", name); out.Shape = hollow
```
Resolve face refs through `_h_resolve_face` exactly like `fillet_edges` does for
edges. Negative offset shells inward (keeps outer dims). Validate `faces`
non-empty and `thickness > 0`.

**Returns:** `{handle, name, volume, wall_thickness, removed_faces}`.

**Test:** box 20³, remove top face, thickness 2 → result `volume` ≈ wall volume
(much less than 8000, > 0), `wall_thickness == 2`.

---

## 2.3 `add_thread` — real helical thread on a cylinder / in a hole  ⚠ hardest packet

**Signature:** `add_thread(handle: str, diameter: float, pitch: float, length: float, internal: bool = False, starts: int = 1, placement: list | None = None, name: str = "Thread") -> dict`

**Worker approach.** Today `hole`/`list_thread_options` only *flag* threads
(`model_thread`); nothing cuts a real helix. Build it:
1. `helix = Part.makeHelix(pitch, length, diameter/2)`.
2. Sweep a triangular ISO thread profile (60° flank, height `H = pitch·0.866`,
   truncated to `5H/8`) along the helix with `makePipeShell([profile_wire], True,
   True)` → a helical rib solid.
3. **External:** `cylinder(diameter/2, length).cut(rib)` for the thread valleys,
   or fuse the rib onto a minor-diameter cylinder — pick whichever yields a valid
   solid (test `.isValid()`; major/minor: external major = `diameter`, minor =
   `diameter - 1.0825·pitch`).
4. **Internal:** the rib becomes the cutting tool against a bore — return the
   tool, or cut it from the caller's solid if `handle` resolves to one.
5. `starts > 1` → multi-start: repeat the helix rotated by `360/starts`.

This is genuinely fiddly (pipe-shell on a helix is finicky; watch for
self-intersection at the ends — trim the profile slightly inside `length`).
Budget time, and **prefer a valid simpler solid over a perfect-but-broken one**.
A cosmetic fallback (annotate `model_thread=True`, return spec, skip the cut) is
acceptable if a valid swept solid can't be produced — say so in `notes`.

**Returns:** `{handle, name, volume, major_diameter, minor_diameter, pitch, length, starts, internal, modeled: bool}`.

**Test:** `diameter=8, pitch=1.25, length=10` external → `volume > 0`,
`.isValid()` true (assert via a follow-up `mass_properties` returning
`volume_mm3 > 0`), `minor_diameter < 8`. If you ship the cosmetic fallback,
assert `modeled is False` and the spec fields are present.

---

## 2.4 `engrave_text` — emboss/engrave text onto a face

**Signature:** `engrave_text(handle: str, face: str, text: str, size: float = 5.0, depth: float = 0.5, mode: str = "engrave", position: list | None = None, font: str | None = None, name: str = "Text") -> dict`
`mode ∈ {"engrave"(cut), "emboss"(add)}`.

**Worker approach.** Use Draft's ShapeString:
```python
import Draft
ss = Draft.make_shapestring(String=text, FontFile=font_path, Size=size)
doc.recompute()
solid = ss.Shape.extrude(App.Vector(0, 0, depth))   # then position onto the face
```
The gotcha is `FontFile`: pick a default that exists headless — probe a list
(`/System/Library/Fonts/Supplemental/Arial.ttf`,
`/Library/Fonts/Arial.ttf`, `/System/Library/Fonts/Helvetica.ttc`) and raise a
clear error if none found / `font` not given. Place the extruded text on the
resolved `face` (use `_h_resolve_face` for the face centroid + `_outward_normal`
to orient; `position` is an optional in-face [u,v] offset in mm). Then
`mode=="engrave"` → `boolean_op` cut from `handle`; `"emboss"` → fuse.
Remove the transient ShapeString object after extruding (like `add_gear` drops
its temp profile).

**Returns:** `{handle, name, volume, text, mode, depth}`.

**Test:** box, engrave `"M3"` depth 0.5 → result `volume` < box volume (material
removed), handle starts `text_`. Guard the test so it skips cleanly (not fails)
if no system font is found — print a SKIP line.

---

## 2.5 `add_rib` — PartDesign rib/web between walls

**Signature:** `add_rib(body: str, sketch: str, thickness: float, midplane: bool = True, reversed: bool = False, name: str = "Rib") -> dict`

**Worker approach.** Mirror the `pad` handler (worker.py:1248): resolve the body
(`_resolve_body`) and an open sketch profile (a single line/arc defining the rib
spine), then:
```python
rib = body.newObject("PartDesign::Rib", name)
rib.Profile = sketch_obj
rib.Width = float(thickness)
rib.Midplane = bool(midplane)
rib.Reversed = bool(reversed)
```
Rib thickens an open profile until it hits surrounding material. Recompute,
register, hide the sketch. If `PartDesign::Rib` proves unavailable/finicky
headless, fall back to: pad the profile both directions by `thickness/2`
(`Midplane`) and document the substitution in `notes`.

**Returns:** `{handle, name, volume, thickness}`.

**Test:** body with two parallel walls + a spanning open sketch → rib fuses them,
`volume` increases vs. the pre-rib body; handle starts `rib_`.

---

## 2.6 `transform` — move / rotate an existing object by handle

**Signature:** `transform(handle: str, translate: list | None = None, rotate_axis: list | None = None, angle: float = 0.0, relative: bool = True) -> dict`

**Worker approach.** Today the only way to move something is poking `Placement`
via `set_property` with a hand-built dict — clumsy. Make it first-class:
```python
obj = _resolve(p["handle"])
trans = App.Vector(*(p.get("translate") or [0, 0, 0]))
axis = App.Vector(*(p.get("rotate_axis") or [0, 0, 1]))
rot = App.Rotation(axis, float(p.get("angle", 0.0)))
delta = App.Placement(trans, rot)
obj.Placement = (delta.multiply(obj.Placement) if relative else delta)
doc.recompute()
```
`relative=True` composes onto current placement; `False` sets absolute. Angle in
degrees (`App.Rotation(axis, deg)` takes degrees). No new handle — same object
moves; return its current placement.

**Returns:** `{handle, name, placement: {base:[x,y,z], axis:[..], angle_deg}}`.

**Test:** box, `transform(translate=[10,0,0])` then `mass_properties` →
`center_of_mass_mm[0]` shifted by 10; rotate 90° about Z about a known box →
bbox swaps as expected.

---

## 2.7 `scale_shape` — uniform/non-uniform scale (bakes a static solid)

**Signature:** `scale_shape(handle: str, factor: float | list, center: list | None = None, name: str = "Scaled") -> dict`
`factor` = scalar (uniform) or `[sx, sy, sz]`.

**Worker approach.** Scaling breaks parametric history, so produce a fresh static
`Part::Feature` from the transformed shape:
```python
obj, shape = _shape_of(p["handle"])
m = App.Matrix()
if isinstance(factor, (int, float)): m.scale(factor, factor, factor)
else: m.scale(*factor)
scaled = shape.transformGeometry(m)   # NOT .transformShape — geometry must change
out = doc.addObject("Part::Feature", name); out.Shape = scaled
```
If `center` given, translate to origin, scale, translate back (compose matrices).
Validate factors > 0.

**Returns:** `{handle, name, volume, factor}`. Note `volume` should scale by
`sx·sy·sz` vs. the source — a good assertion.

**Test:** box 10³ (vol 1000), uniform `factor=2` → `volume ≈ 8000`; handle starts
`scaled_`.

---

## 2.8 `copy_shape` — duplicate a shape (optionally placed)

**Signature:** `copy_shape(handle: str, placement: list | None = None, name: str | None = None) -> dict`

**Worker approach.** Cheap and constantly needed (mirror/pattern need a seed
copy, assemblies need duplicate instances that aren't `App::Link`s).
```python
obj, shape = _shape_of(p["handle"])
out = doc.addObject("Part::Feature", p.get("name") or (obj.Name + "_copy"))
out.Shape = shape.copy()
if p.get("placement"): out.Placement.Base = App.Vector(*p["placement"])
```
Independent geometry (not a link) — edits to the original don't propagate, which
is the point vs. `add_part`'s links. Note the distinction in the docstring.

**Returns:** `{handle, name, volume}`.

**Test:** box, copy with `placement=[50,0,0]` → two independent solids, copy's
`center_of_mass_mm[0]` ≈ original + 50; handle starts `feature_`/`copy_`
(pick a prefix and assert it).
