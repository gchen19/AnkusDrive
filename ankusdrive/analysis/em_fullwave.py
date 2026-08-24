"""Full-wave EM closed-form oracles — the analytic anchors the openEMS FDTD
full-wave path (``em_fullwave_submit``, run out-of-process on the GPL-3.0
openEMS engine via ``ankusdrive/em_fullwave_gpl_runner.py``) is gated against.

Pure-Python, FreeCAD-free. Two two-sided toys, each the closed-form twin of an
openEMS solve (the pairing ``beam_modal`` ↔ ``fem_modal`` already has for the
linear structural path):

* **Rectangular-waveguide cutoff** (``waveguide_cutoff``) — a hollow metal
  rectangular guide (broad wall ``a`` ≥ narrow wall ``b``) propagates a TEₘₙ/TMₘₙ
  mode only above its cutoff frequency

      f_c(m,n) = (c / 2√εᵣ) · √((m/a)² + (n/b)²).

  The dominant mode is **TE₁₀**, whose cutoff **f_c = c / (2a√εᵣ)** is EXACT —
  this is the primary FDTD gate. Below f_c the guide is *evanescent* (the axial
  propagation constant β = √(k² − k_c²) is imaginary, the field decays, nothing
  transmits); above f_c it propagates with guided wavelength λ_g = 2π/β > λ₀.
  An FDTD drive that straddles f_c must show the transmission collapse to zero
  below the analytic cutoff and rise to a plateau above it.

* **Half-wave dipole resonance** (``dipole_resonance``) — a thin centre-fed
  dipole is first (series) resonant when its physical length is slightly under a
  half wavelength, L ≈ k·λ with the end-effect shortening factor k ≈ 0.48
  (≈0.475 for typical wire diameters; thinner wires → closer to 0.5). Inverted,
  the resonant frequency of a length-L dipole is f_r = k·c / L. This is a banded
  correlation (the factor depends on the length/diameter ratio), so
  ``fidelity="banded"`` with a stated ``band_pct``; an FDTD S₁₁ sweep must put
  its first resonance (the |S₁₁| null / reactance zero crossing) inside the band.

Lengths metres (SI), frequencies Hz. ``waveguide_cutoff`` is exact;
``dipole_resonance`` is a ±band correlation. Theory limits (single-mode hollow
guide; thin-wire dipole) are stated in each return's ``warnings`` /
``escalate_to``.
"""
from __future__ import annotations

import math

# Speed of light in vacuum (CODATA, exact by SI definition), m/s.
C0 = 299_792_458.0


# --- rectangular-waveguide cutoff ---------------------------------------------

def waveguide_cutoff(
    a_mm: float,
    b_mm: float | None = None,
    mode: str = "TE10",
    eps_r: float = 1.0,
    freq_ghz: float | None = None,
) -> dict:
    """Exact cutoff frequency of a rectangular-waveguide mode (no solver) — the
    closed-form twin the openEMS FDTD waveguide solve is gated against. Broad
    wall ``a_mm`` (the larger transverse dimension), narrow wall ``b_mm``
    (default ``a_mm``/2, the WR convention). ``mode`` is ``TE<m><n>`` or
    ``TM<m><n>`` (TM needs m,n ≥ 1). Optional dielectric fill ``eps_r``.

    Cutoff f_c(m,n) = (c / 2√εᵣ)·√((m/a)² + (n/b)²); the dominant **TE10**
    reduces to the EXACT f_c = c / (2a√εᵣ). Below f_c the guide is evanescent
    (axial β imaginary, no propagation); when a probe ``freq_ghz`` is given its
    regime (propagating / evanescent), the free-space wavenumber k, the cutoff
    wavenumber k_c, the axial phase constant β = √(k² − k_c²) and the guided
    wavelength λ_g = 2π/β (∞ at and below cutoff) are returned. The FDTD solve
    must reproduce the propagating↔evanescent transition at f_c.

    A real single-mode guide carries TE10 alone between f_c(TE10) and the next
    mode's cutoff (TE20 at 2·f_c for a 2:1 aspect); above that the guide is
    multi-mode and this single-mode picture no longer holds — escalate to a
    multi-mode openEMS solve (``em_fullwave_submit``).

    Returns {mode, m, n, a_mm, b_mm, eps_r, cutoff_hz, cutoff_ghz, kc_per_m,
    next_mode_cutoff_ghz, single_mode_band_ghz, probe_freq_ghz, regime, k_per_m,
    beta_per_m, guided_wavelength_mm, fidelity, band_pct, valid_range_ok,
    warnings, escalate_to}."""
    if a_mm <= 0:
        raise ValueError("a_mm must be > 0")
    b_mm = a_mm / 2.0 if b_mm is None else float(b_mm)
    if b_mm <= 0:
        raise ValueError("b_mm must be > 0")
    if eps_r <= 0:
        raise ValueError("eps_r must be > 0")

    fam = mode[:2].upper()
    if fam not in ("TE", "TM"):
        raise ValueError("mode must be 'TE<m><n>' or 'TM<m><n>'")
    try:
        m, n = int(mode[2]), int(mode[3])
    except (IndexError, ValueError):
        raise ValueError(f"could not parse mode indices from {mode!r} "
                         "(expected e.g. 'TE10', 'TE11', 'TM11')")
    if fam == "TM" and (m < 1 or n < 1):
        raise ValueError("TM modes require m ≥ 1 and n ≥ 1")
    if m == 0 and n == 0:
        raise ValueError("TE00 is not a propagating mode")

    a, b = a_mm * 1e-3, b_mm * 1e-3                  # mm → m
    c_eff = C0 / math.sqrt(eps_r)
    # cutoff wavenumber and frequency
    kc = math.pi * math.sqrt((m / a) ** 2 + (n / b) ** 2)
    fc = c_eff / 2.0 * math.sqrt((m / a) ** 2 + (n / b) ** 2)

    # next-higher cutoff among the low-order modes (for the single-mode band).
    # TE10/TE01/TE20/TE11 cover the usual ordering for a ≥ b.
    others = []
    for (mm, nn) in ((1, 0), (0, 1), (2, 0), (1, 1)):
        if (mm, nn) == (m, n):
            continue
        f = c_eff / 2.0 * math.sqrt((mm / a) ** 2 + (nn / b) ** 2)
        if f > fc * (1.0 + 1e-9):
            others.append(f)
    next_cut = min(others) if others else None

    warnings: list[str] = []
    if a_mm < b_mm:
        warnings.append(
            f"a_mm {a_mm:g} < b_mm {b_mm:g} — by convention a is the BROAD wall; "
            "the dominant-mode labelling assumes a ≥ b")

    out = {
        "mode": fam + str(m) + str(n),
        "m": m, "n": n,
        "a_mm": round(a_mm, 6), "b_mm": round(b_mm, 6), "eps_r": round(eps_r, 6),
        "cutoff_hz": round(fc, 4),
        "cutoff_ghz": round(fc / 1e9, 9),
        "kc_per_m": round(kc, 6),
        "next_mode_cutoff_ghz": (round(next_cut / 1e9, 9) if next_cut else None),
        "single_mode_band_ghz": ([round(fc / 1e9, 9), round(next_cut / 1e9, 9)]
                                 if (next_cut and (m, n) == (1, 0)) else None),
        "probe_freq_ghz": None,
        "regime": None,
        "k_per_m": None,
        "beta_per_m": None,
        "guided_wavelength_mm": None,
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "em_fullwave_submit",
    }

    if freq_ghz is not None:
        if freq_ghz <= 0:
            raise ValueError("freq_ghz must be > 0")
        f = freq_ghz * 1e9
        k = 2.0 * math.pi * f / c_eff               # free-space (filled) wavenumber
        out["probe_freq_ghz"] = round(freq_ghz, 9)
        out["k_per_m"] = round(k, 6)
        if f > fc:                                   # propagating: β real, λ_g finite
            beta = math.sqrt(k * k - kc * kc)
            out["regime"] = "propagating"
            out["beta_per_m"] = round(beta, 6)
            out["guided_wavelength_mm"] = round(2.0 * math.pi / beta * 1e3, 6)
        else:                                        # evanescent: β imaginary, decays
            out["regime"] = "evanescent"
            out["beta_per_m"] = 0.0                  # no propagation; α = √(kc²−k²)
            out["guided_wavelength_mm"] = None
    return out


# --- half-wave dipole resonance ------------------------------------------------

def dipole_resonance(
    length_mm: float | None = None,
    freq_ghz: float | None = None,
    shortening: float = 0.48,
) -> dict:
    """Thin centre-fed half-wave dipole first (series) resonance (no solver) — the
    banded twin the openEMS S₁₁ antenna sweep is gated against. Give EITHER a
    physical ``length_mm`` (→ resonant frequency) OR a target ``freq_ghz`` (→ the
    resonant length). The end-effect shortening factor ``shortening`` k makes the
    resonant length a little under λ/2: L = k·λ, f_r = k·c/L. k ≈ 0.48 is the
    classic textbook value (≈0.475 for typical wire diameters; thinner → 0.5).

    Because k depends on the length/diameter ratio, this is a ±band correlation,
    not an exact anchor: ``fidelity="banded"`` with ``band_pct`` covering the
    realistic k ∈ [0.46, 0.49] spread (~±3%). An FDTD S₁₁ sweep must land its
    first resonance (|S₁₁| null / input-reactance zero crossing) inside
    [freq_lo, freq_hi]. A full pattern, gain, or matched-feed design is beyond
    this one-number estimate — escalate to ``em_fullwave_submit``.

    Returns {given, shortening, half_wavelength_mm, resonant_length_mm,
    resonant_freq_ghz, freq_lo_ghz, freq_hi_ghz, length_lo_mm, length_hi_mm,
    fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    if (length_mm is None) == (freq_ghz is None):
        raise ValueError("give exactly one of length_mm or freq_ghz")
    if not (0.40 < shortening < 0.52):
        raise ValueError("shortening must be in (0.40, 0.52) — physical end-effect")

    k_lo, k_hi = 0.46, 0.49                          # realistic wire-diameter spread
    band_pct = round((k_hi - k_lo) / (2.0 * shortening) * 100.0, 4)
    warnings: list[str] = []

    if length_mm is not None:
        if length_mm <= 0:
            raise ValueError("length_mm must be > 0")
        L = length_mm * 1e-3
        fr = shortening * C0 / L                     # f_r = k·c / L
        f_lo = k_lo * C0 / L                         # shorter k → lower resonance
        f_hi = k_hi * C0 / L
        half_lambda = (C0 / fr) / 2.0 * 1e3
        out_freq = fr
        out = {
            "given": "length",
            "resonant_length_mm": round(length_mm, 6),
            "resonant_freq_ghz": round(out_freq / 1e9, 9),
            "freq_lo_ghz": round(f_lo / 1e9, 9),
            "freq_hi_ghz": round(f_hi / 1e9, 9),
            "length_lo_mm": None,
            "length_hi_mm": None,
        }
    else:
        if freq_ghz <= 0:
            raise ValueError("freq_ghz must be > 0")
        f = freq_ghz * 1e9
        lam = C0 / f
        L = shortening * lam                         # resonant length
        L_lo = k_lo * lam
        L_hi = k_hi * lam
        half_lambda = lam / 2.0 * 1e3
        out_freq = f
        out = {
            "given": "freq",
            "resonant_length_mm": round(L * 1e3, 6),
            "resonant_freq_ghz": round(out_freq / 1e9, 9),
            "freq_lo_ghz": None,
            "freq_hi_ghz": None,
            "length_lo_mm": round(L_lo * 1e3, 6),
            "length_hi_mm": round(L_hi * 1e3, 6),
        }

    out.update({
        "shortening": round(shortening, 6),
        "half_wavelength_mm": round(half_lambda, 6),
        "fidelity": "banded",
        "band_pct": band_pct,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "em_fullwave_submit",
    })
    return out
