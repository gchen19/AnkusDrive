#!/usr/bin/env bash
#
# add-openfoam-repo.sh — add ESI's OpenFOAM apt repository, with the key pinned (#423).
#
# WHAT THIS REPLACES
#   `curl -fsSL https://dl.openfoam.com/add-debian-repo.sh | bash`, which is what ESI
#   documents. That runs whatever that URL serves today, as root, and every OpenFOAM
#   package on the machine then trusts whatever key it installed. A compromise of that
#   one file compromises every solver image built afterwards, silently.
#
#   The steps below are exactly what that script does — fetch the public key, dearmor
#   it, write a sources.list entry — except the key is checked against a fingerprint
#   AND a checksum recorded here, and the repository line is written from constants
#   rather than executed from a download. If ESI rotates the key this FAILS, loudly,
#   which is the point: a key change is a thing to notice, not to absorb.
#
#   Pinned from https://dl.openfoam.com/add-debian-repo.sh as of 2026-09-20.
#
# USAGE
#   scripts/add-openfoam-repo.sh              # needs root (or run under sudo)
#   OPENFOAM_SUITE=noble scripts/add-openfoam-repo.sh
#
# Verify the pin yourself:
#   curl -fsSL https://dl.openfoam.com/pubkey.gpg | sha256sum
#   curl -fsSL https://dl.openfoam.com/pubkey.gpg | gpg --show-keys --with-fingerprint
set -euo pipefail

PUBKEY_URL="https://dl.openfoam.com/pubkey.gpg"
# sha256 of the armoured key as served, and the fingerprint of the key inside it.
# Both, because they fail differently: the checksum catches a re-encoded or truncated
# download, the fingerprint catches a genuinely different key.
PUBKEY_SHA256="e6ddd89ed33131a4fc63460cb131ae7416a2a0062b590f4237121598626edf86"
PUBKEY_FPR="DC93C096174122E256DA24063386DD74948D208F"
REPO_URL="https://dl.openfoam.com/repos/deb"
KEYRING="/etc/apt/keyrings/openfoam.gpg"
SOURCES="/etc/apt/sources.list.d/openfoam.list"

die() { echo "error: $*" >&2; exit 1; }

command -v gpg >/dev/null || die "gpg is required (apt-get install -y gnupg)"
command -v curl >/dev/null || die "curl is required"

arch="${OPENFOAM_ARCH:-$(dpkg --print-architecture)}"
suite="${OPENFOAM_SUITE:-}"
if [ -z "$suite" ]; then
  suite="$(sed -ne 's/^UBUNTU_CODENAME=//p' /etc/os-release 2>/dev/null || true)"
  [ -n "$suite" ] || suite="$(sed -ne 's/^VERSION_CODENAME=//p' /etc/os-release 2>/dev/null || true)"
fi
[ -n "$suite" ] || die "cannot determine the distribution codename — set OPENFOAM_SUITE"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fsSL --retry 3 --connect-timeout 30 -o "$tmp/pubkey.gpg" "$PUBKEY_URL" \
  || die "could not fetch $PUBKEY_URL"

got_sha="$(sha256sum "$tmp/pubkey.gpg" | awk '{print $1}')"
[ "$got_sha" = "$PUBKEY_SHA256" ] || die "OpenFOAM pubkey checksum mismatch
  expected $PUBKEY_SHA256
  got      $got_sha
If ESI rotated the key, update PUBKEY_SHA256/PUBKEY_FPR in $0 after checking the
new key is genuinely theirs — do not just overwrite the pin."

got_fpr="$(gpg --show-keys --with-fingerprint --with-colons "$tmp/pubkey.gpg" 2>/dev/null \
            | awk -F: '/^fpr:/{print $10; exit}')"
[ "$got_fpr" = "$PUBKEY_FPR" ] || die "OpenFOAM pubkey fingerprint mismatch
  expected $PUBKEY_FPR
  got      ${got_fpr:-<none>}"

install -d -m 0755 "$(dirname "$KEYRING")"
gpg --dearmor < "$tmp/pubkey.gpg" > "$tmp/openfoam.gpg"
install -m 0644 "$tmp/openfoam.gpg" "$KEYRING"

# signed-by scopes this key to THIS repository — the key ESI ships cannot vouch for
# packages from anywhere else, which /etc/apt/trusted.gpg.d (what their script uses)
# would allow.
printf '# Written by scripts/add-openfoam-repo.sh — key pinned, see that file (#423)\ndeb [arch=%s signed-by=%s] %s %s main\n' \
  "$arch" "$KEYRING" "$REPO_URL" "$suite" > "$SOURCES"

echo "ok: OpenFOAM repo for $suite/$arch, key $PUBKEY_FPR -> $SOURCES"
