# macOS support (incl. Apple Silicon)

DriftPin's core — CAD modeling and structural FEM — runs natively on macOS against a
stock FreeCAD.app install, no extra setup. This page covers how FreeCAD is located, what
`driftpin doctor` tells you, and the honest per-solver install reality on macOS.

Tracking epic: [#189](https://github.com/gchen19/DriftPin/issues/189).

## TL;DR — verified working on macOS 26.5, Apple Silicon (arm64) + FreeCAD 1.1.1

```bash
# from a clone
python3 -m venv .venv
.venv/bin/pip install -e .

.venv/bin/driftpin ping      # ping=pong freecad=1.1.1
.venv/bin/driftpin doctor    # FreeCAD + solver checklist
```

- **Core CAD** — the `freecadcmd` worker boots from the app bundle and builds geometry. ✅
- **Structural FEM (CalculiX)** — FreeCAD's Mac bundle ships `ccx` and `gmsh` in
  `Contents/Resources/bin`, so `femtools.ccxtools` and the `warpage` family resolve them
  with no separate install. ✅
- **The whole test suite runs as-is** — `bash tests/run_all.sh` exits 0 with the same
  pass set as Linux (1449 asserts; the skips are solver-absent gates). No macOS-specific
  harness needed: the FreeCAD-loading tests drive `freecadcmd` as a subprocess, so the
  driver interpreter never imports FreeCAD. ✅
- **pip-wheel solvers** — `mbd` (mujoco on macOS — see below), `topology`, `optics`,
  `fluids` all install into the `.venv` on arm64 (verified on Python 3.14). ✅
- **Slicing (PrusaSlicer)** — `brew install --cask prusaslicer`; the app bundle is
  auto-discovered, live slice verified. ✅
- **CFD (SU2)** — `scripts/install-solvers.sh su2` downloads the official binary into the
  provisioner dir, auto-discovered with no env var. The official build is x86_64 and runs
  under Rosetta 2 on Apple Silicon. ✅
- **OpenFOAM-backed families and the GPL source builds** stay Linux-only — see below.

## How DriftPin finds FreeCAD

Resolution order (in [`driftpin/client.py`](../driftpin/client.py) `_resolve_freecadcmd`):

1. **`$DRIFTPIN_FREECADCMD`** — explicit override, returned verbatim.
2. **PATH** — `shutil.which` tries `freecadcmd`, `FreeCADCmd`, `freecad.cmd`. The macOS
   app bundle does not put anything on PATH, so this usually misses — which is why
   step 3 exists.
3. **The default bundle path**: `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd`.

So a stock drag-to-Applications install needs no configuration. For a non-standard
location, set `DRIFTPIN_FREECADCMD` — and to make it stick across shells and MCP-host
launches (MCP hosts spawn the server with a minimal env), put it in the config file
instead: `~/.config/driftpin/config.toml` (the [#199](https://github.com/gchen19/DriftPin/issues/199)
layer; every `DRIFTPIN_*` var can live there).

Note: `import FreeCAD` does **not** work in any stock interpreter on macOS — not even
the bundle's own `Contents/Resources/bin/python` without a `sys.path` shim. That's fine:
nothing in DriftPin (or its tests) imports FreeCAD in-process; everything goes through
the `freecadcmd` worker subprocess.

## `driftpin doctor`

One cross-platform health report: resolves FreeCAD **and** every solver family and
prints, per item, found/missing, the resolved path (or why it didn't resolve), and the
exact fix.

```bash
driftpin doctor            # human-readable checklist
driftpin doctor --json     # machine-readable (CI preflight; exits non-zero if FreeCAD unresolved)
driftpin doctor --no-boot  # resolve FreeCAD's path only, skip the version probe (faster)
```

## Running the test suite on macOS

`bash tests/run_all.sh` works unchanged. The two-interpreter split (system `python3` +
`.venv` for numpy/Pillow tests) carries over directly; neither interpreter ever imports
FreeCAD. There is no GNU-coreutils dependency (`timeout` is not used).

## Installing the extra solvers (one script)

[`scripts/install-solvers.sh`](../scripts/install-solvers.sh) handles macOS alongside
Linux (Windows has [`install-solvers.ps1`](../scripts/install-solvers.ps1)):

```bash
scripts/install-solvers.sh                    # pip-wheel extras (mbd, topology, optics, fluids)
scripts/install-solvers.sh su2 prusaslicer    # native binaries, auto-discovered — no env vars
scripts/install-solvers.sh --list             # what resolves right now (≈ driftpin doctor)
```

- **pip-wheel families** install into the `.venv` that runs `driftpin mcp`, best-effort
  and independently — one missing wheel doesn't abort the rest. On macOS the `mbd` extra
  resolves to **mujoco**: PyBullet ships no macOS wheels and its sdist fails to compile
  under clang (verified on arm64 / Python 3.14); the registry accepts either engine.
- **SU2** downloads the official `macos64` release into
  `~/Library/Application Support/DriftPin/solvers` — the Darwin analog of the Windows
  `%LOCALAPPDATA%\DriftPin\solvers` layout, which `driftpin/solvers.py` globs so the
  binary resolves with **no env var**. The official build is x86_64-only; on Apple
  Silicon it runs under **Rosetta 2** (the script checks and refuses to install without
  it: `softwareupdate --install-rosetta --agree-to-license`). A native arm64 SU2 is a
  meson source build from https://su2code.github.io/ if you want to avoid Rosetta.
- **PrusaSlicer** installs via the Homebrew cask; the discovery already probes
  `/Applications/PrusaSlicer.app/Contents/MacOS`.
- **CalculiX** is never installed here — it rides on FreeCAD's bundled `ccx`.

## Per-solver macOS reality

| Family | Solver | macOS status |
|---|---|---|
| Core FEM / warpage | CalculiX `ccx` | ✅ **bundled** in FreeCAD.app's `Contents/Resources/bin` — nothing to install |
| MBD | MuJoCo | ✅ `pip install 'driftpin[mbd]'` (arm64 wheels; PyBullet has none and won't compile — the extra selects mujoco on Darwin) |
| Topology | topopt / solidspy | ✅ `pip install 'driftpin[topology]'` |
| Optics (sequential) | optiland / rayoptics | ✅ `pip install 'driftpin[optics]'` |
| Optics (non-seq) | KrakenOS (GPL) | ✅ `pip install 'driftpin[optics_gpl]'`, run out-of-process |
| Fluids properties | CoolProp | ✅ pip wheel (arm64) |
| Slicing | PrusaSlicer | ✅ `brew install --cask prusaslicer` (or `install-solvers.sh prusaslicer`) — auto-discovered, live slice verified |
| CFD (steady) | SU2 | ✅ `install-solvers.sh su2` — official x86_64 binary under **Rosetta 2**, auto-discovered from the provisioner dir |
| Transient/radiation thermal | Elmer | ⚠️ **no prebuilt macOS binaries exist** — no Homebrew formula, no conda-forge package (older hints claiming one were wrong). Source build (CMake + gfortran) from https://www.elmerfem.org/, then `DRIFTPIN_ELMER_PATH` |
| Acoustics (BEM) | bempp-cl | ⚠️ needs an OpenCL ICD (`pocl` on Apple Silicon); dedicated venv (`DRIFTPIN_BEMPP_PYTHON`) |
| Full-wave EM | openEMS | ⚠️ source build with brew deps; the `em_gpl` recipe is Linux-only today |
| DEM (granular) | YADE (GPL) | ⚠️ no macOS build path in this repo — Linux box or container |
| CFD/FSI/molding | OpenFOAM + preCICE | ✅ **Multipass** (arm64-native Ubuntu VM) — DriftPin runs the apps via `multipass exec`; live FSI coupled solve verified ([#193](https://github.com/gchen19/DriftPin/issues/193), see below) |

Every absent solver **degrades cleanly** — the family returns `{ok: false, reason, install}`
rather than crashing — so an incomplete solver set never breaks the server; those tools
just report "not available" with the fix. `driftpin doctor` shows the current state.

## OpenFOAM-backed families via Multipass (CFD / FSI / molding)

OpenFOAM has no native macOS build; [openfoam.org](https://openfoam.org/version/macos/)
ships it inside a **Canonical Multipass** VM (arm64-native on Apple Silicon — no
emulation). DriftPin routes the OpenFOAM apps through that VM: on macOS
`solvers.bash_argv` launches `multipass exec <instance> -- bash -c "cd <case> && …"`
(issue [#193](https://github.com/gchen19/DriftPin/issues/193)), so the same case the
host builds is meshed/solved inside the VM. **CFD alone needs none of this** — it
degrades to SU2 (native, under Rosetta). Multipass is only for the OpenFOAM-*exclusive*
families: injection-molding fill and the preCICE **FSI** coupling.

**1. Install Multipass and launch the VM** (the instance name must be `openfoam`, or set
`DRIFTPIN_OPENFOAM_INSTANCE`):

```bash
brew install --cask multipass
multipass launch -c 8 -m 8G -d 80G -n openfoam 24.04
```

**2. Provision the FSI stack inside the VM.** `scripts/install-solvers.sh fsi` *prints*
the exact validated recipe; run those steps inside `multipass shell openfoam`. It adds the
ESI OpenFOAM apt repo (`dl.openfoam.com` — publishes **arm64** debs), installs
`openfoam2512-dev`, then source-builds serial libprecice, the CalculiX-preCICE adapter
(`ccx_preCICE`), and the OpenFOAM-preCICE adapter. Two gotchas on a minimal cloud image:
the recipe's apt list includes **`bzip2`** (absent by default; `tar xjf` needs it), and do
**not** run the build under `set -u` — OpenFOAM's `etc/bashrc` references unbound vars.

**3. Share the case dir at a matching host↔VM path**, so `cd <case>` inside the VM resolves
(the host scratch must be mounted at the *same* absolute path):

```bash
mkdir -p ~/fsi-run
multipass mount ~/fsi-run openfoam:$HOME/fsi-run      # same path both sides
```

**4. Point DriftPin at the in-VM stack** (absolute in-VM paths; on macOS these are trusted
as VM paths when `multipass` is present — the VM filesystem is opaque from the host):

```bash
export TMPDIR=$HOME/fsi-run                            # so test case dirs land in the mount
export DRIFTPIN_CCX_PRECICE=/home/ubuntu/calculix-adapter/bin/ccx_preCICE
export DRIFTPIN_PRECICE_LIB=/home/ubuntu/precice-serial/lib
export DRIFTPIN_OPENFOAM_ADAPTER_LIB=/home/ubuntu/OpenFOAM/ubuntu-v2512/platforms/linuxARM64GccDPInt32Opt/lib
export DRIFTPIN_FSI_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc
```

**Verify** — the coupled preCICE OpenFOAM↔CalculiX solve, live on Apple Silicon:

```bash
RUN_HEAVY_SOLVES=1 python3 tests/test_fsi.py
# PASS test_fsi_coupled_plate_deflects — 4 windows converged, tip 0 → 3.47 mm (monotone into the flow)
```

### The renderer scripts

`scripts/install-renderers.sh` and `scripts/build-renderers.sh` remain Linux-x86_64
only: the pinned LuxCore/appleseed/OSPRay tarballs are linux64 **ELF**, which can never
run on macOS (Rosetta translates x86_64 *macOS* binaries, not Linux ones). POV-Ray is
the one turnkey renderer here (`brew install povray`). No automated path for the rest
on any OS yet.

## Process cleanup

On worker shutdown DriftPin sweeps any renderer subprocesses `freecadcmd` spawned via a
POSIX process-group `SIGKILL` — macOS takes the same path as Linux
(`client.py`, guarded on `os.name == "posix"`), so nothing leaks.
