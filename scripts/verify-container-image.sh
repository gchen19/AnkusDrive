#!/usr/bin/env bash
#
# verify-container-image.sh — prove a solver image was built by THIS repository (#423).
#
# WHY THIS EXISTS
#   `docker pull` trusts whatever the registry serves. Pinning a digest is better, but
#   it only proves two people received the same bytes — not who produced them, or from
#   which source. The image workflow signs each published manifest digest through
#   Sigstore with a short-lived GitHub OIDC identity (no key to store or leak), and
#   records the repository, workflow file and commit that built it. This checks that
#   signature, so "the solvers AnkusDrive runs your geometry through" is a statement
#   with evidence behind it rather than a habit.
#
#   It also checks the SBOM attestation, which answers the other half: not "is this
#   authentic" but "what is inside it" — the question a CVE in a bundled solver's
#   dependency actually raises.
#
# USAGE
#   scripts/verify-container-image.sh                         # the slim image, :latest
#   scripts/verify-container-image.sh ghcr.io/gchen19/ankusdrive-heavy:latest
#   scripts/verify-container-image.sh <ref>@sha256:<digest>   # verify an exact digest
#   scripts/verify-container-image.sh --sbom-only <ref>
#
# Requires the GitHub CLI (`gh`, >= 2.49) and a container engine to resolve a tag to a
# digest. `gh auth login` first — the attestations API needs an authenticated call.
#
# EXIT STATUS
#   0  the image is signed by this repository's image workflow
#   1  verification FAILED — do not run this image
#   2  could not verify (missing tool, unauthenticated, no network)
set -uo pipefail

OWNER="${ANKUSDRIVE_IMAGE_OWNER:-gchen19}"
REPO="${ANKUSDRIVE_IMAGE_REPO:-$OWNER/AnkusDrive}"
DEFAULT_IMAGE="ghcr.io/$OWNER/ankusdrive-solvers:latest"
WORKFLOW=".github/workflows/heavy-image.yml"

sbom_only=0
image=""
while [ $# -gt 0 ]; do
  case "$1" in
    --sbom-only) sbom_only=1; shift ;;
    -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option '$1' (see --help)" >&2; exit 2 ;;
    *) image="$1"; shift ;;
  esac
done
image="${image:-$DEFAULT_IMAGE}"

die()  { echo "error: $*" >&2; exit 2; }
fail() { echo "FAILED: $*" >&2; exit 1; }

command -v gh >/dev/null || die "the GitHub CLI (gh) is required: https://cli.github.com"
# `gh attestation` arrived in 2.49; older builds (Ubuntu 24.04 ships 2.45) fail with
# "unknown command", which reads like the image is bad rather than the tool being old.
gh attestation --help >/dev/null 2>&1 \
  || die "this gh ($(gh --version | head -1 | awk '{print $3}')) has no 'attestation' command — needs >= 2.49; see https://cli.github.com"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated — run 'gh auth login'"

# Resolve a tag to the digest the signature is over. A ref that already names a digest
# is used as-is, which is the reproducible way to verify something you have pinned.
case "$image" in
  *@sha256:*) ref="$image" ;;
  *)
    engine="${ANKUSDRIVE_CONTAINER_ENGINE:-docker}"
    command -v "$engine" >/dev/null || die "need $engine to resolve '$image' to a digest (or pass <ref>@sha256:…)"
    echo "resolving $image to a digest…"
    digest=$("$engine" buildx imagetools inspect "$image" --format '{{json .Manifest}}' 2>/dev/null | grep -o '"digest":"sha256:[0-9a-f]*"' | head -1 | cut -d'"' -f4)
    if [ -z "${digest:-}" ]; then
      "$engine" pull -q "$image" >/dev/null 2>&1 || die "cannot pull '$image'"
      digest=$("$engine" image inspect "$image" --format '{{index .RepoDigests 0}}' 2>/dev/null | cut -d@ -f2)
    fi
    [ -n "${digest:-}" ] || die "could not resolve a digest for '$image'"
    # strip the tag, keep the path (a registry port's colon must survive)
    ref="$(printf '%s' "$image" | sed 's/:[^:@/]*$//')@$digest"
    ;;
esac

echo "verifying $ref"
echo "  expecting: built by $REPO via $WORKFLOW"
echo

rc=0
if [ "$sbom_only" = 0 ]; then
  echo "== Provenance =="
  if gh attestation verify "oci://$ref" --repo "$REPO" \
       --signer-workflow "$REPO/$WORKFLOW" 2>&1; then
    echo "  ok: built by $REPO's image workflow"
  else
    rc=1
    echo "  provenance did NOT verify" >&2
  fi
  echo
fi

echo "== SBOM =="
# CycloneDX: an SPDX SBOM of these images runs past GitHub's 16 MiB predicate cap,
# mostly on per-file entries nobody reads (see heavy-image.yml).
if gh attestation verify "oci://$ref" --repo "$REPO" \
     --predicate-type https://cyclonedx.org/bom 2>&1; then
  echo "  ok: an SBOM attestation is present and signed"
else
  # Absent for an image published before SBOM attestation landed, and for one whose
  # SBOM exceeded the cap (the workflow keeps that as a build artifact instead).
  echo "  note: no SBOM attestation for this digest — provenance above is unaffected"
fi

echo
if [ "$rc" != 0 ]; then
  fail "$ref is NOT verifiably from $REPO — do not run it"
fi
echo "VERIFIED: $ref"
echo "Pin this digest to keep what you verified:"
echo "  $ref"
