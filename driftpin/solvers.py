"""External-solver discovery + graceful degradation — the P2 twin of the renderer
provisioning glue.

Pure-Python, FreeCAD-free (the same property ``jobs.py`` has), so the degradation
contract is testable on the no-FreeCAD CI lane. This is the solver-side mirror of
``worker.py``'s ``_RENDERERS`` registry + ``_find_renderer_exec`` /
``_resolve_renderer_exec`` / ``render_capabilities``: the heavy P2 families (CFD,
multibody dynamics, topology optimization, transient/radiation thermal, optics)
ride on external solvers that are *not* vendored in the repo, so every family must
discover its solver, and degrade to a clean structured dict — never an import
crash — when it is absent.

Two solver shapes:
  * **wheel** — a pip-installable Python package (PyBullet, MuJoCo, topology libs,
    rayoptics). "Available" means the module imports. Installed via an optional
    extra: ``pip install driftpin[mbd]``.
  * **binary** — an external executable resolved cross-platform exactly the way the
    renderers are: ``DRIFTPIN_<SOLVER>_PATH`` env override -> PATH (``shutil.which``)
    -> common per-OS install dirs. OpenFOAM / Elmer / SU2 ship via apt/conda, are
    documented (not vendored), and only *execute* on the provisioned/self-hosted
    runner.

The degradation contract (the part that gates every PR, no solver needed):
  ``require_solver(name)`` returns ``{ok: True, ...}`` when the solver resolves, or
  ``{ok: False, reason: "solver not installed", install: <hint>, solver: name}``
  when it does not. A family's ``*_submit`` calls this first and returns the dict
  verbatim on a miss, so it never raises on a missing solver.
``capabilities()`` reports what resolves right now (the ``solve_capabilities`` tool).
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil

from driftpin import config as _config

# Registry of the P2 external solvers, keyed by the name agents pass (and the
# DRIFTPIN_<NAME>_PATH env override is the upper-cased key). Ordered by the
# milestone sequence in docs/archive/SIMULATION_P2_KICKOFF.md. Each family lists every
# solver that can satisfy it; a family is reachable if ANY of its solvers resolves
# (e.g. MBD is happy with PyBullet OR MuJoCo).
_SOLVERS: dict = {
    # --- M2 MBD: pip wheels, the lightest external dep -----------------------
    "pybullet": {
        "kind": "wheel",
        "family": "mbd",
        "extra": "mbd",
        "modules": ("pybullet",),
        "install_hint": "pip install 'driftpin[mbd]'  (pulls pybullet), "
                        "or: pip install pybullet",
    },
    "mujoco": {
        "kind": "wheel",
        "family": "mbd",
        "extra": "mbd",
        "modules": ("mujoco",),
        "install_hint": "pip install 'driftpin[mbd]'  (pulls mujoco), "
                        "or: pip install mujoco",
    },
    # --- M3 topology: pip wheels, returns geometry ---------------------------
    "topopt": {
        "kind": "wheel",
        "family": "topology",
        "extra": "topology",
        "modules": ("topopt", "solidspy"),
        "install_hint": "pip install 'driftpin[topology]'  (pulls topopt/solidspy), "
                        "or: pip install topopt solidspy",
    },
    # --- M4 transient/radiation thermal: Elmer (apt/conda, not vendored) -----
    "elmer": {
        "kind": "binary",
        "family": "thermal_transient",
        "extra": None,                       # system package, not a pip extra
        "binaries": ("ElmerSolver", "ElmerSolver_mpi"),
        "dirs": {
            "Linux":   ("/usr/bin", "/usr/local/bin", "/opt/elmer/bin"),
            "Darwin":  ("/usr/local/bin", "/opt/homebrew/bin",
                        "/Applications/Elmer.app/Contents/MacOS"),
            "Windows": (r"C:\Program Files\Elmer\bin",),
        },
        "install_hint": "'apt install elmerfem-csc' (Linux), "
                        "'conda install -c conda-forge elmer', or a build from "
                        "https://www.elmerfem.org/ — then ensure ElmerSolver is on "
                        "PATH or set DRIFTPIN_ELMER_PATH",
    },
    # --- M5 CFD: OpenFOAM / SU2 (apt/conda, not vendored), heaviest, last ----
    "openfoam": {
        "kind": "binary",
        "family": "cfd",
        "extra": None,
        # foamRun is the modern (openfoam.org v11+) unified app; simpleFoam is the
        # classic steady incompressible solver; blockMesh proves the toolchain.
        "binaries": ("foamRun", "simpleFoam", "blockMesh"),
        "dirs": {
            "Linux":   ("/usr/bin", "/usr/local/bin",
                        "/opt/openfoam/platforms/linux64GccDPInt32Opt/bin"),
            "Darwin":  ("/usr/local/bin", "/opt/homebrew/bin"),
            "Windows": (r"C:\Program Files\OpenFOAM\bin",),
        },
        "install_hint": "OpenFOAM via apt (openfoam.org / openfoam.com repos), "
                        "conda ('conda install -c conda-forge openfoam'), or the "
                        "FreeCAD CfdOF workbench — then ensure foamRun/simpleFoam is "
                        "on PATH or set DRIFTPIN_OPENFOAM_PATH",
        # installed-but-unwired probe (issue #177): foamRun/simpleFoam only land on
        # PATH *after* an etc/bashrc is sourced, so a bare shell reports the binary
        # absent even when OpenFOAM is fully installed. A standard-location etc/bashrc
        # is the evidence that it is installed but not wired into this shell.
        "unwired": {
            "probe": "bashrc",
            "globs": ("/usr/share/openfoam/etc/bashrc",
                      "/usr/lib/openfoam/openfoam*/etc/bashrc",
                      "/opt/openfoam*/etc/bashrc", "/opt/OpenFOAM*/etc/bashrc"),
            "hint": "set DRIFTPIN_OPENFOAM_BASHRC={found} (or source it)",
        },
    },
    "su2": {
        "kind": "binary",
        "family": "cfd",
        "extra": None,
        "binaries": ("SU2_CFD",),
        "dirs": {
            "Linux":   ("/usr/local/bin", "/usr/bin", "/opt/SU2/bin"),
            "Darwin":  ("/usr/local/bin", "/opt/homebrew/bin"),
            "Windows": (r"C:\Program Files\SU2\bin",),
        },
        "install_hint": "download SU2 from https://su2code.github.io/download.html "
                        "(provides SU2_CFD) and put it on PATH, or set "
                        "DRIFTPIN_SU2_PATH",
    },
    # --- optics: pip wheels (the `optics` extra) -----------------------------
    # Two lanes (see memory optics-library-selection). SEQUENTIAL imaging/lens
    # design + optimization runs IN-PROCESS on optiland (MIT). NON-SEQUENTIAL
    # tracing through real STL solids runs on KrakenOS, which is GPL-3.0 and is
    # therefore invoked ONLY out-of-process via driftpin/optics_gpl_runner.py —
    # find_spec discovery below merely checks the file exists, it does not import
    # (or link) the GPL code, so the copyleft boundary stays intact.
    "rayoptics": {
        "kind": "wheel",
        "family": "optics",
        "extra": "optics",
        "modules": ("rayoptics", "optiland"),
        "install_hint": "pip install 'driftpin[optics]'  (pulls rayoptics), "
                        "or: pip install rayoptics optiland",
    },
    "optiland": {
        "kind": "wheel",
        "family": "optics",
        "extra": "optics",
        "modules": ("optiland",),
        "install_hint": "pip install 'driftpin[optics]'  (pulls optiland), "
                        "or: pip install optiland",
    },
    "kraken": {
        "kind": "wheel",
        "family": "optics_nonseq",
        "extra": "optics_gpl",
        "modules": ("KrakenOS",),
        # GPL-3.0: never imported in-process; run via optics_gpl_runner subprocess.
        "license": "GPL-3.0",
        "isolation": "subprocess",
        "install_hint": "pip install 'driftpin[optics_gpl]'  (pulls KrakenOS, GPL-3.0; "
                        "run out-of-process only), or: pip install KrakenOS 'setuptools<81'",
    },
    # --- exterior acoustics: Bempp BEM (MIT, but meshio>=4 clashes with solidspy) ---
    # Bempp is MIT — NOT a license boundary. The subprocess isolation is purely a
    # DEPENDENCY clash: bempp needs meshio>=4 (cells_dict) while the shared venv pins
    # meshio==3.0 for solidspy (driftpin/analysis/topology.py). So bempp lives in a
    # DEDICATED venv (.venv-bempp) and is invoked out-of-process via
    # driftpin/bempp_runner.py; the worker resolves that interpreter via
    # _bempp_python() (the find_spec probe below merely reports installability).
    "bempp": {
        "kind": "wheel",
        "family": "acoustics_bem",
        "extra": "acoustics_bem",
        "modules": ("bempp_cl",),
        # MIT — no copyleft. The subprocess is for the meshio>=4 dependency clash.
        "license": "MIT",
        "isolation": "subprocess",
        "install_hint": "Bempp needs meshio>=4 (cells_dict), which clashes with the "
                        "shared venv's meshio==3 (solidspy). Install it in a DEDICATED "
                        "venv and run out-of-process: python3 -m venv .venv-bempp && "
                        ".venv-bempp/bin/pip install bempp-cl gmsh 'meshio>=5'  "
                        "(scripts/install-solvers.sh acoustics_bem); then point "
                        "DRIFTPIN_BEMPP_PYTHON at that venv's python.",
        # installed-but-unwired probe (issue #177): bempp lives in the dedicated
        # .venv-bempp (meshio>=5), not this interpreter; the venv beside the repo is
        # the evidence it is installed but DRIFTPIN_BEMPP_PYTHON is not set here.
        "unwired": {
            "probe": "venv",
            "venv": ".venv-bempp",
            "hint": "set DRIFTPIN_BEMPP_PYTHON={found}/bin/python3",
        },
    },
    # --- granular DEM: YADE (GPL-3.0, source-built, NOT a pip wheel) ----------
    # YADE is GPL-3.0 and ships no PyPI/conda-noble wheel, so it is source-built
    # (scripts/install-solvers.sh dem_gpl) and driven ONLY out-of-process: the
    # worker shells out to the `yade` EXECUTABLE running driftpin/dem_gpl_runner.py
    # (sentinel-JSON over stdin/stdout). DriftPin never imports YADE in-process, so
    # the copyleft does not link into the permissive code — the same arm's-length
    # isolation used for the GPL Elmer/OpenFOAM binaries and the KrakenOS optics
    # runner. Resolved as a BINARY (DRIFTPIN_YADE_PATH env → PATH → the documented
    # ~/opt/yade/bin source-build prefix); the worker also honors a bare
    # DRIFTPIN_YADE override.
    "yade": {
        "kind": "binary",
        "family": "dem",
        "extra": "dem_gpl",
        # GPL-3.0: never imported in-process; run via dem_gpl_runner subprocess.
        "license": "GPL-3.0",
        "isolation": "subprocess",
        "binaries": ("yade", "yade-batch"),
        "dirs": {
            "Linux":   (os.path.expanduser("~/opt/yade/bin"),
                        "/usr/bin", "/usr/local/bin", "/opt/yade/bin"),
            "Darwin":  ("/usr/local/bin", "/opt/homebrew/bin"),
            "Windows": (r"C:\Program Files\yade\bin",),
        },
        "install_hint": "source-build YADE (GPL-3.0; not on PyPI/conda-noble): "
                        "scripts/install-solvers.sh dem_gpl  (cmake build into "
                        "~/opt/yade), or your distro's 'yade'/'yade-dem' package; "
                        "then ensure `yade` is on PATH or set DRIFTPIN_YADE / "
                        "DRIFTPIN_YADE_PATH. Driven out-of-process only via "
                        "driftpin/dem_gpl_runner.py.",
    },
    # --- full-wave EM: openEMS FDTD (GPL-3.0, source-built, NOT a pip wheel) ---
    # Like KrakenOS, openEMS is GPL-3.0 and is therefore invoked ONLY out-of-process
    # via driftpin/em_fullwave_gpl_runner.py — DriftPin never imports openEMS/CSXCAD
    # in-process, so the copyleft does not link into DriftPin's permissive code. The
    # python bindings (openEMS, CSXCAD) live in a DEDICATED venv (.venv-openems);
    # the worker resolves that interpreter via _em_fullwave_gpl_python() (the
    # find_spec probe below merely reports installability, it does not import).
    "openems": {
        "kind": "wheel",
        "family": "em_fullwave",
        "extra": "em_gpl",
        "modules": ("openEMS",),
        # GPL-3.0: never imported in-process; run via em_fullwave_gpl_runner subprocess.
        "license": "GPL-3.0",
        "isolation": "subprocess",
        "install_hint": "source-build openEMS (GPL-3.0; not on PyPI/conda): "
                        "scripts/install-solvers.sh em_gpl  (clones openEMS-Project, "
                        "runs update_openEMS.sh --python into a dedicated venv); then "
                        "point DRIFTPIN_OPENEMS_PYTHON at that venv's python. Run "
                        "out-of-process only via driftpin/em_fullwave_gpl_runner.py.",
        # installed-but-unwired probe (issue #177): openEMS lives in a dedicated
        # .venv-openems, NOT this interpreter, so the find_spec probe above reports it
        # absent in a bare shell. The venv sitting beside the repo is the evidence
        # that it is installed but DRIFTPIN_OPENEMS_PYTHON is not set in this shell.
        "unwired": {
            "probe": "venv",
            "venv": ".venv-openems",
            "hint": "set DRIFTPIN_OPENEMS_PYTHON={found}/bin/python3",
        },
    },
    # --- FSI coupling: preCICE OpenFOAM<->CalculiX (LGPL core, source adapters) -
    # The partitioned fluid-structure-interaction family. preCICE (LGPL-3.0) is the
    # coupling library; it is only ever invoked OUT-OF-PROCESS (the two heavy
    # solvers — OpenFOAM pimpleFoam and the preCICE-enabled ccx_preCICE — run as
    # their own subprocesses, the same arm's-length boundary OpenFOAM/Elmer already
    # use), so the copyleft never links into DriftPin's permissive code. The stack
    # is version-sensitive and NOT a pip wheel: it needs (a) libprecice (LGPL,
    # conda-forge `precice`/`pyprecice` OR source-built serial — see below), (b) the
    # `precice/calculix-adapter` built against CalculiX 2.20 source + SPOOLES +
    # ARPACK, producing the `ccx_preCICE` binary, and (c) the
    # `precice/openfoam-adapter` function-object lib built with `wmake` against an
    # OpenFOAM with dev headers (ESI openfoam2512-dev). scripts/install-solvers.sh
    # fsi documents the full build; this entry RESOLVES the stack and degrades
    # cleanly — it keys on the `ccx_preCICE` binary (the linchpin that proves the
    # adapter chain built) and the helper resolvers below find the lib/OF dirs.
    # NOTE on the MPI gotcha: conda's libprecice is MPI-enabled (libmpi.so.12,
    # MPICH); a non-MPI standalone run alongside OpenFOAM's OpenMPI (libmpi.so.40)
    # double-loads MPI and segfaults in MPI_Comm_rank. The serial source build
    # (PRECICE_FEATURE_MPI_COMMUNICATION=OFF) avoids this; both adapters must then
    # link that serial libprecice (and the ccx adapter built with gcc/gfortran, not
    # mpicc). install-solvers.sh fsi captures exactly this.
    "precice": {
        "kind": "binary",
        "family": "fsi",
        "extra": "fsi",
        # LGPL-3.0 core; invoked out-of-process only (never imported in-process).
        "license": "LGPL-3.0",
        "isolation": "subprocess",
        # The ccx_preCICE binary is the linchpin: it exists only if the CalculiX
        # adapter built against libprecice + CalculiX source, so it proves the
        # whole stack. DRIFTPIN_PRECICE_PATH / DRIFTPIN_CCX_PRECICE override.
        "binaries": ("ccx_preCICE",),
        "dirs": {
            "Linux":   (os.path.expanduser("~/calculix-adapter/bin"),
                        "/usr/local/bin", "/usr/bin",
                        os.path.expanduser("~/opt/calculix-adapter/bin")),
            "Darwin":  ("/usr/local/bin", "/opt/homebrew/bin"),
            "Windows": (),
        },
        "install_hint": "build the preCICE FSI stack (LGPL core + two source "
                        "adapters; not a pip wheel): scripts/install-solvers.sh fsi "
                        "— installs serial libprecice (MPI off), builds "
                        "precice/calculix-adapter (ccx_preCICE, CalculiX 2.20 src + "
                        "SPOOLES + ARPACK) and precice/openfoam-adapter (wmake vs "
                        "openfoam2512-dev). Then set DRIFTPIN_CCX_PRECICE, "
                        "DRIFTPIN_PRECICE_LIB, DRIFTPIN_OPENFOAM_ADAPTER_LIB and "
                        "DRIFTPIN_OPENFOAM_BASHRC (the v2512 bashrc). Driven out-of-"
                        "process only via driftpin/analysis/fsi_case.py.",
        # installed-but-unwired probe (issue #177): the ccx_preCICE binary is the
        # linchpin, but the two source-built adapter libraries (libprecice /
        # libpreciceAdapterFunctionObject) are the evidence the stack was partly built
        # even when ccx_preCICE is not resolvable / the DRIFTPIN_* envs are unset.
        "unwired": {
            "probe": "fsi_adapter",
            "hint": "FSI stack partly built at {found}; finish the build "
                    "(scripts/install-solvers.sh fsi) and set DRIFTPIN_CCX_PRECICE / "
                    "DRIFTPIN_PRECICE_LIB / DRIFTPIN_OPENFOAM_ADAPTER_LIB / "
                    "DRIFTPIN_FSI_OPENFOAM_BASHRC",
        },
    },
    # --- Sprint 4 follow-on: slicer CLI (apt/AppImage, not vendored) ----------
    "prusaslicer": {
        "kind": "binary",
        "family": "slicing",
        "extra": None,
        # prusa-slicer is the apt/AppImage binary; the console build and the
        # capitalised AppImage name cover the other common installs.
        "binaries": ("prusa-slicer", "prusa-slicer-console", "PrusaSlicer"),
        "dirs": {
            "Linux":   ("/usr/bin", "/usr/local/bin",
                        os.path.expanduser("~/Applications")),
            "Darwin":  ("/usr/local/bin", "/opt/homebrew/bin",
                        "/Applications/PrusaSlicer.app/Contents/MacOS"),
            "Windows": (r"C:\Program Files\Prusa3D\PrusaSlicer",),
        },
        "install_hint": "'apt install prusa-slicer' (Linux), the PrusaSlicer "
                        "AppImage/installer from https://www.prusa3d.com/prusaslicer/ "
                        "— then ensure prusa-slicer is on PATH or set "
                        "DRIFTPIN_PRUSASLICER_PATH",
    },
    # --- structural CalculiX (plain ccx; the warpage thermo-elastic post-step) -
    # The bare `ccx` binary (GPL, invoked out-of-process only — never imported),
    # distinct from the preCICE-patched `ccx_preCICE` above. The molding warpage
    # path (#113 Part B) writes a thermo-elastic .inp deck and runs it directly.
    # FreeCAD ships a bundled ccx too; PATH / the apt `calculix-ccx` cover the rest.
    "calculix": {
        "kind": "binary",
        "family": "warpage",
        "extra": None,
        "license": "GPL-2.0",
        "isolation": "subprocess",
        "binaries": ("ccx", "ccx_2.22", "ccx_2.21", "ccx_2.20", "ccx_2.19", "CalculiX"),
        "dirs": {
            "Linux":   ("/usr/bin", "/usr/local/bin",
                        os.path.expanduser("~/opt/CalculiX/bin")),
            "Darwin":  ("/usr/local/bin", "/opt/homebrew/bin"),
            "Windows": (r"C:\Program Files\CalculiX\bin",),
        },
        # FreeCAD SHIPS ccx (+ gmsh) in its own bin/ on every OS, so the warpage family
        # resolves with no separate CalculiX install — the discovery below adds FreeCAD's
        # bundled bin after the standard install dirs. (Verified on Windows: FreeCAD 1.1
        # bundles ccx 2.22 in `…\FreeCAD 1.1\bin\ccx.exe`.)
        "freecad_bundled": True,
        # NB: there is NO `calculix` formula in core Homebrew (issue #192) — on macOS the
        # bundled FreeCAD ccx (auto-detected above) is the path, so don't suggest brew.
        "install_hint": "auto-detected from FreeCAD's bundled ccx (every FreeCAD install "
                        "ships it, all OSes) when FreeCAD is installed; otherwise "
                        "'apt install calculix-ccx' (Linux) or point DRIFTPIN_CALCULIX_PATH "
                        "at a ccx binary",
    },
}


# --- low-level probes (monkeypatch these in tests to simulate absent/present) --

def _module_available(module: str) -> bool:
    """True if ``module`` is importable in this interpreter, WITHOUT importing it
    (find_spec only). Heavy wheels (mujoco, pybullet) have real import-time cost;
    discovery must stay cheap. Any lookup error -> treat as absent."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        # ValueError: a parent package exists but isn't a package; ImportError /
        # ModuleNotFoundError: a parent in the dotted path is missing. Either way
        # the module is not usable here.
        return False


def _freecad_bundled_bin_dirs() -> list:
    """FreeCAD ships solver binaries (ccx, gmsh) in the SAME ``bin/`` as freecadcmd, on
    every OS. Reuse the client's cross-platform freecadcmd discovery to locate that bin,
    so a FreeCAD-bundled solver resolves with no separate install and no env var. Lazy
    import keeps this module standalone/importable on the no-FreeCAD lane; any failure
    yields no dirs (clean degradation). Read-only — no execution, no env mutation."""
    try:
        from driftpin.client import _freecadcmd_candidates, _resolve_freecadcmd
    except Exception:
        return []
    dirs = []
    # _resolve_freecadcmd() honours DRIFTPIN_FREECADCMD / PATH / the globbed installs and
    # returns the real freecadcmd; its candidates cover the not-yet-resolved installs too.
    for c in [_resolve_freecadcmd(), *_freecadcmd_candidates()]:
        d = os.path.dirname(c)
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


def _binary_candidates(name: str, spec: dict) -> list:
    """Ordered candidate paths for a binary solver, most-preferred first:
    DRIFTPIN_<NAME>_PATH env override -> PATH (shutil.which) -> common per-OS
    install dirs -> FreeCAD's bundled bin/ (for ``freecad_bundled`` solvers). Pure
    lookup — no side effects, no existence check (the caller filters). Mirrors
    worker.py's _renderer_exec_candidates, minus the FreeCAD prefs step."""
    candidates = []
    if env_path := _config.get(f"DRIFTPIN_{name.upper()}_PATH"):
        candidates.append(env_path)                  # 1) env override -> config file
    for binname in spec["binaries"]:                 # 2) PATH (honors Windows PATHEXT)
        if found := shutil.which(binname):
            candidates.append(found)
    for d in spec["dirs"].get(platform.system(), ()):  # 3) common install dirs
        for binname in spec["binaries"]:
            for exe in (binname, binname + ".exe"):
                candidates.append(os.path.join(d, exe))
    # 4) the Windows provisioner's portable extracts: scripts/install-solvers.ps1
    #    unzips SU2/PrusaSlicer/Elmer under %LOCALAPPDATA%\DriftPin\solvers, so a
    #    provisioned box resolves them with NO env var — the way an MCP host with
    #    a minimal environment launches `driftpin mcp` (issue #205 / #199).
    if platform.system() == "Windows" and (lad := os.environ.get("LOCALAPPDATA")):
        import glob as _glob
        base = os.path.join(lad, "DriftPin", "solvers")
        for binname in spec["binaries"]:
            candidates.extend(sorted(_glob.glob(
                os.path.join(base, "*", "**", binname + ".exe"),
                recursive=True), reverse=True))       # newest versioned dir first
    if spec.get("freecad_bundled"):                  # 5) FreeCAD's bundled bin/ (ccx, gmsh)
        for d in _freecad_bundled_bin_dirs():
            for binname in spec["binaries"]:
                for exe in (binname, binname + ".exe"):
                    candidates.append(os.path.join(d, exe))
    return candidates


def _binary_path(name: str, spec: dict):
    """First existing candidate path for a binary solver, or None. Side-effect-free
    (does not mutate any environment) — safe for the capabilities probe."""
    for c in _binary_candidates(name, spec):
        if c and os.path.isfile(c):
            return c
    return None


# --- installed-but-unwired probe (issue #177) ----------------------------------
# A solver's discovery can be *env-scoped*: its binary only lands on PATH after an
# etc/bashrc is sourced (OpenFOAM), or its python lives in a dedicated venv reached
# via a DRIFTPIN_*_PYTHON env var (openEMS, bempp). In a bare shell — env unset —
# find_solver() then reports it flatly absent even though it is installed and green
# in CI. These probes look, READ-ONLY (never source a bashrc, never set an env var),
# for a well-known artifact that proves "installed but not wired into this shell",
# so discovery can report the third state ``unwired`` with the exact wire-up hint.

def _repo_root() -> str:
    """Repo root used to locate the dedicated per-solver venvs (``.venv-openems``,
    ``.venv-bempp``) beside the checkout. Honors ``DRIFTPIN_REPO_ROOT`` — a read-only
    discovery override tests point at a tmp dir — else the checkout holding this file."""
    return _config.get("DRIFTPIN_REPO_ROOT") or \
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _standard_bashrc(cfg: dict):
    """Probe standard OpenFOAM install prefixes for an ``etc/bashrc`` (the unwired
    evidence). ``DRIFTPIN_OPENFOAM_DIRS`` (a pathsep-joined list of install prefixes;
    a read-only discovery override tests point at a tmp dir) is searched first, each
    prefix for ``<prefix>/etc/bashrc``; then the entry's standard-location globs.
    Returns the bashrc path or None. Side-effect-free."""
    roots = _config.get("DRIFTPIN_OPENFOAM_DIRS")
    if roots:
        for root in roots.split(os.pathsep):
            cand = os.path.join(root, "etc", "bashrc")
            if os.path.isfile(cand):
                return cand
    import glob as _glob
    for pat in cfg["globs"]:
        hits = sorted(_glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def _unwired_found(name: str, spec: dict):
    """When a solver fails to resolve, cheaply probe well-known artifact locations for
    evidence it is *installed but not wired into this shell*. READ-ONLY — never sources
    a bashrc, never sets an env var (discovery stays side-effect-free). Returns
    ``(found_at, wire_hint)`` when such evidence exists, else None."""
    cfg = spec.get("unwired")
    if not cfg:
        return None
    probe = cfg["probe"]
    if probe == "venv":
        # the dedicated venv sits beside the repo or one level up (mirrors the worker's
        # _em_fullwave_gpl_python / _bempp_python resolution)
        for base in (_repo_root(), os.path.dirname(_repo_root())):
            venv = os.path.join(base, cfg["venv"])
            for rel in ("bin/python3", "bin/python", "Scripts/python.exe"):
                if os.path.isfile(os.path.join(venv, *rel.split("/"))):
                    return venv, cfg["hint"].format(found=venv)
    elif probe == "bashrc":
        found = _standard_bashrc(cfg)
        if found:
            return found, cfg["hint"].format(found=found)
    elif probe == "fsi_adapter":
        found = openfoam_adapter_lib_dir() or precice_lib_dir()
        if found:
            return found, cfg["hint"].format(found=found)
    return None


# --- discovery -----------------------------------------------------------------

def known_solvers() -> list:
    """Sorted names of every solver in the registry."""
    return sorted(_SOLVERS)


def _spec(name: str) -> dict:
    spec = _SOLVERS.get(name)
    if spec is None:
        raise ValueError(
            f"unknown solver {name!r}; known solvers: {known_solvers()}"
        )
    return spec


def find_solver(name: str) -> dict:
    """Side-effect-free probe of a single solver. Returns
    ``{name, kind, family, extra, available, status}`` plus, when available (status
    ``ok``), ``path`` (a binary) or ``module`` (a resolved wheel module). When it does
    not resolve, ``install_hint`` is always present and ``status`` is one of:

      * ``unwired`` — not resolvable in *this* shell, but a well-known artifact proves
        it is installed (an env-scoped discovery whose env is unset); carries
        ``found_at`` + ``wire_hint`` (the exact ``set DRIFTPIN_…`` fix).
      * ``absent`` — no evidence it is installed anywhere; ``install_hint`` is the
        install path.

    Raises ValueError for an unknown name."""
    spec = _spec(name)
    info = {
        "name": name,
        "kind": spec["kind"],
        "family": spec["family"],
        "extra": spec["extra"],
        "available": False,
    }
    if spec["kind"] == "wheel":
        resolved = next((m for m in spec["modules"] if _module_available(m)), None)
        if resolved is not None:
            info["available"] = True
            info["module"] = resolved
    else:                                            # binary
        path = _binary_path(name, spec)
        if path is not None:
            info["available"] = True
            info["path"] = path
    if info["available"]:
        info["status"] = "ok"
        return info
    # not resolvable here — installed-but-unwired, or truly absent?
    unwired = _unwired_found(name, spec)
    if unwired:
        found_at, wire_hint = unwired
        info["status"] = "unwired"
        info["found_at"] = found_at
        info["wire_hint"] = wire_hint
    else:
        info["status"] = "absent"
    info["install_hint"] = spec["install_hint"]
    return info


def is_available(name: str) -> bool:
    """True if ``name`` resolves to an installed solver right now."""
    return find_solver(name)["available"]


def require_solver(name: str) -> dict:
    """The graceful-degradation gate every P2 family calls before solving.

    Returns ``{ok: True, name, kind, family, path|module}`` when the solver
    resolves, else the structured miss dict
    ``{ok: False, solver: name, status, reason, install: <hint>}`` — which a family's
    ``*_submit`` returns verbatim, so a missing solver is a clean result, never an
    exception. ``status`` distinguishes ``absent`` (not installed; ``install`` is the
    install path) from ``unwired`` (installed but env-scoped and unset in this shell;
    ``install`` is the ``set DRIFTPIN_…`` fix and ``found_at`` names the evidence).
    Raises ValueError only for an unknown solver name (a wiring bug, not a missing
    install)."""
    info = find_solver(name)
    if info["available"]:
        out = {"ok": True, "name": name, "kind": info["kind"],
               "family": info["family"]}
        if "path" in info:
            out["path"] = info["path"]
        if "module" in info:
            out["module"] = info["module"]
        return out
    if info["status"] == "unwired":
        return {
            "ok": False,
            "solver": name,
            "status": "unwired",
            "reason": "solver installed but env not wired in this shell",
            "install": info["wire_hint"],
            "found_at": info["found_at"],
        }
    return {
        "ok": False,
        "solver": name,
        "status": "absent",
        "reason": "solver not installed",
        "install": info["install_hint"],
    }


def openfoam_bashrc() -> str | None:
    """Locate the OpenFOAM environment file to ``source`` before running its apps.

    OpenFOAM binaries need WM_PROJECT_DIR / FOAM_ETC exported or they abort with
    "Could not find mandatory etc entry 'controlDict'" — so a bare ``subprocess.run``
    of blockMesh/simpleFoam fails. Resolution order, side-effect-free:
    ``DRIFTPIN_OPENFOAM_BASHRC`` env -> ``$WM_PROJECT_DIR/etc/bashrc`` -> the source-
    build layout next to the resolved binary (``<foamdir>/platforms/.../bin`` ->
    ``<foamdir>/etc/bashrc``) -> common install dirs (incl. the apt
    ``/usr/share/openfoam`` layout). Returns the path, or None when none resolves."""
    env = _config.get("DRIFTPIN_OPENFOAM_BASHRC")
    if env and os.path.isfile(env):
        return env
    wm = os.environ.get("WM_PROJECT_DIR")
    if wm:
        cand = os.path.join(wm, "etc", "bashrc")
        if os.path.isfile(cand):
            return cand
    binpath = find_solver("openfoam").get("path")
    if binpath:
        # source builds: <foamdir>/platforms/<arch>/bin/<app> -> <foamdir>/etc/bashrc
        marker = os.sep + "platforms" + os.sep
        real = os.path.realpath(binpath)
        if marker in real:
            cand = os.path.join(real.split(marker)[0], "etc", "bashrc")
            if os.path.isfile(cand):
                return cand
    import glob as _glob
    for pat in ("/usr/share/openfoam/etc/bashrc",
                "/usr/lib/openfoam/openfoam*/etc/bashrc",
                "/opt/openfoam*/etc/bashrc", "/opt/OpenFOAM*/etc/bashrc"):
        hits = sorted(_glob.glob(pat))
        if hits:
            return hits[-1]
    return None


# --- FSI (preCICE OpenFOAM<->CalculiX) stack resolvers -------------------------
# Side-effect-free path resolution for the three pieces the partitioned solve
# needs at run time. Each honours a DRIFTPIN_* env override first (the documented
# install path), then a small set of build-default locations, mirroring the
# openfoam_bashrc() resolution style. The FSI handler/runner consumes these.

def ccx_precice_bin() -> str | None:
    """The preCICE-enabled CalculiX solver (``ccx_preCICE``) — the solid
    participant. DRIFTPIN_CCX_PRECICE / DRIFTPIN_PRECICE_PATH env -> the registry
    binary resolution (~/calculix-adapter/bin etc.). Returns the path or None."""
    env = _config.get("DRIFTPIN_CCX_PRECICE")
    if env and os.path.isfile(env):
        return env
    return find_solver("precice").get("path")


def precice_lib_dir() -> str | None:
    """Directory holding ``libprecice.so`` (the serial, MPI-off build that the
    adapters link). DRIFTPIN_PRECICE_LIB env -> the conda-forge env lib ->
    the documented source-build prefix. Returns the dir or None."""
    env = _config.get("DRIFTPIN_PRECICE_LIB")
    if env and os.path.isdir(env):
        return env
    import glob as _glob
    for pat in (os.path.expanduser("~/precice-serial/lib"),
                os.path.expanduser("~/miniforge3/envs/precice/lib"),
                os.path.expanduser("~/miniconda3/envs/precice/lib"),
                "/usr/local/lib", "/usr/lib/x86_64-linux-gnu"):
        if os.path.isfile(os.path.join(pat, "libprecice.so")) or \
           _glob.glob(os.path.join(pat, "libprecice.so*")):
            return pat
    return None


def openfoam_adapter_lib_dir() -> str | None:
    """Directory holding ``libpreciceAdapterFunctionObject.so`` (the OpenFOAM
    function-object adapter the fluid participant loads). DRIFTPIN_OPENFOAM_ADAPTER_LIB
    env -> the wmake user-lib build prefix. Returns the dir or None."""
    env = _config.get("DRIFTPIN_OPENFOAM_ADAPTER_LIB")
    if env and os.path.isdir(env):
        return env
    import glob as _glob
    for pat in (os.path.expanduser(
                    "~/OpenFOAM/*/platforms/*/lib"),
                os.path.expanduser("~/OpenFOAM/*-v*/platforms/*/lib")):
        for d in sorted(_glob.glob(pat)):
            if os.path.isfile(
                    os.path.join(d, "libpreciceAdapterFunctionObject.so")):
                return d
    return None


def fsi_openfoam_bashrc() -> str | None:
    """The OpenFOAM env file the FSI *fluid* (``pimpleFoam`` + the preCICE
    function-object adapter) must source — which MUST match the OpenFOAM version
    the adapter (``openfoam_adapter_lib_dir``) was built against.

    This is deliberately distinct from the general ``openfoam_bashrc()``: a
    function-object library is version/ABI-specific, so a ``pimpleFoam`` from a
    *different* OpenFOAM than the adapter aborts at startup with
    ``functionObject::New ... FOAM exiting`` (the adapter isn't in its registry).
    The fluid then never connects and the Solid hangs at the preCICE handshake
    until the deadline. On a box with several OpenFOAM installs (e.g. an apt
    upgrade past the version the adapter was wmade against) the general resolver
    picks the newest, which is exactly the mismatch that bites.

    Resolution: ``DRIFTPIN_FSI_OPENFOAM_BASHRC`` env -> the install whose version
    matches the adapter lib path (``~/OpenFOAM/<user>-v2512/...`` -> the
    ``…openfoam2512…`` / ``…-v2512…`` bashrc) -> the general ``openfoam_bashrc()``."""
    env = _config.get("DRIFTPIN_FSI_OPENFOAM_BASHRC")
    if env and os.path.isfile(env):
        return env
    ofa = openfoam_adapter_lib_dir()
    if ofa:
        import re as _re
        m = _re.search(r"v?(\d{4})", os.path.basename(os.path.dirname(
            os.path.dirname(os.path.dirname(ofa)))) or ofa)
        # fall back to scanning the whole adapter path for a 4-digit version token
        if not m:
            m = _re.search(r"v?(\d{4})", ofa)
        if m:
            ver = m.group(1)
            for cand in (f"/usr/lib/openfoam/openfoam{ver}/etc/bashrc",
                         os.path.expanduser(f"~/OpenFOAM/OpenFOAM-v{ver}/etc/bashrc"),
                         f"/opt/openfoam{ver}/etc/bashrc",
                         f"/opt/OpenFOAM-v{ver}/etc/bashrc",
                         f"/usr/share/openfoam{ver}/etc/bashrc"):
                if os.path.isfile(cand):
                    return cand
    return openfoam_bashrc()


def fsi_stack_status() -> dict:
    """Resolve the full FSI stack side-effect-free for the capabilities/degradation
    report: ``{ok, ccx_precice, precice_lib, openfoam_adapter_lib, openfoam_bashrc,
    missing}``. ``ok`` is true only when all four resolve. ``openfoam_bashrc`` is
    the *FSI-matched* one (``fsi_openfoam_bashrc``), i.e. the version the adapter
    was built against — what ``run_coupled_fsi`` actually sources."""
    ccx = ccx_precice_bin()
    lib = precice_lib_dir()
    ofa = openfoam_adapter_lib_dir()
    of = fsi_openfoam_bashrc()
    missing = [n for n, v in (("ccx_preCICE", ccx), ("libprecice", lib),
                              ("openfoam-adapter", ofa),
                              ("openfoam-bashrc", of)) if not v]
    return {
        "ok": not missing,
        "ccx_precice": ccx,
        "precice_lib": lib,
        "openfoam_adapter_lib": ofa,
        "openfoam_bashrc": of,
        "missing": missing,
    }


def ccx_bin() -> str | None:
    """Locate the plain ``ccx`` (CalculiX) executable for a direct subprocess solve —
    the molding warpage thermo-elastic post-step (#113 Part B) writes its own ``.inp``
    deck and runs ccx itself (the GPL solver stays out-of-process, never imported).

    Distinct from :func:`ccx_precice_bin` (the preCICE-patched ``ccx_preCICE``).
    Resolution is the registry's standard order — ``DRIFTPIN_CALCULIX_PATH`` env ->
    PATH (``shutil.which``) -> common install dirs — via the ``calculix`` spec.
    Returns the path or None."""
    return _binary_path("calculix", _SOLVERS["calculix"])


def openinjmoldsim_bin() -> str | None:
    """Locate the ``openInjMoldSim`` executable (the GPL-3.0 injection-molding solver,
    a modified compressibleInterFoam built against OpenFOAM 7 .org).

    This is the higher-fidelity twin the molding-fill family prefers when present;
    when it is absent the family degrades to ``interFoam`` on the existing
    OpenFOAM (.com/ESI) — see ``driftpin/analysis/molding_fill.py`` and
    ``tools/build_openinjmoldsim.sh``. Side-effect-free resolution:
    ``DRIFTPIN_OPENINJMOLDSIM`` / ``DRIFTPIN_OPENINJMOLDSIM_PATH`` env -> PATH
    (``shutil.which``) -> the documented source-build prefix
    (``~/opt/openInjMoldSim/...``). Returns the path, or None when it does not
    resolve (the common case until the OF7-org build lands)."""
    for var in ("DRIFTPIN_OPENINJMOLDSIM", "DRIFTPIN_OPENINJMOLDSIM_PATH"):
        env = _config.get(var)
        if env and os.path.isfile(env):
            return env
    found = shutil.which("openInjMoldSim")
    if found:
        return found
    import glob as _glob
    for pat in (os.path.expanduser("~/opt/openInjMoldSim/*/bin/openInjMoldSim"),
                os.path.expanduser("~/OpenFOAM/*/platforms/*/bin/openInjMoldSim"),
                "/opt/openInjMoldSim/*/bin/openInjMoldSim"):
        hits = sorted(_glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def openinjmoldsim_bashrc() -> str | None:
    """The OpenFOAM-7 (.org) environment file to source before running
    ``openInjMoldSim`` — distinct from ``openfoam_bashrc()`` (which resolves the
    ESI v19xx/v25xx build). ``DRIFTPIN_OPENINJMOLDSIM_BASHRC`` env -> the OF7-org
    source-build / apt layouts. Returns the path or None."""
    env = _config.get("DRIFTPIN_OPENINJMOLDSIM_BASHRC")
    if env and os.path.isfile(env):
        return env
    import glob as _glob
    for pat in (os.path.expanduser("~/OpenFOAM/OpenFOAM-7/etc/bashrc"),
                os.path.expanduser("~/opt/OpenFOAM-7/etc/bashrc"),
                "/opt/openfoam7/etc/bashrc",
                "/usr/lib/openfoam/openfoam7/etc/bashrc"):
        hits = sorted(_glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def run_argvs(case_dir: str, argv_list) -> tuple:
    """Run a sequence of solver argv lists directly in ``case_dir`` — no shell.

    The native counterpart of the worker's bash-based OpenFOAM chain, for solvers
    that need no environment sourcing (SU2 today — issue #203): each ``argv`` is
    passed straight to ``subprocess.run`` with ``cwd=case_dir``, so Windows paths
    survive untouched and neither ``bash`` nor WSL is required. Apps run
    left-to-right, stopping at the first failure. Returns
    ``(returncode, combined_output_tail)`` — the same contract as the worker's
    ``_run_foam``."""
    import subprocess
    out, rc = "", 0
    for argv in argv_list:
        proc = subprocess.run([str(a) for a in argv], cwd=case_dir,
                              capture_output=True, text=True)
        out += (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
        if rc != 0:
            break
    return rc, out[-2000:]


def sibling_bin(main_bin: str, name: str) -> str:
    """Resolve a companion executable that ships next to ``main_bin`` (ElmerGrid /
    ViewFactors next to ElmerSolver): PATH first, then the sibling path — with the
    ``.exe`` suffix Windows needs (a bare ``os.path.join(dir, name)`` never passes
    ``isfile`` there, which silently skipped the ViewFactors/ElmerGrid legs on an
    otherwise-complete native Windows Elmer install; issue #205). Existence is the
    caller's check — the returned path may not exist."""
    found = shutil.which(name)
    if found:
        return found
    cand = os.path.join(os.path.dirname(main_bin), name)
    if os.name == "nt" and not os.path.isfile(cand):
        cand += ".exe"
    return cand


def su2_case_config(case_dir: str) -> str | None:
    """The SU2 ``.cfg`` a prepared ``case_dir`` should be solved with.

    ``SU2_CFD`` takes its config filename as a positional argument (there is no
    default), so the native runner must name one. Preference: the conventional
    names, then a lone ``*.cfg``. Returns ``None`` when the case has no config —
    or several ambiguous ones — and the caller lets SU2 print its own usage
    error."""
    import glob as _glob
    cfgs = sorted(os.path.basename(p)
                  for p in _glob.glob(os.path.join(case_dir, "*.cfg")))
    for name in ("config.cfg", "su2.cfg", "case.cfg"):
        if name in cfgs:
            return name
    return cfgs[0] if len(cfgs) == 1 else None


def capabilities() -> dict:
    """Report which P2 solvers (and which families) are usable *right now* —
    the ``solve_capabilities`` tool's payload, the solver twin of
    ``render_capabilities``. Resolves every solver side-effect-free (no execution,
    no env mutation).

    Returns ``{platform, available (sorted ready solver names), unwired (sorted
    installed-but-unwired names, issue #177), solvers: {name: {available, status,
    kind, family, extra, and either path/module or install_hint (+found_at/wire_hint
    when unwired)}}, families: {family: {solvers, available, unwired, any_available}},
    extras: {extra: [solver names]}}``."""
    solvers = {name: find_solver(name) for name in _SOLVERS}

    families: dict = {}
    for name, info in solvers.items():
        fam = families.setdefault(
            info["family"],
            {"solvers": [], "available": [], "unwired": [], "any_available": False},
        )
        fam["solvers"].append(name)
        if info["available"]:
            fam["available"].append(name)
            fam["any_available"] = True
        elif info.get("status") == "unwired":
            fam["unwired"].append(name)

    extras: dict = {}
    for name, spec in _SOLVERS.items():
        if spec["extra"]:
            extras.setdefault(spec["extra"], []).append(name)

    return {
        "platform": platform.system(),
        "available": sorted(n for n, i in solvers.items() if i["available"]),
        "unwired": sorted(
            n for n, i in solvers.items() if i.get("status") == "unwired"),
        "solvers": solvers,
        "families": families,
        "extras": {k: sorted(v) for k, v in extras.items()},
    }
