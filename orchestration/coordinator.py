"""
Reference coordinator — the host-side multi-agent design loop (RFC Appendix A).

Given a design brief (a manifest with per-component build briefs + the mates that
join them), the coordinator:

  1. fans out one builder agent per component (each cold, contract-slice only),
  2. merges them with DriftPin's merge_assembly primitive,
  3. reads the gates (interference / envelope / interface alignment),
  4. on failure, RENEGOTIATES — re-dispatches only the components implicated by the
     failing gate — and re-merges, up to a round budget.

This is one reference binding of the roles in docs/MULTI_AGENT.md §8; it is NOT
part of the DriftPin primitive surface. The contract between coordinator and
builders is only the manifest + the component files on disk.

The coordinator drives builders through orchestration.agentkit, so it runs against
a real Anthropic client OR the scripted ScriptedClient (free dry runs). The merge
and gates are real DriftPin worker calls either way.
"""
import json
from pathlib import Path

from driftpin import Worker
from . import agentkit


# --- decompose: free-text spec -> brief (RFC Appendix A, step 1) ---------------
#
# The coordinator's first role: turn a design brief in prose into the structured
# brief orchestrate() consumes (components with build tasks, instances, mates).
# We force a tool call so the model returns valid JSON, then validate it before it
# can reach the build loop — a malformed decomposition should fail loudly here, not
# halfway through a billed fan-out.

DECOMPOSE_SYSTEM = (
    "You are the COORDINATOR in a multi-agent mechanical-design system. Given a "
    "design spec, decompose it into independent components that separate builder "
    "agents will each construct in their own file, then be merged into one assembly.\n"
    "Rules:\n"
    "- One component per part that can be built independently (a plate, a peg, a "
    "housing, a bracket).\n"
    "- Each component's `task` is a complete, self-contained build instruction in "
    "millimetres for a builder agent that sees ONLY that task — restate every "
    "dimension it needs; do not refer to other components. End each task by telling "
    "the agent to call save_component.\n"
    "- Put any value two components must agree on (a bore diameter, a bolt circle, a "
    "mating face height) explicitly in BOTH their tasks — that shared contract is the "
    "only thing keeping them compatible.\n"
    "- `instances` place each component in the assembly: give an explicit "
    "[x, y, z] placement in mm. One instance per component unless the spec asks for "
    "repeats.\n"
    "Call emit_brief exactly once with the decomposition."
)

_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "components": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "task": {"type": "string"},
                },
                "required": ["file", "task"],
            },
        },
        "instances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "component": {"type": "string"},
                    "name": {"type": "string"},
                    "placement": {"type": "array", "items": {"type": "number"}},
                },
                "required": ["component", "placement"],
            },
        },
    },
    "required": ["name", "components", "instances"],
}

_EMIT_TOOL = {
    "name": "emit_brief",
    "description": "Emit the structured decomposition of the design spec.",
    "input_schema": _BRIEF_SCHEMA,
}


def validate_brief(brief):
    """Cheap structural validation independent of the JSON schema (which the API
    enforces on emit). Returns a list of human-readable problems; empty == valid.
    Catches the cross-reference errors a schema can't: an instance pointing at a
    missing component, duplicate files, an empty decomposition."""
    problems = []
    comps = brief.get("components") or {}
    insts = brief.get("instances") or []
    if not comps:
        problems.append("no components")
    if not insts:
        problems.append("no instances")
    files = {}
    for cid, spec in comps.items():
        f = spec.get("file")
        if not f:
            problems.append(f"component {cid!r} has no file")
        else:
            files.setdefault(f, []).append(cid)
        if not (spec.get("task") or "").strip():
            problems.append(f"component {cid!r} has an empty task")
    for f, owners in files.items():
        if len(owners) > 1:
            problems.append(f"file {f!r} claimed by {owners} (one writer per file)")
    for i, inst in enumerate(insts):
        c = inst.get("component")
        if c not in comps:
            problems.append(f"instance {i} references unknown component {c!r}")
    # every component should be placed at least once
    placed = {inst.get("component") for inst in insts}
    for cid in comps:
        if cid not in placed:
            problems.append(f"component {cid!r} is never placed by an instance")
    return problems


def decompose(client, model, spec, log=print):
    """Free-text spec -> validated brief via a forced emit_brief tool call.
    Returns (brief, usage). Raises ValueError if the emitted brief fails validation."""
    resp = client.messages.create(
        model=model, max_tokens=2048, system=DECOMPOSE_SYSTEM,
        tools=[_EMIT_TOOL], tool_choice={"type": "tool", "name": "emit_brief"},
        messages=[{"role": "user", "content": spec}],
    )
    usage = {"in_tokens": resp.usage.input_tokens, "out_tokens": resp.usage.output_tokens,
             "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
             "cache_write": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0}
    calls = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
    if not calls:
        raise ValueError("decompose: model did not call emit_brief")
    brief = dict(calls[0].input)
    # The harness — not the model — owns the on-disk format: a component file is a
    # FreeCAD document, and save_document only writes .FCStd. Models tend to pick
    # ".step"/".stp" (the interchange format they associate with CAD parts), whose
    # save then fails. Force the extension so the builder's save_component succeeds.
    for spec in brief.get("components", {}).values():
        f = spec.get("file")
        if isinstance(f, str) and f:
            spec["file"] = f.rsplit(".", 1)[0] + ".FCStd" if "." in f else f + ".FCStd"
    problems = validate_brief(brief)
    log(f"  decomposed into {len(brief.get('components', {}))} components: "
        f"{sorted(brief.get('components', {}))}")
    if problems:
        raise ValueError(f"decompose produced an invalid brief: {problems}")
    return brief, usage


# --- brief schema -------------------------------------------------------------
#
# A brief is a manifest plus the natural-language build task per component. We keep
# the agent-facing `task` separate from the geometric `manifest` the merge consumes:
#   {
#     "name": "gearbox",
#     "components": { "<id>": {"file": "<id>.FCStd",
#                              "task": "Build a ... and publish_interface ...",
#                              "envelope": {min,max}?,
#                              "object": "<name>"?} },
#     "instances": [ {"component": "<id>", "name": "<inst>"?,
#                     "placement": [...] | mate-by-frame via "mate"} ],
#     "mates": [ {child, parent, child_iface, parent_iface, verify_align?} ]?
#   }


def _manifest_from_brief(brief, comp_files, root_path):
    """Project a brief + built component files into the JSON merge_assembly reads.
    Strips the agent-only `task` field; keeps file/object/envelope + instances/mates."""
    components = {}
    for cid, spec in brief["components"].items():
        m = {"file": str(comp_files[cid])}
        if spec.get("object"):
            m["object"] = spec["object"]
        if spec.get("envelope"):
            m["envelope"] = spec["envelope"]
        components[cid] = m
    return {
        "name": brief.get("name", "assembly"),
        "root": str(root_path),
        "components": components,
        "instances": brief["instances"],
        "mates": brief.get("mates", []),
    }


# --- which components a failing gate implicates (renegotiation targeting) ------

def _instance_to_component(brief):
    return {inst.get("name", inst["component"]): inst["component"]
            for inst in brief["instances"]}


def _link_to_component(placed):
    """merge_assembly's `placed` maps instance->link name; invert to link->component
    via instance identity (placed entries carry both)."""
    return {p["instance"]: p["component"] for p in placed}


def _implicated(brief, gates, placed):
    """Components a coordinator should re-dispatch given the failing gates. Maps gate
    findings (which name LINKS / instances) back to component ids. Falls back to all
    components if a finding can't be resolved (better to rebuild than silently skip)."""
    inst2comp = _instance_to_component(brief)
    # link Name -> instance name: merge names links after the instance, so the link
    # Name usually equals the instance name; fall back to instance list order.
    impl = set()
    # interference: pairs of link names a, b
    for row in gates.get("interference", []):
        for key in ("a", "b"):
            nm = row.get(key, "")
            comp = inst2comp.get(nm)
            if comp:
                impl.add(comp)
    # envelope: each violation names a part (link name)
    for v in gates.get("envelope", []):
        comp = inst2comp.get(v.get("part", ""))
        if comp:
            impl.add(comp)
    # interface alignment: child/parent are link names
    for v in gates.get("interface_align", []):
        for key in ("child", "parent"):
            comp = inst2comp.get(v.get(key, ""))
            if comp:
                impl.add(comp)
    if not impl:
        impl = set(brief["components"])
    return impl


# --- the loop -----------------------------------------------------------------

def _build_component(client, model, cid, brief, comp_files, log):
    task = brief["components"][cid]["task"]
    r = agentkit.run_builder(client, model, task, comp_files[cid])
    log(f"    build {cid}: turns={r['turns']} saved={r['ok_built']}")
    return r


def orchestrate(client, model, brief, workdir, max_rounds=3, renegotiate=None,
                log=print):
    """Run the full coordinator loop. Returns a structured report.

    client     : anthropic.Anthropic() or agentkit.ScriptedClient
    brief      : the design brief (see schema above)
    workdir    : dir for component files + the merged root .FCStd
    renegotiate: optional fn(brief, implicated, gates, round) -> brief'. Lets a
                 caller adjust the contract (move a frame, grow an envelope) before
                 re-dispatch. Default: re-dispatch implicated components unchanged
                 (useful when failures are stochastic agent errors, not contract
                 conflicts).
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    comp_files = {cid: workdir / spec["file"]
                  for cid, spec in brief["components"].items()}
    root_path = workdir / (brief.get("root") or (brief.get("name", "assembly") + ".FCStd"))

    agents = {}
    usage = {"in_tokens": 0, "out_tokens": 0, "cache_read": 0, "cache_write": 0}
    rounds = []

    def _accumulate(r):
        for k in usage:
            usage[k] += r.get(k, 0)

    # round 0: build everything
    log("round 0: fan out all builders")
    to_build = list(brief["components"])
    for r in range(max_rounds):
        for cid in to_build:
            res = _build_component(client, model, cid, brief, comp_files, log)
            agents[cid] = res
            _accumulate(res)

        built = all(agents[c]["ok_built"] and comp_files[c].exists()
                    for c in brief["components"])
        if not built:
            missing = [c for c in brief["components"]
                       if not (agents.get(c, {}).get("ok_built") and comp_files[c].exists())]
            rounds.append({"round": r, "built": to_build, "ok": False,
                           "reason": f"did not save: {missing}"})
            log(f"  round {r}: FAILED to build {missing}")
            return _report(False, brief, rounds, usage, model, str(root_path))

        # merge + gates via the DriftPin primitive
        manifest = _manifest_from_brief(brief, comp_files, root_path)
        mpath = workdir / f"_manifest_round{r}.json"
        mpath.write_text(json.dumps(manifest))
        with Worker() as w:
            merged = w.call("merge_assembly", manifest=str(mpath))
        gates = merged["gates"]
        ok = merged["ok"]
        rounds.append({"round": r, "built": to_build, "ok": ok,
                       "gates": gates, "placed": merged["placed"]})
        log(f"  round {r}: merge ok={ok}  "
            f"interference={len(gates.get('interference', []))} "
            f"envelope={len(gates.get('envelope', []))} "
            f"align={len(gates.get('interface_align', []))}")

        if ok:
            return _report(True, brief, rounds, usage, model, str(root_path))

        if r + 1 >= max_rounds:
            log("  out of rounds")
            break

        implicated = _implicated(brief, gates, merged["placed"])
        log(f"  renegotiate: re-dispatch {sorted(implicated)}")
        if renegotiate is not None:
            brief = renegotiate(brief, implicated, gates, r)
        to_build = sorted(implicated)

    return _report(False, brief, rounds, usage, model, str(root_path))


def _report(ok, brief, rounds, usage, model, root):
    return {"ok": ok, "name": brief.get("name"), "rounds": len(rounds),
            "root": root, "trace": rounds, "usage": usage,
            "cost_usd": round(agentkit.cost_of(usage, model), 4)}


def design_from_spec(client, model, spec, workdir, max_rounds=3, log=print):
    """Full Appendix A pipeline: free-text spec -> decompose -> orchestrate.
    Returns the orchestrate report with decompose cost folded in and the brief
    attached. Decomposition is billed on top of the build, so the report's
    cost_usd covers the whole design."""
    log("decompose: spec -> brief")
    brief, dcost = decompose(client, model, spec, log=log)
    rep = orchestrate(client, model, brief, workdir, max_rounds=max_rounds, log=log)
    for k, v in dcost.items():
        rep["usage"][k] = rep["usage"].get(k, 0) + v
    rep["cost_usd"] = round(agentkit.cost_of(rep["usage"], model), 4)
    rep["brief"] = brief
    return rep


# --- demo brief (shared by the dry run and the live runner) -------------------

_BORE_D, _CLEAR, _PLATE, _CTR, _PEGLEN = 16.0, 0.4, (60, 60, 10), (30, 30), 20.0

DEMO_BRIEF = {
    "name": "peg_demo",
    "root": "peg_demo.FCStd",
    "components": {
        "plate": {"file": "plate.FCStd",
                  "task": (f"Build a {_PLATE[0]}x{_PLATE[1]}x{_PLATE[2]} mm plate with a "
                           f"Ø{_BORE_D} through-hole at ({_CTR[0]},{_CTR[1]}), then "
                           f"save_component.")},
        "peg": {"file": "peg.FCStd",
                "task": (f"Build a peg that slip-fits a Ø{_BORE_D} bore with {_CLEAR} mm "
                         f"diametral clearance (compute the diameter), length {_PEGLEN} mm, "
                         f"then save_component.")},
    },
    "instances": [
        {"component": "plate", "name": "plate", "placement": [0, 0, 0]},
        {"component": "peg", "name": "peg", "placement": [_CTR[0], _CTR[1], -5]},
    ],
}


def _live_main():
    """Billed: run the coordinator on DEMO_BRIEF with a real model. Gated behind
    RUN_RELIABILITY=1 + ANTHROPIC_API_KEY. M2_MODEL selects the tier (default
    haiku). Confirms the whole loop works with a real model, not just the stub."""
    import os
    import sys
    import tempfile

    if not os.environ.get("RUN_RELIABILITY"):
        print("Gated behind RUN_RELIABILITY=1 (calls the Anthropic API, costs "
              "credits). For a FREE end-to-end check run:  python -m orchestration.dryrun",
              file=sys.stderr)
        sys.exit(0)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Run `anthropic-key` first.", file=sys.stderr)
        sys.exit(1)

    model = agentkit.MODELS.get(os.environ.get("M2_MODEL", "haiku"))
    if model is None:
        print(f"ERROR: M2_MODEL must be one of {sorted(agentkit.MODELS)}", file=sys.stderr)
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic()
    spec = os.environ.get("M2_SPEC")
    with tempfile.TemporaryDirectory() as td:
        if spec:
            # full pipeline: free-text spec -> decompose -> build
            print(f"== coordinator live run (spec -> decompose -> build) — model={model} ==")
            rep = design_from_spec(client, model, spec, Path(td), max_rounds=3)
            comps = sorted((rep.get("brief") or {}).get("components", {}))
            print(f"  brief: {rep['brief']['name']} -> {comps}")
        else:
            print(f"== coordinator live run (DEMO_BRIEF) — model={model} ==")
            rep = orchestrate(client, model, DEMO_BRIEF, Path(td), max_rounds=3)
    print(f"\n  ok={rep['ok']}  rounds={rep['rounds']}  cost=${rep['cost_usd']}")
    for rd in rep["trace"]:
        g = rd.get("gates", {})
        detail = (f"interference={len(g.get('interference', []))} "
                  f"envelope={len(g.get('envelope', []))} "
                  f"align={len(g.get('interface_align', []))}") if "gates" in rd \
            else rd.get("reason", "")
        print(f"    round {rd['round']}: built={rd['built']} ok={rd['ok']}  {detail}")
    sys.exit(0 if rep["ok"] else 1)


if __name__ == "__main__":
    _live_main()
