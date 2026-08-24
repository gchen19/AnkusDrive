"""
The builder brief — the host-agnostic contract-as-data a single component builder
receives (issue #169).

AnkusDrive's multi-agent contract (docs/MULTI_AGENT.md) is a partition-and-merge
design: a coordinator hands each builder a *slice* of the design, the builder
produces one `.FCStd`, and `merge_assembly` + the gates re-assemble and verify the
whole. Until now the slice a builder received lived only inside
`orchestration/coordinator.py::_slice_text` — bespoke prose emitted by the bundled
Anthropic-API loop, reachable by no other host. This module lifts that slice into a
**standalone, versioned schema** any MCP host can produce and any builder (a Claude
Code subagent, a Cursor task, a human) can consume with the full AnkusDrive tool
surface, not just the 6-tool loop in `orchestration/agentkit.py`.

A *builder brief* describes exactly one component:

    {
      "schema": "ankusdrive.builder_brief/1",
      "component": "housing",              # component id (unique in its assembly)
      "assembly": "gearbox",               # the assembly it belongs to (context)
      "task": "Build a 80x80x40 mm ...",   # NL build instruction, self-contained
      "output": "components/housing.FCStd",# where the builder saves its file
      "envelope": {"min": [0,0,0],         # keep-out box the local bbox must fit in
                   "max": [80,80,40]},
      "interfaces": {                      # frames the builder MUST publish
        "lid_seat": {"origin": [0,0,40], "z_axis": [0,0,1],
                     "tol_mm": 0.5, "angle_tol_deg": 1.0}
      },
      "shared_parameters": {"bolt": "M4", "wall_mm": 3.0},   # global facts to honor
      "material": "AISI 1045",             # optional, for DFx / mass
      "constraints": {"process": "cnc_milling", "min_wall_mm": 2.0},  # optional DFx
      "performance": {                     # optional quantitative spec (#226/#261)
        "requirements": [{"name": "dp_at_rated", "limit": {"max": 50.0}}]
      }
    }

`component`, `task`, and `output` are required; everything else is optional but
recommended — a brief with no `envelope` and no `interfaces` cannot be gated by
`component_contract_check`, so the builder-side half of the merge gate becomes a
no-op.

This module is **pure Python — no FreeCAD** — so it imports cleanly in any host,
in the worker, and in tests. `evaluate_contract` is the shared core of the
`component_contract_check` MCP tool: the worker extracts three primitives from the
built geometry (watertight verdict, world/local bbox, published frames) and this
function turns them + the brief into a pass/fail verdict. Keeping the judgement pure
means the tool's logic is testable with synthetic inputs and no worker process.
"""

SCHEMA = "ankusdrive.builder_brief/1"

# keys a brief may carry beyond the three required ones — used to flag typos loudly
_KNOWN_KEYS = {
    "schema", "component", "assembly", "task", "output", "envelope",
    "interfaces", "shared_parameters", "material", "constraints", "owner",
    "performance",
}


# --- validation ---------------------------------------------------------------

def validate_builder_brief(brief):
    """Structural validation of a builder brief. Returns a list of human-readable
    problems; empty == valid. Cheap, pure, and independent of any JSON-schema layer
    so a malformed brief fails loudly at the door instead of halfway through a build.
    """
    problems = []
    if not isinstance(brief, dict):
        return ["brief must be a dict"]
    schema = brief.get("schema")
    if schema is not None and schema != SCHEMA:
        problems.append(f"unknown schema {schema!r} (expected {SCHEMA!r})")
    if not (brief.get("component") or "").strip():
        problems.append("missing component id")
    if not (brief.get("task") or "").strip():
        problems.append("missing build task")
    if not (brief.get("output") or "").strip():
        problems.append("missing output path")
    env = brief.get("envelope")
    if env is not None:
        problems += _envelope_problems(env)
    ifaces = brief.get("interfaces")
    if ifaces is not None:
        if not isinstance(ifaces, dict):
            problems.append("interfaces must be a map of name -> frame spec")
        else:
            for name, spec in ifaces.items():
                if not isinstance(spec, dict):
                    problems.append(f"interface {name!r} spec must be a dict")
                    continue
                o = spec.get("origin")
                if o is not None and not _is_vec3(o):
                    problems.append(f"interface {name!r} origin must be [x,y,z]")
                for ax in ("z_axis", "x_axis"):
                    v = spec.get(ax)
                    if v is not None and not _is_vec3(v):
                        problems.append(f"interface {name!r} {ax} must be [x,y,z]")
    perf = brief.get("performance")
    if perf is not None:
        from ankusdrive.gates import performance as _pgate
        problems += _pgate.brief_problems(perf)
    for k in brief:
        if k not in _KNOWN_KEYS:
            problems.append(f"unknown brief key {k!r}")
    return problems


def _envelope_problems(env):
    if not isinstance(env, dict) or not _is_vec3(env.get("min")) or not _is_vec3(env.get("max")):
        return ["envelope must be {min:[x,y,z], max:[x,y,z]}"]
    lo, hi = env["min"], env["max"]
    return [f"envelope min[{i}] >= max[{i}]" for i in range(3) if lo[i] >= hi[i]]


def _is_vec3(v):
    return (isinstance(v, (list, tuple)) and len(v) == 3
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v))


# --- rendering the slice a builder reads --------------------------------------

def builder_brief_text(brief):
    """Render a builder brief as the self-contained prose a builder agent reads —
    the host-agnostic replacement for `coordinator._slice_text`. Any MCP host drops
    this into a subagent prompt; the subagent then drives the full AnkusDrive tool
    surface to build the component and calls `component_contract_check(handle, brief)`
    before saving."""
    cid = brief.get("component", "component")
    asm = brief.get("assembly", "assembly")
    parts = [f"You are building component '{cid}' of assembly '{asm}'.",
             f"Task: {brief.get('task', '').strip()}"]
    if brief.get("shared_parameters"):
        parts.append("Shared parameters every component must honor (do not re-derive, "
                     f"use as given): {brief['shared_parameters']}")
    if brief.get("envelope"):
        parts.append("Declared keep-out envelope — your part's bounding box must fit "
                     f"inside it: {brief['envelope']}")
    if brief.get("interfaces"):
        lines = [f"  - {name}: {spec}" for name, spec in brief["interfaces"].items()]
        parts.append("Interface frames you MUST publish (publish_interface), at the "
                     "given origins/axes:\n" + "\n".join(lines))
    if brief.get("material"):
        parts.append(f"Material: {brief['material']}.")
    if brief.get("constraints"):
        parts.append(f"Manufacturing / DFx constraints: {brief['constraints']}.")
    perf = brief.get("performance")
    if perf:
        reqs = perf.get("requirements") or []
        lines = [f"  - {r.get('name')}: {r.get('limit')}" for r in reqs]
        parts.append(
            "Quantitative PERFORMANCE requirements you must declare "
            "(declare_performance) and PROVE (verify_performance) before fan-in — a "
            "requirement you never verified is reported as unverified, which is not a "
            "pass:\n" + ("\n".join(lines) if lines
                         else "  - (see the assembly's performance contract)"))
    if brief.get("output"):
        parts.append(f"Save your finished component to: {brief['output']}")
    parts.append("Before saving, call component_contract_check(handle, brief) with "
                 "this brief and repair any failing check.")
    return "\n".join(parts)


def brief_from_slice(coordinator_brief, cid, *, output=None):
    """Project one component of a coordinator brief (the manifest-plus-task shape in
    orchestration/coordinator.py) into a standalone builder brief. This is how the
    reference harness converges onto the shared schema: the coordinator no longer
    owns a private slice format — it emits a `ankusdrive.builder_brief/1`."""
    spec = coordinator_brief["components"][cid]
    out = {
        "schema": SCHEMA,
        "component": cid,
        "assembly": coordinator_brief.get("name", "assembly"),
        "task": spec.get("task", ""),
        "output": output or spec.get("file", f"{cid}.FCStd"),
    }
    if spec.get("envelope"):
        out["envelope"] = spec["envelope"]
    if spec.get("interfaces"):
        out["interfaces"] = spec["interfaces"]
    if spec.get("performance"):
        out["performance"] = spec["performance"]
    shared = coordinator_brief.get("shared_parameters")
    if shared:
        out["shared_parameters"] = shared
    for k in ("material", "constraints", "owner"):
        if spec.get(k):
            out[k] = spec[k]
    return out


# --- the pure gate core (shared by the component_contract_check tool) ----------

def _dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _norm(v):
    return sum(x * x for x in v) ** 0.5


def _angle_deg(a, b):
    import math
    na, nb = _norm(a), _norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 180.0
    c = sum(x * y for x, y in zip(a, b)) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def evaluate_contract(brief, *, watertight, bbox, published, eps=1e-6,
                      performance=None, performance_contract=None):
    """Pure core of `component_contract_check` (issue #169, item 2). The builder-side
    half of the gate `merge_assembly` re-runs at merge time, evaluated locally on a
    component before it is saved:

      * watertight   — the part is one clean closed solid (from check_shape).
      * envelope     — the local bounding box fits inside the declared keep-out box.
      * interfaces   — every required frame is published with a sane frame, and (when
                       the brief pins an origin/axis) within tolerance of it.
      * performance  — the quantitative contract (#226) the part declares, judged
                       against its last recorded verdict (issue #261).

    Inputs are already-extracted primitives so this stays FreeCAD-free and testable:
      watertight : bool | None   check_shape's watertight_solid verdict (None=unknown)
      bbox       : {"min":[x,y,z], "max":[x,y,z]} | None   the part's local bbox
      published  : dict           frames published on the part (name -> frame dict)
      performance: dict | None    a ankusdrive.gates.performance gate block for the part
      performance_contract: dict | None   the part's raw AD_Performance bag, used to
                       check the brief's `performance.requirements` were declared and
                       not quietly loosened

    Returns {ok, checks:[{check, passed, detail}], reasons:[str,...],
    skipped:[{check, reason}], performance?}. `skipped` is #248's vocabulary for
    "no verdict": an undecided performance requirement neither passes nor fails the
    gate, so `ok` is untouched by it and the builder still sees, loudly, that it has
    not shown the thing yet. A part that declares no contract produces no performance
    rows, no `performance` key, and an empty `skipped` — the geometric gate is exactly
    what it was. Never raises."""
    checks = []

    def add(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    # 1) watertight solid
    if watertight is None:
        add("watertight", False, "could not determine watertightness (check_shape failed)")
    else:
        add("watertight", watertight,
            "one clean watertight solid" if watertight
            else "not a single watertight solid (run check_shape)")

    # 2) inside the declared envelope
    env = brief.get("envelope")
    if env:
        if bbox is None:
            add("envelope", False, "no bounding box available")
        else:
            lo, hi = env["min"], env["max"]
            bad = []
            for i, ax in enumerate("xyz"):
                if bbox["min"][i] < lo[i] - eps or bbox["max"][i] > hi[i] + eps:
                    bad.append(f"{ax}:[{bbox['min'][i]:.2f},{bbox['max'][i]:.2f}]"
                               f"⊄[{lo[i]},{hi[i]}]")
            add("envelope", not bad,
                "local bbox fits inside envelope" if not bad else "; ".join(bad))

    # 3) required interfaces published with sane / in-tolerance frames
    for name, spec in (brief.get("interfaces") or {}).items():
        spec = spec or {}
        if name not in published:
            add(f"interface:{name}", False, f"interface {name!r} not published")
            continue
        fr = published[name]
        origin = fr.get("origin", [0.0, 0.0, 0.0])
        detail = None
        passed = True
        if not _is_vec3(origin) or any(abs(c) > 1e6 for c in origin):
            passed, detail = False, f"{name} has a non-finite/degenerate origin {origin}"
        else:
            for ax_key in ("z_axis", "x_axis"):
                if ax_key in fr and _norm(fr[ax_key]) < eps:
                    passed, detail = False, f"{name} has a degenerate {ax_key}"
                    break
        if passed and _is_vec3(spec.get("origin")):
            gap = _dist(origin, spec["origin"])
            tol = float(spec.get("tol_mm", 0.5))
            if gap > tol:
                passed, detail = False, f"{name} origin off by {gap:.3f} mm (tol {tol})"
        if passed and _is_vec3(spec.get("z_axis")):
            ang = _angle_deg(fr.get("z_axis", [0, 0, 1]), spec["z_axis"])
            lim = float(spec.get("angle_tol_deg", 1.0))
            if ang > lim:
                passed, detail = False, f"{name} axis off by {ang:.2f}° (tol {lim})"
        add(f"interface:{name}", passed,
            detail if detail else f"{name} published with a sane frame")

    # 4) the performance contract (#226) as a gate slice (#261)
    from ankusdrive.gates import performance as _pgate
    perf_checks, skipped = _pgate.brief_checks(
        brief.get("performance"), performance_contract, performance)
    checks += perf_checks

    ok = all(c["passed"] for c in checks) if checks else True
    reasons = [c["detail"] for c in checks if not c["passed"]]
    out = {"ok": ok, "checks": checks, "reasons": reasons, "skipped": skipped}
    if performance is not None and performance.get("declared"):
        out["performance"] = performance
    return out
