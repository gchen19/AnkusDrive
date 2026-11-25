#!/usr/bin/env python3
"""Summarise and gate the scheduled paid eval run (#517).

``.github/workflows/paid-evals.yml`` runs the paid reliability layers (A-D), the
tool-selection eval and a fixed M2 panel, and copies each harness's report into
one directory under a fixed name (``SUITES`` below). This script reads them,
writes the pass rates and cost to the job summary, and decides the run's colour.

It fails (exit 1) for two reasons only:

* a suite that was expected to run left no report, so its harness crashed before
  grading anything; and
* a suite's pass rate fell below its committed baseline by more than the allowed
  margin.

A harness's own accuracy bar (Layer A's 70% and so on) does not fail the run:
the workflow ignores harness exit codes, so a suite that is merely below its
historical bar is a number to read, and only a *drop* against the baseline is
red.

The allowed drop for a suite is ``max(margin, 2 * SE)``, where SE is the binomial
standard error of the baseline rate at the baseline's sample size. Ten shapes or
seven diff pairs move 10-14 points per item, so a fixed margin alone would flag
ordinary sampling noise there. The default margin and any per-suite override live
in ``tests/paid_eval_baseline.json``.

A suite with no baseline for the model that ran is reported and passes. The
summary ends with a baseline block, built from this run, that can be pasted into
that file once the numbers look right.

Stdlib plus ``orchestration/claude_models.py`` (itself dependency-free), so it
runs on a bare runner with no FreeCAD and no ``anthropic``.

Usage:
    python3 scripts/paid_eval_report.py REPORT_DIR \\
        [--baseline tests/paid_eval_baseline.json] [--expect layer_a,m2,...] \\
        [--summary "$GITHUB_STEP_SUMMARY"] [--out combined.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestration.claude_models import PRICING, cost_of  # noqa: E402

# suite key -> (file the workflow copies the harness report to, display name)
SUITES = {
    "layer_a": ("layer_a.json", "Layer A: shape classification"),
    "layer_b": ("layer_b.json", "Layer B: diff detection"),
    "layer_c": ("layer_c.json", "Layer C: spec match/violate"),
    "layer_d": ("layer_d.json", "Layer D: agent drives the tools"),
    "tool_selection": ("tool_selection.json", "Tool selection"),
    "m2": ("m2.json", "M2: multi-agent partition vs single"),
}
DEFAULT_MARGIN = 0.15


# --- per-suite extraction -----------------------------------------------------

def _judge_cost(report: dict) -> float:
    """What Layer A's judge calls billed this run (cached verdicts cost nothing)."""
    judge = report.get("judge_model")
    if not judge or judge not in PRICING:
        return 0.0
    total = 0.0
    for r in report.get("results", []):
        v = r.get("judge") or {}
        if v.get("usage") and not v.get("cached"):
            total += cost_of(v["usage"], judge)
    return total


def _simple(report: dict) -> dict:
    """Layers A/B/C: ``correct`` of ``total``, one model."""
    return {"model": report.get("model"), "passed": report.get("correct", 0),
            "n": report.get("total", 0), "cost_usd": report.get("cost_usd")}


def extract(suite: str, report: dict) -> list[dict]:
    """One metric row per (suite, model): model, passed, n, rate, cost_usd, extra."""
    if suite in ("layer_a", "layer_b", "layer_c"):
        row = _simple(report)
        if suite == "layer_a":
            jc = _judge_cost(report)
            if jc:
                row["cost_usd"] = (row["cost_usd"] or 0.0) + jc
            row["extra"] = f"grader={report.get('grader', 'keyword')}"
            if report.get("grader") in ("judge", "both"):
                kw = sum(1 for r in report.get("results", []) if r.get("grade", {}).get("pass"))
                row["extra"] += f", keyword {kw}/{row['n']}, judge {report.get('judge_model')}"
        if suite == "layer_c":
            row["extra"] = (f"false pass {report.get('false_positives', 0)}, "
                            f"false reject {report.get('false_negatives', 0)}")
        rows = [row]
    elif suite == "layer_d":
        rows = []
        for model in report.get("models", []):
            cells = [c for k, c in report.get("cells", {}).items() if k.startswith(model + "/")]
            rows.append({"model": model, "passed": sum(c["pass"] for c in cells),
                         "n": sum(c["n"] for c in cells),
                         "cost_usd": sum(c.get("avg_cost", 0.0) * c["n"] for c in cells),
                         "extra": f"{report.get('trials')} trials x {len(cells)} tasks"})
    elif suite == "tool_selection":
        oc = report.get("outcomes", {})
        rows = [{"model": report.get("model"), "passed": oc.get("pass", 0),
                 "n": report.get("n", 0), "cost_usd": report.get("billed_this_run_usd"),
                 "extra": f"toolsets={report.get('toolsets')}, "
                          f"saved ${report.get('cache_savings_usd', 0):.2f} by caching"}]
    elif suite == "m2":
        # Negative-control rows ("neg/...") measure the gate, not the agents.
        agent_rows = [r for r in report.get("rows", []) if "passed" in r]
        rows = [{"model": report.get("model"),
                 "passed": sum(r["passed"] for r in agent_rows),
                 "n": sum(r["trials"] for r in agent_rows),
                 "cost_usd": report.get("total_cost_usd"),
                 "panel": m2_panel(report),
                 "extra": _m2_conditions(agent_rows)}]
    else:
        raise KeyError(suite)
    for r in rows:
        r["suite"] = suite
        r["rate"] = r["passed"] / r["n"] if r["n"] else 0.0
    return rows


def m2_panel(report: dict) -> str:
    """Which toys, conditions and trial count the pooled M2 rate was taken over.

    A baseline only compares against a run of the same panel: pooling a harder
    toy into the rate would move it with no regression behind the move.
    """
    keys = sorted({f"{r['toy']}/{r['condition']}" for r in report.get("rows", []) if "passed" in r})
    return f"trials={report.get('trials')};" + ",".join(keys)


def _m2_conditions(rows: list[dict]) -> str:
    by = {}
    for r in rows:
        cond = r["condition"].split("_")[0]
        p, n = by.get(cond, (0, 0))
        by[cond] = (p + r["passed"], n + r["trials"])
    return ", ".join(f"{c} {p}/{n}" for c, (p, n) in sorted(by.items()))


# --- baseline comparison ------------------------------------------------------

def allowed_drop(base: dict, margin: float) -> float:
    p, n = base["rate"], base.get("n") or 0
    se = math.sqrt(p * (1 - p) / n) if n else 0.0
    return max(margin, 2 * se)


def compare(row: dict, baseline: dict) -> dict:
    """Attach ``base`` / ``allowed`` / ``status`` to a metric row.

    status: ``ok``, ``regressed``, ``no_baseline`` or ``panel_changed``.
    """
    margin = float(baseline.get("margin", DEFAULT_MARGIN))
    base = (baseline.get("suites", {}).get(row["suite"]) or {}).get(row["model"])
    if not base:
        return {**row, "status": "no_baseline"}
    if row["suite"] == "m2" and base.get("panel") != row.get("panel"):
        return {**row, "base": base, "status": "panel_changed"}
    allowed = allowed_drop(base, float(base.get("margin", margin)))
    drop = base["rate"] - row["rate"]
    status = "regressed" if drop > allowed + 1e-9 else "ok"
    return {**row, "base": base, "allowed": allowed, "status": status}


def baseline_candidate(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        entry = {"rate": round(r["rate"], 4), "n": r["n"]}
        if r.get("panel"):
            entry["panel"] = r["panel"]
        out.setdefault(r["suite"], {})[r["model"]] = entry
    return out


# --- rendering ----------------------------------------------------------------

def _money(x) -> str:
    if x is None:
        return "n/a"
    return f"${x:.2f}" if x >= 0.01 or x == 0 else f"${x:.4f}"


def render(rows: list[dict], missing: list[str], m2_report: dict | None) -> str:
    lines = ["## Paid evals (#517)", ""]
    regressed = [r for r in rows if r["status"] == "regressed"]
    if missing or regressed:
        lines.append("**Failed:** " + "; ".join(
            [f"{SUITES[s][1]} left no report" for s in missing]
            + [f"{SUITES[r['suite']][1]} fell from {r['base']['rate']:.0%} to {r['rate']:.0%} "
               f"(allowed drop {r['allowed']:.0%})" for r in regressed]))
    else:
        lines.append("No suite fell past its margin.")
    lines += ["", "| Suite | Model | Pass | Rate | Baseline | Cost | Notes |",
              "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["status"] == "no_baseline":
            base = "none yet"
        elif r["status"] == "panel_changed":
            base = "other panel"
        else:
            mark = "REGRESSED" if r["status"] == "regressed" else "ok"
            base = f"{r['base']['rate']:.0%} ({mark}, allowed -{r['allowed']:.0%})"
        lines.append(f"| {SUITES[r['suite']][1]} | `{r['model']}` | {r['passed']}/{r['n']} | "
                     f"{r['rate']:.0%} | {base} | {_money(r.get('cost_usd'))} | {r.get('extra', '')} |")
    for s in missing:
        lines.append(f"| {SUITES[s][1]} | | | | | | no report: harness crashed |")
    total = sum(r["cost_usd"] or 0.0 for r in rows)
    lines += ["", f"**Billed this run: {_money(total)}** at list prices "
              "(`orchestration/claude_models.PRICING`)."]
    if m2_report and m2_report.get("rows"):
        lines += ["", "<details><summary>M2 by toy (read only, the gate uses the pooled rate)</summary>",
                  "", "| Toy | Condition | Pass | Built | Cost/trial |", "|---|---|---|---|---|"]
        for r in m2_report["rows"]:
            if "passed" not in r:
                continue
            lines.append(f"| {r['toy']} | {r['condition']} | {r['passed']}/{r['trials']} | "
                         f"{r['built']}/{r['trials']} | ${r['mean_cost_usd']:.4f} |")
        lines += ["", "</details>"]
    lines += ["", "<details><summary>Baseline from this run</summary>", "",
              "Paste into `suites` in `tests/paid_eval_baseline.json` to compare later runs "
              "against this one.", "", "```json",
              json.dumps(baseline_candidate(rows), indent=2), "```", "", "</details>", ""]
    return "\n".join(lines)


# --- main ---------------------------------------------------------------------

def run(report_dir: Path, baseline: dict, expect: list[str]) -> tuple[list[dict], list[str], dict | None]:
    rows, missing, m2 = [], [], None
    for suite in expect:
        path = report_dir / SUITES[suite][0]
        if not path.exists():
            missing.append(suite)
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        if suite == "m2":
            m2 = report
        rows += [compare(r, baseline) for r in extract(suite, report)]
    return rows, missing, m2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("report_dir", type=Path)
    ap.add_argument("--baseline", type=Path, default=REPO / "tests" / "paid_eval_baseline.json")
    ap.add_argument("--expect", default=",".join(SUITES),
                    help="comma list of suites that must have reported")
    ap.add_argument("--summary", type=Path, help="append the markdown here (GITHUB_STEP_SUMMARY)")
    ap.add_argument("--out", type=Path, help="write rows + baseline candidate as JSON")
    a = ap.parse_args(argv)

    expect = [s.strip() for s in a.expect.split(",") if s.strip()]
    unknown = [s for s in expect if s not in SUITES]
    if unknown:
        ap.error(f"unknown suite(s) {unknown}; known: {sorted(SUITES)}")
    baseline = json.loads(a.baseline.read_text(encoding="utf-8")) if a.baseline.exists() else {}
    rows, missing, m2 = run(a.report_dir, baseline, expect)
    md = render(rows, missing, m2)
    print(md)
    if a.summary:
        with a.summary.open("a", encoding="utf-8") as f:
            f.write(md)
    if a.out:
        a.out.write_text(json.dumps({"rows": rows, "missing": missing,
                                     "baseline_candidate": baseline_candidate(rows)}, indent=2),
                         encoding="utf-8")
    return 1 if missing or any(r["status"] == "regressed" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
