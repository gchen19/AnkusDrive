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
| CFD (pipe / plate / bridge / wind tunnel) | OpenFOAM | ✅ **Multipass**, same routing as FSI — needs the two in-VM exports below. All built-in CFD case builders are OpenFOAM-only; live-verified on Apple Silicon ([#223](https://github.com/gchen19/DriftPin/issues/223)) |
| CFD (native, plane channel) | SU2 | ✅ `install-solvers.sh su2` installs it (official x86_64 binary under **Rosetta 2**, auto-discovered from the provisioner dir). Since [#237](https://github.com/gchen19/DriftPin/issues/237) item 3 DriftPin **builds** the plane-Poiseuille case for it (`channel_height_mm`), gated exactly, so SU2 alone makes the cfd family available. Geometry-bearing CFD still needs OpenFOAM in the VM |
| Transient/radiation thermal | Elmer | ⚠️ **no prebuilt macOS binaries exist** — no Homebrew formula, no conda-forge package (older hints claiming one were wrong). Source build (CMake + gfortran) from https://www.elmerfem.org/, then `DRIFTPIN_ELMER_PATH` |
| Acoustics (BEM) | bempp-cl | ⚠️ needs an OpenCL ICD (`pocl` on Apple Silicon); dedicated venv (`DRIFTPIN_BEMPP_PYTHON`) |
| Full-wave EM | openEMS | ⚠️ source build with brew deps; the `em_gpl` recipe is Linux-only today |
| DEM (granular) | YADE (GPL) | ⚠️ no macOS build path in this repo — Linux box or container |
| FSI (preCICE) | OpenFOAM + CalculiX | ✅ **Multipass** (arm64-native Ubuntu VM) — DriftPin runs the apps via `multipass exec`; live coupled solve verified ([#193](https://github.com/gchen19/DriftPin/issues/193), see below). Needs the in-VM `DRIFTPIN_*` exports |
| Injection-molding fill | openInjMoldSim / interFoam | ✅ **Multipass**, same routing — needs the in-VM exports below. Code path verified by unit test; **no live macOS solve yet** (Windows/WSL and Linux are live-verified) |

Every absent solver **degrades cleanly** — the family returns `{ok: false, reason, install}`
rather than crashing — so an incomplete solver set never breaks the server; those tools
just report "not available" with the fix. `driftpin doctor` shows the current state.

## OpenFOAM-backed families via Multipass (CFD / FSI / molding)

OpenFOAM has no native macOS build; [openfoam.org](https://openfoam.org/version/macos/)
ships it inside a **Canonical Multipass** VM (arm64-native on Apple Silicon — no
emulation). DriftPin routes the OpenFOAM apps through that VM: on macOS
`solvers.bash_argv` launches `multipass exec <instance> -- bash -c "cd <case> && …"`
(issue [#193](https://github.com/gchen19/DriftPin/issues/193)), so the same case the
host builds is meshed/solved inside the VM. That includes **plain CFD**: every built-in
case mode of `cfd_internal_flow_submit` / `cfd_external_flow_submit` — pipe, RANS pipe,
snappyHexMesh geometry bridge, flat plate, wind tunnel — emits an OpenFOAM dictionary
tree, so CFD needs this VM exactly as much as molding and FSI do.

> **CFD does degrade to SU2 — for one case.** Since
> [#237](https://github.com/gchen19/DriftPin/issues/237) item 3, DriftPin builds a
> native SU2 case: `cfd_internal_flow_submit(channel_height_mm=...)` writes its own
> `.su2` mesh, config and inlet profile and solves plane Poiseuille against the exact
> closed form Δp = 12·μ·U·L/h² (live ratio **1.000000**, mesh-independent over 400 and
> 3600 cells). No OpenFOAM, no VM, no FreeCAD in the loop. So `solve_capabilities` now
> counts SU2 toward the cfd family's `any_available`, and SU2 alone is enough to ask a
> CFD question and get a *gated* answer.
>
> What that does **not** mean: the OpenFOAM-only modes are still OpenFOAM-only — the
> straight pipe, the snappyHexMesh geometry bridge, the flat plate and the wind tunnel
> all emit OpenFOAM dictionaries. A family reading available means SOME built-in case
> can run, not every one. For anything with real geometry in it, you still want the VM
> below.

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

**4. Point DriftPin at the in-VM stack.** These are absolute *in-VM* paths. Discovery
cannot stat or glob them — a Multipass VM's filesystem is opaque from macOS, unlike WSL's
`\\wsl$` mirror — so on macOS an absolute `DRIFTPIN_*` override is **trusted as a VM path**
whenever `multipass` is on PATH, and that trust is the only way these families resolve
here. Get one wrong and the solve fails inside the VM rather than degrading; with none set
`driftpin doctor` reports OpenFOAM `unwired` and its fix line points back here:

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

This solve is now gated in CI on a self-hosted Apple-Silicon runner — see
[CI: the Apple-Silicon lane](#ci-the-apple-silicon-lane) below.

### Plain CFD (pipe, flat plate, mesh bridge, wind tunnel) in the VM

The built-in CFD case builders — `cfd_internal_flow_submit`, `cfd_external_flow_submit`
(including the geometry wind tunnel) — are OpenFOAM-only, and OpenFOAM lives in the VM.
Two exports on top of the step-3 mount wire them up; they need none of the preCICE stack:

```bash
export TMPDIR=$HOME/fsi-run                            # case dirs land in the mount
export DRIFTPIN_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc
export DRIFTPIN_OPENFOAM_PATH=/usr/lib/openfoam/openfoam2512/platforms/linuxARM64GccDPInt32Opt/bin/simpleFoam
```

Until those are set, `driftpin doctor` / `solve_capabilities` report OpenFOAM `unwired`
with the fix for **the state your VM is actually in** — discovery reads (never starts)
`multipass info`, time-boxed, and picks one of three hints ([#237](https://github.com/gchen19/DriftPin/issues/237)):

| `found_at` | VM state | `wire_hint` says |
|---|---|---|
| `multipass` | no multipass CLI, no such instance, or the read failed/timed out | provision it — `multipass shell openfoam` + `install-solvers.sh cfd`, then the exports |
| `multipass VM 'openfoam' (stopped)` | instance exists, not running | start it — `multipass start openfoam` |
| `multipass VM 'openfoam' (running)` | VM up, this shell unwired | the three exports above — **not** "provision" |

**Verified live on Apple Silicon** (issue #223), whole suites, no skips:

```bash
RUN_HEAVY_SOLVES=1 python3 tests/test_openfoam.py     # 18/18 — Hagen-Poiseuille, D^4, Blasius, Colebrook
RUN_HEAVY_SOLVES=1 python3 tests/test_meshbridge.py   # 19/19 — snappy bridge + the wind-tunnel drag-curve gates
RUN_HEAVY_SOLVES=1 python3 tests/test_wind_tunnel.py  #  3/3  — a FreeCAD solid through the real handler
```

Two macOS-specific defects had to be fixed before those could pass, both of which had
hidden on Linux:

* `snappyHexMesh` v2512 **rejects a `locationInMesh` that lands exactly on a cell
  vertex** ("Point (…) is not inside the mesh or on a face or edge") — which the internal
  bridge's default seed (the solid's bbox centre) does for any symmetric solid. The seed
  is now nudged off the grid by a fraction of a cell.
* `multipass exec` **forwards stdin into the VM**, so a solve launched from a worker job
  thread inherited — and consumed — the client's JSON-RPC pipe. The solve finished fine
  while every `job_status` poll timed out. All substrate launches now pin
  `stdin=DEVNULL`. `bash -c` on Linux never reads stdin, which is why it hid there.

### Injection-molding fill (and any other OpenFOAM app) in the VM

`molding_fill_submit` runs `interFoam` on the ESI OpenFOAM installed in step 2, and prefers
**openInjMoldSim** (GPL-3.0, OpenFOAM-7 .org) when that build resolves. Both run inside the
same VM through the same launcher; they need their own exports because neither the binary
nor the bashrc is visible from the host:

```bash
# interFoam path — also what a generic `cfd_*_flow_submit` on OpenFOAM would use
export DRIFTPIN_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc
export DRIFTPIN_OPENFOAM_PATH=/usr/lib/openfoam/openfoam2512/platforms/linuxARM64GccDPInt32Opt/bin/interFoam

# openInjMoldSim path — build OpenFOAM-7 (.org) + the solver in the VM first:
#   multipass shell openfoam   →   bash tools/build_openinjmoldsim.sh --build
export DRIFTPIN_OPENINJMOLDSIM=/home/ubuntu/OpenFOAM/ubuntu-7/platforms/linuxARM64GccDPInt32Opt/bin/openInjMoldSim
export DRIFTPIN_OPENINJMOLDSIM_BASHRC=/home/ubuntu/OpenFOAM/OpenFOAM-7/etc/bashrc
```

The case dir must sit under the shared mount (`TMPDIR` above), same absolute path on both
sides — `multipass exec` starts in the VM home, so the launcher `cd`s into the case by its
host path. `driftpin doctor` then reports the family as **ready … (in Multipass VM)**,
which is how you tell an in-VM resolution from a native one.

Status: this routing is exercised by `tests/test_wsl_routing.py` (macOS branches, no VM
needed), and the identical path is live-verified on Windows/WSL; the heavy macOS molding
solve (`RUN_HEAVY_SOLVES=1 python3 tests/test_molding_fill.py`) has not been run on Apple
Silicon yet. The OF7-org source build in particular is unproven on arm64 (it is a 2019
tree and needed a gcc-11 pin even on x86_64 — see docs/WINDOWS.md); if it won't build,
skip the second pair of exports and the family runs the `interFoam` fallback.

### The renderer scripts

`scripts/install-renderers.sh` and `scripts/build-renderers.sh` remain Linux-x86_64
only: the pinned LuxCore/appleseed/OSPRay tarballs are linux64 **ELF**, which can never
run on macOS (Rosetta translates x86_64 *macOS* binaries, not Linux ones). POV-Ray is
the one turnkey renderer here (`brew install povray`). No automated path for the rest
on any OS yet.

## CI: the Apple-Silicon lane

Everything above was verified by hand. [`heavy-solves.yml`](../.github/workflows/heavy-solves.yml)
now carries a second job — `heavy-solves-macos`, label `[self-hosted, driftpin, macOS]` —
so a regression in the Darwin substrate is caught automatically instead of on the next
manual run (issue [#220](https://github.com/gchen19/DriftPin/issues/220)). It shares the
Linux lane's triggers (solver-path push to `main`, `workflow_dispatch`, the 06:00 UTC
cron) and its `RUN_HEAVY_SOLVES: "1"`, but the two jobs are independent — a stopped
Multipass VM never blocks the Linux regression report, and vice versa.

**What it runs** — deliberately not the whole suite. Most heavy solvers aren't reachable
on macOS at all (see the table above), so a full run would spend 90 minutes reconfirming
skips. `tests/run_macos_heavy.sh` runs only the macOS-*specific* paths, the ones the
Linux lane cannot see:

| File | What would otherwise go untested |
|---|---|
| `tests/test_wsl_routing.py` | the Darwin routing contracts — `bash_argv` → `multipass exec`, `runs_in_substrate`, `_fsi_override`'s in-VM path trust, FSI participant routing (pure Python, seconds) |
| `tests/test_su2_native.py` | the **live** SU2 channel solve — official x86_64 binary under Rosetta 2, through `solvers.run_argvs` |
| `tests/test_fsi.py` | the **live** preCICE OpenFOAM↔CalculiX coupled solve, executed inside the VM |

The whole lane is ~10 s of solve on the reference box (FSI 8.9 s for four preCICE
windows, SU2 0.2 s) — verified end-to-end, 29/29 asserts, tip 0 → 3.4696 mm. Add files as
arguments once their in-VM provisioning is validated — the molding gate is next, and is
*not* in the default set because the macOS `openInjMoldSim` / `interFoam` solve has never
been run live (see the section above):

```bash
bash tests/run_macos_heavy.sh tests/test_molding_fill.py
```

### One-time runner setup

1. **Provision the box** exactly as the sections above describe: SU2 (`install-solvers.sh su2`)
   + Rosetta, Multipass with a persistent `openfoam` instance carrying the FSI stack
   (`install-solvers.sh fsi`), and the host scratch dir mounted at a matching path.
2. **Register the runner** with the `driftpin` label (the `macOS` and `self-hosted` labels
   are applied automatically from the host OS):

   ```bash
   # token from  Settings -> Actions -> Runners -> New self-hosted runner
   ./config.sh --url https://github.com/gchen19/DriftPin --token <TOKEN> --labels driftpin
   ./svc.sh install && ./svc.sh start      # run as a service so the cron lane fires unattended
   ```

3. **Put the box-specific paths in the runner's own environment**, `~/actions-runner/.env`
   — *not* in the workflow, which must stay portable. The Actions runner applies this file
   to every job it runs:

   ```
   TMPDIR=/Users/<you>/fsi-run
   DRIFTPIN_CCX_PRECICE=/home/ubuntu/calculix-adapter/bin/ccx_preCICE
   DRIFTPIN_PRECICE_LIB=/home/ubuntu/precice-serial/lib
   DRIFTPIN_OPENFOAM_ADAPTER_LIB=/home/ubuntu/OpenFOAM/ubuntu-v2512/platforms/linuxARM64GccDPInt32Opt/lib
   DRIFTPIN_FSI_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc
   DRIFTPIN_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc
   DRIFTPIN_OPENFOAM_PATH=/usr/lib/openfoam/openfoam2512/platforms/linuxARM64GccDPInt32Opt/bin/simpleFoam
   ```

   `TMPDIR` is the host side of the `multipass mount`, so the `mkdtemp` case dirs land
   somewhere the VM can `cd` into at the same absolute path. The other six are in-VM
   paths. The last two are the plain-CFD pair from
   [Plain CFD … in the VM](#plain-cfd-pipe-flat-plate-mesh-bridge-wind-tunnel-in-the-vm):
   the built-in CFD builders resolve through `DRIFTPIN_OPENFOAM_*`, *not* the FSI pair
   above, so omitting them leaves `test_openfoam` / `test_meshbridge` / `test_wind_tunnel`
   skipping their live halves — which is exactly what the preflight refuses to let pass.
   Restart the service after editing (`./svc.sh stop && ./svc.sh start`).
4. **Keep the Mac awake** — `sudo pmset -a sleep 0 disablesleep 1`, or the 06:00 UTC cron
   lane finds the VM suspended.

### The preflight step

The job's first step is [`scripts/ci-macos-preflight.sh`](../scripts/ci-macos-preflight.sh),
which health-checks the whole substrate *before* any solve and reports every problem it
finds at once:

```bash
bash scripts/ci-macos-preflight.sh          # full check — also useful locally
bash scripts/ci-macos-preflight.sh --fsi    # VM substrate only
bash scripts/ci-macos-preflight.sh --su2    # Rosetta + SU2 only
```

It exists because every failure mode here is stateful runner setup that surfaces as an
unrelated-looking error minutes later:

- **VM stopped or wedged.** Checks the instance state, `multipass start`s it if it isn't
  Running, then probes `multipass exec` — a VM can report Running with a wedged agent
  after a host sleep, which hangs a solve rather than failing it. The fix it prints is
  `multipass restart`.
- **The mount dropped.** `multipass-sshfs` is unreliable under load, and a missing mount
  means `cd <case>` inside the VM silently fails. The check is a real round trip — write
  a sentinel under `TMPDIR` on the host, `test -f` the same absolute path in the VM — and
  it remounts once and retries before failing, since one remount beats a red run.
- **A stale in-VM override.** On Darwin `solvers._fsi_override` *trusts* an absolute
  override when `multipass` is present (the VM filesystem is opaque from the host), so a
  typo resolves fine and only explodes mid-solve. The preflight `test -x`/`test -d`s all
  four inside the VM, where they can actually be checked.
- **The silent no-op.** If the FSI stack doesn't resolve, `test_fsi.py`'s live case
  *skips* and the job goes green having tested nothing. So the preflight asserts
  `solvers.fsi_stack_status()["ok"]` — the exact predicate the test gates on — and fails
  when the live solve wouldn't run. Same for SU2 resolving.

## Process cleanup

On worker shutdown DriftPin sweeps any renderer subprocesses `freecadcmd` spawned via a
POSIX process-group `SIGKILL` — macOS takes the same path as Linux
(`client.py`, guarded on `os.name == "posix"`), so nothing leaks.
