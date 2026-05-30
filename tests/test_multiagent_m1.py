"""
Layer M1 — multi-agent partition+merge mechanism (no LLM).

See tests/MULTI_AGENT_EVAL.md. Proves the substrate (manifest -> merge -> gates)
is sound and that the gates DISCRIMINATE: every toy's reference solution passes
all gates, and every negative control is caught by the gate that owns its failure
mode. No API key; part of the default suite.

This is the prerequisite for trusting any agent-driven (Layer M2) numbers: never
measure agent reliability against an oracle you haven't shown catches a wrong
answer.

Run: .venv/bin/python3 tests/test_multiagent_m1.py
"""
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from tests.multiagent_toys import TOYS, toy1_build, run_gates  # noqa: E402


def _counts_multiset(bom):
    return sorted(row["count"] for row in bom)


def _assert_fit(toy, variant, gates):
    """A reference build: every gate must come back clean + BOM must be right."""
    assert gates["interference"] == [], (
        f"{toy.key}/{variant.name}: unexpected interference {gates['interference']}")
    assert gates["envelope"] == [], (
        f"{toy.key}/{variant.name}: unexpected envelope violation {gates['envelope']}")
    want = sorted(toy.bom.values())
    got = _counts_multiset(gates["bom"])
    assert got == want, (
        f"{toy.key}/{variant.name}: BOM counts {got} != expected {want} "
        f"(rows={gates['bom']})")


def _assert_caught(toy, variant, gates):
    """A negative control: the gate that owns this failure mode must fire."""
    fired = gates[variant.gate]
    assert fired, (
        f"{toy.key}/{variant.name}: gate '{variant.gate}' did NOT catch "
        f"a build that should fail — {variant.note}. gates={gates}")


def test_toy_gates():
    """Each toy: gates pass the reference, catch every negative control."""
    with tempfile.TemporaryDirectory() as td, Worker() as w:
        tmp = Path(td)
        for toy in TOYS:
            line = []
            for v in toy.variants:
                asm = toy.build(w, tmp, v.name)
                gates = run_gates(w, asm, envelopes=toy.envelopes)
                if v.kind == "fit":
                    _assert_fit(toy, v, gates)
                    line.append(f"{v.name}=PASS")
                else:
                    _assert_caught(toy, v, gates)
                    worst = max((c["interference_mm3"]
                                 for c in gates["interference"]), default=0.0)
                    detail = (f"{len(gates['envelope'])} env"
                              if v.gate == "envelope" else f"{worst:.0f}mm³")
                    line.append(f"{v.name}=CAUGHT({detail})")
            print(f"    {toy.title}\n      " + "  ".join(line))


def test_gate_determinism():
    """Same toy built in two independent workers yields identical gate readings
    (the 'two workers' discipline from test_determinism.py — no state leakage,
    and the real multi-agent shape: separate processes, same contract)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w1:
            a = run_gates(w1, toy1_build(w1, tmp, "reference"))
        with Worker() as w2:
            b = run_gates(w2, toy1_build(w2, tmp, "reference"))
        assert a["interference"] == b["interference"] == [], (a, b)
        assert _counts_multiset(a["bom"]) == _counts_multiset(b["bom"])
        print("    toy1 reference: identical gate readings across two workers")


# --- runner (same shape as test_integration.py) ------------------------------

def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    tests = _discover()
    failures = []
    t0 = time.time()
    for name, fn in tests:
        ts = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:30s} ({time.time()-ts:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:30s} ({time.time()-ts:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({time.time()-t0:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({time.time()-t0:.1f}s) ==")


if __name__ == "__main__":
    main()
