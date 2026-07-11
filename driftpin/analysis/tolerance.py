"""Tolerance & GD&T — dimension stack-ups, fit classes, ISO 286 limits.

Pure-Python, FreeCAD-free. The first family in DriftPin's simulation roadmap
(``docs/SIMULATION_TOOLS.md`` family 1 / Appendix A); it stands up the
``driftpin/analysis/`` pattern every other pure-Python family reuses. No geometry
read in v1 — every call takes explicit dimensions, so the toys run in milliseconds
without spawning ``freecadcmd``.

Public calls back the ``tolerance_stackup`` / ``fit_check`` / ``fit_class`` /
``gdt_check`` tools:

    stackup(chain, method, samples, spec_min, spec_max) -> resultant dict
    fit_check(hole, shaft)                              -> fit-class dict
    fit_class(basic_size, fit)                          -> ISO 286 limits dict
    gdt_check(control, zone, actual, ...)               -> {pass, actual, margin}

**Sign convention (shipped — stated so the toys test what we ship).** A dimension
is ``{name, nominal, plus, minus}`` where ``plus`` is the *upper* deviation and
``minus`` the *lower* deviation, **both signed**, with ``plus >= minus``. A
symmetric ``10 ± 0.1`` is ``plus=+0.1, minus=-0.1``; shorthand ``{nominal, tol}``
expands to that. This matches ISO 286 (a shaft g6 is ``plus=-0.007, minus=-0.020``)
and ``fit_check``'s hole/shaft inputs — one convention across the family. A stack
link may carry ``direction`` (+1 default, -1 for a gap/subtractive feature).

The half-band of a link (``(plus-minus)/2``) is taken as **3σ** for the RSS and
Monte-Carlo blocks. See ``docs/SIMULATION_EXAMPLES.md`` §1 for the worked toys.
"""
from __future__ import annotations

import math
import random
import statistics


# --- dimension parsing --------------------------------------------------------

def _devs(dim: dict) -> tuple[float, float, float, int]:
    """Normalise a dimension dict to (nominal, plus, minus, direction).

    Accepts ``{nominal, plus, minus}`` (signed deviations, plus>=minus) or the
    symmetric shorthand ``{nominal, tol}``. ``direction`` defaults to +1."""
    nominal = float(dim["nominal"])
    if "tol" in dim and "plus" not in dim and "minus" not in dim:
        t = abs(float(dim["tol"]))
        plus, minus = t, -t
    else:
        plus = float(dim.get("plus", 0.0))
        minus = float(dim.get("minus", 0.0))
    if plus < minus:
        raise ValueError(
            f"plus ({plus}) must be >= minus ({minus}) — deviations are signed "
            f"(upper, lower); for ±{plus} pass plus=+{plus}, minus=-{plus}"
        )
    direction = int(dim.get("direction", 1))
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction}")
    return nominal, plus, minus, direction


def _interval(dim: dict) -> tuple[float, float, float]:
    """Return a dimension's (low, high, mean) contribution, sign-corrected for
    ``direction`` so subtractive links flip their bounds."""
    nominal, plus, minus, d = _devs(dim)
    low, high = nominal + minus, nominal + plus
    mean = nominal + 0.5 * (plus + minus)
    if d == -1:
        low, high = -high, -low
        mean = -mean
    return low, high, mean


# --- 1. dimension stack-up ----------------------------------------------------

def stackup(
    chain: list[dict],
    method: str = "worstcase",
    samples: int = 10000,
    spec_min: float | None = None,
    spec_max: float | None = None,
    seed: int | None = 12345,
) -> dict:
    """Stack a dimension chain three ways.

    ``method`` selects the *deepest* block computed: ``worstcase`` (always),
    ``rss`` (adds the statistical 3σ band), or ``montecarlo`` (adds a sampled
    {mean, std, cpk, pct_in_spec}). Each ``chain`` entry is a signed-deviation
    dimension (see module docstring); a ``direction:-1`` link subtracts.

    Worst-case sums each link's extreme contribution. RSS treats every half-band
    as 3σ: σ = √Σ(half_band/3)². Cpk / pct_in_spec are evaluated against
    ``spec_min``/``spec_max`` when given, else against the worst-case bounds (so a
    bare call still reports a meaningful capability vs. the conservative envelope).

    Returns {nominal, units_note, worstcase:{min,max,spread}, rss:{sigma,min_3s,
    max_3s} (rss/mc only), montecarlo:{mean,std,cpk,pct_in_spec,spec} (mc only)}.
    Raises ValueError on an empty chain or an unknown method."""
    if not chain:
        raise ValueError("chain must contain at least one dimension")
    if method not in ("worstcase", "rss", "montecarlo"):
        raise ValueError(f"unknown method {method!r}: worstcase | rss | montecarlo")

    nominal = sum(_devs(d)[0] * _devs(d)[3] for d in chain)
    intervals = [_interval(d) for d in chain]
    wc_min = sum(lo for lo, _hi, _m in intervals)
    wc_max = sum(hi for _lo, hi, _m in intervals)

    out = {
        "nominal": round(nominal, 6),
        "units_note": "all dimensions share one length unit (mm by convention)",
        "worstcase": {
            "min": round(wc_min, 6),
            "max": round(wc_max, 6),
            "spread": round(wc_max - wc_min, 6),
        },
    }
    if method == "worstcase":
        return out

    mean = sum(m for _lo, _hi, m in intervals)
    sigmas = [((p - mn) / 2.0) / 3.0 for p, mn in
              ((_devs(d)[1], _devs(d)[2]) for d in chain)]
    sigma = math.sqrt(sum(s * s for s in sigmas))
    out["rss"] = {
        "sigma": round(sigma, 6),
        "min_3s": round(mean - 3.0 * sigma, 6),
        "max_3s": round(mean + 3.0 * sigma, 6),
    }
    if method == "rss":
        return out

    lsl = spec_min if spec_min is not None else wc_min
    usl = spec_max if spec_max is not None else wc_max
    rng = random.Random(seed)
    vals = []
    for _ in range(int(samples)):
        total = 0.0
        for d in chain:
            nom, p, mn, direction = _devs(d)
            s = ((p - mn) / 2.0) / 3.0
            center = nom + 0.5 * (p + mn)
            total += direction * rng.gauss(center, s) if s > 0 else direction * center
        vals.append(total)
    mc_mean = statistics.fmean(vals)
    mc_std = statistics.pstdev(vals, mc_mean) if len(vals) > 1 else 0.0
    in_spec = sum(1 for v in vals if lsl <= v <= usl)
    if mc_std > 0:
        cpk = min(usl - mc_mean, mc_mean - lsl) / (3.0 * mc_std)
    else:
        cpk = float("inf")
    out["montecarlo"] = {
        "mean": round(mc_mean, 6),
        "std": round(mc_std, 6),
        "cpk": (round(cpk, 3) if math.isfinite(cpk) else None),
        "pct_in_spec": round(100.0 * in_spec / len(vals), 3),
        "spec": {"min": round(lsl, 6), "max": round(usl, 6),
                 "source": "explicit" if spec_min is not None or spec_max is not None
                 else "worst-case bounds"},
    }
    return out


# --- 2. fit classification ----------------------------------------------------

def fit_check(hole: dict, shaft: dict) -> dict:
    """Classify a hole/shaft pair as clearance, transition, or interference.

    Both are signed-deviation dimensions (``{nominal, plus, minus}`` or
    ``{nominal, tol}``). Clearance = hole_min − shaft_max (positive = gap).

    - **clearance**: shaft always fits (min_clearance ≥ 0).
    - **interference**: shaft always larger (max_clearance ≤ 0).
    - **transition**: the range straddles zero (could go either way).

    ``prob_interference`` is the analytical fraction of assemblies that interfere,
    modelling each part as normal with its half-band = 3σ. Returns {fit_class,
    min_clearance, max_clearance, nominal_clearance, prob_interference}."""
    h_nom, h_plus, h_minus, _ = _devs(hole)
    s_nom, s_plus, s_minus, _ = _devs(shaft)
    hole_min, hole_max = h_nom + h_minus, h_nom + h_plus
    shaft_min, shaft_max = s_nom + s_minus, s_nom + s_plus

    min_clear = hole_min - shaft_max   # tightest fit
    max_clear = hole_max - shaft_min   # loosest fit
    nom_clear = (h_nom + 0.5 * (h_plus + h_minus)) - (s_nom + 0.5 * (s_plus + s_minus))

    if min_clear >= 0:
        fit = "clearance"
    elif max_clear <= 0:
        fit = "interference"
    else:
        fit = "transition"

    # P(interference) = P(clearance < 0), clearance ~ Normal(mean, var_hole+var_shaft)
    sig_h = ((h_plus - h_minus) / 2.0) / 3.0
    sig_s = ((s_plus - s_minus) / 2.0) / 3.0
    sig_c = math.sqrt(sig_h * sig_h + sig_s * sig_s)
    if sig_c > 0:
        z = nom_clear / sig_c
        prob_interf = 0.5 * math.erfc(z / math.sqrt(2.0))
    else:
        prob_interf = 1.0 if nom_clear < 0 else 0.0

    return {
        "fit_class": fit,
        "min_clearance": round(min_clear, 6),
        "max_clearance": round(max_clear, 6),
        "nominal_clearance": round(nom_clear, 6),
        "prob_interference": round(prob_interf, 6),
    }


# --- 3. ISO 286 limits --------------------------------------------------------

# Standard tolerance grades IT (µm) by nominal-size band. Band index follows
# _BANDS (upper bound, mm, inclusive). Source: ISO 286-1 Table 1.
_BANDS = (3, 6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500)
_IT = {
    4:  (3, 4, 4, 5, 6, 7, 8, 10, 12, 14, 16, 18, 20),
    5:  (4, 5, 6, 8, 9, 11, 13, 15, 18, 20, 23, 25, 27),
    6:  (6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36, 40),
    7:  (10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57, 63),
    8:  (14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89, 97),
    9:  (25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140, 155),
    10: (40, 48, 58, 70, 84, 100, 120, 140, 160, 185, 210, 230, 250),
    11: (60, 75, 90, 110, 130, 160, 190, 220, 250, 290, 320, 360, 400),
}

# Clearance-letter shafts (a..h): the fundamental deviation is the *upper*
# deviation es (µm), by _BANDS index. ei = es - IT. These letters sit on or
# below the zero line, so es is constant across each IT band.
# Source: ISO 286-1 Table 2 (curated clearance set).
_SHAFT_ES = {
    "h": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "g": (-2, -4, -5, -6, -7, -9, -10, -12, -14, -15, -17, -18, -20),
    "f": (-6, -10, -13, -16, -20, -25, -30, -36, -43, -50, -56, -62, -68),
    "e": (-14, -20, -25, -32, -40, -50, -60, -72, -85, -100, -110, -125, -135),
}

# Transition / interference-letter shafts (js, k, m, n, p, r, s): the
# fundamental deviation is the *lower* deviation ei (µm); es = ei + IT. Above
# ~50 mm the ei of r and s subdivides finer than the IT bands, so ei is
# tabulated on the finer _FINE_BANDS grid below (k..p simply repeat their value
# across the sub-segments). Source: ISO 286-1 Table 2 / ISO 286-2 shaft limits.
#
# Note on k: the tabulated ei holds for grades IT4–IT7 (the usual interference
# pairings, incl. the k6 goldens); ISO sets k's ei to 0 outside that range. We
# apply that rule in :func:`_shaft_ei`. All other letters here are
# grade-independent.
_FINE_BANDS = (3, 6, 10, 14, 18, 24, 30, 40, 50, 65, 80, 100, 120,
               140, 160, 180, 200, 225, 250, 280, 315, 355, 400, 450, 500)
_SHAFT_EI = {
    "js": None,  # symmetric: ei = -IT/2, es = +IT/2 (handled specially)
    "k":  (0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4, 5, 5),
    "m":  (2, 4, 6, 7, 7, 8, 8, 9, 9, 11, 11, 13, 13, 15, 15, 15, 17, 17, 17, 20, 20, 21, 21, 23, 23),
    "n":  (4, 8, 10, 12, 12, 15, 15, 17, 17, 20, 20, 23, 23, 27, 27, 27, 31, 31, 31, 34, 34, 37, 37, 40, 40),
    "p":  (6, 12, 15, 18, 18, 22, 22, 26, 26, 32, 32, 37, 37, 43, 43, 43, 50, 50, 50, 56, 56, 62, 62, 68, 68),
    "r":  (10, 15, 19, 23, 23, 28, 28, 34, 34, 41, 43, 51, 54, 63, 65, 68, 77, 80, 84, 94, 98, 108, 114, 126, 132),
    "s":  (14, 19, 23, 28, 28, 35, 35, 43, 43, 53, 59, 71, 79, 92, 100, 108, 122, 130, 140, 158, 170, 190, 208, 232, 252),
}
_CLEAR_LETTERS = ", ".join(f"{c}<grade>" for c in _SHAFT_ES)
_INTF_LETTERS = ", ".join(f"{c}<grade>" for c in _SHAFT_EI)
_SUPPORTED = (
    f"hole-basis H only; shaft clearance letters {_CLEAR_LETTERS}; "
    f"shaft transition/interference letters {_INTF_LETTERS}"
)


def _band_index(basic_size: float) -> int:
    if basic_size <= 0:
        raise ValueError("basic_size must be > 0 mm")
    for i, hi in enumerate(_BANDS):
        if basic_size <= hi:
            return i
    raise ValueError(f"basic_size {basic_size} mm exceeds ISO 286 table (>500 mm)")


def _fine_band_index(basic_size: float) -> int:
    if basic_size <= 0:
        raise ValueError("basic_size must be > 0 mm")
    for i, hi in enumerate(_FINE_BANDS):
        if basic_size <= hi:
            return i
    raise ValueError(f"basic_size {basic_size} mm exceeds ISO 286 table (>500 mm)")


def _shaft_ei(letter: str, grade: int, basic_size: float) -> float:
    """Lower deviation ei (mm) for a transition/interference shaft letter.

    ``js`` is symmetric (ei = -IT/2). ``k``'s tabulated ei applies for IT4–IT7;
    ISO 286-1 sets it to 0 for grades outside that range."""
    idx = _band_index(basic_size)
    if letter == "js":
        return -_it(grade, idx) / 2000.0
    fine = _fine_band_index(basic_size)
    ei_um = _SHAFT_EI[letter][fine]
    if letter == "k" and not (4 <= grade <= 7):
        ei_um = 0
    return ei_um / 1000.0


def _it(grade: int, idx: int) -> int:
    if grade not in _IT:
        raise ValueError(f"IT grade {grade} not tabulated (v1 covers IT4–IT11)")
    return _IT[grade][idx]


def _parse_code(code: str) -> tuple[str, int]:
    letter = "".join(c for c in code if c.isalpha())
    digits = "".join(c for c in code if c.isdigit())
    if not letter or not digits:
        raise ValueError(f"bad ISO 286 code {code!r} (want e.g. 'H7' or 'g6')")
    return letter, int(digits)


def fit_class(basic_size: float, fit: str = "H7/g6") -> dict:
    """ISO 286 limits for a hole/shaft fit code (e.g. ``'H7/g6'``), in mm.

    **Hole-basis H** (the dominant convention — a reamer/standard tool defines
    the hole, and the shaft letter selects the fit). Supported shaft letters:

    - clearance: ``h, g, f, e`` (line-to-line to loose),
    - transition: ``js, k, m, n`` (may clear or interfere),
    - interference: ``p, r, s`` (shaft always larger — a press/shrink fit).

    Deviations come from the ISO 286-1 IT-grade + fundamental-deviation tables
    (ei tabulated on the finer size grid used for r/s), then run through
    :func:`fit_check`. For a transition or interference result the ``clearance``
    band goes negative; the returned ``interference`` block restates it as an
    interference band and hands off to :func:`press_fit_stress` (feed
    ``interference_mm`` from that band to size contact pressure / torque).

    Returns {basic_size, fit, hole:{upper_dev,lower_dev,min,max}, shaft:{...},
    ...fit_check fields, interference:{...} (transition/interference only)}.
    Non-H holes and unknown letters raise NotImplementedError naming the
    supported set; out-of-table sizes raise ValueError."""
    idx = _band_index(basic_size)
    try:
        hole_code, shaft_code = fit.split("/")
    except ValueError:
        raise ValueError(f"fit must be 'HOLE/shaft' e.g. 'H7/g6', got {fit!r}")
    h_letter, h_grade = _parse_code(hole_code)
    s_letter, s_grade = _parse_code(shaft_code)

    if h_letter != "H":
        raise NotImplementedError(
            f"hole basis must be H, got hole {h_letter!r}. ({_SUPPORTED}) — "
            f"shaft-basis systems (hole letters K, N, P, S…) need the ISO 286 "
            f"delta rule and are out of scope; specify the equivalent H-basis fit."
        )
    hole_es = _it(h_grade, idx) / 1000.0   # +IT
    hole_ei = 0.0

    s_letter_l = s_letter.lower()
    if s_letter_l in _SHAFT_ES:              # clearance letter: es is fundamental
        shaft_upper = _SHAFT_ES[s_letter_l][idx] / 1000.0
        shaft_lower = shaft_upper - _it(s_grade, idx) / 1000.0
    elif s_letter_l in _SHAFT_EI:            # transition/interference: ei is fundamental
        shaft_lower = _shaft_ei(s_letter_l, s_grade, basic_size)
        shaft_upper = shaft_lower + _it(s_grade, idx) / 1000.0
    else:
        raise NotImplementedError(
            f"shaft letter {s_letter!r} not supported. ({_SUPPORTED})"
        )

    hole = {"nominal": basic_size, "plus": hole_es, "minus": hole_ei}
    shaft = {"nominal": basic_size, "plus": shaft_upper, "minus": shaft_lower}
    result = fit_check(hole, shaft)
    result.update({
        "basic_size": basic_size,
        "fit": fit,
        "hole": {"upper_dev": round(hole_es, 6), "lower_dev": round(hole_ei, 6),
                 "min": round(basic_size + hole_ei, 6),
                 "max": round(basic_size + hole_es, 6)},
        "shaft": {"upper_dev": round(shaft_upper, 6), "lower_dev": round(shaft_lower, 6),
                  "min": round(basic_size + shaft_lower, 6),
                  "max": round(basic_size + shaft_upper, 6)},
    })
    if result["fit_class"] in ("transition", "interference"):
        # interference = -clearance. Tightest fit (min clearance) is the largest
        # interference; loosest fit (max clearance) the smallest. A transition
        # fit's min_interference is negative (some assemblies clear).
        min_intf = -result["max_clearance"]
        max_intf = -result["min_clearance"]
        result["interference"] = {
            "min_mm": round(min_intf, 6),
            "max_mm": round(max_intf, 6),
            "next": "press_fit_stress",
            "hint": (
                "diametral interference band for press_fit_stress(interference_mm=…); "
                "use max(0, min_mm) as the lower bound for a guaranteed press fit"
            ),
        }
    return result


# --- 3b. ISO 2768-1 general tolerances (linear dimensions) ---------------------

# Permissible deviations (± mm) for linear dimensions WITHOUT an individual
# tolerance indication — the "ISO 2768-<class>" drawing-note default. ISO 2768-1
# Table 1, by class (f fine / m medium / c coarse / v very coarse) and nominal
# band (upper bound, mm, inclusive; the first band starts at 0.5 mm — below that
# the standard requires an individual indication). None marks a band the class
# does not tabulate.
_ISO2768_BANDS = (3, 6, 30, 120, 400, 1000, 2000, 4000)
_ISO2768 = {
    "f": (0.05, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, None),
    "m": (0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0),
    "c": (0.2, 0.3, 0.5, 0.8, 1.2, 2.0, 3.0, 4.0),
    "v": (None, 0.5, 1.0, 1.5, 2.5, 4.0, 6.0, 8.0),
}


def general_tolerance_mm(nominal: float, cls: str = "m") -> float:
    """ISO 2768-1 general tolerance (the ± half-band, mm) for an untoleranced
    linear dimension of ``nominal`` mm, tolerance class ``cls`` ('f' fine, 'm'
    medium, 'c' coarse, 'v' very coarse). This is what a "ISO 2768-m" drawing
    note assigns to every dimension without its own tolerance — and what the
    handle-derived stackup chain (issue #175 v2 Shape wiring) uses when the
    caller gives no explicit tolerance. Raises ValueError below 0.5 mm (the
    standard requires an individual indication), above 4000 mm, for an unknown
    class, or where the class has no tabulated value."""
    cls = str(cls).lower()
    if cls not in _ISO2768:
        raise ValueError(f"unknown ISO 2768 class {cls!r}: f | m | c | v")
    nominal = abs(float(nominal))
    if nominal < 0.5:
        raise ValueError(
            "ISO 2768 does not cover dimensions under 0.5 mm — tolerance them "
            "individually (pass an explicit default_tol)")
    for i, hi in enumerate(_ISO2768_BANDS):
        if nominal <= hi:
            t = _ISO2768[cls][i]
            if t is None:
                raise ValueError(
                    f"ISO 2768-{cls} tabulates no value for {nominal} mm — use "
                    "another class or an explicit default_tol")
            return t
    raise ValueError("ISO 2768 covers nominals up to 4000 mm")


# --- 4. GD&T zone check -------------------------------------------------------

# Controls whose 'actual' is a single measured deviation checked against the zone.
_FORM_CONTROLS = {
    "flatness", "straightness", "circularity", "cylindricity",
    "perpendicularity", "parallelism", "angularity", "concentricity",
    "runout", "total_runout", "profile_line", "profile_surface", "position",
}


def gdt_check(
    control: str,
    zone: float,
    actual: float | None = None,
    offset: dict | None = None,
    mmc_bonus: float = 0.0,
    datum_refs: list | None = None,
) -> dict:
    """Check a measured feature against a GD&T tolerance zone.

    ``actual`` is the measured deviation in the zone's units (mm). For
    ``position`` you may instead pass ``offset={'x':..,'y':..}`` and the diametral
    deviation 2·√(x²+y²) is used. ``mmc_bonus`` adds material-condition bonus
    tolerance to the zone. ``datum_refs`` is recorded but not resolved in v1.

    Returns {control, zone, effective_zone, actual, margin, pass, datum_refs}.
    Raises ValueError on an unknown control or missing measurement."""
    if control not in _FORM_CONTROLS:
        raise ValueError(
            f"unknown control {control!r}; supported: {sorted(_FORM_CONTROLS)}"
        )
    if actual is None:
        if control == "position" and offset is not None:
            actual = 2.0 * math.hypot(float(offset.get("x", 0.0)),
                                      float(offset.get("y", 0.0)))
        else:
            raise ValueError("provide actual (or offset={x,y} for position)")
    if actual < 0:
        raise ValueError("actual deviation must be >= 0")

    effective = zone + max(0.0, mmc_bonus)
    margin = effective - actual
    return {
        "control": control,
        "zone": round(zone, 6),
        "effective_zone": round(effective, 6),
        "actual": round(actual, 6),
        "margin": round(margin, 6),
        "pass": margin >= 0,
        "datum_refs": datum_refs or [],
    }
