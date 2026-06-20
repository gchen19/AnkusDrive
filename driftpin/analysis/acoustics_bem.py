"""Exterior-acoustics closed-form oracles — the analytic anchors the Bempp BEM
exterior-Helmholtz path (``acoustic_radiation_submit``, run out-of-process on the
``bempp-cl`` engine via ``driftpin/bempp_runner.py``) is gated against.

Pure-Python, FreeCAD-free. Two two-sided toys, each the closed-form twin of a
boundary-element solve of the exterior Helmholtz equation (the pairing
``beam_modal`` ↔ ``fem_modal`` already has for the linear structural path):

* **Pulsating (monopole) sphere** (``monopole_sphere``) — a sphere of radius ``a``
  whose surface vibrates uniformly with normal velocity amplitude ``U`` radiates a
  spherically symmetric wave. The radiated sound power is EXACT,

      W = (ρc/2)·|U|²·(4πa²)·(ka)²/(1+(ka)²),

  and the far-field pressure magnitude is |p(r)| = ρc·|U|·(ka)/√(1+(ka)²)·(a/r).
  The radiation efficiency σ = (ka)²/(1+(ka)²) → 0 as ka→0 (a small source is a
  poor radiator) and → 1 as ka→∞ (large source radiates like a piston into 2π).
  A BEM solve of the same Neumann (velocity) problem must reproduce W and |p(r)|.

* **Rigid-sphere plane-wave scattering** (``rigid_sphere_scattering``) — a rigid
  (sound-hard) sphere insonified by a unit plane wave scatters a field whose
  far-field directivity is the **Mie series** far-field form function

      f∞(θ) = (2/ ika)·Σ_n (2n+1)·[−j'ₙ(ka)/h'ₙ(ka)]·Pₙ(cosθ),

  with the backscatter (θ=π) magnitude the classic gate. This is an EXACT modal
  sum (spherical Bessel jₙ / Hankel hₙ⁽¹⁾, Legendre Pₙ); the BEM scattered field
  on a sweep of ``ka`` and angle must land on |f∞(θ)| within tolerance.

Lengths metres (SI), frequencies through the wavenumber ``k`` (rad/m), velocities
m/s. ``monopole_sphere`` is exact; ``rigid_sphere_scattering`` is an exact modal
sum truncated past convergence (fidelity="exact"). Theory limits (single rigid/
vibrating sphere in a lossless unbounded fluid) are stated in each return's
``warnings`` / ``escalate_to``.
"""
from __future__ import annotations

import math

# Air at 20 °C, 1 atm (defaults; both are arguments).
RHO_AIR = 1.204          # kg/m³
C_AIR = 343.0            # m/s


# --- spherical Bessel / Hankel recurrences (real argument) --------------------
# jₙ(x), yₙ(x) and their derivatives by stable recurrence — enough for the Mie
# sum (no SciPy dependency, keeping the oracle import-light like the rest of the
# analysis package).

def _sph_jn(nmax: int, x: float) -> list[float]:
    """Spherical Bessel jₙ(x), n=0..nmax. Downward recurrence (Miller) normalised
    by the closed-form j₀ = sin x / x, which is stable for all x > 0."""
    if x == 0.0:
        return [1.0] + [0.0] * nmax
    start = nmax + int(round(math.sqrt(40.0 * nmax))) + 10
    jp1, j = 0.0, 1e-30
    unscaled = [0.0] * (nmax + 1)
    for n in range(start, -1, -1):
        jm1 = (2 * n + 3) / x * j - jp1
        jp1, j = j, jm1
        if n <= nmax:
            unscaled[n] = j
    j0 = math.sin(x) / x
    scale = j0 / j        # j here holds the (unnormalised) n=0 value
    return [v * scale for v in unscaled]


def _sph_yn(nmax: int, x: float) -> list[float]:
    """Spherical Bessel (Neumann) yₙ(x), n=0..nmax, by stable upward recurrence
    from the closed forms y₀ = −cos x / x, y₁ = −cos x / x² − sin x / x."""
    y = [0.0] * (nmax + 1)
    y0 = -math.cos(x) / x
    y[0] = y0
    if nmax >= 1:
        y1 = -math.cos(x) / x**2 - math.sin(x) / x
        y[1] = y1
        for n in range(1, nmax):
            y[n + 1] = (2 * n + 1) / x * y[n] - y[n - 1]
    return y


def _legendre(nmax: int, ct: float) -> list[float]:
    """Legendre polynomials Pₙ(cosθ), n=0..nmax, by the three-term recurrence."""
    p = [0.0] * (nmax + 1)
    p[0] = 1.0
    if nmax >= 1:
        p[1] = ct
        for n in range(1, nmax):
            p[n + 1] = ((2 * n + 1) * ct * p[n] - n * p[n - 1]) / (n + 1)
    return p


def _nmax_for(ka: float) -> int:
    """Mie truncation order: ka + a generous tail so the series is converged well
    past test tolerance (Wiscombe rule, padded)."""
    return max(8, int(math.ceil(ka + 4.0 * ka ** (1.0 / 3.0) + 12)))


# --- pulsating (monopole) sphere ----------------------------------------------

def monopole_sphere(
    a_m: float,
    freq_hz: float,
    u_amp: float = 1.0,
    r_m: float | None = None,
    rho: float = RHO_AIR,
    c: float = C_AIR,
) -> dict:
    """Radiated power and far-field pressure of a pulsating (monopole) sphere of
    radius ``a_m`` vibrating with uniform surface normal velocity amplitude
    ``u_amp`` at ``freq_hz`` (no solver) — the closed-form twin the Bempp exterior
    BEM radiation solve (``acoustic_radiation_submit``) is gated against.

    EXACT: with k = 2πf/c and ka the compactness, the time-averaged radiated sound
    power is W = (ρc/2)·|U|²·(4πa²)·(ka)²/(1+(ka)²) and the far-field pressure
    magnitude at range ``r_m`` (≥ a) is |p(r)| = ρc·|U|·(ka)/√(1+(ka)²)·(a/r). The
    radiation efficiency σ = (ka)²/(1+(ka)²) is the fraction of the piston-limit
    power; σ→0 as ka→0 (a sub-wavelength source radiates poorly) and σ→1 as ka→∞.

    The on-surface pressure phase leads the velocity (the sphere pushes a reactive
    mass of fluid); a BEM Neumann solve of the same uniform-velocity boundary must
    reproduce both W and |p(r)|. A non-spherical or baffled radiator is beyond this
    one-sphere closed form — escalate to ``acoustic_radiation_submit``.

    Returns {a_m, freq_hz, k_per_m, ka, u_amp, rho, c, radiation_efficiency,
    radiated_power_w, surface_pressure_abs, r_m, farfield_pressure_abs,
    farfield_pressure_x_r, fidelity, band_pct, valid_range_ok, warnings,
    escalate_to}."""
    if a_m <= 0:
        raise ValueError("a_m must be > 0")
    if freq_hz <= 0:
        raise ValueError("freq_hz must be > 0")
    if rho <= 0 or c <= 0:
        raise ValueError("rho and c must be > 0")
    if r_m is not None and r_m < a_m:
        raise ValueError("r_m must be ≥ a_m (field point outside the sphere)")

    k = 2.0 * math.pi * freq_hz / c
    ka = k * a_m
    sigma = ka * ka / (1.0 + ka * ka)               # radiation efficiency
    area = 4.0 * math.pi * a_m * a_m
    W = 0.5 * rho * c * abs(u_amp) ** 2 * area * sigma

    # surface pressure amplitude |p(a)| = ρc|U|·ka/√(1+(ka)²)  (the (a/r)=1 case)
    p_surf = rho * c * abs(u_amp) * ka / math.sqrt(1.0 + ka * ka)

    out = {
        "a_m": round(a_m, 9),
        "freq_hz": round(freq_hz, 6),
        "k_per_m": round(k, 9),
        "ka": round(ka, 9),
        "u_amp": round(u_amp, 9),
        "rho": round(rho, 6), "c": round(c, 6),
        "radiation_efficiency": round(sigma, 9),
        "radiated_power_w": W,
        "surface_pressure_abs": p_surf,
        "r_m": None,
        "farfield_pressure_abs": None,
        # the range-independent product |p|·r — the BEM far field is matched on this
        "farfield_pressure_x_r": round(p_surf * a_m, 9),
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": True,
        "warnings": [],
        "escalate_to": "acoustic_radiation_submit",
    }
    if r_m is not None:
        out["r_m"] = round(r_m, 9)
        out["farfield_pressure_abs"] = p_surf * (a_m / r_m)
    return out


# --- rigid-sphere plane-wave scattering (Mie series) --------------------------

def rigid_sphere_scattering(
    ka: float,
    theta_deg: float = 180.0,
    a_m: float | None = None,
) -> dict:
    """Far-field form function of a rigid (sound-hard) sphere scattering a unit
    plane wave (no solver) — the EXACT modal-sum twin the Bempp exterior scattering
    BEM solve (``acoustic_radiation_submit``, problem='scattering') is gated
    against. ``ka`` is the compactness; ``theta_deg`` the scattering angle measured
    from the forward (incidence) direction, so θ=180° is exact backscatter.

    The scattered far field is p_sc(r,θ) → (a/2r)·f∞(θ)·e^{ikr} with the Mie
    form function

        f∞(θ) = (2/ika)·Σ_n (2n+1)·[−j'ₙ(ka)/h'ₙ⁽¹⁾(ka)]·Pₙ(cosθ),

    a rigorous spherical-harmonic sum (Neumann BC ∂p/∂r=0 on the sphere). It is
    truncated past convergence, so fidelity="exact". The classic gate is the
    backscatter magnitude |f∞(π)|, which → 1 in the geometric (ka≫1) limit and
    rises through the Rayleigh/resonance region. A BEM scattered field sampled in
    the far zone must land on |f∞(θ)| within tolerance.

    Single rigid sphere in a lossless unbounded fluid; a penetrable, lossy, or
    multi-body scatterer is beyond this closed form — escalate to
    ``acoustic_radiation_submit``.

    Returns {ka, theta_deg, a_m, form_function_abs, form_function_re,
    form_function_im, backscatter_abs, n_terms, fidelity, band_pct,
    valid_range_ok, warnings, escalate_to}."""
    if ka <= 0:
        raise ValueError("ka must be > 0")
    if not (0.0 <= theta_deg <= 360.0):
        raise ValueError("theta_deg must be in [0, 360]")

    nmax = _nmax_for(ka)
    jn = _sph_jn(nmax + 1, ka)
    yn = _sph_yn(nmax + 1, ka)

    def _djn(n: int) -> float:
        # j'ₙ(x) = jₙ₋₁(x) − (n+1)/x · jₙ(x); j₋₁(x) = cos x / x
        jm1 = jn[n - 1] if n >= 1 else (math.cos(ka) / ka)
        return jm1 - (n + 1) / ka * jn[n]

    def _dyn(n: int) -> float:
        ym1 = yn[n - 1] if n >= 1 else (math.sin(ka) / ka)
        return ym1 - (n + 1) / ka * yn[n]

    ct = math.cos(math.radians(theta_deg))
    pn = _legendre(nmax, ct)

    fsum = 0.0 + 0.0j
    for n in range(nmax + 1):
        djn = _djn(n)
        dhn = _djn(n) + 1j * _dyn(n)                 # h'ₙ⁽¹⁾ = j'ₙ + i·y'ₙ
        coeff = -djn / dhn
        fsum += (2 * n + 1) * coeff * pn[n]
    f_inf = (2.0 / (1j * ka)) * fsum

    # backscatter (θ=π) magnitude — the canonical gate
    pn_back = _legendre(nmax, -1.0)
    bsum = 0.0 + 0.0j
    for n in range(nmax + 1):
        djn = _djn(n)
        dhn = _djn(n) + 1j * _dyn(n)
        bsum += (2 * n + 1) * (-djn / dhn) * pn_back[n]
    f_back = (2.0 / (1j * ka)) * bsum

    return {
        "ka": round(ka, 9),
        "theta_deg": round(theta_deg, 6),
        "a_m": (round(a_m, 9) if a_m is not None else None),
        "form_function_abs": round(abs(f_inf), 9),
        "form_function_re": round(f_inf.real, 9),
        "form_function_im": round(f_inf.imag, 9),
        "backscatter_abs": round(abs(f_back), 9),
        "n_terms": nmax + 1,
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": True,
        "warnings": [],
        "escalate_to": "acoustic_radiation_submit",
    }


# convenience: complex spherical Hankel h_n^(1) for callers that want it
def _sph_hn1(nmax: int, x: float) -> list[complex]:
    jn = _sph_jn(nmax, x)
    yn = _sph_yn(nmax, x)
    return [complex(jn[n], yn[n]) for n in range(nmax + 1)]


__all__ = ["monopole_sphere", "rigid_sphere_scattering", "RHO_AIR", "C_AIR"]
