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
from driftpin.manifest import resolve_constraints
from . import agentkit


# --- the resolve step (RFC §11.1) ----------------------------------------------
#
# Global constraints (totals, grids) are evaluated in plain code BEFORE fan-out
# and each builder's task receives literal values — agents must never share a
# derivation, only a result (the tchainu eval: partition 2/20, single 0/20 when
# agents were asked to reconcile a global sum themselves). Tasks may reference
# resolved parameters as str.format placeholders: "a block {len:.0f} mm long".

def resolve_brief(brief, log=print):
    """If the brief carries constraints, resolve them and substitute each
    component's resolved parameters into its task text. Raises ValueError on an
    infeasible contract — by design this fails BEFORE any builder is billed."""
    if not brief.get("constraints"):
        return brief
    brief = resolve_constraints(brief)
    for cid, spec in brief["components"].items():
        params = spec.get("parameters")
        if params and spec.get("task"):
            spec["task"] = spec["task"].format(**params)
    log(f"  resolve: {sorted(brief['resolved'])} -> literal values in "
        f"{len(brief['components'])} slices")
    return brief


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
    findings (which name LINKS / instances) back to component ids, blaming the part
    that actually violated its contract where the gates let us tell.

    Falls back to all components if a finding can't be resolved (better to rebuild
    than silently skip)."""
    inst2comp = _instance_to_component(brief)
    # link Name -> instance name: add_part names the link after the instance, so in
    # a fresh merge doc with unique instance names the link Name equals the instance
    # name; that's the key the gates report parts under.
    impl = set()

    # Envelope is unambiguous: the named part grew past its declared keep-out box,
    # so it is the culprit. Collect these first — they disambiguate interference.
    envelope_culprits = set()
    for v in gates.get("envelope", []):
        comp = inst2comp.get(v.get("part", ""))
        if comp:
            envelope_culprits.add(comp)
            impl.add(comp)

    # Interference is a RELATIONSHIP — a clash names two parts (a, b) and geometry
    # alone can't say which is wrong. But if exactly one party also busts its own
    # envelope, that one grew into its neighbor: blame only it. If both or neither
    # have an envelope signal, we genuinely can't tell, so blame both.
    for row in gates.get("interference", []):
        pair = [inst2comp.get(row.get(k, "")) for k in ("a", "b")]
        pair = [c for c in pair if c]
        guilty = [c for c in pair if c in envelope_culprits]
        impl.update(guilty if len(guilty) == 1 else pair)

    # Interface alignment names child/parent. The child is the part being mated onto
    # the parent; if the parent is an anchor (placed by raw placement, not itself
    # mated) it's fixed ground truth, so blame the child. If the parent is also
    # mated, either frame could be off — blame both.
    mated_children = {m.get("child") for m in brief.get("mates", [])}
    for inst in brief.get("instances", []):
        if inst.get("mate"):
            mated_children.add(inst.get("name", inst["component"]))
    for v in gates.get("interface_align", []):
        child = inst2comp.get(v.get("child", ""))
        parent_inst = v.get("parent", "")
        parent = inst2comp.get(parent_inst)
        if child:
            impl.add(child)
        if parent and parent_inst in mated_children:
            impl.add(parent)

    if not impl:
        impl = set(brief["components"])
    return impl


# --- round 0 contract review (RFC §8 / §11.8) --------------------------------
#
# Before any geometry, each builder reads ONLY its slice and returns accept/amend.
# Amendments fold back into the brief (grow an envelope, append a clarifying note)
# so an infeasible contract is fixed for the price of a short completion, not a
# full build → merge → gate-fail → rebuild cycle. Sub-brief nodes are reviewed by
# their own round 0 when they orchestrate, so they are skipped here.

def _slice_text(brief, cid):
    spec = brief["components"][cid]
    parts = [f"Component '{cid}' of assembly '{brief.get('name', 'assembly')}'.",
             f"Task: {spec['task']}"]
    if brief.get("shared_parameters"):
        parts.append(f"Shared parameters every component must honor: "
                     f"{brief['shared_parameters']}")
    if spec.get("envelope"):
        parts.append(f"Declared keep-out envelope your bbox must fit inside: "
                     f"{spec['envelope']}")
    return "\n".join(parts)


def round0_review(client, model, brief, log=print):
    """Run each leaf builder's feasibility review of its slice. Returns
    (reviews, usage). reviews[cid] = {verdict, reason, patch, ...usage}."""
    reviews = {}
    usage = {"in_tokens": 0, "out_tokens": 0, "cache_read": 0, "cache_write": 0}
    for cid, spec in brief["components"].items():
        if "sub_brief" in spec:
            continue
        r = agentkit.run_reviewer(client, model, _slice_text(brief, cid))
        reviews[cid] = r
        for k in usage:
            usage[k] += r.get(k, 0)
        log(f"    review {cid}: {r['verdict']} — {r['reason']}")
    return reviews, usage


def apply_reviews(brief, reviews, log=print):
    """Fold round-0 amendments into the brief before fan-out. Returns a new brief."""
    import copy
    brief = copy.deepcopy(brief)
    for cid, r in reviews.items():
        if r.get("verdict") != "amend":
            continue
        patch = r.get("patch") or {}
        if patch.get("envelope"):
            brief["components"][cid]["envelope"] = patch["envelope"]
            log(f"    amend {cid}: envelope -> {patch['envelope']}")
        if patch.get("task_note"):
            brief["components"][cid]["task"] += "\n" + patch["task_note"]
            log(f"    amend {cid}: task note appended")
    return brief


# --- the loop -----------------------------------------------------------------

def _build_component(client, model, cid, brief, comp_files, log):
    task = brief["components"][cid]["task"]
    comp_files[cid].parent.mkdir(parents=True, exist_ok=True)  # §7 per-builder dir
    r = agentkit.run_builder(client, model, task, comp_files[cid])
    log(f"    build {cid}: turns={r['turns']} saved={r['ok_built']}")
    return r


def orchestrate(client, model, brief, workdir, max_rounds=3, renegotiate=None,
                review=True, log=print):
    """Run the full coordinator loop (RFC Appendix A). Returns a structured report.

    client     : anthropic.Anthropic() or agentkit.ScriptedClient
    brief      : the design brief. A component may carry a `sub_brief` (a nested
                 brief) instead of file/task — a SUBASSEMBLY node, orchestrated
                 first and linked as the parent's component (§11.4 / §11.8).
    workdir    : dir for component files + the merged root .FCStd
    renegotiate: optional fn(brief, implicated, gates, round) -> brief'.
    review     : run the round-0 contract review before building (default True).

    Pipelined fan-in (§8): each sub_brief node is orchestrated independently and
    gated on its own, so unrelated branches don't block each other and a
    subassembly failure is isolated to its node. Per-builder isolation (§7): each
    leaf builds in workdir/build/<cid>/."""
    brief = resolve_brief(brief, log=log)  # constraints -> literal slice values
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    usage = {"in_tokens": 0, "out_tokens": 0, "cache_read": 0, "cache_write": 0}

    def _accumulate(r):
        for k in usage:
            usage[k] += r.get(k, 0)

    # round 0: cheap feasibility review, fold amendments before any geometry
    reviews = {}
    if review:
        log("round 0: contract review")
        reviews, rusage = round0_review(client, model, brief, log=log)
        _accumulate(rusage)
        brief = apply_reviews(brief, reviews, log=log)

    root_path = workdir / (brief.get("root") or (brief.get("name", "assembly") + ".FCStd"))
    rounds = []

    # pipelined fan-in: orchestrate each subassembly node first, independently —
    # each one builds + merges + gates on its own; siblings don't block each other.
    nodes = {}
    comp_files = {}
    for cid, spec in brief["components"].items():
        if "sub_brief" in spec:
            log(f"  node '{cid}': orchestrate subassembly")
            sub = orchestrate(client, model, spec["sub_brief"], workdir / cid,
                              max_rounds=max_rounds, renegotiate=renegotiate,
                              review=review, log=lambda m, c=cid: log(f"  [{c}] {m}"))
            nodes[cid] = sub
            for k in usage:
                usage[k] += sub["usage"].get(k, 0)
            comp_files[cid] = Path(sub["root"])
        else:
            comp_files[cid] = workdir / "build" / cid / spec["file"]  # §7 isolation

    failed_nodes = [cid for cid, s in nodes.items() if not s["ok"]]
    if failed_nodes:
        rounds.append({"round": 0, "built": [], "ok": False,
                       "reason": f"subassembly node(s) failed: {failed_nodes}"})
        log(f"  fan-in: subassembly node(s) failed: {failed_nodes}")
        return _report(False, brief, rounds, usage, model, str(root_path),
                       reviews, nodes)

    leaf_cids = [cid for cid, spec in brief["components"].items()
                 if "sub_brief" not in spec]
    agents = {}
    log("  fan out leaf builders")
    to_build = list(leaf_cids)
    for r in range(max_rounds):
        for cid in to_build:
            res = _build_component(client, model, cid, brief, comp_files, log)
            agents[cid] = res
            _accumulate(res)

        built = all(agents[c]["ok_built"] and comp_files[c].exists()
                    for c in leaf_cids)
        if not built:
            missing = [c for c in leaf_cids
                       if not (agents.get(c, {}).get("ok_built") and comp_files[c].exists())]
            rounds.append({"round": r, "built": to_build, "ok": False,
                           "reason": f"did not save: {missing}"})
            log(f"  round {r}: FAILED to build {missing}")
            return _report(False, brief, rounds, usage, model, str(root_path),
                           reviews, nodes)

        # merge the parent: leaf component files + the gated subassembly roots
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
            return _report(True, brief, rounds, usage, model, str(root_path),
                           reviews, nodes)

        if r + 1 >= max_rounds:
            log("  out of rounds")
            break

        # re-dispatch only implicated LEAF components (sub-briefs are gated already)
        implicated = _implicated(brief, gates, merged["placed"]) & set(leaf_cids)
        log(f"  renegotiate: re-dispatch {sorted(implicated)}")
        if renegotiate is not None:
            brief = renegotiate(brief, implicated, gates, r)
        to_build = sorted(implicated)

    return _report(False, brief, rounds, usage, model, str(root_path), reviews, nodes)


def _report(ok, brief, rounds, usage, model, root, reviews=None, nodes=None):
    rep = {"ok": ok, "name": brief.get("name"), "rounds": len(rounds),
           "root": root, "trace": rounds, "usage": usage,
           "cost_usd": round(agentkit.cost_of(usage, model), 4)}
    if reviews:
        rep["reviews"] = reviews
    if nodes:
        rep["nodes"] = {cid: {"ok": s["ok"], "root": s["root"]}
                        for cid, s in nodes.items()}
    return rep


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
