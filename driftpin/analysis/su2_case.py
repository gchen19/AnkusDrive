"""SU2 case builder — the native CFD path, no VM required (issue #237 item 3).

Pure-Python, FreeCAD-free. Writes a complete SU2 case (``mesh.su2`` + ``flow.cfg`` +
``inlet.dat``) for the plane-channel validation case, so ``solve_capabilities`` can
report the ``cfd`` family available on the strength of SU2 alone.

WHY THIS EXISTS
---------------
``docs/MACOS.md`` used to promise "CFD alone needs none of this — it degrades to
SU2". That was false: every built-in case mode of the CFD handlers emitted an
OpenFOAM dictionary tree, and SU2 was reachable only through a ``case_dir`` the
caller hand-prepared, which no DriftPin tool could produce. #237 item 2 made the
capability reporting honest about that (SU2 was marked ``prepared_case_only``); this
module is item 3, which makes the original promise true instead. On Apple Silicon it
matters most — OpenFOAM there lives in a Multipass VM, and SU2 is a native binary.

THE CASE: PLANE POISEUILLE
--------------------------
Steady laminar flow between parallel plates, gap ``h``, length ``L``, mean velocity
``U``. Fully developed, the pressure gradient is exact::

    dp = 12 * mu * U * L / h^2

the plane-channel twin of Hagen–Poiseuille, and the same *kind* of oracle: a closed
form with no empirical constant in it, so a solve either reproduces it or does not.

TWO THINGS THAT DECIDE WHETHER THIS CONVERGES, both learned the expensive way:

1. **The inlet is a parabolic profile, not a uniform slug.** With a uniform inlet the
   flow has to develop, and the entrance length is ``L_e ~ 0.05*Re*D_h`` — at Re 200
   in a 10 mm channel that is 0.2 m, so a 0.2 m channel is developing over its whole
   length and reads ~11 % high. Feeding the fully developed profile in
   (``u(y) = 6*U*(y/h)*(1 - y/h)``) makes the flow developed at x = 0, which both
   removes the entrance error and lets the domain be short (L/h = 10) instead of the
   L/h = 100+ needed to drown the entrance out. Short domain ⇒ fast convergence.

2. **Convergence is judged on the ANSWER, not on rms[p].** The pressure residual is
   absolute, and for a low-speed viscous case it stalls around -5 while the pressure
   drop is still climbing through 87 % of its final value — a run that looks converged
   and is not. ``CONV_FIELD= SURFACE_PRESSURE_DROP`` with a Cauchy window watches the
   quantity being reported. Same lesson the OpenFOAM pipe case learned about the
   wedge ``Uz`` residual (see docs/DESIGN_TO_SPEC_KICKOFF.md); it generalizes.

A viscous fluid is the default for the same conditioning reason: holding Re low with
water means a velocity of millimetres per second, where SU2's incompressible
pseudo-time is badly scaled and crawls. An oil at the same Reynolds number puts the
velocity at O(0.1 m/s) and converges in a few hundred iterations. The physics is
identical — plane Poiseuille is exact for any Newtonian fluid.
"""
from __future__ import annotations

import csv
import os

#: VTK type ids used in the .su2 mesh format.
_QUAD, _LINE = 9, 3


def plane_poiseuille_dp(height_m: float, length_m: float, velocity_m_s: float,
                        mu_pa_s: float) -> float:
    """Exact developed pressure drop between parallel plates: 12*mu*U*L/h^2 [Pa]."""
    if min(height_m, length_m, mu_pa_s) <= 0 or velocity_m_s <= 0:
        raise ValueError("height, length, velocity and viscosity must all be > 0")
    return 12.0 * mu_pa_s * velocity_m_s * length_m / (height_m ** 2)


def _structured_quad_mesh(length_m, height_m, nx, ny):
    """(points, quads, markers) for a rectangular structured grid. Markers are
    named inlet / outlet / wall_bot / wall_top, matching the config below."""
    idx, pts = {}, []
    for j in range(ny + 1):
        for i in range(nx + 1):
            idx[(i, j)] = len(pts)
            pts.append((length_m * i / nx, height_m * j / ny))
    quads = [(idx[(i, j)], idx[(i + 1, j)], idx[(i + 1, j + 1)], idx[(i, j + 1)])
             for j in range(ny) for i in range(nx)]
    markers = {
        "inlet": [(idx[(0, j)], idx[(0, j + 1)]) for j in range(ny)],
        "outlet": [(idx[(nx, j)], idx[(nx, j + 1)]) for j in range(ny)],
        "wall_bot": [(idx[(i, 0)], idx[(i + 1, 0)]) for i in range(nx)],
        "wall_top": [(idx[(i, ny)], idx[(i + 1, ny)]) for i in range(nx)],
    }
    return pts, quads, markers


def write_su2_mesh(path: str, pts, quads, markers) -> None:
    """Write a 2-D .su2 ASCII mesh (SU2's native format)."""
    out = ["NDIME= 2", f"NELEM= {len(quads)}"]
    out += [f"{_QUAD} {a} {b} {c} {d} {k}" for k, (a, b, c, d) in enumerate(quads)]
    out += [f"NPOIN= {len(pts)}"]
    out += [f"{x:.15g} {y:.15g} {k}" for k, (x, y) in enumerate(pts)]
    out += [f"NMARK= {len(markers)}"]
    for tag, pairs in markers.items():
        out += [f"MARKER_TAG= {tag}", f"MARKER_ELEMS= {len(pairs)}"]
        out += [f"{_LINE} {a} {b}" for a, b in pairs]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def write_inlet_profile(path: str, height_m: float, velocity_m_s: float,
                        ny: int) -> None:
    """The fully developed parabolic inlet, in SU2's inlet-profile format.

    Columns are fixed by SU2: COORD-X, COORD-Y, TEMPERATURE, VELOCITY (magnitude),
    NORMAL-X, NORMAL-Y. Temperature is ignored (the energy equation is off).
    ``u(y) = 6*U*(y/h)*(1 - y/h)`` integrates to the mean U and vanishes at both
    walls, so the inlet plane already satisfies the no-slip corner."""
    rows = []
    for j in range(ny + 1):
        y = height_m * j / ny
        eta = y / height_m
        u = 6.0 * velocity_m_s * eta * (1.0 - eta)
        rows.append(f"{0.0:.15e}\t{y:.15e}\t{0.0:.15e}\t{u:.15e}"
                    f"\t{1.0:.15e}\t{0.0:.15e}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"NMARK= 1\nMARKER_TAG= inlet\nNROW={ny + 1}\nNCOL=6\n"
                 "# COORD-X COORD-Y TEMPERATURE VELOCITY NORMAL-X NORMAL-Y\n"
                 + "\n".join(rows) + "\n")


def _config(rho, mu, velocity_m_s, max_iter, cauchy_eps):
    """The SU2 config. Incompressible laminar Navier–Stokes, convergence judged on
    the reported pressure drop rather than on rms[p] — see the module docstring."""
    return f"""% Plane-Poiseuille validation case — written by DriftPin (issue #237).
% Oracle: dp = 12*mu*U*L/h^2, exact for developed laminar flow between plates.
SOLVER= INC_NAVIER_STOKES
KIND_TURB_MODEL= NONE
MATH_PROBLEM= DIRECT
RESTART_SOL= NO

INC_DENSITY_MODEL= CONSTANT
INC_ENERGY_EQUATION= NO
INC_DENSITY_INIT= {rho:.9g}
INC_VELOCITY_INIT= ( {velocity_m_s:.9g}, 0.0, 0.0 )
VISCOSITY_MODEL= CONSTANT_VISCOSITY
MU_CONSTANT= {mu:.9g}

MARKER_HEATFLUX= ( wall_bot, 0.0, wall_top, 0.0 )
INC_INLET_TYPE= VELOCITY_INLET
MARKER_INLET= ( inlet, 0.0, {velocity_m_s:.9g}, 1.0, 0.0, 0.0 )
% the fully developed profile: no entrance length, so the domain can stay short
SPECIFIED_INLET_PROFILE= YES
INLET_FILENAME= inlet.dat
INC_OUTLET_TYPE= PRESSURE_OUTLET
MARKER_OUTLET= ( outlet, 0.0 )

NUM_METHOD_GRAD= GREEN_GAUSS
CFL_NUMBER= 100.0
ITER= {int(max_iter)}
CONV_NUM_METHOD_FLOW= FDS
MUSCL_FLOW= YES
SLOPE_LIMITER_FLOW= NONE
TIME_DISCRE_FLOW= EULER_IMPLICIT
LINEAR_SOLVER= FGMRES
LINEAR_SOLVER_PREC= ILU
LINEAR_SOLVER_ERROR= 1E-12
LINEAR_SOLVER_ITER= 30

% Converge on the ANSWER. rms[p] is absolute and stalls near -5 on a low-speed
% viscous case while the pressure drop is still 13 % short of its final value.
CONV_FIELD= ( SURFACE_PRESSURE_DROP )
CONV_CAUCHY_ELEMS= 100
CONV_CAUCHY_EPS= {cauchy_eps:g}
CONV_STARTITER= 50

MESH_FILENAME= mesh.su2
MESH_FORMAT= SU2
SCREEN_OUTPUT= ( INNER_ITER, RMS_PRESSURE, SURFACE_PRESSURE_DROP )
HISTORY_OUTPUT= ( ITER, RMS_RES, FLOW_COEFF )
CONV_FILENAME= history
MARKER_ANALYZE= ( inlet, outlet )
MARKER_ANALYZE_AVERAGE= AREA
OUTPUT_FILES= ( RESTART )
"""


def write_channel_case(case_dir: str, height_mm: float = 10.0,
                       length_mm: float = 100.0, velocity_m_s: float = 0.277778,
                       rho_kg_m3: float = 900.0, mu_pa_s: float = 0.1,
                       nx: int = 40, ny: int = 40, max_iter: int = 3000,
                       cauchy_eps: float = 1e-12) -> dict:
    """Write a complete plane-channel SU2 case into ``case_dir``.

    Defaults are the validated gate point: a 10 mm × 100 mm channel of light oil at
    Re = 50, which converges in a few hundred iterations and reproduces the closed
    form to the digits the history file prints. Returns the case metadata including
    ``analytic_dp_pa`` — the number the solve is graded against."""
    if min(height_mm, length_mm) <= 0:
        raise ValueError("height_mm and length_mm must be > 0")
    if nx < 4 or ny < 4:
        raise ValueError("nx and ny must be at least 4 (this is a graded oracle, "
                         "not a smoke test)")
    os.makedirs(case_dir, exist_ok=True)
    h, L = height_mm / 1000.0, length_mm / 1000.0
    pts, quads, markers = _structured_quad_mesh(L, h, nx, ny)
    write_su2_mesh(os.path.join(case_dir, "mesh.su2"), pts, quads, markers)
    write_inlet_profile(os.path.join(case_dir, "inlet.dat"), h, velocity_m_s, ny)
    with open(os.path.join(case_dir, "flow.cfg"), "w", encoding="utf-8") as fh:
        fh.write(_config(rho_kg_m3, mu_pa_s, velocity_m_s, max_iter, cauchy_eps))
    reynolds = rho_kg_m3 * velocity_m_s * (2.0 * h) / mu_pa_s
    return {
        "case_dir": case_dir,
        "config": "flow.cfg",
        "n_cells": len(quads),
        "n_points": len(pts),
        "height_mm": height_mm,
        "length_mm": length_mm,
        "velocity_m_s": velocity_m_s,
        "rho_kg_m3": rho_kg_m3,
        "mu_pa_s": mu_pa_s,
        "reynolds": reynolds,
        "analytic_dp_pa": plane_poiseuille_dp(h, L, velocity_m_s, mu_pa_s),
        "laminar": reynolds < 1400.0,     # plane-channel transition is ~Re 1400
    }


def read_history(case_dir: str) -> dict:
    """Parse SU2's ``history.csv`` and return what the gate needs.

    Returns {ok, iterations, pressure_drop_pa, rms_p} — ``pressure_drop_pa`` as a
    MAGNITUDE, because SU2's sign convention for Pressure_Drop is outlet-minus-inlet
    and the oracle is a magnitude. ``ok`` is False (rather than raising) when the file
    is missing or carries no rows, so a failed solve reads as a failed measurement."""
    path = os.path.join(case_dir, "history.csv")
    if not os.path.isfile(path):
        return {"ok": False, "reason": "SU2 wrote no history.csv (the solve did not "
                                       "start — check stdout_tail)"}
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return {"ok": False, "reason": "SU2's history.csv has no iterations in it"}
    header = [h.strip().strip('"') for h in rows[0]]
    last = dict(zip(header, [v.strip() for v in rows[-1]]))
    if "Pressure_Drop" not in last:
        return {"ok": False,
                "reason": "SU2's history.csv carries no Pressure_Drop column — the "
                          "case needs MARKER_ANALYZE and FLOW_COEFF in HISTORY_OUTPUT"}
    def _f(key):
        try:
            return float(last[key])
        except (KeyError, ValueError):
            return None
    return {
        "ok": True,
        "iterations": len(rows) - 1,
        "pressure_drop_pa": abs(float(last["Pressure_Drop"])),
        "rms_p": _f("rms[P]"),
    }
