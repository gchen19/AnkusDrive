"""
Standard reference tables — ISO threads/fasteners, deep-groove ball bearings
(C/C0), ISO 286 limits & fits, and ASME B36.10 stock/profiles (issue #101).

Pure-Python, FreeCAD-free: each table is a small JSON corpus + accessor in
ankusdrive/analysis/standards, plus the wiring that lets bearing_life() pull C/C0
from a designation and bolted_joint_check() pull pitch / tensile-stress-area /
proof strength from a bolt size + property class. ISO 286 fits already live as
computed tables in ankusdrive.analysis.tolerance.fit_class — verified here too so
the four-table acceptance is one lane.

Golden anchors (transcribed from public engineering references — see each JSON
_meta.source):
    - M8x1.25 tensile stress area = 36.6 mm^2 (ISO 724 / Machinery's Handbook)
    - 6205 bearing -> bore 25 / OD 52 / width 15 mm, C ~ 14.0 kN (ISO 15)
    - H7/g6 Ø25 -> clearance 7..41 µm (ISO 286)
    - NPS 1 sch40 pipe -> OD 33.40 mm, wall 3.38 mm (ASME B36.10M)
Two-sided: bad designations/sizes raise; out-of-table numeric lookups degrade
cleanly (nearest-stock helpers report the gap rather than raising).

Run:  python3 tests/test_standards.py   (no solver, no FreeCAD)
"""
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive.analysis import standards as std          # noqa: E402
from ankusdrive.analysis import machine_elements as me     # noqa: E402
from ankusdrive.analysis import tolerance as tol           # noqa: E402


# --- 1. threads & fasteners ---------------------------------------------------

def test_thread_tensile_stress_area_golden():
    """M8x1.25 tensile stress area = 36.6 mm^2 (the headline ISO 724 golden), and
    the tabulated At matches the closed-form At=(pi/4)(d-0.9382p)^2 the materials/
    machine-element code uses, so the table and the formula agree."""
    assert std.tensile_stress_area("M8") == 36.6, std.tensile_stress_area("M8")
    assert std.tensile_stress_area("M8x1.25") == 36.6
    # a couple more anchors across the range
    assert std.tensile_stress_area("M6") == 20.1
    assert std.tensile_stress_area("M10") == 58.0
    assert std.tensile_stress_area("M20") == 245.0
    # table vs closed form (machine_elements) within rounding
    formula = me.tensile_stress_area_mm2(8, 1.25)
    assert abs(formula - 36.6) < 0.1, formula


def test_thread_pitch_and_card():
    """Coarse pitch comes from the table; a designation with an explicit pitch
    (M8x1.0) overrides to the fine pitch."""
    assert std.thread_pitch("M8") == 1.25
    assert std.thread_pitch("M8x1.0") == 1.0
    assert std.thread("M8")["nominal_dia_mm"] == 8.0
    assert "M8" in std.list_threads()
    assert "M14" not in std.list_threads(preferred_only=True)  # ISO 262 non-preferred


def test_property_class_and_proof():
    """ISO 898-1 property classes: 8.8 proof 580 MPa, with the >M16 split raising
    it to 600 MPa; 10.9 proof 830 MPa."""
    assert std.thread_property_class("8.8")["proof_strength_mpa"] == 580
    assert std.thread_property_class("10.9")["tensile_strength_mpa"] == 1040
    assert std.proof_strength_mpa("M8", "8.8") == 580
    assert std.proof_strength_mpa("M20", "8.8") == 600  # >M16 split
    assert std.proof_strength_mpa("M8", "10.9") == 830


def test_recommended_preload_and_torque():
    """Recommended preload Fi = 0.75*Sp*At and torque T = K*Fi*d. For M8 class 8.8:
    Fi = 0.75*580*36.6 = 15921 N; dry K=0.20 -> T = 0.2*15921*0.008 ≈ 25.5 N*m."""
    pre = std.recommended_preload("M8", "8.8", 0.75)
    assert abs(pre["preload_n"] - 0.75 * 580 * 36.6) < 1.0, pre
    tq = std.recommended_torque("M8", "8.8", 0.75, condition="dry")
    expect = 0.20 * pre["preload_n"] * 0.008
    assert abs(tq["torque_nm"] - expect) < 0.1, tq
    assert tq["torque_nm"] > 20 and tq["torque_nm"] < 30  # sane M8 8.8 ballpark


def test_bolted_joint_uses_thread_table():
    """bolted_joint_check(bolt_size='M8', property_class='8.8') pulls pitch, the
    tabulated tensile stress area (36.6 mm^2) and proof strength (580 MPa) from the
    standards table instead of guessing."""
    r = me.bolted_joint_check(bolt_size="M8", property_class="8.8",
                              preload_n=15000)
    assert r["tensile_stress_area_mm2"] == 36.6, r
    # proof_load_n = Sp*At = 580*36.6 = 21228 N
    assert abs(r["proof_load_n"] - 580 * 36.6) < 1.0, r
    # explicit dia path still works unchanged
    r2 = me.bolted_joint_check(bolt_dia_mm=8, pitch_mm=1.25, preload_n=15000)
    assert abs(r2["tensile_stress_area_mm2"] - 36.6) < 0.1, r2


def test_thread_bad_size_raises():
    """A bad thread designation raises StandardNotFound; a bad property class too."""
    for bad in ("M7.3", "M999", "garbage"):
        try:
            std.tensile_stress_area(bad)
        except std.StandardNotFound:
            pass
        else:
            raise AssertionError(f"expected StandardNotFound for {bad!r}")
    try:
        std.thread_property_class("99.9")
    except std.StandardNotFound:
        pass
    else:
        raise AssertionError("expected StandardNotFound for bad property class")


# --- 2. bearings --------------------------------------------------------------

def test_bearing_catalog_golden():
    """6205 deep-groove ball bearing -> bore 25 / OD 52 / width 15 mm, C ~ 14.0 kN,
    C0 = 7.8 kN (the headline catalog golden)."""
    b = std.bearing("6205")
    assert b["bore_mm"] == 25 and b["od_mm"] == 52 and b["width_mm"] == 15, b
    assert b["dynamic_c_kn"] == 14.0, b
    assert b["static_c0_kn"] == 7.80, b
    assert b["dynamic_c_n"] == 14000.0
    assert "6205" in std.list_bearings("6200")
    assert "6205" not in std.list_bearings("6300")


def test_bearing_life_from_designation():
    """bearing_life(designation='6205', ...) pulls C=14.0 kN from the catalog so the
    caller no longer supplies it; an explicit dynamic_load_c_n still overrides."""
    r = me.bearing_life(equivalent_load_p_n=3000, speed_rpm=1500,
                        designation="6205")
    assert r["dynamic_load_c_n"] == 14000.0, r
    assert r["bore_mm"] == 25 and r["static_load_c0_n"] == 7800.0, r
    # L10 = (14000/3000)^3 = 101.6 million rev
    assert abs(r["l10_million_rev"] - (14000 / 3000) ** 3) < 0.1, r
    assert r["static_safety_factor"] == round(7800 / 3000, 2)
    # explicit C overrides the catalog
    r2 = me.bearing_life(equivalent_load_p_n=3000, speed_rpm=1500,
                         designation="6205", dynamic_load_c_n=20000)
    assert r2["dynamic_load_c_n"] == 20000.0, r2
    # legacy positional call (no designation) unchanged
    r3 = me.bearing_life(30000, 3000, 1500)
    assert r3["load_ratio"] == 10.0, r3


def test_bearing_bad_designation_raises():
    """An unknown bearing designation raises StandardNotFound, both at the table and
    through bearing_life."""
    try:
        std.bearing("9999")
    except std.StandardNotFound:
        pass
    else:
        raise AssertionError("expected StandardNotFound for 9999")
    try:
        me.bearing_life(equivalent_load_p_n=3000, speed_rpm=1500,
                        designation="not-a-bearing")
    except std.StandardNotFound:
        pass
    else:
        raise AssertionError("expected StandardNotFound via bearing_life")
    # neither C nor designation -> ValueError
    try:
        me.bearing_life(equivalent_load_p_n=3000, speed_rpm=1500)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError when no C and no designation")


# --- 3. ISO 286 limits & fits (already computed in tolerance.fit_class) -------

def test_iso286_h7g6_clearance_band_golden():
    """H7/g6 Ø25 fit: hole +0/+21 µm, shaft -7/-20 µm -> clearance band 7..41 µm.
    fit_class computes these from the ISO 286 IT-grade + fundamental-deviation
    tables (not approximated)."""
    r = tol.fit_class(25, "H7/g6")
    assert r["fit_class"] == "clearance", r
    assert abs(r["min_clearance"] - 0.007) < 1e-6, r
    assert abs(r["max_clearance"] - 0.041) < 1e-6, r
    # hole H7 @ Ø25 -> +0/+0.021; shaft g6 -> -0.007/-0.020
    assert abs(r["hole"]["max"] - 25.021) < 1e-6, r
    assert abs(r["shaft"]["max"] - 24.993) < 1e-6, r
    assert abs(r["shaft"]["min"] - 24.980) < 1e-6, r


def test_iso286_out_of_table_raises():
    """An out-of-table size (>500 mm) raises; interference letters are supported
    since #168 (H7/p6 → an interference band), but a letter outside the k/m/n/p/r/s
    set and a non-H hole basis still raise rather than returning a wrong number."""
    try:
        tol.fit_class(600, "H7/g6")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for >500 mm")
    # H7/p6 now resolves to an interference fit (issue #168) — negative clearance,
    # classified interference, with a press_fit_stress hand-off band.
    r = tol.fit_class(25, "H7/p6")
    assert r["fit_class"] == "interference", r
    assert r["max_clearance"] < 0, r
    assert r["interference"]["max_mm"] > 0, r
    # A letter outside the supported set, and a non-H hole basis, still raise.
    for spec in ("H7/u6", "G7/h6"):
        try:
            tol.fit_class(25, spec)
        except NotImplementedError:
            pass
        else:
            raise AssertionError(f"expected NotImplementedError for {spec}")


# --- 4. stock & profiles ------------------------------------------------------

def test_pipe_schedule_golden():
    """NPS 1 sch40 pipe -> OD 33.40 mm, wall 3.38 mm (ASME B36.10M). OD is fixed per
    NPS; sch80 thickens the wall. DN lookup resolves to the same row."""
    p = std.pipe("1", "sch40")
    assert p["od_mm"] == 33.40 and p["wall_mm"] == 3.38, p
    assert abs(p["id_mm"] - (33.40 - 2 * 3.38)) < 1e-6, p
    p80 = std.pipe("1", "sch80")
    assert p80["od_mm"] == 33.40 and p80["wall_mm"] == 4.55, p80
    # DN25 == NPS 1
    assert std.pipe("DN25", "sch40")["od_mm"] == 33.40
    # another anchor: NPS 1/2 OD 21.34, sch40 wall 2.77
    half = std.pipe("1/2", "sch40")
    assert half["od_mm"] == 21.34 and half["wall_mm"] == 2.77, half
    assert "1" in std.list_pipes()


def test_sheet_gauge_and_bar():
    """Sheet gauge 16 steel = 1.52 mm; round/hex nearest-stock helpers degrade
    cleanly (return the closest tabulated size and the gap, no raise)."""
    assert std.sheet_gauge(16)["thickness_mm"] == 1.52
    # exact hit
    rb = std.round_bar(25)
    assert rb["nearest_stock"] == 25 and rb["exact"] is True, rb
    # off-size degrades to nearest, reporting the gap
    rb2 = std.round_bar(26)
    assert rb2["nearest_stock"] == 25 and rb2["exact"] is False, rb2
    assert abs(rb2["delta"] - (-1.0)) < 1e-9, rb2
    hx = std.hex_bar(16.5)
    assert hx["nearest_stock"] == 17, hx


def test_stock_bad_lookups_raise():
    """A bad NPS, a schedule a size doesn't tabulate, and an untabulated sheet gauge
    all raise rather than guessing."""
    try:
        std.pipe("99", "sch40")
    except std.StandardNotFound:
        pass
    else:
        raise AssertionError("expected StandardNotFound for NPS 99")
    try:
        std.pipe("1", "sch160")  # not tabulated for this size
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for untabulated schedule")
    try:
        std.sheet_gauge(99)
    except std.StandardNotFound:
        pass
    else:
        raise AssertionError("expected StandardNotFound for gauge 99")


# --- runner -------------------------------------------------------------------

def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    failures = []
    t0 = time.time()
    for name, fn in _discover():
        t = time.time()
        try:
            fn()
        except Exception:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:44s} ({time.time() - t:.2f}s)")
        else:
            print(f"  PASS {name:44s} ({time.time() - t:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({time.time() - t0:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()
