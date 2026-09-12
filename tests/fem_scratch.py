"""Per-run scratch directories for the tests that shell out to CalculiX.

The FEM tests each need a *named* working directory to hand `fem_run` (ccx
writes Mesh.inp/.frd/.dat/.sta into it, and the `fem_*_results` calls read them
back out afterwards, so it cannot be torn down mid-test). Those directories used
to be hardcoded as `/tmp/ankusdrive_<name>` and were never removed, so on the
self-hosted runner they accumulated in place: the boxes' systemd-tmpfiles aging
never reclaimed them either, because every suite run rewrote the same paths and
refreshed their mtime. A modal solve alone leaves an 8 MB Mesh.frd behind.

`fem_workdir()` keeps the same readable names but (a) roots them at TMPDIR, so
a CI runner's `TMPDIR` redirect (e.g. `~/.runner-tmp`, off a small root
filesystem) applies, and (b) registers an atexit hook that removes them when the
test process ends -- pass or fail, one directory per run, nothing cumulative.

Set ANKUSDRIVE_KEEP_SCRATCH=1 to keep the directories for post-mortem inspection
of a failing solve.
"""
import atexit
import os
import shutil
import tempfile

_REGISTERED = set()


def _cleanup():
    if os.environ.get("ANKUSDRIVE_KEEP_SCRATCH"):
        print(f"[fem_scratch] ANKUSDRIVE_KEEP_SCRATCH set — keeping "
              f"{len(_REGISTERED)} scratch dir(s): {', '.join(sorted(_REGISTERED))}")
        return
    for path in _REGISTERED:
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup)


def fem_workdir(name):
    """Return (and create) the ccx scratch dir `<TMPDIR>/ankusdrive_<name>`.

    Removed on interpreter exit unless ANKUSDRIVE_KEEP_SCRATCH is set. Safe to
    call repeatedly for the same name -- the directory is reused and registered
    once."""
    path = os.path.join(tempfile.gettempdir(), f"ankusdrive_{name}")
    os.makedirs(path, exist_ok=True)
    _REGISTERED.add(path)
    return path
