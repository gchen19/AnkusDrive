"""Optimize to a spec — vary parameters until the contract is met (#228).

The end state of the design-to-spec epic is *"make the part meet the spec, and prove
it."* Everything before this issue measures: the geometry bridge produces forces, the
trust layer puts a band on them, the contract layer turns a band into a verdict, and
the study engine records a sweep. None of them SEARCH. This module is the search, and
the two ideas that make it different from a generic minimizer both come from the
contract layer sitting underneath it.

**A three-state verdict changes what a step means.** A conventional optimizer sees
feasible / infeasible. Here a constraint can also come back ``indeterminate`` — the
measurement's uncertainty band straddles the limit — and the correct response is to
measure that point BETTER, not to step away from it. An optimizer that reads
indeterminate as a failure walks away from good designs; one that reads it as a pass
converges on unproven ones. So fidelity escalation is part of the search, not a
post-processing step.

**The search's own tolerance is not the stopping condition.** A converged simplex whose
winning point clears the limit by 2 % while its grid-convergence band is 5 % wide has
not found anything: the answer is inside the noise. Proof is a separate, harsher gate
than convergence, which is why ``proven`` is reported independently of ``converged``.

The optimizer itself is a pure-Python Nelder-Mead with bound clamping and penalized
constraints — no scipy, no gradients, no adjoint. That is deliberate on both counts:
solve counts stay honest and visible (every evaluation is a real measurement, budgeted
and recorded), and the core imports on a bare interpreter so the fast lane can prove
the search arithmetic without FreeCAD or a solver. See ``tests/test_optimize.py``.
"""
from __future__ import annotations

# A constraint violation is scaled by this before being added to the objective, so a
# feasible-but-mediocre point always beats an infeasible-but-brilliant one. Penalized
# (rather than hard-rejected) because a search that starts infeasible still needs a
# gradient pointing back toward the feasible region — a flat "rejected" tells it
# nothing about which way to go.
_PENALTY = 1.0e6

_SENSES = ("min", "max")


def validate_design_vars(variables) -> list:
    """Normalize the search variables, raising ValueError on anything malformed.

    Unlike a study's variables these must be CONTINUOUS and BOUNDED::

        {"name": "diameter_mm", "min": 5.0, "max": 25.0, "start": 10.0}

    A search needs somewhere to go and somewhere to stop; a bare level list gives it
    neither, and an unbounded parameter lets a minimizer walk to a physically absurd
    value that happens to satisfy the arithmetic. ``start`` is optional (default: the
    midpoint), and is clamped into the box.

    Returns the normalized list of ``{name, min, max, start}``."""
    if not variables:
        raise ValueError("an optimization needs at least one variable to vary")
    if isinstance(variables, dict):
        variables = [variables]
    out, seen = [], set()
    for var in variables:
        if not isinstance(var, dict):
            raise ValueError(f"each variable must be a dict, got {type(var).__name__}")
        name = var.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("every variable needs a non-empty string 'name'")
        if name in seen:
            raise ValueError(f"variable {name!r} is declared twice")
        seen.add(name)
        lo, hi = var.get("min"), var.get("max")
        if lo is None or hi is None:
            raise ValueError(
                f"variable {name!r} needs both 'min' and 'max'. An optimizer must be "
                "bounded: without a box it can walk to a value that satisfies the "
                "arithmetic and means nothing physically")
        for key, v in (("min", lo), ("max", hi)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"variable {name!r}: {key} must be a number")
        lo, hi = float(lo), float(hi)
        if hi <= lo:
            raise ValueError(f"variable {name!r}: max must exceed min "
                             f"(got min={lo:g}, max={hi:g})")
        start = var.get("start")
        if start is None:
            start = (lo + hi) / 2.0
        elif isinstance(start, bool) or not isinstance(start, (int, float)):
            raise ValueError(f"variable {name!r}: 'start' must be a number")
        out.append({"name": name, "min": lo, "max": hi,
                    "start": min(max(float(start), lo), hi)})
    return out


def validate_objective(objective) -> dict:
    """Normalize the objective, raising ValueError on anything malformed.

    Shape — the performance-contract mapping, so an objective and a requirement are the
    same kind of thing::

        {"name": "dp", "tool": "cfd_internal_flow_submit",
         "metric": "pressure_drop_pa", "sense": "min",
         "conditions": {"diameter_mm": "$diameter_mm", "length_mm": 500},
         "screen": {"tool": "cfd_pipe_flow", "metric": "pressure_drop_pa",
                    "conditions": {...}}}

    The optional ``screen`` block is what makes the two-phase ladder possible: search
    cheaply on the correlation, then polish on the solver from where the screen landed.
    Returns the normalized objective."""
    if not isinstance(objective, dict):
        raise ValueError("'objective' must be a dict naming what to minimize or "
                         "maximize")
    if not objective.get("tool"):
        raise ValueError("the objective needs a 'tool' — the DriftPin tool that measures "
                         "it; this layer only orchestrates")
    if not objective.get("metric"):
        raise ValueError("the objective needs a 'metric' (a dotted path into the tool's "
                         "result, e.g. 'pressure_drop_pa')")
    sense = objective.get("sense", "min")
    if sense not in _SENSES:
        raise ValueError(f"objective sense must be one of {_SENSES}, got {sense!r}")
    screen = objective.get("screen")
    if screen is not None and (not isinstance(screen, dict) or not screen.get("tool")):
        raise ValueError("the objective's 'screen' block needs at least a 'tool'")
    out = dict(objective)
    out["sense"] = sense
    out.setdefault("name", objective.get("metric"))
    out["conditions"] = dict(objective.get("conditions")
                             or objective.get("args") or {})
    return out


def validate_budget(budget) -> dict:
    """Normalize the search budget. ``{max_evals, max_wall_s}``; defaults 40 evaluations
    and no wall limit.

    A budget is not a nicety here — every evaluation at solver tier is a real solve, so
    an unbudgeted derivative-free search is an unbounded bill. Cache hits do not count
    against ``max_evals`` (a revisited point cost nothing, and charging for it would make
    the optimizer's own bookkeeping the reason it stopped)."""
    budget = dict(budget or {})
    max_evals = int(budget.get("max_evals", 40))
    if max_evals < 2:
        raise ValueError("'max_evals' must be at least 2 — a search needs to compare "
                         "at least two points")
    wall = budget.get("max_wall_s")
    if wall is not None:
        wall = float(wall)
        if wall <= 0:
            raise ValueError("'max_wall_s' must be positive")
    return {"max_evals": max_evals, "max_wall_s": wall}


def score(objective_value, sense: str, violations) -> float:
    """Collapse one measurement plus its constraint violations into the single number
    the simplex walks on.

    Maximization is minimization of the negative; a violation is added as a *relative*
    excess times :data:`_PENALTY`, so constraints on quantities of wildly different
    magnitude (a 0.3 drag coefficient and a 50 Pa pressure drop) still trade off
    sensibly against each other."""
    val = float(objective_value)
    if sense == "max":
        val = -val
    return val + _PENALTY * sum(max(0.0, float(v)) for v in violations)


def relative_violation(measured, limit: dict) -> float:
    """How badly one measurement misses one ``{max, min}`` limit, as a fraction of the
    limit (0.0 when satisfied).

    Relative rather than absolute so the penalty is scale-free — see :func:`score`."""
    if measured is None:
        return 0.0
    m = float(measured)
    worst = 0.0
    hi = limit.get("max")
    if hi is not None and m > hi:
        worst = max(worst, (m - hi) / abs(hi) if hi else (m - hi))
    lo = limit.get("min")
    if lo is not None and m < lo:
        worst = max(worst, (lo - m) / abs(lo) if lo else (lo - m))
    return worst


def _clamp(x, lower, upper):
    return [min(max(v, lo), hi) for v, lo, hi in zip(x, lower, upper)]


def _centroid(pts):
    n = len(pts[0])
    return [sum(p[i] for p in pts) / len(pts) for i in range(n)]


def nelder_mead(f, x0, lower, upper, max_evals: int = 40,
                xtol: float = 1e-4, ftol: float = 1e-6, spent=None) -> dict:
    """Bounded Nelder-Mead simplex search over ``f(x) -> float``, pure Python.

    Derivative-free by necessity: there is no adjoint through a CFD solve, and a finite
    difference would cost one extra solve per variable per step. The simplex instead
    reflects, expands and contracts a set of n+1 points, so it needs roughly one
    evaluation per iteration once running.

    Every trial point is CLAMPED into the box rather than rejected, which is what lets
    the search ride a bound — and riding a bound is the normal outcome of a real design
    problem, where the optimum sits against the envelope, not in the interior.

    ``f`` may return None for a point that could not be measured; that point is treated
    as infinitely bad rather than aborting the search (one failed solve should not
    discard the whole run). Stops on ``max_evals``, on the simplex collapsing below
    ``xtol`` in every dimension, or on its spread in f falling under ``ftol``.

    ``spent`` optionally supplies the caller's OWN count of what the budget has bought.
    A simplex revisits points constantly — shrink steps and repeated reflections land on
    coordinates already measured — and a caller that serves those from cache paid
    nothing for them, so charging them against ``max_evals`` would let bookkeeping, not
    physics, end the search. When ``spent`` is given the internal call counter stays on
    only as a runaway backstop.

    Returns {x, fx, n_evals, iterations, converged, reason, simplex}."""
    n = len(x0)
    if n == 0:
        raise ValueError("nelder_mead needs at least one variable")
    lower, upper = list(lower), list(upper)
    calls = {"n": 0}
    hard_cap = max_evals * 10 if spent is not None else max_evals

    def _budget_used():
        return spent() if spent is not None else calls["n"]

    def _out_of_budget():
        return _budget_used() >= max_evals or calls["n"] >= hard_cap

    def _f(x):
        calls["n"] += 1
        v = f(x)
        return float("inf") if v is None else float(v)

    # initial simplex: the start point plus one step per axis, sized to the box so the
    # first moves are meaningful whatever the parameter's units happen to be
    simplex = [_clamp(list(x0), lower, upper)]
    for i in range(n):
        pt = list(x0)
        span = upper[i] - lower[i]
        step = 0.1 * span if span > 0 else 0.1
        pt[i] = pt[i] + step
        if pt[i] > upper[i]:                       # step inward when already at the top
            pt[i] = x0[i] - step
        simplex.append(_clamp(pt, lower, upper))

    fvals = [_f(p) for p in simplex]
    iterations = 0
    reason = "max_evals"
    converged = False

    while not _out_of_budget():
        order = sorted(range(len(simplex)), key=lambda i: fvals[i])
        simplex = [simplex[i] for i in order]
        fvals = [fvals[i] for i in order]

        widths = [max(p[i] for p in simplex) - min(p[i] for p in simplex)
                  for i in range(n)]
        spans = [upper[i] - lower[i] or 1.0 for i in range(n)]
        if all(w <= xtol * s for w, s in zip(widths, spans)):
            reason, converged = "xtol", True
            break
        finite = [v for v in fvals if v != float("inf")]
        if len(finite) > 1 and (max(finite) - min(finite)) <= ftol * (
                abs(min(finite)) + ftol):
            reason, converged = "ftol", True
            break

        best, worst = simplex[0], simplex[-1]
        cent = _centroid(simplex[:-1])
        refl = _clamp([c + (c - w) for c, w in zip(cent, worst)], lower, upper)
        f_refl = _f(refl)

        if f_refl < fvals[0]:                                     # expand
            if _out_of_budget():
                simplex[-1], fvals[-1] = refl, f_refl
                break
            exp = _clamp([c + 2.0 * (c - w) for c, w in zip(cent, worst)],
                         lower, upper)
            f_exp = _f(exp)
            simplex[-1], fvals[-1] = ((exp, f_exp) if f_exp < f_refl
                                      else (refl, f_refl))
        elif f_refl < fvals[-2]:                                  # accept reflection
            simplex[-1], fvals[-1] = refl, f_refl
        else:                                                     # contract
            if _out_of_budget():
                break
            con = _clamp([c + 0.5 * (w - c) for c, w in zip(cent, worst)],
                         lower, upper)
            f_con = _f(con)
            if f_con < fvals[-1]:
                simplex[-1], fvals[-1] = con, f_con
            else:                                                 # shrink toward best
                for i in range(1, len(simplex)):
                    if _out_of_budget():
                        break
                    simplex[i] = _clamp(
                        [b + 0.5 * (p - b) for b, p in zip(best, simplex[i])],
                        lower, upper)
                    fvals[i] = _f(simplex[i])
        iterations += 1

    order = sorted(range(len(simplex)), key=lambda i: fvals[i])
    simplex = [simplex[i] for i in order]
    fvals = [fvals[i] for i in order]
    return {"x": simplex[0], "fx": fvals[0], "n_evals": calls["n"],
            "iterations": iterations, "converged": converged, "reason": reason,
            "simplex": simplex}


def band_covers_margin(margin_pct, band_pct) -> bool:
    """Is the winning point's margin swallowed by its own uncertainty band?

    The stopping condition the trust layer adds on top of the optimizer's own tolerance
    (kickoff note): clearing a limit by 2 % with a 5 %-wide grid-convergence band is not
    a result, it is noise with a favourable sign. True means "not converged ENOUGH",
    regardless of what the simplex thinks."""
    if band_pct is None or margin_pct is None:
        return False
    return float(band_pct) >= abs(float(margin_pct))
