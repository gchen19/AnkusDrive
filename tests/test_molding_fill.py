"""Injection-molding VOF fill solve (issue #105) — case-structure gate (no solver)
plus the solver-backed fill / short-shot gate (skips when OpenFOAM is absent).

The no-solver half checks the interFoam case text is well-formed and the field
parsers + the moldability gate behave on synthetic fields. The solver-backed half
builds two real 2-D plaque cavities and runs blockMesh+interFoam: a fillable one
(the melt front reaches the far end, gate passes) and a deliberately too-long /
too-viscous / too-short-time one (the front stalls, gate flags a short shot).
"""
import math
import os
import subprocess
import tempfile

from driftpin import solvers
from driftpin.analysis import molding_fill as mf


# --- case structure (no solver) ----------------------------------------------

def test_case_files_present_and_named():
    files = mf.cavity_case_files(length_m=0.05, height_m=0.002, depth_m=0.001,
                                 nx=40, ny=6, inject_velocity_m_s=0.5,
                                 end_time_s=0.2)
    for rel in ("system/blockMeshDict", "system/controlDict", "system/fvSchemes",
                "system/fvSolution", "constant/transportProperties",
                "constant/turbulenceProperties", "constant/g",
                "0/alpha.melt", "0/U", "0/p_rgh"):
        assert rel in files, rel
    assert "application     interFoam" in files["system/controlDict"]
    # phases declared, melt phase named, the gate/vent/wall patches present
    assert "phases (melt air)" in files["constant/transportProperties"]
    bm = files["system/blockMeshDict"]
    for patch in ("inlet", "vent", "walls", "frontAndBack"):
        assert patch in bm, patch
    # alpha starts empty (air); inlet injects melt
    assert "internalField   uniform 0" in files["0/alpha.melt"]
    assert "fixedValue; value uniform 1" in files["0/alpha.melt"]


def test_carreau_switches_viscosity_model():
    newt = mf.cavity_case_files()["constant/transportProperties"]
    assert "Newtonian" in newt and "BirdCarreau" not in newt
    car = mf.cavity_case_files(
        carreau={"nu0": 2e-3, "nuInf": 1e-5, "k": 0.5, "n": 0.4}
    )["constant/transportProperties"]
    assert "BirdCarreau" in car and "BirdCarreauCoeffs" in car


def test_blockmeshdict_rejects_bad_geometry():
    for bad in (
        dict(length_m=-1, height_m=0.002, depth_m=0.001, gate_height_m=0.001,
             vent_height_m=0.001, nx=10, ny=4),
        dict(length_m=0.05, height_m=0.002, depth_m=0.001, gate_height_m=0.003,
             vent_height_m=0.001, nx=10, ny=4),     # gate >= height
        dict(length_m=0.05, height_m=0.002, depth_m=0.001, gate_height_m=0.001,
             vent_height_m=0.001, nx=2, ny=4),       # nx too small
    ):
        try:
            mf.cavity_blockmeshdict(**bad)
            assert False, f"expected ValueError for {bad}"
        except ValueError:
            pass


def test_parse_fill_returns_none_without_field():
    d = tempfile.mkdtemp(prefix="mf_empty_")
    mf.write_cavity_case(d, length_m=0.05, height_m=0.002, depth_m=0.001,
                         nx=20, ny=4)
    assert mf.parse_fill(d, nx=20, ny=4, length_m=0.05) is None   # only 0/ exists


def test_fill_gate_short_shot_and_full():
    # synthetic parse_fill dicts straight into the gate
    full = {"filled_fraction": 0.995, "last_to_fill_x_frac": 0.99,
            "front_x_frac": 1.0, "max_pressure_pa": 4.0e7}
    g = mf.fill_gate(full, expected_fill_time_s=0.1)
    assert g["pass"] is True and g["short_shot"] is False
    assert g["fidelity"] == "solve" and g["band_pct"] == 20.0
    assert g["score"] == 0.995

    short = {"filled_fraction": 0.4, "last_to_fill_x_frac": 0.8,
             "front_x_frac": 0.5, "max_pressure_pa": 3.0e7}
    g2 = mf.fill_gate(short, expected_fill_time_s=0.1)
    assert g2["pass"] is False and g2["short_shot"] is True
    assert any("short shot" in w for w in g2["warnings"])

    over = {"filled_fraction": 0.99, "last_to_fill_x_frac": 0.99,
            "front_x_frac": 1.0, "max_pressure_pa": 3.0e8}   # > 180 MPa
    g3 = mf.fill_gate(over, expected_fill_time_s=0.1)
    assert g3["pass"] is False and g3["pressure_ok"] is False


def test_screen_escalates_to_fill_when_fill_check_runs():
    from driftpin.analysis.molding import molding_screen
    cool_only = molding_screen(wall_thickness_mm=2.0, material="ABS")
    assert cool_only["escalate_to"] is None
    with_fill = molding_screen(wall_thickness_mm=2.0, material="ABS",
                               flow_length_mm=200.0)
    assert with_fill["escalate_to"] == "molding_fill_submit"


# --- solver-backed (skips when OpenFOAM is absent) ---------------------------

def _run_fill(case_dir):
    bashrc = solvers.openfoam_bashrc()
    src = f"source '{bashrc}' >/dev/null 2>&1\n" if bashrc else ""
    return subprocess.run(
        ["bash", "-c", src + "blockMesh > log.bm 2>&1 && interFoam > log.if 2>&1"],
        cwd=case_dir, capture_output=True, text=True)


def test_fillable_cavity_reaches_far_end():
    """A short, thin cavity with enough run time fills: the melt front reaches the
    far end (front_x_frac == 1) and the gate passes (no short shot)."""
    if not solvers.is_available("openfoam") and solvers.openfoam_bashrc() is None:
        print("    SKIP — OpenFOAM not installed")
        return
    d = tempfile.mkdtemp(prefix="mf_fill_")
    built = mf.write_cavity_case(
        d, length_m=0.05, height_m=0.002, depth_m=0.001, nx=80, ny=8,
        inject_velocity_m_s=0.5, end_time_s=0.4, deltaT_s=2e-5)
    proc = _run_fill(d)
    assert proc.returncode == 0, proc.stderr[-1500:]
    parsed = mf.parse_fill(d, nx=80, ny=8, length_m=0.05)
    assert parsed is not None, "no converged alpha field"
    assert parsed["front_x_frac"] >= 0.95, parsed
    gate = mf.fill_gate(parsed, expected_fill_time_s=built["expected_fill_time_s"])
    assert gate["pass"] is True, (parsed, gate)
    assert gate["short_shot"] is False


def test_short_shot_cavity_stalls():
    """A long, thin, viscous cavity with too little run time short-shots: the melt
    front does NOT reach the far end and the gate flags a short shot."""
    if not solvers.is_available("openfoam") and solvers.openfoam_bashrc() is None:
        print("    SKIP — OpenFOAM not installed")
        return
    d = tempfile.mkdtemp(prefix="mf_short_")
    built = mf.write_cavity_case(
        d, length_m=0.20, height_m=0.0015, depth_m=0.001, nx=160, ny=6,
        inject_velocity_m_s=0.4, end_time_s=0.12, deltaT_s=2e-5,
        melt_nu_m2_s=5e-3)
    proc = _run_fill(d)
    assert proc.returncode == 0, proc.stderr[-1500:]
    parsed = mf.parse_fill(d, nx=160, ny=6, length_m=0.20)
    assert parsed is not None
    assert parsed["front_x_frac"] < 0.9, parsed     # front did not reach the end
    gate = mf.fill_gate(parsed, expected_fill_time_s=built["expected_fill_time_s"])
    assert gate["pass"] is False and gate["short_shot"] is True, (parsed, gate)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
