"""
Generate real CAD artifacts (.step + .stl) from multi-agent builds — the geometry
the builders actually produced, not just pass/fail stats.

  1. A LIVE Haiku coordinator build of the peg-in-plate assembly (real agents):
     each builder's component + the merged assembly, exported to STEP + STL.
  2. The 6-speed gearbox manifest (12 gears, scripted reference, free): the merged
     assembly exported to STEP + STL.

Output: results/artifacts/  +  an INDEX.md listing every file with its size.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

OUT = REPO / "results" / "artifacts"
OUT.mkdir(parents=True, exist_ok=True)
index = []   # (group, label, filename, bytes)


def _top_object(w):
    """Name of the top shaped object in the active doc (the part, or the assembly
    App::Part whose members carry it in their InList)."""
    return w.call("run_script", code="""
App.ActiveDocument.recompute()
objs=[o for o in App.ActiveDocument.Objects if hasattr(o,'Shape') and not o.Shape.isNull() and not o.InList]
__result__ = objs[0].Name if objs else None
""")["result"]


def export_fcstd(fcstd_path, stem, group, label):
    """Open a saved .FCStd and export its top object to STEP + STL under OUT."""
    with Worker() as w:
        w.call("open_document", path=str(fcstd_path))
        obj = _top_object(w)
        step = OUT / f"{stem}.step"
        step.parent.mkdir(parents=True, exist_ok=True)
        # STEP: export the SHAPE directly. Part.export() of an App::Part assembly
        # serializes the container but drops linked-child geometry; the object's
        # compound Shape carries every placed solid (single parts work too).
        ssz = w.call("run_script", code=f"""
o = App.ActiveDocument.getObject({obj!r})
o.Shape.exportStep({str(step)!r})
import os
__result__ = os.path.getsize({str(step)!r})
""")["result"]
        index.append((group, label, f"{stem}.step", ssz))
        print(f"  {group:16s} {stem}.step  {ssz:>9,} bytes")
        stl = OUT / f"{stem}.stl"
        r = w.call("export_shape", object=obj, path=str(stl))   # meshes obj.Shape
        index.append((group, label, f"{stem}.stl", r["size"]))
        print(f"  {group:16s} {stem}.stl   {r['size']:>9,} bytes")


# --- 1. LIVE agent build: peg-in-plate via the coordinator -------------------

def live_peg_assembly():
    import anthropic
    from orchestration import agentkit, coordinator
    print("== LIVE Haiku coordinator build: peg-in-plate ==")
    client = anthropic.Anthropic()
    wd = OUT / "_build_peg"
    rep = coordinator.orchestrate(client, agentkit.MODELS["haiku"],
                                  coordinator.DEMO_BRIEF, wd, max_rounds=3)
    print(f"  coordinator ok={rep['ok']} rounds={rep['rounds']} cost=${rep['cost_usd']}")
    if not rep["ok"]:
        print("  (build did not pass gates; exporting whatever was produced)")
    # component files (per-builder isolation -> wd/build/<cid>/<file>)
    for cid, fname in (("plate", "plate.FCStd"), ("peg", "peg.FCStd")):
        f = wd / "build" / cid / fname
        if f.exists():
            export_fcstd(f, f"peg_assembly/{cid}", "agent:peg", f"{cid} (agent-built)")
    # the merged assembly
    root = Path(rep["root"])
    if root.exists():
        export_fcstd(root, "peg_assembly/assembly", "agent:peg", "merged assembly")
    return rep


# --- 2. Gearbox manifest (free, scripted reference) --------------------------

def gearbox_assembly():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gbx", REPO / "example" / "gearbox_manifest.py")
    gbx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gbx)
    print("== 6-speed gearbox manifest (12 gears, scripted) ==")
    import tempfile
    import json
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        man = gbx.build_manifest(tmp)
        mpath = tmp / "gearbox.manifest.json"
        mpath.write_text(json.dumps(man))
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=str(mpath))
        print(f"  gearbox merged ok={rep['ok']}  {len(man['components'])} gears")
        # export the merged assembly root (saved by merge_assembly)
        export_fcstd(Path(rep["root"]), "gearbox/gearbox", "gearbox", "12-gear assembly")
        # also export two representative single gears (input+output of speed 1)
        for cid in ("in0", "out0"):
            f = tmp / f"{cid}.FCStd"
            if f.exists():
                export_fcstd(f, f"gearbox/{cid}", "gearbox", f"{cid} gear")


def write_index():
    lines = ["# Multi-agent build artifacts",
             "",
             "Real CAD geometry the builders produced — openable in any CAD tool "
             "(STEP) or mesh viewer/slicer (STL).",
             ""]
    cur = None
    for group, label, fn, size in index:
        if group != cur:
            lines.append(f"\n## {group}\n")
            cur = group
        lines.append(f"- `{fn}` — {label} ({size:,} bytes)")
    (OUT / "INDEX.md").write_text("\n".join(lines) + "\n")
    print(f"\n  index -> {OUT / 'INDEX.md'}  ({len(index)} files)")


if __name__ == "__main__":
    if os.environ.get("SKIP_LIVE"):
        print("== SKIP_LIVE set: gearbox only (free) ==")
    else:
        live_peg_assembly()
    gearbox_assembly()
    write_index()
