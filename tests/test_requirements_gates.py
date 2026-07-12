"""
Requirements gates (RFC §11.6) — free, no LLM, no key. "The pieces fit" is not
"the product works": the manifest's optional `requirements` block gates the merged
product on mass budget and centre-of-mass window (the always-on, cheap tier built
on mass_properties + the recursive leaf walk). Proves the gate measures the right
numbers, catches an over-budget mass and an off-window CG, reports the numbers on
a pass, and surfaces an unimplemented (physics-tier) requirement as `skipped`
rather than silently passing it.

Run: .venv/bin/python3 tests/test_requirements_gates.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

STEEL = 7.9e-6  # kg/mm^3
_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _block(path, name, s=20):
    with Worker() as w:
        w.call("new_document", name=name)
        w.call("add_primitive", kind="box", w=s, d=s, h=s, name=name)
        w.call("save_document", path=str(path))


def _merge(tmp, requirements):
    """Two 20mm steel cubes at x=0 and x=40 (vol 16000 mm^3, mass 126.4 g,
    CG at [30,10,10]); merge with the given requirements block."""
    _block(tmp / "a.FCStd", "a")
    _block(tmp / "b.FCStd", "b")
    man = {"name": "req", "root": "req.FCStd",
           "components": {"a": {"file": "a.FCStd"}, "b": {"file": "b.FCStd"}},
           "instances": [{"component": "a", "name": "a", "placement": [0, 0, 0]},
                         {"component": "b", "name": "b", "placement": [40, 0, 0]}],
           "requirements": requirements}
    (tmp / "m.json").write_text(json.dumps(man), encoding="utf-8")
    with Worker() as w:
        return w.call("merge_assembly", manifest=str(tmp / "m.json"))


def test_measures_mass_and_cg():
    with tempfile.TemporaryDirectory() as td:
        rep = _merge(Path(td), {"density_kg_mm3": STEEL, "max_mass_g": 200,
                                "cg_window": {"min": [20, 0, 0], "max": [40, 20, 20]}})
        r = rep["requirements"]["report"]
        _check("total volume measured", r["total_volume_mm3"], 16000.0)
        _check("mass computed (16000mm3 steel = 126.4 g)", r["mass_g"], 126.4)
        _check("CG is the volume centroid [30,10,10]", r["cg_mm"], [30.0, 10.0, 10.0])
        _check("requirements met -> ok", rep["ok"], True)
        _check("no violations on a pass", rep["gates"]["requirements"], [])


def test_mass_budget_caught():
    with tempfile.TemporaryDirectory() as td:
        rep = _merge(Path(td), {"density_kg_mm3": STEEL, "max_mass_g": 100})  # < 126.4
        _check("over-budget mass -> NOT ok", rep["ok"], False)
        v = rep["gates"]["requirements"]
        _check("violation names max_mass_g",
               v and v[0]["requirement"] == "max_mass_g", True)


def test_cg_window_caught():
    with tempfile.TemporaryDirectory() as td:
        # CG x = 30, window only allows x in [0,10]
        rep = _merge(Path(td), {"cg_window": {"min": [0, 0, 0], "max": [10, 20, 20]}})
        _check("CG outside window -> NOT ok", rep["ok"], False)
        v = rep["gates"]["requirements"]
        _check("violation names cg_window on the x axis",
               v and v[0]["requirement"] == "cg_window" and v[0]["axes"] == ["x"],
               True)


def test_mass_without_density_is_loud():
    with tempfile.TemporaryDirectory() as td:
        rep = _merge(Path(td), {"max_mass_g": 200})  # no density -> can't compute
        _check("mass budget without density -> NOT ok", rep["ok"], False)
        v = rep["gates"]["requirements"]
        _check("violation flags the missing density",
               v and "density" in v[0].get("error", ""), True)


def test_unimplemented_requirement_is_skipped_not_ignored():
    with tempfile.TemporaryDirectory() as td:
        # the physics tier (FEM modal) is deferred — naming it must SURFACE as
        # skipped, never silently pass (the "unknown key" discipline).
        rep = _merge(Path(td), {"density_kg_mm3": STEEL, "max_mass_g": 200,
                                "min_first_mode_hz": 120})
        _check("tier-1 still passes", rep["ok"], True)
        _check("physics requirement surfaced as skipped",
               rep["requirements"]["skipped"], ["min_first_mode_hz"])


def test_no_requirements_block_is_backcompat():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _block(tmp / "a.FCStd", "a")
        man = {"name": "x", "root": "x.FCStd",
               "components": {"a": {"file": "a.FCStd"}},
               "instances": [{"component": "a", "name": "a", "placement": [0, 0, 0]}]}
        (tmp / "m.json").write_text(json.dumps(man), encoding="utf-8")
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=str(tmp / "m.json"))
        _check("no requirements -> no requirements gate",
               "requirements" not in rep["gates"], True)
        _check("no requirements -> no report block",
               "requirements" not in rep, True)
        _check("still ok", rep["ok"], True)


def main():
    print("== requirements gates — mass / CG over the merged product (no API) ==")
    for t in (test_measures_mass_and_cg, test_mass_budget_caught,
              test_cg_window_caught, test_mass_without_density_is_loud,
              test_unimplemented_requirement_is_skipped_not_ignored,
              test_no_requirements_block_is_backcompat):
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
