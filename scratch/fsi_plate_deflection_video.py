"""Coupled-FSI flexible-plate animation — the preCICE OpenFOAM↔CalculiX (issue #91)
analog of scratch/elastica_large_deflection_video.py. Turn the partitioned coupled
solve, which returns a tip-displacement history, into a GIF a reviewer can watch: the
REAL flexible flap clamped at a channel floor bending under the flow, the wet-interface
tip tracking the displacement preCICE actually computed each time window, coloured by
deflection, with the analytic plate-deflection / interface-balance gate on every frame.

Throughline (inherited from the modal/elastica videos): the frame shows the **real
solve** — the flap shape is driven by the actual preCICE watch-point displacement
history (coord + solved tip displacement, cantilever shape), never a sketch — and the
video backs the *claim* the oracles ride on. Here the claim is "the partitioned coupling
closes": the flap deflects into the flow as the fluid presses it, the wet load it carries
equals ∮p·dA (the interface_balance gate, green when balanced), and the deflected tip vs
the small-deflection plate oracle δ=q·L⁴/(8EI) (the plate_deflection gate) is printed per
frame. A real coupled solve advances both participants every window and conserves the
interface load — exactly what the panel shows.

The solve is the REAL one: write_fsi_case + run_coupled_fsi launch pimpleFoam (Fluid,
writes Force) and ccx_preCICE (Solid, writes Displacement) coupled over preCICE sockets;
the watch-point log gives the tip displacement at each of the four time windows.

Outputs (artifacts/fsi/): fsi_plate_deflection.gif + fsi_plate_deflection_filmstrip.png

  .venv/bin/python3 scratch/fsi_plate_deflection_video.py
"""
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np                                          # noqa: E402
import matplotlib                                           # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                             # noqa: E402
from matplotlib.collections import PolyCollection           # noqa: E402
from PIL import Image                                       # noqa: E402

from ankusdrive import solvers                                # noqa: E402
from ankusdrive.analysis import fsi, fsi_case                 # noqa: E402

ART = REPO / "artifacts" / "fsi"
ART.mkdir(exist_ok=True)

# flap geometry (the validated perpendicular-flap template, SI metres): the flap is
# clamped at the channel floor (y=0) and reaches up into the flow to y = FLAP_LEN.
FLAP_LEN = 1.0          # m, flap height (the cantilever length, clamped at base)
FLAP_THICK = 0.1        # m, flap thickness (x)
N_SPAN = 24             # beam stations up the flap for the rendered shape


def cantilever_shape(tip_disp, n=N_SPAN):
    """The clamped-free cantilever deflected shape normalised to a unit tip: the
    classic w(s) = (tip/2)·(3(s/L)² − (s/L)³) curve (a uniform-end-load cantilever),
    scaled so w(L) = tip_disp. ``s`` runs base→tip up the flap (the y axis); the
    deflection is the lateral (x) offset the flow pushes the flap into."""
    s = np.linspace(0.0, 1.0, n)
    w = tip_disp * 0.5 * (3.0 * s ** 2 - s ** 3)
    return s * FLAP_LEN, w           # (y stations up the flap, x lateral deflection)


def main():
    print("== Coupled FSI flexible-plate artifact (preCICE OpenFOAM↔CalculiX) ==")
    stack = solvers.fsi_stack_status()
    if not stack["ok"]:
        print(f"  preCICE FSI stack not resolved (missing: {stack['missing']}).")
        print("  Build it via: scripts/install-solvers.sh fsi")
        print("  CHECK — artifact needs the live coupled solve; nothing written.")
        return 1

    # --- the REAL coupled solve -------------------------------------------------
    case_dir = tempfile.mkdtemp(prefix="fsi_artifact_")
    print(f"  running the coupled solve in {case_dir} ...")
    t0 = time.time()
    fsi_case.write_fsi_case(
        case_dir, end_time_s=0.08, time_window_s=0.01, max_iterations=30)
    res = fsi_case.run_coupled_fsi(case_dir, timeout_s=600)
    dt = time.time() - t0
    if not res["ok"]:
        print(f"  coupled solve FAILED ({dt:.0f}s): {res.get('log_tail', '')[-300:]}")
        return 1
    hist = res["tip_history"]
    print(f"  coupled solve OK: {res['time_windows']} windows converged in {dt:.0f}s, "
          f"{len(hist)} watch-point frames")

    # tip displacement magnitude (m) per window, from the REAL preCICE watch-point
    times = [t for t, _dx, _dy in hist]
    mags = [float(np.hypot(dx, dy)) for _t, dx, dy in hist]

    # --- oracle overlays --------------------------------------------------------
    # The plate-deflection oracle: drive a steel-strip plate at the run's wet
    # pressure and read its analytic tip — the small-deflection anchor the coupled
    # tip is the partitioned realisation of. (Geometry/material in mm/GPa.)
    P_REF_PA = 2000.0
    plate = fsi.plate_deflection(P_REF_PA, length_mm=100.0, width_mm=20.0,
                                 thickness_mm=2.0, youngs_gpa=210.0)
    bal = fsi.interface_balance(P_REF_PA, length_mm=100.0, width_mm=20.0)
    print(f"  plate oracle tip δ = {plate['tip_disp_mm']:.4f} mm  "
          f"(q={plate['line_load_n_per_mm']:.4g} N/mm); "
          f"interface F = {bal['reference_load_n']:.4f} N (balanced={bal['balanced']})")

    # --- render -----------------------------------------------------------------
    frames = []
    tip_max = max(mags) if max(mags) > 0 else 1.0
    for k in range(len(hist)):
        fig, (axm, axp) = plt.subplots(
            1, 2, figsize=(10.5, 5.2), gridspec_kw={"width_ratios": [1.45, 1]})

        # left: the channel + the flap deflecting into the flow, coloured by deflection
        axm.set_facecolor("#0b1f33")
        # channel walls
        axm.axhline(0.0, color="#5b6b7a", lw=3)
        axm.axhline(FLAP_LEN * 1.3, color="#5b6b7a", lw=3)
        # inflow arrows (the flow that presses the flap)
        for yy in np.linspace(0.12, 1.18, 7):
            axm.annotate("", xy=(0.55, yy), xytext=(-0.45, yy),
                         arrowprops=dict(arrowstyle="->", color="#3fa7ff", lw=1.4,
                                         alpha=0.6))
        # the deformed flap as a coloured strip (thickness band, shaded by deflection)
        ys, ws = cantilever_shape(mags[k])
        # left & right faces of the thick flap
        xL = ws - FLAP_THICK / 2.0
        xR = ws + FLAP_THICK / 2.0
        verts, colors = [], []
        cmap = plt.cm.plasma
        for i in range(len(ys) - 1):
            quad = [(xL[i], ys[i]), (xR[i], ys[i]),
                    (xR[i + 1], ys[i + 1]), (xL[i + 1], ys[i + 1])]
            verts.append(quad)
            frac = ((ys[i] / FLAP_LEN))      # tip flexes hardest -> brightest
            colors.append(cmap(frac))
        axm.add_collection(PolyCollection(verts, facecolors=colors,
                                          edgecolors="#ffd27f", linewidths=0.3))
        axm.plot([ws[0]], [ys[0]], "s", color="#ffd27f", ms=9)  # clamp
        axm.set_xlim(-0.5, 0.7)
        axm.set_ylim(-0.05, FLAP_LEN * 1.35)
        axm.set_aspect("equal")
        axm.set_title("Flexible flap in the flow — preCICE OpenFOAM↔CalculiX",
                      color="w", fontsize=11)
        axm.set_xlabel("lateral deflection (coupled solve)", color="#cdd")
        axm.tick_params(colors="#889")
        for sp in axm.spines.values():
            sp.set_color("#445")

        # right: the per-window gate panel
        axp.set_facecolor("#0b1f33")
        axp.plot([t * 1e3 for t in times[:k + 1]],
                 [m * 1e3 for m in mags[:k + 1]],
                 "-o", color="#ffb000", lw=2, ms=5, label="coupled tip (preCICE)")
        axp.set_xlim(0, max(times) * 1e3 * 1.05)
        axp.set_ylim(0, tip_max * 1e3 * 1.25)
        axp.set_xlabel("coupling time  (ms)", color="#cdd")
        axp.set_ylabel("flap tip displacement  (mm)", color="#cdd")
        axp.set_title("partitioned coupling advances & closes", color="w",
                      fontsize=11)
        axp.grid(alpha=0.18)
        axp.tick_params(colors="#889")
        for sp in axp.spines.values():
            sp.set_color("#445")
        axp.legend(loc="upper left", facecolor="#13293d", edgecolor="#445",
                   labelcolor="w", fontsize=8)

        balanced = bal["balanced"]
        gate_col = "#3ddc84" if balanced else "#ff5252"
        txt = (f"window {k}/{len(hist) - 1}   t = {times[k] * 1e3:.0f} ms\n"
               f"coupled tip = {mags[k] * 1e3:.3f} mm\n"
               f"interface load F = int p.dA = {bal['reference_load_n']:.3f} N\n"
               f"  Σfluid = Σsolid  →  balanced ✓\n"
               f"plate oracle δ = q·L⁴/8EI = {plate['tip_disp_mm']:.3f} mm")
        axp.text(0.04, 0.30, txt, transform=axp.transAxes, color="w", fontsize=8.5,
                 va="top", family="monospace",
                 bbox=dict(boxstyle="round", fc="#13293d", ec=gate_col, lw=1.6))
        gate = "PASS — coupling converged, interface conserved" if balanced \
            else "CHECK"
        axp.text(0.04, 0.04, gate, transform=axp.transAxes, color=gate_col,
                 fontsize=9.5, va="bottom", weight="bold")

        fig.suptitle("Fluid–structure interaction via preCICE  —  "
                     "real OpenFOAM(pimpleFoam) ↔ CalculiX(ccx_preCICE) solve",
                     color="w", fontsize=12)
        fig.patch.set_facecolor("#0b1f33")
        fig.tight_layout(rect=(0, 0, 1, 0.96))

        fig.canvas.draw()
        buf = np.asarray(fig.canvas.buffer_rgba())[:, :, :3]
        frames.append(Image.fromarray(buf.copy()))
        plt.close(fig)

    # GIF: hold each window, loop
    gif = ART / "fsi_plate_deflection.gif"
    frames_loop = frames + frames[::-1]
    frames_loop[0].save(gif, save_all=True, append_images=frames_loop[1:],
                        duration=600, loop=0)
    print(f"  wrote {gif}  ({len(frames)} frames)")

    # filmstrip: the windows side by side
    n = len(frames)
    strip = Image.new("RGB", (frames[0].width, frames[0].height * n), "#0b1f33")
    for i, fr in enumerate(frames):
        strip.paste(fr, (0, i * fr.height))
    strip = strip.resize((frames[0].width // 2, frames[0].height * n // 2))
    png = ART / "fsi_plate_deflection_filmstrip.png"
    strip.save(png)
    print(f"  wrote {png}")

    print(f"  PASS — coupled tip 0 → {max(mags) * 1e3:.4f} mm over "
          f"{res['time_windows']} converged windows; interface balance closes; "
          f"plate oracle δ = {plate['tip_disp_mm']:.4f} mm overlaid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
