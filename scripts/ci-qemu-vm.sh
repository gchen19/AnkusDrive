#!/usr/bin/env bash
#
# ci-qemu-vm.sh — boot a real arm64 Linux VM with the prebuilt solver stack on a hosted
# Apple-Silicon runner, for lane D of #341 (#365).
#
# WHY
#   Hosted macOS runners are M1 VMs without nested virtualization, so Multipass cannot
#   run there. QEMU's pure software emulation (TCG) can: the #365 trial measured the
#   live coupled FSI solve at ~8x native, results identical to 6-7 significant figures,
#   and virtio-9p clean over 10 runs. This script stands that VM up so the UNMODIFIED
#   macOS code path — bash_argv -> `multipass exec` — can drive real solves into it
#   through tests/ci/multipass's SSH/chroot mode.
#
# WHAT IT BUILDS
#   * a checksum-pinned Ubuntu 24.04 arm64 cloud image as the VM's root;
#   * the solver disk (HEAVY_DISK: the arm64 ankusdrive-heavy rootfs as ext4 qcow2,
#     attached copy-on-write) mounted at /mnt/heavy as a chroot, with /proc /sys /dev
#     bind-mounted;
#   * the host scratch ($RUNNER_TEMP/share) shared over virtio-9p at the SAME absolute
#     path, bind-mounted at that path inside the chroot too — what `multipass mount`
#     gives a real Mac;
#   * the guest user at the HOST uid, since 9p (security_model=none) keeps host
#     ownership — the job Multipass's mount uid map does.
#
# EXPORTS (to $GITHUB_ENV, or prints them when run outside Actions)
#   TMPDIR, MULTIPASS_SHIM_MOUNTS, MULTIPASS_SHIM_SSH, MULTIPASS_SHIM_CHROOT, VM_BOOT_S,
#   and the image's own in-VM paths for the SUBSTRATE solvers (read from the image
#   env the disk carries, so they cannot drift from the image).
#
# USAGE   HEAVY_DISK=/path/heavy.qcow2 bash scripts/ci-qemu-vm.sh
set -euo pipefail

: "${HEAVY_DISK:?set HEAVY_DISK to the arm64 solver disk (qcow2)}"
# Pinned cloud image: a floating `current` would change the VM under the lane.
CLOUD_SERIAL="${CLOUD_SERIAL:-20260911}"
CLOUD_SHA256="${CLOUD_SHA256:-7b682958a67ff5de068e36de6af8b75fa645d296af5a70d6500527f6a33781db}"
CLOUD_URL="https://cloud-images.ubuntu.com/noble/$CLOUD_SERIAL/noble-server-cloudimg-arm64.img"
OUT="${GITHUB_ENV:-/dev/stdout}"
TMP_ROOT="${RUNNER_TEMP:-$(mktemp -d)}"

brew list qemu >/dev/null 2>&1 || brew install qemu >/dev/null
W="$TMP_ROOT/vm"; mkdir -p "$W/cidata"; cd "$W"
Q="$(brew --prefix qemu)/share/qemu"

curl -fsSL -o noble.img "$CLOUD_URL"
got=$(shasum -a 256 noble.img | awk '{print $1}')
[ "$got" = "$CLOUD_SHA256" ] || { echo "::error::cloud image checksum mismatch: $got != $CLOUD_SHA256"; exit 1; }
qemu-img resize -q noble.img 12G
cp "$Q/edk2-arm-vars.fd" vars.fd
[ -f key ] || ssh-keygen -q -t ed25519 -N '' -f key
SHARE="$TMP_ROOT/share"; mkdir -p "$SHARE"

printf 'instance-id: ankusdrive-lane-d\nlocal-hostname: openfoam\n' > cidata/meta-data
cat > cidata/user-data <<EOF
#cloud-config
users:
  - name: runner
    uid: "$(id -u)"
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys: ["$(cat key.pub)"]
package_update: false
runcmd:
  - mkdir -p $SHARE /mnt/heavy
  - mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600 hostshare $SHARE
  - mount LABEL=heavy /mnt/heavy
  - mount --bind /proc /mnt/heavy/proc
  - mount --bind /sys /mnt/heavy/sys
  - mount --bind /dev /mnt/heavy/dev
  - mkdir -p /mnt/heavy$SHARE
  - mount --bind $SHARE /mnt/heavy$SHARE
  - chmod 1777 /mnt/heavy/tmp
  - touch /run/lane-d-ready
EOF
hdiutil makehybrid -quiet -iso -joliet -default-volume-name cidata -o seed.iso cidata

t0=$(date +%s)
qemu-system-aarch64 \
  -machine virt -cpu max,pauth-impdef=on -accel tcg,thread=multi \
  -smp "$(sysctl -n hw.ncpu)" -m 4G \
  -drive if=pflash,format=raw,readonly=on,file="$Q/edk2-aarch64-code.fd" \
  -drive if=pflash,format=raw,file=vars.fd \
  -drive if=virtio,format=qcow2,file=noble.img \
  -drive if=virtio,format=raw,readonly=on,file=seed.iso \
  -drive "if=virtio,format=qcow2,file=$HEAVY_DISK,snapshot=on" \
  -netdev user,id=n0,hostfwd=tcp:127.0.0.1:2222-:22 -device virtio-net-pci,netdev=n0 \
  -virtfs local,path="$SHARE",mount_tag=hostshare,security_model=none,id=hs \
  -display none -serial file:console.log -daemonize -pidfile qemu.pid

SSH="ssh -i $W/key -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR runner@127.0.0.1"
for _ in $(seq 1 90); do
  $SSH test -e /run/lane-d-ready 2>/dev/null && break
  sleep 10
done
if ! $SSH test -e /run/lane-d-ready 2>/dev/null; then
  echo "::error::the VM never became ready (boot, 9p or solver-disk mount)"; tail -60 console.log; exit 1
fi
boot=$(( $(date +%s) - t0 ))

# The solver paths come from the IMAGE (docker export drops its ENV, so the disk build
# saved it) — only the substrate solvers', which are what cross the relay.
image_env=$($SSH cat /mnt/heavy/etc/ankusdrive-heavy/image.env)
{
  echo "VM_BOOT_S=$boot"
  echo "TMPDIR=$SHARE"
  echo "MULTIPASS_SHIM_MOUNTS=$SHARE"
  echo "MULTIPASS_SHIM_SSH=$SSH"
  echo "MULTIPASS_SHIM_CHROOT=/mnt/heavy"
  printf '%s\n' "$image_env" \
    | grep -E '^ANKUSDRIVE_(OPENFOAM_PATH|OPENFOAM_BASHRC|FSI_OPENFOAM_BASHRC|CCX_PRECICE|PRECICE_LIB|OPENFOAM_ADAPTER_LIB|OPENINJMOLDSIM|OPENINJMOLDSIM_BASHRC)='
} | tee -a "$OUT"
$SSH 'id; uname -m; nproc; mount | grep -E " type 9p | /mnt/heavy type "' || true
