"""Geometric optics — the exact closed-form gate for the Optics family (§7).

Pure-Python, FreeCAD-free, stdlib-only (``math`` — no NumPy), so it runs on the
fast lane and inside FreeCAD's bundled interpreter alike. Snell's law, the Fresnel
power-reflectance equations, and total internal reflection are *exact* — the
unambiguous oracles the kickoff (``docs/SIMULATION_P3_KICKOFF.md``) names for
optics:

    Snell     n1·sinθ1 = n2·sinθ2     (30° into PMMA n=1.49062 → 19.60°)
    Fresnel   R₀ = ((n1−n2)/(n1+n2))²  at normal incidence (air/PMMA → 3.9%)
    TIR       θc = asin(n2/n1)         (PMMA→air → 42.16°; above it T = 0)

These back a deterministic, energy-conserving ray-bundle trace
(:func:`trace_bundle`) across a single dielectric interface: every ray's unit
energy is partitioned — exactly — into transmitted (``efficiency``), reflected +
totally-internally-reflected (``leakage``), and bulk-absorbed (``absorbed``), so
``leakage + efficiency + absorbed == 1`` to floating point. The full diffuser /
lens trace rides on the ``rayoptics`` wheel (the ``optics`` extra) behind
``optics_raytrace`` and is gated against this band where the physics is exact.

Angles are degrees, refractive indices dimensionless. Worked toys live in
``docs/SIMULATION_EXAMPLES.md`` §7.
"""
from __future__ import annotations

import math


def refract_angle(theta_i_deg: float, n1: float, n2: float):
    """Snell refraction angle (deg) for incidence ``theta_i_deg`` at an n1→n2
    interface. Returns the transmitted angle as a float, or None on total
    internal reflection (n1>n2 with the incidence above the critical angle)."""
    s = n1 * math.sin(math.radians(theta_i_deg)) / n2
    if abs(s) > 1.0:
        return None
    return math.degrees(math.asin(s))


def critical_angle(n1: float, n2: float):
    """Critical angle (deg) for total internal reflection at an n1→n2 interface,
    asin(n2/n1). Returns a float, or None when n1<=n2 (no TIR going into a denser
    or equal medium)."""
    if n1 <= n2:
        return None
    return math.degrees(math.asin(n2 / n1))


def fresnel_reflectance(theta_i_deg: float, n1: float, n2: float,
                        polarization: str = "unpolarized") -> dict:
    """Exact Fresnel power reflectance at an n1→n2 dielectric interface.

    ``polarization`` selects the returned ``reflectance``: 's', 'p', or
    'unpolarized' (the (r_s+r_p)/2 average). Above the critical angle (n1>n2) the
    wave is totally internally reflected — reflectance 1, transmittance 0, ``tir``
    True. At normal incidence r_s == r_p == ((n1−n2)/(n1+n2))².

    Returns {r_s, r_p, reflectance, transmittance, tir}."""
    theta_i = math.radians(theta_i_deg)
    cos_i = math.cos(theta_i)
    sin_t = n1 * math.sin(theta_i) / n2
    if abs(sin_t) > 1.0:                              # TIR (either sign of incidence)
        return {"r_s": 1.0, "r_p": 1.0, "reflectance": 1.0,
                "transmittance": 0.0, "tir": True}
    cos_t = math.sqrt(1.0 - sin_t * sin_t)
    rs = ((n1 * cos_i - n2 * cos_t) / (n1 * cos_i + n2 * cos_t)) ** 2
    rp = ((n1 * cos_t - n2 * cos_i) / (n1 * cos_t + n2 * cos_i)) ** 2
    if polarization == "s":
        R = rs
    elif polarization == "p":
        R = rp
    elif polarization == "unpolarized":
        R = 0.5 * (rs + rp)
    else:
        raise ValueError(f"polarization must be 's'|'p'|'unpolarized', got {polarization!r}")
    return {"r_s": rs, "r_p": rp, "reflectance": R, "transmittance": 1.0 - R,
            "tir": False}


def sample_source(source_config: dict, n_rays: int):
    """Deterministic incidence-angle samples (deg) + per-ray weights summing to 1.

    No RNG (so a trace is reproducible and cacheable). ``source_config['kind']``:
      * ``collimated`` — every ray at ``angle_deg`` (default 0).
      * ``cone``       — ``n_rays`` rays evenly across [0, ``half_angle_deg``],
                         equal weight.
      * ``lambertian`` — same angular grid up to ``max_angle_deg`` (default 90),
                         weighted by cosθ (a Lambertian emitter).

    Returns (angles_deg, weights). Raises ValueError on an unknown kind or
    ``n_rays`` < 1."""
    if n_rays < 1:
        raise ValueError("n_rays must be >= 1")
    kind = source_config.get("kind", "collimated")
    if kind == "collimated":
        a = float(source_config.get("angle_deg", 0.0))
        return [a] * n_rays, [1.0 / n_rays] * n_rays
    a_max = float(source_config.get(
        "half_angle_deg", source_config.get("max_angle_deg", 90.0)))
    angles = [a_max * (k + 0.5) / n_rays for k in range(n_rays)]
    if kind == "cone":
        weights = [1.0 / n_rays] * n_rays
    elif kind == "lambertian":
        raw = [math.cos(math.radians(t)) for t in angles]
        total = sum(raw)
        weights = [r / total for r in raw]
    else:
        raise ValueError(f"unknown source kind {kind!r}")
    return angles, weights


def _histogram(samples, n_bins: int, lo: float = 0.0, hi: float = 90.0):
    """Weighted histogram of (angle, energy) ``samples`` into ``n_bins`` over
    [lo, hi]. Returns a list of {angle_deg (bin centre), intensity}."""
    width = (hi - lo) / n_bins
    bins = [0.0] * n_bins
    for angle, energy in samples:
        idx = int((angle - lo) / width) if width > 0 else 0
        idx = min(max(idx, 0), n_bins - 1)
        bins[idx] += energy
    return [{"angle_deg": round(lo + (i + 0.5) * width, 4),
             "intensity": round(b, 6)} for i, b in enumerate(bins)]


def trace_bundle(n1: float, n2: float, source_config: dict | None = None,
                 n_rays: int = 64, absorption: float = 0.0,
                 target_half_angle_deg: float | None = None,
                 n_bins: int = 18) -> dict:
    """Deterministic, energy-conserving trace of a ray bundle across one n1→n2
    dielectric interface — the pure-Python oracle behind ``optics_raytrace``.

    Each ray carries unit energy split, exactly, by Fresnel / TIR plus an optional
    bulk-absorption fraction ``absorption`` (0..1, a lumped Beer–Lambert loss
    before the interface). Per ray: ``absorbed`` = absorption; the remaining
    (1−absorption) hits the interface — totally reflected (→ leakage) above the
    critical angle, else split into transmitted (→ efficiency) and reflected
    (→ leakage). When ``target_half_angle_deg`` is set, only transmitted rays
    exiting within that cone count toward ``efficiency`` (off-target transmitted
    light becomes leakage). By construction ``efficiency + leakage + absorbed`` ==
    1 to floating point (reported as ``energy_balance``).

    ``source_config`` is :func:`sample_source`'s (default collimated, normal
    incidence). Returns {n_rays, n1, n2, critical_angle_deg, efficiency,
    leakage_fraction, absorbed_fraction, tir_fraction, energy_balance,
    exit_distribution:[{angle_deg, intensity}], hotspot_locations:[{angle_deg,
    intensity}]}."""
    if not 0.0 <= absorption <= 1.0:
        raise ValueError("absorption must be in [0, 1]")
    source_config = source_config or {"kind": "collimated", "angle_deg": 0.0}
    angles, weights = sample_source(source_config, n_rays)
    theta_c = critical_angle(n1, n2)

    efficiency = leakage = absorbed = tir = 0.0
    exit_samples = []                                # (exit_angle_deg, energy)
    for theta_i, w in zip(angles, weights):
        absorbed += w * absorption
        remaining = w * (1.0 - absorption)
        fr = fresnel_reflectance(theta_i, n1, n2)
        if fr["tir"]:
            leakage += remaining                     # trapped — never exits
            tir += remaining
            continue
        transmitted = remaining * fr["transmittance"]
        leakage += remaining * fr["reflectance"]     # surface reflection
        theta_t = refract_angle(theta_i, n1, n2)
        exit_samples.append((theta_t, transmitted))
        if target_half_angle_deg is None or theta_t <= target_half_angle_deg:
            efficiency += transmitted
        else:
            leakage += transmitted                   # exits, but off the target

    dist = _histogram(exit_samples, n_bins)
    hot = sorted((b for b in dist if b["intensity"] > 0),
                 key=lambda b: b["intensity"], reverse=True)[:3]
    return {
        "n_rays": n_rays,
        "n1": n1,
        "n2": n2,
        "critical_angle_deg": (round(theta_c, 4) if theta_c is not None else None),
        "efficiency": round(efficiency, 6),
        "leakage_fraction": round(leakage, 6),
        "absorbed_fraction": round(absorbed, 6),
        "tir_fraction": round(tir, 6),
        "energy_balance": round(efficiency + leakage + absorbed, 6),
        "exit_distribution": dist,
        "hotspot_locations": hot,
    }
