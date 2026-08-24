"""
Change orders + where-used impact + baselines (issue #142, the C3 item of
docs/DESIGN_HIERARCHY.md §2 Theme C). The third leg of the design-control layer,
on top of the C1 item model (ankusdrive/items.py, #140) and the C2 revision +
lifecycle state machine (ankusdrive/lifecycle.py, #141).

The thesis (DESIGN_HIERARCHY §2 C3): turn change into a *record*, not a silent
mutation.

  * An **ECO** (Engineering Change Order) is an object — the **affected items**,
    the **disposition**, and an **effectivity** (date / serial / revision). The
    change is a record that maps onto a git commit / PR: the diff IS the change
    order. `make_eco` / `validate_eco` build and gate it.

  * **Where-used / impact analysis** traverses the existing lockfile dependency
    graph (`depends_on`, MULTI_AGENT.md §9) to report every parent that *consumes*
    a changed item — the blast radius — so exactly those parents re-dispatch. This
    surfaces the §9 `stale` set as an item-level impact report, *before* a change
    is committed. `where_used` / `impact` compute it.

  * A **baseline** is a labeled, immutable snapshot = a pinned `{item: revision}`
    set (a git-tag / lockfile over the item graph) for reproducible rebuilds.
    `create_baseline` pins it; `verify_baseline` proves a rebuild reproduces it
    and catches a drifted input. `serialize_baseline` is the git-diffable text
    sidecar format.

Pure Python — no FreeCAD, no LLM, no key — so any host (the reference coordinator,
a CI script, another tool) can build an ECO, run the where-used analysis, and pin
or verify a baseline, exactly like ankusdrive/items.py and ankusdrive/lifecycle.py.
The worker.py / mcp_server.py handlers are thin append-only wrappers over this
module; it reads the lockfile (the §9 dependency graph) through its existing
on-disk form — the JSON `assembly_lock` writes — and never reaches into the
worker's lock-writing body.

--- The lockfile dependency graph (read, never written, here) ------------------

`assembly_lock` (worker.py) writes a lockfile sidecar:

    { "manifest": "...", "components": {
        "housing": { "file": "...", "interfaces_hash": "...", "depends_on": [] },
        "lid":     { "file": "...", "interfaces_hash": "...",
                     "depends_on": ["housing"] } } }

A component's `depends_on` lists the components it MATES TO (consumes) — `lid`
depends_on `housing` because lid mates against housing's published interface.
So the graph edge is **consumer -> consumed** (lid -> housing). "Where is item X
used?" is therefore the REVERSE reachability of X: every node that reaches X by
following `depends_on` edges. When X's interface moves, exactly those nodes go
stale (the §9 mechanism) and must re-dispatch.

--- The ECO record (a git-diffable JSON object) --------------------------------

eco (schema "ankusdrive.eco/1"):

    { "schema": "ankusdrive.eco/1",
      "id": "ECO-0001",
      "title": "raise bore tolerance",
      "affected": ["housing"],          # the changed items (lockfile node ids)
      "disposition": "revise",          # use-as-is | rework | scrap | revise | ...
      "effectivity": { "revision": "B" },   # exactly one of date|serial|revision
      "interface_change": true }        # did a published interface move (§9)?

--- The baseline sidecar (a pinned {item: rev} snapshot) -----------------------

baseline (schema "ankusdrive.baseline/1"):

    { "schema": "ankusdrive.baseline/1",
      "label": "v1.0",
      "items": {
        "housing": { "part_number": "DP-000001", "rev": "A",
                     "fingerprint": "<blake2b of the artifact bytes>" } } }

The fingerprint pins the *bytes*, so a rebuild is verifiable byte-for-byte: a
drifted input (the file changed, or the rev moved) is caught by `verify_baseline`.
"""
import copy
import hashlib
import json
import os

ECO_SCHEMA = "ankusdrive.eco/1"
BASELINE_SCHEMA = "ankusdrive.baseline/1"

# An effectivity is exactly one of these kinds (date / serial / revision) — the
# classic ECO effectivity forms. "date" = effective on/after a date; "serial" =
# from a unit serial number; "revision" = from a part revision.
EFFECTIVITY_KINDS = ("date", "serial", "revision")

# A disposition is free-form but drawn from the conventional MRB vocabulary by
# default; an unknown string is allowed (domain dispositions exist) but a missing
# one is a loud error.
KNOWN_DISPOSITIONS = ("use_as_is", "rework", "scrap", "return_to_supplier",
                      "revise", "new_part_number")


# === the lockfile dependency graph (read the §9 lockfile, never write it) ======

def load_lockfile(path):
    """Read a lockfile sidecar (the JSON `assembly_lock` wrote) into a dict.
    Raises on unreadable/!JSON — a missing graph must fail loudly, never resolve to
    an empty (and therefore silently impact-free) graph (the house rule)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dependency_graph(lock):
    """The `depends_on` adjacency from a lockfile: {node -> set(nodes it consumes)}.
    Edge direction is consumer -> consumed (lid -> housing), exactly as the §9
    lockfile records it. Every node referenced in any `depends_on` is included as a
    key even if it has no outgoing edges, so the graph is closed."""
    comps = lock.get("components", {})
    graph = {cid: set(spec.get("depends_on", []) or []) for cid, spec in comps.items()}
    # close the graph over any depended-on node not itself a component key
    for deps in list(graph.values()):
        for d in deps:
            graph.setdefault(d, set())
    return graph


def _reverse_graph(graph):
    """Reverse adjacency: {node -> set(nodes that directly consume it)}. If
    lid -> housing in `graph` (lid depends_on housing), then housing -> lid here
    (housing is used by lid)."""
    rev = {n: set() for n in graph}
    for consumer, deps in graph.items():
        for d in deps:
            rev.setdefault(d, set()).add(consumer)
    return rev


def direct_dependents(lock, item):
    """The nodes that consume `item` DIRECTLY — every node whose `depends_on`
    contains `item`. This is the §9 immediate-neighbour layer (the parts that mate
    against item's interface). Raises KeyError if `item` is not a graph node — an
    unknown item is a loud error, never a silent empty result."""
    graph = dependency_graph(lock)
    if item not in graph:
        raise KeyError(
            f"no item {item!r} in the lockfile graph (nodes: {sorted(graph)})")
    return sorted(n for n, deps in graph.items() if item in deps)


def where_used(lock, item):
    """Full where-used / blast-radius of `item`: every node that consumes it
    TRANSITIVELY — the reverse reachability of `item` over `depends_on`. If a
    change moves item's interface, exactly this set must re-evaluate. Returns a
    sorted list (excluding `item` itself). Raises KeyError on an unknown item.

    Worked example (the gearbox fixture):
      depends_on: lid->[housing], shaft->[housing], gear->[shaft], cover->[lid]
      where_used("housing") == ["cover", "gear", "lid", "shaft"]   # all of it
      where_used("shaft")   == ["gear"]
      where_used("gear")    == []                                   # a top consumer
    """
    graph = dependency_graph(lock)
    if item not in graph:
        raise KeyError(
            f"no item {item!r} in the lockfile graph (nodes: {sorted(graph)})")
    rev = _reverse_graph(graph)
    seen = set()
    stack = list(rev.get(item, ()))
    while stack:
        n = stack.pop()
        if n in seen or n == item:
            continue
        seen.add(n)
        stack.extend(rev.get(n, ()))
    return sorted(seen)


def impact(lock, changed_items):
    """The where-used impact report for a set of changed items — the blast radius a
    change order carries. Mirrors the §9 classification but at item level and
    BEFORE the change is applied:

      * `stale`      — the §9 immediate re-dispatch set: nodes that consume a
                       changed item DIRECTLY and are not themselves in the changed
                       set (the neighbours that must re-evaluate first).
      * `where_used` — the full TRANSITIVE blast radius: every node that consumes
                       any changed item, directly or through a chain (the complete
                       set that eventually re-dispatches as the change propagates).
      * `ok`         — True iff the blast radius is empty (no consumer impacted).

    Returns {changed, stale, where_used, ok}. Pure + deterministic. Raises
    KeyError if any changed item is not a graph node."""
    graph = dependency_graph(lock)
    changed = set(changed_items)
    for it in changed:
        if it not in graph:
            raise KeyError(
                f"no item {it!r} in the lockfile graph (nodes: {sorted(graph)})")
    stale = sorted(n for n, deps in graph.items()
                   if n not in changed and (deps & changed))
    blast = set()
    for it in changed_items:
        blast |= set(where_used(lock, it))
    blast -= changed
    return {"changed": sorted(changed), "stale": stale,
            "where_used": sorted(blast), "ok": not blast}


# === the ECO record (a change order = a git-diffable object) ===================

def make_eco(eco_id, affected, disposition, effectivity, *, title=None,
             interface_change=False, note=None):
    """Build an ECO record: the affected items, the disposition, and the
    effectivity. A change becomes a *record*, not a silent mutation (the C3
    thesis). `affected` is the list of changed item / lockfile-node ids;
    `effectivity` is a dict with exactly one of date|serial|revision;
    `interface_change` flags whether a published interface moved (the §9 stale
    trigger). Returns the ECO dict (validate it with validate_eco)."""
    eco = {
        "schema": ECO_SCHEMA,
        "id": eco_id,
        "affected": list(affected),
        "disposition": disposition,
        "effectivity": dict(effectivity) if effectivity else {},
        "interface_change": bool(interface_change),
    }
    if title is not None:
        eco["title"] = title
    if note is not None:
        eco["note"] = note
    return eco


def _validate_effectivity(eff, problems):
    if not isinstance(eff, dict):
        problems.append("effectivity must be an object")
        return
    kinds = [k for k in EFFECTIVITY_KINDS if k in eff]
    if len(kinds) != 1:
        problems.append(
            f"effectivity must have exactly one of {list(EFFECTIVITY_KINDS)} "
            f"(has {kinds or 'none'})")


def validate_eco(eco):
    """Structural validation of an ECO record. Returns a list of human-readable
    problems; empty == valid (mirrors items.validate_registry / _validate_manifest).
    Catches: a wrong/absent schema, a missing id, an empty affected set, a missing
    disposition, and a malformed effectivity (zero or several kinds)."""
    problems = []
    if not isinstance(eco, dict):
        return ["eco must be a JSON object"]

    schema = eco.get("schema")
    if schema is not None and schema != ECO_SCHEMA:
        problems.append(f"unknown schema {schema!r} (expected {ECO_SCHEMA!r} or none)")

    if not isinstance(eco.get("id"), str) or not eco.get("id"):
        problems.append("eco id must be a non-empty string")

    affected = eco.get("affected")
    if not isinstance(affected, list) or not affected:
        problems.append("affected must be a non-empty list of item ids")
    elif not all(isinstance(a, str) and a for a in affected):
        problems.append("every affected entry must be a non-empty item-id string")

    disp = eco.get("disposition")
    if not isinstance(disp, str) or not disp:
        problems.append("disposition must be a non-empty string")

    _validate_effectivity(eco.get("effectivity", {}), problems)

    if "interface_change" in eco and not isinstance(eco["interface_change"], bool):
        problems.append("interface_change must be a boolean")

    return problems


def eco_impact(eco, lock):
    """The where-used impact of an ECO over a lockfile graph — the blast radius the
    change carries, surfacing the §9 stale set at item level. Computes
    impact(lock, eco.affected): the direct `stale` consumers (the immediate
    re-dispatch layer) and the full transitive `where_used` blast radius. Raises
    KeyError if an affected item is not a graph node (a change order against an
    item nothing in the graph knows is a loud error)."""
    return impact(lock, eco.get("affected", []))


def eco_with_impact(eco, lock):
    """A copy of the ECO annotated with its computed impact report under an
    `impact` key — the self-contained change record (the ECO *plus* the blast
    radius it implies), ready to serialise next to the commit it represents."""
    out = copy.deepcopy(eco)
    out["impact"] = eco_impact(eco, lock)
    return out


def serialize_eco(eco):
    """Canonical, git-diffable JSON text for an ECO (sorted keys, 2-space indent) —
    deterministic bytes so two equal ECOs serialise identically (the diff is the
    change order)."""
    return json.dumps(eco, indent=2, sort_keys=True)


# === baselines (a labeled, immutable {item: rev} snapshot) =====================

def _fingerprint_bytes(data):
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def fingerprint_file(path):
    """blake2b fingerprint of a file's bytes — the content pin a baseline records
    so a rebuild is verifiable byte-for-byte. Raises if the file is missing (a
    baseline cannot pin bytes that aren't there)."""
    with open(path, "rb") as f:
        return _fingerprint_bytes(f.read())


def _item_fingerprint(rec, base_dir):
    """A content fingerprint for an item: the blake2b over each of its artifact
    files' bytes, combined in declared order. Missing files are recorded as a
    sentinel so verify catches a vanished artifact rather than silently matching."""
    h = hashlib.blake2b(digest_size=16)
    for fp in rec.get("files", []):
        full = fp if os.path.isabs(fp) else os.path.join(base_dir, fp)
        if os.path.exists(full):
            with open(full, "rb") as f:
                h.update(f.read())
        else:
            h.update(b"\x00<missing>")
        h.update(b"\x00")
    return h.hexdigest()


def create_baseline(label, registry, *, base_dir=".", items=None, note=None):
    """Pin a labeled, immutable baseline = a snapshot of {item -> (part_number,
    rev, fingerprint)} over the item registry (the C3 reproducible-rebuild
    primitive). `items` optionally restricts the pin to a subset of item ids
    (default: every item). The fingerprint is the blake2b of the item's artifact
    bytes (resolved against `base_dir`), so a rebuild is verifiable byte-for-byte.

    Returns the baseline dict (a git-diffable sidecar; serialise with
    serialize_baseline). Deterministic: same registry + same artifact bytes ->
    identical baseline bytes."""
    reg_items = registry.get("items", {})
    ids = list(items) if items is not None else sorted(reg_items)
    pinned = {}
    for iid in ids:
        if iid not in reg_items:
            raise KeyError(f"no item {iid!r} in registry to pin in the baseline")
        rec = reg_items[iid]
        pinned[iid] = {
            "part_number": rec.get("part_number"),
            "rev": rec.get("rev", "-"),
            "fingerprint": _item_fingerprint(rec, base_dir),
        }
    baseline = {"schema": BASELINE_SCHEMA, "label": label, "items": pinned}
    if note is not None:
        baseline["note"] = note
    return baseline


def validate_baseline(baseline):
    """Structural validation of a baseline sidecar. Returns a list of problems;
    empty == well-formed. Catches a wrong schema, a missing label, and a malformed
    pinned-item record (no rev / no fingerprint)."""
    problems = []
    if not isinstance(baseline, dict):
        return ["baseline must be a JSON object"]
    schema = baseline.get("schema")
    if schema is not None and schema != BASELINE_SCHEMA:
        problems.append(
            f"unknown schema {schema!r} (expected {BASELINE_SCHEMA!r} or none)")
    if not isinstance(baseline.get("label"), str) or not baseline.get("label"):
        problems.append("baseline label must be a non-empty string")
    items = baseline.get("items")
    if not isinstance(items, dict):
        problems.append("baseline items must be an object")
        return problems
    for iid, pin in items.items():
        if not isinstance(pin, dict):
            problems.append(f"baseline item {iid!r} must be an object")
            continue
        if not isinstance(pin.get("rev"), str):
            problems.append(f"baseline item {iid!r} must pin a rev string")
        if not isinstance(pin.get("fingerprint"), str) or not pin.get("fingerprint"):
            problems.append(f"baseline item {iid!r} must pin a fingerprint")
    return problems


def verify_baseline(baseline, registry, *, base_dir="."):
    """Verify a rebuild against a baseline: re-resolve every pinned item from the
    current registry + artifacts and check it still matches the pinned rev AND the
    pinned content fingerprint. This is the reproducible-rebuild gate — a drifted
    input (the rev moved, a file's bytes changed, or the item / artifact vanished)
    is caught, never silently accepted.

    Returns {ok, label, drifted, missing} where:
      * `drifted` — [{item, field, expected, actual}] for each rev/fingerprint
                    mismatch (a changed input),
      * `missing` — [item] for each pinned item absent from the registry,
      * `ok`      — True iff nothing drifted and nothing is missing (the rebuild
                    reproduces the baseline exactly)."""
    reg_items = registry.get("items", {})
    drifted, missing = [], []
    for iid, pin in baseline.get("items", {}).items():
        rec = reg_items.get(iid)
        if rec is None:
            missing.append(iid)
            continue
        cur_rev = rec.get("rev", "-")
        if cur_rev != pin.get("rev"):
            drifted.append({"item": iid, "field": "rev",
                            "expected": pin.get("rev"), "actual": cur_rev})
        cur_fp = _item_fingerprint(rec, base_dir)
        if cur_fp != pin.get("fingerprint"):
            drifted.append({"item": iid, "field": "fingerprint",
                            "expected": pin.get("fingerprint"), "actual": cur_fp})
    ok = not drifted and not missing
    return {"ok": ok, "label": baseline.get("label"),
            "drifted": drifted, "missing": sorted(missing)}


def serialize_baseline(baseline):
    """Canonical, git-diffable JSON text for a baseline (sorted keys, 2-space
    indent) — deterministic bytes, so pinning the same state twice yields a
    byte-identical sidecar (the reproducibility the baseline promises)."""
    return json.dumps(baseline, indent=2, sort_keys=True)
