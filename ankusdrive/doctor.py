"""``ankusdrive doctor`` — one cross-platform setup/health report.

Resolves FreeCAD **and** every solver/renderer family and, per item, reports
found/missing, the resolved path (or the reason it didn't resolve), and the exact
fix. Replaces the "set 24 env vars" mental model with a single guided checklist.

Side-effect-free by construction: the solver half reuses
``solvers.capabilities()`` (no execution, no env mutation) and the FreeCAD half
reuses ``client._resolve_freecadcmd()``. A FreeCAD *boot* is attempted only to read
back the version, is time-boxed, and degrades to "resolved but unverified" — so
``doctor`` still produces a report on a box where FreeCAD is installed but broken.

Consumed by the CLI (``cmd_doctor``); returns plain dicts so it can also back an
agent-guided setup surface (issue #196) and ``--json`` for CI preflight.
"""
from __future__ import annotations

import os
import platform

from pathlib import Path

from . import solvers
from .client import (
    _DEFAULT_FREECADCMD_CANDIDATES,
    _freecadcmd_candidates,
    _resolve_freecadcmd,
)


_REPO_URL = "https://github.com/gchen19/AnkusDrive"


def _installer_scripts_location() -> str:
    """Where the ``scripts/install-solvers.*`` named by the fix hints actually lives.

    The hints spell a *repo-relative* path, which is correct from a clone and a dead
    end for everyone else: the installers are not package data, so a
    ``pip install ankusdrive`` never puts them on disk and the advice reads as a
    missing file. Resolve the real directory when we are running out of a checkout,
    and hand out the URL when we are not.
    """
    local = Path(__file__).resolve().parent.parent / "scripts"
    if (local / "install-solvers.sh").is_file():
        return str(local)
    return f"{_REPO_URL}/tree/main/scripts"


def _freecad_fix_hint() -> str:
    """OS-specific guidance when FreeCAD does not resolve."""
    sys = platform.system()
    if sys == "Windows":
        return (
            "install FreeCAD 1.1.x from https://www.freecad.org/ (default lands in "
            r"'C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe'); if installed to a "
            "non-standard location set ANKUSDRIVE_FREECADCMD to the freecadcmd.exe path."
        )
    if sys == "Darwin":
        return (
            "install FreeCAD 1.1.x from https://www.freecad.org/ (drag to "
            "/Applications); for a non-standard location set ANKUSDRIVE_FREECADCMD to the "
            "freecadcmd path inside the .app (Contents/Resources/bin/freecadcmd)."
        )
    return (
        "install FreeCAD 1.1.x (distro package, AppImage, or https://www.freecad.org/) "
        "and ensure `freecadcmd` is on PATH, or set ANKUSDRIVE_FREECADCMD to its path."
    )


def _resolution_source() -> str:
    """Which resolution layer produced the FreeCAD path: env override, the config
    file, PATH, or an auto-discovered default install location. Mirrors
    _resolve_freecadcmd's order (issue #199)."""
    from . import config as _config
    _, src = _config.lookup("ANKUSDRIVE_FREECADCMD")
    if src == "env":
        return "env (ANKUSDRIVE_FREECADCMD)"
    if src == "config":
        return f"config ({_config.config_path()})"
    import shutil

    from .client import _FREECADCMD_NAMES
    if any(shutil.which(n) for n in _FREECADCMD_NAMES):
        return "PATH"
    return "auto (default install location)"


def config_report() -> dict:
    """The persistent-config layer's state: where the file is expected, whether it
    exists, and which ANKUSDRIVE_* equivalents it currently supplies (issue #199)."""
    from . import config as _config
    path = _config.config_path()
    data = _config.load()
    keys = []
    if isinstance(data.get("freecadcmd"), str):
        keys.append("freecadcmd")
    solvers_tbl = data.get("solvers")
    if isinstance(solvers_tbl, dict):
        keys += [f"solvers.{k}" for k in sorted(solvers_tbl)]
    # Anything still resolving through a pre-rename shim (#295) is reported here
    # rather than left to a one-line startup warning the user scrolls past: the
    # shims go away in 0.6, and `doctor` is where someone looks to find out what
    # on their machine is not yet migrated.
    legacy_env = _config.adopted_legacy_env()
    legacy_config = os.path.basename(os.path.dirname(path)) == "driftpin"
    return {"path": path, "present": os.path.isfile(path), "keys": keys,
            "legacy_env": legacy_env, "legacy_config": legacy_config}


def freecad_report(probe_version: bool = True, boot_timeout: float = 20.0) -> dict:
    """Resolve FreeCAD and, if ``probe_version``, boot it once to read the version.

    Returns ``{available, path, exists, source, version?, fix?, error?}``.
    ``available`` is True only when the binary exists on disk; ``version`` is filled
    when the boot succeeds, else ``error`` carries why it couldn't be verified."""
    path = _resolve_freecadcmd()
    exists = bool(path) and os.path.isfile(path)
    report = {
        "available": exists,
        "path": path,
        "exists": exists,
        "source": _resolution_source() if exists else None,
        "candidates_checked": _freecadcmd_candidates()
        or list(_DEFAULT_FREECADCMD_CANDIDATES.get(platform.system(), ())),
    }
    if not exists:
        report["fix"] = _freecad_fix_hint()
        return report
    if probe_version:
        # Boot the worker once purely to confirm FreeCAD actually runs and read its
        # version. Time-boxed; any failure downgrades to "resolved but unverified"
        # rather than aborting the whole report.
        try:
            from .client import Worker
            w = Worker(boot_timeout=boot_timeout)
            try:
                report["version"] = ".".join(w.freecad_version)
            finally:
                w.shutdown()
        except Exception as e:  # noqa: BLE001 — doctor must never crash on a bad boot
            report["error"] = f"resolved but did not boot: {type(e).__name__}: {e}"
    return report


def build_report(probe_version: bool = True) -> dict:
    """Full doctor payload: platform, FreeCAD, and the solver capabilities dict."""
    return {
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "freecad": freecad_report(probe_version=probe_version),
        "config": config_report(),
        "solvers": solvers.capabilities(),
    }


# --- human-readable rendering --------------------------------------------------

_MARK = {"ok": "[ok]  ", "unwired": "[warn]", "absent": "[--]  ", "missing": "[XX]  "}


def _fmt_freecad(fc: dict) -> list[str]:
    lines = []
    if fc.get("version"):
        lines.append(f"{_MARK['ok']} FreeCAD {fc['version']}  {fc['path']}")
        lines.append(f"        source: {fc['source']}")
    elif fc.get("exists"):
        lines.append(f"{_MARK['unwired']} FreeCAD (unverified)  {fc['path']}")
        if fc.get("error"):
            lines.append(f"        {fc['error']}")
    else:
        lines.append(f"{_MARK['missing']} FreeCAD not found")
        lines.append(f"        fix: {fc['fix']}")
    return lines


def _resolved_label(state: dict) -> str:
    """How a ready solver should be named in the ``ready via …`` line. For a wheel
    whose resolved module differs from the registry key (topology's "topopt" entry
    satisfied by solidspy), name the package that actually resolved; otherwise the
    entry name (binaries keep their key, e.g. su2/calculix)."""
    name = state["name"]
    module = state.get("module")
    if module and module != name:
        return module
    return name


def _fmt_solvers(caps: dict) -> list[str]:
    lines = []
    families = caps["families"]
    for fam in sorted(families):
        info = families[fam]
        solver_states = {s: caps["solvers"][s] for s in info["solvers"]}
        if info["any_available"]:
            # A wheel family can be satisfied by a package whose name differs from
            # the registry key (e.g. topology's "topopt" entry resolves via solidspy
            # when topopt itself is absent). Report the package that actually
            # resolved, not the entry name — "ready via topopt" while topopt is not
            # installed is exactly the stale hint this doctor exists to avoid.
            # Only the solvers that made it ready get named: one AnkusDrive cannot
            # build a case for (SU2, issue #237) resolves without contributing, and
            # "ready via su2" would send the reader back to a solver that can only
            # run a case_dir they wrote themselves.
            drivable = [s for s in sorted(info["available"])
                        if not solver_states[s].get("prepared_case_only")]
            ready = ", ".join(_resolved_label(solver_states[s]) for s in drivable)
            # in-substrate resolutions (issue #193) don't run natively: they launch
            # through `wsl -e bash` (Windows) or `multipass exec` (macOS), so say so
            # — "ready" on a box with no local OpenFOAM is otherwise baffling.
            via = {solver_states[s].get("via") for s in drivable}
            substrate = (" (in WSL)" if "wsl" in via else
                         " (in Multipass VM)" if "multipass" in via else "")
            lines.append(f"{_MARK['ok']} {fam:<16} ready via {ready}{substrate}")
            continue
        # nothing ready: prefer the unwired (installed-but-not-wired) hint if present,
        # else the install hint of the first solver in the family.
        if info["unwired"]:
            name = info["unwired"][0]
            st = solver_states[name]
            lines.append(f"{_MARK['unwired']} {fam:<16} unwired ({name})")
            lines.append(f"        found: {st.get('found_at')}")
            lines.append(f"        fix:   {st.get('wire_hint')}")
        else:
            name = info["solvers"][0]
            st = solver_states[name]
            lines.append(f"{_MARK['absent']} {fam:<16} absent")
            lines.append(f"        fix:   {st.get('install_hint')}")
        # A solver can RESOLVE and still not make the family usable: nothing in
        # AnkusDrive builds a case for it, so only a hand-prepared case_dir reaches it
        # (SU2, issue #237). It is deliberately not counted as ready above, but
        # saying nothing would make "cfd unwired" baffling next to an SU2_CFD the
        # user just installed — so name it and say what it can still do.
        for name in info.get("prepared_case_only", ()):
            st = solver_states[name]
            lines.append(f"        note:  {name} resolves ({st.get('path') or st.get('module')})")
            lines.append(f"               but {st['prepared_case_only']}")
    return lines


def render(report: dict) -> str:
    """Render the report dict as a human-readable checklist."""
    plat = report["platform"]
    caps = report["solvers"]
    out = [f"platform: {plat['system']} ({plat['machine']})", ""]
    out += _fmt_freecad(report["freecad"])
    cfg = report.get("config")
    if cfg:
        state = ("supplies: " + ", ".join(cfg["keys"])
                 if cfg["present"] and cfg["keys"]
                 else ("present, no keys set" if cfg["present"] else "not present"))
        out.append(f"config: {cfg['path']} ({state})")
        if cfg.get("legacy_config"):
            out.append("  DEPRECATED: that is the pre-rename config location. The next "
                       "`ankusdrive setup` migrates it; removed in 0.6.")
        if cfg.get("legacy_env"):
            out.append("  DEPRECATED: reading %d pre-rename env var(s) — %s. Rename them "
                       "to ANKUSDRIVE_*; removed in 0.6." %
                       (len(cfg["legacy_env"]), ", ".join(cfg["legacy_env"])))
    out.append("")
    out.append("Solver families:")
    out += _fmt_solvers(caps)
    out.append("")
    fam = caps["families"]
    ready = sum(1 for f in fam.values() if f["any_available"])
    unwired = sum(1 for f in fam.values() if not f["any_available"] and f["unwired"])
    absent = len(fam) - ready - unwired
    if ready < len(fam):
        # The fix hints above name scripts/install-solvers.{sh,ps1}; say where that is
        # for the pip-installed case, where no such path exists locally.
        out.append(f"install-solvers scripts: {_installer_scripts_location()}")
        out.append("")
    out.append(f"{ready} families ready | {unwired} unwired | {absent} absent")
    return "\n".join(out)
