# Container substrate

Run AnkusDrive's OpenFOAM-backed families inside a container instead of installing
the solvers on the host: CFD, preCICE FSI, injection-molding fill, and the snappyHexMesh
meshbridge (issue #361).

The prebuilt solver image, `ghcr.io/gchen19/ankusdrive-heavy`, carries ESI OpenFOAM,
the serial preCICE stack with both adapters, and OpenFOAM-7 + openInjMoldSim. That
replaces multi-hour source builds with one `pull`. Its component licences are listed
in [`docker/heavy-solvers/LICENSES.md`](../docker/heavy-solvers/LICENSES.md).

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
   docker run -d --name ankusdrive-solvers -v "$TMPDIR:$TMPDIR" \
     ghcr.io/gchen19/ankusdrive-heavy sleep infinity
   ```

   On macOS, Docker Desktop shares `/private` and `/var/folders` (where `$TMPDIR`
   lives) by default. With another engine, make sure that path is shared into its VM.

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

## Limits

- **Not on Windows hosts yet.** A Windows case dir has no same-path meaning inside a
  Linux container. Use the WSL substrate there.
- **Architecture.** The image is currently `linux/amd64`. On Apple Silicon it runs
  under emulation until the multi-arch image lands (#363).
- **Only the OpenFOAM-backed families** use the container. SU2, Elmer, YADE, openEMS,
  Bempp and the pip-wheel solvers resolve on the host as usual.
