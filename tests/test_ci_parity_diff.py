"""The CI parity diff (#342) finds what a green job did not test.

`scripts/ci_parity_diff.py` is the gate on decommissioning the self-hosted
runners (#343): it must report a test that PASSes on the baseline job and SKIPs,
or never runs, on the hosted one. A parser that silently matched nothing would
report perfect parity, the exact failure it exists to catch, so the fixtures
here are shaped like real `run_all.sh` job logs and the vacuous case is asserted.

Imports only the script and the stdlib. No FreeCAD, no network.

Run:  python3 tests/test_ci_parity_diff.py
"""

import contextlib
import io
import pathlib
import sys
import tempfile
import time
import traceback

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import ci_parity_diff as pd  # noqa: E402

TS = "2026-09-13T10:50:0{}.1234567Z "


def _log(*lines):
    return "\n".join(TS.format(i % 10) + line for i, line in enumerate(lines))


SELF_HOSTED = _log(
    "##[group]Run bash tests/run_all.sh",
    "== Static contracts (no FreeCAD) ==",
    "  PASS test_registry_parity                        (0.01s)",
    "  PASS test_docstrings                             (1.27s)",
    "",
    "== 2/2 passed  (1.3s) ==",
    "== Exterior acoustics BEM ==",
    "  PASS test_monopole_oracle                        (0.10s)",
    "    SKIP — no bempp venv resolves (scripts/install-solvers.sh acoustics_bem)",
    "== 1/1 passed, 1 skipped   ==",
    "== Photoreal render tests ==",
    "  \x1b[32mPASS\x1b[0m test_blender_async_job                (4.20s)",
    "== MCP boot ==",
    "  SKIP test_mcp_boot — `mcp` not installed in ***/.venv/bin/python3",
    "== Elmer ==",
    "  PASS test_elmer_heat                             (9.00s)",
    "== ALL DONE ==",
)

HOSTED = _log(
    "== Static contracts (no FreeCAD) ==",
    "  PASS test_registry_parity                        (0.02s)",
    "  PASS test_docstrings                             (2.02s)",
    "== Exterior acoustics BEM ==",
    "  PASS test_monopole_oracle                        (0.11s)",
    "  PASS bempp live solve matches Mie                (30.0s)",
    "== Photoreal render tests ==",
    "  SKIP test_blender_async_job Blender not installed",
    "== MCP boot ==",
    "  PASS test_mcp_boot                               (3.00s)",
    "== ALL DONE ==",
)


def _sides(a_text=SELF_HOSTED, b_text=HOSTED):
    a, b = pd.Side(), pd.Side()
    a.add_log(a_text)
    b.add_log(b_text)
    return a, b


def test_suites_open_on_headers_not_footers():
    a, _ = _sides()
    assert a.suites == ["Static contracts (no FreeCAD)", "Exterior acoustics BEM",
                        "Photoreal render tests", "MCP boot", "Elmer"], a.suites
    assert a.total("PASS") == 5 and a.total("SKIP") == 2, (a.total("PASS"), a.total("SKIP"))


def test_wall_time_closes_the_last_suite_on_its_last_outcome():
    s = pd.Side()
    s.add_log("\n".join((
        "2026-09-13T10:00:00.0Z == First ==",
        "2026-09-13T10:00:05.0Z   PASS test_a (5.00s)",
        "2026-09-13T10:00:10.0Z == Last ==",
        "2026-09-13T10:01:10.0Z   PASS test_b (60.0s)",
        "2026-09-13T10:09:00.0Z Post job cleanup.",
    )))
    assert s.wall["First"] == 10 and s.wall["Last"] == 60, dict(s.wall)


def test_timings_ansi_and_masked_paths_normalize_away():
    a, b = _sides()
    rep = pd.diff(a, b)
    gap_lines = {(s, l) for s, l, _, _ in rep["gaps"]}
    # Differing timings on the same test are not a gap.
    assert not any(s.startswith("Static") for s, _ in gap_lines), gap_lines
    assert ("Photoreal render tests", "PASS test_blender_async_job") in gap_lines, gap_lines
    skip_a = [l for _, l, _ in rep["skip_only_a"]]
    assert "SKIP test_mcp_boot — `mcp` not installed in <path>" in skip_a, skip_a


def test_pass_on_a_skip_on_b_is_a_gap_and_whole_missing_suites_collapse():
    a, b = _sides()
    rep = pd.diff(a, b)
    rows = {(s, w) for s, _, _, w in rep["gaps"]}
    assert rows == {("Photoreal render tests", "not PASS on B"),
                    ("Photoreal render tests", "SKIP only on B"),
                    ("Elmer", "suite not run on B")}, rows
    elmer = [r for r in rep["gaps"] if r[0] == "Elmer"]
    assert elmer == [("Elmer", "(whole suite: 1 PASS)", 1, "suite not run on B")], elmer


def test_accept_moves_a_gap_out_of_the_failure_set():
    a, b = _sides()
    rep = pd.diff(a, b, accept=[r"^Elmer :: ", r"test_blender_"])
    assert rep["gaps"] == [] and len(rep["accepted"]) == 3, rep


def test_an_inner_skip_behind_a_matching_pass_is_still_a_gap():
    # The arm64 image's absent Elmer: every PASS line matched, but the live gate
    # inside each test printed a SKIP the baseline did not.
    a, b = pd.Side(), pd.Side()
    a.add_log("== Elmer transient-thermal ==\n  PASS test_plane_wall (2.00s)\n")
    b.add_log("== Elmer transient-thermal ==\n    SKIP — ElmerSolver not installed\n"
              "  PASS test_plane_wall (0.01s)\n")
    rep = pd.diff(a, b)
    assert rep["gaps"] == [("Elmer transient-thermal", "SKIP — ElmerSolver not installed", 1,
                            "SKIP only on B")], rep["gaps"]
    assert pd.diff(a, b, accept=["ElmerSolver not installed"])["gaps"] == []


def test_b_extra_coverage_is_reported_not_failed():
    a, b = _sides()
    rep = pd.diff(a, b)
    extra = {l for _, l, _ in rep["pass_only_b"]}
    assert {"PASS bempp live solve matches Mie", "PASS test_mcp_boot"} <= extra, extra


def test_main_exit_codes_and_the_vacuous_log():
    # Silenced: the report's SKIP/PASS lines would land in the very run_all.sh
    # log this script later parses.
    with tempfile.TemporaryDirectory() as d, \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        paths = {}
        for name, text in (("a", SELF_HOSTED), ("b", HOSTED), ("empty", "no suites here\n")):
            paths[name] = pathlib.Path(d, name + ".log")
            paths[name].write_text(text, encoding="utf-8")
        assert pd.main(["--a", str(paths["a"]), "--b", str(paths["b"])]) == 1
        assert pd.main(["--a", str(paths["a"]), "--b", str(paths["b"]),
                        "--accept", "Elmer", "--accept", "blender"]) == 0
        assert pd.main(["--a", str(paths["a"]), "--b", str(paths["a"])]) == 0
        # A log the parser cannot read must not report perfect parity.
        assert pd.main(["--a", str(paths["empty"]), "--b", str(paths["b"])]) == 2


def test_fail_on_b_fails_the_diff():
    a, b = _sides(b_text=SELF_HOSTED.replace("PASS test_docstrings", "FAIL test_docstrings"))
    rep = pd.diff(a, b)
    assert [l for _, l, _ in rep["fail_b"]] == ["FAIL test_docstrings"], rep["fail_b"]


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    tests = _discover()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:44s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:44s} ({time.time() - t0:.2f}s)")
    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
