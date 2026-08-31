# Installing solvers on macOS (Apple Silicon)

Verified end-to-end on macOS 26.5 / arm64, FreeCAD 1.1.1, 2026-08-30. Every command
below was actually run on that machine; the per-family results are what
`solve_capabilities` reported afterwards, not what the packages claim.

> **Read this first — the one mistake that costs an afternoon.**
> Wheel-backed solvers must be installed into **FreeCAD's bundled Python**, not into
> the repo `.venv`. See [Which interpreter](#which-interpreter) below.

## Which interpreter

AnkusDrive runs two different Pythons, and solver discovery happens in the second one:

| | Path | Version | Runs |
|---|---|---|---|
| MCP server | `.venv/bin/ankusdrive` | 3.14.6 | the JSON-RPC server, tool dispatch |
| **Worker** | `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd` | **3.11.14** | FreeCAD, **and every solver probe** |

`solve_capabilities` is a *worker* handler (`ankusdrive/worker.py:12445`), so the
`find_spec` probe in `ankusdrive/solvers.py:452` runs inside **FreeCAD's Python 3.11**.
A wheel installed into the 3.14 venv is invisible to it — and cannot be bridged with
`PYTHONPATH`, because 3.11 cannot load 3.14 wheels (mujoco is a compiled `cp314`
extension).

`scripts/install-solvers.sh:95` defaults `PY` to `.venv/bin/python3`, which is wrong on
macOS. **Always override it:**

```bash
PY=/Applications/FreeCAD.app/Contents/Resources/bin/python \
  bash scripts/install-solvers.sh mbd topology optics
```

The same trap exists on Windows (FreeCAD bundles its own Python there too). On Linux it
depends on how FreeCAD was installed — see [SOLVERS_LINUX.md](SOLVERS_LINUX.md).

FreeCAD's `bin/pip` shim is broken (`ImportError: No module named _internal.cli.main`) —
use `python -m pip`.

## Result on the reference machine

8 of 12 families working — 7 reported by `solve_capabilities`, plus `acoustics_bem`,
which functions but reports `unwired` (see the [reporting gap](#the-unwired-reporting-gap)).
Before this pass only 3 were.

| Family | Status | Backend |
|---|---|---|
| `warpage` | ✅ | CalculiX — bundled with FreeCAD, zero setup |
| `slicing` | ✅ | PrusaSlicer.app |
| `cfd` | ✅ | OpenFOAM v2512 (Multipass VM) + SU2 8.5.0 |
| `mbd` | ✅ | mujoco 3.12.0 |
| `topology` | ✅ | solidspy |
| `optics` | ✅ | optiland 0.6.2 |
| `optics_nonseq` | ✅ | KrakenOS 1.0.0.24 |
| `acoustics_bem` | ⚠️ works, reports "unwired" | bempp-cl 0.4.2 — see [caveat](#the-unwired-reporting-gap) |
| `thermal_transient` | ❌ | Elmer — no prebuilt binaries; buildable from source, see [appendix](#appendix-building-elmer-on-apple-silicon) |
| `dem` | ❌ | YADE — Linux-only build recipe |
| `em_fullwave` | ❌ | openEMS — Linux-only build recipe |
| `fsi` | ❌ | preCICE — Linux-only build recipe |

## 1. Wheel solvers — into FreeCAD's Python

**Pin `numpy<2` on every install.** Unconstrained, pip upgrades FreeCAD's numpy
1.26.4 → 2.4.6 inside the app bundle, and numpy 2's ABI break takes FreeCAD's compiled
extensions with it.

```bash
FCPY="/Applications/FreeCAD.app/Contents/Resources/bin/python"

# mbd (mujoco — PyBullet ships no macOS wheels), topology, optics
$FCPY -m pip install "numpy<2" mujoco solidspy optiland

# optics_nonseq (GPL-3.0; AnkusDrive only ever runs it in a subprocess)
$FCPY -m pip install "numpy<2" KrakenOS "setuptools<81"
```

Verify numpy survived:

```bash
$FCPY -c "import numpy; print(numpy.__version__)"   # must still print 1.26.4
```

### Do NOT install rayoptics

`rayoptics` is the only package that drags a second Qt binding into FreeCAD's
site-packages — unpinned it wants PySide6 6.11.2 over FreeCAD's bundled 6.8.3; pinned to
`numpy<2` it resolves to rayoptics 0.9.3, which wants **PyQt5** inside a Qt6 app. Either
risks breaking the FreeCAD install.

You don't need it. The `optics` family gate is `any_available` across
`("rayoptics", "optiland")`, so **optiland alone turns the family on** — and it pulls no
Qt at all.

### `topopt` is not needed either

The solver entry declares `"modules": ("topopt", "solidspy")`
(`ankusdrive/solvers.py:74`) — *either* satisfies it, and the real implementation
(`ankusdrive/analysis/topology.py`) runs on **solidspy**. Don't chase the `topopt` PyPI
package: its sdist imports itself at build time, so it demands cvxopt, then `nlopt`,
which has no cp311 arm64 wheel and whose CMake build fails under CMake 4.x.

## 2. Binary solvers

```bash
# SU2 — official binary into ~/Library/Application Support/AnkusDrive/solvers,
# auto-discovered with no env var. x86_64, runs under Rosetta 2.
bash scripts/install-solvers.sh su2

# PrusaSlicer — app bundle is auto-discovered
brew install --cask prusaslicer
```

CalculiX needs nothing: FreeCAD ships `ccx` (and `gmsh`) in
`Contents/Resources/bin`, and `solvers.py` globs it.

## 3. OpenFOAM via Multipass

There is no native macOS OpenFOAM. AnkusDrive drives a Linux VM instead: `bash_argv`
(`ankusdrive/solvers.py:707`) swaps `["bash","-c",script]` for
`["multipass","exec",<vm>,"--","bash","-c","cd <case> && …"]`.

The mechanism is opt-in per solver via a `substrate_bins` key — **only `openfoam` and
`precice` declare one**. Elmer, YADE and openEMS have no substrate path, so putting them
in the VM would not help; they must run natively, and no macOS recipe exists.

```bash
bash scripts/install-solvers.sh multipass          # provision the VM
multipass shell openfoam
  bash scripts/install-solvers.sh cfd              # OpenFOAM inside the VM
```

### The mount must be an identity mount

The worker `cd`s into `case_dir` **inside the VM** (`worker.py:17782`), so the case
directory needs to exist at the *same absolute path* on both sides:

```bash
multipass mount /Users/you/fsi-run openfoam:/Users/you/fsi-run
```

Then point `TMPDIR` at the host side, so Python's `tempfile` builds cases there.

### Wiring it up persistently

`ANKUSDRIVE_*` vars belong in `~/.config/ankusdrive/config.toml` — MCP hosts spawn the
server with a minimal environment, so shell exports do not reach it. Keys map as
`ANKUSDRIVE_FOO_BAR` → `[solvers] foo_bar` (`ankusdrive/config.py:166`).

```toml
freecadcmd = "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd"

[solvers]
openfoam_bashrc = "/usr/lib/openfoam/openfoam2512/etc/bashrc"
openfoam_path = "/usr/lib/openfoam/openfoam2512/platforms/linuxARM64GccDPInt32Opt/bin/simpleFoam"
bempp_python = "/Users/you/AnkusDrive/.venv-bempp/bin/python3"
```

These are **in-VM paths**. macOS never `stat()`s them — the VM filesystem is opaque from
the host — it embeds them in the script it hands to `multipass exec`.

`TMPDIR` is not an `ANKUSDRIVE_*` var, so config.toml cannot carry it. It has to be in
the server's process environment — in Claude Code, the `env` block of the MCP entry in
`~/.claude.json`:

```json
"ankusdrive": {
  "type": "stdio",
  "command": "/Users/you/AnkusDrive/.venv/bin/ankusdrive",
  "args": ["mcp"],
  "env": {
    "TMPDIR": "/Users/you/fsi-run",
    "ANKUSDRIVE_OPENFOAM_BASHRC": "/usr/lib/openfoam/openfoam2512/etc/bashrc",
    "ANKUSDRIVE_OPENFOAM_PATH": "/usr/lib/openfoam/openfoam2512/platforms/linuxARM64GccDPInt32Opt/bin/simpleFoam"
  }
}
```

Smoke-test the bridge:

```bash
multipass exec openfoam -- bash -c \
  'source /usr/lib/openfoam/openfoam2512/etc/bashrc && simpleFoam -help | head -3'
```

## 4. bempp (exterior acoustics)

bempp-cl needs `meshio>=5`; solidspy pins `meshio==3` in FreeCAD's Python. So it gets its
own venv and is driven out-of-process. This is a *dependency* clash, not a licence one —
bempp is MIT.

Use **Python 3.11**, not 3.14: bempp pulls numba, which has no 3.14 wheels yet.

```bash
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m venv .venv-bempp
.venv-bempp/bin/pip install bempp-cl gmsh "meshio>=5"
```

Then set `bempp_python` in config.toml (above).

### The "unwired" reporting gap

bempp will report **`unwired`** even when correctly installed and wired. This is a
reporting bug, not a broken install:

- the availability probe is `find_spec("bempp_cl")` **in the worker interpreter**, which
  by design does not have it;
- the *execution* gate is `_bempp_python()` (`worker.py:15972`), which does read
  `ANKUSDRIVE_BEMPP_PYTHON`.

So `acoustic_radiation_submit` works while `acoustics_bem.any_available` reads `false`.
Verify the real state directly:

```bash
/Applications/FreeCAD.app/Contents/Resources/bin/python -c "
import sys, subprocess; sys.path.insert(0,'/Users/you/AnkusDrive')
from ankusdrive import config
exe = config.get('ANKUSDRIVE_BEMPP_PYTHON'); print('resolved:', exe)
print(subprocess.run([exe,'-c','import bempp_cl;print(\"child import OK\")'],
                     capture_output=True, text=True).stdout)"
```

The same gap applies to `openems` (`ANKUSDRIVE_OPENEMS_PYTHON`) and `kraken`
(`ANKUSDRIVE_OPTICS_GPL_PYTHON`) wherever those live in a separate venv.

## 5. What is genuinely unavailable on macOS

| Family | Solver | Why |
|---|---|---|
| `thermal_transient` | Elmer | No prebuilt binaries anywhere — but it **does** build on Apple Silicon. See [Building Elmer](#appendix-building-elmer-on-apple-silicon). |
| `dem` | YADE | `build_dem_gpl` is hard-gated to Linux (apt toolchain: boost, CGAL, VTK, metis). |
| `em_fullwave` | openEMS | `build_openems` is hard-gated to Linux (apt + `update_openEMS.sh`). |
| `fsi` | preCICE | `build_fsi` is hard-gated to Linux. *In principle* reachable via Multipass — preCICE is one of the two solvers declaring `substrate_bins` — but the recipe (libprecice + CalculiX adapter + OpenFOAM adapter, 4 env vars) has not been run in the VM. |

Note `scripts/install-solvers.sh` still prints "macOS: Docker only — the runner glue is
Linux-only (issue #193)" for OpenFOAM. That guidance is **stale**: the Multipass path in
`solvers.py` works, as section 3 demonstrates.

## Restart after wiring

Config and MCP `env` changes are read at server start, and `config.load()` caches
per-process. Restart Claude Code (or your MCP host) before expecting
`solve_capabilities` to reflect them.

## Fragility

Everything in section 1 lives **inside `/Applications/FreeCAD.app`**. A FreeCAD reinstall
or update wipes it. Re-run section 1 afterwards.

---

# Appendix: building Elmer on Apple Silicon

The `thermal_transient` family (plus CHT, low-frequency EM, and acoustic/harmonic FEM)
runs on Elmer. macOS is the only platform where AnkusDrive cannot hand you a binary — but
"no binaries" is not "does not work". Elmer builds and passes its own test suite on
arm64; it just has to be compiled.

## Why there is no package

Checked 2026-08-30, all four negative:

| Source | Result |
|---|---|
| Homebrew | no formula — `brew info elmerfem` → *No available formula* |
| conda-forge | no package (the conda `elmer` hits are an unrelated bioinformatics package) |
| CSC's official binary mirror `nic.funet.fi/pub/sci/physics/elmer/bin/` | only `windows/` and `linux/` (the latter last touched 2016) — **no macOS directory** |
| GitHub releases (`release-26.2`) | source tarball/zip only, no platform binaries |

The Windows portable zip that `install-solvers.ps1` downloads
(`ElmerFEM-nogui-nompi-Windows-AMD64.zip`) comes from that same CSC mirror, which is
precisely why Windows gets Elmer for free and macOS does not.

## It is supported on arm64 — upstream CI proves it

`ElmerCSC/elmerfem` runs a dedicated
[`build-macos-homebrew.yaml`](https://github.com/ElmerCSC/elmerfem/blob/devel/.github/workflows/build-macos-homebrew.yaml)
workflow on **`macos-14` (ARM64)** — against both OpenBLAS and Apple's Accelerate — plus
`macos-15-intel`. It builds and then runs `ctest`. That workflow, not the prose page, is
the recipe to trust.

⚠️ The repo's own
[`compilation_instructions/macOS.md`](https://github.com/ElmerCSC/elmerfem/blob/devel/compilation_instructions/macOS.md)
carries a "may be out of date" disclaimer and **is**: it still tells you to
`brew install cartr/qt4/qt@4` for the GUI, while CI has moved to `-DWITH_QT6=ON`. Ignore
its Qt instructions. For our purposes the GUI is off anyway.

## What AnkusDrive actually needs

Not just `ElmerSolver`. The worker resolves two companions **as siblings in the same
directory** via `solvers.sibling_bin` (`worker.py:16398`):

| Binary | Used for |
|---|---|
| `ElmerSolver` | the solve itself (`binaries: ("ElmerSolver", "ElmerSolver_mpi")`) |
| `ElmerGrid` | converts the Gmsh UNV mesh — without it the family degrades to `{ok: false}` (`worker.py:16460`) |
| `ViewFactors` | radiation view factors; `thermal_radiation_submit` runs **ViewFactors then ElmerSolver** (`worker.py:16936`) |

So do not cherry-pick `ElmerSolver` out of the build tree. Install the whole prefix and
keep the three together — the same layout `install-solvers.ps1` checks for on Windows.

## Recipe (serial, no GUI)

MPI is not needed: `ElmerSolver_mpi` is accepted but the serial `ElmerSolver` is what the
Windows package ships (`nogui-nompi`), so match it.

```bash
brew install gcc cmake openblas suitesparse libomp

# Prefer the release tarball (~71 MB) over `git clone`.
curl -fsSL --retry 3 -o elmer-26.2.tar.gz \
  https://github.com/ElmerCSC/elmerfem/archive/refs/tags/release-26.2.tar.gz
tar xzf elmer-26.2.tar.gz
cd elmerfem-release-26.2 && mkdir build && cd build

export CMAKE_PREFIX_PATH=/opt/homebrew/opt/openblas   # openblas is keg-only

cmake .. \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/opt/homebrew \
  -DCMAKE_C_COMPILER=clang \
  -DCMAKE_CXX_COMPILER=clang++ \
  -DCMAKE_Fortran_COMPILER=gfortran \
  -DHOMEBREW_PREFIX=/opt/homebrew \
  -DWITH_MPI=FALSE \
  -DWITH_OpenMP=FALSE \
  -DWITH_ELMERGUI=FALSE

cmake --build . -j$(sysctl -n hw.logicalcpu)
sudo cmake --install .
```

**This configure is verified.** Run on this machine against `release-26.2` it completes
clean:

```
--   Fortran compiler:        /opt/homebrew/bin/gfortran
--   Fortran flags:            -fallow-argument-mismatch -O3
--   C compiler:              /usr/bin/clang
--   Package filename: elmerfem-26.2--20260830_Darwin-arm64
-- Configuring done (4.5s) / Generating done (2.0s)
[cmake exit=0]
```

and `make help` lists all three binaries AnkusDrive needs — `elmersolver`, `ElmerGrid`,
`ViewFactors`. The compile itself was not run.

Two notes on the flags above:

- **`WITH_OpenMP` is off deliberately.** `-DWITH_OpenMP=TRUE` *fails* here: Apple's clang
  needs libomp wired up explicitly, and configure dies with
  `Could NOT find OpenMP_C (missing: OpenMP_C_FLAGS OpenMP_C_LIB_NAMES)` at
  `CMakeLists.txt:205`. Upstream CI works around it with explicit `LDFLAGS` and
  Clang-specific OpenMP flags. For a thermal solver driven one case at a time, serial is
  fine — turn it on only if you want it and are willing to pass
  `-DOpenMP_C_FLAGS`/`-DOpenMP_C_LIB_NAMES=omp`/`-DOpenMP_omp_LIBRARY` by hand.
- **CMake 4.2.3 configured `release-26.2` without complaint.** The release tarball
  declares `CMAKE_MINIMUM_REQUIRED(VERSION 3.10)` (the `devel` branch says 3.12); both
  clear CMake 4's floor of 3.5, so `-DCMAKE_POLICY_VERSION_MINIMUM` was **not** needed.
  Keep it in your back pocket for a bundled subproject, not the top level.

Use the tarball, not `git clone`. A `--depth 1` clone of `ElmerCSC/elmerfem` failed here
after ~15 MB with `fetch-pack: unexpected disconnect while reading sideband packet` /
`fatal: early EOF`, and git removed the partial tree on the way out. The repo carries a
lot of history and binary test data; the tagged tarball is a fraction of the size.

⚠️ **The tarball cannot be resumed.** GitHub generates archive tarballs on the fly, so
codeload answers a `Range:` request with a plain `200` and no `Accept-Ranges` —
`curl -C -` bails with `curl: (56) HTTP server doesn't seem to support byte ranges`, and
if you have piped its output it can still exit 0 while leaving a truncated file. Always
`tar tzf` the archive before trusting it. On a flaky link, use the speed guards so a
stall aborts instead of hanging forever, and re-download from scratch:

```bash
curl -fL --retry 5 --retry-all-errors --speed-limit 2048 --speed-time 30 \
  -o elmer-26.2.tar.gz \
  https://codeload.github.com/ElmerCSC/elmerfem/tar.gz/refs/tags/release-26.2
tar tzf elmer-26.2.tar.gz >/dev/null && echo "archive intact"
```

`CMAKE_INSTALL_PREFIX=/opt/homebrew` puts the binaries in
`/opt/homebrew/bin`, which is already in the `Darwin` search list of the `elmer` spec
(`ankusdrive/solvers.py`) — so it is auto-discovered with **no env var**, which matters
because MCP hosts launch the server with a minimal environment. Install anywhere else and
you must set `elmer_path` in `~/.config/ankusdrive/config.toml`.

Note `-DWITH_MPI=FALSE` is not optional boilerplate: **`WITH_MPI` defaults to `TRUE`** in
Elmer's top-level `CMakeLists.txt`. Leave it out and you need `brew install open-mpi` and
you get `ElmerSolver_mpi` instead.

## The four macOS-specific traps

**1. Homebrew *and* MacPorts both installed → hard configure error.** Elmer's
`USE_MACOS_PACKAGE_MANAGER` (default `ON`) auto-detects the package manager, and refuses
to guess:

```cmake
if(DEFINED MACPORTS_PREFIX AND DEFINED HOMEBREW_PREFIX)
    message(SEND_ERROR "Multiple package management systems detected - ")
    message(SEND_ERROR "define either MACPORTS_PREFIX or HOMEBREW_PREFIX")
```

**This machine has both** (`/opt/local/bin/port` and `/opt/homebrew/bin/brew`), and an
unqualified `cmake ..` was confirmed to fail here exactly as predicted:

```
-- Detected MacPorts install at /opt/local
-- Detected Homebrew install at /opt/homebrew
CMake Error at CMakeLists.txt:60 (message):
  Multiple package management systems detected -
CMake Error at CMakeLists.txt:61 (message):
  define either MACPORTS_PREFIX or HOMEBREW_PREFIX
```

Adding `-DHOMEBREW_PREFIX=/opt/homebrew` fixes it — verified, configure then exits 0.

The mechanism is worth understanding, because CMake will tell you the flag was pointless:

```
CMake Warning:
  Manually-specified variables were not used by the project:
    HOMEBREW_PREFIX
```

**That warning is expected and the flag is still doing its job.** What matters is that the
variable is *defined*, not that anything reads it: the detection block is guarded by
`if(NOT DEFINED MACPORTS_PREFIX AND NOT DEFINED HOMEBREW_PREFIX)`, so a `-D` on the
command line short-circuits the whole block, `MACPORTS_PREFIX` is never set, and the
conflict test cannot fire. Don't "clean up" the warning by dropping the flag.

**2. CMake 4.x versus old bundled subprojects.** `release-26.2` declares
`CMAKE_MINIMUM_REQUIRED(VERSION 3.10)` at the top level (the `devel` branch says 3.12);
both clear CMake 4's floor of 3.5, and **CMake 4.2.3 configured it here with no policy
complaint** — so this trap did *not* bite. It remains possible for a bundled contrib
subproject to declare an older minimum. If configure dies with a
`cmake_minimum_required` error from a subdirectory, add:

```bash
-DCMAKE_POLICY_VERSION_MINIMUM=3.5
```

(This is the same failure mode that made the `nlopt` build unusable when chasing the
`topopt` package — see the topopt note earlier in this document.)

**3. Homebrew's `openblas` is keg-only.** It is deliberately not symlinked into
`/opt/homebrew`, so CMake will not find it on its own:

```bash
export CMAKE_PREFIX_PATH="/opt/homebrew/opt/openblas"
```

Set that before configuring, or point `-DBLAS_LIBRARIES`/`-DLAPACK_LIBRARIES` at
`/opt/homebrew/opt/openblas/lib` explicitly. (Upstream CI sidesteps this by selecting the
BLAS vendor conditionally — on arm64 you can also just use Apple's Accelerate framework,
which CI tests too and which needs no Homebrew BLAS at all.) Note the formula installs as
`suite-sparse`; `brew install suitesparse` is an accepted alias.

**4. Fortran needs real GCC, not Apple's toolchain.** Elmer requires GCC ≥ 7 for Fortran
and there is no system `gfortran`; `brew install gcc` supplies it (verified here: GNU
Fortran, Homebrew GCC 15.2.0). C/C++ still use `clang` — that split is exactly what CI
does, so don't "helpfully" set `CMAKE_C_COMPILER=gcc`.

## Wiring and verification

```bash
ElmerSolver -v
ls $(dirname $(which ElmerSolver))/{ElmerGrid,ViewFactors}
```

If it landed outside the auto-discovered directories:

```toml
[solvers]
elmer_path = "/opt/elmer/bin/ElmerSolver"
```

Then confirm the family flips:

```bash
/Applications/FreeCAD.app/Contents/Resources/bin/python -c "
import sys; sys.path.insert(0,'/Users/you/AnkusDrive')
from ankusdrive import solvers
print(solvers.capabilities()['families']['thermal_transient'])"
```

## Status of this recipe

**Configure is verified end-to-end on this machine** against `release-26.2`
(macOS 26.5, arm64, CMake 4.2.3, gfortran 15.2.0): it exits 0, reports
`Darwin-arm64`, and `make help` offers `elmersolver`, `ElmerGrid` and `ViewFactors` —
the exact three binaries the `thermal_transient` family needs. Both macOS-specific
failures documented above (the MacPorts/Homebrew conflict, and OpenMP under clang) were
reproduced and fixed rather than predicted.

**The compile and install steps have not been run**, so `ElmerSolver -v` is still
unproven, as is the runtime behaviour of the family. Elmer is a large Fortran project —
budget accordingly. The remaining risk is in compilation, not configuration.

Also worth updating when this is done: the `elmer` entry's `install_hint` in
`ankusdrive/solvers.py` currently says only "a source build from https://www.elmerfem.org/
(macOS ships no prebuilt binaries)", which is accurate but gives the user nothing to act
on — no flags, no mention of the ElmerGrid/ViewFactors siblings the family actually needs.
