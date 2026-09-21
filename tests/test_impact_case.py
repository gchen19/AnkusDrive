"""Impact dynamics (issue #311) — mesh topology + deck + parsers + gate (no solver),
plus the solver-backed St-Venant gate that skips when ``ccx`` is absent.

The no-solver half checks boundary-face extraction and its outward normals on tets
and bricks, the floor brick's placement for an axis and a corner drop, the deck's
cards for both integrators, the ``.dat`` / ``.frd`` parsers on synthetic blocks, the
Newton's-law reduction on a known half-sine pulse, and that the gate FAILS a
truncated or energy-growing run.

The solver-backed half flies a ν = 0 steel bar into the floor through real penalty
contact and holds ccx to the exact 1-D answers — face stress ρ·c₀·v₀, contact
duration 2L/c₀, restitution 1 — then repeats it past yield against the bilinear
plastic-wave cap, shows a run cut short is rejected, lands a stiff block on a soft
contact to recover the ``drop_impact`` screen's own G = 2h/d, and drops a cube on its
corner — the case face-to-face contact falls straight through.
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive import solvers  # noqa: E402
from ankusdrive.analysis import impact as I  # noqa: E402
from ankusdrive.analysis import impact_case as C  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402


def _raises(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return True
    return False


# --- the closed-form twin ---------------------------------------------------------

def test_bar_impact_elastic_is_rho_c_v():
    r = I.bar_impact(1.0, 100.0, youngs_gpa=210, density_kg_m3=7850, area_mm2=1.0)
    c0 = math.sqrt(210e9 / 7850)
    assert math.isclose(r["wave_speed_m_s"], c0, rel_tol=1e-6)
    assert math.isclose(r["stress_mpa"], 7850 * c0 * 1.0 / 1e6, rel_tol=1e-6)
    assert math.isclose(r["force_n"], r["stress_mpa"], rel_tol=1e-9)      # 1 mm²
    assert math.isclose(r["contact_duration_ms"], 2 * 0.1 / c0 * 1e3, rel_tol=1e-5)
    assert r["rebound_velocity_m_s"] == 1.0 and r["plastic"] is False
    assert r["fidelity"] == "exact" and r["escalate_to"] == "impact_dynamics_submit"
    # stress is linear in speed and blind to length
    assert math.isclose(I.bar_impact(2.0, 5.0, youngs_gpa=210, density_kg_m3=7850)
                        ["stress_mpa"], 2 * r["stress_mpa"], rel_tol=1e-6)


def test_bar_impact_plastic_wave_cap():
    kw = dict(youngs_gpa=210, density_kg_m3=7850, yield_mpa=250)
    c0 = math.sqrt(210e9 / 7850)
    v_y = 250e6 / (7850 * c0)
    below = I.bar_impact(0.9 * v_y, 100, **kw)
    assert below["plastic"] is False and below["stress_mpa"] < 250
    flat = I.bar_impact(10.0, 100, **kw)                     # perfectly plastic
    assert flat["plastic"] and math.isclose(flat["stress_mpa"], 250.0)
    hard = I.bar_impact(10.0, 100, tangent_mpa=2100, **kw)
    cp = math.sqrt(2100e6 / 7850)
    assert math.isclose(hard["stress_mpa"], 250 + 7850 * cp * (10 - v_y) / 1e6,
                        rel_tol=1e-4)
    assert hard["stress_mpa"] < hard["elastic_stress_mpa"]
    assert hard["contact_duration_ms"] is None and not hard["valid_range_ok"]


def test_bar_impact_material_and_bad_input():
    r = I.bar_impact(1.0, 50, material="abs")
    assert r["wave_speed_m_s"] > 1000 and r["yield_velocity_m_s"] > 0
    assert _raises(lambda: I.bar_impact(0, 100, youngs_gpa=1, density_kg_m3=1))
    assert _raises(lambda: I.bar_impact(1, 100, youngs_gpa=1))          # no density
    assert _raises(lambda: I.bar_impact(1, 100, youngs_gpa=1, density_kg_m3=1e3,
                                        yield_mpa=1, tangent_mpa=2e3))  # E_t >= E


def test_drop_screen_escalates_to_the_solve():
    assert I.drop_impact(1000, crush_distance_mm=10)["escalate_to"] == \
        "impact_dynamics_submit"


# --- mesh topology ------------------------------------------------------------------

_HEX = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
        (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]


def _write_bar_mesh(path, *, L=100.0, a=1.0, nel=100, etype="C3D8"):
    """A 1×1-element-section bar along x as a FreeCAD-style mesh.inp."""
    def nid(i, j, k):
        return 1 + i * 4 + j * 2 + k
    lines = ["*Node, NSET=Nall"]
    for i in range(nel + 1):
        for j in range(2):
            for k in range(2):
                lines.append(f"{nid(i, j, k)}, {L * i / nel:.6f}, {a * j}, {a * k}")
    lines.append(f"*Element, TYPE={etype}, ELSET=Evolumes")
    for i in range(nel):
        conn = [nid(i + dx, dy, dz) for dx, dy, dz in _HEX]
        lines.append(f"{i + 1}, " + ", ".join(map(str, conn)))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_mesh_volume_and_boundary_faces_brick():
    d = tempfile.mkdtemp(prefix="impact_mesh_")
    try:
        p = os.path.join(d, "mesh.inp")
        _write_bar_mesh(p, L=10.0, a=2.0, nel=5)
        m = C.parse_mesh_inp(p)
        assert m["etype"] == "C3D8" and m["elset"] == "Evolumes" and m["nset"] == "Nall"
        assert len(m["nodes"]) == 24 and len(m["elements"]) == 5
        assert math.isclose(C.mesh_volume(m["nodes"], m["elements"], "C3D8"), 40.0)
        faces = C.boundary_faces(m["nodes"], m["elements"], "C3D8")
        assert len(faces) == 5 * 4 + 2                      # 4 sides per cell + 2 ends
        down = [(e, f) for e, f, n in faces if C._dot(n, (-1, 0, 0)) > 0.5]
        assert down == [(1, 6)]                             # the x=0 end is cell 1, S6
        assert all(math.isclose(C._dot(n, n), 1.0) for _, _, n in faces)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_boundary_faces_tet_normals_point_out_and_rows_wrap():
    d = tempfile.mkdtemp(prefix="impact_tet_")
    try:
        p = os.path.join(d, "mesh.inp")
        # one C3D10 whose connectivity wraps a line, as FreeCAD writes it
        Path(p).write_text(
            "*Node, NSET=Nall\n1, 0,0,0\n2, 1,0,0\n3, 0,1,0\n4, 0,0,1\n"
            "5, .5,0,0\n6, .5,.5,0\n7, 0,.5,0\n8, 0,0,.5\n9, .5,0,.5\n10, 0,.5,.5\n"
            "*Element, TYPE=C3D10, ELSET=Evolumes\n1, 1, 2, 3, 4, 5, 6,\n7, 8, 9, 10\n",
            encoding="utf-8")
        m = C.parse_mesh_inp(p)
        assert m["elements"][1] == list(range(1, 11))
        assert math.isclose(C.mesh_volume(m["nodes"], m["elements"], "C3D10"), 1 / 6)
        faces = C.boundary_faces(m["nodes"], m["elements"], "C3D10")
        assert len(faces) == 4
        cen = (0.25, 0.25, 0.25)
        by_face = {f: n for _, f, n in faces}
        assert C._dot(by_face[1], (0, 0, -1)) > 0.99       # S1 = 1-2-3, the z=0 face
        for _, f, n in faces:
            fc = [sum(m["nodes"][m["elements"][1][i]][k] for i in C._TET_FACES[f - 1]) / 3
                  for k in range(3)]
            assert C._dot(n, C._sub(fc, cen)) > 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_direction_and_floor_for_axis_and_corner_drops():
    assert C.drop_direction("-z") == (0.0, 0.0, -1.0)
    assert C.drop_direction("+X") == (1.0, 0.0, 0.0)
    corner = C.drop_direction((-1, -1, -1))
    assert math.isclose(C._dot(corner, corner), 1.0)
    assert _raises(lambda: C.drop_direction("down"))
    assert _raises(lambda: C.drop_direction((0, 0, 0)))
    cube = {i + 1: tuple(float(c) for c in p) for i, p in enumerate(_HEX)}
    for d in ((0.0, 0.0, -1.0), corner):
        fl = C.floor_block(cube, d, gap_mm=0.5)
        near, far = fl["coords"][:4], fl["coords"][4:]
        s_max = max(C._dot(p, d) for p in cube.values())
        # the near face is a plane normal to d, exactly gap beyond the part
        assert all(math.isclose(C._dot(p, d), s_max + 0.5, abs_tol=1e-9) for p in near)
        assert all(C._dot(p, d) > s_max + 0.5 for p in far)
        # right-handed brick (positive Jacobian): (p2-p1)x(p4-p1) points to the far face
        n = C._cross(C._sub(near[1], near[0]), C._sub(near[3], near[0]))
        assert C._dot(n, d) > 0
    assert C.floor_block(cube, corner, gap_mm=0.0)["lowest_node"] == 1   # (0,0,0) strikes
    assert math.isclose(C.floor_block(cube, corner, gap_mm=0.0)["extent_mm"], math.sqrt(3))


# --- the deck ---------------------------------------------------------------------

def _bar_case(d, **kw):
    _write_bar_mesh(os.path.join(d, "mesh.inp"), etype=kw.pop("etype", "C3D8"),
                    nel=kw.pop("nel", 100))
    mesh = C.parse_mesh_inp(os.path.join(d, "mesh.inp"))
    args = dict(youngs_mpa=210000.0, poisson=0.0, density_kg_m3=7850.0,
                velocity_m_s=1.0, direction="-x", gravity=False, gap_mm=0.005)
    args.update(kw)
    return C.write_impact_case(d, mesh=mesh, **args)


def test_deck_cards_implicit_and_explicit():
    d = tempfile.mkdtemp(prefix="impact_deck_")
    try:
        b = _bar_case(d)
        t = Path(d, "case.inp").read_text(encoding="utf-8")
        assert "*INCLUDE, INPUT=mesh.inp" in t
        assert "*CONTACT PAIR, INTERACTION=floor, TYPE=SURFACE TO SURFACE" in t
        assert "\n1, S6\n" in t and b["n_slave_faces"] == 1      # only the struck end
        assert "Nall, 1, -1000" in t and "Nall, 2," not in t     # velocity along -x only
        assert "Nfloor, 1, 3, 0." in t and "*PLASTIC" not in t and "GRAV" not in t
        # ONE output cadence — ccx lets the last card win for .dat and .frd alike.
        # Implicit is ADAPTIVE by default: a flat face landing at once needs cutbacks.
        assert b["adaptive"] and t.count("TIME POINTS=Tout") == 3
        assert "\n*DYNAMIC\n" in t and "FREQUENCY" not in t
        assert b["samples"] == 400 and C.default_samples(20000) == 100
        # the minimum increment is 1/50 of the step: ccx pins the step there while a
        # contact closes, and at 1e-7 a node-to-face strike never arrives
        step = [float(v) for v in t.split("*DYNAMIC\n")[1].splitlines()[0].split(",")]
        assert math.isclose(step[2], step[0] / 50, rel_tol=1e-6) and step[0] < step[3]
        # ... and a time_step_s opts in to the fixed step, which refuses time points
        fx = _bar_case(d, time_step_s=b["duration_s"] / 1000)
        tf = Path(d, "case.inp").read_text(encoding="utf-8")
        assert not fx["adaptive"] and tf.count("FREQUENCY=2") == 3
        assert "*DYNAMIC, DIRECT\n" in tf and "TIME POINTS" not in tf
        assert C.default_samples(10 ** 6) == 40
        assert math.isclose(b["mass_t"], 7.85e-9 * 100.0, rel_tol=1e-9)
        assert math.isclose(b["contact_stiffness_mpa_mm"], 25 * 210000.0)
        c0 = math.sqrt(210000.0 / 7.85e-9)
        assert math.isclose(b["duration_s"], 0.005 / 1000 + 20 * 100 / c0)  # 10 round trips
        assert math.isclose(_bar_case(d, gap_mm=None)["gap_mm"], 0.1)  # 0.1 % of 100 mm

        e = _bar_case(d, method="explicit", etype="C3D8R",
                      yield_mpa=250.0, tangent_mpa=2100.0, gravity=True)
        t = Path(d, "case.inp").read_text(encoding="utf-8")
        assert "*DYNAMIC, EXPLICIT, DIRECT" in t and "TYPE=C3D8R, ELSET=Efloor" in t
        # ccx refuses *TIME POINTS under DIRECT — a fixed step counts increments instead
        assert "TIME POINTS" not in t and "TOTALS=ONLY, FREQUENCY=" in t
        # 1 mm cells, K = 5E: the penalty spring (2·√(ρh/2K)) governs, not h/c_d
        st = e["time_step"]
        assert st["governs"] == "contact" and st["contact_s"] < st["cfl_s"]
        assert math.isclose(st["contact_s"], 2 * math.sqrt(7.85e-9 / (2 * 5 * 210000.0)))
        assert math.isclose(st["cfl_s"], 1.0 / c0) and e["time_step_s"] == 0.5 * st["contact_s"]
        assert "*PLASTIC\n250, 0.\n" in t and "GRAV, 9806.65, -1, 0, 0" in t
        h = 210000.0 * 2100.0 / (210000.0 - 2100.0)
        assert f"{250 + h:.9g}, 1." in t
        assert e["plastic"] and math.isclose(e["contact_stiffness_mpa_mm"], 5 * 210000.0)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_contact_formulation_follows_the_strike():
    """Neither ccx penalty formulation covers both ends (measured): face-to-face never
    engages a cube corner, node-to-face locks up on a broad flat landing."""
    assert C.pick_contact(1.0) == "face" and C.pick_contact(0.7072) == "face"   # <= 45°
    assert C.pick_contact(0.5774) == "node"                                   # corner
    d = tempfile.mkdtemp(prefix="impact_contact_")
    try:
        _write_cube_mesh(os.path.join(d, "mesh.inp"), n=2)
        mesh = C.parse_mesh_inp(os.path.join(d, "mesh.inp"))
        kw = dict(mesh=mesh, youngs_mpa=2300.0, poisson=0.35, density_kg_m3=1050.0,
                  velocity_m_s=4.43)
        flat = C.write_impact_case(d, direction="-z", **kw)
        assert flat["contact"] == "face" and flat["strike_alignment"] == 1.0
        assert "TYPE=SURFACE TO SURFACE" in Path(d, "case.inp").read_text(encoding="utf-8")
        assert flat["adaptive"]                      # face contact <-> adaptive stepping
        corner = C.write_impact_case(d, direction=(-1, -1, -1), **kw)
        assert corner["contact"] == "node"
        # node contact <-> a fixed step: ccx's adaptive impact rules stall on it
        assert not corner["adaptive"]
        assert math.isclose(corner["time_step_s"], corner["duration_s"] / 1000)
        assert math.isclose(corner["strike_alignment"], 1 / math.sqrt(3), abs_tol=1e-4)
        assert "TYPE=NODE TO SURFACE" in Path(d, "case.inp").read_text(encoding="utf-8")
        assert C.write_impact_case(d, direction="-z", contact="node", **kw)["contact"] == "node"
        assert _raises(lambda: C.write_impact_case(d, contact="glue", **kw))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_deck_rejects_what_cannot_run():
    d = tempfile.mkdtemp(prefix="impact_bad_")
    try:
        # incompatible-mode / second-order meshes cannot go explicit (lumped mass)
        assert _raises(lambda: _bar_case(d, method="explicit", etype="C3D8I"))
        assert _raises(lambda: _bar_case(d, method="rk4"))
        assert _raises(lambda: _bar_case(d, velocity_m_s=0))
        assert _raises(lambda: _bar_case(d, yield_mpa=250, tangent_mpa=3e5))
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- parsers + reduction + gate ---------------------------------------------------

def _half_sine_history(*, m=1e-3, v0=1000.0, T=1e-3, n=400, e=1.0, t_end=2e-3):
    """A half-sine contact pulse whose impulse is m·v0·(1+e), then free flight."""
    fp = m * v0 * (1 + e) * math.pi / (2 * T)
    ts = [t_end * i / n for i in range(n + 1)]
    f = [fp * math.sin(math.pi * t / T) if t < T else 0.0 for t in ts]
    ke0 = 0.5 * m * v0 * v0
    return {"t": ts, "force_n": f, "strain_mj": [0.0] * len(ts),
            "kinetic_mj": [ke0] + [ke0 * e * e] * n}, fp


def test_reduce_recovers_newtons_law_on_a_half_sine():
    hist, fp = _half_sine_history(e=0.8)
    r = C.reduce_impact(hist, mass_t=1e-3, velocity_mm_s=1000.0, gravity=False)
    assert r["engagement_lag_mm"] is None                   # no gap given, no claim
    assert math.isclose(r["peak_force_n"], fp, rel_tol=1e-4)
    assert math.isclose(r["peak_g"], fp / (1e-3 * C.G0_MM_S2), rel_tol=1e-3)
    assert math.isclose(r["restitution"], 0.8, abs_tol=2e-3)
    # a lone contact-onset spike moves the raw peak, not the G the verdict reads
    spiked = dict(hist, force_n=list(hist["force_n"]))
    spiked["force_n"][3] = 5 * fp
    rs = C.reduce_impact(spiked, mass_t=1e-3, velocity_mm_s=1000.0, gravity=False)
    assert math.isclose(rs["peak_force_n"], 5 * fp, rel_tol=1e-6)
    assert math.isclose(rs["peak_g"], r["peak_g"], rel_tol=1e-3)
    assert math.isclose(r["contact_duration_s"], 1e-3, rel_tol=0.03)
    assert r["separated"] and r["arrested"] and r["mass_check"] == 1.0
    # ... and ccx's ½mv² is not read off a sample already in contact
    late = {k: v[5:] for k, v in hist.items()}
    assert C.reduce_impact(late, mass_t=1e-3, velocity_mm_s=1000.0,
                           gravity=False)["mass_check"] is None
    assert math.isclose(r["energy_end_ratio"], 0.64, abs_tol=1e-3)


def test_gate_fails_truncated_and_unstable_runs_and_applies_criteria():
    hist, _ = _half_sine_history()
    full = C.reduce_impact(hist, mass_t=1e-3, velocity_mm_s=1000.0, gravity=False)
    assert C.impact_gate(full)["pass"] is True and C.impact_gate(full)["score"] == 1.0

    cut = {k: v[:60] for k, v in hist.items()}              # stop while still closing
    trunc = C.reduce_impact(cut, mass_t=1e-3, velocity_mm_s=1000.0, gravity=False)
    g = C.impact_gate(trunc)
    assert trunc["arrested"] is False and g["pass"] is False and g["score"] == 0.0
    assert any("duration_s" in w for w in g["warnings"])

    late = C.impact_gate(dict(full, engagement_lag_mm=0.8), extent_mm=10.0)
    assert late["pass"] and any("char_length_mm" in w for w in late["warnings"])
    grown = dict(full, energy_end_ratio=1.4)
    assert C.impact_gate(grown)["checks"]["energy_bounded"] is False
    assert C.impact_gate(dict(full, mass_check=1e3))["pass"] is False   # unit slip

    stress = {"peak_von_mises_mpa": 300.0, "location_mm": [0, 0, 0]}
    over = C.impact_gate(full, stress, yield_mpa=250.0)
    assert over["pass"] is False and over["utilisation"]["stress"] == 1.2
    # the same stress in a PLASTIC run is the model working, not a failure
    assert C.impact_gate(full, stress, yield_mpa=250.0, plastic=True)["pass"] is True
    lim = C.impact_gate(full, deceleration_limit_g=full["peak_g"] * 2)
    assert lim["pass"] and math.isclose(lim["score"], 0.5, abs_tol=1e-3)
    assert C.impact_gate(full, deceleration_limit_g=full["peak_g"] / 2)["pass"] is False


def test_parse_dat_and_frd_synthetic():
    d = tempfile.mkdtemp(prefix="impact_parse_")
    try:
        dat = os.path.join(d, "case.dat")
        rows = []
        for i, (t, fx) in enumerate(((0.0, 0.0), (1e-6, 12.5), (2e-6, 40.0), (3e-6, 0.0))):
            rows += [f"\n total force (fx,fy,fz) for set NFLOOR and time  {t:.7E}\n",
                     f"\n       {fx:.6E}  0.000000E+00  0.000000E+00\n",
                     f"\n total internal energy for set EVOLUMES and time  {t:.7E}\n",
                     f"\n        {0.1 * i:.6E}\n",
                     f"\n total kinetic energy for set EVOLUMES and time  {t:.7E}\n",
                     f"\n        {1.0 - 0.1 * i:.6E}\n"]
        Path(dat).write_text("".join(rows), encoding="utf-8")
        h = C.parse_impact_dat(dat, (-1.0, 0.0, 0.0))
        # the part pushes the floor along d = -x; ccx prints the support reaction (+x)
        assert h["force_n"] == [0.0, 12.5, 40.0, 0.0] and h["t"][2] == 2e-6
        assert h["strain_mj"][3] == 0.3 and math.isclose(h["kinetic_mj"][3], 0.7)
        assert C.parse_impact_dat(os.path.join(d, "absent.dat"), (1, 0, 0)) is None

        def rec(n, vals):
            return " -1" + f"{n:10d}" + "".join(f"{v:12.5E}" for v in vals) + "\n"
        frd = os.path.join(d, "case.frd")
        Path(frd).write_text(
            "    2C                             3                                     1\n"
            + rec(1, (0, 0, 0)) + rec(2, (5, 6, 7)) + rec(9, (1, 1, 1)) + " -3\n"
            "  100CL  101 1.00000E-06           3                     0    1           1\n"
            " -4  STRESS      6    1\n"
            + rec(1, (10, 0, 0, 0, 0, 0)) + rec(2, (100, 0, 0, 0, 0, 0))
            + rec(9, (900, 0, 0, 0, 0, 0)) + " -3\n"
            "  100CL  102 2.00000E-06           3                     0    1           1\n"
            " -4  STRESS      6    1\n"
            + rec(1, (10, 0, 0, 0, 0, 0)) + rec(2, (0, 0, 0, 200, 0, 0)) + " -3\n",
            encoding="utf-8")
        s = C.parse_peak_stress_frd(frd, exclude_nodes=(9,))    # 9 = a floor node
        assert s["node"] == 2 and s["frames"] == 2 and s["time_s"] == 2e-6
        assert math.isclose(s["peak_von_mises_mpa"], 200 * math.sqrt(3), rel_tol=1e-4)
        assert s["location_mm"] == [5.0, 6.0, 7.0]
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- the live St-Venant gate --------------------------------------------------------

def _solve(d, **kw):
    built = _bar_case(d, **kw)
    ccx = solvers.ccx_bin()
    subprocess.run([ccx] + built["argv"][1:], cwd=d, capture_output=True, text=True,
                   timeout=900)
    hist = C.parse_impact_dat(os.path.join(d, "case.dat"), tuple(built["direction"]))
    assert hist is not None, "ccx produced no floor-reaction history"
    red = C.reduce_impact(hist, mass_t=built["mass_t"],
                          velocity_mm_s=built["velocity_mm_s"], gravity=False)
    return built, hist, red


def _plateau(hist, t0, t1):
    v = [f for t, f in zip(hist["t"], hist["force_n"]) if t0 < t < t1]
    return sum(v) / len(v)


def test_ccx_bar_impact_lands_on_st_venant():
    if skip_heavy("CalculiX impact dynamics"):
        return
    if solvers.ccx_bin() is None:
        print("    SKIP — CalculiX (ccx) not installed")
        return
    base = tempfile.mkdtemp(prefix="impact_ccx_")
    try:
        twin = I.bar_impact(1.0, 100.0, youngs_gpa=210, density_kg_m3=7850, area_mm2=1.0)
        t_c = twin["contact_duration_ms"] * 1e-3
        d = os.path.join(base, "elastic"); os.makedirs(d)
        built, hist, red = _solve(d, duration_s=1.7 * t_c, time_step_s=t_c / 200)
        t0 = red["contact_start_s"]
        ratio_f = _plateau(hist, t0 + 0.25 * t_c, t0 + 0.75 * t_c) / twin["force_n"]
        assert abs(ratio_f - 1.0) < 0.02, f"face force {ratio_f:.4f} x rho*c0*v0*A"
        assert abs(red["contact_duration_s"] / t_c - 1.0) < 0.03, red
        assert red["separated"] and 0.95 <= red["restitution"] <= 1.01, red
        assert 0.95 <= red["energy_end_ratio"] <= 1.01, red
        assert abs(red["mass_check"] - 1.0) < 1e-3, red
        stress = C.parse_peak_stress_frd(os.path.join(d, "case.frd"),
                                         exclude_nodes=built["floor_nodes"])
        # the FIELD peak rides ~20 % over ρ·c₀·v₀: a step wave front rings (Gibbs) in any
        # Newmark-family scheme. The face force above is the exact check; this one only
        # holds the stress read-out to its stated band.
        assert 0.95 < stress["peak_von_mises_mpa"] / twin["stress_mpa"] < 1.25, stress
        gate = C.impact_gate(red, stress, yield_mpa=250.0)
        assert gate["pass"] is True, gate

        # a run cut off mid-contact must be REJECTED, not reported as a gentle drop
        d2 = os.path.join(base, "cut"); os.makedirs(d2)
        _, _, red2 = _solve(d2, duration_s=0.4 * t_c, time_step_s=t_c / 200)
        g2 = C.impact_gate(red2)
        assert red2["arrested"] is False and g2["pass"] is False, (red2, g2)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_ccx_bar_impact_past_yield_caps_at_the_plastic_wave():
    if skip_heavy("CalculiX impact dynamics (plastic)"):
        return
    if solvers.ccx_bin() is None:
        print("    SKIP — CalculiX (ccx) not installed")
        return
    base = tempfile.mkdtemp(prefix="impact_ccx_pl_")
    try:
        twin = I.bar_impact(10.0, 100.0, youngs_gpa=210, density_kg_m3=7850,
                            area_mm2=1.0, yield_mpa=250.0, tangent_mpa=2100.0)
        assert twin["plastic"] and twin["stress_mpa"] < 0.7 * twin["elastic_stress_mpa"]
        _, hist, red = _solve(base, nel=50, velocity_m_s=10.0, yield_mpa=250.0,
                              tangent_mpa=2100.0, duration_s=4e-5, time_step_s=2e-7)
        t0 = red["contact_start_s"]
        ratio = _plateau(hist, t0 + 8e-6, t0 + 3e-5) / twin["force_n"]
        assert abs(ratio - 1.0) < 0.03, f"plastic face force {ratio:.4f} x twin"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _write_cube_mesh(path, *, a=10.0, n=2):
    """An n×n×n C3D8 cube of side ``a`` as a FreeCAD-style mesh.inp."""
    def nid(i, j, k):
        return 1 + i + (n + 1) * (j + (n + 1) * k)
    lines = ["*Node, NSET=Nall"]
    for k in range(n + 1):
        for j in range(n + 1):
            for i in range(n + 1):
                lines.append(f"{nid(i, j, k)}, {a * i / n}, {a * j / n}, {a * k / n}")
    lines.append("*Element, TYPE=C3D8, ELSET=Evolumes")
    eid = 1
    for k in range(n):
        for j in range(n):
            for i in range(n):
                conn = [nid(i + dx, j + dy, k + dz) for dx, dy, dz in _HEX]
                lines.append(f"{eid}, " + ", ".join(map(str, conn)))
                eid += 1
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ccx_soft_landing_recovers_the_drop_screen():
    """The Tier-A screen and the solve must tell one story. A stiff block on a SOFT
    linear contact is the screen's 'linear_spring' cushion exactly: crush
    d = v·√(m/k), peak G = 2h/d, a half-sine pulse lasting π·d/v."""
    if skip_heavy("CalculiX impact dynamics (screen cross-check)"):
        return
    if solvers.ccx_bin() is None:
        print("    SKIP — CalculiX (ccx) not installed")
        return
    base = tempfile.mkdtemp(prefix="impact_ccx_soft_")
    try:
        _write_cube_mesh(os.path.join(base, "mesh.inp"))
        mesh = C.parse_mesh_inp(os.path.join(base, "mesh.inp"))
        v, crush = 1.0, 0.1                                 # m/s, mm
        m_t = 7.85e-9 * 1000.0
        k_n_mm = m_t * (v * 1e3) ** 2 / crush ** 2          # spring that stops it in d
        h_mm = (v * 1e3) ** 2 / (2 * C.G0_MM_S2)
        screen = I.drop_impact(h_mm, crush_distance_mm=crush, pulse="linear_spring")
        built = C.write_impact_case(
            base, mesh=mesh, youngs_mpa=210000.0, poisson=0.3, density_kg_m3=7850.0,
            velocity_m_s=v, direction="-z", gravity=False, gap_mm=0.005,
            contact_stiffness_mpa_mm=k_n_mm / 100.0,        # 10×10 mm face
            duration_s=5e-4, samples=200)                   # the DEFAULT, adaptive path
        assert built["adaptive"]
        subprocess.run([solvers.ccx_bin()] + built["argv"][1:], cwd=base,
                       capture_output=True, text=True, timeout=900)
        hist = C.parse_impact_dat(os.path.join(base, "case.dat"),
                                  tuple(built["direction"]))
        assert hist is not None, "ccx produced no floor-reaction history"
        red = C.reduce_impact(hist, mass_t=built["mass_t"],
                              velocity_mm_s=built["velocity_mm_s"], gravity=False)
        assert abs(red["peak_g"] / screen["g_peak"] - 1.0) < 0.03, (red, screen)
        t_pulse = math.pi * crush / (v * 1e3)
        assert abs(red["contact_duration_s"] / t_pulse - 1.0) < 0.06, red
        assert red["separated"] and 0.97 <= red["restitution"] <= 1.01, red
        assert C.impact_gate(red, deceleration_limit_g=1.2 * screen["g_peak"])["pass"]
        assert not C.impact_gate(red, deceleration_limit_g=0.8 * screen["g_peak"])["pass"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_ccx_corner_drop_engages_and_rebounds():
    """The corner drop the issue asked for. No closed form — what is held is that the
    contact ENGAGES at first touch (face-to-face contact never does: the part falls
    through), the body leaves again without gaining energy, and the worst stress is
    found at the corner that struck."""
    if skip_heavy("CalculiX impact dynamics (corner drop)"):
        return
    if solvers.ccx_bin() is None:
        print("    SKIP — CalculiX (ccx) not installed")
        return
    base = tempfile.mkdtemp(prefix="impact_ccx_corner_")
    try:
        _write_cube_mesh(os.path.join(base, "mesh.inp"), n=4)
        mesh = C.parse_mesh_inp(os.path.join(base, "mesh.inp"))
        built = C.write_impact_case(
            base, mesh=mesh, youngs_mpa=2300.0, poisson=0.35, density_kg_m3=1050.0,
            velocity_m_s=4.43, direction=(-1, -1, -1), gravity=False, gap_mm=0.05,
            duration_s=1.5e-3, samples=200)
        assert built["contact"] == "node" and built["lowest_node"] == 1
        subprocess.run([solvers.ccx_bin()] + built["argv"][1:], cwd=base,
                       capture_output=True, text=True, timeout=1500)
        hist = C.parse_impact_dat(os.path.join(base, "case.dat"),
                                  tuple(built["direction"]))
        assert hist is not None, "ccx produced no floor-reaction history"
        red = C.reduce_impact(hist, mass_t=built["mass_t"],
                              velocity_mm_s=built["velocity_mm_s"], gravity=False,
                              gap_mm=built["gap_mm"])
        assert red["peak_force_n"] > 0 and red["engagement_lag_mm"] < 0.1, red
        assert red["arrested"] and red["separated"], red
        # a point penalty contact under HHT-α can hand back a few % too much; the gate's
        # own tolerance (5 %) is the line, and it is held here too
        assert 0.5 < red["restitution"] <= 1.05 and red["energy_end_ratio"] <= 1.05, red
        assert C.impact_gate(red)["pass"], red
        assert abs(red["mass_check"] - 1.0) < 1e-3, red
        stress = C.parse_peak_stress_frd(os.path.join(base, "case.frd"),
                                         exclude_nodes=built["floor_nodes"])
        assert stress["node"] == 1 and stress["location_mm"] == [0.0, 0.0, 0.0], stress
        # far above the 1-D ρ·c₀·v₀: a point strike concentrates what a flat one spreads
        twin = I.bar_impact(4.43, 10.0, youngs_gpa=2.3, density_kg_m3=1050)
        assert stress["peak_von_mises_mpa"] > 3 * twin["stress_mpa"], (stress, twin)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_ccx_explicit_bar_impact_agrees():
    if skip_heavy("CalculiX impact dynamics (explicit)"):
        return
    if solvers.ccx_bin() is None:
        print("    SKIP — CalculiX (ccx) not installed")
        return
    base = tempfile.mkdtemp(prefix="impact_ccx_ex_")
    try:
        twin = I.bar_impact(1.0, 100.0, youngs_gpa=210, density_kg_m3=7850, area_mm2=1.0)
        t_c = twin["contact_duration_ms"] * 1e-3
        built, hist, red = _solve(base, method="explicit", etype="C3D8R",
                                  duration_s=1.7 * t_c)
        assert built["time_step"]["governs"] == "contact"
        t0 = red["contact_start_s"]
        ratio_f = _plateau(hist, t0 + 0.25 * t_c, t0 + 0.75 * t_c) / twin["force_n"]
        assert abs(ratio_f - 1.0) < 0.02, f"face force {ratio_f:.4f}"
        assert abs(red["contact_duration_s"] / t_c - 1.0) < 0.10, red   # penalty tail
        assert red["separated"] and 0.97 <= red["restitution"] <= 1.01, red
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    for name in sorted(n for n in dir() if n.startswith("test_")):
        print(name)
        globals()[name]()
    print("OK")
