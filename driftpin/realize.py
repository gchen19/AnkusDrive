"""Geometry-realizes-declaration oracle (RFC §11.10) — does the METAL back the claim?

The motion oracle (:mod:`driftpin.mechanism`, §11.9) judges the declared *topology*:
given that the output gears freewheel and a dog clutch engages one per speed, the
train has a determinate single-DOF power path at the design ratio. It takes the
declaration on faith. Three times in the gearbox arc a green validation passed while
the exported geometry was actually wrong, because the check ran on a *model of the
mechanism* rather than the *artifact itself* (see docs/KICKOFF_validate_the_artifact.md):

  * a gear declared ``keyed`` was bored ROUND onto a plain shaft — no torque path;
  * a dog collar declared ENGAGED had a SOLID FACE where the dog gaps belonged — it
    could not interlock; the gear teeth jammed into it (250 mm³ of overlap where an
    interleaved clutch is a few mm³). A human eye caught it; no gate did.

This module is the missing layer: given the exported CAD and the declaration, it
verifies the geometry *implements* the topology. It is deliberately FreeCAD-bound —
it consumes the real ``Part.Shape``, never a separate idealised model — so it cannot
be fooled by a clean abstraction the way a from-scratch PyBullet rig was.

Two primitives, both read directly off the shape about its own axis (so they are
placement-independent — a property of the part, not of one assembled pose):

  * **bore keying** — a ``keyed`` bore carries a RADIAL FLAT (the D-key / keyway
    chord) near the axis; a ``freewheel`` bore is ROUND (no such flat). This is the
    geometry behind "real torque path" vs "loose gear on a rod".
  * **dog ring** — a real dog ring has N teeth with GAPS between them: sampling the
    material angularly at the dog radius gives a fill fraction near 0.5 and N distinct
    arcs. A solid face gives fill 1.0 and a single 360° arc. This is the geometry
    behind "interlocks" vs "solid face where the gaps belong".

Thresholds are calibrated on real CAD in scratch/calibrate_realize.py (interleaved
fill ≈ 0.47, solid face = 1.0; the band below is generous around that gap).
"""
from __future__ import annotations

import math

import FreeCAD as App

# Calibrated on scratch/calibrate_realize.py: an interleaved dog ring samples ~0.47
# fill; a solid face samples 1.0. The midline 0.80 is a wide moat between them.
_SOLID_FACE_FILL = 0.80     # fill >= this at the dog radius == a solid face, not teeth
_RING_MIN_FILL = 0.15       # below this there is essentially no material -> not a ring
_RADIAL_NORMAL_TOL = 0.20   # |n . axis| below this == a radial (D-flat) face normal
_NEAR_AXIS_MARGIN = 1.5     # mm a bore feature may sit beyond its nominal radius
# Relative-phase (interleave) of two engaged rings: half-pitch teeth fall in each
# other's gaps so the co-occupied fraction is ~0; teeth IN PHASE co-occupy ~the tooth
# fill (~0.45). 0.12 is a wide moat between them (calibrated in calibrate_realize.py).
_INTERLEAVE_MAX_BOTH = 0.12  # both-occupied fraction >= this == teeth in phase (jam)
_INTERLEAVE_MIN_FILL = 0.10  # each ring must actually carry teeth in the shared band


def _perp_basis(d: App.Vector):
    """Two orthonormal vectors spanning the plane perpendicular to unit dir ``d``."""
    d = App.Vector(d); d.normalize()
    seed = App.Vector(1, 0, 0) if abs(d.x) < 0.9 else App.Vector(0, 1, 0)
    u = seed - d * seed.dot(d)
    u.normalize()
    v = d.cross(u)
    return u, v


def _shape_centroid(shape):
    """Centroid robust to compounds (a fused/boolean-cut part whose .CenterOfMass is
    not reliably exposed): volume-weighted over the constituent solids."""
    sols = shape.Solids or [shape]
    tv = sum(s.Volume for s in sols)
    if tv <= 0:
        return shape.BoundBox.Center
    return App.Vector(
        sum(s.Volume * s.CenterOfMass.x for s in sols) / tv,
        sum(s.Volume * s.CenterOfMass.y for s in sols) / tv,
        sum(s.Volume * s.CenterOfMass.z for s in sols) / tv)


def part_axis(shape):
    """(point, unit-dir) of a part's own rotation axis for an UNPLACED local shape:
    its local +Z, through the shape's xy-centroid. Dog rings and bores are built
    about this axis, so measuring about it is placement-independent."""
    c = _shape_centroid(shape)
    return App.Vector(c.x, c.y, 0.0), App.Vector(0, 0, 1)


def _dist_to_axis(pt, axis_pt, axis_dir):
    w = pt - axis_pt
    return (w - axis_dir * w.dot(axis_dir)).Length


def _circular_runs(flags):
    """Number of maximal runs of True in a CIRCULAR boolean list. All-True -> 1
    (a solid 360° ring); N teeth alternating with gaps -> N; all-False -> 0."""
    n = len(flags)
    if all(flags):
        return 1
    if not any(flags):
        return 0
    # rotate so index 0 starts a gap (a False), then count rising edges
    start = next(i for i in range(n) if not flags[i])
    rot = flags[start:] + flags[:start]
    return sum(1 for i in range(n) if rot[i] and not rot[i - 1])


def dog_ring_profile(shape, radius, z_lo, z_hi, *, axis_pt=None, axis_dir=None,
                     n_samples=360):
    """Angular material profile of ``shape`` on the cylinder at ``radius``, sampled
    midway up the axial band [z_lo, z_hi] (axial coordinates along the axis from
    ``axis_pt``). Returns {fill, sectors, n_samples}:

      fill     fraction of angular samples inside material (≈0.5 for an N-tooth ring
               with ~half-pitch teeth, 1.0 for a solid face, ~0 for empty space).
      sectors  number of distinct material arcs around the circle (N for a real ring,
               1 for a solid face).

    This is the discriminator the solid-face collar bug needed: a ring has gaps."""
    if axis_pt is None or axis_dir is None:
        axis_pt, axis_dir = part_axis(shape)
    u, v = _perp_basis(axis_dir)
    z_mid = 0.5 * (z_lo + z_hi)
    centre = axis_pt + axis_dir * z_mid
    flags = []
    for k in range(n_samples):
        th = 2.0 * math.pi * k / n_samples
        pt = centre + u * (radius * math.cos(th)) + v * (radius * math.sin(th))
        flags.append(bool(shape.isInside(pt, 1e-6, True)))
    fill = sum(flags) / float(n_samples)
    return {"fill": round(fill, 4), "sectors": _circular_runs(flags),
            "n_samples": n_samples}


def interleave_profile(shape_a, shape_b, radius, z_lo, z_hi, *, axis_pt=None,
                       axis_dir=None, n_samples=360):
    """Angular CO-OCCUPANCY of two parts sharing a frame (the engaged pose), at the dog
    ``radius``, sampled midway up the shared band [z_lo, z_hi]. Where dog_ring_profile
    asks of ONE part "are there gaps?", this asks of the PAIR "do a's teeth fall in b's
    gaps?" — it reads RELATIVE phase. Returns {a_fill, b_fill, both_fraction,
    union_fraction, n_samples}:

      a_fill / b_fill   each part's own angular fill here (both must be >0 to be engaged)
      both_fraction     angles where BOTH have material — ≈0 when teeth INTERLEAVE (a's
                        teeth sit in b's gaps), ≈the tooth fill when the rings are IN
                        PHASE (teeth-on-teeth: each ring still has gaps, but they jam).
      union_fraction    angles with either — ≈2× a single ring interleaved, ≈1× in phase.

    The axis defaults to ``shape_a``'s (an engaged clutch is coaxial); pass it for a
    pair whose centroid is not on the axis."""
    if axis_pt is None or axis_dir is None:
        axis_pt, axis_dir = part_axis(shape_a)
    u, v = _perp_basis(axis_dir)
    z_mid = 0.5 * (z_lo + z_hi)
    centre = axis_pt + axis_dir * z_mid
    a_n = b_n = both = union = 0
    for k in range(n_samples):
        th = 2.0 * math.pi * k / n_samples
        pt = centre + u * (radius * math.cos(th)) + v * (radius * math.sin(th))
        ai = bool(shape_a.isInside(pt, 1e-6, True))
        bi = bool(shape_b.isInside(pt, 1e-6, True))
        a_n += ai; b_n += bi
        both += ai and bi
        union += ai or bi
    f = float(n_samples)
    return {"a_fill": round(a_n / f, 4), "b_fill": round(b_n / f, 4),
            "both_fraction": round(both / f, 4), "union_fraction": round(union / f, 4),
            "n_samples": n_samples}


def bore_keying(shape, bore_radius, *, axis_pt=None, axis_dir=None):
    """Inspect a part's bore about its axis. Returns {has_radial_flat, has_round_bore,
    flat_count, bore_radius_seen}:

      has_radial_flat  a planar face with a RADIAL normal sits within the bore (the
                       D-key / keyway chord) -> the bore is KEYED, drives the shaft.
      has_round_bore   a cylindrical inner face near the axis at ~bore_radius exists
                       -> a real round hole (a freewheel bore, or the round part of a
                       D-bore).

    A ``keyed`` link must have a radial flat; a ``freewheel`` link must NOT (and must
    have a round bore)."""
    if axis_pt is None or axis_dir is None:
        axis_pt, axis_dir = part_axis(shape)
    flats, bore_seen = 0, None
    for f in shape.Faces:
        surf = f.Surface
        name = type(surf).__name__
        if name == "Plane":
            pr = f.ParameterRange
            n = f.normalAt((pr[0] + pr[1]) / 2.0, (pr[2] + pr[3]) / 2.0)
            # A D-key / keyway FLAT is a radial face whose chord sits INSIDE the round
            # bore (its centroid is closer to the axis than the bore wall). The crucial
            # exclusion: dog teeth / external features have radial faces too, but they
            # sit OUTSIDE the bore radius — so the strict `< bore_radius` cut keeps this
            # measuring the bore, not the dog band.
            if abs(n.dot(axis_dir)) < _RADIAL_NORMAL_TOL and \
                    _dist_to_axis(f.CenterOfMass, axis_pt, axis_dir) < bore_radius:
                flats += 1
        elif name == "Cylinder":
            # an inner cylindrical face about the part axis at ~bore_radius
            ax_parallel = abs(App.Vector(surf.Axis).dot(axis_dir)) > 0.9
            if ax_parallel and surf.Radius <= bore_radius + _NEAR_AXIS_MARGIN and \
                    _dist_to_axis(surf.Center, axis_pt, axis_dir) < _NEAR_AXIS_MARGIN:
                if bore_seen is None or abs(surf.Radius - bore_radius) < abs(bore_seen - bore_radius):
                    bore_seen = surf.Radius
    return {"has_radial_flat": flats > 0, "has_round_bore": bore_seen is not None,
            "flat_count": flats,
            "bore_radius_seen": None if bore_seen is None else round(bore_seen, 4)}


# --- per-part declaration validators (return a list of violations) -----------

def check_bore_keying(shape, decl):
    """Validate one bore against its declaration. ``decl`` = {role, keyed: bool,
    bore_radius_mm, axis_pt?, axis_dir?}. A keyed bore MUST carry a radial flat; a
    freewheel bore MUST be round with no flat."""
    role = decl.get("role", "part")
    r = bore_keying(shape, float(decl["bore_radius_mm"]),
                    axis_pt=decl.get("axis_pt"), axis_dir=decl.get("axis_dir"))
    keyed = bool(decl["keyed"])
    out = []
    if keyed and not r["has_radial_flat"]:
        out.append({"role": role, "keyed": True, **r,
                    "reason": f"{role} declared KEYED but its bore is ROUND (no radial "
                              f"flat / keyway) — a freewheel bore cannot drive the shaft"})
    if not keyed:
        if r["has_radial_flat"]:
            out.append({"role": role, "keyed": False, **r,
                        "reason": f"{role} declared FREEWHEEL but its bore carries a "
                                  f"radial flat (keyed) — it cannot spin on the shaft"})
        if not r["has_round_bore"]:
            out.append({"role": role, "keyed": False, **r,
                        "reason": f"{role} declared FREEWHEEL but has no round bore at "
                                  f"~{decl['bore_radius_mm']} mm about its axis"})
    return out


def check_dog_ring(shape, decl):
    """Validate one dog ring against its declaration. ``decl`` = {role, dog_radius_mm,
    z_lo_mm, z_hi_mm, n_dogs?, axis_pt?, axis_dir?}. A real dog ring has GAPS: the
    angular fill at the dog radius is well below a solid face's 1.0, and there are
    multiple distinct teeth. A solid face (the bug) fails here."""
    role = decl.get("role", "collar")
    prof = dog_ring_profile(
        shape, float(decl["dog_radius_mm"]), float(decl["z_lo_mm"]),
        float(decl["z_hi_mm"]), axis_pt=decl.get("axis_pt"),
        axis_dir=decl.get("axis_dir"), n_samples=int(decl.get("n_samples", 360)))
    out = []
    if prof["fill"] >= _SOLID_FACE_FILL:
        out.append({"role": role, **prof,
                    "reason": f"{role} dog band is {prof['fill']*100:.0f}% filled at "
                              f"r={decl['dog_radius_mm']} mm — a SOLID FACE where the dog "
                              f"gaps belong, not interleaving teeth (cannot engage)"})
    elif prof["fill"] < _RING_MIN_FILL:
        out.append({"role": role, **prof,
                    "reason": f"{role} dog band is only {prof['fill']*100:.0f}% filled at "
                              f"r={decl['dog_radius_mm']} mm — no dog teeth present"})
    elif prof["sectors"] < 2:
        out.append({"role": role, **prof,
                    "reason": f"{role} dog band forms a single {prof['fill']*100:.0f}% arc "
                              f"(1 sector) — not separated teeth"})
    n_exp = decl.get("n_dogs")
    if n_exp is not None and not out and prof["sectors"] != int(n_exp):
        out.append({"role": role, **prof,
                    "reason": f"{role} has {prof['sectors']} dog teeth, declared "
                              f"{n_exp}"})
    return out


def check_interleave(shape_a, shape_b, decl):
    """Validate that two ENGAGED dog rings interleave. ``decl`` = {role_a, role_b,
    dog_radius_mm, z_lo_mm, z_hi_mm, axis_pt?, axis_dir?}. A real engaged dog clutch
    has each ring's teeth in the OTHER's gaps (half-pitch offset), so few angles carry
    both. Two rings IN PHASE (teeth-on-teeth) jam — and each can still pass its own
    ``check_dog_ring`` (it has gaps), so this is the relative-phase layer that gap
    check cannot see. Both parts must be given in a common (engaged) frame."""
    ra, rb = decl.get("role_a", "a"), decl.get("role_b", "b")
    prof = interleave_profile(
        shape_a, shape_b, float(decl["dog_radius_mm"]), float(decl["z_lo_mm"]),
        float(decl["z_hi_mm"]), axis_pt=decl.get("axis_pt"),
        axis_dir=decl.get("axis_dir"), n_samples=int(decl.get("n_samples", 360)))
    out = []
    if prof["a_fill"] < _INTERLEAVE_MIN_FILL or prof["b_fill"] < _INTERLEAVE_MIN_FILL:
        out.append({**prof, "reason":
                    f"{ra}/{rb} are not both toothed at r={decl['dog_radius_mm']} mm "
                    f"(a_fill={prof['a_fill']}, b_fill={prof['b_fill']}) — the dog "
                    f"bands do not engage in this band"})
    elif prof["both_fraction"] >= _INTERLEAVE_MAX_BOTH:
        out.append({**prof, "reason":
                    f"{ra} and {rb} dog teeth are IN PHASE ({prof['both_fraction']*100:.0f}% "
                    f"of angles carry both) at r={decl['dog_radius_mm']} mm — teeth-on-"
                    f"teeth, they jam instead of interleaving (each ring has gaps, but "
                    f"they are not half-pitch offset)"})
    return out


def check_part(shape, decls):
    """Run every declaration in ``decls`` (each a {check: 'bore_keying'|'dog_ring',
    ...}) against one part shape; return the flat list of violations."""
    out = []
    for d in decls:
        kind = d.get("check")
        if kind == "bore_keying":
            out.extend(check_bore_keying(shape, d))
        elif kind == "dog_ring":
            out.extend(check_dog_ring(shape, d))
        else:
            out.append({"error": f"unknown realize check {kind!r}"})
    return out
