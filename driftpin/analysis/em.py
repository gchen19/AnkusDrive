"""Low-frequency electromagnetics — DC conduction + AC skin effect (P3 M6 frontier).

Pure-Python, FreeCAD-free (the analytic core + case text run on the no-FreeCAD
lane; the *solve* needs the ElmerSolver binary). The frontier family the P3 kickoff
names "low-frequency electromagnetics / induction heating", built on Elmer's EM
solvers with **exact** closed-form anchors:

    DC conduction   R = L/(σ·A)  (and Joule P = V²/R)            — exact
    skin effect     δ = √(2/(ω·μ·σ));  A(x) = A₀·e^(−x/δ)·e^(−i·x/δ) — exact
    long wire       B(r) = μ₀·I/(2π·r)                            — exact (Ampère)
    solenoid        B = μ₀·μ_r·n·I                                — exact (interior)

Two Elmer gates, both validated live to <0.2 %:

* **DC strip** (``StatCurrentSolver``): a rectangular conductor with a voltage
  across its ends. The electrode's ``diffusive flux`` of Potential (coefficient =
  Electric Conductivity) is the total current — machine-exact against V/R — and
  the solver's own ``res: effective resistance`` reproduces L/(σ·A) to machine
  precision.
* **Skin-effect slab** (``MagnetoDynamics2DHarmonic``): a conductor half-space
  driven by the vector potential at its surface; the solved complex A(x) must
  decay with e-folding length δ in BOTH magnitude and phase (measured 0.999 /
  1.000 of δ for copper at 50 Hz). This is the induction-heating anchor — the
  Joule deposition profile is |J|² ∝ e^(−2x/δ).

Units SI; conductivities S/m. The conductor σ / µ_r values now live in the
Materials DB (``driftpin.analysis.materials`` — the electrical layer added in
issue #175, carrying ``electrical_conductivity`` + ``relative_permeability`` with
provenance), so ``em_*`` tools and ``material_get`` share ONE source of truth.
``_FALLBACK_CONDUCTORS`` below is a mirror used only when the DB can't be loaded.
See ``tests/test_em.py``.
"""
from __future__ import annotations

import math
import os

MU_0 = 4.0e-7 * math.pi  # vacuum permeability, H/m

# Offline fallback ONLY (used when the Materials DB is unavailable). The DB is the
# source of truth — test_em.py::test_conductors_come_from_materials_db asserts each
# of these equals the DB value so the mirror can never silently drift. Handbook DC
# conductivities (S/m) near 20 °C — CRC / IACS values.
_FALLBACK_CONDUCTORS = {
    "silver": 6.14e7,
    "copper": 5.80e7,          # annealed, 100% IACS
    "gold": 4.10e7,
    "aluminum": 3.77e7,
    "brass": 1.60e7,
    "steel-mild": 6.99e6,
    "stainless-304": 1.39e6,
}

# The conductor names em ships. material_get resolves each to a DB card via the
# aliases seeded in issue #175 (e.g. 'copper' -> 'Copper Generic').
CONDUCTOR_NAMES = tuple(_FALLBACK_CONDUCTORS)


def _db_conductivity(conductor: str) -> float | None:
    """σ (S/m) for a named conductor from the Materials DB electrical layer, or
    None if the DB / card / electrical field is unavailable. Never raises."""
    try:
        from . import materials
        card = materials.get(conductor)
        return materials.numeric(card, "electrical_conductivity_s_m")
    except Exception:
        return None


def conductor_conductivity(conductor: str) -> float:
    """Public: resolve a conductor name to σ (S/m) — DB first, handbook fallback
    otherwise. Raises ValueError for an unknown conductor."""
    sigma = _db_conductivity(conductor)
    if sigma is None:
        sigma = _FALLBACK_CONDUCTORS.get(conductor.strip().lower())
    if sigma is None:
        raise ValueError(
            f"unknown conductor {conductor!r}; known: {sorted(_FALLBACK_CONDUCTORS)} "
            "(or pass conductivity_s_m)")
    return sigma


def _conductivity(conductivity_s_m, conductor: str | None) -> float:
    """Resolve σ (S/m) from an explicit value or a named conductor (Materials DB
    electrical layer, handbook fallback)."""
    if conductivity_s_m is not None:
        sigma = float(conductivity_s_m)
    elif conductor:
        sigma = conductor_conductivity(conductor)
    else:
        raise ValueError("provide conductivity_s_m or a conductor name")
    if sigma <= 0:
        raise ValueError("conductivity must be > 0")
    return sigma


def skin_depth(frequency_hz: float, conductivity_s_m: float | None = None,
               mu_r: float = 1.0, conductor: str | None = None) -> dict:
    """Exact AC skin depth δ = √(2/(ω·μ₀·μ_r·σ)) and the derived surface measures.

    ``surface_resistance_ohm`` is R_s = 1/(σ·δ) (the per-square AC sheet
    resistance); fields and current density decay e^(−x/δ) into the conductor, so
    ~95 % of the induced Joule heat deposits within 1.5·δ (the induction-heating
    rule of thumb). Returns {skin_depth_m, skin_depth_mm, surface_resistance_ohm,
    angular_frequency_rad_s, conductivity_s_m, mu_r}. Raises ValueError on
    non-positive frequency/σ/μ_r."""
    if frequency_hz <= 0 or mu_r <= 0:
        raise ValueError("frequency_hz and mu_r must be > 0")
    sigma = _conductivity(conductivity_s_m, conductor)
    omega = 2.0 * math.pi * frequency_hz
    delta = math.sqrt(2.0 / (omega * MU_0 * mu_r * sigma))
    return {
        "skin_depth_m": delta,
        "skin_depth_mm": round(delta * 1000.0, 6),
        "surface_resistance_ohm": 1.0 / (sigma * delta),
        "angular_frequency_rad_s": omega,
        "conductivity_s_m": sigma,
        "mu_r": mu_r,
    }


def dc_resistance(length_mm: float, area_mm2: float,
                  conductivity_s_m: float | None = None,
                  conductor: str | None = None,
                  voltage_v: float | None = None) -> dict:
    """Exact DC resistance of a uniform conductor, R = L/(σ·A) — the closed-form
    anchor the Elmer ``StatCurrentSolver`` gate reproduces to machine precision.

    With ``voltage_v`` the Ohm/Joule pair is included: I = V/R, P = V·I. Returns
    {resistance_ohm, conductivity_s_m, length_m, area_m2, current_a?, joule_w?}.
    Raises ValueError on non-positive geometry/σ."""
    if length_mm <= 0 or area_mm2 <= 0:
        raise ValueError("length_mm and area_mm2 must be > 0")
    sigma = _conductivity(conductivity_s_m, conductor)
    L = length_mm / 1000.0
    A = area_mm2 * 1e-6
    R = L / (sigma * A)
    out = {
        "resistance_ohm": R,
        "conductivity_s_m": sigma,
        "length_m": L,
        "area_m2": A,
    }
    if voltage_v is not None:
        out["current_a"] = voltage_v / R
        out["joule_w"] = voltage_v * voltage_v / R
    return out


def wire_field(current_a: float, distance_mm: float) -> dict:
    """Exact magnetic flux density of a long straight wire (Ampère's law):
    B(r) = μ₀·I/(2π·r). Returns {b_t, b_mt, current_a, distance_m}. Raises
    ValueError on a non-positive distance."""
    if distance_mm <= 0:
        raise ValueError("distance_mm must be > 0")
    r = distance_mm / 1000.0
    b = MU_0 * current_a / (2.0 * math.pi * r)
    return {"b_t": b, "b_mt": round(b * 1000.0, 9),
            "current_a": current_a, "distance_m": r}


def solenoid_field(turns_per_m: float, current_a: float, mu_r: float = 1.0) -> dict:
    """Exact interior field of a long solenoid: B = μ₀·μ_r·n·I (uniform; the
    classic magnetostatic anchor). Returns {b_t, b_mt, turns_per_m, current_a,
    mu_r}. Raises ValueError on non-positive n/μ_r."""
    if turns_per_m <= 0 or mu_r <= 0:
        raise ValueError("turns_per_m and mu_r must be > 0")
    b = MU_0 * mu_r * turns_per_m * current_a
    return {"b_t": b, "b_mt": round(b * 1000.0, 9),
            "turns_per_m": turns_per_m, "current_a": current_a, "mu_r": mu_r}


# --- shared rectangle mesh (DC strip + skin slab) --------------------------------

def rect_mesh_files(nx: int, ny: int, length_m: float, width_m: float) -> dict:
    """Native Elmer 2-D rectangle mesh, nx×ny quads over [0,L]×[0,W]. Boundary
    tags: **1** the x=0 edge, **2** the x=L edge — each with its true parent bulk
    element (required by SaveScalars' ``diffusive flux``). The long edges are
    natural (insulated / field-symmetric). Returns the four Elmer mesh files as
    ``{filename: text}``. Raises ValueError on degenerate sizes."""
    if nx < 4 or ny < 1:
        raise ValueError("need nx >= 4 and ny >= 1")
    if length_m <= 0 or width_m <= 0:
        raise ValueError("length_m and width_m must be > 0")

    nodes: list = []
    nid: dict = {}

    def add(i, j):
        if (i, j) not in nid:
            nid[(i, j)] = len(nodes) + 1
            nodes.append((nid[(i, j)], length_m * i / nx, width_m * j / ny))
        return nid[(i, j)]

    elems = []
    for j in range(ny):
        for i in range(nx):
            elems.append((len(elems) + 1, 1, add(i, j), add(i + 1, j),
                          add(i + 1, j + 1), add(i, j + 1)))
    bnd = []
    for j in range(ny):
        bnd.append((len(bnd) + 1, 1, j * nx + 1, add(0, j), add(0, j + 1)))
        bnd.append((len(bnd) + 1, 2, j * nx + nx, add(nx, j), add(nx, j + 1)))

    header = f"{len(nodes)} {len(elems)} {len(bnd)}\n2\n202 {len(bnd)}\n404 {len(elems)}\n"
    return {
        "mesh.header": header,
        "mesh.nodes": "".join(f"{n} -1 {x:.10g} {y:.10g} 0.0\n" for n, x, y in nodes),
        "mesh.elements": "".join(
            f"{e[0]} {e[1]} 404 {e[2]} {e[3]} {e[4]} {e[5]}\n" for e in elems),
        "mesh.boundary": "".join(
            f"{b[0]} {b[1]} {b[2]} 0 202 {b[3]} {b[4]}\n" for b in bnd),
    }


# --- DC conduction case ----------------------------------------------------------

def dc_strip_sif(*, conductivity_s_m: float, voltage_v: float,
                 mesh_name: str = "strip", scalars: str = "dc.dat") -> str:
    """The ``.sif`` deck for the DC strip: ``StatCurrentSolver`` with ``voltage_v``
    on tag 1 and ground on tag 2; SaveScalars writes the electrode's diffusive flux
    of Potential × Electric Conductivity (= the total current) plus the solver's
    own Joule heating / effective resistance scalars. Parse with
    :func:`parse_dc_scalars`."""
    if conductivity_s_m <= 0:
        raise ValueError("conductivity_s_m must be > 0")
    return f"""Header
  Mesh DB "." "{mesh_name}"
End
Simulation
  Coordinate System = Cartesian 2D
  Simulation Type = Steady State
  Steady State Max Iterations = 1
  Output Intervals = 0
End
Body 1
  Equation = 1
  Material = 1
End
Material 1
  Electric Conductivity = {conductivity_s_m:.10g}
End
Equation 1
  Active Solvers(2) = 1 2
End
Solver 1
  Equation = Static Current Conduction
  Procedure = "StatCurrentSolve" "StatCurrentSolver"
  Variable = Potential
  Calculate Joule Heating = True
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
  Steady State Convergence Tolerance = 1.0e-9
End
Solver 2
  Equation = SaveScalars
  Procedure = "SaveData" "SaveScalars"
  Filename = "{scalars}"
  Variable 1 = Potential
  Operator 1 = diffusive flux
  Coefficient 1 = Electric Conductivity
  Mask Name 1 = electrodemask
End
Boundary Condition 1
  Target Boundaries(1) = 1
  Potential = {voltage_v:.10g}
  electrodemask = Logical True
End
Boundary Condition 2
  Target Boundaries(1) = 2
  Potential = 0.0
End
"""


def write_dc_strip_case(
    case_dir: str,
    *,
    voltage_v: float = 0.001,
    length_m: float = 0.1,
    width_m: float = 0.02,
    conductivity_s_m: float | None = None,
    conductor: str | None = "copper",
    nx: int = 40,
    ny: int = 8,
    mesh_name: str = "strip",
    scalars: str = "dc.dat",
) -> dict:
    """Write a complete, runnable DC-strip case under ``case_dir`` and return it
    next to its exact oracle (unit depth, so A = width_m·1). Returns {case_dir,
    sif, mesh_name, scalars, oracle: {resistance_ohm, current_a, joule_w, …}}."""
    sigma = _conductivity(conductivity_s_m, conductor)
    mesh_dir = os.path.join(case_dir, mesh_name)
    os.makedirs(mesh_dir, exist_ok=True)
    for fname, text in rect_mesh_files(nx, ny, length_m, width_m).items():
        with open(os.path.join(mesh_dir, fname), "w", encoding="utf-8") as f:
            f.write(text)
    with open(os.path.join(case_dir, "case.sif"), "w", encoding="utf-8") as f:
        f.write(dc_strip_sif(conductivity_s_m=sigma, voltage_v=voltage_v,
                             mesh_name=mesh_name, scalars=scalars))
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w", encoding="utf-8") as f:
        f.write("case.sif\n")
    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_name": mesh_name,
        "scalars": scalars,
        "oracle": dc_resistance(length_m * 1000.0, width_m * 1e6,
                                conductivity_s_m=sigma, voltage_v=voltage_v),
    }


def parse_dc_scalars(case_dir: str, scalars: str = "dc.dat") -> dict | None:
    """Read the DC-strip SaveScalars output, resolving columns BY NAME from the
    ``.names`` sidecar (StatCurrentSolver appends its own ``res:`` scalars, so
    fixed positions would be fragile). Returns {current_a (electrode flux),
    joule_w, effective_resistance_ohm} (missing entries omitted), or None when
    the files are absent/unreadable."""
    data_path = os.path.join(case_dir, scalars)
    names_path = data_path + ".names"
    if not (os.path.isfile(data_path) and os.path.isfile(names_path)):
        return None
    cols = {}
    for line in open(names_path, encoding="utf-8"):
        line = line.strip()
        if ":" in line and line.split(":")[0].strip().isdigit():
            idx, name = line.split(":", 1)
            cols[name.strip().lower()] = int(idx) - 1
    rows = [r for r in open(data_path, encoding="utf-8").read().splitlines() if r.strip()]
    if not rows:
        return None
    vals = [float(v) for v in rows[-1].split()]
    out = {}
    for name, idx in cols.items():
        if idx >= len(vals):
            continue
        # prefix-match: StatCurrentSolver also emits "min/max diffusive flux"
        if name.startswith("diffusive flux"):
            out["current_a"] = abs(vals[idx])
        elif "total joule heating" in name:
            out["joule_w"] = vals[idx]
        elif "effective resistance" in name:
            out["effective_resistance_ohm"] = vals[idx]
    return out or None


# --- AC skin-effect case ----------------------------------------------------------

def skin_slab_sif(*, frequency_hz: float, conductivity_s_m: float, mu_r: float,
                  a_surface: float = 1.0e-3, mesh_name: str = "skin",
                  line_file: str = "line.dat", length_m: float = 0.05,
                  width_m: float = 0.002) -> str:
    """The ``.sif`` deck for the harmonic skin-effect slab:
    ``MagnetoDynamics2DHarmonic`` (complex out-of-plane vector potential) with
    ``Potential Re = a_surface`` on the x=0 surface and 0 at the far end; a
    SaveLine solver writes the complex A(x) profile along the mid-line for
    :func:`parse_line_profile` / :func:`fit_decay_length`."""
    if frequency_hz <= 0 or conductivity_s_m <= 0 or mu_r <= 0:
        raise ValueError("frequency_hz, conductivity_s_m, mu_r must be > 0")
    ym = width_m / 2.0
    return f"""Header
  Mesh DB "." "{mesh_name}"
End
Simulation
  Coordinate System = Cartesian 2D
  Simulation Type = Steady State
  Steady State Max Iterations = 1
  Output Intervals = 0
End
Body 1
  Equation = 1
  Material = 1
End
Material 1
  Electric Conductivity = {conductivity_s_m:.10g}
  Relative Permeability = {mu_r:.10g}
End
Equation 1
  Active Solvers(2) = 1 2
End
Solver 1
  Equation = MgDyn2DHarmonic
  Procedure = "MagnetoDynamics2D" "MagnetoDynamics2DHarmonic"
  Variable = Potential[Potential Re:1 Potential Im:1]
  Frequency = {frequency_hz:.10g}
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
  Steady State Convergence Tolerance = 1.0e-9
End
Solver 2
  Equation = SaveLine
  Procedure = "SaveData" "SaveLine"
  Filename = "{line_file}"
  Polyline Coordinates(2,2) = 0.0 {ym:.10g} {length_m:.10g} {ym:.10g}
  Polyline Divisions(1) = 100
End
Boundary Condition 1
  Target Boundaries(1) = 1
  Potential Re = {a_surface:.10g}
  Potential Im = 0.0
End
Boundary Condition 2
  Target Boundaries(1) = 2
  Potential Re = 0.0
  Potential Im = 0.0
End
"""


def write_skin_effect_case(
    case_dir: str,
    *,
    frequency_hz: float = 50.0,
    conductivity_s_m: float | None = None,
    conductor: str | None = "copper",
    mu_r: float = 1.0,
    depths: float = 5.3,
    nx: int = 100,
    ny: int = 2,
    mesh_name: str = "skin",
    line_file: str = "line.dat",
) -> dict:
    """Write a complete, runnable skin-effect case under ``case_dir``: the slab is
    ``depths`` skin depths deep (the far-end truncation error is e^(−depths), 0.5 %
    at the default 5.3) with ~``nx/depths`` cells per δ. Returns {case_dir, sif,
    mesh_name, line_file, length_m, oracle: skin_depth(...)}."""
    sigma = _conductivity(conductivity_s_m, conductor)
    orc = skin_depth(frequency_hz, conductivity_s_m=sigma, mu_r=mu_r)
    if depths < 3.0:
        raise ValueError("depths < 3 — the far-end truncation would pollute the fit")
    length_m = depths * orc["skin_depth_m"]
    width_m = length_m / 25.0
    mesh_dir = os.path.join(case_dir, mesh_name)
    os.makedirs(mesh_dir, exist_ok=True)
    for fname, text in rect_mesh_files(nx, ny, length_m, width_m).items():
        with open(os.path.join(mesh_dir, fname), "w", encoding="utf-8") as f:
            f.write(text)
    with open(os.path.join(case_dir, "case.sif"), "w", encoding="utf-8") as f:
        f.write(skin_slab_sif(frequency_hz=frequency_hz, conductivity_s_m=sigma,
                              mu_r=mu_r, mesh_name=mesh_name, line_file=line_file,
                              length_m=length_m, width_m=width_m))
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w", encoding="utf-8") as f:
        f.write("case.sif\n")
    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_name": mesh_name,
        "line_file": line_file,
        "length_m": length_m,
        "oracle": orc,
    }


def parse_line_profile(case_dir: str, line_file: str = "line.dat"):
    """Read a SaveLine output of the harmonic solve into a sorted list of
    (x_m, complex A). Columns per the ``.names`` sidecar: 4 = x, 7 = Re, 8 = Im.
    Returns the list, or None when the file is absent/unreadable."""
    path = os.path.join(case_dir, line_file)
    if not os.path.isfile(path):
        return None
    pts = []
    try:
        for ln in open(path, encoding="utf-8"):
            f = ln.split()
            if len(f) >= 8:
                pts.append((float(f[3]), complex(float(f[6]), float(f[7]))))
    except ValueError:
        return None
    pts.sort(key=lambda p: p[0])
    return pts or None


def fit_decay_length(points, x_min: float, x_max: float) -> dict:
    """Least-squares e-folding lengths of a complex exponential profile over
    [x_min, x_max]: ln|A| and (unwrapped) phase are both linear in x for the exact
    skin solution A₀·e^(−x/δ)·e^(−i·x/δ), each with slope −1/δ. Pure-Python (the
    gate helper — feed it :func:`parse_line_profile`'s points). Returns
    {decay_length_m, phase_length_m, n_points}. Raises ValueError with fewer than
    4 usable points."""
    sel = [(x, a) for x, a in points if x_min <= x <= x_max and abs(a) > 0.0]
    if len(sel) < 4:
        raise ValueError(f"only {len(sel)} usable points in [{x_min}, {x_max}]")
    xs = [x for x, _ in sel]
    ln_mag = [math.log(abs(a)) for _, a in sel]
    ph = [math.atan2(a.imag, a.real) for _, a in sel]
    for i in range(1, len(ph)):                        # unwrap
        while ph[i] - ph[i - 1] > math.pi:
            ph[i] -= 2.0 * math.pi
        while ph[i] - ph[i - 1] < -math.pi:
            ph[i] += 2.0 * math.pi

    def slope(ys):
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den

    s_mag, s_ph = slope(ln_mag), slope(ph)
    if s_mag >= 0 or s_ph >= 0:
        raise ValueError("profile does not decay — not a skin-effect solution")
    return {"decay_length_m": -1.0 / s_mag, "phase_length_m": -1.0 / s_ph,
            "n_points": len(sel)}


# --- Tier B5: coupled induction heating (SIMULATION_NEXT) -----------------------
#
# Completes the skin-effect slab into a THERMAL answer: the harmonic
# MagnetoDynamics solve runs once (Before Simulation), MagnetoDynamicsCalcFields
# turns it into a time-averaged Joule loss field, and a transient HeatSolver
# integrates it with adiabatic walls. Two exact anchors:
#
#   P'' = omega^2*sigma*A0^2*delta/4  ( == R_s*|H0|^2/2 with H0 = A0*sqrt(2)/(mu*delta) )
#       — the total dissipation per unit driven area of a thick slab (>= ~5 delta),
#   dT_mean = P*t/(m*cp)              — the adiabatic energy balance.
#
# CalcFields also reports the integrated 'eddy current power' as a SaveScalars
# 'res:' global — the solved P the joule gate reads (lands ~0.03 % off the
# closed form on the default mesh).


def induction_heating_power(frequency_hz: float, conductivity_s_m: float,
                            mu_r: float, a_surface: float) -> float:
    """Exact dissipation per unit driven-surface area of a deep slab,
    P'' = omega^2*sigma*A0^2*delta/4 (identically R_s*|H0|^2/2)."""
    omega = 2.0 * math.pi * frequency_hz
    delta = skin_depth(frequency_hz, conductivity_s_m=conductivity_s_m,
                       mu_r=mu_r)["skin_depth_m"]
    return omega ** 2 * conductivity_s_m * a_surface ** 2 * delta / 4.0


def write_induction_heating_case(
    case_dir: str,
    *,
    frequency_hz: float = 1.0e4,
    conductivity_s_m: float | None = None,
    conductor: str | None = "copper",
    mu_r: float = 1.0,
    a_surface: float = 1.0e-3,
    density_kg_m3: float = 8960.0,
    cp_j_kgk: float = 385.0,
    k_thermal: float = 400.0,
    heat_duration_s: float = 0.01,
    n_steps: int = 20,
    depths: float = 5.3,
    nx: int = 100,
    ny: int = 2,
    mesh_name: str = "skin",
    scalars: str = "induct.dat",
) -> dict:
    """Write the coupled induction-heating slab: the same ``depths``-deep
    geometry as the skin-effect case, plus MagnetoDynamicsCalcFields (Joule
    heating) and a transient adiabatic HeatSolver driven through the Body Force
    ``Joule Heat = True`` idiom. Returns {case_dir, sif, mesh_name, scalars,
    length_m, width_m, oracle:{skin...}, p_area_w_m2, p_total_w_m,
    dt_mean_exact_k, heat_duration_s}. Raises ValueError on non-positive
    inputs or a slab too shallow for the closed form."""
    sigma = _conductivity(conductivity_s_m, conductor)
    if min(density_kg_m3, cp_j_kgk, k_thermal, heat_duration_s) <= 0 or n_steps < 2:
        raise ValueError("thermal properties, duration and n_steps must be positive")
    if depths < 4.0:
        raise ValueError("depths < 4 — the closed-form P'' assumes a deep slab")
    orc = skin_depth(frequency_hz, conductivity_s_m=sigma, mu_r=mu_r)
    length_m = depths * orc["skin_depth_m"]
    width_m = length_m / 25.0
    p_area = induction_heating_power(frequency_hz, sigma, mu_r, a_surface)
    p_total = p_area * width_m                       # W per unit depth (2-D)
    mass_cp = density_kg_m3 * cp_j_kgk * length_m * width_m
    dt_exact = p_total * heat_duration_s / mass_cp
    mesh_dir = os.path.join(case_dir, mesh_name)
    os.makedirs(mesh_dir, exist_ok=True)
    for fname, text in rect_mesh_files(nx, ny, length_m, width_m).items():
        with open(os.path.join(mesh_dir, fname), "w", encoding="utf-8") as f:
            f.write(text)
    sif = f"""Header
  Mesh DB "." "{mesh_name}"
End
Simulation
  Coordinate System = Cartesian 2D
  Simulation Type = Transient
  Timestep Intervals = {n_steps}
  Timestep Sizes = {heat_duration_s / n_steps:.10g}
  Timestepping Method = BDF
  BDF Order = 2
  Output Intervals = 0
End
Body 1
  Equation = 1
  Material = 1
  Body Force = 1
  Initial Condition = 1
End
Initial Condition 1
  Temperature = 0.0
End
Body Force 1
  Joule Heat = Logical True
End
Material 1
  Electric Conductivity = {sigma:.10g}
  Relative Permeability = {mu_r:.10g}
  Density = {density_kg_m3:.10g}
  Heat Conductivity = {k_thermal:.10g}
  Heat Capacity = {cp_j_kgk:.10g}
End
Equation 1
  Active Solvers(4) = 1 2 3 4
End
Solver 1
  Equation = MgDyn2DHarmonic
  Procedure = "MagnetoDynamics2D" "MagnetoDynamics2DHarmonic"
  Variable = Potential[Potential Re:1 Potential Im:1]
  Frequency = {frequency_hz:.10g}
  Exec Solver = Before Simulation
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
End
Solver 2
  Equation = CalcFields
  Procedure = "MagnetoDynamics" "MagnetoDynamicsCalcFields"
  Potential Variable = "Potential"
  Calculate Joule Heating = Logical True
  Exec Solver = Before Simulation
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
End
Solver 3
  Equation = Heat Equation
  Procedure = "HeatSolve" "HeatSolver"
  Variable = Temperature
  Stabilize = True
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
  Nonlinear System Max Iterations = 1
End
Solver 4
  Equation = SaveScalars
  Procedure = "SaveData" "SaveScalars"
  Filename = "{scalars}"
  Variable 1 = Temperature
  Operator 1 = "mean"
End
Boundary Condition 1
  Target Boundaries(1) = 1
  Potential Re = {a_surface:.10g}
  Potential Im = 0.0
End
Boundary Condition 2
  Target Boundaries(1) = 2
  Potential Re = 0.0
  Potential Im = 0.0
End
"""
    with open(os.path.join(case_dir, "case.sif"), "w", encoding="utf-8") as f:
        f.write(sif)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w", encoding="utf-8") as f:
        f.write("case.sif\n")
    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_name": mesh_name,
        "scalars": scalars,
        "length_m": length_m,
        "width_m": width_m,
        "oracle": orc,
        "p_area_w_m2": p_area,
        "p_total_w_m": p_total,
        "dt_mean_exact_k": dt_exact,
        "heat_duration_s": heat_duration_s,
    }


def parse_induction_scalars(case_dir: str, scalars: str = "induct.dat") -> dict | None:
    """Last SaveScalars row, with columns located via the ``.names`` sidecar
    (CalcFields appends its 'res:' globals after the requested variables, so
    positions are not fixed). Returns {t_mean_final_k, eddy_power_w_m} or None
    when the file/columns are absent."""
    path = os.path.join(case_dir, scalars)
    if not os.path.exists(path):
        return None
    names_path = path + ".names"
    cols = {}
    if os.path.exists(names_path):
        for ln in open(names_path, encoding="utf-8"):
            m = ln.strip()
            if ":" in m and m[0].isdigit():
                idx, label = m.split(":", 1)
                cols[label.strip().lower()] = int(idx) - 1
    rows = [ln.split() for ln in open(path, encoding="utf-8") if ln.strip()]
    if not rows:
        return None
    last = [float(v) for v in rows[-1]]
    t_idx = cols.get("mean: temperature", 0)
    p_idx = cols.get("res: eddy current power")
    out = {"t_mean_final_k": last[t_idx] if t_idx < len(last) else None,
           "eddy_power_w_m": (last[p_idx] if p_idx is not None
                              and p_idx < len(last) else None)}
    if out["t_mean_final_k"] is None:
        return None
    return out
