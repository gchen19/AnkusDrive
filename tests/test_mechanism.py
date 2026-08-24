"""Mechanism motion oracle (ankusdrive/mechanism.py) — the *moving*-assembly gate.

Pure Python, no FreeCAD, no API. The same constant-mesh gearbox geometry is
declared two ways and the oracle must separate them:

  * RIGID — every gear keyed to its shaft. Six pairs demand six different ratios of
    the same two shafts: over-constrained, mobility DOF < 1, LOCKED. This is exactly
    what scratch/gearbox_experiment.py built and the static gates green-lit.
  * SELECTIVE — output gears freewheel; a dog clutch engages one pair per speed.
    Each state is a determinate single-DOF train whose realised ratio = design.

Run: .venv/bin/python3 tests/test_mechanism.py
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive.mechanism import (  # noqa: E402
    analyze, _speed_ratio, meshes_from_checks, validate)

# gearbox_real.py: tooth-sum S=48, RATIOS = N_out/N_in (size). teeth() below.
RATIOS = [3.0, 2.0, 1.4, 1.0, 5.0 / 7.0, 0.5]
S = 48


def teeth(ratio):
    n_in = int(round(S / (1.0 + ratio)))
    return n_in, S - n_in


def _links(n, rigid):
    """Two shafts. rigid=True welds every gear to its shaft; rigid=False keeps only
    the shafts as links (output gears will freewheel)."""
    in_members = ["input_shaft"] + [f"in{s}" for s in range(n)]
    out_rigid = ["output_shaft"] + [f"out{s}" for s in range(n)]
    return {
        "input_shaft": {"members": in_members, "ground": "revolute"},
        "output_shaft": {"members": out_rigid if rigid else ["output_shaft"],
                         "ground": "revolute"},
    }


def _meshes(n):
    out = []
    for s in range(n):
        ni, no = teeth(RATIOS[s])
        out.append({"a": f"in{s}", "b": f"out{s}", "teeth_a": ni, "teeth_b": no,
                    "id": f"pair{s}"})
    return out


def rigid_spec(n):
    return {"expected_dof": 1, "input": "input_shaft", "output": "output_shaft",
            "links": _links(n, rigid=True), "meshes": _meshes(n)}


def selective_spec(n):
    states = {f"speed{s+1}": [f"pair{s}"] for s in range(n)}
    design = {}
    for s in range(n):
        ni, no = teeth(RATIOS[s])
        design[f"speed{s+1}"] = ni / no              # omega_out/omega_in
    return {"expected_dof": 1, "input": "input_shaft", "output": "output_shaft",
            "links": _links(n, rigid=False), "meshes": _meshes(n),
            "engagement": {"freewheel": [f"out{s}" for s in range(n)],
                           "rides_on": {f"out{s}": "output_shaft" for s in range(n)},
                           "states": states, "design_ratios": design,
                           "input": "input_shaft", "output": "output_shaft"}}


# --- the headline separation -------------------------------------------------

def test_rigid_3speed_is_locked():
    r = analyze(rigid_spec(3))
    assert not r["ok"], "rigid gearbox must FAIL the motion oracle"
    assert r["verdict"] == "locked", r["verdict"]
    assert r["rigid"]["mobility_dof"] == -1, r["rigid"]["mobility_dof"]
    assert not r["rigid"]["consistent"], "must detect conflicting ratios"
    assert r["rigid"]["conflicts"], "must name the conflict"


def test_rigid_6speed_is_more_locked():
    r = analyze(rigid_spec(6))
    assert not r["ok"]
    assert r["rigid"]["mobility_dof"] == -4, r["rigid"]["mobility_dof"]
    assert "conflicting" in r["violations"][0]["reason"] or \
        "cannot move" in r["violations"][0]["reason"]


def test_selective_3speed_is_functional():
    r = analyze(selective_spec(3))
    assert r["ok"], f"selective gearbox must PASS: {r['violations']}"
    assert r["verdict"] == "functional"
    for s in range(3):
        st = r["states"][f"speed{s+1}"]
        assert st["power_path_dof"] == 1, (s, st)
        assert st["ratio_ok"], st


def test_selective_6speed_is_functional():
    r = analyze(selective_spec(6))
    assert r["ok"], r["violations"]
    assert len(r["states"]) == 6
    assert all(st["ok"] for st in r["states"].values())


def test_selective_realised_ratio_matches_design():
    """Each speed's realised omega_out/omega_in equals N_in/N_out within tol."""
    r = analyze(selective_spec(6))
    for s in range(6):
        ni, no = teeth(RATIOS[s])
        st = r["states"][f"speed{s+1}"]
        assert abs(abs(st["ratio_realised"]) - ni / no) < 1e-4, st  # 5-dp rounded


# --- guard rails on the analysis itself --------------------------------------

def test_a_corrupted_ratio_is_caught():
    """If one speed's output gear is built with the WRONG tooth count, its realised
    ratio diverges from design and the state fails — the oracle isn't a rubber stamp."""
    spec = selective_spec(3)
    for m in spec["meshes"]:
        if m["id"] == "pair1":
            m["teeth_b"] += 3                         # mis-sized output gear
    r = analyze(spec)
    assert not r["ok"]
    assert any(v["state"] == "speed2" for v in r["violations"]), r["violations"]


def test_single_engaged_pair_is_one_dof():
    """A 2-shaft, single-mesh train is the canonical DOF=1 mechanism (matches the
    closed-form gruebler check run against the as-built numbers)."""
    spec = {"expected_dof": 1, "input": "a", "output": "b",
            "links": {"a": {"members": ["a", "g0"], "ground": "revolute"},
                      "b": {"members": ["b", "g1"], "ground": "revolute"}},
            "meshes": [{"a": "g0", "b": "g1", "teeth_a": 20, "teeth_b": 20,
                        "id": "p"}]}
    r = analyze(spec)
    assert r["ok"] and r["rigid"]["mobility_dof"] == 1, r


def test_internal_mesh_does_not_reverse():
    assert _speed_ratio({"teeth_a": 10, "teeth_b": 20}) < 0           # external
    assert _speed_ratio({"teeth_a": 10, "teeth_b": 20,
                         "external": False}) > 0                      # internal/annular


def test_size_ratio_form_equivalent_to_teeth():
    """ratio = Nb/Na must give the same speed ratio as the teeth form."""
    by_teeth = _speed_ratio({"teeth_a": 12, "teeth_b": 36})
    by_ratio = _speed_ratio({"ratio": 36 / 12})
    assert abs(by_teeth - by_ratio) < 1e-9, (by_teeth, by_ratio)


# --- overall train-ratio gate (the emergent product invariant) ---------------

def _train_spec(stage_teeth, target=None, tol=0.004):
    """Serial K-stage train: shaft0..shaftK each grounded; stage i meshes a driver on
    shaft i with a driven on shaft i+1. DOF 1, no loops."""
    K = len(stage_teeth)
    links, meshes = {}, []
    for i in range(K + 1):
        members = [f"shaft{i}"] + ([f"d{i}"] if i < K else []) + ([f"e{i-1}"] if i else [])
        links[f"shaft{i}"] = {"members": members, "ground": "revolute"}
    for i, (a, b) in enumerate(stage_teeth):
        meshes.append({"a": f"d{i}", "b": f"e{i}", "teeth_a": a, "teeth_b": b,
                       "id": f"stage{i}"})
    spec = {"expected_dof": 1, "links": links, "meshes": meshes}
    if target is not None:
        spec["target_ratio"] = {"input": "shaft0", "output": f"shaft{K}",
                                "ratio": target, "tol": tol}
    return spec


def test_train_is_one_dof_and_hits_target():
    """A 3-stage train with each stage meshing -> DOF 1; product of stage ratios
    (10/30)(22/22)(16/32) = 1/6 hits target."""
    target = (10 / 30) * (22 / 22) * (16 / 32)
    r = analyze(_train_spec([(10, 30), (22, 22), (16, 32)], target=target))
    assert r["ok"], r["violations"]
    assert r["rigid"]["mobility_dof"] == 1
    assert abs(abs(r["target_ratio"]["realised"]) - target) < 1e-4, r["target_ratio"]


def test_train_off_target_is_caught_though_each_stage_meshes():
    """Every stage meshes locally (sum to its centre), but the PRODUCT misses target —
    the emergent failure no single stage builder can see. The gate must catch it."""
    target = (10 / 30) * (22 / 22) * (16 / 32)            # 1/6
    bad = analyze(_train_spec([(20, 20), (22, 22), (16, 32)], target=target))  # 1/2
    assert not bad["ok"]
    assert bad["target_ratio"]["ok"] is False
    assert "overall ratio" in bad["violations"][0]["reason"], bad["violations"]


def test_train_without_target_only_checks_mobility():
    r = analyze(_train_spec([(20, 20), (22, 22), (16, 32)]))   # no target_ratio
    assert r["ok"] and "target_ratio" not in r, r


# --- the worker-delegated helpers (front-door derivation + validation) -------

def test_meshes_derived_from_gear_mesh_checks():
    checks = [{"kind": "gear_mesh", "a": "in0", "b": "out0", "ratio": 3.0},
              {"kind": "bore_fit", "pin": "s", "bore": "brg"},
              {"kind": "gear_mesh", "a": "in1", "b": "out1", "ratio": 0.5}]
    ms = meshes_from_checks(checks)
    assert [m["id"] for m in ms] == ["in0__out0", "in1__out1"]
    assert ms[0]["ratio"] == 3.0 and ms[1]["a"] == "in1"


def test_validate_flags_phantom_member_and_state():
    spec = selective_spec(3)
    inst_names = {f"in{s}" for s in range(3)} | {f"out{s}" for s in range(3)} \
        | {"input_shaft", "output_shaft"}
    assert validate(spec, inst_names, []) == []          # well-formed
    bad = selective_spec(3)
    bad["links"]["input_shaft"]["members"].append("ghost_gear")
    bad["engagement"]["states"]["speed1"] = ["pairX"]
    probs = validate(bad, inst_names, [])
    assert any("ghost_gear" in p for p in probs), probs
    assert any("pairX" in p for p in probs), probs


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    t0 = time.time()
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n== {len(tests) - failed}/{len(tests)} passed  "
          f"({time.time() - t0:.2f}s) ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
