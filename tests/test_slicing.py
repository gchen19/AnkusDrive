"""FDM slice-estimate toys — two-sided oracles for driftpin.analysis.slicing.

Pure-Python, no FreeCAD. Each toy pins a closed-form result against a hand
calculation AND verifies a deliberately-bad input is caught, mirroring
tests/TOYS.md and docs/SIMULATION_EXAMPLES.md (family 9, slicing).

Run:  python3 tests/test_slicing.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import slicing as sl  # noqa: E402

PLA_RHO = 1.24  # g/cc, from the Materials DB (asserted below)


def test_solid_filament_equals_mass_at_full_infill():
    # 100% infill: filament_g == mass_g == density * volume_cm3.
    # V = 20000 mm^3 = 20 cm^3 ; mass = 1.24 * 20 = 24.8 g
    r = sl.slice_estimate(volume_mm3=20000.0, bbox_mm=[40, 25, 20], material="PLA",
                          infill_fraction=1.0)
    vol_cm3 = 20000.0 * 1e-3
    expected = PLA_RHO * vol_cm3
    assert abs(r["mass_g"] - expected) < 1e-3, r["mass_g"]
    assert abs(r["filament_g"] - r["mass_g"]) < 1e-3, (r["filament_g"], r["mass_g"])
    assert abs(r["filament_g"] - expected) < 1e-3, r["filament_g"]
    # at full infill the deposited volume equals the solid volume
    assert abs(r["deposited_volume_mm3"] - 20000.0) < 1e-3, r["deposited_volume_mm3"]
    assert r["infill_fraction"] == 1.0


def test_density_is_read_from_materials_db():
    # the closed-form anchor above assumes PLA = 1.24 g/cc; pin that the value
    # really comes from the Materials DB (an explicit override would also work).
    from driftpin.analysis import materials
    rho = materials.numeric(materials.get("PLA"), "density_g_cc")
    assert abs(rho - PLA_RHO) < 1e-6, rho


def test_layer_count_is_exact_ceil():
    # height 20 mm / 0.2 = 100 layers exactly; 20.05 / 0.2 -> ceil(100.25) = 101
    r = sl.slice_estimate(1000.0, bbox_mm=[10, 10, 20.0], layer_height_mm=0.2)
    assert r["layer_count"] == 100, r["layer_count"]
    r2 = sl.slice_estimate(1000.0, bbox_mm=[10, 10, 20.05], layer_height_mm=0.2)
    assert r2["layer_count"] == math.ceil(20.05 / 0.2) == 101, r2["layer_count"]
    # deliberately wrong: a zero layer height is non-physical and must raise
    try:
        sl.slice_estimate(1000.0, bbox_mm=[10, 10, 20.0], layer_height_mm=0.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for layer_height_mm <= 0")


def test_finer_layers_more_layers_and_longer_time():
    # finer layer_height -> strictly more layers AND strictly longer print time
    # (volumetric flow = nozzle * layer_height * speed, so thin layers print slow)
    coarse = sl.slice_estimate(50000.0, bbox_mm=[60, 40, 30], layer_height_mm=0.3)
    fine = sl.slice_estimate(50000.0, bbox_mm=[60, 40, 30], layer_height_mm=0.1)
    assert fine["layer_count"] > coarse["layer_count"], (
        fine["layer_count"], coarse["layer_count"])
    assert fine["print_time_min"] > coarse["print_time_min"], (
        fine["print_time_min"], coarse["print_time_min"])
    # closed-form time anchor for the coarse pass:
    # flow = 0.4 * 0.3 * 50 = 6 mm^3/s ; deposited = 50000 (full infill) ;
    # t = 50000 / 6 / 60 = 138.89 min
    flow = 0.4 * 0.3 * 50.0
    expected_min = 50000.0 / flow / 60.0
    assert abs(coarse["print_time_min"] - expected_min) < 0.05, coarse["print_time_min"]


def test_lower_infill_deposits_less_filament():
    # 20% infill must yield strictly LESS filament than 100% (and less than mass_g)
    full = sl.slice_estimate(20000.0, bbox_mm=[40, 25, 20], infill_fraction=1.0)
    sparse = sl.slice_estimate(20000.0, bbox_mm=[40, 25, 20], infill_fraction=0.2)
    assert sparse["filament_g"] < full["filament_g"], (
        sparse["filament_g"], full["filament_g"])
    assert sparse["filament_g"] < sparse["mass_g"], (
        sparse["filament_g"], sparse["mass_g"])
    # mass_g is the solid weight and does not depend on infill
    assert abs(sparse["mass_g"] - full["mass_g"]) < 1e-9, (
        sparse["mass_g"], full["mass_g"])
    # closed-form: fill = 0.35 + 0.2*0.65 = 0.48 -> deposited = 9600 mm^3
    assert abs(sparse["deposited_volume_mm3"] - 9600.0) < 1e-2, sparse["deposited_volume_mm3"]
    assert abs(sparse["filament_g"] - 9600.0 * 1e-3 * PLA_RHO) < 1e-3, sparse["filament_g"]


def test_unknown_material_raises():
    # an unknown material with no override must raise (no silent default density)
    try:
        sl.slice_estimate(1000.0, bbox_mm=[10, 10, 10], material="Unobtainium-7")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without a usable density")
    # but an explicit override lets the same unknown material through
    r = sl.slice_estimate(1000.0, bbox_mm=[10, 10, 10], material="Unobtainium-7",
                          density_g_cc=7.85)
    assert abs(r["mass_g"] - 1000.0 * 1e-3 * 7.85) < 1e-3, r["mass_g"]


# --- external-CLI upgrade (Sprint 4 follow-on) ----------------------------------
# Structure/parse tests run always (pure-Python, synthetic G-code); the live gate
# runs the real PrusaSlicer CLI when the `prusaslicer` solver resolves, else SKIPs.

_SYNTHETIC_GCODE = """;LAYER_CHANGE
G1 X1 Y1 E0.5
;LAYER_CHANGE
G1 X2 Y2 E1.0
;LAYER_CHANGE
; filament used [mm] = 1506.75
; filament used [cm3] = 3.62
; total filament used [g] = 0.00
; estimated printing time (normal mode) = 1h 19m 21s
; layer_height = 0.2
; first_layer_height = 0.35
; fill_density = 20%
; perimeters = 3
; nozzle_diameter = 0.4
; filament_diameter = 1.75
"""


def test_slicer_cmd_builds_headless_argv():
    argv = sl.slicer_cmd("a.stl", "out.gcode", layer_height_mm=0.2,
                         infill_fraction=0.2)
    assert argv[0] == "--export-gcode" and argv[-1] == "a.stl"
    assert "--fill-density" in argv and argv[argv.index("--fill-density") + 1] == "20%"
    assert "--fill-pattern" not in argv
    # PrusaSlicer's default pattern refuses 100% -> rectilinear is forced
    full = sl.slicer_cmd("a.stl", "o.gcode", infill_fraction=1.0)
    assert full[full.index("--fill-pattern") + 1] == "rectilinear"
    assert "--support-material" in sl.slicer_cmd("a.stl", "o.gcode", supports=True)
    for bad in (lambda: sl.slicer_cmd("", "o.gcode"),
                lambda: sl.slicer_cmd("a.stl", "o.gcode", layer_height_mm=0),
                lambda: sl.slicer_cmd("a.stl", "o.gcode", infill_fraction=0.0)):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_parse_gcode_time_formats():
    assert sl.parse_gcode_time("19m 21s") == 19 * 60 + 21
    assert sl.parse_gcode_time("1h 2m 3s") == 3723
    assert sl.parse_gcode_time("2d 1h") == 2 * 86400 + 3600
    assert sl.parse_gcode_time("5s") == 5
    try:
        sl.parse_gcode_time("soon")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_parse_gcode_stats_reads_footer_and_layers():
    r = sl.parse_gcode_stats(_SYNTHETIC_GCODE, density_g_cc=1.24)
    assert abs(r["filament_mm"] - 1506.75) < 1e-9
    assert abs(r["filament_cm3"] - 3.62) < 1e-9
    # grams recomputed from cm3 x density (the slicer reported 0.00)
    assert abs(r["filament_g"] - 3.62 * 1.24) < 1e-3, r["filament_g"]
    assert r["print_time_s"] == 3600 + 19 * 60 + 21
    assert r["layer_count"] == 3
    cfg = r["config"]
    assert cfg["layer_height_mm"] == 0.2 and cfg["first_layer_height_mm"] == 0.35
    assert cfg["fill_density_pct"] == 20 and cfg["perimeters"] == 3
    try:
        sl.parse_gcode_stats("G1 X0 Y0\n")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a non-PrusaSlicer G-code")


def _cube_stl_text(side_mm: float) -> str:
    """A closed ASCII-STL cube with outward winding (the live-gate part)."""
    s = side_mm
    quads = [
        ((0, 0, 0), (0, s, 0), (s, s, 0), (s, 0, 0)),
        ((0, 0, s), (s, 0, s), (s, s, s), (0, s, s)),
        ((0, 0, 0), (s, 0, 0), (s, 0, s), (0, 0, s)),
        ((0, s, 0), (0, s, s), (s, s, s), (s, s, 0)),
        ((0, 0, 0), (0, 0, s), (0, s, s), (0, s, 0)),
        ((s, 0, 0), (s, s, 0), (s, s, s), (s, 0, s)),
    ]
    out = ["solid cube"]
    for q in quads:
        for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3])):
            out.append("  facet normal 0 0 0\n    outer loop")
            out += [f"      vertex {p[0]} {p[1]} {p[2]}" for p in t]
            out.append("    endloop\n  endfacet")
    out.append("endsolid cube")
    return "\n".join(out) + "\n"


def test_real_slicer_brackets_the_analytic_estimate():
    """The live Sprint-4 gate: PrusaSlicer on a 20 mm cube. At 100% infill the
    sliced filament volume must land on the exact 8 cm3 within 10% (measured
    +0.75% — the skirt); at 20% it must deposit strictly less; the layer count
    must match the first-layer + layer-height arithmetic."""
    import subprocess
    import tempfile

    from driftpin import solvers
    if not solvers.is_available("prusaslicer"):
        print("    SKIP — PrusaSlicer not installed")
        return
    slicer = solvers.find_solver("prusaslicer")["path"]
    side = 20.0
    with tempfile.TemporaryDirectory() as d:
        stl = f"{d}/cube.stl"
        open(stl, "w", encoding="utf-8").write(_cube_stl_text(side))
        results = {}
        for frac in (1.0, 0.2):
            gcode = f"{d}/cube_{int(frac * 100)}.gcode"
            argv = [slicer] + sl.slicer_cmd(stl, gcode, layer_height_mm=0.2,
                                            infill_fraction=frac)
            proc = subprocess.run(argv, capture_output=True, text=True)
            assert proc.returncode == 0, (proc.stdout + proc.stderr)[-600:]
            results[frac] = sl.parse_gcode_stats(
                open(gcode, encoding="utf-8", errors="replace").read(), density_g_cc=1.24)
    full, sparse = results[1.0], results[0.2]
    exact_cm3 = side ** 3 / 1000.0
    ratio = full["filament_cm3"] / exact_cm3
    assert 0.95 < ratio < 1.10, (full["filament_cm3"], exact_cm3, ratio)
    assert sparse["filament_cm3"] < 0.7 * full["filament_cm3"], (sparse, full)
    # layers: first_layer_height + n*layer_height fills the cube height
    cfg = full["config"]
    expect = 1 + math.floor((side - cfg["first_layer_height_mm"])
                            / cfg["layer_height_mm"] + 1e-9)
    assert abs(full["layer_count"] - expect) <= 1, (full["layer_count"], expect)
    assert full["print_time_s"] and full["print_time_s"] > sparse["print_time_s"]
    print(f"    PrusaSlicer cube: 100% -> {full['filament_cm3']} cm3 "
          f"(exact {exact_cm3}, ratio {ratio:.3f}); 20% -> {sparse['filament_cm3']} cm3; "
          f"{full['layer_count']} layers")


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")

    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
