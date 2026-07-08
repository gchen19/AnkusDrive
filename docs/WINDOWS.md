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
`heavy-solves.yml`, which are now pinned to the `Linux` label — both self-hosted runners
share the `driftpin` label, so the OS label keeps a job from landing on the wrong box and
running the other OS's shell scripts. The `Windows`/`Linux` labels are applied
automatically by the Actions runner from the host OS. The FreeCAD-free `fast-checks.yml`
(ruff + contracts) stays on a GitHub-hosted Ubuntu runner and covers both.

## Per-solver Windows reality

| Family | Solver | Windows status |
|---|---|---|
| Core FEM | CalculiX `ccx` | ✅ **bundled** in FreeCAD's `bin\ccx.exe` — nothing to install |
| MBD | PyBullet / MuJoCo | ⚠️ `pip install 'driftpin[mbd]'` — PyBullet ships **no wheel for Python 3.13** (source-build needs a C++ compiler). Use Python **3.11/3.12**, or `pip install mujoco` (the registry's alternative, which has 3.13 wheels). Absent → `test_mbd` skips |
| Topology | topopt / solidspy | ✅ `pip install 'driftpin[topology]'` |
| Optics (sequential) | optiland / rayoptics | ✅ `pip install 'driftpin[optics]'` |
| Optics (non-seq) | KrakenOS (GPL) | ✅ `pip install 'driftpin[optics_gpl]'`, run out-of-process |
| Fluids properties | CoolProp | ✅ pip wheel |
| CFD (steady) | SU2 | ✅ official [Windows binary](https://su2code.github.io/download.html); put `SU2_CFD.exe` on PATH or set `DRIFTPIN_SU2_PATH` |
| Transient/radiation thermal | Elmer | ✅ good native Windows installer ([elmerfem.org](https://www.elmerfem.org/)); `DRIFTPIN_ELMER_PATH` if not on PATH |
| Slicing | PrusaSlicer | ✅ Windows installer; `DRIFTPIN_PRUSASLICER_PATH` |
| Acoustics (BEM) | bempp-cl | ⚠️ needs an OpenCL ICD; dedicated venv (`DRIFTPIN_BEMPP_PYTHON`) |
| Full-wave EM | openEMS | ⚠️ prebuilt Windows binaries exist upstream, but not yet wired into the installer scripts |
| DEM (granular) | YADE (GPL) | ⚠️ no native Windows build — **WSL** |
| CFD/FSI/molding | OpenFOAM + preCICE | ⚠️ Linux shell + linker glue (`bash`, `LD_LIBRARY_PATH`, `.so`) — **WSL / Docker / blueCFD** ([#193](https://github.com/gchen19/DriftPin/issues/193)) |

Every absent solver **degrades cleanly** — the family returns `{ok: false, reason, install}`
rather than crashing — so an incomplete solver set never breaks the server; those tools just
report "not available" with the fix. `driftpin doctor` shows the current state.

### The OpenFOAM families on Windows

CFD, FSI, and injection molding shell out to OpenFOAM through Linux-only mechanisms
(`bash -c 'source etc/bashrc && ...'`, `LD_LIBRARY_PATH`, `.so` adapter libs). None run
under native Windows today. Two supported paths:

- **WSL2** — install DriftPin + OpenFOAM inside a WSL Ubuntu, run the server there; the
  Windows FreeCAD isn't reachable from WSL, so install FreeCAD in the WSL environment too.
- **Docker** — run the OpenFOAM steps in a Linux OpenFOAM container (a container backend is
  proposed in [#193](https://github.com/gchen19/DriftPin/issues/193), option **b**).

Until then, on native Windows these families report "not available" via clean degradation.

## Known Windows gaps (tracked)

- **Provisioning/build scripts** ([#194](https://github.com/gchen19/DriftPin/issues/194)) —
  `scripts/*.sh` are bash + Linux-x86_64; no PowerShell wrappers yet. The pip-wheel extras
  install fine with a plain `pip install 'driftpin[...]'`; the shell scripts are only needed
  for the source-built GPL families.
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
