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

**The deck** (#437 item 2). A retained case answers "what was the solver handed?"
only while it exists, and only if nothing has written into it since — and the solver
writes its outputs into the same directory. So each job records a manifest of its case
*at the moment its first solver step launches* (:func:`snapshot`, called from every
launch chokepoint in the worker), and :func:`attach` puts it on the job's result beside
``case_dir`` as ``deck: {digest, count, bytes, files}``. That is what lets a replay say
*which file* changed when a number drifts, instead of only that it drifted.

Measured before choosing the moment: two independent runs of the same Elmer slab and
the same OpenFOAM pipe produce byte-identical **inputs**; every file that differs is a
solver **output** (``log.*`` embeds the case path and a timestamp, ``*.names`` a date).
Hashing after the solve would report every deck as changed.

Pure stdlib and FreeCAD-free: the worker, the out-of-process GPL runners and the tests
all import it.
"""
from __future__ import annotations

import hashlib
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


# --- the deck: what a job's solver was handed ------------------------------------------

DECK_FILE_CAP = 200          # list per-file hashes up to this many files
DECK_MAX_FILES = 5_000       # past this, a deck is reported as too large to digest
DECK_MAX_BYTES = 512 * 1024 ** 2
_local = threading.local()


# Lines a deck writer stamps with the wall clock, by file extension. Hashing them
# would make every replay's deck "differ" for no reason. Each entry is here because it
# was measured, not guessed: two identical runs were diffed and this was the only
# line that moved. Elmer .sif/mesh and OpenFOAM dictionaries carry none.
_VOLATILE_LINES = {
    # FreeCAD's CalculiX writer: "**   written on    --> Mon Sep 21 00:15:05 2026"
    ".inp": re.compile(rb"^\*\*\s+written on\s+-->.*\n?", re.M),
}
_VOLATILE_MAX = 256 * 1024 ** 2         # read whole only up to here; past it, hash as-is


def _file_hash(path: str) -> str | None:
    """SHA-256 of ``path`` with CRLF read as LF — the same deck written on Windows
    (text mode writes ``\r\n``) and on Linux is the same deck to a solver, and a
    transcript recorded on one and replayed on the other must not call every file
    changed — and with any :data:`_VOLATILE_LINES` for its type removed. ``None`` for
    a file that cannot be read."""
    volatile = _VOLATILE_LINES.get(os.path.splitext(path)[1].lower())
    if volatile is not None:
        try:
            if os.path.getsize(path) <= _VOLATILE_MAX:
                with open(path, "rb") as fh:
                    data = fh.read().replace(b"\r\n", b"\n")
                return hashlib.sha256(volatile.sub(b"", data)).hexdigest()
        except OSError:
            return None
    h = hashlib.sha256()
    carry = b""
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                block = carry + block
                carry = b"\r" if block.endswith(b"\r") else b""
                if carry:
                    block = block[:-1]
                h.update(block.replace(b"\r\n", b"\n"))
        h.update(carry)
    except OSError:
        return None
    return h.hexdigest()


def manifest(case_dir: str, only: list | None = None) -> dict:
    """What is in ``case_dir`` right now, as ``{digest, count, bytes, files}``.

    ``only`` restricts it to those relative paths — for a solver whose whole input is
    one file in a directory that also holds earlier runs' outputs (CalculiX reads
    exactly its ``.inp``; FreeCAD's FEM workdir keeps the last solve's ``.frd``).

    ``files`` maps each POSIX relative path to the first 16 hex digits of its hash —
    enough to say *which* file differs, at a third of the size, since this rides on
    every solve result an agent reads. ``digest`` is the full SHA-256 over every
    ``(path, full file hash)`` pair, so the aggregate claim stays at full strength.
    Over :data:`DECK_FILE_CAP` files, ``files`` is omitted; over
    :data:`DECK_MAX_FILES` files or :data:`DECK_MAX_BYTES`, ``digest`` is ``None`` and
    ``truncated`` says why rather than taking minutes over a finished prepared case.
    Symlinks are not followed."""
    root = os.path.abspath(case_dir)
    rows, total, count = [], 0, 0
    wanted = None if only is None else {str(x).replace(os.sep, "/") for x in only}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if wanted is not None and \
                    os.path.relpath(full, root).replace(os.sep, "/") not in wanted:
                continue
            if os.path.islink(full):
                continue
            count += 1
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
            if count > DECK_MAX_FILES or total > DECK_MAX_BYTES:
                return {"digest": None, "count": count, "bytes": total,
                        "truncated": (f"over {DECK_MAX_FILES} files" if count > DECK_MAX_FILES
                                      else f"over {DECK_MAX_BYTES // 1024 ** 2} MB")}
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            rows.append((rel, _file_hash(full) or "unreadable"))
    rows.sort()                          # by relative path, whatever order the walk took
    agg = hashlib.sha256()
    for rel, fh in rows:
        agg.update(rel.encode("utf-8", "replace") + b"\0" + fh.encode() + b"\n")
    out = {"digest": "sha256:" + agg.hexdigest(), "count": count, "bytes": total}
    if count <= DECK_FILE_CAP:
        out["files"] = {rel: fh[:16] for rel, fh in rows}
    return out


def snapshot(case_dir: str | None, only: list | None = None) -> None:
    """Record ``case_dir``'s deck for the job running on this thread, the first time
    this job launches a solver there. Called from every launch chokepoint.

    Once per job, not once per directory: a job's later steps (ElmerGrid, then
    ElmerSolver; blockMesh, then simpleFoam; fill, then pack) see their own earlier
    outputs, which are not the deck. But a *new* job on the same directory — a
    prepared case re-run, the molding pack continuing from a fill — gets a fresh
    manifest, because what it was handed genuinely includes those results. Every job
    runs on its own thread (``jobs.submit``), so a thread-local is exactly per-job;
    the worker gives its main thread the same boundary per request, so a synchronous
    solve gets its deck too. Never raises: an unreadable deck is a gap in the record,
    not a failed solve."""
    if not case_dir:
        return
    try:
        key = os.path.abspath(str(case_dir))
        seen = getattr(_local, "decks", None)
        if seen is None:
            seen = _local.decks = {}
        if key not in seen:
            seen[key] = manifest(key, only=only)
    except Exception:
        pass


# How a result names the directory its solver ran in: `case_dir` everywhere except
# FreeCAD FEM, whose results have always said `workdir`.
_DIR_KEYS = ("case_dir", "workdir")


def attach(result, _depth: int = 0):
    """Put this job's recorded decks on ``result``: every dict in it that names a
    directory (``case_dir``, or FEM's ``workdir``) the job snapshotted gains ``deck``
    beside it. Returns ``result``. Called as a job finishes, on the job's own thread,
    and by the worker after each synchronous request."""
    seen = getattr(_local, "decks", None)
    if not seen or _depth > 4:
        return result
    if isinstance(result, dict):
        for key in _DIR_KEYS:
            cd = result.get(key)
            if isinstance(cd, str) and "deck" not in result:
                m = seen.get(os.path.abspath(cd))
                if m is not None:
                    result["deck"] = dict(m)
        for v in list(result.values()):
            if isinstance(v, (dict, list)):
                attach(v, _depth + 1)
    elif isinstance(result, list):
        for v in result:
            if isinstance(v, (dict, list)):
                attach(v, _depth + 1)
    return result


def forget() -> None:
    """Drop this thread's recorded decks (a job is over)."""
    _local.decks = {}


def diff(recorded: dict, current: dict) -> list:
    """Human-readable differences between two deck manifests; empty when they agree.

    The aggregate digest decides; the per-file table says which files, when both
    sides carried one. A deck over the listing cap can only say that it differs."""
    if not recorded or not current:
        return ["no deck on " + ("the recording" if not recorded else "this run")]
    if recorded.get("digest") and recorded.get("digest") == current.get("digest"):
        return []
    if not recorded.get("digest") or not current.get("digest"):
        why = recorded.get("truncated") or current.get("truncated") or "not digested"
        return [f"deck too large to compare ({why})"]
    was, now = recorded.get("files"), current.get("files")
    if was is None or now is None:
        return [f"deck differs: {recorded.get('count')} files recorded, "
                f"{current.get('count')} now (too many to list per file)"]
    lines = []
    for rel in sorted(set(was) | set(now)):
        if rel not in now:
            lines.append(f"- {rel}  (recorded, missing now)")
        elif rel not in was:
            lines.append(f"+ {rel}  (new, not in the recording)")
        elif was[rel] != now[rel]:
            lines.append(f"~ {rel}")
    return lines or ["deck differs, but every listed file matches (an unlisted file changed)"]

