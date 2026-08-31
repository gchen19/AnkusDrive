# Installing solvers on Linux

Verified on Ubuntu 24.04.4 LTS, x86_64, kernel 7.0.0-30, Python 3.12.3, 2026-08-30. This is the most complete solver install of the three platforms:
**11 of 12 families resolve.** Linux is where every source-build recipe in
`scripts/install-solvers.sh` is actually supported — `build_dem_gpl`, `build_openems` and
`build_fsi` all `die` on any other OS.

> ⚠️ **Two caveats on the reference machine.** It has **no FreeCAD installed**, so the
> numbers below come from `.venv/bin/python`, not from a real `freecadcmd` worker — see
> [Which interpreter](#which-interpreter). And its bempp install has a
> [latent runtime break](#4-bempp--the-meshio-trap).

## Result on the reference machine

```
available: bempp, calculix, elmer, kraken, openfoam, optiland,
           precice, prusaslicer, pybullet, rayoptics, topopt, yade
unwired:   openems
```

| Family | Status | Backend | How it got there |
|---|---|---|---|
| `warpage` | ✅ | CalculiX 2.21 | `apt install calculix-ccx` |
| `thermal_transient` | ✅ | Elmer 9.0 | `apt install elmerfem-csc` (CSC PPA) |
| `slicing` | ✅ | prusa-slicer | `apt install prusa-slicer` |
| `cfd` | ✅ | OpenFOAM v2512 + v2606 | apt, openfoam.com repo |
| `mbd` | ✅ | pybullet | pip |
| `topology` | ✅ | solidspy | pip |
| `optics` | ✅ | rayoptics + optiland | pip |
| `optics_nonseq` | ✅ | KrakenOS | pip |
| `dem` | ✅ | YADE → `~/opt/yade/bin/yade` | cmake source build |
| `fsi` | ✅ | preCICE → `~/calculix-adapter/bin/ccx_preCICE` | source build |
| `acoustics_bem` | ⚠️ reports ok, **will fail at runtime** | bempp-cl | pip, into the *shared* venv |
| `em_fullwave` | ⚠️ works, reports "unwired" | openEMS → `~/.venv-openems` | source build |

## Which interpreter

Solver discovery runs in the **worker** — the interpreter behind `freecadcmd` — not in
the venv that runs the MCP server. On macOS and Windows FreeCAD always bundles its own
Python, so `pip install` into the repo `.venv` is silently invisible to the probe. On
Linux it depends entirely on how FreeCAD was installed:

| FreeCAD install | Worker interpreter | Does a `.venv` install work? |
|---|---|---|
| `apt install freecad` (distro) | system `python3` | Only if the venv was made with `--system-site-packages`, or you `pip install --user` |
| AppImage / snap / flatpak | FreeCAD's **bundled** Python | ❌ same trap as macOS — install into the bundle |
| conda | the conda env's Python | Install into that env |

`scripts/install-solvers.sh:95` defaults `PY` to `.venv/bin/python3`. On Linux that is a
reasonable default *only* for the distro-package case. Check before trusting it:

```bash
freecadcmd -c "import sys; print(sys.executable, sys.prefix)"
```

then point `PY` at whatever that reports:

```bash
PY=/usr/bin/python3 bash scripts/install-solvers.sh mbd topology optics
```

**The reference machine has no FreeCAD at all**, which is why its report is clean — the
probe ran in `.venv/bin/python`, where all the wheels live. Install FreeCAD there and the
wheel-backed families (`mbd`, `topology`, `optics`, `optics_nonseq`, `acoustics_bem`)
will flip to absent until they are reinstalled into the worker's interpreter.

FreeCAD is not in the Ubuntu 24.04 archive (`apt-cache policy freecad` → `Candidate:
(none)`). Use the AppImage:

```bash
bash scripts/install-solvers.sh freecad
```

## 1. apt-installable solvers

The easy majority — no build, no wiring, auto-discovered on `PATH`:

```bash
sudo apt install -y calculix-ccx prusa-slicer

# Elmer — CSC PPA, not the base archive
sudo apt install -y elmerfem-csc
```

Verify: `ccx -v`, `ElmerSolver -v`, `prusa-slicer --help`.

## 2. OpenFOAM

Native on Linux — no VM, unlike macOS.

```bash
# openfoam.com (ESI) packages
sudo apt install -y openfoam2512 openfoam2512-dev
# openfoam.org alternative:
#   sudo add-apt-repository http://dl.openfoam.org/ubuntu
#   sudo apt install -y openfoam11
```

### Wire it explicitly if you have more than one version

The reference machine carries three side by side:

```
/usr/lib/openfoam/openfoam  /usr/lib/openfoam/openfoam2512  /usr/lib/openfoam/openfoam2606
```

With several installed, discovery finds a `bashrc` but no unambiguous binary and reports
`unwired`. **Setting `openfoam_bashrc` alone is not enough** — that was verified: the
family stayed `false` until `openfoam_path` was set to the actual solver binary.

`~/.config/ankusdrive/config.toml`:

```toml
[solvers]
openfoam_path = "/usr/lib/openfoam/openfoam2606/platforms/linux64GccDPInt32Opt/bin/simpleFoam"
openfoam_bashrc = "/usr/lib/openfoam/openfoam2606/etc/bashrc"
openems_python = "/home/george/.venv-openems/bin/python3"
```

Keys map `ANKUSDRIVE_FOO_BAR` → `[solvers] foo_bar` (`ankusdrive/config.py:166`). Use the
config file rather than shell exports: MCP hosts spawn the server with a minimal
environment.

Note the platform triple differs by arch — `linux64GccDPInt32Opt` on x86_64,
`linuxARM64GccDPInt32Opt` on arm64.

## 3. Source builds (Linux-only)

All three are GPL/LGPL and are driven strictly **out-of-process**, so their copyleft never
links into AnkusDrive's permissive code. Each takes a while and needs `sudo` for apt
build-deps.

```bash
bash scripts/install-solvers.sh dem_gpl    # YADE   -> ~/opt/yade/bin/yade
bash scripts/install-solvers.sh em_gpl     # openEMS -> ~/.venv-openems
bash scripts/install-solvers.sh fsi        # preCICE -> ~/calculix-adapter/bin/ccx_preCICE
```

`build_fsi` is the heaviest: serial libprecice (MPI off) + the CalculiX adapter
(CalculiX 2.20 source + SPOOLES + ARPACK) + the OpenFOAM adapter (`wmake` against
`openfoam2512-dev`). It needs OpenFOAM **already installed** and sets four env vars —
`ANKUSDRIVE_CCX_PRECICE`, `ANKUSDRIVE_PRECICE_LIB`, `ANKUSDRIVE_OPENFOAM_ADAPTER_LIB`,
`ANKUSDRIVE_OPENFOAM_BASHRC`.

### openEMS reports "unwired" even when correct

Same reporting gap as bempp on macOS. `openems` declares `"modules": ("openEMS",)`, probed
with `find_spec` **in the worker interpreter** — which by design does not have it, since
openEMS lives in `~/.venv-openems`. Setting `ANKUSDRIVE_OPENEMS_PYTHON` makes it *run*
(the runner reads it) but does not flip `available`, so
`em_fullwave.any_available` stays `false`.

Verified on the reference machine: config resolves the var correctly and
`~/.venv-openems/bin/python3 -c "import openEMS, CSXCAD"` succeeds — while the report
still says unwired. Treat it as cosmetic.

## 4. bempp — the meshio trap

bempp-cl needs `meshio>=4` for `cells_dict`; solidspy pins `meshio==3`. The design puts
bempp in a **dedicated** `.venv-bempp` for exactly this reason.

**The reference machine gets this wrong**, and it is worth checking on yours. bempp-cl was
installed into the *shared* `.venv` alongside solidspy, so:

```
meshio 3.0.0
solidspy ok
bempp_cl ok            <- imports fine, so the probe reports "available"
has cells_dict: False  <- but the API bempp needs is missing
```

`import bempp_cl` succeeds, so `acoustics_bem` reports `any_available: true` — then fails
at runtime the moment it converts a mesh. This is the mirror image of the macOS bempp
gap: there it *works but reports unwired*; here it *reports ok but will break*.

The fix is the documented layout:

```bash
python3 -m venv .venv-bempp
.venv-bempp/bin/pip install bempp-cl gmsh "meshio>=5"
.venv/bin/pip uninstall -y bempp-cl        # get it out of the shared venv
```

then set `bempp_python` in config.toml. (On the reference machine `.venv-bempp` exists but
is an empty stub — no `bin/python` — so it was never actually built.)

## 5. Wheel solvers

```bash
PY=<worker interpreter> bash scripts/install-solvers.sh mbd topology optics
PY=<worker interpreter> bash scripts/install-solvers.sh optics_gpl   # KrakenOS, GPL-3.0
```

Unlike macOS, **pybullet has Linux wheels**, so `mbd` resolves through pybullet rather
than mujoco. Also unlike macOS, `rayoptics` is safe here — there is no bundled-Qt app to
conflict with, which is why the reference machine shows both rayoptics and optiland.

`topopt` still is not needed: the entry accepts `("topopt", "solidspy")` and the
implementation uses solidspy.

## Cross-platform summary

| Family | Linux | macOS | Windows |
|---|---|---|---|
| `warpage` | ✅ apt | ✅ bundled | ✅ bundled |
| `thermal_transient` | ✅ apt | ⚠️ source build ([recipe](SOLVERS_MACOS.md#appendix-building-elmer-on-apple-silicon)) | ✅ portable zip |
| `cfd` | ✅ native | ✅ Multipass VM | ⚠️ WSL2 / SU2 only |
| `mbd` | ✅ pybullet | ✅ mujoco | ✅ pybullet |
| `topology` / `optics` | ✅ | ✅ | ✅ |
| `optics_nonseq` | ✅ | ✅ | ❌ |
| `slicing` | ✅ apt | ✅ brew | ❌ |
| `acoustics_bem` | ⚠️ venv layout | ⚠️ reports unwired | ❌ |
| `dem` / `em_fullwave` / `fsi` | ✅ source build | ❌ Linux-only | ❌ Linux-only |
