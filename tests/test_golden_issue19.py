"""
Golden-fixture tests against the real issue #19 adapter models.

These open the actual .FCStd files the LLM agent produced (provided by @mpetne,
see tests/fixtures/issue19/README.md) and pin the pathology the issue is about:
the *delivered* parts read as perfectly watertight solids, yet none is a usable
enclosed-flow adapter. They are regression guards — if a future change makes
check_shape/check_airtight_path quietly bless these, or the fixtures are
replaced, these fail.

Each test skips (not fails) if the fixture file is absent, so a sparse checkout
still runs the rest of the suite.

Run:  python3 tests/test_golden_issue19.py
"""
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker, WorkerError  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "issue19"

# The delivered top-level part in each file (the largest empty-InList solid),
# confirmed by inspection. v2 and v3 ship the SAME AdapterV2 tip.
_DELIVERED = {1: "Adapter", 2: "AdapterV2", 3: "AdapterV2"}


class SkipTest(Exception):
    """Raised by a test to mark itself skipped (fixture absent)."""


def _fixture(v):
    p = FIXTURES / f"router_vac_adapter_v{v}.FCStd"
    if not p.exists():
        raise SkipTest(f"missing fixture {p.name}")
    return str(p)


_DELIVERED_PART_SRC = '''
import FreeCAD as App
d = App.ActiveDocument
cands = [o for o in d.Objects
         if hasattr(o, "Shape") and not o.Shape.isNull() and o.Shape.Solids
         and len(o.InList) == 0]
o = max(cands, key=lambda o: o.Shape.Volume)
__result__ = {"h": _register("part", o), "name": o.Name,
              "vol": round(o.Shape.Volume, 1), "shells": len(o.Shape.Shells)}
'''


def _open_delivered(w, v):
    """Open fixture v and return the delivered part as {h, name, vol, shells}."""
    w.call("open_document", path=_fixture(v))
    return w.call("run_script", code=_DELIVERED_PART_SRC)["result"]


# --- tests --------------------------------------------------------------------

def test_golden_delivered_parts_are_misleadingly_watertight():
    """The crux of #19: all three delivered parts pass check_shape as watertight
    solids — the false 'looks fine' signal — yet each is a single closed shell
    with no enclosed cavity (shells == 1), i.e. no realized airflow void."""
    with Worker() as w:
        for v in (1, 2, 3):
            part = _open_delivered(w, v)
            assert part["name"] == _DELIVERED[v], (v, part)
            cs = w.call("check_shape", handle=part["h"])
            assert cs["watertight_solid"] is True, (v, cs)
            assert cs["valid"] is True, (v, cs)
            assert cs["shells"] == 1, (v, cs)  # no internal cavity shell


def test_golden_v2_solid_blank_has_no_flow_path():
    """check_airtight_path refuses to certify the v2 part: it is a solid blank,
    so capping any face finds no void behind it — the tool says so instead of
    returning a bogus 'ok'."""
    with Worker() as w:
        part = _open_delivered(w, 2)
        try:
            w.call("check_airtight_path", handle=part["h"], inlet="Face3", outlet="Face4")
        except WorkerError as e:
            assert "does not open into a void" in str(e), e
        else:
            assert False, "expected check_airtight_path to reject the solid blank"
        assert w.call("ping") == "pong"


def test_golden_v3_doorway_not_integrated():
    """v3's doorway never made it into the delivered tip: AdapterV2 is byte-for-
    byte the v2 solid (identical volume), and the doorway lives in a separate
    Cut002 that the cut split into 2 solids (the snap-tab disconnection)."""
    with Worker() as w:
        v3 = _open_delivered(w, 3)
        cut002 = w.call("run_script", code=(
            "o = App.ActiveDocument.getObject('Cut002'); "
            "__result__ = {'solids': len(o.Shape.Solids)}"))["result"]["solids"]
        v2 = _open_delivered(w, 2)
        assert v3["name"] == "AdapterV2" and v2["name"] == "AdapterV2"
        assert v3["vol"] == v2["vol"], (v3["vol"], v2["vol"])  # doorway not fused in
        assert cut002 == 2, f"expected Cut002 to be split into 2 solids, got {cut002}"


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    tests = _discover()
    failures, skipped = [], 0
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except SkipTest as e:
            skipped += 1
            print(f"  SKIP {name:48s} ({time.time() - t0:.2f}s)  {e}")
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")

    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed, {skipped} skipped  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests) - skipped}/{len(tests)} passed, {skipped} skipped  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
