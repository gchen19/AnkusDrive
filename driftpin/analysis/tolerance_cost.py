"""Tolerance–cost coupling (issue #235): what a tolerance *costs*, and how much of
it you can give back.

``tolerance_stackup`` / ``fit_class`` answer "what tolerance works". ``cost_estimate``
answers "what does the part cost". Until this module they did not talk, so nothing in
the tool surface pushed a design toward **the loosest tolerance that works** — the
actual production-engineering skill, and precisely the judgement an LLM agent will
not apply unless a tool makes it visible.

Pure-Python, FreeCAD-free, in the ``analysis/`` house style: explicit inputs, small
rounded dicts, a labelled fidelity. The ISO 286 IT tables are **reused** from
:mod:`driftpin.analysis.tolerance` — there is one source of truth for the bands and
this module only reads it.

Three ideas carry the module:

* **The IT grade is continuous.** A 15 µm band on Ø20 is not "IT7"; it is tighter
  than IT7 and looser than IT6. :func:`it_grade` log-interpolates the tabulated row
  so the grade — and therefore the cost — moves smoothly with the band. That is what
  makes the cost index STRICTLY monotone rather than a staircase, which in turn is
  what lets :func:`suggest_loosening` rank candidate moves at all.
* **Cost has a knee at the process's natural capability.** Below it you are buying
  a slower feed, an extra spring pass, a secondary operation and 100 % gauging, and
  the handbook shape applies: cost roughly doubles every 1–2 IT grades. Above it
  the process is already at its floor and the only thing left to save is inspection
  and scrap, so the curve keeps falling but far more gently. One kink, both sides
  documented.
* **The ratios are the product; the absolutes are not.** Everything here is
  ``fidelity="correlation"``. A cost *index* is dimensionless — it is meaningful
  only as scheme-A-vs-scheme-B, never as dollars. Say so on every return.

Units: lengths mm, IT grades dimensionless (fractional), cost a dimensionless index
normalised to 1.0 at the process's natural capability.
"""
from __future__ import annotations

import math

from . import tolerance

# --- the IT-grade axis --------------------------------------------------------
#
# ISO 286-1 builds grades IT5 and up on the R5 preferred-number series: each grade
# is 10^(1/5) ≈ 1.5849x the previous one, so five grades is exactly a decade. We
# INTERPOLATE inside the tabulated range (tolerance._IT covers IT4..IT11, the real
# numbers including their standard rounding) and EXTRAPOLATE outside it on the R5
# ratio. Extrapolation is faithful: from the tabulated IT11 at 3-6 mm (75 µm) the
# series predicts IT12=119, IT13=188, IT14=299, IT15=473, IT16=750 µm against the
# standard's 120/180/300/480/750 — under a tenth of a grade of error out at IT16,
# which is far inside this module's declared band.

_R5 = 10.0 ** 0.2          # one IT grade, as a ratio of tolerance widths
_LOG_R5 = math.log(_R5)

#: Coarsest grade this module will talk about. ISO 286-1 stops at IT18; IT16 is
#: already coarser than a sand casting, so nothing useful lives past it.
COARSEST_IT = 16.0
#: Finest grade. IT01..IT3 leave the R5 series, so extrapolating below IT4 is a
#: guess; we refuse rather than invent a number.
FINEST_IT = 4.0


def _row(nominal_mm: float) -> tuple[list[int], list[float]]:
    """(grades, band_um) for the ISO 286 size band containing ``nominal_mm``.

    Straight out of :mod:`driftpin.analysis.tolerance` — this module never
    tabulates an IT number of its own."""
    idx = tolerance._band_index(abs(float(nominal_mm)))
    grades = sorted(tolerance._IT)
    return grades, [float(tolerance._IT[g][idx]) for g in grades]


def it_grade(nominal_mm: float, band_mm: float) -> float:
    """The ISO 286 IT grade a tolerance band corresponds to, as a **float**.

    ``band_mm`` is the TOTAL band (upper deviation − lower deviation), not the
    half-band: a ``±0.1`` dimension has a band of 0.2. Returns e.g. 7.0 for a band
    that is exactly the tabulated IT7 width, 6.42 for one between IT6 and IT7.

    The fractional part is what makes the cost curve differentiable, so
    :func:`suggest_loosening` can rank two candidate links that would otherwise
    round to the same grade. Clamped to [:data:`FINEST_IT`, :data:`COARSEST_IT`].
    Raises ValueError on a non-positive band or an off-table size (>500 mm)."""
    band_um = abs(float(band_mm)) * 1000.0
    if band_um <= 0:
        raise ValueError("band_mm must be > 0 — a zero-tolerance (basic) dimension "
                         "has no IT grade; it is held by its geometric control")
    grades, bands = _row(nominal_mm)
    if band_um <= bands[0]:
        # tighter than the finest tabulated grade — extrapolate down the R5 series
        g = grades[0] + math.log(band_um / bands[0]) / _LOG_R5
        return round(max(FINEST_IT, g), 4)
    if band_um >= bands[-1]:
        g = grades[-1] + math.log(band_um / bands[-1]) / _LOG_R5
        return round(min(COARSEST_IT, g), 4)
    for i in range(len(grades) - 1):
        lo, hi = bands[i], bands[i + 1]
        if lo <= band_um <= hi:
            frac = math.log(band_um / lo) / math.log(hi / lo)
            return round(grades[i] + frac, 4)
    raise AssertionError("unreachable: band fell outside a bracketed row")


def band_for_grade(nominal_mm: float, grade: float) -> float:
    """Inverse of :func:`it_grade` — the tolerance band (mm) of an IT grade at a
    nominal size. Accepts a fractional grade. This is how a link is "opened one
    grade": ``band_for_grade(nominal, it_grade(nominal, band) + 1)``."""
    g = float(grade)
    grades, bands = _row(nominal_mm)
    if g <= grades[0]:
        um = bands[0] * (_R5 ** (g - grades[0]))
    elif g >= grades[-1]:
        um = bands[-1] * (_R5 ** (g - grades[-1]))
    else:
        i = min(int(math.floor(g)) - grades[0], len(grades) - 2)
        lo, hi = bands[i], bands[i + 1]
        frac = g - grades[i]
        um = lo * ((hi / lo) ** frac)
    return round(um / 1000.0, 9)


# --- the per-process corpus ---------------------------------------------------
#
# `natural_it` is the grade a well-set-up process holds on a production run without
# special effort — the knee of its cost curve. These are the classic shop
# capability numbers (drilling ~IT11, milling ~IT9-10, turning ~IT8-9, reaming ~IT7,
# grinding ~IT6) and they are ORDINAL: what this module guarantees is that reaming
# sits two grades tighter than milling, not that any absolute grade is exactly right
# for a given machine.
#
# `in_process_it` is how far BELOW natural the same operation can be pushed by
# slowing the feed, adding a spring pass and tightening the offsets — i.e. without a
# secondary operation. Cutting processes get 2 grades: the tool is in contact and
# under closed-loop control, so the operator has a knob. Net-shape and additive
# processes get 0: a moulded or printed dimension's scatter is thermal and material,
# not tool-controlled, so tightening it means machining it afterwards — which is a
# different operation and a different cost line, and must show up as a flag.
#
# `base` is a per-process cost anchor only used to compare ROUTES (which process to
# choose), never mixed into the tolerance curve itself.

_PROCESSES = {
    # --- single machining operations, coarse -> fine ---
    "drilling":  {"natural_it": 11.0, "in_process_it": 2.0, "kind": "cutting"},
    "milling":   {"natural_it": 10.0, "in_process_it": 2.0, "kind": "cutting"},
    "turning":   {"natural_it": 9.0,  "in_process_it": 2.0, "kind": "cutting"},
    "boring":    {"natural_it": 8.0,  "in_process_it": 2.0, "kind": "cutting"},
    "reaming":   {"natural_it": 7.0,  "in_process_it": 2.0, "kind": "cutting"},
    "grinding":  {"natural_it": 6.0,  "in_process_it": 2.0, "kind": "cutting"},
    "honing":    {"natural_it": 5.0,  "in_process_it": 1.0, "kind": "cutting"},
    "lapping":   {"natural_it": 4.0,  "in_process_it": 1.0, "kind": "cutting"},
    # --- part-level processes, i.e. what cost_estimate/dfm_check call a process ---
    # A CNC cell is not one operation: a finish bore or a fine turning pass lives in
    # the same setup as the roughing, so the cell quotes a grade tighter than plain
    # milling.
    "cnc":       {"natural_it": 9.0,  "in_process_it": 2.0, "kind": "cutting"},
    "injection": {"natural_it": 11.0, "in_process_it": 0.0, "kind": "net_shape"},
    "casting":   {"natural_it": 13.0, "in_process_it": 0.0, "kind": "net_shape"},
    "sheet":     {"natural_it": 12.0, "in_process_it": 0.0, "kind": "forming"},
    "fdm":       {"natural_it": 13.0, "in_process_it": 0.0, "kind": "additive"},
}

#: Machining operations in the order a process planner would reach for them —
#: coarsest (cheapest) first. :func:`cheapest_process` walks this, not the whole
#: table, because "cnc"/"injection" are part-level answers, not operations.
_OPERATION_LADDER = ["drilling", "milling", "turning", "boring", "reaming",
                     "grinding", "honing", "lapping"]

# The handbook shape: below its natural capability a process's cost roughly DOUBLES
# every 1-2 IT grades tightened. 1.5 is the midpoint of that range and is the single
# number the whole curve rests on — it makes the IT6-vs-IT9 ratio for a machined
# bore 2^(3/1.5) = 4.0x, the centre of the 2-6x the handbooks quote.
GRADES_PER_DOUBLING = 1.5

# ABOVE natural capability the process is already at its floor: the cut does not get
# any faster because the print says ±0.5 instead of ±0.2. What still falls is
# inspection and scrap — coarser gauges, sampling instead of 100 %, fewer rejects —
# so the curve keeps sloping but far more gently. 6 grades per halving (~12 % per
# grade) is a convention, not a measurement; it exists so the index stays STRICTLY
# monotone across the whole axis instead of flat-lining, which is what lets the
# loosen loop tell two already-cheap schemes apart.
FREE_GRADES_PER_HALVING = 6.0

# The declared band. The curve's only real parameter is GRADES_PER_DOUBLING, and
# the handbook range behind it is 1-2 grades. At 3 grades of tightening that spans
# 2^(3/2)=2.8x to 2^(3/1)=8x against our central 4x — call it -50 %/+100 %, and
# report the symmetric-worst 50 %. Read the index as a ratio; it is not dollars.
BAND_PCT = 50.0


def processes() -> dict:
    """The corpus, as ``{name: {natural_it, in_process_it, kind}}`` — copied, so a
    caller poking at the result cannot corrupt the table."""
    return {k: dict(v) for k, v in _PROCESSES.items()}


def _proc(process: str) -> dict:
    key = str(process or "").strip().lower()
    if key not in _PROCESSES:
        raise ValueError(
            f"unknown process {process!r}; choose from {sorted(_PROCESSES)}")
    return _PROCESSES[key]


def relative_cost(grade: float, process: str = "cnc") -> float:
    """Relative cost index of holding IT ``grade`` with ``process``.

    Normalised so that the process's natural capability costs **1.0**. Tighter than
    natural follows the handbook doubling curve
    (``2 ** (delta / GRADES_PER_DOUBLING)``); looser follows the shallow
    inspection/scrap curve (``2 ** (delta / FREE_GRADES_PER_HALVING)``). Strictly
    increasing as the grade tightens, with a documented kink at the knee.

    Dimensionless: only ratios of this number mean anything."""
    natural = _proc(process)["natural_it"]
    delta = natural - float(grade)          # >0 == tighter than the process likes
    rate = GRADES_PER_DOUBLING if delta > 0 else FREE_GRADES_PER_HALVING
    return round(2.0 ** (delta / rate), 6)


def cheapest_process(grade: float) -> dict:
    """The cheapest single machining operation that holds IT ``grade`` naturally.

    Walks :data:`_OPERATION_LADDER` coarse-to-fine and returns the FIRST operation
    whose natural capability is at least as tight as the requested grade — i.e. the
    one a process planner would reach for. Returns
    ``{operation, natural_it, note}``; ``operation`` is None when the grade is
    coarser than any tabulated operation (nothing to choose — the coarsest cut
    already holds it)."""
    g = float(grade)
    for name in _OPERATION_LADDER:
        if _PROCESSES[name]["natural_it"] <= g:
            return {"operation": name,
                    "natural_it": _PROCESSES[name]["natural_it"],
                    "note": f"{name} holds IT{_PROCESSES[name]['natural_it']:g} "
                            "naturally"}
    finest = _OPERATION_LADDER[-1]
    return {"operation": finest, "natural_it": _PROCESSES[finest]["natural_it"],
            "note": f"IT{g:.1f} is tighter than {finest} holds naturally — expect "
                    "a hand-fit / selective-assembly step, not a process"}


# --- the per-dimension check --------------------------------------------------

# Verdicts, coarse to severe. `ok` and `in_process` do not fail the check; the
# other two do.
OK = "ok"                                  # at or looser than natural capability
IN_PROCESS = "in_process_tightening"       # tighter, but reachable in the same op
SECONDARY = "needs_secondary_operation"    # tighter than the op can reach at all
UNTOLERANCED = "no_tolerance"              # nothing to price


def _link_row(dim: dict, process: str, index: int) -> dict:
    """Price one toleranced dimension. See :func:`tolerance_cost_check`."""
    nominal, plus, minus, _direction = tolerance._devs(dim)
    name = str(dim.get("name") or f"link{index + 1}")
    band = plus - minus
    spec = _proc(process)
    natural = spec["natural_it"]
    reachable = natural - spec["in_process_it"]

    if band <= 0:
        # A basic/zero-band dimension carries no IT grade at all — it is held by a
        # geometric control, priced there. Report it rather than charging for it.
        return {"name": name, "nominal_mm": round(nominal, 6), "band_mm": 0.0,
                "it_grade": None, "cost_index": None, "verdict": UNTOLERANCED,
                "cheapest_operation": None, "natural_it": natural,
                "note": "zero band (basic dimension) — no IT grade; its cost sits "
                        "with the geometric tolerance that controls it"}

    grade = it_grade(nominal, band)
    cost = relative_cost(grade, process)
    route = cheapest_process(grade)

    if grade >= natural:
        verdict = OK
        note = (f"IT{grade:.1f} is at or looser than {process}'s natural "
                f"IT{natural:g} — no tolerance premium")
    elif grade >= reachable:
        verdict = IN_PROCESS
        note = (f"IT{grade:.1f} is {natural - grade:.1f} grade(s) tighter than "
                f"{process}'s natural IT{natural:g}; reachable in-process (slower "
                f"finish pass, tighter offsets) at ~{cost:.2f}x")
    else:
        verdict = SECONDARY
        note = (f"IT{grade:.1f} is {natural - grade:.1f} grade(s) tighter than "
                f"{process}'s natural IT{natural:g} and beyond the "
                f"{spec['in_process_it']:g}-grade in-process reach — needs a "
                f"secondary operation ({route['operation']}) at ~{cost:.2f}x")

    return {"name": name, "nominal_mm": round(nominal, 6),
            "band_mm": round(band, 6), "it_grade": grade,
            "cost_index": cost, "verdict": verdict,
            "cheapest_operation": route["operation"], "natural_it": natural,
            "note": note}


def tolerance_cost_check(chain: list, process: str = "cnc") -> dict:
    """Price a tolerance scheme: per dimension, and as one comparable total.

    ``chain`` is a list of signed-deviation dimensions in
    :mod:`driftpin.analysis.tolerance`'s convention — ``{name, nominal, plus,
    minus}`` or the symmetric shorthand ``{name, nominal, tol}``. It is exactly the
    chain ``tolerance_stackup`` takes, so a stackup and its price read the same
    input. ``process`` is the part's declared manufacturing process (see
    :func:`processes`).

    Each link reports its IT grade, the cheapest machining operation that holds
    that grade naturally, its relative cost index, and a verdict:

    * ``ok`` — at or looser than the process's natural capability;
    * ``in_process_tightening`` — tighter, but reachable in the same operation;
    * ``needs_secondary_operation`` — tighter than the declared process can hold at
      all, so the part silently acquires an operation nobody costed. **This is the
      finding the whole tool exists for**, and it is what ``pass`` keys on;
    * ``no_tolerance`` — a zero-band (basic) dimension, priced by its geometric
      control instead.

    ``total_cost_index`` is the SUM of the link indices — dimensionless, and the
    number to compare two tolerance schemes over the same chain with. A chain whose
    every link sits at natural capability totals exactly ``len(chain)``.

    Fidelity: ``correlation`` at ``band_pct`` 50 (see :data:`BAND_PCT`). The
    ratios are defensible; the absolute index is not a cost and not a currency.

    Returns ``{process, links, n_links, total_cost_index, mean_cost_index,
    flagged, pass, fidelity, band_pct, basis, escalate_to}``. Raises ValueError on
    an empty chain, an unknown process, or a malformed dimension."""
    if not chain:
        raise ValueError("chain must contain at least one dimension")
    _proc(process)                       # validate before doing any work

    links = [_link_row(d, process, i) for i, d in enumerate(chain)]
    priced = [r for r in links if r["cost_index"] is not None]
    total = sum(r["cost_index"] for r in priced)
    flagged = [{"name": r["name"], "it_grade": r["it_grade"],
                "verdict": r["verdict"], "note": r["note"]}
               for r in links if r["verdict"] == SECONDARY]

    return {
        "process": str(process).lower(),
        "links": links,
        "n_links": len(links),
        "total_cost_index": round(total, 6),
        "mean_cost_index": round(total / len(priced), 6) if priced else None,
        "flagged": flagged,
        "pass": not flagged,
        "fidelity": "correlation",
        "band_pct": BAND_PCT,
        "basis": (f"relative cost doubles every {GRADES_PER_DOUBLING:g} IT grades "
                  "tightened below the process's natural capability (handbook "
                  "range 1-2 grades); above it only inspection/scrap falls, at "
                  f"one halving per {FREE_GRADES_PER_HALVING:g} grades. Index is "
                  "dimensionless and normalised to 1.0 at natural capability — "
                  "compare schemes, never read it as money"),
        "escalate_to": "suggest_loosening",
    }


# --- the loosen-to-save loop --------------------------------------------------

def _loosened(dim: dict, grades: float) -> dict:
    """A copy of ``dim`` opened by ``grades`` IT grades, keeping its MEAN.

    Keeping the mean (rather than the nominal) matters: loosening a link must not
    shift the stack it sits in, or the cpk the loop is protecting would move for a
    reason that has nothing to do with the tolerance."""
    nominal, plus, minus, direction = tolerance._devs(dim)
    mean_dev = 0.5 * (plus + minus)
    band = plus - minus
    new_band = band_for_grade(nominal, it_grade(nominal, band) + grades)
    out = dict(dim)
    out.pop("tol", None)
    out["nominal"] = nominal
    out["plus"] = round(mean_dev + new_band / 2.0, 9)
    out["minus"] = round(mean_dev - new_band / 2.0, 9)
    if direction != 1:
        out["direction"] = direction
    return out


def _cpk(chain, spec_min, spec_max, samples, seed):
    """Cpk of a chain against the spec, via the Monte-Carlo block of
    :func:`tolerance.stackup`. Seeded, so the loop is reproducible."""
    res = tolerance.stackup(chain, method="montecarlo", samples=samples,
                            spec_min=spec_min, spec_max=spec_max, seed=seed)
    cpk = res["montecarlo"]["cpk"]
    # stackup returns None for an infinite cpk (a chain with no scatter at all).
    return float("inf") if cpk is None else float(cpk)


def suggest_loosening(
    chain: list,
    spec_min: float | None = None,
    spec_max: float | None = None,
    process: str = "cnc",
    target_cpk: float = 1.33,
    samples: int = 4000,
    seed: int = 12345,
    max_steps: int = 12,
    step_grades: float = 1.0,
    coarsest_it: float = 13.0,
) -> dict:
    """Which links can give up tolerance for the biggest saving while the stack
    still passes — the tool-supported version of "the loosest tolerance that works".

    Greedy, one IT grade at a time. At each step every link is trial-loosened by
    ``step_grades``; candidates whose resulting chain still meets ``target_cpk``
    against ``[spec_min, spec_max]`` are ranked by cost saving, and the best is
    committed. The loop stops when no single move survives the cpk gate, when a
    link would pass ``coarsest_it``, or after ``max_steps``.

    Greedy is the right shape here and not a compromise: the cost curve is convex
    in the grade (cost falls fastest where the tolerance is tightest) while the
    stack variance is a sum of independent per-link terms, so the largest-saving
    move is also the one that spends the least of the stack's margin. It will not
    always find the global optimum over many links, but every step it reports is
    individually verified against the real stackup, so nothing it suggests can
    fail the spec.

    Cpk comes from :func:`tolerance.stackup`'s seeded Monte-Carlo block, so the
    result is reproducible run to run. If the chain does not already meet
    ``target_cpk``, the loop refuses: there is no margin to give away, and the
    answer is to tighten or re-spec, not to loosen. It returns ``ok=False`` with
    that reason rather than a scheme.

    Returns ``{ok, steps, stopped, chain, cost_index_before, cost_index_after,
    saving, saving_pct, cpk_before, cpk_after, target_cpk, spec, note, fidelity,
    band_pct, basis}``. ``stopped`` is ``no_further_move`` (the loop converged),
    ``max_steps`` (the cap bit — re-run on the returned chain to keep going), or
    ``no_margin``. ``steps == []`` with ``saving == 0`` is the honest "already as
    loose as it goes" answer — the loop never invents a saving. Raises ValueError
    on an empty chain or an unknown process."""
    if not chain:
        raise ValueError("chain must contain at least one dimension")
    _proc(process)

    current = [dict(d) for d in chain]
    before = tolerance_cost_check(current, process)["total_cost_index"]
    cpk_before = _cpk(current, spec_min, spec_max, samples, seed)
    spec = {"min": spec_min, "max": spec_max,
            "source": "explicit" if (spec_min is not None or spec_max is not None)
            else "worst-case bounds of the chain as given"}

    if cpk_before < target_cpk:
        return {
            "ok": False, "steps": [], "stopped": "no_margin", "chain": current,
            "cost_index_before": before, "cost_index_after": before,
            "saving": 0.0, "saving_pct": 0.0,
            "cpk_before": round(cpk_before, 4), "cpk_after": round(cpk_before, 4),
            "target_cpk": target_cpk, "spec": spec,
            "note": (f"the chain as given reaches cpk {cpk_before:.3f}, below the "
                     f"{target_cpk:g} target — there is no margin to give away; "
                     "tighten a link or widen the spec, do not loosen"),
            "fidelity": "correlation", "band_pct": BAND_PCT,
            "basis": "greedy one-grade loosening, each step verified against "
                     "tolerance.stackup's seeded Monte-Carlo cpk",
        }

    steps: list = []
    cpk_now = cpk_before
    stopped = "max_steps"
    for _ in range(int(max_steps)):
        best = None
        for i, dim in enumerate(current):
            nominal, plus, minus, _d = tolerance._devs(dim)
            band = plus - minus
            if band <= 0:
                continue                          # a basic dimension has nothing to give
            g0 = it_grade(nominal, band)
            g1 = g0 + step_grades
            if g1 > coarsest_it:
                continue                          # already at the coarse end
            trial = list(current)
            trial[i] = _loosened(dim, step_grades)
            cpk_trial = _cpk(trial, spec_min, spec_max, samples, seed)
            if cpk_trial < target_cpk:
                continue
            saving = relative_cost(g0, process) - relative_cost(g1, process)
            if saving <= 0:
                continue
            cand = (saving, -i, i, trial, g0, g1, cpk_trial, dim)
            # Ties break on the LOWEST link index so the walk is order-stable and
            # bit-reproducible; the -i in the sort key does that under max().
            if best is None or cand[:2] > best[:2]:
                best = cand
        if best is None:
            stopped = "no_further_move"
            break
        saving, _neg, i, trial, g0, g1, cpk_trial, dim = best
        steps.append({
            "link": str(dim.get("name") or f"link{i + 1}"),
            "index": i,
            "from_it": g0, "to_it": round(g1, 4),
            "from_band_mm": round(tolerance._devs(dim)[1] - tolerance._devs(dim)[2], 6),
            "to_band_mm": round(trial[i]["plus"] - trial[i]["minus"], 6),
            "cost_before": relative_cost(g0, process),
            "cost_after": relative_cost(g1, process),
            "saving": round(saving, 6),
            "cpk": round(cpk_trial, 4),
        })
        current = trial
        cpk_now = cpk_trial

    after = tolerance_cost_check(current, process)["total_cost_index"]
    saving = round(before - after, 6)
    if steps:
        touched = len({s["index"] for s in steps})
        more = (" — stopped at max_steps, re-run to keep going"
                if stopped == "max_steps" else "")
        note = (f"{len(steps)} step(s) across {touched} link(s); total cost index "
                f"{before:.3f} -> {after:.3f} ({100.0 * saving / before:.1f}% "
                f"cheaper) with cpk still {cpk_now:.2f} >= {target_cpk:g}{more}")
    else:
        note = (f"no link can be opened a further {step_grades:g} grade(s) without "
                f"dropping cpk below {target_cpk:g} — this scheme is already as "
                "loose as the spec allows")
    return {
        "ok": True,
        "steps": steps,
        "stopped": stopped,
        "chain": current,
        "cost_index_before": before,
        "cost_index_after": after,
        "saving": saving,
        "saving_pct": round(100.0 * saving / before, 4) if before else 0.0,
        "cpk_before": round(cpk_before, 4),
        "cpk_after": round(cpk_now, 4),
        "target_cpk": target_cpk,
        "spec": spec,
        "note": note,
        "fidelity": "correlation",
        "band_pct": BAND_PCT,
        "basis": ("greedy one-grade loosening ranked by the tolerance-cost curve; "
                  "every committed step re-verified against tolerance.stackup's "
                  "seeded Monte-Carlo cpk, so no suggestion can fail the spec"),
    }


# --- the cost_estimate hook ---------------------------------------------------

def parse_tolerance_class(tolerance_class) -> float:
    """Normalise a tolerance class to a float IT grade.

    Accepts ``"IT7"`` / ``"it7"`` / ``"7"`` / ``7`` / ``7.5``. Raises ValueError on
    anything else, and on a grade outside [:data:`FINEST_IT`,
    :data:`COARSEST_IT`] — there is no silent default."""
    raw = tolerance_class
    if isinstance(raw, str):
        raw = raw.strip().lower()
        if raw.startswith("it"):
            raw = raw[2:]
    try:
        grade = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"tolerance_class {tolerance_class!r} is not an IT grade; pass e.g. "
            "'IT7' or 7")
    if not (FINEST_IT <= grade <= COARSEST_IT):
        raise ValueError(
            f"IT grade {grade:g} outside the supported IT{FINEST_IT:g}-"
            f"IT{COARSEST_IT:g} range")
    return grade


def time_factor(tolerance_class, process: str = "cnc") -> dict:
    """Machine-time multiplier for holding ``tolerance_class`` with ``process``.

    The same :func:`relative_cost` curve, exposed for the time models: this is what
    ``cost_estimate`` and ``machining.machining_time`` multiply their cutting time
    by, so a tolerance costs the same thing in both places (one corpus, two
    consumers). ``tolerance_class=None`` returns a factor of exactly 1.0 with
    ``basis="none"``, which is how both callers keep their pre-#235 behaviour
    byte-for-byte.

    Returns ``{factor, it_grade, natural_it, process, basis}``."""
    spec = _proc(process)
    if tolerance_class is None:
        return {"factor": 1.0, "it_grade": None,
                "natural_it": spec["natural_it"], "process": str(process).lower(),
                "basis": "none — no tolerance class declared"}
    grade = parse_tolerance_class(tolerance_class)
    factor = relative_cost(grade, process)
    return {"factor": factor, "it_grade": grade, "natural_it": spec["natural_it"],
            "process": str(process).lower(),
            "basis": (f"IT{grade:g} vs {process} natural IT{spec['natural_it']:g}; "
                      f"{GRADES_PER_DOUBLING:g} grades per doubling below the knee")}
