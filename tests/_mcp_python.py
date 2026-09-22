"""Find an interpreter that can import `mcp`, for the tests that need one (#288, #317).

Not a test file: the leading underscore keeps it out of every runner's discovery.
Not named ``_interpreters``: that is a CPython 3.13+ stdlib module (PEP 734),
built in on Windows, so it wins over anything on sys.path.
A test script imports it as a sibling (``tests/`` is ``sys.path[0]`` when a script
under it is run directly) BEFORE importing ``mcp`` itself.

Why a resolver at all. A test that judged "can I import mcp" only by the
interpreter it happened to be started under was wrong on the self-hosted Linux
runner: ``tests/setup_local.sh`` makes ``.venv/bin/python3`` a symlink to
FreeCAD's *bundled* python — which carries numpy and Pillow but never ``mcp`` —
and ``run_all.sh`` prefers that interpreter, so the test skipped on a box whose
system python3 serves MCP fine. The skip was well-behaved and invisible in a
green run, and the lane covered nothing there for months (#317).

The rule, the same one ``ankusdrive/client.py`` follows for freecadcmd — derive
and probe, never hardcode:

  1. if THIS interpreter has ``mcp``, use it — true whether that is a venv, a
     conda env, a worktree, or a plain ``pip install -e .``;
  2. else re-exec into the first candidate that does: the repo venv (per-platform
     layout, so ``Scripts\\python.exe`` on Windows and ``bin/python3`` elsewhere),
     then ``python3``/``python`` from PATH;
  3. else tell the caller which interpreters were asked, so its SKIP can name them.

PINNING. Some callers are asking about ONE interpreter, not "any on this box":
``scripts/install-core.ps1`` runs test_mcp_boot.py to prove the venv it just
built can serve MCP. Re-execing into the system python there would answer the
wrong question and pass for a venv with no ``mcp`` in it. Such a caller sets
``ANKUSDRIVE_TEST_MCP_THIS_PYTHON=1``: no re-exec, and a missing ``mcp`` FAILS
(exit 1) instead of skipping, because the caller has said it must be there.

TIERED TESTS (#449). Most MCP-using tests also have tiers that need no ``mcp`` and
ran fine under whatever interpreter started them. They re-exec the whole script,
so a candidate must not break those tiers: they pass ``needs=HOST_DEPS`` and only
an interpreter carrying the package's full core dependency set qualifies. When
none does, they keep running their other tiers and SKIP the MCP ones with
``mcp_skip_reason()``.

Each candidate is PROBED (``-c "import mcp"``), never judged by its path — the same
reason ``ankusdrive/solvers.py`` probes solvers instead of trusting a directory.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Guards the step-2 re-exec against looping if a candidate that passed the probe
# still cannot import `mcp` once it is running the script.
_REEXEC_FLAG = "ANKUSDRIVE_TEST_MCP_REEXEC"
# Set by a caller that wants THIS interpreter judged, never swapped (see PINNING).
PIN_FLAG = "ANKUSDRIVE_TEST_MCP_THIS_PYTHON"

# pyproject's core `dependencies`, as import names. `pip install -e .` puts all three
# in any interpreter it puts `mcp` in, so requiring them costs a real install nothing
# and keeps a re-exec from landing a test's non-MCP tiers somewhere they can't run.
HOST_DEPS = ("mcp", "numpy", "PIL")


def venv_python(root: Path) -> Path:
    """The interpreter a venv rooted at *root* exposes, per platform."""
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")


def candidate_pythons():
    """Every interpreter worth asking, best first, de-duplicated.

    The repo venv is a candidate, not THE answer — on the self-hosted Linux runner
    it is FreeCAD's bundled python, which never has `mcp`.
    """
    # De-duplicate on the LITERAL path, never on Path.resolve(): a venv's
    # bin/python3 is a symlink to the base interpreter, so resolving collapses
    # every venv on the machine onto the same target and throws away the only
    # thing that distinguishes them — which packages they can import.
    seen, out = set(), []
    for cand in (Path(sys.executable),
                 venv_python(REPO / ".venv"),
                 *(Path(p) for p in (shutil.which("python3"), shutil.which("python")) if p)):
        key = os.path.abspath(str(cand))
        if key in seen or not cand.is_file():
            continue
        seen.add(key)
        out.append(cand)
    return out


def imports_mcp(python: Path, needs=("mcp",)) -> bool:
    """Ask an interpreter whether it imports every module in *needs*, rather than
    inferring from its path."""
    try:
        return subprocess.run(
            [str(python), "-c", "import " + ", ".join(needs)],
            capture_output=True, timeout=120,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_mcp_interpreter(script: str, needs=("mcp",)):
    """Make sure *script* runs under an interpreter that can import `mcp`.

    Returns ``None`` when this interpreter already can. Otherwise re-execs *script*
    (with the current argv) under the first candidate that imports everything in
    *needs*, and does not return. If none can, returns the list of interpreters asked, for the caller's
    SKIP message. Under ``ANKUSDRIVE_TEST_MCP_THIS_PYTHON=1`` a missing `mcp` exits 1.
    """
    if importlib.util.find_spec("mcp") is not None:
        return None
    if os.environ.get(PIN_FLAG) == "1":
        print(f"  FAIL {Path(script).name} — {PIN_FLAG}=1 pins the check to "
              f"{sys.executable}, and the `mcp` package is not importable there. "
              "Install the host deps into it (pip install -e .).")
        raise SystemExit(1)
    tried = candidate_pythons()
    if not os.environ.get(_REEXEC_FLAG):
        here = os.path.abspath(sys.executable)
        for cand in tried:
            if os.path.abspath(str(cand)) != here and imports_mcp(cand, needs):
                os.environ[_REEXEC_FLAG] = "1"
                sys.stdout.flush()
                os.execv(str(cand), [str(cand), str(Path(script).resolve()), *sys.argv[1:]])
    return tried


def mcp_skip_reason(needs=HOST_DEPS) -> str:
    """The SKIP text for a tier that found no qualifying interpreter: names what was
    required and every interpreter asked, not just this one."""
    return (f"`mcp` not importable here, and no interpreter imports {', '.join(needs)} "
            "(tried: " + ", ".join(str(c) for c in candidate_pythons()) + ")")
