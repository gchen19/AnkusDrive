# Windows support

AnkusDrive's core — CAD modeling and structural FEM — runs natively on Windows against
a stock FreeCAD 1.1 install, no extra setup. This page covers how FreeCAD is located,
what `ankusdrive doctor` tells you, and the honest per-solver install reality on Windows.

Tracking epic: [#189](https://github.com/gchen19/AnkusDrive/issues/189).

## TL;DR — verified working on Windows 11 + FreeCAD 1.1.1

```powershell
# 1. Install FreeCAD 1.1.x from https://www.freecad.org/  (nothing goes on PATH)
# 2. From a clone — one script does venv + pinned pip install + doctor + MCP wiring:
powershell -ExecutionPolicy Bypass -File scripts\install-core.ps1
```

It ends by printing the `claude mcp add` one-liner and the
`claude_desktop_config.json` block with **your** absolute paths — see
[The MCP step](#the-mcp-step-registering-the-server) below. Everything it does by
hand, if you'd rather:

```powershell
py -m venv .venv
.venv\Scripts\pip install -e .

.venv\Scripts\ankusdrive ping      # ping=pong freecad=1.1.1
.venv\Scripts\ankusdrive doctor    # FreeCAD + solver checklist
.venv\Scripts\ankusdrive fem cantilever   # CalculiX solve via bundled ccx.exe
```

- **Core CAD** — the `freecadcmd.exe` worker boots and builds geometry. ✅
- **Structural FEM (CalculiX)** — FreeCAD's Windows install bundles `ccx.exe` and
  `gmsh.exe` in `bin\`, so `femtools.ccxtools` resolves them with no separate install.
  A cantilever solve returns the expected displacement/stress. ✅
- **Pure-Python families** (materials, tolerance/GD&T, gears, bearings, thermal-lumped,
  fits, standards) — platform-agnostic. ✅
- **pip-wheel solvers** (PyBullet, MuJoCo, topopt/solidspy, rayoptics, optiland,
  CoolProp) — Windows wheels exist; `pip install 'ankusdrive[...]'` works. ✅
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

## The core install script

[`scripts/install-core.ps1`](../scripts/install-core.ps1) exists because of
[#279](https://github.com/gchen19/AnkusDrive/issues/279): a user on Windows 11 followed the
README by hand, got stuck at the MCP step, and abandoned the local install. Windows had a
script for the *optional solvers* and none for the *core*. It runs, in order:

1. **Interpreter check first** — states the supported range before pip can fail inside its
   resolver with no attribution.
2. **FreeCAD lookup** (report only; the client resolves it at runtime the same way).
3. **venv + `pip install -e .`** with pyproject's pins, notably `mcp>=1.2,<2` (#277).
4. **Dependency verification** — re-imports the resolved `mcp`/`numpy`/`Pillow` and proves
   `mcp.server.fastmcp` is really there. `pip install` succeeding is not the same thing:
   mcp 2.x installs cleanly, `ping`/`doctor` keep passing, and only `ankusdrive mcp` dies.
5. **`ankusdrive doctor` + `ankusdrive ping`**, then a real **MCP stdio handshake**
   (`tests/test_mcp_boot.py` — initialize + `tools/list`, no FreeCAD needed).
6. **The MCP registration block**, with resolved absolute paths.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-core.ps1
powershell -ExecutionPolicy Bypass -File scripts\install-core.ps1 -Extras mbd,fluids -Persist
powershell -ExecutionPolicy Bypass -File scripts\install-core.ps1 -Python 'C:\Program Files\Python313\python.exe'
```

| Switch | Effect |
|---|---|
| `-Python <path>` | interpreter to build the venv from (default: `py -3` / `python` / common install dirs, each validated by running it) |
| `-VenvDir <path>` | where the venv goes (default `.venv` in the checkout) |
| `-Extras a,b` | pip-wheel solver extras (`mbd`, `topology`, `optics`, `fluids`), installed best-effort and independently |
| `-SkipDoctor` | skip the FreeCAD/MCP verification (offline or FreeCAD-less box) |
| `-Persist` | write the resolved FreeCAD path to `%APPDATA%\ankusdrive\config.toml` via `ankusdrive setup --yes` |

It is idempotent, needs no admin, and installs nothing system-wide.
`scripts\install-solvers.ps1 core` forwards to it, for anyone who found the solver script
first. Windows PowerShell 5.1 is enough (the script is 5.1-compatible and ASCII-only).

## Supported Python versions

**3.10 – 3.14.** `requires-python` is `>=3.10` with **no upper bound**, and that is a
decision, not an oversight (#279): 3.14 was *run*, not guessed at. On Windows 11 with
CPython **3.14.7**, `pip install -e .` resolves `mcp 1.29.0` (pure-Python wheel),
`numpy 2.5.2` and `Pillow 12.3.0` (both ship `cp314-win_amd64` wheels), the MCP server
completes a stdio handshake, and `ankusdrive ping` returns `freecad=1.1.1`. A hard `<3.15`
ceiling would only break the next CPython for no reason, so the *verified* range lives in
the `Programming Language :: Python :: 3.x` classifiers and in `install-core.ps1`'s
`$PY_MAX_VERIFIED` instead — `tests/test_windows_support.py` keeps the two in step.

Your host Python is independent of FreeCAD's: `freecadcmd.exe` runs as a subprocess with
its own bundled interpreter, so a 3.14 host driving FreeCAD 1.1's Python 3.11 is normal.
Above the verified ceiling the script **warns and continues** rather than refusing — the
deps are wheels-or-pure-Python and usually catch up quickly.

## The MCP step (registering the server)

This is where #279's reporter stopped, so it gets its own section. The server speaks
**stdio**; the host launches `ankusdrive mcp` itself. The one thing that trips people up:
**a GUI MCP host does not inherit your shell's `PATH`**, and a venv install puts
`ankusdrive.exe` somewhere that is not on the system PATH at all — so always register the
**absolute** path. Print the exact block for this machine at any time:

```powershell
.venv\Scripts\ankusdrive setup --print-mcp-config
```

**Claude Code:**

```powershell
claude mcp add ankusdrive -- C:\Users\you\AnkusDrive\.venv\Scripts\ankusdrive.exe mcp
claude mcp list                      # ankusdrive should report connected
```

**Claude Desktop / Cursor** — `%APPDATA%\Claude\claude_desktop_config.json` (create it if
it doesn't exist), then restart the app. JSON needs escaped backslashes; forward slashes
work too:

```json
{
  "mcpServers": {
    "ankusdrive": {
      "command": "C:\\Users\\you\\AnkusDrive\\.venv\\Scripts\\ankusdrive.exe",
      "args": ["mcp"]
    }
  }
}
```

If the host reports a closed connection:

- Run `.venv\Scripts\ankusdrive ping` — same worker boot path, far better error messages.
- Run `.venv\Scripts\python tests\test_mcp_boot.py` — proves the stdio handshake works
  without FreeCAD, which separates "MCP is broken" from "FreeCAD isn't resolving".
- Check the mcp version: `.venv\Scripts\pip show mcp`. It must be **1.x** — 2.0.0 dropped
  `mcp.server.fastmcp` and breaks the server while `ping`/`doctor` still pass
  ([#277](https://github.com/gchen19/AnkusDrive/issues/277)).
- If FreeCAD resolves in your terminal but not under the host, that's the minimal-env
  problem: persist the path with `install-core.ps1 -Persist` (writes
  `%APPDATA%\ankusdrive\config.toml`), which survives a host launch where `$env:` does not.

## How AnkusDrive finds FreeCAD

Resolution order (in [`ankusdrive/client.py`](../ankusdrive/client.py) `_resolve_freecadcmd`):

1. **`$ANKUSDRIVE_FREECADCMD`** — explicit override, returned verbatim.
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
$env:ANKUSDRIVE_FREECADCMD = "D:\Apps\FreeCAD\bin\freecadcmd.exe"
```

To make it stick across shells and MCP-host launches, set it as a user environment
variable (`setx ANKUSDRIVE_FREECADCMD "..."`) — MCP hosts spawn the server with a minimal
env, so a one-shot `$env:` export in your terminal won't carry over (see
[#199](https://github.com/gchen19/AnkusDrive/issues/199) for the planned config-file layer).

`ankusdrive doctor` reports which layer resolved FreeCAD and lists every candidate it
checked — the fastest way to debug "FreeCAD not found".

## `ankusdrive doctor`

One cross-platform health report: resolves FreeCAD **and** every solver family and prints,
per item, found/missing, the resolved path (or why it didn't resolve), and the exact fix.
The solver half needs no FreeCAD boot and no running MCP server.

```powershell
ankusdrive doctor            # human-readable checklist
ankusdrive doctor --json     # machine-readable (CI preflight; exits non-zero if FreeCAD unresolved)
ankusdrive doctor --no-boot  # resolve FreeCAD's path only, skip the version probe (faster)
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
suite on a **self-hosted Windows runner** (label `[self-hosted, ankusdrive, Windows]`) with
FreeCAD 1.1 preinstalled. It is a separate workflow from the Linux `test.yml` /
`heavy-solves.yml`, which are now pinned to the `Linux` label — every self-hosted runner
shares the `ankusdrive` label, so the OS label keeps a job from landing on the wrong box and
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
pwsh scripts\install-solvers.ps1 list               # == ankusdrive doctor
```

- **pip-wheel families** (`mbd`, `topology`, `optics`, `fluids`) install into the `.venv`
  that runs `ankusdrive mcp` — a plain `pip install '.[extra]'`, best-effort/independent so
  one missing wheel doesn't abort the rest.
- **Portable binaries** (`su2`, `prusaslicer`) download as a zip into
  `%LOCALAPPDATA%\AnkusDrive\solvers`, extract, and wire `ANKUSDRIVE_<SOLVER>_PATH`. No
  installer, no admin.
- **CalculiX** is never installed here — it rides on FreeCAD's bundled `ccx.exe`, which
  `ankusdrive/solvers.py` auto-discovers (see the `warpage` row below).

### Verified on this box (Windows 11 + FreeCAD 1.1.1, Python 3.13.14)

Every result below is a real solve/run on the machine, not a dry check:

| Family | Solver | How | Verified result |
|---|---|---|---|
| **warpage** | CalculiX (bundled `ccx.exe` 2.22) | auto-discovered — no install | thin-plate thermo-elastic solve: `rc=0`, 4981 nodes / 2424 tets, warp 7.0 mm vs 3.5 mm analytic, gate computed |
| **FEM modal** | CalculiX (bundled) | auto-discovered | `test_merge_modal_gate` live modal solve: FEM f₁=304.6 Hz vs 308.5 Hz oracle (ratio 0.99), 17/17 |
| **mbd** | PyBullet 3.2.7 | `pip install '.[mbd]'` | wheel installs on 3.13, `test_mbd` runs |
| **slicing** | PrusaSlicer 2.9.6 | portable zip + `ANKUSDRIVE_PRUSASLICER_PATH` | live slice: 20 mm cube → 8.06 cm³ @100% infill (exact 8.0, ratio 1.008), 99 layers, 10/10 |
| **cfd** | SU2 8.5.0 | portable zip + `ANKUSDRIVE_SU2_PATH` | `SU2_CFD.exe` runs; end-to-end `cfd_*_flow_submit` solve verified native — no bash (#203) |
| **thermal_transient** | Elmer 26.2 | portable no-GUI zip + `ANKUSDRIVE_ELMER_PATH` | ElmerSolver/ElmerGrid/ViewFactors verified live: thermal, radiation, CHT, EM, acoustic + harmonic FEM, geometry bridge (#205) |
| **studio_render** | Blender 5.2.1 LTS | portable zip via `install-solvers.ps1 blender` — **no env var** | live Cycles render through the MCP `render_photoreal` tool: 5-part assembly, per-part appearance, `scene='studio'`, `quality='final'` (384 samples, OIDN) at 960×720 in 56 s on the **HIP** GPU (AMD Radeon 780M) — see [`artifacts/rendering/render_blender_studio_windows.png`](../artifacts/rendering/render_blender_studio_windows.png) (#335) |
| **optics** | optiland + rayoptics | `pip install '.[optics]'` | ready |
| **topology** | solidspy | `pip install '.[topology]'` | ready |
| **fluids** | CoolProp 8.0.0 | `pip install '.[fluids]'` | ready (in-process f(T,P)) |

The bundled-`ccx` auto-discovery means the two live-CalculiX tests that used to skip on
Windows (`test_merge_modal_gate`, `test_render::test_fem_colormap_monotonic_gradient`) now
run the **real** solve — their gates consult `ankusdrive.solvers.ccx_bin()`, which finds
FreeCAD's bundled `ccx.exe` even though it isn't on PATH.

## Per-solver Windows reality

| Family | Solver | Windows status |
|---|---|---|
| Core FEM / warpage | CalculiX `ccx` | ✅ **bundled** in FreeCAD's `bin\ccx.exe` (v2.22 in FreeCAD 1.1) — **nothing to install**. `ankusdrive/solvers.py` auto-discovers it in FreeCAD's bin, so the `warpage` family and the live-ccx tests resolve it with no PATH entry or env var |
| MBD | PyBullet / MuJoCo | ✅ `pip install 'ankusdrive[mbd]'` — PyBullet now ships a **Python 3.13 wheel** (3.2.7, verified installing on 3.13.14). `pip install mujoco` is the registry's alternative if a future Python lacks a PyBullet wheel |
| Topology | topopt / solidspy | ✅ `pip install 'ankusdrive[topology]'` |
| Optics (sequential) | optiland / rayoptics | ✅ `pip install 'ankusdrive[optics]'` |
| Optics (non-seq) | KrakenOS (GPL) | ✅ `pip install 'ankusdrive[optics_gpl]'`, run out-of-process |
| Fluids properties | CoolProp | ✅ pip wheel |
| CFD (steady) | SU2 | ✅ fully native: the [Windows binary](https://su2code.github.io/download.html) runs and the CFD runner's SU2 branch is a direct subprocess — no bash/WSL (#203). Set `ANKUSDRIVE_SU2_PATH` (or use the provisioner; `%LOCALAPPDATA%\AnkusDrive\solvers` is auto-discovered) |
| Transient/radiation thermal | Elmer | ✅ verified native (26.2, portable no-GUI zip via `install-solvers.ps1 elmer` — note: no winget package exists); covers CHT, low-frequency EM, acoustic + harmonic FEM and the geometry bridge (#205) |
| Slicing | PrusaSlicer | ✅ Windows installer; `ANKUSDRIVE_PRUSASLICER_PATH` |
| Studio render (photoreal) | Blender (Cycles) | ✅ **verified native** ([#335](https://github.com/gchen19/AnkusDrive/issues/335)): `scripts\install-solvers.ps1 blender` drops the pinned portable zip under `%LOCALAPPDATA%\AnkusDrive\solvers` and `solvers.py` globs `blender.exe` out of it — **no admin, no env var, no PATH entry**. GPU is picked automatically: OptiX/CUDA (NVIDIA), **HIP** (AMD), oneAPI (Intel), else CPU. ⚠️ The `blender-winget` target / `winget install BlenderFoundation.Blender` **does not work**: winget fetches the MSI from `download.blender.org`, which answers 403 to scripted clients (`0x80190193`) — the same challenge that made the portable target try mirrors first. Use `install-solvers.ps1 blender` |
| Acoustics (BEM) | bempp-cl | ⚠️ needs an OpenCL ICD; dedicated venv (`ANKUSDRIVE_BEMPP_PYTHON`) |
| Full-wave EM | openEMS | ⚠️ prebuilt Windows binaries exist upstream, but not yet wired into the installer scripts |
| DEM (granular) | YADE (GPL) | ⚠️ no native Windows build — **WSL** |
| CFD/FSI/molding | OpenFOAM + preCICE + openInjMoldSim | ✅ **WSL2-backed, verified** ([#193](https://github.com/gchen19/AnkusDrive/issues/193)): discovery probes `\\wsl$\<distro>` (glob-only, side-effect-free) and launches route through `wsl -e bash`, so the server itself stays native Windows. Provision with `scripts/install-solvers.ps1 wsl` (one-time prerequisite: `wsl --install -d Ubuntu`) |

Every absent solver **degrades cleanly** — the family returns `{ok: false, reason, install}`
rather than crashing — so an incomplete solver set never breaks the server; those tools just
report "not available" with the fix. `ankusdrive doctor` shows the current state.

### The OpenFOAM families on Windows (WSL2-backed, #193)

CFD, FSI, and injection molding shell out to OpenFOAM through Linux-only mechanisms
(`bash -c 'source etc/bashrc && ...'`, `LD_LIBRARY_PATH`, `.so` adapter libs). Since
[#193](https://github.com/gchen19/AnkusDrive/issues/193) those launches route through the
WSL2 distro **automatically** — AnkusDrive itself (server, FreeCAD, case generation) runs
native Windows, and only the OpenFOAM subprocesses cross into the distro:

- **Launch**: `solvers.bash_argv(script)` swaps `["bash","-c",…]` for
  `["wsl","-d",<distro>,"-e","bash","-c",…]`. The Windows case dir passes as `cwd`
  unchanged — wsl.exe auto-maps it to `/mnt/<drive>/...`, so case files live on the
  Windows side and results parse natively.
- **Discovery**: side-effect-free globs over the `\\wsl$\<distro>` mirror find the
  in-distro bashrc/binaries/adapter libs and report them as POSIX paths with
  `via: "wsl"` (`ankusdrive doctor` shows `(in WSL)`). Nothing is executed to probe;
  note that merely statting `\\wsl$` can auto-start the distro VM.
- **Distro selection**: the registry default; override with `ANKUSDRIVE_WSL_DISTRO`
  (env var or `config.toml`).
- **Provisioning**: `scripts/install-solvers.ps1 wsl` — the Linux
  `scripts/install-solvers.sh` runs verbatim inside the distro (the repo is visible at
  `/mnt/...`). Source builds (FSI stack, OF7-org + openInjMoldSim) belong in the distro
  home (`~`), not `/mnt/c` — the 9P mount is slow for compiles. Case I/O on `/mnt/c` is
  fine at AnkusDrive's validation-case scale.
- **Docker** was evaluated and rejected for Windows (it runs on WSL2 anyway — strictly
  more machinery; see the decision record in #193). macOS remains documented-unsupported
  with clean degradation.

Without WSL (or with an unprovisioned distro), these families still degrade cleanly to
`{ok: false, reason, install}` with the WSL setup hint.

## Known Windows gaps (tracked)

- **Provisioning/build scripts** ([#194](https://github.com/gchen19/AnkusDrive/issues/194)) —
  the Windows-viable path is now covered by [`scripts/install-solvers.ps1`](../scripts/install-solvers.ps1)
  (pip-wheel extras + portable SU2/PrusaSlicer/Elmer, and the `wsl` target that reaches
  the `scripts/*.sh` builders inside the distro for the OpenFOAM families — #193).
- **Persistent config file** ([#199](https://github.com/gchen19/AnkusDrive/issues/199)) — env
  vars don't survive MCP-host launches; a `%APPDATA%\ankusdrive\config.toml` resolution layer
  is planned so paths persist without `setx`.
- **Renderers** (LuxCore/appleseed/cycles/OSPRay) — no automated install path on any OS yet.
  The photoreal path that *is* turnkey on Windows is Blender (issue
  [#335](https://github.com/gchen19/AnkusDrive/issues/335)):
  `pwsh scripts\install-solvers.ps1 blender` (pinned portable zip, no admin, auto-discovered
  under `%LOCALAPPDATA%\AnkusDrive\solvers`). See [`RENDERING.md`](RENDERING.md) §3.
- **`winget install BlenderFoundation.Blender`** (and so the `blender-winget` target) is
  **broken upstream, not in AnkusDrive**: the winget manifest points at
  `download.blender.org`, which returns 403 to scripted clients, so winget aborts with
  `0x80190193 : Forbidden (403)` after resolving the package. winget cannot be pointed at a
  mirror. The portable target already works around the same challenge by trying official
  mirrors first, so use it; the winget target stays in the script and will start working
  again if Blender's CDN stops challenging winget. If you specifically want the
  **per-machine** install winget would have done, use `install-solvers.ps1 blender-msi`:
  the same official `.msi`, fetched from the mirrors with the same pinned SHA-256 and
  installed with `msiexec /i /qn` into `Program Files\Blender Foundation\Blender 5.2`
  (auto-discovered, no env var). It needs an **elevated** shell; the portable `blender`
  target does not.

## Process cleanup

On worker shutdown AnkusDrive sweeps any renderer subprocesses `freecadcmd` spawned. On POSIX
this is a process-group `SIGKILL`; on Windows the worker is started with
`CREATE_NEW_PROCESS_GROUP` and the tree is terminated with `taskkill /T /F`
([#195](https://github.com/gchen19/AnkusDrive/issues/195)), so a render in flight when the
worker dies doesn't leak an orphaned process.
