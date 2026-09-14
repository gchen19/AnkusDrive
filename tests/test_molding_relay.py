"""Molding solvers cross the substrate relay: a short-horizon smoke (#386).

`tests/test_molding_fill.py` gates the real fill / pack / cool physics, which takes
~15 min native and ran past 3 h under lane D's QEMU emulation, so that file is not
on the hosted macOS VM lane. What the macOS path adds to those solves is not physics
but plumbing, and none of it needs a full fill:

  * interFoam launched through ``bash_argv`` after ``solvers.openfoam_bashrc()``;
  * openInjMoldSim launched through ``bash_argv`` after the SEPARATE OpenFOAM-7
    bashrc, with the binary and bashrc given as in-VM paths
    (``ANKUSDRIVE_OPENINJMOLDSIM`` / ``..._BASHRC``);
  * ``setFields`` / ``foamDictionary`` from that OF7 environment;
  * the solver's time directories landing in the host-side case dir, over the mount.

Each test runs its solver for a few dozen timesteps and asserts that it exited 0,
wrote a time directory the host can see, and logged no nan. Native these are
seconds, so the file runs on every heavy lane, not only on emulation.

Gated behind RUN_HEAVY_SOLVES like every live solve (tests/heavy_solve.py), and each
test SKIPs, saying so, where its solver does not resolve.

Run:  RUN_HEAVY_SOLVES=1 python3 tests/test_molding_relay.py
"""

import os
import subprocess
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ankusdrive import solvers  # noqa: E402
from ankusdrive.analysis import molding_fill as mf  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402


# A few dozen openInjMoldSim steps (deltaT starts at 1e-7, capped at 3e-6).
_OIMS_END_S = 2e-5


def _run(script, case_dir, log):
    with open(os.path.join(case_dir, log), "w", encoding="utf-8") as f:
        # stdin: `multipass exec` forwards it into the VM (#223)
        return subprocess.run(solvers.bash_argv(script, case_dir), cwd=case_dir,
                              stdin=subprocess.DEVNULL, stdout=f,
                              stderr=subprocess.STDOUT).returncode


def _tail(case_dir, log, n=1500):
    try:
        with open(os.path.join(case_dir, log), encoding="utf-8", errors="replace") as f:
            return f.read()[-n:]
    except OSError as e:
        return f"<no {log}: {e}>"


def _assert_advanced(case_dir, log):
    latest = mf._latest_time_dir(case_dir)
    assert latest is not None, f"no time directory reached the host side\n{_tail(case_dir, log)}"
    with open(os.path.join(case_dir, log), encoding="utf-8", errors="replace") as f:
        text = f.read()
    assert "nan" not in text.lower(), f"nan in {log}\n{text[-1500:]}"
    return latest


def test_interfoam_cavity_advances_through_the_relay():
    if skip_heavy("interFoam relay smoke"):
        return
    bashrc = solvers.openfoam_bashrc()
    if not solvers.is_available("openfoam") and bashrc is None:
        print("    SKIP — OpenFOAM not installed")
        return
    d = tempfile.mkdtemp(prefix="mf_relay_if_")
    # 25 steps of the fillable cavity test_molding_fill solves to 0.4 s.
    mf.write_cavity_case(d, length_m=0.05, height_m=0.002, depth_m=0.001, nx=80, ny=8,
                         inject_velocity_m_s=0.5, end_time_s=5e-4, deltaT_s=2e-5)
    src = f"source '{bashrc}' >/dev/null 2>&1\n" if bashrc else ""
    rc = _run(src + "blockMesh > log.bm 2>&1 && interFoam", d, "log.if")
    assert rc == 0, _tail(d, "log.if") + _tail(d, "log.bm")
    _assert_advanced(d, "log.if")


def test_openinjmoldsim_advances_through_the_relay():
    if skip_heavy("openInjMoldSim relay smoke"):
        return
    binp, bashrc = solvers.openinjmoldsim_bin(), solvers.openinjmoldsim_bashrc()
    if not binp or not bashrc:
        print("    SKIP — openInjMoldSim (OF7-org) not built")
        return
    d = tempfile.mkdtemp(prefix="oims_relay_")
    # The generated case test_molding_fill fills. Its injection-pressure table spans
    # the case's own end time, so the case keeps it and only controlDict is cut short
    # afterwards, by foamDictionary from the OF7 environment (the pack phase's call).
    mf.write_openinjmoldsim_case(
        d, resin="PS", length_m=0.02, height_m=1e-3, depth_m=1e-3, nx=60, ny=8,
        peak_pressure_pa=2.0e6)
    chain = " && ".join(" ".join(a) for a in (
        ["blockMesh"], ["setFields"],
        *mf.time_extend_cmds(end_time_s=_OIMS_END_S, write_interval_s=_OIMS_END_S,
                             max_deltaT_s=3e-6),
        [f"'{binp}'"]))
    script = f"source '{bashrc}' >/dev/null 2>&1\nunset FOAM_SIGFPE\n{chain}"
    rc = _run(script, d, "log.oims")
    assert rc == 0, _tail(d, "log.oims")
    latest = _assert_advanced(d, "log.oims")
    assert os.path.isfile(os.path.join(d, latest, "alpha.poly")), \
        f"time {latest} has no alpha.poly: {sorted(os.listdir(os.path.join(d, latest)))}"


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
