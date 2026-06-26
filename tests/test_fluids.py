"""
Fluid / thermophysical property corpus via CoolProp — oracle-gated (issue #100).

Two tiers, mirroring every other family in the suite:

  Always-run (no CoolProp) — the degradation contract + wiring plumbing:
    - test_aliases_and_fallback_shape : name canonicalisation; absent-CoolProp
        air/water serve ≈20 °C constants (ok, fidelity='constant_fallback');
        an unknown fluid with no fallback returns {ok:false, reason, install}.
    - test_validation                 : non-positive T/P raise ValueError.
    - test_wiring_overrides_preserved : cfd._fluid_props / h_estimate keep caller
        overrides winning whether or not CoolProp is the default source.

  CoolProp leg (skipped when the extra is absent) — the physics anchors:
    - test_water_anchor   : water @ 20 °C  ρ≈998, μ≈1.0e-3, cp≈4182, k≈0.598, Pr≈7
    - test_air_anchor     : air @ 25 °C 1 atm  ρ≈1.18, μ≈1.85e-5, Pr≈0.71
    - test_ideal_gas      : air ρ → P/(R·T) at low pressure
    - test_two_sided      : μ and Pr move the right way with T; an out-of-range
        (T,P) trips valid_range_ok=false
    - test_default_source : CoolProp is the DEFAULT behind h_estimate/cfd (the
        screen numbers shift off the hardcoded constants onto the EOS)

Run:  python3 tests/test_fluids.py
"""
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin.analysis import fluids  # noqa: E402
from driftpin.analysis import cfd  # noqa: E402
from driftpin.analysis import convection  # noqa: E402

_HAVE = fluids.available()
_R_AIR = 287.05  # J/kg·K, dry-air gas constant


def _skip(reason):
    print(f"    SKIP: {reason}")


# --- always-run: degradation contract + wiring -------------------------------

def test_aliases_and_fallback_shape():
    """Friendly names canonicalise; the absent-CoolProp path still serves usable
    air/water constants (so the wired screens never hard-break), while an unknown
    fluid with no fallback degrades to {ok:false, reason, install}."""
    assert fluids._canonical("water") == "Water"
    assert fluids._canonical("H2O") == "Water"
    assert fluids._canonical(" Air ") == "Air"
    assert fluids._canonical("r744") == "CO2"

    # air/water always come back populated (EOS when present, constants when not)
    for name, rho_lo, rho_hi in (("water", 950, 1010), ("air", 1.0, 1.3)):
        r = fluids.fluid_props(name, 293.15, 101325.0)
        assert r["ok"], r
        assert rho_lo < r["density"] < rho_hi, r["density"]
        assert r["viscosity"] > 0 and r["cp"] > 0 and r["conductivity"] > 0
        assert r["prandtl"] > 0
        assert "CoolProp" in r["source"] or "fallback" in r["source"], r["source"]
        assert r["coolprop_available"] == _HAVE
        if not _HAVE:
            assert r["fidelity"] == "constant_fallback", r
            assert r["warnings"], "fallback must warn it is not f(T,P)"

    if not _HAVE:
        # a fluid with no constant fallback degrades explicitly
        miss = fluids.fluid_props("R134a", 300.0, 101325.0)
        assert miss["ok"] is False and "install" in miss and "reason" in miss, miss


def test_validation():
    """Non-physical inputs raise (independent of CoolProp)."""
    for bad in (lambda: fluids.fluid_props("water", 0.0),
                lambda: fluids.fluid_props("water", -5.0),
                lambda: fluids.fluid_props("water", 300.0, 0.0),
                lambda: fluids.fluid_props("water", 300.0, -1.0)):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_wiring_overrides_preserved():
    """Caller-supplied properties always win over the default (CoolProp or table)
    source — the explicit-override contract the issue requires."""
    # cfd: explicit μ,ρ override whatever the default source would pick
    mu, rho = cfd._fluid_props("water-20c", 2.5e-3, 1234.0)
    assert mu == 2.5e-3 and rho == 1234.0, (mu, rho)
    # a default lookup still returns sane water numbers
    mu0, rho0 = cfd._fluid_props("water-20c", None, None)
    assert 0.9e-3 < mu0 < 1.1e-3 and 990 < rho0 < 1005, (mu0, rho0)
    # convection: explicit film props override the air default
    r = convection.h_estimate("vertical_plate", 100, 80, 20,
                              fluid="custom", k_w_mk=0.5, nu_m2_s=1e-6,
                              pr=5.0, beta_per_k=3e-4)
    assert abs(r["prandtl"] - 5.0) < 1e-9, r["prandtl"]


# --- CoolProp leg (gated) -----------------------------------------------------

def test_water_anchor():
    """Liquid water at 20 °C, 1 atm against handbook values."""
    if not _HAVE:
        return _skip("CoolProp absent")
    w = fluids.fluid_props("water", 293.15, 101325.0)
    assert w["fidelity"] == "equation_of_state" and w["valid_range_ok"], w
    assert abs(w["density"] - 998.0) / 998.0 < 0.01, w["density"]
    assert abs(w["viscosity"] - 1.0e-3) / 1.0e-3 < 0.05, w["viscosity"]
    assert abs(w["cp"] - 4182.0) / 4182.0 < 0.02, w["cp"]
    assert abs(w["conductivity"] - 0.598) / 0.598 < 0.02, w["conductivity"]
    assert abs(w["prandtl"] - 7.0) / 7.0 < 0.05, w["prandtl"]
    assert "CoolProp" in w["source"], w["source"]
    print(f"    water@20C: rho {w['density']:.1f}, mu {w['viscosity']:.3e}, "
          f"cp {w['cp']:.0f}, k {w['conductivity']:.3f}, Pr {w['prandtl']:.2f}")


def test_air_anchor():
    """Dry air at 25 °C, 1 atm against table values."""
    if not _HAVE:
        return _skip("CoolProp absent")
    a = fluids.fluid_props("air", 298.15, 101325.0)
    assert a["fidelity"] == "equation_of_state" and a["valid_range_ok"], a
    assert abs(a["density"] - 1.18) / 1.18 < 0.02, a["density"]
    assert abs(a["viscosity"] - 1.85e-5) / 1.85e-5 < 0.03, a["viscosity"]
    assert abs(a["prandtl"] - 0.71) / 0.71 < 0.03, a["prandtl"]
    print(f"    air@25C: rho {a['density']:.3f}, mu {a['viscosity']:.3e}, "
          f"Pr {a['prandtl']:.3f}")


def test_ideal_gas():
    """At low pressure real air → ideal gas: ρ tracks P/(R·T)."""
    if not _HAVE:
        return _skip("CoolProp absent")
    T = 300.0
    for P in (1.0e3, 1.0e4, 1.0e5):
        rho = fluids.fluid_props("air", T, P)["density"]
        ideal = P / (_R_AIR * T)
        assert abs(rho - ideal) / ideal < 0.01, (P, rho, ideal)
    # the relative error grows with pressure (real-gas departure)
    e_lo = abs(fluids.fluid_props("air", T, 1.0e3)["density"] - 1.0e3 / (_R_AIR * T))
    e_hi = abs(fluids.fluid_props("air", T, 5.0e6)["density"] - 5.0e6 / (_R_AIR * T))
    assert e_hi / 5.0e6 > e_lo / 1.0e3, "ideal-gas error should grow with P"


def test_two_sided():
    """Property trends with temperature + the out-of-range flag (two-sided)."""
    if not _HAVE:
        return _skip("CoolProp absent")
    # liquid-water viscosity falls steeply as it warms
    mu_cold = fluids.fluid_props("water", 283.15)["viscosity"]
    mu_hot = fluids.fluid_props("water", 343.15)["viscosity"]
    assert mu_hot < 0.6 * mu_cold, (mu_cold, mu_hot)
    # air Prandtl is nearly flat but viscosity rises with T (Sutherland-like)
    assert (fluids.fluid_props("air", 600.0)["viscosity"]
            > fluids.fluid_props("air", 300.0)["viscosity"])
    # an out-of-range (T,P) trips the flag but still returns numbers
    oor = fluids.fluid_props("water", 50.0, 101325.0)  # below Tmin
    assert oor["valid_range_ok"] is False and oor["warnings"], oor
    assert oor["density"] > 0, oor


def test_default_source():
    """CoolProp is the DEFAULT source behind the convection/CFD screens — the
    screen properties come off the EOS, not the legacy hardcoded constants, while
    the result still lands in the same physical ballpark."""
    if not _HAVE:
        return _skip("CoolProp absent")
    # convection air-film props now come from CoolProp at the film temperature
    p = convection._air_film_properties(300.0)
    eos = fluids.fluid_props("air", 300.0, 101325.0)
    assert abs(p["pr"] - eos["prandtl"]) < 1e-9, (p["pr"], eos["prandtl"])
    assert abs(p["nu_m2_s"] - eos["kinematic_viscosity"]) < 1e-12
    # cfd default water props match the EOS (not the rounded table constant)
    mu, rho = cfd._fluid_props("water-20c", None, None)
    w = fluids.fluid_props("water", 293.15, 101325.0)
    assert abs(mu - w["viscosity"]) < 1e-12 and abs(rho - w["density"]) < 1e-9
    # and the h-screen still produces a physically sane coefficient
    r = convection.h_estimate("vertical_plate", 200, 80, 20)
    assert r["valid_range_ok"] and 2 < r["h_conv_w_m2k"] < 20, r["h_conv_w_m2k"]


def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    print(f"CoolProp available: {_HAVE}")
    failures = []
    t0 = time.time()
    for name, fn in _discover():
        t = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:40s} ({time.time() - t:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:40s} ({time.time() - t:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({time.time() - t0:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()
