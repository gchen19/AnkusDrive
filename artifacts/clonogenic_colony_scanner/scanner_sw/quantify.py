#!/usr/bin/env python3
"""Quantify one scan folder: raw Bayer stack -> linear green -> OD map -> per-well colony table.

  python3 quantify.py /home/pi/scans/20260909_101500

Writes into the scan folder:
  od.npy            full-res OD image (float32, plate frame after rotation) — the wide artifact
  colonies.csv      one row per colony: well, x_mm, y_mm, area_mm2, eq_diam_mm, integrated_od
  wells.csv         one row per well: n_colonies, total_area_mm2, integrated_od, area_fraction
  overlay.png       OD image with well circles + colony outlines, for the eye
  summary.json      + a run-record in paths.runs_dir

Why OD: I = E(x) * T(x). Marker ink on the lid modulates E(x) smoothly (see optics_model.py);
log(I) = log E + log T so the shadow is an additive, low-frequency term in OD and a grey-opening
background estimate removes it. Integrated OD per colony is proportional to bound crystal violet.
"""
from __future__ import annotations
import argparse, csv, json, sys
from pathlib import Path
import numpy as np, yaml
from scipy import ndimage as ndi
from runlog import run_log

HERE = Path(__file__).resolve().parent
CFG = yaml.safe_load(open(HERE / "config.yaml"))

def apply_plate_preset(cfg: dict) -> dict:
    """Fill geometry.* from plate_presets[cfg['plate']] (well pitch may differ per brand: Eppendorf is 40 x 38)."""
    pre = cfg.get("plate_presets", {}).get(cfg.get("plate"))
    g = cfg["geometry"]
    if pre:
        g["well_id_mm"] = pre["well_id_mm"]; g["well_depth_mm"] = pre["well_depth_mm"]
        g["well_pitch_mm"] = pre["pitch_x_mm"]; g["pitch_x_mm"] = pre["pitch_x_mm"]; g["pitch_y_mm"] = pre["pitch_y_mm"]
        px, py = pre["pitch_x_mm"], pre["pitch_y_mm"]
        ox, oy = pre.get("grid_offset_mm", [0.0, 0.0])          # well-pattern centre vs plate centre (TPP: -1.95, 0)
        g["well_centres_mm"] = {"A1": [-px + ox, py/2 + oy], "A2": [ox, py/2 + oy], "A3": [px + ox, py/2 + oy],
                                "B1": [-px + ox, -py/2 + oy], "B2": [ox, -py/2 + oy], "B3": [px + ox, -py/2 + oy]}
    inst = cfg.get("instrument", {})
    if inst.get("variant") == "v2_flip":
        # the colony shot is of the FLIPPED plate: mirror the well map about the flip axis
        ax = inst.get("flip_axis", "long")
        for w, (x, y) in cfg["geometry"]["well_centres_mm"].items():
            cfg["geometry"]["well_centres_mm"][w] = [x, -y] if ax == "long" else [-x, y]
    return cfg
CFG = apply_plate_preset(CFG)
G, C, A = CFG["geometry"], CFG["camera"], CFG["analysis"]

# ------------------------------------------------------------- raw -> linear green
def green_full_res(bayer: np.ndarray, pattern: str) -> np.ndarray:
    """Full-resolution green plane by bilinear fill of the non-green sites (no colour maths)."""
    g = np.zeros(bayer.shape, bool)
    if pattern in ("RGGB", "BGGR"):
        g[0::2, 1::2] = True; g[1::2, 0::2] = True
    elif pattern in ("GRBG", "GBRG"):
        g[0::2, 0::2] = True; g[1::2, 1::2] = True
    else:
        raise ValueError(pattern)
    img = bayer.astype(np.float32)
    k = np.array([[0, 1, 0], [1, 0, 1], [0, 1, 0]], np.float32) / 4
    filled = ndi.convolve(np.where(g, img, 0.0), k, mode="reflect")
    return np.where(g, img, filled)

def load_green(folder: Path) -> np.ndarray:
    stack = np.load(folder / "bottom_raw_stack.npy")
    mean = stack.astype(np.float32).mean(0)
    return green_full_res(mean, C["bayer_pattern"])

def load_calib():
    cal = Path(CFG["paths"]["calib_dir"])
    dark = load_green(cal / "dark") if (cal / "dark").exists() else None
    flat = load_green(cal / "flat") if (cal / "flat").exists() else None
    return dark, flat

def od_image(green: np.ndarray, dark, flat) -> np.ndarray:
    if dark is not None:
        green = green - dark
        flat = None if flat is None else flat - dark
    if flat is None:                      # no blank-plate flat yet: normalise to the 99.5th pct
        flat = np.full_like(green, np.percentile(green, 99.5))
    t = np.clip(green, 1.0, None) / np.clip(flat, 1.0, None)
    return (-np.log10(np.clip(t, 1e-3, None))).astype(np.float32)

# ------------------------------------------------------------- plate frame
def to_plate_frame(od: np.ndarray) -> np.ndarray:
    rot = float(C["rotation_deg"])
    return ndi.rotate(od, rot, reshape=False, order=1, mode="nearest") if abs(rot) > 1e-3 else od

def mm_to_px(x_mm, y_mm):
    ppm = C["px_per_mm"]; cx, cy = C["plate_centre_px"]
    return cx + x_mm * ppm, cy - y_mm * ppm            # image rows grow downward = -Y

def refine_centre(od: np.ndarray, cx: float, cy: float) -> tuple[float, float]:
    """Lock onto the INNER edge of the well-wall shadow: an annulus template that is +1 just
    outside R = well_id/2 and -1 just inside it, so a thick wall ring cannot bias the fit.
    Coarse (4 px) then fine (1 px) search over +-refine_search_mm."""
    ppm = C["px_per_mm"]; R = G["well_id_mm"] / 2 * ppm; s = int(G["refine_search_mm"] * ppm)
    band = max(2, int(0.6 * ppm))
    n = int(R) + band + 2
    yy, xx = np.mgrid[-n: n + 1, -n: n + 1]
    rr = np.hypot(xx, yy)
    tmpl = np.where((rr >= R) & (rr < R + band), 1.0, 0.0) - np.where((rr >= R - band) & (rr < R), 1.0, 0.0)
    tmpl = tmpl.astype(np.float32)
    def score(x0, y0):
        sub = od[y0 - n: y0 + n + 1, x0 - n: x0 + n + 1]
        return float((sub * tmpl).sum()) if sub.shape == tmpl.shape else -np.inf
    bx, by = int(round(cx)), int(round(cy))
    for step, rad in ((4, s), (1, 5)):
        best, cand = -np.inf, (bx, by)
        for dy in range(-rad, rad + 1, step):
            for dx in range(-rad, rad + 1, step):
                v = score(bx + dx, by + dy)
                if v > best: best, cand = v, (bx + dx, by + dy)
        bx, by = cand
    return float(bx), float(by)

# ------------------------------------------------------------- colonies
def segment_well(od: np.ndarray, cx: float, cy: float):
    from skimage.segmentation import watershed
    from skimage.feature import peak_local_max
    from skimage.measure import label, regionprops
    ppm = C["px_per_mm"]; R = int(G["analysis_radius_mm"] * ppm)
    y0, y1, x0, x1 = int(cy) - R, int(cy) + R + 1, int(cx) - R, int(cx) + R + 1
    sub = od[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    inside = np.hypot(xx - cx, yy - cy) <= R
    # Parallax: an off-axis well is viewed at an angle, so rays to the far side of its floor
    # pass through the 17 mm wall. The floor seen through the OPEN well mouth is the floor disc
    # intersected with the mouth disc projected along the rays: the same disc shifted toward
    # the optical axis by well_depth * (offset / camera_height). Exclude the crescent.
    I = CFG.get("instrument", {})
    if I.get("camera_height_mm"):
        ax, ay = mm_to_px(*I.get("optical_axis_mm", [0.0, 0.0]))
        k = G["well_depth_mm"] / I["camera_height_mm"]
        sx, sy = cx + (ax - cx) * k, cy + (ay - cy) * k
        inside &= np.hypot(xx - sx, yy - sy) <= R
    fp = int(A["background_open_mm"] * ppm) | 1
    bg = ndi.grey_opening(sub, size=(fp, fp))
    bg = ndi.gaussian_filter(bg, A["background_sigma_mm"] * ppm)
    sig = np.where(inside, sub - bg, 0.0)
    mask = sig > A["od_threshold"]
    mask = ndi.binary_opening(mask, iterations=1)
    dist = ndi.distance_transform_edt(mask)
    dist_s = ndi.gaussian_filter(dist, 0.1 * ppm)
    md = max(1, int(A["watershed_min_distance_mm"] * ppm))
    peaks = peak_local_max(dist_s, min_distance=md, threshold_abs=A["min_colony_diameter_mm"] * ppm / 2,
                           labels=label(mask), exclude_border=False)
    markers = np.zeros(mask.shape, np.int32); markers[tuple(peaks.T)] = np.arange(1, len(peaks) + 1)
    lab = watershed(-dist, markers, mask=mask) if len(peaks) else label(mask)
    rows = []
    px_area = 1.0 / ppm**2
    edge = inside & ~ndi.binary_erosion(inside, iterations=3)       # 3-px rim of the analysis disc
    for rp in regionprops(lab, intensity_image=sig):
        d = 2 * np.sqrt(rp.area * px_area / np.pi)
        if d < A["min_colony_diameter_mm"]: continue
        if edge[rp.coords[:, 0], rp.coords[:, 1]].any(): continue       # touches the rim: wall shadow or a cut colony
        yc, xc = rp.centroid
        rows.append({"x_mm": (x0 + xc - C["plate_centre_px"][0]) / ppm,
                     "y_mm": -(y0 + yc - C["plate_centre_px"][1]) / ppm,
                     "area_mm2": rp.area * px_area, "eq_diam_mm": d,
                     "integrated_od": float(rp.image_intensity[rp.image].sum() * px_area)})
    return rows, lab, (y0, x0), inside

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("folder"); a = ap.parse_args()
    folder = Path(a.folder)
    with run_log("quantify", CFG["paths"]["runs_dir"], config={"geometry": G, "camera": C, "analysis": A},
                 inputs=[str(folder / "bottom_raw_stack.npy")]) as rec:
        dark, flat = load_calib()
        rec["inputs"] += [f"calib:dark={'yes' if dark is not None else 'no'}", f"calib:flat={'yes' if flat is not None else 'no'}"]
        od = to_plate_frame(od_image(load_green(folder), dark, flat))
        np.save(folder / "od.npy", od); rec["outputs"].append(str(folder / "od.npy"))
        colonies, wells, labels_img = [], [], np.zeros(od.shape, np.int32)
        centres = {w: refine_centre(od, *mm_to_px(*G["well_centres_mm"][w])) for w in G["well_names"]}
        if A.get("fit_scale_per_scan", False):
            # similarity fit of the six refined rings to the nominal pitch: absorbs plate-height
            # differences between brands (1 % scale per 2 mm) and any residual rotation
            P = np.array([[G["well_centres_mm"][w][0], -G["well_centres_mm"][w][1]] for w in G["well_names"]])
            Q = np.array([centres[w] for w in G["well_names"]])
            pc, qc = P.mean(0), Q.mean(0); U, S, Vt = np.linalg.svd((P - pc).T @ (Q - qc))
            Rm = Vt.T @ U.T; scale = S.sum() / ((P - pc) ** 2).sum()
            resid = np.linalg.norm(Q - (scale * (P @ Rm.T) + (qc - scale * (Rm @ pc))), axis=1) / scale
            rec["metrics"]["px_per_mm_fitted"] = float(scale); rec["metrics"]["ring_fit_resid_mm_max"] = float(resid.max())
            C["px_per_mm"] = float(scale)                          # this scan only (not written back)
            if resid.max() > 0.5:
                print(f"WARNING: well-ring fit residual {resid.max():.2f} mm — wrong plate preset or a mis-seated plate?")
        for w in G["well_names"]:
            cx, cy = centres[w]
            rows, lab, (y0, x0), inside = segment_well(od, cx, cy)
            for r in rows: r["well"] = w
            colonies += rows
            ppm = C["px_per_mm"]
            well_area = float(inside.sum()) / ppm**2            # usable floor area after the parallax cut
            wells.append({"well": w, "centre_px": [round(cx, 1), round(cy, 1)], "n_colonies": len(rows),
                          "total_area_mm2": sum(r["area_mm2"] for r in rows),
                          "integrated_od": sum(r["integrated_od"] for r in rows),
                          "area_fraction": sum(r["area_mm2"] for r in rows) / well_area,
                          "mean_od_in_well": float(od[int(cy) - int(G["analysis_radius_mm"] * ppm): int(cy) + int(G["analysis_radius_mm"] * ppm),
                                                     int(cx) - int(G["analysis_radius_mm"] * ppm): int(cx) + int(G["analysis_radius_mm"] * ppm)].mean())})
            labels_img[y0:y0 + lab.shape[0], x0:x0 + lab.shape[1]] = np.where(lab > 0, lab + labels_img.max(), labels_img[y0:y0 + lab.shape[0], x0:x0 + lab.shape[1]])
        for name, rows in (("colonies.csv", colonies), ("wells.csv", wells)):
            if rows:
                with open(folder / name, "w", newline="") as f:
                    wr = csv.DictWriter(f, fieldnames=list(rows[0].keys())); wr.writeheader(); wr.writerows(rows)
                rec["outputs"].append(str(folder / name))
        overlay(od, labels_img, wells, folder / "overlay.png"); rec["outputs"].append(str(folder / "overlay.png"))
        summary = {"wells": wells, "n_colonies_total": len(colonies)}
        (folder / "summary.json").write_text(json.dumps(summary, indent=1))
        rec["metrics"] = {w["well"]: w["n_colonies"] for w in wells}
        print(json.dumps(rec["metrics"]))

def overlay(od, labels_img, wells, path):
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    from skimage.segmentation import find_boundaries
    fig, ax = plt.subplots(figsize=(12, 9), dpi=110)
    ax.imshow(od, cmap="gray_r", vmin=0, vmax=max(0.3, float(np.percentile(od, 99.9))))
    b = find_boundaries(labels_img, mode="outer")
    ov = np.zeros(od.shape + (4,), np.float32); ov[b] = (1, 0.2, 0.1, 1); ax.imshow(ov)
    ppm = C["px_per_mm"]
    for w in wells:
        cx, cy = w["centre_px"]
        ax.add_patch(plt.Circle((cx, cy), G["analysis_radius_mm"] * ppm, fill=False, color="tab:blue", lw=0.8))
        ax.text(cx, cy - G["well_id_mm"] / 2 * ppm - 10, f"{w['well']}: {w['n_colonies']}", color="tab:blue", ha="center", fontsize=9)
    ax.set_axis_off(); fig.tight_layout(); fig.savefig(path); plt.close(fig)

if __name__ == "__main__":
    main()
