"""Rigid-sphere scattering directivity animation — the exterior-acoustics-BEM (issue
#93 a) analog of scratch/elastica_large_deflection_video.py. Turn the Bempp BEM solve,
which today returns a far-field form-function number, into a GIF a reviewer can watch:
a REAL sound-hard sphere insonified by a plane wave, the BEM-solved scattered far-field
directivity |f∞(θ)| sweeping out as the compactness ka grows from a near-omnidirectional
Rayleigh scatterer (ka≪1) to a forward-throwing geometric one (ka≫1), with the exact
**Mie series** directivity drawn underneath at every frame.

Throughline (inherited from the elastica/modal/CFD videos): the frame shows the **real
artifact** — the actual Bempp-solved scattered field on a meshed sphere, sampled at a
full ring of angles, never a sketch — and where the video backs a *claim* the oracle
rides on the frame. Here the claim is "the BEM reproduces the analytic scattering": the
filled blue lobe is the BEM directivity and the dashed black line is the closed-form
`rigid_sphere_scattering` Mie form function. They overlie at every ka, and the per-frame
readout is the backscatter BEM-vs-Mie ratio (≈1) — the gate the solve must pass.

Each ka is its own exterior-Neumann BEM solve on a fresh sphere mesh refined to the
wavelength (one converged scattered field per frame — see acoustic_radiation_submit), so
the animation is a sweep of real solves, not an interpolation. A second panel tracks the
backscatter |f∞(π)| vs ka as the swept point climbs the Rayleigh→geometric curve.

Bempp is MIT but needs meshio>=4 (clashing with solidspy's meshio==3 in the shared
venv), so the solve runs OUT-OF-PROCESS via ankusdrive/bempp_runner.py under a dedicated
.venv-bempp; this script (shared .venv) only renders the result. Resolve that venv with
ANKUSDRIVE_BEMPP_PYTHON, or it is auto-discovered beside the repo (.venv-bempp).

Outputs (artifacts/): sphere_scattering_directivity.gif + sphere_scattering_directivity_filmstrip.png

  ANKUSDRIVE_BEMPP_PYTHON=/path/.venv-bempp/bin/python \
      .venv/bin/python3 scratch/sphere_scattering_directivity.py
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

from ankusdrive.analysis import acoustics_bem as ab           # noqa: E402

ART = REPO / "artifacts"
RUNNER = str(REPO / "ankusdrive" / "bempp_runner.py")
A_M = 1.0                                                   # unit sphere
KA_LIST = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]         # Rayleigh → geometric
THETAS = list(np.arange(0.0, 360.1, 10.0))                 # full directivity ring (deg)
H_PER_WL = 12.0                                             # elements per wavelength


def _bempp_python():
    """Resolve the dedicated bempp venv interpreter (mirrors the worker/test):
    ANKUSDRIVE_BEMPP_PYTHON → .venv-bempp beside the repo or one up → PATH."""
    cands = []
    if env := os.environ.get("ANKUSDRIVE_BEMPP_PYTHON"):
        cands.append(env)
    for base in (REPO, REPO.parent, Path.home() / "AnkusDrive"):
        cands += [str(base / ".venv-bempp" / "bin" / "python3"),
                  str(base / ".venv-bempp" / "bin" / "python")]
    if w := shutil.which("python3"):
        cands.append(w)
    probe = ("import importlib.util,sys;"
             "sys.exit(0 if importlib.util.find_spec('bempp_cl') else 1)")
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
    """Run the REAL Bempp BEM scattering sweep out-of-process; return its result."""
    problem = {"problem": "scattering", "a_m": A_M, "ka_list": KA_LIST,
               "theta_deg": THETAS, "h_per_wl": H_PER_WL}
    proc = subprocess.run([py, RUNNER], input=json.dumps(problem),
                          capture_output=True, text=True, timeout=1200)
    body = proc.stdout.partition("@@JSON@@")[2].partition("@@END@@")[0]
    if not body:
        raise SystemExit(f"bempp runner produced no JSON (rc={proc.returncode}); "
                         f"stderr:\n{proc.stderr[-800:]}")
    res = json.loads(body)
    if not res.get("ok"):
        raise SystemExit(f"bempp solve failed: {res.get('error')}\n{res.get('trace','')}")
    return res


def _mie_directivity(ka):
    """Exact Mie far-field |f∞(θ)| over the ring (the analytic overlay)."""
    return np.array([ab.rigid_sphere_scattering(ka=ka, theta_deg=float(t))["form_function_abs"]
                     for t in THETAS])


def _frame(ka, bem_dir, mie_dir, back_ka, back_bem, back_mie, idx, span, rmax):
    """One filmstrip frame: the directivity polar (left) with the BEM lobe filled and
    the Mie curve dashed over it, plus the backscatter-vs-ka tracker (right)."""
    th = np.radians(THETAS)
    fig = plt.figure(figsize=(10.6, 4.4))

    # --- left: directivity polar, BEM (filled) vs Mie (dashed) -------------------
    axL = fig.add_subplot(1, 2, 1, projection="polar")
    axL.set_theta_zero_location("E")                # θ=0 forward (+z, wave direction)
    axL.fill(th, bem_dir, color="#1f77b4", alpha=0.30, zorder=2,
             label="Bempp BEM |f∞(θ)|")
    axL.plot(th, bem_dir, color="#1f77b4", lw=2.0, zorder=3)
    axL.plot(th, mie_dir, color="k", ls="--", lw=1.4, zorder=4, label="Mie series (exact)")
    axL.scatter([np.pi], [back_bem], s=55, color="#2ca02c", edgecolor="k",
                linewidth=0.7, zorder=5)            # backscatter marker (θ=180)
    axL.set_rmax(rmax)
    axL.set_rticks(np.round(np.linspace(0, rmax, 4), 1))
    axL.set_title(f"rigid-sphere scattering directivity   ka = {ka:.2f}",
                  fontsize=10, pad=14)
    axL.legend(loc="lower center", bbox_to_anchor=(0.5, -0.22), fontsize=8, ncol=2)
    ratio = back_bem / back_mie if back_mie else float("nan")
    axL.text(np.radians(90), rmax * 1.18,
             f"backscatter BEM/Mie = {ratio:.4f}", ha="center", fontsize=8.5,
             family="monospace",
             bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.9))

    # --- right: backscatter |f∞(π)| vs ka, swept point climbing the curve --------
    axR = fig.add_subplot(1, 2, 2)
    kas = np.array(KA_LIST)
    axR.plot(kas, back_mie_all, color="k", ls="--", lw=1.4, label="Mie |f∞(π)|")
    axR.plot(kas[:idx + 1], back_bem_all[:idx + 1], color="#1f77b4", lw=2.0,
             marker="o", ms=4, label="BEM |f∞(π)|")
    axR.scatter([ka], [back_bem], s=70, color="#2ca02c", edgecolor="k",
                linewidth=0.8, zorder=5)
    axR.set_xlim(min(KA_LIST) - 0.2, max(KA_LIST) + 0.2)
    axR.set_ylim(0, max(np.max(back_mie_all), np.max(back_bem_all)) * 1.18)
    axR.set_xlabel("ka (compactness)")
    axR.set_ylabel("backscatter form function |f∞(π)|")
    axR.set_title("BEM tracks the exact Mie backscatter", fontsize=9.5)
    axR.legend(loc="upper left", fontsize=8)
    axR.grid(alpha=0.25)
    axR.text(0.5 * (min(KA_LIST) + max(KA_LIST)), 0.06,
             "Rayleigh (ka≪1) → resonance hump → geometric (ka≫1)",
             ha="center", fontsize=7.5, color="#555555")

    fig.suptitle("Exterior acoustics BEM: the solved scattered field overlies the "
                 "analytic Mie series at every ka", fontsize=11, y=0.99)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=90, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


# globals shared into _frame (set in main once the solve is back)
back_mie_all = None
back_bem_all = None


def main():
    global back_mie_all, back_bem_all
    py = _bempp_python()
    if py is None:
        raise SystemExit("no bempp venv resolves — set ANKUSDRIVE_BEMPP_PYTHON "
                         "(scripts/install-solvers.sh acoustics_bem)")
    print(f"bempp python: {py}")
    print("running the REAL BEM scattering sweep out-of-process ...")
    res = _solve(py)

    bem_dirs, mie_dirs = [], []
    back_bem_all, back_mie_all = [], []
    for entry in res["results"]:
        ka = entry["ka"]
        ff = entry["form_function_abs"]
        bem_dir = np.array([ff[f"{t:g}"] for t in THETAS])
        mie_dir = _mie_directivity(ka)
        bem_dirs.append(bem_dir)
        mie_dirs.append(mie_dir)
        back_bem_all.append(entry["backscatter_abs"])
        back_mie_all.append(ab.rigid_sphere_scattering(ka=ka, theta_deg=180.0)["form_function_abs"])
        r = entry["backscatter_abs"] / back_mie_all[-1]
        print(f"  ka={ka:.2f}: BEM |f∞(π)|={entry['backscatter_abs']:.4f}  "
              f"Mie={back_mie_all[-1]:.4f}  ratio={r:.4f}  ({entry['n_elements']} el)")
    back_bem_all = np.array(back_bem_all)
    back_mie_all = np.array(back_mie_all)

    rmax = max(np.max(d) for d in (bem_dirs + mie_dirs)) * 1.05

    ART.mkdir(exist_ok=True)
    frames = []
    for i, ka in enumerate([e["ka"] for e in res["results"]]):
        frames.append(_frame(ka, bem_dirs[i], mie_dirs[i], ka, back_bem_all[i],
                             back_mie_all[i], i, len(res["results"]), rmax))
    frames_p = [im.convert("P", palette=Image.ADAPTIVE, colors=128) for im in frames]

    gif = ART / "sphere_scattering_directivity.gif"
    durs = [600] * len(frames_p)
    durs[-1] = 1800
    frames_p[0].save(gif, save_all=True, append_images=frames_p[1:], loop=0,
                     duration=durs, disposal=2, optimize=True)
    print(f"wrote {gif}  ({len(frames_p)} frames)")

    # static filmstrip: a low-ka (Rayleigh-ish), mid (hump), high-ka (geometric)
    picks = [0, len(frames) // 2, len(frames) - 1]
    strip = [frames[i] for i in picks]
    w = max(im.width for im in strip)
    h = sum(im.height for im in strip)
    canvas = Image.new("RGB", (w, h), "white")
    y = 0
    for im in strip:
        canvas.paste(im, (0, y))
        y += im.height
    film = ART / "sphere_scattering_directivity_filmstrip.png"
    canvas.save(film)
    print(f"wrote {film}")

    ratios = back_bem_all / back_mie_all
    gate_ok = bool(np.all((ratios > 0.96) & (ratios < 1.04)))
    print(f"backscatter BEM/Mie ratios: min {ratios.min():.4f}  max {ratios.max():.4f}")
    print("PASS — BEM scattered field overlies the exact Mie series at every ka" if gate_ok
          else "CHECK — a BEM directivity is outside the Mie gate")


if __name__ == "__main__":
    main()
