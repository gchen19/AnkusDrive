"""Sequential lens-design analysis and optimization, backed by optiland (MIT).

FreeCAD-free and imported IN-PROCESS (optiland is permissively licensed, so unlike
the GPL non-sequential engine it needs no subprocess isolation — see
``driftpin/optics_gpl_runner.py`` for that boundary). Pure numpy underneath; never
touches a display (no ``.draw*()``). The worker handlers ``optics_lens_design`` and
``optics_lens_optimize`` are thin wrappers over ``analyze`` / ``optimize`` here.

A *system* is a JSON-friendly dict::

    {
      "surfaces": [                      # object->image, object plane implicit
        {"radius": 50.0, "thickness": 4.0, "material": "N-BK7", "stop": true},
        {"radius": -50.0, "thickness": 45.0, "material": "air"}
      ],
      "epd": 10.0,                       # entrance-pupil diameter (or "fno")
      "wavelengths_um": [0.5876],        # first is primary
      "field_angles_deg": [0.0],
      "image_solve": true                # solve last gap to paraxial focus
    }

optiland is gated against the analytic thick-lens oracle (lensmaker) so a wrong
build shows up as an EFL deviation, not a silent number.
"""
from __future__ import annotations

import math

# ---- analytic oracle (no optiland needed) ------------------------------------


def thick_lens_efl(n: float, r1: float, r2: float, t: float) -> float:
    """Effective focal length of a single thick lens in air (lensmaker's
    equation with the thickness term): 1/f = (n-1)[1/R1 - 1/R2 + (n-1)t/(n R1 R2)].
    Sign convention: R>0 if the center of curvature is to the right. Returns +inf
    for a null-power configuration."""
    power = (n - 1.0) * (1.0 / r1 - 1.0 / r2 + (n - 1.0) * t / (n * r1 * r2))
    return math.inf if abs(power) < 1e-15 else 1.0 / power


# ---- optiland builders -------------------------------------------------------


def _build(system: dict):
    """Construct an optiland ``Optic`` from a system dict. Imported lazily so the
    module stays import-cheap and degrades only at call time."""
    import numpy as np
    from optiland.optic import Optic

    surfaces = system.get("surfaces") or []
    if not surfaces:
        raise ValueError("system needs a non-empty 'surfaces' list")
    wls = system.get("wavelengths_um") or [0.5876]
    fields = system.get("field_angles_deg") or [0.0]

    lens = Optic()
    lens.add_surface(index=0, thickness=np.inf)          # object at infinity
    stop_seen = False
    for i, s in enumerate(surfaces, start=1):
        radius = float(s.get("radius", math.inf))
        thickness = float(s.get("thickness", 0.0))
        material = s.get("material", "air")
        is_stop = bool(s.get("stop", False))
        stop_seen = stop_seen or is_stop
        lens.add_surface(index=i, radius=radius, thickness=thickness,
                         material=material, is_stop=is_stop)
    if not stop_seen:                                    # default: first surface is the stop
        raise ValueError("exactly one surface must set 'stop': true")
    lens.add_surface(index=len(surfaces) + 1)            # image plane

    if "fno" in system and "epd" not in system:
        lens.set_aperture(aperture_type="imageFNO", value=float(system["fno"]))
    else:
        lens.set_aperture(aperture_type="EPD", value=float(system.get("epd", 10.0)))
    lens.set_field_type(field_type="angle")
    for fy in fields:
        lens.add_field(y=float(fy))
    for j, wl in enumerate(wls):
        lens.add_wavelength(value=float(wl), is_primary=(j == 0))
    if system.get("image_solve", True):
        lens.image_solve()                               # last gap -> paraxial focus
    return lens, wls, fields


def _rms_spot_um(lens, fields, wls) -> list:
    """RMS geometric spot radius (microns) per field, or [] if it cannot be
    computed (e.g. afocal). Uses optiland's SpotDiagram with batching disabled
    (the batched path errors in 0.6.0)."""
    import numpy as np
    from optiland.analysis import SpotDiagram
    try:
        sd = SpotDiagram(lens, fields=[(0.0, fy / max(fields)) if max(fields) else (0.0, 0.0)
                                       for fy in fields],
                         wavelengths=[wls[0]], num_rings=12)
        rms = np.ravel(np.array(sd.rms_spot_radius()))
        return [round(float(r) * 1000.0, 4) for r in rms]
    except Exception:
        return []


# ---- public API --------------------------------------------------------------


def analyze(system: dict, want_spot: bool = True) -> dict:
    """First-order + (optional) spot analysis of a sequential system.

    Returns {ok, backend:'optiland', optiland_version, efl_mm, bfl_mm, fno,
    rms_spot_um:[per field], oracle_efl_mm (single-lens only), oracle_dev_pct}."""
    import optiland
    lens, wls, fields = _build(system)
    par = lens.paraxial
    efl = float(par.f2())
    fno = float(par.FNO())
    try:
        bfl = float(par.BFL())
    except Exception:
        bfl = None

    out = {
        "ok": True,
        "backend": "optiland",
        "optiland_version": getattr(optiland, "__version__", "unknown"),
        "efl_mm": round(efl, 6),
        "bfl_mm": (round(bfl, 6) if bfl is not None else None),
        "fno": round(fno, 6),
        "n_surfaces": len(system.get("surfaces") or []),
    }
    if want_spot:
        out["rms_spot_um"] = _rms_spot_um(lens, fields, wls)

    # Oracle gate: only meaningful for a single thin/thick lens in air.
    surfaces = system.get("surfaces") or []
    if len(surfaces) == 2 and surfaces[0].get("material", "").lower() not in ("", "air"):
        try:
            n = float(lens.n(wls[0])[1]) if hasattr(lens.n(wls[0]), "__len__") else float(lens.n(wls[0]))
            oracle = thick_lens_efl(n, float(surfaces[0]["radius"]),
                                    float(surfaces[1]["radius"]), float(surfaces[0]["thickness"]))
            out["oracle_efl_mm"] = round(oracle, 6)
            out["oracle_dev_pct"] = round(100.0 * (efl - oracle) / oracle, 6)
        except Exception:
            pass
    return out


def optimize(system: dict, variables: list, targets: list,
             maxiter: int = 200) -> dict:
    """Optimize a sequential system. ``variables`` is a list of
    {type:'radius'|'thickness', surface:<int>} (surface index is 1-based into
    'surfaces'); ``targets`` is a list of {operand:'f2'|'rms_spot_size'|..., target,
    weight?, surface?}. Returns {ok, converged, n_fev, before, after, surfaces}
    with the optimized radii/thicknesses folded back into a system-style dict.

    optiland's optimization framework is the reason it wins the sequential lane
    over rayoptics (which has no optimizer) — see memory optics-library-selection."""
    from optiland.optimization import OptimizationProblem, OptimizerGeneric

    lens, wls, fields = _build(system)
    before = float(lens.paraxial.f2())

    prob = OptimizationProblem()
    try:
        prob.disable_batching()                          # batched operands error in 0.6.0
    except Exception:
        pass
    for t in targets:
        data = {"optic": lens}
        if "surface" in t:
            data["surface_number"] = int(t["surface"])
        prob.add_operand(operand_type=t.get("operand", "f2"),
                         target=float(t["target"]),
                         weight=float(t.get("weight", 1.0)),
                         input_data=data)
    for v in variables:
        prob.add_variable(lens, v.get("type", "radius"),
                          surface_number=int(v["surface"]))

    rss0 = float(prob.rss())
    res = OptimizerGeneric(prob).optimize(maxiter=maxiter, disp=False)
    rss1 = float(prob.rss())

    radii = [round(float(r), 6) for r in lens.surface_group.radii]
    out_surfaces = []
    for i, s in enumerate(system.get("surfaces") or [], start=1):
        out_surfaces.append({**s, "radius": radii[i]})
    return {
        "ok": True,
        "backend": "optiland",
        "converged": bool(getattr(res, "success", rss1 < rss0)),
        "n_fev": int(getattr(res, "nfev", 0) or 0),
        "before": {"efl_mm": round(before, 6), "rss": round(rss0, 9)},
        "after": {"efl_mm": round(float(lens.paraxial.f2()), 6), "rss": round(rss1, 9)},
        "surfaces": out_surfaces,
    }
