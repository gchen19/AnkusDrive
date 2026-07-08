"""``driftpin doctor`` — one cross-platform setup/health report.

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

from . import solvers
from .client import (
    _DEFAULT_FREECADCMD_CANDIDATES,
    _freecadcmd_candidates,
    _resolve_freecadcmd,
)


def _freecad_fix_hint() -> str:
    """OS-specific guidance when FreeCAD does not resolve."""
    sys = platform.system()
    if sys == "Windows":
        return (
            "install FreeCAD 1.1.x from https://www.freecad.org/ (default lands in "
            r"'C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe'); if installed to a "
            "non-standard location set DRIFTPIN_FREECADCMD to the freecadcmd.exe path."
        )
    if sys == "Darwin":
        return (
            "install FreeCAD 1.1.x from https://www.freecad.org/ (drag to "
            "/Applications); for a non-standard location set DRIFTPIN_FREECADCMD to the "
            "freecadcmd path inside the .app (Contents/Resources/bin/freecadcmd)."
        )
    return (
        "install FreeCAD 1.1.x (distro package, AppImage, or https://www.freecad.org/) "
        "and ensure `freecadcmd` is on PATH, or set DRIFTPIN_FREECADCMD to its path."
    )


def _resolution_source() -> str:
    """Which resolution layer produced the FreeCAD path: env override, PATH, or an
    auto-discovered default install location. Mirrors _resolve_freecadcmd's order."""
    if os.environ.get("DRIFTPIN_FREECADCMD"):
        return "env (DRIFTPIN_FREECADCMD)"
    import shutil

    from .client import _FREECADCMD_NAMES
    if any(shutil.which(n) for n in _FREECADCMD_NAMES):
        return "PATH"
    return "auto (default install location)"


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


def _fmt_solvers(caps: dict) -> list[str]:
    lines = []
    families = caps["families"]
    for fam in sorted(families):
        info = families[fam]
        solver_states = {s: caps["solvers"][s] for s in info["solvers"]}
        if info["any_available"]:
            ready = ", ".join(sorted(info["available"]))
            lines.append(f"{_MARK['ok']} {fam:<16} ready via {ready}")
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
    return lines


def render(report: dict) -> str:
    """Render the report dict as a human-readable checklist."""
    plat = report["platform"]
    caps = report["solvers"]
    out = [f"platform: {plat['system']} ({plat['machine']})", ""]
    out += _fmt_freecad(report["freecad"])
    out.append("")
    out.append("Solver families:")
    out += _fmt_solvers(caps)
    out.append("")
    fam = caps["families"]
    ready = sum(1 for f in fam.values() if f["any_available"])
    unwired = sum(1 for f in fam.values() if not f["any_available"] and f["unwired"])
    absent = len(fam) - ready - unwired
    out.append(f"{ready} families ready | {unwired} unwired | {absent} absent")
    return "\n".join(out)
