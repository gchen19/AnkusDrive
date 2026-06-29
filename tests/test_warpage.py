"""Injection-molding warpage (issue #113 Part B) — analytic-twin + deck + parser +
gate (no solver), plus a solver-backed thermo-elastic gate that skips when ``ccx``
is absent.

The no-solver half checks the closed-form free-plate bow, the through-thickness
field, the CalculiX ``.inp`` deck (3-2-1 dofs, material/step cards), the ``.frd``
parser on a synthetic block, and the gate's flatness pass/fail + faithfulness. The
solver-backed half builds a structured-hex plate mesh in pure Python (no FreeCAD)
and runs ccx: an asymmetric through-thickness field bows it to the analytic
curvature (directionally correct), a balanced field leaves it flat.
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import solvers  # noqa: E402
from driftpin.analysis import warpage as W  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402


# --- analytic twin (no solver) -----------------------------------------------

def test_analytic_bow_magnitude_and_sign():
    # κ = α·ΔT/h ; δ = κ·L²/8.  α=7e-5, ΔT=100, h=1, L=40 → κ=7e-3, δ=1.4 mm
    tw = W.free_plate_thermal_bow(span_mm=40, thickness_mm=1, dT_through_k=100,
                                  cte_per_k=7e-5)
    assert math.isclose(tw["curvature_per_mm"], 7e-3, rel_tol=1e-9)
    assert math.isclose(tw["bow_mm"], 1.4, rel_tol=1e-9)
    # sign tracks ΔT (hotter bottom → opposite-sign bow vs hotter top)
    flip = W.free_plate_thermal_bow(span_mm=40, thickness_mm=1, dT_through_k=-100,
                                    cte_per_k=7e-5)
    assert flip["bow_mm"] == -tw["bow_mm"]


def test_analytic_rejects_bad_geometry():
    for bad in (dict(span_mm=0, thickness_mm=1), dict(span_mm=40, thickness_mm=0)):
        try:
            W.free_plate_thermal_bow(dT_through_k=10, cte_per_k=7e-5, **bad)
            assert False, "expected ValueError"
        except ValueError:
            pass


# --- mesh geometry helpers (no solver) ---------------------------------------

def _grid_nodes(L=40.0, Wd=10.0, t=1.0, nx=2, ny=2, nz=1):
    """Tiny corner-only node dict {id:(x,y,z)} on an L×Wd×t grid (thinnest = z)."""
    nodes, nid = {}, 1
    for k in range(nz + 1):
        for j in range(ny + 1):
            for i in range(nx + 1):
                nodes[nid] = (L * i / nx, Wd * j / ny, t * k / nz)
                nid += 1
    return nodes


def test_thickness_axis_is_thinnest():
    assert W.thickness_axis(_grid_nodes(L=40, Wd=10, t=1)) == 2   # z thin
    # a part thin in y
    assert W.thickness_axis(_grid_nodes(L=40, Wd=1, t=10)) == 1


def test_pick_321_nodes_noncollinear_and_in_plane():
    nodes = _grid_nodes()
    a = W.pick_321_nodes(nodes)
    assert a["axis"] == 2 and set(a["in_plane"]) == {0, 1}
    ids = {a["a"], a["b"], a["c"]}
    assert len(ids) == 3, "the three anchors must be distinct"
    pa, pb, pc = nodes[a["a"]], nodes[a["b"]], nodes[a["c"]]
    # A→B spans the i axis, A→C spans the j axis ⇒ non-collinear
    assert pb[0] > pa[0] and abs(pb[1] - pa[1]) < 1e-9
    assert pc[1] > pa[1] and abs(pc[0] - pa[0]) < 1e-9
    assert math.isclose(a["span_mm"], 40.0)


def test_linear_through_thickness_field_is_antisymmetric():
    nodes = _grid_nodes(t=1, nz=1)
    temps = W.linear_through_thickness_temps(nodes, axis=2, dT_through_k=80,
                                             ref_temp_c=0.0)
    bot = [temps[n] for n, p in nodes.items() if p[2] == 0.0]
    top = [temps[n] for n, p in nodes.items() if p[2] == 1.0]
    assert math.isclose(max(bot), 40.0) and math.isclose(min(bot), 40.0)   # +ΔT/2
    assert math.isclose(max(top), -40.0) and math.isclose(min(top), -40.0)  # −ΔT/2


# --- the CalculiX deck (no solver) -------------------------------------------

def test_inp_deck_has_material_step_and_321_boundary():
    nodes = _grid_nodes()
    anchors = W.pick_321_nodes(nodes)
    txt = W.warpage_inp_text(
        mesh_include="mesh.inp",
        node_temps={n: 10.0 for n in nodes}, anchors=anchors,
        youngs_mpa=3200, poisson=0.35, cte_per_k=7e-5, ref_temp_c=50.0)
    for card in ("*INCLUDE, INPUT=mesh.inp", "*ELASTIC", "*EXPANSION, ZERO=50",
                 "*SOLID SECTION, ELSET=Evolumes, MATERIAL=resin",
                 "*INITIAL CONDITIONS, TYPE=TEMPERATURE", "*STATIC", "*BOUNDARY",
                 "*TEMPERATURE", "*NODE FILE"):
        assert card in txt, card
    # 3-2-1: corner A pins all of 1..3, B pins two dofs, C pins one (the thickness)
    lines = txt.splitlines()
    bidx = lines.index("*BOUNDARY")
    bcs = lines[bidx + 1:bidx + 5]
    assert bcs[0] == f"{anchors['a']}, 1, 3, 0.0"                 # A: all three
    # exactly 6 pinned dofs total across the four rows (3 + 2 + 1)
    pinned = 0
    for row in bcs:
        _, d0, d1, _ = [s.strip() for s in row.split(",")]
        pinned += int(d1) - int(d0) + 1
    assert pinned == 6


def test_write_case_requires_exactly_one_field_source():
    nodes = _grid_nodes()
    cd = tempfile.mkdtemp(prefix="warp_")
    try:
        for bad in (dict(), dict(dT_through_k=10, node_temps={1: 1.0})):
            try:
                W.write_warpage_case(cd, nodes=nodes, youngs_mpa=3200, poisson=0.35,
                                     cte_per_k=7e-5, **bad)
                assert False, "expected ValueError"
            except ValueError:
                pass
    finally:
        shutil.rmtree(cd, ignore_errors=True)


# --- the .frd parser (no solver) ---------------------------------------------

def _synthetic_disp_frd(rows):
    """A minimal ccx .frd with one DISP block; rows = [(node, ux, uy, uz), ...]."""
    out = [" -4  DISP        4    1"]
    for node, ux, uy, uz in rows:
        out.append(" -1" + f"{node:10d}" + f"{ux:12.5E}{uy:12.5E}{uz:12.5E}")
    out.append(" -3")
    return "\n".join(out) + "\n"


def test_parse_frd_picks_out_of_plane_peak():
    cd = tempfile.mkdtemp(prefix="warp_")
    try:
        frd = os.path.join(cd, "case.frd")
        # node 2 has the biggest |Uz|; node 3 the biggest total magnitude
        open(frd, "w").write(_synthetic_disp_frd([
            (1, 0.0, 0.0, 0.0),
            (2, 0.01, 0.02, 1.50),
            (3, 5.0, 0.0, 0.10),
        ]))
        r = W.parse_warp_frd(frd, axis_index=2)
        assert math.isclose(r["max_warp_mm"], 1.5, rel_tol=1e-4)
        assert math.isclose(r["max_disp_mm"], 5.001, rel_tol=1e-3)
        assert r["n_nodes"] == 3
        assert W.parse_warp_frd(os.path.join(cd, "nope.frd")) is None
    finally:
        shutil.rmtree(cd, ignore_errors=True)


# --- the gate (no solver) ----------------------------------------------------

def test_gate_flatness_pass_fail():
    big = W.warpage_gate({"max_warp_mm": 1.5, "max_disp_mm": 1.5}, span_mm=40)
    assert big["pass"] is False and big["score"] == 0.0 and big["warnings"]
    flat = W.warpage_gate({"max_warp_mm": 0.02, "max_disp_mm": 0.02}, span_mm=40)
    assert flat["pass"] is True and flat["score"] > 0.0 and not flat["warnings"]
    assert flat["fidelity"] == "solve"


def test_gate_faithfulness_against_analytic():
    # solved ~ analytic ⇒ faithful, no warning
    ok = W.warpage_gate({"max_warp_mm": 1.45}, span_mm=40, analytic_bow_mm=1.40)
    assert ok["warp_faithful"] is True
    # an order-of-magnitude miss ⇒ unfaithful warning (does NOT change pass/fail)
    bad = W.warpage_gate({"max_warp_mm": 14.0}, span_mm=40, analytic_bow_mm=1.40)
    assert bad["warp_faithful"] is False
    assert any("analytic" in w for w in bad["warnings"])


# --- solver-backed: ccx thermo-elastic (skips when ccx is absent) ------------

# Abaqus/CalculiX C3D8 corner order: bottom face CCW then top face CCW.
_HEX = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
        (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]


def _write_hex_plate_mesh(path, *, L=40.0, Wd=10.0, t=1.0, nx=20, ny=5, nz=2):
    """Write a structured C3D8I hex-plate mesh.inp (NSET=Nall, ELSET=Evolumes) and
    return {id:(x,y,z)}. C3D8I (incompatible modes) bends without shear locking, so a
    coarse plate still reproduces the thermal curvature."""
    def nid(i, j, k):
        return 1 + i + (nx + 1) * (j + (ny + 1) * k)
    nodes = {}
    for k in range(nz + 1):
        for j in range(ny + 1):
            for i in range(nx + 1):
                nodes[nid(i, j, k)] = (L * i / nx, Wd * j / ny, t * k / nz)
    lines = ["*Node, NSET=Nall"]
    for n in sorted(nodes):
        x, y, z = nodes[n]
        lines.append(f"{n}, {x:.6f}, {y:.6f}, {z:.6f}")
    lines.append("*Element, TYPE=C3D8I, ELSET=Evolumes")
    eid = 1
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                conn = [nid(i + dx, j + dy, k + dz) for dx, dy, dz in _HEX]
                lines.append(f"{eid}, " + ", ".join(str(c) for c in conn))
                eid += 1
    open(path, "w").write("\n".join(lines) + "\n")
    return nodes


def _run_ccx_warpage(cd, nodes, *, dT, cte=7e-5):
    built = W.write_warpage_case(cd, nodes=nodes, youngs_mpa=3200, poisson=0.35,
                                 cte_per_k=cte, dT_through_k=dT)
    ccx = solvers.ccx_bin()
    argv = [ccx] + built["argv"][1:]
    subprocess.run(argv, cwd=cd, capture_output=True, text=True, timeout=300)
    parsed = W.parse_warp_frd(os.path.join(cd, "case.frd"),
                              axis_index=built["axis_index"])
    return built, parsed


def test_ccx_warpage_matches_analytic_and_balanced_is_flat():
    if skip_heavy("CalculiX warpage"):
        return
    if solvers.ccx_bin() is None:
        print("    SKIP — CalculiX (ccx) not installed")
        return
    base = tempfile.mkdtemp(prefix="warp_ccx_")
    try:
        nodes = _write_hex_plate_mesh(os.path.join(base, "mesh_src.inp"))
        # asymmetric through-thickness field → bow ≈ analytic, directionally correct
        cd = os.path.join(base, "asym"); os.makedirs(cd)
        shutil.copy(os.path.join(base, "mesh_src.inp"), os.path.join(cd, "mesh.inp"))
        built, parsed = _run_ccx_warpage(cd, nodes, dT=100.0)
        assert parsed is not None, "ccx produced no displacement field"
        tw = W.free_plate_thermal_bow(span_mm=built["span_mm"],
                                      thickness_mm=built["thickness_mm"],
                                      dT_through_k=100.0, cte_per_k=7e-5)
        # within the thin-plate idealisation spread (a few % to ~50%)
        ratio = parsed["max_warp_mm"] / abs(tw["bow_mm"])
        assert 0.7 <= ratio <= 1.5, f"warp {parsed['max_warp_mm']} vs {tw['bow_mm']}"
        # the distortion is genuinely out-of-plane (warp ≈ total displacement)
        assert parsed["max_warp_mm"] >= 0.9 * parsed["max_disp_mm"]
        gate = W.warpage_gate(parsed, span_mm=built["span_mm"],
                              analytic_bow_mm=tw["bow_mm"])
        assert gate["warp_faithful"] is True

        # balanced (no through-thickness gradient) → essentially flat
        cd0 = os.path.join(base, "bal"); os.makedirs(cd0)
        shutil.copy(os.path.join(base, "mesh_src.inp"), os.path.join(cd0, "mesh.inp"))
        _, parsed0 = _run_ccx_warpage(cd0, nodes, dT=0.0)
        assert parsed0["max_warp_mm"] < 1e-3, parsed0
        flat_gate = W.warpage_gate(parsed0, span_mm=built["span_mm"])
        assert flat_gate["pass"] is True
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    for name in sorted(n for n in dir() if n.startswith("test_")):
        print(name)
        globals()[name]()
    print("OK")
