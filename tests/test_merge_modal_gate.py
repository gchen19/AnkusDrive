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

STEEL = {"Name": "Steel-Generic", "YoungsModulus": "210000 MPa",
         "PoissonRatio": "0.30", "Density": "7900 kg/m^3"}
_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _ccx_present():
    return any(shutil.which(n) for n in ("ccx", "ccx_2.21", "ccx_2.20", "ccx_2.19"))


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
    (tmp / "m.json").write_text(json.dumps(man))
    return tmp / "m.json"


def _cantilever_oracle(w, prong_len):
    return w.call("beam_modal", length_mm=prong_len, width_mm=_PRONG_Y,
                  height_mm=_PRONG_X, boundary="cantilever", n_modes=2,
                  youngs_gpa=210, density_kg_m3=7900)["first_mode_hz"]


_FIXTURE = {"clamp": {"axis": "z", "side": "min", "tol_mm": 0.5}}
_FLOOR_HZ = 600.0


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
            rep = w.call("merge_assembly", manifest=str(m), _timeout=300.0)
        fm = rep["requirements"]["report"]["first_mode"]
        f1 = fm["measured_first_mode_hz"]
        print(f"    stiff fork: FEM f1={f1:.1f} Hz  cantilever oracle={oracle:.1f} Hz "
              f"ratio={f1 / oracle:.2f}  (nodes={fm['mesh']['nodes']})")
        _check("stiff fork clears the 600 Hz floor -> ok", rep["ok"], True)
        _check("no requirements violation on a pass", rep["gates"]["requirements"], [])
        _check("gate reports pass", fm["pass"], True)
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
            rep = w.call("merge_assembly", manifest=str(m), _timeout=300.0)
        fm = rep["requirements"]["report"]["first_mode"]
        f1 = fm["measured_first_mode_hz"]
        print(f"    floppy fork: FEM f1={f1:.1f} Hz  cantilever oracle={oracle:.1f} Hz "
              f"ratio={f1 / oracle:.2f}")
        _check("floppy fork fails the 600 Hz floor -> NOT ok", rep["ok"], False)
        _check("gate reports fail", fm["pass"], False)
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
    the gate can't solve. It must fail LOUD (a violation), never raise, never pass."""
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


def main():
    print("== merge-time physics requirements gate — min_first_mode_hz (#172) ==")
    # Fixture/skip/stub paths need no solver; run them always.
    light = (test_bare_requirement_still_skipped,
             test_declared_but_incomplete_fails_loud_not_raise,
             test_tied_bonding_is_skipped_stub)
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
    print(f"\n== {_PASS}/{_PASS + _FAIL} checks passed "
          f"({'OK' if not _FAIL else str(_FAIL) + ' FAILED'}) ==")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
