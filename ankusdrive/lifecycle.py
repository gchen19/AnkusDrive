"""
Revision + lifecycle state machine + the Form/Fit/Function predicate (issue #141,
the C2 item of docs/DESIGN_HIERARCHY.md §2 Theme C). Builds directly on the C1
item model (ankusdrive/items.py, #140): C1 *holds* the reserved `rev` and
`lifecycle` fields on every item record but never interprets them — this module
is where they become a state machine, a revision counter, and a deterministic
rename-vs-revise decision.

Why this exists (DESIGN_HIERARCHY §6, the modularity lens):

  * **Released-immutable = API stability.** A released item cannot mutate in
    place; downstream consumers rely on it exactly as a pinned semver dependency.
    Any further change opens a *new revision* (or a new part number) — never a
    silent edit. `is_editable()` makes "may a builder write this?" a cheap check.

  * **Form/Fit/Function = backward compatibility.** An F3-*preserving* change is
    interchangeable ⇒ a compatible (MINOR/PATCH) change ⇒ **bump the revision**
    (same part number). An F3-*breaking* change ⇒ a MAJOR break ⇒ a **new part
    number** (a new item, allocated via items.allocate_part_number). `form_fit_
    function()` turns "did the interface break?" into a function a test can gate.

Pure Python — no FreeCAD, no LLM, no key — so any host (the reference
coordinator, a CI script, another tool) can drive the state machine and the
predicate, exactly like ankusdrive/items.py and ankusdrive/manifest.py's resolve
step. The worker.py / mcp_server.py handlers are thin append-only wrappers.

--- The lifecycle state machine -------------------------------------------------

States (the reserved vocabulary, imported from items.LIFECYCLE_STATES):

    in_work  ──submit──>  in_review  ──release──>  released  ──retire──>  obsolete
       ^                      │                                              ^
       └──────reject──────────┘                                             │
       └──────────────────────abandon (in_work/in_review ──> obsolete)──────┘

Encoded as a guard table (TRANSITIONS): a transition NOT in the table is rejected
loudly (LifecycleError). The two rules the table enforces:

  * **No skipping review.** in_work cannot jump straight to released — it must
    pass through in_review. (The "skip review" negative control.)
  * **Released is terminal-except-retire.** A released item only moves to
    obsolete; it is never re-opened in place. To change a released part you open
    a NEW revision (open_revision) or a NEW part number (apply_change) — the
    immutability rule.

EDITABLE_STATES defines "is this editable?": only `in_work`. in_review is frozen
for review; released and obsolete are frozen permanently. A builder calls
assert_editable() before writing geometry; editing a released item raises.

--- Revisions -------------------------------------------------------------------

A **revision** is a deliberate released milestone (Rev A/B/C — the classic
mechanical convention; numeric major.minor is also supported), distinct from an
every-save *version*. "-" is the C1 default meaning "no released revision yet".
On first release the rev advances "-" -> "A"; opening a new revision off a
released item bumps "A" -> "B" -> ... -> "Z" -> "AA" (spreadsheet-column style).

--- The transition log ----------------------------------------------------------

Every transition / revision bump appends an entry to the item record's
`lifecycle_log` list ({from, to, rev, action, actor, note}). It is deterministic
(no wall-clock timestamp — the house determinism rule) and C1-compatible: C1's
validate_registry ignores keys it does not know, so a logged registry still
validates clean.
"""
import copy
import re

from ankusdrive import items as _items

# Reuse C1's reserved lifecycle vocabulary verbatim — one source of truth.
LIFECYCLE_STATES = _items.LIFECYCLE_STATES  # in_work, in_review, released, obsolete
NO_REV = "-"  # the C1 sentinel: "no released revision yet"


class LifecycleError(Exception):
    """An illegal lifecycle transition, or a write to a frozen (non-editable)
    item. Raised loudly — a guard must never fail silently (the house rule)."""


# --- the guarded transition table -------------------------------------------
#
# state -> the set of states it may move to. Anything not listed is rejected.
# Named so the intent of each edge is legible in the log.
TRANSITIONS = {
    "in_work":   ("in_review", "obsolete"),     # submit for review / abandon
    "in_review": ("in_work", "released", "obsolete"),  # reject / release / abandon
    "released":  ("obsolete",),                 # retire only — immutable otherwise
    "obsolete":  (),                            # terminal
}

# Human-readable names for the edges, for the transition log.
_EDGE_ACTION = {
    ("in_work", "in_review"): "submit",
    ("in_review", "in_work"): "reject",
    ("in_review", "released"): "release",
    ("in_work", "obsolete"): "abandon",
    ("in_review", "obsolete"): "abandon",
    ("released", "obsolete"): "retire",
}

# The only state in which an item may be written/edited in place. Everything
# else is frozen: in_review (under review), released (immutable API), obsolete.
EDITABLE_STATES = ("in_work",)


# --- state machine: pure predicates -----------------------------------------

def can_transition(frm, to):
    """True iff `frm -> to` is a legal lifecycle edge per TRANSITIONS. A no-op
    (frm == to) is NOT a transition and returns False."""
    return to in TRANSITIONS.get(frm, ())


def assert_transition(frm, to):
    """Raise LifecycleError unless `frm -> to` is a legal edge. The loud guard
    behind every state change."""
    if to not in LIFECYCLE_STATES:
        raise LifecycleError(
            f"unknown lifecycle state {to!r} (expected one of {list(LIFECYCLE_STATES)})")
    if not can_transition(frm, to):
        allowed = list(TRANSITIONS.get(frm, ()))
        raise LifecycleError(
            f"illegal lifecycle transition {frm!r} -> {to!r}; "
            f"from {frm!r} only {allowed} are allowed")


def is_editable(state):
    """The cheap "is this editable?" check any builder runs before writing.
    Accepts a lifecycle-state string or a whole item record (dict)."""
    if isinstance(state, dict):
        state = state.get("lifecycle", _items._DEFAULT_LIFECYCLE)
    return state in EDITABLE_STATES


def assert_editable(state):
    """Raise LifecycleError if `state` is not editable (frozen). The immutability
    guard: an attempt to edit a Released (or in_review / obsolete) item fails
    loudly. Accepts a state string or an item record."""
    if not is_editable(state):
        cur = state.get("lifecycle") if isinstance(state, dict) else state
        raise LifecycleError(
            f"item is not editable in lifecycle state {cur!r} "
            f"(only {list(EDITABLE_STATES)} may be written; a released item is "
            f"immutable — open a new revision or part number to change it)")


# --- revisions: deliberate released milestones ------------------------------

def _alpha_inc(s):
    """Spreadsheet-column increment over uppercase letters: A->B, Z->AA, AZ->BA."""
    chars = list(s)
    i = len(chars) - 1
    while i >= 0:
        if chars[i] != "Z":
            chars[i] = chr(ord(chars[i]) + 1)
            return "".join(chars)
        chars[i] = "A"
        i -= 1
    return "A" + "".join(chars)


def bump_rev(rev):
    """Advance a revision to its next deliberate milestone. Deterministic:

      * "-" / "" / None  -> "A"           (the first released revision)
      * "A" -> "B", "Z" -> "AA"           (alpha — the classic mechanical rev)
      * "1.0" -> "1.1", "2.9" -> "2.10"   (numeric major.minor — minor bump)

    Raises ValueError on an un-bumpable rev string."""
    if rev in (None, "", NO_REV):
        return "A"
    if isinstance(rev, str) and re.fullmatch(r"\d+\.\d+", rev):
        major, minor = rev.split(".")
        return f"{major}.{int(minor) + 1}"
    if isinstance(rev, str) and re.fullmatch(r"[A-Z]+", rev):
        return _alpha_inc(rev)
    raise ValueError(
        f"cannot bump revision {rev!r}: expected '-', an alpha rev (A, B, .., AA), "
        f"or numeric major.minor (1.0)")


# --- registry-level operations (mutate an items.json registry) --------------

def _require_item(registry, item_id):
    rec = registry.get("items", {}).get(item_id)
    if rec is None:
        raise KeyError(f"no item {item_id!r} in registry")
    return rec


def item_state(registry, item_id):
    """The current lifecycle state of an item (defaulting to the C1 default)."""
    return _require_item(registry, item_id).get(
        "lifecycle", _items._DEFAULT_LIFECYCLE)


def item_editable(registry, item_id):
    """Cheap check: may this item be written/edited in place?"""
    return is_editable(_require_item(registry, item_id))


def _log(rec, frm, to, rev, action, actor, note):
    entry = {"from": frm, "to": to, "rev": rev, "action": action}
    if actor is not None:
        entry["actor"] = actor
    if note is not None:
        entry["note"] = note
    rec.setdefault("lifecycle_log", []).append(entry)


def transition(registry, item_id, to, *, actor=None, note=None):
    """Move an item to lifecycle state `to`, guarded by the transition table.
    Mutates and returns the registry; raises LifecycleError on an illegal edge.

    Side effects on the milestone edges:
      * entering `released` for the first time (rev == "-") stamps the first
        revision "A" — releasing freezes the item AT a revision.

    Appends a deterministic entry to the item's `lifecycle_log`."""
    rec = _require_item(registry, item_id)
    frm = rec.get("lifecycle", _items._DEFAULT_LIFECYCLE)
    assert_transition(frm, to)
    rev = rec.get("rev", NO_REV)
    if to == "released" and rev in (NO_REV, "", None):
        rev = bump_rev(rev)  # "-" -> "A": the first released milestone
        rec["rev"] = rev
    rec["lifecycle"] = to
    _log(rec, frm, to, rev, _EDGE_ACTION.get((frm, to), "transition"), actor, note)
    return registry


def open_revision(registry, item_id, *, actor=None, note=None):
    """Open a NEW working revision off a *released* item: bump the rev (A->B) and
    move the lifecycle back to `in_work`. This is the sanctioned way to change a
    released, immutable part — it is never edited in place. Mutates and returns
    the registry; raises LifecycleError unless the item is currently released.

    Returns the registry (the item now carries the bumped rev, in_work)."""
    rec = _require_item(registry, item_id)
    frm = rec.get("lifecycle", _items._DEFAULT_LIFECYCLE)
    if frm != "released":
        raise LifecycleError(
            f"open_revision requires a released item; item {item_id!r} is {frm!r}. "
            f"An unreleased item is edited in place (it is already in_work).")
    new_rev = bump_rev(rec.get("rev", NO_REV))
    rec["rev"] = new_rev
    rec["lifecycle"] = "in_work"
    _log(rec, frm, "in_work", new_rev, "open_revision", actor, note)
    return registry


# --- the Form/Fit/Function predicate ----------------------------------------
#
# A change is classified by *which attributes it touches*. Form/Fit/Function are
# the externally-visible (public-API) attributes — the "visible design rules" of
# Baldwin & Clark; everything else is a hidden parameter, free to change without
# affecting any consumer. The taxonomy below is the default; a caller can extend
# it (extra_f3) for domain-specific interface attributes.
#
#   FORM     — physical shape / size / mass / envelope / mounting footprint
#   FIT      — how it mates: mating diameters, bolt circles, datums, tolerances,
#              published interface geometry, threads, keyways
#   FUNCTION — what it does: rating, capacity, ratio, performance-defining spec
#
# Anything not in the union is INTERNAL (hidden): wall thickness, internal ribs,
# pocketing, fillet radii, cosmetic finish, supplier, cost, owner, notes, name.

F3_FORM = frozenset({
    "envelope_mm", "overall_length_mm", "overall_width_mm", "overall_height_mm",
    "mass_g", "mounting_pattern", "mounting_hole_spacing_mm", "bounding_box",
    "footprint", "cg_mm",
})
F3_FIT = frozenset({
    "bore_dia_mm", "diameter_mm", "bolt_circle_mm", "mating_face", "interface",
    "interfaces", "tolerance_class", "fit_class", "thread", "keyway",
    "pilot_dia_mm", "shaft_dia_mm", "datum", "pitch_dia_mm",
})
F3_FUNCTION = frozenset({
    "rating", "load_capacity_n", "ratio", "function", "stiffness_n_mm",
    "flow_area_mm2", "pressure_rating_kpa", "voltage_rating_v", "torque_rating_nm",
})

# The categorised union, for reporting which leg of F3 a change touched.
_F3_CATEGORIES = (("form", F3_FORM), ("fit", F3_FIT), ("function", F3_FUNCTION))


def f3_category(attr, extra_f3=None):
    """Return the F3 leg ("form" / "fit" / "function") an attribute belongs to,
    or None if it is an internal (hidden) parameter. `extra_f3` optionally maps
    extra attribute names to one of the three legs (domain interface attrs)."""
    if extra_f3 and attr in extra_f3:
        return extra_f3[attr]
    for name, members in _F3_CATEGORIES:
        if attr in members:
            return name
    return None


def is_f3_attr(attr, extra_f3=None):
    """True iff `attr` is a Form/Fit/Function (public, interface-defining)
    attribute — a change to it breaks interchangeability."""
    return f3_category(attr, extra_f3) is not None


def changed_keys(before, after):
    """Sorted list of keys whose value differs between two attribute dicts
    (added, removed, or changed) — the raw delta a change represents."""
    return sorted(k for k in set(before) | set(after)
                  if before.get(k) != after.get(k))


def form_fit_function(before, after, *, extra_f3=None):
    """The deterministic, testable Form/Fit/Function predicate. Compares two
    attribute dicts (an item's interface-defining + internal attributes, before
    and after a proposed change) and decides rename-vs-revise:

      * any changed key is a Form/Fit/Function attribute  => the change BREAKS
        interchangeability => disposition "new_part_number" (a MAJOR break).
      * only internal (hidden) keys changed                => F3-preserving =>
        disposition "revise" (a compatible MINOR/PATCH change; bump the rev).
      * nothing changed                                    => "noop".

    Returns a verdict dict:
        {disposition, f3, changed, f3_changed, categories, reason}
    where `disposition` is one of "revise" / "new_part_number" / "noop",
    `f3` is the boolean "did Form/Fit/Function change", `changed` is every
    differing key, `f3_changed` is the subset that is F3, and `categories` maps
    each f3-changed key to its leg. Pure + deterministic — gate-testable both
    ways."""
    changed = changed_keys(before, after)
    cats = {k: f3_category(k, extra_f3) for k in changed if is_f3_attr(k, extra_f3)}
    f3_hits = sorted(cats)
    if not changed:
        disposition, reason = "noop", "no attribute changed"
    elif f3_hits:
        disposition = "new_part_number"
        reason = ("Form/Fit/Function changed (" +
                  ", ".join(f"{k}:{cats[k]}" for k in f3_hits) +
                  ") — not interchangeable; allocate a new part number")
    else:
        disposition = "revise"
        reason = ("only internal/hidden attributes changed (" +
                  ", ".join(changed) + ") — interchangeable; bump the revision")
    return {
        "disposition": disposition,
        "f3": bool(f3_hits),
        "changed": changed,
        "f3_changed": f3_hits,
        "categories": cats,
        "reason": reason,
    }


def classify_change(before, after, *, extra_f3=None):
    """Just the disposition string from form_fit_function(): "revise",
    "new_part_number", or "noop". The terse entry point for a gate test."""
    return form_fit_function(before, after, extra_f3=extra_f3)["disposition"]


# --- apply a change to a released item: revise vs. new part number -----------

def apply_change(registry, item_id, after_metadata, *, new_item_id=None,
                 extra_f3=None, actor=None, note=None):
    """Apply a proposed change to an item, dispatching on the Form/Fit/Function
    predicate. This is the sanctioned path to change a *released* item (the only
    way to touch a frozen part):

      * F3-preserving (revise)  -> open a NEW revision on the SAME part number
        (bump_rev, lifecycle back to in_work) and write the new metadata. The
        consumer's part number is unchanged; it is a compatible revision.

      * F3-breaking (new_part_number) -> allocate a NEW part number via
        items.allocate_part_number and register a NEW item (`new_item_id`
        required), rev "-", in_work, carrying the new metadata and a
        `supersedes` back-link. The original released item is left untouched
        (still released) — its API never silently changes.

      * noop -> nothing changes.

    Mutates and returns a result dict:
        {disposition, item, part_number, rev, ...}
    Raises LifecycleError if the item is not released (an unreleased item is just
    edited in place — guard with assert_editable, no F3 decision needed)."""
    rec = _require_item(registry, item_id)
    if rec.get("lifecycle") != "released":
        raise LifecycleError(
            f"apply_change is for a released item (the F3 revise-vs-renumber "
            f"decision); item {item_id!r} is {rec.get('lifecycle')!r} — edit it "
            f"in place after assert_editable() instead.")
    verdict = form_fit_function(rec.get("metadata", {}), after_metadata,
                                extra_f3=extra_f3)
    disposition = verdict["disposition"]

    if disposition == "noop":
        return {"disposition": "noop", "item": item_id,
                "part_number": rec["part_number"], "rev": rec.get("rev", NO_REV),
                "verdict": verdict}

    if disposition == "revise":
        # F3-preserving: same part number, bump the revision, reopen as in_work.
        open_revision(registry, item_id, actor=actor,
                      note=note or "F3-preserving change (revise)")
        rec["metadata"] = dict(after_metadata)
        return {"disposition": "revise", "item": item_id,
                "part_number": rec["part_number"], "rev": rec["rev"],
                "verdict": verdict}

    # F3-breaking: a new part number / new item. The original stays released.
    if not new_item_id:
        raise ValueError(
            "an F3-breaking change needs a new_item_id for the new part number")
    if new_item_id in registry.get("items", {}):
        raise ValueError(f"new_item_id {new_item_id!r} already exists")
    meta = dict(after_metadata)
    meta.setdefault("supersedes", item_id)
    _items.new_item(registry, new_item_id, files=list(rec.get("files", [])),
                    metadata=meta)
    new_rec = registry["items"][new_item_id]
    _log(new_rec, None, "in_work", new_rec["rev"], "new_part_number",
         actor, note or f"F3-breaking change, supersedes {item_id}")
    return {"disposition": "new_part_number", "item": new_item_id,
            "supersedes": item_id, "part_number": new_rec["part_number"],
            "rev": new_rec["rev"], "verdict": verdict}


# --- whole-registry validation (C1 shape + lifecycle/rev coherence) ---------

def validate_lifecycle(registry):
    """Validate the lifecycle/rev layer ON TOP of C1's structural validation.
    Returns a list of problems; empty == coherent. Catches what C1's shape check
    cannot: a released item with no revision (rev still "-"), or a malformed
    lifecycle_log entry. C1 owns part-number uniqueness + the held field shapes;
    this owns the state-machine semantics."""
    problems = list(_items.validate_registry(registry))
    for item_id, rec in registry.get("items", {}).items():
        if not isinstance(rec, dict):
            continue
        lc = rec.get("lifecycle", _items._DEFAULT_LIFECYCLE)
        rev = rec.get("rev", NO_REV)
        if lc == "released" and rev in (NO_REV, "", None):
            problems.append(
                f"item {item_id!r} is released but has no revision (rev {rev!r}) "
                f"— a released milestone must carry a revision")
        log = rec.get("lifecycle_log", [])
        if not isinstance(log, list):
            problems.append(f"item {item_id!r} lifecycle_log must be a list")
            continue
        for j, entry in enumerate(log):
            if not isinstance(entry, dict) or "to" not in entry:
                problems.append(
                    f"item {item_id!r} lifecycle_log[{j}] must be an object with a 'to'")
    return problems


def get_log(registry, item_id):
    """A deep copy of an item's transition log (or [] if none)."""
    rec = registry.get("items", {}).get(item_id, {})
    return copy.deepcopy(rec.get("lifecycle_log", []))
