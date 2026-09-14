#!/usr/bin/env bash
#
# ci-vmdisk.sh — export the arm64 half of the pinned ankusdrive-heavy image to the ext4
# qcow2 that lane D's VM mounts as its solver root (#365; cached per digest, #385).
#
# WHY A SCRIPT
#   The disk is a pure function of (image digest, this recipe). heavy-solves.yml caches
#   it under a key built from HEAVY_IMAGE and this file's hash, so a run with an
#   unchanged pin skips the ~10 min export. Keeping the recipe here, not inline in the
#   workflow, means an unrelated workflow edit does not invalidate a 4.6 GiB cache entry,
#   and a recipe edit always does.
#
# `docker export` drops the image's ENV, so it is saved into the disk
# (/etc/ankusdrive-heavy/image.env), where scripts/ci-qemu-vm.sh reads it back.
#
# Runs on an arm64 Linux runner with docker, logged in to ghcr.io.
# USAGE   HEAVY_IMAGE=ghcr.io/...@sha256:... bash scripts/ci-vmdisk.sh <out.qcow2>
set -euo pipefail

: "${HEAVY_IMAGE:?set HEAVY_IMAGE to the pinned ankusdrive-heavy image}"
out="${1:?usage: ci-vmdisk.sh <out.qcow2>}"
work="$(dirname "$out")"
mkdir -p "$work"

sudo apt-get update -qq && sudo apt-get install -y -qq qemu-utils >/dev/null
docker pull --platform linux/arm64 "$HEAVY_IMAGE"
cid=$(docker create --platform linux/arm64 "$HEAVY_IMAGE")
# Staged under /mnt, where the lane has always staged it (not the checkout's disk).
root=/mnt/root
sudo rm -rf "$root"; sudo mkdir -p "$root"
docker export "$cid" | sudo tar -x -C "$root" --numeric-owner
sudo mkdir -p "$root/etc/ankusdrive-heavy"
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$cid" \
  | sudo tee "$root/etc/ankusdrive-heavy/image.env" >/dev/null
docker rm "$cid" >/dev/null; docker image rm "$HEAVY_IMAGE" >/dev/null || true

used=$(sudo du -sm "$root" | cut -f1); size=$(( used * 12 / 10 + 2048 ))
echo "rootfs ${used} MiB -> disk ${size} MiB"
raw="$work/heavy.raw"
sudo truncate -s "${size}M" "$raw"
sudo mkfs.ext4 -q -L heavy -d "$root" "$raw"
sudo rm -rf "$root"
sudo qemu-img convert -c -O qcow2 "$raw" "$out"
sudo rm -f "$raw"
sudo chown "$(id -u)" "$out"; ls -lh "$out"
