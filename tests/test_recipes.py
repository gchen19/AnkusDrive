"""
Part recipes (issue #136) — named, parameterized, declared-input build templates.
Free, no LLM, no key. Two halves:

  * PURE (no FreeCAD): the FROZEN contract — recipe-ref + input-schema shapes match
    the golden fixture; door validation discriminates a well-formed reference from
    every malformed one (unknown recipe, missing required, out-of-range, wrong type,
    unknown input key, bad unit); the typed-units layer accepts a unit-bearing
    length and refuses a wrong-dimension one; lowering maps a recipe component onto
    the library form.

  * WORKER (FreeCAD): a recipe builds a real part (geometry + published interface +
    declared intent); the build is byte-identical across two workers and idempotent
    on re-run (rides the determinism envelope, #123/#127); merge_assembly builds a
    lowered recipe component the way it builds a library component, and the gates
    pass.

Run: python3 tests/test_recipes.py     (or .venv/bin/python3)
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import recipes  # noqa: E402

FIXTURE = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "recipe_spur_gear.json").read_text(encoding="utf-8"))

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _rejects(label, recipe, inputs, substr):
    """Assert validate_ref flags a recipe-ref, and a problem mentions substr."""
    problems = recipes.validate_ref({"recipe": recipe, "inputs": inputs})
    ok = bool(problems) and any(substr in p for p in problems)
    _check(label, ok, True)
    if not ok:
        print(f"       (problems were: {problems})")


# =============================================================================
# PURE — the frozen contract + the door (no FreeCAD)
# =============================================================================

def test_input_schema_matches_golden_fixture():
    """The live registry's declared schema for spur_gear is exactly the frozen
    fixture — the contract downstream Wave-1 work pins to."""
    _check("input_schema == golden fixture",
           recipes.input_schema("spur_gear"), FIXTURE["input_schema"])
    _check("contract schema stamp", recipes.SCHEMA, "ankusdrive.recipe/1")


def test_lowering_matches_golden_fixture():
    """A recipe component lowers to exactly the library form in the fixture, so
    merge_assembly builds it the way it builds a standard part."""
    _check("lower_component == golden lowered form",
           recipes.lower_component(FIXTURE["component"]),
           FIXTURE["lowered_component"])


def test_valid_reference_passes():
    _check("well-formed recipe-ref -> no problems",
           recipes.validate_ref(FIXTURE["component"]), [])
    resolved = recipes.validate_inputs("spur_gear", FIXTURE["component"]["inputs"])
    _check("resolved fills the optional defaults",
           resolved, {"module_mm": 2.0, "teeth": 24, "width_mm": 6.0,
                      "pressure_angle_deg": 20.0, "external": True})


def test_unknown_recipe_is_loud():
    _rejects("unknown recipe name -> loud",
             "no_such_recipe", {"teeth": 12}, "unknown recipe")


def test_missing_required_no_default_is_loud():
    """A required input with NO default, omitted, fails loudly."""
    import ankusdrive.recipes as r
    spec = r.Recipe(
        name="_t_req", doc="t",
        inputs=[r.InputSpec("w", "length", unit="mm", required=True)],  # no default
        build_fn=lambda p, c: {})
    r.register(spec)
    try:
        _rejects("missing required (no default) -> loud",
                 "_t_req", {}, "missing required input")
    finally:
        r.RECIPES.pop("_t_req", None)


def test_out_of_range_rejected():
    _rejects("teeth below min -> rejected",
             "spur_gear", {"module_mm": 2.0, "teeth": 1}, "below the minimum")
    _rejects("module above max -> rejected",
             "spur_gear", {"module_mm": 999.0, "teeth": 12}, "above the maximum")
    _rejects("pressure angle below min -> rejected",
             "spur_gear", {"module_mm": 2.0, "teeth": 12,
                           "pressure_angle_deg": 5.0}, "below the minimum")


def test_wrong_type_rejected():
    _rejects("non-integer teeth -> rejected",
             "spur_gear", {"module_mm": 2.0, "teeth": 12.5}, "expected an integer")
    _rejects("non-bool external -> rejected",
             "spur_gear", {"module_mm": 2.0, "teeth": 12, "external": "yes"},
             "expected a bool")


def test_unknown_input_key_rejected():
    _rejects("unknown input key -> rejected",
             "spur_gear", {"module_mm": 2.0, "teeth": 12, "bogus": 1},
             "unknown input")


def test_typed_units_on_length_inputs():
    """A length input accepts a unit-bearing string and converts to canonical mm;
    a wrong-dimension value is refused at the door (the #102 mis-scale trap)."""
    resolved = recipes.validate_inputs(
        "spur_gear", {"module_mm": "0.25 in", "teeth": 12})
    _check("0.25 in -> 6.35 mm canonical", round(resolved["module_mm"], 4), 6.35)
    _rejects("a force given for a length -> refused",
             "spur_gear", {"module_mm": "5 N", "teeth": 12}, "module_mm")


def test_non_object_reference_is_loud():
    _check("non-object recipe component flagged",
           bool(recipes.validate_ref("nope")), True)
    _check("missing 'recipe' key flagged",
           bool(recipes.validate_ref({"inputs": {}})), True)


# =============================================================================
# WORKER — real geometry, determinism, merge (needs FreeCAD)
# =============================================================================

def _build(w, inputs):
    """Build spur_gear via the worker and return (recipe-result, geom-summary)."""
    w.call("new_document", name="rec")
    res = w.call("recipe", recipe="spur_gear", inputs=inputs)
    mp = w.call("mass_properties", handle=res["handle"], density=7.9e-6)
    summary = {
        "volume": mp["volume_mm3"],
        "area": mp["surface_area_mm2"],
        "cg": tuple(mp["center_of_mass_mm"]),
        "pitch_radius": res["part"]["pitch_radius"],
        "interfaces": tuple(res["interfaces"]),
        "intent": tuple(sorted(res["intent"].items())),
    }
    return res, summary


def test_recipe_builds_part_with_interface_and_intent():
    from ankusdrive import Worker
    with Worker() as w:
        res, summary = _build(w, {"module_mm": 2.0, "teeth": 24, "width_mm": 6.0})
    _check("recipe stamps its name", res["recipe"], "spur_gear")
    _check("recipe stamps contract schema", res["schema"], "ankusdrive.recipe/1")
    _check("publishes a gear_mesh interface", "gear_mesh" in res["interfaces"], True)
    _check("declares watertight intent", res["intent"].get("watertight"), True)
    _check("pitch radius = module*teeth/2", summary["pitch_radius"], 24.0)
    _check("real solid (positive volume)", summary["volume"] > 0, True)


def test_recipe_build_is_deterministic_two_workers():
    """Same inputs -> byte-identical geometry across two independent workers
    (rides the determinism envelope) — the A1 reference gate."""
    from ankusdrive import Worker
    inputs = {"module_mm": 2.0, "teeth": 24, "width_mm": 6.0}
    with Worker() as wa:
        _, sa = _build(wa, inputs)
    with Worker() as wb:
        _, sb = _build(wb, inputs)
    _check("two workers -> identical part", sa, sb)


def test_recipe_build_is_idempotent_one_worker():
    """Re-running the recipe in one worker reproduces the same part exactly."""
    from ankusdrive import Worker
    inputs = {"module_mm": 2.5, "teeth": 19}
    with Worker() as w:
        _, s1 = _build(w, inputs)
        _, s2 = _build(w, inputs)
    _check("re-run -> identical part", s1, s2)


def test_recipe_handlers_on_the_worker_surface():
    """recipe_list / recipe_schema / recipe_validate are exposed and discriminate."""
    from ankusdrive import Worker
    with Worker() as w:
        lst = w.call("recipe_list")
        sch = w.call("recipe_schema", recipe="spur_gear")
        good = w.call("recipe_validate", recipe="spur_gear",
                      inputs={"module_mm": 2.0, "teeth": 24})
        bad = w.call("recipe_validate", recipe="spur_gear",
                     inputs={"module_mm": 2.0, "teeth": 1})
    _check("recipe_list lists spur_gear", "spur_gear" in lst["recipes"], True)
    _check("recipe_schema matches fixture", sch, FIXTURE["input_schema"])
    _check("recipe_validate ok on good inputs", good["ok"], True)
    _check("recipe_validate loud on bad inputs", bad["ok"], False)


def test_merge_builds_a_recipe_component():
    """merge_assembly materializes a (lowered) recipe component through the library
    path and gates it green — two gears placed clear of each other."""
    from ankusdrive import Worker
    man = {
        "schema": "ankusdrive.manifest/1", "name": "gearpair", "root": "gearpair.FCStd",
        "components": {
            "g_big": {"recipe": "spur_gear",
                      "inputs": {"module_mm": 2.0, "teeth": 24, "width_mm": 6.0}},
            "g_sml": {"recipe": "spur_gear",
                      "inputs": {"module_mm": 2.0, "teeth": 17, "width_mm": 6.0}},
        },
        "instances": [
            {"component": "g_big", "name": "big", "placement": [0, 0, 0]},
            {"component": "g_sml", "name": "sml", "placement": [200, 0, 0]},
        ],
    }
    lowered = recipes.lower_manifest(man)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mp = tmp / "gearpair.manifest.json"
        mp.write_text(json.dumps(lowered, indent=2), encoding="utf-8")
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=str(mp))
    _check("recipe-component merge -> ok", rep["ok"], True)
    _check("two parts in the BOM", len(rep["gates"]["bom"]), 2)
    _check("built through the library path", set(rep.get("library", {})),
           {"g_big", "g_sml"})
    _check("no interference between the placed gears",
           rep["gates"]["interference"], [])


_PURE = [
    test_input_schema_matches_golden_fixture,
    test_lowering_matches_golden_fixture,
    test_valid_reference_passes,
    test_unknown_recipe_is_loud,
    test_missing_required_no_default_is_loud,
    test_out_of_range_rejected,
    test_wrong_type_rejected,
    test_unknown_input_key_rejected,
    test_typed_units_on_length_inputs,
    test_non_object_reference_is_loud,
]

_WORKER = [
    test_recipe_builds_part_with_interface_and_intent,
    test_recipe_build_is_deterministic_two_workers,
    test_recipe_build_is_idempotent_one_worker,
    test_recipe_handlers_on_the_worker_surface,
    test_merge_builds_a_recipe_component,
]


def main():
    print("== part recipes — frozen contract + door (no API) ==")
    # The pure contract/door tests need no FreeCAD; the worker tests spawn
    # freecadcmd in a subprocess (the harness interpreter never imports FreeCAD).
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
