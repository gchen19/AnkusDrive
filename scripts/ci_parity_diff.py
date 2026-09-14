#!/usr/bin/env python3
"""Diff the test coverage of two heavy-solves CI jobs (#342).

A green job proves nothing about *what* ran: the self-hosted Linux nightly was
green for months while silently SKIPping live Bempp, openEMS and CoolProp. The
parity window before the self-hosted runners are decommissioned (#343) therefore
compares what each side actually exercised, suite by suite, instead of eyeballing
two 40-minute logs.

Each side is one or more job logs (a GitHub Actions job ID, fetched with ``gh``,
or a local log file). Several jobs on one side are unioned, e.g. the self-hosted
Mac job against hosted lanes C + D together.

The logs are ``tests/run_all.sh``-shaped. A ``== title ==`` line opens a suite,
and inside it every ``PASS ...`` / ``FAIL ...`` line and every line mentioning
``SKIP`` is an outcome. Outcome lines are normalized (timestamps, ANSI, timings,
absolute and masked ``***`` paths) and compared as multisets per suite.

Reported, as markdown ready to paste into the issue:
  * totals per side;
  * **coverage gaps**: outcomes that PASS on the baseline side (A) but do not PASS
    on the candidate side (B), including whole suites B never ran;
  * SKIP lines unique to either side, and anything B covers that A does not;
  * FAIL lines on either side.

``--accept REGEX`` (repeatable) marks a gap as an explicit, recorded decision
(e.g. Elmer on arm64). It is matched against ``<suite> :: <line>``.

Exit status: 0 when B covers A (every gap accepted, no FAIL on B); 1 otherwise;
2 on usage or fetch errors, or a log with no PASS lines.

Usage:
  GH_TOKEN=$(gh auth token -u gchen19) \\
    python3 scripts/ci_parity_diff.py --a <SELF_HOSTED_JOB> --b <HOSTED_JOB> [--b <JOB> ...]
  # job IDs:  gh run view <RUN_ID> -R gchen19/AnkusDrive --json jobs \\
  #             --jq '.jobs[]|"\\(.databaseId)\\t\\(.name)"'
"""

import argparse
import collections
import datetime
import os
import re
import subprocess
import sys

REPO = "gchen19/AnkusDrive"

_TS = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TIMING = re.compile(r"\(\d+(?:\.\d+)?s\)")
# Absolute POSIX paths and the runner's `***` secret mask; the self-hosted log
# masks $HOME while hosted logs print it, so both collapse to one token.
_PATH = re.compile(r"(?:\*\*\*|(?<![\w.])/(?=[\w.-]))[^\s'\"(),:;]*")
_HEADER = re.compile(r"^== (.*?)( +)==$")
# `== 21/21 passed  ==`, `== ALL DONE ==`, `== 3 run, suite skipped ==` close a
# suite rather than open one; test-printed banners pad before the closing `==`.
_FOOTER = re.compile(r"^(\d+(/\d+)? |all passed|ALL DONE)")
_OUTCOME = re.compile(r"^(?:\[[^\]]*\]\s*)?(PASS|FAIL)\b")
_SKIP = re.compile(r"\bSKIP")


def normalize(line):
    line = _ANSI.sub("", line)
    line = _TIMING.sub("", line)
    line = _PATH.sub("<path>", line)
    return " ".join(line.split())


class Side:
    """Outcome multisets per suite, for one or more job logs."""

    def __init__(self):
        self.outcomes = collections.defaultdict(collections.Counter)  # (suite, kind) -> lines
        self.suites = []
        self.wall = collections.Counter()  # suite -> seconds

    def add_log(self, text):
        suite, opened = None, None
        for raw in text.splitlines():
            m = _TS.match(raw)
            stamp = _parse_ts(m.group(0)) if m else None
            body = _ANSI.sub("", raw[m.end():] if m else raw).strip()
            h = _HEADER.match(body)
            if h and len(h.group(2)) == 1 and not _FOOTER.match(h.group(1)):
                if suite and opened and stamp:
                    self.wall[suite] += (stamp - opened).total_seconds()
                suite, opened = normalize(h.group(1)), stamp
                if suite not in self.suites:
                    self.suites.append(suite)
                continue
            if suite is None or body.startswith("##["):
                continue
            o = _OUTCOME.match(body)
            kind = o.group(1) if o else ("SKIP" if _SKIP.search(body) else None)
            if kind:
                self.outcomes[(suite, kind)][normalize(body)] += 1

    def lines(self, kind):
        return {(s, k): c for (s, k), c in self.outcomes.items() if k == kind}

    def total(self, kind):
        return sum(sum(c.values()) for (s, k), c in self.outcomes.items() if k == kind)


def _parse_ts(s):
    s = s.strip().rstrip("Z")
    head, _, frac = s.partition(".")
    return datetime.datetime.strptime(head, "%Y-%m-%dT%H:%M:%S") + \
        datetime.timedelta(seconds=float("0." + frac) if frac else 0)


def load(ref, repo):
    if os.path.exists(ref):
        with open(ref, encoding="utf-8", errors="replace") as f:
            return f.read()
    if not ref.isdigit():
        raise SystemExit(f"error: {ref!r} is neither a log file nor a job ID")
    r = subprocess.run(["gh", "api", f"repos/{repo}/actions/jobs/{ref}/logs"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise SystemExit(f"error: fetching job {ref}: {r.stderr.strip()}")
    return r.stdout


def diff(a, b, accept=()):
    """Return a report dict; pure, so the test drives it without logs on disk."""
    patterns = [re.compile(p) for p in accept]

    def accepted(suite, line):
        return any(p.search(f"{suite} :: {line}") for p in patterns)

    gaps, ok = [], []
    for (suite, _), passed in sorted(a.lines("PASS").items()):
        if suite not in b.suites:
            # One row for a whole missing suite, not one per test.
            line = f"(whole suite: {sum(passed.values())} PASS)"
            (ok if accepted(suite, line) else gaps).append((suite, line, 1, "suite not run on B"))
            continue
        missing = passed - b.outcomes.get((suite, "PASS"), collections.Counter())
        for line, n in sorted(missing.items()):
            (ok if accepted(suite, line) else gaps).append((suite, line, n, "not PASS on B"))

    def only(kind, x, y):
        out = []
        for (suite, _), lines in sorted(x.lines(kind).items()):
            for line, n in sorted((lines - y.outcomes.get((suite, kind), collections.Counter())).items()):
                out.append((suite, line, n))
        return out

    return {
        "gaps": gaps,
        "accepted": ok,
        "skip_only_a": only("SKIP", a, b),
        "skip_only_b": only("SKIP", b, a),
        "pass_only_b": only("PASS", b, a),
        "fail_a": [(s, l, n) for (s, _), c in sorted(a.lines("FAIL").items()) for l, n in c.items()],
        "fail_b": [(s, l, n) for (s, _), c in sorted(b.lines("FAIL").items()) for l, n in c.items()],
    }


def _md(s):
    return s.replace("|", "\\|")


def render(a, b, rep, label_a, label_b, times=False):
    out = ["| | A: " + _md(label_a) + " | B: " + _md(label_b) + " |", "|---|---|---|"]
    for kind in ("PASS", "FAIL", "SKIP"):
        out.append(f"| {kind} | {a.total(kind)} | {b.total(kind)} |")
    out.append(f"| Suites | {len(a.suites)} | {len(b.suites)} |")
    out.append(f"| Suite wall time | {sum(a.wall.values()) / 60:.0f} min | {sum(b.wall.values()) / 60:.0f} min |")

    def section(title, rows, empty="none"):
        out.extend(["", f"**{title}** ({sum(r[2] for r in rows)})"])
        if not rows:
            out.append(f"- {empty}")
        for r in rows:
            extra = f" — {r[3]}" if len(r) > 3 else ""
            count = f" ×{r[2]}" if r[2] > 1 else ""
            out.append(f"- `{r[0]}` :: {r[1]}{count}{extra}")

    section("Coverage gaps: PASS on A, not on B", rep["gaps"])
    section("Accepted gaps (--accept)", rep["accepted"])
    section("FAIL on B", rep["fail_b"])
    section("FAIL on A", rep["fail_a"])
    section("SKIP only on B", rep["skip_only_b"])
    section("SKIP only on A", rep["skip_only_a"])
    out.extend(["", f"**PASS only on B** ({sum(r[2] for r in rep['pass_only_b'])} lines, "
                    f"{len({r[0] for r in rep['pass_only_b']})} suites)"])
    missing_suites = [s for s in b.suites if s not in a.suites]
    if missing_suites:
        out.append("- suites only on B: " + ", ".join(f"`{s}`" for s in missing_suites))
    if times:
        out.extend(["", "| Suite | A (s) | B (s) |", "|---|---|---|"])
        for s in sorted(set(a.suites) | set(b.suites), key=lambda s: -max(a.wall[s], b.wall[s])):
            out.append(f"| {_md(s)} | {a.wall[s]:.0f} | {b.wall[s]:.0f} |")
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--a", action="append", required=True, metavar="JOB_OR_LOG",
                   help="baseline side (the self-hosted job); repeat to union jobs")
    p.add_argument("--b", action="append", required=True, metavar="JOB_OR_LOG",
                   help="candidate side (the hosted lane(s)); repeat to union jobs")
    p.add_argument("--accept", action="append", default=[], metavar="REGEX",
                   help="a recorded, accepted gap; matched against '<suite> :: <line>'")
    p.add_argument("--repo", default=REPO)
    p.add_argument("--times", action="store_true", help="append a per-suite wall-time table")
    args = p.parse_args(argv)

    a, b = Side(), Side()
    for side, refs in ((a, args.a), (b, args.b)):
        for ref in refs:
            before = side.total("PASS")
            side.add_log(load(ref, args.repo))
            # An empty or unrecognized log would make every comparison vacuous.
            if side.total("PASS") == before:
                print(f"error: no PASS lines parsed from {ref}", file=sys.stderr)
                return 2
    rep = diff(a, b, args.accept)
    print(render(a, b, rep, " + ".join(args.a), " + ".join(args.b), args.times))
    return 1 if rep["gaps"] or rep["fail_b"] else 0


if __name__ == "__main__":
    sys.exit(main())
