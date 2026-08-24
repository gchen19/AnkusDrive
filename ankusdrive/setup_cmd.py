"""``ankusdrive setup`` — the interactive provisioner tying the three onboarding
concerns together (issue #200): FreeCAD location, opt-in pip-wheel solver
extras, and MCP host wiring — persisting choices to the config file (#199)
instead of 24 env vars.

Scope discipline (from the epic #196): the pip-wheel families install here,
into **this interpreter** (the one that runs ``ankusdrive mcp`` — the discovery
contract requires that); the source-build / system solvers (OpenFOAM, Elmer,
YADE, openEMS, preCICE) are never installed by this command — ``ankusdrive
doctor`` prints their exact per-item fix instead.

``--print-mcp-config`` (issue #201) emits just the registration block: the
``mcpServers`` JSON with the **resolved absolute path** to the launcher (pipx
installs to ``~/.local/bin``, which a GUI host's PATH may not carry), the
``claude mcp add`` one-liner, and the ``uvx`` variant for once-on-PyPI.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from . import config

# The pip-wheel extras a plain `pip install ankusdrive[<extra>]` provisions —
# label -> (extra name, what it unlocks). Mirrors pyproject's optional deps.
_WHEEL_EXTRAS = (
    ("mbd", "PyBullet — rigid-body/mechanism simulation"),
    ("topology", "solidspy — topology optimization"),
    ("optics", "optiland/rayoptics — lens design + sequential tracing"),
    ("fluids", "CoolProp — fluid properties f(T,P)"),
)


def _launcher() -> list:
    """How to start the MCP server on THIS machine, as an argv list: the
    ``ankusdrive`` entry-point script when it resolves (absolute path — GUI hosts
    don't inherit shell PATH), else ``<this python> -m ankusdrive``."""
    exe = shutil.which("ankusdrive")
    if not exe:
        # entry-point script next to the current interpreter (venv/pipx layout)
        cand = os.path.join(os.path.dirname(sys.executable),
                            "ankusdrive.exe" if os.name == "nt" else "ankusdrive")
        if os.path.isfile(cand):
            exe = cand
    if exe:
        return [os.path.abspath(exe), "mcp"]
    return [sys.executable, "-m", "ankusdrive", "mcp"]


def mcp_config_text() -> str:
    """The ready-to-paste MCP registration block (#201): mcpServers JSON with
    the resolved absolute launcher, the `claude mcp add` one-liner, and the uvx
    variant (usable once the package is on PyPI)."""
    argv = _launcher()
    snippet = {"mcpServers": {"ankusdrive": {"command": argv[0], "args": argv[1:]}}}
    lines = [
        "Paste into your MCP host's config (Claude Desktop's",
        "claude_desktop_config.json, Cursor, etc.):",
        "",
        json.dumps(snippet, indent=2),
        "",
        "Claude Code one-liner:",
        f"  claude mcp add ankusdrive -- {' '.join(argv)}",
        "",
        "Zero-install variant (once ankusdrive is on PyPI):",
        '  "command": "uvx", "args": ["ankusdrive", "mcp"]',
    ]
    return "\n".join(lines)


def _ask(prompt: str, default: str = "") -> str:
    """input() with a default; returns the default on EOF (piped stdin)."""
    try:
        got = input(prompt).strip()
    except EOFError:
        got = ""
    return got or default


def _resolve_freecad(assume_yes: bool, explicit: str | None):
    """FreeCAD step: report what resolves (via doctor, no boot), let the user
    confirm or supply a path unless --yes, and return the path to persist
    (None = resolved-from-auto and user accepted; nothing to persist... except
    persisting makes it survive host launches, so a confirmed path IS written)."""
    from . import doctor
    if explicit:
        if not os.path.isfile(explicit):
            print(f"  warning: {explicit} does not exist", file=sys.stderr)
        return explicit
    rep = doctor.freecad_report(probe_version=False)
    if rep["exists"]:
        print(f"FreeCAD:  found {rep['path']}  (source: {rep['source']})")
        if assume_yes:
            return rep["path"]
        ans = _ask("          use this? [Y/n or path] ", "y")
        if ans.lower() in ("y", "yes"):
            return rep["path"]
        if ans.lower() in ("n", "no"):
            return _ask("          freecadcmd path: ") or None
        return ans                                    # a path was typed
    print("FreeCAD:  NOT FOUND")
    print(f"          {rep['fix']}")
    if assume_yes:
        return None
    return _ask("          freecadcmd path (Enter to skip): ") or None


def _pick_extras(assume_yes: bool, requested: list | None) -> list:
    """Which pip-wheel extras to install. --extras wins; interactive shows the
    menu; --yes with no --extras installs nothing (explicit opt-in only)."""
    from . import solvers
    caps = solvers.capabilities()
    families = caps["families"]
    print("Optional solver families (pip wheels — installed into THIS interpreter,")
    print(f"  {sys.executable}):")
    installed = set()
    for extra, blurb in _WHEEL_EXTRAS:
        have = families.get(extra, {}).get("any_available") or \
            (extra == "fluids" and families.get("fluids", {}).get("any_available"))
        mark = "x" if have else " "
        if have:
            installed.add(extra)
        print(f"  [{mark}] {extra:<9} {blurb}")
    if requested is not None:
        picks = [e.strip() for e in requested if e.strip()]
    elif assume_yes:
        picks = []
    else:
        raw = _ask("→ extras to install (comma-separated, Enter for none): ")
        picks = [e.strip() for e in raw.split(",") if e.strip()]
    known = {e for e, _ in _WHEEL_EXTRAS}
    bad = [e for e in picks if e not in known]
    if bad:
        print(f"  unknown extras skipped: {', '.join(bad)} (known: {', '.join(sorted(known))})")
    return [e for e in picks if e in known and e not in installed]


def _install_extras(extras: list) -> None:
    """pip-install the chosen extras into this interpreter. From a repo checkout
    the editable path is used (the package isn't on PyPI yet); a failed extra is
    a warning — its family degrades cleanly."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    editable = os.path.isfile(os.path.join(repo, "pyproject.toml"))
    for extra in extras:
        spec = f"{repo}[{extra}]" if editable else f"ankusdrive[{extra}]"
        argv = [sys.executable, "-m", "pip", "install", "--quiet",
                *(["-e"] if editable else []), spec]
        print(f"  installing [{extra}] ...")
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode == 0:
            print(f"  [{extra}] installed")
        else:
            tail = (proc.stderr or proc.stdout or "")[-400:]
            print(f"  [{extra}] FAILED (family degrades cleanly): {tail}",
                  file=sys.stderr)


def run_setup(assume_yes: bool = False, extras: list | None = None,
              freecadcmd: str | None = None) -> int:
    """The interactive (or --yes scripted) provision flow. Returns an exit code."""
    fc_path = _resolve_freecad(assume_yes, freecadcmd)
    to_persist = {}
    if fc_path:
        to_persist["ANKUSDRIVE_FREECADCMD"] = fc_path

    picks = _pick_extras(assume_yes, extras)
    if picks:
        _install_extras(picks)

    if to_persist:
        path = config.write(to_persist)
        print(f"Wrote {path}")
    else:
        print(f"Nothing to persist (config stays at {config.config_path()})")

    print()
    print("Source-built / system solver families (OpenFOAM, Elmer, YADE, openEMS,")
    print("preCICE) are NOT installed by this command — run `ankusdrive doctor` for")
    print("the per-family status and the exact install command for this OS.")
    print()
    print(mcp_config_text())
    return 0
