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
