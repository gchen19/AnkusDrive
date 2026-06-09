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


# --- radiation: two-plate diffuse-gray enclosure (P3 M2) ----------------------
# The sibling of the transient slab: two parallel plates exchanging heat by
# diffuse-gray radiation across a vacuum gap. The exact oracle is the two infinite
# parallel plates net flux q = σ(T₁⁴−T₂⁴)/(1/ε₁+1/ε₂−1) (thermal.radiation_exchange).
# Model (2-D, unit depth): two thin conducting strips, the gap between their facing
# faces unmeshed. Plate 1's outer face is held at T₁ and its inner face radiates
# (ε₁); plate 2 symmetric at T₂ (ε₂). ElmerSolver's HeatSolver runs the Diffuse Gray
# radiation (view factors from the ViewFactors binary); in steady state the diffusive
# flux conducted in through plate 1's outer (Dirichlet) face equals the net radiative
# exchange — SaveScalars writes it. A high plate conductivity keeps each plate nearly
# isothermal, so its radiating face sits at the imposed temperature. Validated against
# the two-plate oracle to < 0.5 % (the residual is finite-plate edge leakage, F<1).
# Radiation is T⁴, so this case runs in KELVIN (unlike the offset-invariant slab).

_RAD_FLUX_COL = 0  # SaveScalars col 1 = integrated "diffusive flux over bc 1" (net W)


def radiation_plates_mesh_files(
    n_x: int, width_m: float, plate_thickness_m: float, gap_m: float, n_y: int = 2,
) -> dict:
    """Native Elmer 2-D mesh of two parallel plates across a vacuum gap.

    Two stacked quad (404) grids — plate 1 over y∈[−t, 0], plate 2 over y∈[g, g+t],
    each ``n_x`` × ``n_y`` cells across ``width_m`` — with the gap [0, g] left
    unmeshed (radiation crosses it). Boundary tags: **1** plate-1 outer face (y=−t,
    Dirichlet T₁), **2** plate-1 inner face (y=0, radiates), **3** plate-2 inner face
    (y=g, radiates), **4** plate-2 outer face (y=g+t, Dirichlet T₂). Returns the four
    Elmer mesh files as a ``{filename: text}`` dict. Raises ValueError on a degenerate
    size."""
    if n_x < 2 or n_y < 1:
        raise ValueError("n_x must be >= 2 and n_y >= 1")
    if width_m <= 0 or plate_thickness_m <= 0 or gap_m <= 0:
        raise ValueError("width_m, plate_thickness_m, gap_m must be > 0")

    nodes: list = []
    node_id: dict = {}

    def add(ix, iy, y):
        key = (ix, iy)
        if key not in node_id:
            node_id[key] = len(nodes) + 1
            nodes.append((node_id[key], width_m * ix / n_x, y))
        return node_id[key]

    elems: list = []
    # body 1 rows iy 0..n_y over [-t, 0]; body 2 rows iy 100.. over [g, g+t]
    for body, y0, y1, iy_off in ((1, -plate_thickness_m, 0.0, 0),
                                 (2, gap_m, gap_m + plate_thickness_m, 100)):
        for j in range(n_y):
            ya = y0 + (y1 - y0) * j / n_y
            yb = y0 + (y1 - y0) * (j + 1) / n_y
            for i in range(n_x):
                n1 = add(i, iy_off + j, ya)
                n2 = add(i + 1, iy_off + j, ya)
                n3 = add(i + 1, iy_off + j + 1, yb)
                n4 = add(i, iy_off + j + 1, yb)
                elems.append((len(elems) + 1, body, n1, n2, n3, n4))

    bnd: list = []
    for i in range(n_x):
        p1_bot = i + 1
        p1_top = (n_y - 1) * n_x + i + 1
        p2_bot = n_x * n_y + i + 1
        p2_top = n_x * n_y + (n_y - 1) * n_x + i + 1
        bnd.append((len(bnd) + 1, 1, p1_bot, node_id[(i, 0)], node_id[(i + 1, 0)]))
        bnd.append((len(bnd) + 1, 2, p1_top, node_id[(i, n_y)], node_id[(i + 1, n_y)]))
        bnd.append((len(bnd) + 1, 3, p2_bot, node_id[(i, 100)], node_id[(i + 1, 100)]))
        bnd.append((len(bnd) + 1, 4, p2_top,
                    node_id[(i, 100 + n_y)], node_id[(i + 1, 100 + n_y)]))

    header = f"{len(nodes)} {len(elems)} {len(bnd)}\n2\n202 {len(bnd)}\n404 {len(elems)}\n"
    mnodes = "".join(f"{nid} -1 {x:.10g} {y:.10g} 0.0\n" for nid, x, y in nodes)
    melems = "".join(f"{e[0]} {e[1]} 404 {e[2]} {e[3]} {e[4]} {e[5]}\n" for e in elems)
    mbnd = "".join(f"{b[0]} {b[1]} {b[2]} 0 202 {b[3]} {b[4]}\n" for b in bnd)
    return {
        "mesh.header": header,
        "mesh.nodes": mnodes,
        "mesh.elements": melems,
        "mesh.boundary": mbnd,
    }


def radiation_plates_sif(
    *,
    t1_c: float,
    t2_c: float,
    emissivity_1: float,
    emissivity_2: float,
    k_plate: float = 400.0,
    view_factors: str = "ViewFactors.dat",
    gebhart: str = "GebhartFactors.dat",
    scalars: str = "rad.dat",
    stefan_boltzmann: float = 5.670374419e-8,
) -> str:
    """The ``.sif`` deck for the two-plate diffuse-gray radiation enclosure.

    Steady-state HeatSolver in **Kelvin** (radiation is T⁴): boundary tag 1 Dirichlet
    T₁, tag 4 Dirichlet T₂, tags 2 & 3 ``Radiation = Diffuse Gray`` with the two
    emissivities (the ``ViewFactors`` binary fills ``view_factors``/``gebhart`` from
    the Simulation block). A SaveScalars solver writes the integrated diffusive flux
    over tag 1 to ``scalars`` (the net radiative exchange). ``k_plate`` is the (high)
    plate conductivity that keeps each plate near-isothermal. Returns the deck text."""
    return f"""Header
  Mesh DB "." "{{mesh_db}}"
End
Constants
  Stefan Boltzmann = {stefan_boltzmann:.10g}
End
Simulation
  Coordinate System = Cartesian 2D
  Simulation Type = Steady State
  Steady State Max Iterations = 60
  Output Intervals = 0
  View Factors = "{view_factors}"
  Gebhart Factors = "{gebhart}"
End
Body 1
  Equation = 1
  Material = 1
End
Body 2
  Equation = 1
  Material = 1
End
Material 1
  Density = 1.0
  Heat Conductivity = {k_plate:.10g}
End
Solver 1
  Equation = Heat Equation
  Procedure = "HeatSolve" "HeatSolver"
  Variable = Temperature
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
  Nonlinear System Max Iterations = 50
  Nonlinear System Convergence Tolerance = 1.0e-7
  Nonlinear System Relaxation Factor = 0.5
  Steady State Convergence Tolerance = 1.0e-6
End
Solver 2
  Equation = SaveScalars
  Procedure = "SaveData" "SaveScalars"
  Filename = "{scalars}"
  Variable 1 = Temperature
  Operator 1 = "diffusive flux"
  Coefficient 1 = "Heat Conductivity"
End
Equation 1
  Active Solvers(2) = 1 2
End
Boundary Condition 1
  Target Boundaries(1) = 1
  Temperature = {t1_c + 273.15:.10g}
  Save Scalars = True
End
Boundary Condition 2
  Target Boundaries(1) = 2
  Radiation = Diffuse Gray
  Emissivity = {emissivity_1:.10g}
End
Boundary Condition 3
  Target Boundaries(1) = 3
  Radiation = Diffuse Gray
  Emissivity = {emissivity_2:.10g}
End
Boundary Condition 4
  Target Boundaries(1) = 4
  Temperature = {t2_c + 273.15:.10g}
End
"""


def write_radiation_plates_case(
    case_dir: str,
    *,
    t1_c: float,
    t2_c: float,
    emissivity_1: float,
    emissivity_2: float,
    width_m: float = 1.0,
    gap_m: float = 0.01,
    plate_thickness_m: float = 0.01,
    n_x: int = 80,
    n_y: int = 2,
    k_plate: float = 400.0,
    depth_m: float = 1.0,
    mesh_name: str = "rad",
    scalars: str = "rad.dat",
) -> dict:
    """Write a complete, runnable Elmer two-plate radiation case under ``case_dir``.

    Lays down ``<case_dir>/<mesh_name>/mesh.*`` (the native two-plate mesh), ``case.sif``
    and ``ELMERSOLVER_STARTINFO``. The case must be run as ``ViewFactors case.sif``
    *then* ``ElmerSolver case.sif`` (view factors first). ``width_m``×``depth_m`` is the
    plate area used to reduce the integrated flux to W/m². Returns metadata
    ``{case_dir, sif, mesh_db, scalars, area_1_m2, width_m, gap_m, n_x}``. Raises
    ValueError on bad emissivities/geometry."""
    if not (0.0 < emissivity_1 <= 1.0 and 0.0 < emissivity_2 <= 1.0):
        raise ValueError("emissivities must be in (0, 1]")

    mesh_dir = os.path.join(case_dir, mesh_name)
    os.makedirs(mesh_dir, exist_ok=True)
    files = radiation_plates_mesh_files(n_x, width_m, plate_thickness_m, gap_m, n_y)
    for fname, text in files.items():
        with open(os.path.join(mesh_dir, fname), "w") as f:
            f.write(text)

    sif_text = radiation_plates_sif(
        t1_c=t1_c, t2_c=t2_c, emissivity_1=emissivity_1, emissivity_2=emissivity_2,
        k_plate=k_plate, scalars=scalars).replace("{mesh_db}", mesh_name)
    with open(os.path.join(case_dir, "case.sif"), "w") as f:
        f.write(sif_text)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w") as f:
        f.write("case.sif\n")

    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_db": mesh_name,
        "scalars": scalars,
        "area_1_m2": width_m * depth_m,
        "width_m": width_m,
        "gap_m": gap_m,
        "n_x": n_x,
    }


def parse_radiation_flux(
    case_dir: str, scalars: str = "rad.dat", area_1_m2: float = 1.0,
) -> dict | None:
    """Read the SaveScalars output and return the net radiative exchange.

    Column 1 of the last row is the integrated diffusive flux over boundary 1 (net
    watts conducted into plate 1 = the radiative exchange in steady state).
    ``{q_net_w, flux_w_m2}`` with ``flux_w_m2 = q_net_w / area_1_m2``. Returns None
    when the file is absent or empty (e.g. the solve failed)."""
    path = os.path.join(case_dir, scalars)
    matches = sorted(glob.glob(path)) or sorted(glob.glob(path + "*"))
    data = next((m for m in [path, *matches] if os.path.isfile(m)
                 and not m.endswith(".names")), None)
    if data is None:
        return None
    rows = [r for r in open(data).read().splitlines() if r.strip()]
    if not rows:
        return None
    try:
        q_net = float(rows[-1].split()[_RAD_FLUX_COL])
        return {"q_net_w": q_net,
                "flux_w_m2": q_net / area_1_m2 if area_1_m2 else q_net}
    except (ValueError, IndexError):
        return None
