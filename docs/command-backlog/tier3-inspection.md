# Tier 3 — measurement & inspection

The agent can't eyeball the model, so it leans on `mass_properties`,
`interference_check`, and renders. These close the inspection gaps every MCAD UI
has but DriftPin doesn't. They return measured data, **no handle** (except
`section_view`, which can optionally emit a section profile). Read
[`README.md`](README.md) first. Each packet is one agent. Dispatch all 6 in
parallel.

| # | Command | Anchor (insert handler after) | Difficulty |
|---|---|---|---|
| 3.1 | `measure_distance` | `mass_properties` | low |
| 3.2 | `measure_angle` | `query_faces` | low |
| 3.3 | `bounding_box` | `get_object` | low |
| 3.4 | `min_clearance` | `interference_check` | low |
| 3.5 | `check_shape` | `tessellate` | low |
| 3.6 | `section_view` | `export_shape` | medium |

---

## 3.1 `measure_distance` — minimum distance between two entities

**Signature:** `measure_distance(a: str, b: str, a_ref: str | None = None, b_ref: str | None = None) -> dict`
`a`/`b` are handles; `a_ref`/`b_ref` optionally narrow to a face/edge tag
(`f_*`/`e_*`) or `FaceN`/`EdgeN` on that handle.

**Worker approach.** The workhorse inspection tool. FreeCAD does the math:
```python
sa = sub_shape(a, a_ref)   # whole Shape, or shape.Faces[i]/Edges[i] if ref given
sb = sub_shape(b, b_ref)
dist, points, infos = sa.distToShape(sb)
```
`points[0]` is the (pt_on_a, pt_on_b) closest pair. Resolve sub-shapes via
`_h_resolve_face`/`_h_resolve_edge` (they give a 1-based index). `dist == 0`
means touching/intersecting.

**Returns:** `{distance_mm, point_on_a: [x,y,z], point_on_b: [x,y,z], touching: dist < 1e-7}`.

**Test:** two boxes 10 apart on X → `distance_mm ≈ 10`; same boxes overlapping →
`distance_mm == 0`, `touching True`. Face-to-face variant: top face of one to
bottom face of another.

---

## 3.2 `measure_angle` — angle between two faces or two edges

**Signature:** `measure_angle(a: str, a_ref: str, b: str, b_ref: str) -> dict`
refs required (`f_*` planar faces → angle between normals; `e_*` straight edges →
angle between directions).

**Worker approach.**
```python
fa = resolve face/edge on a; fb = on b
if faces:  va, vb = _outward_normal(fa), _outward_normal(fb)
if edges:  va, vb = edge_a.tangentAt(0), edge_b.tangentAt(0)  # or LastParameter dir
import math
ang = math.degrees(va.getAngle(vb))   # getAngle returns radians, 0..π
```
Report both `angle_deg` and its supplement `180 - angle_deg` (the agent often
wants the acute one). Raise if a ref isn't planar/linear.

**Returns:** `{angle_deg, supplement_deg, kind: "face"|"edge"}`.

**Test:** two faces of a box meeting at a corner → `angle_deg ≈ 90`; two parallel
faces → `angle_deg ≈ 0` or `180`.

---

## 3.3 `bounding_box` — AABB (and optional oriented BB) of one object

**Signature:** `bounding_box(handle: str, oriented: bool = False) -> dict`

**Worker approach.** `mass_properties` already returns an AABB buried in its
payload; expose a focused, cheap query.
```python
obj, shape = _shape_of(p["handle"])
bb = shape.BoundBox
out = {"min": [bb.XMin, bb.YMin, bb.ZMin], "max": [bb.XMax, bb.YMax, bb.ZMax],
       "size": [bb.XLength, bb.YLength, bb.ZLength],
       "center": [bb.Center.x, bb.Center.y, bb.Center.z], "diagonal": bb.DiagonalLength}
if p.get("oriented"):
    try: out["oriented"] = {...}  # shape.optimalBoundingBox() if available (FreeCAD ≥0.20)
    except Exception: out["oriented"] = None
```

**Returns:** `{min, max, size, center, diagonal, oriented?}`.

**Test:** box `w=10,d=20,h=5` → `size ≈ [10,20,5]`, `diagonal ≈ 22.9`.

---

## 3.4 `min_clearance` — closest approach between two solids (richer than the binary check)

**Signature:** `min_clearance(a: str, b: str) -> dict`

**Worker approach.** `interference_check` only says yes/no overlap. Designers
need the *gap*. Use `distToShape`, then classify:
```python
_, sa = _shape_of(a); _, sb = _shape_of(b)
common = sa.common(sb)
if common.Volume > 1e-9:  # interpenetrating
    return {"status": "interference", "overlap_volume_mm3": common.Volume, "clearance_mm": 0.0}
dist, pts, _ = sa.distToShape(sb)
return {"status": "clear" if dist > 1e-7 else "contact", "clearance_mm": dist,
        "point_on_a": [...], "point_on_b": [...]}
```

**Returns:** `{status: "clear"|"contact"|"interference", clearance_mm, overlap_volume_mm3?, point_on_a?, point_on_b?}`.

**Test:** boxes 3 apart → `status "clear"`, `clearance_mm ≈ 3`; overlapping →
`status "interference"`, `overlap_volume_mm3 > 0`; face-touching → `contact`,
`clearance_mm ≈ 0`. Reuse the fixtures from `test_assembly_interference_detected`.

---

## 3.5 `check_shape` — geometry validity / sanity (ROADMAP "Slice 3.5 deferred")

**Signature:** `check_shape(handle: str) -> dict`

**Worker approach.** Cheap guard so agents don't build on broken solids.
```python
obj, shape = _shape_of(p["handle"])
valid = shape.isValid()
out = {"valid": valid, "shape_type": shape.ShapeType, "closed": shape.isClosed(),
       "solids": len(shape.Solids), "shells": len(shape.Shells),
       "faces": len(shape.Faces), "edges": len(shape.Edges),
       "volume_mm3": shape.Volume, "is_null": shape.isNull()}
if not valid:
    try: shape.check(True); out["check"] = "see worker log"   # check() prints details to stderr
    except Exception as e: out["check_error"] = str(e)
out["watertight_solid"] = (out["solids"] == 1 and valid and shape.isClosed())
```
Don't auto-fix here (a `fix_shape` could be a later sibling) — just report.

**Returns:** `{valid, watertight_solid, shape_type, closed, solids, shells, faces, edges, volume_mm3, is_null, ...}`.

**Test:** a clean box → `valid True`, `watertight_solid True`, `solids == 1`. (A
deliberately broken solid is hard to construct cheaply — asserting the happy path
+ field presence is sufficient; note the negative path as covered-by-inspection.)

---

## 3.6 `section_view` — cut a solid with a plane, return the cross-section

**Signature:** `section_view(handle: str, plane: str = "XY", offset: float = 0.0, emit_profile: bool = False, name: str = "Section") -> dict`
`plane ∈ {"XY","XZ","YZ"}` or a datum-plane handle; `offset` shifts along the
plane normal.

**Worker approach.** Slice the solid and report the cut area (and optionally a
profile object for rendering/export):
```python
obj, shape = _shape_of(p["handle"])
normal, base = plane_to_normal_and_point(plane, offset)   # 'XY'->(0,0,1); base on it
wires = shape.slice(App.Vector(*normal), offset_along_normal)   # list of section wires
# area: build faces from closed wires and sum, or use shape.common(half-space) cross-section
area = sum(Part.Face(w).Area for w in wires if w.isClosed())
if p.get("emit_profile"):
    comp = Part.Compound(wires); out = doc.addObject("Part::Feature", name); out.Shape = comp
```
`shape.slice(normal, d)` returns the section wires at signed distance `d` along
`normal`. Also report the section bbox so the agent knows the extent. This
doubles as a much better render input than the 8 preset views (note that in
`notes`).

**Returns:** `{plane, offset, section_area_mm2, wire_count, bbox: {...}, handle?}`
(`handle` only when `emit_profile=True`).

**Test:** cylinder r=5 sliced on XY mid-height → `section_area_mm2 ≈ π·25 ≈ 78.5`,
`wire_count >= 1`; box 10³ sliced on XY → `section_area_mm2 ≈ 100`.
