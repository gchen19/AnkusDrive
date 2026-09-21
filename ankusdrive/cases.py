"""Solver case directories: one root, a stated retention, no unbounded growth (#437).

Every built-in solve writes its deck — a ``.sif`` plus mesh, an OpenFOAM case tree, a
``.inp``, a sliced ``.gcode`` — into a fresh directory and reports that directory in
its result. Two facts about those directories are in tension, and the old behaviour
(``tempfile.mkdtemp`` per handler, no cleanup anywhere) resolved it by doing neither
thing well:

* **They must outlive the job.** The result names the directory, a caller inspects it,
  and a second tool is *handed* it — ``molding_warpage_submit(cooling_case_dir=…)``
  consumes the case a ``molding_fill_submit`` wrote (#116). Deleting on completion
  would make the result's ``case_dir`` a lie.
* **They must not accumulate.** 31 ``mkdtemp`` calls against 2 ``rmtree`` meant every
  solve a session ever ran stayed on disk until the OS felt like reclaiming it —
  measured at 23 directories and 1.3 GB on one developer machine.

So: keep them, in one place, up to a stated bound, and reap oldest-first. Retention is
a decision this module makes and states, not something left to ``systemd-tmpfiles``.

**The root** is ``<tempdir>/ankusdrive-cases`` (``ANKUSDRIVE_CASE_ROOT`` to move it).
One root means reaping only ever looks at directories AnkusDrive made: a case
directory the *caller* supplied is never touched, wherever it lives.

**The bound**, checked on each :func:`new`:

* at most :data:`KEEP` directories (``ANKUSDRIVE_CASE_KEEP``, default 64),
* at most :data:`MAX_GB` gigabytes in total (``ANKUSDRIVE_CASE_MAX_GB``, default 4),
* but nothing younger than :data:`GRACE_S` (``ANKUSDRIVE_CASE_GRACE_S``, default 3600)
  is ever removed, whichever bound it breaks.

The grace window is what makes this safe without locks. A directory's mtime tracks the
solve writing into it, so "old" means "nothing has touched it for an hour" — and a
running solve cannot be reaped by *another* worker sharing the root, which is the
failure this would otherwise invite. The caps are therefore soft: a burst inside one
hour is kept in full, and the next :func:`new` after it ages out brings the root back
under the bound.

``ANKUSDRIVE_KEEP_SCRATCH=1`` (the same switch the FEM gate and the test suite use)
turns reaping off entirely, for when a case directory is the thing being debugged.

Pure stdlib and FreeCAD-free: the worker, the out-of-process GPL runners and the tests
all import it.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time

KEEP = 64
MAX_GB = 4.0
GRACE_S = 3600.0
ROOT_NAME = "ankusdrive-cases"

# A name this module made: <kind>-<mkdtemp token>. Reaping matches on it, so a
# directory somebody else put in the root is left alone rather than deleted.
_OURS = re.compile(r"^[a-z0-9][a-z0-9_]*-[A-Za-z0-9_]{6,}$")
_KIND_OK = re.compile(r"[^a-z0-9_]+")
_WALK_CAP = 20_000                      # entries; past this a directory is "big enough"

_lock = threading.Lock()
_sizes: dict = {}                       # (path, mtime) -> bytes


def _cfg(env_var: str, default):
    """``ANKUSDRIVE_*`` env, then config.toml, then the default — the house
    precedence. Falls back to the default for anything unparseable rather than
    failing a solve over a malformed setting."""
    try:
        from ankusdrive import config
        raw = config.get(env_var)
    except Exception:
        raw = os.environ.get(env_var)
    if raw is None or str(raw).strip() == "":
        return default
    if isinstance(default, str):
        return str(raw)
    try:
        return type(default)(str(raw).strip())
    except (TypeError, ValueError):
        return default


def reaping_disabled() -> bool:
    return bool(os.environ.get("ANKUSDRIVE_KEEP_SCRATCH"))


def root(create: bool = True) -> str:
    """The directory every generated case lives under. Created on demand, 0700."""
    path = _cfg("ANKUSDRIVE_CASE_ROOT", "") or os.path.join(tempfile.gettempdir(), ROOT_NAME)
    if create:
        os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def new(kind: str) -> str:
    """Create and return a fresh case directory for ``kind`` (``"elmer_slab"``,
    ``"foam_tunnel"``), reaping old ones first. Never raises on a reaping failure —
    a full disk is a problem, but so is a solve that dies tidying up."""
    base = _KIND_OK.sub("_", str(kind).strip().lower()).strip("_") or "case"
    where = root()
    try:
        reap()
    except Exception:
        pass
    return tempfile.mkdtemp(prefix=f"{base}-", dir=where)


def _entries(where: str) -> list:
    """``[(mtime, path)]`` for the case directories under ``where``, newest first."""
    out = []
    try:
        with os.scandir(where) as it:
            for e in it:
                try:
                    if not e.is_dir(follow_symlinks=False) or not _OURS.match(e.name):
                        continue
                    out.append((e.stat(follow_symlinks=False).st_mtime, e.path))
                except OSError:
                    continue
    except OSError:
        return []
    out.sort(reverse=True)
    return out


def _size(path: str, mtime: float, cap: int = _WALK_CAP) -> tuple:
    """``(bytes, exact, entries_walked)`` under ``path``, memoised per (path, mtime).

    The walk stops after ``cap`` entries — past that the exact number cannot change
    the decision, only how long it takes to reach it, and ``exact`` says so. A memo
    hit reports 0 entries walked, so a caller budgeting real work is not charged for
    a directory it already measured."""
    key = (path, mtime)
    with _lock:
        hit = _sizes.get(key)
    if hit is not None:
        return hit + (0,)
    total, seen, stack = 0, 0, [path]
    while stack and seen < cap:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    seen += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if seen >= cap:
                        break
        except OSError:
            continue
    out = (total, seen < cap)
    if out[1]:                              # only a complete walk is worth remembering
        with _lock:
            _sizes[key] = out
    return out + (seen,)


def reap(keep: int | None = None, max_gb: float | None = None,
         grace_s: float | None = None, now: float | None = None) -> dict:
    """Delete case directories past the bound, oldest first.

    Returns ``{removed, freed_bytes, kept, bytes, root, skipped_in_grace}``. A
    directory younger than the grace window is never removed, so ``kept`` can exceed
    ``keep`` — the bound is what the root settles back to, not a hard ceiling that
    would license deleting a running solve."""
    keep = _cfg("ANKUSDRIVE_CASE_KEEP", KEEP) if keep is None else keep
    max_gb = _cfg("ANKUSDRIVE_CASE_MAX_GB", MAX_GB) if max_gb is None else max_gb
    grace_s = _cfg("ANKUSDRIVE_CASE_GRACE_S", GRACE_S) if grace_s is None else grace_s
    where = root(create=False)
    out = {"removed": 0, "freed_bytes": 0, "kept": 0, "bytes": 0,
           "root": where, "skipped_in_grace": 0}
    if reaping_disabled() or keep <= 0:
        out["disabled"] = True
        return out
    now = time.time() if now is None else now
    cutoff = now - float(grace_s)
    budget = float(max_gb) * 1024 ** 3
    total = 0
    for i, (mtime, path) in enumerate(_entries(where)):
        over = i >= keep or total > budget
        if not over:
            total += _size(path, mtime)[0]
            out["kept"] += 1
            continue
        if mtime > cutoff:                    # still warm: a solve may be writing it
            out["skipped_in_grace"] += 1
            out["kept"] += 1
            total += _size(path, mtime)[0]
            continue
        freed = _size(path, mtime)[0]
        shutil.rmtree(path, ignore_errors=True)
        if os.path.exists(path):              # a file we could not remove: count it kept
            out["kept"] += 1
            total += freed
            continue
        out["removed"] += 1
        out["freed_bytes"] += freed
        with _lock:
            _sizes.pop((path, mtime), None)
    out["bytes"] = total
    return out


def usage(budget: int = 50_000) -> dict:
    """``{root, count, bytes, bytes_exact, keep, max_gb, grace_s, reaping}`` — what is
    on disk and the bound it is held to. The diagnostic behind "where did my solve
    go?", so it is called from a capability report and must stay quick: the total
    stops walking after ``budget`` directory entries and says ``bytes_exact: False``
    rather than stat-ing a 4 GB root of OpenFOAM time directories to answer."""
    where = root(create=False)
    entries = _entries(where)
    total, exact, left = 0, True, budget
    for mtime, path in entries:
        if left <= 0:                       # out of budget with directories left over
            exact = False
            break
        size, ok, walked = _size(path, mtime, cap=left)
        total += size
        exact = exact and ok
        left -= walked                      # a memo hit costs nothing
    return {"root": where, "count": len(entries),
            "bytes": total, "bytes_exact": exact,
            "keep": _cfg("ANKUSDRIVE_CASE_KEEP", KEEP),
            "max_gb": _cfg("ANKUSDRIVE_CASE_MAX_GB", MAX_GB),
            "grace_s": _cfg("ANKUSDRIVE_CASE_GRACE_S", GRACE_S),
            "reaping": not reaping_disabled()}
