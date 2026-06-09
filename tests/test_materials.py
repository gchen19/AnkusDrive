"""Materials DB toys — two-sided oracles for driftpin.analysis.materials.

Pure-Python, no FreeCAD, no numpy: imports the analysis module directly and
checks it against known answers. Every test is two-sided — the right answer
passes AND a deliberately-wrong input is caught — mirroring tests/TOYS.md.

Covers the §2 toys in docs/SIMULATION_EXAMPLES.md:
  - round-trip: property strings parse to known values; output is a valid FEM card
  - ranking: specific-strength / cost orderings are the known monotone ones
  - empty filter returns nothing (not the closest miss)
  - unknown name raises with a suggestion

Run:  python3 tests/test_materials.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import materials  # noqa: E402


def test_parse_quantity():
    assert materials.parse_quantity("68900 MPa") == (68900.0, "MPa")
    assert materials.parse_quantity("2700 kg/m^3") == (2700.0, "kg/m^3")
    assert materials.parse_quantity("0.33") == (0.33, "")
    v, u = materials.parse_quantity("23.6e-6 1/K")
    assert abs(v - 23.6e-6) < 1e-12 and u == "1/K"
    # negative: garbage must raise, not silently return 0
    try:
        materials.parse_quantity("not-a-number MPa")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on unparseable quantity")


def test_get_roundtrip_and_aliasing():
    c = materials.get("AL6061-T6")
    e, _ = materials.parse_quantity(c["YoungsModulus"])
    rho, _ = materials.parse_quantity(c["Density"])
    assert abs(e - 68900) / 68900 < 0.01, e
    assert abs(rho - 2700) / 2700 < 0.01, rho
    # name normalisation: hyphen/space/case insensitive
    assert materials.get("al 6061 t6")["name"] == "AL6061-T6"


def test_output_is_a_valid_fem_card():
    """The integration claim: material_get output feeds fem_set_material as-is."""
    fem = materials.to_fem_material(materials.get("Steel-A36"))
    assert set(("YoungsModulus", "PoissonRatio", "Density", "Name")) <= set(fem)
    # values are still quantity strings in the FEM convention
    assert fem["YoungsModulus"].endswith("MPa")
    assert fem["Density"].endswith("kg/m^3")
    assert fem["Name"] == "Steel-A36"


def test_unknown_name_suggests():
    try:
        materials.get("AL6061")  # partial / wrong
    except materials.MaterialNotFound as e:
        assert "did_you_mean" in str(e)
    else:
        raise AssertionError("expected MaterialNotFound")


def test_rank_specific_strength_known_order():
    """7075-T6 (high strength, ~same density) must outrank 6061-T6, which must
    outrank pure 1100-O — a known specific-strength ordering."""
    res = materials.select(criteria={"max_density_g_cc": 3.0},
                           rank_by="specific_strength")
    order = [c["name"] for c in res["candidates"]]
    assert order.index("AL7075-T6") < order.index("AL6061-T6") < order.index("AL1100-O"), order
    # scores must be strictly descending (it's the sort key)
    scores = [c["score"] for c in res["candidates"]]
    assert scores == sorted(scores, reverse=True), scores


def test_rank_cost_inverts_toward_commodity():
    """Ranking by cost (cheapest first) must put commodity steel ahead of Ti."""
    res = materials.select(rank_by="cost")
    order = [c["name"] for c in res["candidates"]]
    assert order.index("Steel-A36") < order.index("Ti-6Al-4V"), order


def test_filter_is_a_hard_gate():
    """A min-yield filter must EXCLUDE everything below it, and an impossible
    filter must return an empty set — not the closest miss."""
    res = materials.select(criteria={"min_yield_mpa": 500})
    names = [c["name"] for c in res["candidates"]]
    assert "AL6061-T6" not in names  # 276 MPa < 500, excluded
    assert all(c["yield_mpa"] >= 500 for c in res["candidates"]), names
    impossible = materials.select(criteria={"min_yield_mpa": 99999})
    assert impossible["count"] == 0 and impossible["candidates"] == []


def test_list_by_category():
    al = materials.list_materials(category="aluminum")
    assert al["count"] >= 3
    assert {m["name"] for m in al["materials"]} >= {"AL6061-T6", "AL7075-T6", "AL1100-O"}
    assert all(m["category"] == "aluminum" for m in al["materials"])


def test_optical_property_present():
    """Optical members carry refractive_index for the optics family / corpus."""
    assert abs(materials.numeric(materials.get("PMMA"), "refractive_index") - 1.49) < 0.01
    assert abs(materials.numeric(materials.get("N-BK7"), "refractive_index") - 1.5168) < 1e-6


def test_vendor_optical_merge_preserves_mechanical():
    """The vendor optical layer (optical.json) augments seed cards field-wise:
    N-BK7 keeps its hand-authored Young's modulus AND gains vendor n_d/Abbe."""
    bk7 = materials.get("N-BK7")
    assert "YoungsModulus" in bk7  # from seed.json
    assert bk7.get("optical_source", "").startswith("refractiveindex.info-database")
    assert bk7.get("abbe_number") == "64.17"
    # vendor-only optical materials are present too
    names = {m["name"] for m in materials.list_materials("glass")["materials"]}
    assert {"N-SF11", "F2", "Fused-Silica"} <= names


def test_refractive_index_dispersion():
    """refractive_index_at() reproduces known n via the Sellmeier formula.
    N-BK7 n_d=1.5168; dispersion gives higher n in the blue (486 nm) than red."""
    bk7 = materials.get("N-BK7")
    nd = materials.refractive_index_at(bk7, 587.56)
    nF = materials.refractive_index_at(bk7, 486.13)  # F-line (blue)
    nC = materials.refractive_index_at(bk7, 656.27)  # C-line (red)
    assert abs(nd - 1.5168) < 5e-4, nd
    assert nF > nd > nC, (nF, nd, nC)  # normal dispersion
    # Abbe number reconstructed from the three lines matches the catalog ~64
    abbe = (nd - 1.0) / (nF - nC)
    assert abs(abbe - 64.17) < 1.0, abbe
    # out-of-range wavelength is rejected, not silently extrapolated
    try:
        materials.refractive_index_at(bk7, 100.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError outside fitted range")


def test_bad_criterion_raises():
    for bad in ({"yield_mpa": 100}, {"min_unknownprop": 1}):
        try:
            materials.select(criteria=bad)
        except KeyError:
            pass
        else:
            raise AssertionError(f"expected KeyError for {bad}")


# --- runner (mirrors tests/test_contracts.py) ---------------------------------

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
