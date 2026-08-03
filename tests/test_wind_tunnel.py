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
