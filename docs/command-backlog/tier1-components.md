# Tier 1 — parametric standard-component generators

Siblings of `add_gear`: take a few parameters, generate a solid, and **return
the mating/reference dimensions** so the coordinator can place neighbours
without guessing. Read [`README.md`](README.md) first. Each packet is one agent.

These are independent — dispatch all 7 in parallel.

| # | Command | Anchor (insert handler after) | Difficulty |
|---|---|---|---|
| 1.1 | `add_rack` | `add_gear` | low |
| 1.2 | `add_sprocket` | `helix` | medium |
| 1.3 | `add_pulley` | `loft` | medium |
| 1.4 | `add_spring` | `sweep` | low |
| 1.5 | `add_fastener` | `hole` | medium (needs a small table) |
| 1.6 | `add_bearing` | `revolve` | medium (needs a small table) |
| 1.7 | `oring_groove` | `pocket` | low |

---

## 1.1 `add_rack` — linear gear (the gear's straight counterpart)

**Signature:** `add_rack(teeth: int, module: float, height: float = 6.0, width: float = 10.0, pressure_angle: float = 20.0, placement: list | None = None, name: str = "Rack") -> dict`

**Worker approach.** A rack is a gear of infinite radius: straight-flanked teeth.
Build one tooth profile from the module and pressure angle, then linear-pattern
it `teeth` times at pitch `p = π·module`. Tooth geometry (standard full-depth):
addendum `= module`, dedendum `= 1.25·module`, so tooth height `= 2.25·module`;
flank angle `= pressure_angle` from vertical. Draw the rack rail as a 2D profile
(a base band of thickness ≥ dedendum plus the trapezoidal teeth on top) in the
XZ plane, `Part.Face` it, `extrude` by `height` to a solid (mirror `add_gear`'s
extrude + negative-volume reversal). `width` is the rail base thickness below the
root line.

**Returns:** `{handle, name, volume, pitch: module*π, module, teeth, tooth_height: 2.25*module, length: teeth*module*π}`.
`pitch` (mm/tooth) is the mating number — it must equal a meshing gear's
`module*π`, and `length` lets the coordinator size the rail.

**Test:** `m=2, teeth=10` → `length ≈ 62.83`, `pitch ≈ 6.283`, `volume > 0`,
handle starts `rack_`.

---

## 1.2 `add_sprocket` — roller-chain sprocket (ISO 606 / ANSI)

**Signature:** `add_sprocket(teeth: int, chain_pitch: float, roller_diameter: float, height: float = 6.0, placement: list | None = None, name: str = "Sprocket") -> dict`

**Worker approach.** Pitch diameter `PD = chain_pitch / sin(π/teeth)`. The tooth
gap is a circular pocket of radius ≈ `roller_diameter/2` centred on the pitch
circle, one per tooth, with the tooth tip between them. Simplest robust build:
make a disc of radius `PD/2 + chain_pitch*0.3` (outer/tip radius approximation),
then cut `teeth` roller seats — cylinders of radius `roller_diameter/2 * 1.05`
spaced on the pitch circle by `polar_pattern` of a single cut. Extrude to
`height`. (A true ISO 606 tooth form is involved; a roller-seat approximation is
acceptable for fit/visualisation and matches how `add_gear` approximates.)
Reuse `HANDLERS["polar_pattern"]` or just place cut cylinders in a loop.

**Returns:** `{handle, name, volume, pitch_diameter: PD, chain_pitch, teeth, tip_radius, bore: 0}`.
`pitch_diameter` + `chain_pitch` are the mating numbers (a chain of the same
`chain_pitch` wraps it; centre distance derives from two sprockets' PDs).

**Test:** `teeth=17, chain_pitch=12.7 (#40 chain), roller_diameter=7.92` →
`pitch_diameter ≈ 69.1`, `volume > 0`.

---

## 1.3 `add_pulley` — timing-belt (or V) pulley

**Signature:** `add_pulley(teeth: int, belt_pitch: float, width: float, flanged: bool = True, height: float | None = None, placement: list | None = None, name: str = "Pulley") -> dict`

**Worker approach.** Pitch diameter `PD = belt_pitch * teeth / π`. Build a
cylinder of radius `PD/2` and length = `width` (the belt face), cut `teeth`
tooth grooves around it (axial pockets on the pitch circle — same polar-cut
technique as the sprocket; groove ≈ half `belt_pitch` wide, ~`belt_pitch*0.4`
deep). If `flanged`, add two thin disc flanges (radius `PD/2 + 2·belt_pitch`,
thin) at each end via `boolean_op` fuse. `height` overrides `width` if both
given; default `height = width`.

**Returns:** `{handle, name, volume, pitch_diameter: PD, belt_pitch, teeth, width, flanged}`.
`pitch_diameter` sets centre distance with a mating pulley + belt length.

**Test:** `teeth=20, belt_pitch=2.0 (GT2), width=6` → `pitch_diameter ≈ 12.73`,
`volume > 0`; with `flanged=True` volume strictly greater than `flanged=False`.

---

## 1.4 `add_spring` — helical compression spring

**Signature:** `add_spring(wire_diameter: float, outer_diameter: float, free_length: float, coils: float, kind: str = "compression", placement: list | None = None, name: str = "Spring") -> dict`

**Worker approach.** You already have the pieces: build a helix wire and sweep a
circular wire-section along it.
- Mean coil radius `Rm = (outer_diameter - wire_diameter) / 2`.
- Pitch `= free_length / coils`. `helix = Part.makeHelix(pitch, free_length, Rm)`.
- Sweep a circle of radius `wire_diameter/2` (in the plane normal to the helix
  start) along the helix: `Part.Wire(helix).makePipeShell([circle_wire], True, True)`
  → solid. (`makePipeShell(profiles, make_solid=True, is_frenet=True)`.)
- For `kind="compression"`, optionally flatten the end coils later; not required
  for v1 — note it as a TODO comment.

Estimate spring rate (steel, G=79.3 GPa): `k = G·d⁴ / (8·D³·Na)` where `d`=wire
dia, `D`=mean dia, `Na`=active coils (≈ `coils`). Report it in N/mm — units:
d,D in mm, G in MPa (79300) → k in N/mm.

**Returns:** `{handle, name, volume, mean_diameter, free_length, coils, solid_height: coils*wire_diameter, spring_rate_n_per_mm}`.
`spring_rate` + `free_length` + `solid_height` are what a designer needs to spec
the spring into a mechanism.

**Test:** `wire_diameter=2, outer_diameter=20, free_length=40, coils=8` →
`volume > 0`, `solid_height ≈ 16`, `spring_rate_n_per_mm` within a sane band
(roughly 1–10 N/mm); handle starts `spring_`.

---

## 1.5 `add_fastener` — standard screw / nut / washer

**Signature:** `add_fastener(kind: str, size: str, length: float | None = None, placement: list | None = None, name: str | None = None) -> dict`
`kind ∈ {"socket_head_cap_screw", "hex_bolt", "hex_nut", "washer"}`; `size` like
`"M3"`, `"M4"`, `"M5"`, `"M6"`, `"M8"`, `"M10"`, `"M12"`. `length` = shank length
mm (screws/bolts only).

**Worker approach.** Embed a small ISO metric table (this is the only "data"
command — keep it inline, well-commented):

```python
# ISO metric: {size: (major_dia, pitch, head_dia, head_height, nut_width_af, nut_height, washer_od, washer_thk)}
_FASTENER = {
    "M3":  (3.0, 0.5, 5.5, 3.0, 5.5,  2.4, 7.0, 0.5),
    "M4":  (4.0, 0.7, 7.0, 4.0, 7.0,  3.2, 9.0, 0.8),
    "M5":  (5.0, 0.8, 8.5, 5.0, 8.0,  4.7, 10.0, 1.0),
    "M6":  (6.0, 1.0, 10.0, 6.0, 10.0, 5.2, 12.0, 1.6),
    "M8":  (8.0, 1.25, 13.0, 8.0, 13.0, 6.8, 16.0, 1.6),
    "M10": (10.0, 1.5, 16.0, 10.0, 16.0, 8.4, 20.0, 2.0),
    "M12": (12.0, 1.75, 18.0, 12.0, 18.0, 10.8, 24.0, 2.5),
}
```
(Verify a couple of rows against a current ISO 4762 / 4032 chart before relying
on them — these are representative, not gospel.) Build geometry from Part
primitives + fuse/cut:
- **socket_head_cap_screw:** cylinder head (`head_dia`×`head_height`) + shank
  cylinder (`major_dia`×`length`); cut a hex socket (a hex prism) into the head
  top. Threads optional — represent as plain shank (cosmetic), like a machined
  hole. Add `model_thread: False` to the return.
- **hex_bolt:** hex-prism head (across-flats `head_dia`) + shank cylinder.
- **hex_nut:** hex prism (`nut_width_af`×`nut_height`) with an axial clearance
  hole of `major_dia`.
- **washer:** annulus (`washer_od` OD, `major_dia` ID, `washer_thk` thick).

Default `name = kind.title()`. Validate `size` in table, `length` required for
screw/bolt.

**Returns:** `{handle, name, kind, size, major_diameter, pitch, length, head_diameter, head_height}` (drop irrelevant keys per kind).
These are the mating numbers: a `hole` of `major_diameter` (+ clearance) takes
the shank; `head_diameter` sizes a counterbore.

**Test:** `kind="hex_nut", size="M6"` → `major_diameter == 6.0`, has an axial
hole (volume < solid hex prism), handle starts `fastener_`. `kind="socket_head_cap_screw",
size="M3", length=10` → `volume > 0`, `head_diameter == 5.5`.

---

## 1.6 `add_bearing` — deep-groove ball bearing (envelope solid)

**Signature:** `add_bearing(designation: str | None = None, bore: float | None = None, outer_diameter: float | None = None, width: float | None = None, placement: list | None = None, name: str = "Bearing") -> dict`

**Worker approach.** Either look up `designation` in a small table or take
explicit `bore`/`outer_diameter`/`width`. A bearing for assembly purposes is an
**envelope**: an annular ring (OD cylinder minus bore cylinder), `width` long.
You don't need balls/races — the coordinator needs the fit envelope and the bore
shoulder. Optional cosmetic groove on the OD/bore faces; skip for v1.

```python
# common metric series: {designation: (bore, OD, width)}
_BEARING = {
    "608":  (8.0, 22.0, 7.0),    # skateboard
    "623":  (3.0, 10.0, 4.0),
    "624":  (4.0, 13.0, 5.0),
    "625":  (5.0, 16.0, 5.0),
    "626":  (6.0, 19.0, 6.0),
    "688":  (8.0, 16.0, 5.0),
    "6000": (10.0, 26.0, 8.0),
    "6200": (10.0, 30.0, 9.0),
    "6800": (10.0, 19.0, 5.0),
    "6900": (10.0, 22.0, 6.0),
}
```
If `designation` unknown and dims not all given, raise with the list of known
designations. Build: `outer = cylinder(OD/2, width)`, `inner = cylinder(bore/2,
width)`, `ring = outer.cut(inner)`.

**Returns:** `{handle, name, designation, bore, outer_diameter, width}`.
`bore` sizes the shaft; `outer_diameter` sizes the housing; `width` sets the
shoulder spacing.

**Test:** `designation="608"` → `bore == 8`, `outer_diameter == 22`,
`width == 7`, volume ≈ `π·(11²-4²)·7`; explicit dims path also works; handle
starts `bearing_`.

---

## 1.7 `oring_groove` — standard O-ring gland dimensions (+ optional cut)

**Signature:** `oring_groove(handle: str, face: str, cross_section: float, inner_diameter: float, gland_type: str = "static_radial", cut: bool = True, name: str = "ORingGroove") -> dict`

**Worker approach.** This is the calc designers always fumble. Given the O-ring
cross-section `w` (e.g. 1.78, 2.62 mm) compute the gland per a static seal rule
of thumb:
- groove depth `= w · 0.75` (≈ 25% squeeze; clamp to the standard squeeze band
  20–30%),
- groove width `= w · 1.3` (room for swell),
- corner radii small (`≤ 0.4`).
If `cut=True`, cut an annular groove of that width/depth into the named `face`
(resolve via `_h_resolve_face`) of `handle`, centred on `inner_diameter`. If
`cut=False`, just return the numbers (a pure calculator). Validate
`cross_section > 0`.

**Returns:** `{handle?, groove_depth, groove_width, groove_inner_diameter, groove_outer_diameter, squeeze_pct, cross_section}`.
Return `handle` only when `cut=True`.

**Test (calc-only):** `oring_groove(cross_section=2.62, inner_diameter=20, cut=False)`
→ `groove_depth ≈ 1.97`, `groove_width ≈ 3.4`, `20 < groove_outer_diameter`.
A `cut=True` test needs a host solid with a flat face — keep it to the calc path
if that's fiddly, and note the cut path as covered-by-inspection.
