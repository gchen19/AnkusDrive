"""Packaging contract (issue #249) — the BUILT distribution must carry its data.

Every other suite in this repo tests the source tree. That is exactly how the ISO
standards corpus went unshipped for the project's whole life: ``pyproject.toml``
declared ``package-data = ["worker.py", "analysis/materials/*.json"]`` and omitted
``analysis/standards/*.json``, so ``threads.json`` / ``bearings.json`` /
``stock.json`` never entered a wheel and ``thread()`` / ``bearing()`` / ``pipe()``
/ ``sheet_gauge()`` raised ``FileNotFoundError`` for everyone who pip-installed.
Running the tests from a git checkout could never see it — the files were right
there on disk. Only the artifact tells the truth, so this suite builds one.

What it asserts, and why the expected list is DERIVED rather than written down:

  * **loader-derived** — every path the package constructs from ``__file__`` at
    import/run time (``Path(__file__).with_name("threads.json")``,
    ``Path(__file__).resolve().parent / "worker.py"``,
    ``os.path.join(os.path.dirname(__file__), "fsi_template")``) is recovered by
    walking each module's AST. A directory reference expands to every file under
    it. This is literally "what the code reads", so a corpus added tomorrow is
    covered the moment its loader is written — nobody has to remember this file.
  * **tree sweep** — every file that lives under ``driftpin/`` and is not a
    ``.pyc``/``__pycache__`` artifact must land in both the wheel and the sdist.
    A superset of the above, and the backstop for data a loader reaches by a
    computed name the AST scan cannot see.

A hardcoded list would rot in precisely the way ``package-data`` rotted, which is
the bug being prevented; that is why neither layer contains one.

``worker.py`` is included deliberately even though it is a ``.py`` file inside the
package. It is never imported by the host interpreter — it opens with
``import FreeCAD``, which only resolves inside ``freecadcmd``'s bundled Python —
so ``driftpin.client`` spawns it **by filesystem path** (``WORKER_SCRIPT``). It is
data that happens to be Python, its presence is a path fact rather than an import
fact, and the loader-derived layer picks it up from that path reference for free.

Two-sided, and that half is not decorative: a packaging test that cannot fail is
as useless as the absent one that let this through. The suite rebuilds the project
once more from a temp copy whose ``package-data`` has been stripped of the
standards glob and asserts the very same comparison reports those files missing.
Pass ``--no-negative`` to skip that second build.

Needs no FreeCAD, no numpy, no network — just a PEP 517 build backend. If none is
importable it degrades to ``{ok: false, install: ...}`` and exits 0, the way the
optional-solver suites do, rather than failing the lane.

Run: python3 tests/test_package_data.py [--no-negative]
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "driftpin"

_PASS = _FAIL = 0

# Build inputs setuptools needs to reproduce the distribution from a scratch copy:
# the config, the package itself, and the two files pyproject references by name
# (readme, license). Nothing else in the repo affects what lands in the artifact.
_PROJECT_FILES = ("pyproject.toml", "README.md", "LICENSE")

# Never shipped, never expected: bytecode and editor/OS litter.
_IGNORED_NAMES = {".DS_Store"}
_IGNORED_DIRS = {"__pycache__"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _ok(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}  {detail}")


def _skip(reason, install):
    print(f"  SKIP: {reason}")
    print(f"        {{'ok': False, 'install': {install!r}}}")


# --- deriving the expected file list ------------------------------------------
#
# Layer 1: recover the paths the code builds out of __file__. Three spellings are
# in use across driftpin/ and all three reduce to "a filename literal sitting next
# to a __file__-derived directory":
#
#     Path(__file__).with_name("threads.json")
#     Path(__file__).resolve().parent / "worker.py"
#     os.path.join(os.path.dirname(__file__), "fsi_template")
#
# so the scan looks for a string constant in a `with_name` call, on the right of a
# `/`, or among the arguments of an `os.path.join` — in each case only when the
# expression it hangs off contains __file__. Anything computed at runtime (a
# variable filename, a repo root two dirnames up) yields no constant and is simply
# not claimed; the tree sweep below is the backstop for those.

def _mentions_file(node):
    return any(isinstance(n, ast.Name) and n.id == "__file__" for n in ast.walk(node))


def _str_const(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _referenced_segments(source):
    """Path segments a module derives from its own __file__, as relative strings."""
    segments = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "with_name" and _mentions_file(node.func.value):
                segments += [a.value for a in node.args if _str_const(a)]
            elif node.func.attr == "join" and any(_mentions_file(a) for a in node.args):
                parts = [a.value for a in node.args if _str_const(a)]
                if parts:
                    segments.append(os.path.join(*parts))
        elif (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
              and _mentions_file(node.left) and _str_const(node.right)):
            segments.append(node.right.value)
    return segments


def _expand(path):
    """A referenced file -> itself; a referenced directory -> everything in it."""
    if path.is_file():
        return [path]
    if path.is_dir():
        return [p for p in sorted(path.rglob("*")) if p.is_file() and _shippable(p)]
    return []


def _shippable(path):
    if path.name in _IGNORED_NAMES or path.suffix in _IGNORED_SUFFIXES:
        return False
    return not (_IGNORED_DIRS & set(path.parts))


def loader_derived_paths(pkg=PKG):
    """Files the package reads back at runtime, mined from its own source."""
    found = set()
    for module in sorted(pkg.rglob("*.py")):
        if not _shippable(module):
            continue
        try:
            source = module.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            continue
        for segment in _referenced_segments(source):
            target = (module.parent / segment).resolve()
            # Only claim things that live inside the package and exist today;
            # a "repo root" reference resolves outside and is a dev-tree path.
            if pkg.resolve() not in target.parents and target != pkg.resolve():
                continue
            for hit in _expand(target):
                found.add(hit.resolve().relative_to(pkg.resolve().parent).as_posix())
    return found


def tree_paths(pkg=PKG):
    """Every file living under driftpin/ that a distribution ought to carry."""
    return {
        p.relative_to(pkg.parent).as_posix()
        for p in pkg.rglob("*") if p.is_file() and _shippable(p)
    }


# --- building the distribution ------------------------------------------------

def _have(module):
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def _local_setuptools_is_usable():
    """True if THIS interpreter's setuptools satisfies pyproject's build-system
    floor. Read from pyproject rather than pinned here, so bumping the floor can't
    leave the test quietly building with a backend too old to honour the config
    (recursive `**/*` package-data, for one) and reporting a false failure."""
    if not _have("setuptools"):
        return False
    import setuptools
    requires = re.search(r"setuptools\s*>=\s*([\d.]+)",
                         (REPO / "pyproject.toml").read_text(encoding="utf-8"))
    if not requires:
        return True
    def key(version):
        return tuple(int(p) for p in re.findall(r"\d+", version)[:3])

    return key(setuptools.__version__) >= key(requires.group(1))


def build(project_dir, outdir, sdist=True):
    """Build project_dir into outdir. Returns (wheel, sdist_or_None) or None.

    Strategies, best first: `python -m build` (canonical, produces both), the PEP
    517 backend called directly (no venv, no network — what the system python has),
    then `pip wheel --no-deps` (wheel only). None available -> None, and the caller
    degrades gracefully.
    """
    project_dir, outdir = str(project_dir), str(outdir)
    if _have("build"):
        cmd = [sys.executable, "-m", "build", "--outdir", outdir]
        if not sdist:
            cmd.append("--wheel")
        # Isolation would pip-install setuptools from PyPI; skip it when the
        # interpreter already satisfies the build-system requirement.
        if _local_setuptools_is_usable():
            cmd.append("--no-isolation")
        cmd.append(project_dir)
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            return _collect(outdir)
    if _local_setuptools_is_usable():
        # One hook per process on purpose: setuptools' command objects keep state,
        # and calling build_sdist after build_wheel in the SAME interpreter
        # silently produces no tarball (exit 0, empty dist dir).
        hooks = ["build_wheel"] + (["build_sdist"] if sdist else [])
        script = (
            "import os,sys;os.chdir(sys.argv[1]);"
            "from setuptools import build_meta as b;"
            "getattr(b, sys.argv[3])(sys.argv[2])"
        )
        runs = [
            subprocess.run(
                [sys.executable, "-c", script, project_dir, outdir, hook],
                capture_output=True,
            )
            for hook in hooks
        ]
        if all(r.returncode == 0 for r in runs):
            return _collect(outdir)
    run = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", outdir, project_dir],
        capture_output=True,
    )
    if run.returncode == 0:
        return _collect(outdir)
    return None


def _collect(outdir):
    out = Path(outdir)
    wheels = sorted(out.glob("*.whl"))
    sdists = sorted(out.glob("*.tar.gz"))
    if not wheels:
        return None
    return wheels[-1], (sdists[-1] if sdists else None)


def wheel_members(path):
    with zipfile.ZipFile(path) as zf:
        return {n for n in zf.namelist() if not n.startswith("driftpin-")}


def sdist_members(path):
    # encoding= here is the member-NAME codec, not a text mode; pinning it keeps
    # the listing identical on a cp1252 Windows host (and satisfies issue #204's
    # contract check).
    with tarfile.open(path, "r:gz", encoding="utf-8") as tf:
        # Strip the "driftpin-<version>/" prefix every sdist wraps its tree in.
        return {n.split("/", 1)[1] for n in tf.getnames() if "/" in n}


def missing_from(expected, members):
    """The one comparison every assertion below runs through — including the
    negative control, so what CI proves can fail is the same code that passes."""
    return sorted(set(expected) - set(members))


# --- the checks ---------------------------------------------------------------

def check_derivation_is_not_empty(derived, swept):
    """A discovery bug would silently make every later assertion vacuous."""
    _ok("loader scan finds the __file__-derived corpora", len(derived) >= 5,
        f"found {len(derived)}")
    _ok("tree sweep finds files beyond the loaders", len(swept) > len(derived),
        f"swept {len(swept)} vs derived {len(derived)}")
    _ok("every loader-derived path is a real file in the source tree",
        all((PKG.parent / p).is_file() for p in derived))
    _ok("worker.py is claimed by the loader scan (spawned by path, never imported)",
        "driftpin/worker.py" in derived)
    _ok("the standards corpus is claimed by the loader scan",
        {"driftpin/analysis/standards/threads.json",
         "driftpin/analysis/standards/bearings.json",
         "driftpin/analysis/standards/stock.json",
         "driftpin/analysis/standards/catalog.json"} <= derived)


def check_wheel_carries_runtime_data(derived, swept, members):
    gone = missing_from(derived, members)
    _ok("wheel carries every file the code loads from __file__", not gone,
        f"missing from wheel: {gone}")
    gone = missing_from(swept, members)
    _ok("wheel carries every non-bytecode file under driftpin/", not gone,
        f"missing from wheel: {gone}")


def check_sdist_carries_runtime_data(derived, swept, members):
    if members is None:
        print("  SKIP sdist checks (backend produced a wheel only)")
        return
    gone = missing_from(derived, members)
    _ok("sdist carries every file the code loads from __file__", not gone,
        f"missing from sdist: {gone}")
    gone = missing_from(swept, members)
    _ok("sdist carries every non-bytecode file under driftpin/", not gone,
        f"missing from sdist: {gone}")


def check_comparison_detects_a_drop(members):
    """Cheap always-on sanity check on the comparator itself: pull one corpus
    file out of the artifact listing and the same call must name it."""
    victim = "driftpin/analysis/standards/threads.json"
    trimmed = set(members) - {victim}
    _ok("the comparison names a corpus file absent from the artifact",
        missing_from([victim], trimmed) == [victim])
    _ok("the comparison stays quiet when nothing is absent",
        missing_from([victim], members) == [])


# --- negative control: a deliberately broken build must fail the suite ---------

_PKG_DATA = re.compile(r"(?s)\[tool\.setuptools\.package-data\].*?(driftpin\s*=\s*)\[(.*?)\]")


def _trim_package_data(pyproject_text, drop="standards"):
    """Delete the standards glob from [tool.setuptools.package-data], recreating
    the exact pyproject bug #244 fixed."""
    match = _PKG_DATA.search(pyproject_text)
    if not match:
        return None
    entries = re.findall(r'"([^"]+)"', match.group(2))
    kept = [e for e in entries if drop not in e]
    if len(kept) == len(entries):
        return None
    body = ", ".join(f'"{e}"' for e in kept)
    return (pyproject_text[:match.start(1)]
            + f"{match.group(1)}[{body}]"
            + pyproject_text[match.end(0):])


def _copy_project(dest):
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PKG, dest / "driftpin",
                    ignore=shutil.ignore_patterns(*_IGNORED_DIRS, "*.pyc"))
    for name in _PROJECT_FILES:
        shutil.copy2(REPO / name, dest / name)
    return dest


def check_trimmed_config_fails(derived, tmp):
    """Build a copy whose package-data has lost the standards glob. The identical
    assertion that passes above must now report the corpus missing — otherwise
    this whole suite is decorative."""
    project = _copy_project(Path(tmp) / "trimmed")
    pyproject = project / "pyproject.toml"
    trimmed = _trim_package_data(pyproject.read_text(encoding="utf-8"))
    if trimmed is None:
        _ok("negative control could edit package-data", False,
            "no standards glob found in [tool.setuptools.package-data]")
        return
    pyproject.write_text(trimmed, encoding="utf-8")

    built = build(project, Path(tmp) / "trimmed-dist", sdist=False)
    if built is None:
        print("  SKIP negative control (build backend unavailable)")
        return
    members = wheel_members(built[0])
    gone = missing_from(derived, members)
    _ok("a wheel built without the standards glob FAILS the wheel assertion",
        bool(gone), "trimmed build still satisfied the check — the suite cannot fail")
    _ok("and it fails naming the standards corpus specifically",
        any("analysis/standards/" in p for p in gone), f"reported: {gone}")
    _ok("while unrelated corpora still pass (the control is surgical)",
        "driftpin/analysis/materials/fcmat.json" in members)


def main():
    negative = "--no-negative" not in sys.argv

    derived = loader_derived_paths()
    swept = tree_paths()

    print("== Expected file list, derived from the source (no hardcoded corpus) ==")
    check_derivation_is_not_empty(derived, swept)
    for path in sorted(derived):
        print(f"       loader-derived: {path}")

    with tempfile.TemporaryDirectory(prefix="driftpin-pkg-") as tmp:
        print("\n== Building the distribution (one build, reused by every check) ==")
        project = _copy_project(Path(tmp) / "src")
        built = build(project, Path(tmp) / "dist")
        if built is None:
            _skip("no PEP 517 build backend available in this interpreter "
                  f"({sys.executable})", "pip install build")
            print(f"\n{_PASS} passed, {_FAIL} failed (packaging checks skipped)")
            return 1 if _FAIL else 0

        wheel, sdist = built
        print(f"  built {wheel.name}" + (f" + {sdist.name}" if sdist else ""))
        wmembers = wheel_members(wheel)
        smembers = sdist_members(sdist) if sdist else None

        print("\n== The wheel carries its runtime data ==")
        check_wheel_carries_runtime_data(derived, swept, wmembers)
        print("\n== The sdist carries its runtime data ==")
        check_sdist_carries_runtime_data(derived, swept, smembers)
        print("\n== The comparison itself is two-sided ==")
        check_comparison_detects_a_drop(wmembers)

        if negative:
            print("\n== Negative control: a trimmed package-data must break it ==")
            check_trimmed_config_fails(derived, tmp)
        else:
            print("\n  SKIP negative control (--no-negative)")

    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
