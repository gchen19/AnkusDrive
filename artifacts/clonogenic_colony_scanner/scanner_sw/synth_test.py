#!/usr/bin/env python3
"""Synthetic end-to-end test of quantify.py without hardware: a vignetted, marker-shadowed,
mis-seated 6-well plate with known colonies. Prints truth vs found and writes
synthetic_overlay.png next to this package's parent README.
  python3 synth_test.py
"""
import sys, json, tempfile, shutil
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import quantify as q

def main(seed=0):
    cfg = q.CFG; ppm = cfg["camera"]["px_per_mm"]
    H, W = cfg["camera"]["raw_size"][1], cfg["camera"]["raw_size"][0]
    yy, xx = np.mgrid[:H, :W]
    E = 3000.0 * (1 - 0.15 * ((xx - W / 2) / (W / 2)) ** 2)            # vignetting
    E *= 1 - 0.12 * np.exp(-((yy - H / 2) ** 2) / (2 * (15 * ppm) ** 2))  # chisel-marker shadow, 12 %
    T = np.ones((H, W), np.float32)
    rng = np.random.default_rng(seed); truth, truth_area, true_centre = {}, {}, {}
    for w, (x_mm, y_mm) in cfg["geometry"]["well_centres_mm"].items():
        cx, cy = q.mm_to_px(x_mm, y_mm)
        cx += rng.uniform(-1.5, 1.5) * ppm; cy += rng.uniform(-1.5, 1.5) * ppm   # plate not perfectly seated
        true_centre[w] = (cx, cy)
        rr = np.hypot(xx - cx, yy - cy); R = cfg["geometry"]["well_id_mm"] / 2 * ppm
        T[(rr >= R) & (rr < R + 2.4 * ppm)] *= 0.5                # well-wall shadow: starts AT the well ID, 2.4 mm out
        n = int(rng.integers(5, 40)); truth[w] = 0; ta = 0.0
        I = cfg.get("instrument", {}); k = cfg["geometry"]["well_depth_mm"] / I["camera_height_mm"] if I.get("camera_height_mm") else 0.0
        ax, ay = q.mm_to_px(*I.get("optical_axis_mm", [0.0, 0.0]))
        sx, sy = cx + (ax - cx) * k, cy + (ay - cy) * k          # the parallax-shifted mouth disc
        Ra = cfg["geometry"]["analysis_radius_mm"] * ppm
        for _ in range(n):
            r = rng.uniform(3, 13.5) * ppm; th = rng.uniform(0, 2 * np.pi); d = rng.uniform(0.4, 1.2)
            px, py = cx + r * np.cos(th), cy + r * np.sin(th)
            T[np.hypot(xx - px, yy - py) < d * ppm / 2] *= 10 ** (-0.3)
            if np.hypot(px - sx, py - sy) <= Ra - d * ppm / 2:      # truth = colonies fully inside the usable floor
                truth[w] += 1; ta += np.pi * (d / 2) ** 2
        truth_area[w] = round(ta, 2)
    I = E * T; bayer = np.zeros((H, W), np.float32)
    bayer[0::2, 1::2] = I[0::2, 1::2]; bayer[1::2, 0::2] = I[1::2, 0::2]              # green sites, BGGR
    bayer[0::2, 0::2] = I[0::2, 0::2] * 0.6; bayer[1::2, 1::2] = I[1::2, 1::2] * 0.6
    bayer = (bayer + rng.normal(0, 8, bayer.shape)).clip(0, 4095).astype(np.uint16)
    d = Path(tempfile.mkdtemp()); np.save(d / "bottom_raw_stack.npy", bayer[None])
    cfg["paths"]["runs_dir"] = str(d / "runs"); cfg["paths"]["calib_dir"] = str(d / "nocal")
    sys.argv = ["quantify.py", str(d)]; q.main()
    wells = json.loads((d / "summary.json").read_text())["wells"]
    found = {w["well"]: w["n_colonies"] for w in wells}
    print("centre error (mm):", {w["well"]: round(float(np.hypot(*(np.array(w["centre_px"]) - true_centre[w["well"]])) / ppm), 2) for w in wells})
    print("count truth:", truth); print("count found:", found)
    print("area  truth:", truth_area); print("area  found:", {w["well"]: round(w["total_area_mm2"], 2) for w in wells})
    shutil.copy(d / "overlay.png", Path(__file__).resolve().parent.parent / "synthetic_overlay.png")
    return truth, found

if __name__ == "__main__":
    main()
