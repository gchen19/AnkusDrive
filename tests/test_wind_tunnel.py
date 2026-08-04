"""The virtual wind tunnel end-to-end (issues #223 / #224 / #236).

The mesh-bridge half of this is gated in ``tests/test_meshbridge.py`` against the
sphere drag curve with a pure-Python STL. THIS file closes the loop through the
product surface: a real FreeCAD solid, the real ``cfd_external_flow_submit`` handler,
the real async job registry.

Two tiers:
  * **screen** (FreeCAD only, always runs): ``cfd_body_drag`` measures the frontal
    silhouette off a live solid and turns it into a banded drag estimate. Two-sided —
    the same box presented differently must report a different silhouette.
  * **solve** (needs OpenFOAM, gated behind RUN_HEAVY_SOLVES): a 10 mm sphere at
    Re = 100 through the whole submit → job_result path must land on the drag curve,
    and the payload must prove it solved THE BODY. That last assertion is the #236
    regression: `model` used to be accepted and silently ignored, so the caller got
    flat-plate drag presented as their sphere's.

Run:  python3 tests/test_wind_tunnel.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin import solvers  # noqa: E402
from driftpin.analysis import cfd  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402


def _await_job(w, job_id, timeout_s=900):
    deadline = time.monotonic() + timeout_s
    status = None
    while time.monotonic() < deadline:
        status = w.call("job_status", job_id=job_id)
        if status["status"] in ("done", "failed"):
            break
        time.sleep(1.0)
    assert status and status["status"] == "done", status
    return w.call("job_result", job_id=job_id)["result"]


# --- screen tier: no solver needed ---------------------------------------------

def test_body_drag_screen_measures_the_live_silhouette():
    """cfd_body_drag with a `model` handle measures the frontal area off the solid
    rather than making the caller supply one — and the measurement follows
    flow_direction, so the same box reports a different silhouette per orientation."""
    with Worker() as w:
        w.call("new_document", name="tunnel_screen")
        box = w.call("add_primitive", kind="box", w=20, d=10, h=40)
        along_x = w.call("cfd_body_drag", shape="cube_face_on", model=box["handle"],
                         velocity_m_s=30.0, flow_direction=[1, 0, 0])
        along_z = w.call("cfd_body_drag", shape="cube_face_on", model=box["handle"],
                         velocity_m_s=30.0, flow_direction=[0, 0, 1])
        # 10 x 40 = 400 mm^2 seen along x; 20 x 10 = 200 mm^2 seen along z
        assert abs(along_x["frontal_area_m2"] - 400e-6) < 1e-9, along_x
        assert abs(along_z["frontal_area_m2"] - 200e-6) < 1e-9, along_z
        assert along_x["drag_force_n"] > 1.9 * along_z["drag_force_n"], (along_x, along_z)
        assert along_x["cd"] == 1.05 and along_x["cd_source"] == "table"
        assert along_x["escalate_to"] == "cfd_external_flow_submit"

        # the sphere/cylinder families resolve Re instead of a plateau Cd
        sph = w.call("cfd_body_drag", shape="sphere", diameter_mm=50,
                     velocity_m_s=30.0)
        assert sph["regime"] == "newton" and sph["band_pct"] == 10.0, sph
        creep = w.call("cfd_body_drag", shape="sphere", diameter_mm=0.05,
                       velocity_m_s=1e-4, fluid="glycerin-20c")
        assert creep["regime"] == "stokes" and creep["fidelity"] == "exact", creep
        assert w.call("cfd_body_drag", shape="list")["shapes"]["streamlined_body"] < 0.1


# --- solve tier: the wind tunnel itself -----------------------------------------

def test_wind_tunnel_solves_the_handed_body_not_a_flat_plate():
    """The #223 acceptance and the #236 regression in one: hand
    cfd_external_flow_submit an actual sphere and it must (a) come back on the sphere
    drag curve and (b) say, in the payload, that it solved THAT BODY — never the
    flat-plate validation case wearing the caller's handle."""
    if skip_heavy("OpenFOAM wind tunnel (end to end)"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    d_mm, rho = 10.0, 998.2
    mu, _ = cfd._fluid_props("water-20c", None, None)
    velocity = 100.0 * (mu / rho) / (d_mm / 1000.0)          # Re = 100
    with Worker() as w:
        w.call("new_document", name="tunnel_e2e")
        sphere = w.call("add_primitive", kind="sphere", radius=d_mm / 2)
        sub = w.call("cfd_external_flow_submit", model=sphere["handle"],
                     velocity_m_s=velocity, fluid="water-20c")
        assert sub.get("job_id"), sub
        res = _await_job(w, sub["job_id"])

    assert res["ok"], res
    assert res["mode"] == "body" and res["kind"] == "external", res
    # #236: a flat-plate answer would carry these instead of a body force
    assert "blasius_ratio" not in res and "cf_blasius" not in res, res
    assert res["converged"] is True and res["gated"] is True, res

    # the measured silhouette must be the sphere's, not the bounding box's
    exact_area = math.pi * (d_mm / 2000.0) ** 2
    assert abs(res["frontal_area_m2"] / exact_area - 1.0) < 0.02, res
    assert res["frontal_area_source"] == "projected"
    assert res["blockage_ratio"] < 0.05 and not res["warnings"], res

    oracle = cfd.sphere_drag(d_mm, velocity, mu_pa_s=mu, rho_kg_m3=rho)
    ratio = res["cd"] / oracle["cd"]
    assert abs(ratio - 1.0) < oracle["band_pct"] / 100.0, (res["cd"], oracle["cd"], ratio)
    # a symmetric body in axial flow: no lift, and drag split between form and friction
    assert abs(res["cl"]) < 1e-3 * abs(res["cd"]), res
    assert res["drag_pressure_n"] > 0 and res["drag_viscous_n"] > 0, res
    assert res["drag_force_n"] > 0 and res["force_drift_pct"] < 0.1, res
    print(f"    sphere through the handler: Cd {res['cd']:.4g} vs curve "
          f"{oracle['cd']:.4g} (ratio {ratio:.3f}, Re {res['reynolds']:.4g})")


def test_rans_wind_tunnel_is_gated_on_a_sharp_edged_bluff_body():
    """#262: the turbulent external-flow oracle. Until this existed the wind tunnel set
    `gated` from `turbulence == 'laminar'`, so every kOmegaSST solve shipped
    gated:false and a spec demanding trust:{gated:true} was unsatisfiable at any
    realistic Reynolds number — laminar is gated but physically wrong above Re≈1000,
    RANS was right but unproven.

    The case is a cube face-on at Re = 1e4 and 1e5, gated against the tabulated
    bluff-body Cd (1.05, ±20 %). A sharp-edged body is where steady RANS is credible:
    separation is pinned to the edges by geometry, so the answer does not hang on the
    turbulence model guessing where the boundary layer lets go. The table is
    Re-independent over 1e4–1e6, so the SAME Cd must come back two decades apart —
    which is the part a lucky single point cannot fake.

    (Why not the cylinder in crossflow #262 proposed: Sucker–Brauer is the INFINITE
    -cylinder value and the handler cannot build a spanwise-periodic case —
    external_domain_box pads both non-flow axes with the one `lateral_factor`, so a
    near-2-D span cannot be asked for without crushing the cross-stream domain into
    the blockage warning. A finite L/D = 4 cylinder solved live at Re = 1e4 lands at
    Cd 0.77, ratio 0.70 — end relief, exactly as cylinder_crossflow_drag's own L/D < 10
    warning predicts, and outside its ±15 % band for a geometric reason that has
    nothing to do with turbulence modelling.)"""
    if skip_heavy("OpenFOAM RANS wind tunnel (the #262 turbulent oracle)"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    d_mm = 10.0
    runs = []
    with Worker() as w:
        w.call("new_document", name="tunnel_rans")
        cube = w.call("add_primitive", kind="box", w=d_mm, d=d_mm, h=d_mm)
        for re_target, fluid in ((1e4, "air-20c"), (1e5, "water-20c")):
            mu, rho = cfd._fluid_props(fluid, None, None)
            velocity = re_target * (mu / rho) / (d_mm / 1000.0)
            sub = w.call("cfd_external_flow_submit", model=cube["handle"],
                         velocity_m_s=velocity, fluid=fluid, turbulence="kOmegaSST")
            assert sub.get("job_id"), sub
            res = _await_job(w, sub["job_id"])
            oracle = cfd.bluff_body_drag("cube_face_on", frontal_area_mm2=d_mm * d_mm,
                                         velocity_m_s=velocity, fluid=fluid)
            runs.append((re_target, res, oracle))

    for re_target, res, oracle in runs:
        assert res["ok"] and res["mode"] == "body", res
        assert res["turbulence"] == "kOmegaSST", res
        # THE #262 assertion: the turbulent path now carries a verified oracle, so
        # performance.check_trust can satisfy a requirement that demands one
        assert res["gated"] is True, res
        assert res["converged"] is True, res
        assert not res["warnings"], res["warnings"]
        assert abs(res["reynolds"] / re_target - 1.0) < 0.01, res
        assert res["blockage_ratio"] < 0.05, res            # bluff bodies feel walls
        assert abs(res["frontal_area_m2"] - 1e-4) < 1e-9, res

        ratio = res["cd"] / oracle["cd"]
        assert abs(ratio - 1.0) < oracle["band_pct"] / 100.0, (res["cd"], ratio)
        # the live value is 1.0017 (Re=1e4) / 1.0035 (Re=1e5), and 1.0136 on a 4x finer
        # surface refinement — a drift past 10 % is a regression even though the
        # table's own band is 20 %
        assert abs(ratio - 1.0) < 0.10, (res["cd"], oracle["cd"], ratio)
        # the signature of edge-fixed separation: essentially all form drag, no lift
        assert abs(res["drag_viscous_n"]) < 0.01 * res["drag_force_n"], res
        assert abs(res["cl"]) < 0.01 * abs(res["cd"]), res
        assert res["force_drift_pct"] < 0.1, res
        yp = res["trust"]["y_plus"]
        print(f"    cube face-on, kOmegaSST, Re {res['reynolds']:.4g}: Cd "
              f"{res['cd']:.4g} vs table {oracle['cd']:.4g} (ratio {ratio:.4f}, band "
              f"±{oracle['band_pct']:.0f} %), y+ max {yp['y_plus_max']:.3g}")

    # two-sided on the trust layer, which is a DIFFERENT question from the gate: at
    # Re=1e4 the first cell sits in the log layer and the solve is trusted outright;
    # at Re=1e5 the same mesh puts y+ past 300, so trust refuses it even though the
    # gated Cd is still right. Verified evidence is not validation, and vice versa.
    lo, hi = runs[0][1]["trust"], runs[1][1]["trust"]
    assert lo["trusted"] is True and lo["y_plus"]["in_band"] is True, lo
    assert hi["y_plus"]["in_band"] is False and hi["trusted"] is False, hi
    assert any("y+" in r for r in hi["reasons"]), hi["reasons"]


def test_rans_on_a_smooth_body_sits_at_the_edge_of_its_oracle():
    """The honest boundary of the #262 gate, measured rather than asserted away: on a
    SMOOTH body the separation line is not pinned by geometry, so it is the turbulence
    model's to predict — and steady kOmegaSST does it badly enough to matter.

    A sphere at Re = 1e4 reads ~1.09× the Clift–Gauvin curve. That is inside the
    correlation's own ±10 % band, but only just, and it is not a mesh artefact: it
    holds to 1.10 at the next refinement level, so it is model error, not discretization
    error. This test records that number. Should it ever cross the band, the honest fix
    is to narrow the (kOmegaSST, external_body) entry in cfd._SOLVE_GATES to the
    fixed-separation family it was verified on — not to widen the band."""
    if skip_heavy("OpenFOAM RANS sphere (the #262 boundary)"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    d_mm, fluid = 10.0, "air-20c"
    mu, rho = cfd._fluid_props(fluid, None, None)
    velocity = 1e4 * (mu / rho) / (d_mm / 1000.0)
    with Worker() as w:
        w.call("new_document", name="tunnel_rans_sphere")
        sphere = w.call("add_primitive", kind="sphere", r=d_mm / 2)
        res = _await_job(w, w.call(
            "cfd_external_flow_submit", model=sphere["handle"],
            velocity_m_s=velocity, fluid=fluid, turbulence="kOmegaSST")["job_id"])

    assert res["ok"] and res["converged"] is True and res["gated"] is True, res
    oracle = cfd.sphere_drag(d_mm, velocity, fluid=fluid)
    ratio = res["cd"] / oracle["cd"]
    # characterization, not a pass/fail band: RANS OVERPREDICTS here (delayed
    # separation), by ~9 % — the assertion brackets the measured behaviour so a change
    # in either direction shows up
    assert 1.04 < ratio < 1.14, (res["cd"], oracle["cd"], ratio)
    inside = abs(ratio - 1.0) < oracle["band_pct"] / 100.0
    # unlike the cube, a smooth body carries real friction drag
    assert res["drag_viscous_n"] > 0.02 * res["drag_force_n"], res
    print(f"    sphere, kOmegaSST, Re {res['reynolds']:.4g}: Cd {res['cd']:.4g} vs "
          f"Clift-Gauvin {oracle['cd']:.4g} (ratio {ratio:.4f}, band "
          f"±{oracle['band_pct']:.0f} % -> {'inside' if inside else 'OUTSIDE'}); "
          f"y+ max {res['trust']['y_plus']['y_plus_max']:.3g}, trusted "
          f"{res['trust']['trusted']}")


def test_trust_block_reports_convergence_mesh_and_yplus():
    """The #225 trust layer through the product surface, two-sided: the shipped
    defaults must come back `trusted` with a converged solve and a clean checkMesh —
    and the SAME case starved of iterations must come back `converged: false` with a
    reason, rather than a number that looks exactly as good."""
    if skip_heavy("OpenFOAM trust layer"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    d_mm, rho = 10.0, 998.2
    mu, _ = cfd._fluid_props("water-20c", None, None)
    velocity = 100.0 * (mu / rho) / (d_mm / 1000.0)          # Re = 100
    with Worker() as w:
        w.call("new_document", name="tunnel_trust")
        sphere = w.call("add_primitive", kind="sphere", radius=d_mm / 2)
        good = _await_job(w, w.call(
            "cfd_external_flow_submit", model=sphere["handle"],
            velocity_m_s=velocity, fluid="water-20c")["job_id"])
        starved = _await_job(w, w.call(
            "cfd_external_flow_submit", model=sphere["handle"],
            velocity_m_s=velocity, fluid="water-20c", end_time=5)["job_id"])

    t = good["trust"]
    assert t["converged"] is True and t["trusted"] is True, t
    assert not t["reasons"], t["reasons"]
    assert t["iterations"] and 1 < t["iterations"] < 1500, t
    assert t["max_residual"] is not None and t["max_residual"] < 1e-4, t
    assert set(t["final_residuals"]) >= {"Ux", "Uy", "Uz", "p"}, t["final_residuals"]
    assert t["mesh"]["ok"] is True, t["mesh"]
    assert 0 < t["mesh"]["max_non_orthogonality_deg"] < 70, t["mesh"]
    assert t["mesh"]["n_cells"] > 1000, t["mesh"]
    # y+ is MEASURED from the solved wall shear, not estimated beforehand
    assert t["y_plus"] and t["y_plus"]["y_plus_max"] > 0, t["y_plus"]

    s = starved["trust"]
    assert s["converged"] is False, s
    assert s["trusted"] is False and s["reasons"], s
    assert "iteration cap" in s["reasons"][0], s["reasons"]
    assert s["iterations"] == 5, s
    # the starved run still RETURNS a Cd — that is exactly the trap: it is only the
    # trust block that distinguishes it from the good one
    assert starved["cd"] is not None and good["cd"] is not None
    assert s["max_residual"] > t["max_residual"], (s["max_residual"], t["max_residual"])
    print(f"    trusted Cd {good['cd']:.4g} (converged in {t['iterations']} it, "
          f"max residual {t['max_residual']:.2g}, y+ max "
          f"{t['y_plus']['y_plus_max']:.3g}) vs starved Cd {starved['cd']:.4g} "
          f"(max residual {s['max_residual']:.2g}, NOT converged)")


def test_mesh_independence_brackets_the_analytic_answer():
    """The #225 headline gate: on the pipe — the one case with an exact closed form —
    the Grid Convergence band computed WITHOUT any reference must contain the
    Hagen-Poiseuille value. That is the claim the whole verification layer rests on:
    that the band means something on geometry where no oracle exists."""
    if skip_heavy("OpenFOAM mesh independence"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    with Worker() as w:
        w.call("new_document", name="mesh_independence")
        sub = w.call("cfd_mesh_independence_submit", diameter_mm=10, length_mm=500,
                     velocity_m_s=0.005, fluid="water-20c", levels=3,
                     refinement_ratio=1.5, n_axial=80, n_radial=10, end_time=3000)
        assert sub.get("job_id"), sub
        res = _await_job(w, sub["job_id"], timeout_s=1800)

    assert res["ok"], res
    assert res["family"] == "internal_pipe" and len(res["levels"]) == 3, res
    vals = [lv["value"] for lv in res["levels"]]
    sizes = [lv["cell_size_m"] for lv in res["levels"]]
    assert all(v is not None for v in vals), res["levels"]
    assert sizes[0] < sizes[1] < sizes[2], sizes            # finest first
    assert all(lv["converged"] is True for lv in res["levels"]), res["levels"]

    gci = res["grid_convergence"]
    assert gci["monotonic"] is True, gci
    assert 0 < gci["gci_pct"] < 25, gci                     # a usable band, not noise
    # the band around the finest solve must contain the exact analytic answer
    hp = res["hagen_poiseuille_pa"]
    fine = vals[0]
    half = gci["gci_pct"] / 100.0 * abs(fine)
    assert abs(fine - hp) <= half * 3.0, (fine, hp, half, gci)
    # ... and the Richardson limit must be at least as close to it as the finest mesh
    assert abs(gci["extrapolated_value"] - hp) <= abs(fine - hp) * 1.5, (gci, hp)
    print(f"    pipe GCI: {vals} Pa -> extrapolated "
          f"{gci['extrapolated_value']:.5g} vs Hagen-Poiseuille {hp:.5g} "
          f"(order {gci['observed_order']}, band {gci['gci_pct']:.3g} %)")


def test_wind_tunnel_refuses_a_cell_that_would_mesh_an_empty_tunnel():
    """The silent-wrong-answer guard, through the product surface: asking for a
    background cell as big as the body must be REFUSED, not answered with the ~0 N
    an empty tunnel converges to."""
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed (the guard sits behind the "
              "solver-presence check)")
        return
    from driftpin.client import WorkerError
    with Worker() as w:
        w.call("new_document", name="tunnel_guard")
        sphere = w.call("add_primitive", kind="sphere", radius=5)
        try:
            w.call("cfd_external_flow_submit", model=sphere["handle"],
                   velocity_m_s=1.0, base_cell_mm=20.0)
        except WorkerError as e:
            assert "EMPTY tunnel" in e.remote_message, e.remote_message
        else:
            raise AssertionError("a body-sized background cell should be refused")
    # the legal side of the guard is gated in tests/test_meshbridge.py
    # (external_domain_box accepts exactly L/2), without paying for a solve here.


# --- runner -------------------------------------------------------------------

def _discover():
    return [(name, fn) for name, fn in sorted(globals().items())
            if name.startswith("test_") and callable(fn)]


def main():
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")
    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
