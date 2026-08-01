# Windows support

DriftPin's core — CAD modeling and structural FEM — runs natively on Windows against
a stock FreeCAD 1.1 install, no extra setup. This page covers how FreeCAD is located,
what `driftpin doctor` tells you, and the honest per-solver install reality on Windows.

Tracking epic: [#189](https://github.com/gchen19/DriftPin/issues/189).

## TL;DR — verified working on Windows 11 + FreeCAD 1.1.1

```powershell
# from a clone (PowerShell)
py -m venv .venv
.venv\Scripts\pip install -e .

.venv\Scripts\driftpin ping      # ping=pong freecad=1.1.1
.venv\Scripts\driftpin doctor    # FreeCAD + solver checklist
.venv\Scripts\driftpin fem cantilever   # CalculiX solve via bundled ccx.exe
```

- **Core CAD** — the `freecadcmd.exe` worker boots and builds geometry. ✅
- **Structural FEM (CalculiX)** — FreeCAD's Windows install bundles `ccx.exe` and
  `gmsh.exe` in `bin\`, so `femtools.ccxtools` resolves them with no separate install.
  A cantilever solve returns the expected displacement/stress. ✅
- **Pure-Python families** (materials, tolerance/GD&T, gears, bearings, thermal-lumped,
  fits, standards) — platform-agnostic. ✅
- **pip-wheel solvers** (PyBullet, MuJoCo, topopt/solidspy, rayoptics, optiland,
  CoolProp) — Windows wheels exist; `pip install 'driftpin[...]'` works. ✅
- **Molding warpage** (CalculiX thermo-elastic) — the bundled `ccx.exe` is auto-discovered,
  so `molding_warpage_submit` solves natively (verified: `rc=0`, real bow). ✅
- **Slicing** (PrusaSlicer) and **CFD** (SU2) — self-contained native binaries;
  `scripts\install-solvers.ps1 prusaslicer su2` downloads + wires them. Live PrusaSlicer
  slice AND an end-to-end native SU2 solve are verified (#203: the SU2 branch of the CFD
  runner is bash-free). ✅
- **Transient/radiation thermal, CHT, low-frequency EM, acoustic/harmonic FEM** (Elmer) —
  portable no-GUI zip; `scripts\install-solvers.ps1 elmer` downloads + wires it. All five
  Elmer-backed suites verified live natively, including the ViewFactors radiation legs and
  the FreeCAD geometry bridge (#205). ✅

## How DriftPin finds FreeCAD

Resolution order (in [`driftpin/client.py`](../driftpin/client.py) `_resolve_freecadcmd`):

1. **`$DRIFTPIN_FREECADCMD`** — explicit override, returned verbatim.
2. **PATH** — `shutil.which` tries `freecadcmd`, `FreeCADCmd`, `freecad.cmd`
   (Windows `PATHEXT` appends `.exe`). FreeCAD's installer does *not* add `bin\` to
   PATH, so this usually misses — which is why step 3 exists.
3. **Per-OS default install locations** (newest version wins):

   ```
   C:\Program Files\FreeCAD *\bin\freecadcmd.exe        (version-globbed: 1.1, 1.0, …)
   C:\Program Files (x86)\FreeCAD *\bin\freecadcmd.exe
   %LOCALAPPDATA%\Programs\FreeCAD *\bin\freecadcmd.exe (per-user / winget install)
   ```

So a stock installer needs no configuration. For a non-standard location:

```powershell
$env:DRIFTPIN_FREECADCMD = "D:\Apps\FreeCAD\bin\freecadcmd.exe"
```

To make it stick across shells and MCP-host launches, set it as a user environment
variable (`setx DRIFTPIN_FREECADCMD "..."`) — MCP hosts spawn the server with a minimal
env, so a one-shot `$env:` export in your terminal won't carry over (see
[#199](https://github.com/gchen19/DriftPin/issues/199) for the planned config-file layer).

`driftpin doctor` reports which layer resolved FreeCAD and lists every candidate it
checked — the fastest way to debug "FreeCAD not found".

## `driftpin doctor`

One cross-platform health report: resolves FreeCAD **and** every solver family and prints,
per item, found/missing, the resolved path (or why it didn't resolve), and the exact fix.
The solver half needs no FreeCAD boot and no running MCP server.

```powershell
driftpin doctor            # human-readable checklist
driftpin doctor --json     # machine-readable (CI preflight; exits non-zero if FreeCAD unresolved)
driftpin doctor --no-boot  # resolve FreeCAD's path only, skip the version probe (faster)
```

## Running the test suite on Windows (CI tooling)

The Linux suite (`tests/setup_local.sh` + `tests/run_all.sh`) is bash-only: it extracts a
FreeCAD AppImage, symlinks `freecadcmd` onto PATH, and points a `.venv` at FreeCAD's
bundled Python for a two-interpreter split. **None of that applies on Windows** — FreeCAD
is a normal installer and the worker runs `freecadcmd.exe` as a subprocess, so the driver
interpreter never imports FreeCAD. Windows has its own PowerShell tooling:

```powershell
pwsh tests\setup_local.ps1   # one .venv from `pip install -e .` + Windows-viable extras
pwsh tests\run_all.ps1       # the Windows suite (single interpreter)
```

(Windows PowerShell 5.1 works too — `powershell -File tests\run_all.ps1`; the scripts are
5.1-compatible and ASCII-only so they parse under the cp1252 console default.)

- **One interpreter.** A single `.venv` from `pip install -e .` has `mcp` + `numpy` +
  `Pillow` and drives `freecadcmd.exe` — no symlink or bundled-python dance.
- **Extras install best-effort, independently.** `setup_local.ps1` installs `mbd`,
  `topology`, `optics`, `fluids` one at a time so one missing wheel (e.g. PyBullet on
  3.13) doesn't abort the rest — the corresponding test just skips.
- **`run_all.ps1` skips the Linux-only solver families** outright (OpenFOAM, YADE,
  openEMS, bempp, preCICE) and prints what it skipped and why. CalculiX (`ccx.exe`) and
  `gmsh.exe` ship with FreeCAD on Windows, so structural FEM runs for real. It runs every
  selected module and summarizes failures (vs. the Linux `set -e` stop-on-first).
- **The Windows-native contract** (`tests/test_windows_support.py`) pins the
  `freecadcmd.exe` discovery + `doctor` report and runs first.

### CI: the Windows lane

[`.github/workflows/test-windows.yml`](../.github/workflows/test-windows.yml) runs this
suite on a **self-hosted Windows runner** (label `[self-hosted, driftpin, Windows]`) with
FreeCAD 1.1 preinstalled. It is a separate workflow from the Linux `test.yml` /
`heavy-solves.yml`, which are now pinned to the `Linux` label — every self-hosted runner
shares the `driftpin` label, so the OS label keeps a job from landing on the wrong box and
running another OS's shell scripts. `heavy-solves.yml` also carries a `macOS`-pinned job
for the Apple-Silicon substrate (see [MACOS.md](MACOS.md#ci-the-apple-silicon-lane)). The
`Windows`/`Linux`/`macOS` labels are applied automatically by the Actions runner from the
host OS. The FreeCAD-free `fast-checks.yml` (ruff + contracts) stays on a GitHub-hosted
Ubuntu runner and covers all three.

## Installing the extra solvers (one script)

[`scripts/install-solvers.ps1`](../scripts/install-solvers.ps1) is the Windows analog of
`scripts/install-solvers.sh` — it automates the two install shapes that need no compiler
or WSL, and prints guidance for the rest.

```powershell
pwsh scripts\install-solvers.ps1                    # pip-wheel extras (mbd, topology, optics, fluids)
pwsh scripts\install-solvers.ps1 su2 prusaslicer    # download portable binaries + wire env for this shell
pwsh scripts\install-solvers.ps1 su2 prusaslicer -Persist   # ...and setx so they survive new shells / MCP hosts
pwsh scripts\install-solvers.ps1 list               # == driftpin doctor
```

- **pip-wheel families** (`mbd`, `topology`, `optics`, `fluids`) install into the `.venv`
  that runs `driftpin mcp` — a plain `pip install '.[extra]'`, best-effort/independent so
  one missing wheel doesn't abort the rest.
- **Portable binaries** (`su2`, `prusaslicer`) download as a zip into
  `%LOCALAPPDATA%\DriftPin\solvers`, extract, and wire `DRIFTPIN_<SOLVER>_PATH`. No
  installer, no admin.
- **CalculiX** is never installed here — it rides on FreeCAD's bundled `ccx.exe`, which
  `driftpin/solvers.py` auto-discovers (see the `warpage` row below).

### Verified on this box (Windows 11 + FreeCAD 1.1.1, Python 3.13.14)

Every result below is a real solve/run on the machine, not a dry check:

| Family | Solver | How | Verified result |
|---|---|---|---|
| **warpage** | CalculiX (bundled `ccx.exe` 2.22) | auto-discovered — no install | thin-plate thermo-elastic solve: `rc=0`, 4981 nodes / 2424 tets, warp 7.0 mm vs 3.5 mm analytic, gate computed |
| **FEM modal** | CalculiX (bundled) | auto-discovered | `test_merge_modal_gate` live modal solve: FEM f₁=304.6 Hz vs 308.5 Hz oracle (ratio 0.99), 17/17 |
| **mbd** | PyBullet 3.2.7 | `pip install '.[mbd]'` | wheel installs on 3.13, `test_mbd` runs |
| **slicing** | PrusaSlicer 2.9.6 | portable zip + `DRIFTPIN_PRUSASLICER_PATH` | live slice: 20 mm cube → 8.06 cm³ @100% infill (exact 8.0, ratio 1.008), 99 layers, 10/10 |
| **cfd** | SU2 8.5.0 | portable zip + `DRIFTPIN_SU2_PATH` | `SU2_CFD.exe` runs; end-to-end `cfd_*_flow_submit` solve verified native — no bash (#203) |
| **thermal_transient** | Elmer 26.2 | portable no-GUI zip + `DRIFTPIN_ELMER_PATH` | ElmerSolver/ElmerGrid/ViewFactors verified live: thermal, radiation, CHT, EM, acoustic + harmonic FEM, geometry bridge (#205) |
| **optics** | optiland + rayoptics | `pip install '.[optics]'` | ready |
| **topology** | solidspy | `pip install '.[topology]'` | ready |
| **fluids** | CoolProp 8.0.0 | `pip install '.[fluids]'` | ready (in-process f(T,P)) |

The bundled-`ccx` auto-discovery means the two live-CalculiX tests that used to skip on
Windows (`test_merge_modal_gate`, `test_render::test_fem_colormap_monotonic_gradient`) now
run the **real** solve — their gates consult `driftpin.solvers.ccx_bin()`, which finds
FreeCAD's bundled `ccx.exe` even though it isn't on PATH.

## Per-solver Windows reality

| Family | Solver | Windows status |
|---|---|---|
| Core FEM / warpage | CalculiX `ccx` | ✅ **bundled** in FreeCAD's `bin\ccx.exe` (v2.22 in FreeCAD 1.1) — **nothing to install**. `driftpin/solvers.py` auto-discovers it in FreeCAD's bin, so the `warpage` family and the live-ccx tests resolve it with no PATH entry or env var |
| MBD | PyBullet / MuJoCo | ✅ `pip install 'driftpin[mbd]'` — PyBullet now ships a **Python 3.13 wheel** (3.2.7, verified installing on 3.13.14). `pip install mujoco` is the registry's alternative if a future Python lacks a PyBullet wheel |
| Topology | topopt / solidspy | ✅ `pip install 'driftpin[topology]'` |
| Optics (sequential) | optiland / rayoptics | ✅ `pip install 'driftpin[optics]'` |
| Optics (non-seq) | KrakenOS (GPL) | ✅ `pip install 'driftpin[optics_gpl]'`, run out-of-process |
| Fluids properties | CoolProp | ✅ pip wheel |
| CFD (steady) | SU2 | ✅ fully native: the [Windows binary](https://su2code.github.io/download.html) runs and the CFD runner's SU2 branch is a direct subprocess — no bash/WSL (#203). Set `DRIFTPIN_SU2_PATH` (or use the provisioner; `%LOCALAPPDATA%\DriftPin\solvers` is auto-discovered) |
| Transient/radiation thermal | Elmer | ✅ verified native (26.2, portable no-GUI zip via `install-solvers.ps1 elmer` — note: no winget package exists); covers CHT, low-frequency EM, acoustic + harmonic FEM and the geometry bridge (#205) |
| Slicing | PrusaSlicer | ✅ Windows installer; `DRIFTPIN_PRUSASLICER_PATH` |
| Acoustics (BEM) | bempp-cl | ⚠️ needs an OpenCL ICD; dedicated venv (`DRIFTPIN_BEMPP_PYTHON`) |
| Full-wave EM | openEMS | ⚠️ prebuilt Windows binaries exist upstream, but not yet wired into the installer scripts |
| DEM (granular) | YADE (GPL) | ⚠️ no native Windows build — **WSL** |
| CFD/FSI/molding | OpenFOAM + preCICE + openInjMoldSim | ✅ **WSL2-backed, verified** ([#193](https://github.com/gchen19/DriftPin/issues/193)): discovery probes `\\wsl$\<distro>` (glob-only, side-effect-free) and launches route through `wsl -e bash`, so the server itself stays native Windows. Provision with `scripts/install-solvers.ps1 wsl` (one-time prerequisite: `wsl --install -d Ubuntu`) |

Every absent solver **degrades cleanly** — the family returns `{ok: false, reason, install}`
rather than crashing — so an incomplete solver set never breaks the server; those tools just
report "not available" with the fix. `driftpin doctor` shows the current state.

### The OpenFOAM families on Windows (WSL2-backed, #193)

CFD, FSI, and injection molding shell out to OpenFOAM through Linux-only mechanisms
(`bash -c 'source etc/bashrc && ...'`, `LD_LIBRARY_PATH`, `.so` adapter libs). Since
[#193](https://github.com/gchen19/DriftPin/issues/193) those launches route through the
WSL2 distro **automatically** — DriftPin itself (server, FreeCAD, case generation) runs
native Windows, and only the OpenFOAM subprocesses cross into the distro:

- **Launch**: `solvers.bash_argv(script)` swaps `["bash","-c",…]` for
  `["wsl","-d",<distro>,"-e","bash","-c",…]`. The Windows case dir passes as `cwd`
  unchanged — wsl.exe auto-maps it to `/mnt/<drive>/...`, so case files live on the
  Windows side and results parse natively.
- **Discovery**: side-effect-free globs over the `\\wsl$\<distro>` mirror find the
  in-distro bashrc/binaries/adapter libs and report them as POSIX paths with
  `via: "wsl"` (`driftpin doctor` shows `(in WSL)`). Nothing is executed to probe;
  note that merely statting `\\wsl$` can auto-start the distro VM.
- **Distro selection**: the registry default; override with `DRIFTPIN_WSL_DISTRO`
  (env var or `config.toml`).
- **Provisioning**: `scripts/install-solvers.ps1 wsl` — the Linux
  `scripts/install-solvers.sh` runs verbatim inside the distro (the repo is visible at
  `/mnt/...`). Source builds (FSI stack, OF7-org + openInjMoldSim) belong in the distro
  home (`~`), not `/mnt/c` — the 9P mount is slow for compiles. Case I/O on `/mnt/c` is
  fine at DriftPin's validation-case scale.
- **Docker** was evaluated and rejected for Windows (it runs on WSL2 anyway — strictly
  more machinery; see the decision record in #193). macOS remains documented-unsupported
  with clean degradation.

Without WSL (or with an unprovisioned distro), these families still degrade cleanly to
`{ok: false, reason, install}` with the WSL setup hint.

## Known Windows gaps (tracked)

- **Provisioning/build scripts** ([#194](https://github.com/gchen19/DriftPin/issues/194)) —
  the Windows-viable path is now covered by [`scripts/install-solvers.ps1`](../scripts/install-solvers.ps1)
  (pip-wheel extras + portable SU2/PrusaSlicer/Elmer, and the `wsl` target that reaches
  the `scripts/*.sh` builders inside the distro for the OpenFOAM families — #193).
- **Persistent config file** ([#199](https://github.com/gchen19/DriftPin/issues/199)) — env
  vars don't survive MCP-host launches; a `%APPDATA%\driftpin\config.toml` resolution layer
  is planned so paths persist without `setx`.
- **Renderers** (LuxCore/appleseed/cycles/OSPRay) — no automated install path on any OS yet.

## Process cleanup

On worker shutdown DriftPin sweeps any renderer subprocesses `freecadcmd` spawned. On POSIX
this is a process-group `SIGKILL`; on Windows the worker is started with
`CREATE_NEW_PROCESS_GROUP` and the tree is terminated with `taskkill /T /F`
([#195](https://github.com/gchen19/DriftPin/issues/195)), so a render in flight when the
worker dies doesn't leak an orphaned process.
