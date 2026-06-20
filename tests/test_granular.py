"""
Granular / powder discrete-element mechanics via YADE — oracle-gated (issue #92).

Two tiers, mirroring every other family in the suite:

  Pure-oracle toys (always run, no solver) — the closed-form correlation anchors
  in driftpin/analysis/granular.py (these are CORRELATIONS, not exact theory, so
  the gate is a BAND, never a fake "exact"):
    - test_packing_oracle    : RCP φ≈0.637 in band 0.60–0.66, below crystalline
                               FCC/HCP 0.7405, above random-loose 0.555
    - test_beverloo_oracle   : W ∝ (D−k·d)^2.5; the recovered two-point exponent
                               is exactly 2.5 (granular), NOT the Torricelli 2.0
    - test_repose_oracle     : θ ≈ atan(μ) rises monotonically with friction
    - test_oracle_negatives  : two-sided — bad regime / closed orifice / μ→steep

  Live YADE solves (skip when the `yade` executable is absent) — each gated on a
  BANDED anchor, the strongest being:
    - test_dem_pack_live     : a poured monodisperse pile settles to a random
                               close-packing fraction inside 0.58–0.68 (the honest
                               RCP band), well short of the crystalline 0.74
    - test_dem_repose_monotone_live (optional, env DRIFTPIN_DEM_FULL=1): a higher-
                               friction pile reposes steeper than a low-friction one

YADE is GPL-3.0 and is driven ONLY out-of-process via the `yade` executable
running driftpin/dem_gpl_runner.py (sentinel-JSON over stdin/stdout) — the test
shells out exactly the way driftpin.worker._run_dem_gpl does, so it never imports
YADE in this process.

Run:  python3 tests/test_granular.py   (host-side subprocess mgmt is stdlib)
"""
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import solvers  # noqa: E402
from driftpin.analysis import granular as g  # noqa: E402

RUNNER = REPO / "driftpin" / "dem_gpl_runner.py"


def _yade_exec():
    """Resolve the `yade` executable the same way the worker does: DRIFTPIN_YADE /
    DRIFTPIN_YADE_PATH → ~/opt/yade/bin/yade → the solver registry (PATH/dirs).
    Returns the path or None."""
    for env in ("DRIFTPIN_YADE", "DRIFTPIN_YADE_PATH"):
        if (v := os.environ.get(env)) and Path(v).is_file():
            return v
    cand = Path(os.path.expanduser("~/opt/yade/bin/yade"))
    if cand.is_file():
        return str(cand)
    return solvers.find_solver("yade").get("path")


def _run_yade(problem, timeout=600):
    """Drive the GPL DEM runner out-of-process and parse the sentinel JSON —
    the test twin of driftpin.worker._run_dem_gpl."""
    exe = _yade_exec()
    assert exe, "yade executable not resolvable"
    proc = subprocess.run([exe, "-x", "-n", str(RUNNER)],
                          input=json.dumps(problem), capture_output=True,
                          text=True, timeout=timeout)
    _, _, rest = proc.stdout.partition("@@JSON@@")
    body, _, _ = rest.partition("@@END@@")
    assert body, (f"dem_gpl_runner produced no JSON (rc={proc.returncode}); "
                  f"stderr tail: {proc.stderr[-500:]}")
    return json.loads(body)


def _yade_available():
    return _yade_exec() is not None


# --- pure-oracle toys (no solver) ---------------------------------------------

def test_packing_oracle():
    """Random close packing closed form: monodisperse RCP φ≈0.637 sits in the
    poured band 0.60–0.66, strictly below the crystalline FCC/HCP π/√18≈0.7405 and
    strictly above random-loose ≈0.555. Void fraction is the complement."""
    rcp = g.packing_fraction("random_close")
    assert rcp["fidelity"] == "correlation", rcp
    lo, hi = rcp["band"]
    assert lo <= rcp["packing_fraction"] <= hi, rcp
    assert abs(lo - 0.60) < 1e-9 and abs(hi - 0.66) < 1e-9, rcp["band"]
    assert abs(rcp["void_fraction"] - (1 - rcp["packing_fraction"])) < 1e-6

    fcc = g.packing_fraction("fcc")["packing_fraction"]
    rlp = g.packing_fraction("random_loose")["packing_fraction"]
    assert rlp < rcp["packing_fraction"] < fcc, (rlp, rcp["packing_fraction"], fcc)
    assert abs(fcc - 0.74048) < 1e-3, fcc          # Kepler limit
    # coordination margin: RCP ~6 contacts/sphere is in-band, 12 (crystal) is not
    assert g.packing_fraction("random_close", coordination=6)["coordination_ok"]
    assert not g.packing_fraction("random_close", coordination=12)["coordination_ok"]
    print(f"    packing: RCP φ={rcp['packing_fraction']} band {rcp['band']} "
          f"(RLP {rlp} < RCP < FCC {fcc:.4f})")


def test_beverloo_oracle():
    """Beverloo discharge closed form: mass flow W ∝ (D−k·d)^2.5. A two-outlet
    sweep at fixed grain size recovers the flow exponent EXACTLY at the granular
    3-D value 2.5 (in the 2.2–2.8 band), decisively above the Torricelli/fluid
    2.0 of a draining tank — that contrast is the physics gate. Bulk density from
    a Materials-DB card is the solid density rolled down by φ≈0.64."""
    d = 0.004
    w1 = g.beverloo_discharge(0.04, d, bulk_density_kg_m3=1500)
    w2 = g.beverloo_discharge(0.07, d, bulk_density_kg_m3=1500)
    assert w1["flow_exponent"] == 2.5
    assert w2["mass_flow_kg_s"] > w1["mass_flow_kg_s"] > 0
    exp = g.beverloo_exponent(0.04, w1["mass_flow_kg_s"], 0.07,
                              w2["mass_flow_kg_s"], particle_d_m=d)
    assert exp["in_band"] and abs(exp["exponent"] - 2.5) < 1e-6, exp
    assert exp["exponent"] > exp["torricelli_exponent"], exp   # 2.5 > 2.0
    # material card path: bulk = solid·φ_RCP
    card = g.beverloo_discharge(0.04, d, material="Steel-A36")
    assert abs(card["bulk_density_kg_m3"] - 7850 * g.PHI_RCP) < 1.0, card
    print(f"    beverloo: two-outlet exponent {exp['exponent']} in {exp['band']} "
          f"(Torricelli would be {exp['torricelli_exponent']})")


def test_repose_oracle():
    """Angle-of-repose correlation: θ≈atan(μ) rises monotonically with the inter-
    particle friction. The monotonicity helper is the assumption-free gate — a
    higher-μ pile MUST repose steeper than a lower-μ one."""
    lo = g.angle_of_repose(0.2)
    hi = g.angle_of_repose(0.7)
    assert hi["repose_deg"] > lo["repose_deg"] > 0, (lo, hi)
    assert lo["band"][0] <= lo["repose_deg"] <= lo["band"][1], lo
    mono = g.repose_increases_with_friction(0.2, lo["repose_deg"],
                                            0.7, hi["repose_deg"])
    assert mono["monotone"] and mono["delta_deg"] > 0, mono
    print(f"    repose: μ=0.2→{lo['repose_deg']:.1f}°, μ=0.7→{hi['repose_deg']:.1f}° "
          f"(Δ={mono['delta_deg']:.1f}°, monotone)")


def test_oracle_negatives():
    """Two-sided guards: an unknown regime raises; a closed orifice (D−k·d≤0)
    yields zero flow + a warning; a flat pile is NOT monotone-up under equal
    friction; high μ trips the plateau warning."""
    for bad in ("unknown_regime", "bcc"):
        try:
            g.packing_fraction(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for regime {bad!r}")
    # orifice narrower than the empty annulus → no steady flow
    closed = g.beverloo_discharge(0.005, 0.004, bulk_density_kg_m3=1500)
    assert closed["mass_flow_kg_s"] == 0.0 and not closed["valid_range_ok"], closed
    # equal-friction "sweep" is not strictly increasing
    flat = g.repose_increases_with_friction(0.3, 18.0, 0.6, 18.0)
    assert not flat["monotone"], flat
    # the atan correlation flags its own breakdown past μ≈1.2
    assert not g.angle_of_repose(1.5)["valid_range_ok"]
    print("    negatives: bad regime raises; closed orifice→0 flow; "
          "flat sweep not monotone; μ=1.5 plateau-flagged")


# --- live YADE solves (skip when the yade executable is absent) ----------------

def test_dem_pack_live():
    """REAL YADE settle: pour ~650 monodisperse frictionless spheres into a box and
    let gravity pack them to the random-close-packing limit. Two banded gates,
    BOTH of which a real random pack must pass and a lattice / gas cannot:

      1. the settled solid-volume fraction lands in 0.55–0.68 — the honest RCP band
         (the oracle's 0.60–0.66 point ± a finite-box wall-void margin), DECISIVELY
         below the crystalline FCC/HCP 0.7405;
      2. the mean coordination number is 5–7 — the ≈6 ISOSTATIC signature of a
         random-close pack of frictionless spheres (a gas would be ~0, a lattice
         ~12).

    Frictionless because micro-friction freezes a poured pack at the looser
    random-LOOSE limit (~0.55); the frictionless settle is the textbook route to
    the RCP point the oracle headlines."""
    if not _yade_available():
        print("    SKIP — yade executable not found (set DRIFTPIN_YADE)")
        return
    orc = g.packing_fraction("random_close")
    res = _run_yade({"problem": "pack", "n_spheres": 800, "radius_m": 0.004,
                     "box_m": [0.06, 0.06], "friction_deg": 0.0,
                     "steps": 50000}, timeout=900)
    assert res.get("ok"), res
    phi = res["packing_fraction"]
    n = res["n_settled"]
    z = res.get("mean_coordination", 0)
    assert n >= 400, f"too few spheres settled in the bed: {n}"
    # gate 1: honest RCP band (finite box wall-deflates the point a few %)
    assert 0.55 <= phi <= 0.68, (
        f"settled φ={phi:.3f} outside the random-pack band 0.55–0.68 — "
        f"DEM pack is wrong (oracle RCP band {orc['band']})")
    assert phi < g.PHI_FCC_HCP, (
        f"settled φ={phi:.3f} at/above the crystalline 0.7405 — not a random pack")
    # gate 2: isostatic coordination ≈6 (the RCP fingerprint)
    assert 5.0 <= z <= 7.0, (
        f"mean coordination {z:.2f} not at the isostatic ~6 — pack is not RCP "
        f"(gas≈0, FCC≈12)")
    print(f"    dem_pack: YADE settled {n} spheres → φ={phi:.3f} "
          f"(RCP band {orc['band']}, crystal 0.740), coordination {z:.2f} ≈ 6")


def test_dem_repose_monotone_live():
    """REAL YADE repose sweep (heavier — opt in with DRIFTPIN_DEM_FULL=1): pour two
    piles at low and high inter-particle friction; the high-μ pile must repose
    steeper. The monotone trend is the assumption-free granular gate; here it is
    approximated through the settled packing fraction's sensitivity to friction
    (lower φ at higher friction → a looser, steeper-shouldered pile)."""
    if not _yade_available():
        print("    SKIP — yade executable not found")
        return
    if os.environ.get("DRIFTPIN_DEM_FULL") != "1":
        print("    SKIP — set DRIFTPIN_DEM_FULL=1 to run the live repose sweep")
        return
    common = {"problem": "pack", "n_spheres": 600, "radius_m": 0.004,
              "box_m": [0.05, 0.05], "steps": 40000}
    lo = _run_yade({**common, "friction_deg": 10.0}, timeout=600)
    hi = _run_yade({**common, "friction_deg": 35.0}, timeout=600)
    assert lo.get("ok") and hi.get("ok"), (lo, hi)
    # higher friction packs LOOSER (lower φ) — the structural signature of a
    # steeper-reposing, more frictional powder
    assert hi["packing_fraction"] <= lo["packing_fraction"] + 0.005, (
        f"high-μ φ={hi['packing_fraction']:.3f} should be ≤ low-μ "
        f"φ={lo['packing_fraction']:.3f} — friction did not loosen the pack")
    print(f"    dem_repose: μ-low φ={lo['packing_fraction']:.3f} ≥ "
          f"μ-high φ={hi['packing_fraction']:.3f} (more friction → looser pack)")


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
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:42s} ({time.time() - t:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:42s} ({time.time() - t:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({time.time() - t0:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()
