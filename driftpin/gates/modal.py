"""Modal (`min_first_mode_hz`) merge-gate outcomes — issues #172, #248.

The physics-tier requirement gate (worker.py ``_first_mode_requirement``) runs a
live CalculiX frequency extraction on the fused assembly and compares mode 1
against a declared floor. That has THREE outcomes, not two — and collapsing the
third into "the part failed" is what let a busy CI runner block PR #241:

  * the solve completed, f1 at/above the floor   -> ``PASS``
  * the solve completed, f1 below the floor      -> ``FAIL`` — a real VERDICT:
    the part is too floppy, and it fails the merge. Nothing here may soften it.
  * the solve produced no frequency at all — ccx absent, ccx killed, the mesh
    never solved, or the caller's wall-clock budget ran out -> ``INCOMPLETE``:
    NO verdict. The gate has learned nothing about the part, and must say so.

``INCOMPLETE`` is reported through the vocabulary this gate already has for "the
modelling assumptions weren't declared" (a bare requirement, ``bonding: tied``):
the requirement rides in the merge report's ``requirements.skipped`` list with a
``skipped_reason`` naming the cause. So it is loud in the log, it never reads as
"requirement met", and it never fails a merge for something the part didn't do.

What it must never be is a report shaped like a pass with the measurement simply
missing — then every caller trips over the same hole independently, which is
literally how #248 surfaced (``KeyError: 'measured_first_mode_hz'`` inside
tests/test_merge_modal_gate.py).

Pure Python, no FreeCAD: the worker gate builds its sub-report through
:func:`incomplete_report`, and every consumer — the tests, a coordinator deciding
whether to re-dispatch a component — reads it back through :func:`classify`, so
there is exactly one predicate for "was there a verdict?".
"""

REQUIREMENT = "min_first_mode_hz"

# --- outcomes ----------------------------------------------------------------
PASS = "pass"                    # solved, at/above the floor
FAIL = "fail"                    # solved, below the floor -> fails the merge
INCOMPLETE = "incomplete"        # no measurement: solver absent / failed / timed out
NOT_DECLARED = "not_declared"    # no fixture / bare number / bonding != fused
ERROR = "error"                  # declared but unmodellable -> fails the merge
NOT_GATED = "not_gated"          # the manifest never asked for this gate

#: Outcomes that are a statement about the PART (and so decide a merge). The
#: others are statements about the run, or about what the manifest declared.
VERDICTS = (PASS, FAIL)

# --- causes of an INCOMPLETE solve -------------------------------------------
SOLVER_ABSENT = "solver_absent"      # CalculiX not found at solve time
SOLVE_FAILED = "solve_failed"        # ccx ran but produced no frequencies
TIMED_OUT = "timed_out"              # the caller's wall-clock budget ran out
NO_MEASUREMENT = "no_measurement"    # report has no f1 and doesn't say why

_CAUSE_TEXT = {
    SOLVER_ABSENT: "modal solve did not run — CalculiX (ccx) not found",
    SOLVE_FAILED: "modal solve did not complete",
    TIMED_OUT: "modal solve exceeded its wall-clock budget",
    NO_MEASUREMENT: "modal gate returned no first-mode measurement and no reason",
}

_NO_VERDICT = ("no first-mode measurement, so the gate has NO verdict on this "
               "part (not a pass, not a failure)")


def incomplete_reason(cause, detail="", elapsed_s=None):
    """One loud sentence for a solve that produced no measurement: what went
    wrong, how long it burned, and — explicitly — that there is no verdict."""
    text = _CAUSE_TEXT.get(cause, _CAUSE_TEXT[SOLVE_FAILED])
    if elapsed_s is not None:
        text += f" (after {float(elapsed_s):.1f} s)"
    if detail:
        text += f": {detail}"
    return f"{text} — {_NO_VERDICT}"


def incomplete_report(min_hz, cause, detail="", elapsed_s=None, **extra):
    """The ``first_mode`` sub-report the worker gate emits when the solve
    produced no frequency. Deliberately shaped like the gate's other skips
    (``skipped_reason``) and deliberately WITHOUT ``measured_first_mode_hz`` or
    ``pass`` — there is nothing to report, and a placeholder would be a lie."""
    rep = {"min_hz": min_hz, "solve": INCOMPLETE, "cause": cause,
           "skipped_reason": incomplete_reason(cause, detail, elapsed_s)}
    if elapsed_s is not None:
        rep["elapsed_s"] = round(float(elapsed_s), 1)
    rep.update({k: v for k, v in extra.items() if v is not None})
    return rep


def timed_out(budget_s, detail=""):
    """Outcome for a merge call the CALLER abandoned on its own deadline (the
    worker never got to answer, so there is no report to classify). Same shape
    as :func:`classify`'s result so both paths are handled by one branch."""
    d = f"no response within {float(budget_s):.0f} s"
    if detail:
        d += f" ({detail})"
    return {"outcome": INCOMPLETE, "cause": TIMED_OUT, "measured_hz": None,
            "min_hz": None, "reason": incomplete_reason(TIMED_OUT, d)}


def _result(outcome, reason, measured_hz=None, min_hz=None, cause=None,
            violation=None):
    return {"outcome": outcome, "reason": reason, "measured_hz": measured_hz,
            "min_hz": min_hz, "cause": cause, "violation": violation}


def _violation(merge_report):
    for v in ((merge_report or {}).get("gates") or {}).get("requirements") or []:
        if isinstance(v, dict) and v.get("requirement") == REQUIREMENT:
            return v
    return None


def classify(merge_report):
    """Classify a ``merge_assembly`` report's modal gate.

    Returns ``{outcome, reason, measured_hz, min_hz, cause, violation}`` where
    ``outcome`` is one of PASS / FAIL / INCOMPLETE / NOT_DECLARED / ERROR /
    NOT_GATED. Reads only what the report actually carries — a missing
    measurement can therefore never come back as PASS, whatever else is wrong
    with the report, and never raises."""
    req = (merge_report or {}).get("requirements") or {}
    fm = (req.get("report") or {}).get("first_mode")
    viol = _violation(merge_report)

    if not isinstance(fm, dict):
        if viol is not None:
            return _result(ERROR, viol.get("reason") or viol.get("error")
                           or "modal gate reported a violation", violation=viol)
        return _result(NOT_GATED,
                       f"the manifest declares no {REQUIREMENT} requirement")

    min_hz = fm.get("min_hz")
    measured = fm.get("measured_first_mode_hz")
    if measured is not None:
        # A completed solve. This is the branch that must stay strict.
        if "pass" in fm:
            passed = bool(fm["pass"])
        else:
            passed = min_hz is None or float(measured) >= float(min_hz)
        if passed:
            return _result(PASS, f"first mode {float(measured):.1f} Hz >= floor "
                           f"{min_hz} Hz", measured, min_hz)
        return _result(FAIL, (viol or {}).get("reason")
                       or f"first mode {float(measured):.1f} Hz < floor "
                          f"{min_hz} Hz (too floppy)",
                       measured, min_hz, violation=viol)

    # No measurement — never a pass, and never a KeyError for the caller.
    if fm.get("solve") == INCOMPLETE:
        return _result(INCOMPLETE, fm.get("skipped_reason")
                       or incomplete_reason(fm.get("cause", SOLVE_FAILED)),
                       min_hz=min_hz, cause=fm.get("cause", SOLVE_FAILED))
    if viol is not None:
        return _result(ERROR, viol.get("reason") or viol.get("error")
                       or "modal gate reported a violation",
                       min_hz=min_hz, violation=viol)
    if fm.get("solve") == NOT_DECLARED or REQUIREMENT in (req.get("skipped") or []):
        return _result(NOT_DECLARED,
                       fm.get("skipped_reason") or "modelling assumptions not declared",
                       min_hz=min_hz)
    return _result(INCOMPLETE, incomplete_reason(NO_MEASUREMENT),
                   min_hz=min_hz, cause=NO_MEASUREMENT)


def has_verdict(outcome):
    """True only when the gate actually measured the part. Callers that need a
    pass/fail decision must branch on this rather than on ``ok``."""
    if isinstance(outcome, dict):
        outcome = outcome.get("outcome")
    return outcome in VERDICTS
