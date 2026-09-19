#!/usr/bin/env bash
#
# container_runtime_deps.sh — derive the RUNTIME apt packages a solver prefix needs,
# by asking the binaries rather than by reading the build recipe (#422).
#
# WHY THIS EXISTS
#   The heavy image (docker/heavy-solvers/Dockerfile) installs the packages each
#   solver needs to BUILD: -dev headers, the toolchain, cmake. The slim solver image
#   runs the same binaries without ever compiling, so it wants the much smaller set
#   they LINK against. Hand-copying that list from the build recipe is how a slim
#   image ends up missing one .so and failing inside a user's solve instead of here.
#
#   So: ldd every ELF in the prefix, resolve each DT_NEEDED to a file, map the file
#   to its apt package with dpkg -S, and print the package set. A library that does
#   not resolve is printed as MISSING — in the heavy image that means the build left
#   a dangling link; in the slim image it means the package list is short.
#
# USAGE (inside an image that HAS the solvers — the heavy image):
#   docker run --rm ghcr.io/gchen19/ankusdrive-heavy \
#     bash -s -- /opt/yade < tools/container_runtime_deps.sh
#   bash tools/container_runtime_deps.sh /opt/yade /opt/openEMS        # all at once
#   bash tools/container_runtime_deps.sh --check /opt/yade /opt/fsi    # gate, no output
#   EXTRA_LIB_DIRS=/usr/lib/openfoam/openfoam2512/platforms/current/lib \
#     bash tools/container_runtime_deps.sh --check /opt/fsi              # cross-prefix
#
# --check turns it into an ASSERTION: exit 1 naming every unresolved library instead
# of printing the package list. That is what the slim image runs after copying the
# solver prefixes in, so a short package list fails the BUILD, per architecture,
# rather than a user's solve. It is the reason the slim package set can be trimmed
# with confidence on an arch whose Elmer/openEMS are source-built.
#
# A solver's OWN libraries are found the way a solve finds them — every directory
# holding a .so under the prefixes is put on LD_LIBRARY_PATH, which is what sourcing
# OpenFOAM's etc/bashrc does. Without that, ldd calls libOpenFOAM.so "not found" and
# the check fails on a perfectly good image. EXTRA_LIB_DIRS adds prefixes a solver
# links ACROSS (the preCICE OpenFOAM adapter against the ESI install).
#
# Output: one package per line on stdout (sortable, diffable), MISSING lines and the
# per-prefix summary on stderr, so `$(…)` captures exactly the package list.
set -uo pipefail

CHECK=0
[ "${1:-}" = "--check" ] && { CHECK=1; shift; }
[ $# -gt 0 ] || { echo "usage: $0 [--check] <prefix> [prefix...]" >&2; exit 2; }
command -v ldd >/dev/null || { echo "ldd not found" >&2; exit 2; }

pkgs=$(mktemp) ; missing=$(mktemp)
trap 'rm -f "$pkgs" "$missing"' EXIT

# every dir under the prefixes that holds a shared object, plus EXTRA_LIB_DIRS
own_libs=""
for prefix in "$@"; do
  [ -d "$prefix" ] || continue
  d=$(find "$prefix" -name '*.so' -o -name '*.so.*' 2>/dev/null \
        | sed 's|/[^/]*$||' | sort -u | tr '\n' ':')
  own_libs="${own_libs}${d}"
done
export LD_LIBRARY_PATH="${own_libs}${EXTRA_LIB_DIRS:-}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

for prefix in "$@"; do
  [ -d "$prefix" ] || { echo "skip: $prefix does not exist" >&2; continue; }
  n_elf=0
  # Every executable and shared object under the prefix. `file` is not installed in
  # every base, so select on the ELF magic directly.
  while IFS= read -r -d '' f; do
    [ "$(head -c 4 "$f" 2>/dev/null)" = $'\x7fELF' ] || continue
    n_elf=$((n_elf + 1))
    while read -r name arrow path _rest; do
      # ldd also prints "statically linked", "not a dynamic executable" and the vdso;
      # only an explicit "=> not found" is a real unmet dependency.
      case "$name" in linux-vdso.so*|ld-linux*|statically|not) continue ;; esac
      if [ "$arrow" = "=>" ] && [ "$path" = "not" ]; then
        echo "$name  <- $f" >> "$missing"
      elif [ "$arrow" = "=>" ] && [ -n "${path:-}" ]; then
        # a dependency INSIDE a solver prefix is shipped with it, not a package
        case "$path" in /opt/*) continue ;; esac
        echo "$path" >> "$pkgs"
      fi
    done < <(ldd "$f" 2>/dev/null | sed 's/^[[:space:]]*//')
  done < <(find "$prefix" -type f \( -perm -u+x -o -name '*.so' -o -name '*.so.*' \) -print0 2>/dev/null)
  echo "== $prefix: $n_elf ELF files" >&2
done

if [ "$CHECK" = 1 ]; then
  if [ -s "$missing" ]; then
    echo "unresolved libraries — this image is missing their packages:" >&2
    sort -u "$missing" >&2
    exit 1
  fi
  echo "ok: every DT_NEEDED resolves under: $*" >&2
  exit 0
fi

# Map each resolved library to the package owning it. dpkg -S takes the realpath;
# a symlink (libfoo.so.1 -> libfoo.so.1.2) is owned by the same package either way.
sort -u "$pkgs" | while read -r lib; do
  real=$(readlink -f "$lib" 2>/dev/null || echo "$lib")
  dpkg -S "$real" 2>/dev/null | head -1 | cut -d: -f1 \
    || echo "UNOWNED $real" >&2
done | sort -u

if [ -s "$missing" ]; then
  echo "== MISSING (unresolved DT_NEEDED — the package list is short, or the build left a dangling link):" >&2
  sort -u "$missing" >&2
fi
