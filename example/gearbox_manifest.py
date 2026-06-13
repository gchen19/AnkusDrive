"""
The 6-speed gearbox as a MANIFEST (RFC docs/MULTI_AGENT.md §11.2).

Before typed interfaces this lived only as a test fixture: 12 gears, every
input+output pair sharing one shaft centre distance C, each pair's ratio its own
contract. With the `gear_mesh` typed interface it is expressible declaratively —
each gear is a component, each mesh a typed `check` — and `merge_assembly`
assembles it and dispatches the per-mesh gates in one call. The canonical
shared-constraint partition: each builder sizes ONE gear so its pair meshes at
the shared C, and the merge gate proves the whole train meshes.

This is the host-agnostic substrate; a real run would fan one builder agent per
gear. Here scripted reference gears stand in so it runs free (no API).

Run: .venv/bin/python3 example/gearbox_manifest.py
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

MODULE_MM = 2.0
CENTER_DISTANCE_MM = 48.0
RATIOS = [3.0, 2.0, 1.4, 1.0, 5.0 / 7.0, 0.5]   # 6 speeds, all integer-tooth
SPEED_PITCH_MM = 90.0                            # stack speeds clear of each other


def _teeth(ratio):
    """Tooth counts (input, output) for a pair at the shared centre distance:
    they sum to 2C/module, and split by the ratio."""
    tooth_sum = int(round(2 * CENTER_DISTANCE_MM / MODULE_MM))
    n_in = int(round(tooth_sum / (1.0 + ratio)))
    return n_in, tooth_sum - n_in


def build_manifest(tmp):
    """Write the gear component files (scripted stand-ins for builder agents) and
    return the manifest dict: a component per gear, a gear_mesh check per pair."""
    components, instances, checks = {}, [], []
    for speed, ratio in enumerate(RATIOS):
        n_in, n_out = _teeth(ratio)
        for tag, teeth, x in ((f"in{speed}", n_in, 0.0),
                              (f"out{speed}", n_out, CENTER_DISTANCE_MM)):
            with Worker() as w:
                w.call("new_document", name=tag)
                w.call("add_gear", teeth=teeth, module=MODULE_MM, height=6,
                       name="gear")
                w.call("save_document", path=str(tmp / f"{tag}.FCStd"))
            components[tag] = {"file": f"{tag}.FCStd"}
            instances.append({"component": tag, "name": tag,
                              "placement": [x, speed * SPEED_PITCH_MM, 0]})
        checks.append({"kind": "gear_mesh", "a": f"in{speed}", "b": f"out{speed}",
                       "module_mm": MODULE_MM,
                       "center_distance_mm": CENTER_DISTANCE_MM,
                       "ratio": n_out / n_in, "tol_mm": 0.5})
    return {"name": "gearbox6", "root": "gearbox6.FCStd",
            "components": components, "instances": instances, "checks": checks}


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        man = build_manifest(tmp)
        mpath = tmp / "gearbox.manifest.json"
        mpath.write_text(json.dumps(man, indent=2))
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=str(mpath))

    g = rep["gates"]
    print("== 6-speed gearbox merged from a manifest ==")
    print(f"  components: {len(man['components'])} gears   "
          f"gear_mesh checks: {len(man['checks'])}")
    print(f"  BOM rows: {len(g['bom'])}   "
          f"interference (after mesh exclusion): {len(g['interference'])}   "
          f"typed violations: {len(g.get('typed', []))}")
    print(f"  ok = {rep['ok']}")
    if not rep["ok"]:
        for v in g.get("typed", []):
            print("   typed:", v.get("reason") or v.get("error"))
        for v in g["interference"]:
            print("   interference:", v)
        sys.exit(1)
    print("  PASS — every pair meshes at the shared centre distance with its "
          "contracted ratio")


if __name__ == "__main__":
    main()
