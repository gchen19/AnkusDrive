"""Typed units / quantity layer for the MCP tool / worker boundary.

Why this module exists (issue #102): FreeCAD object properties have non-obvious
base units. ``App::PropertyForce`` is base **mN** (kg·mm/s²) and
``App::PropertyPressure`` is base **kPa**, so assigning a bare float
(``c.Force = 541.67``) applies the load **1000x too small**. The previous fix
scattered ``f"{x} N"`` strings across handlers — fragile, and easy to forget on
the next constraint added.

This is a lightweight, **FreeCAD-free** quantity layer (no Pint dependency):

  * a tiny unit table per dimension family (force, pressure, length, …) with a
    canonical unit and conversion factors,
  * a :class:`Quantity` that parses unit-bearing inputs (``"541.67 N"``,
    ``"50 MPa"``, ``"300 mm"``) and converts between compatible units,
  * :func:`quantity` — the boundary coercion: a bare float keeps its documented
    canonical unit; a unit-bearing string is parsed and **dimension-validated**
    (a force given in ``"Pa"`` raises instead of silently mis-scaling),
  * :func:`to_freecad` and the :func:`freecad_force` / :func:`freecad_pressure`
    convenience wrappers, which centralize conversion to a FreeCAD property's
    base unit in ONE place — no more per-handler ``f"{x} N"``.

Pure Python, importable without FreeCAD; covered by ``tests/test_units.py``.
"""
from __future__ import annotations

__all__ = [
    "UnitError",
    "Quantity",
    "parse",
    "quantity",
    "convert",
    "to_freecad",
    "freecad_force",
    "freecad_pressure",
    "freecad_length",
    "CANONICAL",
    "FREECAD_BASE",
]


class UnitError(ValueError):
    """Raised when a unit is unknown or its dimension mismatches the expectation."""


# Per-dimension unit tables. Each entry maps a unit symbol to its factor =
# "how many CANONICAL units one of this unit is worth", so
#   value_in_canonical = value * factor.
# The canonical unit (factor 1.0) is the documented bare-float convention for
# that family at the tool boundary (force→N, pressure→MPa, length→mm).
_FAMILIES: dict[str, dict[str, float]] = {
    "force": {
        "N": 1.0,
        "kN": 1.0e3,
        "MN": 1.0e6,
        "mN": 1.0e-3,
        "uN": 1.0e-6,
        "lbf": 4.4482216152605,
        "kgf": 9.80665,
    },
    "pressure": {
        "MPa": 1.0,
        "Pa": 1.0e-6,
        "kPa": 1.0e-3,
        "GPa": 1.0e3,
        "hPa": 1.0e-4,
        "bar": 0.1,
        "psi": 6.894757293168e-3,
        "ksi": 6.894757293168,
    },
    "length": {
        "mm": 1.0,
        "m": 1.0e3,
        "cm": 10.0,
        "dm": 100.0,
        "um": 1.0e-3,
        "µm": 1.0e-3,
        "nm": 1.0e-6,
        "in": 25.4,
        "ft": 304.8,
    },
}

# The canonical (bare-float) unit per family.
CANONICAL: dict[str, str] = {
    "force": "N",
    "pressure": "MPa",
    "length": "mm",
}

# Reverse index: unit symbol -> dimension family. Catches an ambiguous symbol
# being registered in two families at import time.
_UNIT_DIM: dict[str, str] = {}
for _dim, _units in _FAMILIES.items():
    for _u in _units:
        if _u in _UNIT_DIM:
            raise RuntimeError(f"unit {_u!r} registered in two families")
        _UNIT_DIM[_u] = _dim

# FreeCAD property type -> (dimension, base unit). The base unit is the property's
# raw internal unit; assigning a value already expressed in this unit is
# unambiguous and immune to the mN/kPa 1000x trap.
FREECAD_BASE: dict[str, tuple[str, str]] = {
    "App::PropertyForce": ("force", "mN"),
    "App::PropertyPressure": ("pressure", "kPa"),
    "App::PropertyLength": ("length", "mm"),
    "App::PropertyDistance": ("length", "mm"),
}


def _dimension_of(unit: str) -> str:
    try:
        return _UNIT_DIM[unit]
    except KeyError:
        raise UnitError(
            f"unknown unit {unit!r}; known units: "
            f"{', '.join(sorted(_UNIT_DIM))}"
        ) from None


def _factor(unit: str, dimension: str) -> float:
    table = _FAMILIES[dimension]
    if unit not in table:
        raise UnitError(
            f"unit {unit!r} is not a {dimension} unit "
            f"(it is {_dimension_of(unit)}); expected one of: "
            f"{', '.join(sorted(table))}"
        )
    return table[unit]


def parse(s) -> tuple[float, str]:
    """Split a quantity into ``(value, unit)``. ``"541.67 N"`` -> (541.67, "N");
    ``"50MPa"`` -> (50.0, "MPa"); a bare number -> ``(value, "")``. Accepts an
    already-numeric input. Raises :class:`UnitError` on an unparseable value."""
    if isinstance(s, bool):  # bool is an int subclass; reject to avoid surprises
        raise UnitError(f"cannot parse quantity from bool {s!r}")
    if isinstance(s, (int, float)):
        return float(s), ""
    if s is None:
        raise UnitError("cannot parse quantity from None")
    text = str(s).strip()
    if not text:
        raise UnitError("cannot parse quantity from empty string")
    # Split on the first whitespace; tolerate "50MPa" (no space) by peeling the
    # leading numeric run off the front.
    parts = text.split(None, 1)
    head = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    try:
        value = float(head)
    except ValueError:
        i = 0
        while i < len(head) and (head[i].isdigit() or head[i] in "+-.eE"):
            i += 1
        num, tail = head[:i], head[i:]
        try:
            value = float(num)
        except ValueError:
            raise UnitError(f"unparseable quantity: {s!r}") from None
        rest = (tail + (" " + rest if rest else "")).strip()
    return value, rest


class Quantity:
    """A value with a unit and a known dimension. Immutable in practice."""

    __slots__ = ("value", "unit", "dimension")

    def __init__(self, value: float, unit: str, dimension: str | None = None):
        self.value = float(value)
        self.unit = unit
        self.dimension = dimension or _dimension_of(unit)
        # Validate that unit belongs to the (declared) dimension.
        _factor(unit, self.dimension)

    def magnitude(self, unit: str) -> float:
        """This quantity's numeric value expressed in ``unit`` (same dimension)."""
        target = _factor(unit, self.dimension)
        return self.value * _factor(self.unit, self.dimension) / target

    def to(self, unit: str) -> "Quantity":
        """A new :class:`Quantity` converted to ``unit`` (same dimension)."""
        return Quantity(self.magnitude(unit), unit, self.dimension)

    @property
    def canonical(self) -> float:
        """Numeric value in this family's canonical unit (see :data:`CANONICAL`)."""
        return self.magnitude(CANONICAL[self.dimension])

    def __repr__(self) -> str:
        return f"Quantity({self.value!r}, {self.unit!r})"

    def __eq__(self, other) -> bool:
        if not isinstance(other, Quantity):
            return NotImplemented
        if self.dimension != other.dimension:
            return False
        return abs(self.canonical - other.canonical) <= 1e-12 * max(
            1.0, abs(self.canonical), abs(other.canonical)
        )


def quantity(value, dimension: str, default_unit: str | None = None) -> Quantity:
    """Coerce a tool-boundary input into a :class:`Quantity` of ``dimension``.

    A bare float (or unit-less string) keeps the documented canonical unit for
    the family (force→N, pressure→MPa, length→mm), unless ``default_unit`` is
    given. A unit-bearing string is parsed and its dimension validated: a unit
    from another family (e.g. ``"Pa"`` where a force is expected) raises
    :class:`UnitError` rather than silently mis-scaling. Already-:class:`Quantity`
    inputs are validated and passed through.
    """
    if dimension not in _FAMILIES:
        raise UnitError(f"unknown dimension {dimension!r}")
    canon = default_unit or CANONICAL[dimension]
    _factor(canon, dimension)  # validate the assumed unit is in-family
    if isinstance(value, Quantity):
        if value.dimension != dimension:
            raise UnitError(
                f"expected a {dimension} quantity, got {value.dimension} "
                f"({value!r})"
            )
        return value
    val, unit = parse(value)
    if not unit:
        return Quantity(val, canon, dimension)
    got_dim = _dimension_of(unit)
    if got_dim != dimension:
        raise UnitError(
            f"expected a {dimension} quantity (unit like {canon!r}), but got "
            f"{unit!r} which is a {got_dim} unit — refusing to mis-scale {value!r}"
        )
    return Quantity(val, unit, dimension)


def convert(value, from_unit: str, to_unit: str) -> float:
    """Convert a numeric ``value`` from ``from_unit`` to ``to_unit`` (compatible
    dimensions). Raises :class:`UnitError` if the dimensions differ."""
    dim = _dimension_of(from_unit)
    if _dimension_of(to_unit) != dim:
        raise UnitError(
            f"cannot convert {from_unit!r} ({dim}) to {to_unit!r} "
            f"({_dimension_of(to_unit)})"
        )
    return Quantity(value, from_unit, dim).magnitude(to_unit)


def _fmt(x: float) -> str:
    # Trim float noise (e.g. 541670.00000000006 -> 541670) without losing real
    # precision for typical force/pressure/length magnitudes.
    return f"{x:.12g}"


def to_freecad(value, fc_property: str, default_unit: str | None = None) -> str:
    """Return a FreeCAD quantity string for ``fc_property``, expressed in that
    property's raw base unit (mN for force, kPa for pressure, …).

    This is the single conversion seam: ``c.Force = to_freecad(541.67,
    "App::PropertyForce")`` -> ``"541670 mN"`` (correct magnitude), so the
    base-unit 1000x trap cannot recur. ``value`` may be a bare float in the
    canonical unit, a unit-bearing string, or a :class:`Quantity`.
    """
    try:
        dimension, base_unit = FREECAD_BASE[fc_property]
    except KeyError:
        raise UnitError(
            f"no FreeCAD base unit registered for {fc_property!r}; "
            f"known: {', '.join(sorted(FREECAD_BASE))}"
        ) from None
    q = quantity(value, dimension, default_unit=default_unit)
    return f"{_fmt(q.magnitude(base_unit))} {base_unit}"


def freecad_force(value) -> str:
    """FreeCAD ``App::PropertyForce`` string in base mN. Bare float assumed N."""
    return to_freecad(value, "App::PropertyForce")


def freecad_pressure(value) -> str:
    """FreeCAD ``App::PropertyPressure`` string in base kPa. Bare float assumed MPa."""
    return to_freecad(value, "App::PropertyPressure")


def freecad_length(value) -> str:
    """FreeCAD length string in base mm. Bare float assumed mm."""
    return to_freecad(value, "App::PropertyLength")
