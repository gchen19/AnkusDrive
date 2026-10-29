"""
fem_run_submit — CalculiX off the MCP channel (issue #308).

What has to be true for the fix to count, and the test that holds each one:

  test_submit_matches_sync
      The async path is the same solve, not a lookalike: fem_run and fem_run_submit
      on one analysis give identical fem_results, and fem_results(job_id=) reads the
      same numbers as fem_results(analysis=).
  test_submit_keeps_channel_free
      The point of the issue. ccx is swapped for a wrapper that sleeps before solving,
      so the solve is slow by construction. Two-sided: synchronous fem_run through the
      wrapper must take at least the sleep (proving the wrapper really slows ccx, so
      the next leg is not vacuous), and while a submitted solve is still running a
      `ping` must come back in well under the sleep.
      Also: results read by job_id while running, and a second solve on the same
      analysis, are both refused with a reason — never a read of purged results.
  test_ccx_failure_is_loud
      A ccx that exits non-zero fails fem_run (raise) and fails the submitted job
      (status 'failed', ccx's *ERROR line in the message). fem_run used to report
      status 'ok' whatever ccx returned.
  test_door_checks
      A submit that cannot run (no solver) fails synchronously and creates no job;
      result readers insist on exactly one of analysis/job_id; a job_id from some
      other job kind is refused.

The wrapper tests are POSIX-only (a /bin/sh script) and SKIP on Windows.

Run: python3 tests/test_fem_run_submit.py
"""
import os
import re
import stat
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ -> fem_scratch

from ankusdrive import Worker, WorkerError  # noqa: E402
from ankusdrive.client import FREECADCMD  # noqa: E402
from fem_scratch import fem_workdir  # noqa: E402

_STEEL = {"Name": "Steel", "YoungsModulus": "210000 MPa",
          "PoissonRatio": "0.30", "Density": "7900 kg/m^3"}
_SLEEP_S = 6.0          # the wrapper's delay; a free channel answers ping in far less
_CCX_PARAM = "User parameter:BaseApp/Preferences/Mod/Fem/Ccx"


def _freecad_available():
    return Path(FREECADCMD).exists()


def _expect_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except WorkerError as e:
        return e
    raise AssertionError(f"expected WorkerError from {args[:1]}; got success")


def _cantilever(w, doc="fem_submit"):
    """Steel 120x12x6 cantilever, -X face fixed, 1 MPa on the top face, linear static,
    1st-order mesh — a solve of a second or two. Returns {analysis, box}."""
    w.call("new_document", name=doc)
    box = w.call("add_primitive", kind="box", w=120, d=12, h=6)
    fixed = w.call("query_faces", handle=box["handle"],
                   predicate={"type": "planar", "normal_dir": [-1, 0, 0]})
    top = w.call("query_faces", handle=box["handle"],
                 predicate={"type": "planar", "normal_dir": [0, 0, 1]})
    an = w.call("fem_new_analysis", name="A")
    w.call("fem_set_solver", analysis=an["handle"], kind="ccx",
           tunables={"GeometricalNonlinearity": "linear", "MatrixSolverType": "default",
                     "IterationsControlParameterTimeUse": False})
    w.call("fem_set_material", analysis=an["handle"], body=box["handle"],
           material=dict(_STEEL))
    w.call("fem_add_constraint", analysis=an["handle"], kind="fixed",
           refs=[{"handle": box["handle"], "tag": fixed[0]["tag"]}])
    w.call("fem_add_constraint", analysis=an["handle"], kind="pressure",
           refs=[{"handle": box["handle"], "tag": top[0]["tag"]}], pressure=1.0)
    w.call("fem_mesh", analysis=an["handle"], body=box["handle"], char_length=4.0,
           _timeout=120.0)
    return {"analysis": an["handle"], "box": box["handle"]}


def _poll(w, job_id, timeout_s=300.0):
    """Poll job_status until terminal — each poll is also what gives the worker's main
    thread its turn to import the results. Returns the final status dict."""
    t0 = time.monotonic()
    while True:
        st = w.call("job_status", job_id=job_id)
        if st["status"] != "running":
            return st
        if time.monotonic() - t0 > timeout_s:
            raise AssertionError(f"{job_id} still running after {timeout_s}s: {st}")
        time.sleep(0.2)


def _same(a, b, label, rel=1e-9):
    assert abs(a - b) <= rel * max(abs(a), abs(b), 1e-30), f"{label}: {a!r} != {b!r}"


# --- ccx wrapper: a solver that is slow (or broken) by construction -----------

def _real_ccx(w):
    """The ccx the worker would launch: the FEM preference if set, else PATH."""
    return w.call("run_script", code=(
        "import shutil\n"
        f"pref = App.ParamGet({_CCX_PARAM!r}).GetString('ccxBinaryPath', '')\n"
        "__result__ = {'pref': pref, 'resolved': shutil.which(pref or 'ccx')}\n"
    ))["result"]


def _set_ccx(w, path):
    w.call("run_script", code=(
        f"App.ParamGet({_CCX_PARAM!r}).SetString('ccxBinaryPath', {path!r})\n"
        "__result__ = 'ok'\n"))


def _write_wrapper(body):
    """An executable /bin/sh ccx stand-in. Run with no arguments (FemToolsCcx's
    setup_ccx probe) it must answer at once, so only the real solve is affected."""
    d = tempfile.mkdtemp(prefix="ankusdrive_ccxwrap_")
    path = os.path.join(d, "ccx")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _CcxSwapped:
    """Point the worker's ccx preference at a wrapper, restoring it on exit — FreeCAD
    persists user parameters, so a leaked wrapper path would outlive the test."""

    def __init__(self, w, body_for_real_ccx):
        self.w = w
        self.body_for_real_ccx = body_for_real_ccx

    def __enter__(self):
        info = _real_ccx(self.w)
        assert info["resolved"], f"no ccx to wrap: {info}"
        self.prev = info["pref"]
        self.path = _write_wrapper(self.body_for_real_ccx(info["resolved"]))
        _set_ccx(self.w, self.path)
        return self

    def __exit__(self, *exc):
        _set_ccx(self.w, self.prev)
        return False


# --- tests --------------------------------------------------------------------

def test_submit_matches_sync():
    if not _freecad_available():
        print("    SKIP — freecadcmd not found")
        return
    with Worker() as w:
        h = _cantilever(w)
        sync = w.call("fem_run", analysis=h["analysis"],
                      workdir=fem_workdir("submit_sync"), _timeout=300.0)
        assert sync["status"] == "ok" and sync["returncode"] == 0, sync
        assert sync["analysis"] == h["analysis"] and sync["result_objects"] >= 1, sync
        r_sync = w.call("fem_results", analysis=h["analysis"])

        sub = w.call("fem_run_submit", analysis=h["analysis"],
                     workdir=fem_workdir("submit_async"))
        assert sub["status"] == "running" and sub["cache_hit"] is False, sub
        st = _poll(w, sub["job_id"])
        assert st["status"] == "done", st
        assert st["kind"] == "fem_run" and st["meta"]["analysis_type"] == "static", st
        res = w.call("job_result", job_id=sub["job_id"])["result"]
        assert {k: res[k] for k in ("status", "returncode", "analysis")} == \
            {"status": "ok", "returncode": 0, "analysis": h["analysis"]}, res
        assert res["result_objects"] == sync["result_objects"], (res, sync)

        r_job = w.call("fem_results", job_id=sub["job_id"])
        r_an = w.call("fem_results", analysis=h["analysis"])
        assert r_job == r_an, "fem_results(job_id) and fem_results(analysis) disagree"
        _same(r_job["max_vonmises_mpa"], r_sync["max_vonmises_mpa"], "max von Mises")
        _same(r_job["max_displacement_mm"], r_sync["max_displacement_mm"], "max disp")
        assert r_job["max_vonmises_mpa"] > 0 and r_job["max_displacement_mm"] > 0, r_job

        tip = w.call("fem_result_probe", job_id=sub["job_id"], point=[120, 6, 3])
        tip_an = w.call("fem_result_probe", analysis=h["analysis"], point=[120, 6, 3])
        assert tip == tip_an and tip["displacement_mm"] > 0, (tip, tip_an)
        print(f"    sync vs submit: vM {r_sync['max_vonmises_mpa']:.4f} / "
              f"{r_job['max_vonmises_mpa']:.4f} MPa, |u| "
              f"{r_sync['max_displacement_mm']:.6f} / {r_job['max_displacement_mm']:.6f} mm "
              f"(job {st['elapsed_s']}s)")


def test_submit_keeps_channel_free():
    if not _freecad_available():
        print("    SKIP — freecadcmd not found")
        return
    if os.name == "nt":
        print("    SKIP — the slow-ccx wrapper is a /bin/sh script")
        return
    with Worker() as w:
        h = _cantilever(w)
        slow = lambda real: (f'if [ "$#" -gt 0 ]; then sleep {_SLEEP_S:g}; fi\n'  # noqa: E731
                             f'exec "{real}" "$@"\n')
        with _CcxSwapped(w, slow):
            # Leg 1: the wrapper really does slow the solve (else leg 2 proves nothing).
            t0 = time.monotonic()
            w.call("fem_run", analysis=h["analysis"],
                   workdir=fem_workdir("submit_slow_sync"), _timeout=300.0)
            sync_s = time.monotonic() - t0
            assert sync_s >= _SLEEP_S, f"blocking fem_run took {sync_s:.1f}s < {_SLEEP_S}s"

            # Leg 2: the same slow solve, submitted — the channel answers meanwhile.
            t0 = time.monotonic()
            sub = w.call("fem_run_submit", analysis=h["analysis"],
                         workdir=fem_workdir("submit_slow_async"))
            submit_s = time.monotonic() - t0
            t1 = time.monotonic()
            assert w.call("ping") == "pong"
            ping_s = time.monotonic() - t1
            st = w.call("job_status", job_id=sub["job_id"])
            assert st["status"] == "running", f"expected the slow solve mid-flight: {st}"
            assert submit_s < _SLEEP_S / 2 and ping_s < 1.0, (
                f"channel blocked: submit {submit_s:.2f}s, ping {ping_s:.2f}s "
                f"while the solve sleeps {_SLEEP_S}s")

            # mid-flight guards: no read of purged results, no second solve
            e = _expect_error(w.call, "fem_results", job_id=sub["job_id"])
            assert "still running" in e.remote_message, e.remote_message
            e = _expect_error(w.call, "fem_run", analysis=h["analysis"],
                              workdir=fem_workdir("submit_slow_other"), _timeout=60.0)
            assert sub["job_id"] in e.remote_message, e.remote_message

            st = _poll(w, sub["job_id"])
            assert st["status"] == "done", st
            r = w.call("fem_results", job_id=sub["job_id"])
            assert r["max_vonmises_mpa"] > 0, r
            # the guard releases once the job is done
            w.call("fem_run", analysis=h["analysis"],
                   workdir=fem_workdir("submit_slow_sync"), _timeout=300.0)
        print(f"    blocking fem_run {sync_s:.1f}s; submit returned in {submit_s:.2f}s, "
              f"ping {ping_s * 1000:.0f} ms mid-solve; job done in {st['elapsed_s']}s")


def test_ccx_failure_is_loud():
    if not _freecad_available():
        print("    SKIP — freecadcmd not found")
        return
    if os.name == "nt":
        print("    SKIP — the failing-ccx wrapper is a /bin/sh script")
        return
    with Worker() as w:
        h = _cantilever(w)
        broken = lambda real: ('if [ "$#" -eq 0 ]; then exec "' + real + '"; fi\n'  # noqa: E731
                               'echo " *ERROR in calinput: forced by test_fem_run_submit"\n'
                               'exit 201\n')
        with _CcxSwapped(w, broken):
            e = _expect_error(w.call, "fem_run", analysis=h["analysis"],
                              workdir=fem_workdir("submit_fail_sync"), _timeout=120.0)
            assert "code 201" in e.remote_message and "forced by" in e.remote_message, \
                e.remote_message
            sub = w.call("fem_run_submit", analysis=h["analysis"],
                         workdir=fem_workdir("submit_fail_async"))
            st = _poll(w, sub["job_id"], timeout_s=120.0)
            assert st["status"] == "failed" and "forced by" in st["error"], st
            e = _expect_error(w.call, "fem_results", job_id=sub["job_id"])
            assert "failed" in e.remote_message, e.remote_message
        assert w.call("ping") == "pong"
        print(f"    ccx exit 201 → fem_run raised; job failed: {st['error'][:90]}…")


def test_door_checks():
    if not _freecad_available():
        print("    SKIP — freecadcmd not found")
        return
    with Worker() as w:
        w.call("new_document", name="fem_submit_door")
        w.call("add_primitive", kind="box", w=10, d=10, h=10)
        an = w.call("fem_new_analysis")
        before = w.call("job_list")["count"]
        e = _expect_error(w.call, "fem_run_submit", analysis=an["handle"],
                          workdir=fem_workdir("submit_door"))
        assert "solver" in e.remote_message.lower(), e.remote_message
        assert w.call("job_list")["count"] == before, "a refused submit left a job behind"

        e = _expect_error(w.call, "fem_results")
        assert "exactly one" in e.remote_message, e.remote_message
        e = _expect_error(w.call, "fem_results", analysis=an["handle"], job_id="job_1")
        assert "exactly one" in e.remote_message, e.remote_message

        demo = w.call("async_demo_submit", duration_s=0.01)
        _poll(w, demo["job_id"])
        e = _expect_error(w.call, "fem_modal_results", job_id=demo["job_id"])
        assert "not a fem_run_submit" in e.remote_message, e.remote_message
        assert w.call("ping") == "pong"


def test_mesh_floor_and_private_workdir():
    """fem_mesh floors the element size at char_length/10 unless told otherwise (a 0
    floor let sliver edges in imported CAD run Gmsh to a multi-GB mesh), and each
    worker's default fem_run workdir is its own, so concurrent workspaces never
    solve in the same directory."""
    if not _freecad_available():
        print("    SKIP — freecadcmd not found")
        return
    with Worker() as w1, Worker() as w2:
        dirs = []
        for w in (w1, w2):
            h = _cantilever(w, doc="fem_floor")
            dirs.append(w.call("fem_run", analysis=h["analysis"], _timeout=300.0)["workdir"])
        assert dirs[0] != dirs[1], dirs

        w1.call("new_document", name="fem_floor_args")
        box = w1.call("add_primitive", kind="box", w=20, d=10, h=10)
        an = w1.call("fem_new_analysis")
        m = w1.call("fem_mesh", analysis=an["handle"], body=box["handle"], char_length=4.0)
        assert abs(m["min_length"] - 0.4) < 1e-12, m
        m = w1.call("fem_mesh", analysis=an["handle"], body=box["handle"], char_length=4.0,
                    min_length=1.0, name="Mesh2")
        assert m["min_length"] == 1.0, m
        e = _expect_error(w1.call, "fem_mesh", analysis=an["handle"], body=box["handle"],
                          char_length=4.0, min_length=5.0, name="Mesh3")
        assert "min_length" in e.remote_message, e.remote_message


def test_default_workdir_reaped():
    """The per-pid default workdirs do not accumulate: a worker removes its own on
    exit, and its first default solve sweeps those of workers that died without
    doing so. The sweep only takes `ankusdrive_fem_<pid>` dirs whose pid is gone
    and that have sat untouched for an hour (TMPDIR may be shared with workers in
    another pid namespace); a live pid, a fresh dir, a near-miss name, or a
    caller-named workdir all survive, as does the solve that is still running."""
    if not _freecad_available():
        print("    SKIP — freecadcmd not found")
        return
    import shutil
    import subprocess
    root = tempfile.mkdtemp(prefix="ankusdrive_reap_")
    old = time.time() - 2 * 3600

    def dead_pid():
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def plant(name, stale):
        path = os.path.join(root, name)
        os.makedirs(path)
        Path(path, "Mesh.frd").write_text("x", encoding="utf-8")
        if stale:
            os.utime(os.path.join(path, "Mesh.frd"), (old, old))
            os.utime(path, (old, old))
        return path

    gone = plant(f"ankusdrive_fem_{dead_pid()}", stale=True)
    keep = [plant(f"ankusdrive_fem_{dead_pid()}", stale=False),   # dead, but fresh
            plant(f"ankusdrive_fem_{os.getpid()}", stale=True),   # live pid
            plant("ankusdrive_fem_0123", stale=True),             # not a name we make
            plant("ankusdrive_fem_keep", stale=True)]
    mine = os.path.join(root, "caller_workdir")
    prev = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = root                   # the worker inherits it
    try:
        with Worker() as w:
            h = _cantilever(w, doc="fem_reap")
            own = w.call("fem_run", analysis=h["analysis"], _timeout=300.0)["workdir"]
            # Not w.proc.pid: on Windows that is the launcher, not the freecadcmd
            # process whose os.getpid() names the dir.
            assert os.path.realpath(os.path.dirname(own)) == os.path.realpath(root), own
            assert re.fullmatch(r"ankusdrive_fem_[1-9]\d*", os.path.basename(own)), own
            assert os.path.isfile(os.path.join(own, "Mesh.frd")), os.listdir(own)
            assert not os.path.exists(gone), "dead, stale workdir was not swept"
            w.call("fem_run", analysis=h["analysis"], workdir=mine, _timeout=300.0)
        assert not os.path.exists(own), "worker left its default workdir behind"
        for path in keep:
            assert os.path.isdir(path), f"swept a dir it must not touch: {path}"
        assert os.path.isfile(os.path.join(mine, "Mesh.frd")), "caller workdir removed"
    finally:
        if prev is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = prev
        shutil.rmtree(root, ignore_errors=True)


# --- runner -------------------------------------------------------------------

def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    failures = []
    t0 = time.time()
    for name, fn in _discover():
        t = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:44s} ({time.time() - t:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:44s} ({time.time() - t:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({time.time() - t0:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()
