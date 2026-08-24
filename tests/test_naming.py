"""Naming contract (issue #295) — the old project name must stay gone.

The project was renamed DriftPin -> AnkusDrive. A rename is not a one-time sweep:
it is a property the tree has to keep. Every later merge of a branch authored
before the rename, every doc copy-pasted from an old doc, and every new
``ANKUSDRIVE_``-lookalike env var is an opportunity to put "driftpin" back, and
nothing about that fails — it just quietly reintroduces a second name for one
thing. Grepping by hand is what let it rot in the first place, so the grep lives
here and runs in the fast lane.

Two assertions, plus one that guards the guard:

  * **no content** — no tracked file contains the old name (case-insensitive),
    outside the explicit allowlist below.
  * **no paths** — no tracked path contains the old name, same allowlist.
  * **the allowlist is honest** — every entry must still exist AND still contain
    the old name. An entry whose reason has expired fails here rather than
    silently widening the exemption forever.

Like ``test_contracts.py`` this imports NEITHER the package nor any third-party
module: it shells out to ``git ls-files`` and reads bytes. Pure stdlib, seconds,
no FreeCAD.

Run:  python3 tests/test_naming.py
"""
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OLD = re.compile(rb"driftpin", re.I)

# --- the deliberate survivors ----------------------------------------------
# Each entry is a tracked path that is ALLOWED to contain the old name, with the
# reason it survives. Anything not listed here is a regression. Keep this list
# short and keep every reason falsifiable — `test_allowlist_is_honest` deletes
# the temptation to leave a stale entry lying around.
ALLOWED = {
    # Compatibility shims. Each one exists to keep a pre-rename install working
    # and names the old spelling on purpose; all are marked "Deprecated: 0.6".
    "ankusdrive/__init__.py":       "promotes legacy DRIFTPIN_* env vars",
    "ankusdrive/config.py":         "legacy env prefix + pre-rename config path",
    "ankusdrive/solvers.py":        "probes the pre-rename provisioned-solver dir",
    "ankusdrive/doctor.py":         "reports what still resolves through a shim",
    "ankusdrive/_legacy_cli.py":    "the deprecated `driftpin` console script",
    # NOTE: ankusdrive/props.py carries the pre-rename DP_* property fallback but
    # is NOT listed — "DP_" is not the old name, so it passes the sweep unaided.
    "pyproject.toml":               "declares the deprecated `driftpin` script",
    "tests/test_naming.py":         "this file — it has to name what it forbids",
    "tests/test_compat_rename.py":  "asserts the compatibility shims still work",
    "MIGRATION.md":                 "tells users what changed and how to move",
    "README.md":                    "one line: the wordmark is pending redesign",
    # Brand artwork. The DriftPin mark is a drift pin threading a reticle — the
    # picture IS the old name, so it needs a redesign, not a re-export. Tracked
    # under its original filenames until that lands (#295 follow-up).
    "logo/":                        "name-derived artwork pending redesign",
}


def _tracked():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True,
                         capture_output=True).stdout
    return [p.decode() for p in out.split(b"\0") if p]


def _allowed(path):
    return any(path == k or path.startswith(k) for k in ALLOWED)


def test_no_old_name_in_content():
    """No tracked file's CONTENT says the old name, outside the allowlist."""
    offenders = []
    for rel in _tracked():
        if _allowed(rel):
            continue
        f = REPO / rel
        try:
            blob = f.read_bytes()
        except OSError:
            continue
        if OLD.search(blob):
            hits = len(OLD.findall(blob))
            offenders.append(f"{rel} ({hits} hit{'s' if hits > 1 else ''})")
    assert not offenders, (
        "the pre-rename name is back in %d file(s) — rename them to AnkusDrive, "
        "or add an allowlist entry in tests/test_naming.py with the reason:\n  %s"
        % (len(offenders), "\n  ".join(sorted(offenders))))


def test_no_old_name_in_paths():
    """No tracked PATH contains the old name, outside the allowlist."""
    offenders = [p for p in _tracked() if OLD.search(p.encode()) and not _allowed(p)]
    assert not offenders, (
        "%d tracked path(s) still carry the pre-rename name:\n  %s"
        % (len(offenders), "\n  ".join(sorted(offenders))))


def test_allowlist_is_honest():
    """Every allowlist entry still exists and still contains the old name.

    Without this, an exemption outlives its reason: the shim gets deleted in 0.6,
    the entry stays, and the next file at that path is exempt for free."""
    stale = []
    for entry, reason in sorted(ALLOWED.items()):
        target = REPO / entry
        if not target.exists():
            stale.append(f"{entry} — gone from the tree ({reason})")
            continue
        files = ([p for p in target.rglob("*") if p.is_file()]
                 if target.is_dir() else [target])
        if not any(OLD.search(p.name.encode()) or
                   (p.is_file() and OLD.search(p.read_bytes()))
                   for p in files):
            stale.append(f"{entry} — no longer contains the old name ({reason})")
    assert not stale, (
        "stale allowlist entries in tests/test_naming.py — delete them:\n  %s"
        % "\n  ".join(stale))


def test_the_sweep_actually_swept():
    """The guard guards something.

    A broken glob, a bad cwd, or a `git ls-files` that returns nothing would make
    every assertion above pass vacuously — a green test proving nothing. Assert
    the sweep saw a plausible tree and that it can still find the name where the
    name is known to be."""
    tracked = _tracked()
    assert len(tracked) > 200, f"only {len(tracked)} tracked files — sweep is broken"
    assert any(p.startswith("ankusdrive/") for p in tracked), "package tree not seen"
    assert OLD.search((REPO / "ankusdrive" / "config.py").read_bytes()), \
        "the matcher cannot find the old name where it is known to be"


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    tests = _discover()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:44s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:44s} ({time.time() - t0:.2f}s)")
    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
