"""Elmer case generation — the heavy-solver case-from-geometry half of P2 family 4.

Pure-Python, FreeCAD-free (the analytic core is testable on the no-FreeCAD lane; the
*solve* needs the ElmerSolver binary). The transient/radiation thermal family
(``docs/SIMULATION_P2_KICKOFF.md`` M4) rides on Elmer; the kickoff's gate is
**relative to the analytic oracle** in the trivial-mesh limit: a 1-D plane-wall
transient where ``thermal_transient_1d`` (the one-term Heisler series) is exact.

This module builds exactly that case — the slab BVP the oracle solves, so the two are
directly comparable:

    a plane wall of half-thickness L cooling (or heating) from a uniform T_initial
    toward T_ambient through surface convection h; by symmetry only the half-domain
    [0, L] is modelled, with an insulated (natural, zero-flux) face at the centre x=0
    and a convective face −k·∂T/∂x = h·(T − T_ambient) at the surface x=L.

It emits Elmer's **native ASCII mesh** (a 1-D line mesh — full control of the two
boundary tags, no ElmerGrid round-trip) plus the ``.sif`` solver deck. A SaveScalars
solver writes ``max``/``min`` of the temperature field each step: for a cooling slab
the maximum is the centre (x=0) and the minimum the convective surface (x=L), so the
two columns are the centre and surface histories the oracle predicts. Validated
against ``thermal_transient_1d`` to < 0.1 % (Bi=0.5, Fo=0.5).

Units are SI throughout (m, kg, s, K-difference; W/m·K, kg/m³, J/kg·K, W/m²·K, °C).
The heat equation is linear in T, so feeding °C with an °C external temperature is
consistent. See ``tests/test_elmer.py`` for the structure gate and the solver-backed
end-to-end gate.
"""
from __future__ import annotations

import glob
import os

# SaveScalars column order is fixed by the .sif below: operator 1 = max, 2 = min.
_COL_CENTER = 0   # max(Temperature) -> the symmetry plane x=0 (hottest while cooling)
_COL_SURFACE = 1  # min(Temperature) -> the convective surface x=L (coolest)


def slab_mesh_files(n_elements: int, length_m: float) -> dict:
    """Native Elmer 1-D line mesh of the half-slab ``[0, length_m]``.

    ``n_elements`` two-node line (202) bulk elements over ``n_elements+1`` nodes, plus
    two point (101) boundary elements: tag **1** at x=0 (the symmetry plane) and tag
    **2** at x=length_m (the convective surface). Returns the four Elmer mesh files as
    a ``{filename: text}`` dict (``mesh.header``, ``mesh.nodes``, ``mesh.elements``,
    ``mesh.boundary``). Raises ValueError on a degenerate size."""
    if n_elements < 2:
        raise ValueError("n_elements must be >= 2")
    if length_m <= 0:
        raise ValueError("length_m must be > 0")
    n_nodes = n_elements + 1

    nodes = "".join(
        f"{i} -1 {(i - 1) * length_m / n_elements:.10g} 0.0 0.0\n"
        for i in range(1, n_nodes + 1)
    )
    elements = "".join(
        f"{e} 1 202 {e} {e + 1}\n" for e in range(1, n_elements + 1)
    )
    # boundary: <id> <tag> <parent-bulk-elem> <parent2=0> <type=101> <node>
    boundary = (
        f"1 1 1 0 101 1\n"
        f"2 2 {n_elements} 0 101 {n_nodes}\n"
    )
    header = f"{n_nodes} {n_elements} 2\n2\n101 2\n202 {n_elements}\n"
    return {
        "mesh.header": header,
        "mesh.nodes": nodes,
        "mesh.elements": elements,
        "mesh.boundary": boundary,
    }


def slab_transient_sif(
    *,
    k: float,
    rho: float,
    cp: float,
    h_conv: float,
    t_initial_c: float,
    t_ambient_c: float,
    dt: float,
    n_steps: int,
    mesh_name: str = "slab",
    scalars: str = "scalars.dat",
) -> str:
    """The ``.sif`` deck for the 1-D transient-conduction slab.

    Transient BDF(2) HeatSolver on the ``mesh_name`` mesh DB: uniform ``t_initial_c``
    initial field, material (``k``, ``rho``, ``cp``), boundary tag 1 natural/insulated
    (symmetry), tag 2 convective (``h_conv``, ``t_ambient_c``). A SaveScalars solver
    writes ``max`` then ``min`` of Temperature to ``scalars``. ``n_steps`` steps of
    size ``dt`` (so the solve reaches ``dt*n_steps`` seconds). Returns the deck text."""
    return f"""Header
  Mesh DB "." "{mesh_name}"
End
Simulation
  Coordinate System = "Cartesian 1D"
  Simulation Type = Transient
  Timestepping Method = BDF
  BDF Order = 2
  Timestep Sizes = {dt:.10g}
  Timestep Intervals = {int(n_steps)}
  Steady State Max Iterations = 1
  Output Intervals = 0
End
Body 1
  Equation = 1
  Material = 1
  Initial Condition = 1
End
Initial Condition 1
  Temperature = {t_initial_c:.10g}
End
Material 1
  Density = {rho:.10g}
  Heat Conductivity = {k:.10g}
  Heat Capacity = {cp:.10g}
End
Solver 1
  Equation = Heat Equation
  Procedure = "HeatSolve" "HeatSolver"
  Variable = Temperature
  Linear System Solver = Direct
  Linear System Direct Method = Banded
  Nonlinear System Max Iterations = 1
  Steady State Convergence Tolerance = 1.0e-6
End
Solver 2
  Equation = "SaveScalars"
  Procedure = "SaveData" "SaveScalars"
  Filename = "{scalars}"
  Variable 1 = Temperature
  Operator 1 = max
  Operator 2 = min
End
Equation 1
  Active Solvers(2) = 1 2
End
Boundary Condition 1
  Target Boundaries(1) = 1
End
Boundary Condition 2
  Target Boundaries(1) = 2
  Heat Transfer Coefficient = {h_conv:.10g}
  External Temperature = {t_ambient_c:.10g}
End
"""


def write_slab_transient_case(
    case_dir: str,
    *,
    half_thickness_m: float,
    k: float,
    rho: float,
    cp: float,
    h_conv: float,
    t_initial_c: float,
    t_ambient_c: float,
    duration_s: float,
    n_elements: int = 40,
    n_steps: int = 120,
    mesh_name: str = "slab",
    scalars: str = "scalars.dat",
) -> dict:
    """Write a complete, runnable Elmer slab case under ``case_dir``.

    Lays down ``<case_dir>/<mesh_name>/mesh.*`` (native 1-D mesh), ``case.sif``, and
    ``ELMERSOLVER_STARTINFO`` (so a bare ``ElmerSolver`` run with cwd=case_dir finds
    the deck). ``duration_s`` is split into ``n_steps`` equal BDF steps. Returns
    metadata ``{case_dir, sif, mesh_db, n_elements, n_steps, dt, duration_s,
    scalars}``. Raises ValueError on non-positive geometry/time."""
    if half_thickness_m <= 0 or duration_s <= 0:
        raise ValueError("half_thickness_m and duration_s must be > 0")
    if n_steps < 1:
        raise ValueError("n_steps must be >= 1")
    dt = duration_s / n_steps

    mesh_dir = os.path.join(case_dir, mesh_name)
    os.makedirs(mesh_dir, exist_ok=True)
    for fname, text in slab_mesh_files(n_elements, half_thickness_m).items():
        with open(os.path.join(mesh_dir, fname), "w") as f:
            f.write(text)

    sif_text = slab_transient_sif(
        k=k, rho=rho, cp=cp, h_conv=h_conv, t_initial_c=t_initial_c,
        t_ambient_c=t_ambient_c, dt=dt, n_steps=n_steps, mesh_name=mesh_name,
        scalars=scalars)
    with open(os.path.join(case_dir, "case.sif"), "w") as f:
        f.write(sif_text)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w") as f:
        f.write("case.sif\n")

    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_db": mesh_name,
        "n_elements": n_elements,
        "n_steps": n_steps,
        "dt": dt,
        "duration_s": duration_s,
        "scalars": scalars,
    }


def parse_slab_scalars(case_dir: str, scalars: str = "scalars.dat") -> dict | None:
    """Read the SaveScalars output and return the final-step centre/surface temps.

    ``{t_center_c, t_surface_c, n_steps_written}`` from the last row of
    ``<case_dir>/<scalars>`` (column 1 = max = centre x=0, column 2 = min = surface
    x=L, per ``slab_transient_sif``). Returns None when the file is absent or empty
    (e.g. the solve failed)."""
    path = os.path.join(case_dir, scalars)
    matches = sorted(glob.glob(path)) or sorted(glob.glob(path + "*"))
    # SaveScalars may suffix the name; prefer the exact file, else the first match
    # that is not the .names sidecar.
    data = next((m for m in [path, *matches] if os.path.isfile(m)
                 and not m.endswith(".names")), None)
    if data is None:
        return None
    rows = [r for r in open(data).read().splitlines() if r.strip()]
    if not rows:
        return None
    cols = rows[-1].split()
    try:
        return {
            "t_center_c": float(cols[_COL_CENTER]),
            "t_surface_c": float(cols[_COL_SURFACE]),
            "n_steps_written": len(rows),
        }
    except (ValueError, IndexError):
        return None
