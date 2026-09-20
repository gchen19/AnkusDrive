"""What actually computed a number: the environment behind a session (#433, epic #309).

The journal (``journal.py``) records *which tools ran with which arguments*, and the
exporter (``replay.py``) turns that into a script. Neither says what the machine was:
which AnkusDrive, which FreeCAD, and — for anything that reached an external solver —
which binary, which version, reached how. For screening that is fine. For a number
that goes into a design decision, "Elmer said 45.3 °C" is not evidence until you can
say *which Elmer*: my own substrate moved from Multipass to a container mid-session
and five solvers relocated with it (#433).

Two things live here, both pure Python (no FreeCAD, no ``mcp``), so the exporter and
the journal can use them without importing the server:

* :func:`identify` — one solver's identity: the resolved path (or module), the
  substrate it is reached through (``via``: host / wsl / multipass / container), and
  its **version**, probed by running the binary once. Cached per (path, mtime): the
  probe costs 0.1-3 s, so it never runs on a tool call — the journal records the
  cheap half (path + via, resolved the same way the solve resolved it, at the moment
  it ran) and the version is filled in later, when a transcript is exported.
* :func:`header` — the session's own environment: AnkusDrive and Python versions, the
  platform, how AnkusDrive was installed, the substrate, and (when the caller hands it
  one, since only the worker knows) FreeCAD's version.

Probing is best-effort by construction. A solver that will not say its version yields
``version: None`` with ``version_source`` explaining why, and never an exception: an
audit record that is missing one field is useful, one that failed to be written is not.
"""
from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
import sys
import threading

PROBE_TIMEOUT_S = 30.0

# How to ask each solver its version. Either:
#   ("argv", [args after the binary], regex with one group)  — run it once, read stdout+stderr
#   ("path", regex with one group)                           — read it off the resolved path
# A solver absent from this table reports version None (version_source "no probe").
# Notes on the odd ones:
#   * ElmerSolver has no pure --version: it boots, prints its banner, and exits 0 with
#     no case to run. Harmless (nothing is written) and it is the only way to get 26.2.
#   * OpenFOAM's apps need their environment sourced before they will run at all, and
#     the version is in the install path anyway (openfoam2606, OpenFOAM-11).
#   * SU2_CFD with no arguments prints its banner, then complains about the missing cfg.
_VERSION_PROBE: dict = {
    "elmer":       ("argv", ["--version"], r"ELMER SOLVER \(v\s*([^)]+?)\s*\)"),
    "calculix":    ("argv", ["-v"], r"[Vv]ersion\s+([\w.]+)"),
    "precice":     ("argv", ["-v"], r"[Vv]ersion\s+([\w.]+)"),
    "prusaslicer": ("argv", ["--help"], r"PrusaSlicer-(\S+)"),
    "blender":     ("argv", ["--version"], r"Blender\s+([\w.]+)"),
    "yade":        ("argv", ["--version"], r"[Yy]ade(?:\s+version)?:?\s+([\w.-]+)"),
    "su2":         ("argv", [], r"[Rr]elease\s+([\w.]+)"),
    "openfoam":    ("path", r"(?:openfoam|OpenFOAM)[-_]?(\d[\w.]*)"),
}

_lock = threading.Lock()
_cache: dict = {}                     # (name, path, mtime) -> {version, version_source}


def _solvers():
    from ankusdrive import solvers
    return solvers


def _run(argv: list, cwd: str | None = None) -> str:
    """Run ``argv`` once, time-boxed, and return stdout+stderr. Never raises."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                              timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as e:
        return f"\x00probe failed: {type(e).__name__}: {e}"
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _probe_binary(name: str, path: str, via: str | None) -> dict:
    """Ask a binary its version, through whatever substrate it is reached by."""
    probe = _VERSION_PROBE.get(name)
    if probe is None:
        return {"version": None, "version_source": "no probe for this solver"}
    if probe[0] == "path":
        m = re.search(probe[1], path)
        return ({"version": m.group(1), "version_source": "install path"} if m else
                {"version": None, "version_source": "version not in the install path"})
    _, args, pattern = probe
    if via in (None, "host", "native"):
        argv = [path, *args]
    else:                              # wsl / multipass / container: the same bash launcher
        script = " ".join(shlex.quote(a) for a in [path, *args]) + " 2>&1"
        try:
            argv = _solvers().bash_argv(script)
        except Exception as e:
            return {"version": None, "version_source": f"cannot launch via {via}: {e}"}
    out = _run(argv, cwd=os.path.dirname(path) if via in (None, "host", "native") else None)
    if out.startswith("\x00"):
        return {"version": None, "version_source": out[1:]}
    m = re.search(pattern, out)
    if m is None:
        return {"version": None, "version_source": "the binary did not state a version"}
    return {"version": m.group(1), "version_source": f"{os.path.basename(path)} "
                                                     f"{' '.join(args)}".strip()}


def _probe_module(module: str, interpreter: str | None) -> dict:
    """A wheel solver's version: its distribution metadata, read in the interpreter
    that will import it (some solvers run under their own — issue #351)."""
    if interpreter and interpreter != sys.executable:
        expr = (f"import importlib.metadata as m;print(m.version({module!r}))")
        out = _run([interpreter, "-c", expr])
        m = re.search(r"^\s*([\w.+-]+)\s*$", out.strip().splitlines()[0] if out.strip() else "")
        return ({"version": m.group(1), "version_source": "importlib.metadata"} if m else
                {"version": None, "version_source": "the module did not state a version"})
    try:
        import importlib.metadata as md
        return {"version": md.version(module), "version_source": "importlib.metadata"}
    except Exception:
        pass
    try:
        mod = __import__(module)
        v = getattr(mod, "__version__", None)
        if v:
            return {"version": str(v), "version_source": "__version__"}
    except Exception:
        pass
    return {"version": None, "version_source": "the module did not state a version"}


def resolve(name: str) -> dict:
    """Where ``name`` resolves **right now**, without running anything.

    ``{name, available, kind, family, path|module, via}`` — the cheap half, safe on a
    tool call: pure path resolution, no subprocess. ``via`` is the substrate the solve
    is reached through (``host`` when it is this machine). Returns
    ``{name, available: False, ...}`` for a solver that does not resolve, and
    ``{name, error}`` for a name the registry does not know."""
    try:
        info = _solvers().find_solver(name)
    except Exception as e:
        return {"name": name, "error": f"{type(e).__name__}: {e}"}
    out = {"name": name, "available": bool(info.get("available")),
           "kind": info.get("kind"), "family": info.get("family"),
           "via": info.get("via", "host")}
    for k in ("path", "module", "status"):
        if info.get(k) is not None:
            out[k] = info[k]
    if out["available"] and "path" in out:
        try:
            out["mtime"] = int(os.path.getmtime(out["path"]))
        except OSError:
            pass
    return out


def identify(name: str, resolved: dict | None = None) -> dict:
    """:func:`resolve` plus the version — the full identity, for an audit record.

    ``resolved`` is a record :func:`resolve` produced earlier (the journal keeps one
    per solve, taken while the solve ran); pass it so the identity describes the
    binary that *ran*, not whatever resolves at export time. The version probe runs
    the binary once and is cached per (name, path, mtime)."""
    out = dict(resolved) if resolved else resolve(name)
    if out.get("error") or not out.get("available"):
        return out
    key = (name, out.get("path") or out.get("module"), out.get("mtime"))
    with _lock:
        hit = _cache.get(key)
    if hit is None:
        try:
            if out.get("module") and out.get("kind") == "wheel":
                # a `path` on a wheel solver is the dedicated interpreter that imports
                # it (#351), not a binary — ask *that* Python, not this one
                hit = _probe_module(out["module"], out.get("path"))
            else:
                hit = _probe_binary(name, out["path"], out.get("via"))
        except Exception as e:                     # a probe must never break an export
            hit = {"version": None, "version_source": f"probe error: {type(e).__name__}: {e}"}
        with _lock:
            _cache[key] = hit
    return {**out, **hit}


def tilde(path: str | None) -> str | None:
    """``/home/user/opt/yade/bin/yade`` -> ``~/opt/yade/bin/yade``.

    A transcript is meant to be shareable, and a solver under ``$HOME`` carries the
    user's name in its path. Which install it was survives; who they are does not.
    Applied to both sides of a :func:`drift` comparison, so the collapse never reads
    as a difference."""
    if not path:
        return path
    home = os.path.expanduser("~")
    if home and home != os.sep and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def _redact(info: dict) -> dict:
    out = dict(info)
    for key in ("path", "module"):
        if isinstance(out.get(key), str):
            out[key] = tilde(out[key])
    out.pop("mtime", None)
    return out


def from_entries(entries: list, freecad: dict | None = None) -> dict:
    """The environment record for a session's journal entries.

    ``{env: <header>, solvers: {name: {…identity…, moved: [...]}}}``. Each solver the
    session reached is identified once — the resolution the journal captured *while
    the solve ran*, plus the version probed now. When a solver resolved differently
    at different points (a substrate change relocates everything under it — #433),
    the last resolution is the identity and the earlier ones are listed under
    ``moved``, because "which binary produced this number" then has two answers and
    an audit record must say so."""
    seen: dict = {}
    for e in sorted(entries, key=lambda e: e.get("seq", 0)):
        for rec in e.get("solvers") or []:
            name = rec.get("name")
            if not name:
                continue
            seen.setdefault(name, []).append(rec)
    out: dict = {"env": header(freecad), "solvers": {}}
    for name, records in sorted(seen.items()):
        ident = _redact(identify(name, records[-1]))
        earlier = []
        for rec in records[:-1]:
            slim = _redact({k: v for k, v in rec.items()
                            if k in ("path", "module", "via", "available")})
            if slim != {k: v for k, v in ident.items() if k in slim} and slim not in earlier:
                earlier.append(slim)
        if earlier:
            ident["moved"] = earlier
        out["solvers"][name] = ident
    return out


def header(freecad: dict | None = None) -> dict:
    """The session's environment: ``{ankusdrive, python, platform, install, substrate,
    run_script_allowed, freecad}``.

    ``freecad`` is the worker's own ``version`` result when the caller has one (only
    the worker knows which FreeCAD it booted); omitted otherwise rather than guessed."""
    from ankusdrive import __version__ as ver
    out: dict = {
        "ankusdrive": ver,
        "python": platform.python_version(),
        "platform": {"system": platform.system(), "machine": platform.machine(),
                     "release": platform.release()},
    }
    try:
        from ankusdrive import install_kind
        out["install"] = install_kind.detect()
    except Exception:
        pass
    try:
        out["substrate"] = _solvers().substrate()
    except Exception:
        pass
    out["run_script_allowed"] = bool(os.environ.get("ANKUSDRIVE_ALLOW_RUN_SCRIPT"))
    if freecad:
        out["freecad"] = _freecad_version(freecad)
    return out


def _freecad_version(payload: dict) -> dict:
    """The worker's ``version`` result as ``{version, build, python}``.

    FreeCAD reports its version as the raw ``App.Version()`` list — major, minor,
    patch, build string, then the git branch and hash. Flattened here, hash kept:
    two FreeCAD 1.1.0 builds from different commits are different FreeCADs."""
    out: dict = {}
    raw = payload.get("freecad")
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        out["version"] = ".".join(str(x) for x in raw[:3])
        rest = [str(x) for x in raw[3:] if x and str(x) != "Unknown"]
        if rest:
            out["build"] = rest[-1][:12] if len(rest[-1]) == 40 else rest[0]
    elif isinstance(raw, str):
        out["version"] = raw
    elif payload.get("version"):
        out["version"] = str(payload["version"])
    if payload.get("python"):
        out["python"] = str(payload["python"])
    return out


def drift(recorded: dict, current: dict | None = None) -> list:
    """What differs between a recorded environment and this machine's, as a list of
    human-readable lines. Empty means the two agree on everything recorded.

    The audit question a replay answers is not "did it run" but "did it run the same
    way": a different solver version is not a failure, it is the finding."""
    cur = current if current is not None else {}
    lines = []
    if not cur:
        cur = {"env": header(), "solvers": {}}
        for name, rec in (recorded.get("solvers") or {}).items():
            cur["solvers"][name] = _redact(identify(name))
    for key in ("ankusdrive", "python", "substrate"):
        was, now = (recorded.get("env") or {}).get(key), (cur.get("env") or {}).get(key)
        if was is not None and now is not None and was != now:
            lines.append(f"{key}: recorded {was}, now {now}")
    was_fc = ((recorded.get("env") or {}).get("freecad") or {}).get("version")
    now_fc = ((cur.get("env") or {}).get("freecad") or {}).get("version")
    if was_fc and now_fc and was_fc != now_fc:
        lines.append(f"freecad: recorded {was_fc}, now {now_fc}")
    for name, rec in sorted((recorded.get("solvers") or {}).items()):
        now = (cur.get("solvers") or {}).get(name) or {}
        if not now.get("available"):
            lines.append(f"solver {name}: recorded {rec.get('version') or 'unknown version'} "
                         f"at {rec.get('path') or rec.get('module')}, NOT AVAILABLE here")
            continue
        for key in ("version", "via", "path", "module"):
            was, got = rec.get(key), now.get(key)
            if was and got and was != got:
                lines.append(f"solver {name} {key}: recorded {was}, now {got}")
    return lines
