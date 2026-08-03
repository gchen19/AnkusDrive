"""Design of experiments — the parameter search, recorded (#227).

Exploring a design space in DriftPin has always been possible and never been a
*thing*: the agent hand-rolls ``recipe(params)`` -> ``*_submit`` -> ``job_result`` ->
mutate -> repeat, and when the loop ends the search itself is gone. Only the last
part survives, which means nobody can tell whether the design is good or merely the
last one tried. A study is that loop made into an object: a declared sampling of the
space, a declared way of measuring each sample, and a table that outlives the run.

Three ideas do the work:

**A design point is a dict of parameters, not a position in an array.** Every row
carries the parameters that produced it, so the table is self-describing — sortable,
resumable, and directly consumable by a surrogate model later (the epic's horizon
item) without a key to the axis order.

**Sampling is deterministic.** Full grid for 1-3 variables, Latin hypercube beyond
that, both reproducible from ``seed`` alone. A study you cannot re-run point-for-point
is not evidence; and because the job layer caches on content hash, a reproducible
sample sequence is exactly what makes a re-submitted study free.

**A response is whatever a tool already returns.** ``{tool, metric}`` names an
existing DriftPin tool and a dotted path into its result, the same mapping the
performance-contract layer uses — so a response can be a raw solver number OR a whole
``verify_performance`` verdict, and the two compose without this module knowing the
difference between drag, pressure drop and first mode.

Pure Python, FreeCAD-free, solver-free: sampling, validation and table arithmetic
only. The worker drives it. See ``tests/test_study.py``.
"""
from __future__ import annotations

import itertools
import random

_METHODS = ("grid", "lhs")
_SENSES = ("min", "max")


def _linspace(lo: float, hi: float, n: int) -> list:
    """``n`` evenly spaced values from lo to hi inclusive. n == 1 gives the midpoint —
    a single-level variable means "hold it here", and the midpoint is the only
    defensible reading of a range with one level."""
    if n == 1:
        return [(lo + hi) / 2.0]
    step = (hi - lo) / float(n - 1)
    return [lo + step * i for i in range(n)]


def validate_variables(variables) -> list:
    """Normalize the swept variables, raising ValueError with a specific message on
    anything malformed.

    Two spellings, both allowed and mixable::

        {"name": "diameter_mm", "values": [8, 10, 12]}       # explicit levels
        {"name": "diameter_mm", "min": 8, "max": 12, "levels": 3}   # a range

    A range is expanded to ``levels`` evenly spaced values for grid sampling, and kept
    as continuous bounds for Latin hypercube (which samples *between* the levels — that
    is the point of it). An explicit level list is discrete under both methods: LHS
    stratifies over its indices rather than inventing values the caller did not offer,
    because a list is how you say "these and only these" (materials, tooth counts,
    stock sizes).

    Returns the normalized list of ``{name, kind, values, min, max, levels}``."""
    if not variables:
        raise ValueError("a study needs at least one variable to sweep")
    if isinstance(variables, dict):                 # a single variable, unwrapped
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
        values = var.get("values")
        if values is not None:
            if not isinstance(values, (list, tuple)) or not values:
                raise ValueError(f"variable {name!r}: 'values' must be a non-empty list")
            vals = list(values)
            nums = [v for v in vals if isinstance(v, (int, float))
                    and not isinstance(v, bool)]
            lo = min(nums) if nums else None
            hi = max(nums) if nums else None
            out.append({"name": name, "kind": "levels", "values": vals,
                        "min": lo, "max": hi, "levels": len(vals)})
            continue
        lo, hi = var.get("min"), var.get("max")
        if lo is None or hi is None:
            raise ValueError(
                f"variable {name!r} needs either 'values' (explicit levels) or both "
                "'min' and 'max' (a range)")
        for key, v in (("min", lo), ("max", hi)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"variable {name!r}: {key} must be a number")
        lo, hi = float(lo), float(hi)
        if hi < lo:
            raise ValueError(f"variable {name!r}: min {lo:g} exceeds max {hi:g}")
        levels = int(var.get("levels", 3))
        if levels < 1:
            raise ValueError(f"variable {name!r}: 'levels' must be >= 1")
        out.append({"name": name, "kind": "range", "values": _linspace(lo, hi, levels),
                    "min": lo, "max": hi, "levels": levels})
    return out


def validate_responses(responses) -> list:
    """Normalize the measured responses, raising ValueError on anything malformed.

    Shape (the performance-contract mapping, deliberately)::

        {"name": "dp",
         "tool": "cfd_pipe_flow",              # any DriftPin tool
         "metric": "pressure_drop_pa",         # dotted path into its result
         "conditions": {"diameter_mm": "$diameter_mm", "length_mm": 200}}

    ``conditions`` may reference a swept variable as ``"$<variable name>"`` and the
    point's own materialized part as ``"$handle"``; the worker substitutes both. Naming
    the tool rather than reimplementing the metric is what keeps a study honest: the
    number in the table is the number the tool reports, with its band and its trust
    block attached.

    Returns the normalized list. Whether the named tool EXISTS is the worker's check —
    this module never imports the handler registry."""
    if not responses:
        raise ValueError("a study needs at least one response to measure")
    if isinstance(responses, dict):
        responses = [responses]
    out, seen = [], set()
    for resp in responses:
        if not isinstance(resp, dict):
            raise ValueError(f"each response must be a dict, got {type(resp).__name__}")
        name = resp.get("name") or resp.get("metric")
        if not name or not isinstance(name, str):
            raise ValueError("every response needs a 'name' (or a 'metric' to name it "
                             "after)")
        if name in seen:
            raise ValueError(f"response {name!r} is declared twice")
        seen.add(name)
        if not resp.get("tool"):
            raise ValueError(f"response {name!r} needs a 'tool' — the DriftPin tool that "
                             "measures it; the study layer only orchestrates")
        conditions = resp.get("conditions", resp.get("args"))
        if conditions is not None and not isinstance(conditions, dict):
            raise ValueError(f"response {name!r}: 'conditions' must be a dict")
        out.append({"name": name, "tool": resp["tool"], "metric": resp.get("metric"),
                    "conditions": dict(conditions or {})})
    return out


def sample_points(variables, method: str = "grid", n_samples=None, seed: int = 0) -> list:
    """The design points to evaluate, as a list of ``{name: value}`` dicts.

    ``'grid'`` (default) is the full factorial — every combination, in odometer order
    with the LAST variable varying fastest. Exhaustive and the only thing that can prove
    a trend, but it is the product of the level counts: three variables at five levels
    is 125 solves.

    ``'lhs'`` is a Latin hypercube of ``n_samples`` points: each variable's range is cut
    into ``n_samples`` equal strata and every stratum is used exactly once, so the sample
    covers each axis evenly no matter how many axes there are. That decoupling of cost
    from dimensionality is why it exists — 20 points explore 6 variables as well as they
    explore 2. Use it past 2-3 variables; a grid there is unaffordable.

    Both are deterministic: a grid by construction, LHS from ``seed`` alone. Re-running a
    study therefore re-submits identical work, which the job layer serves from cache.

    Returns the point list; ordering is stable across runs."""
    if method not in _METHODS:
        raise ValueError(f"sampling method must be one of {_METHODS}, got {method!r}")
    if method == "grid":
        combos = itertools.product(*[v["values"] for v in variables])
        return [dict(zip([v["name"] for v in variables], combo)) for combo in combos]

    if n_samples is None:
        raise ValueError("latin-hypercube sampling needs 'n_samples' — unlike a grid it "
                         "has no natural size, that is the whole point of it")
    n = int(n_samples)
    if n < 1:
        raise ValueError("'n_samples' must be >= 1")
    rng = random.Random(seed)
    points = []
    columns = {}
    for var in variables:
        strata = list(range(n))
        rng.shuffle(strata)
        if var["kind"] == "levels":
            # discrete: map each stratum onto an offered level, never between them
            vals = var["values"]
            columns[var["name"]] = [vals[int(s * len(vals) / n)] for s in strata]
        else:
            lo, hi = var["min"], var["max"]
            width = (hi - lo) / float(n)
            columns[var["name"]] = [lo + width * (s + rng.random()) for s in strata]
    for i in range(n):
        points.append({name: col[i] for name, col in columns.items()})
    return points


def _value_of(row, response_name):
    """The numeric value a row recorded for one response, or None when it has none
    (not measured, tool unavailable, metric absent)."""
    cell = (row.get("responses") or {}).get(response_name)
    if not isinstance(cell, dict):
        return None
    v = cell.get("value")
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def summarize_responses(rows, response_names) -> dict:
    """Per-response extremes over the recorded table.

    For each response: ``{n, n_missing, min, max, mean, argmin, argmax}`` where the
    ``arg*`` fields are the *parameter dicts* that produced the extreme, not indices —
    an agent reading this should be able to act on it without holding the table.
    A response no point could measure reports ``n: 0`` and null extremes rather than
    being omitted, because "nothing measured this" is a finding."""
    out = {}
    for name in response_names:
        pairs = [(row, _value_of(row, name)) for row in rows]
        good = [(r, v) for r, v in pairs if v is not None]
        if not good:
            out[name] = {"n": 0, "n_missing": len(pairs), "min": None, "max": None,
                         "mean": None, "argmin": None, "argmax": None}
            continue
        lo_row, lo = min(good, key=lambda rv: rv[1])
        hi_row, hi = max(good, key=lambda rv: rv[1])
        out[name] = {
            "n": len(good),
            "n_missing": len(pairs) - len(good),
            "min": lo, "max": hi,
            "mean": sum(v for _, v in good) / len(good),
            "argmin": dict(lo_row.get("params") or {}),
            "argmax": dict(hi_row.get("params") or {}),
        }
    return out


def best_point(rows, objective) -> dict | None:
    """The row that best satisfies ``objective`` — ``{"response": "dp", "sense": "min"}``.

    Returns ``{index, params, value, sense, response}``, or None when no point produced a
    value for that response (a study where every solve degraded has no best point, and
    inventing one would be a lie). Raises ValueError on a malformed objective or one
    naming a response the study does not measure."""
    if not objective:
        return None
    if not isinstance(objective, dict):
        raise ValueError("'objective' must be a dict like {'response': 'dp', "
                         "'sense': 'min'}")
    name = objective.get("response") or objective.get("name")
    if not name:
        raise ValueError("'objective' needs a 'response' naming one of the study's "
                         "responses")
    sense = objective.get("sense", "min")
    if sense not in _SENSES:
        raise ValueError(f"objective sense must be one of {_SENSES}, got {sense!r}")
    good = [(row, _value_of(row, name)) for row in rows]
    good = [(r, v) for r, v in good if v is not None]
    if not good:
        return None
    pick = (min if sense == "min" else max)(good, key=lambda rv: rv[1])
    row, value = pick
    return {"index": row.get("index"), "params": dict(row.get("params") or {}),
            "value": value, "response": name, "sense": sense}


def fit_power_law(xs, ys):
    """Least-squares exponent of ``y = a * x**b`` over positive samples — the cheap
    trend read that turns a swept table into a statement about physics.

    A pipe study that sweeps diameter and reports ``exponent ≈ -4`` has reproduced
    Hagen-Poiseuille from solved numbers, which is a far stronger claim than any single
    point matching a correlation. Returns ``{exponent, coefficient, r2, n}``, or None
    when fewer than two positive (x, y) pairs survive (a log fit is undefined on
    non-positive data, and a two-point fit is exact by construction — check ``n``
    before reading ``r2``)."""
    import math
    pts = [(float(x), float(y)) for x, y in zip(xs, ys)
           if x is not None and y is not None and float(x) > 0 and float(y) > 0]
    if len(pts) < 2:
        return None
    lx = [math.log(x) for x, _ in pts]
    ly = [math.log(y) for _, y in pts]
    n = len(pts)
    mx, my = sum(lx) / n, sum(ly) / n
    sxx = sum((v - mx) ** 2 for v in lx)
    if sxx <= 0:                       # every x identical — no trend to fit
        return None
    sxy = sum((lx[i] - mx) * (ly[i] - my) for i in range(n))
    b = sxy / sxx
    a = math.exp(my - b * mx)
    ss_tot = sum((v - my) ** 2 for v in ly)
    ss_res = sum((ly[i] - (my + b * (lx[i] - mx))) ** 2 for i in range(n))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return {"exponent": b, "coefficient": a, "r2": r2, "n": n}
