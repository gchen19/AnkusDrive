"""Persistent config file — the resolution layer between env and auto-discovery.

Every non-default path in AnkusDrive used to be an environment variable (~24
``ANKUSDRIVE_*`` vars). That is brittle for a local MCP server: the vars must be
set in the exact shell/launcher that spawns ``ankusdrive mcp``, but MCP hosts
(Claude Desktop, ``claude mcp add``, Smithery) launch the command with a
*minimal* env, so the user's shell exports don't carry over — "works in my
terminal, unresolved under the MCP host" (issue #199).

This module adds a single persistent file as a middle layer. Resolution
precedence everywhere it is consulted:

    ``ANKUSDRIVE_*`` env override  →  **config file**  →  PATH / auto-discovery

Location: ``%APPDATA%\\ankusdrive\\config.toml`` on Windows,
``$XDG_CONFIG_HOME/ankusdrive/config.toml`` (default ``~/.config/...``)
elsewhere; ``ANKUSDRIVE_CONFIG`` overrides the path outright. Schema — flat,
mirroring the env vars (one key per ``ANKUSDRIVE_*`` var, lowercased, minus the
prefix)::

    freecadcmd = "C:/Program Files/FreeCAD 1.1/bin/freecadcmd.exe"
    journal_dir = "~/ankusdrive-journal"         # ANKUSDRIVE_JOURNAL_DIR (#433); unset = off
    [solvers]
    su2_path        = "C:/.../SU2_CFD.exe"       # ANKUSDRIVE_SU2_PATH
    elmer_path      = "C:/.../ElmerSolver.exe"   # ANKUSDRIVE_ELMER_PATH
    openfoam_bashrc = "/usr/lib/openfoam/openfoam2312/etc/bashrc"

Pure-Python and FreeCAD-free (importable by ``client.py``, ``solvers.py`` and
the worker alike). Parsed with stdlib ``tomllib`` (3.11+); on 3.10 a minimal
flat parser covers exactly the schema above (bare string values, one table
level). Read-only — nothing here writes the file (``ankusdrive setup`` will,
issue #200)."""
from __future__ import annotations

import os

_ENV_PREFIX = "ANKUSDRIVE_"
# (path, mtime) -> parsed dict; a missing file caches under mtime None so a
# server process doesn't stat-parse on every solver probe yet picks up a file
# created/edited later.
_cache: dict = {}


def config_path() -> str:
    """The config file location for this platform (the file may not exist).
    ``ANKUSDRIVE_CONFIG`` overrides outright; else ``%APPDATA%\\ankusdrive\\config.toml``
    on Windows, ``$XDG_CONFIG_HOME|~/.config/ankusdrive/config.toml`` elsewhere."""
    return _current_config_path()


def _config_base() -> str:
    """The per-platform config root that holds the app's config directory."""
    if os.name == "nt":
        return os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def _current_config_path() -> str:
    """The config file location, before any existence check — what ``write`` targets."""
    if env := os.environ.get("ANKUSDRIVE_CONFIG"):
        return env
    return os.path.join(_config_base(), "ankusdrive", "config.toml")


def _parse_minimal(text: str) -> dict:
    """Flat-TOML fallback for Python 3.10 (no ``tomllib``): bare ``key = "value"``
    pairs and one level of ``[table]`` headers. Comments and blank lines skipped;
    anything fancier needs 3.11+."""
    out: dict = {}
    table = out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            table = out.setdefault(line[1:-1].strip(), {})
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.split("#", 1)[0].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        table[key.strip()] = val
    return out


def load() -> dict:
    """The parsed config dict — ``{}`` when the file is absent or unreadable
    (config is always optional). Cached per (path, mtime), so edits are picked
    up without restarting the server."""
    path = config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _cache[path, None] = {}
        return {}
    key = (path, mtime)
    if key not in _cache:
        try:
            try:
                import tomllib
                with open(path, "rb") as f:
                    _cache[key] = tomllib.load(f)
            except ImportError:                      # Python 3.10
                with open(path, encoding="utf-8") as f:
                    _cache[key] = _parse_minimal(f.read())
        except Exception:
            _cache[key] = {}                         # malformed file -> optional
    return _cache[key]


def _config_key(env_var: str):
    """Map a ``ANKUSDRIVE_*`` env var to its config location: ``FREECADCMD`` and
    ``TOOLSETS`` (#377) and ``ALLOW_RUN_SCRIPT`` (#378) and the durable journal's
    ``JOURNAL_*`` settings (#433) are top-level keys; everything else lives lowercased
    under ``[solvers]``."""
    name = env_var[len(_ENV_PREFIX):] if env_var.startswith(_ENV_PREFIX) else env_var
    if name in ("FREECADCMD", "TOOLSETS", "ALLOW_RUN_SCRIPT") or name.startswith("JOURNAL_"):
        return (name.lower(),)
    return ("solvers", name.lower())


def _config_keys(env_var: str) -> list:
    """Every config location ``env_var`` is read from, first match wins: the
    canonical :func:`_config_key`, then an alias. The only alias is #433's proposed
    spelling for the journal — ``[journal] dir`` for ``journal_dir``, and likewise
    ``redact``, ``keep``, … — read but never written."""
    keys = [_config_key(env_var)]
    name = env_var[len(_ENV_PREFIX):] if env_var.startswith(_ENV_PREFIX) else env_var
    if name.startswith("JOURNAL_"):
        keys.append(("journal", name[len("JOURNAL_"):].lower()))
    return keys


def lookup(env_var: str) -> tuple:
    """Resolve ``env_var`` through the two explicit layers. Returns
    ``(value, source)`` with source ``"env"`` | ``"config"``, or ``(None, None)``
    when neither sets it (the caller falls through to PATH/auto-discovery)."""
    if env := os.environ.get(env_var):
        return env, "env"
    data = load()
    for path in _config_keys(env_var):
        node: object = data
        for part in path:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if path[0] == "journal" or (len(path) == 1 and path[0].startswith("journal_")):
            # the journal's settings are booleans and numbers (`redact = true`,
            # `keep = 20`); every other key stays string-only, as it always was
            if isinstance(node, bool):
                return ("1" if node else "0"), "config"
            if isinstance(node, (int, float)):
                return str(node), "config"
        if isinstance(node, str) and node:
            return node, "config"
    return None, None


def get(env_var: str, default=None):
    """The env-or-config value for ``env_var`` (no source tag), or ``default``."""
    value, _ = lookup(env_var)
    return default if value is None else value


def write(values: dict) -> str:
    """Merge ``{env_var: value}`` pairs into the config file and return its path
    (``ankusdrive setup``'s persistence step, issue #200). Existing keys the update
    doesn't name are preserved; the directory is created if needed. Values are
    written with forward slashes (valid TOML without escaping, and Windows
    accepts them)."""
    path = _current_config_path()
    data = load()
    top = dict(data)
    solvers_tbl = dict(top.pop("solvers", {}) or {})
    for env_var, value in values.items():
        key = _config_key(env_var)
        if len(key) == 1:
            top[key[0]] = value
        else:
            solvers_tbl[key[1]] = value
    lines = []
    for k in sorted(top):
        if isinstance(top[k], str):
            lines.append(f'{k} = "{top[k].replace(os.sep, "/")}"')
    if solvers_tbl:
        lines.append("")
        lines.append("[solvers]")
        for k in sorted(solvers_tbl):
            if isinstance(solvers_tbl[k], str):
                lines.append(f'{k} = "{solvers_tbl[k].replace(os.sep, "/")}"')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    _cache.clear()                                   # next load() re-reads
    return path
