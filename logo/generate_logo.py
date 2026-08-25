#!/usr/bin/env python3
"""
AnkusDrive logo generator
=========================

Regenerates the complete AnkusDrive brand asset set (icon, favicon, wordmark
lockups) as SVG, plus optional PNG/ICO exports.

    python3 generate_logo.py            # SVG + PNG + ICO (if deps available)
    python3 generate_logo.py --svg-only # SVG only, no dependencies needed

Dependencies
------------
  SVG output ....... none (pure standard library)
  PNG / ICO ........ pip install cairosvg pillow
  Outlined text .... pip install matplotlib   (see WORDMARK below)

Everything you'd want to tune lives in the CONFIG block. Geometry is described
in a 100x100 viewBox; all values are in those units.

The design
----------
A slate line -- the goad/pin -- pierces two overlapping holes:
  * the GENERATIVE hole (teal): attention-grid matrix + sampled point cloud
  * the CAD hole (ink): dimensioned datum with dash-dot centerlines
They overlap like a Venn diagram; the line threads under the far rim and over
the near rim of each, so it reads as passing THROUGH both onto one shared axis.

Note: the artwork produced by this script is a project brand asset and is NOT
covered by the repository's Apache-2.0 license. See TRADEMARKS.md.
"""

import argparse
import math
import os
import random

# ============================================================================
# CONFIG -- edit these
# ============================================================================

# ---- palettes -------------------------------------------------------------
# line  : the pin/goad. Slate, because that's the bare steel such a tool is made of.
# gen   : the generative / LLM hole.
# cad   : the precise CAD datum hole + its dimension annotations.
# word  : the "Ankus" half of the wordmark ("Drive" uses `line`).
# bg    : tile background.
LIGHT = dict(bg="#F2EFE7", gen="#2E8B84", cad="#1E2A2A", line="#5E6A78", word="#1E2A2A")
DARK  = dict(bg="#12201E", gen="#4FB3AA", cad="#DCE7E5", line="#93A0AE", word="#DCE7E5")

# ---- geometry -------------------------------------------------------------
SIZE        = 100     # viewBox is SIZE x SIZE
ANGLE       = 32      # axis angle, degrees clockwise from +x
OVERLAP     = 0.40    # hole overlap, 0 = tangent, 1 = concentric
RADIUS      = 15      # hole radius (to ring centerline)
RING_W      = 2.6     # ring stroke width
LINE_W      = 2.2     # the pin line stroke width
BACK_EXT    = 7       # how far the line extends past the generative hole
TIP_EXT     = 7       # how far the line extends past the CAD hole
TILE_RADIUS = 22      # rounded-corner radius of the background tile

# ---- CAD dimension callout ------------------------------------------------
# "Ø14" encodes the initials: A = 1, D = 4.
DIM_LABEL   = "Ø14"
DIM_FONT_SZ = 5.0

# ---- generative hole texture ---------------------------------------------
GRID_ROWS   = 5       # attention-grid matrix rows/cols
GRID_SEED   = 7
GRID_MIN_OP = 0.07
GRID_MAX_OP = 0.42
CLOUD_N     = 9       # points sampled around the aperture
CLOUD_SEED  = 5

# ---- wordmark -------------------------------------------------------------
WORD_1      = "Ankus"
WORD_2      = "Drive"
TAGLINE     = "align generative intent with the CAD kernel"
WM_W, WM_H  = 700, 180
WM_FONT_SZ  = 60
WM_TAG_SZ   = 15
WM_TEXT_X   = 168     # left edge of the wordmark text
WM_BASELINE = 110     # text baseline
WM_ICON_XY  = (12, 26)
WM_ICON_SC  = 1.28

# Font search order for outlining the wordmark. First hit wins.
FONT_CANDIDATES_BOLD = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
FONT_CANDIDATES_REG = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
# CSS fallback used if text cannot be outlined (see WORDMARK note below).
FONT_CSS = "Helvetica Neue, Helvetica, Inter, Arial, sans-serif"

# ---- export ---------------------------------------------------------------
PNG_ICON_SIZES    = (512, 256, 128)
PNG_FAVICON_SIZES = (48, 32, 16)
PNG_WORDMARK_W    = 1280

MONO = "ui-monospace, DejaVu Sans Mono, Menlo, monospace"


# ============================================================================
# primitives
# ============================================================================

def f(v):
    """Format a number for SVG, trimming float noise."""
    return f"{v:.2f}"


def _circle_path(r):
    """A full circle as two arcs, usable inside an evenodd compound path."""
    return (f"M {f(r)} 0 A {f(r)} {f(r)} 0 0 1 {f(-r)} 0 "
            f"A {f(r)} {f(r)} 0 0 1 {f(r)} 0 Z ")


def ring_halves(cx, cy, deg, R, tw, color, uid):
    """A ring split into 'far' and 'near' bands relative to the pin axis.

    The ring is drawn in a local frame rotated to `deg`, then clipped by the
    local y=0 line. Drawing far -> line -> near makes the line appear to pass
    *through* the hole rather than sit on top of it.

    Returns (defs, far_svg, near_svg).
    """
    rin = R - tw
    big = R * 2.4
    annulus = (f'<path d="{_circle_path(R)}{_circle_path(rin)}" '
               f'fill="{color}" fill-rule="evenodd"/>')
    defs = (f'<clipPath id="{uid}n">'
            f'<rect x="{f(-big)}" y="0" width="{f(2*big)}" height="{f(big)}"/></clipPath>'
            f'<clipPath id="{uid}b">'
            f'<rect x="{f(-big)}" y="{f(-big)}" width="{f(2*big)}" height="{f(big)}"/></clipPath>')
    xf = f'transform="translate({f(cx)},{f(cy)}) rotate({f(deg)})"'
    far  = f'<g {xf}><g clip-path="url(#{uid}b)">{annulus}</g></g>'
    near = f'<g {xf}><g clip-path="url(#{uid}n)">{annulus}</g></g>'
    return defs, far, near


def attention_grid(cx, cy, r, clip_id, color,
                   rows=GRID_ROWS, gap=0.7, seed=GRID_SEED,
                   min_op=GRID_MIN_OP, max_op=GRID_MAX_OP):
    """Varying-opacity matrix clipped to the aperture -- the 'attention weights'."""
    rnd = random.Random(seed)
    span = r * 1.6
    cell = span / rows
    x0, y0 = cx - span / 2, cy - span / 2
    cells = []
    for row in range(rows):
        for col in range(rows):
            cells.append(
                f'<rect x="{f(x0+col*cell)}" y="{f(y0+row*cell)}" '
                f'width="{f(cell-gap)}" height="{f(cell-gap)}" '
                f'opacity="{round(rnd.uniform(min_op, max_op), 2)}"/>')
    return (f'<g clip-path="url(#{clip_id})" fill="{color}">'
            + "".join(cells) + "</g>")


def point_cloud(cx, cy, r, color, n=CLOUD_N, seed=CLOUD_SEED):
    """Sampled points loosely tracing the aperture, plus a few spurious outliers."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        a = 2 * math.pi * i / n + rnd.uniform(-0.26, 0.26)
        rr = r * 1.15 + rnd.uniform(-1.2, 1.6)
        out.append(
            f'<circle cx="{f(cx+rr*math.cos(a))}" cy="{f(cy+rr*math.sin(a))}" '
            f'r="{f(rnd.uniform(1.3, 2.0))}" fill="{color}" '
            f'opacity="{round(rnd.uniform(0.6, 0.92), 2)}"/>')
    for ox, oy, op in ((r * 1.50, -r * 0.35, 0.42),
                       (-r * 0.45, -r * 1.50, 0.40),
                       (r * 0.35,  r * 1.50, 0.42)):
        out.append(f'<circle cx="{f(cx+ox)}" cy="{f(cy+oy)}" r="1.3" '
                   f'fill="{color}" opacity="{op}"/>')
    return "".join(out)


def cad_dimensions(cx, cy, r, color, label=DIM_LABEL, fs=DIM_FONT_SZ):
    """Draftsman annotations: dash-dot centerlines, a diameter dimension with
    arrowheads and extension lines, and a center datum dot."""
    def arrow(px, py, dx, dy, sz=2.3):
        m = math.hypot(dx, dy)
        dx, dy = dx / m, dy / m
        ox, oy = -dy, dx
        return (f'<polygon points="{f(px)},{f(py)} '
                f'{f(px-dx*sz+ox*sz*0.42)},{f(py-dy*sz+oy*sz*0.42)} '
                f'{f(px-dx*sz-ox*sz*0.42)},{f(py-dy*sz-oy*sz*0.42)}" fill="{color}"/>')

    ext = r + 6
    s = (f'<line x1="{f(cx-ext)}" y1="{f(cy)}" x2="{f(cx+ext)}" y2="{f(cy)}" '
         f'stroke="{color}" stroke-width="0.8" stroke-dasharray="5 1.8 0.8 1.8" opacity="0.8"/>'
         f'<line x1="{f(cx)}" y1="{f(cy-ext)}" x2="{f(cx)}" y2="{f(cy+ext)}" '
         f'stroke="{color}" stroke-width="0.8" stroke-dasharray="5 1.8 0.8 1.8" opacity="0.8"/>')

    xoff = cx + r + 5.5
    s += (f'<line x1="{f(cx+r*0.5)}" y1="{f(cy-r)}" x2="{f(xoff+1.5)}" y2="{f(cy-r)}" '
          f'stroke="{color}" stroke-width="0.6" opacity="0.65"/>'
          f'<line x1="{f(cx+r*0.5)}" y1="{f(cy+r)}" x2="{f(xoff+1.5)}" y2="{f(cy+r)}" '
          f'stroke="{color}" stroke-width="0.6" opacity="0.65"/>'
          f'<line x1="{f(xoff)}" y1="{f(cy-r+0.2)}" x2="{f(xoff)}" y2="{f(cy+r-0.2)}" '
          f'stroke="{color}" stroke-width="0.8"/>')
    s += arrow(xoff, cy - r, 0, -1) + arrow(xoff, cy + r, 0, 1)
    s += (f'<text x="{f(xoff+2.6)}" y="{f(cy+fs*0.36)}" font-family="{MONO}" '
          f'font-size="{fs}" fill="{color}">{label}</text>')
    s += f'<circle cx="{f(cx)}" cy="{f(cy)}" r="1.6" fill="{color}"/>'
    return s


# ============================================================================
# the mark
# ============================================================================

def build_icon(pal, tile=True, favicon=False, detail=True):
    """Compose the icon.

    favicon=True strips the interior texture and annotations, leaving two
    overlapping rings + the line -- legible down to ~16px.
    """
    cx = cy = SIZE / 2
    t = math.radians(ANGLE)
    dx, dy = math.cos(t), math.sin(t)

    sep = RADIUS * (2 - 2 * OVERLAP)          # distance between hole centers
    g1x, g1y = cx - sep / 2 * dx, cy - sep / 2 * dy   # generative hole
    g2x, g2y = cx + sep / 2 * dx, cy + sep / 2 * dy   # CAD hole

    back_a = -(sep / 2 + RADIUS + BACK_EXT)
    tip_a  = (sep / 2 + RADIUS + TIP_EXT)
    line = (f'<line x1="{f(cx+back_a*dx)}" y1="{f(cy+back_a*dy)}" '
            f'x2="{f(cx+tip_a*dx)}" y2="{f(cy+tip_a*dy)}" '
            f'stroke="{pal["line"]}" stroke-width="{LINE_W}" stroke-linecap="round"/>')

    d1, far1, near1 = ring_halves(g1x, g1y, ANGLE, RADIUS, RING_W, pal["gen"], "g1")
    d2, far2, near2 = ring_halves(g2x, g2y, ANGLE, RADIUS, RING_W, pal["cad"], "g2")
    defs = d1 + d2

    if favicon or not detail:
        body = far1 + far2 + line + near1 + near2
    else:
        defs += (f'<clipPath id="hm"><circle cx="{f(g1x)}" cy="{f(g1y)}" '
                 f'r="{f(RADIUS-0.6)}"/></clipPath>')
        grid  = attention_grid(g1x, g1y, RADIUS, "hm", pal["gen"])
        cloud = point_cloud(g1x, g1y, RADIUS, pal["gen"])
        dims  = cad_dimensions(g2x, g2y, RADIUS, pal["cad"])
        # far rims -> line -> near rims  == the line threads through both holes
        body = grid + far1 + far2 + line + near1 + near2 + cloud + dims

    bg = (f'<rect width="{SIZE}" height="{SIZE}" rx="{TILE_RADIUS}" '
          f'fill="{pal["bg"]}"/>') if tile else ""
    return (f'<svg viewBox="0 0 {SIZE} {SIZE}" xmlns="http://www.w3.org/2000/svg">'
            f'<defs>{defs}</defs>{bg}{body}</svg>')


# ============================================================================
# wordmark
# ============================================================================
# Text is converted to outlines so the SVG renders identically on machines that
# don't have the brand font installed. That needs matplotlib. Without it we fall
# back to live <text> elements, which look right only where the font exists --
# fine for previewing, not for shipping.

def _find_font(candidates):
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _outline_text(text, font_file, size, x, y, color):
    """Return (svg_path, advance_width) with the glyphs converted to outlines."""
    from matplotlib.textpath import TextPath
    from matplotlib.font_manager import FontProperties
    tp = TextPath((0, 0), text, size=size, prop=FontProperties(fname=font_file))
    verts, codes = tp.vertices, tp.codes
    d, i = [], 0
    while i < len(codes):
        c = codes[i]
        if c == 1:      # MOVETO
            d.append(f"M {f(x+verts[i][0])} {f(y-verts[i][1])}"); i += 1
        elif c == 2:    # LINETO
            d.append(f"L {f(x+verts[i][0])} {f(y-verts[i][1])}"); i += 1
        elif c == 3:    # CURVE3
            d.append(f"Q {f(x+verts[i][0])} {f(y-verts[i][1])} "
                     f"{f(x+verts[i+1][0])} {f(y-verts[i+1][1])}"); i += 2
        elif c == 4:    # CURVE4
            d.append(f"C {f(x+verts[i][0])} {f(y-verts[i][1])} "
                     f"{f(x+verts[i+1][0])} {f(y-verts[i+1][1])} "
                     f"{f(x+verts[i+2][0])} {f(y-verts[i+2][1])}"); i += 3
        elif c == 79:   # CLOSEPOLY
            d.append("Z"); i += 1
        else:
            i += 1
    return (f'<path d="{" ".join(d)}" fill="{color}" fill-rule="nonzero"/>',
            tp.get_extents().width)


def _live_text(text, size, x, y, color, weight):
    approx = len(text) * size * 0.56   # rough advance, only used for fallback layout
    return (f'<text x="{f(x)}" y="{f(y)}" font-family="{FONT_CSS}" '
            f'font-size="{size}" font-weight="{weight}" fill="{color}">{text}</text>',
            approx)


def build_wordmark(pal, tagline=True, accent="second", outline=True):
    """Horizontal lockup: icon + AnkusDrive + optional tagline.

    accent="second" -> "Drive" in the pin colour (default)
    accent="first"  -> "Ankus" in the pin colour
    """
    icon = build_icon(pal, tile=False)
    inner = icon.split('xmlns="http://www.w3.org/2000/svg">', 1)[1].rsplit("</svg>", 1)[0]
    ig = (f'<g transform="translate({WM_ICON_XY[0]},{WM_ICON_XY[1]}) '
          f'scale({WM_ICON_SC})">{inner}</g>')

    c1 = pal["line"] if accent == "first" else pal["word"]
    c2 = pal["word"] if accent == "first" else pal["line"]

    bold = _find_font(FONT_CANDIDATES_BOLD)
    reg  = _find_font(FONT_CANDIDATES_REG)
    use_outline = outline and bold is not None
    if use_outline:
        try:
            p1, w1 = _outline_text(WORD_1, bold, WM_FONT_SZ, WM_TEXT_X, WM_BASELINE, c1)
            p2, _  = _outline_text(WORD_2, bold, WM_FONT_SZ, WM_TEXT_X + w1 + 1,
                                   WM_BASELINE, c2)
            tag = ""
            if tagline:
                tag, _ = _outline_text(TAGLINE, reg or bold, WM_TAG_SZ,
                                       WM_TEXT_X + 2, WM_BASELINE + 26, pal["gen"])
        except Exception as exc:                       # noqa: BLE001
            print(f"  ! outlining failed ({exc}); falling back to live text")
            use_outline = False
    if not use_outline:
        p1, w1 = _live_text(WORD_1, WM_FONT_SZ, WM_TEXT_X, WM_BASELINE, c1, "bold")
        p2, _  = _live_text(WORD_2, WM_FONT_SZ, WM_TEXT_X + w1 + 1, WM_BASELINE, c2, "bold")
        tag = ""
        if tagline:
            tag, _ = _live_text(TAGLINE, WM_TAG_SZ, WM_TEXT_X + 2,
                                WM_BASELINE + 26, pal["gen"], "normal")

    bg = f'<rect width="{WM_W}" height="{WM_H}" fill="{pal["bg"]}"/>'
    return (f'<svg viewBox="0 0 {WM_W} {WM_H}" xmlns="http://www.w3.org/2000/svg">'
            f'{bg}{ig}{p1}{p2}{tag}</svg>')


# ============================================================================
# export
# ============================================================================

def main():
    ap = argparse.ArgumentParser(description="Generate AnkusDrive brand assets.")
    ap.add_argument("--svg-only", action="store_true",
                    help="skip PNG/ICO export (no third-party dependencies)")
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)),
                    help="output directory (default: this script's directory)")
    args = ap.parse_args()

    base = args.out
    for sub in ("icon", "favicon", "wordmark"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)

    def write(rel, svg):
        path = os.path.join(base, rel)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg)
        return path

    # ---- SVG ----
    svgs = {
        "icon/ankusdrive-icon.svg":                   build_icon(LIGHT, tile=True),
        "icon/ankusdrive-icon-dark.svg":              build_icon(DARK,  tile=True),
        "icon/ankusdrive-icon-transparent.svg":       build_icon(LIGHT, tile=False),
        "icon/ankusdrive-icon-dark-transparent.svg":  build_icon(DARK,  tile=False),
        "favicon/ankusdrive-favicon.svg":             build_icon(LIGHT, favicon=True),
        "favicon/ankusdrive-favicon-dark.svg":        build_icon(DARK,  favicon=True),
        "wordmark/ankusdrive-wordmark.svg":              build_wordmark(LIGHT),
        "wordmark/ankusdrive-wordmark-dark.svg":         build_wordmark(DARK),
        "wordmark/ankusdrive-wordmark-compact.svg":      build_wordmark(LIGHT, tagline=False),
        "wordmark/ankusdrive-wordmark-compact-dark.svg": build_wordmark(DARK,  tagline=False),
        "wordmark/ankusdrive-wordmark-alt.svg":          build_wordmark(LIGHT, accent="first"),
        "wordmark/ankusdrive-wordmark-alt-dark.svg":     build_wordmark(DARK,  accent="first"),
    }
    for rel, svg in svgs.items():
        write(rel, svg)
    print(f"wrote {len(svgs)} SVG files to {base}")

    if args.svg_only:
        return

    try:
        import cairosvg
        from PIL import Image
    except ImportError:
        print("! cairosvg/pillow not installed -- SVGs written, skipping PNG/ICO.")
        print("  pip install cairosvg pillow")
        return

    def png(svg, path, width, height=None):
        cairosvg.svg2png(bytestring=svg.encode(), write_to=path,
                         output_width=width, output_height=height)

    n = 0
    for size in PNG_ICON_SIZES:
        png(svgs["icon/ankusdrive-icon.svg"],
            os.path.join(base, f"icon/ankusdrive-icon-{size}.png"), size, size)
        png(svgs["icon/ankusdrive-icon-dark.svg"],
            os.path.join(base, f"icon/ankusdrive-icon-dark-{size}.png"), size, size)
        png(svgs["icon/ankusdrive-icon-transparent.svg"],
            os.path.join(base, f"icon/ankusdrive-icon-transparent-{size}.png"), size, size)
        n += 3
    for size in PNG_FAVICON_SIZES:
        png(svgs["favicon/ankusdrive-favicon.svg"],
            os.path.join(base, f"favicon/favicon-{size}.png"), size, size)
        n += 1
    for rel in [k for k in svgs if k.startswith("wordmark/")]:
        out = os.path.join(base, rel.replace(".svg", f"-{PNG_WORDMARK_W}.png"))
        png(svgs[rel], out, PNG_WORDMARK_W)
        n += 1
    print(f"wrote {n} PNG files")

    icons = [Image.open(os.path.join(base, f"favicon/favicon-{s}.png")).convert("RGBA")
             for s in PNG_FAVICON_SIZES]
    icons[0].save(os.path.join(base, "favicon/favicon.ico"),
                  sizes=[(s, s) for s in PNG_FAVICON_SIZES])
    print("wrote favicon/favicon.ico")


if __name__ == "__main__":
    main()
