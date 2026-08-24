"""Typed units / quantity layer — two-sided oracles for ankusdrive.units (issue #102).

Pure-Python, no FreeCAD: imports the units module directly and checks it against
known answers. Every test is two-sided — the right answer passes AND a
deliberately-wrong input is caught — mirroring tests/TOYS.md and
tests/test_materials.py.

This guards the **PropertyForce=mN 1000x bug class**: FreeCAD's App::PropertyForce
is base mN and App::PropertyPressure is base kPa, so a bare-float assignment
applies the load 1000x too small. The conversion seam (freecad_force /
freecad_pressure) is exercised here so the magnitude bug fails at the unit layer,
without needing a live FreeCAD/ccx solve.

Assertions are marked [pure-oracle] (run here, no FreeCAD) vs [live-FEM] (would
need FreeCAD+ccx; described but NOT run in this environment).

Run:  python3 tests/test_units.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive import units  # noqa: E402


# --- parsing -----------------------------------------------------------------

def test_parse_value_and_unit():
    """[pure-oracle] parse() splits value/unit; bare numbers parse unit-less."""
    assert units.parse("541.67 N") == (541.67, "N")
    assert units.parse("50 MPa") == (50.0, "MPa")
    assert units.parse("300 mm") == (300.0, "mm")
    assert units.parse("50MPa") == (50.0, "MPa")          # no space
    assert units.parse(541.67) == (541.67, "")            # already numeric
    assert units.parse("0.33") == (0.33, "")
    v, u = units.parse("23.6e-6 1/K".split(" 1")[0] + " m")  # sci notation value
    assert abs(v - 23.6e-6) < 1e-12 and u == "m"
    # negative: garbage must raise, not silently return 0
    for bad in ("not-a-number N", None, "", "  "):
        try:
            units.parse(bad)
        except units.UnitError:
            pass
        else:
            raise AssertionError(f"expected UnitError on {bad!r}")
    # bool is not a quantity
    try:
        units.parse(True)
    except units.UnitError:
        pass
    else:
        raise AssertionError("expected UnitError on bool")


# --- round-trip conversions (force / pressure / length) ----------------------

def test_roundtrip_force_exact():
    """[pure-oracle] Force family round-trips exactly across the units used."""
    assert units.convert(1.0, "N", "mN") == 1000.0
    assert units.convert(1.0, "kN", "N") == 1000.0
    assert units.convert(1000.0, "mN", "N") == 1.0
    assert units.convert(1.0, "MN", "N") == 1.0e6
    # canonical of a Quantity
    assert units.Quantity(2.0, "kN").canonical == 2000.0  # canonical force = N
    # round trip there-and-back is identity
    for u in ("N", "kN", "mN", "MN", "lbf", "kgf"):
        back = units.convert(units.convert(123.0, "N", u), u, "N")
        assert abs(back - 123.0) < 1e-9, (u, back)


def test_roundtrip_pressure_exact():
    """[pure-oracle] Pressure family round-trips; MPa is canonical."""
    assert units.convert(1.0, "MPa", "kPa") == 1000.0
    assert units.convert(1.0, "MPa", "Pa") == 1.0e6
    assert units.convert(1.0, "GPa", "MPa") == 1000.0
    assert units.convert(1.0, "bar", "kPa") == 100.0
    for u in ("MPa", "Pa", "kPa", "GPa", "bar", "psi", "ksi"):
        back = units.convert(units.convert(45.0, "MPa", u), u, "MPa")
        assert abs(back - 45.0) < 1e-9, (u, back)


def test_roundtrip_length_exact():
    """[pure-oracle] Length family round-trips; mm is canonical."""
    assert units.convert(1.0, "m", "mm") == 1000.0
    assert units.convert(25.4, "mm", "in") == 1.0
    assert units.convert(1.0, "in", "mm") == 25.4
    assert units.convert(1.0, "ft", "mm") == 304.8
    for u in ("mm", "m", "cm", "um", "in", "ft"):
        back = units.convert(units.convert(72.0, "mm", u), u, "mm")
        assert abs(back - 72.0) < 1e-9, (u, back)


# --- boundary coercion (bare float keeps canonical; strings validated) -------

def test_quantity_bare_float_keeps_canonical():
    """[pure-oracle] Backward compat: a bare float keeps the documented unit."""
    assert units.quantity(541.67, "force").unit == "N"
    assert units.quantity(541.67, "force").canonical == 541.67
    assert units.quantity(50, "pressure").unit == "MPa"
    assert units.quantity(300, "length").unit == "mm"
    # a unit-bearing string of the right family is parsed and converted
    assert abs(units.quantity("2 kN", "force").canonical - 2000.0) < 1e-9
    # a Quantity passes through (and is dimension-checked)
    q = units.Quantity(5.0, "N")
    assert units.quantity(q, "force") is q


def test_wrong_unit_raises_not_misscale():
    """[pure-oracle] ACCEPTANCE: a force given in 'Pa' (a pressure unit) raises
    rather than silently mis-scaling. Same for an unknown unit / wrong family."""
    for bad in ("100 Pa", "5 MPa", "10 psi"):
        try:
            units.quantity(bad, "force")
        except units.UnitError:
            pass
        else:
            raise AssertionError(f"force from {bad!r} should raise")
    # a length given where pressure is expected
    try:
        units.quantity("3 mm", "pressure")
    except units.UnitError:
        pass
    else:
        raise AssertionError("pressure from a length should raise")
    # outright unknown unit
    try:
        units.quantity("5 furlongs", "length")
    except units.UnitError:
        pass
    else:
        raise AssertionError("unknown unit should raise")
    # cross-dimension convert() is also rejected
    try:
        units.convert(1.0, "N", "MPa")
    except units.UnitError:
        pass
    else:
        raise AssertionError("converting force->pressure should raise")


# --- the magnitude bug guard at the FreeCAD base-unit seam -------------------

def test_freecad_force_base_unit_mN():
    """[pure-oracle] ACCEPTANCE / #94 regression: 541.67 N must land at 541670 mN
    (App::PropertyForce base unit), NOT 541.67 (which is the 1000x-too-small bug)."""
    s = units.freecad_force(541.67)
    val, unit = units.parse(s)
    assert unit == "mN", s
    assert abs(val - 541670.0) < 1e-6, s
    # the bug magnitude (541.67 mN) is exactly what we must NOT produce
    assert abs(val - 541.67) > 1.0, s
    # a unit-bearing input is honoured: 0.54167 kN -> the same 541670 mN
    val2, _ = units.parse(units.freecad_force("0.54167 kN"))
    assert abs(val2 - 541670.0) < 1e-6
    # default 9000 N (the cantilever demo load) -> 9_000_000 mN
    val3, _ = units.parse(units.freecad_force(9000.0))
    assert abs(val3 - 9.0e6) < 1e-3


def test_freecad_pressure_base_unit_kPa():
    """[pure-oracle] 50 MPa must land at 50000 kPa (App::PropertyPressure base
    unit), NOT 50 (the 1000x-too-small bug)."""
    s = units.freecad_pressure(50.0)
    val, unit = units.parse(s)
    assert unit == "kPa", s
    assert abs(val - 50000.0) < 1e-6, s
    assert abs(val - 50.0) > 1.0, s
    # a Pa-valued input still converts correctly: 50e6 Pa -> 50000 kPa
    val2, _ = units.parse(units.freecad_pressure("50000000 Pa"))
    assert abs(val2 - 50000.0) < 1e-3
    # and a wrong-dimension input is refused at this seam too
    try:
        units.freecad_pressure("541.67 N")
    except units.UnitError:
        pass
    else:
        raise AssertionError("pressure from a force unit should raise")


def test_to_freecad_unknown_property_raises():
    """[pure-oracle] to_freecad() only knows registered FreeCAD property types."""
    try:
        units.to_freecad(1.0, "App::PropertyBogus")
    except units.UnitError:
        pass
    else:
        raise AssertionError("unknown FreeCAD property should raise")


# --- physics-level acceptance (described; gated as live-FEM) -----------------

def test_cantilever_reaction_equals_applied_force():
    """[live-FEM, NOT run here] A tip-loaded cantilever's reaction / total CLOAD
    must equal the APPLIED force in N. The #94 bug (bare float -> mN) would put
    the reaction at 1/1000 of the load. We assert the upstream invariant the live
    solve depends on: the value the worker writes to App::PropertyForce is the
    applied force expressed in base mN, i.e. force_in_N * 1000.

    Running ccx here is too heavy/slow, so this is the conversion-seam proxy for
    the live reaction == applied-force check (see test docstring header)."""
    for F_newton in (1.0, 9000.0, 541.67):
        mN, _ = units.parse(units.freecad_force(F_newton))
        # FreeCAD will integrate this base-mN value; back in N it is the load.
        assert abs(mN / 1000.0 - F_newton) < 1e-6, (F_newton, mN)


def test_uniaxial_pressure_equals_stress():
    """[live-FEM, NOT run here] For a uniaxial bar pulled by pressure p on its end
    face, σ = p. The bug would give σ = p/1000. Conversion-seam proxy: the value
    written to App::PropertyPressure equals p expressed in base kPa = p_MPa*1000,
    so σ recovered in MPa equals p_MPa."""
    for p_mpa in (50.0, 1.0, 123.45):
        kPa, _ = units.parse(units.freecad_pressure(p_mpa))
        assert abs(kPa / 1000.0 - p_mpa) < 1e-6, (p_mpa, kPa)


# --- runner (mirrors tests/test_materials.py) --------------------------------

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
            print(f"  FAIL {name:52s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:52s} ({time.time() - t0:.2f}s)")

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
