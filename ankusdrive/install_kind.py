"""How this AnkusDrive was installed, and the pip advice that is true for it (#347).

Every "install the X extra" hint used to read ``pip install 'ankusdrive[X]'``. That
is right for a venv or a clone and wrong for everything else AnkusDrive now ships
as:

  * ``mcpb``    — the Claude Desktop extension. The host builds a uv-managed
                  ``.venv`` inside its extensions directory from a pyproject that
                  pins bare ``ankusdrive==X``; nothing can be added to it, and a
                  reinstall re-creates it.
  * ``uvx``     — an ephemeral environment in uv's cache (the MCP Registry's
                  ``runtimeHint``). Extras are chosen in the launch command.
  * ``uv_tool`` — ``uv tool install``; extras are chosen at (re)install time.
  * ``pipx``    — a pipx venv; ``pip`` in the user's shell is some other Python,
                  ``pipx runpip ankusdrive install …`` is this one.
  * ``venv``    — anything else: plain ``pip install`` into the interpreter running
                  ``ankusdrive`` is correct, so hints are left exactly as written.

A shell ``pip install`` in the first four installs into a different Python, the
family stays missing, and nothing says why — advice that assumed an install layout
it never checked.

Detection: ``ANKUSDRIVE_INSTALL_KIND`` first (the MCPB launcher sets it, and the
client forwards the host's answer to the FreeCAD worker, whose own ``sys.prefix``
is FreeCAD's and says nothing about the install), then the shape of ``sys.prefix``.
An unrecognised env value is reported, not silently accepted or dropped.

Pure stdlib, FreeCAD-free.
"""
from __future__ import annotations

import os
import re
import sys

ENV = "ANKUSDRIVE_INSTALL_KIND"
KINDS = ("mcpb", "uvx", "uv_tool", "pipx", "venv")

# Matched against sys.prefix with backslashes folded to "/", so one table covers
# macOS/Linux and Windows (%APPDATA%\Claude\Claude Extensions, %LOCALAPPDATA%\uv\
# cache\archive-v0, %APPDATA%\uv\tools, %USERPROFILE%\pipx\venvs). Ordered: the MCPB
# extension dir is checked before the uv markers, since its .venv is uv-built.
_PREFIX_MARKERS = (
    ("mcpb", "/Claude Extensions/"),
    ("uvx", "/archive-v0/"),
    ("uv_tool", "/uv/tools/"),
    ("pipx", "/pipx/venvs/"),
)

# A hint this module may rewrite starts with a bare `pip install `. Anything else
# (apt, brew, a source build, `.venv-bempp/bin/pip install …` into a dedicated venv)
# is not about this interpreter and passes through untouched.
_PIP = re.compile(r"(?<![\w./\\-])pip install ")
_EXTRA = re.compile(r"ankusdrive\[([^\]]+)\]")
_NOTE = re.compile(r"ankusdrive\[[^\]]+\]'?\s+\((.*?)\)(?=,|$)")


def detect(environ=None, prefix: str | None = None) -> dict:
    """``{kind, source}`` for this process. ``source`` is ``"env"``, ``"prefix"``, or
    ``"default"``; an unrecognised ``ANKUSDRIVE_INSTALL_KIND`` falls through to
    prefix detection and is echoed back as ``ignored_env``."""
    env = os.environ if environ is None else environ
    out: dict = {}
    raw = (env.get(ENV) or "").strip()
    if raw in KINDS:
        return {"kind": raw, "source": "env"}
    if raw:
        out["ignored_env"] = raw
    p = (sys.prefix if prefix is None else prefix).replace("\\", "/") + "/"
    for kind, marker in _PREFIX_MARKERS:
        if marker in p:
            return {"kind": kind, "source": "prefix", **out}
    return {"kind": "venv", "source": "default", **out}


def kind(environ=None, prefix: str | None = None) -> str:
    return detect(environ, prefix)["kind"]


def adapt(hint: str, environ=None, prefix: str | None = None) -> str:
    """The install hint rewritten for this install kind. Non-pip hints, and every
    hint under ``venv``, are returned unchanged."""
    if not hint or not hint.startswith("pip install "):
        return hint
    k = kind(environ, prefix)
    if k == "venv":
        return hint
    if k == "pipx":
        return _PIP.sub("pipx runpip ankusdrive install ", hint)

    m = _EXTRA.search(hint)
    note = _NOTE.search(hint)
    note = f"  ({note.group(1)})" if note else ""
    if not m:
        # Not an extra: a dependency of ankusdrive itself (the mcp pin). In the three
        # managed kinds it comes from the package metadata, so the fix is a reinstall.
        return {
            "mcpb": "update or reinstall the AnkusDrive extension in Claude Desktop "
                    "(its environment is rebuilt from the pinned release)",
            "uvx": "relaunch with `uvx --refresh ankusdrive mcp` to rebuild the "
                   "cached environment",
            "uv_tool": "uv tool install --reinstall ankusdrive",
        }[k]
    extra = m.group(1)
    if k == "mcpb":
        return (f"not installable into the Claude Desktop extension: its environment is "
                f"managed and pinned by the host, and a reinstall re-creates it. To use "
                f"this family, install AnkusDrive with pipx instead — "
                f"pipx install 'ankusdrive[{extra}]' — and register that command in "
                f"place of the extension (`ankusdrive setup --print-mcp-config` prints "
                f"it){note}")
    if k == "uvx":
        return (f"this server runs from an ephemeral uvx environment, so nothing can be "
                f"installed into it: change the MCP host command to "
                f"uvx --from 'ankusdrive[{extra}]' ankusdrive mcp  (list every extra "
                f"you want in the one bracket, comma-separated){note}")
    return (f"uv tool install --reinstall 'ankusdrive[{extra}]'  (a reinstall keeps "
            f"only the extras it names — list every one you want, comma-separated)"
            f"{note}")


def worker_env(environ=None) -> dict:
    """The environment for a child process that must see the host's install kind
    (the FreeCAD worker). Pins the detected kind unless the caller already set a
    valid one."""
    env = dict(os.environ if environ is None else environ)
    if env.get(ENV, "").strip() not in KINDS:
        env[ENV] = kind(env)
    return env
