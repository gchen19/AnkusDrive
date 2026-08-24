"""Design-for-Cost toys — two-sided oracles for ankusdrive.analysis.cost.

Pure-Python, no FreeCAD. Each check pins a closed-form / handbook cost against a
hand calculation AND verifies a deliberately-bad input is caught, mirroring
tests/TOYS.md and docs/SIMULATION_EXAMPLES.md (family 9).

Run:  python3 tests/test_cost.py
"""
import ast
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive.analysis import cost, materials  # noqa: E402


# --- the MCP-visible signature (issue #238) -----------------------------------
#
# #238 was not a math error but a LAYER error: cost.cost_estimate accepted
# price_usd_kg/density_kg_m3 and the worker handler forwarded **p, but the FastMCP
# wrapper's signature omitted both — so an agent obeying the ValueError's own
# instruction ("pass price_usd_kg") got a schema rejection instead of a price.
# Calling the analysis function directly cannot see that gap. These helpers bind a
# call against the wrapper's parameter list AND the names it forwards to the
# worker, read straight out of ankusdrive/mcp_server.py by ast — so the check stays
# pure-Python (no mcp import, no FreeCAD worker) while still failing if the two
# layers drift apart again.

_MCP = Path(__file__).resolve().parent.parent / "ankusdrive" / "mcp_server.py"


def _mcp_cost_estimate_signature():
    """(parameters the MCP cost_estimate tool accepts, parameters it forwards to
    the worker handler), parsed statically out of mcp_server.py."""
    tree = ast.parse(_MCP.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "cost_estimate")
    accepted = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    forwarded = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_call"
                and node.args
                and getattr(node.args[0], "value", None) == "cost_estimate"):
            forwarded = [kw.arg for kw in node.keywords]
    return accepted, forwarded


def _mcp_cost_estimate(**kwargs):
    """Roll up a cost the way an MCP client can: every kwarg must be a parameter
    the tool exposes and a name it hands to the handler (which calls
    cost.cost_estimate(**p)), else this raises the way the tool's schema would."""
    accepted, forwarded = _mcp_cost_estimate_signature()
    for key in kwargs:
        if key not in accepted:
            raise AssertionError(
                f"MCP cost_estimate does not accept {key!r}; accepts {accepted}")
        if key not in forwarded:
            raise AssertionError(
                f"MCP cost_estimate accepts {key!r} but never forwards it to the worker")
    return cost.cost_estimate(**kwargs)


def test_material_cost_closed_form():
    # AL6061-T6: density 2700 kg/m^3, price 4.5 USD/kg.
    # volume 1e6 mm^3 -> mass 1e6 * 1e-9 * 2700 = 2.7 kg
    # material_cost = 2.7 * 4.5 = 12.15 USD (the exact anchor)
    r = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6")
    assert abs(r["mass_kg"] - 2.7) < 1e-6, r["mass_kg"]
    assert abs(r["material_cost"] - 12.15) < 1e-2, r["material_cost"]
    assert r["breakdown"]["density_basis"] == "material"
    assert r["breakdown"]["price_basis"] == "material"
    # ties to Materials DB §2: material_cost == volume * density * price / 1e9
    expect = 1e6 * 2700.0 * 4.5 * 1e-9
    assert abs(r["material_cost"] - expect) < 1e-2, (r["material_cost"], expect)


def test_scrap_fraction_scales_material_cost():
    # scrap adds (1+scrap_fraction) onto the buy-to-fly material cost
    base = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6")
    scr = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6", scrap_fraction=0.20)
    assert abs(scr["material_cost"] - 1.20 * base["material_cost"]) < 1e-2, scr["material_cost"]
    # deliberately-wrong: pretend scrap is free -> the inflated cost must NOT match base
    assert abs(scr["material_cost"] - base["material_cost"]) > 1e-2


def test_unit_cost_drops_with_quantity_monotonic():
    # With tooling + setup as fixed costs, amortizing over a larger lot must lower
    # the unit cost: 10000-off cheaper than 1-off.
    one = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                             tooling_usd=5000.0, quantity=1)
    many = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                              tooling_usd=5000.0, quantity=10000)
    assert many["unit_cost"] < one["unit_cost"], (one["unit_cost"], many["unit_cost"])
    # tooling amortizes exactly: 5000/1 vs 5000/10000
    assert abs(one["tooling_amortized"] - 5000.0) < 1e-6, one["tooling_amortized"]
    assert abs(many["tooling_amortized"] - 0.5) < 1e-6, many["tooling_amortized"]
    # at high volume the unit cost approaches the per-unit floor (material+machining)
    floor = one["material_cost"] + one["breakdown"]["machining_cost"]
    assert many["unit_cost"] - floor < 1.0, (many["unit_cost"], floor)
    # deliberately-wrong: cost must NOT be flat across quantity when tooling > 0
    assert one["unit_cost"] - many["unit_cost"] > 1.0


def test_unit_cost_is_sum_of_parts():
    r = cost.cost_estimate(volume_mm3=5e5, material="ABS", process="injection",
                           tooling_usd=8000.0, quantity=10000, setup_min=30.0)
    parts = r["material_cost"] + r["process_cost"] + r["tooling_amortized"]
    assert abs(r["unit_cost"] - parts) < 1e-3, (r["unit_cost"], parts)
    # process_cost is itself setup_amortized + machining_cost
    b = r["breakdown"]
    assert abs(r["process_cost"] - (b["setup_amortized"] + b["machining_cost"])) < 1e-3


def test_process_factor_orders_cnc_above_injection():
    # Same part: subtractive CNC must cost more machine time than net-shape injection.
    cnc = cost.cost_estimate(volume_mm3=1e6, material="ABS", process="cnc")
    inj = cost.cost_estimate(volume_mm3=1e6, material="ABS", process="injection")
    assert cnc["breakdown"]["machine_time_hr"] > inj["breakdown"]["machine_time_hr"]
    assert cnc["breakdown"]["machining_cost"] > inj["breakdown"]["machining_cost"]
    # an unknown process is caught (no silent default to cnc)
    try:
        cost.cost_estimate(volume_mm3=1e6, material="ABS", process="wishful")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on an unknown process")


def test_fidelity_contract_labels_rollup_as_correlation():
    # SIMULATION_NEXT.md contract: the process-time table is order-of-magnitude,
    # so the rollup must label itself a focusing estimate, never a gate.
    r = cost.cost_estimate(volume_mm3=1e6, material="ABS", process="cnc")
    assert r["fidelity"] == "correlation", r
    assert r["band_pct"] == 100.0, r["band_pct"]


def test_override_density_and_price():
    # explicit overrides bypass the DB and are flagged in the breakdown
    r = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                           density_kg_m3=1000.0, price_usd_kg=2.0)
    assert abs(r["mass_kg"] - 1.0) < 1e-6, r["mass_kg"]
    assert abs(r["material_cost"] - 2.0) < 1e-2, r["material_cost"]
    assert r["breakdown"]["density_basis"] == "explicit"
    assert r["breakdown"]["price_basis"] == "explicit"


def test_unknown_material_raises():
    # unknown material with no override -> ValueError (no silent default)
    try:
        cost.cost_estimate(volume_mm3=1e6, material="NoSuchAlloy")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown material")
    # an override on one missing property still needs the other -> raise
    try:
        cost.cost_estimate(volume_mm3=1e6, material="NoSuchAlloy", density_kg_m3=2700.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without a usable price")
    # but supplying both overrides lets an unknown material through
    r = cost.cost_estimate(volume_mm3=1e6, material="NoSuchAlloy",
                           density_kg_m3=2700.0, price_usd_kg=4.5)
    assert abs(r["material_cost"] - 12.15) < 1e-2, r["material_cost"]


def test_mcp_signature_takes_the_price_override(): # issue #238, gate half 1
    # 'aluminum' is a CATEGORY in the corpus, not a card: it carries a density but
    # no price. The documented recovery — pass price_usd_kg — has to be reachable
    # through the MCP tool's own signature, which is what #238 found it was not.
    r = _mcp_cost_estimate(volume_mm3=30429, material="aluminum", price_usd_kg=4.5)
    assert r["breakdown"]["price_basis"] == "explicit", r["breakdown"]
    assert abs(r["breakdown"]["price_usd_kg"] - 4.5) < 1e-9, r["breakdown"]
    # only the price was overridden: density still comes from the DB card
    assert r["breakdown"]["density_basis"] == "material", r["breakdown"]
    assert abs(r["mass_kg"] - 30429 * 1e-9 * 2700.0) < 1e-6, r["mass_kg"]
    # the density override is exposed too, so an entirely unknown word also works
    r2 = _mcp_cost_estimate(volume_mm3=30429, material="NoSuchAlloy",
                            density_kg_m3=2700.0, price_usd_kg=4.5)
    assert r2["breakdown"]["density_basis"] == "explicit", r2["breakdown"]
    assert abs(r2["material_cost"] - r["material_cost"]) < 1e-6


def test_missing_price_error_names_both_exits(): # issue #238, gate half 2
    # No price and no override must still raise — but the message has to name the
    # two ways out, or the agent that reads it is stuck: the override, and a real
    # Materials-DB card (via material_list) from the category it typed.
    try:
        _mcp_cost_estimate(volume_mm3=30429, material="aluminum")
    except ValueError as e:
        msg = str(e)
    else:
        raise AssertionError("expected ValueError for a material with no price")
    assert "price_usd_kg" in msg, msg          # exit 1: the override
    assert "material_list" in msg, msg         # exit 2: name a real card
    assert "AL6061-T6" in msg, msg             # ...suggested by category
    # the category is matched fuzzily, so a plausible misspelling recovers too
    try:
        cost.cost_estimate(volume_mm3=30429, material="aluminium")
    except ValueError as e:
        assert "AL6061-T6" in str(e), str(e)
    else:
        raise AssertionError("expected ValueError for a material with no price")


def test_error_suggestions_come_from_the_live_corpus():
    # A suggestion that can't be acted on is worse than none: every card the price
    # error names must actually carry a price, and the list must be derived from
    # the corpus (not hardcoded), so an unmatched word suggests nothing at all.
    try:
        cost.cost_estimate(volume_mm3=1e6, material="aluminum")
    except ValueError as e:
        names = str(e).split("→", 1)[1].strip().rstrip(")").split(", ")
    else:
        raise AssertionError("expected ValueError for a material with no price")
    named = [n for n in names if n != "..."]
    assert named, names
    for name in named:
        card = materials.get(name)             # raises MaterialNotFound if invented
        assert materials.numeric(card, "cost_usd_kg") is not None, name
    # nothing in the corpus resembles this word -> degrade, don't invent
    try:
        cost.cost_estimate(volume_mm3=1e6, material="unobtanium")
    except ValueError as e:
        assert "density_kg_m3" in str(e), str(e)   # the density leg fails first
        assert "material_list" in str(e), str(e)
        assert "→" not in str(e), str(e)
    else:
        raise AssertionError("expected ValueError for an unknown material")


def test_bad_volume_and_quantity_raise():
    try:
        cost.cost_estimate(volume_mm3=0.0, material="AL6061-T6")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on non-positive volume")
    try:
        cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6", quantity=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on quantity < 1")


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
