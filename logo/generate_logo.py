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
WM_ICON_GAP = 42      # ink gap between mark and text, when the icon is re-scaled

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

# ---- social preview card --------------------------------------------------
# GitHub's repo "social preview" slot is 1280x640 (2:1). GitHub's own template
# advises keeping everything important inside a 40px border, because the card is
# re-cropped for different surfaces (link unfurls, cards in feeds). The lockup is
# sized to SOCIAL_CONTENT_W/H and then centered on its ink, which at the defaults
# leaves at least ~100px of clear space on every side.
#
# The card has far more room than a README header, so it carries the mark larger
# relative to the type than the standard lockup does (SOCIAL_ICON_SC).
SOCIAL_W, SOCIAL_H = 1280, 640
SOCIAL_SAFE        = 40     # GitHub's recommended safe border, px
SOCIAL_CONTENT_W   = 1080   # target ink width; the rest is clear space
SOCIAL_CONTENT_H   = 420    # ...and the height cap, whichever binds first
SOCIAL_ICON_SC     = 2.05   # icon scale on the card (vs WM_ICON_SC in the lockup)

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


def icon_ink_extent():
    """Bounding box of the icon's actual ink, in the 100x100 icon frame.

    The icon viewBox is square for tiling, but the artwork inside it is not
    centered and does not fill it -- the point cloud and the dimension callout
    push out asymmetrically. Anything that centers the mark has to center this
    box, not the viewBox.
    """
    cx = cy = SIZE / 2
    t = math.radians(ANGLE)
    dx, dy = math.cos(t), math.sin(t)
    sep = RADIUS * (2 - 2 * OVERLAP)
    g1x, g1y = cx - sep / 2 * dx, cy - sep / 2 * dy       # generative hole
    g2x, g2y = cx + sep / 2 * dx, cy + sep / 2 * dy       # CAD hole

    # generative side: the sampled cloud reaches furthest -- ring at r*1.15, the
    # jitter adds at most +1.6, and the dots themselves are at most r=2.0
    # (see point_cloud)
    cloud_r = RADIUS * 1.15 + 1.6 + 2.0
    # CAD side: dash-dot centerlines run to r+6; the dimension line, its gap and
    # the label run further right (see cad_dimensions)
    cl = RADIUS + 6
    dim_r = RADIUS + 5.5 + 2.6 + len(DIM_LABEL) * DIM_FONT_SZ * 0.62
    # the pin itself
    pin = sep / 2 + RADIUS + max(BACK_EXT, TIP_EXT) + LINE_W / 2

    return dict(
        x0=min(g1x - cloud_r, g2x - cl, cx - pin * dx),
        y0=min(g1y - cloud_r, g2y - cl, cy - pin * dy),
        x1=max(g1x + cloud_r, g2x + dim_r, cx + pin * dx),
        y1=max(g1y + cloud_r, g2y + cl, cy + pin * dy),
    )


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
    # width is the ink width, used as the inter-word advance; x_ink is the
    # absolute right edge of the ink, which sits a left-side-bearing further
    # right than x + width and is what a bounding box has to use.
    ext = tp.get_extents()
    return (f'<path d="{" ".join(d)}" fill="{color}" fill-rule="nonzero"/>',
            ext.width, x + ext.x1)


def _live_text(text, size, x, y, color, weight):
    approx = len(text) * size * 0.56   # rough advance, only used for fallback layout
    return (f'<text x="{f(x)}" y="{f(y)}" font-family="{FONT_CSS}" '
            f'font-size="{size}" font-weight="{weight}" fill="{color}">{text}</text>',
            approx, x + approx)


def build_wordmark(pal, tagline=True, accent="second", outline=True, bg=True,
                   with_extent=False, icon_scale=None):
    """Horizontal lockup: icon + AnkusDrive + optional tagline.

    accent="second" -> "Drive" in the pin colour (default)
    accent="first"  -> "Ankus" in the pin colour
    bg=False        -> omit the background rect (for compositing, e.g. the
                       social card, which paints its own ground)
    with_extent     -> return (svg, inner_body, ink_extent) instead of just svg
    icon_scale      -> override WM_ICON_SC. The fixed WM_ICON_XY origin only
                       works for the default scale, so when this is given the
                       mark is instead placed by its *ink*: WM_ICON_GAP to the
                       left of the text, vertically centered on the text block.
                       Leave it None to reproduce the standard lockup exactly.
    """
    icon = build_icon(pal, tile=False)
    icon_inner = icon.split('xmlns="http://www.w3.org/2000/svg">', 1)[1].rsplit("</svg>", 1)[0]

    c1 = pal["line"] if accent == "first" else pal["word"]
    c2 = pal["word"] if accent == "first" else pal["line"]

    bold = _find_font(FONT_CANDIDATES_BOLD)
    reg  = _find_font(FONT_CANDIDATES_REG)
    use_outline = outline and bold is not None
    if use_outline:
        try:
            p1, w1, _ = _outline_text(WORD_1, bold, WM_FONT_SZ, WM_TEXT_X, WM_BASELINE, c1)
            p2, w2, x2 = _outline_text(WORD_2, bold, WM_FONT_SZ, WM_TEXT_X + w1 + 1,
                                       WM_BASELINE, c2)
            tag, xtag = "", 0.0
            if tagline:
                tag, _, xtag = _outline_text(TAGLINE, reg or bold, WM_TAG_SZ,
                                             WM_TEXT_X + 2, WM_BASELINE + 26, pal["gen"])
        except Exception as exc:                       # noqa: BLE001
            print(f"  ! outlining failed ({exc}); falling back to live text")
            use_outline = False
    if not use_outline:
        p1, w1, _ = _live_text(WORD_1, WM_FONT_SZ, WM_TEXT_X, WM_BASELINE, c1, "bold")
        p2, w2, x2 = _live_text(WORD_2, WM_FONT_SZ, WM_TEXT_X + w1 + 1, WM_BASELINE, c2, "bold")
        tag, xtag = "", 0.0
        if tagline:
            tag, _, xtag = _live_text(TAGLINE, WM_TAG_SZ, WM_TEXT_X + 2,
                                      WM_BASELINE + 26, pal["gen"], "normal")

    # Ink extent, in wordmark units. The viewBox is deliberately roomier than
    # the artwork; anything that needs to *center* the lockup (the social card)
    # has to center this box, not the viewBox.
    txt_right = max(x2, xtag if tagline else 0)
    txt_top = WM_BASELINE - WM_FONT_SZ * 0.73
    txt_bot = WM_BASELINE + (26 + WM_TAG_SZ * 0.25 if tagline else 0)

    ie = icon_ink_extent()
    sc = WM_ICON_SC if icon_scale is None else icon_scale
    if icon_scale is None:
        ox, oy = WM_ICON_XY
    else:
        # right ink edge WM_ICON_GAP before the text, ink centered on the text block
        ox = WM_TEXT_X - WM_ICON_GAP - ie["x1"] * sc
        oy = (txt_top + txt_bot) / 2 - (ie["y0"] + ie["y1"]) / 2 * sc
    ig = f'<g transform="translate({f(ox)},{f(oy)}) scale({f(sc)})">{icon_inner}</g>'

    ix0, iy0 = ox + ie["x0"] * sc, oy + ie["y0"] * sc
    ix1, iy1 = ox + ie["x1"] * sc, oy + ie["y1"] * sc
    extent = dict(
        x0=ix0,
        y0=min(iy0, txt_top),
        x1=max(ix1, txt_right),
        y1=max(iy1, txt_bot),
    )

    ground = (f'<rect width="{WM_W}" height="{WM_H}" fill="{pal["bg"]}"/>'
              if bg else "")
    body = f'{ground}{ig}{p1}{p2}{tag}'
    svg = (f'<svg viewBox="0 0 {WM_W} {WM_H}" xmlns="http://www.w3.org/2000/svg">'
           f'{body}</svg>')
    return (svg, body, extent) if with_extent else svg


# ============================================================================
# social preview card (GitHub 1280x640)
# ============================================================================

def build_social(pal, tagline=True, accent="second", outline=True):
    """The repo social-preview card: the wordmark lockup centered on a
    1280x640 ground.

    GitHub crops this card differently across surfaces, so the lockup is sized
    to SOCIAL_CONTENT_W/H and centered on its *ink*, which leaves far more than
    the recommended 40px safe border on every side.
    """
    _, inner, ext = build_wordmark(pal, tagline=tagline, accent=accent,
                                   outline=outline, bg=False, with_extent=True,
                                   icon_scale=SOCIAL_ICON_SC)

    bw, bh = ext["x1"] - ext["x0"], ext["y1"] - ext["y0"]
    scale = min(SOCIAL_CONTENT_W / bw, SOCIAL_CONTENT_H / bh)

    # translate so the ink box lands centered on the card
    tx = (SOCIAL_W - bw * scale) / 2 - ext["x0"] * scale
    ty = (SOCIAL_H - bh * scale) / 2 - ext["y0"] * scale
    margin = min((SOCIAL_W - bw * scale) / 2, (SOCIAL_H - bh * scale) / 2)
    if margin < SOCIAL_SAFE:
        print(f"  ! social card: artwork within {SOCIAL_SAFE}px of the crop edge "
              f"(margin {margin:.0f}px); lower SOCIAL_CONTENT_W/H")

    return (f'<svg viewBox="0 0 {SOCIAL_W} {SOCIAL_H}" width="{SOCIAL_W}" '
            f'height="{SOCIAL_H}" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{SOCIAL_W}" height="{SOCIAL_H}" fill="{pal["bg"]}"/>'
            f'<g transform="translate({f(tx)},{f(ty)}) scale({f(scale)})">'
            f'{inner}</g></svg>')


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
    for sub in ("icon", "favicon", "wordmark", "social"):
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
        "social/ankusdrive-social.svg":                  build_social(LIGHT),
        "social/ankusdrive-social-dark.svg":             build_social(DARK),
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
    for rel in [k for k in svgs if k.startswith("social/")]:
        out = os.path.join(base, rel.replace(".svg", f"-{SOCIAL_W}x{SOCIAL_H}.png"))
        png(svgs[rel], out, SOCIAL_W, SOCIAL_H)
        n += 1
    print(f"wrote {n} PNG files")

    icons = [Image.open(os.path.join(base, f"favicon/favicon-{s}.png")).convert("RGBA")
             for s in PNG_FAVICON_SIZES]
    icons[0].save(os.path.join(base, "favicon/favicon.ico"),
                  sizes=[(s, s) for s in PNG_FAVICON_SIZES])
    print("wrote favicon/favicon.ico")


if __name__ == "__main__":
    main()
