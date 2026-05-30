"""
Phase 0 multi-agent walkthrough — partition + merge with TODAY's tools.

Companion to docs/MULTI_AGENT.md §10 (Phase 0). Proves the partition+merge model
end-to-end with ZERO new DriftPin code: separate "builder" sessions each produce a
component .FCStd; a "coordinator" session links them by file path into one assembly
and runs the verification gates.

The team never shares process state. Their only shared truth is phase0_manifest.json
(the interface contract) + the component files on disk. That is exactly the boundary
the RFC's thin primitives will formalize in Phase 1 (publish_interface, merge_assembly,
mate-by-frame); here we do the same moves by hand so the model is visible.

Maps each RFC concept onto an existing tool:
  - interface contract (§3)      -> phase0_manifest.json, read below
  - component build (builder)     -> add_primitive / boolean_op in its own Worker
  - envelope gate (§6)            -> mass_properties bbox vs declared envelope
  - merge / construct-up (§5)     -> make_assembly + add_part(source={path})
  - interference gate (§6)        -> interference_check (clean fit AND a clash demo)
  - recursive BOM (§5/§6)         -> bom_extract (single level here)
  - visual gate (§6)              -> render_view -> PNG

Run:  .venv/bin/python3 example/phase0_walkthrough.py
  (host venv needs Pillow + numpy for the render step; renders are skipped if absent)

Outputs (written next to this script):
  phase0_plate.FCStd, phase0_peg.FCStd       component files (one writer each)
  phase0_assembly.FCStd                       the merged result
  phase0_*_iso.png                            renders (if Pillow/numpy present)
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

try:
    import numpy  # noqa: F401
    from PIL import Image  # noqa: F401
    from driftpin import render as render_lib
    _RENDER_OK = True
except ImportError as e:
    _RENDER_OK = False
    _RENDER_ERR = str(e)

MANIFEST = HERE / "phase0_manifest.json"
INTERFERENCE_TOL_MM3 = 1.0  # below this, "touching", not a clash


def _save_render(w, handle, out_name):
    """Best-effort iso render to a PNG next to this script. Returns True if written.

    render_view is a host-side MCP tool; the raw Worker only exposes `tessellate`,
    so we mesh in the worker and rasterize here with driftpin.render (same as the
    render_view tool does internally)."""
    if not _RENDER_OK:
        return False
    try:
        mesh = w.call("tessellate", handle=handle, deflection=0.4)
        png = render_lib.render_mesh(
            mesh["vertices"], mesh["triangles"],
            width=384, height=384, view="iso",
        )
        (HERE / out_name).write_bytes(png)
        return True
    except Exception as e:  # a shapeless container can't tessellate; don't abort
        print(f"      (render of {out_name} skipped: {type(e).__name__}: {e})")
        return False


# An App::Part assembly has no single Shape, so it can't be tessellated directly.
# Compound every linked part in its placed (world) position via the run_script
# escape hatch, then render THAT — a true picture of the merged result.
_MERGE_COMPOUND_SRC = """
import Part
asm = _resolve({asm!r})
shapes = []
for o in asm.Group:
    lo = getattr(o, "LinkedObject", None)
    if lo is not None and hasattr(lo, "Shape") and not lo.Shape.isNull():
        shapes.append(lo.Shape.transformed(o.Placement.Matrix))
comp = App.ActiveDocument.addObject("Part::Feature", "MergedView")
comp.Shape = Part.makeCompound(shapes)
App.ActiveDocument.recompute()
__result__ = comp.Name
"""


def _render_merged(w, asm_handle, out_name):
    """Render the whole assembly by compounding its placed parts (run_script)."""
    if not _RENDER_OK:
        return False
    try:
        res = w.call("run_script", code=_MERGE_COMPOUND_SRC.format(asm=asm_handle))
        comp = res["registered"][0]["handle"]
        return _save_render(w, comp, out_name)
    except Exception as e:
        print(f"      (merged render skipped: {type(e).__name__}: {e})")
        return False


def _envelope_ok(bbox_min, bbox_max, env):
    """Envelope gate (§6): component world bbox must sit inside its declared box."""
    e_min, e_max = env["min"], env["max"]
    eps = 1e-6
    return all(bbox_min[i] >= e_min[i] - eps and bbox_max[i] <= e_max[i] + eps
               for i in range(3))


def build_component(name, spec):
    """A 'builder agent': its own Worker (own process, own file). Builds to the
    manifest's build spec, runs its envelope self-check, renders, saves."""
    build = spec["build"]
    out = HERE / spec["file"]
    print(f"  [{spec['owner']}] build '{name}' {build} -> {spec['file']}")
    with Worker() as w:
        w.call("new_document", name=name)
        if build["kind"] == "box":
            obj = w.call("add_primitive", kind="box",
                         w=build["w"], d=build["d"], h=build["h"], name=name)
        elif build["kind"] == "cylinder":
            obj = w.call("add_primitive", kind="cylinder",
                         r=build["r"], h=build["h"], name=name)
        else:
            raise ValueError(f"unknown build kind: {build['kind']!r}")

        # Envelope self-check before handing off (the contract the team relies on).
        mp = w.call("mass_properties", handle=obj["handle"])
        bb = mp["bounding_box_mm"]  # flat [xmin,ymin,zmin, xmax,ymax,zmax]
        bb_min, bb_max = bb[:3], bb[3:]
        ok = _envelope_ok(bb_min, bb_max, spec["envelope"])
        status = "OK" if ok else "VIOLATION"
        print(f"      vol={obj['volume']:.0f}mm³  bbox={bb_min}..{bb_max}  "
              f"envelope-self-check: {status}")
        assert ok, (f"{name} bbox {bb_min}..{bb_max} escapes declared envelope "
                    f"{spec['envelope']} — would surprise neighbors")

        _save_render(w, obj["handle"], f"phase0_{name}_iso.png")
        w.call("save_document", path=str(out))
    return out


def merge(manifest, instances, asm_doc_name, asm_file=None):
    """A 'coordinator' move: link each instance by file path into one assembly and
    place it. Returns (worker, assembly_handle) — worker left open for gates."""
    comps = manifest["components"]
    owner_file = asm_file or f"{asm_doc_name}.FCStd"
    w = Worker()
    w.call("new_document", name=asm_doc_name)
    asm = w.call("make_assembly", name="DemoAssembly")
    # A cross-document App::Link requires the OWNER doc to already have a path on
    # disk — save the (empty) assembly first, then link, then re-save with links.
    w.call("save_document", path=str(HERE / owner_file))
    for inst in instances:
        spec = comps[inst["component"]]
        link = w.call(
            "add_part",
            assembly=asm["handle"],
            source={"path": str(HERE / spec["file"])},
            placement=inst["placement"],
            name=inst["component"],
        )
        print(f"      linked {inst['component']:6s} -> {link['linked']:12s} "
              f"@ {inst['placement']}")
    if asm_file is not None:
        w.call("save_document", path=str(HERE / asm_file))
    return w, asm["handle"]


def main():
    manifest = json.loads(MANIFEST.read_text())
    print(f"== Phase 0 walkthrough: {manifest['name']} ==")
    if not _RENDER_OK:
        print(f"  (renders disabled: {_RENDER_ERR})")

    # --- Fan out: builders produce component files in parallel (serial here). -----
    print("\n-- builders (one file, one owner each) --")
    for cname, cspec in manifest["components"].items():
        build_component(cname, cspec)

    # --- Fan in: coordinator merges + runs the gates (happy path). ----------------
    print("\n-- coordinator: merge + verify (intended fit) --")
    w, asm = merge(manifest, manifest["instances"],
                   "phase0_assembly", asm_file=manifest["root"])
    try:
        parts = w.call("list_assembly_parts", assembly=asm)
        print(f"      assembled {len(parts)} instances")

        clashes = w.call("interference_check", assembly=asm)
        gate_fit = (max((c["interference_mm3"] for c in clashes), default=0.0)
                    < INTERFERENCE_TOL_MM3)
        print(f"      interference gate: {'PASS (parts fit)' if gate_fit else 'FAIL'}"
              f"  {clashes if clashes else '[]'}")
        assert gate_fit, f"unexpected interference in intended fit: {clashes}"

        bom = w.call("bom_extract", assembly=asm, density=7.9e-6)
        print("      BOM:")
        for row in bom:
            mass_g = row.get("total_mass_kg", 0) * 1000
            print(f"        {row['part']:14s} x{row['count']}  "
                  f"{row['total_volume_mm3']:.0f}mm³  {mass_g:.1f}g")

        if _render_merged(w, asm, "phase0_assembly_iso.png"):
            print("      rendered merged assembly -> phase0_assembly_iso.png")
    finally:
        w.shutdown()

    # --- Negative control: the same gate must CATCH a real clash. -----------------
    print("\n-- coordinator: clash demo (a peg embedded 4mm into the plate) --")
    clash_instances = [
        {"component": "plate", "placement": [0, 0, 0]},
        {"component": "peg",   "placement": [20, 30, 4]},  # 4mm into the plate
    ]
    w2, asm2 = merge(manifest, clash_instances, "phase0_clash")
    try:
        clashes = w2.call("interference_check", assembly=asm2)
        worst = max((c["interference_mm3"] for c in clashes), default=0.0)
        caught = worst >= INTERFERENCE_TOL_MM3
        print(f"      interference gate: "
              f"{'PASS (clash detected)' if caught else 'FAIL (missed clash)'}"
              f"  worst={worst:.0f}mm³")
        assert caught, "interference gate failed to catch an embedded peg"
    finally:
        w2.shutdown()

    print(f"\n== done. artifacts in {HERE} ==")
    print("   files reference each other by path — that IS the Phase 0 handoff.")


if __name__ == "__main__":
    main()
