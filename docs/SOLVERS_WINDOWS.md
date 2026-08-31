# Installing solvers on Windows

Verified on Windows 11, FreeCAD 1.1, WSL2 (Ubuntu 26.04), 2026-08-30.

> ⚠️ **Measurement caveat.** These results were gathered over SSH. The WSL UNC mirror
> that OpenFOAM discovery depends on is **not reachable from an SSH session**, so the CFD
> result below is inconclusive rather than a genuine failure — see
> [The WSL discovery caveat](#the-wsl-discovery-caveat). Everything else is solid.

## Which interpreter — the trap

Windows has the same split as macOS, and it is the single most common reason a solver
"won't install":

| | Path | Version |
|---|---|---|
| MCP server venv | `%USERPROFILE%\AnkusDrive\.venv\Scripts\python.exe` | 3.13.14 |
| **Worker** | `C:\Program Files\FreeCAD 1.1\bin\python.exe` | **3.11.14** |

`solve_capabilities` is a worker handler, so its `find_spec` probe runs in FreeCAD's
Python. The difference is stark — same machine, same moment, two interpreters:

```
# from the venv                        # from FreeCAD's python (what the MCP sees)
available: calculix, elmer, optiland,  available: calculix, elmer, su2
           pybullet, rayoptics,          mbd        False
           su2, topopt                   topology   False
  mbd/topology/optics -> True            optics     False
```

`scripts\install-solvers.ps1` states in its header comment that "the wheel installs MUST
target the same venv that runs `ankusdrive mcp`", and `Install-Pip` (line 123) installs
into `$venvPy`. **That is wrong on Windows** for exactly the reason above — it is why the
box reports three wheel families as working from the venv while the MCP server sees none
of them.

Install wheel solvers into FreeCAD's Python instead:

```powershell
$FCPY = "C:\Program Files\FreeCAD 1.1\bin\python.exe"
& $FCPY -m pip install "numpy<2" pybullet solidspy optiland
```

Pin `numpy<2` — FreeCAD's bundled numpy is 1.26.x and numpy 2's ABI break takes FreeCAD's
compiled extensions with it. As on macOS, prefer `python -m pip` over the `pip` shim, and
skip `rayoptics` if it wants to pull a second Qt binding into the bundle (`optiland`
alone satisfies the `optics` family — the gate is `any_available`).

`topopt` is not needed: the entry accepts `("topopt", "solidspy")` and the implementation
uses solidspy.

## Result on the reference machine

From the worker's point of view — 3 of 12 families:

| Family | Status | Backend |
|---|---|---|
| `warpage` | ✅ | `C:\Program Files\FreeCAD 1.1\bin\ccx.exe` — bundled, zero setup |
| `thermal_transient` | ✅ | Elmer, portable no-GUI zip |
| `cfd` | ⚠️ inconclusive | SU2 8.5.0 ✅; OpenFOAM installed in WSL but probe blocked |
| `mbd`/`topology`/`optics` | ❌ from worker | wheels are in the venv, wrong interpreter |
| `optics_nonseq`, `acoustics_bem`, `dem`, `em_fullwave`, `fsi`, `slicing` | ❌ | not installed |

Windows is the only platform where **Elmer is easy** — a portable zip, versus a source
build on macOS and a PPA on Linux. `$ELMER_URL` points at CSC's official mirror,
`nic.funet.fi/pub/sci/physics/elmer/bin/windows/ElmerFEM-nogui-nompi-Windows-AMD64.zip`.
That mirror carries only `windows/` and `linux/` directories — there is no macOS build
there or anywhere else, which is the whole reason macOS has to compile it
([recipe](SOLVERS_MACOS.md#appendix-building-elmer-on-apple-silicon)).

Note `Install-Elmer` also verifies `ElmerGrid.exe` and `ViewFactors.exe` sit **next to**
`ElmerSolver.exe` — the worker resolves them as siblings (`worker.py:16398`), so all
three must stay in one directory on every platform.

## 1. `install-solvers.ps1`

```powershell
pwsh scripts\install-solvers.ps1 list          # ankusdrive doctor
pwsh scripts\install-solvers.ps1 su2           # portable SU2 binary
pwsh scripts\install-solvers.ps1 elmer         # portable no-GUI Elmer zip
pwsh scripts\install-solvers.ps1 prusaslicer
pwsh scripts\install-solvers.ps1 wsl           # OpenFOAM families via WSL2
pwsh scripts\install-solvers.ps1 all
```

Targets: `core pip su2 prusaslicer elmer wsl all list`.

Portable binaries land in the provisioner directory and are auto-discovered with **no env
var** — which matters because MCP hosts launch the server with a minimal environment:

```
%LOCALAPPDATA%\AnkusDrive\solvers\SU2-8.5.0\bin\SU2_CFD.exe
%LOCALAPPDATA%\AnkusDrive\solvers\ElmerFEM-nogui-nompi\...\ElmerSolver.exe
```

(On a checkout made before the 0.5 rename these sit under the old package name's
directory instead. Both resolve; the glob follows the package name of the checkout.)

CalculiX needs nothing at all: FreeCAD 1.1 bundles `ccx.exe` (verified 2.22) in its `bin`,
and `solvers.py` globs FreeCAD's bundled bin on every OS.

Use `-Persist` to write resolved paths through to the config layer rather than relying on
the current shell.

## 2. OpenFOAM families via WSL2

There is no native Windows OpenFOAM. The Windows analogue of the macOS Multipass bridge
is WSL2: `bash_argv` swaps `["bash","-c",script]` for
`["wsl","-d",<distro>,"-e","bash","-c",script]`. `wsl.exe` auto-maps a Windows cwd to
`/mnt/<drive>/...`, so case directories cross with zero path translation — no identity
mount needed (unlike macOS).

```powershell
wsl --install -d Ubuntu     # then reboot
pwsh scripts\install-solvers.ps1 wsl
```

That target runs `bash scripts/install-solvers.sh cfd fsi` inside the distro. Build
**inside the distro filesystem** (`~`), not `/mnt/c` — the 9P mount is slow for compiles.
Case dirs on `/mnt/c` are fine.

Override the probed distro with `ANKUSDRIVE_WSL_DISTRO` (env or config.toml).

Only `openfoam` and `precice` declare `substrate_bins`, so only those two can be driven
from inside WSL. Elmer, YADE and openEMS must run natively on Windows — which for YADE
and openEMS means they are unavailable, since both build recipes are Linux-gated.

### The WSL discovery caveat

Discovery probes the distro's filesystem through its `\\wsl$\<distro>` UNC mirror —
glob-only, nothing is executed. **This does not work from an SSH session**, which is how
the reference machine was measured:

```
\\wsl$\Ubuntu\usr\lib\openfoam        -> False
\\wsl.localhost\Ubuntu\usr\lib\openfoam -> Access is denied
```

Meanwhile, executing into the distro directly shows OpenFOAM is fully installed:

```
$ wsl -e bash -c '...'
/usr/lib/openfoam/openfoam2512/platforms/linux64GccDPInt32Opt/bin/simpleFoam
/usr/lib/openfoam/openfoam2512/etc/bashrc
Ubuntu 26.04 LTS
```

The WSL 9P share is bound to an interactive desktop session, so a service/SSH logon
cannot see it. **Re-check `cfd` from an interactive session on that machine** before
concluding anything is broken. If it really does report absent there, wire it explicitly
in `%APPDATA%\ankusdrive\config.toml`:

```toml
[solvers]
openfoam_bashrc = "/usr/lib/openfoam/openfoam2512/etc/bashrc"
openfoam_path = "/usr/lib/openfoam/openfoam2512/platforms/linux64GccDPInt32Opt/bin/simpleFoam"
```

These stay **POSIX paths** — they are consumed only by the bash that runs in-distro; the
UNC form exists purely as the discovery probe vehicle.

## 3. Not available on Windows

| Family | Solver | Why |
|---|---|---|
| `dem` | YADE | `build_dem_gpl` is Linux-gated (apt toolchain) |
| `em_fullwave` | openEMS | `build_openems` is Linux-gated |
| `optics_nonseq` | KrakenOS | pip-installable — just not installed here. `& $FCPY -m pip install KrakenOS "setuptools<81"` |
| `acoustics_bem` | bempp | needs a dedicated venv (`meshio>=5`); not set up here |
| `slicing` | PrusaSlicer | `install-solvers.ps1 prusaslicer` not yet run |

`fsi` (preCICE) is reachable in principle — it declares `substrate_bins`, so a preCICE
build inside the WSL distro would resolve — but the recipe has not been run there.

## Gotchas

- **Default SSH shell is PowerShell, not cmd.** `&` is not a statement separator; use `;`.
  `2>/dev/null` in a PowerShell context becomes a literal `C:\dev\null` write and fails.
  For anything with redirects, base64 the script and decode it on the far side.
- **`wsl -l` output is UTF-16.** It renders as one character per line through a pipe. Use
  `wsl -e bash -c` for anything you intend to parse.
- Windows OpenSSH prints a multi-line post-quantum key-exchange warning to stderr. It is
  cosmetic; filter it when parsing.

## Config location

`%APPDATA%\ankusdrive\config.toml` (`ankusdrive/config.py:90`). Keys map
`ANKUSDRIVE_FOO_BAR` → `[solvers] foo_bar`. Prefer it over shell exports — MCP hosts spawn
the server with a minimal environment.
