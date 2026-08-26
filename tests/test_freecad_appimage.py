"""Extracted-AppImage FreeCAD on Linux: discovery + the installer script (issue #280).

Ubuntu 24.04 dropped FreeCAD from universe, and snap/flatpak are unusable in a
container (no snapd, no FUSE), so the working path there is the official AppImage
unpacked with ``--appimage-extract``. Two halves are pinned here:

  * DISCOVERY — ``client._freecadcmd_candidates`` / ``_resolve_freecadcmd`` must find
    an extracted tree under the conventional prefixes (``/opt/freecad``, a versioned
    ``/opt/freecad-1.1.3``, ``~/.local/opt/freecad``) so the symlink step is optional.
    Exercised against a REAL fake install on disk: the candidate patterns in
    client.py are used verbatim, with every absolute probe re-rooted into a tmpdir.
    Equally load-bearing: the new prefixes must NOT change what already resolved —
    env > PATH > ~/.local/bin all still win.
  * THE SCRIPT — scripts/install-freecad-appimage.sh really extracts, symlinks the
    four binaries, and refuses a checksum mismatch. Driven with a stub AppImage into
    a tmp prefix, so this needs no network, no root and no FreeCAD.

Run:  python3 tests/test_freecad_appimage.py
"""
import glob as _glob_mod
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive import client  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "install-freecad-appimage.sh"

# Captured BEFORE any patching: the shims below re-implement these, and calling the
# patched module attribute from inside a shim would recurse forever.
_real_isfile = os.path.isfile
_real_glob = _glob_mod.glob


class _rooted:
    """Run the real discovery code against a fake filesystem rooted at ``root``.

    Patches ``platform.system`` to Linux, drops the env override, silences PATH, and
    re-roots ``glob.glob``/``os.path.isfile`` so an absolute candidate like
    ``/opt/freecad/...`` is probed at ``<root>/opt/freecad/...``. The candidate
    PATTERNS themselves are never faked — that is the point of the test."""

    def __init__(self, root, which=None):
        self.root = str(root).rstrip("/")
        self.which = which or (lambda name: None)
        self._saved = []
        self._env = None

    def _set(self, obj, attr, value):
        self._saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def __enter__(self):
        self._env = os.environ.pop("ANKUSDRIVE_FREECADCMD", None)

        class _P:
            @staticmethod
            def system():
                return "Linux"

            @staticmethod
            def machine():
                return "x86_64"

        self._set(client, "platform", _P)
        self._set(client.shutil, "which", self.which)
        self._set(client.glob, "glob",
                  lambda pat: [p[len(self.root):] for p in sorted(_real_glob(self.root + pat))])
        self._set(client.os.path, "isfile", lambda p: _real_isfile(self.root + p))
        return self

    def __exit__(self, *exc):
        for obj, attr, val in reversed(self._saved):
            setattr(obj, attr, val)
        if self._env is not None:
            os.environ["ANKUSDRIVE_FREECADCMD"] = self._env
        return False


def _fake_install(root, path):
    """Create an executable file at <root><path> (path is absolute, POSIX)."""
    full = Path(str(root).rstrip("/") + path)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    full.chmod(0o755)
    return path


# --- discovery ----------------------------------------------------------------

def test_opt_freecad_squashfs_root_is_discovered():
    """The conventional extracted-AppImage prefix resolves with no symlink, no PATH
    entry and no env var — the whole point of #280's discovery ask."""
    with tempfile.TemporaryDirectory() as root:
        want = _fake_install(root, "/opt/freecad/squashfs-root/usr/bin/freecadcmd")
        with _rooted(root):
            cands = client._freecadcmd_candidates()
            assert want in cands, cands
            assert cands.count(want) == 1, cands      # literal + glob must dedup
            assert client._resolve_freecadcmd() == want


def test_versioned_prefix_and_newest_wins():
    """A version-suffixed prefix (what a side-by-side upgrade leaves behind) is
    globbed, and the highest version wins."""
    with tempfile.TemporaryDirectory() as root:
        old = _fake_install(root, "/opt/freecad-1.1.0/squashfs-root/usr/bin/freecadcmd")
        new = _fake_install(root, "/opt/freecad-1.1.3/squashfs-root/usr/bin/freecadcmd")
        with _rooted(root):
            cands = client._freecadcmd_candidates()
            assert old in cands and new in cands, cands
            assert client._resolve_freecadcmd() == new


def test_user_prefix_discovered():
    """The no-root fallback prefix the installer uses when /opt isn't writable
    (~/.local/opt/freecad) is discovered too."""
    user_prefix = os.path.expanduser("~/.local/opt/freecad/squashfs-root/usr/bin/freecadcmd")
    assert user_prefix.startswith("/"), user_prefix
    with tempfile.TemporaryDirectory() as root:
        _fake_install(root, user_prefix)
        with _rooted(root):
            assert client._resolve_freecadcmd() == user_prefix


def test_existing_resolution_order_is_not_regressed():
    """The new prefixes are ADDITIVE: a package install, a ~/.local/bin install and
    an /opt AppImage side by side still resolve the way they did before #280 —
    ~/.local/bin (the last, i.e. most-preferred, candidate) wins."""
    local_bin = os.path.expanduser("~/.local/bin/freecadcmd")
    with tempfile.TemporaryDirectory() as root:
        _fake_install(root, "/usr/bin/freecadcmd")
        _fake_install(root, "/opt/freecad/squashfs-root/usr/bin/freecadcmd")
        _fake_install(root, local_bin)
        with _rooted(root):
            assert client._resolve_freecadcmd() == local_bin


def test_env_and_path_still_beat_the_appimage_prefix():
    """Precedence is unchanged: $ANKUSDRIVE_FREECADCMD > PATH > default locations."""
    with tempfile.TemporaryDirectory() as root:
        _fake_install(root, "/opt/freecad/squashfs-root/usr/bin/freecadcmd")
        with _rooted(root, which=lambda name: "/somewhere/on/path/freecadcmd"):
            assert client._resolve_freecadcmd() == "/somewhere/on/path/freecadcmd"
        with _rooted(root):
            os.environ["ANKUSDRIVE_FREECADCMD"] = "/custom/freecadcmd"
            try:
                assert client._resolve_freecadcmd() == "/custom/freecadcmd"
            finally:
                os.environ.pop("ANKUSDRIVE_FREECADCMD", None)


def test_nothing_installed_placeholder_unchanged():
    """With no install anywhere the placeholder is still ~/.local/bin/freecadcmd —
    adding glob patterns that expand to nothing must not change the error path."""
    with tempfile.TemporaryDirectory() as root:
        with _rooted(root):
            assert client._resolve_freecadcmd() == os.path.expanduser("~/.local/bin/freecadcmd")


# --- the installer script -----------------------------------------------------

def test_script_pin_is_a_real_pin():
    """Version + a 64-hex SHA-256 per arch, and a URL derived from the version — an
    unpinned download makes the checksum meaningless."""
    src = SCRIPT.read_text(encoding="utf-8")
    ver = re.search(r'FREECAD_VERSION="\$\{FREECAD_VERSION:-([0-9.]+)\}"', src)
    assert ver, "no pinned FREECAD_VERSION"
    for name in ("SHA256_X86_64", "SHA256_AARCH64"):
        m = re.search(rf'{name}="([0-9a-f]+)"', src)
        assert m and len(m.group(1)) == 64, f"{name} is not a sha256"
    assert "${FREECAD_VERSION}/${ASSET}" in src, "download URL is not built from the pin"
    assert "--appimage-extract" in src, "the FUSE-free extraction is the whole point"
    for b in ("freecadcmd", "freecad", "ccx", "gmsh"):
        assert b in src, f"{b} is not among the symlinked binaries"


def test_script_parses_and_help_works():
    if not shutil.which("bash"):
        print("    (no bash — skipping)")
        return
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    r = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "--appimage-extract" in r.stdout, r.stdout


def test_install_solvers_has_a_freecad_target():
    """`install-solvers.sh freecad` must reach the AppImage script — one documented
    entry point for provisioning, solvers and FreeCAD alike."""
    src = (REPO / "scripts" / "install-solvers.sh").read_text(encoding="utf-8")
    assert re.search(r"freecad\|core\|appimage\)\s*install_freecad", src), "no freecad target"
    assert "install-freecad-appimage.sh" in src, "the freecad target does not delegate"


def _stub_appimage(path):
    """A stand-in AppImage: honors --appimage-extract by writing the four binaries a
    real FreeCAD tree exposes. Lets the extract/symlink logic be exercised with no
    network and no 800 MB download."""
    path.write_text(
        "#!/usr/bin/env bash\n"
        '[ "$1" = "--appimage-extract" ] || exit 2\n'
        "mkdir -p squashfs-root/usr/bin\n"
        "for b in freecadcmd freecad ccx gmsh; do\n"
        '  printf "#!/bin/sh\\necho stub-%s\\n" "$b" > squashfs-root/usr/bin/$b\n'
        '  chmod +x squashfs-root/usr/bin/$b\n'
        "done\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def test_script_extracts_and_symlinks_into_a_temp_prefix():
    """End to end on a stub AppImage: extract into --prefix, symlink all four
    binaries into --bindir, and be idempotent on a re-run. --no-verify because the
    stubs are not FreeCAD; the live verify is exercised by hand against a real
    AppImage (see the commit message for #280)."""
    if sys.platform != "linux" or not shutil.which("bash"):
        print("    (not Linux/bash — skipping)")
        return
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        img = _stub_appimage(td / "fake.AppImage")
        prefix, bindir = td / "opt" / "freecad", td / "bin"
        env = dict(os.environ, FREECAD_SHA256=_sha256(img))
        cmd = ["bash", str(SCRIPT), "--prefix", str(prefix), "--bindir", str(bindir),
               "--appimage", str(img), "--no-verify"]
        for run in ("first", "re-run"):
            r = subprocess.run(cmd, env=env, capture_output=True, text=True)
            assert r.returncode == 0, f"{run}: {r.returncode}\n{r.stdout}\n{r.stderr}"
            for b in ("freecadcmd", "freecad", "ccx", "gmsh"):
                link = bindir / b
                assert link.is_symlink(), f"{run}: {link} is not a symlink"
                assert link.resolve() == (prefix / "squashfs-root/usr/bin" / b).resolve()
        # the scratch download/extract dir must not be left behind next to the prefix
        assert not list(prefix.parent.glob(".ankusdrive-freecad.*")), list(prefix.parent.iterdir())


def test_script_refuses_a_checksum_mismatch():
    """A bad digest aborts before anything is installed — the pin has to bite."""
    if sys.platform != "linux" or not shutil.which("bash"):
        print("    (not Linux/bash — skipping)")
        return
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        img = _stub_appimage(td / "fake.AppImage")
        prefix, bindir = td / "opt" / "freecad", td / "bin"
        env = dict(os.environ, FREECAD_SHA256="0" * 64)
        r = subprocess.run(
            ["bash", str(SCRIPT), "--prefix", str(prefix), "--bindir", str(bindir),
             "--appimage", str(img), "--no-verify"],
            env=env, capture_output=True, text=True)
        assert r.returncode != 0, r.stdout
        assert "SHA-256 mismatch" in r.stderr, r.stderr
        assert not prefix.exists(), "a failed checksum still installed something"


# --- runner -------------------------------------------------------------------

def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    for name, fn in _discover():
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({time.time() - t_suite:.1f}s) ==")


if __name__ == "__main__":
    main()
