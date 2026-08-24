#!/usr/bin/env python3
"""
render-material-gallery.py — the material-library gallery (docs/render_gallery.png),
but one per renderer.

For each renderer requested (default: every renderer render_capabilities reports
available), render the same box ∪ cylinder with every Render material card from the
same iso view and montage them into a labeled grid. Writes one image per renderer:
docs/render_gallery_<renderer>.png.

This generalizes the original POV-Ray-only docs/render_gallery.png so the material
library can be compared across POV-Ray / LuxCore / Appleseed (and Cycles/OSPRay/pbrt
once built). A material that fails on a given renderer is shown as a marked
placeholder cell, so the grid honestly reflects per-renderer support.

Usage:
    .venv/bin/python3 scripts/render-material-gallery.py [--renderer NAME|all]
                                                         [--cols N] [--width W] [--height H]

Photoreal output is presentation-only / not bit-reproducible, so this lives in
scripts/, not the test suite.
"""
import argparse
import base64
import io
import math
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ankusdrive import Worker                                   # noqa: E402
from ankusdrive.client import WorkerError                       # noqa: E402

_ORDER = ["Povray", "Luxcore", "Appleseed", "Cycles", "Ospray", "Pbrt"]
_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "/System/Library/Fonts")


def _font(bold, size):
    names = ("DejaVuSans-Bold.ttf",) if bold else ("DejaVuSans.ttf",)
    for d in _FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.is_file():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def _build_part(w):
    w.call("new_document", name="matgallery")
    box = w.call("add_primitive", kind="box", w=40, d=28, h=14)
    cyl = w.call("add_primitive", kind="cylinder", r=8, h=26, placement=[20, 14, 0])
    fused = w.call("boolean_op", op="fuse", base=box["handle"], tool=cyl["handle"])
    return fused["handle"]


def _render_cell(w, handle, renderer, material, width, height):
    """Return (image|None, error|None) for one material on one renderer."""
    try:
        res = w.call("render_photoreal", handle=handle, renderer=renderer,
                     view="iso", material=material, width=width, height=height,
                     _timeout=600.0)
        img = Image.open(io.BytesIO(base64.b64decode(res["png_base64"]))).convert("RGB")
        return img, None
    except WorkerError as e:
        return None, e.remote_message.splitlines()[0]


def _placeholder(width, height, material, reason):
    """A marked cell for a material that didn't render on this renderer."""
    img = Image.new("RGB", (width, height), (54, 40, 40))
    d = ImageDraw.Draw(img)
    d.text((10, height // 2 - 18), "✗ did not render", font=_font(True, 18), fill=(220, 150, 150))
    d.text((10, height // 2 + 6), reason[:42], font=_font(False, 13), fill=(170, 140, 140))
    return img


def _grid(renderer, cells, cols, out_path):
    """cells: list of (material, image, ok). One labeled grid on a dark card."""
    pad, cap_h, title_h = 14, 30, 60
    pw = max(im.width for _, im, _ in cells)
    ph = max(im.height for _, im, _ in cells)
    rows = math.ceil(len(cells) / cols)
    W = pad + cols * (pw + pad)
    H = title_h + pad + rows * (ph + cap_h + pad)
    bg, fg, sub, bad = (32, 33, 36), (235, 235, 235), (165, 170, 178), (210, 150, 150)

    canvas = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(canvas)
    ok_n = sum(1 for _, _, ok in cells if ok)
    d.text((pad, 18), f"render_photoreal material library — {renderer}",
           font=_font(True, 28), fill=fg)
    d.text((W - 260, 28), f"{ok_n}/{len(cells)} materials rendered",
           font=_font(False, 16), fill=sub)

    f_name = _font(True, 19)
    for i, (material, im, ok) in enumerate(cells):
        r, c = divmod(i, cols)
        x = pad + c * (pw + pad)
        y = title_h + pad + r * (ph + cap_h + pad)
        canvas.paste(im, (x + (pw - im.width) // 2, y + (ph - im.height) // 2))
        d.rectangle([x, y, x + pw - 1, y + ph - 1], outline=(70, 72, 78))
        d.text((x + 3, y + ph + 5), material, font=f_name, fill=(fg if ok else bad))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return W, H, ok_n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--renderer", default="all", help="renderer name, or 'all' (default)")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--width", type=int, default=256)
    ap.add_argument("--height", type=int, default=192)
    args = ap.parse_args()

    with Worker() as w:
        caps = w.call("render_capabilities", _timeout=60.0)
        if not caps["addon_importable"]:
            sys.exit(f"Render addon not importable: {caps.get('addon_error')}")
        materials = caps["materials"]
        if not materials:
            sys.exit("No material cards found in the addon.")
        available = caps["available"]
        if args.renderer != "all":
            if args.renderer not in available:
                sys.exit(f"{args.renderer} not available; have {available}")
            targets = [args.renderer]
        else:
            targets = [r for r in _ORDER if r in available] + \
                      [r for r in available if r not in _ORDER]
        print(f"Renderers: {targets}\nMaterials ({len(materials)}): {materials}")
        handle = _build_part(w)

        for renderer in targets:
            print(f"\n=== {renderer} ===")
            cells = []
            t0 = time.time()
            for m in materials:
                img, err = _render_cell(w, handle, renderer, m, args.width, args.height)
                if img is not None:
                    cells.append((m, img, True))
                    print(f"  {m:14s} ok")
                else:
                    cells.append((m, _placeholder(args.width, args.height, m, err), False))
                    print(f"  {m:14s} FAILED — {err}")
            out = REPO / "docs" / f"render_gallery_{renderer.lower()}.png"
            W, H, ok_n = _grid(renderer, cells, args.cols, out)
            print(f"  wrote {out.name} ({W}x{H}), {ok_n}/{len(materials)} ok, "
                  f"{time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
