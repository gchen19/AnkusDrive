# Container substrate

Run AnkusDrive's Linux-only solvers inside a container instead of installing them on
the host: the OpenFOAM-backed families — CFD, preCICE FSI, injection-molding fill and
the snappyHexMesh meshbridge (issue #361) — and YADE (DEM), Elmer (transient/radiation
thermal, CHT, low-frequency EM, acoustic and harmonic FEM), openEMS (full-wave EM) and
Bempp (acoustic BEM) (issue #419).

The prebuilt solver image, **`ghcr.io/gchen19/ankusdrive-solvers`**, carries ESI
OpenFOAM, the serial preCICE stack with both adapters, OpenFOAM-7 + openInjMoldSim,
YADE, Elmer, openEMS and Bempp.

It is the **slim** image (#422): solvers and the libraries they link, and nothing
else. AnkusDrive, FreeCAD, CalculiX, PrusaSlicer, Blender and SU2 all run on the host
under this substrate, so none of them is in it, and neither is a compiler — every
solver is prebuilt. `ghcr.io/gchen19/ankusdrive-heavy` still exists and still works
here, but it is the **CI** image: it runs the whole test suite inside itself, so it
also carries FreeCAD, the driver venv and every `-dev` package — roughly four times
the download for no extra solver. That
replaces multi-hour source builds with one `pull`. Its component licences are listed
in [`docker/heavy-solvers/LICENSES.md`](../docker/heavy-solvers/LICENSES.md).

It is **public** (no login needed) and **multi-arch** — `linux/amd64` and `linux/arm64`,
each built natively — so on Apple Silicon it runs native, not emulated. `latest` follows
the default branch; every build is also tagged `sha-<commit>`. **Pin a digest or a
`sha-` tag** for anything reproducible: the same suite CI runs pins a digest in
[`heavy-solves.yml`](../.github/workflows/heavy-solves.yml).

```bash
docker pull ghcr.io/gchen19/ankusdrive-solvers:latest        # or :sha-<commit>
docker image inspect --format '{{.Architecture}}' ghcr.io/gchen19/ankusdrive-solvers
```

## How it works

AnkusDrive keeps running on the host. Only the substrate solvers cross into the
container, through the same seam the Multipass VM on macOS and WSL on Windows already
use:

- **Launch**: every OpenFOAM-backed script runs as
  `<engine> exec -w <case dir> <container> bash -c "…"`, and ElmerSolver / ElmerGrid /
  ViewFactors run as `<engine> exec -w <case dir> <container> <binary> …`. Neither gets
  `-i`, so the container cannot read the host process's stdin. The YADE, openEMS and
  Bempp runners exchange JSON over a pipe AnkusDrive itself opens, so only they get
  `-i`; before launch the runner script is copied into the mounted scratch
  (`$TMPDIR/ankusdrive-runners/`), since the installed package is not visible inside.
- **Same-path scratch**: case directories are created under the host's `$TMPDIR`,
  and the container mounts that directory **at the same absolute path**. That makes a
  case dir mean the same thing on both sides. This is the one requirement you must
  get right.
- **Opaque paths**: the host cannot see inside the container, so the solver paths you
  export name locations *inside* it. They are trusted, not checked on the host.
- **Probed, not assumed** (YADE, Elmer, openEMS, Bempp): with no override exported,
  AnkusDrive asks the running container — `command -v` for the binaries, a
  `find_spec` in `/opt/venv-openems` / `/opt/venv-bempp` for the Python solvers. These
  probes are read-only and cached briefly. An image without the solver reports it as
  not resolving, with a hint, instead of "ready".
- **Host installs are ignored**: under this substrate, a host install of OpenFOAM,
  preCICE, YADE, Elmer, openEMS or Bempp is never used, because it isn't what the
  container would run. Everything else (CalculiX, SU2, PrusaSlicer, Blender, the pip
  wheels) still resolves on the host.

## Setup

1. Choose the substrate. Set it in the environment, or as `substrate = "container"`
   under `[solvers]` in the AnkusDrive config file:

   ```bash
   export ANKUSDRIVE_SUBSTRATE=container
   # optional: ANKUSDRIVE_CONTAINER_ENGINE=podman|nerdctl   (default docker)
   # optional: ANKUSDRIVE_CONTAINER=<name>                  (default ankusdrive-solvers)
   ```

2. Create the long-running container, with the scratch mounted at the same path:

   ```bash
   docker run -d --name ankusdrive-solvers \
     --user "$(id -u):$(id -g)" -e HOME=/tmp \
     --tmpfs /tmp:rw,exec,size=2g \
     -v "$TMPDIR:$TMPDIR" \
     ghcr.io/gchen19/ankusdrive-solvers sleep infinity
   ```

   `--user` makes the solvers run as you. Without it, a native Linux engine writes
   every case file as root, and AnkusDrive can't clean up its own scratch.

   `--tmpfs /tmp` gives the container its own writable scratch in memory. Solvers use
   it heavily — OpenMPI's session files (Elmer), openEMS's simulation dirs, numba's
   cache — and on a host whose Docker disk is full, every one of those fails in a way
   that looks like a solver bug rather than a full disk.

   On macOS, Docker Desktop shares `/private` and `/var/folders` (where `$TMPDIR`
   lives) by default. With another engine (OrbStack, colima, podman), make sure that
   path is shared into its VM. `$TMPDIR` on macOS is a per-user
   `/var/folders/...` path — mount **that**, not `/tmp`, or the case dir the host
   creates will not exist inside the container.

3. Export the in-container solver paths. The image publishes them as its own
   environment:

   ```bash
   docker exec ankusdrive-solvers env | grep ANKUSDRIVE_
   ```

   Export at least `ANKUSDRIVE_OPENFOAM_PATH` and `ANKUSDRIVE_OPENFOAM_BASHRC`. For
   FSI, also export `ANKUSDRIVE_CCX_PRECICE`, `ANKUSDRIVE_PRECICE_LIB`,
   `ANKUSDRIVE_OPENFOAM_ADAPTER_LIB` and `ANKUSDRIVE_FSI_OPENFOAM_BASHRC`.

   YADE, Elmer, openEMS and Bempp need no export: they are found by probing the
   container. Export `ANKUSDRIVE_YADE`, `ANKUSDRIVE_ELMER_PATH`,
   `ANKUSDRIVE_OPENEMS_PYTHON` or `ANKUSDRIVE_BEMPP_PYTHON` only if your image puts
   them somewhere else. **Do not** copy the image's `ANKUSDRIVE_FREECADCMD` or
   `ANKUSDRIVE_CALCULIX_PATH`: those still resolve on the host.

4. Check: `ankusdrive doctor` reports `ready via openfoam (in container)`, and the
   same for `dem`, `thermal_transient`, `em_fullwave` and `acoustics_bem`. If
   something is missing, the hint names the container's state (no container, stopped,
   or running with this shell unwired) and the exact fix.

## macOS: what this gives you, and what it doesn't

On macOS the alternative substrate is a Multipass VM ([`MACOS.md`](MACOS.md)), which
you provision and maintain yourself. The container is one `pull` instead, and native
on Apple Silicon. Both cross the same seam, so the families behave identically.

**Covered by the container:** CFD (`cfd_internal_flow_submit`,
`cfd_external_flow_submit`, the meshbridge), preCICE FSI and injection-molding fill (the
OpenFOAM-backed families), plus DEM (YADE), every Elmer family (transient and radiation
thermal, CHT, low-frequency EM and induction heating, acoustic and harmonic FEM),
full-wave EM (openEMS) and acoustic BEM (Bempp) — none of which has a practical macOS
build.

**Not covered, by design:** SU2 and Blender need no container on macOS. SU2 runs
natively (under Rosetta 2) and Blender has a native build; the arm64 image has no
Blender at all. See [`SOLVERS_MACOS.md`](SOLVERS_MACOS.md).

## Limits

- **Not on Windows hosts yet.** A Windows case dir has no same-path meaning inside a
  Linux container. Use the WSL substrate there.
- **Only the solvers listed above** use the container. SU2, CalculiX (`ccx` for
  warpage and core FEM), PrusaSlicer, Blender, KrakenOS and the pip-wheel solvers
  resolve on the host as usual.
- **A host install's paths in your config win, and then fail inside the container.**
  `ANKUSDRIVE_*` overrides — in the environment or in `config.toml` — are taken as
  IN-CONTAINER paths and trusted, because the host cannot see inside. So a
  `config.toml` written for a native install (say `openfoam_bashrc =
  "/usr/lib/openfoam/openfoam2606/etc/bashrc"`) is sourced in a container that has
  openfoam2512, and the solve fails with `blockMesh: command not found` rather than
  anything about paths. Under this substrate, either clear those entries or set them
  to what the image publishes (`docker exec <container> env | grep ANKUSDRIVE_`).
- **Prepared `case_dir`s must live under the mounted scratch.** A case directory you
  pass in yourself is used at its host path, so it only exists inside the container
  if it is under `$TMPDIR`.
- **`blender` is amd64-only in the image.** Blender publishes no arm64 Linux build, so
  the arm64 image records it as an exclusion; use a native Blender on the host.
