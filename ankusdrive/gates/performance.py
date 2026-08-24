"""Performance-contract gate outcomes — issue #261 (#226's unbuilt Integration section).

#226 made a quantitative spec an object that rides on the part (``AD_Performance``)
and re-verifies standalone. Nothing else in the product consulted it, so a part that
was a drop-in geometric fit but missed its Δp spec sailed through
``substitutability_check``, ``merge_assembly`` and ``component_contract_check``. This
module is the shared judgement those three gates read the contract with — the same
role ``ankusdrive.gates.modal`` plays for ``min_first_mode_hz``.

THE ASYNC POLICY, which is the whole design decision
----------------------------------------------------
Verifying a performance requirement may be ASYNCHRONOUS: a solver-tier requirement
submits an OpenFOAM/CalculiX job and ``verify_performance`` hands back a ``job_id``.
A gate, by contrast, must answer NOW. Three policies were available (the issue names
them): consult only the last recorded verdict; run the screen tier inline; or refuse
to gate and report "unverified".

**The gate consults the last RECORDED verdict and never measures.** ``verify_performance``
stamps its verdict onto the part (``AD_PerformanceVerdict``); every gate reads that
record and classifies it. Why not the other two:

  * *Running screen tier inline* would silently substitute a WEAKER measurement for the
    one the contract declares. A requirement with ``fidelity_floor: 'solver'`` cannot be
    proved by a screen at all, so an inline screen would return ``indeterminate`` for
    exactly the requirements that matter while making ``merge_assembly`` — documented as
    deterministic and idempotent — do arbitrary solver work on a path nobody asked for.
  * *Refusing to gate outright* throws away a verdict that is genuinely on record, which
    is the only thing that could ever let the gate say "met".

Consulting the record is synchronous, deterministic, cheap, and honest: the gate reports
the evidence that exists, and the absence of evidence is reported as an absence.

WHAT AN ABSENCE MEANS (the #248 discipline, one level up)
--------------------------------------------------------
A gate must NEVER read "not yet verified" as "fine" — that silent pass is the exact trap
#226 exists to avoid, and the trap #248 was fixed for on the modal gate. So a requirement
has FIVE outcomes here, of which only two are verdicts about the part:

  * ``PASS``          — the recorded row passed, and the record is current.
  * ``FAIL``          — the recorded row failed. A real VERDICT: it fails the merge.
  * ``INDETERMINATE`` — the measurement was taken and could not decide (the band
    straddles the limit, or a trust demand was unmet). #226's third state, propagated.
  * ``UNVERIFIED``    — no verdict on record for this requirement: never verified, or a
    solve is still in flight.
  * ``STALE``         — a verdict IS on record, but the part's geometry changed since,
    so it measures a shape that no longer exists.

Only ``PASS``/``FAIL`` are :data:`VERDICTS`. The other three are reported through the
vocabulary ``merge_assembly`` already has for a requirement it could not decide —
``skipped``, with a ``skipped_reason`` naming what is missing and what would fix it. So an
undecided requirement is loud in the report, never reads as "requirement met", and never
fails a merge for something the part did not do.

STALENESS
---------
``verify_intent`` has no staleness concept because its checks are cheap: it simply re-runs
them. A performance verification can cost a CFD solve, so re-running is not an option for a
gate, and staleness has to be DETECTED. The mechanism mirrors the lockfile (§9), which
detects component drift by hashing: ``verify_performance`` stamps the verdict with a
geometry signature of the part it measured, and a gate compares that against the part's
current signature. A mismatch — or a record with no signature at all, or a part whose
signature cannot be computed — is ``STALE``: freshness that cannot be established is not
freshness, which is the conservative answer and the only honest one.

Pure Python, FreeCAD-free: the worker extracts the contract, the record and the signature
and hands them here. See ``tests/test_performance.py``.
"""

#: Schema tag for the verdict record ``verify_performance`` persists on a part.
RECORD_SCHEMA = "ankusdrive.performance_verdict/1"
#: Schema tag for the gate block this module produces.
SCHEMA = "ankusdrive.performance_gate/1"

# --- per-requirement outcomes -------------------------------------------------
PASS = "pass"
FAIL = "fail"
INDETERMINATE = "indeterminate"   # measured; the band/trust could not decide (#226)
UNVERIFIED = "unverified"         # no verdict on record, or a solve still in flight
STALE = "stale"                   # verdict on record, but the part changed since

#: Outcomes that are a statement about the PART (and so decide a gate). Everything
#: else is a statement about the EVIDENCE, and decides nothing.
VERDICTS = (PASS, FAIL)
#: The three ways a requirement can fail to be decided. None of them is a pass.
UNDECIDED = (INDETERMINATE, UNVERIFIED, STALE)

# --- contract-level outcomes --------------------------------------------------
NOT_DECLARED = "not_declared"     # the part declares no performance contract
MET = "met"                       # every requirement passed, on a current record
UNMET = "unmet"                   # at least one requirement failed -> fails the gate
UNPROVEN = "unproven"             # nothing failed, but something is undecided

#: Contract outcomes that decide a gate. ``UNPROVEN`` deliberately does not.
CONTRACT_VERDICTS = (MET, UNMET)

_NO_VERDICT = ("so the gate has NO verdict on this requirement (not a pass, "
               "not a failure)")


def has_verdict(outcome):
    """True only when the gate actually decided the requirement/contract. Callers
    that need a pass/fail decision must branch on this rather than on ``ok`` — an
    ``ok=False`` block may mean "failed" or may mean "nobody measured it", and those
    two must never be actioned the same way."""
    if isinstance(outcome, dict):
        outcome = outcome.get("outcome")
    return outcome in VERDICTS or outcome in CONTRACT_VERDICTS


def _row_state(row):
    """The recorded row's own three-state verdict, defensively. A row with no
    readable state is treated as INDETERMINATE, never as a pass."""
    state = (row or {}).get("state")
    if state == PASS:
        return PASS
    if state == FAIL:
        return FAIL
    return INDETERMINATE


def _undecided_reason(state, name, row, record, stale_detail=None):
    """One loud sentence for a requirement the gate could not decide: what is
    missing, what would fix it, and — explicitly — that there is no verdict."""
    job_id = (row or {}).get("job_id")
    if job_id and state != STALE:
        # An asynchronous measurement that has not landed. Naming the job is the
        # actionable part: the caller knows exactly what to wait on.
        what = (f"requirement {name!r} has a solve in flight (job {job_id}); its "
                "verdict is not in yet")
    elif state == UNVERIFIED:
        if record is None:
            what = (f"requirement {name!r} has never been verified on this part "
                    "(run verify_performance)")
        else:
            what = (f"requirement {name!r} is declared but the recorded verdict "
                    "covers no such requirement (re-run verify_performance)")
    elif state == STALE:
        what = (f"the recorded verdict for {name!r} measured a different shape "
                f"({stale_detail}) — re-run verify_performance on the current part")
    else:
        detail = (row or {}).get("detail")
        what = (f"requirement {name!r} was measured but could not be decided"
                + (f": {detail}" if detail else ""))
    return f"{what} — {_NO_VERDICT}"


def _freshness(record, signature):
    """(stale, detail) for a recorded verdict against the part's current geometry
    signature. Unknown freshness counts as stale: a record we cannot date against
    the current shape is not evidence about the current shape."""
    if record is None:
        return False, None
    recorded = record.get("signature")
    if not recorded:
        return True, "the verdict was recorded without a geometry signature"
    if not signature:
        return True, "the part's current geometry signature could not be computed"
    if recorded != signature:
        return True, f"recorded {recorded}, part is now {signature}"
    return False, None


def evaluate(contract, record, signature=None, component=None):
    """Classify one part's performance contract against its last recorded verdict.

    Args:
      contract:  the ``AD_Performance`` bag (``{"requirements": [...]}``), or None/{}
                 for a part that declares nothing.
      record:    the ``AD_PerformanceVerdict`` bag written by ``verify_performance``,
                 or None when the part has never been verified.
      signature: the part's CURRENT geometry signature (see the module docstring);
                 None means it could not be computed, which counts as stale.
      component: optional label (a manifest component id) stamped into violations so a
                 coordinator knows WHICH part missed its spec.

    Returns the gate block::

        {schema, component?, declared, outcome, ok, n_requirements,
         requirements: [{name, state, detail, measured?, limit?, tier?, metric?,
                         margin_pct?, job_id?, skipped_reason? }],
         violations: [{requirement: "performance", component?, name, reason,
                       measured, limit, state}],
         skipped: ["<name>", ...],        # every requirement without a verdict
         verified_at?, tier?, stale, reason}

    ``ok`` is True only for ``MET`` (and, vacuously, for a part that declares
    nothing) — an undecided requirement is not a pass, exactly as #226's
    ``summarize`` has it one level down. Never raises."""
    reqs = list((contract or {}).get("requirements") or [])
    block = {"schema": SCHEMA, "declared": bool(reqs),
             "n_requirements": len(reqs), "requirements": [],
             "violations": [], "skipped": [], "stale": False}
    if component is not None:
        block["component"] = component
    if not reqs:
        block.update({"outcome": NOT_DECLARED, "ok": True,
                      "reason": "no performance contract declared on this part"})
        return block

    stale, stale_detail = _freshness(record, signature)
    block["stale"] = bool(stale)
    if record is not None:
        block["verified_at"] = record.get("verified_at")
        block["tier"] = record.get("tier")
    rows = {r.get("name"): r for r in (record or {}).get("results") or []
            if isinstance(r, dict) and r.get("name")}

    for req in reqs:
        name = req.get("name", "?")
        row = rows.get(name)
        if row is None:
            state = UNVERIFIED
        elif stale:
            # Staleness outranks the recorded state in BOTH directions, deliberately.
            # A stale pass obviously cannot stand; a stale FAILURE cannot either,
            # because it condemned a shape that no longer exists. Editing a part
            # therefore drops it from `unmet` to `unproven` — which is not a way to
            # sneak past the gate: the block's `ok` stays False and the requirement is
            # named in `skipped` until someone re-measures the part that exists now.
            state = STALE
        else:
            state = _row_state(row)
        item = {"name": name, "state": state,
                "limit": req.get("limit"), "metric": req.get("metric")}
        for key in ("measured", "tier", "margin_pct", "detail", "job_id",
                    "trust_reasons"):
            if isinstance(row, dict) and row.get(key) is not None:
                item[key] = row[key]
        if state == FAIL:
            reason = (row.get("detail")
                      or f"requirement {name!r} is not met by the recorded measurement")
            item["detail"] = reason
            viol = {"requirement": "performance", "name": name, "reason": reason,
                    "measured": row.get("measured"), "limit": req.get("limit"),
                    "state": FAIL}
            if component is not None:
                viol["component"] = component
            block["violations"].append(viol)
        elif state != PASS:
            item["skipped_reason"] = _undecided_reason(
                state, name, row, record, stale_detail)
            block["skipped"].append(name)
        block["requirements"].append(item)

    states = [i["state"] for i in block["requirements"]]
    if FAIL in states:
        outcome = UNMET
    elif all(s == PASS for s in states):
        outcome = MET
    else:
        outcome = UNPROVEN
    block["outcome"] = outcome
    block["ok"] = outcome == MET
    block["reason"] = _contract_reason(outcome, block)
    return block


def _contract_reason(outcome, block):
    n = block["n_requirements"]
    label = f"{block['component']!r}: " if block.get("component") else ""
    if outcome == MET:
        return (f"{label}all {n} performance requirement(s) met by the recorded "
                "verdict")
    if outcome == UNMET:
        bad = ", ".join(v["name"] for v in block["violations"])
        return f"{label}performance requirement(s) NOT met: {bad}"
    undec = ", ".join(block["skipped"])
    return (f"{label}performance requirement(s) not decided: {undec} — "
            "no verdict, so this gate neither passes nor fails the part")


def roll_up(blocks):
    """Roll per-component gate blocks into the assembly-level block
    ``merge_assembly`` reports.

    ``violations`` fails the merge (a declared requirement measured as NOT met);
    ``skipped`` does not — it is the #248 vocabulary for "the gate has no verdict",
    and a non-verdict must not fail a merge any more than it may pass one. What it
    MUST do is be impossible to mistake for a pass, which is why ``ok`` is True only
    when every consulted contract came back ``MET`` and ``outcome`` names the
    difference. Returns None when no component declares a contract at all, so a
    manifest of parts that declare nothing gets no performance block and behaves
    exactly as it did before #261."""
    declared = {cid: b for cid, b in (blocks or {}).items() if b.get("declared")}
    if not declared:
        return None
    violations, skipped = [], []
    for cid in sorted(declared):
        b = declared[cid]
        violations.extend(b["violations"])
        skipped.extend(f"{cid}:{name}" for name in b["skipped"])
    outcomes = [declared[cid]["outcome"] for cid in sorted(declared)]
    if UNMET in outcomes:
        outcome = UNMET
    elif all(o == MET for o in outcomes):
        outcome = MET
    else:
        outcome = UNPROVEN
    return {"schema": SCHEMA, "outcome": outcome, "ok": outcome == MET,
            "components": declared, "violations": violations,
            "skipped": sorted(skipped),
            "reason": _rollup_reason(outcome, declared, skipped)}


def _rollup_reason(outcome, declared, skipped):
    n = len(declared)
    if outcome == MET:
        return f"every declared performance contract ({n}) is met"
    if outcome == UNMET:
        bad = sorted({v.get("component") or "?"
                      for b in declared.values() for v in b["violations"]})
        return f"component(s) {bad} do not meet their declared performance contract"
    return (f"performance contract(s) not decided: {sorted(skipped)} — the merge is "
            "not blocked by this, and it is NOT evidence the spec is met")


def brief_problems(slice_):
    """Validate a builder brief's optional ``performance`` slice. Returns a list of
    human-readable problems (empty == valid), matching
    ``ankusdrive.builder_brief.validate_builder_brief``'s style."""
    if not isinstance(slice_, dict):
        return ["performance must be an object "
                "{requirements?: [...], required?: bool}"]
    problems = []
    reqs = slice_.get("requirements")
    if reqs is not None:
        if not isinstance(reqs, list) or not reqs:
            problems.append("performance.requirements must be a non-empty list")
        else:
            for r in reqs:
                if not isinstance(r, dict) or not r.get("name"):
                    problems.append("each performance requirement needs a 'name'")
                elif not isinstance(r.get("limit"), dict):
                    problems.append(
                        f"performance requirement {r['name']!r} needs a 'limit'")
    for k in slice_:
        if k not in ("requirements", "required"):
            problems.append(f"unknown performance key {k!r}")
    return problems


def _at_least_as_tight(declared, demanded):
    """Is the part's declared limit at least as strict as the brief's? A ``max`` may
    only be lowered and a ``min`` may only be raised; a builder that declared a looser
    spec than it was briefed for has not honored the brief, however green its own
    contract is."""
    if not isinstance(declared, dict):
        return False, "declares no limit"
    for key, worse in (("max", lambda a, b: a > b), ("min", lambda a, b: a < b)):
        if key in (demanded or {}):
            if key not in declared:
                return False, f"declares no {key} limit (brief asks {key}={demanded[key]:g})"
            if worse(float(declared[key]), float(demanded[key])):
                return False, (f"declares {key}={float(declared[key]):g}, looser than the "
                               f"brief's {key}={float(demanded[key]):g}")
    return True, "matches the brief's limit"


def brief_checks(slice_, contract, block):
    """The builder-side rows ``component_contract_check`` adds for the brief's
    ``performance`` slice plus whatever the part itself declares (issue #261).

    Returns ``(checks, skipped)``:
      * ``checks``  — normal pass/fail rows ``{check, passed, detail}``. Two families:
        ``performance_spec:<name>`` asks whether the part DECLARED what it was briefed
        for (and no looser), ``performance:<name>`` whether the recorded verdict MET it.
        They are separate because a builder that quietly relaxed its limit and then
        passed its own contract has still not honored the brief.
      * ``skipped`` — ``{check, reason}`` rows for requirements with NO verdict. They
        do not fail the gate and they do not pass it; a builder must read them as
        "you have not shown this yet", which for a self-check before fan-in is
        precisely the actionable state.

    A part that declares nothing and a brief with no ``performance`` slice produce
    ([], []) — the 99 % case is untouched."""
    slice_ = slice_ or {}
    declared = {r.get("name"): r for r in (contract or {}).get("requirements") or []}
    checks, skipped = [], []

    def add(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    demanded = slice_.get("requirements") or []
    if slice_ and slice_.get("required", True) and not declared:
        add("performance", False,
            "the brief requires a performance contract, but none is declared on the "
            "part (use declare_performance, then verify_performance)")
        return checks, skipped
    for want in demanded:
        name = want.get("name", "?")
        got = declared.get(name)
        if got is None:
            add(f"performance_spec:{name}", False,
                f"the brief requires requirement {name!r}, which the part does not "
                "declare")
            continue
        tight, why = _at_least_as_tight(got.get("limit"), want.get("limit"))
        add(f"performance_spec:{name}", tight, f"{name}: {why}")

    for item in (block or {}).get("requirements", []):
        name = item["name"]
        if item["state"] == FAIL:
            add(f"performance:{name}", False, item.get("detail") or "not met")
        elif item["state"] == PASS:
            add(f"performance:{name}", True,
                item.get("detail") or f"{name} met by the recorded verdict")
        else:
            skipped.append({"check": f"performance:{name}",
                            "reason": item.get("skipped_reason")
                            or _undecided_reason(item["state"], name, None, None)})
    return checks, skipped
