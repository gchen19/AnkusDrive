"""Conjugate heat transfer — coupled multi-region thermal (P3 M6 frontier).

Pure-Python, FreeCAD-free (the analytic core + case text are testable on the
no-FreeCAD lane; the *solve* needs the ElmerSolver binary). The frontier family the
P3 kickoff names "conjugate heat transfer (solid+fluid thermal)": one solve spanning
a flowing fluid region AND a conducting solid region, coupled at a shared interface.

Two exact anchors, chosen so NO Nusselt correlation is needed:

* **Composite wall / film network** (:func:`composite_wall`) — the series thermal
  resistance of N solid layers plus optional convection films:
  U = 1/(1/h_in + Σ tᵢ/kᵢ + 1/h_out), q = U·ΔT, with every interface temperature
  exact. Closed-form, the fast-lane oracle (and a useful tool on its own).
* **The plug-flow heated channel** (:func:`cht_channel_oracle`) — a fluid channel
  with uniform (plug) velocity under a solid wall heated by a constant outer flux
  q″. Steady state makes both gates exact *independent of the convection
  coefficient*: the outlet bulk temperature follows the energy balance
  T_out = T_in + q″·L/(ρ·U·H·c_p), and the mean temperature drop across the solid
  layer is q″·t/k. Plug flow also makes the area mean equal the mixing-cup mean,
  so the solved boundary mean is directly comparable.

The Elmer case (:func:`write_cht_channel_case`) is a native 2-D two-body mesh —
fluid channel + solid wall sharing interface nodes — with ``Convection = Constant``
(prescribed plug velocity) on the fluid equation only, inlet Dirichlet, constant
``Heat Flux`` on the outer solid face, everything else natural/adiabatic. A
SaveScalars solver records the boundary-mean temperature of the outlet, the outer
face and the (tagged, condition-free) internal interface via per-BC masks.

Numerical envelope, measured live: the SUPG-stabilized advection keeps the energy
balance within ~0.5 % at cell Péclet ρ·c_p·U·Δx/k ≈ 9 (the default parameters);
the error grows with Pe_cell (~12 % at ≈ 90), while the solid-layer drop stays
within 0.2 % regardless. Keep Pe_cell ≲ 25 — the writer rejects above that.

Units SI (m, m/s, W/m·K, kg/m³, J/kg·K), temperatures °C (the heat equation is
linear, so °C offsets are consistent). See ``tests/test_cht.py``.
"""
from __future__ import annotations

import os

from . import materials

# SaveScalars column order fixed by cht_channel_sif's mask order.
_COL_OUTLET = 0
_COL_OUTER = 1
_COL_INTERFACE = 2


def _layer_conductivity(layer: dict) -> float:
    """Resolve a layer's thermal conductivity (W/m·K) from an explicit ``k`` or a
    Materials-DB ``material`` name. Raises ValueError when neither resolves."""
    if layer.get("k") is not None:
        k = float(layer["k"])
    elif layer.get("material"):
        card = materials.get(layer["material"])
        val = card.get("thermal_conductivity")
        if val is None:
            raise ValueError(
                f"material {layer['material']!r} has no thermal_conductivity")
        k = materials.parse_quantity(val)[0]
    else:
        raise ValueError("each layer needs k (W/m·K) or a material name")
    if k <= 0:
        raise ValueError("layer conductivity must be > 0")
    return k


def composite_wall(
    layers: list,
    t_in_c: float,
    t_out_c: float,
    h_in: float | None = None,
    h_out: float | None = None,
    area_m2: float = 1.0,
) -> dict:
    """Exact series thermal-resistance network of a plane composite wall — the
    closed-form conjugate-coupling oracle (and the classic overall-U calculation).

    ``layers`` is the in→out sequence of solid layers, each
    ``{thickness_mm, k | material}``; ``h_in`` / ``h_out`` are optional convection
    film coefficients (W/m²K) on the inner/outer faces. Per unit area
    R_total = 1/h_in + Σ tᵢ/kᵢ + 1/h_out, U = 1/R_total, q = U·(t_in − t_out), and
    walking the drops gives every surface/interface temperature exactly
    (``interface_temps_c``: inner fluid-side surface first, outer last — the film
    drops happen before/after the listed surfaces).

    Returns {u_w_m2k, r_total_m2k_w, q_w_m2, q_w, layer_resistances_m2k_w,
    interface_temps_c, t_in_c, t_out_c, area_m2}. Raises ValueError on no layers, a
    non-positive thickness/k/h/area, or an unresolvable material."""
    if not layers:
        raise ValueError("at least one layer is required")
    if area_m2 <= 0:
        raise ValueError("area_m2 must be > 0")
    if (h_in is not None and h_in <= 0) or (h_out is not None and h_out <= 0):
        raise ValueError("film coefficients must be > 0 when given")

    r_layers = []
    for layer in layers:
        t_mm = float(layer.get("thickness_mm", 0.0))
        if t_mm <= 0:
            raise ValueError("each layer needs thickness_mm > 0")
        r_layers.append((t_mm / 1000.0) / _layer_conductivity(layer))

    r_total = sum(r_layers)
    if h_in is not None:
        r_total += 1.0 / h_in
    if h_out is not None:
        r_total += 1.0 / h_out
    u = 1.0 / r_total
    q = u * (t_in_c - t_out_c)                        # W/m², in -> out

    temps = []                                        # surfaces, inner -> outer
    t = t_in_c - (q / h_in if h_in is not None else 0.0)
    temps.append(t)
    for r in r_layers:
        t -= q * r
        temps.append(t)

    return {
        "u_w_m2k": round(u, 6),
        "r_total_m2k_w": round(r_total, 6),
        "q_w_m2": round(q, 6),
        "q_w": round(q * area_m2, 6),
        "layer_resistances_m2k_w": [round(r, 6) for r in r_layers],
        "interface_temps_c": [round(v, 4) for v in temps],
        "t_in_c": t_in_c,
        "t_out_c": t_out_c,
        "area_m2": area_m2,
    }


def cht_channel_oracle(
    *,
    flux_w_m2: float,
    length_m: float,
    fluid_height_m: float,
    velocity_m_s: float,
    t_in_c: float,
    rho: float,
    cp: float,
    solid_thickness_m: float,
    k_solid: float,
) -> dict:
    """The exact answers for the plug-flow heated-channel CHT case, per unit depth.

    Steady state, all walls adiabatic except the constant-flux outer face, so the
    energy balance is exact and h-free: ṁ′ = ρ·U·H (kg/s per metre of depth),
    T_out = T_in + q″·L/(ṁ′·c_p); and the mean drop across the solid layer is
    ΔT_solid = q″·t/k (every watt crosses it). Returns {t_out_c, dt_out_k,
    dt_solid_k, m_dot_kg_s_m, q_total_w_m}. Raises ValueError on non-positive
    inputs (flux may be 0 — the zero-power negative)."""
    if min(length_m, fluid_height_m, velocity_m_s, rho, cp,
           solid_thickness_m, k_solid) <= 0:
        raise ValueError("geometry, velocity and properties must be > 0")
    if flux_w_m2 < 0:
        raise ValueError("flux_w_m2 must be >= 0")
    m_dot = rho * velocity_m_s * fluid_height_m
    q_total = flux_w_m2 * length_m
    dt_out = q_total / (m_dot * cp)
    return {
        "t_out_c": round(t_in_c + dt_out, 6),
        "dt_out_k": round(dt_out, 6),
        "dt_solid_k": round(flux_w_m2 * solid_thickness_m / k_solid, 6),
        "m_dot_kg_s_m": round(m_dot, 9),
        "q_total_w_m": round(q_total, 6),
    }


# --- the Elmer two-body channel case --------------------------------------------

def cht_channel_mesh_files(
    nx: int, ny_fluid: int, ny_solid: int,
    length_m: float, fluid_height_m: float, solid_thickness_m: float,
) -> dict:
    """Native Elmer 2-D mesh: fluid channel (body 1, y ∈ [0, H]) under a solid wall
    (body 2, y ∈ [H, H+t]) sharing the interface nodes — the conjugate coupling is
    the mesh itself. Boundary tags: **1** inlet (fluid x=0), **2** outlet (fluid
    x=L), **3** outer solid face (y=H+t), **4** fluid bottom (y=0), **5** the
    internal fluid–solid interface (condition-free; tagged for SaveScalars).
    Everything untagged (the solid's x ends) is natural/adiabatic. Returns the four
    Elmer mesh files as ``{filename: text}``. Raises ValueError on degenerate
    sizes."""
    if nx < 4 or ny_fluid < 2 or ny_solid < 1:
        raise ValueError("need nx >= 4, ny_fluid >= 2, ny_solid >= 1")
    if min(length_m, fluid_height_m, solid_thickness_m) <= 0:
        raise ValueError("length_m, fluid_height_m, solid_thickness_m must be > 0")

    nodes: list = []
    nid: dict = {}

    def add(ix, iy):
        if (ix, iy) not in nid:
            if iy <= ny_fluid:
                y = fluid_height_m * iy / ny_fluid
            else:
                y = fluid_height_m + solid_thickness_m * (iy - ny_fluid) / ny_solid
            nid[(ix, iy)] = len(nodes) + 1
            nodes.append((nid[(ix, iy)], length_m * ix / nx, y))
        return nid[(ix, iy)]

    elems: list = []
    for body, j0, j1 in ((1, 0, ny_fluid), (2, ny_fluid, ny_fluid + ny_solid)):
        for j in range(j0, j1):
            for i in range(nx):
                elems.append((len(elems) + 1, body, add(i, j), add(i + 1, j),
                              add(i + 1, j + 1), add(i, j + 1)))

    # true parent bulk elements (fluid rows first): fluid (i,j) -> j*nx+i+1,
    # solid (i,j) -> nx*ny_fluid + (j-ny_fluid)*nx + i + 1
    bnd: list = []
    for j in range(ny_fluid):
        bnd.append((len(bnd) + 1, 1, j * nx + 1, add(0, j), add(0, j + 1)))
        bnd.append((len(bnd) + 1, 2, j * nx + nx, add(nx, j), add(nx, j + 1)))
    top_row0 = nx * ny_fluid + (ny_solid - 1) * nx
    iface_row0 = (ny_fluid - 1) * nx
    for i in range(nx):
        bnd.append((len(bnd) + 1, 3, top_row0 + i + 1,
                    add(i, ny_fluid + ny_solid), add(i + 1, ny_fluid + ny_solid)))
        bnd.append((len(bnd) + 1, 4, i + 1, add(i, 0), add(i + 1, 0)))
        bnd.append((len(bnd) + 1, 5, iface_row0 + i + 1,
                    add(i, ny_fluid), add(i + 1, ny_fluid)))

    header = f"{len(nodes)} {len(elems)} {len(bnd)}\n2\n202 {len(bnd)}\n404 {len(elems)}\n"
    return {
        "mesh.header": header,
        "mesh.nodes": "".join(f"{n} -1 {x:.10g} {y:.10g} 0.0\n" for n, x, y in nodes),
        "mesh.elements": "".join(
            f"{e[0]} {e[1]} 404 {e[2]} {e[3]} {e[4]} {e[5]}\n" for e in elems),
        "mesh.boundary": "".join(
            f"{b[0]} {b[1]} {b[2]} 0 202 {b[3]} {b[4]}\n" for b in bnd),
    }


def cht_channel_sif(
    *,
    velocity_m_s: float,
    flux_w_m2: float,
    t_in_c: float,
    k_fluid: float,
    rho_fluid: float,
    cp_fluid: float,
    k_solid: float,
    mesh_name: str = "chan",
    scalars: str = "cht.dat",
) -> str:
    """The ``.sif`` deck for the conjugate plug-flow channel.

    Steady HeatSolver over both bodies; the fluid equation carries
    ``Convection = Constant`` with the plug ``velocity_m_s`` along +x (the solid
    equation has none — that asymmetry IS the conjugate coupling). Inlet Dirichlet
    ``t_in_c``, outer face constant ``Heat Flux``, all else natural. SaveScalars
    writes the boundary-mean temperature of outlet / outer face / interface via
    per-BC masks (columns in that order — see ``parse_cht_scalars``). Stabilized
    (SUPG): keep cell Péclet ≲ 25 for the energy balance to hold to a few %."""
    if min(velocity_m_s, k_fluid, rho_fluid, cp_fluid, k_solid) <= 0:
        raise ValueError("velocity and material properties must be > 0")
    if flux_w_m2 < 0:
        raise ValueError("flux_w_m2 must be >= 0")
    return f"""Header
  Mesh DB "." "{mesh_name}"
End
Simulation
  Coordinate System = Cartesian 2D
  Simulation Type = Steady State
  Steady State Max Iterations = 1
  Output Intervals = 0
End
Body 1
  Equation = 1
  Material = 1
End
Body 2
  Equation = 2
  Material = 2
End
Material 1
  Density = {rho_fluid:.10g}
  Heat Conductivity = {k_fluid:.10g}
  Heat Capacity = {cp_fluid:.10g}
  Convection Velocity 1 = {velocity_m_s:.10g}
  Convection Velocity 2 = 0.0
End
Material 2
  Density = 1.0
  Heat Conductivity = {k_solid:.10g}
  Heat Capacity = 1.0
End
Equation 1
  Active Solvers(2) = 1 2
  Convection = Constant
End
Equation 2
  Active Solvers(2) = 1 2
End
Solver 1
  Equation = Heat Equation
  Procedure = "HeatSolve" "HeatSolver"
  Variable = Temperature
  Stabilize = True
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
  Nonlinear System Max Iterations = 1
  Steady State Convergence Tolerance = 1.0e-8
End
Solver 2
  Equation = SaveScalars
  Procedure = "SaveData" "SaveScalars"
  Filename = "{scalars}"
  Variable 1 = Temperature
  Operator 1 = boundary mean
  Mask Name 1 = outletmask
  Variable 2 = Temperature
  Operator 2 = boundary mean
  Mask Name 2 = outermask
  Variable 3 = Temperature
  Operator 3 = boundary mean
  Mask Name 3 = ifacemask
End
Boundary Condition 1
  Target Boundaries(1) = 1
  Temperature = {t_in_c:.10g}
End
Boundary Condition 2
  Target Boundaries(1) = 2
  outletmask = Logical True
End
Boundary Condition 3
  Target Boundaries(1) = 3
  Heat Flux = {flux_w_m2:.10g}
  outermask = Logical True
End
Boundary Condition 4
  Target Boundaries(1) = 5
  ifacemask = Logical True
End
"""


def write_cht_channel_case(
    case_dir: str,
    *,
    flux_w_m2: float = 10000.0,
    velocity_m_s: float = 0.001,
    t_in_c: float = 20.0,
    length_m: float = 0.1,
    fluid_height_m: float = 0.005,
    solid_thickness_m: float = 0.002,
    k_fluid: float = 0.6,
    rho_fluid: float = 1000.0,
    cp_fluid: float = 4180.0,
    k_solid: float = 1.0,
    nx: int = 80,
    ny_fluid: int = 10,
    ny_solid: int = 4,
    mesh_name: str = "chan",
    scalars: str = "cht.dat",
) -> dict:
    """Write a complete, runnable conjugate-channel Elmer case under ``case_dir``
    (mesh + ``case.sif`` + STARTINFO) and return it next to its exact oracle.

    Defaults are the live-validated water channel: cell Péclet
    ρ·c_p·U·Δx/k ≈ 8.7 (energy balance holds to ~0.5 %, solid drop to ~0.2 %).
    Returns {case_dir, sif, mesh_name, scalars, pe_cell, oracle: {t_out_c,
    dt_solid_k, …}}. Raises ValueError on degenerate inputs (via the mesh/sif
    writers) — including a cell Péclet above 25, where the stabilized advection
    visibly breaks the energy balance (~12 % under-collection at Pe_cell ≈ 90)."""
    pe_cell = rho_fluid * cp_fluid * velocity_m_s * (length_m / nx) / k_fluid
    if pe_cell > 25.0:
        raise ValueError(
            f"cell Péclet {pe_cell:.3g} > 25 — the SUPG-stabilized advection "
            "under-reports the energy balance there (measured ~12% at Pe_cell~90); "
            "raise nx / k_fluid or lower velocity_m_s")
    mesh_dir = os.path.join(case_dir, mesh_name)
    os.makedirs(mesh_dir, exist_ok=True)
    files = cht_channel_mesh_files(nx, ny_fluid, ny_solid,
                                   length_m, fluid_height_m, solid_thickness_m)
    for fname, text in files.items():
        with open(os.path.join(mesh_dir, fname), "w") as f:
            f.write(text)
    sif_text = cht_channel_sif(
        velocity_m_s=velocity_m_s, flux_w_m2=flux_w_m2, t_in_c=t_in_c,
        k_fluid=k_fluid, rho_fluid=rho_fluid, cp_fluid=cp_fluid, k_solid=k_solid,
        mesh_name=mesh_name, scalars=scalars)
    with open(os.path.join(case_dir, "case.sif"), "w") as f:
        f.write(sif_text)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w") as f:
        f.write("case.sif\n")
    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_name": mesh_name,
        "scalars": scalars,
        "pe_cell": round(pe_cell, 3),
        "oracle": cht_channel_oracle(
            flux_w_m2=flux_w_m2, length_m=length_m,
            fluid_height_m=fluid_height_m, velocity_m_s=velocity_m_s,
            t_in_c=t_in_c, rho=rho_fluid, cp=cp_fluid,
            solid_thickness_m=solid_thickness_m, k_solid=k_solid),
    }


def parse_cht_scalars(case_dir: str, scalars: str = "cht.dat") -> dict | None:
    """Read the SaveScalars output of the conjugate channel: the last row's three
    boundary means in mask order. Returns {t_outlet_mean_c, t_outer_mean_c,
    t_interface_mean_c, dt_solid_k (outer − interface)} or None when absent/
    unreadable (the solve failed)."""
    import glob
    path = os.path.join(case_dir, scalars)
    matches = sorted(glob.glob(path)) or sorted(glob.glob(path + "*"))
    data = next((m for m in [path, *matches]
                 if os.path.isfile(m) and not m.endswith(".names")), None)
    if data is None:
        return None
    rows = [r for r in open(data).read().splitlines() if r.strip()]
    if not rows:
        return None
    try:
        last = [float(v) for v in rows[-1].split()]
        return {
            "t_outlet_mean_c": last[_COL_OUTLET],
            "t_outer_mean_c": last[_COL_OUTER],
            "t_interface_mean_c": last[_COL_INTERFACE],
            "dt_solid_k": last[_COL_OUTER] - last[_COL_INTERFACE],
        }
    except (ValueError, IndexError):
        return None
