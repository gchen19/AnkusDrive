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
        with open(os.path.join(mesh_dir, fname), "w", encoding="utf-8") as f:
            f.write(text)
    sif_text = cht_channel_sif(
        velocity_m_s=velocity_m_s, flux_w_m2=flux_w_m2, t_in_c=t_in_c,
        k_fluid=k_fluid, rho_fluid=rho_fluid, cp_fluid=cp_fluid, k_solid=k_solid,
        mesh_name=mesh_name, scalars=scalars)
    with open(os.path.join(case_dir, "case.sif"), "w", encoding="utf-8") as f:
        f.write(sif_text)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w", encoding="utf-8") as f:
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
    rows = [r for r in open(data, encoding="utf-8").read().splitlines() if r.strip()]
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


# --- Tier B4: flow-coupled Graetz channel (SIMULATION_NEXT) ---------------------
#
# The plug-flow channel above keeps its gates h-free BY CONSTRUCTION; this case
# upgrades it to a TRUE Nusselt validation: Elmer FlowSolve computes the real
# laminar profile (gate 1: the parabola's u_max/u_mean = 3/2 exactly) and
# HeatSolver rides on it (Convection = Computed) between two isothermal walls.
# In the thermally developed region the mixing-cup temperature then obeys the
# exact decay law
#
#     d ln(T_wall - T_bulk)/dx = -Nu * k * P / (Dh * mdot * cp)
#
# whose Nu is the Graetz eigenvalue: parallel plates at constant wall
# temperature give Nu_T = 7.5407 (a SLUG profile would give pi^2 = 9.8696 —
# the discriminator that proves the profile coupling is real). This closes the
# loop with h_estimate: the same number an agent gets from the correlation
# screen is here measured from a meshed solve.

GRAETZ_NU_PLATES_T = 7.5407    # parallel plates, both walls isothermal
GRAETZ_NU_SLUG_T = 9.8696      # pi^2 — the plug-flow value the M6 model implies

_GRAETZ_RE_MAX = 400.0         # keep the channel laminar with margin


def graetz_channel_mesh_files(length_m: float, gap_m: float,
                              nx: int, ny: int) -> dict:
    """Native Elmer 2-D quad mesh of the open channel: tag 1 = inlet (x=0),
    tag 2 = outlet, tag 3 = both isothermal walls."""
    nnx, nny = nx + 1, ny + 1

    def nid(i, j):
        return j * nnx + i + 1

    def parent(i, j):
        return j * nx + i + 1

    nodes = []
    for j in range(nny):
        for i in range(nnx):
            nodes.append(
                f"{nid(i, j)} -1 {i * length_m / nx:.10g} {j * gap_m / ny:.10g} 0.0\n")
    elements = []
    eid = 0
    for j in range(ny):
        for i in range(nx):
            eid += 1
            elements.append(
                f"{eid} 1 404 {nid(i, j)} {nid(i + 1, j)} "
                f"{nid(i + 1, j + 1)} {nid(i, j + 1)}\n")
    boundary, bid = [], 0
    for j in range(ny):
        bid += 1
        boundary.append(f"{bid} 1 {parent(0, j)} 0 202 {nid(0, j)} {nid(0, j + 1)}\n")
        bid += 1
        boundary.append(f"{bid} 2 {parent(nx - 1, j)} 0 202 {nid(nx, j)} {nid(nx, j + 1)}\n")
    for i in range(nx):
        bid += 1
        boundary.append(f"{bid} 3 {parent(i, 0)} 0 202 {nid(i, 0)} {nid(i + 1, 0)}\n")
        bid += 1
        boundary.append(f"{bid} 3 {parent(i, ny - 1)} 0 202 {nid(i, ny)} {nid(i + 1, ny)}\n")
    header = f"{nnx * nny} {nx * ny} {bid}\n2\n202 {bid}\n404 {nx * ny}\n"
    return {"mesh.header": header, "mesh.nodes": "".join(nodes),
            "mesh.elements": "".join(elements), "mesh.boundary": "".join(boundary)}


def write_graetz_channel_case(
    case_dir: str,
    *,
    velocity_m_s: float = 0.025,
    gap_m: float = 0.01,
    length_m: float = 0.12,
    rho_fluid: float = 1000.0,
    mu_fluid: float = 0.02,
    k_fluid: float = 80.0,
    cp_fluid: float = 4000.0,
    t_in_c: float = 20.0,
    t_wall_c: float = 80.0,
    nx: int = 120,
    ny: int = 20,
    max_iterations: int = 30,
    mesh_name: str = "chan",
    output: str = "graetz",
) -> dict:
    """Write the flow-coupled Graetz channel: FlowSolve (Navier-Stokes) +
    HeatSolver with Convection = Computed, uniform inlet, no-slip isothermal
    walls. The writer polices the physics the fit depends on: Re < 400
    (laminar), both development lengths (0.05*Re*Dh and 0.05*Re*Pr*Dh) inside
    the first 45 % of the channel so the second-half fit window is developed,
    and cell Peclet U*dx/alpha <= 25 (the stabilized-advection envelope the M6
    case established). Returns {case_dir, sif, mesh_db, output, reynolds,
    prandtl, pe_cell, nu_exact, nu_slug, nx, ny, gap_m, length_m}."""
    if min(velocity_m_s, gap_m, length_m, rho_fluid, mu_fluid, k_fluid,
           cp_fluid) <= 0:
        raise ValueError("flow, geometry and material properties must be > 0")
    if t_wall_c == t_in_c:
        raise ValueError("t_wall_c must differ from t_in_c (no decay to fit)")
    if nx < 40 or ny < 10:
        raise ValueError("need nx >= 40 and ny >= 10 to resolve the profile")
    dh = 2.0 * gap_m
    re = rho_fluid * velocity_m_s * dh / mu_fluid
    if not 4.0 <= re <= _GRAETZ_RE_MAX:
        raise ValueError(f"Re = {re:.0f} outside the laminar channel envelope "
                         f"[4, {_GRAETZ_RE_MAX:.0f}]")
    alpha = k_fluid / (rho_fluid * cp_fluid)
    pr = mu_fluid * cp_fluid / k_fluid
    dev = 0.05 * re * dh * max(1.0, pr)
    if dev > 0.45 * length_m:
        raise ValueError(
            f"development length {dev:.3g} m exceeds 45% of the channel — "
            "lengthen it or drop Re/Pr so the fit window is developed")
    pe_cell = velocity_m_s * (length_m / nx) / alpha
    if pe_cell > 25.0:
        raise ValueError(f"cell Peclet {pe_cell:.1f} > 25 — refine nx or slow "
                         "the flow (stabilized advection leaks beyond it)")
    mesh_dir = os.path.join(case_dir, mesh_name)
    os.makedirs(mesh_dir, exist_ok=True)
    for name, content in graetz_channel_mesh_files(length_m, gap_m, nx, ny).items():
        with open(os.path.join(mesh_dir, name), "w", encoding="utf-8") as f:
            f.write(content)
    sif = f"""Header
  Mesh DB "." "{mesh_name}"
End
Simulation
  Coordinate System = Cartesian 2D
  Simulation Type = Steady State
  Steady State Max Iterations = {max_iterations}
  Output Intervals = 0
End
Body 1
  Equation = 1
  Material = 1
End
Material 1
  Density = {rho_fluid:.10g}
  Viscosity = {mu_fluid:.10g}
  Heat Conductivity = {k_fluid:.10g}
  Heat Capacity = {cp_fluid:.10g}
End
Equation 1
  Active Solvers(2) = 1 2
  Convection = Computed
  NS Convect = True
End
Solver 1
  Equation = Navier-Stokes
  Procedure = "FlowSolve" "FlowSolver"
  Variable = Flow Solution[Velocity:2 Pressure:1]
  Stabilize = True
  Nonlinear System Max Iterations = 30
  Nonlinear System Convergence Tolerance = 1.0e-7
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
  Steady State Convergence Tolerance = 1.0e-6
End
Solver 2
  Equation = Heat Equation
  Procedure = "HeatSolve" "HeatSolver"
  Variable = Temperature
  Stabilize = True
  Nonlinear System Max Iterations = 1
  Linear System Solver = Direct
  Linear System Direct Method = UMFPACK
  Steady State Convergence Tolerance = 1.0e-6
End
Solver 3
  Equation = ResultOutput
  Procedure = "ResultOutputSolve" "ResultOutputSolver"
  Output File Name = "{output}"
  Vtu Format = True
  Ascii Output = True
  Exec Solver = After Simulation
End
Boundary Condition 1
  Target Boundaries(1) = 1
  Velocity 1 = {velocity_m_s:.10g}
  Velocity 2 = 0.0
  Temperature = {t_in_c:.10g}
End
Boundary Condition 2
  Target Boundaries(1) = 2
  Velocity 2 = 0.0
End
Boundary Condition 3
  Target Boundaries(1) = 3
  Velocity 1 = 0.0
  Velocity 2 = 0.0
  Temperature = {t_wall_c:.10g}
End
"""
    with open(os.path.join(case_dir, "case.sif"), "w", encoding="utf-8") as f:
        f.write(sif)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w", encoding="utf-8") as f:
        f.write("case.sif\n")
    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_db": mesh_name,
        "output": output,
        "reynolds": re,
        "prandtl": pr,
        "pe_cell": pe_cell,
        "nu_exact": GRAETZ_NU_PLATES_T,
        "nu_slug": GRAETZ_NU_SLUG_T,
        "nx": nx,
        "ny": ny,
        "gap_m": gap_m,
        "length_m": length_m,
        "velocity_m_s": velocity_m_s,
        "rho_fluid": rho_fluid,
        "k_fluid": k_fluid,
        "cp_fluid": cp_fluid,
        "t_wall_c": t_wall_c,
    }


def fit_nusselt(xs, theta, *, dh: float, mdot_cp: float, k_fluid: float,
                perimeter: float) -> float | None:
    """Nu from the developed decay law: a linear fit of ln(theta) vs x (theta =
    T_wall - T_bulk > 0) has slope -Nu*k*P/(Dh*mdot*cp). Exact for a
    synthetic exponential — the standalone-testable core of the Graetz gate.
    Returns None when fewer than 4 usable points."""
    import math
    pts = [(x, math.log(t)) for x, t in zip(xs, theta) if t > 0]
    if len(pts) < 4:
        return None
    n = len(pts)
    sx = sum(x for x, _ in pts)
    sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts)
    sxy = sum(x * y for x, y in pts)
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    return -slope * dh * mdot_cp / (k_fluid * perimeter)


def parse_graetz_channel(
    case_dir: str,
    *,
    nx: int,
    ny: int,
    gap_m: float,
    length_m: float,
    rho_fluid: float,
    cp_fluid: float,
    k_fluid: float,
    t_wall_c: float,
    mesh_name: str = "chan",
    output: str = "graetz",
) -> dict | None:
    """Read the solved T and U fields (ASCII vtu) and extract the two gates:
    the mid-length velocity-profile ratio u_max/u_mean (parabola: 3/2 exactly)
    and the fitted developed Nusselt number, using the SOLVED mass flux in the
    decay law (physically consistent with the field the heat rode on). The fit
    window is the second half of the channel. Returns {u_max_over_mean,
    u_mean_solved, nu_fit, n_fit_points} or None."""
    import re as _re
    path = os.path.join(case_dir, mesh_name, f"{output}_t0001.vtu")
    if not os.path.exists(path):
        return None
    txt = open(path, encoding="utf-8").read()

    def field(name, ncomp):
        m = _re.search(rf'Name="{name}"[^>]*format="ascii"[^>]*>(.*?)</DataArray>',
                       txt, _re.S)
        if not m:
            return None
        vals = [float(v) for v in m.group(1).split()]
        if ncomp == 1:
            return vals
        return [tuple(vals[i:i + ncomp]) for i in range(0, len(vals), ncomp)]

    temp = field("temperature", 1)
    vel = field("velocity", 3)
    nnx, nny = nx + 1, ny + 1
    if not temp or not vel or len(temp) != nnx * nny:
        return None

    def u_x(i, j):
        return vel[j * nnx + i][0]

    # mid-length profile: parabola check + solved mean velocity (trapezoid)
    i_mid = nx // 2
    prof = [u_x(i_mid, j) for j in range(nny)]
    u_mean = sum((prof[j] + prof[j + 1]) / 2.0 for j in range(nny - 1)) / (nny - 1)
    if u_mean <= 0:
        return None
    ratio = max(prof) / u_mean

    def bulk(i):
        num = den = 0.0
        for j in range(nny - 1):
            u1, u2 = u_x(i, j), u_x(i, j + 1)
            t1, t2 = temp[j * nnx + i], temp[(j + 1) * nnx + i]
            num += (u1 * t1 + u2 * t2) / 2.0
            den += (u1 + u2) / 2.0
        return num / den if den > 0 else None

    xs, theta = [], []
    for i in range(nnx):
        x = i * length_m / nx
        if x < length_m / 2.0:
            continue
        tb = bulk(i)
        if tb is None:
            continue
        xs.append(x)
        theta.append(t_wall_c - tb)
    mdot_cp = rho_fluid * u_mean * gap_m * cp_fluid    # solved flux, per depth
    nu = fit_nusselt(xs, theta, dh=2.0 * gap_m, mdot_cp=mdot_cp,
                     k_fluid=k_fluid, perimeter=2.0)
    return {
        "u_max_over_mean": round(ratio, 5),
        "u_mean_solved": u_mean,
        "nu_fit": (round(nu, 4) if nu is not None else None),
        "n_fit_points": len(xs),
    }
