"""Experiment-readiness gate (driftpin/experiment.py) — the free go/no-go that
decides whether a billed multi-agent run on an emergent property would measure
anything. Each test isolates one failure mode so the gate can't silently wave a
worthless experiment through to billing.

Run: .venv/bin/python3 tests/test_experiment_readiness.py
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin.experiment import readiness  # noqa: E402

# A clean emergent toy modelled as plain dicts: a config is {parts:[...], sum, target}.
# The SYSTEM property is sum(parts) == target; each part is locally "valid" (>0).
GOOD = {"parts": [3, 3, 4], "target": 10}     # sums to 10 == target
BAD = {"parts": [3, 3, 3], "target": 10}      # each part fine; system sum 9 != 10


def sum_oracle(cfg):
    s = sum(cfg["parts"])
    ok = s == cfg["target"]
    return {"ok": ok, "score": 1.0 if ok else -(abs(s - cfg["target"]))}


def local_slices(cfg):
    return [{"ok": p > 0} for p in cfg["parts"]]   # every part locally valid


def _score(r):
    return r["score"]


# variants spanning plausible agent outputs: some hit target, some miss
VARIANTS = [{"parts": [3, 3, 4], "target": 10}, {"parts": [2, 4, 4], "target": 10},
            {"parts": [3, 3, 3], "target": 10}, {"parts": [5, 5, 5], "target": 10}]


def test_clean_toy_is_go():
    r = readiness(sum_oracle, GOOD, BAD, local_slices=local_slices,
                  agent_variants=VARIANTS, score=_score, min_margin=1.0)
    assert r["go"], r
    assert all(c["ok"] for c in r["checks"].values() if c["ok"] is not None)


def test_no_discrimination_is_nogo():
    """Oracle that passes both controls — measures nothing."""
    r = readiness(lambda c: {"ok": True, "score": 1.0}, GOOD, BAD,
                  local_slices=local_slices, agent_variants=VARIANTS, score=_score)
    assert not r["go"]
    assert "discriminates" in r["reasons"]


def test_flaky_oracle_is_nogo():
    """A non-deterministic oracle is rejected on the determinism check."""
    state = {"n": 0}

    def flaky(cfg):
        state["n"] += 1
        ok = (state["n"] % 2 == 0) if cfg is GOOD else False
        return {"ok": ok, "score": 1.0 if ok else -1.0}

    r = readiness(flaky, GOOD, BAD, score=_score)
    assert not r["go"] and "deterministic" in r["reasons"], r


def test_knife_edge_margin_is_nogo():
    """Clear verdicts but a sub-threshold score gap — too close to trust."""
    def thin(cfg):
        ok = cfg is GOOD
        return {"ok": ok, "score": 0.4 if ok else -0.1}

    r = readiness(thin, GOOD, BAD, score=_score, min_margin=1.0)
    assert not r["go"] and "margin" in r["reasons"], r["checks"]["margin"]


def test_locally_visible_defect_is_nogo():
    """If a local slice already catches the bad config, the property is not emergent —
    partition/merge has no special story, so reject."""
    bad_local = {"parts": [3, 3, -1], "target": 10}   # negative part = locally invalid

    def disc(cfg):
        return {"ok": cfg is GOOD, "score": 1.0 if cfg is GOOD else -2.0}

    r = readiness(disc, GOOD, bad_local, local_slices=local_slices,
                  agent_variants=VARIANTS, score=_score)
    assert not r["go"] and "emergent" in r["reasons"], r["checks"]["emergent"]


def test_not_agent_determined_is_nogo():
    """Emergent and discriminating, but every plausible agent output yields the same
    system verdict — the property is fixed by the design, not produced by the
    builders. Nothing to measure -> NO-GO."""
    invariant = [{"parts": [3, 3, 3], "target": 10}, {"parts": [2, 3, 4], "target": 99},
                 {"parts": [1, 1, 1], "target": 50}]   # none hit their target
    r = readiness(sum_oracle, GOOD, BAD, local_slices=local_slices,
                  agent_variants=invariant, score=_score)
    assert not r["go"] and "agent_determined" in r["reasons"], r["checks"]["agent_determined"]


def test_missing_inputs_do_not_block_go():
    """emergent/agent_determined are None (not supplied) and don't fail the gate."""
    r = readiness(sum_oracle, GOOD, BAD, score=_score)
    assert r["go"], r
    assert r["checks"]["emergent"]["ok"] is None
    assert r["checks"]["agent_determined"]["ok"] is None


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
    print(f"\n== {len(tests) - failed}/{len(tests)} passed  ({time.time() - t0:.2f}s) ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
