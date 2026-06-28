"""
Manifest contract machinery — the resolve step (RFC docs/MULTI_AGENT.md §11.1).

The 2026-06-12 evals measured the dominant multi-agent failure mode: agents
re-deriving shared math. The unequal grid chain (tchainu) made it conclusive —
NO agent condition reliably reconciles a global constraint against a
manufacturing grid (partition 2/20, single 0/20). The fix is structural, not
better prompting: a deterministic resolve step evaluates the manifest's global
constraints in plain code and writes *literal resolved values* into each
component's slice before fan-out. Builders receive only numbers — never a
formula to re-derive, never a sibling's value to guess.

This module is pure Python (no FreeCAD, no LLM) so any host — the reference
coordinator, a CI script, another AI tool — can run it on the shared manifest.

Constraint schema (v0 — deliberately small; RFC §13 warns where this stops):

    "components": {
      "segA": { "file": "segA.FCStd", "parameters": { "len": 12.6 } },
      ...
    },
    "constraints": {
      "total_length": {
        "sum": ["segA.len", "segB.len", ...],   # refs: <component>.<parameter>
        "equals": 100.0,                          # required target
        "grid_mm": 1.0                            # optional manufacturing grid
      }
    }

resolve_constraints() returns a deep-copied manifest whose referenced
parameters are replaced by resolved literals, plus a "resolved" audit block.
Infeasible or malformed constraints raise ValueError loudly — a bad contract
must fail before any geometry (or token) is spent on it.
"""
import copy

_TOL = 1e-9


def _get_param(comps, ref):
    cid, _, pname = ref.partition(".")
    if not pname:
        raise ValueError(f"constraint ref {ref!r} must be '<component>.<parameter>'")
    if cid not in comps:
        raise ValueError(f"constraint ref {ref!r}: unknown component {cid!r}")
    params = comps[cid].get("parameters") or {}
    if pname not in params:
        raise ValueError(f"constraint ref {ref!r}: component {cid!r} has no "
                         f"parameter {pname!r}")
    v = params[pname]
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"constraint ref {ref!r}: parameter is not a number ({v!r})")
    return float(v)


def _set_param(comps, ref, value):
    cid, _, pname = ref.partition(".")
    comps[cid]["parameters"][pname] = value


def _resolve_sum(comps, name, spec):
    """Resolve a sum(...) == equals constraint, optionally on a grid.

    Without a grid the residual is spread equally (deterministic, keeps the
    values as close to nominal as possible). With a grid, every value is
    snapped to the nearest grid step, then the leftover excess is walked off
    one grid step at a time from the LAST ref backwards — the reconciliation a
    correct global builder would do (trim the trailing segments), and the same
    one the eval's reference solution encodes."""
    refs = spec["sum"]
    if not isinstance(refs, list) or len(refs) < 2:
        raise ValueError(f"constraint {name!r}: 'sum' needs a list of >=2 refs")
    target = spec["equals"]
    if not isinstance(target, (int, float)) or isinstance(target, bool):
        raise ValueError(f"constraint {name!r}: 'equals' must be a number")
    grid = spec.get("grid_mm")
    nominals = [_get_param(comps, r) for r in refs]

    if grid is None:
        residual = (target - sum(nominals)) / len(refs)
        values = [v + residual for v in nominals]
    else:
        if grid <= 0:
            raise ValueError(f"constraint {name!r}: grid_mm must be > 0")
        units_target = target / grid
        if abs(units_target - round(units_target)) > _TOL:
            raise ValueError(f"constraint {name!r}: equals={target} is not "
                             f"representable on grid_mm={grid}")
        units = [round(v / grid) for v in nominals]
        excess = int(round(sum(units) - units_target))
        i = len(units) - 1
        step = 1 if excess > 0 else -1
        while excess != 0:
            units[i] -= step
            if units[i] <= 0:
                raise ValueError(f"constraint {name!r}: reconciliation drives "
                                 f"{refs[i]!r} to a non-positive length")
            excess -= step
            i = (i - 1) % len(units)
        values = [u * grid for u in units]

    for ref, v in zip(refs, values):
        _set_param(comps, ref, v)
    return {"values": dict(zip(refs, values)),
            "residual": sum(values) - target}


_KINDS = {"sum"}


def resolve_constraints(manifest):
    """Evaluate manifest['constraints'] and return a deep-copied manifest whose
    referenced component parameters are literal resolved values, with a
    'resolved' audit block. Raises ValueError on any malformed or infeasible
    constraint — fail loudly before fan-out, never hand a builder a guess."""
    out = copy.deepcopy(manifest)
    constraints = out.get("constraints") or {}
    comps = out.get("components") or {}
    audit = {}
    for name, spec in constraints.items():
        if not isinstance(spec, dict):
            raise ValueError(f"constraint {name!r} must be an object")
        kinds = _KINDS & set(spec)
        if len(kinds) != 1:
            raise ValueError(
                f"constraint {name!r}: expected exactly one of {sorted(_KINDS)} "
                f"(got keys {sorted(spec)}) — unknown constraint kinds fail "
                f"loudly rather than resolve to silence")
        audit[name] = _resolve_sum(comps, name, spec)
    out["resolved"] = audit
    return out


# --- relations hook (issue #137) ---------------------------------------------
# The generalized resolve flow. A2 (driving/driven relations) is an append-only
# layer over this module — it lives in driftpin/relations.py (the parser /
# evaluator / DAG) and is invoked here WITHOUT touching resolve_constraints'
# body, so #137 stays localized and back-compatible. Order matters: relations
# compute literal driven values into the component slices FIRST, then the
# sum/grid constraints reconcile over those literals.

def resolve_manifest(manifest):
    """Resolve a manifest end to end: first the driving/driven relation DAG
    (driftpin.relations, #137 — arithmetic + table lookup, no solver), then the
    sum/grid global constraints (:func:`resolve_constraints`, §11.1). A manifest
    with neither relations nor constraints is returned as a deep copy unchanged.
    Raises ValueError loudly on any malformed/infeasible/double-driven/cyclic
    input — the whole contract fails before any builder is billed."""
    from driftpin.relations import resolve_relations  # local: keep import light
    out = resolve_relations(manifest)
    if out.get("constraints"):
        out = resolve_constraints(out)
    return out
