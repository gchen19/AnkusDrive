# Container substrate

Run AnkusDrive's OpenFOAM-backed families inside a container instead of installing
the solvers on the host: CFD, preCICE FSI, injection-molding fill, and the snappyHexMesh
meshbridge (issue #361).

The prebuilt solver image, `ghcr.io/gchen19/ankusdrive-heavy`, carries ESI OpenFOAM,
the serial preCICE stack with both adapters, and OpenFOAM-7 + openInjMoldSim. That
replaces multi-hour source builds with one `pull`. Its component licences are listed
in [`docker/heavy-solvers/LICENSES.md`](../docker/heavy-solvers/LICENSES.md).

It is **public** (no login needed) and **multi-arch** — `linux/amd64` and `linux/arm64`,
each built natively — so on Apple Silicon it runs native, not emulated. `latest` follows
the default branch; every build is also tagged `sha-<commit>`. **Pin a digest or a
`sha-` tag** for anything reproducible: the same suite CI runs pins a digest in
[`heavy-solves.yml`](../.github/workflows/heavy-solves.yml).

```bash
docker pull ghcr.io/gchen19/ankusdrive-heavy:latest          # or :sha-<commit>
docker image inspect --format '{{.Architecture}}' ghcr.io/gchen19/ankusdrive-heavy
```

## How it works

AnkusDrive keeps running on the host. Only the substrate solvers cross into the
container, through the same seam the Multipass VM on macOS and WSL on Windows already
use:

- **Launch**: every OpenFOAM-backed script runs as
  `<engine> exec -w <case dir> <container> bash -c "…"`. It never gets `-i`, so the
  container cannot read the host process's stdin.
- **Same-path scratch**: case directories are created under the host's `$TMPDIR`,
  and the container mounts that directory **at the same absolute path**. That makes a
  case dir mean the same thing on both sides. This is the one requirement you must
  get right.
- **Opaque paths**: the host cannot see inside the container, so the solver paths you
  export name locations *inside* it. They are trusted, not checked on the host.
- **Host installs are ignored**: under this substrate, an OpenFOAM or preCICE install
  on the host is never used for these families, because it isn't what the container
  would run.

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
     -v "$TMPDIR:$TMPDIR" \
     ghcr.io/gchen19/ankusdrive-heavy sleep infinity
   ```

   `--user` makes the solvers run as you. Without it, a native Linux engine writes
   every case file as root, and AnkusDrive can't clean up its own scratch.

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

4. Check: `ankusdrive doctor` reports `ready via openfoam (in container)`. If
   something is missing, the hint names the container's state (no container, stopped,
   or running with this shell unwired) and the exact fix.

## macOS: what this gives you, and what it doesn't

On macOS the alternative substrate is a Multipass VM ([`MACOS.md`](MACOS.md)), which
you provision and maintain yourself. The container is one `pull` instead, and native
on Apple Silicon. Both cross the same seam, so the families behave identically.

**Covered by the container:** CFD (`cfd_internal_flow_submit`,
`cfd_external_flow_submit`, the meshbridge), preCICE FSI, and injection-molding fill —
the OpenFOAM-backed families, which are the ones with no macOS build path at all.

**Not covered:** YADE, openEMS, Bempp and Elmer are *in* the image but are resolved on
the **host**, so on a Mac they stay absent and their families degrade cleanly. Routing
them through the container too is [#419](https://github.com/gchen19/AnkusDrive/issues/419).
SU2 and Blender need no container on macOS: SU2 runs natively (under Rosetta 2) and
Blender has a native build — see [`SOLVERS_MACOS.md`](SOLVERS_MACOS.md).

## Limits

- **Not on Windows hosts yet.** A Windows case dir has no same-path meaning inside a
  Linux container. Use the WSL substrate there.
- **Only the OpenFOAM-backed families** use the container (#419). SU2, Elmer, YADE,
  openEMS, Bempp and the pip-wheel solvers resolve on the host as usual.
- **`blender` is amd64-only in the image.** Blender publishes no arm64 Linux build, so
  the arm64 image records it as an exclusion; use a native Blender on the host.
