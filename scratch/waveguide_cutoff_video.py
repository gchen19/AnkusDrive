"""Rectangular-waveguide cutoff animation — the full-wave-EM (issue #93 b) analog of
scratch/elastica_large_deflection_video.py. Turn the openEMS FDTD waveguide solve,
which today returns a transmission sweep, into a GIF a reviewer can watch: a REAL
WR-90 guide driven over a band that STRADDLES its TE10 cutoff, the solved transmission
sweeping out frequency by frequency and crossing from the evanescent (cut-off, dark)
band into the propagating (passing, bright) band exactly at the analytic
f_c = c/(2a) line.

Throughline (inherited from the nonlinear/modal videos): the frame shows the **real
artifact** — the actual openEMS-solved S21(f) over a meshed guide, never a sketch — and
where the video backs a *claim* the oracle rides on the frame. Here the claim is "the
guide cuts off at exactly c/2a": the dashed vertical line is the closed-form
`waveguide_cutoff` f_c (EXACT), and the per-frame readout is the FDTD half-power
crossing vs that oracle (`fc_ratio` ≈ 1). Below f_c the swept marker sits on the floor
(nothing transmits, β imaginary); the instant it passes f_c the transmission leaps to
the plateau — that step AT the analytic line IS the cutoff.

The companion panel draws the TE10 broad-wall field pattern E_y(x) ∝ sin(πx/a) — the
mode the FDTD actually carries above cutoff — fading to dark below it, so the cross
section visibly "lights up" as the sweep crosses f_c.

openEMS is GPL-3.0, so the solve runs OUT-OF-PROCESS via
driftpin/em_fullwave_gpl_runner.py under a dedicated openEMS venv; this script (shared
.venv) only renders the result. Resolve that venv with DRIFTPIN_OPENEMS_PYTHON, or it
is auto-discovered beside the repo (.venv-openems).

Outputs (artifacts/): waveguide_cutoff.gif + waveguide_cutoff_filmstrip.png

  DRIFTPIN_OPENEMS_PYTHON=/path/.venv-openems/bin/python \
      .venv/bin/python3 scratch/waveguide_cutoff_video.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np                                          # noqa: E402
import matplotlib                                           # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                             # noqa: E402
from PIL import Image                                       # noqa: E402

from driftpin.analysis import em_fullwave as ew             # noqa: E402

ART = REPO / "artifacts"
RUNNER = str(REPO / "driftpin" / "em_fullwave_gpl_runner.py")
A_MM, B_MM, LENGTH_MM = 22.86, 10.16, 60.0                  # WR-90-like X-band guide
F_START, F_STOP, N_FREQ = 4.0, 10.0, 121                    # GHz, straddling f_c≈6.56


def _openems_python():
    """Resolve the dedicated openEMS venv interpreter (mirrors the worker/test):
    DRIFTPIN_OPENEMS_PYTHON → .venv-openems beside the repo or one up → PATH."""
    cands = []
    if env := os.environ.get("DRIFTPIN_OPENEMS_PYTHON"):
        cands.append(env)
    for base in (REPO, REPO.parent, Path.home()):
        cands += [str(base / ".venv-openems" / "bin" / "python3"),
                  str(base / ".venv-openems" / "bin" / "python")]
    if w := shutil.which("python3"):
        cands.append(w)
    probe = ("import importlib.util,sys;"
             "sys.exit(0 if importlib.util.find_spec('openEMS') else 1)")
    seen = set()
    for c in cands:
        if not c or c in seen or not os.path.isfile(c):
            continue
        seen.add(c)
        try:
            r = subprocess.run([c, "-c", probe], capture_output=True, timeout=30)
        except Exception:
            continue
        if r.returncode == 0:
            return c
    return None


def _solve(py):
    """Run the REAL openEMS FDTD waveguide sweep out-of-process; return its result."""
    problem = {"problem": "waveguide_sweep", "a_mm": A_MM, "b_mm": B_MM,
               "length_mm": LENGTH_MM, "f_start_ghz": F_START, "f_stop_ghz": F_STOP,
               "n_freq": N_FREQ, "nrts": 30000}
    proc = subprocess.run([py, RUNNER], input=json.dumps(problem),
                          capture_output=True, text=True, timeout=600)
    body = proc.stdout.partition("@@JSON@@")[2].partition("@@END@@")[0]
    if not body:
        raise SystemExit(f"openEMS runner produced no JSON (rc={proc.returncode}); "
                         f"stderr:\n{proc.stderr[-800:]}")
    res = json.loads(body)
    if not res.get("ok"):
        raise SystemExit(f"openEMS solve failed: {res.get('error')}\n{res.get('trace','')}")
    return res


def _frame(freq, normt, db, fc, fc_cross, ratio, k, span):
    """One filmstrip frame: the swept S21 curve (left) + the TE10 cross-section field
    pattern lighting up above cutoff (right). `k` = sweep index, `span` = total."""
    f_now = freq[k]
    propagating = f_now > fc
    fig = plt.figure(figsize=(10.4, 4.2))
    gs = fig.add_gridspec(1, 3, width_ratios=[2.2, 0.05, 1.0], wspace=0.28)

    # --- left: the real FDTD transmission sweep, revealed up to f_now -------------
    axL = fig.add_subplot(gs[0, 0])
    axL.plot(freq[:k + 1], db[:k + 1], color="#1f77b4", lw=2.2, zorder=3)
    axL.plot(freq, db, color="#1f77b4", lw=0.8, alpha=0.18, zorder=1)   # ghost full curve
    axL.axvline(fc, color="#d62728", ls="--", lw=1.6, zorder=2,
                label=f"oracle f_c = c/2a = {fc:.3f} GHz")
    if fc_cross:
        axL.axvline(fc_cross, color="#2ca02c", ls=":", lw=1.6, zorder=2,
                    label=f"FDTD half-power = {fc_cross:.3f} GHz")
    axL.scatter([f_now], [db[k]], s=70, zorder=5,
                color=("#2ca02c" if propagating else "#444444"),
                edgecolor="k", linewidth=0.8)
    axL.axvspan(F_START, fc, color="#000000", alpha=0.05, zorder=0)
    axL.text(0.5 * (F_START + fc), -57, "evanescent\n(cut off)", ha="center",
             va="bottom", fontsize=8, color="#555555")
    axL.text(0.5 * (fc + F_STOP), -57, "propagating", ha="center", va="bottom",
             fontsize=8, color="#2ca02c")
    axL.set_xlim(F_START, F_STOP)
    axL.set_ylim(-60, 3)
    axL.set_xlabel("frequency (GHz)")
    axL.set_ylabel("transmission $|S_{21}|$ (dB)")
    axL.set_title(f"openEMS FDTD — WR-90 guide (a={A_MM} mm), real solve "
                  f"({span} freqs)", fontsize=9.5)
    axL.legend(loc="lower right", fontsize=7.5, framealpha=0.9)
    axL.grid(alpha=0.25)
    tag = ("PROPAGATING  ratio FDTD/oracle = %.4f" % ratio if (propagating and ratio)
           else "EVANESCENT  (β imaginary, no transmission)")
    axL.text(0.015, 0.96, f"f = {f_now:.3f} GHz   {tag}", transform=axL.transAxes,
             va="top", ha="left", fontsize=8.5, family="monospace",
             bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.9))

    # --- right: TE10 broad-wall field pattern, brightness ∝ |normalized trans| ----
    axR = fig.add_subplot(gs[0, 2])
    nx, ny = 80, 36
    xs = np.linspace(0, A_MM, nx)
    Ey = np.sin(np.pi * xs / A_MM)[None, :] * np.ones((ny, 1))   # TE10 E_y(x)
    field = Ey * float(normt[k])                                 # fade by solved transmission
    axR.imshow(field, extent=[0, A_MM, 0, B_MM], origin="lower",
               cmap="inferno", vmin=0, vmax=1, aspect="auto")
    axR.set_xlabel("x — broad wall (mm)")
    axR.set_ylabel("y (mm)")
    axR.set_title("TE10 $E_y \\propto \\sin(\\pi x/a)$\n(brightness = solved trans.)",
                  fontsize=8.5)

    fig.suptitle("Waveguide cutoff: the FDTD field lights up exactly at the analytic "
                 "c/2a", fontsize=11, y=0.995)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=88, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def main():
    py = _openems_python()
    if py is None:
        raise SystemExit("no openEMS venv resolves — set DRIFTPIN_OPENEMS_PYTHON "
                         "(scripts/install-solvers.sh em_gpl)")
    print(f"openEMS python: {py}")
    print("running the REAL FDTD waveguide sweep out-of-process ...")
    res = _solve(py)

    freq = np.asarray(res["freq_ghz"])
    db = np.asarray(res["s21_db"])
    normt = np.asarray(res["transmission_norm"])
    fc = res["fc_analytic_ghz"]
    fc_cross = res["fc_crossing_ghz"]
    ratio = res["fc_ratio"]

    # cross-check the solve's analytic cutoff against the in-process oracle
    orc = ew.waveguide_cutoff(a_mm=A_MM, b_mm=B_MM)
    assert abs(orc["cutoff_ghz"] - fc) < 1e-3, (orc["cutoff_ghz"], fc)

    print(f"  f_c (oracle, exact c/2a) : {fc:.4f} GHz")
    print(f"  FDTD half-power crossing : {fc_cross:.4f} GHz")
    print(f"  ratio FDTD/oracle        : {ratio:.4f}")
    print(f"  evanescent mean trans.   : {res['evanescent_mean']:.4f}")
    print(f"  propagating mean trans.  : {res['propagating_mean']:.4f}")
    print(f"  cells {res['n_cells']}, wall {res['wall_s']:.2f}s")

    ART.mkdir(exist_ok=True)
    # sample ~26 sweep frames for a watchable GIF (skip the very-low-amplitude tail)
    idx = np.linspace(0, len(freq) - 1, 22).round().astype(int)
    frames = [_frame(freq, normt, db, fc, fc_cross, ratio, int(k), len(freq))
              for k in idx]
    # quantize to a shared adaptive palette — keeps the GIF lean without banding
    frames = [im.convert("P", palette=Image.ADAPTIVE, colors=128) for im in frames]

    gif = ART / "waveguide_cutoff.gif"
    durs = [130] * len(frames)
    durs[-1] = 1600                                   # hold the final propagating frame
    frames[0].save(gif, save_all=True, append_images=frames[1:], loop=0,
                   duration=durs, disposal=2, optimize=True)
    print(f"wrote {gif}  ({len(frames)} frames)")

    # static filmstrip: evanescent → at-cutoff → propagating
    picks = [idx[3], idx[len(idx) // 2], idx[-3]]
    strip = [_frame(freq, normt, db, fc, fc_cross, ratio, int(k), len(freq))
             for k in picks]
    w = max(im.width for im in strip)
    h = sum(im.height for im in strip)
    canvas = Image.new("RGB", (w, h), "white")
    y = 0
    for im in strip:
        canvas.paste(im, (0, y))
        y += im.height
    film = ART / "waveguide_cutoff_filmstrip.png"
    canvas.save(film)
    print(f"wrote {film}")

    gate_ok = (ratio is not None and 0.985 <= ratio <= 1.015
               and res["evanescent_mean"] < 0.05 and res["propagating_mean"] > 0.9)
    print("PASS — FDTD cutoff lands on the exact c/2a oracle" if gate_ok
          else "CHECK — FDTD cutoff transition is outside the gate")


if __name__ == "__main__":
    main()
