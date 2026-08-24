"""
Unit tests for the resolve step (ankusdrive/manifest.py, RFC §11.1) — pure
Python, no FreeCAD, no API. The grid case must reproduce the tchainu eval
oracle exactly: that toy measured partition 2/20 / single 0/20 when agents
were asked to reconcile, and the resolved values below are what a passing
contract hands its builders instead.

Run: .venv/bin/python3 tests/test_manifest_resolve.py
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive.manifest import resolve_constraints  # noqa: E402

# the tchainu contract (tests/test_multiagent_m2.py): nominals sum to 100.0 but
# every one rounds UP on a whole-mm grid -> unreconciled chains hit 102.
TCHAINU_NOMINALS = [12.6, 14.6, 16.6, 18.6, 18.7, 18.9]


def _chain_manifest(nominals=TCHAINU_NOMINALS, grid=1.0, total=100.0):
    comps = {f"seg{i}": {"file": f"seg{i}.FCStd", "parameters": {"len": v}}
             for i, v in enumerate(nominals)}
    spec = {"sum": [f"seg{i}.len" for i in range(len(nominals))], "equals": total}
    if grid is not None:
        spec["grid_mm"] = grid
    return {"name": "chain", "components": comps, "constraints": {"total": spec}}


def _lengths(man):
    return [man["components"][f"seg{i}"]["parameters"]["len"]
            for i in range(len(man["components"]))]


def test_tchainu_grid_reconciliation():
    """The exact eval case: whole-mm grid, all-round-up nominals -> trailing
    segments trimmed, sum exactly 100."""
    out = resolve_constraints(_chain_manifest())
    assert _lengths(out) == [13, 15, 17, 19, 18, 18], _lengths(out)
    assert sum(_lengths(out)) == 100.0
    assert out["resolved"]["total"]["residual"] == 0.0


def test_grid_deficit_adds():
    """Nominals that round DOWN get grid steps added back — one step per
    segment walking from the end, so the correction stays spread out."""
    out = resolve_constraints(_chain_manifest([16.4, 16.4, 16.4, 16.4, 16.4, 16.4]))
    assert sum(_lengths(out)) == 100.0
    assert _lengths(out) == [16, 16, 17, 17, 17, 17], _lengths(out)


def test_no_grid_spreads_residual_equally():
    out = resolve_constraints(_chain_manifest([16.0] * 6, grid=None))
    lens = _lengths(out)
    assert all(abs(v - (16.0 + 4.0 / 6)) < 1e-9 for v in lens), lens
    assert abs(sum(lens) - 100.0) < 1e-9


def test_already_exact_grid_unchanged():
    out = resolve_constraints(_chain_manifest([13, 15, 17, 19, 18, 18]))
    assert _lengths(out) == [13, 15, 17, 19, 18, 18]


def test_deterministic_and_pure():
    man = _chain_manifest()
    a, b = resolve_constraints(man), resolve_constraints(man)
    assert a == b
    # input untouched (deep copy)
    assert man["components"]["seg0"]["parameters"]["len"] == 12.6
    assert "resolved" not in man


def test_infeasible_target_off_grid():
    try:
        resolve_constraints(_chain_manifest(total=100.5))
    except ValueError as e:
        assert "not representable" in str(e)
    else:
        raise AssertionError("off-grid target must raise")


def test_unknown_constraint_kind_raises():
    man = _chain_manifest()
    man["constraints"]["bogus"] = {"product": ["seg0.len"], "equals": 1}
    try:
        resolve_constraints(man)
    except ValueError as e:
        assert "bogus" in str(e)
    else:
        raise AssertionError("unknown kind must raise, never resolve to silence")


def test_bad_ref_raises():
    man = _chain_manifest()
    man["constraints"]["total"]["sum"][0] = "nosuch.len"
    try:
        resolve_constraints(man)
    except ValueError as e:
        assert "nosuch" in str(e)
    else:
        raise AssertionError("unknown component ref must raise")


def test_nonpositive_reconciliation_raises():
    """A target so far below the nominals that trimming would zero a segment."""
    try:
        resolve_constraints(_chain_manifest([1.4] * 6, total=2.0))
    except ValueError as e:
        assert "non-positive" in str(e)
    else:
        raise AssertionError("driving a length to <=0 must raise")


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
