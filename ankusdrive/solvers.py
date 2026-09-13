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
    extra: ``pip install ankusdrive[mbd]``.
  * **binary** — an external executable resolved cross-platform exactly the way the
    renderers are: ``ANKUSDRIVE_<SOLVER>_PATH`` env override -> PATH (``shutil.which``)
    -> common per-OS install dirs. OpenFOAM / Elmer / SU2 ship via apt/conda, are
    documented (not vendored), and only *execute* on the provisioned/self-hosted
    runner. On Windows the OpenFOAM-backed families additionally resolve *inside*
    the WSL2 distro (probed glob-only through \\wsl$; launched via ``bash_argv`` —
    issue #193), reported with ``via: "wsl"``; on macOS they resolve inside the
    Multipass VM, whose filesystem the host cannot probe, so there they resolve
    only from explicit in-VM ``ANKUSDRIVE_*`` overrides, reported with
    ``via: "multipass"``.

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

from ankusdrive import config as _config
from ankusdrive import install_kind as _install_kind

# Registry of the P2 external solvers, keyed by the name agents pass (and the
# ANKUSDRIVE_<NAME>_PATH env override is the upper-cased key). Ordered by the
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
        "install_hint": "pip install 'ankusdrive[mbd]'  (pulls pybullet on "
                        "Linux/Windows; PyBullet ships no macOS wheels and its sdist "
                        "fails to compile there — the extra pulls mujoco instead), "
                        "or: pip install pybullet",
    },
    "mujoco": {
        "kind": "wheel",
        "family": "mbd",
        "extra": "mbd",
        "modules": ("mujoco",),
        "install_hint": "pip install 'ankusdrive[mbd]'  (pulls mujoco), "
                        "or: pip install mujoco",
    },
    # --- M3 topology: pip wheels, returns geometry ---------------------------
    "topopt": {
        "kind": "wheel",
        "family": "topology",
        "extra": "topology",
        "modules": ("topopt", "solidspy"),
        "install_hint": "pip install 'ankusdrive[topology]'  (pulls topopt/solidspy), "
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
        # NB: there is NO conda-forge/Homebrew Elmer package (issue #194, same class
        # of stale hint as the brew calculix one in #192) — don't suggest conda.
        "install_hint": "'apt install elmerfem-csc' (Linux), the portable no-GUI zip "
                        "via scripts/install-solvers.ps1 elmer (Windows), or a source "
                        "build from https://www.elmerfem.org/ (macOS ships no prebuilt "
                        "binaries) — then ensure ElmerSolver is on PATH or set "
                        "ANKUSDRIVE_ELMER_PATH",
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
                        "on PATH or set ANKUSDRIVE_OPENFOAM_PATH; on Windows install "
                        "WSL2 ('wsl --install -d Ubuntu') and provision inside the "
                        "distro (scripts/install-solvers.ps1 wsl)",
        # in-substrate install layouts (issue #193): POSIX glob dirs matched against
        # each registry binary, probed through \\wsl$ on Windows. Deliberately NOT
        # dirs["Linux"] — those are expanduser'd on the Windows side at import. The
        # key also marks this solver as substrate-routable, which on macOS is what
        # lets an in-VM (Multipass) override resolve — see _vm_binary_path.
        "substrate_bins": ("/usr/lib/openfoam/openfoam*/platforms/*/bin",
                           "/opt/openfoam*/platforms/*/bin",
                           "/opt/OpenFOAM*/platforms/*/bin",
                           "~/OpenFOAM/OpenFOAM-*/platforms/*/bin"),
        # installed-but-unwired probe (issue #177): foamRun/simpleFoam only land on
        # PATH *after* an etc/bashrc is sourced, so a bare shell reports the binary
        # absent even when OpenFOAM is fully installed. A standard-location etc/bashrc
        # is the evidence that it is installed but not wired into this shell.
        "unwired": {
            "probe": "bashrc",
            "globs": ("/usr/share/openfoam/etc/bashrc",
                      "/usr/lib/openfoam/openfoam*/etc/bashrc",
                      "/opt/openfoam*/etc/bashrc", "/opt/OpenFOAM*/etc/bashrc"),
            "hint": "set ANKUSDRIVE_OPENFOAM_BASHRC={found} (or source it)",
            # a \\wsl$ hit means "wired" once the WSL launcher is used (issue #193)
            "hint_wsl": "set ANKUSDRIVE_OPENFOAM_BASHRC={found} "
                        "(runs via WSL distro '{distro}')",
            # macOS: OpenFOAM lives inside the Multipass VM (issue #193), so there is
            # no host-visible bashrc to glob. The `multipass` CLI on PATH is the honest
            # side-effect-free signal that the substrate exists; the solver inside the
            # VM can't be confirmed without executing it, so this is reported as
            # unwired (substrate present, provisioning unverified), never as ready.
            # Three of them (issue #237): "unwired" is one word for three different
            # machine states, and only one of them is fixed by provisioning. A
            # `multipass info` READ (side-effect-free, timeout-bounded — see
            # multipass_vm_state) tells them apart, so the hint names the step that
            # is actually missing instead of telling someone with a running,
            # provisioned VM to go build one.
            "darwin_multipass": (
                "provision OpenFOAM in the Multipass VM — "
                "`multipass shell {inst}` then `bash scripts/install-solvers.sh cfd`; "
                "then export the IN-VM paths, which macOS trusts unstat'd (the VM "
                "filesystem is opaque from the host): ANKUSDRIVE_OPENFOAM_BASHRC="
                "/usr/lib/openfoam/openfoam<ver>/etc/bashrc and ANKUSDRIVE_OPENFOAM_PATH="
                "<that prefix>/platforms/linuxARM64GccDPInt32Opt/bin/interFoam. "
                "Set ANKUSDRIVE_OPENFOAM_INSTANCE={inst} if the instance is named "
                "otherwise. See docs/MACOS.md"),
            "darwin_multipass_stopped": (
                "the Multipass VM '{inst}' exists but is not running — start it: "
                "`multipass start {inst}` (then re-check; if OpenFOAM was never "
                "provisioned inside it, `multipass shell {inst}` and "
                "`bash scripts/install-solvers.sh cfd`). See docs/MACOS.md"),
            "darwin_multipass_running": (
                "the Multipass VM '{inst}' is running — only this shell's env is "
                "missing. Export the IN-VM paths, which macOS trusts unstat'd (the "
                "VM filesystem is opaque from the host): "
                "export TMPDIR=$HOME/fsi-run (the host side of `multipass mount`, so "
                "case dirs land at a path that resolves in the VM too), "
                "export ANKUSDRIVE_OPENFOAM_BASHRC="
                "/usr/lib/openfoam/openfoam<ver>/etc/bashrc, "
                "export ANKUSDRIVE_OPENFOAM_PATH="
                "<that prefix>/platforms/linuxARM64GccDPInt32Opt/bin/simpleFoam. "
                "If OpenFOAM is not installed in the VM yet, `multipass shell {inst}` "
                "then `bash scripts/install-solvers.sh cfd` first. See docs/MACOS.md"),
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
                        "ANKUSDRIVE_SU2_PATH",
        # `prepared_case_only` was set here from #237 item 2 until item 3 landed: no
        # AnkusDrive tool could BUILD an SU2 case, so SU2 resolving must not make the
        # cfd family read "available" to an agent hunting for a solver to escalate
        # into. That is now false — analysis/su2_case.py writes the plane-Poiseuille
        # validation case (cfd_internal_flow_submit's `channel_height_mm` mode), it
        # runs natively with no VM, and it is gated live against the closed form. So
        # SU2 counts toward the cfd family again, which is what makes docs/MACOS.md's
        # "CFD degrades to SU2" true on Apple Silicon rather than aspirational.
        #
        # The OpenFOAM-only modes (the straight pipe, the snappyHexMesh geometry
        # bridge, the flat plate, the wind tunnel) are still OpenFOAM-only; the
        # family being available means SOME built-in case can run, not every one.
    },
    # --- optics: pip wheels (the `optics` extra) -----------------------------
    # Two lanes (see memory optics-library-selection). SEQUENTIAL imaging/lens
    # design + optimization runs IN-PROCESS on optiland (MIT). NON-SEQUENTIAL
    # tracing through real STL solids runs on KrakenOS, which is GPL-3.0 and is
    # therefore invoked ONLY out-of-process via ankusdrive/optics_gpl_runner.py —
    # find_spec discovery below merely checks the file exists, it does not import
    # (or link) the GPL code, so the copyleft boundary stays intact.
    "rayoptics": {
        "kind": "wheel",
        "family": "optics",
        "extra": "optics",
        "modules": ("rayoptics", "optiland"),
        "install_hint": "pip install 'ankusdrive[optics]'  (pulls rayoptics), "
                        "or: pip install rayoptics optiland",
    },
    "optiland": {
        "kind": "wheel",
        "family": "optics",
        "extra": "optics",
        "modules": ("optiland",),
        "install_hint": "pip install 'ankusdrive[optics]'  (pulls optiland), "
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
        "install_hint": "pip install 'ankusdrive[optics_gpl]'  (pulls KrakenOS, GPL-3.0; "
                        "run out-of-process only), or: pip install KrakenOS 'setuptools<81'",
    },
    # --- exterior acoustics: Bempp BEM (MIT, but meshio>=4 clashes with solidspy) ---
    # Bempp is MIT — NOT a license boundary. The subprocess isolation is purely a
    # DEPENDENCY clash: bempp needs meshio>=4 (cells_dict) while the shared venv pins
    # meshio==3.0 for solidspy (ankusdrive/analysis/topology.py). So bempp lives in a
    # DEDICATED venv (.venv-bempp) and is invoked out-of-process via
    # ankusdrive/bempp_runner.py; the worker resolves that interpreter via
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
                        "ANKUSDRIVE_BEMPP_PYTHON at that venv's python.",
        # installed-but-unwired probe (issue #177): bempp lives in the dedicated
        # .venv-bempp (meshio>=5), not this interpreter; the venv beside the repo is
        # the evidence it is installed but ANKUSDRIVE_BEMPP_PYTHON is not set here.
        "unwired": {
            "probe": "venv",
            "venv": ".venv-bempp",
            "hint": "set ANKUSDRIVE_BEMPP_PYTHON={found}/bin/python3",
        },
    },
    # --- granular DEM: YADE (GPL-3.0, source-built, NOT a pip wheel) ----------
    # YADE is GPL-3.0 and ships no PyPI/conda-noble wheel, so it is source-built
    # (scripts/install-solvers.sh dem_gpl) and driven ONLY out-of-process: the
    # worker shells out to the `yade` EXECUTABLE running ankusdrive/dem_gpl_runner.py
    # (sentinel-JSON over stdin/stdout). AnkusDrive never imports YADE in-process, so
    # the copyleft does not link into the permissive code — the same arm's-length
    # isolation used for the GPL Elmer/OpenFOAM binaries and the KrakenOS optics
    # runner. Resolved as a BINARY (ANKUSDRIVE_YADE_PATH env → PATH → the documented
    # ~/opt/yade/bin source-build prefix); the worker also honors a bare
    # ANKUSDRIVE_YADE override.
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
                        "then ensure `yade` is on PATH or set ANKUSDRIVE_YADE / "
                        "ANKUSDRIVE_YADE_PATH. Driven out-of-process only via "
                        "ankusdrive/dem_gpl_runner.py.",
    },
    # --- full-wave EM: openEMS FDTD (GPL-3.0, source-built, NOT a pip wheel) ---
    # Like KrakenOS, openEMS is GPL-3.0 and is therefore invoked ONLY out-of-process
    # via ankusdrive/em_fullwave_gpl_runner.py — AnkusDrive never imports openEMS/CSXCAD
    # in-process, so the copyleft does not link into AnkusDrive's permissive code. The
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
                        "point ANKUSDRIVE_OPENEMS_PYTHON at that venv's python. Run "
                        "out-of-process only via ankusdrive/em_fullwave_gpl_runner.py.",
        # installed-but-unwired probe (issue #177): openEMS lives in a dedicated
        # .venv-openems, NOT this interpreter, so the find_spec probe above reports it
        # absent in a bare shell. The venv sitting beside the repo is the evidence
        # that it is installed but ANKUSDRIVE_OPENEMS_PYTHON is not set in this shell.
        "unwired": {
            "probe": "venv",
            "venv": ".venv-openems",
            "hint": "set ANKUSDRIVE_OPENEMS_PYTHON={found}/bin/python3",
        },
    },
    # --- FSI coupling: preCICE OpenFOAM<->CalculiX (LGPL core, source adapters) -
    # The partitioned fluid-structure-interaction family. preCICE (LGPL-3.0) is the
    # coupling library; it is only ever invoked OUT-OF-PROCESS (the two heavy
    # solvers — OpenFOAM pimpleFoam and the preCICE-enabled ccx_preCICE — run as
    # their own subprocesses, the same arm's-length boundary OpenFOAM/Elmer already
    # use), so the copyleft never links into AnkusDrive's permissive code. The stack
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
        # whole stack. ANKUSDRIVE_PRECICE_PATH / ANKUSDRIVE_CCX_PRECICE override.
        "binaries": ("ccx_preCICE",),
        "dirs": {
            "Linux":   (os.path.expanduser("~/calculix-adapter/bin"),
                        "/usr/local/bin", "/usr/bin",
                        os.path.expanduser("~/opt/calculix-adapter/bin")),
            "Darwin":  ("/usr/local/bin", "/opt/homebrew/bin"),
            "Windows": (),
        },
        # in-substrate build layouts (issue #193): globbed through \\wsl$ on Windows;
        # on macOS the key marks the solver in-VM-routable (see _vm_binary_path).
        "substrate_bins": ("~/calculix-adapter/bin", "~/opt/calculix-adapter/bin",
                           "/usr/local/bin"),
        # ANKUSDRIVE_CCX_PRECICE is the variable the FSI docs/provisioning set (and
        # what ccx_precice_bin() reads); honoring it here too keeps discovery — and
        # so `ankusdrive doctor` — in step with what the solve actually runs.
        "env_aliases": ("ANKUSDRIVE_CCX_PRECICE",),
        "install_hint": "build the preCICE FSI stack (LGPL core + two source "
                        "adapters; not a pip wheel): scripts/install-solvers.sh fsi "
                        "— installs serial libprecice (MPI off), builds "
                        "precice/calculix-adapter (ccx_preCICE, CalculiX 2.20 src + "
                        "SPOOLES + ARPACK) and precice/openfoam-adapter (wmake vs "
                        "openfoam2512-dev). Then set ANKUSDRIVE_CCX_PRECICE, "
                        "ANKUSDRIVE_PRECICE_LIB, ANKUSDRIVE_OPENFOAM_ADAPTER_LIB and "
                        "ANKUSDRIVE_OPENFOAM_BASHRC (the v2512 bashrc). Driven out-of-"
                        "process only via ankusdrive/analysis/fsi_case.py.",
        # installed-but-unwired probe (issue #177): the ccx_preCICE binary is the
        # linchpin, but the two source-built adapter libraries (libprecice /
        # libpreciceAdapterFunctionObject) are the evidence the stack was partly built
        # even when ccx_preCICE is not resolvable / the ANKUSDRIVE_* envs are unset.
        "unwired": {
            "probe": "fsi_adapter",
            "hint": "FSI stack partly built at {found}; finish the build "
                    "(scripts/install-solvers.sh fsi) and set ANKUSDRIVE_CCX_PRECICE / "
                    "ANKUSDRIVE_PRECICE_LIB / ANKUSDRIVE_OPENFOAM_ADAPTER_LIB / "
                    "ANKUSDRIVE_FSI_OPENFOAM_BASHRC",
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
                        "ANKUSDRIVE_PRUSASLICER_PATH",
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
                        "'apt install calculix-ccx' (Linux) or point ANKUSDRIVE_CALCULIX_PATH "
                        "at a ccx binary",
    },
    # --- studio render: full Blender (Cycles) headless (issue #335) -----------
    # The render_photoreal backend for assemblies, per-part appearance and the studio
    # scene. Full Blender, NOT the stripped standalone `cycles` the Render add-on
    # drives (_RENDERERS["Cycles"] in worker.py): run as `blender --background
    # --python ankusdrive/blender_scene.py`, so it is GPL at arm's length like ccx —
    # a subprocess running a script we ship, never linked or imported.
    "blender": {
        "kind": "binary",
        "family": "studio_render",
        "extra": None,
        "license": "GPL-3.0",
        "isolation": "subprocess",
        "binaries": ("blender", "Blender"),
        "dirs": {
            "Linux":   ("/usr/bin", "/usr/local/bin", "/snap/bin"),
            "Darwin":  ("/Applications/Blender.app/Contents/MacOS",
                        os.path.expanduser("~/Applications/Blender.app/Contents/MacOS")),
            "Windows": (),
        },
        # Versioned install dirs a literal `dirs` entry cannot name: the official
        # installer's `Blender Foundation\Blender 5.2`, and the tarball extracts
        # scripts/install-renderers.sh writes (root -> /opt, user -> ~/.local/opt).
        # Globbed newest-version first, so a provisioned box resolves with no env var.
        "dir_globs": {
            "Linux":   ("/opt/blender-*", "~/.local/opt/blender-*"),
            "Darwin":  (),
            "Windows": (r"%ProgramFiles%\Blender Foundation\Blender *",
                        r"%LOCALAPPDATA%\Programs\Blender Foundation\Blender *"),
        },
        "install_hint": "Blender 4.2+ (5.2 LTS recommended; free, GPL-3.0, run only as a "
                        "subprocess): Linux 'scripts/install-renderers.sh blender' (pinned "
                        "official tarball, auto-discovered) or 'sudo snap install blender "
                        "--classic'; macOS 'brew install --cask blender' (or "
                        "'scripts/install-renderers.sh blender'); Windows 'winget install "
                        "BlenderFoundation.Blender' (or 'scripts/install-solvers.ps1 blender') "
                        "— or set ANKUSDRIVE_BLENDER_PATH to the blender executable",
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
        from ankusdrive.client import _freecadcmd_candidates, _resolve_freecadcmd
    except Exception:
        return []
    dirs = []
    # _resolve_freecadcmd() honours ANKUSDRIVE_FREECADCMD / PATH / the globbed installs and
    # returns the real freecadcmd; its candidates cover the not-yet-resolved installs too.
    for c in [_resolve_freecadcmd(), *_freecadcmd_candidates()]:
        d = os.path.dirname(c)
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


# --- WSL bridge (issue #193) -----------------------------------------------------
# On Windows the OpenFOAM-backed families (CFD / FSI / injection molding) run their
# bash scripts inside a WSL2 distro: the launcher swaps ["bash","-c",script] for
# ["wsl","-d",<distro>,"-e","bash","-c",script] (wsl.exe auto-maps a Windows cwd to
# /mnt/<drive>/..., so case dirs cross with zero path translation), and discovery
# probes the distro's filesystem through its \\wsl$\<distro> UNC mirror — glob-only,
# nothing is ever executed. Resolvers return POSIX paths on Windows (/usr/lib/...):
# that is the only form ever consumed (embedded into the scripts bash runs
# in-distro); the UNC form exists purely as the probe vehicle.

def _wsl_registry_distro() -> str | None:
    """Name of the default WSL distro, read from the registry
    (HKCU\\...\\Lxss -> DefaultDistribution GUID -> DistributionName). A pure
    winreg READ: never executes wsl.exe and never starts the distro VM, so
    discovery stays side-effect-free. None when no distro is registered."""
    if platform.system() != "Windows":
        return None
    try:
        import winreg
        base = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as key:
            guid, _ = winreg.QueryValueEx(key, "DefaultDistribution")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + guid) as sub:
            return winreg.QueryValueEx(sub, "DistributionName")[0]
    except OSError:
        return None


def wsl_distro() -> str | None:
    """The WSL distro AnkusDrive probes and launches into on Windows:
    ``ANKUSDRIVE_WSL_DISTRO`` (env -> config.toml) -> the registry default.
    None off-Windows or when no distro is registered."""
    if platform.system() != "Windows":
        return None
    return _config.get("ANKUSDRIVE_WSL_DISTRO") or _wsl_registry_distro()


def wsl_available() -> bool:
    """True when Windows can reach a WSL distro (wsl.exe on PATH and a registered
    distro). Side-effect-free."""
    return (platform.system() == "Windows"
            and shutil.which("wsl") is not None
            and wsl_distro() is not None)


def wsl_unc(posix_path: str, distro: str | None = None) -> str:
    """Windows-visible UNC mirror of an in-distro POSIX path:
    ``/usr/lib/...`` -> ``\\\\wsl$\\<distro>\\usr\\lib\\...``.

    CAUTION: merely statting/globbing \\\\wsl$ can auto-start the distro VM — a
    filesystem-only side effect (nothing is executed) that issue #193 accepts."""
    d = distro or wsl_distro() or ""
    return "\\\\wsl$\\" + d + posix_path.replace("/", "\\")


def wsl_posix(path: str) -> str:
    """Normalize a user-supplied path to the POSIX form embedded into in-distro
    scripts: ``\\\\wsl$\\<d>\\usr\\...`` or ``\\\\wsl.localhost\\<d>\\usr\\...`` ->
    ``/usr/...`` (either slash direction accepted). Anything else passes through
    unchanged, so ordinary Windows/POSIX paths are unaffected."""
    p = path.replace("/", "\\")
    for prefix in ("\\\\wsl$\\", "\\\\wsl.localhost\\"):
        if p.lower().startswith(prefix):
            rest = p[len(prefix):].split("\\", 1)   # ["<distro>", "usr\lib\..."]
            return "/" + (rest[1].replace("\\", "/") if len(rest) > 1 else "")
    return path


def _posix_isfile(path: str) -> bool:
    """``os.path.isfile`` that, on Windows, checks a leading-``/`` POSIX path
    through the \\\\wsl$ mirror of the resolved distro."""
    if platform.system() == "Windows" and path.startswith("/"):
        return wsl_available() and os.path.isfile(wsl_unc(path))
    return os.path.isfile(path)


def _posix_isdir(path: str) -> bool:
    """``os.path.isdir`` twin of :func:`_posix_isfile`."""
    if platform.system() == "Windows" and path.startswith("/"):
        return wsl_available() and os.path.isdir(wsl_unc(path))
    return os.path.isdir(path)


def _expand_linux_home(pattern: str) -> list:
    """``~/...`` patterns name the LINUX home when probed through \\\\wsl$
    (``expanduser`` would wrongly substitute ``C:\\Users\\...``): expand to
    ``/home/*/...`` + ``/root/...``. Non-``~`` patterns pass through."""
    if pattern == "~" or pattern.startswith("~/"):
        rest = pattern[2:]
        return [("/home/*/" + rest).rstrip("/"), ("/root/" + rest).rstrip("/")]
    return [pattern]


def _posix_glob(patterns) -> list:
    """Sorted existing POSIX paths for POSIX glob patterns. Linux/macOS: a plain
    ``expanduser`` + ``glob``. Windows: glob the \\\\wsl$ mirror of the resolved
    distro and map hits BACK to POSIX — the form the wsl-launched bash scripts
    consume. Glob-only, never executes anything (see :func:`wsl_unc`'s
    VM-auto-start caveat)."""
    import glob as _glob
    out = []
    if platform.system() == "Windows":
        if not wsl_available():
            return []
        for pat in patterns:
            for p in _expand_linux_home(pat):
                out.extend(wsl_posix(h) for h in _glob.glob(wsl_unc(p)))
    else:
        for pat in patterns:
            out.extend(_glob.glob(os.path.expanduser(pat)))
    return sorted(set(out))


def multipass_available() -> bool:
    """True when macOS can reach the Multipass substrate (the ``multipass`` CLI on
    PATH). The Darwin twin of :func:`wsl_available` and, like it, side-effect-free —
    but a weaker signal: \\\\wsl$ lets Windows *see* into the distro, while a
    Multipass VM's filesystem is opaque from the host, so this proves only that the
    substrate exists, never that a solver is provisioned inside it."""
    return platform.system() == "Darwin" and shutil.which("multipass") is not None


def foam_instance() -> str:
    """The Multipass instance name that hosts OpenFOAM on macOS (issue #193).
    ``ANKUSDRIVE_OPENFOAM_INSTANCE`` (env -> config.toml) overrides; the default
    ``openfoam`` matches openfoam.org's documented ``multipass launch -n openfoam``."""
    return _config.get("ANKUSDRIVE_OPENFOAM_INSTANCE") or "openfoam"


# --- Multipass VM state (issue #237) -------------------------------------------
# `multipass` on PATH proves the substrate exists but says nothing about the VM, so
# discovery used to collapse "no VM", "VM stopped" and "VM running but this shell is
# unwired" into one hint that told all three to provision. `multipass info` is a
# READ — it starts nothing, mutates nothing — so discovery may run it and stay
# side-effect-free. It is still a subprocess against a daemon that can hang, so it is
# timeout-bounded, cached for a few seconds (capabilities() probes every solver in a
# row), and degrades to the absent state on any failure.

_VM_INFO_TTL_S = 5.0
_vm_info_cache: dict = {}          # instance -> (monotonic_deadline, payload|None)


def _multipass_info_exec(instance: str, timeout_s: float):
    """Run ``multipass info <instance> --format json`` and return its stdout, or None
    when multipass is missing, errors (no such instance), hangs past ``timeout_s``, or
    is otherwise unreadable. Read-only: ``info`` neither starts nor changes a VM."""
    import subprocess
    try:
        proc = subprocess.run(
            ["multipass", "info", instance, "--format", "json"],
            capture_output=True, text=True, timeout=timeout_s,
            stdin=subprocess.DEVNULL)      # never steal the caller's stdin (#237/#223)
    except (OSError, subprocess.SubprocessError):
        return None                        # not installed, or hung past the timeout
    return proc.stdout if proc.returncode == 0 else None


def _multipass_info(instance: str):
    """Cached ``multipass info`` payload for ``instance`` (raw JSON text, or None).

    THE INJECTABLE SEAM: tests fake a VM state by replacing this function, which also
    bypasses the cache. ``ANKUSDRIVE_MULTIPASS_TIMEOUT_S`` bounds the read (default 5 s)
    and ``ANKUSDRIVE_MULTIPASS_CACHE_S`` the reuse window (default 5 s) — long enough to
    cover one capabilities() sweep, short enough that starting the VM shows up right
    after."""
    import time as _time
    ttl = float(_config.get("ANKUSDRIVE_MULTIPASS_CACHE_S") or _VM_INFO_TTL_S)
    now = _time.monotonic()
    hit = _vm_info_cache.get(instance)
    if hit and hit[0] > now:
        return hit[1]
    timeout_s = float(_config.get("ANKUSDRIVE_MULTIPASS_TIMEOUT_S") or 5.0)
    payload = _multipass_info_exec(instance, timeout_s)
    _vm_info_cache[instance] = (now + ttl, payload)
    return payload


def multipass_vm_state(instance: str | None = None) -> str:
    """Which of the three substrate states the OpenFOAM VM is in, read-only:

      * ``"absent"``  — not macOS, no ``multipass`` CLI, no such instance, or the
        read failed/timed out (degrade to the provision hint, never to a wrong one).
      * ``"stopped"`` — the instance exists but is not running (Stopped, Suspended,
        Starting, …): the fix is ``multipass start``, not another provisioning pass.
      * ``"running"`` — the VM is up, so anything still unresolved is this shell's
        environment.

    Never starts, stops or otherwise touches the VM."""
    if not multipass_available():
        return "absent"
    inst = instance or foam_instance()
    payload = _multipass_info(inst)
    if not payload:
        return "absent"
    import json as _json
    try:
        data = _json.loads(payload)
    except (ValueError, TypeError):
        return "absent"
    if not isinstance(data, dict):
        return "absent"
    entry = (data.get("info") or {}).get(inst) or {}
    state = str(entry.get("state") or "")
    if state == "Running":
        return "running"
    # "Deleted" (purge pending) and an unparseable/empty state are provisioning
    # problems, not start-the-VM problems.
    if not state or state in ("Deleted", "Unknown"):
        return "absent"
    return "stopped"


def bash_argv(script: str, case_dir: str | None = None) -> list:
    """The argv that runs ``script`` under bash in the caller's ``cwd`` (the case
    dir). One per-OS substrate, the same bash script inside (issue #193):

      * **POSIX/Linux** — ``["bash", "-c", script]``; the caller's ``cwd`` is used.
      * **Windows** — ``["wsl", "-d", <distro>, "-e", "bash", "-c", script]``.
        wsl.exe auto-maps a Windows ``cwd`` to ``/mnt/<drive>/...`` so callers pass
        their Windows case_dir unchanged, and ``-d`` pins the SAME distro discovery
        probed via \\\\wsl$ so probe and launch can never disagree.
      * **macOS** — ``["multipass", "exec", <inst>, "--", "bash", "-c", "cd <case>
        && " + script]``. OpenFOAM on macOS runs in a Multipass VM (openfoam.org's
        official, arm64-native substrate); ``multipass exec`` starts in the VM home,
        so the case dir is ``cd``'d into *inside* the script — the host scratch is
        expected mounted at the same absolute path in the VM (``multipass mount``),
        so the path lines up. The host ``cwd`` is then irrelevant.

    The bash-launch twin of :func:`run_argvs` (the no-shell native path SU2 uses)."""
    system = platform.system()
    if system == "Windows":
        d = wsl_distro()
        return ["wsl", *(["-d", d] if d else []), "-e", "bash", "-c", script]
    if system == "Darwin":
        import shlex
        body = f"cd {shlex.quote(case_dir)} && {script}" if case_dir else script
        return ["multipass", "exec", foam_instance(), "--", "bash", "-c", body]
    return ["bash", "-c", script]


def runs_in_substrate() -> bool:
    """True when :func:`bash_argv` wraps the script in a separate *relay* process —
    ``wsl.exe`` on Windows, ``multipass`` on macOS (issue #193) — rather than
    launching bash directly. Behind a relay, terminating the launched Popen only
    reaches the relay, not the solver inside the distro/VM, so callers that must
    actually stop the solver (FSI's ``_stop_participant``) sweep it by pidfile
    instead. False on Linux, where the Popen IS the solver's process tree."""
    return platform.system() in ("Windows", "Darwin")


def clean_wsl_text(s: str) -> str:
    """Strip the NULs wsl.exe's own UTF-16LE messages leave in text-mode capture
    (e.g. a missing-distro error) so log tails stay readable. Solver output is
    plain UTF-8 and passes through untouched; no-op off-Windows."""
    return s.replace("\x00", "") if s else s


def _substrate_override(value: str, *, is_dir: bool) -> str | None:
    """Validate a user-supplied ``ANKUSDRIVE_*`` path override for a solver that may
    live inside the platform's Linux substrate (issue #193) — the OpenFOAM-backed
    families: CFD, the preCICE FSI stack, and injection-molding fill.

    Windows normalizes a \\\\wsl$ override and checks it through the mirror; Linux
    checks the host directly. macOS trusts an absolute POSIX override as an in-VM
    path when ``multipass`` is present — the Multipass VM filesystem is opaque from
    the host, so the override (which the provisioning step sets to a VM path) can't
    be stat'd from macOS, and rejecting it would leave every OpenFOAM-exclusive
    family permanently unresolvable there. Returns the normalized path, or None if
    it doesn't check out."""
    p = wsl_posix(value)                     # \\wsl$ overrides normalize to POSIX
    if _posix_isdir(p) if is_dir else _posix_isfile(p):
        return p
    if multipass_available() and p.startswith("/"):
        return p                             # in-VM path; host can't confirm it
    return None


def _override_candidates(name: str, spec: dict) -> list:
    """The user-supplied path overrides for a binary solver, in precedence order:
    ``ANKUSDRIVE_<NAME>_PATH`` then the spec's ``env_aliases`` (the documented
    per-solver variable names, e.g. the FSI stack's ANKUSDRIVE_CCX_PRECICE). Each
    resolves through the config layer (env -> config.toml). No existence check."""
    out = []
    for var in (f"ANKUSDRIVE_{name.upper()}_PATH", *spec.get("env_aliases", ())):
        if value := _config.get(var):
            out.append(value)
    return out


def _version_key(path: str) -> list:
    """Natural sort key, so `Blender 10.0` sorts after `Blender 9.9`."""
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path)]


def _versioned_dirs(patterns) -> list:
    """Existing directories matching a spec's ``dir_globs`` (``~`` and ``%VAR%``
    expanded), newest version first. An unset ``%VAR%`` drops its pattern rather
    than globbing a literal ``%ProgramFiles%`` directory."""
    import glob as _glob
    out = []
    for pat in patterns:
        pat = os.path.expandvars(os.path.expanduser(pat))
        if "%" in pat or "$" in pat:
            continue
        out.extend(sorted((d for d in _glob.glob(pat) if os.path.isdir(d)),
                          key=_version_key, reverse=True))
    return out


def _binary_candidates(name: str, spec: dict) -> list:
    """Ordered candidate paths for a binary solver, most-preferred first:
    ANKUSDRIVE_<NAME>_PATH env override -> PATH (shutil.which) -> common per-OS
    install dirs -> FreeCAD's bundled bin/ (for ``freecad_bundled`` solvers). Pure
    lookup — no side effects, no existence check (the caller filters). Mirrors
    worker.py's _renderer_exec_candidates, minus the FreeCAD prefs step."""
    candidates = list(_override_candidates(name, spec))  # 1) env -> config file
    for binname in spec["binaries"]:                 # 2) PATH (honors Windows PATHEXT)
        if found := shutil.which(binname):
            candidates.append(found)
    for d in spec["dirs"].get(platform.system(), ()):  # 3) common install dirs
        for binname in spec["binaries"]:
            for exe in (binname, binname + ".exe"):
                candidates.append(os.path.join(d, exe))
    for d in _versioned_dirs(spec.get("dir_globs", {}).get(platform.system(), ())):
        for binname in spec["binaries"]:             # 3b) versioned install dirs
            for exe in (binname, binname + ".exe"):
                candidates.append(os.path.join(d, exe))
    # 4) the provisioners' portable extracts: scripts/install-solvers.ps1 unzips
    #    SU2/PrusaSlicer/Elmer under %LOCALAPPDATA%\AnkusDrive\solvers, and
    #    scripts/install-solvers.sh (Darwin) unzips SU2 under
    #    ~/Library/Application Support/AnkusDrive/solvers — so a provisioned box
    #    resolves them with NO env var, the way an MCP host with a minimal
    #    environment launches `ankusdrive mcp` (issue #205 / #199 / #194).
    #    Both the current name and the pre-rename "DriftPin" one are probed: a box
    #    provisioned by <=0.4.x has real binaries on disk under the old directory,
    #    and dropping it would silently un-resolve a solver that is still there
    #    (#295). New provisioning only ever writes the new path. Deprecated: 0.6.
    prov_bases = []
    if platform.system() == "Windows" and (lad := os.environ.get("LOCALAPPDATA")):
        prov_bases = [os.path.join(lad, n, "solvers") for n in ("AnkusDrive", "DriftPin")]
    elif platform.system() == "Darwin":
        prov_bases = [os.path.expanduser(f"~/Library/Application Support/{n}/solvers")
                      for n in ("AnkusDrive", "DriftPin")]
    if prov_bases:
        import glob as _glob
        suffix = ".exe" if platform.system() == "Windows" else ""
        for prov_base in prov_bases:
            for binname in spec["binaries"]:
                candidates.extend(sorted(_glob.glob(
                    os.path.join(prov_base, "*", "**", binname + suffix),
                    recursive=True), reverse=True))   # newest versioned dir first
    if spec.get("freecad_bundled"):                  # 5) FreeCAD's bundled bin/ (ccx, gmsh)
        for d in _freecad_bundled_bin_dirs():
            for binname in spec["binaries"]:
                for exe in (binname, binname + ".exe"):
                    candidates.append(os.path.join(d, exe))
    # 6) inside the WSL distro (issue #193): the OpenFOAM-backed families run via
    #    `wsl -e bash` on Windows, so an in-distro binary counts as resolvable.
    #    Probed glob-only through the \\wsl$ mirror; the candidates are the POSIX
    #    paths the in-distro bash scripts consume. (No macOS twin: a Multipass VM's
    #    filesystem is opaque from the host, so there is nothing to glob — the
    #    in-VM binary resolves only from an explicit override, see _vm_binary_path.)
    if platform.system() == "Windows" and spec.get("substrate_bins") and wsl_available():
        for d in spec["substrate_bins"]:
            for binname in spec["binaries"]:
                candidates.extend(_posix_glob((d + "/" + binname,)))
    return candidates


def _binary_path(name: str, spec: dict):
    """First existing candidate path for a binary solver, or None. Side-effect-free
    (does not mutate any environment) — safe for the capabilities probe."""
    for c in _binary_candidates(name, spec):
        if c and _posix_isfile(c):
            return c
    return None


def _vm_binary_path(name: str, spec: dict):
    """macOS only: the in-VM path of a substrate solver, taken from an explicit
    override (issue #193). OpenFOAM and the preCICE stack live inside the Multipass
    VM, whose filesystem the host cannot stat or glob — so unlike WSL there is no
    discovery here, only trust: an absolute POSIX override for a substrate solver
    (``substrate_bins``) is accepted when ``multipass`` is present, because that is
    exactly the path ``bash_argv`` will hand to ``multipass exec``. Returns the path
    or None; always None off macOS, where :func:`_binary_path` governs."""
    if not (spec.get("substrate_bins") and multipass_available()):
        return None
    for c in _override_candidates(name, spec):
        if r := _substrate_override(c, is_dir=False):
            return r
    return None


# --- installed-but-unwired probe (issue #177) ----------------------------------
# A solver's discovery can be *env-scoped*: its binary only lands on PATH after an
# etc/bashrc is sourced (OpenFOAM), or its python lives in a dedicated venv reached
# via a ANKUSDRIVE_*_PYTHON env var (openEMS, bempp). In a bare shell — env unset —
# find_solver() then reports it flatly absent even though it is installed and green
# in CI. These probes look, READ-ONLY (never source a bashrc, never set an env var),
# for a well-known artifact that proves "installed but not wired into this shell",
# so discovery can report the third state ``unwired`` with the exact wire-up hint.

def _repo_root() -> str:
    """Repo root used to locate the dedicated per-solver venvs (``.venv-openems``,
    ``.venv-bempp``) beside the checkout. Honors ``ANKUSDRIVE_REPO_ROOT`` — a read-only
    discovery override tests point at a tmp dir — else the checkout holding this file."""
    return _config.get("ANKUSDRIVE_REPO_ROOT") or \
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _standard_bashrc(cfg: dict):
    """Probe standard OpenFOAM install prefixes for an ``etc/bashrc`` (the unwired
    evidence). ``ANKUSDRIVE_OPENFOAM_DIRS`` (a pathsep-joined list of install prefixes;
    a read-only discovery override tests point at a tmp dir) is searched first, each
    prefix for ``<prefix>/etc/bashrc``; then the entry's standard-location globs.
    Returns the bashrc path or None. Side-effect-free."""
    roots = _config.get("ANKUSDRIVE_OPENFOAM_DIRS")
    if roots:
        for root in roots.split(os.pathsep):     # POSIX prefixes carry no ';'
            root = wsl_posix(root)
            cand = (root.rstrip("/") + "/etc/bashrc" if root.startswith("/")
                    else os.path.join(root, "etc", "bashrc"))
            if _posix_isfile(cand):
                return cand
    for pat in cfg["globs"]:
        hits = _posix_glob((pat,))
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
            # a POSIX hit on Windows came through the \\wsl$ mirror — the wire-up
            # hint must say the solver runs in-distro (issue #193). The Linux hint
            # string stays byte-identical (pinned by test_solve_degradation).
            wsl_hit = platform.system() == "Windows" and found.startswith("/")
            tmpl = cfg.get("hint_wsl") if wsl_hit else None
            return found, (tmpl or cfg["hint"]).format(found=found,
                                                       distro=wsl_distro())
        # macOS: no host-visible bashrc — OpenFOAM lives in the Multipass VM (#193).
        # `multipass` on PATH is the read-only signal the substrate is present (the
        # solver inside the VM can't be confirmed without executing it). WHICH hint
        # is right depends on the VM, so read its state (issue #237): telling someone
        # whose VM is provisioned and running to go provision one is the single most
        # common way this hint used to be wrong.
        if cfg.get("darwin_multipass") and multipass_available():
            inst = foam_instance()
            state = multipass_vm_state(inst)
            tmpl = cfg.get(f"darwin_multipass_{state}") or cfg["darwin_multipass"]
            found_at = ("multipass" if state == "absent"
                        else f"multipass VM {inst!r} ({state})")
            return found_at, tmpl.format(inst=inst)
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
        ``found_at`` + ``wire_hint`` (the exact ``set ANKUSDRIVE_…`` fix).
      * ``absent`` — no evidence it is installed anywhere; ``install_hint`` is the
        install path.

    A solver AnkusDrive cannot build a case for additionally carries
    ``prepared_case_only`` (issue #237) — the reason string, always present when the
    registry declares it, resolved or not. It still runs (``require_solver`` is
    unaffected: a hand-prepared ``case_dir`` is a legitimate way to use it), but it
    cannot satisfy its family on its own — see :func:`capabilities`.

    Raises ValueError for an unknown name."""
    spec = _spec(name)
    info = {
        "name": name,
        "kind": spec["kind"],
        "family": spec["family"],
        "extra": spec["extra"],
        "available": False,
    }
    if spec.get("prepared_case_only"):
        info["prepared_case_only"] = spec["prepared_case_only"]
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
            if platform.system() == "Windows" and path.startswith("/"):
                # resolved inside the WSL distro — the runner must launch it via
                # `wsl -e bash` (bash_argv), never a native subprocess (issue #193)
                info["via"] = "wsl"
        elif (vm_path := _vm_binary_path(name, spec)) is not None:
            # macOS: nothing on the host, but an explicit override names the binary
            # inside the Multipass VM — launched via `multipass exec` (bash_argv).
            # Tagged from the resolution branch, not the path shape: a plain macOS
            # host path is absolute POSIX too (issue #193).
            info["available"] = True
            info["path"] = vm_path
            info["via"] = "multipass"
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
    info["install_hint"] = install_hint(name)
    return info


def install_hint(name: str) -> str:
    """``name``'s install hint, written for how THIS AnkusDrive was installed (#347).

    The registry strings are the plain-venv form (``pip install 'ankusdrive[X]'``);
    under pipx / uv tool / uvx / the Claude Desktop extension that command installs
    into a different Python and changes nothing, so it is rewritten here — the one
    place every ``install_hint`` and ``require_solver`` ``install`` field comes from.
    Raises ValueError for an unknown name."""
    return _install_kind.adapt(_spec(name)["install_hint"])


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
    ``install`` is the ``set ANKUSDRIVE_…`` fix and ``found_at`` names the evidence).
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
        if "via" in info:
            out["via"] = info["via"]
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
    ``ANKUSDRIVE_OPENFOAM_BASHRC`` env -> ``$WM_PROJECT_DIR/etc/bashrc`` -> the source-
    build layout next to the resolved binary (``<foamdir>/platforms/.../bin`` ->
    ``<foamdir>/etc/bashrc``) -> common install dirs (incl. the apt
    ``/usr/share/openfoam`` layout). Returns the path, or None when none resolves."""
    env = _config.get("ANKUSDRIVE_OPENFOAM_BASHRC")
    if env:
        # normalized + checked per substrate: \\wsl$ -> POSIX through the mirror on
        # Windows, host stat on Linux, trusted as an in-VM path on macOS (#193)
        if r := _substrate_override(env, is_dir=False):
            return r
    wm = os.environ.get("WM_PROJECT_DIR")
    if wm:
        cand = os.path.join(wm, "etc", "bashrc")
        if os.path.isfile(cand):
            return cand
    binpath = find_solver("openfoam").get("path")
    if binpath:
        # source builds: <foamdir>/platforms/<arch>/bin/<app> -> <foamdir>/etc/bashrc
        if binpath.startswith("/") and platform.system() == "Windows":
            # in-distro (WSL) resolution — keep it POSIX (realpath would mangle it)
            if "/platforms/" in binpath:
                cand = binpath.split("/platforms/")[0] + "/etc/bashrc"
                if _posix_isfile(cand):
                    return cand
        else:
            marker = os.sep + "platforms" + os.sep
            real = os.path.realpath(binpath)
            if marker in real:
                cand = os.path.join(real.split(marker)[0], "etc", "bashrc")
                if os.path.isfile(cand):
                    return cand
    for pat in ("/usr/share/openfoam/etc/bashrc",
                "/usr/lib/openfoam/openfoam*/etc/bashrc",
                "/opt/openfoam*/etc/bashrc", "/opt/OpenFOAM*/etc/bashrc"):
        hits = _posix_glob((pat,))
        if hits:
            return hits[-1]
    return None


# --- FSI (preCICE OpenFOAM<->CalculiX) stack resolvers -------------------------
# Side-effect-free path resolution for the three pieces the partitioned solve
# needs at run time. Each honours a ANKUSDRIVE_* env override first (the documented
# install path), then a small set of build-default locations, mirroring the
# openfoam_bashrc() resolution style. The FSI handler/runner consumes these.

def ccx_precice_bin() -> str | None:
    """The preCICE-enabled CalculiX solver (``ccx_preCICE``) — the solid
    participant. ANKUSDRIVE_CCX_PRECICE / ANKUSDRIVE_PRECICE_PATH env -> the registry
    binary resolution (~/calculix-adapter/bin etc.). Returns the path or None."""
    if env := _config.get("ANKUSDRIVE_CCX_PRECICE"):
        if r := _substrate_override(env, is_dir=False):
            return r
    return find_solver("precice").get("path")


def precice_lib_dir() -> str | None:
    """Directory holding ``libprecice.so`` (the serial, MPI-off build that the
    adapters link). ANKUSDRIVE_PRECICE_LIB env -> the conda-forge env lib ->
    the documented source-build prefix. Returns the dir or None."""
    if env := _config.get("ANKUSDRIVE_PRECICE_LIB"):
        if r := _substrate_override(env, is_dir=True):
            return r
    for base in ("~/precice-serial/lib",
                 "~/miniforge3/envs/precice/lib",
                 "~/miniconda3/envs/precice/lib",
                 "/usr/local/lib", "/usr/lib/x86_64-linux-gnu"):
        hits = _posix_glob((base + "/libprecice.so*",))
        if hits:
            return os.path.dirname(hits[0])  # ntpath.dirname handles '/' fine
    return None


def openfoam_adapter_lib_dir() -> str | None:
    """Directory holding ``libpreciceAdapterFunctionObject.so`` (the OpenFOAM
    function-object adapter the fluid participant loads). ANKUSDRIVE_OPENFOAM_ADAPTER_LIB
    env -> the wmake user-lib build prefix. Returns the dir or None."""
    if env := _config.get("ANKUSDRIVE_OPENFOAM_ADAPTER_LIB"):
        if r := _substrate_override(env, is_dir=True):
            return r
    hits = _posix_glob(
        ("~/OpenFOAM/*/platforms/*/lib/libpreciceAdapterFunctionObject.so",))
    if hits:
        return os.path.dirname(hits[0])      # ntpath.dirname handles '/' fine
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

    Resolution: ``ANKUSDRIVE_FSI_OPENFOAM_BASHRC`` env -> the install whose version
    matches the adapter lib path (``~/OpenFOAM/<user>-v2512/...`` -> the
    ``…openfoam2512…`` / ``…-v2512…`` bashrc) -> the general ``openfoam_bashrc()``."""
    if env := _config.get("ANKUSDRIVE_FSI_OPENFOAM_BASHRC"):
        if r := _substrate_override(env, is_dir=False):
            return r
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
                         f"~/OpenFOAM/OpenFOAM-v{ver}/etc/bashrc",
                         f"/opt/openfoam{ver}/etc/bashrc",
                         f"/opt/OpenFOAM-v{ver}/etc/bashrc",
                         f"/usr/share/openfoam{ver}/etc/bashrc"):
                hits = _posix_glob((cand,))
                if hits:
                    return hits[-1]
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
    Resolution is the registry's standard order — ``ANKUSDRIVE_CALCULIX_PATH`` env ->
    PATH (``shutil.which``) -> common install dirs — via the ``calculix`` spec.
    Returns the path or None."""
    return _binary_path("calculix", _SOLVERS["calculix"])


def openinjmoldsim_bin() -> str | None:
    """Locate the ``openInjMoldSim`` executable (the GPL-3.0 injection-molding solver,
    a modified compressibleInterFoam built against OpenFOAM 7 .org).

    This is the higher-fidelity twin the molding-fill family prefers when present;
    when it is absent the family degrades to ``interFoam`` on the existing
    OpenFOAM (.com/ESI) — see ``ankusdrive/analysis/molding_fill.py`` and
    ``tools/build_openinjmoldsim.sh``. Side-effect-free resolution:
    ``ANKUSDRIVE_OPENINJMOLDSIM`` / ``ANKUSDRIVE_OPENINJMOLDSIM_PATH`` env -> PATH
    (``shutil.which``) -> the documented source-build prefix
    (``~/opt/openInjMoldSim/...``). Returns the path, or None when it does not
    resolve (the common case until the OF7-org build lands).

    Substrate-aware (issue #193): the globs probe the WSL distro through \\\\wsl$ on
    Windows, and on macOS — where OF7-org can only be built inside the Multipass VM,
    invisible to the host — the env override is trusted as an in-VM path. Either
    way the returned POSIX path is what ``bash_argv`` runs inside the substrate."""
    for var in ("ANKUSDRIVE_OPENINJMOLDSIM", "ANKUSDRIVE_OPENINJMOLDSIM_PATH"):
        env = _config.get(var)
        if env:
            # \\wsl$ -> POSIX on Windows; in-VM (Multipass) path trusted on macOS,
            # where the OF7-org build is only ever reachable inside the VM (#193)
            if r := _substrate_override(env, is_dir=False):
                return r
    found = shutil.which("openInjMoldSim")
    if found:
        return found
    for pat in ("~/opt/openInjMoldSim/*/bin/openInjMoldSim",
                "~/OpenFOAM/*/platforms/*/bin/openInjMoldSim",
                "/opt/openInjMoldSim/*/bin/openInjMoldSim"):
        hits = _posix_glob((pat,))
        if hits:
            return hits[-1]
    return None


def openinjmoldsim_bashrc() -> str | None:
    """The OpenFOAM-7 (.org) environment file to source before running
    ``openInjMoldSim`` — distinct from ``openfoam_bashrc()`` (which resolves the
    ESI v19xx/v25xx build). ``ANKUSDRIVE_OPENINJMOLDSIM_BASHRC`` env -> the OF7-org
    source-build / apt layouts. Returns the path or None."""
    env = _config.get("ANKUSDRIVE_OPENINJMOLDSIM_BASHRC")
    if env:
        if r := _substrate_override(env, is_dir=False):   # \\wsl$ / in-VM (#193)
            return r
    for pat in ("~/OpenFOAM/OpenFOAM-7/etc/bashrc",
                "~/opt/OpenFOAM-7/etc/bashrc",
                "/opt/openfoam7/etc/bashrc",
                "/usr/lib/openfoam/openfoam7/etc/bashrc"):
        hits = _posix_glob((pat,))
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

    ``any_available`` is the family gate, and it answers "can AnkusDrive actually
    DRIVE this family here?", not merely "did some binary resolve?" (issue #237). A
    solver the registry marks ``prepared_case_only`` — it resolves, but nothing in
    AnkusDrive can BUILD a case for it, so only a hand-prepared ``case_dir`` reaches it
    — is listed in the family's ``available`` and ``prepared_case_only`` but does NOT
    set ``any_available``. Without that, macOS reported the cfd family available on a
    bare SU2 install, and an agent following ``cfd_pipe_flow``'s ``escalate_to`` hint
    dead-ended: every built-in CFD case mode emits OpenFOAM dictionaries.

    Returns ``{platform, available (sorted ready solver names), unwired (sorted
    installed-but-unwired names, issue #177), prepared_case_only (sorted resolved-but-
    undrivable names, issue #237), solvers: {name: {available, status, kind, family,
    extra, and either path/module or install_hint (+found_at/wire_hint when unwired,
    +prepared_case_only when undrivable)}}, families: {family: {solvers, available,
    unwired, prepared_case_only, any_available}}, extras: {extra: [solver names]}}``."""
    solvers = {name: find_solver(name) for name in _SOLVERS}

    families: dict = {}
    for name, info in solvers.items():
        fam = families.setdefault(
            info["family"],
            {"solvers": [], "available": [], "unwired": [],
             "prepared_case_only": [], "any_available": False},
        )
        fam["solvers"].append(name)
        if info["available"]:
            fam["available"].append(name)
            if info.get("prepared_case_only"):
                fam["prepared_case_only"].append(name)
            else:
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
        "prepared_case_only": sorted(
            n for n, i in solvers.items()
            if i["available"] and i.get("prepared_case_only")),
        "solvers": solvers,
        "families": families,
        "extras": {k: sorted(v) for k, v in extras.items()},
    }
