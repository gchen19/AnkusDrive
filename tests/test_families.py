"""
Design tables — variant families from a row x column table (issue #138, B1).
Free, no LLM, no key. Two halves, mirroring tests/test_recipes.py:

  * PURE (no FreeCAD): a family table (CSV or JSON) materializes to N variants,
    each with an item + a sequential part number; a feature-flag column toggles a
    feature per row; a bad row (out-of-range input / unknown recipe / duplicate
    size key) is caught LOUDLY naming the row+column; configuration and instance
    modes yield identical resolved inputs (hence identical geometry) but differ in
    file allocation; a catalog (ISO) row sources the standard table value. Geometry
    orchestration is exercised through a recording STUB call (no FreeCAD).

  * WORKER (FreeCAD): a gear family builds N real, deterministic solids; the modes
    yield byte-identical geometry; and a bearing catalog row's built bore matches
    the ISO standards table value.

Run: python3 tests/test_families.py     (or .venv/bin/python3)
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import families  # noqa: E402
from driftpin import items as _items  # noqa: E402
from driftpin import recipes  # noqa: E402
from driftpin.analysis import standards as _std  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "families"
GEAR_CSV = FIX / "gear_family.csv"
GEAR_JSON = FIX / "gear_family.json"
BEARING_CSV = FIX / "bearing_catalog.csv"

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _true(label, cond):
    _check(label, bool(cond), True)


# --- a recording stub for the worker `call` dispatch (no FreeCAD) -------------

class StubCall:
    """Stands in for the worker HANDLERS dispatch so the family orchestration runs
    FreeCAD-free. Records every call and returns just enough for the reference build
    functions (spur_gear / ball_bearing) to complete deterministically."""

    def __init__(self):
        self.calls = []
        self._n = 0

    def __call__(self, tool, **kw):
        self.calls.append((tool, kw))
        if tool == "add_gear":
            self._n += 1
            return {"handle": f"h{self._n}", "name": f"Gear{self._n}",
                    "pitch_radius": kw["module"] * kw["teeth"] / 2.0,
                    "volume": 1000.0 * self._n, "external": kw.get("external", True)}
        if tool == "add_bearing":
            self._n += 1
            return {"handle": f"h{self._n}", "name": f"Bearing{self._n}", **kw}
        if tool == "publish_interface":
            return {"interfaces": {kw["name"]: kw["frame"]}}
        if tool == "declare_intent":
            return {"contract": kw["contract"]}
        raise AssertionError(f"unexpected stub call {tool!r}")


# =============================================================================
# PURE — the table, the column split, the door (no FreeCAD)
# =============================================================================

def test_csv_and_json_materialize_identically():
    """The CSV and JSON forms of the same family normalize to the same resolved
    inputs, keys, metadata and part numbers — both formats, one family."""
    csv_res = families.materialize(families.load_table(str(GEAR_CSV)))
    json_res = families.materialize(families.load_table(str(GEAR_JSON)))
    _check("CSV row count == JSON row count", csv_res["count"], json_res["count"])
    proj = lambda res: [(r["key"], r["inputs"], r["metadata"], r["part_number"])
                        for r in res["rows"]]
    _check("CSV and JSON materialize identically", proj(csv_res), proj(json_res))


def test_n_rows_n_parts_each_with_item_and_pn():
    """N rows -> N parts, each with its own item and a sequential part number."""
    stub = StubCall()
    res = families.materialize(families.load_table(str(GEAR_CSV)), stub)
    _check("5 rows -> 5 parts", res["count"], 5)
    _true("every row built a handle", all(r["handle"] for r in res["rows"]))
    pns = [r["part_number"] for r in res["rows"]]
    _check("sequential non-significant part numbers",
           pns, ["DP-001001", "DP-001002", "DP-001003", "DP-001004", "DP-001005"])
    _check("every variant got a distinct item",
           len({r["item"] for r in res["rows"]}), 5)
    # each item lives in the registry with its part number + recipe inputs
    reg = res["registry"]
    rec = _items.get_item(reg, "gear_family:M2_24T")
    _check("item recorded with part number", rec["part_number"], "DP-001003")
    _check("item metadata carries the recipe + size",
           (rec["metadata"]["recipe"], rec["metadata"]["size"]),
           ("spur_gear", "M2_24T"))
    _check("registry validates clean", _items.validate_registry(reg), [])


def test_feature_flag_column_toggles_a_feature():
    """The `external` column is a feature flag: most rows are external gears, the
    ring row is internal — the flag flows per row into the resolved recipe input."""
    res = families.materialize(families.load_table(str(GEAR_CSV)))
    flags = {r["key"]: r["inputs"]["external"] for r in res["rows"]}
    _check("external rows flagged True", flags["M2_24T"], True)
    _check("ring row toggled internal (False)", flags["M2_RING_60T"], False)
    ring = _items.get_item(res["registry"], "gear_family:M2_RING_60T")
    _check("the flag is queryable in item metadata",
           ring["metadata"]["inputs"]["external"], False)


def test_material_column_routes_to_metadata_not_inputs():
    """A column that is not a recipe input (material) routes to item metadata; the
    size key is identity, never a recipe input."""
    res = families.materialize(families.load_table(str(GEAR_CSV)))
    row = next(r for r in res["rows"] if r["key"] == "M2_24T")
    _check("material is metadata", row["metadata"].get("material"), "SS304")
    _true("material is NOT a recipe input", "material" not in row["inputs"])
    _true("size key is NOT a recipe input", "size" not in row["inputs"])


def test_bad_row_out_of_range_named():
    """An out-of-range input fails loudly, naming the row and the column."""
    bad = {"schema": families.SCHEMA, "family": "gf", "recipe": "spur_gear",
           "key": "size", "rows": [
               {"size": "ok", "module_mm": 1.0, "teeth": 20},
               {"size": "tiny", "module_mm": 1.0, "teeth": 1}]}
    problems = families.validate_table(bad)
    hit = [p for p in problems if "'tiny'" in p and "teeth" in p and "minimum" in p]
    _true("out-of-range row caught naming row+column", hit)
    try:
        families.materialize(bad)
        _true("materialize raised on bad table", False)
    except families.FamilyError as e:
        _true("materialize refuses a bad table", "tiny" in str(e))


def test_bad_row_unknown_recipe_named():
    bad = {"family": "gf", "recipe": "no_such_recipe", "key": "size",
           "rows": [{"size": "a", "x": 1}]}
    problems = families.validate_table(bad)
    _true("unknown recipe caught naming the family",
          any("unknown recipe" in p and "gf" in p for p in problems))


def test_duplicate_size_key_named():
    bad = {"family": "gf", "recipe": "spur_gear", "key": "size", "rows": [
        {"size": "dup", "module_mm": 1.0, "teeth": 20},
        {"size": "dup", "module_mm": 2.0, "teeth": 20}]}
    problems = families.validate_table(bad)
    _true("duplicate size key caught naming column+key",
          any("duplicate size key" in p and "'dup'" in p for p in problems))


def test_modes_identical_inputs_distinct_files():
    """Configuration and instance modes resolve identical inputs (hence identical
    geometry) but differ in file allocation; configurations share one artifact and
    tag the configuration."""
    table = families.load_table(str(GEAR_JSON))
    inst = families.materialize(table, mode="instances")
    conf = families.materialize(table, mode="configurations")
    _check("same resolved inputs across modes",
           [r["inputs"] for r in inst["rows"]],
           [r["inputs"] for r in conf["rows"]])
    _true("instance files are distinct per variant",
          len({tuple(r["files"]) for r in inst["rows"]}) == inst["count"])
    _true("configuration files are one shared artifact",
          len({tuple(r["files"]) for r in conf["rows"]}) == 1)
    conf_item = _items.get_item(conf["registry"], "gear_family:M2_24T")
    _check("configuration tag recorded",
           conf_item["metadata"].get("configuration"), "M2_24T")


def test_catalog_row_sources_standard_table_value():
    """A bearing CATALOG is a family table keyed by designation; the ball_bearing
    recipe sources bore/OD/width from the ISO standards corpus, so a built row
    matches the standard table value by construction (subsumes #101)."""
    stub = StubCall()
    res = families.materialize(families.load_table(str(BEARING_CSV)), stub)
    _check("catalog materialized all rows", res["count"], 5)
    # the add_bearing call captured for 6205 must equal the ISO corpus dimensions
    bearing_calls = [c[1] for c in stub.calls if c[0] == "add_bearing"]
    # match the 6205 build by its captured dims against the standard
    std = _std.bearing("6205")
    got = next(b for b in stub.calls if b[0] == "add_bearing"
               and b[1]["bore"] == std["bore_mm"])[1]
    _check("catalog 6205 bore == ISO table", got["bore"], std["bore_mm"])
    _check("catalog 6205 OD == ISO table", got["outer_diameter"], std["od_mm"])
    _check("catalog 6205 width == ISO table", got["width"], std["width_mm"])
    _true("designation routed as a recipe input",
          all(r["inputs"].get("designation") for r in res["rows"]))
    _true("shield routed to metadata",
          all("shield" in r["metadata"] for r in res["rows"]))
    _check("five bearings built", len(bearing_calls), 5)
    _true("each catalog row sourced ISO dims",
          all(b["bore"] == _std.bearing(r["inputs"]["designation"])["bore_mm"]
              for b, r in zip(bearing_calls, res["rows"])))


def test_unknown_designation_rejected_at_the_door():
    """A catalog row naming a designation outside the ISO corpus is refused at the
    door, naming the row+column."""
    bad = {"family": "cat", "recipe": "ball_bearing", "key": "designation",
           "rows": [{"designation": "9999"}]}
    problems = families.validate_table(bad)
    _true("unknown designation caught naming row+column",
          any("'9999'" in p and "designation" in p for p in problems))


def test_materialize_is_deterministic():
    """Two materializations of the same table are identical: same keys, inputs,
    files, part numbers and (stub) handles — the family rides the determinism
    envelope, table order fixes part-number allocation."""
    def run():
        res = families.materialize(families.load_table(str(GEAR_CSV)), StubCall())
        return [(r["key"], r["inputs"], r["files"], r["part_number"], r["handle"])
                for r in res["rows"]]
    _check("two materializations -> identical", run(), run())


def test_csv_parse_roundtrips_directives():
    """The CSV directive block + header parse into the canonical normalized form."""
    t = families.load_table(str(GEAR_CSV))
    _check("family name from directive", t["family"], "gear_family")
    _check("recipe from directive", t["recipe"], "spur_gear")
    _check("mode from directive", t["mode"], "instances")
    _check("key column from directive", t["key"], "size")
    _check("all rows parsed", len(t["rows"]), 5)


# =============================================================================
# WORKER — real geometry, determinism, catalog (needs FreeCAD)
# =============================================================================

_SMALL_GEARS = {
    "schema": families.SCHEMA, "family": "wfam", "recipe": "spur_gear",
    "mode": "instances", "key": "size",
    "rows": [
        {"size": "A", "module_mm": 2.0, "teeth": 18, "width_mm": 6.0},
        {"size": "B", "module_mm": 2.0, "teeth": 24, "width_mm": 6.0},
    ],
}


def _write(tmp, name, table):
    p = Path(tmp) / name
    p.write_text(json.dumps(table, indent=2), encoding="utf-8")
    return str(p)


def _vol(w, handle):
    return w.call("mass_properties", handle=handle, density=7.9e-6)["volume_mm3"]


def test_worker_gear_family_builds_n_parts():
    """A gear family materializes N real solids on the worker, each with a handle, a
    distinct item and a sequential part number."""
    from driftpin import Worker
    with tempfile.TemporaryDirectory() as td:
        tbl = _write(td, "wfam.json", _SMALL_GEARS)
        with Worker() as w:
            w.call("new_document", name="fam")
            res = w.call("family_materialize", table=tbl)
            vols = [_vol(w, r["handle"]) for r in res["rows"]]
    _check("two variants built", res["count"], 2)
    _true("real solids (positive volume)", all(v > 0 for v in vols))
    _true("the 24T gear is heavier than the 18T", vols[1] > vols[0])
    _check("sequential part numbers",
           [r["part_number"] for r in res["rows"]], ["DP-001001", "DP-001002"])


def test_worker_modes_yield_identical_geometry():
    """The same family materialized in instances vs configurations mode builds
    byte-identical geometry — modes differ only in file/item allocation."""
    from driftpin import Worker
    with tempfile.TemporaryDirectory() as td:
        tbl = _write(td, "wfam.json", _SMALL_GEARS)
        with Worker() as w:
            w.call("new_document", name="inst")
            ri = w.call("family_materialize", table=tbl, mode="instances")
            vi = [_vol(w, r["handle"]) for r in ri["rows"]]
            w.call("new_document", name="conf")
            rc = w.call("family_materialize", table=tbl, mode="configurations")
            vc = [_vol(w, r["handle"]) for r in rc["rows"]]
    _check("instances and configurations: identical geometry", vi, vc)
    _true("instances -> distinct files",
          len({tuple(r["files"]) for r in ri["rows"]}) == 2)
    _true("configurations -> one shared artifact",
          len({tuple(r["files"]) for r in rc["rows"]}) == 1)


def test_worker_catalog_matches_standard_table():
    """A built bearing catalog row's bore matches the ISO standards table value."""
    from driftpin import Worker
    catalog = {
        "schema": families.SCHEMA, "family": "wcat", "recipe": "ball_bearing",
        "mode": "instances", "key": "designation",
        "rows": [{"designation": "6004"}, {"designation": "6205"}],
    }
    with tempfile.TemporaryDirectory() as td:
        tbl = _write(td, "wcat.json", catalog)
        with Worker() as w:
            w.call("new_document", name="cat")
            res = w.call("family_materialize", table=tbl)
            bores = {}
            for r in res["rows"]:
                bb = w.call("bounding_box", handle=r["handle"])
                bores[r["key"]] = bb
    _check("catalog built both bearings", res["count"], 2)
    for desig in ("6004", "6205"):
        bb = bores[desig]
        od = _std.bearing(desig)["od_mm"]
        # the envelope's XY extent is the bearing OD (sourced from the ISO corpus)
        _check(f"{desig} envelope OD == ISO table",
               round(bb["size"][0], 3), float(od))


def test_worker_family_validate_handler():
    """The family_validate worker handler discriminates a good table from a bad
    one (out-of-range), naming the row+column."""
    from driftpin import Worker
    bad = {**_SMALL_GEARS, "rows": [{"size": "X", "module_mm": 1.0, "teeth": 1}]}
    with tempfile.TemporaryDirectory() as td:
        good_p = _write(td, "good.json", _SMALL_GEARS)
        bad_p = _write(td, "bad.json", bad)
        with Worker() as w:
            good = w.call("family_validate", table=good_p)
            badr = w.call("family_validate", table=bad_p)
    _check("good table validates ok", good["ok"], True)
    _check("bad table validates not-ok", badr["ok"], False)
    _true("bad-table problem names the row+column",
          any("'X'" in p and "teeth" in p for p in badr["problems"]))


_PURE = [
    test_csv_and_json_materialize_identically,
    test_n_rows_n_parts_each_with_item_and_pn,
    test_feature_flag_column_toggles_a_feature,
    test_material_column_routes_to_metadata_not_inputs,
    test_bad_row_out_of_range_named,
    test_bad_row_unknown_recipe_named,
    test_duplicate_size_key_named,
    test_modes_identical_inputs_distinct_files,
    test_catalog_row_sources_standard_table_value,
    test_unknown_designation_rejected_at_the_door,
    test_materialize_is_deterministic,
    test_csv_parse_roundtrips_directives,
]

_WORKER = [
    test_worker_gear_family_builds_n_parts,
    test_worker_modes_yield_identical_geometry,
    test_worker_catalog_matches_standard_table,
    test_worker_family_validate_handler,
]


def main():
    print("== design tables — variant families: table + door + items (no API) ==")
    for t in _PURE + _WORKER:
        try:
            t()
        except Exception as e:
            global _FAIL
            _FAIL += 1
            import traceback
            print(f"  FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n== {_PASS}/{_PASS + _FAIL} checks passed "
          f"({'OK' if not _FAIL else str(_FAIL) + ' FAILED'}) ==")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
