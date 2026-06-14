"""Animate the SHIFT — how a constant-mesh box changes ratio.

The gears never move; they stay meshed and always spinning (each freewheels on the
mainshaft at its own speed). A dog COLLAR, splined to the mainshaft, SLIDES along it
(pushed by a shift fork) until its dog teeth interlock the chosen gear, locking that
gear to the shaft. Slide to a different gear -> different already-spinning gear is
clutched to the output -> different ratio.

Side view along the mainshaft. The output dial (top-right) shows the shaft's speed
stepping through the ratios as the collar engages each gear.

  .venv/bin/python3 scratch/gearbox_shift_animate.py
Outputs: artifacts/gearbox_shift.gif + artifacts/gearbox_shift_filmstrip.png
"""
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
import io  # noqa: E402
from PIL import Image  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ART = REPO / "artifacts"

# speed gears on the mainshaft (x position, radius ~ tooth count, ratio, label)
GEARS = [
    {"x": 2.5, "r": 2.0, "ratio": 0.286, "name": "1st  (28T)"},
    {"x": 5.5, "r": 1.6, "ratio": 0.667, "name": "2nd  (20T)"},
    {"x": 8.5, "r": 1.2, "ratio": 1.556, "name": "3rd  (12T)"},
]
COL_A_NEUTRAL, COL_B_NEUTRAL = 4.0, 9.9      # collar-A between 1st/2nd; collar-B by 3rd
SHAFT_Y = 0.0

# dog-tooth geometry for the schematic (axial depth of a band; radial span)
GDOG, CDOG = 0.42, 0.42          # gear / collar tooth band axial depth
GDOG_H = 1.30                     # radial height the comb spans (5 teeth)
ENGAGE_OVERLAP = 0.28            # how far the meshed combs interpenetrate axially
NEUTRAL_GAP = 0.20              # visible axial clearance when disengaged


def build_timeline():
    """Per-frame (colA_x, colB_x, engaged_index_or_None, label)."""
    seq = []

    def hold(ca, cb, eng, label, n):
        seq.extend([(ca, cb, eng, label)] * n)

    def slide(ca0, cb0, ca1, cb1, eng, label, n):
        for k in range(n):
            t = k / (n - 1)
            seq.append((ca0 + (ca1 - ca0) * t, cb0 + (cb1 - cb0) * t, eng, label))

    A0, B0 = COL_A_NEUTRAL, COL_B_NEUTRAL
    A1 = GEARS[0]["x"] + 0.95          # collar-A engaged with 1st
    A2 = GEARS[1]["x"] - 0.95          # collar-A engaged with 2nd
    B3 = GEARS[2]["x"] - 0.95          # collar-B engaged with 3rd
    hold(A0, B0, None, "NEUTRAL — every gear freewheels, output disconnected", 16)
    slide(A0, B0, A1, B0, None, "shift fork slides collar-A toward 1st...", 18)
    hold(A1, B0, 0, "1st GEAR engaged — output = 0.286 x input", 26)
    slide(A1, B0, A0, B0, None, "...back to neutral...", 14)
    slide(A0, B0, A2, B0, None, "...slide collar-A the other way toward 2nd...", 18)
    hold(A2, B0, 1, "2nd GEAR engaged — output = 0.667 x input", 26)
    slide(A2, B0, A0, B0, None, "...neutral...", 12)
    slide(A0, B0, A0, B3, None, "...slide collar-B toward 3rd...", 18)
    hold(A0, B3, 2, "3rd GEAR engaged — output = 1.556 x input (overdrive)", 26)
    return seq


def dog_band(ax, x, y, w, h, color, n=5, phase=0, zorder=5):
    """A castellated band of n dog teeth spanning height h, centred at (x, y).

    The teeth are the COMB that does the clutching: each tooth is ~60% of a
    pitch wide so the gaps between them are real, openable slots (echoing the
    0.45*pitch teeth the §11.10 CAD builds). ``phase`` shifts the comb by a
    fraction of one pitch — pass phase=0.5 on the mating face so its teeth fall
    in the OTHER comb's gaps (half-pitch offset, exactly how the real collar is
    rotated math.pi/ND from the gear) and the two combs interlock instead of
    butting tooth-on-tooth. ``x`` here is the AXIAL tip plane of the band and
    ``w`` its axial depth; the teeth run radially (along y) so they read as a
    comb when seen from the side.
    """
    pitch = h / n
    tooth = pitch * 0.6                                   # ~60% solid, ~40% gap
    for i in range(n):
        yc = y - h / 2 + (i + (phase % 1.0)) * pitch
        ax.add_patch(Rectangle((x, yc - tooth / 2), w, tooth,
                               color=color, zorder=zorder))


def _mesh_glow(ax, x0, x1):
    """Highlight the axial band where two combs interleave (teeth-into-gaps)."""
    if x1 <= x0:
        return
    ax.add_patch(Rectangle((x0, -GDOG_H / 2 - 0.08), x1 - x0, GDOG_H + 0.16,
                           fc="#fff6c8", ec="none", alpha=0.55, zorder=4))


def _nearest_gear_tip(cx, side):
    """Tip plane of the nearest gear dog band on ``side`` (-1 left / +1 right) of cx.

    Used to keep a *disengaged* collar's comb from poking into a gear's gaps, so
    neutral always reads as a clear axial gap rather than accidental meshing.
    """
    tips = []
    for g in GEARS:
        if side < 0 and g["x"] < cx:
            tips.append(g["x"] + 0.45 + GDOG)        # right-face tip plane
        elif side > 0 and g["x"] > cx:
            tips.append(g["x"] - 0.45 - GDOG)        # left-face tip plane
    if not tips:
        return None
    return max(tips) if side < 0 else min(tips)


def draw(ax, axd, colA, colB, engaged, label, out_angle, frame):
    ax.clear(); ax.set_xlim(0, 11.5); ax.set_ylim(-3.2, 3.6); ax.axis("off")
    # mainshaft
    ax.add_patch(Rectangle((0.3, SHAFT_Y - 0.18), 10.2, 0.36, color="#888", zorder=1))
    ax.text(0.3, -0.6, "mainshaft (output) →", fontsize=8, color="#555")
    # freewheeling speed gears (each spins at its own rate; show a marker)
    for gi, g in enumerate(GEARS):
        lit = (engaged == gi)
        col = "#5bcf7a" if lit else "#a9c8e8"
        ax.add_patch(Rectangle((g["x"] - 0.45, -g["r"]), 0.9, 2 * g["r"],
                               color=col, zorder=2, alpha=0.95))
        # each freewheeler keeps spinning: a small rotating spoke on its face
        spin = frame * 0.25 * g["ratio"] * 6
        ax.plot([g["x"], g["x"] + 0.35 * math.cos(spin)],
                [g["r"] - 0.4, g["r"] - 0.4 + 0.35 * math.sin(spin)], color="white", lw=1.5, zorder=3)
        ax.text(g["x"], -g["r"] - 0.35, g["name"], ha="center", fontsize=8,
                color="#1a7" if lit else "#555")
        # dog teeth on both faces (phase 0 — the collar comes in at phase 0.5).
        # tips point outward, away from the gear body, toward an approaching collar.
        gcol = "#2f7d45" if lit else "#444"
        dog_band(ax, g["x"] - 0.45 - GDOG, 0, GDOG, GDOG_H, gcol, phase=0)
        dog_band(ax, g["x"] + 0.45,        0, GDOG, GDOG_H, gcol, phase=0)
    # fixed shift rails (the rods the forks slide along) — drawn first, behind
    for x0, x1 in ((2.0, 5.6), (6.4, 10.3)):
        ax.add_patch(Rectangle((x0, 2.55), x1 - x0, 0.14, color="#999", zorder=2))
    ax.text(0.3, 2.75, "shift rails", fontsize=8, color="#555")

    def draw_collar(cx, tag, active, eng_gear):
        edge, body = "#7a4a10", ("#d98b3a" if active else "#ecd0a6")
        # grooved sleeve: hub + two raised flanges with a groove between them
        ax.add_patch(Rectangle((cx - 0.5, -0.85), 1.0, 1.35, fc=body, ec=edge, zorder=4))
        ax.add_patch(Rectangle((cx - 0.5, -0.85), 0.22, 1.85, fc=body, ec=edge, zorder=4))  # L flange
        ax.add_patch(Rectangle((cx + 0.28, -0.85), 0.22, 1.85, fc=body, ec=edge, zorder=4))  # R flange
        # Dog teeth on both ends, drawn HALF-PITCH offset (phase 0.5) from the gear
        # combs so they drop into the gear's gaps instead of butting tooth-on-tooth.
        # If this end is meshing a gear, slide its tips PAST the gear's tip plane
        # (axial overlap) so you can see them interleave; otherwise leave a gap.
        dcol = "#5a3000"
        # left end
        gL = GEARS[eng_gear] if (active and eng_gear is not None
                                 and GEARS[eng_gear]["x"] < cx) else None
        if gL is not None:                                   # meshing the gear on the left
            tip = gL["x"] + 0.45 + GDOG - ENGAGE_OVERLAP     # past the gear's tip plane
            dog_band(ax, tip, 0, CDOG, GDOG_H, dcol, phase=0.5)
            _mesh_glow(ax, tip, gL["x"] + 0.45 + GDOG)
        else:                                                # disengaged: keep clear daylight
            tip = cx - 0.5 - CDOG
            gn = _nearest_gear_tip(cx, side=-1)
            if gn is not None:
                tip = max(tip, gn + NEUTRAL_GAP)             # don't poke into a gear's gaps
            dog_band(ax, tip, 0, CDOG, GDOG_H, dcol, phase=0.5)
        # right end
        gR = GEARS[eng_gear] if (active and eng_gear is not None
                                 and GEARS[eng_gear]["x"] > cx) else None
        if gR is not None:                                   # meshing the gear on the right
            tip = gR["x"] - 0.45 - GDOG - CDOG + ENGAGE_OVERLAP
            dog_band(ax, tip, 0, CDOG, GDOG_H, dcol, phase=0.5)
            _mesh_glow(ax, gR["x"] - 0.45 - GDOG, tip + CDOG)
        else:                                                # disengaged: keep clear daylight
            tip = cx + 0.5
            gn = _nearest_gear_tip(cx, side=+1)
            if gn is not None:
                tip = min(tip, gn - NEUTRAL_GAP - CDOG)
            dog_band(ax, tip, 0, CDOG, GDOG_H, dcol, phase=0.5)
        ax.text(cx, -1.15, tag, ha="center", fontsize=7, color=edge)
        # shift fork: two prongs riding in the groove + yoke + stem up to the rail
        fc = "#333" if active else "#b0b0b0"
        ax.add_patch(Rectangle((cx - 0.30, 0.50), 0.07, 1.05, color=fc, zorder=6))   # L prong
        ax.add_patch(Rectangle((cx + 0.23, 0.50), 0.07, 1.05, color=fc, zorder=6))   # R prong
        ax.add_patch(Rectangle((cx - 0.30, 1.48), 0.60, 0.12, color=fc, zorder=6))   # yoke
        ax.add_patch(Rectangle((cx - 0.06, 1.55), 0.12, 1.1, color=fc, zorder=6))    # stem to rail
        ax.add_patch(plt.Circle((cx, 2.62), 0.13, color=fc, zorder=7))               # rail clamp

    draw_collar(colA, "collar-A", engaged in (0, 1), engaged if engaged in (0, 1) else None)
    draw_collar(colB, "collar-B", engaged == 2, 2 if engaged == 2 else None)
    # which fork is being pushed
    fork_x = colA if engaged != 2 else colB
    if engaged is not None:
        ax.annotate("", xy=(fork_x, 3.05), xytext=(fork_x + (0.9 if engaged == 1 else -0.9), 3.05),
                    arrowprops=dict(arrowstyle="-|>", color="#1a7", lw=2.5))
        ax.text(fork_x, 3.3, "fork pushes the collar", ha="center", fontsize=8, color="#1a7")
        # fixed bottom-centre so it never overflows into the output dial on the right
        ax.text(5.75, -2.95,
                "dog teeth interleave (half-pitch offset) — they mesh into the gaps, "
                "they don't jam tooth-on-tooth", ha="center", fontsize=7.5,
                color="#a06000")
    ax.set_title(label, fontsize=11, color="#1a7" if engaged is not None else "#555")
    # output dial
    axd.clear(); axd.set_xlim(-1.3, 1.3); axd.set_ylim(-1.3, 1.4); axd.set_aspect("equal"); axd.axis("off")
    axd.add_patch(plt.Circle((0, 0), 1.0, fill=False, lw=2, color="#333"))
    axd.plot([0, math.cos(out_angle)], [0, math.sin(out_angle)], lw=3,
             color="#5bcf7a" if engaged is not None else "#bbb")
    r = GEARS[engaged]["ratio"] if engaged is not None else 0.0
    axd.set_title(f"output speed\n{r:.3f} x input" if engaged is not None
                  else "output speed\nfree (0)", fontsize=9)


def main():
    seq = build_timeline()
    fig = plt.figure(figsize=(11, 5))
    ax = fig.add_axes([0.0, 0.0, 0.74, 0.92])
    axd = fig.add_axes([0.76, 0.18, 0.22, 0.6])
    ART.mkdir(parents=True, exist_ok=True)

    out_angle, images = 0.0, []
    for i in range(len(seq)):
        colA, colB, eng, label = seq[i]
        out_angle += 0.25 * (GEARS[eng]["ratio"] if eng is not None else 0.0) * 6
        draw(ax, axd, colA, colB, eng, label, out_angle, i)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=82)
        buf.seek(0)
        images.append(Image.open(buf).convert("RGB"))
    gif = ART / "gearbox_shift.gif"
    images[0].save(str(gif), save_all=True, append_images=images[1:], duration=70, loop=0)
    print(f"  shift GIF -> {gif}  ({gif.stat().st_size:,} bytes, {len(images)} frames)")

    # filmstrip: one representative frame per stage (neutral, 1st, 2nd, 3rd)
    picks = [8, 50, 108, 168]
    out = 0.0
    angles = []
    for i in range(len(seq)):
        out += 0.25 * (GEARS[seq[i][2]]["ratio"] if seq[i][2] is not None else 0) * 6
        angles.append(out)
    fig2 = plt.figure(figsize=(20, 4.2))
    for j, idx in enumerate(picks):
        a = fig2.add_axes([j * 0.25 + 0.005, 0.12, 0.18, 0.78])
        ad = fig2.add_axes([j * 0.25 + 0.185, 0.30, 0.055, 0.45])
        colA, colB, eng, label = seq[idx]
        draw(a, ad, colA, colB, eng, label, angles[idx], idx)
    strip = ART / "gearbox_shift_filmstrip.png"
    fig2.savefig(str(strip), dpi=95)
    print(f"  filmstrip -> {strip}")


if __name__ == "__main__":
    main()
