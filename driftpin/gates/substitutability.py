"""Liskov-substitutability gate — Form/Fit/Function as code (issue #147).

DESIGN_HIERARCHY §7.1 (the headline modularity test) and §6 ("F3 = backward
compatibility"). The single most valuable deterministic modularity check:

    Take an assembly that gates green with variant A in a slot; swap in variant B
    (a different row of the same family, or any part claiming the same interface);
    re-run merge_assembly + all the gates.
      - Still green  ⇒ B is interchangeable with A — *by construction* a compatible
                       (MINOR/PATCH) change ⇒ REVISE the existing part number.
      - A gate fails ⇒ the swap broke Form/Fit/Function ⇒ a NEW part number.

This is a purely deterministic check — no API, no judgment. It *proves* an
interface abstraction holds, and it doubles as the substitutability test that
#138 (B1 design-table families: every family member must be substitutable at the
shared interface) and #146 (the §6.3 interface registry: conformance ⇒
substitutability) call.

Design (§10.1, parallel-safe):
  - All logic lives here, in pure Python with NO FreeCAD import. The worker wraps
    it with one appended handler (``substitutability_check``).
  - The swap re-uses the EXISTING ``merge_assembly`` entry point — this module
    never reaches into the merge body. It rewrites the manifest's component
    definition for one slot and re-merges, comparing the two reports' gate
    outcomes. The merge primitive is injected as a ``call`` callable, so this
    module stays host-agnostic and unit-testable.

The pass/fail gates mirror ``merge_assembly``'s own ``ok`` predicate (worker.py):
interference, envelope, interface_align, typed, children, requirements, mobility,
performance. ``bom`` is informational (not part of ``ok``) and is never treated as a
failure.

Form/Fit/**Function** (issue #261). Until #261 this gate compared geometry and
interfaces only, so a variant that was a perfect drop-in FIT but missed its Δp spec
came back "substitutable" — the F3 verdict was really F2. A performance contract
(#226) declared on the swapped-in part is now part of the swap comparison, and it
introduces a THIRD answer this gate did not have: a variant whose contract has no
recorded verdict is not substitutable and not un-substitutable, because nobody has
measured it. ``substitutable`` is therefore tri-state — True / False / **None** — and
None means "come back when you have verified it", never "close enough". Collapsing it
to True would let an unproven part inherit an existing part number, which is exactly
the silent pass #226 exists to prevent.
"""

import copy
import json
import os
import tempfile

from . import performance as _perf

# The gates whose violations decide merge_assembly's `ok` (worker.py
# _h_merge_assembly). Kept in lockstep with that predicate; `bom` is excluded
# because it is informational, not pass/fail.
LIST_GATES = ("interference", "envelope", "interface_align", "typed",
              "requirements", "mobility", "performance")
# A single source of truth: a component spec carries exactly one of these.
COMPONENT_SOURCES = ("file", "manifest", "library")

SCHEMA = "substitutability/v1"


# --- pure helpers (no FreeCAD, unit-testable) -------------------------------

def failing_gates(report):
    """The named gates that FAIL in a merge_assembly report (empty == all green).

    Returns ``{gate_name: violations}`` where each gate's value is the merge's own
    violation payload (a non-empty list for the list-gates, or, for ``children``,
    the sorted ids of sub-assemblies that did not gate green). Mirrors the `ok`
    predicate in ``_h_merge_assembly`` so "which gate broke?" is answered the same
    way the merge decides pass/fail — never trust a gate the merge wouldn't fail."""
    gates = (report or {}).get("gates", {}) or {}
    failing = {}
    for name in LIST_GATES:
        v = gates.get(name)
        if v:  # non-empty list of violations
            failing[name] = v
    children = gates.get("children")
    if children:
        bad = sorted(cid for cid, ok in children.items() if not ok)
        if bad:
            failing["children"] = bad
    return failing


def _validate_component_spec(spec):
    """A swap variant must be a well-formed component spec (the same one-source
    rule merge_assembly enforces): a dict with exactly one of file/manifest/
    library. Returns a list of problems (empty == valid)."""
    problems = []
    if not isinstance(spec, dict):
        return ["variant must be a component-spec object "
                "(one of file/manifest/library)"]
    sources = [k for k in COMPONENT_SOURCES if k in spec]
    if len(sources) != 1:
        problems.append(
            "variant must have exactly one of file/manifest/library "
            f"(has {sources or 'none'})")
    if "library" in spec and not (isinstance(spec["library"], dict)
                                  and spec["library"].get("tool")):
        problems.append("variant library needs a 'tool'")
    return problems


def swap_manifest(manifest, slot, variant):
    """Return a deep copy of ``manifest`` with component ``slot``'s definition
    replaced by ``variant`` (a component spec). Instances/checks/mates are
    untouched — every instance that references ``slot`` now resolves to the
    variant part, which is exactly the Liskov swap. Raises ValueError if the slot
    is absent or the variant is malformed."""
    comps = manifest.get("components", {})
    if slot not in comps:
        raise ValueError(
            f"slot {slot!r} is not a component (have {sorted(comps)})")
    problems = _validate_component_spec(variant)
    if problems:
        raise ValueError(f"bad variant for slot {slot!r}: {problems}")
    swapped = copy.deepcopy(manifest)
    swapped["components"][slot] = copy.deepcopy(variant)
    return swapped


def classify(substitutable):
    """The F3 / semver verdict for a swap outcome (§6: "Form/Fit/Function =
    backward compatibility"). Substitutable ⇒ a compatible change ⇒ revise;
    not ⇒ an interface break ⇒ a new part number.

    ``None`` is the third answer (#261): every geometric gate is green but the
    variant's declared PERFORMANCE contract has no verdict, so there is no basis for
    either decision yet. Reporting that as "compatible" would hand an unproven part an
    existing part number on the strength of a measurement nobody took."""
    if substitutable is None:
        return {"compatibility": "undecided",
                "semver": None,
                "decision": "no part-number decision yet — verify_performance on the "
                            "variant first (an unverified spec is not a passed spec)"}
    if substitutable:
        return {"compatibility": "compatible",
                "semver": "MINOR/PATCH",
                "decision": "revise existing part number"}
    return {"compatibility": "incompatible",
            "semver": "MAJOR",
            "decision": "new part number"}


def performance_outcome(report):
    """The performance block a merge report carries (``None`` when no component in it
    declares a contract), reduced to (outcome, undecided) where ``outcome`` is one of
    driftpin.gates.performance's contract outcomes and ``undecided`` names the
    requirements with no verdict."""
    perf = (report or {}).get("performance")
    if not isinstance(perf, dict):
        return None, []
    return perf.get("outcome"), list(perf.get("skipped") or [])


# --- the gate ----------------------------------------------------------------

def substitutability_report(base_manifest_path, slot, variant, call,
                            verify_baseline=True):
    """Run the Liskov-substitutability gate (§7.1).

    Args:
      base_manifest_path: path to a manifest that gates green with variant A in
        ``slot`` (the precondition — checked unless ``verify_baseline`` is False).
      slot: the component id to swap (variant A → variant B).
      variant: the replacement component spec (a dict with one of
        file/manifest/library — a different family row, a registry-conforming
        part, anything claiming the same interface).
      call: callable ``call(method, **params) -> report`` used to invoke the
        existing ``merge_assembly`` (the merge primitive, injected so this module
        needs no FreeCAD).
      verify_baseline: re-merge the base assembly first and require it green, so
        the test is honest about its premise (default True).

    Returns a deterministic report dict:
      {schema, slot, variant,
       baseline_ok, swap_ok, substitutable,        # tri-state: True/False/None
       verdict: 'substitutable' | 'not_substitutable' | 'baseline_not_green'
                | 'performance_unproven',
       broken_gates: [gate names the swap broke],   # NAMES the broken gate(s)
       broken: {gate: violations},
       classification: {compatibility, semver, decision},
       baseline_failing: {...},   # only when the premise is violated
       performance: {...},        # only when a component declares a contract (#261)
       reports: {baseline_ok, swap_ok}}
    """
    with open(base_manifest_path, encoding="utf-8") as f:
        base_man = json.load(f)

    base_failing = {}
    base_rep = None
    baseline_ok = True
    if verify_baseline:
        base_rep = call("merge_assembly", manifest=base_manifest_path)
        baseline_ok = bool(base_rep.get("ok"))
        base_failing = failing_gates(base_rep)
        if not baseline_ok:
            # Premise violated: the gate only makes sense over an assembly that is
            # already green with variant A. Report it loudly rather than silently
            # comparing against a broken baseline.
            return {
                "schema": SCHEMA, "slot": slot, "variant": variant,
                "baseline_ok": False, "swap_ok": None, "substitutable": False,
                "verdict": "baseline_not_green",
                "broken_gates": [], "broken": {},
                "baseline_failing": base_failing,
                "classification": classify(False),
                "reports": {"baseline_ok": False, "swap_ok": None},
            }

    swapped = swap_manifest(base_man, slot, variant)

    # Write the swapped manifest ALONGSIDE the base one so every relative
    # component path resolves against the same directory (merge_assembly resolves
    # component files relative to the manifest dir). Give it a distinct name +
    # absolute temp root so the swap run never clobbers the baseline artifact.
    base_dir = os.path.dirname(os.path.abspath(base_manifest_path))
    swapped.setdefault("name", base_man.get("name", "merged"))
    swapped["name"] = f"{swapped['name']}__subst_{slot}"
    out_root = os.path.join(
        tempfile.gettempdir(), f"subst_{swapped['name']}.FCStd")
    swapped["root"] = out_root

    fd, swp_path = tempfile.mkstemp(
        prefix=".subst_", suffix=".json", dir=base_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(swapped, f)
        swap_rep = call("merge_assembly", manifest=swp_path)
    finally:
        for pth in (swp_path, out_root):
            try:
                os.remove(pth)
            except OSError:
                pass

    swap_ok = bool(swap_rep.get("ok"))
    swap_failing = failing_gates(swap_rep)
    # A gate is "broken by the swap" iff it fails now but did NOT fail at baseline
    # (baseline is green here, so this is just the swap's failing gates — the diff
    # keeps it honest if verify_baseline was waived on a non-green base).
    broken = {g: v for g, v in swap_failing.items() if g not in base_failing}
    substitutable = swap_ok and baseline_ok

    # #261: Function, not just Form and Fit. A performance contract that came back
    # UNMET has already failed the `performance` gate above and shows up in `broken`.
    # What the geometric machinery cannot express is a contract with NO verdict: the
    # merge is green, nothing failed, and nothing was proved. That is neither answer.
    perf_outcome, undecided = performance_outcome(swap_rep)
    perf = None
    if perf_outcome is not None:
        perf = {"swap": swap_rep.get("performance"),
                "baseline": (base_rep or {}).get("performance"),
                "outcome": perf_outcome, "undecided": undecided}
    verdict = "substitutable" if substitutable else "not_substitutable"
    if substitutable and perf_outcome == _perf.UNPROVEN:
        substitutable = None
        verdict = "performance_unproven"

    out = {
        "schema": SCHEMA, "slot": slot, "variant": variant,
        "baseline_ok": baseline_ok, "swap_ok": swap_ok,
        "substitutable": substitutable,
        "verdict": verdict,
        "broken_gates": sorted(broken),
        "broken": broken,
        "classification": classify(substitutable),
        "reports": {"baseline_ok": baseline_ok, "swap_ok": swap_ok},
    }
    if perf is not None:
        out["performance"] = perf
    return out
