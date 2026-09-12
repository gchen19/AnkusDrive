#!/usr/bin/env bash
# Build the AnkusDrive MCPB bundle (issue #201): dist/ankusdrive-<version>.mcpb
#
# The bundle is mcpb/ plus the 512px icon — no AnkusDrive code. mcpb/pyproject.toml
# pins the PyPI release, so a bundle for version X only installs once X is on PyPI;
# publish.yml runs this after the PyPI upload for exactly that reason.
#
# Usage:  scripts/build-mcpb.sh [outdir]     (default: dist)
# Needs:  node/npx (the packer is the pinned @anthropic-ai/mcpb CLI)
set -euo pipefail

MCPB_CLI_VERSION="2.1.2"

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
outdir="${1:-$repo/dist}"
version="$(sed -nE 's/^__version__ = "([^"]+)"$/\1/p' "$repo/ankusdrive/__init__.py")"
[ -n "$version" ] || { echo "error: no __version__ in ankusdrive/__init__.py" >&2; exit 1; }

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
cp -R "$repo/mcpb/." "$stage/"
cp "$repo/logo/icon/ankusdrive-icon-512.png" "$stage/icon.png"

mkdir -p "$outdir"
out="$outdir/ankusdrive-$version.mcpb"
npx --yes "@anthropic-ai/mcpb@$MCPB_CLI_VERSION" validate "$stage/manifest.json"
npx --yes "@anthropic-ai/mcpb@$MCPB_CLI_VERSION" pack "$stage" "$out"
echo "built $out"
