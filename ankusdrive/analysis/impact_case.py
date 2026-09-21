"""Impact dynamics — the transient contact solve behind the ``drop_impact`` screen.

This is **GitHub issue #311**, the last unbuilt row of ``docs/SIMULATION_NEXT.md``.
``analysis/impact.py`` answers "what deceleration does a 1 m drop mean?" by energy
balance; it cannot say *where the part is stressed*, what the stress wave does, or
what changes on a corner drop rather than a flat one. This module does: it flies the
real meshed part into a fixed floor through penalty contact and integrates the
motion.

The anchor (why this is not a horizon item any more). The issue filed this as
having no exact oracle. It has one — St-Venant's elastic bar striking a rigid wall:

* stress behind the wave front      ``σ = ρ·c₀·v₀``
* contact lasts                     ``T = 2L/c₀``      (``c₀ = √(E/ρ)``)
* the bar leaves stress-free at     ``+v₀``            (restitution 1)

and, past yield, the bilinear plastic-wave cap ``σ = σ_y + ρ·c_p·(v₀ − v_y)``. With
ν = 0 a 3-D bar IS that 1-D problem, and stock CalculiX lands on all of it to
< 1 % (``impact.bar_impact`` is the closed-form twin; ``tests/test_impact_case.py``
is the live gate).

Solver. **CalculiX** (``ccx``) ``*DYNAMIC`` — the deck is written here and ``ccx``
runs at the subprocess boundary like the warpage post-step, so there is no new
solver and no new licence boundary. Two integrators:

* ``method="implicit"`` (default) — HHT-α. The right tool for a drop: the event lasts
  milliseconds, the stable explicit step is nanoseconds. Its numerical damping costs
  ~1 % of the rebound speed. By default ccx steps it adaptively, which is what a real
  part needs — a flat face lands thousands of contact elements in one increment, and
  a FIXED step has no cutback to absorb that (ccx stops: "solution seems to
  diverge"). Adaptive is not free: in implicit contact dynamics ccx applies "impact
  rules" — every energy jump drops the increment to the minimum and climbs back at
  1.5× — several times the increments of a fixed step. So ``time_step_s`` opts in to
  the fixed step where contact comes on smoothly (the bar oracle runs 4–8× faster).
* ``method="explicit"`` — central difference, for stress-wave events (≲ 100 µs).
  Needs first-order elements (C3D4 / C3D8R). Always a fixed step, at
  :func:`stable_time_step`; ccx's own automatic choice sits ~20× under it on the bar
  oracle.

The floor is one fully-fixed brick normal to ``direction``; the part's
floor-facing boundary faces are the slave surface of a LINEAR penalty pair —
face-to-face for a flat or edge landing, node-to-face for a corner, because neither
ccx formulation handles both (:func:`pick_contact`).
Rotating the *floor and velocity* rather than the part is what makes an edge or
corner drop one parameter (``direction=(-1, -1, -1)``) instead of a re-mesh.

What comes back is reduced from the floor's reaction history, not from nodal
velocities: by Newton's second law the centre-of-mass velocity is
``v₀ − (1/m)∫F dt``, exactly, whatever the mesh. Peak G, contact duration, impulse
and restitution all follow from that one signal.

Fidelity & honesty. ``fidelity="solve"``, banded: peak contact force depends on the
penalty stiffness and the mesh, and a stress peak *at* a contact point is mesh-
dependent (a corner drop is near-singular). Face-to-face contact acts at the slave
faces' integration points, so a tilted edge sinks a fraction of an element before
anything pushes back — reported as ``engagement_lag_mm``; refine the mesh to shrink
it. A squat body landing flat is 3-D, not a bar: its face stress follows the
dilatational speed (1.27·c₀ at ν = 0.35), above ``bar_impact``'s ρ·c₀·v₀. Penalty-spring energy is not part of
``ELSE``, so total energy dips mid-contact — the gate reads the **end-state**
energy. Small-strain plasticity (``yield_mpa`` + ``tangent_mpa``) is bilinear and
rate-independent; no failure/erosion, no friction.

Units. **mm / tonne / s / MPa** (so N, mJ): FreeCAD's mm mesh export with density in
t/mm³ (kg/m³ × 1e-12). Pure-Python and FreeCAD-free: deck, mesh topology, parsers
and gate all run on the no-solver CI lane; only the *solve* needs ``ccx``.
"""

from __future__ import annotations

import math
import os
import re

G0_MM_S2 = 9806.65

_METHODS = ("implicit", "explicit")
_CONTACTS = {"face": "SURFACE TO SURFACE", "node": "NODE TO SURFACE"}

# Corner-node faces, 0-based, in CalculiX S1..Sn order, outward-oriented.
_TET_FACES = ((0, 1, 2), (0, 3, 1), (1, 3, 2), (2, 3, 0))
_HEX_FACES = ((0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
              (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0))
# A brick as six tets around the 1–7 diagonal (volume only).
_HEX_TETS = ((0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6),
             (0, 7, 4, 6), (0, 4, 5, 6), (0, 5, 1, 6))
_FIRST_ORDER = ("C3D4", "C3D8R", "C3D8")


def _corners(etype: str) -> int:
    et = etype.upper()
    if et.startswith("C3D4") or et.startswith("C3D10"):
        return 4
    if et.startswith("C3D8") or et.startswith("C3D20"):
        return 8
    raise ValueError(f"unsupported element type {etype!r} (tets or bricks only)")


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _unit(v):
    n = math.sqrt(_dot(v, v))
    if n <= 0:
        raise ValueError("direction must be a non-zero vector")
    return (v[0] / n, v[1] / n, v[2] / n)


# --- mesh ---------------------------------------------------------------------

def parse_mesh_inp(path: str) -> dict:
    """Read the ``*Node`` and the (single) volume ``*Element`` block of an ABAQUS /
    CalculiX mesh file — what FreeCAD's ``FemMesh.writeABAQUS`` emits.

    Returns ``{nodes: {id: (x, y, z)}, elements: {id: [node ids]}, etype, elset,
    nset}``. Element rows may wrap across lines (C3D10 / C3D20 do). Raises
    ValueError when the file has no nodes or no volume elements."""
    nodes: dict = {}
    elements: dict = {}
    etype = elset = nset = None
    mode = None
    pending: list = []
    n_per = 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            ln = raw.strip()
            if not ln or ln.startswith("**"):
                continue
            if ln.startswith("*"):
                head = ln.upper()
                opts = {k.strip(): v.strip() for k, _, v in
                        (o.partition("=") for o in ln.split(",")[1:])}
                opts = {k.upper(): v for k, v in opts.items()}
                pending = []
                if head.startswith("*NODE") and not head.startswith("*NODE "):
                    mode = "node"
                    nset = opts.get("NSET", nset)
                elif head.startswith("*ELEMENT"):
                    et = opts.get("TYPE", "")
                    if et.upper().startswith("C3D") and etype in (None, et):
                        mode, etype = "elem", et
                        elset = opts.get("ELSET", elset)
                        n_per = {4: 4, 8: 8}[_corners(et)]
                        if et.upper().startswith("C3D10"):
                            n_per = 10
                        elif et.upper().startswith("C3D20"):
                            n_per = 20
                    else:
                        mode = None
                else:
                    mode = None
                continue
            vals = [v for v in (t.strip() for t in ln.split(",")) if v]
            if mode == "node":
                nodes[int(vals[0])] = (float(vals[1]), float(vals[2]), float(vals[3]))
            elif mode == "elem":
                pending += [int(v) for v in vals]
                if len(pending) >= n_per + 1:
                    elements[pending[0]] = pending[1:n_per + 1]
                    pending = []
    if not nodes or not elements:
        raise ValueError(f"{path}: no nodes / volume elements found")
    return {"nodes": nodes, "elements": elements, "etype": etype,
            "elset": elset or "Evolumes", "nset": nset or "Nall"}


def mesh_volume(nodes: dict, elements: dict, etype: str) -> float:
    """Mesh volume (mm³) from corner nodes — the mass the impulse is divided by."""
    tets = ((0, 1, 2, 3),) if _corners(etype) == 4 else _HEX_TETS
    vol = 0.0
    for conn in elements.values():
        for a, b, c, d in tets:
            p0 = nodes[conn[a]]
            vol += abs(_dot(_sub(nodes[conn[b]], p0),
                            _cross(_sub(nodes[conn[c]], p0),
                                   _sub(nodes[conn[d]], p0)))) / 6.0
    return vol


def boundary_faces(nodes: dict, elements: dict, etype: str) -> list:
    """The mesh's free faces: ``[(element id, face number 1.., outward unit normal)]``.
    A face is on the boundary when exactly one element owns its corner-node set."""
    table = _TET_FACES if _corners(etype) == 4 else _HEX_FACES
    owners: dict = {}
    for eid, conn in elements.items():
        for fi, face in enumerate(table):
            key = tuple(sorted(conn[i] for i in face))
            owners.setdefault(key, []).append((eid, fi))
    out = []
    nc = _corners(etype)
    for owned in owners.values():
        if len(owned) != 1:
            continue
        eid, fi = owned[0]
        conn = elements[eid]
        face = table[fi]
        p = [nodes[conn[i]] for i in face]
        n = _cross(_sub(p[1], p[0]), _sub(p[2], p[0]))
        # orient away from the element's own centroid — robust to a mesher's winding
        cen = [sum(nodes[c][k] for c in conn[:nc]) / nc for k in range(3)]
        fcen = [sum(q[k] for q in p) / len(p) for k in range(3)]
        if _dot(n, _sub(fcen, cen)) < 0:
            n = (-n[0], -n[1], -n[2])
        out.append((eid, fi + 1, _unit(n)))
    return out


def drop_direction(spec) -> tuple:
    """The unit vector the part TRAVELS along, from ``'-z'`` / ``'+x'`` … or any
    3-vector (``(-1, -1, -1)`` is a corner drop onto a floor normal to the body
    diagonal). Raises ValueError on anything else."""
    if isinstance(spec, str):
        s = spec.strip().lower()
        sign = -1.0 if s.startswith("-") else 1.0
        axis = s.lstrip("+-")
        if axis not in ("x", "y", "z"):
            raise ValueError(f"direction {spec!r}: use '-z', '+x', … or a 3-vector")
        v = [0.0, 0.0, 0.0]
        v["xyz".index(axis)] = sign
        return tuple(v)
    try:
        v = tuple(float(c) for c in spec)
    except (TypeError, ValueError) as e:
        raise ValueError(f"direction {spec!r}: use '-z', '+x', … or a 3-vector") from e
    if len(v) != 3:
        raise ValueError("direction must have three components")
    return _unit(v)


def pick_contact(strike_alignment: float) -> str:
    """Which ccx penalty formulation a strike needs, from ``strike_alignment`` — the
    best ``n·d`` among the free faces meeting at the strike node (1 = landing flat).

    Measured, not assumed — neither formulation covers both ends:

    * ``"face"`` (SURFACE TO SURFACE) engages a flat landing and edges tilted up to
      45° (n·d ≥ 0.707), but NEVER a cube corner (n·d = 0.577): ccx generates no
      contact element at all and the part falls through the floor.
    * ``"node"`` (NODE TO SURFACE) catches the corner node at first touch and matches
      the bar oracle, but on a broad flat landing it GAINS energy (restitution 1.2–1.5)
      and under adaptive stepping ccx's "impact rules" stall short of the floor.

    So: face contact (adaptive stepping) down to 45°, node contact (fixed step, see
    :func:`write_impact_case`) for anything sharper."""
    return "face" if strike_alignment >= 0.70 else "node"


def floor_block(nodes: dict, d: tuple, *, gap_mm: float, margin: float = 1.0) -> dict:
    """The rigid floor: one brick normal to ``d``, its near face ``gap_mm`` beyond the
    part's furthest point along ``d``, overhanging the part's footprint by ``margin``
    × its size. Node order makes face **S1** the one facing the part.

    Returns ``{coords: [8 × (x, y, z)], face: 'S1', extent_mm, lowest_node}`` where
    ``extent_mm`` is the part's length along ``d`` and ``lowest_node`` the node that
    strikes first."""
    ref = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
    e1 = _unit(_cross(ref, d))
    e2 = _cross(d, e1)                      # e1 × e2 = d  → right-handed brick
    s = {n: _dot(p, d) for n, p in nodes.items()}
    lowest = max(s, key=s.get)
    s_max, s_min = s[lowest], min(s.values())
    a = [_dot(p, e1) for p in nodes.values()]
    b = [_dot(p, e2) for p in nodes.values()]
    size = max(max(a) - min(a), max(b) - min(b), s_max - s_min, 1e-9)
    a0, a1 = min(a) - margin * size, max(a) + margin * size
    b0, b1 = min(b) - margin * size, max(b) + margin * size
    coords = []
    for sv in (s_max + gap_mm, s_max + gap_mm + 0.25 * size):
        for av, bv in ((a0, b0), (a1, b0), (a1, b1), (a0, b1)):
            coords.append(tuple(av * e1[k] + bv * e2[k] + sv * d[k] for k in range(3)))
    return {"coords": coords, "face": "S1", "extent_mm": s_max - s_min,
            "lowest_node": lowest}


def min_element_size(nodes: dict, elements: dict, etype: str) -> float:
    """The mesh's smallest wave-crossing length (mm): a tet's shortest altitude
    (3V / largest face), a brick's shortest edge."""
    best = float("inf")
    if _corners(etype) == 4:
        for conn in elements.values():
            p = [nodes[c] for c in conn[:4]]
            vol = abs(_dot(_sub(p[1], p[0]), _cross(_sub(p[2], p[0]), _sub(p[3], p[0])))) / 6.0
            area = 0.0
            for i, j, k in _TET_FACES:
                n = _cross(_sub(p[j], p[i]), _sub(p[k], p[i]))
                area = max(area, 0.5 * math.sqrt(_dot(n, n)))
            if area > 0:
                best = min(best, 3.0 * vol / area)
    else:
        edges = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
                 (0, 4), (1, 5), (2, 6), (3, 7))
        for conn in elements.values():
            for i, j in edges:
                e = _sub(nodes[conn[i]], nodes[conn[j]])
                best = min(best, math.sqrt(_dot(e, e)))
    return best


def stable_time_step(*, h_min_mm: float, youngs_mpa: float, poisson: float,
                     density_t_mm3: float, contact_stiffness: float,
                     safety: float = 0.5) -> dict:
    """The explicit step this module runs at: ``safety`` × the smaller of the CFL
    limit ``h/c_d`` (dilatational speed) and the penalty spring's own limit
    ``2/ω = 2·√(ρ·h / 2K)`` — a surface node's half-cell of mass on the contact
    stiffness per unit area. ccx's automatic choice sits ~20× under the latter on the
    bar oracle, where this one is stable and exact; the energy gate is what catches a
    step that was too bold. Returns ``{time_step_s, cfl_s, contact_s, governs}``."""
    lam = (1.0 - poisson) / ((1.0 + poisson) * (1.0 - 2.0 * poisson))
    cfl = h_min_mm / math.sqrt(youngs_mpa * lam / density_t_mm3)
    contact = 2.0 * math.sqrt(density_t_mm3 * h_min_mm / (2.0 * contact_stiffness))
    return {"time_step_s": safety * min(cfl, contact), "cfl_s": cfl,
            "contact_s": contact, "governs": "cfl" if cfl <= contact else "contact"}


def default_samples(n_nodes: int, *, budget: int = 2_000_000) -> int:
    """Output samples for a run: ccx has one cadence, so every force sample also costs
    a full stress frame (one ``.frd`` record per node). Hold the file near ``budget``
    records — 400 samples for a small mesh, never fewer than 40."""
    return max(40, min(400, budget // max(1, n_nodes)))


# --- the CalculiX deck ----------------------------------------------------------

def impact_inp_text(
    *, mesh_include: str, elset: str, nset: str, floor: dict, floor_node0: int,
    floor_elem: int, floor_etype: str, slave_faces: list, d: tuple,
    youngs_mpa: float, poisson: float, density_t_mm3: float,
    velocity_mm_s: float, duration_s: float, method: str = "implicit",
    contact_stiffness: float, yield_mpa: float | None = None,
    tangent_mpa: float | None = None, time_step_s: float | None = None,
    max_time_step_s: float | None = None, gravity: bool = True,
    samples: int = 200, contact: str = "face",
) -> str:
    """The ``*DYNAMIC`` contact deck. Flat and fully resolved: mesh ``*INCLUDE``, the
    floor brick (all 24 dofs fixed), the face-to-face LINEAR penalty pair, the initial
    velocity ``velocity_mm_s·d`` on every part node, and ``samples`` outputs of the
    floor reaction + energies (``.dat``) and the stress field (``.frd``)."""
    if method not in _METHODS:
        raise ValueError(f"method must be one of {_METHODS}")
    if contact not in _CONTACTS:
        raise ValueError(f"contact must be one of {_CONTACTS}")
    if youngs_mpa <= 0 or density_t_mm3 <= 0 or not (0.0 <= poisson < 0.5):
        raise ValueError("youngs_mpa, density must be > 0 and 0 <= poisson < 0.5")
    if velocity_mm_s <= 0 or duration_s <= 0 or contact_stiffness <= 0:
        raise ValueError("velocity, duration and contact stiffness must be > 0")
    if yield_mpa is not None and yield_mpa <= 0:
        raise ValueError("yield_mpa must be > 0")
    fn = [floor_node0 + i for i in range(8)]
    L = ["*NODE, NSET=Nfloor"]
    L += [f"{n}, {x:.9g}, {y:.9g}, {z:.9g}" for n, (x, y, z) in zip(fn, floor["coords"])]
    L += [f"*INCLUDE, INPUT={mesh_include}",
          f"*ELEMENT, TYPE={floor_etype}, ELSET=Efloor",
          f"{floor_elem}, " + ", ".join(str(n) for n in fn),
          "*MATERIAL, NAME=part", "*ELASTIC", f"{youngs_mpa:.9g}, {poisson:.9g}"]
    if yield_mpa is not None:
        # bilinear: tangent modulus E_t  ->  plastic modulus H = E·E_t/(E − E_t)
        et = tangent_mpa if tangent_mpa is not None else 0.0
        if not (0.0 <= et < youngs_mpa):
            raise ValueError("tangent_mpa must satisfy 0 <= tangent_mpa < youngs_mpa")
        L += ["*PLASTIC", f"{yield_mpa:.9g}, 0."]
        if et > 0:
            h = youngs_mpa * et / (youngs_mpa - et)
            L.append(f"{yield_mpa + h:.9g}, 1.")
    L += ["*DENSITY", f"{density_t_mm3:.9g}",
          f"*SOLID SECTION, ELSET={elset}, MATERIAL=part",
          "*SOLID SECTION, ELSET=Efloor, MATERIAL=part",
          "*SURFACE, NAME=Sslave, TYPE=ELEMENT"]
    L += [f"{eid}, S{face}" for eid, face in slave_faces]
    L += ["*SURFACE, NAME=Smaster, TYPE=ELEMENT", f"{floor_elem}, {floor['face']}",
          f"*CONTACT PAIR, INTERACTION=floor, TYPE={_CONTACTS[contact]}",
          "Sslave, Smaster",
          "*SURFACE INTERACTION, NAME=floor",
          "*SURFACE BEHAVIOR, PRESSURE-OVERCLOSURE=LINEAR", f"{contact_stiffness:.9g}",
          "*BOUNDARY", "Nfloor, 1, 3, 0.",
          "*INITIAL CONDITIONS, TYPE=VELOCITY"]
    L += [f"{nset}, {k + 1}, {velocity_mm_s * d[k]:.9g}" for k in range(3) if d[k]]
    # Output cadence. ccx keeps ONE per step — the last FREQUENCY / TIME POINTS card
    # read wins for the .dat and the .frd alike — so history and stress frames share
    # `samples`. A fixed-step (DIRECT) run refuses *TIME POINTS, and there the
    # increment count is known, so FREQUENCY does the same job; an adaptive run lands
    # on the time points.
    adaptive = not time_step_s
    if adaptive and method == "explicit":
        raise ValueError("explicit dynamics needs time_step_s (see stable_time_step)")
    if adaptive:
        cadence = "TIME POINTS=Tout"
        L += ["*TIME POINTS, NAME=Tout, GENERATE",
              f"0., {duration_s:.9e}, {duration_s / samples:.9e}"]
    else:
        n_inc = max(1, int(math.ceil(duration_s / time_step_s)))
        cadence = f"FREQUENCY={max(1, n_inc // samples)}"
    L.append("*STEP, INC=100000000")
    if method == "explicit":
        L += ["*DYNAMIC, EXPLICIT, DIRECT", f"{time_step_s:.9e}, {duration_s:.9e}"]
    elif adaptive:
        dt_max = max_time_step_s or 2.0 * duration_s / samples   # time points govern
        dt = dt_max / 5.0
        # The MINIMUM increment is a working parameter here, not a floor nobody reaches.
        # ccx's "impact rules" pin the step AT the minimum while a contact is about to
        # close, to resolve the instant of impact. At 1e-7 of the step a node-to-face
        # strike never gets there — the part creeps 1e-9 mm per increment (measured:
        # 17 000 increments, no progress). At 1/50 it crosses in a handful; the price is
        # a cutback floor only 50x under the step, and ccx says so if it needs more.
        L += ["*DYNAMIC", f"{dt:.9e}, {duration_s:.9e}, {dt / 50.0:.9e}, {dt_max:.9e}"]
    else:
        L += ["*DYNAMIC, DIRECT", f"{time_step_s:.9e}, {duration_s:.9e}"]
    if gravity:
        L += ["*DLOAD", f"{elset}, GRAV, {G0_MM_S2}, {d[0]:.9g}, {d[1]:.9g}, {d[2]:.9g}"]
    L += [f"*NODE PRINT, NSET=Nfloor, TOTALS=ONLY, {cadence}", "RF",
          f"*EL PRINT, ELSET={elset}, TOTALS=ONLY, {cadence}", "ELSE, ELKE",
          f"*EL FILE, {cadence}", "S",
          "*END STEP"]
    return "\n".join(L) + "\n"


def write_impact_case(
    case_dir: str, *, mesh: dict, youngs_mpa: float, poisson: float,
    density_kg_m3: float, velocity_m_s: float, direction="-z",
    duration_s: float | None = None, gap_mm: float | None = None,
    method: str = "implicit", contact_stiffness_mpa_mm: float | None = None,
    yield_mpa: float | None = None, tangent_mpa: float | None = None,
    time_step_s: float | None = None, max_time_step_s: float | None = None,
    gravity: bool = True, mesh_filename: str = "mesh.inp", job_name: str = "case",
    samples: int | None = None, contact: str = "auto",
) -> dict:
    """Write the impact deck ``<job_name>.inp`` into ``case_dir`` (the mesh file is
    already there; ``mesh`` is its :func:`parse_mesh_inp`).

    Defaults that carry judgement: ``gap_mm`` = 0.1 % of the part's length along the
    drop (it only costs free-flight time); ``duration_s`` = free flight + 10 wave
    round trips of the part's length along the drop at the bar speed ``c₀`` (a stiff
    body rebounds in one) — a compliant one needs more, and the result says so
    (``arrested``) when it did;
    ``contact_stiffness_mpa_mm`` = 25·E implicit / 5·E explicit (ccx advises 5–50·E;
    the explicit stable step scales with 1/√K); ``time_step_s`` = None for implicit
    face contact (ccx steps adaptively; give one to run a fixed step instead),
    ``duration/1000`` for implicit node contact, :func:`stable_time_step` explicit; ``samples`` = :func:`default_samples`.

    Returns ``{case_dir, inp, job_name, argv, direction, velocity_mm_s, duration_s,
    gap_mm, method, mass_t, volume_mm3, extent_mm, lowest_node, n_slave_faces,
    contact_stiffness_mpa_mm, contact, strike_alignment, wave_speed_mm_s,
    floor_nodes, plastic, samples,
    time_step_s, time_step, adaptive}`` (``time_step`` is the explicit stability
    breakdown)."""
    if method not in _METHODS:
        raise ValueError(f"method must be one of {_METHODS}")
    nodes, elements, etype = mesh["nodes"], mesh["elements"], mesh["etype"]
    if method == "explicit" and etype.upper() not in _FIRST_ORDER:
        raise ValueError(
            f"explicit dynamics needs first-order elements (C3D4 / C3D8R), got {etype}")
    if velocity_m_s <= 0:
        raise ValueError("velocity_m_s must be > 0")
    d = drop_direction(direction)
    v = velocity_m_s * 1e3
    rho = density_kg_m3 * 1e-12
    c0 = math.sqrt(youngs_mpa / rho)
    probe = floor_block(nodes, d, gap_mm=0.0)
    if gap_mm is None:
        gap_mm = max(1e-3 * probe["extent_mm"], 1e-4)
    floor = floor_block(nodes, d, gap_mm=gap_mm)
    if duration_s is None:
        duration_s = gap_mm / v + 20.0 * floor["extent_mm"] / c0
    k_pen = contact_stiffness_mpa_mm or (25.0 if method == "implicit" else 5.0) * youngs_mpa
    free = boundary_faces(nodes, elements, etype)
    slave = [(eid, face) for eid, face, n in free if _dot(n, d) > 0.0]
    if not slave:
        raise ValueError("no boundary face of the mesh faces the floor")
    table = _TET_FACES if _corners(etype) == 4 else _HEX_FACES
    alignment = max((_dot(n, d) for eid, face, n in free
                     if floor["lowest_node"] in (elements[eid][i] for i in table[face - 1])),
                    default=1.0)
    if contact == "auto":
        contact = pick_contact(alignment)
    volume = mesh_volume(nodes, elements, etype)
    step = None
    if method == "explicit" and not time_step_s:
        step = stable_time_step(
            h_min_mm=min_element_size(nodes, elements, etype), youngs_mpa=youngs_mpa,
            poisson=poisson, density_t_mm3=rho, contact_stiffness=k_pen)
        time_step_s = step["time_step_s"]
    if method == "implicit" and contact == "node" and not time_step_s:
        # node-to-face contact runs at a FIXED step: a point strike comes on one node
        # at a time, which DIRECT handles, while ccx's adaptive "impact rules" either
        # pin the step at its minimum short of the floor or run out of cutbacks
        # (measured both ways). Stable across 500–2000 steps and a 2x finer mesh.
        time_step_s = duration_s / 1000.0
    samples = int(samples) if samples else default_samples(len(nodes))
    text = impact_inp_text(
        mesh_include=mesh_filename, elset=mesh["elset"], nset=mesh["nset"], floor=floor,
        floor_node0=max(nodes) + 1, floor_elem=max(elements) + 1,
        floor_etype="C3D8R" if method == "explicit" else "C3D8",
        slave_faces=slave, d=d, youngs_mpa=youngs_mpa, poisson=poisson,
        density_t_mm3=rho, velocity_mm_s=v, duration_s=duration_s, method=method,
        contact_stiffness=k_pen, yield_mpa=yield_mpa, tangent_mpa=tangent_mpa,
        time_step_s=time_step_s, max_time_step_s=max_time_step_s, gravity=gravity,
        samples=samples, contact=contact)
    os.makedirs(case_dir, exist_ok=True)
    inp = f"{job_name}.inp"
    with open(os.path.join(case_dir, inp), "w", encoding="utf-8") as f:
        f.write(text)
    return {
        "case_dir": case_dir, "inp": inp, "job_name": job_name,
        "argv": ["ccx", "-i", job_name],
        "direction": [round(c, 9) for c in d], "velocity_mm_s": v,
        "duration_s": duration_s, "gap_mm": gap_mm, "method": method,
        "mass_t": rho * volume, "volume_mm3": volume,
        "extent_mm": floor["extent_mm"], "lowest_node": floor["lowest_node"],
        "n_slave_faces": len(slave), "contact_stiffness_mpa_mm": k_pen,
        "contact": contact, "strike_alignment": round(alignment, 4),
        "wave_speed_mm_s": c0, "gravity": bool(gravity),
        "floor_nodes": [max(nodes) + 1 + i for i in range(8)],
        "plastic": yield_mpa is not None,
        "samples": samples, "time_step_s": time_step_s, "time_step": step,
        "adaptive": not time_step_s,
    }


# --- result parsers -------------------------------------------------------------

_DAT_HEAD = re.compile(r"^\s*(.+?) for set (\S+) and time\s+(\S+)")


def parse_impact_dat(dat_path: str, d: tuple) -> dict | None:
    """The floor-reaction and energy histories from the ``.dat``.

    ``force_n`` is the contact force the part exerts on the floor along ``d`` (≥ 0 in
    contact) — minus the support reaction ccx prints. Returns ``{t, force_n,
    strain_mj, kinetic_mj}`` (equal-length lists), or None when the file is absent or
    holds no force samples (the solve failed)."""
    if not os.path.isfile(dat_path):
        return None
    force: dict = {}
    ese: dict = {}
    eke: dict = {}
    what = t = None
    with open(dat_path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            m = _DAT_HEAD.match(ln)
            if m:
                what, t = m.group(1).strip().lower(), float(m.group(3))
                continue
            p = ln.split()
            if not p or what is None:
                continue
            try:
                v = [float(x) for x in p]
            except ValueError:
                continue
            if what.startswith("total force") and len(v) >= 3:
                force[t] = -_dot(v[:3], d)
            elif what.startswith("total internal energy"):
                ese[t] = v[0]
            elif what.startswith("total kinetic energy"):
                eke[t] = v[0]
            what = None
    ts = sorted(force)
    if len(ts) < 3:
        return None
    return {"t": ts, "force_n": [force[x] for x in ts],
            "strain_mj": [ese.get(x) for x in ts],
            "kinetic_mj": [eke.get(x) for x in ts]}


def parse_peak_stress_frd(frd_path: str, *, exclude_nodes=()) -> dict | None:
    """Stream the ``.frd`` frames for the worst nodal von Mises stress of the run.

    Fixed-width records (node id in cols 4–13, then 12-char floats): the ``2C`` block
    carries coordinates, each ``100CL`` header a frame time, each ``-4  STRESS`` block
    six components (xx yy zz xy yz zx). Returns ``{peak_von_mises_mpa, node, time_s,
    location_mm, frames}`` or None when no stress frame was written."""
    if not os.path.isfile(frd_path):
        return None
    skip = set(exclude_nodes)
    coords: dict = {}
    best = (-1.0, None, None)
    frames = 0
    block = None
    t = None
    with open(frd_path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            tag = ln[:6]
            if tag.strip() == "2C":
                block = "coords"
            elif ln.startswith("  100C"):
                try:
                    t = float(ln[12:24])
                except ValueError:
                    t = None
            elif ln.startswith(" -4"):
                block = "stress" if "STRESS" in ln else None
                frames += block == "stress"
            elif ln.startswith(" -3"):
                block = None
            elif ln.startswith(" -1") and block:
                try:
                    nid = int(ln[3:13])
                    vals = [float(ln[13 + 12 * i:25 + 12 * i])
                            for i in range(3 if block == "coords" else 6)]
                except ValueError:
                    continue
                if block == "coords":
                    coords[nid] = tuple(vals)
                elif nid not in skip:
                    sx, sy, sz, sxy, syz, szx = vals
                    vm = math.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2)
                                   + 3.0 * (sxy ** 2 + syz ** 2 + szx ** 2))
                    if vm > best[0]:
                        best = (vm, nid, t)
    if best[1] is None:
        return None
    loc = coords.get(best[1])
    return {"peak_von_mises_mpa": round(best[0], 4), "node": best[1],
            "time_s": best[2], "frames": frames,
            "location_mm": [round(c, 4) for c in loc] if loc else None}


# --- reduction + gate -----------------------------------------------------------

def reduce_impact(history: dict, *, mass_t: float, velocity_mm_s: float,
                  gravity: bool = True, contact_frac: float = 0.02,
                  gap_mm: float | None = None) -> dict:
    """Reduce the floor-reaction history to the drop's headline numbers.

    Everything follows from one signal by Newton's second law: impulse
    ``J = ∫F dt`` (trapezoid), centre-of-mass velocity along the drop
    ``v = v₀ − J/m (+ g·t)``, peak COM deceleration ``F_peak/(m·g)`` — off the
    3-point-median force, so a one-sample contact-onset spike does not set it (the raw
    ``peak_force_n`` is still returned). Contact is
    ``F > contact_frac·F_peak``; ``separated`` means it ended before the window did,
    ``arrested`` that the fall was at least stopped. ``mass_check`` is ccx's own
    initial kinetic energy over ``½·m·v₀²`` — a units / density tripwire. With
    ``gap_mm``, ``engagement_lag_mm`` is how far past first touch the part travelled
    before the contact pushed back (face-to-face contact on a sharp strike point).

    Returns ``{peak_force_n, peak_force_median3_n, peak_force_time_s, peak_g, impulse_n_s, contact_start_s,
    contact_duration_s, separated, arrested, rebound_velocity_m_s, restitution,
    energy_end_ratio, energy_min_ratio, mass_check, samples, contact_samples,
    engagement_lag_mm}``."""
    t, f = history["t"], history["force_n"]
    peak = max(f)
    i_peak = f.index(peak)
    # A penalty contact closing on a moving face rings for one sample (measured: a
    # first-sample spike ~1.5x the plateau on a flat landing). A 3-point median drops
    # a lone spike and leaves any pulse wider than two samples alone; G is read off it.
    med = [sorted(f[max(0, i - 1):i + 2])[1] if 0 < i < len(f) - 1 else f[i]
           for i in range(len(f))]
    peak_med = max(med)
    ke0 = 0.5 * mass_t * velocity_mm_s ** 2
    imp = [0.0]
    for i in range(1, len(t)):
        imp.append(imp[-1] + 0.5 * (f[i] + f[i - 1]) * (t[i] - t[i - 1]))
    g = G0_MM_S2 if gravity else 0.0
    v_end = velocity_mm_s - imp[-1] / mass_t + g * (t[-1] - t[0])
    thr = contact_frac * peak
    on = [i for i, x in enumerate(f) if x > thr] if peak > 0 else []
    start = t[on[0]] if on else None
    dur = (t[on[-1]] - t[on[0]]) if on else None
    separated = bool(on) and on[-1] < len(t) - 1
    etot = [a + b for a, b in zip(history["strain_mj"], history["kinetic_mj"])
            if a is not None and b is not None]
    # ccx's own ½mv² — only meaningful from a sample taken BEFORE contact began
    k0 = history["kinetic_mj"][0] if (not on or on[0] > 0) else None
    return {
        "peak_force_n": round(peak, 4),
        "peak_force_time_s": t[i_peak],
        "peak_force_median3_n": round(peak_med, 4),
        "peak_g": round(peak_med / (mass_t * G0_MM_S2), 3),
        "impulse_n_s": round(imp[-1], 9),
        "contact_start_s": start,
        "contact_duration_s": dur,
        "separated": separated,
        "arrested": v_end <= 0.0,
        "rebound_velocity_m_s": round(-v_end / 1e3, 6) if separated else None,
        "restitution": round(-v_end / velocity_mm_s, 4) if separated else None,
        "energy_end_ratio": round(etot[-1] / ke0, 4) if etot else None,
        "energy_min_ratio": round(min(etot) / ke0, 4) if etot else None,
        "mass_check": round(k0 / ke0, 4) if k0 is not None else None,
        "samples": len(t),
        "contact_samples": len(on),
        "engagement_lag_mm": (round(max(0.0, start * velocity_mm_s - gap_mm), 5)
                              if start is not None and gap_mm is not None else None),
    }


def impact_gate(
    metrics: dict, stress: dict | None = None, *, yield_mpa: float | None = None,
    deceleration_limit_g: float | None = None, plastic: bool = False,
    energy_tol: float = 0.05, band_pct: float = 20.0, extent_mm: float | None = None,
) -> dict:
    """The house verdict for an impact solve.

    Two layers. **Health** (always): the fall was ``arrested`` inside the window (a
    truncated run has not seen its peak — it FAILS, it does not pass quietly), total
    energy at the end has not GROWN past ``1 + energy_tol`` and restitution is not
    above it (either is an unstable integration), and ccx's initial kinetic energy
    matches ``½mv²``. **Criteria** (whichever are given): peak von Mises below
    ``yield_mpa`` — skipped for a ``plastic`` run, where exceeding yield is the model
    working — and peak COM deceleration within ``deceleration_limit_g``.

    ``score`` = 1 − the worst criterion utilisation, clamped to [0, 1] (1.0 with no
    criterion and a healthy run). Energy LOSS only warns: it is physical with
    plasticity and ~1–2 % numerical (HHT-α) without. Returns ``{pass, score, fidelity,
    band_pct, checks, utilisation, warnings}``."""
    warnings: list[str] = []
    checks: dict = {}
    checks["arrested"] = bool(metrics.get("arrested"))
    if not checks["arrested"]:
        warnings.append(
            "the part was still travelling into the floor when the solve ended — the "
            "peak has not been seen; raise duration_s")
    elif not metrics.get("separated"):
        warnings.append(
            "contact was still active at the end of the window — a later peak "
            "(secondary hit, rebound) may be missed; raise duration_s to see it leave")
    if checks["arrested"] and (metrics.get("contact_samples") or 0) < 10:
        warnings.append(
            f"the contact pulse spans only {metrics.get('contact_samples')} output "
            "samples — the peak is under-resolved; shorten duration_s or raise samples")
    lag = metrics.get("engagement_lag_mm")
    if lag and extent_mm and lag > 0.02 * extent_mm:
        warnings.append(
            f"the strike point sank {lag:.2f} mm before the contact pushed back — "
            "face-to-face contact resolves a tilted edge only to a fraction of an "
            "element; refine the mesh (char_length_mm), or set contact='node'")
    end = metrics.get("energy_end_ratio")
    checks["energy_bounded"] = end is None or end <= 1.0 + energy_tol
    if not checks["energy_bounded"]:
        warnings.append(
            f"total energy ended at {end:.2f}x the drop energy — the integration is "
            "unstable; lower contact_stiffness_mpa_mm or the time step")
    elif end is not None and not plastic and end < 0.8:
        warnings.append(
            f"an elastic run lost {100 * (1 - end):.0f}% of its energy — numerical "
            "damping is doing the work; lower time_step_s")
    e = metrics.get("restitution")
    checks["restitution_physical"] = e is None or e <= 1.0 + energy_tol
    mc = metrics.get("mass_check")
    checks["mass_consistent"] = mc is None or abs(mc - 1.0) <= 0.02
    if not checks["mass_consistent"]:
        warnings.append(
            f"ccx's initial kinetic energy is {mc:.3f}x ½mv² — check density / units")

    util: dict = {}
    if yield_mpa and stress and not plastic:
        util["stress"] = stress["peak_von_mises_mpa"] / yield_mpa
        checks["below_yield"] = util["stress"] < 1.0
        if not checks["below_yield"]:
            warnings.append(
                f"peak von Mises {stress['peak_von_mises_mpa']:.0f} MPa exceeds yield "
                f"{yield_mpa:.0f} MPa at {stress.get('location_mm')} — the elastic "
                "result is an upper bound there; re-run with yield_mpa + tangent_mpa "
                "plasticity, or add compliance / a radius at the strike point")
    if deceleration_limit_g:
        util["deceleration"] = metrics["peak_g"] / deceleration_limit_g
        checks["within_g_limit"] = util["deceleration"] <= 1.0
        if not checks["within_g_limit"]:
            warnings.append(
                f"peak deceleration {metrics['peak_g']:.0f} g exceeds the "
                f"{deceleration_limit_g:.0f} g limit — add crush stroke (drop_impact "
                "sizes it) or soften the strike")
    healthy = all(checks[k] for k in ("arrested", "energy_bounded",
                                      "restitution_physical", "mass_consistent"))
    passed = all(checks.values())
    worst = max(util.values()) if util else 0.0
    return {
        "pass": bool(passed),
        # an unhealthy solve scores 0 whatever its numbers say — they are not trusted
        "score": round(max(0.0, min(1.0, 1.0 - worst)), 4) if healthy else 0.0,
        "fidelity": "solve",
        "band_pct": band_pct,
        "checks": checks,
        "utilisation": {k: round(v, 4) for k, v in util.items()},
        "warnings": warnings,
    }
