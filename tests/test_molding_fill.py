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
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import solvers  # noqa: E402
from driftpin.analysis import molding_fill as mf  # noqa: E402


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


# --- openInjMoldSim (OF7-org) case generation (no solver) --------------------

def test_openinjmoldsim_case_files_structure():
    """The generated OF7-org case has the right files, names openInjMoldSim, and
    bakes in BOTH SHA1 gotchas: NO `#calc`/`#codeStream` anywhere and NO
    `functions{}` functionObject block in controlDict."""
    files = mf.openinjmoldsim_case_files(resin="PS", length_m=0.02, height_m=1e-3,
                                         nx=60, ny=8)
    for rel in ("system/blockMeshDict", "system/controlDict", "system/fvSchemes",
                "system/fvSolution", "system/setFieldsDict",
                "constant/thermophysicalProperties",
                "constant/thermophysicalProperties.poly",
                "constant/thermophysicalProperties.air",
                "constant/solidificationProperties", "constant/turbulenceProperties",
                "constant/g", "0/alpha.poly", "0/U", "0/p", "0/p_rgh", "0/T",
                "0/shrRate"):
        assert rel in files, rel
    assert "application     openInjMoldSim" in files["system/controlDict"]
    # GOTCHA 1: no on-the-fly compiled directives anywhere (SHA1-broken here)
    for rel, text in files.items():
        assert "#calc" not in text, rel
        assert "#codeStream" not in text, rel
    # GOTCHA 2: no functionObject block (probes/libsampling.so aborts the loop)
    assert "functions" not in files["system/controlDict"]
    assert "libsampling" not in files["system/controlDict"]
    # the melt phase is alpha.poly; pressure-driven inlet (uniformFixedValue table)
    assert "phases (poly air)" in files["constant/thermophysicalProperties"]
    assert "uniformValue table" in files["0/p_rgh"]
    assert "fixedValue; value uniform 1" in files["0/alpha.poly"]
    # the two SHA1-prone values are written as LITERALS (no #calc)
    sp = files["constant/solidificationProperties"]
    assert "viscLimEl" in sp and "#calc" not in sp
    # elastic stress OFF by default → viscLimEl ABOVE etaMax (no elSigDev divergence
    # during cooling); elastic=True restores the tutorial's below-etaMax value
    assert "viscLimEl 20000000" in sp                 # etaMax(1e7) * 2
    sp_el = mf.openinjmoldsim_case_files(elastic=True)["constant/solidificationProperties"]
    assert "viscLimEl 5000000" in sp_el               # etaMax(1e7) * 0.5


def test_openinjmoldsim_corpus_drives_cross_wlf_and_tait():
    """The Cross-WLF + 2-domain Tait coefficients come from the #106 corpus: PS and
    HDPE produce DIFFERENT viscosity/PVT cards, and an unknown resin falls back to
    the tutorial-proven PS coefficients (never empty/broken)."""
    ps = mf.openinjmoldsim_case_files(resin="PS")["constant/thermophysicalProperties.poly"]
    hd = mf.openinjmoldsim_case_files(resin="HDPE")["constant/thermophysicalProperties.poly"]
    assert ps != hd                                   # corpus actually consulted
    assert "crossWLF" in ps and "polymerPVT" in ps
    # PS corpus n=0.250, HDPE n=0.330 — the cards reflect the corpus
    assert "n          0.25" in ps
    assert "n          0.33" in hd
    unknown = mf.openinjmoldsim_case_files(resin="not-a-resin-xyz")
    poly = unknown["constant/thermophysicalProperties.poly"]
    assert "D1" in poly and "b1m" in poly             # fell back, still complete


def test_openinjmoldsim_blockmesh_patches_and_2d():
    bm = mf.openinjmoldsim_case_files()["system/blockMeshDict"]
    for patch in ("inlet", "outlet", "walls", "frontAndBack"):
        assert patch in bm, patch
    assert "empty" in bm                              # 2-D: ±z faces are empty


# --- packing / cooling helpers (no solver; issue #113) -----------------------

def test_controldict_starts_from_latest_time():
    """The pack stage resumes from the filled state, so the fill controlDict must use
    `startFrom latestTime` (latestTime is 0 initially, so fill still starts at 0)."""
    cd = mf.openinjmoldsim_case_files()["system/controlDict"]
    assert "startFrom       latestTime" in cd


def test_close_outlet_cmds_seal_and_cool():
    """close_outlet emits the three BC switches that seal the gate and cool through the
    former outlet: p_rgh→fixedFluxPressure, U→fixedValue (0 0 0), T outlet h←walls h."""
    cmds = mf.close_outlet_cmds("0.22")
    flat = [" ".join(c) for c in cmds]
    assert any("0.22/p_rgh" in c and "fixedFluxPressure" in c for c in flat)
    assert any("0.22/U" in c and "fixedValue" in c for c in flat)
    assert any("0.22/U" in c and "(0 0 0)" in c for c in flat)
    # T outlet h is set from the walls' h via a runtime foamDictionary substitution
    assert any("0.22/T" in c and "boundaryField.outlet.h" in c
               and "boundaryField.walls.h" in c for c in flat)


def test_time_extend_and_plan_and_walls_h():
    te = mf.time_extend_cmds(end_time_s=1.0, write_interval_s=0.05, max_deltaT_s=1e-4)
    entries = {c[3] for c in te}
    assert entries == {"endTime", "writeInterval", "maxDeltaT"}
    plan = mf.pack_phase_plan(0.2, n_phases=2, cool_window_s=1.0)
    assert len(plan) == 2
    assert plan[0][0] < plan[1][0]                    # each phase extends further
    assert plan[-1][0] == 0.2 + 1.0                   # spans the cool window
    wh = mf.set_walls_h_cmd("0.22", 1250.0)
    assert wh[1] == "0.22/T" and "walls.h" in wh[3] and wh[-1] == "1250"
    rd = mf.reset_restart_deltaT_cmd("0.22")
    assert "0.22/uniform/time" in rd and rd[3] == "deltaT"


def test_tait_density_and_densification():
    """The 2-domain Tait EOS gives physical PS densities (~970 melt, denser cold) and a
    positive densification on cooling."""
    tait = mf._resin_cross_wlf_tait("PS")[1]
    rho_hot = mf.tait_density(tait, 493.15, 2.0e6)    # 220 C melt
    rho_cold = mf.tait_density(tait, 423.15, 2.0e6)   # 150 C
    assert 900 < rho_hot < 1050 and rho_cold > rho_hot
    d = mf.tait_densification_pct(tait, T_hot_k=493.15, T_cold_k=423.15, p_pa=2.0e6)
    assert 0.3 < d < 6.0                              # a few % volumetric


def test_pack_gate_sink_drives_pass_and_pvt_faithfulness():
    tait = mf._resin_cross_wlf_tait("PS")[1]
    # well-packed (rho_min close to mean) → pass; faithful densification → no warning
    g = mf.pack_gate({"volumetric_shrinkage_pct": 2.0, "rho_mean_final": 1005.0,
                      "rho_min": 990.0, "T_mean_melt": 423.15, "frozen_fraction": 0.8,
                      "residual_pressure_pa": 2.0e6},
                     tait=tait, fill_T_mean_k=493.15)
    assert g["pass"] is True and g["fidelity"] == "solve"
    assert g["score"] > 0.95 and g["sink_risk"] is False
    assert g["expected_densification_pct"] is not None and g["pvt_faithful"] is True
    # a strongly under-packed region (>8% below mean) → sink risk → FAIL
    g2 = mf.pack_gate({"volumetric_shrinkage_pct": 2.0, "rho_mean_final": 1005.0,
                       "rho_min": 880.0, "T_mean_melt": 423.15,
                       "frozen_fraction": 0.8, "residual_pressure_pa": 2.0e6},
                      tait=tait, fill_T_mean_k=493.15)
    assert g2["pass"] is False and g2["sink_risk"] is True and g2["warnings"]
    # solved densification wildly off the EOS → unfaithful warning (but NOT a fail)
    g3 = mf.pack_gate({"volumetric_shrinkage_pct": 9.0, "rho_mean_final": 1005.0,
                       "rho_min": 990.0, "T_mean_melt": 423.15,
                       "frozen_fraction": 0.8, "residual_pressure_pa": 2.0e6},
                      tait=tait, fill_T_mean_k=493.15)
    assert g3["pass"] is True and g3["pvt_faithful"] is False
    assert any("Tait-EOS" in w for w in g3["warnings"])


# --- openInjMoldSim solver-backed fill (skips when the OF7 build is absent) ---

def test_openinjmoldsim_generated_case_fills():
    """The headline path end-to-end: generate an OF7-org openInjMoldSim case from
    corpus PS, run blockMesh→setFields→openInjMoldSim -fillEnd (FOAM_SIGFPE unset),
    and confirm the real Cross-WLF + Tait fill reaches the far end and the gate
    passes. Skips unless the from-source OpenFOAM-7 build is present.

    Slow (~3 min): the violent compressible fill needs a small maxDeltaT. Guarded
    on the binary so it only runs where tools/build_openinjmoldsim.sh has run."""
    binp = solvers.openinjmoldsim_bin()
    bashrc = solvers.openinjmoldsim_bashrc()
    if not binp or not bashrc:
        print("    SKIP — openInjMoldSim (OF7-org) not built")
        return
    d = tempfile.mkdtemp(prefix="oims_fill_test_")
    built = mf.write_openinjmoldsim_case(
        d, resin="PS", length_m=0.02, height_m=1e-3, depth_m=1e-3, nx=60, ny=8,
        peak_pressure_pa=2.0e6)
    script = (f"source '{bashrc}' >/dev/null 2>&1\nunset FOAM_SIGFPE\n"
              "blockMesh > log.bm 2>&1 && setFields > log.sf 2>&1 && "
              f"'{binp}' -fillEnd 0.98 > log.oims 2>&1")
    proc = subprocess.run(["bash", "-c", script], cwd=d, capture_output=True,
                          text=True)
    assert proc.returncode == 0, proc.stderr[-1500:]
    parsed = mf.parse_fill(d, nx=60, ny=8, length_m=0.02)
    assert parsed is not None, "no converged alpha.poly field"
    assert parsed["filled_fraction"] >= 0.9, parsed
    assert parsed["front_x_frac"] >= 0.95, parsed
    gate = mf.fill_gate(parsed, expected_fill_time_s=built["expected_fill_time_s"])
    assert gate["pass"] is True and gate["fidelity"] == "solve", (parsed, gate)


def test_openinjmoldsim_fill_pack_cools_and_densifies():
    """The packing/cooling continuation (issue #113): fill (near-adiabatic, hot) → switch
    walls to cooling + seal the gate → cool. Confirms it runs STABLE (no nan) and the
    melt DENSIFIES on cooling, with the densification faithful to the resin's Tait EOS and
    the gate passing (no sink). Skips unless the OF7 build is present. Slow (~5-6 min)."""
    binp = solvers.openinjmoldsim_bin()
    bashrc = solvers.openinjmoldsim_bashrc()
    if not binp or not bashrc:
        print("    SKIP — openInjMoldSim (OF7-org) not built")
        return
    d = tempfile.mkdtemp(prefix="oims_pack_test_")
    mf.write_openinjmoldsim_case(
        d, resin="PS", length_m=0.02, height_m=1e-3, depth_m=1e-3, nx=60, ny=8,
        peak_pressure_pa=2.0e6, wall_h_w_m2k=1.0)        # fill near-adiabatic

    def run(cmds, log):
        chain = " && ".join(" ".join(a) for a in cmds)
        script = f"source '{bashrc}' >/dev/null 2>&1\nunset FOAM_SIGFPE\n{chain}"
        with open(os.path.join(d, log), "w") as f:
            return subprocess.run(["bash", "-c", script], cwd=d, stdout=f,
                                  stderr=subprocess.STDOUT).returncode

    assert run([["blockMesh"], ["setFields"], [binp, "-fillEnd", "0.98"]],
               "log.fill") == 0
    fe = mf._latest_time_dir(d)
    fstats = mf._melt_stats(d, fe)
    # pack: reset → switch walls to cooling → close outlet → extend + re-run
    cmds = ([mf.reset_restart_deltaT_cmd(fe), mf.set_walls_h_cmd(fe, 1250.0)]
            + mf.close_outlet_cmds(fe))
    for (e, w, m) in mf.pack_phase_plan(float(fe), n_phases=1, cool_window_s=0.6):
        cmds += mf.time_extend_cmds(end_time_s=e, write_interval_s=w, max_deltaT_s=m)
        cmds += [[binp]]
    assert run(cmds, "log.pack") == 0
    with open(os.path.join(d, "log.pack")) as f:
        assert "nan" not in f.read().lower(), "pack diverged (nan in log)"

    ppar = mf.parse_pack(d, fill_rho_mean=fstats["rho_mean"],
                         fill_end_time_s=float(fe))
    assert ppar is not None
    assert ppar["rho_mean_final"] > fstats["rho_mean"]   # densified on cooling
    assert ppar["T_mean_melt"] < fstats["T_mean"]        # cooled
    tait = mf._resin_cross_wlf_tait("PS")[1]
    pg = mf.pack_gate(ppar, tait=tait, fill_T_mean_k=fstats["T_mean"])
    assert pg["fidelity"] == "solve"
    assert pg["pass"] is True and pg["sink_risk"] is False
    assert pg["pvt_faithful"] is True                    # solve tracks its own EOS


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
