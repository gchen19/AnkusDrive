#!/usr/bin/env python3
"""
Optical / illumination design calculation for the clonogenic 6-well plate scanner.

Design calculation, not a lab analysis: every constant carries a provenance tag in
SOURCES (lab rule: a number is `distilled` until a datasheet is behind it).

What it answers
  1. Pixel scale, field of view and depth of field of the bottom (quantification)
     camera: Raspberry Pi HQ Camera + 6 mm CS lens, sensor 150 mm below the plate seat.
  2. Whether marker ink on the LID corrupts colony quantification when the plate is
     imaged lid-on from below.  The ink sits ~20 mm above the colony plane and the
     hood is a large diffuse source, so its shadow is a smooth, MULTIPLICATIVE dimming.
     In OD space that is an additive low-frequency offset removed by local
     background estimation.  The number here is how large that offset is before
     removal.
  3. Field of view of the top (label) camera, Camera Module 3, 127.5 mm above the lid.
  4. Photometric budget: exposure time at a chosen F-number.
"""
from __future__ import annotations
import json, math

SOURCES = {
    "hq_sensor":  "Raspberry Pi HQ Camera (IMX477): 4056x3040, 1.55 um pixels, "
                  "6.287 x 4.712 mm active area, CS mount (12.5 mm flange)",
    "lens6":      "Raspberry Pi 6 mm CS lens PT361060M3MP12: f=6 mm, F1.2, iris to F16",
    "cam3":       "Raspberry Pi Camera Module 3 (IMX708): 4608x2592, 1.4 um px, "
                  "HFOV 66 deg, VFOV 41 deg (datasheet), autofocus 10 cm - inf",
    "plate":      "Corning 3516 (Sigma cls3516 spec): 127.76 x 85.47 x 20.27 mm, well 35.43 top / 34.80 "
                  "bottom, depth 17.4, bottom elevation 2.54 + thickness 1.27; pitch 39.12 (SLAS 4-2004). "
                  "Eppendorf 6-well TDS: with lid 23.2 mm, pitch 40 x 38. Lid heights for Corning/Falcon not "
                  "found in a datasheet; 22.5 assumed, slot is 34.",
    "cv_abs":     "Crystal violet lambda_max ~590 nm in water (Merck/Sigma spectra); broad band 540-620 nm",
    "ink":        "Sharpie fine tip stroke ~1 mm, chisel tip ~5 mm (measured on a lid; VERIFY)",
    "geometry":   "build_scanner.py: hood interior 170 x 112 mm, LED cove top Z=+43, ceiling Z=+145, "
                  "lid top Z=+22.5, well floor Z~+1.5, sensor Z=-150",
    "coc":        "Circle of confusion taken as 2 px = 3.1 um on the HQ sensor (conservative)",
    "led":        "5 V COB strip ~4 W/m, ~350 lm/m at 5000 K (typical vendor spec; VERIFY)",
    "colony":     "Franken et al. 2006 Nat Protoc 1:2315 — a colony is >= 50 cells; "
                  "typical diameter 0.3-1 mm at 10-14 d for adherent lines",
}

# ---------------------------------------------------------------- inputs
HQ_W, HQ_H, HQ_PX = 6.287, 4.712, 4056           # mm, mm, px
F_LENS = 6.0                                     # mm
OBJ_DIST = 150.0 + 3.81                          # sensor -> well floor, mm (Corning 3516 floor at 3.81)
COC = 2 * 1.55e-3                                # mm
PLATE_L, PLATE_W = 127.76, 85.47
APER_X, APER_Y = 121.0, 79.0

LID_TOP, WELL_FLOOR = 22.5, 3.81                 # Z, mm; floor = 2.54 elevation + 1.27 bottom (Corning 3516)
COVE_TOP, CEIL_Z = 43.0, 145.0
HOOD_X, HOOD_Y = 170.0, 112.0

CAM3_HFOV, CAM3_VFOV, CAM3_PX = 66.0, 41.0, 4608
CAM3_DIST = 150.0 - LID_TOP                      # lens (Z=+150) to lid top

# ---------------------------------------------------------------- 1. bottom camera
mag = F_LENS / (OBJ_DIST - F_LENS)               # thin-lens magnification
fov_x, fov_y = HQ_W / mag, HQ_H / mag
px_per_mm = HQ_PX / fov_x
um_per_px = 1000.0 / px_per_mm

def dof(N: float) -> tuple[float, float]:
    """Near/far limits (mm from sensor) for F-number N."""
    H = F_LENS**2 / (N * COC) + F_LENS
    s = OBJ_DIST
    near = H * s / (H + s - F_LENS)
    far = H * s / (H - s + F_LENS) if H > s - F_LENS else math.inf
    return near, far

# ---------------------------------------------------------------- 2. ink shadow
def cos_weighted_solid_angle_rect(hx: float, hy: float, h: float, n: int = 200) -> float:
    """Projected solid angle (sr, cos-weighted) of a horizontal rectangle +-hx, +-hy at height h,
    seen from a point directly below its centre."""
    xs = [(-hx + (i + 0.5) * 2 * hx / n) for i in range(n)]
    ys = [(-hy + (j + 0.5) * 2 * hy / n) for j in range(n)]
    dA = (2 * hx / n) * (2 * hy / n)
    tot = 0.0
    for x in xs:
        for y in ys:
            r2 = x * x + y * y + h * h
            tot += h * h / (r2 * r2) * dA          # cos^2 theta / r^2  (cos for projection, cos for foreshortening)
    return tot

h_src = CEIL_Z - WELL_FLOOR
# source = ceiling (full rectangle) + upper walls between the cove lip and the ceiling.
# Approximate the walls' contribution by extending the effective source cap: a colony at the
# plate centre sees the walls out to half-angle atan(85/(43-1.5)) = 64 deg in X, 53 deg in Y.
omega_ceiling = cos_weighted_solid_angle_rect(HOOD_X / 2, HOOD_Y / 2, h_src)
# walls: integrate vertical strips (radiance assumed equal to ceiling, i.e. an integrating box)
def wall_strip(d: float, L: float, z0: float, z1: float, n: int = 400) -> float:
    """Projected solid angle of a vertical wall at horizontal distance d, length 2L (centred),
    from z0 to z1 above the colony plane."""
    tot = 0.0
    for i in range(n):
        z = z0 + (i + 0.5) * (z1 - z0) / n
        dz = (z1 - z0) / n
        for j in range(n // 4):
            x = -L + (j + 0.5) * 2 * L / (n // 4)
            dx = 2 * L / (n // 4)
            r2 = x * x + d * d + z * z
            cos_p = z / math.sqrt(r2)             # projection onto the colony plane normal
            cos_s = d / math.sqrt(r2)             # foreshortening of the wall element
            tot += cos_p * cos_s / r2 * dx * dz
    return tot
omega_walls = (2 * wall_strip(HOOD_Y / 2, HOOD_X / 2, COVE_TOP + 6 - WELL_FLOOR, h_src)
               + 2 * wall_strip(HOOD_X / 2, HOOD_Y / 2, COVE_TOP + 6 - WELL_FLOOR, h_src))
omega_src = omega_ceiling + omega_walls

h_ink = LID_TOP - WELL_FLOOR
def ink_dimming(width: float, length: float) -> float:
    """Fractional irradiance loss directly under the centre of an opaque ink stroke."""
    return cos_weighted_solid_angle_rect(width / 2, length / 2, h_ink) / omega_src

shadow = {
    "fine_1mm_x_10mm": ink_dimming(1.0, 10.0),
    "chisel_5mm_x_35mm_across_a_well": ink_dimming(5.0, 35.0),
    "block_letter_10mm_x_10mm": ink_dimming(10.0, 10.0),
}
# penumbra scale: the shadow edge is blurred over ~ h_ink * tan(source half-angle)
penumbra_mm = h_ink * math.tan(math.radians(55))

# ---------------------------------------------------------------- 3. top camera
top_fov_x = 2 * CAM3_DIST * math.tan(math.radians(CAM3_HFOV / 2))
top_fov_y = 2 * CAM3_DIST * math.tan(math.radians(CAM3_VFOV / 2))
top_px_per_mm = CAM3_PX / top_fov_x

# ---------------------------------------------------------------- 4. photometry (order of magnitude)
# Integrating-box radiance: a 0.8 m strip at ~350 lm/m = 280 lm into a box of internal area
# A ~ 2*(170*112) + 2*(170+112)*102 mm^2 ~ 0.096 m^2 with wall reflectance 0.85 ->
# E_wall ~ Phi / (A (1-rho)) ~ 280 / (0.096*0.15) ~ 19,000 lux at the plate (upper bound).
STRIP_M, LM_PER_M, RHO = 0.8, 350.0, 0.85
A_box = 2 * (HOOD_X * HOOD_Y) * 1e-6 + 2 * (HOOD_X + HOOD_Y) * (CEIL_Z - COVE_TOP) * 1e-6
E_plate_lux = STRIP_M * LM_PER_M / (A_box * (1 - RHO))
L_plate = E_plate_lux / math.pi                  # cd/m^2 seen by the bottom camera through a clear well
# Exposure: for an ISO-100 sensor, correct exposure at N, t satisfies L = K N^2 / (t S), K = 12.5
def exposure_s(N: float, iso: float = 100.0) -> float:
    return 12.5 * N**2 / (L_plate * iso)

out = {
    "bottom_camera": {
        "magnification": round(mag, 4),
        "fov_mm": [round(fov_x, 1), round(fov_y, 1)],
        "covers_aperture": fov_x > APER_X and fov_y > APER_Y,
        "covers_full_plate": fov_x > PLATE_L and fov_y > PLATE_W,
        "px_per_mm": round(px_per_mm, 1),
        "um_per_px": round(um_per_px, 1),
        "px_across_0p3mm_colony": round(0.3 * px_per_mm, 1),
        "px_across_1mm_colony": round(1.0 * px_per_mm, 1),
        "dof_mm_from_sensor": {f"F{N:g}": [round(a, 1), (round(b, 1) if b != math.inf else "inf")]
                               for N, (a, b) in ((N, dof(N)) for N in (2.8, 4, 5.6, 8))},
    },
    "ink_shadow_lid_on": {
        "projected_solid_angle_of_source_sr": round(omega_src, 3),
        "of_which_ceiling_sr": round(omega_ceiling, 3),
        "fractional_dimming_under_stroke": {k: round(v, 4) for k, v in shadow.items()},
        "penumbra_scale_mm": round(penumbra_mm, 1),
        "note": "multiplicative and smooth on a ~20 mm scale; in OD space an additive offset "
                "removed by a >=10 mm background estimate. Colonies are 0.3-2 mm.",
    },
    "top_camera": {
        "distance_to_lid_mm": CAM3_DIST,
        "fov_mm": [round(top_fov_x, 1), round(top_fov_y, 1)],
        "covers_full_plate": top_fov_x > PLATE_L and top_fov_y > PLATE_W,
        "px_per_mm": round(top_px_per_mm, 1),
    },
    "photometry_order_of_magnitude": {
        "illuminance_at_plate_lux": round(E_plate_lux),
        "exposure_s": {f"F{N:g}": round(exposure_s(N), 4) for N in (4, 5.6, 8)},
        "note": "upper bound (ideal integrating box); real box is lossier, expect 2-5x longer",
    },
    "sources": SOURCES,
}
if __name__ == "__main__":
    print(json.dumps(out, indent=2))
