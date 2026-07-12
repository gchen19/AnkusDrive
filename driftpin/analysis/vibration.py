"""Random-vibration response off a modal run — Miles' equation, no new solver.

Pure-Python, FreeCAD-free. The structural-extension member of simulation family 5
(``docs/SIMULATION_TOOLS.md``) that needs *no* external solver: it layers PSD
(power-spectral-density) math on top of the modal frequencies the existing
``fem_modal`` / ``fem_modal_results`` path already produces. Because the modal
solve has already run, there is nothing to background here — this is a closed-form
post-process (no ``jobs.py``), the lowest-risk first PR of the P2 tier.

Model — Miles' equation. A single-DOF resonator at natural frequency ``f_n`` with
amplification (quality factor) ``Q``, driven by a base-acceleration PSD that is
``W`` (g²/Hz) at ``f_n``, has a 1-σ (RMS) acceleration response

    GRMS = sqrt( (π/2) · f_n · W · Q ).

A real part has several modes; treating each as an independent SDOF resonator and
combining by SRSS gives ``rms_g = sqrt(Σ_i (π/2)·f_i·W(f_i)·Q)`` — which collapses
to Miles exactly for one dominant mode. The PSD level at each mode is read from the
profile by **log-log interpolation** (the convention for vibration specs: constant
dB/octave segments), and is **zero outside the profile's frequency band** — a mode
stiffened above the excitation band escapes resonant drive, which is the whole
point of the ruggedization rule "push the first mode above the test band."

Units: frequencies Hz, PSD g²/Hz, response g (RMS). Optional modal stress coupling
(``modal_stress_mpa_per_g``) converts the g response to an RMS / 3-σ stress. See
``docs/SIMULATION_EXAMPLES.md`` §5 for the worked toy (f_n=312 Hz, W=0.01, Q=10 →
GRMS = 7.0 g).
"""
from __future__ import annotations

import math
import os
import re

_HALF_PI = math.pi / 2.0


def _normalize_profile(psd_profile) -> list:
    """Validate + sort a PSD profile into ascending [(hz, g2_hz), ...] tuples.
    Accepts dicts ({"hz":..,"g2_hz":..}) or 2-sequences. Raises ValueError if
    empty or if any breakpoint has a non-positive frequency."""
    pts = []
    for bp in psd_profile or []:
        if isinstance(bp, dict):
            f, w = bp["hz"], bp["g2_hz"]
        else:
            f, w = bp[0], bp[1]
        f = float(f)
        if f <= 0:
            raise ValueError(f"PSD breakpoint frequency must be > 0 (got {f})")
        pts.append((f, float(w)))
    if not pts:
        raise ValueError("psd_profile must have at least one breakpoint")
    pts.sort(key=lambda p: p[0])
    return pts


def psd_at(psd_profile, f_hz: float) -> float:
    """PSD level (g²/Hz) at ``f_hz`` from a breakpoint profile.

    Log-log interpolation between adjacent breakpoints (constant slope per octave,
    the vibration-spec convention); falls back to linear when a segment endpoint is
    non-positive. A single-breakpoint profile is a flat PSD at that level for all
    frequencies. With ≥2 breakpoints the profile defines a band [f_min, f_max] and
    the level is **0 outside it** (no specified excitation = no resonant drive)."""
    pts = _normalize_profile(psd_profile)
    if len(pts) == 1:                                # single point -> flat everywhere
        return pts[0][1]
    f_min, f_max = pts[0][0], pts[-1][0]
    if f_hz < f_min or f_hz > f_max:                 # outside the specified band
        return 0.0
    for (f0, w0), (f1, w1) in zip(pts, pts[1:]):
        if f0 <= f_hz <= f1:
            if f1 == f0:
                return w1
            if w0 <= 0 or w1 <= 0:                   # can't log a zero/neg -> linear
                t = (f_hz - f0) / (f1 - f0)
                return w0 + t * (w1 - w0)
            # w = w0 * (f/f0) ** slope, slope in log-log space
            slope = math.log(w1 / w0) / math.log(f1 / f0)
            return w0 * (f_hz / f0) ** slope
    return 0.0                                       # unreachable (covered above)


def miles_grms(f_n: float, w_g2_hz: float, q: float) -> float:
    """Miles' single-DOF RMS acceleration response (g): sqrt((π/2)·f_n·W·Q)."""
    if f_n <= 0:
        raise ValueError("f_n must be > 0")
    if q <= 0:
        raise ValueError("q (amplification) must be > 0")
    return math.sqrt(_HALF_PI * f_n * max(w_g2_hz, 0.0) * q)


# Dimensionless eigenvalues (βL)_n of the Euler–Bernoulli beam by boundary condition
# — roots of the mode-shape characteristic equation. The natural frequencies are
# f_n = (βL)_n² / (2π) · sqrt(E·I / (ρ·A·L⁴)).
_BEAM_BETA_L = {
    # clamped–free: roots of cos(βL)·cosh(βL) = −1
    "cantilever":       (1.8751041, 4.6940911, 7.8547574, 10.9955407, 14.1371684),
    # pinned–pinned: βL = nπ (exact)
    "simply_supported": (math.pi, 2 * math.pi, 3 * math.pi, 4 * math.pi, 5 * math.pi),
    # clamped–clamped / free–free (same nonzero roots): cos(βL)·cosh(βL) = 1
    "clamped_clamped":  (4.7300408, 7.8532046, 10.9956078, 14.1371655, 17.2787596),
    "free_free":        (4.7300408, 7.8532046, 10.9956078, 14.1371655, 17.2787596),
    # clamped–pinned: tan(βL) = tanh(βL)
    "clamped_pinned":   (3.9266023, 7.0685827, 10.2101761, 13.3517688, 16.4933614),
}


def beam_natural_frequencies(
    length_mm: float,
    width_mm: float,
    height_mm: float,
    boundary: str = "cantilever",
    n_modes: int = 3,
    youngs_gpa: float | None = None,
    density_kg_m3: float | None = None,
    material: str | None = None,
) -> dict:
    """Exact Euler–Bernoulli natural frequencies of a uniform rectangular beam — the
    closed-form oracle the FEM modal solve (``fem_modal`` / CalculiX) is gated against.

    The transverse-bending frequencies are f_n = (βL)_n²/(2π)·sqrt(E·I/(ρ·A·L⁴)), where
    (βL)_n are the boundary-condition eigenvalues (``cantilever``, ``simply_supported``,
    ``clamped_clamped``, ``free_free``, ``clamped_pinned``). The beam bends in the
    ``height`` direction, so I = width·height³/12 and A = width·height. Slender-beam
    theory: accurate while L ≫ height (thick beams need a Timoshenko shear correction;
    higher modes drift first).

    E from ``youngs_gpa`` or a Materials-DB ``material``; ρ from ``density_kg_m3`` or the
    material. Lengths mm. Returns {boundary, n_modes, frequencies_hz, beta_l,
    first_mode_hz, youngs_gpa, density_kg_m3, area_mm2, I_mm4, slenderness}. Raises
    ValueError on bad geometry, unknown boundary, n_modes>5, or unresolved E/ρ."""
    if boundary not in _BEAM_BETA_L:
        raise ValueError(f"boundary must be one of {sorted(_BEAM_BETA_L)}")
    if length_mm <= 0 or width_mm <= 0 or height_mm <= 0:
        raise ValueError("length_mm, width_mm, height_mm must be > 0")
    betas = _BEAM_BETA_L[boundary]
    if not 1 <= n_modes <= len(betas):
        raise ValueError(f"n_modes must be in 1..{len(betas)} for {boundary!r}")

    E, rho = _resolve_beam_material(youngs_gpa, density_kg_m3, material)
    L = length_mm / 1000.0
    b = width_mm / 1000.0
    h = height_mm / 1000.0
    A = b * h                                            # m²
    I = b * h ** 3 / 12.0                                # m⁴ (bending in height)
    coeff = math.sqrt(E * I / (rho * A * L ** 4)) / (2.0 * math.pi)
    freqs = [round((bl * bl) * coeff, 4) for bl in betas[:n_modes]]
    return {
        "boundary": boundary,
        "n_modes": n_modes,
        "frequencies_hz": freqs,
        "beta_l": [round(bl, 6) for bl in betas[:n_modes]],
        "first_mode_hz": freqs[0],
        "youngs_gpa": round(E / 1e9, 4),
        "density_kg_m3": round(rho, 3),
        "area_mm2": round(A * 1e6, 4),
        "I_mm4": round(I * 1e12, 4),
        "slenderness": round(length_mm / height_mm, 3),
    }


def _resolve_beam_material(youngs_gpa, density_kg_m3, material):
    """(E [Pa], ρ [kg/m³]) from explicit youngs_gpa/density_kg_m3, else a Materials-DB
    card by name. Raises ValueError if neither resolves both."""
    E = youngs_gpa * 1e9 if youngs_gpa is not None else None
    rho = float(density_kg_m3) if density_kg_m3 is not None else None
    if (E is None or rho is None) and material:
        from . import materials
        card = materials.get(material)
        if E is None:
            e_gpa = materials.numeric(card, "youngs_gpa")
            E = e_gpa * 1e9 if e_gpa else None
        if rho is None:
            rho = materials.numeric(card, "density_kg_m3")
    if not E or not rho:
        raise ValueError(
            "provide youngs_gpa + density_kg_m3, or a material with both")
    return E, rho


def random_vibration(
    frequencies_hz,
    psd_profile,
    q: float = 10.0,
    modal_stress_mpa_per_g: float | None = None,
    allowable_stress_mpa: float | None = None,
) -> dict:
    """Random-vibration response from modal frequencies + a base-acceleration PSD.

    ``frequencies_hz`` are the natural frequencies (from ``fem_modal_results``).
    Each mode is treated as an SDOF resonator with amplification ``q`` (default 10;
    a common rule of thumb is Q≈√f_n) and driven by the PSD level at its frequency;
    the modal responses combine by SRSS. ``rms_g`` is that combined 1-σ response;
    ``miles_grms_g`` is the pure SDOF Miles anchor at the first (lowest) mode.

    With ``modal_stress_mpa_per_g`` (peak modal stress per 1 g of RMS response) the
    g response is converted to ``rms_stress_mpa`` and the 3-σ design value
    ``three_sigma_stress_mpa``; with ``allowable_stress_mpa`` too, ``pass`` is the
    3-σ-below-allowable check (else None).

    Returns ``{rms_g, first_mode_hz, dominant_mode_hz, q, psd_band_hz, miles_grms_g,
    modes: [{mode, frequency_hz, psd_g2_hz, contribution_g, in_band}],
    rms_stress_mpa, three_sigma_stress_mpa, pass}``. Raises ValueError on empty
    inputs or non-positive q."""
    freqs = [float(f) for f in (frequencies_hz or [])]
    if not freqs:
        raise ValueError("frequencies_hz must be non-empty (run fem_modal first)")
    if q <= 0:
        raise ValueError("q (amplification) must be > 0")
    pts = _normalize_profile(psd_profile)
    band = [pts[0][0], pts[-1][0]]

    freqs.sort()
    modes = []
    sum_sq = 0.0
    for i, f in enumerate(freqs, start=1):
        w = psd_at(psd_profile, f)
        contrib = miles_grms(f, w, q)                # SDOF response of this mode
        sum_sq += contrib * contrib
        modes.append({
            "mode": i,
            "frequency_hz": round(f, 3),
            "psd_g2_hz": round(w, 8),
            "contribution_g": round(contrib, 4),
            "in_band": band[0] <= f <= band[1],
        })

    rms_g = math.sqrt(sum_sq)
    # Dominant mode = largest single-mode contribution (drives the SRSS); ties go to
    # the lowest frequency (modes is frequency-sorted, max() keeps the first max).
    dominant = max(modes, key=lambda m: m["contribution_g"])
    miles_first = modes[0]["contribution_g"]         # SDOF anchor at the first mode

    out = {
        "rms_g": round(rms_g, 4),
        "first_mode_hz": round(freqs[0], 3),
        "dominant_mode_hz": dominant["frequency_hz"],
        "q": q,
        "psd_band_hz": [round(band[0], 3), round(band[1], 3)],
        "miles_grms_g": miles_first,
        "modes": modes,
        "rms_stress_mpa": None,
        "three_sigma_stress_mpa": None,
        "pass": None,
    }

    if modal_stress_mpa_per_g is not None:
        rms_stress = rms_g * float(modal_stress_mpa_per_g)
        three_sigma = 3.0 * rms_stress
        out["rms_stress_mpa"] = round(rms_stress, 3)
        out["three_sigma_stress_mpa"] = round(three_sigma, 3)
        if allowable_stress_mpa is not None:
            out["pass"] = bool(three_sigma <= float(allowable_stress_mpa))

    return out


# --- Tier B2: harmonic forced response (SIMULATION_NEXT) ----------------------
#
# The SDOF frequency-response oracle (exact closed forms) plus the Elmer
# StressSolve harmonic-sweep case it gates: a plane-stress cantilever driven by
# a harmonic tip traction, swept through its first resonance. Three gates, all
# from the real (in-phase) response the solver writes:
#   - Re(H) = 0 exactly AT f_n (any damping) -> the swept tip response flips
#     sign through resonance; interpolating the zero against f**2 localizes f_1
#     to compare with the Euler-Bernoulli beam_modal closed form.
#   - the low-frequency point is the static tip compliance F*L**3/(3*E*I).
#   - max|Re(H)| = Q/2 times static (light damping) — the damping/Q gate.
# Case generation is pure-Python; the solve runs through harmonic_response_submit.

def harmonic_response(
    natural_frequency_hz: float,
    damping_ratio: float,
    frequency_hz: float | None = None,
    static_deflection_mm: float | None = None,
) -> dict:
    """Exact SDOF harmonic frequency response (no solver) — the FRF oracle the
    Elmer ``harmonic_response_submit`` sweep is gated against, and the bridge
    between ``beam_modal`` (which gives f_n) and ``random_vibration`` (whose Q
    is 1/(2ζ)). With r = f/f_n:

        |H| = 1/sqrt((1-r²)² + (2ζr)²),  phase = atan2(2ζr, 1-r²)
        Q (peak amplification) = 1/(2ζ·sqrt(1-ζ²)),  f_peak = f_n·sqrt(1-2ζ²)
        half-power bandwidth Δf ≈ 2ζ·f_n (= f_n/Q, light damping)

    With ``frequency_hz`` the response at that drive is returned;
    ``static_deflection_mm`` scales |H| into an absolute ``amplitude_mm``. For
    ζ ≥ 1/√2 there is no resonant peak (f_peak/q_factor are None, flagged).

    Returns {natural_frequency_hz, damping_ratio, q_factor, f_peak_hz,
    half_power_bandwidth_hz, frequency_ratio, amplification, phase_deg,
    amplitude_mm, fidelity, band_pct, valid_range_ok, warnings, escalate_to}.
    Raises ValueError on non-positive f_n / drive or ζ outside (0, 1)."""
    if natural_frequency_hz <= 0:
        raise ValueError("natural_frequency_hz must be > 0")
    if not 0.0 < damping_ratio < 1.0:
        raise ValueError("damping_ratio must be in (0, 1)")
    fn, z = natural_frequency_hz, damping_ratio
    warnings: list[str] = []
    if z < 1.0 / math.sqrt(2.0):
        q = 1.0 / (2.0 * z * math.sqrt(1.0 - z * z))
        f_peak = fn * math.sqrt(1.0 - 2.0 * z * z)
    else:
        q = f_peak = None
        warnings.append("ζ ≥ 1/√2 — overdamped response, no resonant peak")
    out = {
        "natural_frequency_hz": round(fn, 4),
        "damping_ratio": z,
        "q_factor": (round(q, 4) if q is not None else None),
        "f_peak_hz": (round(f_peak, 4) if f_peak is not None else None),
        "half_power_bandwidth_hz": round(2.0 * z * fn, 4),
        "frequency_ratio": None,
        "amplification": None,
        "phase_deg": None,
        "amplitude_mm": None,
        "fidelity": "exact",
        "band_pct": None,
        "escalate_to": "harmonic_response_submit",
    }
    if frequency_hz is not None:
        if frequency_hz <= 0:
            raise ValueError("frequency_hz must be > 0")
        r = frequency_hz / fn
        amp = 1.0 / math.sqrt((1.0 - r * r) ** 2 + (2.0 * z * r) ** 2)
        out["frequency_ratio"] = round(r, 6)
        out["amplification"] = round(amp, 6)
        out["phase_deg"] = round(math.degrees(math.atan2(2.0 * z * r, 1.0 - r * r)), 4)
        if static_deflection_mm is not None:
            out["amplitude_mm"] = round(static_deflection_mm * amp, 6)
    out["warnings"] = warnings
    out["valid_range_ok"] = not warnings
    return out


def harmonic_beam_mesh_files(length_m: float, height_m: float, nx: int, ny: int) -> dict:
    """Native Elmer 2-D quad mesh of the cantilever strip: boundary tag 1 is the
    clamped root (x=0), tag 2 the driven tip edge (x=L)."""
    nnx, nny = nx + 1, ny + 1

    def nid(i, j):
        return j * nnx + i + 1

    def parent(i, j):
        return j * nx + i + 1

    nodes = []
    for j in range(nny):
        for i in range(nnx):
            nodes.append(
                f"{nid(i, j)} -1 {i * length_m / nx:.10g} {j * height_m / ny:.10g} 0.0\n")
    elements = []
    eid = 0
    for j in range(ny):
        for i in range(nx):
            eid += 1
            elements.append(
                f"{eid} 1 404 {nid(i, j)} {nid(i + 1, j)} "
                f"{nid(i + 1, j + 1)} {nid(i, j + 1)}\n")
    boundary, bid = [], 0
    for j in range(ny):
        bid += 1
        boundary.append(f"{bid} 1 {parent(0, j)} 0 202 {nid(0, j)} {nid(0, j + 1)}\n")
        bid += 1
        boundary.append(f"{bid} 2 {parent(nx - 1, j)} 0 202 {nid(nx, j)} {nid(nx, j + 1)}\n")
    header = f"{nnx * nny} {nx * ny} {bid}\n2\n202 {bid}\n404 {nx * ny}\n"
    return {"mesh.header": header, "mesh.nodes": "".join(nodes),
            "mesh.elements": "".join(elements), "mesh.boundary": "".join(boundary)}


def write_harmonic_beam_case(
    case_dir: str,
    *,
    length_m: float = 0.2,
    height_m: float = 0.01,
    nx: int = 80,
    ny: int = 4,
    youngs_pa: float = 200e9,
    density_kg_m3: float = 7850.0,
    poisson: float = 0.3,
    damping_ratio: float = 0.02,
    traction_pa: float = 1000.0,
    span_pct: float = 10.0,
    n_sweep: int = 21,
    mesh_name: str = "beam",
    output: str = "frf",
) -> dict:
    """Write the harmonic-swept plane-stress cantilever case: clamped at x=0,
    harmonic tip traction (y) at x=L, Rayleigh β = 2ζ/ω₁ tuned to give
    ``damping_ratio`` at the first mode. The Scanning sweep runs one
    quasi-static point (f₁/40) followed by ``n_sweep`` points spanning
    ±span_pct% of the Euler-Bernoulli f₁. ResultOutput writes one ASCII .vtu
    per step into the mesh directory. Returns {case_dir, sif, mesh_db, output,
    freqs, f1_eb_hz, static_exact_m, q_factor, damping_ratio, tip_node,
    n_steps}."""
    if min(length_m, height_m) <= 0 or nx < 16 or ny < 2:
        raise ValueError("need positive dimensions and nx >= 16, ny >= 2")
    if not 0.0 < damping_ratio < 0.2:
        raise ValueError("damping_ratio must be in (0, 0.2) for a meaningful peak")
    if n_sweep < 9:
        raise ValueError("n_sweep must be >= 9 to resolve the peak")
    i_area = height_m ** 3 / 12.0           # per unit depth
    area = height_m
    f1 = (1.8751041 ** 2 / (2.0 * math.pi)) * math.sqrt(
        youngs_pa * i_area / (density_kg_m3 * area * length_m ** 4))
    beta = 2.0 * damping_ratio / (2.0 * math.pi * f1)
    span = span_pct / 100.0
    freqs = [f1 / 40.0] + [
        f1 * (1.0 - span + 2.0 * span * i / (n_sweep - 1)) for i in range(n_sweep)]
    force_n = traction_pa * height_m        # per unit depth
    static_exact = force_n * length_m ** 3 / (3.0 * youngs_pa * i_area)
    mesh_dir = os.path.join(case_dir, mesh_name)
    os.makedirs(mesh_dir, exist_ok=True)
    for name, content in harmonic_beam_mesh_files(length_m, height_m, nx, ny).items():
        with open(os.path.join(mesh_dir, name), "w", encoding="utf-8") as f:
            f.write(content)
    freq_rows = "".join(f"      {i + 1}.0 {f:.10g}\n" for i, f in enumerate(freqs))
    sif = f"""Header
  Mesh DB "." "{mesh_name}"
End

Simulation
  Coordinate System = Cartesian 2D
  Simulation Type = Scanning
  Timestep Intervals = {len(freqs)}
  Output Intervals = 0
  Frequency = Variable time
    Real
{freq_rows}    End
End

Body 1
  Equation = 1
  Material = 1
End

Equation 1
  Active Solvers(1) = 1
  Plane Stress = True
End

Material 1
  Density = {density_kg_m3:.10g}
  Youngs Modulus = {youngs_pa:.10g}
  Poisson Ratio = {poisson:.10g}
  Rayleigh Damping Alpha = 0.0
  Rayleigh Damping Beta = {beta:.10g}
End

Solver 1
  Equation = Linear Elasticity
  Procedure = "StressSolve" "StressSolver"
  Variable = Displacement
  Variable Dofs = 2
  Harmonic Analysis = True
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
End

Solver 2
  Equation = ResultOutput
  Procedure = "ResultOutputSolve" "ResultOutputSolver"
  Output File Name = "{output}"
  Vtu Format = True
  Ascii Output = True
End

Boundary Condition 1
  Target Boundaries(1) = 1
  Displacement 1 = 0.0
  Displacement 2 = 0.0
End

Boundary Condition 2
  Target Boundaries(1) = 2
  Force 2 = {traction_pa:.10g}
End
"""
    with open(os.path.join(case_dir, "case.sif"), "w", encoding="utf-8") as f:
        f.write(sif)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w", encoding="utf-8") as f:
        f.write("case.sif\n")
    tip_node = (ny // 2) * (nx + 1) + nx    # 0-based index of the mid-tip node
    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_db": mesh_name,
        "output": output,
        "freqs": freqs,
        "f1_eb_hz": f1,
        "static_exact_m": static_exact,
        "q_factor": 1.0 / (2.0 * damping_ratio),
        "damping_ratio": damping_ratio,
        "tip_node": tip_node,
        "n_steps": len(freqs),
    }


_VTU_ARRAY_RE = (r'Name="displacement HarmonicMode1"[^>]*format="ascii"[^>]*>'
                 r'(.*?)</DataArray>')


def parse_harmonic_beam(case_dir, *, mesh_name="beam", output="frf",
                        n_steps, tip_node) -> list | None:
    """Signed in-phase tip deflection u_y per sweep step, read from the ASCII
    .vtu files ResultOutput wrote into the mesh directory ('displacement
    HarmonicMode1' holds the real part, 3 components per node). Returns a list
    of floats (one per step), or None when files are missing."""
    out = []
    for i in range(1, n_steps + 1):
        path = os.path.join(case_dir, mesh_name, f"{output}_t{i:04d}.vtu")
        if not os.path.exists(path):
            return None
        m = re.search(_VTU_ARRAY_RE, open(path, encoding="utf-8").read(), re.S)
        if not m:
            return None
        vals = m.group(1).split()
        idx = 3 * tip_node + 1
        if idx >= len(vals):
            return None
        out.append(float(vals[idx]))
    return out


def locate_frf_resonance(freqs, tip_re) -> float | None:
    """f_n from the swept in-phase response: Re(H) crosses zero exactly at f_n,
    ~linearly in f² nearby — interpolate the sign-flip pair with the largest
    response magnitude (a sample landing exactly on zero IS the resonance).
    Returns the f_n estimate in Hz, or None."""
    for f, a in zip(freqs, tip_re):
        if a == 0.0:
            return f
    best, best_mag = None, 0.0
    for (f1, a1), (f2, a2) in zip(zip(freqs, tip_re), zip(freqs[1:], tip_re[1:])):
        if (a1 > 0) == (a2 > 0):
            continue
        mag = min(abs(a1), abs(a2))
        if mag > best_mag:
            best_mag = mag
            x1, x2 = f1 * f1, f2 * f2
            best = math.sqrt(x1 - a1 * (x2 - x1) / (a2 - a1))
    return best
