"""
component_contract_check + the builder-brief schema (issue #169) — free, no LLM,
no key. Two halves:

  * The PURE core (driftpin.builder_brief) — validation, slice rendering, and the
    FreeCAD-free evaluate_contract judgement — tested with synthetic inputs, so the
    gate's logic is verified without a worker process.
  * The WORKER tool — component_contract_check driven against real geometry,
    two-sided: a correct component passes; each violation (not watertight, out of
    envelope, missing/misplaced interface) is caught as exactly the right check, and
    the tool never raises on a failing check.

Run: .venv/bin/python3 tests/test_component_contract_check.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin import builder_brief as bb  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _failed(rep):
    return {c["check"] for c in rep["checks"] if not c["passed"]}


# --- pure schema layer --------------------------------------------------------

_BRIEF = {
    "schema": bb.SCHEMA, "component": "plate", "assembly": "demo",
    "task": "build a plate", "output": "plate.FCStd",
    "envelope": {"min": [0, 0, 0], "max": [60, 60, 10]},
    "interfaces": {"bore_axis": {"origin": [30, 30, 0], "z_axis": [0, 0, 1]}},
}


def test_validate():
    _check("valid brief -> no problems", bb.validate_builder_brief(_BRIEF), [])
    _check("missing component/task/output flagged",
           len(bb.validate_builder_brief({"schema": bb.SCHEMA})) >= 3, True)
    _check("bad schema flagged",
           any("schema" in p for p in bb.validate_builder_brief(
               {**_BRIEF, "schema": "nope/9"})), True)
    _check("bad envelope flagged",
           any("envelope" in p for p in bb.validate_builder_brief(
               {**_BRIEF, "envelope": {"min": [0, 0, 0], "max": [0, 60, 10]}})), True)
    _check("unknown key flagged",
           any("unknown brief key" in p for p in bb.validate_builder_brief(
               {**_BRIEF, "wat": 1})), True)


def test_slice_text_and_projection():
    txt = bb.builder_brief_text(_BRIEF)
    _check("slice text names the component", "'plate'" in txt, True)
    _check("slice text tells builder to self-gate",
           "component_contract_check" in txt, True)
    # project a coordinator-brief component into a standalone builder brief
    coord = {"name": "demo", "shared_parameters": {"wall_mm": 3.0},
             "components": {"plate": {"file": "plate.FCStd", "task": "build a plate",
                                      "envelope": _BRIEF["envelope"],
                                      "interfaces": _BRIEF["interfaces"],
                                      "material": "AISI 1045"}}}
    proj = bb.brief_from_slice(coord, "plate")
    _check("projected brief is valid", bb.validate_builder_brief(proj), [])
    _check("projection carries schema + envelope + shared params",
           (proj["schema"], "envelope" in proj, proj["shared_parameters"]),
           (bb.SCHEMA, True, {"wall_mm": 3.0}))


def test_evaluate_core():
    ok = bb.evaluate_contract(
        _BRIEF, watertight=True, bbox={"min": [0, 0, 0], "max": [60, 60, 10]},
        published={"bore_axis": {"origin": [30, 30, 0], "z_axis": [0, 0, 1]}})
    _check("core: reference component -> ok", ok["ok"], True)

    bad = bb.evaluate_contract(
        _BRIEF, watertight=False, bbox={"min": [-1, 0, 0], "max": [60, 60, 12]},
        published={})
    _check("core: three violations flagged",
           _failed(bad), {"watertight", "envelope", "interface:bore_axis"})
    _check("core: ok False + reasons populated",
           (bad["ok"], len(bad["reasons"])), (False, 3))

    # published but in the wrong place -> caught only when the brief pins the origin
    off = bb.evaluate_contract(
        {**_BRIEF, "interfaces": {"bore_axis": {"origin": [30, 30, 0], "tol_mm": 0.5}}},
        watertight=True, bbox={"min": [0, 0, 0], "max": [60, 60, 10]},
        published={"bore_axis": {"origin": [35, 30, 0]}})
    _check("core: misplaced frame caught", _failed(off), {"interface:bore_axis"})

    # empty brief -> only the always-on watertight check runs
    empty = bb.evaluate_contract({}, watertight=True, bbox=None, published={})
    _check("core: empty brief -> ok, watertight-only",
           (empty["ok"], [c["check"] for c in empty["checks"]]),
           (True, ["watertight"]))


# --- worker tool, two-sided against real geometry -----------------------------

def _plate(worker, w=60, d=60, h=10):
    """A watertight plate with a Ø16 bore at (30,30) and a published bore_axis."""
    worker.call("new_document", name="plate")
    worker.call("add_primitive", kind="box", w=w, d=d, h=h, name="box")
    worker.call("add_primitive", kind="cylinder", r=8, h=h + 20,
                placement=[30, 30, -10], name="bore")
    handle = worker.call("boolean_op", op="cut", base="box_1",
                         tool="cylinder_1")["handle"]
    worker.call("publish_interface", handle=handle, name="bore_axis",
                frame={"origin": [30, 30, 0], "z_axis": [0, 0, 1]})
    return handle


def test_worker_gate():
    brief = {
        "schema": bb.SCHEMA, "component": "plate", "assembly": "demo",
        "task": "t", "output": "plate.FCStd",
        "envelope": {"min": [0, 0, 0], "max": [60, 60, 10]},
        "interfaces": {"bore_axis": {"origin": [30, 30, 0], "z_axis": [0, 0, 1],
                                     "tol_mm": 0.5}},
    }
    with Worker() as w:
        h = _plate(w)
        rep = w.call("component_contract_check", handle=h, brief=brief)
        _check("reference plate -> ok", rep["ok"], True)
        _check("reference plate: all checks passed", _failed(rep), set())

        # out-of-envelope part: a fat plate whose bbox busts the keep-out box
        fat = w.call("add_primitive", kind="box", w=80, d=60, h=10,
                     name="fat")["handle"]
        w.call("publish_interface", handle=fat, name="bore_axis",
               frame={"origin": [30, 30, 0], "z_axis": [0, 0, 1]})
        rep = w.call("component_contract_check", handle=fat, brief=brief)
        _check("oversize part: envelope caught", "envelope" in _failed(rep), True)

        # forgot to publish the required interface
        bare = w.call("add_primitive", kind="box", w=60, d=60, h=10,
                      name="bare")["handle"]
        rep = w.call("component_contract_check", handle=bare, brief=brief)
        _check("missing interface caught",
               "interface:bore_axis" in _failed(rep), True)
        _check("tool never raises on failing checks (returns a report)",
               rep["ok"], False)

        # not-watertight: two disjoint boxes fused into a 2-solid compound is not
        # "one clean watertight solid".
        w.call("new_document", name="shell")
        c1 = w.call("add_primitive", kind="box", w=20, d=20, h=20,
                    name="cube")["handle"]
        c2 = w.call("add_primitive", kind="box", w=10, d=10, h=10,
                    placement=[40, 0, 0], name="cube2")["handle"]
        fused = w.call("boolean_op", op="fuse", base=c1, tool=c2)["handle"]
        rep = w.call("component_contract_check", handle=fused,
                     brief={"envelope": {"min": [-1, -1, -1], "max": [100, 100, 100]}})
        _check("non-single-solid: watertight caught",
               "watertight" in _failed(rep), True)


def main():
    print("== component_contract_check + builder brief (issue #169, no API) ==")
    for t in (test_validate, test_slice_text_and_projection, test_evaluate_core,
              test_worker_gate):
        t()
    print(f"\n== {_PASS}/{_PASS + _FAIL} checks passed "
          f"({'OK' if not _FAIL else str(_FAIL) + ' FAILED'}) ==")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
