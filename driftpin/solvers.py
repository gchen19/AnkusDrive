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

# Registry of the P2 external solvers, keyed by the name agents pass (and the
# DRIFTPIN_<NAME>_PATH env override is the upper-cased key). Ordered by the
# milestone sequence in docs/SIMULATION_P2_KICKOFF.md. Each family lists every
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
    "rayoptics": {
        "kind": "wheel",
        "family": "optics",
        "extra": "optics",
        "modules": ("rayoptics", "optiland"),
        "install_hint": "pip install 'driftpin[optics]'  (pulls rayoptics), "
                        "or: pip install rayoptics optiland",
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


def _binary_candidates(name: str, spec: dict) -> list:
    """Ordered candidate paths for a binary solver, most-preferred first:
    DRIFTPIN_<NAME>_PATH env override -> PATH (shutil.which) -> common per-OS
    install dirs. Pure lookup — no side effects, no existence check (the caller
    filters). Mirrors worker.py's _renderer_exec_candidates, minus the FreeCAD
    prefs step (solvers aren't FreeCAD-managed)."""
    candidates = []
    if env_path := os.environ.get(f"DRIFTPIN_{name.upper()}_PATH"):
        candidates.append(env_path)                  # 1) explicit env override
    for binname in spec["binaries"]:                 # 2) PATH (honors Windows PATHEXT)
        if found := shutil.which(binname):
            candidates.append(found)
    for d in spec["dirs"].get(platform.system(), ()):  # 3) common install dirs
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
    ``{name, kind, family, extra, available}`` plus, when available, ``path`` (a
    binary) or ``module`` (a resolved wheel module); when absent, ``install_hint``.
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
    if not info["available"]:
        info["install_hint"] = spec["install_hint"]
    return info


def is_available(name: str) -> bool:
    """True if ``name`` resolves to an installed solver right now."""
    return find_solver(name)["available"]


def require_solver(name: str) -> dict:
    """The graceful-degradation gate every P2 family calls before solving.

    Returns ``{ok: True, name, kind, family, path|module}`` when the solver
    resolves, else the structured miss dict
    ``{ok: False, solver: name, reason: "solver not installed", install: <hint>}``
    — which a family's ``*_submit`` returns verbatim, so a missing solver is a clean
    result, never an exception. Raises ValueError only for an unknown solver name
    (a wiring bug, not a missing install)."""
    info = find_solver(name)
    if info["available"]:
        out = {"ok": True, "name": name, "kind": info["kind"],
               "family": info["family"]}
        if "path" in info:
            out["path"] = info["path"]
        if "module" in info:
            out["module"] = info["module"]
        return out
    return {
        "ok": False,
        "solver": name,
        "reason": "solver not installed",
        "install": info["install_hint"],
    }


def capabilities() -> dict:
    """Report which P2 solvers (and which families) are usable *right now* —
    the ``solve_capabilities`` tool's payload, the solver twin of
    ``render_capabilities``. Resolves every solver side-effect-free (no execution,
    no env mutation).

    Returns ``{platform, available (sorted ready solver names), solvers: {name:
    {available, kind, family, extra, and either path/module or install_hint}},
    families: {family: {solvers, available, any_available}}, extras: {extra:
    [solver names]}}``."""
    solvers = {name: find_solver(name) for name in _SOLVERS}

    families: dict = {}
    for name, info in solvers.items():
        fam = families.setdefault(
            info["family"], {"solvers": [], "available": [], "any_available": False}
        )
        fam["solvers"].append(name)
        if info["available"]:
            fam["available"].append(name)
            fam["any_available"] = True

    extras: dict = {}
    for name, spec in _SOLVERS.items():
        if spec["extra"]:
            extras.setdefault(spec["extra"], []).append(name)

    return {
        "platform": platform.system(),
        "available": sorted(n for n, i in solvers.items() if i["available"]),
        "solvers": solvers,
        "families": families,
        "extras": {k: sorted(v) for k, v in extras.items()},
    }
