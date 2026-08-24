"""Design-hierarchy demo — the features from the epic #135 program, end to end.

Runnable showcase (no API key) of what the parametric/variant/design-control
layer (docs/DESIGN_HIERARCHY.md) makes possible, driven against the real worker:

    A) a whole gear FAMILY from ONE design table  -> N items, sequential part #s
    B) a drop-in SUBSTITUTION gate                -> Form/Fit/Function as code
    C) CHANGE CONTROL: where-used blast radius + an ECO + a reproducible baseline

Run from the repo root:   python example/design_hierarchy_demo.py
(needs FreeCAD for toys A/B, like the other worker-driven examples; toy C is
pure-Python over a lockfile + item registry. Artifacts go to a tempdir.)
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # prefer the repo source over any installed copy

from ankusdrive import Worker  # noqa: E402
from ankusdrive import change as C  # noqa: E402
from ankusdrive import items as I  # noqa: E402

FAM_TABLE = REPO / "tests" / "fixtures" / "families" / "gear_family.csv"
CHANGE_FIXTURE = REPO / "tests" / "fixtures" / "change"  # a released 5-part gearbox


def banner(t):
    print("\n" + "=" * 72 + f"\n  {t}\n" + "=" * 72)


def toy_a_gear_family(outdir):
    """recipes (#136) + design tables (#138) + item model (#140): one table -> a
    family of real parts, each with its own sequential part number."""
    banner("TOY A — a whole gear family from ONE table (recipes + families + items)")
    print(f"table: {FAM_TABLE.relative_to(REPO)}  (row = variant, column = recipe input / metadata)\n")
    with Worker() as w:
        w.call("new_document", name="fam")
        res = w.call("family_materialize", table=str(FAM_TABLE))
        print(f"  built {res['count']} variants from one table (mode={res.get('mode')}):\n")
        print(f"    {'size':<14}{'part #':<12}{'recipe':<11}{'volume mm^3':>12}")
        print(f"    {'-' * 49}")
        for r in res["rows"]:
            vol = w.call("mass_properties", handle=r["handle"], density=7.9e-6)["volume_mm3"]
            w.call("export_shape", handle=r["handle"], path=str(outdir / f"gear_{r['key']}.step"))
            print(f"    {r['key']:<14}{r['part_number']:<12}{res['recipe']:<11}{vol:>12.1f}")
    print(f"\n  -> {res['count']} STEP files written to {outdir}")
    print("  PAYOFF: change the family = edit the table; the recipe is authored ONCE.")


def _gear(path, teeth, module=2.0):
    with Worker() as w:
        w.call("new_document", name="g")
        w.call("add_gear", teeth=int(teeth), module=module, height=6, name="gear")
        w.call("save_document", path=str(path))


def toy_b_substitution():
    """substitutability gate (#147) + the F3 predicate (#141): swapping a part is a
    deterministic interchangeability check, not a judgment call."""
    banner("TOY B — drop-in substitution gate: Form/Fit/Function as code")
    M, C = 2.0, 48.0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _gear(tmp / "a.FCStd", 12, M)        # pinion 12T
        _gear(tmp / "bA.FCStd", 36, M)       # reference gear 36T  (ratio 3.0 @ C=48)
        _gear(tmp / "bB_ok.FCStd", 36, M)    # candidate: same 36T, re-cut
        _gear(tmp / "bB_bad.FCStd", 40, M)   # candidate: 40T (pitch-radii sum 52 != 48)
        man = {
            "name": "drive", "root": str(tmp / "drive.FCStd"),
            "components": {"gearA": {"file": "a.FCStd"}, "gearB": {"file": "bA.FCStd"}},
            "instances": [
                {"component": "gearA", "name": "gearA", "placement": [0, 0, 0]},
                {"component": "gearB", "name": "gearB", "placement": [C, 0, 0]}],
            "checks": [{"kind": "gear_mesh", "a": "gearA", "b": "gearB", "module_mm": M,
                        "center_distance_mm": C, "ratio": 3.0, "tol_mm": 0.5}],
        }
        mpath = tmp / "manifest.json"
        mpath.write_text(json.dumps(man))
        print("  base: pinion 12T + gear 36T meshing at C=48 (ratio 3.0) — gates green.\n")
        with Worker() as w:
            ok = w.call("substitutability_check", manifest=str(mpath),
                        slot="gearB", variant={"file": "bB_ok.FCStd"})
            bad = w.call("substitutability_check", manifest=str(mpath),
                         slot="gearB", variant={"file": "bB_bad.FCStd"})

    for label, r in (("36T (same pitch)", ok), ("40T (won't mesh)", bad)):
        sub = r.get("substitutable")
        cls = r.get("classification", {})
        decision = cls.get("decision", "revise" if sub else "new part number")
        broke = r.get("broken_gates") or r.get("failing_gates") or r.get("broken") or []
        extra = "" if sub else f"  broke: {broke}"
        print(f"    swap in {label:<18} -> substitutable={sub!s:<5}  decision: {decision}{extra}")
    print("\n  PAYOFF: 'is this interchangeable?' is a deterministic gate (F3 = semver).")


def toy_c_change_control():
    """item model (#140) + lifecycle (#141) + ECO/where-used/baselines (#142): a
    change becomes a RECORD that carries its blast radius, and a release pins a
    byte-reproducible baseline. Pure-Python over the released-gearbox fixture."""
    banner("TOY C — change control: where-used blast radius + ECO + reproducible baseline")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "gearbox"
        shutil.copytree(CHANGE_FIXTURE, base)  # copy so we can drift a file later
        lock = C.load_lockfile(base / "gearbox.lock.json")
        reg = I.load_registry(base / "items.json")

        print("  a released 5-part gearbox (depends_on: lid/shaft<-housing, gear<-shaft, cover<-lid):")
        for iid, rec in reg["items"].items():
            print(f"    {rec['part_number']}  {iid:<8} rev {rec['rev']}  [{rec['lifecycle']}]")

        print("\n  WHERE-USED — who must re-evaluate if I change this part?")
        for it in ("gear", "shaft", "housing"):
            radius = C.where_used(lock, it) or ["(none — top-level consumer)"]
            print(f"    change {it:<8} -> blast radius: {radius}")

        eco = C.make_eco(
            "ECO-0042", affected=["shaft"], disposition="rework — enlarge bearing journal",
            effectivity={"serial": "SN-100"}, interface_change=True,
            title="Shaft journal upsize")
        assert not C.validate_eco(eco), C.validate_eco(eco)
        rec = C.eco_with_impact(eco, lock)
        print(f"\n  ECO {eco['id']} '{eco['title']}'  affected={eco['affected']}  effectivity={eco['effectivity']}")
        print(f"    -> immediate re-dispatch (§9 stale): {rec['impact']['stale']}")
        print(f"    -> full transitive blast radius:     {rec['impact']['where_used']}")

        bl = C.create_baseline("gearbox-rel-1.0", reg, base_dir=str(base))
        v0 = C.verify_baseline(bl, reg, base_dir=str(base))
        print(f"\n  BASELINE 'gearbox-rel-1.0' pinned {len(bl['items'])} items (rev + byte fingerprint).")
        print(f"    clean rebuild verifies: ok={v0['ok']}")
        gp = base / "components" / "gear.part"     # someone edits the gear artifact
        gp.write_bytes(gp.read_bytes() + b"\n# tweaked\n")
        v1 = C.verify_baseline(bl, reg, base_dir=str(base))
        print(f"    after editing gear.part: ok={v1['ok']}, drift caught -> "
              f"{[(d['item'], d['field']) for d in v1['drifted']]}")
    print("\n  PAYOFF: a change carries its blast radius; a release is a byte-reproducible pin.")


if __name__ == "__main__":
    out = Path(tempfile.mkdtemp(prefix="dp_demo_"))
    toy_a_gear_family(out)
    toy_b_substitution()
    toy_c_change_control()
    banner("done")
