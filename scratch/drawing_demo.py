"""Generate example 2D mechanical drawings (multi-view PDF/SVG/DXF + dimensions)
straight from CAD, headless — the deliverable for the TechDraw export kickoff
(docs/KICKOFF_techdraw_export.md).

Run:
    .venv/bin/python scratch/drawing_demo.py

Writes artifacts/{lbracket,plate}_demo.{pdf,svg,dxf}. Every printed value is
measured from the real solid (validate-the-artifact): overall extents come from
the projected geometry, feature dims from the model edges/holes.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from driftpin import Worker  # noqa: E402

ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)


def _export_all(w, page, stem):
    for ext in ("pdf", "svg", "dxf"):
        r = w.call("export_drawing", page=page, path=str(ART / f"{stem}.{ext}"))
        print(f"  {stem}.{ext:3s}  {r['size']:>6d} B  "
              f"views={r['views']} dims={r['dimensions']}")


def lbracket(w):
    """Asymmetric L-bracket — three views, overall + feature dimensions."""
    w.call("new_document", name="lbracket")
    b1 = w.call("add_primitive", kind="box", w=40, d=25, h=15)
    b2 = w.call("add_primitive", kind="box", w=15, d=25, h=8, placement=[25, 0, 7])
    cut = w.call("boolean_op", op="cut", base=b1["handle"], tool=b2["handle"])
    page = w.call("make_drawing_page", name="Page")["handle"]
    w.call("add_projection_group", page=page, body=cut["handle"],
           views=["Front", "Top", "Right"])
    w.call("add_dimension", page=page, auto=True)                       # overall L×W×H
    w.call("add_dimension", page=page, view="Front", kind="horizontal",  # ledge width
           from_point=[0, 0, 7], to_point=[25, 0, 7])
    w.call("add_dimension", page=page, view="Front", kind="vertical",    # step height
           from_point=[0, 0, 0], to_point=[0, 0, 7])
    w.call("add_annotation", page=page, text="L-BRACKET  rev A  (mm)", x=12, y=16)
    _export_all(w, page, "lbracket_demo")


def plate(w):
    """Mounting plate with a Ø12 hole — two views, diameter + position dims."""
    w.call("new_document", name="plate")
    plate_ = w.call("add_primitive", kind="box", w=60, d=40, h=8)
    drill = w.call("add_primitive", kind="cylinder", r=6, h=8, placement=[30, 20, 0])
    part = w.call("boolean_op", op="cut", base=plate_["handle"], tool=drill["handle"])
    edges = w.call("list_edges", handle=part["handle"])
    hole = next(e["tag"] for e in edges
                if e.get("radius") and abs(e["radius"] - 6.0) < 1e-6)
    page = w.call("make_drawing_page", name="Page")["handle"]
    w.call("add_projection_group", page=page, body=part["handle"],
           views=["Front", "Top"])
    w.call("add_dimension", page=page, auto=True)                        # overall
    w.call("add_dimension", page=page, view="Top", kind="diameter", edge=hole)
    w.call("add_dimension", page=page, view="Top", kind="horizontal",    # hole X
           from_point=[0, 20, 0], to_point=[30, 20, 0])
    w.call("add_dimension", page=page, view="Top", kind="vertical",      # hole Y
           from_point=[30, 0, 0], to_point=[30, 20, 0])
    w.call("add_annotation", page=page, text="MOUNT PLATE  rev A  (mm)", x=14, y=16)
    _export_all(w, page, "plate_demo")


def main():
    with Worker() as w:
        print("L-bracket:")
        lbracket(w)
        print("Mount plate:")
        plate(w)
    print(f"\nWrote drawings to {ART}")


if __name__ == "__main__":
    main()
