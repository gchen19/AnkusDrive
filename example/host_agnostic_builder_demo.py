"""
Host-agnostic component-builder contract — a runnable, falsifiable proof (issue #169).

docs/MULTI_AGENT.md describes a partition-and-merge design loop any MCP host can
drive: a coordinator decomposes a product into per-component *builder briefs*, each
builder builds one `.FCStd` with the FULL AnkusDrive tool surface (not a bundled
6-tool loop), self-gates with `component_contract_check`, and the coordinator merges
+ gates the integrated result. This script IS that loop, run end to end with real
geometry and hard asserts, so "the contract works from any host" is a check, not a
claim.

What it proves, concretely:
  1. DECOMPOSE — a plain-code coordinator turns one design into three standalone
     `ankusdrive.builder_brief/1` briefs (base plate, spacer, cap). No LLM: the brief
     is data, so a host that is Claude Code, Cursor, a shell script, or this file
     produces it the same way.
  2. FAN OUT with ISOLATION — each builder runs in its OWN Worker (its own
     freecadcmd process, its own handle registry). This is the bundled-worker analog
     of issue #167's `use_workspace(name)`: one builder = one workspace = one file,
     handles never cross. Each builder drives the full tool surface (new_document,
     add_primitive, boolean_op, publish_interface, check_shape, ...).
  3. SELF-GATE — before saving, each builder calls
     `component_contract_check(handle, brief)` and we assert it passes: watertight,
     inside its local envelope, every required interface published with a sane frame.
     This is the builder-side half of the gate the coordinator re-runs at merge.
  4. FAN IN + GATES — the coordinator writes a manifest (mates by published frame,
     with `verify_align` secondary datums), calls `merge_assembly`, and we assert the
     integrated result passes interference, envelope, and interface-alignment gates,
     then writes + verifies the lockfile (`assembly_lock` / `assembly_lock_check`).

Run:  .venv/bin/python3 example/host_agnostic_builder_demo.py
Exit: 0 iff every builder self-gate AND every integration gate passes.
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker  # noqa: E402
from ankusdrive import builder_brief as bb  # noqa: E402

_FAIL = 0


def _assert(label, cond):
    global _FAIL
    if cond:
        print(f"  PASS  {label}")
    else:
        _FAIL += 1
        print(f"  FAIL  {label}")


# --- step 1: DECOMPOSE — the design as three standalone builder briefs ----------
#
# A stacked bracket: a 60x60x10 base plate, a 40x40x8 spacer seated on it, and a
# 40x40x6 cap on the spacer. Each brief is complete on its own — an owner reads
# only its slice. Envelopes are LOCAL (each part's own bbox) for the builder-side
# gate; the merge manifest below carries the WORLD envelopes for the assembly gate.

def decompose():
    return {
        "base": {
            "schema": bb.SCHEMA, "component": "base", "assembly": "bracket_stack",
            "task": "Build a 60x60x10 mm base plate; publish frames 'top' at "
                    "(30,30,10) +Z and 'pin' at (10,10,10) +Z; save.",
            "output": "base.FCStd",
            "envelope": {"min": [0, 0, 0], "max": [60, 60, 10]},
            "interfaces": {"top": {"origin": [30, 30, 10], "z_axis": [0, 0, 1]},
                           "pin": {"origin": [10, 10, 10], "z_axis": [0, 0, 1]}},
            "material": "AISI 1045",
        },
        "spacer": {
            "schema": bb.SCHEMA, "component": "spacer", "assembly": "bracket_stack",
            "task": "Build a 40x40x8 mm spacer; publish 'bottom' at (20,20,0) +Z, "
                    "'pin' at (0,0,0) +Z, 'top' at (20,20,8) +Z, 'pin_top' at "
                    "(0,0,8) +Z; save.",
            "output": "spacer.FCStd",
            "envelope": {"min": [0, 0, 0], "max": [40, 40, 8]},
            "interfaces": {"bottom": {"origin": [20, 20, 0], "z_axis": [0, 0, 1]},
                           "pin": {"origin": [0, 0, 0], "z_axis": [0, 0, 1]},
                           "top": {"origin": [20, 20, 8], "z_axis": [0, 0, 1]},
                           "pin_top": {"origin": [0, 0, 8], "z_axis": [0, 0, 1]}},
        },
        "cap": {
            "schema": bb.SCHEMA, "component": "cap", "assembly": "bracket_stack",
            "task": "Build a 40x40x6 mm cap; publish 'bottom' at (20,20,0) +Z and "
                    "'pin' at (0,0,0) +Z; save.",
            "output": "cap.FCStd",
            "envelope": {"min": [0, 0, 0], "max": [40, 40, 6]},
            "interfaces": {"bottom": {"origin": [20, 20, 0], "z_axis": [0, 0, 1]},
                           "pin": {"origin": [0, 0, 0], "z_axis": [0, 0, 1]}},
        },
    }


# --- step 2/3: one builder — its own Worker, full tool surface, self-gate -------
#
# Each builder is handed ONLY its brief. It builds a box of the size named in the
# task, publishes exactly the frames the brief requires, then runs
# component_contract_check and refuses to save on a failing gate.

_BOX = {"base": (60, 60, 10), "spacer": (40, 40, 8), "cap": (40, 40, 6)}


def build_component(brief, out_dir):
    """Build one component in its OWN worker (isolation), self-gate, save. Returns
    (ok, path, gate_report)."""
    cid = brief["component"]
    w, d, h = _BOX[cid]
    path = out_dir / brief["output"]
    with Worker() as worker:                      # one builder = one workspace
        worker.call("new_document", name=cid)
        handle = worker.call("add_primitive", kind="box", w=w, d=d, h=h,
                             name=cid)["handle"]
        for name, frame in brief["interfaces"].items():
            worker.call("publish_interface", handle=handle, name=name, frame=frame)
        # builder-side gate — the local half of the merge gate
        gate = worker.call("component_contract_check", handle=handle, brief=brief)
        if gate["ok"]:
            worker.call("save_document", path=str(path))
    return gate["ok"], path, gate


# --- step 4: coordinator — manifest, merge, gates, lock -------------------------

def build_manifest(briefs, comp_files, root_path):
    """Project the built components + their mates into the merge_assembly manifest.
    WORLD envelopes here (assembly frame); the mates seat the parts by published
    frame and carry secondary `verify_align` pin datums for the alignment gate."""
    return {
        "schema": "ankusdrive.manifest/1",
        "name": "bracket_stack",
        "root": str(root_path),
        "components": {
            "base": {"file": str(comp_files["base"]),
                     "envelope": {"min": [0, 0, 0], "max": [60, 60, 10]}},
            "spacer": {"file": str(comp_files["spacer"]),
                       "envelope": {"min": [10, 10, 10], "max": [50, 50, 18]}},
            "cap": {"file": str(comp_files["cap"]),
                    "envelope": {"min": [10, 10, 18], "max": [50, 50, 24]}},
        },
        "instances": [
            {"component": "base", "name": "base", "placement": [0, 0, 0]},
            {"component": "spacer", "name": "spacer",
             "mate": {"child_iface": "bottom", "parent": "base",
                      "parent_iface": "top",
                      "verify_align": {"child_iface": "pin", "parent_iface": "pin"}}},
            {"component": "cap", "name": "cap",
             "mate": {"child_iface": "bottom", "parent": "spacer",
                      "parent_iface": "top",
                      "verify_align": {"child_iface": "pin",
                                       "parent_iface": "pin_top"}}},
        ],
    }


def run(workdir):
    briefs = decompose()

    print("step 1 — decompose: 1 design -> 3 builder briefs "
          f"({sorted(briefs)})")
    for cid, brief in briefs.items():
        _assert(f"brief {cid!r} is a valid ankusdrive.builder_brief/1",
                not bb.validate_builder_brief(brief))

    print("\nstep 2/3 — fan out: each builder in its OWN worker, self-gates before save")
    comp_files = {}
    for cid, brief in briefs.items():
        ok, path, gate = build_component(brief, workdir)
        comp_files[cid] = path
        detail = "" if ok else f" reasons={gate['reasons']}"
        _assert(f"builder {cid!r} passed component_contract_check{detail}", ok)
        _assert(f"builder {cid!r} saved its file", path.exists())

    print("\nstep 4 — fan in: coordinator merges + gates the integrated result")
    root_path = workdir / "bracket_stack.FCStd"
    manifest = build_manifest(briefs, comp_files, root_path)
    mpath = workdir / "bracket_stack.manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2))

    with Worker() as w:
        report = w.call("merge_assembly", manifest=str(mpath))
        gates = report["gates"]
        _assert("merge_assembly ok (all integration gates green)", report["ok"])
        _assert("interference gate: no clashes", not gates["interference"])
        _assert("envelope gate: every part inside its world envelope",
                not gates["envelope"])
        _assert("interface_align gate: secondary pin datums coincide",
                not gates.get("interface_align"))
        _assert("all 3 components linked into the root",
                len(report["placed"]) == 3)

        lock = w.call("assembly_lock", manifest=str(mpath))
        _assert("assembly_lock wrote a lockfile", Path(lock["lockfile"]).exists())
        check = w.call("assembly_lock_check", manifest=str(mpath))
        _assert("assembly_lock_check: clean tree, no drift", check["ok"])

    return report


def main():
    print("== host-agnostic builder contract — runnable proof (issue #169, no API) ==\n")
    with tempfile.TemporaryDirectory() as td:
        run(Path(td))
    print(f"\n== {'OK' if not _FAIL else str(_FAIL) + ' FAILED'} ==")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
