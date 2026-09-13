#!/usr/bin/env python3
"""
render-gallery.py — render one part with every available photoreal renderer and
montage the results side by side, labeled per renderer.

It asks the worker (via render_capabilities) which renderers actually resolve right
now, renders the same box ∪ cylinder from the same iso view at the same resolution
with each, and writes a labeled montage. Renderers whose binary isn't installed are
simply skipped, so the gallery reflects what this box can do.

Usage:
    .venv/bin/python3 scripts/render-gallery.py [--out PATH] [--material NAME]
                                                [--width W] [--height H]

Defaults: --out artifacts/rendering/render_renderers_gallery.png, --material Gold, 480x360 panels.
Photoreal output is presentation-only / not bit-reproducible (sampler noise), so
this lives in scripts/, not the test suite.
"""
import argparse
import base64
import io
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ankusdrive import Worker                                   # noqa: E402
from ankusdrive.client import WorkerError                       # noqa: E402

# Preferred display order; any others reported available are appended after these.
_ORDER = ["Blender", "Povray", "Luxcore", "Appleseed", "Cycles", "Ospray", "Pbrt"]

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "/System/Library/Fonts")


def _font(bold, size):
    names = ("DejaVuSans-Bold.ttf", "Arial Bold.ttf") if bold else ("DejaVuSans.ttf", "Arial.ttf")
    for d in _FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.is_file():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def _png_to_image(res):
    return Image.open(io.BytesIO(base64.b64decode(res["png_base64"]))).convert("RGB")


def _build_part(w):
    """A box with a cylinder fused on top — enough geometry to show off shading,
    reflection and shadows. Returns the fused solid's handle."""
    w.call("new_document", name="gallery")
    box = w.call("add_primitive", kind="box", w=40, d=28, h=14)
    cyl = w.call("add_primitive", kind="cylinder", r=8, h=26, placement=[20, 14, 0])
    fused = w.call("boolean_op", op="fuse", base=box["handle"], tool=cyl["handle"])
    return fused["handle"]


def _render(w, handle, renderer, material, width, height):
    """Render one renderer. Try the requested material; on a material-specific
    failure retry with the default material so one bad card doesn't drop a panel.
    Returns (image, material_used, seconds) or None if the renderer itself fails."""
    for mat in ([material, None] if material else [None]):
        t0 = time.time()
        try:
            res = w.call("render_photoreal", handle=handle, renderer=renderer,
                         view="iso", material=mat, width=width, height=height,
                         _timeout=600.0)
            return _png_to_image(res), mat, time.time() - t0
        except WorkerError as e:
            msg = e.remote_message.splitlines()[0]
            if mat is not None and "material" in msg.lower():
                print(f"  {renderer}: material {mat!r} failed ({msg}); retrying default")
                continue
            print(f"  {renderer}: FAILED — {msg}")
            return None
    return None


def _montage(panels, material, out_path):
    """panels: list of (renderer, image, material_used, seconds). Lay out a single
    labeled row on a dark card with a title strip."""
    pad, cap_h, title_h = 18, 52, 70
    pw = max(im.width for _, im, _, _ in panels)
    ph = max(im.height for _, im, _, _ in panels)
    n = len(panels)
    W = pad + n * (pw + pad)
    H = title_h + pad + ph + cap_h + pad
    bg, fg, sub = (32, 33, 36), (235, 235, 235), (165, 170, 178)

    canvas = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(canvas)
    d.text((pad, 20), "render_photoreal — one part, each renderer",
           font=_font(True, 30), fill=fg)

    f_name, f_meta = _font(True, 24), _font(False, 17)
    x = pad
    for renderer, im, mat, secs in panels:
        y = title_h + pad
        canvas.paste(im, (x + (pw - im.width) // 2, y + (ph - im.height) // 2))
        d.rectangle([x, y, x + pw - 1, y + ph - 1], outline=(70, 72, 78))
        cy = y + ph + 8
        d.text((x + 4, cy), renderer, font=f_name, fill=fg)
        meta = f"{mat or 'default material'} · {secs:.1f}s"
        d.text((x + 4, cy + 28), meta, font=f_meta, fill=sub)
        x += pw + pad

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return W, H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "artifacts" / "rendering" / "render_renderers_gallery.png"))
    ap.add_argument("--material", default="Gold", help="material card, or '' for default")
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--height", type=int, default=360)
    args = ap.parse_args()
    material = args.material or None

    with Worker() as w:
        caps = w.call("render_capabilities", _timeout=60.0)
        if not caps["addon_importable"]:
            sys.exit(f"Render addon not importable: {caps.get('addon_error')}")
        available = caps["available"]
        if not available:
            sys.exit("No renderers available — install one (scripts/install-renderers.sh).")
        ordered = [r for r in _ORDER if r in available] + \
                  [r for r in available if r not in _ORDER]
        print(f"Available renderers: {ordered}")
        handle = _build_part(w)

        panels = []
        for r in ordered:
            print(f"Rendering {r} ...")
            got = _render(w, handle, r, material, args.width, args.height)
            if got:
                im, mat, secs = got
                panels.append((r, im, mat, secs))
                print(f"  {r}: ok ({mat or 'default'}, {secs:.1f}s)")

    if not panels:
        sys.exit("No panels rendered.")
    out = Path(args.out)
    W, H = _montage(panels, material, out)
    print(f"\nWrote {out} ({W}x{H}) with {len(panels)} renderer(s): "
          f"{', '.join(r for r, *_ in panels)}")


if __name__ == "__main__":
    main()
