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
from matplotlib.patches import Rectangle, FancyBboxPatch  # noqa: E402
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


def dog_band(ax, x, y, w, h, color, n=5):
    """A little castellated band (dog teeth) of width w centred at x."""
    for i in range(n):
        ax.add_patch(Rectangle((x - w / 2 + i * w / n, y - h / 2), w / n * 0.6, h,
                               color=color, zorder=5))


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
        dog_band(ax, g["x"] + 0.45, 0, 0.5, 0.7, "#444")     # dog teeth on the gear face
    # collars (splined to the shaft; slide axially)
    for cx, tag in ((colA, "collar-A"), (colB, "collar-B")):
        ax.add_patch(FancyBboxPatch((cx - 0.5, -0.85), 1.0, 1.7,
                     boxstyle="round,pad=0.02", fc="#d98b3a", ec="#7a4a10", zorder=4))
        ax.text(cx, 1.05, tag, ha="center", fontsize=7, color="#7a4a10")
        dog_band(ax, cx - 0.5, 0, 0.5, 0.7, "#5a3000")       # dog teeth facing left
        dog_band(ax, cx + 0.5, 0, 0.5, 0.7, "#5a3000")       # and right
    # shift fork arrow
    fork_x = colA if engaged != 2 else colB
    ax.annotate("", xy=(fork_x, 2.4), xytext=(fork_x, 3.2),
                arrowprops=dict(arrowstyle="-|>", color="#333", lw=2))
    ax.text(fork_x, 3.35, "shift fork", ha="center", fontsize=7)
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
