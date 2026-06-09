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
