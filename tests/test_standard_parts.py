"""
Standard / library parts (RFC §11.5) — free, no LLM, no key. A component may be
GENERATED from a spec (`library: {tool, spec}`) instead of built by an agent;
merge_assembly makes it on the fly from the deterministic standard-part tools — no
builder, no owner. Proves: generation + linking + gating works; it's deterministic
and idempotent; the generated geometry matches the spec; the lockfile keys it by
spec hash (computed, can't drift) and flags only a spec change; and a bad/
non-allow-listed spec fails loudly.

Run: .venv/bin/python3 tests/test_standard_parts.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _plate(path, hole_r=3.3):
    with Worker() as w:
        w.call("new_document", name="plate")
        w.call("add_primitive", kind="box", w=60, d=60, h=10, name="box")
        w.call("add_primitive", kind="cylinder", r=hole_r, h=30,
               placement=[30, 30, -10], name="hole")
        w.call("boolean_op", op="cut", base="box_1", tool="cylinder_1")
        w.call("save_document", path=str(path))


def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _bolted_manifest(tmp, size="M6", length=20, place_bolt=(30, 30, 10)):
    _plate(tmp / "plate.FCStd")
    return _write(tmp / "manifest.json", {
        "name": "bolted", "root": "bolted.FCStd",
        "components": {
            "plate": {"file": "plate.FCStd"},
            "bolt": {"library": {"tool": "add_fastener",
                                 "spec": {"kind": "hex_bolt", "size": size,
                                          "length": length}}}},
        "instances": [{"component": "plate", "name": "plate", "placement": [0, 0, 0]},
                      {"component": "bolt", "name": "bolt",
                       "placement": list(place_bolt)}]})


def _merge(mpath):
    with Worker() as w:
        return w.call("merge_assembly", manifest=str(mpath))


# --- tests -------------------------------------------------------------------

def test_generates_and_gates():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rep = _merge(_bolted_manifest(tmp))
        _check("merge with a library part ok", rep["ok"], True)
        _check("library block reports the generated part",
               "bolt" in rep.get("library", {}), True)
        _check("generated file exists on disk",
               Path(rep["library"]["bolt"]["file"]).exists(), True)
        bom_parts = {r["part"] for r in rep["gates"]["bom"]}
        _check("BOM includes plate + the generated bolt (2 leaves)",
               len(rep["gates"]["bom"]), 2)
        _check("BOM bolt name is legible (carries the spec slug)",
               any("hex_bolt" in pp for pp in bom_parts), True)


def test_deterministic_and_idempotent():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        m = _bolted_manifest(tmp)
        a, b = _merge(m), _merge(m)
        _check("same spec -> same spec_hash",
               a["library"]["bolt"]["spec_hash"], b["library"]["bolt"]["spec_hash"])
        _check("same spec -> same generated path",
               a["library"]["bolt"]["file"], b["library"]["bolt"]["file"])
        _check("re-merge stays ok (idempotent)", b["ok"], True)


def test_generated_geometry_matches_spec():
    """The generator is deterministic and its 'computed, not designed' anchor (the
    major diameter a hole would be sized to) matches the spec; the generated solid
    honors the length spec along its axis."""
    with Worker() as w:
        w.call("new_document", name="b")
        r = w.call("add_fastener", kind="hex_bolt", size="M6", length=20)
    _check("add_fastener M6 reports the computed major Ø6",
           abs(r["major_diameter"] - 6.0) <= 0.01, True)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rep = _merge(_bolted_manifest(tmp, size="M6", length=20))
        path = rep["library"]["bolt"]["file"]
        with Worker() as w:
            w.call("open_document", path=str(path))
            ext = w.call("run_script", code="""
objs=[o for o in App.ActiveDocument.Objects if hasattr(o,'Shape') and not o.Shape.isNull() and not o.InList]
b=objs[0].Shape.BoundBox
__result__ = round(max(b.XLength, b.YLength, b.ZLength), 2)
""")["result"]
        # longest extent (the bolt axis) = shank length 20 + head height 6 = 26
        _check("generated M6x20 honors length (axis extent ~26mm)",
               24.0 <= ext <= 28.0, True)


def test_lockfile_keys_on_spec_hash():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        m = _bolted_manifest(tmp, size="M6", length=20)
        with Worker() as w:
            w.call("merge_assembly", manifest=str(m))
            w.call("assembly_lock", manifest=str(m))
            clean = w.call("assembly_lock_check", manifest=str(m))
        _check("freshly locked is clean", clean["ok"], True)
        # re-merge regenerates the file bytes, but the SPEC is unchanged -> not drift
        with Worker() as w:
            w.call("merge_assembly", manifest=str(m))
            still = w.call("assembly_lock_check", manifest=str(m))
        _check("regenerated bytes don't read as drift (computed identity)",
               "bolt" in still["modified"], False)
        _check("still clean after regenerate", still["ok"], True)
        # change the SPEC (M6 -> M8) -> the library component is modified
        _bolted_manifest(tmp, size="M8", length=20)
        with Worker() as w:
            chk = w.call("assembly_lock_check", manifest=str(m))
        _check("changed spec -> library part modified", "bolt" in chk["modified"], True)


def test_bad_library_specs_fail_loudly():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _plate(tmp / "plate.FCStd")
        # non-allow-listed tool
        m = _write(tmp / "m1.json", {
            "name": "x", "root": "x.FCStd",
            "components": {"plate": {"file": "plate.FCStd"},
                           "bad": {"library": {"tool": "run_script",
                                               "spec": {"code": "1"}}}},
            "instances": [{"component": "plate", "name": "plate", "placement": [0, 0, 0]},
                          {"component": "bad", "name": "bad", "placement": [0, 0, 0]}]})
        raised = False
        try:
            _merge(m)
        except Exception as e:
            raised = "not an allowed" in str(e) or "library tool" in str(e)
        _check("non-allow-listed tool rejected", raised, True)
        # allow-listed tool, bad spec (missing required length for a bolt)
        m2 = _write(tmp / "m2.json", {
            "name": "y", "root": "y.FCStd",
            "components": {"plate": {"file": "plate.FCStd"},
                           "bad": {"library": {"tool": "add_fastener",
                                               "spec": {"kind": "hex_bolt",
                                                        "size": "M6"}}}},
            "instances": [{"component": "plate", "name": "plate", "placement": [0, 0, 0]},
                          {"component": "bad", "name": "bad", "placement": [0, 0, 0]}]})
        raised2 = False
        try:
            _merge(m2)
        except Exception:
            raised2 = True
        _check("allow-listed tool with bad spec fails loudly", raised2, True)


def test_interference_still_catches_a_clash():
    """A standard part is gated like any other: place the bolt INTO the plate
    (not the clearance hole) and interference must fire."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # bolt driven into solid plate material (offset from the hole)
        rep = _merge(_bolted_manifest(tmp, place_bolt=(10, 10, -5)))
        _check("bolt clashing with plate -> NOT ok", rep["ok"], False)
        _check("clash is interference", bool(rep["gates"]["interference"]), True)


def main():
    print("== standard / library parts — generate, gate, lock (no API) ==")
    for t in (test_generates_and_gates, test_deterministic_and_idempotent,
              test_generated_geometry_matches_spec, test_lockfile_keys_on_spec_hash,
              test_bad_library_specs_fail_loudly, test_interference_still_catches_a_clash):
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
