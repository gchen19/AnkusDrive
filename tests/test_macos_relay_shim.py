"""The macOS Multipass code path, end to end against a stub `multipass` (issue #364).

Hosted Apple-Silicon runners cannot run Multipass, but what the macOS lane actually
guards is mostly host-side. `tests/ci/multipass` is a stand-in CLI that runs `exec`
locally while refusing what a real VM would break on: an attached stdin (#223), a
`cd` into a path the VM cannot see, host env crossing the relay, a relay whose death
kills the solver. The stub solvers in `tests/ci/multipass-vm/` emit what fsi_case's
parsers read. Nothing here patches the code under test: `bash_argv`,
`multipass_vm_state`, `run_coupled_fsi` and `_stop_participant` run unmodified.

Pins, each a guard that has already broken once:
  * VM state from real `multipass info --format json` shapes; a WEDGED `info` read
    returns within its timeout instead of hanging discovery.
  * A coupled FSI solve crosses the relay from the real $TMPDIR — on macOS the
    `/var/folders/…/` path, which must be handed over as-is (never realpath'd to
    `/private/var/…`, which the VM mount does not have).
  * stdin never crosses: even with the test process's own stdin a readable pipe, every
    launch is `stdin=DEVNULL`.
  * The pidfile sweep stops the solver BEHIND the relay; terminating the relay alone
    does not (control).
  * A wedged coupled solve hits run_coupled_fsi's timeout and leaves no solver running.

Runs on any POSIX host: on non-macOS it fakes `platform.system()` so the Darwin branch
is taken (the real-/var/folders assertion then only applies on a real Mac — lane C).

Run:  python3 tests/test_macos_relay_shim.py
"""
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import solvers  # noqa: E402
from ankusdrive.analysis import fsi_case  # noqa: E402

SHIM_DIR = REPO / "tests" / "ci"
VM = REPO / "tests" / "ci" / "multipass-vm"
REAL_DARWIN = sys.platform == "darwin"
_ENV = ("PATH", "TMPDIR", "MULTIPASS_SHIM_STATE", "MULTIPASS_SHIM_MOUNTS",
        "MULTIPASS_SHIM_HANG", "MULTIPASS_SHIM_LOG", "MULTIPASS_SHIM_HOME",
        "ANKUSDRIVE_SUBSTRATE", "ANKUSDRIVE_CONFIG", "ANKUSDRIVE_OPENFOAM_INSTANCE",
        "ANKUSDRIVE_MULTIPASS_TIMEOUT_S", "ANKUSDRIVE_MULTIPASS_CACHE_S",
        "ANKUSDRIVE_FSI_OPENFOAM_BASHRC", "ANKUSDRIVE_OPENFOAM_BASHRC",
        "ANKUSDRIVE_CCX_PRECICE", "ANKUSDRIVE_PRECICE_LIB",
        "ANKUSDRIVE_OPENFOAM_ADAPTER_LIB")


class _Darwin:
    @staticmethod
    def system():
        return "Darwin"

    @staticmethod
    def machine():
        return "arm64"


@contextlib.contextmanager
def _shim(**env):
    """The stub `multipass` first on PATH, the config file blinded, the substrate left
    to the Darwin default, the FSI overrides pointing at the stub stack, and a hard
    per-test ceiling so a hung relay fails the test instead of the job."""
    saved = {n: os.environ.get(n) for n in _ENV}
    saved_platform = solvers.platform
    solvers._vm_info_cache.clear()
    os.environ["PATH"] = f"{SHIM_DIR}{os.pathsep}{saved['PATH'] or ''}"
    os.environ["ANKUSDRIVE_CONFIG"] = str(REPO / "tests" / "no-such-config.toml")
    os.environ["ANKUSDRIVE_MULTIPASS_CACHE_S"] = "0"
    for n in ("ANKUSDRIVE_SUBSTRATE", "ANKUSDRIVE_OPENFOAM_INSTANCE",
              "MULTIPASS_SHIM_HANG", "MULTIPASS_SHIM_STATE"):
        os.environ.pop(n, None)
    os.environ.update({
        "ANKUSDRIVE_FSI_OPENFOAM_BASHRC": str(VM / "bashrc"),
        "ANKUSDRIVE_OPENFOAM_BASHRC": str(VM / "bashrc"),
        "ANKUSDRIVE_CCX_PRECICE": str(VM / "bin" / "ccx_preCICE"),
        "ANKUSDRIVE_PRECICE_LIB": str(VM / "bin"),
        "ANKUSDRIVE_OPENFOAM_ADAPTER_LIB": str(VM / "bin"),
    })
    # The VM sees ONLY the scratch, at its literal path (what `multipass mount` gives).
    scratch = os.environ.get("TMPDIR") or tempfile.gettempdir()
    os.environ["MULTIPASS_SHIM_MOUNTS"] = scratch
    os.environ.update(env)
    if not REAL_DARWIN:
        solvers.platform = _Darwin
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(
        TimeoutError("test exceeded its 120 s ceiling — a relay hung")))
    signal.alarm(120)
    try:
        yield
    finally:
        signal.alarm(0)
        solvers.platform = saved_platform
        solvers._vm_info_cache.clear()
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v


def _case():
    """A case dir exactly as the product makes one: mkdtemp under the real TMPDIR."""
    d = tempfile.mkdtemp(prefix="fsi_shim_")
    fsi_case.write_fsi_case(d, end_time_s=0.04, time_window_s=0.01, max_iterations=5)
    return d


@contextlib.contextmanager
def _stdin_is_a_pipe():
    """Make THIS process's fd 0 a readable pipe, so any launch that forgets
    stdin=DEVNULL hands the stub a live stdin (which it rejects)."""
    r, w = os.pipe()
    os.write(w, b"a JSON-RPC request the relay must not eat\n")
    saved = os.dup(0)
    os.dup2(r, 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        for fd in (saved, r, w):
            os.close(fd)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid(case_sub):
    for _ in range(100):
        p = os.path.join(case_sub, ".ankusdrive-participant.pid")
        if os.path.exists(p):
            text = Path(p).read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(0.1)
    raise AssertionError(f"no pidfile appeared in {case_sub}")


# --- state ----------------------------------------------------------------------

def test_vm_state_reads_real_info_shapes():
    for shim_state, expect in (("Running", "running"), ("Stopped", "stopped"),
                               ("Suspended", "stopped"), ("Starting", "stopped"),
                               ("absent", "absent")):
        with _shim(MULTIPASS_SHIM_STATE=shim_state):
            assert solvers.multipass_available() is True
            got = solvers.multipass_vm_state()
            assert got == expect, (shim_state, got)


def test_wedged_info_read_returns_within_its_timeout():
    """A hung multipassd must degrade discovery to 'absent', not hang it."""
    with _shim(MULTIPASS_SHIM_HANG="info", ANKUSDRIVE_MULTIPASS_TIMEOUT_S="2"):
        t0 = time.monotonic()
        got = solvers.multipass_vm_state()
        took = time.monotonic() - t0
    assert got == "absent", got
    assert took < 15, f"the wedged info read took {took:.1f}s — its timeout is not bounding it"


# --- the relay ------------------------------------------------------------------

def test_fsi_crosses_the_relay_from_the_real_tmpdir():
    """The whole macOS launch sequence — blockMesh, then both participants, each cd'd
    into its case subdir through `multipass exec` — completes against the stub stack.
    The stub rejects a cd outside the mount, so a case dir handed over in any other
    spelling than the mounted one fails here."""
    with _shim():
        d = _case()
        if REAL_DARWIN:
            scratch = os.environ["MULTIPASS_SHIM_MOUNTS"].rstrip("/")
            assert d.startswith(scratch + "/"), (d, scratch)
            assert not scratch.startswith("/private/"), \
                f"TMPDIR {scratch!r} is already a realpath — this Mac is not exercising /var"
        res = fsi_case.run_coupled_fsi(d, timeout_s=60)
    assert res["ok"] is True, res
    assert res["returncode_fluid"] == 0 and res["returncode_solid"] == 0, res
    assert res["time_windows"] == 4 and res["coupling_converged"] is True, res
    assert res["tip_history"] and len(res["tip_history"]) >= 4, res


def test_stdin_never_crosses_the_relay():
    """With this process's stdin a readable pipe, every product launch still hands the
    relay /dev/null — the stub fails any that does not (#223)."""
    log = tempfile.mktemp(prefix="shim-", suffix=".jsonl")
    with _shim(MULTIPASS_SHIM_LOG=log), _stdin_is_a_pipe():
        d = _case()
        res = fsi_case.run_coupled_fsi(d, timeout_s=60)
        solvers._multipass_info_exec("openfoam", 5.0)
    records = ([json.loads(ln) for ln in open(log, encoding="utf-8")]
               if os.path.exists(log) else [])
    execs = [r for r in records if r.get("sub") == "exec"]
    assert execs, "no launch went through the relay"
    leaked = [r["cmd"][-1][:60] for r in execs if not r["stdin_devnull"]]
    assert not leaked, f"stdin crossed the relay for: {leaked}"
    assert res["ok"] is True, res


def test_pidfile_sweep_stops_the_solver_behind_the_relay():
    """Control: terminating the relay (the `multipass exec` Popen) leaves the solver
    running. Then _stop_participant, which also sweeps by pidfile THROUGH the relay,
    actually stops it."""
    with _shim():
        d = _case()
        sub = os.path.join(d, "fluid-openfoam")
        script = "echo $$ > .ankusdrive-participant.pid; exec sleep 300"

        proc = subprocess.Popen(solvers.bash_argv(script, sub), cwd=sub,
                                stdin=subprocess.DEVNULL)
        pid = _pid(sub)
        proc.terminate()
        proc.wait(timeout=10)
        time.sleep(0.5)
        assert _alive(pid), "control: killing the relay alone already stopped the solver"
        os.kill(pid, signal.SIGKILL)
        os.remove(os.path.join(sub, ".ankusdrive-participant.pid"))

        proc = subprocess.Popen(solvers.bash_argv(script, sub), cwd=sub,
                                stdin=subprocess.DEVNULL)
        pid = _pid(sub)
        assert solvers.runs_in_substrate() is True
        fsi_case._stop_participant(proc, sub)
        for _ in range(50):
            if not _alive(pid):
                break
            time.sleep(0.1)
        alive = _alive(pid)
        if alive:
            os.kill(pid, signal.SIGKILL)
    assert not alive, "_stop_participant did not stop the solver behind the relay"


def test_wedged_coupled_solve_hits_the_timeout_and_leaves_nothing_running():
    with _shim():
        d = _case()
        open(os.path.join(d, "HANG"), "w", encoding="utf-8").close()  # both stub participants hang
        t0 = time.monotonic()
        res = fsi_case.run_coupled_fsi(d, timeout_s=6)
        took = time.monotonic() - t0
        pids = [int(Path(p).read_text(encoding="utf-8").strip())
                for p in (os.path.join(d, s, ".ankusdrive-participant.pid")
                          for s in ("fluid-openfoam", "solid-calculix"))
                if os.path.exists(p)]
        time.sleep(1.0)
        survivors = [p for p in pids if _alive(p)]
        for p in survivors:
            os.kill(p, signal.SIGKILL)
    assert res["ok"] is False, res
    assert took < 60, f"a wedged solve with timeout_s=6 took {took:.1f}s"
    assert len(pids) == 2, f"expected both participants' pidfiles, got {pids}"
    assert not survivors, f"solvers still running behind the relay after the timeout: {survivors}"


# --- runner ---------------------------------------------------------------------

def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    if os.name != "posix":
        print("  SKIP — the Multipass relay path is POSIX-hosted (macOS); nothing to run here")
        return 0
    failures = []
    t_suite = time.time()
    for name, fn in _discover():
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:62s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:62s} ({time.time() - t0:.2f}s)")
    print()
    total = len(_discover())
    if failures:
        print(f"== {len(failures)}/{total} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        return 1
    print(f"== {total}/{total} passed  ({time.time() - t_suite:.1f}s) ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
