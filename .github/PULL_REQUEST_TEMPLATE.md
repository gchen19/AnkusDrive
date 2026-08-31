<!--
Thanks for the PR. CONTRIBUTING.md has the parts that are hard to guess: the
two-interpreter split, which CI lanes a fork PR does and does not get and why,
and the seven places a new tool has to be registered.
-->

## What was wrong

<!-- The failure, not the diff. A reader who knows what was actually broken can
     check whether this fixes it; a list of changed files does not let them. -->

## What this does about it

<!-- And anything you decided against, if the road not taken is interesting. -->

Fixes #

## Checks

- [ ] `ruff check ankusdrive tests` and `python3 tests/test_contracts.py` pass locally (seconds, no FreeCAD)
- [ ] A test covers this — and fails without the change
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]`, or this changes nothing a user would notice

If you added a tool, the seven registration points are listed in
[CONTRIBUTING.md](https://github.com/gchen19/AnkusDrive/blob/main/CONTRIBUTING.md#adding-a-tool). The one people miss
is `tests/run_all.ps1` — add only `run_all.sh` and the Windows lane silently skips
your suite.

## Anything the reviewer should run

<!-- Especially if this needs a lane your PR does not get: the self-hosted suites
     skip fork PRs on purpose, and the heavy solver regressions are not on
     pull_request for anyone. Say so and they can be dispatched. -->
