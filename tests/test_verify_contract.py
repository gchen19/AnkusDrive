"""
verify_contract (RFC §11.3) — free, no LLM, no key. A builder's build-time
self-check against its own manifest slice, run BEFORE save so a contract
violation is caught locally instead of after a fan-in merge. Proves each check
DISCRIMINATES (correct part -> ok; each violation -> a passed=False row) and
that the tool never raises, mirroring verify_intent.

Checks covered: envelope (local bbox), interfaces (published + frame within
tol), features (gear module, bore Ø, extent), intent passthrough.

Run: .venv/bin/python3 tests/test_verify_contract.py
"""
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got}, want {want}")


def _rows_failed(rep):
    return {r["check"] for r in rep["results"] if not r["passed"]}


def test_envelope():
    with Worker() as w:
        w.call("new_document", name="p")
        r = w.call("add_primitive", kind="box", w=40, d=40, h=20, name="b")
        h = r["handle"]
        env = {"min": [0, 0, 0], "max": [50, 50, 25]}
        ok = w.call("verify_contract", handle=h, contract={"envelope": env})
        _check("envelope: fits -> ok", ok["ok"], True)
        env2 = {"min": [0, 0, 0], "max": [50, 50, 10]}  # part is 20 tall
        bad = w.call("verify_contract", handle=h, contract={"envelope": env2})
        _check("envelope: too tall -> caught", _rows_failed(bad), {"envelope"})


def test_interfaces():
    with Worker() as w:
        w.call("new_document", name="p")
        r = w.call("add_primitive", kind="box", w=40, d=40, h=8, name="b")
        h = r["handle"]
        w.call("publish_interface", handle=h, name="seat",
               frame={"origin": [20, 20, 8], "z_axis": [0, 0, 1]})
        contract_ok = {"interfaces": {
            "seat": {"origin": [20, 20, 8], "z_axis": [0, 0, 1], "tol_mm": 0.5}}}
        _check("iface: published & matching -> ok",
               w.call("verify_contract", handle=h, contract=contract_ok)["ok"], True)
        # contracted but never published
        miss = w.call("verify_contract", handle=h, contract={"interfaces": {
            "mount": {"origin": [0, 0, 0]}}})
        _check("iface: missing -> caught", _rows_failed(miss), {"interface:mount"})
        # published in the wrong place
        off = w.call("verify_contract", handle=h, contract={"interfaces": {
            "seat": {"origin": [20, 20, 5], "tol_mm": 0.5}}})  # 3mm off in Z
        _check("iface: wrong origin -> caught", _rows_failed(off), {"interface:seat"})
        # published rotated
        tilt = w.call("verify_contract", handle=h, contract={"interfaces": {
            "seat": {"origin": [20, 20, 8], "z_axis": [0, 1, 0],
                     "angle_tol_deg": 1.0}}})
        _check("iface: wrong axis -> caught", _rows_failed(tilt), {"interface:seat"})


def test_features():
    # gear: module 2, teeth 24 -> pitch radius 24
    with Worker() as w:
        w.call("new_document", name="g")
        h = w.call("add_gear", teeth=24, module=2.0, height=6, name="g")["handle"]
        good = w.call("verify_contract", handle=h, contract={"features": [
            {"kind": "gear", "module_mm": 2.0, "teeth": 24, "tol_mm": 0.3}]})
        _check("feature gear: correct -> ok", good["ok"], True)
        wrong = w.call("verify_contract", handle=h, contract={"features": [
            {"name": "g", "kind": "gear", "module_mm": 2.0, "teeth": 30}]})
        _check("feature gear: wrong teeth -> caught", _rows_failed(wrong), {"feature:g"})
    # bore: a Ø16 hole through a plate
    with Worker() as w:
        w.call("new_document", name="pl")
        w.call("add_primitive", kind="box", w=60, d=60, h=10, name="box")
        w.call("add_primitive", kind="cylinder", r=8, h=30,
               placement=[30, 30, -10], name="bore")
        h = w.call("boolean_op", op="cut", base="box_1", tool="cylinder_1")["handle"]
        good = w.call("verify_contract", handle=h, contract={"features": [
            {"name": "bore", "kind": "bore", "diameter_mm": 16.0, "tol_mm": 0.1}]})
        _check("feature bore: Ø16 present -> ok", good["ok"], True)
        wrong = w.call("verify_contract", handle=h, contract={"features": [
            {"name": "bore", "kind": "bore", "diameter_mm": 12.0, "tol_mm": 0.1}]})
        _check("feature bore: Ø12 absent -> caught", _rows_failed(wrong), {"feature:bore"})
    # extent: a tchain segment built to a resolved length
    with Worker() as w:
        w.call("new_document", name="seg")
        h = w.call("add_primitive", kind="box", w=17, d=20, h=10, name="seg")["handle"]
        good = w.call("verify_contract", handle=h, contract={"features": [
            {"name": "len", "kind": "extent", "axis": "x", "length_mm": 17.0,
             "tol_mm": 0.2}]})
        _check("feature extent: 17mm -> ok", good["ok"], True)
        wrong = w.call("verify_contract", handle=h, contract={"features": [
            {"name": "len", "kind": "extent", "axis": "x", "length_mm": 18.0,
             "tol_mm": 0.2}]})
        _check("feature extent: wrong length -> caught", _rows_failed(wrong),
               {"feature:len"})


def test_intent_passthrough_and_never_raises():
    with Worker() as w:
        w.call("new_document", name="p")
        h = w.call("add_primitive", kind="box", w=10, d=10, h=10, name="b")["handle"]
        w.call("declare_intent", handle=h, contract={"watertight": True})
        good = w.call("verify_contract", handle=h, contract={"intent": True})
        _check("intent: watertight box -> ok", good["ok"], True)
        # intent requested but none declared -> failed row, NOT an exception
        with Worker() as w2:
            w2.call("new_document", name="q")
            h2 = w2.call("add_primitive", kind="box", w=5, d=5, h=5, name="b")["handle"]
            r = w2.call("verify_contract", handle=h2, contract={"intent": True})
            _check("intent: none declared -> caught, no raise", _rows_failed(r),
                   {"intent"})
        # empty contract -> ok True, empty results (nothing to check)
        empty = w.call("verify_contract", handle=h, contract={})
        _check("empty contract -> ok, no rows", (empty["ok"], empty["results"]),
               (True, []))


def test_combined_slice():
    """A realistic peg slice: envelope + bore + a published bore-axis frame, all
    in one call — the build-time mirror of what merge would later check."""
    with Worker() as w:
        w.call("new_document", name="plate")
        w.call("add_primitive", kind="box", w=60, d=60, h=10, name="box")
        w.call("add_primitive", kind="cylinder", r=8, h=30,
               placement=[30, 30, -10], name="bore")
        h = w.call("boolean_op", op="cut", base="box_1", tool="cylinder_1")["handle"]
        w.call("publish_interface", handle=h, name="bore_axis",
               frame={"origin": [30, 30, 0], "z_axis": [0, 0, 1]})
        slice_ = {
            "envelope": {"min": [0, 0, 0], "max": [60, 60, 10]},
            "interfaces": {"bore_axis": {"origin": [30, 30, 0], "tol_mm": 0.5}},
            "features": [{"name": "bore", "kind": "bore", "diameter_mm": 16.0,
                          "tol_mm": 0.1}]}
        rep = w.call("verify_contract", handle=h, contract=slice_)
        _check("combined slice: all green -> ok", rep["ok"], True)
        _check("combined slice: 3 checks ran", len(rep["results"]), 3)


def main():
    print("== verify_contract — self-check discrimination (no API) ==")
    for t in (test_envelope, test_interfaces, test_features,
              test_intent_passthrough_and_never_raises, test_combined_slice):
        t()
    print(f"\n== {_PASS}/{_PASS + _FAIL} checks passed "
          f"({'OK' if not _FAIL else str(_FAIL) + ' FAILED'}) ==")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
