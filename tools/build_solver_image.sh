#!/usr/bin/env bash
#
# build_solver_image.sh — build the slim solver image with the solvers you want (#422).
#
# The image can carry any subset. Fewer solvers means fewer prefixes AND fewer apt
# packages (each solver's runtime packages are listed in
# docker/heavy-solvers/runtime-deps/), which is where most of a subset's saving comes
# from. Nothing is compiled: the prefixes are copied from stages that are already
# built, so this takes minutes even though a source build of OpenFOAM-7 takes hours.
#
# USAGE
#   tools/build_solver_image.sh --solvers "openfoam fsi"          # CFD + FSI only
#   tools/build_solver_image.sh --solvers all -t my-solvers:dev   # everything
#   tools/build_solver_image.sh --solvers "yade" --from-published # no local stages
#
#   --solvers "<list>"  space-separated, or `all` (default). Known:
#                         openfoam elmer yade openems fsi oims bempp
#   --from-published    copy the prefixes out of the published heavy image instead of
#                       building the source stages locally (minutes, not hours)
#   -t, --tag <ref>     image tag (default ankusdrive-solvers:custom)
#   --dry-run           print the docker command and exit
#   everything after -- is passed to `docker build`
#
# WHAT YOU GET
#   The image records its own contents at /etc/ankusdrive/solvers.json, which
#   AnkusDrive reads: a solver you left out is reported as excluded-by-design, with
#   this command in the hint, instead of "ready" followed by a failed solve.
set -euo pipefail

cd "$(dirname "$0")/.."

ALL="openfoam elmer yade openems fsi oims bempp"
solvers="all"
tag="ankusdrive-solvers:custom"
solver_src="stages"
dry=0
passthru=()

while [ $# -gt 0 ]; do
  case "$1" in
    --solvers) solvers="${2:?--solvers needs a list}"; shift 2 ;;
    --from-published) solver_src="published"; shift ;;
    -t|--tag) tag="${2:?--tag needs a value}"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    --) shift; passthru+=("$@"); break ;;
    -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option '$1' (see --help)" >&2; exit 2 ;;
  esac
done

# `fsi` implies `openfoam` and so on — one place decides that, shared with the image.
selected=$(bash tools/select_runtime_deps.sh --closure $solvers)
for s in $selected; do
  case " $ALL " in
    *" $s "*) ;;
    *) echo "unknown solver '$s'; known: $ALL" >&2; exit 2 ;;
  esac
done
echo "solvers: $selected"
[ "$selected" != "$solvers" ] && echo "  (expanded from '$solvers' — fsi links libOpenFOAM)"

# Stamp the checkout this was built from, so `ankusdrive doctor` reports "the image
# you built here (commit …), unsigned" instead of warning about an unknown image.
commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
args=(--target slim -t "$tag" -f docker/heavy-solvers/Dockerfile
      --build-arg "SOLVER_SRC=$solver_src"
      --build-arg "BUILT_SOURCE=local"
      --build-arg "BUILT_COMMIT=$commit")
for s in $ALL; do
  on=off
  case " $selected " in *" $s "*) on=on ;; esac
  args+=(--build-arg "WITH_$(echo "$s" | tr '[:lower:]' '[:upper:]')=$on")
done

cmd=(docker build "${args[@]}" ${passthru[@]+"${passthru[@]}"} .)
printf '%q ' "${cmd[@]}"; echo
[ "$dry" = 1 ] && exit 0

"${cmd[@]}"
echo
echo "built $tag — what it contains:"
docker run --rm "$tag" cat /etc/ankusdrive/solvers.json
