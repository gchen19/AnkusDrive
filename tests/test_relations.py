"""
Unit tests for the relation layer (ankusdrive/relations.py, issue #137 / RFC
docs/DESIGN_HIERARCHY.md §2.1 A2) — pure Python, no FreeCAD, no API key.

Two-sided gate validation (the house standard, MULTI_AGENT.md §11.x):

  REFERENCE (must pass)
    * ``pitch_d = module * teeth`` computes the known value (48.0);
    * a multi-step DAG (center distance from two pitch diameters) resolves;
    * a driven ``comp.param`` lands as a literal in the component slice;
    * a table lookup resolves; resolution is deterministic + pure (deep copy);
    * the generalized resolve_manifest still resolves a legacy sum/grid contract
      (back-compat) — and relations + constraints compose in one pass.

  NEGATIVE CONTROLS (each must be caught LOUDLY, before any fan-out)
    * a value driven by BOTH a parameter literal and a relation;
    * a ``comp.param`` driven by BOTH a component literal (table/row) and a
      relation — Creo's "table OR relation, never both";
    * a dependency CYCLE;
    * an UNKNOWN reference;
    * an INFEASIBLE expression (division by zero / lookup miss).

Run: .venv/bin/python3 tests/test_relations.py
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive.relations import resolve_relations            # noqa: E402
from ankusdrive.manifest import resolve_manifest               # noqa: E402


def _raises(fn, *needles):
    """Assert fn() raises ValueError whose message contains every needle."""
    try:
        fn()
    except ValueError as e:
        msg = str(e)
        for n in needles:
            assert n in msg, f"missing {n!r} in error: {msg}"
        return
    raise AssertionError(f"expected ValueError containing {needles}, none raised")


# --- reference: the headline formula -----------------------------------------

def test_pitch_d_known_value():
    man = {"name": "gear",
           "parameters": {"module": 2.0, "teeth": 24},
           "relations": {"pitch_d": "module * teeth"}}
    out = resolve_relations(man)
    assert out["relations_resolved"]["pitch_d"] == 48.0, out["relations_resolved"]


def test_multi_step_dag_center_distance():
    """A two-level DAG: each pitch diameter, then the center distance from both —
    exactly the gearbox center-distance case the DoD calls out."""
    man = {"parameters": {"module": 2.0, "teeth": 24, "pinion_teeth": 12},
           "relations": {
               "pitch_d": "module * teeth",
               "pinion_pitch_d": "module * pinion_teeth",
               "center_distance": "(pitch_d + pinion_pitch_d) / 2"}}
    out = resolve_relations(man)
    r = out["relations_resolved"]
    assert r["pitch_d"] == 48.0 and r["pinion_pitch_d"] == 24.0
    assert r["center_distance"] == 36.0, r


def test_driven_value_lands_in_component_slice():
    man = {"parameters": {"module": 2.0, "teeth": 24},
           "relations": {"gearA.pitch_mm": "module * teeth"},
           "components": {"gearA": {"file": "gearA.FCStd"}}}
    out = resolve_relations(man)
    assert out["components"]["gearA"]["parameters"]["pitch_mm"] == 48.0
    assert out["relations_resolved"]["gearA.pitch_mm"] == 48.0


def test_component_literal_is_a_driving_input():
    """A component's own literal parameter is a DRIVING input a formula may read."""
    man = {"parameters": {"clearance": 1.0},
           "relations": {"gearB.outer": "gearA.bore + clearance"},
           "components": {"gearA": {"file": "a.FCStd", "parameters": {"bore": 10.0}},
                          "gearB": {"file": "b.FCStd"}}}
    out = resolve_relations(man)
    assert out["components"]["gearB"]["parameters"]["outer"] == 11.0


def test_table_lookup():
    man = {"parameters": {"teeth": 24},
           "tables": {"bore": {"24": 10.0, "12": 6.0}},
           "relations": {"bore_mm": "lookup('bore', teeth)"}}
    out = resolve_relations(man)
    assert out["relations_resolved"]["bore_mm"] == 10.0


def test_arithmetic_and_functions():
    man = {"parameters": {"a": 3, "b": 4},
           "relations": {"hyp2": "a**2 + b**2",
                         "big": "max(a, b)",
                         "rnd": "round(a / b, 2)"}}
    out = resolve_relations(man)["relations_resolved"]
    assert out["hyp2"] == 25 and out["big"] == 4 and out["rnd"] == 0.75, out


def test_deterministic_and_pure():
    man = {"parameters": {"module": 2.0, "teeth": 24},
           "relations": {"gearA.pitch_mm": "module * teeth"},
           "components": {"gearA": {"file": "a.FCStd"}}}
    a, b = resolve_relations(man), resolve_relations(man)
    assert a == b
    # input untouched (deep copy)
    assert "relations_resolved" not in man
    assert "parameters" not in man["components"]["gearA"]


def test_no_relations_is_backcompat_passthrough():
    man = {"name": "plain", "components": {"a": {"file": "a.FCStd"}}}
    out = resolve_relations(man)
    assert out == man and "relations_resolved" not in out


# --- back-compat: the generalized resolve_manifest still does sum/grid --------

def test_resolve_manifest_legacy_sum_grid():
    nominals = [12.6, 14.6, 16.6, 18.6, 18.7, 18.9]
    comps = {f"seg{i}": {"file": f"seg{i}.FCStd", "parameters": {"len": v}}
             for i, v in enumerate(nominals)}
    man = {"name": "chain", "components": comps,
           "constraints": {"total": {"sum": [f"seg{i}.len" for i in range(6)],
                                      "equals": 100.0, "grid_mm": 1.0}}}
    out = resolve_manifest(man)
    lens = [out["components"][f"seg{i}"]["parameters"]["len"] for i in range(6)]
    assert lens == [13, 15, 17, 19, 18, 18], lens
    assert out["resolved"]["total"]["residual"] == 0.0


def test_resolve_manifest_relations_then_constraints_compose():
    """Relations feed a literal into a slice; a sum constraint then reconciles
    over it — one pass, relations first."""
    man = {"parameters": {"base": 13.0},
           "relations": {"seg0.len": "base"},
           "components": {"seg0": {"file": "s0.FCStd"},
                          "seg1": {"file": "s1.FCStd", "parameters": {"len": 90.0}}},
           "constraints": {"total": {"sum": ["seg0.len", "seg1.len"],
                                     "equals": 100.0}}}
    out = resolve_manifest(man)
    assert out["relations_resolved"]["seg0.len"] == 13.0
    lens = [out["components"][f"seg{i}"]["parameters"]["len"] for i in range(2)]
    assert abs(sum(lens) - 100.0) < 1e-9, lens


# --- negative controls -------------------------------------------------------

def test_double_driven_param_and_relation():
    man = {"parameters": {"pitch_d": 50.0, "module": 2.0, "teeth": 24},
           "relations": {"pitch_d": "module * teeth"}}
    _raises(lambda: resolve_relations(man), "pitch_d", "never both")


def test_double_driven_table_and_relation():
    """A component param given as a literal (the table/row value) AND targeted by
    a relation — the Creo 'table OR relation' invariant."""
    man = {"parameters": {"module": 2.0, "teeth": 24},
           "relations": {"gearA.pitch_mm": "module * teeth"},
           "components": {"gearA": {"file": "a.FCStd",
                                    "parameters": {"pitch_mm": 50.0}}}}
    _raises(lambda: resolve_relations(man), "gearA.pitch_mm", "never both")


def test_cycle_caught():
    man = {"relations": {"a": "b + 1", "b": "c + 1", "c": "a + 1"}}
    _raises(lambda: resolve_relations(man), "cycle", "a", "b", "c")


def test_self_cycle_caught():
    man = {"relations": {"x": "x + 1"}}
    _raises(lambda: resolve_relations(man), "cycle", "x")


def test_unknown_reference_caught():
    man = {"parameters": {"module": 2.0},
           "relations": {"pitch_d": "module * teeth"}}   # teeth undefined
    _raises(lambda: resolve_relations(man), "unknown reference", "teeth")


def test_unknown_component_target_caught():
    man = {"parameters": {"x": 1.0},
           "relations": {"ghost.p": "x"},
           "components": {"real": {"file": "r.FCStd"}}}
    _raises(lambda: resolve_relations(man), "unknown component", "ghost")


def test_infeasible_division_by_zero():
    man = {"parameters": {"a": 1.0, "z": 0.0},
           "relations": {"q": "a / z"}}
    _raises(lambda: resolve_relations(man), "division by zero")


def test_infeasible_lookup_miss():
    man = {"parameters": {"teeth": 99},
           "tables": {"bore": {"24": 10.0}},
           "relations": {"bore_mm": "lookup('bore', teeth)"}}
    _raises(lambda: resolve_relations(man), "lookup miss")


def test_non_whitelisted_call_rejected():
    man = {"parameters": {"a": 1.0},
           "relations": {"q": "__import__('os').getpid()"}}
    _raises(lambda: resolve_relations(man))


def test_non_string_formula_rejected():
    man = {"relations": {"q": 5}}
    _raises(lambda: resolve_relations(man), "must be a string")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    t0 = time.time()
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n== {len(tests) - failed}/{len(tests)} passed  "
          f"({time.time() - t0:.2f}s) ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
