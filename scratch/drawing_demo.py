"""Generate example 2D mechanical drawings (multi-view PDF/SVG/DXF + dimensions)
straight from CAD, headless — the deliverable for the TechDraw export kickoff
(docs/archive/KICKOFF_techdraw_export.md).

Run:
    .venv/bin/python scratch/drawing_demo.py

Writes artifacts/drawings/{lbracket,plate,fc_bracket,cbblock}_demo.{pdf,svg,dxf}. Every printed value is
measured from the real solid (validate-the-artifact): overall extents come from
the projected geometry, feature dims from the model edges/holes.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ankusdrive import Worker  # noqa: E402

ART = ROOT / "artifacts" / "drawings"
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
    w.call("set_title_block", page=page, part="L-BRACKET", material="STEEL 1045",
           rev="A", drawn_by="AnkusDrive", date="2026-06-15", project="DEMO")
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
    w.call("add_dimension", page=page, view="Top", kind="diameter", edge=hole,
           tolerance={"fit": "H7"})                                      # Ø12 H7 fit
    w.call("add_dimension", page=page, view="Top", kind="horizontal",    # hole X
           from_point=[0, 20, 0], to_point=[30, 20, 0])
    w.call("add_dimension", page=page, view="Top", kind="vertical",      # hole Y
           from_point=[30, 0, 0], to_point=[30, 20, 0])
    w.call("set_title_block", page=page, part="MOUNT PLATE", material="AL 6061-T6",
           rev="A", drawn_by="AnkusDrive", date="2026-06-15", project="DEMO")
    _export_all(w, page, "plate_demo")


def fillet_chamfer(w):
    """Bracket with a filleted edge (R6) and a chamfered edge (4x45) — the fillet
    reads as an R leader callout, the chamfer as a linear size."""
    w.call("new_document", name="fc_bracket")
    box = w.call("add_primitive", kind="box", w=70, d=45, h=12)
    vert = [e for e in w.call("list_edges", handle=box["handle"])
            if abs(e.get("length", 0) - 12) < 1e-6]   # vertical edges

    def near(e, x, y):
        c = e["centroid"]
        return abs(c[0] - x) < 1 and abs(c[1] - y) < 1

    fe = next(e["tag"] for e in vert if near(e, 0, 0))     # fillet the (0,0) corner
    ce = next(e["tag"] for e in vert if near(e, 70, 45))   # chamfer the (70,45) corner
    fil = w.call("fillet_edges", handle=box["handle"], edges=[fe], radius=6)
    part = w.call("chamfer_edges", handle=fil["handle"], edges=[ce], size=4)["handle"]
    page = w.call("make_drawing_page", name="Page")["handle"]
    w.call("add_projection_group", page=page, body=part, views=["Front", "Top"])
    w.call("add_dimension", page=page, view="Front", kind="horizontal",
           from_point=[0, 0, 0], to_point=[70, 0, 0])       # width
    w.call("add_dimension", page=page, view="Front", kind="vertical",
           from_point=[0, 0, 0], to_point=[0, 0, 12])       # thickness
    w.call("add_dimension", page=page, view="Top", kind="vertical",
           from_point=[0, 0, 0], to_point=[0, 45, 0])       # depth
    fre = next(e["tag"] for e in w.call("list_edges", handle=part)
               if e.get("radius") and abs(e["radius"] - 6) < 1e-6)
    w.call("add_dimension", page=page, view="Top", kind="radius", edge=fre)  # R6 fillet
    w.call("add_dimension", page=page, view="Top", kind="horizontal",
           from_point=[66, 45, 0], to_point=[70, 45, 0])    # chamfer leg = 4
    w.call("set_title_block", page=page, part="FC BRACKET", material="STEEL 1018",
           rev="A", drawn_by="AnkusDrive", date="2026-06-15", project="DEMO")
    w.call("fit_page", page=page)
    _export_all(w, page, "fc_bracket_demo")


def cbblock(w):
    """Counterbored block — exercises the drawings-next pair: the internal step is
    detected (drawing_gate.section_recommended) and an auto cross-section is added to
    show the bore profile, and a top-right isometric pictorial is dropped in if the
    sheet has room. A machinist gets the outline views, the section that explains the
    hidden geometry, and the glance-reference iso."""
    w.call("new_document", name="cbblock")
    box = w.call("add_primitive", kind="box", w=60, d=40, h=20)
    cb = w.call("add_primitive", kind="cylinder", r=10, h=6, placement=[30, 20, 14])
    bore = w.call("add_primitive", kind="cylinder", r=5, h=20, placement=[30, 20, 0])
    p1 = w.call("boolean_op", op="cut", base=box["handle"], tool=cb["handle"])
    part = w.call("boolean_op", op="cut", base=p1["handle"], tool=bore["handle"])["handle"]
    page = w.call("make_drawing_page", name="Page")["handle"]
    pg = w.call("add_projection_group", page=page, body=part, views=["Front", "Top"])
    w.call("set_property", handle=pg["handle"], name="ScaleType", value="Custom")
    w.call("set_property", handle=pg["handle"], name="Scale", value=1.5)
    w.call("add_dimension", page=page, auto=True)                        # overall
    w.call("set_title_block", page=page, part="CB BLOCK", material="STEEL 1045",
           rev="A", drawn_by="AnkusDrive", date="2026-06-15", project="DEMO")
    rec = w.call("drawing_gate", page=page)["section_recommended"]
    print(f"  section recommended: {rec['recommended']} ({'; '.join(rec['reasons'])})")
    sec = w.call("add_section_view", page=page)                          # auto section
    print(f"  section added: {sec['added']} as {sec.get('view')}")
    thumb = w.call("add_thumbnail", page=page)                           # iso pictorial
    print(f"  thumbnail placed: {thumb['placed']}"
          + (f" (scale {thumb['scale']})" if thumb["placed"]
             else f" ({thumb.get('reason')})"))
    _export_all(w, page, "cbblock_demo")


def main():
    with Worker() as w:
        print("L-bracket:")
        lbracket(w)
        print("Mount plate:")
        plate(w)
        print("Fillet + chamfer bracket:")
        fillet_chamfer(w)
        print("Counterbore block (auto section + iso thumbnail):")
        cbblock(w)
    print(f"\nWrote drawings to {ART}")


if __name__ == "__main__":
    main()
