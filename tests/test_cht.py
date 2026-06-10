"""Conjugate-heat-transfer toys (P3 M6) — exact oracles + the solver-backed gates.

Two tiers, both runnable on the no-FreeCAD lane:
  * **structure** (always): the composite-wall network is exact (handbook insulated
    wall, single-layer Fourier limit, drops sum to ΔT identically), the channel
    oracle obeys the energy balance algebra, the two-body mesh/sif are well-formed
    (shared interface nodes, per-BC SaveScalars masks, convection on the fluid
    equation only), and the cell-Péclet guard rejects the regime where stabilized
    advection visibly leaks energy. Pure string/math checks, no solver.
  * **solver-backed** (when ElmerSolver resolves, else SKIP): run the conjugate
    plug-flow channel — the outlet bulk temperature must match the exact h-free
    energy balance q″·L = ṁ·c_p·ΔT within 3 % and the solid-layer drop must match
    q″·t/k within 3 % (measured 0.5 % / 0.2 % live); the zero-flux negative must
    return T_out == T_in.

Run:  python3 tests/test_cht.py
"""
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import solvers  # noqa: E402
from driftpin.analysis import cht  # noqa: E402


# --- composite wall (exact oracle) ----------------------------------------------

def test_composite_wall_handbook_insulated_wall():
    # 20 mm brick-ish (k=1) + 50 mm insulation (k=0.04), films 10 / 25 W/m²K,
    # 20 °C across: R = 0.1 + 0.02 + 1.25 + 0.04 = 1.41, U = 0.70922, q = 14.184
    r = cht.composite_wall(
        [{"thickness_mm": 20, "k": 1.0}, {"thickness_mm": 50, "k": 0.04}],
        t_in_c=20.0, t_out_c=0.0, h_in=10.0, h_out=25.0)
    assert abs(r["r_total_m2k_w"] - 1.41) < 1e-9, r["r_total_m2k_w"]
    assert abs(r["u_w_m2k"] - 1.0 / 1.41) < 1e-6, r["u_w_m2k"]
    assert abs(r["q_w_m2"] - 20.0 / 1.41) < 1e-4, r["q_w_m2"]
    # interface temps walk monotonically and the drops reconstruct ΔT exactly
    temps = r["interface_temps_c"]
    assert len(temps) == 3 and all(a > b for a, b in zip(temps, temps[1:])), temps
    q = 20.0 / 1.41
    assert abs((20.0 - temps[0]) - q / 10.0) < 1e-3          # inner film drop
    assert abs((temps[-1] - 0.0) - q / 25.0) < 1e-3          # outer film drop


def test_composite_wall_single_layer_is_fourier():
    # no films, one layer: q = k·ΔT/t exactly (Fourier's law)
    r = cht.composite_wall([{"thickness_mm": 10, "k": 2.0}],
                           t_in_c=100.0, t_out_c=0.0)
    assert abs(r["q_w_m2"] - 2.0 * 100.0 / 0.01) < 1e-6, r["q_w_m2"]
    assert r["interface_temps_c"][0] == 100.0 and r["interface_temps_c"][-1] == 0.0


def test_composite_wall_material_lookup_and_area():
    r = cht.composite_wall([{"thickness_mm": 10, "material": "AL6061-T6"}],
                           t_in_c=50.0, t_out_c=0.0, area_m2=2.0)
    assert r["q_w"] == r["q_w_m2"] * 2.0
    assert r["q_w_m2"] > 1e5, "aluminum wall must conduct strongly"


def test_composite_wall_validation():
    for bad in (
        lambda: cht.composite_wall([], 20, 0),
        lambda: cht.composite_wall([{"thickness_mm": 0, "k": 1}], 20, 0),
        lambda: cht.composite_wall([{"thickness_mm": 10, "k": -1}], 20, 0),
        lambda: cht.composite_wall([{"thickness_mm": 10}], 20, 0),
        lambda: cht.composite_wall([{"thickness_mm": 10, "k": 1}], 20, 0, h_in=0),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# --- channel oracle + case structure ---------------------------------------------

def test_channel_oracle_energy_balance_algebra():
    orc = cht.cht_channel_oracle(
        flux_w_m2=10000.0, length_m=0.1, fluid_height_m=0.005,
        velocity_m_s=0.001, t_in_c=20.0, rho=1000.0, cp=4180.0,
        solid_thickness_m=0.002, k_solid=1.0)
    assert abs(orc["m_dot_kg_s_m"] - 0.005) < 1e-12
    assert abs(orc["t_out_c"] - (20.0 + 1000.0 / (0.005 * 4180.0))) < 1e-6
    assert abs(orc["dt_solid_k"] - 20.0) < 1e-9
    # doubling the flux doubles both temperature rises (linearity)
    orc2 = cht.cht_channel_oracle(
        flux_w_m2=20000.0, length_m=0.1, fluid_height_m=0.005,
        velocity_m_s=0.001, t_in_c=20.0, rho=1000.0, cp=4180.0,
        solid_thickness_m=0.002, k_solid=1.0)
    assert abs(orc2["dt_out_k"] - 2 * orc["dt_out_k"]) < 1e-9
    assert abs(orc2["dt_solid_k"] - 2 * orc["dt_solid_k"]) < 1e-9


def test_channel_mesh_shares_interface_nodes():
    files = cht.cht_channel_mesh_files(8, 3, 2, 0.1, 0.005, 0.002)
    header = files["mesh.header"].split("\n")[0].split()
    # nodes: 9 columns x (3+2+1) rows = 54; elems 8x5=40; bnd 2*3 + 3*8 = 30
    assert header == ["54", "40", "30"], header
    # the interface row is single (shared): exactly (nx+1) nodes at y = 0.005
    ys = [float(ln.split()[3]) for ln in files["mesh.nodes"].strip().splitlines()]
    assert sum(1 for y in ys if abs(y - 0.005) < 1e-12) == 9, "interface not shared"
    # five boundary tags present, interface tag 5 has nx segments
    tags = [int(ln.split()[1]) for ln in files["mesh.boundary"].strip().splitlines()]
    assert sorted(set(tags)) == [1, 2, 3, 4, 5]
    assert tags.count(5) == 8


def test_channel_sif_structure():
    sif = cht.cht_channel_sif(velocity_m_s=0.001, flux_w_m2=10000.0, t_in_c=20.0,
                              k_fluid=0.6, rho_fluid=1000.0, cp_fluid=4180.0,
                              k_solid=1.0)
    for token in ("Convection Velocity 1 = 0.001", "Heat Flux = 10000",
                  "Temperature = 20", "Mask Name 1 = outletmask",
                  "Mask Name 2 = outermask", "Mask Name 3 = ifacemask",
                  "Stabilize = True"):
        assert token in sif, f"missing {token!r}"
    # the conjugate asymmetry: convection on the fluid equation ONLY
    eq1 = sif.split("Equation 1")[1].split("End")[0]
    eq2 = sif.split("Equation 2")[1].split("End")[0]
    assert "Convection = Constant" in eq1 and "Convection" not in eq2


def test_channel_writer_guards_cell_peclet():
    with tempfile.TemporaryDirectory() as d:
        try:
            cht.write_cht_channel_case(d, velocity_m_s=0.01)   # Pe_cell ~ 87
        except ValueError as e:
            assert "Péclet" in str(e) or "Peclet" in str(e), e
        else:
            raise AssertionError("expected ValueError for Pe_cell > 25")
        built = cht.write_cht_channel_case(d)                  # defaults: Pe ~ 8.7
        assert os.path.isfile(os.path.join(d, "case.sif"))
        assert 5 < built["pe_cell"] < 15, built["pe_cell"]
        assert abs(built["oracle"]["dt_solid_k"] - 20.0) < 1e-9


def test_parse_cht_scalars_reads_columns():
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "cht.dat"), "w").write(
            "6.8e1 8.8e1 6.8e1\n6.806768766018E+001 8.798331272069E+001 6.794480627737E+001\n")
        r = cht.parse_cht_scalars(d)
        assert abs(r["t_outlet_mean_c"] - 68.06768766018) < 1e-9
        assert abs(r["dt_solid_k"] - (87.98331272069 - 67.94480627737)) < 1e-9
        assert cht.parse_cht_scalars(d, "missing.dat") is None


# --- solver-backed: the conjugate gates -----------------------------------------

def test_conjugate_channel_matches_energy_balance_and_solid_drop():
    """The M6 CHT gate: one Elmer solve across the coupled fluid+solid regions —
    outlet bulk temperature on the exact h-free energy balance, solid-layer drop on
    q″·t/k (measured 0.5 % / 0.2 % live; gates 3 %)."""
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    with tempfile.TemporaryDirectory() as d:
        built = cht.write_cht_channel_case(d)
        proc = subprocess.run([solvers.find_solver("elmer")["path"], built["sif"]],
                              cwd=d, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout[-800:]
        parsed = cht.parse_cht_scalars(d, built["scalars"])
        assert parsed, "no scalars written"
        orc = built["oracle"]
        eb = (parsed["t_outlet_mean_c"] - 20.0) / orc["dt_out_k"]
        sd = parsed["dt_solid_k"] / orc["dt_solid_k"]
        assert 0.97 < eb < 1.03, (parsed["t_outlet_mean_c"], orc["t_out_c"], eb)
        assert 0.97 < sd < 1.03, (parsed["dt_solid_k"], orc["dt_solid_k"], sd)
        print(f"    conjugate channel: energy balance {eb:.4f}, "
              f"solid drop {sd:.4f} (Pe_cell {built['pe_cell']})")


def test_conjugate_channel_zero_flux_negative():
    """The two-sided negative: with q″ = 0 every gate temperature equals the inlet
    temperature — any spurious source/sink in the coupled assembly would show."""
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    with tempfile.TemporaryDirectory() as d:
        built = cht.write_cht_channel_case(d, flux_w_m2=0.0)
        proc = subprocess.run([solvers.find_solver("elmer")["path"], built["sif"]],
                              cwd=d, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout[-800:]
        parsed = cht.parse_cht_scalars(d, built["scalars"])
        for key in ("t_outlet_mean_c", "t_outer_mean_c", "t_interface_mean_c"):
            assert abs(parsed[key] - 20.0) < 0.05, (key, parsed[key])


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    if not solvers.is_available("elmer"):
        print("  (ElmerSolver absent — structure tests run, solver tests SKIP)")
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")

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
