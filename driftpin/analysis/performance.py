"""Performance contracts — a quantitative spec as a first-class, checkable object (#226).

DriftPin's contract layer was purely geometric: watertight, airtight path, envelope,
interfaces. A performance requirement — "Cd ≤ 0.30 at 30 m/s", "Δp ≤ 50 Pa at 10 L/min",
"first mode ≥ 200 Hz" — lived only in the conversation, which is exactly where it gets
lost. This module is the arithmetic half of making it an object that rides on the part
and can be re-checked after every edit.

Three ideas do the work:

**A verdict has three states, not two.** ``pass`` / ``fail`` / ``indeterminate``. The
third is what makes a banded estimate usable: a correlation with ±10 % scatter that
measures Cd = 0.28 against a limit of 0.30 has NOT shown the part passes — the true value
could be 0.308. Collapsing that to "pass" is how a spec silently goes unmet. Only a
verdict whose whole band sits on one side of the limit is decided.

**Evidence is laddered.** A requirement names a cheap screening estimator and a solver,
and declares which one counts as proof (``fidelity_floor``). ``tier='auto'`` runs the
screen first and escalates only when the screen cannot decide or when the floor demands
a solve — the epic's "screening tier narrows the design space" made mechanical.

**Trust is part of the measurement, not a footnote.** An unconverged solve, or one whose
grid-convergence band is wider than the requirement tolerates, cannot satisfy a
contract — it comes back ``indeterminate`` with the reason, never ``pass``.

Pure-Python, FreeCAD-free, solver-free: everything here operates on numbers and dicts the
worker collects. See ``tests/test_performance.py``.
"""
from __future__ import annotations

_TIERS = ("screen", "solver")
_STATES = ("pass", "fail", "indeterminate")


def _get_path(payload, path: str):
    """Dotted-path lookup into a result payload — ``"cd"``, ``"trust.converged"``,
    ``"modes.0.frequency_hz"``. Returns None when any step is missing rather than
    raising, since a metric the tool did not produce is a reportable outcome, not a
    crash."""
    cur = payload
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def validate_requirement(req: dict) -> dict:
    """Normalize and check one requirement, raising ValueError with a specific message
    on anything malformed.

    Shape::

        {
          "name": "drag_at_cruise",
          "metric": "cd",                       # dotted path into the tool's result
          "tool": "cfd_external_flow_submit",   # what measures it at solver tier
          "conditions": {"model": "$handle", "velocity_m_s": 30},
          "limit": {"max": 0.30},               # max, min, or both
          "screen": {"tool": "cfd_body_drag", "metric": "cd",
                     "conditions": {"shape": "sphere", "diameter_mm": 50,
                                    "velocity_m_s": 30}},
          "fidelity_floor": "screen" | "solver",
          "trust": {"converged": true, "band_max_pct": 5}
        }

    ``"$handle"`` anywhere in ``conditions`` is replaced with the part's own handle when
    the requirement runs, so a contract survives being re-declared on a different part.
    ``limit`` needs at least one of ``max``/``min``; giving both means a window.
    ``fidelity_floor`` defaults to ``'solver'`` — proof is a solve unless the author
    says a screen is enough. Returns the normalized requirement."""
    if not isinstance(req, dict):
        raise ValueError(f"each requirement must be a dict, got {type(req).__name__}")
    name = req.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("every requirement needs a non-empty string 'name'")
    if not req.get("metric"):
        raise ValueError(f"requirement {name!r} needs a 'metric' (a dotted path into "
                         "the measuring tool's result, e.g. 'cd')")
    if not req.get("tool"):
        raise ValueError(f"requirement {name!r} needs a 'tool' — the DriftPin tool that "
                         "measures the metric; the contract layer only orchestrates")
    limit = req.get("limit")
    if not isinstance(limit, dict) or not ({"max", "min"} & set(limit)):
        raise ValueError(f"requirement {name!r} needs a 'limit' with 'max' and/or 'min'")
    for key in ("max", "min"):
        if key in limit and not isinstance(limit[key], (int, float)):
            raise ValueError(f"requirement {name!r}: limit.{key} must be a number")
    if "max" in limit and "min" in limit and limit["min"] > limit["max"]:
        raise ValueError(f"requirement {name!r}: limit.min exceeds limit.max")
    floor = req.get("fidelity_floor", "solver")
    if floor not in _TIERS:
        raise ValueError(f"requirement {name!r}: fidelity_floor must be one of {_TIERS}")
    screen = req.get("screen")
    if screen is not None:
        if not isinstance(screen, dict) or not screen.get("tool"):
            raise ValueError(f"requirement {name!r}: 'screen' needs at least a 'tool'")
    if floor == "screen" and screen is None:
        raise ValueError(
            f"requirement {name!r} declares fidelity_floor='screen' but gives no "
            "'screen' block — there is nothing that could serve as proof")
    trust = req.get("trust")
    if trust is not None and not isinstance(trust, dict):
        raise ValueError(f"requirement {name!r}: 'trust' must be a dict")
    out = dict(req)
    out["fidelity_floor"] = floor
    return out


def evaluate_limit(measured, limit: dict, band_pct=None) -> dict:
    """Decide one measurement against one limit, WITH its uncertainty band applied.

    The band (a correlation's ±%, or a solve's grid-convergence %) is not decoration: a
    measurement of 0.28 with ±10 % against ``max: 0.30`` spans 0.252–0.308 and therefore
    has not shown the part passes. This returns ``pass`` only when the whole band clears
    the limit, ``fail`` only when the whole band misses it, and ``indeterminate`` when
    the band straddles it — which is the signal to escalate to a tighter measurement,
    not to guess.

    ``margin`` is the fractional slack against the binding limit (positive = satisfied)
    computed on the nominal measurement, so it stays comparable across tiers.

    Returns {state, measured, limit, band_pct, worst_case, best_case, margin,
    margin_pct, detail}. Raises ValueError if ``measured`` is not a number."""
    if measured is None or isinstance(measured, bool) or not isinstance(
            measured, (int, float)):
        raise ValueError(f"measured value must be a number, got {measured!r}")
    m = float(measured)
    band = float(band_pct) if band_pct not in (None, "") else 0.0
    if band < 0:
        raise ValueError("band_pct must be >= 0")
    spread = abs(m) * band / 100.0
    hi, lo = m + spread, m - spread

    lo_lim = limit.get("min")
    hi_lim = limit.get("max")
    states, details, margins = [], [], []
    if hi_lim is not None:
        if hi <= hi_lim:
            states.append("pass")
        elif lo > hi_lim:
            states.append("fail")
        else:
            states.append("indeterminate")
        margins.append((hi_lim - m) / abs(hi_lim) if hi_lim else float("inf"))
        details.append(f"{m:.6g} vs max {hi_lim:.6g}")
    if lo_lim is not None:
        if lo >= lo_lim:
            states.append("pass")
        elif hi < lo_lim:
            states.append("fail")
        else:
            states.append("indeterminate")
        margins.append((m - lo_lim) / abs(lo_lim) if lo_lim else float("inf"))
        details.append(f"{m:.6g} vs min {lo_lim:.6g}")

    if "fail" in states:
        state = "fail"
    elif "indeterminate" in states:
        state = "indeterminate"
    else:
        state = "pass"
    margin = min(margins)
    detail = " and ".join(details)
    if band:
        detail += f" (band ±{band:g} % spans {lo:.6g}..{hi:.6g})"
    if state == "indeterminate":
        detail += " — the band straddles the limit, so this measurement cannot decide it"
    return {
        "state": state,
        "measured": m,
        "limit": dict(limit),
        "band_pct": band or None,
        "worst_case": hi,
        "best_case": lo,
        "margin": margin,
        "margin_pct": margin * 100.0,
        "detail": detail,
    }


def check_trust(payload: dict, trust: dict | None) -> list:
    """Reasons a measurement is not admissible as proof, given the requirement's ``trust``
    demands. Empty list = admissible.

    Understands the fields the CFD trust layer (#225) produces:
      ``converged``    — require ``trust.converged`` True on the result
      ``band_max_pct`` — the reported band (``band_pct`` or ``gci_pct``) must be no wider
      ``mesh_ok``      — require checkMesh to have passed
      ``gated``        — require the solve path to have a verified oracle

    A demand the payload cannot answer is itself a reason: silence is not evidence."""
    if not trust:
        return []
    reasons = []
    if trust.get("converged"):
        conv = _get_path(payload, "trust.converged")
        if conv is None:
            conv = payload.get("converged")
        if conv is not True:
            reasons.append(
                "the requirement demands a converged solve; this result reports "
                f"converged={conv!r} — an unconverged solve is not proof of anything")
    if trust.get("mesh_ok"):
        mesh_ok = _get_path(payload, "trust.mesh.ok")
        if mesh_ok is not True:
            reasons.append(
                f"the requirement demands a clean mesh check; checkMesh reports "
                f"ok={mesh_ok!r}")
    if trust.get("gated"):
        gated = payload.get("gated")
        if gated is not True:
            reasons.append(
                "the requirement demands a solve path with a verified oracle; this one "
                f"reports gated={gated!r}")
    cap = trust.get("band_max_pct")
    if cap is not None:
        band = payload.get("gci_pct")
        if band is None:
            band = payload.get("band_pct")
        if band is None:
            reasons.append(
                f"the requirement caps the uncertainty band at {cap:g} % but the result "
                "reports no band at all — run a mesh-independence study "
                "(cfd_mesh_independence_submit) or drop the cap")
        elif float(band) > float(cap):
            reasons.append(
                f"the uncertainty band is {float(band):.3g} %, wider than the "
                f"{float(cap):g} % this requirement allows")
    return reasons


def summarize(results: list) -> dict:
    """Roll per-requirement verdicts into a contract verdict.

    ``ok`` is True only when EVERY requirement passed — an indeterminate result is not a
    pass, and a contract with nothing decided is not satisfied. ``escalate`` lists the
    requirements a tighter measurement could still decide, which is the actionable half
    of a non-ok verdict."""
    states = [r.get("state") for r in results]
    return {
        "ok": bool(results) and all(s == "pass" for s in states),
        "n_requirements": len(results),
        "passed": sum(1 for s in states if s == "pass"),
        "failed": sum(1 for s in states if s == "fail"),
        "indeterminate": sum(1 for s in states if s == "indeterminate"),
        "escalate": [r["name"] for r in results
                     if r.get("state") == "indeterminate" and r.get("name")],
    }
