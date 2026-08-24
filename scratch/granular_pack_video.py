"""Poured-pile DEM animation — the granular (issue #92) analog of
scratch/elastica_large_deflection_video.py. Turn the REAL YADE discrete-element
settle, which the gated test reduces to a packing-fraction scalar, into a GIF a
reviewer can watch: a cloud of ~800 monodisperse spheres raining into a box and
gravity packing them, frame by frame, to the random-close-packing limit.

Throughline (inherited from §11.10 / the modal & elastica videos): the frame
shows the **real artifact** — every dot is a real YADE rigid sphere at its actual
solved centre, never a sketch — and where the video backs a *claim* the oracle
rides on the frame. Here the claim is "a poured monodisperse pile lands at random
close packing, not a crystal": the green band is the granular.packing_fraction RCP
window (0.60–0.66) and the dashed line above is the crystalline FCC/HCP 0.7405 a
random pour must NOT reach. The per-frame readout shows the live solid-volume
fraction φ climbing as the cloud densifies and the mean coordination number rising
to the ≈6 isostatic signature of RCP — the two banded gates the test asserts.

The whole settle is ONE real DEM solve (YADE, GPL-3.0, run out-of-process via
ankusdrive/dem_gpl_runner.py — this script never imports YADE); the animation is its
position snapshots, not an interpolation.

Outputs (artifacts/): granular_pack_settling.gif + granular_pack_settling_filmstrip.png

  .venv/bin/python3 scratch/granular_pack_video.py
"""
import io
import json
import math
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scratch"))

import numpy as np                                          # noqa: E402
import matplotlib                                           # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                             # noqa: E402
from PIL import Image                                       # noqa: E402

from ankusdrive.analysis import granular as g                 # noqa: E402
import sim_video as sv                                      # noqa: E402

ART = REPO / "artifacts"
RUNNER = REPO / "ankusdrive" / "dem_gpl_runner.py"

# pour config (mm shown to the viewer; the solve is in SI metres)
N_SPHERES = 800
RADIUS_M = 0.004
BOX_M = (0.06, 0.06)
N_SNAPSHOTS = 14


def _yade_exec():
    for env in ("ANKUSDRIVE_YADE", "ANKUSDRIVE_YADE_PATH"):
        if (v := os.environ.get(env)) and Path(v).is_file():
            return v
    cand = Path(os.path.expanduser("~/opt/yade/bin/yade"))
    if cand.is_file():
        return str(cand)
    from ankusdrive import solvers
    return solvers.find_solver("yade").get("path")


def _run_pack(exe):
    """Drive the REAL YADE settle out-of-process and parse the sentinel JSON."""
    problem = {"problem": "pack", "n_spheres": N_SPHERES, "radius_m": RADIUS_M,
               "box_m": list(BOX_M), "friction_deg": 0.0, "steps": 50000,
               "n_snapshots": N_SNAPSHOTS}
    proc = subprocess.run([exe, "-x", "-n", str(RUNNER)],
                          input=json.dumps(problem), capture_output=True,
                          text=True, timeout=900)
    _, _, rest = proc.stdout.partition("@@JSON@@")
    body, _, _ = rest.partition("@@END@@")
    if not body:
        raise RuntimeError(f"runner produced no JSON (rc={proc.returncode}); "
                           f"stderr: {proc.stderr[-400:]}")
    return json.loads(body)


def _phi_of(spheres, lx, ly):
    """Instantaneous solid fraction of a snapshot: Σ sphere volume over
    footprint × the 95th-percentile bed top (same measure the runner reports)."""
    if not spheres:
        return 0.0, 0.0
    tops = sorted(z + r for (_x, _y, z, r) in spheres)
    top = tops[int(0.95 * (len(tops) - 1))]
    vol = sum(4.0 / 3.0 * math.pi * r ** 3 for (_x, _y, _z, r) in spheres)
    bed = lx * ly * top
    return (vol / bed if bed > 0 else 0.0), top


def _frame(spheres, phi, top, lx, ly, frac, orc, z_final):
    """Render one settle snapshot: real sphere centres as a 3-D scatter coloured by
    height, with the φ progress bar against the RCP band + crystalline ceiling."""
    fig = plt.figure(figsize=(7.2, 4.2), dpi=100)
    ax = fig.add_subplot(1, 2, 1, projection="3d")
    if spheres:
        arr = np.array(spheres)
        x, y, zz = arr[:, 0], arr[:, 1], arr[:, 2]
        ax.scatter(x * 1000, y * 1000, zz * 1000, c=zz * 1000, cmap="viridis",
                   s=22, depthshade=True, edgecolors="none")
    ax.set_xlim(-lx / 2 * 1000, lx / 2 * 1000)
    ax.set_ylim(-ly / 2 * 1000, ly / 2 * 1000)
    ax.set_zlim(0, max(top * 1000 * 1.05, 60))
    ax.set_xlabel("x (mm)"); ax.set_ylabel("y (mm)"); ax.set_zlabel("z (mm)")
    ax.set_title(f"YADE settle — {len(spheres)} spheres", fontsize=10)
    ax.view_init(elev=18, azim=-60)

    # φ progress panel: live fraction vs the RCP band and the crystal ceiling
    bx = fig.add_subplot(1, 2, 2)
    lo, hi = orc["band"]
    bx.axhspan(lo, hi, color="tab:green", alpha=0.25, label=f"RCP band {lo:.2f}-{hi:.2f}")
    bx.axhline(g.PHI_FCC_HCP, color="tab:red", ls="--", lw=1.2,
               label=f"FCC/HCP {g.PHI_FCC_HCP:.3f} (forbidden)")
    bx.axhline(g.PHI_RLP, color="tab:gray", ls=":", lw=1.0,
               label=f"RLP {g.PHI_RLP:.3f}")
    bx.bar([0], [phi], width=0.5, color="tab:blue", alpha=0.8)
    bx.text(0, phi + 0.01, f"φ={phi:.3f}", ha="center", fontsize=11, fontweight="bold")
    bx.set_xlim(-0.6, 0.6); bx.set_ylim(0, 0.80)
    bx.set_xticks([]); bx.set_ylabel("solid volume fraction φ")
    in_band = lo <= phi <= hi
    gate = "in RCP band" if in_band else ("settling…" if phi < lo else "CHECK")
    bx.set_title(f"settle {frac * 100:3.0f}%   φ={phi:.3f}  [{gate}]\n"
                 f"final coordination ≈ {z_final:.1f} (isostatic ~6)", fontsize=9)
    bx.legend(loc="upper right", fontsize=6.5, framealpha=0.9)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def main():
    ART.mkdir(parents=True, exist_ok=True)
    exe = _yade_exec()
    if not exe:
        print("  no `yade` executable (set ANKUSDRIVE_YADE) — cannot render the REAL "
              "pile; skipping (this artifact requires the GPL DEM solver).")
        return 2

    print(f"  running REAL YADE settle ({N_SPHERES} frictionless spheres, "
          f"{BOX_M[0] * 1000:.0f}×{BOX_M[1] * 1000:.0f} mm box)…")
    res = _run_pack(exe)
    if not res.get("ok"):
        print(f"  YADE solve failed: {res.get('error')}")
        return 1
    snaps = res.get("snapshots") or []
    lx, ly = res["footprint_m"]
    phi_final = res["packing_fraction"]
    z_final = res["mean_coordination"]
    orc = g.packing_fraction("random_close")
    print(f"  settled {res['n_settled']} spheres → φ={phi_final:.3f} "
          f"(RCP band {orc['band']}), coordination {z_final:.2f}, "
          f"{len(snaps)} snapshots")

    images = []
    for i, snap in enumerate(snaps):
        spheres = snap["spheres"]
        phi, top = _phi_of(spheres, lx, ly)
        frac = (i + 1) / len(snaps)
        images.append(_frame(spheres, phi, top, lx, ly, frac, orc, z_final))

    gif = sv.encode_gif(images, ART / "granular_pack_settling", fps=4, hold_last=6)
    picks = [0, len(images) // 3, 2 * len(images) // 3, len(images) - 1]
    strip = sv.filmstrip(images, ART / "granular_pack_settling_filmstrip.png", picks=picks)
    print(f"  gif       -> {gif}  ({gif.stat().st_size:,} bytes)")
    print(f"  filmstrip -> {strip}  ({strip.stat().st_size:,} bytes)")

    lo, hi = 0.55, 0.68          # honest finite-box RCP band (matches the gated test)
    ok = (lo <= phi_final <= hi) and (5.0 <= z_final <= 7.0) and (phi_final < g.PHI_FCC_HCP)
    print(f"\n  RESULT: {'PASS' if ok else 'CHECK'} — poured monodisperse pile settled "
          f"to φ={phi_final:.3f} (RCP band {orc['band']}, finite-box 0.55–0.68), "
          f"coordination {z_final:.2f} ≈ 6 (isostatic), below crystalline "
          f"{g.PHI_FCC_HCP:.3f}.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
