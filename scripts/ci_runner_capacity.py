#!/usr/bin/env python3
"""Tell a heavy-solves run that never got a runner apart from one that failed (#515).

Three of seven nightly "Heavy solver regressions" runs in November went red with
no solver test failing: the macOS lanes sat queued until GitHub gave up with
"The job was not acquired by Runner of type hosted even after multiple attempts".
A nightly that goes red on runner capacity stops being read as a regression
signal, so .github/workflows/heavy-solves-retry.yml calls this on every failed
run and re-runs it once when capacity is the only thing that went wrong.

A job that never reached a runner has a recognisable shape in the jobs API: it
ended ``cancelled`` (or ``timed_out``) with no ``runner_name`` and no steps,
because nothing ever ran. One that ended ``failure`` with no runner failed
before a runner was requested, like a workflow expression error, and counts as
a failure: a retry would only repeat it. Anything else that did not succeed (a FAILed
test, a timeout, a step error) is a real failure, and a run with one of those is
never re-run: retrying would only spend hours of macOS minutes to repeat it, and
a green retry could hide a flaky regression.

Classification of each job:
  ok          success, skipped or neutral
  no-runner   ended without ever being acquired by a runner (infrastructure)
  failed      everything else (the lane ran and did not pass)

Verdict for the run:
  green       nothing failed
  no-runner   at least one job never got a runner, and no job failed
  failed      at least one job failed

Usage:
  GH_TOKEN=... python3 scripts/ci_runner_capacity.py --repo OWNER/NAME \\
      --run-id RUN_ID --attempt N [--max-attempts 2] [--summary FILE] [--output FILE]
  python3 scripts/ci_runner_capacity.py --jobs-json jobs.json   # offline, for tests

``--output`` (GitHub's $GITHUB_OUTPUT) receives ``verdict=`` and ``rerun=true|false``;
``--summary`` ($GITHUB_STEP_SUMMARY) receives a markdown table, one row per lane.
``rerun`` is true only for a ``no-runner`` verdict on an attempt below
``--max-attempts``, so the retry is bounded however long capacity stays short.

Exit status: 0 whatever the verdict (the caller acts on ``rerun``); 2 on usage
or fetch errors.
"""

import argparse
import json
import subprocess
import sys

OK = "ok"
NO_RUNNER = "no-runner"
FAILED = "failed"

_OK_CONCLUSIONS = {"success", "skipped", "neutral"}
# How a job that waited in the queue and never got a runner ends. A job that ends
# "failure" with no runner failed before one was ever requested (a broken `if:`,
# `runs-on:` or `uses:` after an edit to heavy-solves.yml): retrying repeats it.
_QUEUE_CONCLUSIONS = {"cancelled", "timed_out"}

_LABEL = {
    OK: "passed",
    NO_RUNNER: "never got a runner (infrastructure, not a test)",
    FAILED: "failed",
}


def classify_job(job):
    """``ok``, ``no-runner`` or ``failed`` for one entry of the jobs API."""
    conclusion = job.get("conclusion")
    if conclusion in _OK_CONCLUSIONS:
        return OK
    if conclusion in _QUEUE_CONCLUSIONS and not job.get("runner_name") and not job.get("steps"):
        return NO_RUNNER
    return FAILED


def verdict(jobs):
    """The run's verdict and the per-job classes, in the API's job order."""
    classes = [(job.get("name", "?"), classify_job(job), job.get("conclusion")) for job in jobs]
    kinds = {kind for _, kind, _ in classes}
    if FAILED in kinds:
        return FAILED, classes
    if NO_RUNNER in kinds:
        return NO_RUNNER, classes
    return "green", classes


def should_rerun(run_verdict, attempt, max_attempts):
    return run_verdict == NO_RUNNER and attempt < max_attempts


def summary_markdown(run_verdict, classes, attempt, rerun, run_url=None):
    head = {
        "green": "Every lane passed.",
        NO_RUNNER: "No lane failed a test. The red is runner capacity: "
                   "the lanes below never got a runner.",
        FAILED: "A lane failed. This is a regression signal, so it is not re-run.",
    }[run_verdict]
    where = f" ([run]({run_url}))" if run_url else ""
    lines = [
        f"### Heavy solver regressions, attempt {attempt}{where}",
        "",
        head,
        "",
        "| Lane | Result |",
        "| --- | --- |",
    ]
    for name, kind, conclusion in classes:
        label = _LABEL[kind]
        if kind == FAILED and conclusion:
            label = f"{label} ({conclusion})"
        lines.append(f"| {name} | {label} |")
    lines.append("")
    if rerun:
        lines.append(f"Re-running the jobs that never got a runner (attempt {attempt + 1}).")
    elif run_verdict == NO_RUNNER:
        lines.append(f"Not re-run again: attempt {attempt} is the last automatic retry.")
    return "\n".join(lines) + "\n"


def fetch_jobs(repo, run_id, attempt):
    """Every job of one run attempt, via ``gh api`` (paginated)."""
    cmd = ["gh", "api", "--paginate",
           f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
           "--jq", ".jobs[]"]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(f"ci_runner_capacity: could not list jobs: {detail.strip()}", file=sys.stderr)
        raise SystemExit(2) from None
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--run-id", help="workflow run id (fetched with gh api)")
    src.add_argument("--jobs-json", help="a file holding a jobs API response, or a list of jobs")
    ap.add_argument("--repo", help="OWNER/NAME, required with --run-id")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--max-attempts", type=int, default=2)
    ap.add_argument("--run-url")
    ap.add_argument("--summary", help="append the markdown table here ($GITHUB_STEP_SUMMARY)")
    ap.add_argument("--output", help="append verdict= and rerun= here ($GITHUB_OUTPUT)")
    args = ap.parse_args(argv)

    if args.run_id:
        if not args.repo:
            ap.error("--repo is required with --run-id")
        jobs = fetch_jobs(args.repo, args.run_id, args.attempt)
    else:
        with open(args.jobs_json, encoding="utf-8") as fh:
            data = json.load(fh)
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not jobs:
        print("ci_runner_capacity: the run has no jobs to classify", file=sys.stderr)
        return 2

    run_verdict, classes = verdict(jobs)
    rerun = should_rerun(run_verdict, args.attempt, args.max_attempts)
    md = summary_markdown(run_verdict, classes, args.attempt, rerun, args.run_url)
    print(md, end="")
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    if args.output:
        with open(args.output, "a", encoding="utf-8") as fh:
            fh.write(f"verdict={run_verdict}\nrerun={'true' if rerun else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
