"""SU2 plane-channel case — the native CFD path (issue #237 item 3).

Two tiers:
  * **pure** (always, no solver): the closed form, the mesh/config/inlet writers, and
    the history parser's failure modes. These pin the case CONSTRUCTION, which is
    where a silent error would produce a plausible-looking wrong answer.
  * **live** (needs the SU2 binary): the gate. Solve the channel and compare against
    dp = 12*mu*U*L/h^2, which is exact for developed laminar flow — no empirical
    constant, so the solve either reproduces it or does not.

The live gate is what makes docs/MACOS.md's "CFD degrades to SU2" true rather than
aspirational: it needs no OpenFOAM and, on Apple Silicon, no Multipass VM.

Run:  python3 tests/test_su2_case.py
"""
import os
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import solvers  # noqa: E402
from ankusdrive.analysis import su2_case as su2  # noqa: E402


def test_plane_poiseuille_is_the_closed_form():
    # dp = 12 mu U L / h^2, checked against a hand calculation and its own scalings
    dp = su2.plane_poiseuille_dp(height_m=0.01, length_m=0.1,
                                 velocity_m_s=0.277778, mu_pa_s=0.1)
    assert abs(dp - 333.3333) < 1e-3, dp
    # linear in U, mu and L; inverse-square in the gap
    base = dict(height_m=0.01, length_m=0.1, velocity_m_s=0.2, mu_pa_s=0.05)
    d0 = su2.plane_poiseuille_dp(**base)
    assert abs(su2.plane_poiseuille_dp(**{**base, "velocity_m_s": 0.4}) / d0 - 2) < 1e-9
    assert abs(su2.plane_poiseuille_dp(**{**base, "length_m": 0.2}) / d0 - 2) < 1e-9
    assert abs(su2.plane_poiseuille_dp(**{**base, "height_m": 0.02}) / d0 - 0.25) < 1e-9
    for bad in ({"height_m": 0}, {"velocity_m_s": -1}, {"mu_pa_s": 0}):
        try:
            su2.plane_poiseuille_dp(**{**base, **bad})
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad} should have raised")


def test_the_case_is_complete_and_self_consistent(tmp=None):
    import tempfile
    case = tempfile.mkdtemp(prefix="su2_case_test_")
    built = su2.write_channel_case(case, height_mm=10.0, length_mm=100.0,
                                   nx=10, ny=10)
    for name in ("mesh.su2", "flow.cfg", "inlet.dat"):
        assert os.path.isfile(os.path.join(case, name)), name
    assert built["n_cells"] == 100, built
    assert built["n_points"] == 121, built
    mesh = Path(case, "mesh.su2").read_text(encoding="utf-8")
    assert "NDIME= 2" in mesh and "NELEM= 100" in mesh and "NPOIN= 121" in mesh
    # all four boundaries are named, or SU2 silently leaves an open edge
    for tag in ("inlet", "outlet", "wall_bot", "wall_top"):
        assert f"MARKER_TAG= {tag}" in mesh, tag
    cfg = Path(case, "flow.cfg").read_text(encoding="utf-8")
    # the two settings the module docstring says decide whether this converges
    assert "SPECIFIED_INLET_PROFILE= YES" in cfg, cfg
    assert "CONV_FIELD= ( SURFACE_PRESSURE_DROP )" in cfg, cfg
    # ...and the outputs the gate reads back
    assert "MARKER_ANALYZE=" in cfg and "FLOW_COEFF" in cfg, cfg


def test_the_inlet_profile_is_developed_flow():
    """The parabola is the whole reason the domain can be short. It must integrate to
    the mean velocity and vanish at both walls — a profile that does neither would
    quietly change the flow rate the analytic dp is computed from."""
    import tempfile
    case = tempfile.mkdtemp(prefix="su2_inlet_test_")
    h, u_mean, ny = 0.01, 0.25, 40
    su2.write_inlet_profile(os.path.join(case, "inlet.dat"), h, u_mean, ny)
    lines = Path(case, "inlet.dat").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "NMARK= 1" and lines[2] == f"NROW={ny + 1}", lines[:4]
    rows = [ln.split() for ln in lines[5:] if ln.strip()]
    assert len(rows) == ny + 1, len(rows)
    ys = [float(r[1]) for r in rows]
    us = [float(r[3]) for r in rows]
    assert abs(us[0]) < 1e-12 and abs(us[-1]) < 1e-12, (us[0], us[-1])   # no-slip
    assert abs(max(us) - 1.5 * u_mean) < 1e-9, max(us)                   # u_max=1.5U
    # trapezoidal mean over the gap must be the mean velocity that sets analytic dp
    area = sum((us[i] + us[i + 1]) / 2 * (ys[i + 1] - ys[i]) for i in range(ny))
    assert abs(area / h - u_mean) / u_mean < 1e-3, area / h


def test_history_parser_reports_a_failed_solve_as_a_failed_measurement():
    """A missing or empty history.csv means the solve produced no number. That must
    read as ok:false with a reason — never a raise, and never a zero that a ratio
    would happily divide into something plausible."""
    import tempfile
    case = tempfile.mkdtemp(prefix="su2_hist_test_")
    got = su2.read_history(case)                      # no file at all
    assert got["ok"] is False and "no history.csv" in got["reason"], got
    Path(case, "history.csv").write_text('"Inner_Iter","rms[P]"\n',
                                         encoding="utf-8")   # header only
    got = su2.read_history(case)
    assert got["ok"] is False and "no iterations" in got["reason"], got
    Path(case, "history.csv").write_text('"Inner_Iter","rms[P]"\n1,-5.0\n',
                                         encoding="utf-8")
    got = su2.read_history(case)
    assert got["ok"] is False and "Pressure_Drop" in got["reason"], got
    # a good file: the magnitude is taken, because SU2 signs it outlet-minus-inlet
    Path(case, "history.csv").write_text(
        '"Inner_Iter","rms[P]","Pressure_Drop"\n1,-5.0,-333.5\n2,-9.0,-333.4\n',
        encoding="utf-8")
    got = su2.read_history(case)
    assert got["ok"] is True and got["iterations"] == 2, got
    assert abs(got["pressure_drop_pa"] - 333.4) < 1e-9, got
    assert abs(got["rms_p"] + 9.0) < 1e-9, got


def test_su2_counts_toward_the_cfd_family_now():
    """#237 item 3's product-visible point: SU2 has a case builder, so it must not be
    marked prepared_case_only. If this flips back, `docs/MACOS.md`'s "CFD degrades to
    SU2" becomes a lie again."""
    assert "prepared_case_only" not in solvers._SOLVERS["su2"], solvers._SOLVERS["su2"]
    from ankusdrive.analysis import cfd
    gate = cfd.solve_gate("laminar", "internal_channel", reynolds=50.0)
    assert gate["gated"] is True, gate
    assert "Poiseuille" in gate["oracle"], gate
    # past the plane-channel transition the closed form is the wrong oracle, and the
    # gate must say so rather than inheriting credibility from the laminar case
    assert cfd.solve_gate("laminar", "internal_channel", reynolds=5000)["gated"] \
        is False


# --- live gate (needs the SU2 binary) ----------------------------------------

def _su2_available():
    return solvers.find_solver("su2").get("available") is True


def test_the_channel_solve_reproduces_plane_poiseuille():
    """THE gate for #237 item 3. Solve the channel on SU2 and compare with the exact
    closed form. No OpenFOAM, no Multipass VM, no FreeCAD — a native binary and a
    2-D mesh this repo wrote."""
    if not _su2_available():
        print("    SKIP — SU2 binary not installed")
        return
    from ankusdrive import Worker

    with Worker() as w:
        sub = w.call("cfd_internal_flow_submit", channel_height_mm=10.0)
        assert sub.get("job_id"), sub
        deadline = time.time() + 900
        while time.time() < deadline:
            st = w.call("job_status", job_id=sub["job_id"])
            if st["status"] in ("done", "failed"):
                break
            time.sleep(0.5)
        assert st["status"] == "done", w.call("job_result", job_id=sub["job_id"])
        got = w.call("job_result", job_id=sub["job_id"])["result"]

        assert got["ok"] is True, got
        assert got["solver"] == "su2", got
        assert got["laminar"] is True and got["gated"] is True, got
        ratio = got["poiseuille_ratio"]
        print(f"    SU2 channel, Re {got['reynolds']:.0f}: dp "
              f"{got['pressure_drop_pa']:.4f} Pa vs exact "
              f"{got['analytic_dp_pa']:.4f} Pa (ratio {ratio:.6f}) in "
              f"{got['iterations']} iterations")
        # EXACT closed form, so the band is tight — this is not a correlation
        assert abs(ratio - 1.0) < 0.01, (ratio, got)
        # it stopped because the ANSWER stopped moving, not because it ran out
        assert got["converged"] is True, got
        assert got["iterations"] < 3000, got


def test_the_channel_gate_is_mesh_independent():
    """A ratio of 1.0 on one mesh could be a lucky cancellation. Refining must not
    move it — that is the difference between agreeing with the oracle and agreeing
    with the oracle for a reason."""
    if not _su2_available():
        print("    SKIP — SU2 binary not installed")
        return
    from ankusdrive import Worker

    ratios = []
    with Worker() as w:
        for nx, ny in ((20, 20), (60, 60)):
            sub = w.call("cfd_internal_flow_submit", channel_height_mm=10.0,
                         nx=nx, ny=ny)
            deadline = time.time() + 900
            while time.time() < deadline:
                st = w.call("job_status", job_id=sub["job_id"])
                if st["status"] in ("done", "failed"):
                    break
                time.sleep(0.5)
            got = w.call("job_result", job_id=sub["job_id"])["result"]
            assert got["ok"] is True, got
            ratios.append(got["poiseuille_ratio"])
            print(f"    {nx}x{ny}: ratio {got['poiseuille_ratio']:.6f} "
                  f"({got['n_cells']} cells, {got['iterations']} iters)")
    for r in ratios:
        assert abs(r - 1.0) < 0.01, ratios
    assert abs(ratios[0] - ratios[-1]) < 0.005, ratios


def _discover():
    return sorted(((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f)), key=lambda kv: kv[0])


def main():
    failures = []
    t_suite = time.time()
    tests = _discover()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({time.time() - t_suite:.1f}s) ==")


if __name__ == "__main__":
    main()
