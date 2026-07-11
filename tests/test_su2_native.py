"""SU2 native runner — the bash-free CFD execution path (issue #203).

The prepared-`case_dir` CFD path used to route SU2 through the same `bash -c`
chain as OpenFOAM, which (a) required bash/WSL on Windows and (b) mangled
`C:\\...` solver paths. SU2 is a self-contained binary needing no environment
sourcing, so it now runs via `solvers.run_argvs` — a direct subprocess chain.

Two tiers, both runnable on the no-FreeCAD lane:
  * **native runner + config resolution** (always): `run_argvs` chains argvs
    without any shell (Windows backslash paths survive verbatim, first failure
    stops the chain) and `su2_case_config` picks the right `.cfg` for a case.
  * **live solve** (RUN_HEAVY_SOLVES=1 + SU2 resolves, else SKIP): generate a
    tiny 2-D inviscid channel (structured quad `.su2` mesh + Euler `.cfg`) and
    run the real `SU2_CFD` through the exact helper the worker's
    `_openfoam_submit` SU2 branch calls — no bash anywhere in the chain.

Run:  python3 tests/test_su2_native.py
"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import solvers  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402


PY = sys.executable


# --- native runner (no solver needed) ------------------------------------------

def test_run_argvs_chains_without_shell():
    with tempfile.TemporaryDirectory() as d:
        rc, tail = solvers.run_argvs(d, [
            [PY, "-c", "print('first-ok')"],
            [PY, "-c", "print('second-ok')"],
        ])
        assert rc == 0, (rc, tail)
        assert "first-ok" in tail and "second-ok" in tail, tail


def test_run_argvs_stops_at_first_failure():
    with tempfile.TemporaryDirectory() as d:
        rc, tail = solvers.run_argvs(d, [
            [PY, "-c", "import sys; print('ran'); sys.exit(3)"],
            [PY, "-c", "print('never-reached')"],
        ])
        assert rc == 3, (rc, tail)
        assert "ran" in tail and "never-reached" not in tail, tail


def test_run_argvs_runs_in_case_dir():
    with tempfile.TemporaryDirectory() as d:
        rc, tail = solvers.run_argvs(d, [
            [PY, "-c", "import os; print(os.getcwd())"],
        ])
        assert rc == 0, (rc, tail)
        assert os.path.realpath(d) in os.path.realpath(tail.strip()), (d, tail)


def test_run_argvs_preserves_windows_paths():
    # the bash chain joined argvs into a POSIX string, eating backslashes; the
    # native runner must hand a Windows solver path through verbatim.
    exotic = r"C:\Program Files\SU2\bin\SU2_CFD.exe"
    with tempfile.TemporaryDirectory() as d:
        rc, tail = solvers.run_argvs(d, [
            [PY, "-c", "import sys; print(sys.argv[1])", exotic],
        ])
        assert rc == 0, (rc, tail)
        assert exotic in tail, tail


# --- SU2 config resolution ------------------------------------------------------

def test_su2_case_config_resolution():
    with tempfile.TemporaryDirectory() as d:
        # no config at all -> None (SU2 reports its own usage error)
        assert solvers.su2_case_config(d) is None
        # a lone .cfg is unambiguous
        Path(d, "channel_flow.cfg").write_text("% su2", encoding="utf-8")
        assert solvers.su2_case_config(d) == "channel_flow.cfg"
        # conventional name wins over other candidates
        Path(d, "config.cfg").write_text("% su2", encoding="utf-8")
        assert solvers.su2_case_config(d) == "config.cfg"
    with tempfile.TemporaryDirectory() as d:
        # several non-conventional candidates -> ambiguous -> None
        Path(d, "a.cfg").write_text("% su2", encoding="utf-8")
        Path(d, "b.cfg").write_text("% su2", encoding="utf-8")
        assert solvers.su2_case_config(d) is None


# --- live SU2 solve (gated) -----------------------------------------------------

def _write_channel_su2_mesh(path, nx=31, ny=11, length=3.0, height=1.0):
    """Structured quad mesh of a 2-D channel in SU2's native format: interior
    quads (VTK type 9) + four line markers (VTK type 3)."""
    def nid(i, j):
        return j * nx + i

    lines = ["NDIME= 2"]
    quads = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            quads.append((nid(i, j), nid(i + 1, j), nid(i + 1, j + 1), nid(i, j + 1)))
    lines.append(f"NELEM= {len(quads)}")
    lines += [f"9 {a} {b} {c} {d} {k}" for k, (a, b, c, d) in enumerate(quads)]
    lines.append(f"NPOIN= {nx * ny}")
    for j in range(ny):
        for i in range(nx):
            x = length * i / (nx - 1)
            y = height * j / (ny - 1)
            lines.append(f"{x:.16g} {y:.16g} {nid(i, j)}")
    markers = {
        "inlet":      [(nid(0, j), nid(0, j + 1)) for j in range(ny - 1)],
        "outlet":     [(nid(nx - 1, j), nid(nx - 1, j + 1)) for j in range(ny - 1)],
        "lower_wall": [(nid(i, 0), nid(i + 1, 0)) for i in range(nx - 1)],
        "upper_wall": [(nid(i, ny - 1), nid(i + 1, ny - 1)) for i in range(nx - 1)],
    }
    lines.append(f"NMARK= {len(markers)}")
    for tag, edges in markers.items():
        lines.append(f"MARKER_TAG= {tag}")
        lines.append(f"MARKER_ELEMS= {len(edges)}")
        lines += [f"3 {a} {b}" for a, b in edges]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


_CHANNEL_CFG = """\
% tiny 2-D inviscid channel — SU2 native-runner gate (issue #203)
SOLVER= EULER
MATH_PROBLEM= DIRECT
MACH_NUMBER= 0.1
AOA= 0.0
FREESTREAM_PRESSURE= 101325.0
FREESTREAM_TEMPERATURE= 288.15
REF_LENGTH= 1.0
REF_AREA= 1.0
MARKER_EULER= ( upper_wall, lower_wall )
MARKER_INLET= ( inlet, 288.7, 102035.0, 1.0, 0.0, 0.0 )
MARKER_OUTLET= ( outlet, 101325.0 )
NUM_METHOD_GRAD= GREEN_GAUSS
CFL_NUMBER= 10.0
ITER= 200
LINEAR_SOLVER= FGMRES
LINEAR_SOLVER_PREC= ILU
LINEAR_SOLVER_ERROR= 1E-6
LINEAR_SOLVER_ITER= 10
CONV_NUM_METHOD_FLOW= JST
JST_SENSOR_COEFF= ( 0.5, 0.02 )
TIME_DISCRE_FLOW= EULER_IMPLICIT
CONV_FIELD= RMS_DENSITY
CONV_RESIDUAL_MINVAL= -6
CONV_STARTITER= 10
MESH_FILENAME= channel.su2
MESH_FORMAT= SU2
TABULAR_FORMAT= CSV
CONV_FILENAME= history
RESTART_FILENAME= restart_flow.dat
OUTPUT_WRT_FREQ= 1000
SCREEN_OUTPUT= ( INNER_ITER, RMS_DENSITY, RMS_ENERGY )
OUTPUT_FILES= ( RESTART )
"""


def test_su2_live_channel_solve():
    if skip_heavy("SU2 channel"):
        return
    info = solvers.find_solver("su2")
    if not info["available"]:
        print("    SKIP — SU2_CFD not installed (see install_hint)")
        return
    with tempfile.TemporaryDirectory() as d:
        _write_channel_su2_mesh(os.path.join(d, "channel.su2"))
        Path(d, "channel.cfg").write_text(_CHANNEL_CFG, encoding="utf-8")
        # exactly what the worker's SU2 branch does: resolve the cfg, run native
        cfg = solvers.su2_case_config(d)
        assert cfg == "channel.cfg", cfg
        rc, tail = solvers.run_argvs(d, [[info["path"], cfg]])
        assert rc == 0, (rc, tail)
        assert os.path.isfile(os.path.join(d, "restart_flow.dat")), \
            (os.listdir(d), tail)


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")

    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
