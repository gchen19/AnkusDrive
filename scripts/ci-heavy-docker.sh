#!/usr/bin/env bash
#
# ci-heavy-docker.sh — run a command from THIS checkout inside the heavy-solver image
# (#340). The hosted heavy-solves lane calls it for the preflight and for the suite.
#
# The image (docker/heavy-solvers/Dockerfile, #339) carries every solver plus a
# driver venv with every Python dependency, but deliberately NOT ankusdrive itself:
# the code under test is the checkout, mounted at /work and installed editable with
# --no-deps, so a PR tests its own tree against the pinned solver stack.
#
# USAGE
#   HEAVY_IMAGE=ghcr.io/<owner>/ankusdrive-heavy@sha256:... \
#     bash scripts/ci-heavy-docker.sh bash tests/run_all.sh
#
# Also works locally against a built image:
#   HEAVY_IMAGE=ankusdrive-heavy:dev bash scripts/ci-heavy-docker.sh bash scripts/ci-linux-preflight.sh
set -euo pipefail
cd "$(dirname "$0")/.."

: "${HEAVY_IMAGE:?set HEAVY_IMAGE to the solver image (tag or @sha256 digest)}"
[ $# -gt 0 ] || { echo "usage: HEAVY_IMAGE=... $0 <command> [args...]" >&2; exit 2; }

# -t only when attached to a terminal; CI has none. RUN_HEAVY_SOLVES defaults to on:
# this wrapper exists for the heavy lane. GITHUB_ACTIONS passes through so the
# preflight emits ::error:: annotations.
tty_flag=(); [ -t 1 ] && tty_flag=(-t)

# The checkout is owned by the runner user and the container runs as root, so git
# (used by run_all.sh's naming and PII guards) needs /work marked safe.
# --shm-size: OpenFOAM and preCICE use POSIX shared memory; docker's 64 MB default is
# too small for the coupled cases.
exec docker run --rm "${tty_flag[@]}" \
  --shm-size=2g \
  -v "$PWD:/work" -w /work \
  -e RUN_HEAVY_SOLVES="${RUN_HEAVY_SOLVES:-1}" \
  -e GITHUB_ACTIONS="${GITHUB_ACTIONS:-}" \
  "$HEAVY_IMAGE" \
  bash -c 'set -euo pipefail
    git config --global --add safe.directory /work
    pip install -q --no-deps -e /work
    exec "$@"' ankusdrive-heavy "$@"
