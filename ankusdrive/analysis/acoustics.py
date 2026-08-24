"""Acoustics screening — cavity modes, resonator tuning, wall attenuation.

Pure-Python, FreeCAD-free. A Tier-A screening estimator from
``docs/SIMULATION_NEXT.md``: the closed-form acoustics an enclosure / duct /
resonator design needs *before* any FEM. Four kinds under one tool:

- ``cavity_modes`` — rigid rectangular cavity eigenfrequencies
  f = (c/2)·√((n_x/L_x)² + (n_y/L_y)² + (n_z/L_z)²)            (exact)
- ``helmholtz`` — neck-on-volume resonator f = (c/2π)·√(A/(V·L_eff)),
  L_eff = L + 1.7·r (two flanged-end corrections)              (correlation ±10 %)
- ``mass_law`` — normal-incidence transmission loss of a limp wall
  TL = 20·log₁₀(f·m″) − 47 dB                                  (correlation ±3 dB)
- ``duct_cutoff`` — first cross-mode cutoff: rectangular f_c = c/(2·a),
  circular f_c = 1.8412·c/(π·d); below it only plane waves     (exact)

Fidelity is labeled per kind (``SIMULATION_NEXT.md`` contract): the exact rows
carry ``fidelity="exact"``; the correlations carry ``band_pct`` / ``band_db``.
The rigid-cavity modes double as the oracle the Elmer ``HelmholtzSolve``
acoustic FEM (Tier B1, ``acoustic_fem_submit`` — case builders at the bottom of
this module) is gated against. Lengths mm, temperatures °C.
"""
from __future__ import annotations

import glob
import math
import os

# speed of sound in dry air: c = sqrt(gamma*R*T)
_GAMMA_AIR = 1.400
_R_AIR = 287.05  # J/kg/K


def speed_of_sound(t_ambient_c: float = 20.0) -> float:
    """c = √(γ·R·T) for dry air — 343.2 m/s at 20 °C (exact ideal-gas form)."""
    return math.sqrt(_GAMMA_AIR * _R_AIR * (t_ambient_c + 273.15))


def _cavity_modes(lx_m, ly_m, lz_m, c, n_modes):
    """All (n_x,n_y,n_z) rigid-wall eigenfrequencies up to n_modes, ascending."""
    modes = []
    n_max = 8  # indices beyond this are far past any screening interest
    for nx in range(n_max + 1):
        for ny in range(n_max + 1):
            for nz in range(n_max + 1):
                if nx == ny == nz == 0:
                    continue
                f = (c / 2.0) * math.sqrt(
                    (nx / lx_m) ** 2 + (ny / ly_m) ** 2 + (nz / lz_m) ** 2)
                modes.append((f, [nx, ny, nz]))
    modes.sort(key=lambda m: m[0])
    return modes[:n_modes]


def acoustic_screen(
    kind: str,
    # cavity_modes
    lx_mm: float | None = None,
    ly_mm: float | None = None,
    lz_mm: float | None = None,
    n_modes: int = 10,
    # helmholtz
    neck_area_mm2: float | None = None,
    neck_length_mm: float | None = None,
    cavity_volume_mm3: float | None = None,
    # mass_law
    frequency_hz: float | None = None,
    surface_density_kg_m2: float | None = None,
    # duct_cutoff
    duct_width_mm: float | None = None,
    duct_diameter_mm: float | None = None,
    # shared
    t_ambient_c: float = 20.0,
    c_m_s: float | None = None,
) -> dict:
    """Closed-form acoustics screen (no solver) — see the module docstring for the
    four kinds and their formulas.

    ``kind``: 'cavity_modes' (needs lx/ly/lz_mm; returns the lowest ``n_modes``
    rigid-cavity eigenfrequencies with their [n_x,n_y,n_z] indices — exact, and
    the oracle the acoustic_fem_submit solve is gated against) | 'helmholtz' (neck_area_mm2,
    neck_length_mm, cavity_volume_mm3; flanged end correction L_eff = L + 1.7·r —
    ±10 %) | 'mass_law' (frequency_hz, surface_density_kg_m2; limp-wall
    normal-incidence TL — ±3 dB, and +6 dB per doubling of f or m″) |
    'duct_cutoff' (duct_width_mm or duct_diameter_mm; below f_c only plane waves
    propagate — exact). Sound speed from dry air at ``t_ambient_c`` unless
    ``c_m_s`` is given.

    Returns {kind, c_m_s, fidelity, band_pct, band_db, valid_range_ok, warnings,
    escalate_to} plus per kind: cavity_modes → {modes:[{f_hz, n}], f_fundamental_hz};
    helmholtz → {f_resonance_hz, neck_radius_mm, l_eff_mm}; mass_law → {tl_db,
    fm_product}; duct_cutoff → {f_cutoff_hz, geometry}. Raises ValueError on an
    unknown kind, missing inputs for the kind, or non-positive dimensions."""
    c = float(c_m_s) if c_m_s is not None else speed_of_sound(t_ambient_c)
    if c <= 0:
        raise ValueError("c_m_s must be > 0")
    warnings: list[str] = []
    out = {
        "kind": kind,
        "c_m_s": round(c, 2),
        "band_pct": None,
        "band_db": None,
        # the Tier-B1 higher-order twin: the Elmer HelmholtzSolve FEM, whose
        # gates are exactly these screens (duct standing wave, cavity modes).
        "escalate_to": "acoustic_fem_submit",
    }

    if kind == "cavity_modes":
        if not (lx_mm and ly_mm and lz_mm) or min(lx_mm, ly_mm, lz_mm) <= 0:
            raise ValueError("cavity_modes needs positive lx_mm, ly_mm, lz_mm")
        if n_modes < 1:
            raise ValueError("n_modes must be >= 1")
        modes = _cavity_modes(lx_mm / 1e3, ly_mm / 1e3, lz_mm / 1e3, c, n_modes)
        out["fidelity"] = "exact"
        out["modes"] = [{"f_hz": round(f, 3), "n": n} for f, n in modes]
        out["f_fundamental_hz"] = round(modes[0][0], 3)

    elif kind == "helmholtz":
        if not (neck_area_mm2 and neck_length_mm and cavity_volume_mm3) or \
                min(neck_area_mm2, neck_length_mm, cavity_volume_mm3) <= 0:
            raise ValueError(
                "helmholtz needs positive neck_area_mm2, neck_length_mm, "
                "cavity_volume_mm3")
        a_m2 = neck_area_mm2 * 1e-6
        v_m3 = cavity_volume_mm3 * 1e-9
        r_m = math.sqrt(a_m2 / math.pi)          # equivalent circular neck
        l_eff = neck_length_mm / 1e3 + 1.7 * r_m  # two flanged ends (0.85·r each)
        f = (c / (2.0 * math.pi)) * math.sqrt(a_m2 / (v_m3 * l_eff))
        out["fidelity"] = "correlation"
        out["band_pct"] = 10.0
        out["f_resonance_hz"] = round(f, 3)
        out["neck_radius_mm"] = round(r_m * 1e3, 4)
        out["l_eff_mm"] = round(l_eff * 1e3, 4)
        # lumped model needs the resonator small vs wavelength
        wavelength_m = c / f
        if v_m3 ** (1.0 / 3.0) > wavelength_m / 4.0:
            warnings.append(
                "cavity dimension is not small vs wavelength/4 — the lumped "
                "Helmholtz model degrades toward a cavity-mode problem")

    elif kind == "mass_law":
        if not (frequency_hz and surface_density_kg_m2) or \
                min(frequency_hz, surface_density_kg_m2) <= 0:
            raise ValueError(
                "mass_law needs positive frequency_hz, surface_density_kg_m2")
        fm = frequency_hz * surface_density_kg_m2
        tl = 20.0 * math.log10(fm) - 47.0
        out["fidelity"] = "correlation"
        out["band_db"] = 3.0
        out["tl_db"] = round(tl, 2)
        out["fm_product"] = round(fm, 3)
        if tl < 0:
            warnings.append(
                "f·m″ below the mass-law floor (TL < 0 dB) — the wall is "
                "acoustically transparent at this frequency; the law does not apply")
        else:
            warnings.extend(
                [] if tl < 60 else
                ["TL > 60 dB — flanking/coincidence limits real walls below "
                 "the mass-law line"])

    elif kind == "duct_cutoff":
        if duct_width_mm is not None and duct_diameter_mm is not None:
            raise ValueError("give duct_width_mm OR duct_diameter_mm, not both")
        if duct_width_mm is not None:
            if duct_width_mm <= 0:
                raise ValueError("duct_width_mm must be > 0")
            f_c = c / (2.0 * duct_width_mm / 1e3)
            out["geometry"] = "rectangular"
        elif duct_diameter_mm is not None:
            if duct_diameter_mm <= 0:
                raise ValueError("duct_diameter_mm must be > 0")
            # first asymmetric mode of a rigid circular duct: ka = 1.8412
            f_c = 1.8412 * c / (math.pi * duct_diameter_mm / 1e3)
            out["geometry"] = "circular"
        else:
            raise ValueError("duct_cutoff needs duct_width_mm or duct_diameter_mm")
        out["fidelity"] = "exact"
        out["f_cutoff_hz"] = round(f_c, 3)

    else:
        raise ValueError(
            f"unknown kind {kind!r}; choose from ['cavity_modes', 'duct_cutoff', "
            "'helmholtz', 'mass_law']")

    out["warnings"] = warnings
    out["valid_range_ok"] = not warnings
    return out


# --- Tier B1: Elmer HelmholtzSolve cases (SIMULATION_NEXT) -------------------
#
# Two driven-Helmholtz cases whose gates are the exact screens above:
#
# - duct: a 1-D closed duct driven p=1 at x=0, rigid at x=L. Exact field
#   p(x) = cos(k(L-x))/cos(kL), so the rigid-end pressure 1/cos(kL) is a
#   machine-tight oracle (drive off-resonance: kL away from pi/2 + n*pi).
# - cavity: a 2-D rigid rectangular cavity driven by a Wave Flux patch on a
#   corner of the left edge (a flux source keeps the homogeneous problem
#   ALL-rigid, so the resonances are exactly the acoustic_screen cavity modes).
#   A Scanning sweep tabulates the in-phase response A(f); A flips sign through
#   each eigenfrequency, and interpolating the zero of 1/A against f**2 (the
#   response is ~C/(f_n**2 - f**2)) localizes f_n to well under the sweep step.
#
# Case generation is pure-Python (testable without Elmer); the solve needs the
# ElmerSolver binary and runs through acoustic_fem_submit.

def duct_end_pressure(kl: float) -> float:
    """Exact rigid-end pressure of the driven closed duct: p(L)/p(0) = 1/cos(kL)."""
    return 1.0 / math.cos(kl)


def duct_mean_pressure(kl: float) -> float:
    """Exact domain mean of the duct standing wave: sin(kL)/(kL*cos(kL))."""
    return math.sin(kl) / (kl * math.cos(kl))


def helmholtz_duct_mesh_files(n_elements: int, length_m: float) -> dict:
    """Native Elmer 1-D line mesh: tag 1 = driven end x=0, tag 2 = rigid end x=L."""
    n_nodes = n_elements + 1
    nodes = "".join(
        f"{i} -1 {(i - 1) * length_m / n_elements:.10g} 0.0 0.0\n"
        for i in range(1, n_nodes + 1))
    elements = "".join(
        f"{e} 1 202 {e} {e + 1}\n" for e in range(1, n_elements + 1))
    boundary = f"1 1 1 0 101 1\n2 2 {n_elements} 0 101 {n_nodes}\n"
    header = f"{n_nodes} {n_elements} 2\n2\n101 2\n202 {n_elements}\n"
    return {"mesh.header": header, "mesh.nodes": nodes,
            "mesh.elements": elements, "mesh.boundary": boundary}


def write_helmholtz_duct_case(
    case_dir: str,
    *,
    length_m: float = 1.0,
    kl: float = 2.0,
    n_elements: int = 200,
    c_m_s: float = 343.0,
    mesh_name: str = "duct",
    scalars: str = "duct.dat",
) -> dict:
    """Write the driven closed-duct Helmholtz case. ``kl`` sets the drive
    frequency f = kL*c/(2*pi*L); keep it away from the quarter-wave resonances
    (pi/2 + n*pi) where the exact answer diverges. Returns {case_dir, sif,
    mesh_db, scalars, frequency_hz, kl, p_end_exact, p_mean_exact}."""
    if length_m <= 0 or n_elements < 8 or c_m_s <= 0:
        raise ValueError("need length_m > 0, n_elements >= 8, c_m_s > 0")
    if abs(math.cos(kl)) < 0.05:
        raise ValueError(f"kl = {kl:g} is within 5% of a duct resonance — "
                         "the exact oracle diverges there; pick another kl")
    frequency_hz = kl * c_m_s / (2.0 * math.pi * length_m)
    mesh_dir = os.path.join(case_dir, mesh_name)
    os.makedirs(mesh_dir, exist_ok=True)
    for name, content in helmholtz_duct_mesh_files(n_elements, length_m).items():
        with open(os.path.join(mesh_dir, name), "w", encoding="utf-8") as f:
            f.write(content)
    sif = f"""Header
  Mesh DB "." "{mesh_name}"
End

Simulation
  Coordinate System = Cartesian 1D
  Simulation Type = Steady State
  Steady State Max Iterations = 1
  Output Intervals = 0
  Frequency = {frequency_hz:.10g}
End

Body 1
  Equation = 1
  Material = 1
End

Equation 1
  Active Solvers(1) = 1
End

Material 1
  Density = 1.205
  Sound Speed = {c_m_s:.10g}
End

Solver 1
  Equation = Helmholtz Equation
  Procedure = "HelmholtzSolve" "HelmholtzSolver"
  Variable = Pressure
  Variable Dofs = 2
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
End

Solver 2
  Equation = SaveScalars
  Procedure = "SaveData" "SaveScalars"
  Filename = "{scalars}"
  Variable 1 = Pressure 1
  Operator 1 = "boundary mean"
  Variable 2 = Pressure 2
  Operator 2 = "boundary mean"
  Variable 3 = Pressure 1
  Operator 3 = "mean"
End

Boundary Condition 1
  Target Boundaries(1) = 1
  Pressure 1 = 1.0
  Pressure 2 = 0.0
End

Boundary Condition 2
  Target Boundaries(1) = 2
  Save Scalars = True
End
"""
    with open(os.path.join(case_dir, "case.sif"), "w", encoding="utf-8") as f:
        f.write(sif)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w", encoding="utf-8") as f:
        f.write("case.sif\n")
    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_db": mesh_name,
        "scalars": scalars,
        "frequency_hz": frequency_hz,
        "kl": kl,
        "p_end_exact": duct_end_pressure(kl),
        "p_mean_exact": duct_mean_pressure(kl),
    }


def parse_helmholtz_duct(case_dir: str, scalars: str = "duct.dat") -> dict | None:
    """Last SaveScalars row -> {p_end_re, p_end_im, p_mean_re}. None if absent."""
    path = os.path.join(case_dir, scalars)
    if not os.path.exists(path):
        hits = glob.glob(os.path.join(case_dir, scalars + "*"))
        hits = [h for h in hits if not h.endswith(".names")]
        if not hits:
            return None
        path = hits[0]
    rows = [ln.split() for ln in open(path, encoding="utf-8") if ln.strip()]
    if not rows:
        return None
    last = [float(v) for v in rows[-1]]
    return {"p_end_re": last[0], "p_end_im": last[1], "p_mean_re": last[2]}


def helmholtz_cavity_mesh_files(
    lx_m: float, ly_m: float, nx: int, ny: int, drive_fraction: float = 0.125,
) -> dict:
    """Native Elmer 2-D quad mesh of the rectangular cavity. Boundary tag 1 is
    the Wave Flux drive patch (the bottom ``drive_fraction`` of the left edge —
    off-center so oblique modes are excited too); tag 3 is a one-element probe
    at the far corner (Lx, Ly) — a corner is never on a nodal line and keeps a
    fixed sign per mode, so the in-phase probe flips sign cleanly through each
    resonance; tag 2 is every other rigid wall."""
    nnx, nny = nx + 1, ny + 1

    def nid(i, j):
        return j * nnx + i + 1

    def parent(i, j):
        return j * nx + i + 1

    nodes = []
    for j in range(nny):
        for i in range(nnx):
            nodes.append(
                f"{nid(i, j)} -1 {i * lx_m / nx:.10g} {j * ly_m / ny:.10g} 0.0\n")
    elements = []
    eid = 0
    for j in range(ny):
        for i in range(nx):
            eid += 1
            elements.append(
                f"{eid} 1 404 {nid(i, j)} {nid(i + 1, j)} "
                f"{nid(i + 1, j + 1)} {nid(i, j + 1)}\n")
    boundary, bid = [], 0
    drive_n = max(2, int(round(ny * drive_fraction)))
    for j in range(ny):           # left edge: drive patch then rigid
        bid += 1
        tag = 1 if j < drive_n else 2
        boundary.append(f"{bid} {tag} {parent(0, j)} 0 202 {nid(0, j)} {nid(0, j + 1)}\n")
    for j in range(ny):           # right edge; top element is the corner probe
        bid += 1
        tag = 3 if j == ny - 1 else 2
        boundary.append(f"{bid} {tag} {parent(nx - 1, j)} 0 202 {nid(nx, j)} {nid(nx, j + 1)}\n")
    for i in range(nx):           # bottom + top edges
        bid += 1
        boundary.append(f"{bid} 2 {parent(i, 0)} 0 202 {nid(i, 0)} {nid(i + 1, 0)}\n")
        bid += 1
        boundary.append(f"{bid} 2 {parent(i, ny - 1)} 0 202 {nid(i, ny)} {nid(i + 1, ny)}\n")
    header = f"{nnx * nny} {nx * ny} {bid}\n2\n202 {bid}\n404 {nx * ny}\n"
    return {"mesh.header": header, "mesh.nodes": "".join(nodes),
            "mesh.elements": "".join(elements), "mesh.boundary": "".join(boundary)}


def _frequency_table(freqs) -> str:
    """A tabulated Elmer dependency: Frequency = Variable time, evaluated at the
    integer scan steps 1..N -> exactly the listed frequencies."""
    rows = "".join(f"      {i + 1}.0 {f:.10g}\n" for i, f in enumerate(freqs))
    return f"Variable time\n    Real\n{rows}    End"


def write_helmholtz_cavity_case(
    case_dir: str,
    *,
    lx_m: float = 0.5,
    ly_m: float = 0.4,
    nx: int = 50,
    ny: int = 40,
    mode_nx: int = 1,
    mode_ny: int = 0,
    span_pct: float = 8.0,
    n_steps: int = 13,
    c_m_s: float = 343.0,
    mesh_name: str = "cav",
    scalars: str = "cav.dat",
) -> dict:
    """Write the flux-driven cavity sweep around the exact (mode_nx, mode_ny)
    rigid-cavity eigenfrequency (from the same closed form acoustic_screen
    uses). The sweep spans ±span_pct% in n_steps Scanning steps; sweep points
    avoid landing exactly on the eigenvalue (singular matrix). Returns
    {case_dir, sif, mesh_db, scalars, f_exact_hz, freqs, mode}."""
    if min(lx_m, ly_m) <= 0 or nx < 8 or ny < 8:
        raise ValueError("need positive lx_m/ly_m and nx, ny >= 8")
    if mode_nx < 0 or mode_ny < 0 or mode_nx + mode_ny == 0:
        raise ValueError("mode_nx/mode_ny must be >= 0 with at least one > 0")
    if n_steps < 5:
        raise ValueError("n_steps must be >= 5 to bracket the resonance")
    f_exact = (c_m_s / 2.0) * math.sqrt((mode_nx / lx_m) ** 2 + (mode_ny / ly_m) ** 2)
    span = span_pct / 100.0
    # even n_steps straddles f_exact without ever evaluating exactly on it
    n_steps += (n_steps % 2 == 1)
    freqs = [f_exact * (1.0 - span + 2.0 * span * i / (n_steps - 1))
             for i in range(n_steps)]
    mesh_dir = os.path.join(case_dir, mesh_name)
    os.makedirs(mesh_dir, exist_ok=True)
    for name, content in helmholtz_cavity_mesh_files(lx_m, ly_m, nx, ny).items():
        with open(os.path.join(mesh_dir, name), "w", encoding="utf-8") as f:
            f.write(content)
    sif = f"""Header
  Mesh DB "." "{mesh_name}"
End

Simulation
  Coordinate System = Cartesian 2D
  Simulation Type = Scanning
  Timestep Intervals = {n_steps}
  Output Intervals = 0
  Frequency = {_frequency_table(freqs)}
End

Body 1
  Equation = 1
  Material = 1
End

Equation 1
  Active Solvers(1) = 1
End

Material 1
  Density = 1.205
  Sound Speed = {c_m_s:.10g}
End

Solver 1
  Equation = Helmholtz Equation
  Procedure = "HelmholtzSolve" "HelmholtzSolver"
  Variable = Pressure
  Variable Dofs = 2
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
End

Solver 2
  Equation = SaveScalars
  Procedure = "SaveData" "SaveScalars"
  Filename = "{scalars}"
  Variable 1 = Pressure 1
  Operator 1 = "boundary mean"
End

Boundary Condition 1
  Target Boundaries(1) = 1
  Wave Flux 1 = 1.0
  Wave Flux 2 = 0.0
End

Boundary Condition 2
  Target Boundaries(1) = 3
  Save Scalars = True
End
"""
    with open(os.path.join(case_dir, "case.sif"), "w", encoding="utf-8") as f:
        f.write(sif)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w", encoding="utf-8") as f:
        f.write("case.sif\n")
    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_db": mesh_name,
        "scalars": scalars,
        "f_exact_hz": f_exact,
        "freqs": freqs,
        "mode": [mode_nx, mode_ny],
    }


def parse_helmholtz_cavity(case_dir: str, scalars: str = "cav.dat") -> list | None:
    """SaveScalars sweep rows -> [(response, frequency_hz), ...]. The 'max abs'
    operator keeps the sign of the extreme value — exactly what the resonance
    locator needs. None when the file is absent/empty."""
    path = os.path.join(case_dir, scalars)
    if not os.path.exists(path):
        hits = glob.glob(os.path.join(case_dir, scalars + "*"))
        hits = [h for h in hits if not h.endswith(".names")]
        if not hits:
            return None
        path = hits[0]
    rows = []
    for ln in open(path, encoding="utf-8"):
        parts = ln.split()
        if len(parts) >= 2:
            rows.append((float(parts[0]), float(parts[1])))
    return rows or None


def locate_resonance(rows) -> float | None:
    """Eigenfrequency from a swept in-phase response: A(f) ~ C/(f_n**2 - f**2)
    flips sign through f_n, so 1/A is ~linear in f**2 there — interpolate its
    zero on the sign-flip pair with the largest response magnitude. Returns the
    f_n estimate in Hz, or None if no flip was captured."""
    best, best_mag = None, 0.0
    for (a1, f1), (a2, f2) in zip(rows, rows[1:]):
        if a1 == 0.0 or a2 == 0.0 or (a1 > 0) == (a2 > 0):
            continue
        mag = min(abs(a1), abs(a2))
        if mag > best_mag:
            best_mag = mag
            x1, x2 = f1 * f1, f2 * f2
            y1, y2 = 1.0 / a1, 1.0 / a2
            best = math.sqrt(x1 - y1 * (x2 - x1) / (y2 - y1))
    return best
