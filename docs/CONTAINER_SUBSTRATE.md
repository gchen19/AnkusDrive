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
each built natively — so on Apple Silicon it runs native, not emulated. Two solver sets
are published, each signed:

| tag | carries | for |
|---|---|---|
| `:latest` = `:full` (and `:sha-<commit>`) | every solver below | the default |
| `:openfoam` (and `:openfoam-sha-<commit>`) | ESI OpenFOAM only — CFD and the snappyHexMesh meshbridge | a CFD-only machine; roughly half the download |

`latest`/`full`/`openfoam` follow the default branch. **Pin a digest or a
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
     --network none --cap-drop ALL --security-opt no-new-privileges \
     --read-only --tmpfs /tmp:rw,exec,size=2g \
     -v "$TMPDIR:$TMPDIR" \
     ghcr.io/gchen19/ankusdrive-solvers sleep infinity
   ```

   `--user` makes the solvers run as you. Without it, a native Linux engine writes
   every case file as root, and AnkusDrive can't clean up its own scratch.

   `--tmpfs /tmp` gives the container its own writable scratch in memory. Solvers use
   it heavily — OpenMPI's session files (Elmer), openEMS's simulation dirs, numba's
   cache — and on a host whose Docker disk is full, every one of those fails in a way
   that looks like a solver bug rather than a full disk.

   The rest is least privilege, and each flag is there because the solvers genuinely
   do not need what it removes — verified by running the live solver suites with all
   of them on. `--network none` is the notable one: nothing in a mesh is a reason to
   reach the internet. They are not a sandbox for hostile code (a bind-mounted scratch
   is still a hole), but they are the difference between a solver bug being contained
   and being a foothold. `ankusdrive doctor` says so when a container has more
   privilege than the solvers need.

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

## Checking the image is ours

`docker pull` trusts whatever the registry serves, and a digest pin only proves that
two people got the same bytes — not who built them. Every published manifest is signed
through Sigstore with a short-lived GitHub OIDC identity (no key to store or leak) and
carries provenance naming the repository, workflow and commit that produced it, plus
an SBOM of what is inside:

```bash
scripts/verify-container-image.sh                          # the slim image, :latest
scripts/verify-container-image.sh ghcr.io/gchen19/ankusdrive-solvers@sha256:<digest>
```

It needs the GitHub CLI (`gh` ≥ 2.49, authenticated). Under the hood it is:

```bash
gh attestation verify oci://ghcr.io/gchen19/ankusdrive-solvers@sha256:<digest> \
  --repo gchen19/AnkusDrive \
  --signer-workflow gchen19/AnkusDrive/.github/workflows/heavy-image.yml
```

Verify a **digest**, then run that digest — verifying `:latest` and then pulling
`:latest` later is two different images. `ankusdrive doctor` prints the digest the
running container was created from, and the exact command to check it.

Images published before this landed have no attestation; the script says so rather
than failing, since "nothing to check" and "check failed" are different answers.

### Your own image is not a failure

There are three outcomes, and only one is alarming:

| | meaning | what to do |
|---|---|---|
| **verified** | provenance names this repository's image workflow | nothing |
| **unsigned** | no attestation at all: an image you built, one published before signing, or no network/`gh` | expected for a custom image — silence it below |
| **mismatch** | an attestation exists but names a **different** repository | do not run it; this is an image claiming to be ours |

An image built by `tools/build_solver_image.sh` records the checkout it came from, so
it is reported as *"unsigned — built here, commit abc1234"* rather than as an unknown
image. (That record is the image describing itself; it is used to phrase the message
and is never evidence. Only the signature is.)

To accept an unsigned image for good — the normal case when you build your own:

```bash
export ANKUSDRIVE_ALLOW_UNVERIFIED_IMAGE=1     # or allow_unverified_image = true in config.toml
```

That silences **unsigned** only. A **mismatch** still warns, because the override is
for images you built, not for one impersonating this repository. Nothing here blocks a
solve either way: a check that stranded you offline, or on your own image, would cost
more than it protects.

`ankusdrive doctor --verify-image` runs the check, and the MCP tool
`setup_status(verify_image=True)` returns the same verdict for an agent that is asked
whether the solvers are authentic. Both are opt-in: it is the one call here that
reaches the network.

## Building an image with only the solvers you want

`:full` carries every solver and `:openfoam` only OpenFOAM. For any other set, build
your own — nothing is compiled, the prebuilt solver trees are copied, so it takes
minutes:

```bash
tools/build_solver_image.sh --solvers "openfoam fsi" -t my-solvers:dev
tools/build_solver_image.sh --solvers all --from-published   # no local stage builds
```

Known solvers: `openfoam elmer yade openems fsi oims bempp` (`oims` is
openInjMoldSim, the molding solver on OpenFOAM-7). `fsi` implies `openfoam` — its
adapter links libOpenFOAM — and the script expands that for you.

A subset is smaller in two ways: it copies fewer solver trees, and it installs fewer
apt packages, since each solver's runtime packages are listed separately in
[`docker/heavy-solvers/runtime-deps/`](../docker/heavy-solvers/runtime-deps/) (derived
by `tools/container_runtime_deps.sh`, which ldd's every binary rather than trusting a
hand-written list). Measured on amd64: **0.69 GB** for OpenFOAM alone and **1.6 GB**
for YADE + Elmer, against 3.3 GB for the full set.

Then point the substrate at it:

```bash
docker run -d --name ankusdrive-solvers --user "$(id -u):$(id -g)" -e HOME=/tmp \
  --tmpfs /tmp:rw,exec,size=2g -v "$TMPDIR:$TMPDIR" my-solvers:dev sleep infinity
```

### The image says what it contains

Every image records its own contents at `/etc/ankusdrive/solvers.json`, and AnkusDrive
reads that before trusting any in-container path. So a solver your image left out is
reported as excluded **by design**, with the command to rebuild:

```
[warn] fsi              unwired (precice)
        fix:   this image does not include fsi (not selected when this image was built).
               Rebuild with it: `bash tools/build_solver_image.sh --solvers "<yours> fsi"`…
```

That matters because the host cannot see inside the container: without the manifest,
a partial image would report a missing solver as `ready via … (in container)` and the
solve would fail minutes later. An image built before this existed has no manifest,
and AnkusDrive falls back to probing it, exactly as before.

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
- **A host install's paths in your config are caught, not trusted — while the
  container runs.** `ANKUSDRIVE_*` overrides, in the environment or in `config.toml`,
  name IN-CONTAINER paths. A `config.toml` written for a native install (say
  `openfoam_bashrc = "/usr/lib/openfoam/openfoam2606/etc/bashrc"`) is absolute too, so
  it used to be trusted and then sourced in a container that has openfoam2512 — the
  solve failing with `blockMesh: command not found`, naming nothing useful. AnkusDrive
  now checks such a path inside the running container and, when it is not there, says
  so instead. While the container is stopped there is nothing to ask, so the override
  is still trusted; either clear those entries or set them to what the image publishes
  (`docker exec <container> env | grep ANKUSDRIVE_`).
- **Prepared `case_dir`s must live under the mounted scratch.** A case directory you
  pass in yourself is used at its host path, so it only exists inside the container
  if it is under `$TMPDIR`.
- **`blender` is amd64-only in the image.** Blender publishes no arm64 Linux build, so
  the arm64 image records it as an exclusion; use a native Blender on the host.
