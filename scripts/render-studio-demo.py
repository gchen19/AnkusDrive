#!/usr/bin/env python3
"""
render-studio-demo.py — the Blender studio backend's reference image (issue #335).

Builds a five-part instrument-enclosure stand-in (base plate, housing, display, button,
PCB) from primitives and renders it in ONE render_photoreal call with a different
appearance per part — brushed aluminium, FDM-textured grey plastic, an emissive
screen, brass, green solder mask — in the `studio` scene. Writes the PNG and the
`.blend` beside it.

Usage:
    .venv/bin/python3 scripts/render-studio-demo.py [--out-dir DIR] [--quality Q]
                                                    [--width W] [--height H]

Defaults: --out-dir artifacts/rendering, --quality final, 960x720. Needs Blender 4.2+
(render_capabilities -> renderers.Blender); without it the script prints the install
hint and exits 2. Presentation-only / not bit-reproducible, so it lives in scripts/.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ankusdrive import Worker  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out-dir", default=str(REPO / "artifacts" / "rendering"))
    ap.add_argument("--quality", default="final", choices=("draft", "preview", "final"))
    ap.add_argument("--width", type=int, default=960)
    ap.add_argument("--height", type=int, default=720)
    args = ap.parse_args()

    with Worker() as w:
        w.call("new_document", name="studio_demo")

        def prim(**kw):
            return w.call("add_primitive", **kw)["handle"]

        # Iso looks from +X+Y+Z, so the display and button sit on the +Y face.
        parts = [
            {"name": "base_plate", "handle": prim(kind="box", w=120, d=80, h=4),
             "appearance": {"base": "Aluminium", "finish": "brushed"}},
            {"name": "housing", "handle": prim(kind="box", w=100, d=60, h=45, placement=[10, 10, 4]),
             "appearance": {"color": "#8a8d91", "roughness": 0.6, "finish": "fdm_layers",
                            "layer_height_mm": 0.3}},
            {"name": "display", "handle": prim(kind="box", w=56, d=1, h=24, placement=[18, 70, 16]),
             "appearance": {"color": "#101418", "roughness": 0.1, "emission": "#3fa7ff",
                            "emission_strength": 2.5}},
            {"name": "button", "handle": prim(kind="cylinder", r=6, h=5, placement=[92, 40, 49]),
             "appearance": "Brass"},
            {"name": "pcb", "handle": prim(kind="box", w=40, d=30, h=1.6, placement=[20, 25, 49]),
             "appearance": {"color": "#1b6e2a", "roughness": 0.35}},
        ]
        res = w.call("render_photoreal", _timeout=900.0, renderer="Blender", parts=parts,
                     scene="studio", quality=args.quality, width=args.width,
                     height=args.height)
    if res.get("ok") is False:
        print(f"Blender is not available: {res['reason']}\n  install: {res['install']}")
        sys.exit(2)
    os.makedirs(args.out_dir, exist_ok=True)
    png = os.path.join(args.out_dir, "render_blender_studio.png")
    shutil.copyfile(res["png_path"], png)
    res.pop("png_base64")
    print(json.dumps({k: res[k] for k in ("blender_version", "device", "samples", "denoised",
                                          "elapsed_s", "blend_path")}, indent=1))
    print(f"wrote {png}")


if __name__ == "__main__":
    main()
