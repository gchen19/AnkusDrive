"""Experiment-readiness gate — can a multi-agent run on an emergent property be
trusted to measure anything, BEFORE we spend a billed agent round on it?

The multi-agent eval (tests/test_multiagent_m2.py) bills real model calls. The hard
lesson of this project's cost discipline is *validate free first* — two earlier
"divergences" turned out to be harness artifacts (an underspecified prompt, a turn
budget that censored builders), not real effects. This module turns that lesson into
a deterministic gate run on the ORACLE — the thing that will judge the agents —
against known-good / known-bad controls, for free.

An emergent-property assembly experiment (does the mechanism move? does the train
hit its overall ratio?) is only worth billing when ALL of:

  1. discriminates    — the oracle PASSES a known-good config and FAILS a known-bad
                        one. If it can't tell them apart it measures nothing.
  2. deterministic    — identical verdict + score across repeats. A flaky oracle
                        makes a pass-rate meaningless.
  3. margin           — a clear quantitative gap between good and bad, each
                        comfortably on its own side — not a knife-edge that rounding
                        or noise could flip.
  4. emergent         — the bad config passes EVERY local builder slice yet fails at
                        the system level. If a local check already catches it, the
                        property is not emergent and partition/merge has no story.
  5. agent-determined — across a sample of plausible agent outputs the system verdict
                        actually VARIES. If it's invariant to what the agents do,
                        there is nothing for the experiment to measure (a property
                        fixed by the design, not produced by the builders).

Criteria 4 and 5 are the two that specifically protect an *emergent-property*
experiment: 4 says the property is genuinely system-level, 5 says the builders
genuinely drive it. A toy can be emergent yet not agent-determined (e.g. a fixed-
topology gearbox lock) — valuable to reject, since billing it would learn nothing.

Pure-Python, no FreeCAD, no API. See tests/test_experiment_readiness.py for the gate
on synthetic oracles and scratch/readiness_report.py for the gearbox-derived toys.
"""
from __future__ import annotations


def _default_score(result):
    return 1.0 if result.get("ok") else -1.0


def readiness(oracle, positive, negative, *, local_slices=None, agent_variants=None,
              n_repeats=5, min_margin=1.0, score=None):
    """Decide GO / NO-GO for a billed multi-agent experiment from free oracle runs.

    ``oracle(config) -> dict`` with at least ``ok: bool`` (and whatever ``score``
    reads). ``positive`` / ``negative`` are the known-good / known-bad control
    configs. ``score(result) -> float`` ranks how cleanly a result passes (default
    +1 / −1 on ``ok``). ``local_slices(config) -> [ {ok}... ]`` are the per-builder
    local checks (for the emergence test). ``agent_variants`` is a list of configs
    sampling plausible agent outputs (for the agent-determined test). Returns
    {go, checks: {name: {ok, ...}}, reasons:[failed names]}; a check with ok=None was
    not applicable (its input was not supplied) and does not block GO."""
    score = score or _default_score
    pos, neg = oracle(positive), oracle(negative)
    checks = {}

    checks["discriminates"] = {
        "ok": bool(pos.get("ok")) and not neg.get("ok"),
        "positive_ok": bool(pos.get("ok")), "negative_ok": bool(neg.get("ok"))}

    pos_reps = [oracle(positive) for _ in range(n_repeats)]
    neg_reps = [oracle(negative) for _ in range(n_repeats)]
    det = (all(r.get("ok") == pos.get("ok") and abs(score(r) - score(pos)) < 1e-9
               for r in pos_reps)
           and all(r.get("ok") == neg.get("ok") and abs(score(r) - score(neg)) < 1e-9
                   for r in neg_reps))
    checks["deterministic"] = {"ok": det, "n_repeats": n_repeats}

    sp, sn = score(pos), score(neg)
    gap = sp - sn
    checks["margin"] = {"ok": gap >= min_margin and sp > 0 and sn < 0,
                        "positive_score": round(sp, 4), "negative_score": round(sn, 4),
                        "gap": round(gap, 4), "min_margin": min_margin}

    if local_slices is not None:
        neg_local = local_slices(negative)
        all_pass = bool(neg_local) and all(s.get("ok") for s in neg_local)
        emergent = all_pass and not neg.get("ok")
        checks["emergent"] = {
            "ok": emergent, "negative_local_all_pass": all_pass,
            "negative_system_fails": not neg.get("ok"),
            "n_local_slices": len(neg_local),
            "reason": ("every local slice passes yet the system fails — the defect is "
                       "invisible to any single builder")
            if emergent else
            ("a local slice already catches the defect — not an emergent property"
             if all_pass is False else "the negative system did not actually fail")}
    else:
        checks["emergent"] = {"ok": None}

    if agent_variants is not None:
        verdicts = [bool(oracle(v).get("ok")) for v in agent_variants]
        varies = (True in verdicts) and (False in verdicts)
        checks["agent_determined"] = {
            "ok": varies, "n_variants": len(verdicts),
            "n_pass": sum(verdicts), "n_fail": sum(1 for v in verdicts if not v),
            "reason": "agent choices move the system verdict" if varies else
                      "system verdict is invariant to agent choices — nothing to measure"}
    else:
        checks["agent_determined"] = {"ok": None}

    go = all(c["ok"] for c in checks.values() if c.get("ok") is not None)
    return {"go": go, "checks": checks,
            "reasons": [k for k, c in checks.items() if c.get("ok") is False]}
