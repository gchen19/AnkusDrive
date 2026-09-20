#!/usr/bin/env bash
#
# select_runtime_deps.sh — the apt packages a chosen set of solvers needs (#422).
#
# The slim image can be built with a subset of solvers. Each one's runtime packages
# live in docker/heavy-solvers/runtime-deps/<solver>.txt (derived by
# tools/container_runtime_deps.sh); this resolves a selection into the flat, sorted,
# de-duplicated list the Dockerfile hands to `apt-get install`.
#
# It also applies the two rules a selection must obey:
#   * CLOSURE — `fsi` links libOpenFOAM, so it implies `openfoam`. Selecting it
#     without one would build an image whose preCICE adapter cannot load.
#   * ARCH — a line's optional second field restricts it to one architecture
#     (libquadmath0 is x86-only; arm64's Elmer is a source build needing BLAS).
#
# USAGE
#   tools/select_runtime_deps.sh <arch> <solver>...        # packages, one per line
#   tools/select_runtime_deps.sh --closure <solver>...     # the solver set, expanded
#   SOLVERS="openfoam fsi yade" tools/select_runtime_deps.sh amd64 $SOLVERS
#
# `all` selects every solver the image can carry.
set -uo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
deps_dir="${RUNTIME_DEPS_DIR:-$here/docker/heavy-solvers/runtime-deps}"

ALL="openfoam elmer yade openems fsi oims bempp"

# <solver>:<implied>… — the closure rules above.
implies() {
  case "$1" in
    fsi) echo "openfoam" ;;
    *) ;;
  esac
}

expand() {   # solver names -> the closed set, sorted, de-duplicated
  local out="" s imp
  for s in "$@"; do
    [ "$s" = "all" ] && { out="$out $ALL"; continue; }
    out="$out $s"
    for imp in $(implies "$s"); do out="$out $imp"; done
  done
  tr ' ' '\n' <<< "$out" | sed '/^$/d' | sort -u
}

if [ "${1:-}" = "--closure" ]; then
  shift
  expand "$@" | tr '\n' ' ' | sed 's/ $//'
  echo
  exit 0
fi

arch="${1:-}"
shift || true
case "$arch" in
  amd64|arm64) ;;
  *) echo "usage: $0 <amd64|arm64> <solver>... (got arch ${arch:-<empty>})" >&2; exit 2 ;;
esac
[ $# -gt 0 ] || { echo "usage: $0 <arch> <solver>...  (or 'all')" >&2; exit 2; }

selected=$(expand "$@")
for s in $selected; do
  case " $ALL " in
    *" $s "*) ;;
    *) echo "unknown solver '$s'; known: $ALL" >&2; exit 2 ;;
  esac
  f="$deps_dir/$s.txt"
  [ -f "$f" ] || { echo "no runtime-deps file for '$s' ($f)" >&2; exit 2; }
  # "<package> [arch]", '#' comments and blank lines ignored
  while read -r pkg pkg_arch _rest; do
    case "$pkg" in ''|'#'*) continue ;; esac
    [ -z "${pkg_arch:-}" ] || [ "$pkg_arch" = "$arch" ] || continue
    echo "$pkg"
  done < "$f"
done | sort -u
