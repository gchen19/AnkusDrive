"""Solver case directories: one root, a stated retention (#437, item 3).

Every built-in solve writes its deck into a directory and reports it. Those have to
outlive the job — a result names them and a second tool is handed them (#116) — and
they must not accumulate, which they did: 31 `mkdtemp` calls against 2 `rmtree`, and
23 directories / 1.3 GB on one developer machine. `ankusdrive/cases.py` keeps them
under one root with a bound it states.

Two tiers:

  * **static** (stdlib, fast lane) — the policy itself: a fresh directory per call,
    reaping oldest-first past the count and size caps, the grace window that makes it
    safe without locks, a foreign directory in the root left alone, `KEEP_SCRATCH`
    and `KEEP=0` turning reaping off, and the env/config knobs. Plus the wiring: no
    solver handler still calls `tempfile.mkdtemp` directly, and the GPL runner still
    imports no AnkusDrive code.
  * **worker** (needs FreeCAD) — a real solve's `case_dir` lands under the root and
    survives the job, and `solve_capabilities` reports the policy.

Run:  .venv/bin/python3 tests/test_case_cleanup.py
"""
import ast
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
sys.path.insert(0, str(REPO))


def _cases_module():
    spec = importlib.util.spec_from_file_location("cases_under_test", PKG / "cases.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _cases_module()


class _Root:
    """A scratch case root, pointed at by the env var, torn down after."""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="caseroot_")
        self.prev = os.environ.get("ANKUSDRIVE_CASE_ROOT")
        self.prev_keep = os.environ.get("ANKUSDRIVE_KEEP_SCRATCH")
        os.environ["ANKUSDRIVE_CASE_ROOT"] = self.path
        os.environ.pop("ANKUSDRIVE_KEEP_SCRATCH", None)
        C._sizes.clear()
        return self

    def __exit__(self, *exc):
        for var, val in (("ANKUSDRIVE_CASE_ROOT", self.prev),
                         ("ANKUSDRIVE_KEEP_SCRATCH", self.prev_keep)):
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
        shutil.rmtree(self.path, ignore_errors=True)

    def make(self, kind="elmer_slab", kb=1, age_s=0.0):
        d = C.new(kind)
        with open(os.path.join(d, "case.sif"), "w", encoding="utf-8") as fh:
            fh.write("x" * 1024 * kb)
        if age_s:
            when = time.time() - age_s
            os.utime(d, (when, when))
        return d

    def names(self):
        return sorted(os.listdir(self.path))


# --- the policy ----------------------------------------------------------------------

def test_a_case_is_its_own_directory_under_one_root():
    with _Root() as r:
        a, b = C.new("elmer_slab"), C.new("elmer_slab")
        assert a != b and os.path.isdir(a) and os.path.isdir(b)
        assert os.path.dirname(a) == r.path and os.path.dirname(b) == r.path
        assert os.path.basename(a).startswith("elmer_slab-"), a
        # a kind is sanitised into the name pattern reaping matches on
        odd = C.new("Foam Mesh/../probe")
        assert C._OURS.match(os.path.basename(odd)), odd
        assert os.path.dirname(odd) == r.path


def test_oldest_go_first_past_the_count_cap():
    with _Root() as r:
        for i in range(10):
            r.make(age_s=7200 - i)          # oldest first, all well past the grace window
        out = C.reap(keep=4)
        assert out["removed"] == 6 and out["kept"] == 4, out
        assert len(r.names()) == 4, r.names()
        # the four that survived are the four youngest
        left = [os.path.join(r.path, n) for n in r.names()]
        assert all(time.time() - os.path.getmtime(p) < 7196 for p in left), left


def test_the_size_cap_bites_even_under_the_count():
    with _Root() as r:
        for i in range(6):
            r.make(kb=64, age_s=7200 - i)
        out = C.reap(keep=100, max_gb=128 / 1024 ** 2)      # 128 KB total
        assert out["removed"] >= 3, out
        assert out["bytes"] <= 3 * 64 * 1024, out
        assert out["freed_bytes"] > 0, out


def test_a_warm_directory_is_never_reaped():
    """The grace window is what makes reaping safe without a lock: a solve still
    writing keeps its directory's mtime fresh, and another worker sharing the root
    cannot delete it out from under them."""
    with _Root() as r:
        for i in range(5):
            r.make(age_s=7200 - i)
        warm = r.make()                                   # just now
        out = C.reap(keep=1)
        assert os.path.isdir(warm), "a directory touched seconds ago was reaped"
        assert out["skipped_in_grace"] == 0, out           # `warm` is the one kept by `keep`
        out = C.reap(keep=0 - 0 + 1, grace_s=0.0)         # no grace: everything over goes
        assert os.path.isdir(warm), "the newest is kept whatever the grace"
        assert len(r.names()) == 1, r.names()


def test_grace_wins_over_both_caps():
    with _Root() as r:
        for _ in range(6):
            r.make(kb=64)                                  # all warm
        out = C.reap(keep=1, max_gb=1e-9)
        assert out["removed"] == 0, out
        assert out["skipped_in_grace"] >= 4, out
        assert len(r.names()) == 6, r.names()


def test_a_directory_we_did_not_make_is_left_alone():
    with _Root() as r:
        foreign = os.path.join(r.path, "someone-elses-work")
        os.makedirs(foreign)
        with open(os.path.join(foreign, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("keep me")
        for _ in range(4):
            r.make(age_s=7200)
        C.reap(keep=1)
        assert os.path.isdir(foreign), "reaping touched a directory it did not create"
        assert os.path.isfile(os.path.join(foreign, "notes.txt"))


def test_reaping_can_be_turned_off():
    with _Root() as r:
        for _ in range(5):
            r.make(age_s=7200)
        os.environ["ANKUSDRIVE_KEEP_SCRATCH"] = "1"
        out = C.reap(keep=1)
        assert out.get("disabled") and out["removed"] == 0, out
        assert len(r.names()) == 5, r.names()
        os.environ.pop("ANKUSDRIVE_KEEP_SCRATCH")
        out = C.reap(keep=0)
        assert out.get("disabled") and out["removed"] == 0, out
        assert len(r.names()) == 5, "keep=0 means keep everything, not delete everything"


def test_new_reaps_and_never_raises():
    with _Root() as r:
        os.environ["ANKUSDRIVE_CASE_KEEP"] = "3"
        try:
            for _ in range(5):
                r.make(age_s=7200)
            d = C.new("foam_pipe")                 # this call does the reaping
            assert os.path.isdir(d)
            assert len(r.names()) <= 4, r.names()  # <= keep, plus the one just made
        finally:
            os.environ.pop("ANKUSDRIVE_CASE_KEEP", None)
    # a root that cannot be listed is not a reason to fail a solve
    assert C.reap() is not None


def test_usage_reports_the_policy():
    with _Root() as r:
        r.make(kb=4)
        u = C.usage()
        assert u["root"] == r.path and u["count"] == 1, u
        assert u["bytes"] >= 4096 and u["reaping"] is True, u
        assert u["bytes_exact"] is True, u
        for key in ("keep", "max_gb", "grace_s"):
            assert key in u, u


def test_usage_stays_quick_on_a_big_root():
    """It backs a capability report, so it must not stat a 4 GB root of OpenFOAM time
    directories to answer. Past its entry budget it stops and says the total is
    partial rather than taking the time."""
    with _Root() as r:
        for _ in range(4):
            d = C.new("foam_big")
            for j in range(300):
                with open(os.path.join(d, f"f{j}"), "w", encoding="utf-8") as fh:
                    fh.write("y" * 10)
        C._sizes.clear()
        full = C.usage()
        assert full["bytes"] == 12000 and full["bytes_exact"] is True, full
        C._sizes.clear()
        capped = C.usage(budget=500)
        assert capped["bytes_exact"] is False, capped
        assert 0 < capped["bytes"] < full["bytes"], capped
        assert capped["count"] == 4, "every directory is still counted"
        # a memo hit costs no budget, so a second pass is exact again
        C._sizes.clear()
        C.usage()
        assert C.usage(budget=1)["bytes_exact"] is True, "memoised sizes were re-walked"


# --- the wiring ------------------------------------------------------------------------

def test_no_solver_handler_still_strands_a_directory():
    """The bug was 31 mkdtemp calls against 2 rmtree. Every directory a result names
    goes through cases.new now; the only mkdtemp left in the worker are the two
    transient scratch dirs that already remove themselves."""
    src = (PKG / "worker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    stranded = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "mkdtemp"):
            continue
        prefix = next((kw.value for kw in node.keywords if kw.arg == "prefix"), None)
        name = prefix.value if isinstance(prefix, ast.Constant) else "<computed>"
        stranded.append((node.lineno, name))
    assert [n for _, n in stranded] == ["ankusdrive_modal_gate_", "release_"], stranded
    # and both of those are still paired with a cleanup
    assert src.count("rmtree(workdir, ignore_errors=True)") == 1, "modal gate lost its cleanup"
    assert src.count("rmtree(work, ignore_errors=True)") == 1, "release_package lost its cleanup"
    assert "from ankusdrive import cases as _cases" in src


def test_the_gpl_runner_imports_no_ankusdrive_code():
    """openEMS is GPL-3.0 and runs out-of-process precisely so AnkusDrive never
    imports it. The case root therefore reaches it as an environment variable — if
    this ever becomes an import, the isolation is gone."""
    src = (PKG / "em_fullwave_gpl_runner.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):       # the imports, not the prose about them
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "ankusdrive" not in imported, f"the GPL runner imports AnkusDrive: {imported}"
    assert "openEMS" in imported, "this is the runner that imports the GPL engine"
    assert 'os.environ.get("ANKUSDRIVE_CASE_ROOT")' in src, src[:400]
    assert src.count("dir=CASE_ROOT") == 2, "both openEMS sims must honour the root"
    worker = (PKG / "worker.py").read_text(encoding="utf-8")
    assert '"ANKUSDRIVE_CASE_ROOT": _cases.root()' in worker, \
        "the worker no longer hands the runner the managed root"


def test_the_module_is_freecad_free_and_stdlib_only():
    tree = ast.parse((PKG / "cases.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "FreeCAD" not in imported and "Part" not in imported, imported
    assert imported <= {"os", "re", "shutil", "tempfile", "threading", "time",
                        "__future__", "ankusdrive"}, imported


# --- worker tier -------------------------------------------------------------------------

def worker_test_a_real_solve_lands_under_the_root():
    from ankusdrive import Worker
    with _Root() as r, Worker() as w:
        caps = w.call("solve_capabilities")
        assert caps["cases"]["root"] == r.path, caps["cases"]
        for key in ("count", "bytes", "keep", "max_gb", "grace_s", "reaping"):
            assert key in caps["cases"], caps["cases"]
        if not caps["families"].get("thermal_transient", {}).get("any_available"):
            print("    (no Elmer — policy checked, solve skipped)")
            return
        sub = w.call("thermal_transient_submit", half_thickness_mm=1.5, h_conv=8.0,
                     duration_s=60.0, k=0.2, rho=1040.0, cp=1400.0, t_initial_c=200.0)
        for _ in range(120):
            if w.call("job_status", job_id=sub["job_id"])["status"] != "running":
                break
            time.sleep(1.0)
        res = w.call("job_result", job_id=sub["job_id"])
        assert res["status"] == "done", res
        case = res["result"]["case_dir"]
        assert os.path.dirname(case) == r.path, f"{case} is not under {r.path}"
        assert os.path.isdir(case), "the case was gone before the caller could read it"
        assert os.path.isfile(os.path.join(case, "case.sif")), sorted(os.listdir(case))


# --- runner --------------------------------------------------------------------------------

def _freecad_available() -> bool:
    try:
        from ankusdrive import client
        return bool(client._resolve_freecadcmd())
    except Exception:
        return False


def _tests():
    g = globals()
    out = [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]
    if not _freecad_available():
        print("  SKIP worker tier — freecadcmd not resolvable")
    else:
        out.append(("worker_test_a_real_solve_lands_under_the_root",
                    worker_test_a_real_solve_lands_under_the_root))
    return out


def main():
    failures = []
    t_suite = time.time()
    tests = _tests()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:58s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:58s} ({time.time() - t0:.2f}s)")
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
