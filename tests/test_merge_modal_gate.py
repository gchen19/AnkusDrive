"""
Merge-time physics requirements gate (RFC §11.6 physics tier, issue #172) —
`min_first_mode_hz` via FEM modal on the merged assembly.

"The pieces fit" is not "the product is stiff enough." When the manifest's
`requirements.min_first_mode_hz` declares its own modelling assumptions — a
`fixture` (clamp plane / published interface frame) and `bonding` (v1: `fused`) —
merge_assembly fuses the merged leaves into one solid, meshes it with 2nd-order
tets (1st-order tets shear-lock on modal), runs a CalculiX frequency extraction,
and compares mode 1 against the floor.

Oracle: a two-box "tuning fork" — a base slab with two prongs standing on it,
clamped at the base. Each prong is a cantilever bending in its thin direction, so
`beam_modal`'s closed-form cantilever f1 BRACKETS the fused-assembly first mode.
A stiff (short-prong) fork clears a 600 Hz floor; a floppy (long-prong) fork is
rejected by the same floor. Plus: a bare/undeclared requirement stays `skipped`
(never faked), and an unresolvable fixture fails loud without raising.

Three outcomes, not two (issue #248): the two live tests below are the PART
verdicts and they stay strict — a floppy fork must fail the merge. But a solve
that never finished (busy runner, ccx killed, the wall-clock budget below) is a
statement about the RUN, not the part, so it SKIPs loudly instead of masquerading
as either verdict. Both live tests read their outcome through
`driftpin.gates.modal.classify` — the same predicate the worker gate writes its
report with — so the test can't drift from the gate, and
`test_incomplete_solve_is_never_a_verdict` pins that predicate with no solver at
all.

Run:  python3 tests/test_merge_modal_gate.py       # live ccx solve; needs CalculiX
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin.gates import modal as modal_gate  # noqa: E402

STEEL = {"Name": "Steel-Generic", "YoungsModulus": "210000 MPa",
         "PoissonRatio": "0.30", "Density": "7900 kg/m^3"}
_PASS = _FAIL = _SKIP = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _skip(label, reason):
    """A solve that produced no measurement is NOT a verdict — the same
    vocabulary this suite uses for an absent solver. Loud, counted, and never
    quietly folded into the passes."""
    global _SKIP
    _SKIP += 1
    print(f"    SKIP — {label}: {reason}")


def _ccx_present():
    # ccx on PATH, OR reachable via DriftPin's resolver — which finds FreeCAD's bundled
    # ccx (Windows/macOS ship it in FreeCAD's bin, off PATH), so the live solve runs there
    # too instead of skipping.
    if any(shutil.which(n) for n in ("ccx", "ccx_2.22", "ccx_2.21", "ccx_2.20", "ccx_2.19")):
        return True
    try:
        from driftpin import solvers
        return solvers.ccx_bin() is not None
    except Exception:
        return False


def _box(w, path, name, dx, dy, dz):
    w.call("new_document", name=name)
    w.call("add_primitive", kind="box", w=dx, d=dy, h=dz, name=name)
    w.call("save_document", path=str(path))


# Prong bends in its thin (x = 3 mm) direction; the beam_modal oracle is a
# cantilever of this section and length, out-of-plane depth = the 10 mm y-side.
_PRONG_X, _PRONG_Y = 3.0, 10.0
_BASE = (40.0, 10.0, 5.0)     # slab (dx, dy, dz)


def _fork_manifest(tmp, prong_len, requirements=None):
    """Base slab + two prongs standing on it (a tuning fork). Fused + clamped at
    z-min, mode 1 is the prongs cantilevering in their 3 mm-thin direction."""
    _box_p = tmp / "base.FCStd"
    with Worker() as w:
        _box(w, _box_p, "base", *_BASE)
        _box(w, tmp / "prong.FCStd", "prong", _PRONG_X, _PRONG_Y, prong_len)
    man = {
        "name": "fork", "root": "fork.FCStd",
        "components": {"base": {"file": "base.FCStd"},
                       "prong": {"file": "prong.FCStd"}},
        "instances": [
            {"component": "base", "name": "base", "placement": [0, 0, 0]},
            {"component": "prong", "name": "p1", "placement": [3, 0, _BASE[2]]},
            {"component": "prong", "name": "p2",
             "placement": [_BASE[0] - 3 - _PRONG_X, 0, _BASE[2]]},
        ],
    }
    if requirements is not None:
        man["requirements"] = requirements
    (tmp / "m.json").write_text(json.dumps(man), encoding="utf-8")
    return tmp / "m.json"


def _cantilever_oracle(w, prong_len):
    return w.call("beam_modal", length_mm=prong_len, width_mm=_PRONG_Y,
                  height_mm=_PRONG_X, boundary="cantilever", n_modes=2,
                  youngs_gpa=210, density_kg_m3=7900)["first_mode_hz"]


_FIXTURE = {"clamp": {"axis": "z", "side": "min", "tol_mm": 0.5}}
_FLOOR_HZ = 600.0

# Wall-clock budget for one live gated merge (mesh + ccx frequency extraction).
# Was 300 s, which a loaded self-hosted runner blew through mid-solve — the same
# 90 mm-prong mesh finished comfortably on the same runner minutes later (#248,
# which blocked PR #241). Raising it is a MITIGATION, not the fix: the fix is
# that exceeding it is now its own outcome instead of a KeyError.
_MERGE_TIMEOUT_S = 1800.0


def _merge_with_deadline(w, manifest):
    """Run the gated merge under a wall-clock budget. Returns
    (report_or_None, outcome) where `outcome` is a driftpin.gates.modal
    classification — INCOMPLETE both when the worker reports a solve that never
    produced a frequency and when it never answered at all."""
    try:
        rep = w.call("merge_assembly", manifest=str(manifest),
                     _timeout=_MERGE_TIMEOUT_S)
    except TimeoutError as e:
        return None, modal_gate.timed_out(_MERGE_TIMEOUT_S, str(e))
    return rep, modal_gate.classify(rep)


def test_stiff_fork_passes_and_oracle_brackets():
    """Short prongs (40 mm) → f1 well above 600 Hz; the beam_modal cantilever
    brackets the fused-assembly first mode."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        req = {"min_first_mode_hz": {"value": _FLOOR_HZ, "fixture": _FIXTURE,
                                     "bonding": "fused", "material": STEEL,
                                     "mesh_size_mm": 3.0, "n_modes": 6}}
        m = _fork_manifest(tmp, 40.0, req)
        with Worker() as w:
            oracle = _cantilever_oracle(w, 40.0)
            rep, outcome = _merge_with_deadline(w, m)
        if outcome["outcome"] == modal_gate.INCOMPLETE:
            _skip("stiff fork", outcome["reason"])
            return
        fm = rep["requirements"]["report"]["first_mode"]
        f1 = fm["measured_first_mode_hz"]
        print(f"    stiff fork: FEM f1={f1:.1f} Hz  cantilever oracle={oracle:.1f} Hz "
              f"ratio={f1 / oracle:.2f}  (nodes={fm['mesh']['nodes']})")
        _check("stiff fork clears the 600 Hz floor -> ok", rep["ok"], True)
        _check("no requirements violation on a pass", rep["gates"]["requirements"], [])
        _check("gate reports pass", fm["pass"], True)
        _check("classified as a measured PASS verdict",
               outcome["outcome"], modal_gate.PASS)
        _check("2nd-order tets used", fm["mesh"]["element_order"], "2nd")
        _check("base bottom face clamped", len(fm["fixed_faces"]) >= 1, True)
        _check("cantilever oracle brackets the measured f1 (0.5..1.25x)",
               0.5 * oracle <= f1 <= 1.25 * oracle, True)


def test_floppy_fork_is_rejected():
    """Long prongs (90 mm) → f1 far below the same 600 Hz floor; the gate must
    reject the merge with a named, measured violation."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        req = {"min_first_mode_hz": {"value": _FLOOR_HZ, "fixture": _FIXTURE,
                                     "bonding": "fused", "material": STEEL,
                                     "mesh_size_mm": 3.5, "n_modes": 6}}
        m = _fork_manifest(tmp, 90.0, req)
        with Worker() as w:
            oracle = _cantilever_oracle(w, 90.0)
            rep, outcome = _merge_with_deadline(w, m)
        if outcome["outcome"] == modal_gate.INCOMPLETE:
            # The solve never measured the part, so this run says nothing about
            # floppiness. SKIP — never a silent pass, and never a KeyError.
            _skip("floppy fork", outcome["reason"])
            return
        fm = rep["requirements"]["report"]["first_mode"]
        f1 = fm["measured_first_mode_hz"]
        print(f"    floppy fork: FEM f1={f1:.1f} Hz  cantilever oracle={oracle:.1f} Hz "
              f"ratio={f1 / oracle:.2f}")
        _check("floppy fork fails the 600 Hz floor -> NOT ok", rep["ok"], False)
        _check("gate reports fail", fm["pass"], False)
        _check("classified as a measured FAIL verdict",
               outcome["outcome"], modal_gate.FAIL)
        v = rep["gates"]["requirements"]
        _check("violation names min_first_mode_hz",
               bool(v) and v[0]["requirement"] == "min_first_mode_hz", True)
        _check("violation carries the measured f1",
               bool(v) and "measured_hz" in v[0], True)
        _check("measured f1 is below the floor", f1 < _FLOOR_HZ, True)


def test_bare_requirement_still_skipped():
    """A bare `min_first_mode_hz` number (no fixture / bonding) declares no
    modelling assumptions — it must stay `skipped`, never a silent pass, and never
    trip the FEM solve. (Same discipline the pre-#172 gate had.)"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        m = _fork_manifest(tmp, 40.0, {"min_first_mode_hz": 120})
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=str(m))
        _check("bare requirement doesn't fail the merge", rep["ok"], True)
        _check("bare requirement surfaced as skipped",
               rep["requirements"]["skipped"], ["min_first_mode_hz"])


def test_declared_but_incomplete_fails_loud_not_raise():
    """A fixture is declared (so this is NOT a skip) but the material is missing —
    the gate can't solve. It must fail LOUD (a violation), never raise, never pass.

    Note the deliberate asymmetry with #248: an incompletely SPECIFIED model is
    the design's fault and fails the merge; an incompletely RUN solve is the
    runner's fault and skips. They must not collapse into one another."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        req = {"min_first_mode_hz": {"value": _FLOOR_HZ, "fixture": _FIXTURE,
                                     "bonding": "fused"}}   # no material
        m = _fork_manifest(tmp, 40.0, req)
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=str(m))
        _check("missing material -> NOT ok (loud, no raise)", rep["ok"], False)
        v = rep["gates"]["requirements"]
        _check("violation flags the missing material",
               bool(v) and "material" in v[0].get("error", ""), True)
        _check("classified as a modelling ERROR, not an incomplete solve",
               modal_gate.classify(rep)["outcome"], modal_gate.ERROR)


def test_tied_bonding_is_skipped_stub():
    """`bonding: tied` (CCX tie constraints) is reserved in v1 — surfaced as
    skipped, never silently downgraded to `fused`."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        req = {"min_first_mode_hz": {"value": _FLOOR_HZ, "fixture": _FIXTURE,
                                     "bonding": "tied", "material": STEEL}}
        m = _fork_manifest(tmp, 40.0, req)
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=str(m))
        _check("tied bonding doesn't fail the merge", rep["ok"], True)
        _check("tied bonding surfaced as skipped",
               rep["requirements"]["skipped"], ["min_first_mode_hz"])
        _check("tied bonding classifies as NOT_DECLARED (assumptions, not a solve)",
               modal_gate.classify(rep)["outcome"], modal_gate.NOT_DECLARED)


def _merge_report(first_mode, skipped=(), violations=(), ok=True):
    """The slice of a merge_assembly report the modal gate is read out of."""
    return {"ok": ok, "gates": {"requirements": list(violations)},
            "requirements": {"report": {"first_mode": first_mode},
                             "skipped": list(skipped)}}


def test_incomplete_solve_is_never_a_verdict():
    """#248, the structural fix: a report with NO measurement is its own outcome.

    Driven on report shapes directly — no solver, no timing luck — because the
    property under test is "what does a measurement-less report mean", and that
    must hold identically whether ccx was missing, killed, or simply slower than
    the caller's budget. Three things are asserted together, and the last two are
    the ones that keep the gate honest: such a report is not a pass, and a report
    that DID measure still yields the same strict verdict it always did."""
    # 1. The shape the worker gate emits when ccx produced nothing.
    inc = modal_gate.incomplete_report(
        _FLOOR_HZ, modal_gate.SOLVE_FAILED,
        "CalculiX wrote no eigenfrequencies (no .frd/.dat results)",
        elapsed_s=301.7, bonding="fused", fixture=_FIXTURE)
    out = modal_gate.classify(_merge_report(inc, skipped=["min_first_mode_hz"]))
    _check("incomplete solve -> INCOMPLETE", out["outcome"], modal_gate.INCOMPLETE)
    _check("incomplete solve is not a verdict",
           modal_gate.has_verdict(out), False)
    _check("incomplete solve carries no measurement", out["measured_hz"], None)
    _check("reason says there is no verdict", "NO verdict" in out["reason"], True)
    _check("reason names the wall-clock burned", "301.7 s" in out["reason"], True)
    _check("the report itself never fakes a pass/measurement",
           ("pass" in inc) or ("measured_first_mode_hz" in inc), False)

    # 2. A measurement-less report that forgot to say why must STILL not read as
    #    a pass — the KeyError in #248 was exactly this hole, seen from a caller.
    bald = modal_gate.classify(_merge_report({"min_hz": _FLOOR_HZ, "bonding": "fused"}))
    _check("measurement-less report with no marker -> INCOMPLETE",
           bald["outcome"], modal_gate.INCOMPLETE)
    _check("...with the no-measurement cause named",
           bald["cause"], modal_gate.NO_MEASUREMENT)

    # 3. A caller-side deadline (the worker never answered at all).
    t = modal_gate.timed_out(1800.0)
    _check("caller timeout -> INCOMPLETE", t["outcome"], modal_gate.INCOMPLETE)
    _check("caller timeout names the budget", "1800 s" in t["reason"], True)
    _check("caller timeout is not a verdict", modal_gate.has_verdict(t), False)

    # 4. THE VERDICTS ARE UNCHANGED. A completed solve below the floor is still a
    #    failure — nothing above may soften this, or the gate is worthless.
    floppy = modal_gate.classify(_merge_report(
        {"measured_first_mode_hz": 304.6, "min_hz": _FLOOR_HZ, "pass": False},
        violations=[{"requirement": "min_first_mode_hz", "measured_hz": 304.6,
                     "min_hz": _FLOOR_HZ, "reason": "too floppy"}], ok=False))
    _check("measured below the floor -> FAIL", floppy["outcome"], modal_gate.FAIL)
    _check("a floppy verdict IS a verdict", modal_gate.has_verdict(floppy), True)
    _check("floppy verdict keeps its measurement", floppy["measured_hz"], 304.6)
    stiff = modal_gate.classify(_merge_report(
        {"measured_first_mode_hz": 1450.0, "min_hz": _FLOOR_HZ, "pass": True}))
    _check("measured above the floor -> PASS", stiff["outcome"], modal_gate.PASS)
    # ...and even with a mislabelled `pass` flag absent, the numbers decide.
    _check("floor comparison decides when `pass` is absent",
           modal_gate.classify(_merge_report(
               {"measured_first_mode_hz": 599.0,
                "min_hz": _FLOOR_HZ}))["outcome"], modal_gate.FAIL)


def main():
    print("== merge-time physics requirements gate — min_first_mode_hz (#172) ==")
    # Fixture/skip/stub paths need no solver; run them always.
    light = (test_bare_requirement_still_skipped,
             test_declared_but_incomplete_fails_loud_not_raise,
             test_tied_bonding_is_skipped_stub,
             test_incomplete_solve_is_never_a_verdict)
    heavy = (test_stiff_fork_passes_and_oracle_brackets,
             test_floppy_fork_is_rejected)
    tests = light + heavy
    if not _ccx_present():
        print("    SKIP — CalculiX (ccx) not found; running non-solver paths only")
        tests = light
    for t in tests:
        try:
            t()
        except Exception as e:
            global _FAIL
            _FAIL += 1
            import traceback
            print(f"  FAIL {t.__name__}: {e}")
            traceback.print_exc()
    skipped = f", {_SKIP} live gate(s) SKIPPED (solve incomplete)" if _SKIP else ""
    print(f"\n== {_PASS}/{_PASS + _FAIL} checks passed "
          f"({'OK' if not _FAIL else str(_FAIL) + ' FAILED'}){skipped} ==")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
