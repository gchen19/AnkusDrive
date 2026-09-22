"""The durable journal: the session journal on disk, opt-in, bounded (#433, epic #309).

``journal.py`` keeps every tool call in the server's memory, and ``session_transcript``
turns that into a script — for as long as the server lives. An analysis that feeds a
design decision has to outlive the session that ran it: the question "what exactly was
run, with what inputs, under what environment, to get this figure?" is asked weeks
later, after the server, its memory and the agent's transcript are gone (#433). This
module is the on-disk half: **off by default, one setting turns it on.**

**Enabling.** ``ANKUSDRIVE_JOURNAL_DIR`` (env), or ``journal_dir`` in config.toml
(``[journal] dir`` is read too, the spelling #433 proposed). Unset means nothing is
ever written and :func:`append` returns at its first line. A relative or ``~`` path is
expanded against the user's home; nothing per-OS is assumed.

**Layout.** One file per server process — a *session* —
``<dir>/session-<UTC start>-<pid>-<rand>.jsonl``, append-only, one JSON object per
line (UTF-8, ``\\n`` on every OS):

* ``{"type": "header", ...}`` — once, first: ``format``, ``session``, ``ts``, ``pid``,
  ``env`` (:func:`provenance.header` — AnkusDrive / Python / platform / install /
  substrate; the same record ``session_transcript`` opens with, minus the solver
  versions, which cost a subprocess each and are probed at export instead), and the
  journal's own settings (``redact``, the caps). Rewritten, marked ``reopened``, if the
  file disappears under a live session (deleted by hand, reaped by another server).
* ``{"type": "worker", workspace, worker_pid, freecad}`` — the first time a call sees a
  new worker, with the FreeCAD version it booted. Read off the worker object; no RPC.
* ``{"type": "call", ts, session, ...}`` — one per tool call: exactly the entry
  :func:`journal.record` built (``seq``, ``tool``, ``args``, ``ok``, ``result`` or
  ``error``, ``workspace``, ``worker_pid``, ``txn_depth``, ``elapsed_s``, ``solvers``,
  ``code_sha256``) plus ``result_digest``: ``sha256:`` over the canonical JSON
  (:func:`digest`) of the **full, uncompacted** result. The stored ``result`` is the
  compacted copy — a base64 render is a size marker — so the digest is what lets
  whoever holds the real value prove it is the one this call returned.

Lines are written as calls *finish*, so two overlapping calls can land out of ``seq``
order; every line carries its ``seq`` and :func:`load` sorts. A missing ``seq`` is a
gap in the record, and the export says so.

**Best-effort, always.** A journal write happens after the tool has produced its
result, under a lock, as one ``write`` of one line (tens of microseconds; the digest
adds a JSON encode, ~milliseconds for a multi-megabyte render). Any failure — a
read-only directory, a full disk, an unencodable value — is logged on the
``ankusdrive.journal`` logger (the first time, then every 100th) and swallowed. It
never changes what the tool returns or raises.

**Retention** (#433's open question; the policy is ``cases.py``'s, #437). Checked when
a session opens its file:

* at most :data:`KEEP` session files (``ANKUSDRIVE_JOURNAL_KEEP``, default 50),
* at most :data:`MAX_MB` megabytes in total (``ANKUSDRIVE_JOURNAL_MAX_MB``, default 512),
* oldest first by mtime, but never this session's own file, and never a file written
  within :data:`GRACE_S` (``ANKUSDRIVE_JOURNAL_GRACE_S``, default 3600) — that is
  another live server sharing the directory. Only ``session-*.jsonl`` names are
  touched; anything else in the directory is left alone.
* one session is bounded too: past :data:`FILE_MAX_MB` (``ANKUSDRIVE_JOURNAL_FILE_MAX_MB``,
  default 64) a call line keeps its arguments, digest and verdict but drops the
  ``result`` body (``result_elided``). Arguments are what replay needs; the digest
  still pins the result.

**Redaction** (#433's other open question), opt-in: ``ANKUSDRIVE_JOURNAL_REDACT=1``.
String values that look like a path, or that sit under a naming key (``name``,
``label``, ``title``, ``doc``, ``author``, ``customer``, …, or any ``*path`` /
``*_dir`` / ``*file`` key), in arguments *and* results, become
``redacted:sha256:<hex>`` of the value; paths inside error messages are hashed the
same way; solver paths collapse to ``~/…``. The hash is deterministic, so a document
name returned by one call and passed to the next still reads as the same value.
Never redacted: ``run_script``'s ``code`` (it *is* the analysis — verbatim plus its
SHA-256, #433's non-negotiable), handles and job ids, numbers, booleans, enum-like
strings. **The trade-off, stated:** a redacted journal is *auditable by digest* — you
can prove a given file name or result was the one used — but *not replayable*: the
script it exports passes hashes where the paths were. The hash is unsalted, so a short
guessable name can be recovered by trying candidates; redaction keeps names out of a
shared file, it is not encryption.

Pure stdlib and FreeCAD-free, like ``journal.py``; imported lazily from
:func:`journal.record` only once the journal directory is set.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any

FORMAT = "ankusdrive-journal/1"
KEEP = 50
MAX_MB = 512.0
FILE_MAX_MB = 64.0
GRACE_S = 3600.0

ENV_DIR = "ANKUSDRIVE_JOURNAL_DIR"
ENV_REDACT = "ANKUSDRIVE_JOURNAL_REDACT"

_NAME = re.compile(r"^session-(\d{8}T\d{6}Z-\d+-[0-9a-f]{6})\.jsonl$")
_SESSION_ID = re.compile(r"^\d{8}T\d{6}Z-\d+-[0-9a-f]{6}$")

log = logging.getLogger("ankusdrive.journal")

_lock = threading.Lock()
_state: dict = {"session": None, "path": None, "bytes": 0, "headers": 0,
                "workers": set(), "errors": 0}


# --- settings ------------------------------------------------------------------------

def _cfg(env_var: str, default):
    """``ANKUSDRIVE_*`` env, then config.toml, then ``default`` — ``cases._cfg``'s rule:
    an unparseable value falls back to the default instead of failing a call."""
    try:
        from ankusdrive import config
        raw = config.get(env_var)
    except Exception:
        raw = os.environ.get(env_var)
    if raw is None or str(raw).strip() == "":
        return default
    if isinstance(default, bool):
        return str(raw).strip().lower() not in ("0", "false", "no", "off")
    if isinstance(default, str):
        return str(raw)
    try:
        return type(default)(str(raw).strip())
    except (TypeError, ValueError):
        return default


def journal_dir() -> str | None:
    """The configured journal directory, expanded, or ``None`` when the durable
    journal is off (the default)."""
    raw = _cfg(ENV_DIR, "")
    if not raw.strip():
        return None
    return os.path.abspath(os.path.expanduser(raw.strip()))


def redacting() -> bool:
    return _cfg(ENV_REDACT, False)


def caps() -> dict:
    return {"keep": _cfg("ANKUSDRIVE_JOURNAL_KEEP", KEEP),
            "max_mb": _cfg("ANKUSDRIVE_JOURNAL_MAX_MB", MAX_MB),
            "file_max_mb": _cfg("ANKUSDRIVE_JOURNAL_FILE_MAX_MB", FILE_MAX_MB),
            "grace_s": _cfg("ANKUSDRIVE_JOURNAL_GRACE_S", GRACE_S)}


# --- digest and redaction --------------------------------------------------------------

def canonical(value: Any) -> str:
    """The canonical JSON a digest is taken over: sorted keys, no whitespace, UTF-8
    as-is, anything non-JSON as its ``str()``. Mixed-type keys that cannot be sorted
    fall back to insertion order — still deterministic for the same value."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
    except TypeError:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    """``sha256:<hex>`` of :func:`canonical` — how a full result is pinned. Recompute
    it over the value a client received to show it is the one the journal recorded."""
    return "sha256:" + hashlib.sha256(
        canonical(value).encode("utf-8", "surrogatepass")).hexdigest()


def _hash_str(value: str) -> str:
    return "redacted:sha256:" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


# A value that is a path: absolute (POSIX, ~, drive letter, UNC), relative with a
# separator, or a bare file name with a CAD / solver / data extension.
_PATHLIKE = re.compile(
    r"^(/|~[/\\]|[A-Za-z]:[\\/]|\\\\)"
    r"|^[^\s/\\]+[/\\][^\s]"
    r"|\.(fcstd|fcstd1|step|stp|iges|igs|brep|brp|stl|obj|3mf|dxf|dwg|svg|pdf|png|jpe?g"
    r"|inp|frd|sif|vtu|vtk|msh|unv|gcode|json|csv|toml|ya?ml|txt|py|zip)$", re.I)
_NAME_KEYS = frozenset("""
name label title doc document author company customer client project owner engineer
drawn_by checked_by approved_by designer part part_number part_name item drawing_number
description text material_name filename basename
""".split())
_PATH_KEY = re.compile(r"(path|_dir|dir|file|files|paths)$")
# Never hashed: replay linkage (worker counters, not names) and the analysis itself.
_KEEP_KEY = re.compile(r"(^|_)(handle|handles|job_id|analysis)$")
# Absolute paths inside free text (error messages).
_PATH_IN_TEXT = re.compile(r"(?:(?<=[\s'\"(=:])|^)(?:~|[A-Za-z]:)?[/\\][^\s'\"(),:;]*[/\\][^\s'\"(),;]*")


def _redact(value: Any, key: str = "", _depth: int = 0) -> Any:
    if _depth > 20:
        return value
    if isinstance(value, dict):
        return {k: _redact(v, str(k), _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key, _depth + 1) for v in value]
    if not isinstance(value, str) or not value or value.startswith("redacted:"):
        return value
    k = key.lower()
    if _KEEP_KEY.search(k):
        return value
    if k in _NAME_KEYS or _PATH_KEY.search(k) or _PATHLIKE.search(value):
        return _hash_str(value)
    return value


def _redact_text(text: str) -> str:
    return _PATH_IN_TEXT.sub(lambda m: _hash_str(m.group(0)), text)


def redact_entry(entry: dict) -> dict:
    """A copy of a journal entry with identifying strings hashed (module docstring).
    ``run_script``'s ``code`` and ``code_sha256`` pass through untouched."""
    out = dict(entry)
    args = dict(entry.get("args") or {})
    code = args.pop("code", None) if entry.get("tool") == "run_script" else None
    args = _redact(args)
    if code is not None:
        args["code"] = code
    out["args"] = args
    if "result" in out:
        out["result"] = _redact(out["result"])
    if isinstance(out.get("error"), str):
        out["error"] = _redact_text(out["error"])
    if out.get("solvers"):
        try:
            from ankusdrive import provenance
            tilde = provenance.tilde
        except Exception:
            def tilde(p):
                return p
        out["solvers"] = [{**s, **{k: tilde(s[k]) for k in ("path", "module")
                                   if isinstance(s.get(k), str)}} for s in out["solvers"]]
    out["redacted"] = True
    return out


# --- writing -----------------------------------------------------------------------

def _now_iso() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t % 1 * 1000):03d}Z"


def session_id() -> str:
    """This process's session id, minted on first use: ``<UTC start>-<pid>-<rand>``."""
    with _lock:
        if _state["session"] is None:
            _state["session"] = (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                                 + f"-{os.getpid()}-{secrets.token_hex(3)}")
        return _state["session"]


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"


def _write_locked(path: str, text: str) -> None:
    data = text.encode("utf-8", "surrogatepass")
    fresh = not os.path.exists(path)
    with open(path, "ab") as fh:            # binary append: "\n" on every OS, one write
        fh.write(data)
    if fresh:
        try:
            os.chmod(path, 0o600)           # it holds arguments and results: owner only
        except OSError:
            pass
    _state["bytes"] += len(data)


def _open_locked(where: str, sid: str) -> str:
    """Make sure this session's file exists under ``where`` and starts with a header;
    reap old sessions when a file is first opened. Returns its path."""
    path = os.path.join(where, f"session-{sid}.jsonl")
    if _state["path"] == path and os.path.exists(path):
        return path
    os.makedirs(where, mode=0o700, exist_ok=True)
    reopened = _state["path"] == path            # it was ours, and it vanished
    if not os.path.exists(path):
        _state["bytes"] = 0
        _state["workers"] = set()
        header = {"type": "header", "format": FORMAT, "session": sid, "ts": _now_iso(),
                  "pid": os.getpid(), "env": _env(), "journal": {"redact": redacting(),
                                                                 **caps()}}
        if reopened:
            header["reopened"] = True
        _write_locked(path, _line(header))
        _state["headers"] += 1
    else:
        _state["bytes"] = os.path.getsize(path)
    _state["path"] = path
    try:
        reap(where, live=path)
    except Exception as e:
        _complain(e)
    return path


def _env() -> dict:
    """The session's environment, from the same function ``session_transcript`` uses.
    No solver is run: versions are probed at export."""
    try:
        from ankusdrive import provenance
        return provenance.header()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _complain(e: BaseException) -> None:
    _state["errors"] += 1
    n = _state["errors"]
    if n == 1 or n % 100 == 0:
        log.warning("AnkusDrive journal: could not write to %s (%s: %s); the tool call "
                    "is unaffected [%d failure(s) so far]",
                    _state.get("path") or journal_dir(), type(e).__name__, e, n)


def append(entry: dict, result: Any = None, *, freecad: Any = None) -> bool:
    """Write one finished call to this session's journal file, if the durable journal
    is on. ``entry`` is :func:`journal.record`'s entry (compacted result); ``result``
    the full value the tool returned (digested, never stored); ``freecad`` the
    worker's booted FreeCAD version when known. Returns whether a line was written.
    Never raises."""
    try:
        where = journal_dir()
        if where is None:
            return False
        rec = dict(entry)
        if entry.get("ok"):
            rec["result_digest"] = digest(result)
        if redacting():
            rec = redact_entry(rec)
        sid = session_id()
        with _lock:
            path = _open_locked(where, sid)
            wkey = (entry.get("workspace"), entry.get("worker_pid"))
            text = ""
            if entry.get("worker_pid") is not None and wkey not in _state["workers"] \
                    and freecad:
                _state["workers"].add(wkey)
                text += _line({"type": "worker", "ts": _now_iso(), "session": sid,
                               "workspace": wkey[0], "worker_pid": wkey[1],
                               "freecad": freecad})
            line = _line({"type": "call", "ts": _now_iso(), "session": sid, **rec})
            if _state["bytes"] + len(line) > caps()["file_max_mb"] * 1024 ** 2 \
                    and "result" in rec:
                rec.pop("result")
                rec["result_elided"] = "journal file cap (ANKUSDRIVE_JOURNAL_FILE_MAX_MB)"
                line = _line({"type": "call", "ts": _now_iso(), "session": sid, **rec})
            _write_locked(path, text + line)
        return True
    except Exception as e:
        with _lock:
            _complain(e)
        return False


def status() -> dict:
    """``{enabled, dir, session, file, redact, errors, caps}`` — for diagnostics."""
    where = journal_dir()
    return {"enabled": where is not None, "dir": where, "session": _state["session"],
            "file": _state["path"], "redact": redacting(), "errors": _state["errors"],
            **caps()}


def _reset_for_tests() -> None:
    with _lock:
        _state.update(session=None, path=None, bytes=0, headers=0, workers=set(), errors=0)


# --- retention -----------------------------------------------------------------------

def _files(where: str) -> list:
    """``[(mtime, size, path)]`` for the session files in ``where``, newest first."""
    out = []
    try:
        with os.scandir(where) as it:
            for e in it:
                if not _NAME.match(e.name):
                    continue
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                out.append((st.st_mtime, st.st_size, e.path))
    except OSError:
        return []
    out.sort(reverse=True)
    return out


def reap(where: str | None = None, *, live: str | None = None, keep: int | None = None,
         max_mb: float | None = None, grace_s: float | None = None,
         now: float | None = None) -> dict:
    """Delete session files past the caps, oldest first — never ``live`` (this
    session's own file) and never one written within the grace window.

    Returns ``{removed, freed_bytes, kept, bytes, dir, skipped_in_grace}``. As in
    ``cases.reap``, the caps are what the directory settles back to, not a ceiling
    that would license deleting another live server's journal."""
    where = where or journal_dir()
    c = caps()
    keep = c["keep"] if keep is None else keep
    max_mb = c["max_mb"] if max_mb is None else max_mb
    grace_s = c["grace_s"] if grace_s is None else grace_s
    out = {"removed": 0, "freed_bytes": 0, "kept": 0, "bytes": 0, "dir": where,
           "skipped_in_grace": 0}
    if not where:
        return out
    now = time.time() if now is None else now
    budget = float(max_mb) * 1024 ** 2
    live_abs = os.path.abspath(live) if live else None
    files = _files(where)
    # the live file always counts first, whatever its mtime
    files.sort(key=lambda f: (os.path.abspath(f[2]) != live_abs, -f[0]))
    total = 0
    for i, (mtime, size, path) in enumerate(files):
        over = i >= keep or total + size > budget
        if not over or os.path.abspath(path) == live_abs:
            total += size
            out["kept"] += 1
            continue
        if mtime > now - float(grace_s):
            out["skipped_in_grace"] += 1
            out["kept"] += 1
            total += size
            continue
        try:
            os.remove(path)
        except OSError:
            out["kept"] += 1
            total += size
            continue
        out["removed"] += 1
        out["freed_bytes"] += size
    out["bytes"] = total
    return out


# --- reading and export ----------------------------------------------------------------

def sessions(where: str | None = None) -> list:
    """The session files in the journal directory, newest first:
    ``[{session, file, bytes, modified, live}]``."""
    where = where or journal_dir()
    if not where:
        return []
    live = _state.get("path")
    return [{"session": _NAME.match(os.path.basename(p)).group(1), "file": p,
             "bytes": size, "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                      time.gmtime(mtime)),
             "live": live is not None and os.path.abspath(p) == os.path.abspath(live)}
            for mtime, size, p in _files(where)]


def resolve(session: str, where: str | None = None) -> str:
    """The journal file for ``session`` — a session id, ``"latest"``, or (from the
    CLI only) a path to a ``.jsonl`` file. An id is looked up inside the journal
    directory and nowhere else."""
    if _SESSION_ID.match(session or ""):
        where = where or journal_dir()
        if not where:
            raise ValueError("the durable journal is off: set ANKUSDRIVE_JOURNAL_DIR "
                             "(or journal_dir in config.toml) to the directory to read")
        path = os.path.join(where, f"session-{session}.jsonl")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no journal for session {session!r} in {where}")
        return path
    if session == "latest":
        found = sessions(where)
        if not found:
            raise FileNotFoundError(f"no journal sessions in {where or journal_dir()}")
        return found[0]["file"]
    raise ValueError(f"{session!r} is not a session id (expected like "
                     f"20260922T101500Z-4242-a1b2c3) or 'latest'")


_DISK_ONLY = ("type", "ts", "session", "result_digest", "result_elided", "redacted")


def load(path: str) -> dict:
    """Parse a journal file: ``{header, entries, workers, ledger, bad_lines, gaps,
    elided, redacted}``. ``entries`` are in ``seq`` order and carry exactly the fields
    the in-memory journal holds, so the exporter sees what ``session_transcript``
    would. A torn last line (a server killed mid-write) is counted, not fatal."""
    header, calls, workers, bad = None, {}, [], 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                bad += 1
                continue
            kind = rec.get("type") if isinstance(rec, dict) else None
            if kind == "header":
                header = header or rec
            elif kind == "worker":
                workers.append(rec)
            elif kind == "call" and isinstance(rec.get("seq"), int):
                calls.setdefault(rec["seq"], rec)
            else:
                bad += 1
    ordered = [calls[s] for s in sorted(calls)]
    seqs = [r["seq"] for r in ordered]
    gaps = []
    expect = 1
    for s in seqs:
        if s > expect:
            gaps.append([expect, s - 1])
        expect = s + 1
    ledger = []
    for r in ordered:
        row = {"seq": r["seq"], "ts": r.get("ts"), "tool": r.get("tool"),
               "workspace": r.get("workspace"), "ok": r.get("ok")}
        for k in ("result_digest", "code_sha256", "error", "result_elided"):
            if r.get(k) is not None:
                row[k] = r[k]
        ledger.append(row)
    entries = [{k: v for k, v in r.items() if k not in _DISK_ONLY} for r in ordered]
    return {"header": header, "entries": entries, "workers": workers, "ledger": ledger,
            "bad_lines": bad, "gaps": gaps,
            "elided": sum(1 for r in ordered if r.get("result_elided")),
            "redacted": bool((header or {}).get("journal", {}).get("redact"))
            or any(r.get("redacted") for r in ordered)}


def final_workspace(entries: list) -> str:
    """The workspace current when the session ended, replayed from its own calls —
    ``session_transcript``'s default, for a session that is no longer running."""
    ws = "default"
    for e in entries:
        if not e.get("ok"):
            continue
        if e.get("tool") == "use_workspace":
            ws = (e.get("args") or {}).get("name") or ws
        elif e.get("tool") == "close_workspace" and (e.get("args") or {}).get("name") == ws:
            ws = "default"
    return ws


def _provenance(loaded: dict, entries: list, workspace: str) -> tuple:
    """The PROVENANCE record for a past session: the *recorded* environment (the
    header, plus the FreeCAD the workspace's last worker booted) and every solver its
    calls reached, identified from the resolution recorded while each solve ran.
    Returns ``(record, warnings)``."""
    from ankusdrive import provenance
    freecad = None
    for w in loaded["workers"]:
        if w.get("workspace") == workspace and w.get("freecad"):
            freecad = w["freecad"]
    payload = {"freecad": freecad} if freecad else None
    rec = provenance.from_entries(entries, freecad=payload)
    recorded_env = (loaded["header"] or {}).get("env")
    if isinstance(recorded_env, dict) and not recorded_env.get("error"):
        env = dict(recorded_env)
        if payload:
            env["freecad"] = provenance._freecad_version(payload)
        rec["env"] = env
    warnings = []
    last: dict = {}
    for e in entries:
        for s in e.get("solvers") or []:
            if s.get("name"):
                last[s["name"]] = s
    probed = time.strftime("%Y-%m-%d", time.gmtime())
    for name, s in sorted(last.items()):
        path, mtime = s.get("path"), s.get("mtime")
        if not path or mtime is None:
            continue
        try:
            now = int(os.path.getmtime(path))
        except OSError:
            now = None
        if now != mtime:
            warnings.append(f"solver {name}: its version was probed at export ({probed}) "
                            f"and the binary at {provenance.tilde(path)} has changed or "
                            f"gone since the session ran — the recorded version may not "
                            f"be the one that ran")
    return rec, warnings


def export(session: str, *, workspace: str | None = None, include_read_only: bool = False,
           checkpoints: bool = True, prune_aborted: bool = False, provenance: bool = True,
           where: str | None = None, path: str | None = None) -> dict:
    """A past session's journal as the record ``session_transcript`` would have given
    while it ran: the replay script, plus the recorded environment and the ordered
    call ledger with every digest. ``path`` reads a file directly (the CLI); otherwise
    ``session`` is resolved inside the journal directory."""
    from ankusdrive import replay
    file = path or resolve(session, where)
    loaded = load(file)
    ws = workspace or final_workspace(loaded["entries"])
    entries = [e for e in loaded["entries"] if e.get("workspace") == ws]
    truncated = bool(loaded["gaps"]) or loaded["header"] is None
    prov, prov_warn = (_provenance(loaded, entries, ws) if provenance else (None, []))
    out = replay.export(entries, workspace=ws, include_read_only=include_read_only,
                        checkpoints=checkpoints, prune_aborted=prune_aborted,
                        truncated=truncated, provenance=prov)
    skipped: dict = {}
    for s in out["skipped"]:
        skipped[s["reason"]] = skipped.get(s["reason"], 0) + 1
    warnings = list(out.get("warnings") or []) + prov_warn
    if loaded["header"] is None:
        warnings.append("the journal has no session header: the start of the session "
                        "is missing")
    if loaded["gaps"]:
        warnings.append(f"calls missing from the journal (seq ranges {loaded['gaps']}): "
                        f"a write failed or the journal was enabled mid-session")
    if loaded["bad_lines"]:
        warnings.append(f"{loaded['bad_lines']} unreadable line(s) skipped (a torn "
                        f"write from a killed server)")
    if loaded["elided"]:
        warnings.append(f"{loaded['elided']} call(s) past the per-session size cap kept "
                        f"their arguments and digest but not their result body")
    if loaded["redacted"]:
        warnings.append("this journal was written with ANKUSDRIVE_JOURNAL_REDACT: "
                        "identifying strings are sha256 hashes, so the script is an "
                        "audit record, not a runnable replay")
    header = loaded["header"] or {}
    result = {"session": header.get("session") or session, "file": file, "workspace": ws,
              "workspaces": sorted({e.get("workspace") for e in loaded["entries"]
                                    if e.get("workspace")}),
              "started": header.get("ts"),
              "ended": loaded["ledger"][-1]["ts"] if loaded["ledger"] else None,
              "env": header.get("env"), **out, "skipped": skipped, "warnings": warnings,
              "truncated": truncated, "redacted": loaded["redacted"],
              "ledger": loaded["ledger"],
              "integrity": {"calls": len(loaded["entries"]), "gaps": loaded["gaps"],
                            "bad_lines": loaded["bad_lines"], "elided": loaded["elided"]}}
    if prov is not None:
        result["provenance"] = prov
    if not entries:
        result["note"] = f"no tool calls recorded in workspace {ws!r} in this session"
    return result
