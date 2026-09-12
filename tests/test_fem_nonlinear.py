"""
Nonlinear-structural FEM via the bundled CalculiX ccx — oracle-gated (issue #90).

Two tiers, mirroring every other family in the suite:

  Pure-oracle toys (always run, no solver) — the closed-form anchors:
    - test_plastic_collapse_oracle  : M_p = σ_y·Z, shape factor 1.5, regimes
    - test_elastica_oracle          : Bisshopp–Drucker tip vs linear divergence
    - test_hertz_oracle             : p₀ = 3F/2πa², a = (3FR/4E*)^⅓

  Live ccx solves (skip when freecadcmd absent) — each gated on its anchor:
    - test_fem_plasticity_uniaxial_tension : *PLASTIC caps von Mises at the σ_y
        plateau (≈5× below the linear E·ε a perfectly-elastic solve predicts)
    - test_fem_large_deflection_elastica   : *NLGEOM tip tracks the elliptic-
        integral elastica (transverse to <4%), not the over-predicting line
    - test_fem_contact_setup_wiring        : contact_setup builds the *CONTACT
        PAIR constraints and flips the solver nonlinear (the wiring a contact
        solve rides on)

Run:  python3 tests/test_fem_nonlinear.py   (host-side subprocess mgmt is stdlib)
"""
import math
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ -> fem_scratch

from ankusdrive import Worker  # noqa: E402
from ankusdrive.analysis import nonlinear as nl  # noqa: E402
from ankusdrive.client import FREECADCMD  # noqa: E402
from fem_scratch import fem_workdir  # noqa: E402

_STEEL = {"Name": "Steel", "YoungsModulus": "210000 MPa",
          "PoissonRatio": "0.30", "Density": "7900 kg/m^3"}


def _freecad_available():
    return Path(FREECADCMD).exists()


# --- pure-oracle toys (no solver) ---------------------------------------------

def test_plastic_collapse_oracle():
    """Plastic-hinge collapse closed form: a rectangle's shape factor Z/S is
    exactly 1.5, M_p = σ_y·Z, and the applied-load regime walks elastic →
    partially_plastic → collapsed as the moment crosses M_y then M_p."""
    r = nl.plastic_collapse(200, 20, 10, yield_mpa=250)
    assert abs(r["shape_factor"] - 1.5) < 1e-9, r["shape_factor"]
    # S = 20·10²/6, Z = 20·10²/4 (returns are rounded, so compare loosely)
    assert abs(r["S_elastic_mm3"] - 20 * 100 / 6) < 1e-3
    assert abs(r["Z_plastic_mm3"] - 20 * 100 / 4) < 1e-3
    assert abs(r["plastic_moment_nmm"] - 250 * 500.0) < 1e-2
    assert abs(r["collapse_load_n"] - r["plastic_moment_nmm"] / 200) < 1e-2

    # two-sided regime walk at a cantilever's three load levels
    my, mp = r["yield_load_n"], r["collapse_load_n"]
    assert nl.plastic_collapse(200, 20, 10, yield_mpa=250,
                               load_n=0.5 * my)["regime"] == "elastic"
    assert nl.plastic_collapse(200, 20, 10, yield_mpa=250,
                               load_n=0.5 * (my + mp))["regime"] == "partially_plastic"
    assert nl.plastic_collapse(200, 20, 10, yield_mpa=250,
                               load_n=1.2 * mp)["regime"] == "collapsed"

    # simply-supported central load uses M = P·L/4, so collapses at 4× the load
    ss = nl.plastic_collapse(200, 20, 10, yield_mpa=250, support="simply_supported")
    assert abs(ss["collapse_load_n"] - 4 * r["collapse_load_n"]) < 1e-3

    for bad in (lambda: nl.plastic_collapse(-1, 20, 10, yield_mpa=250),
                lambda: nl.plastic_collapse(200, 20, 10),                 # no σ_y
                lambda: nl.plastic_collapse(200, 20, 10, yield_mpa=250, support="x")):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    print(f"    plastic: shape factor {r['shape_factor']}, "
          f"M_p {r['plastic_moment_nmm']:.0f} N·mm, P_collapse {mp:.1f} N")


def test_elastica_oracle():
    """Bisshopp–Drucker elastica: at vanishing load the tip → linear theory
    (δ/L = α/3); as the load grows the exact tip falls increasingly short of the
    line and draws axially inward, and the tip stays within reach (x²+y² ≤ L²)."""
    tiny = nl.elastica_deflection(1.0, 300, youngs_gpa=210, width_mm=20, height_mm=4)
    assert abs(tiny["nonlinear_over_linear"] - 1.0) < 1e-3, tiny["nonlinear_over_linear"]
    assert tiny["axial_drawin_mm"] >= 0

    prev = 1.0
    for P in (200.0, 400.0, 800.0):
        e = nl.elastica_deflection(P, 300, youngs_gpa=210, width_mm=20, height_mm=4)
        # the nonlinear tip diverges monotonically below the linear prediction
        assert e["nonlinear_over_linear"] < prev, (P, e["nonlinear_over_linear"])
        prev = e["nonlinear_over_linear"]
        # tip slope grows with load; tip stays geometrically reachable
        assert 0 < e["tip_slope_deg"] < 90
        assert e["tip_x_mm"] ** 2 + e["tip_disp_mm"] ** 2 <= (300.0 ** 2) * 1.0001
        assert e["axial_drawin_mm"] > 0
    big = nl.elastica_deflection(800.0, 300, youngs_gpa=210, width_mm=20, height_mm=4)
    assert big["nonlinear_over_linear"] < 0.75, big["nonlinear_over_linear"]

    # I from width/height must equal an explicit i_mm4 give the same answer
    e_sec = nl.elastica_deflection(400, 300, youngs_gpa=210, width_mm=20, height_mm=4)
    e_exp = nl.elastica_deflection(400, 300, youngs_gpa=210, i_mm4=20 * 4 ** 3 / 12)
    assert abs(e_sec["tip_disp_mm"] - e_exp["tip_disp_mm"]) < 1e-6
    print(f"    elastica: α={big['alpha']:.2f} θ₀={big['tip_slope_deg']:.0f}° "
          f"tip {big['tip_disp_mm']:.1f}mm vs linear {big['linear_tip_mm']:.1f}mm "
          f"(nl/lin {big['nonlinear_over_linear']:.3f})")


def test_hertz_oracle():
    """Hertz sphere-on-flat against a hand calc: a steel ball (E=210 GPa, ν=0.3,
    R=10) under 100 N gives E* = E/2(1−ν²), a = (3FR/4E*)^⅓, p₀ = 3F/2πa²,
    p₀ = 1.5× the mean. A large patch (a/R > 0.1) trips the validity warning."""
    h = nl.hertz_contact(100, 10, youngs1_gpa=210, poisson1=0.3)
    e_star = 210e3 / (2 * (1 - 0.3 ** 2))
    a = (3 * 100 * 10 / (4 * e_star)) ** (1 / 3)
    p0 = 3 * 100 / (2 * math.pi * a ** 2)
    assert abs(h["e_star_mpa"] - e_star) / e_star < 1e-4
    assert abs(h["contact_radius_mm"] - a) / a < 1e-4
    assert abs(h["peak_pressure_mpa"] - p0) / p0 < 1e-4
    assert abs(h["peak_pressure_mpa"] / h["mean_pressure_mpa"] - 1.5) < 1e-4
    assert h["valid_range_ok"] and not h["warnings"]

    # dissimilar pair: softer body 2 lowers E*, enlarging the patch, dropping p₀
    mix = nl.hertz_contact(100, 10, youngs1_gpa=210, poisson1=0.3,
                           youngs2_gpa=70, poisson2=0.33)
    assert mix["e_star_mpa"] < h["e_star_mpa"]
    assert mix["peak_pressure_mpa"] < h["peak_pressure_mpa"]

    # heavy load on a small ball → a/R large → validity warning fires
    warned = nl.hertz_contact(5000, 2, youngs1_gpa=210, poisson1=0.3)
    assert warned["a_over_R"] > 0.1 and not warned["valid_range_ok"]
    print(f"    hertz: E*={h['e_star_mpa']:.0f} MPa, a={h['contact_radius_mm']:.3f}mm, "
          f"p₀={h['peak_pressure_mpa']:.0f} MPa")


# --- live ccx solves ----------------------------------------------------------

def _new_beam(w, length, width, height, fix_norm=(-1, 0, 0)):
    """Steel box with the `fix_norm` face fixed; returns the analysis/box/material
    handles plus the fixed-face query (solver NOT yet configured nonlinear)."""
    w.call("new_document", name="nl_beam")
    box = w.call("add_primitive", kind="box", w=length, d=width, h=height)
    fixed = w.call("query_faces", handle=box["handle"],
                   predicate={"type": "planar", "normal_dir": list(fix_norm)})
    assert len(fixed) == 1, fixed
    analysis = w.call("fem_new_analysis", name="A")
    mat = w.call("fem_set_material", analysis=analysis["handle"], body=box["handle"],
                 material=dict(_STEEL))
    w.call("fem_add_constraint", analysis=analysis["handle"], kind="fixed",
           refs=[{"handle": box["handle"], "tag": fixed[0]["tag"]}])
    return {"analysis": analysis["handle"], "box": box["handle"],
            "material": mat["handle"]}


def test_fem_plasticity_uniaxial_tension():
    """*PLASTIC (elastic–perfectly-plastic) uniaxial tension. A steel bar
    (σ_y=250 MPa) is stretched to ε=0.006 — five× past the 0.00119 yield strain —
    by a prescribed end displacement. J2 plasticity caps the von Mises stress at
    the σ_y plateau, so the solved peak lands at σ_y (a few % of nodal-extrapolation
    overshoot at the clamped end), DECISIVELY below the E·ε = 1260 MPa a purely
    elastic solve would report. That ~5× gap is the gate that the *PLASTIC card
    (fem_set_nonlinear_material) is actually doing its job."""
    if not _freecad_available():
        print("    SKIP — freecadcmd not found")
        return
    L, A, SY, E, eps = 100.0, 10.0, 250.0, 210000.0, 0.006
    with Worker() as w:
        h = _new_beam(w, L, A, A)
        w.call("fem_set_solver", analysis=h["analysis"], kind="ccx",
               tunables={"GeometricalNonlinearity": "linear", "MatrixSolverType": "default",
                         "IterationsControlParameterTimeUse": False})
        far = w.call("query_faces", handle=h["box"],
                     predicate={"type": "planar", "normal_dir": [1, 0, 0]})
        w.call("fem_add_constraint", analysis=h["analysis"], kind="displacement",
               refs=[{"handle": h["box"], "tag": far[0]["tag"]}], x=eps * L)
        info = w.call("fem_set_nonlinear_material", analysis=h["analysis"],
                      base_material=h["material"], yield_mpa=SY, ramp_increments=6)
        assert info["solver_material_nonlinear"] is True
        assert info["yield_points"] == ["250, 0", "250, 0.2"], info["yield_points"]
        w.call("fem_mesh", analysis=h["analysis"], body=h["box"],
               char_length=5.0, element_order="2nd", _timeout=180.0)
        w.call("fem_run", analysis=h["analysis"],
               workdir=fem_workdir("nl_tension"), _timeout=500.0)
        res = w.call("fem_results", analysis=h["analysis"])
        vm = res["max_vonmises_mpa"]
        linear = E * eps                                   # 1260 MPa, elastic prediction
        ratio = vm / SY
        assert 0.95 <= ratio <= 1.12, (
            f"von Mises {vm:.1f} MPa vs σ_y {SY} (ratio {ratio:.3f}) — the *PLASTIC "
            f"plateau cap is off")
        assert vm < 0.4 * linear, (
            f"von Mises {vm:.1f} MPa not far below the elastic E·ε={linear:.0f} MPa "
            f"— plasticity did not engage")
        print(f"    plasticity: ccx von Mises {vm:.1f} MPa ≈ σ_y {SY} "
              f"(elastic would be {linear:.0f} MPa) → capped {linear / vm:.1f}×")


def test_fem_large_deflection_elastica():
    """*NLGEOM end-loaded cantilever vs the Bisshopp–Drucker elastica. A slender
    steel beam (300×20×8, L/h=37) under a 3 kN dead tip load rotates ~37° — well
    into large deflection. With geometric nonlinearity on, the solved transverse
    tip matches the elliptic-integral oracle to <4% and the axial draw-in to <12%,
    while sitting clearly below the linear PL³/3EI line (nl/lin ≈ 0.82). A linear
    solve would over-predict the tip by ~22% and show zero draw-in."""
    if not _freecad_available():
        print("    SKIP — freecadcmd not found")
        return
    L, bw, h_, P = 300.0, 20.0, 8.0, 3000.0
    orc = nl.elastica_deflection(load_n=P, length_mm=L, youngs_gpa=210,
                                 width_mm=bw, height_mm=h_)
    with Worker() as w:
        h = _new_beam(w, L, bw, h_)
        w.call("fem_set_solver", analysis=h["analysis"], kind="ccx",
               tunables={"GeometricalNonlinearity": "nonlinear", "MatrixSolverType": "default",
                         "IterationsControlParameterTimeUse": False,
                         "TimeInitialIncrement": 0.05, "TimeMaximumIncrement": 0.1,
                         "OutputFrequency": 1000000})
        tip = w.call("query_faces", handle=h["box"],
                     predicate={"type": "planar", "normal_dir": [1, 0, 0]})
        edges = w.call("list_edges", handle=h["box"])
        zed = [e for e in edges if e["kind"] == "line"
               and abs(e["length"] - h_) < 1e-3 and abs(e["centroid"][0] - L) < 1e-3][0]
        w.call("fem_add_constraint", analysis=h["analysis"], kind="force",
               refs=[{"handle": h["box"], "tag": tip[0]["tag"]}], force=P,
               direction={"handle": h["box"], "edge": zed["tag"]}, reversed=False)
        w.call("fem_mesh", analysis=h["analysis"], body=h["box"],
               char_length=6.0, element_order="2nd", _timeout=200.0)
        w.call("fem_run", analysis=h["analysis"],
               workdir=fem_workdir("nl_elastica"), _timeout=600.0)
        res = w.call("fem_results", analysis=h["analysis"])
        vx, _, vz = res["max_displacement_vector"]
        transverse, drawin = abs(vz), abs(vx)
        rt = transverse / orc["tip_disp_mm"]
        rd = drawin / orc["axial_drawin_mm"]
        assert 0.96 <= rt <= 1.04, (
            f"transverse tip {transverse:.1f} vs elastica {orc['tip_disp_mm']:.1f} "
            f"(ratio {rt:.3f}) — outside 4% gate")
        assert 0.85 <= rd <= 1.12, (
            f"axial draw-in {drawin:.1f} vs elastica {orc['axial_drawin_mm']:.1f} "
            f"(ratio {rd:.3f})")
        # large-deflection divergence: nonlinear tip well below the linear line
        assert transverse < 0.92 * orc["linear_tip_mm"], (
            f"transverse {transverse:.1f} not below linear {orc['linear_tip_mm']:.1f} "
            f"— geometric nonlinearity did not engage")
        print(f"    elastica: ccx tip {transverse:.1f}mm vs oracle {orc['tip_disp_mm']:.1f}mm "
              f"(ratio {rt:.3f}); draw-in {drawin:.1f} vs {orc['axial_drawin_mm']:.1f}; "
              f"linear would be {orc['linear_tip_mm']:.1f}mm")


def test_fem_contact_setup_wiring():
    """contact_setup wiring: two stacked boxes meshed as one compound, a face pair
    declared at their interface. The constraint is built as a FemConstraintContact
    with the requested friction, and the solver is flipped to the nonlinear regime
    a CCX contact solve requires (GeometricalNonlinearity=nonlinear, single-frame
    output). Validates the *CONTACT PAIR plumbing without riding the full
    (mesh-sensitive) contact solve."""
    if not _freecad_available():
        print("    SKIP — freecadcmd not found")
        return
    A, H1, H2 = 30.0, 20.0, 20.0
    with Worker() as w:
        w.call("new_document", name="ctc")
        b1 = w.call("add_primitive", kind="box", w=A, d=A, h=H1)
        b2 = w.call("add_primitive", kind="box", w=A, d=A, h=H2, placement=[0, 0, H1])
        comp = w.call("run_script", code=(
            "import Part\n"
            "c=App.ActiveDocument.addObject('Part::Feature','Stack')\n"
            "c.Shape=Part.Compound([_resolve('%s').Shape,_resolve('%s').Shape])\n"
            "App.ActiveDocument.recompute(); __result__=c.Name\n"
            % (b1["handle"], b2["handle"])))
        comp_h = comp["registered"][0]["handle"]
        an = w.call("fem_new_analysis", name="A")
        w.call("fem_set_solver", analysis=an["handle"], kind="ccx",
               tunables={"GeometricalNonlinearity": "linear", "MatrixSolverType": "default",
                         "IterationsControlParameterTimeUse": False})
        # the two coincident interface faces at z=H1 (top of box1, bottom of box2)
        zp = w.call("query_faces", handle=comp_h,
                    predicate={"type": "planar", "normal_dir": [0, 0, 1]})
        zn = w.call("query_faces", handle=comp_h,
                    predicate={"type": "planar", "normal_dir": [0, 0, -1]})
        master = [f for f in zp if abs(f["centroid"][2] - H1) < 1e-6][0]
        slave = [f for f in zn if abs(f["centroid"][2] - H1) < 1e-6][0]
        out = w.call("contact_setup", analysis=an["handle"], friction=0.2,
                     face_pairs=[{"a": {"handle": comp_h, "tag": master["tag"]},
                                  "b": {"handle": comp_h, "tag": slave["tag"]}}])
        assert out["n_pairs"] == 1 and len(out["contacts"]) == 1, out
        assert out["friction"] == 0.2 and out["nonlinear"] is True, out
        # confirm the constraint object and the flipped solver flags landed
        probe = w.call("run_script", code=(
            "an=_resolve('%s')\n"
            "cons=[o for o in an.Group if o.isDerivedFrom('Fem::ConstraintContact')]\n"
            "sol=[o for o in an.Group if 'Solver' in o.TypeId][0]\n"
            "__result__={'n_contacts':len(cons),'geo':sol.GeometricalNonlinearity,"
            "'outfreq':int(sol.OutputFrequency)}\n" % an["handle"]))["result"]
        assert probe["n_contacts"] == 1, probe
        assert probe["geo"] == "nonlinear", probe
        assert probe["outfreq"] >= 1000000, probe
        print(f"    contact: {probe['n_contacts']} *CONTACT PAIR built, "
              f"solver → {probe['geo']} (final-frame output)")


# --- runner -------------------------------------------------------------------

def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    failures = []
    t0 = time.time()
    for name, fn in _discover():
        t = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:44s} ({time.time() - t:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:44s} ({time.time() - t:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({time.time() - t0:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()
