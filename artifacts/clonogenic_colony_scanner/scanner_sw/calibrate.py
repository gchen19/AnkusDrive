#!/usr/bin/env python3
"""One-time geometric calibration of the bottom camera from a scan of an EMPTY 6-well plate
(lid on, no writing): finds the six well-wall rings, fits px_per_mm, rotation and the plate
centre, and writes them back into config.yaml (camera: section).

  python3 calibrate.py /home/pi/scans/_calibration/empty_plate

Then capture the flat with the same empty plate:  python3 capture.py --flat
and the dark with the slot door shut and LEDs off:  python3 capture.py --dark
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import numpy as np, yaml
from scipy import ndimage as ndi
from quantify import load_green, od_image, refine_centre, HERE
from runlog import run_log

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("folder"); a = ap.parse_args()
    cfg = yaml.safe_load(open(HERE / "config.yaml"))
    G, C = cfg["geometry"], cfg["camera"]
    with run_log("calibrate", cfg["paths"]["runs_dir"], config=C, inputs=[a.folder]) as rec:
        od = od_image(load_green(Path(a.folder)), None, None)
        # start from the nominal frame, refine each well ring, then solve a similarity transform
        C["refine_search_mm"] = 6.0
        found, nominal = [], []
        for w in G["well_names"]:
            x_mm, y_mm = G["well_centres_mm"][w]
            cx, cy = C["plate_centre_px"][0] + x_mm * C["px_per_mm"], C["plate_centre_px"][1] - y_mm * C["px_per_mm"]
            found.append(refine_centre(od, cx, cy)); nominal.append((x_mm, -y_mm))
        P, Q = np.array(nominal, float), np.array(found, float)   # mm (y flipped) -> px
        pc, qc = P.mean(0), Q.mean(0)
        H = (P - pc).T @ (Q - qc); U, S, Vt = np.linalg.svd(H)
        R = Vt.T @ U.T
        scale = S.sum() / ((P - pc) ** 2).sum()
        rot = float(np.degrees(np.arctan2(R[1, 0], R[0, 0])))
        centre = qc - scale * (R @ pc)
        resid = np.linalg.norm(Q - (scale * (P @ R.T) + centre), axis=1) / scale
        C.update({"px_per_mm": round(float(scale), 3), "rotation_deg": round(-rot, 3),
                  "plate_centre_px": [round(float(centre[0]), 1), round(float(centre[1]), 1)]})
        cfg["camera"] = C
        (HERE / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        rec["metrics"] = {"px_per_mm": C["px_per_mm"], "rotation_deg": C["rotation_deg"],
                          "residual_mm_max": float(resid.max())}
        rec["outputs"].append(str(HERE / "config.yaml"))
        print(json.dumps(rec["metrics"], indent=1))
        if resid.max() > 0.5:
            print("WARNING: residual > 0.5 mm — check well_id_mm / that the plate was seated against the rear wall")

if __name__ == "__main__":
    main()
