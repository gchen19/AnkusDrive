"""Whether the MCP ``run_script`` tool may execute agent-supplied code (#378).

``run_script`` runs arbitrary Python inside the FreeCAD worker with full filesystem
and process access. Locally that is a deliberate escape hatch; in a directory-listed
extension it reads as a way around the host's guardrails, which Anthropic's Software
Directory Policy rules out. So it is a switch, ``ANKUSDRIVE_ALLOW_RUN_SCRIPT``
(env, then ``allow_run_script`` in config.toml):

* **unset** — allowed. pip / pipx / uvx / clone installs keep today's behaviour; the
  switch exists for installs that opt out, and the change is recorded in CHANGELOG.
* ``true`` / ``1`` / ``yes`` / ``on`` — allowed; ``false`` / ``0`` / ``no`` / ``off``
  — not. Anything else raises at server start: a typo must not silently re-enable
  code execution, or silently disable it.
* **The Claude Desktop extension** declares it as an install-dialog toggle that
  defaults to **off**, and its launcher treats a toggle the host did not pass as off.

When not allowed, the tool is not registered (no tokens, nothing to call), and
``setup_status`` / ``doctor`` report it with the switch that enables it. As a second
line, the worker refuses any ``run_script`` call tagged ``origin="mcp"`` with a
structured result. Package-internal callers — feature templates building geometry,
and the user's own ``ankusdrive run script.py`` — pass no origin and are unaffected:
their code is the package's or the user's, not an agent's.
"""
from __future__ import annotations

ENV = "ANKUSDRIVE_ALLOW_RUN_SCRIPT"
TOOL = "run_script"
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def allowed(value: str | None) -> bool:
    """Parse a raw switch value. Unset/blank -> True. Raises ValueError on junk."""
    raw = (value or "").strip().lower()
    if not raw:
        return True
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{ENV}={value!r} is not a boolean; use true or false")


def current() -> bool:
    from ankusdrive import config as _config
    return allowed(_config.get(ENV))


def enable_hint() -> str:
    from ankusdrive import install_kind as _ik
    if _ik.kind() == "mcpb":
        return ("In Claude Desktop: Settings -> Extensions -> AnkusDrive -> configure, turn on "
                "'Allow run_script (execute agent-written Python)', then restart the extension.")
    return (f"Set {ENV}=true in the MCP server's environment, or `allow_run_script = \"true\"` "
            "in config.toml, then restart the server.")


def refusal() -> dict:
    """The structured result for a refused call — the require_solver() shape, never a raise."""
    return {"ok": False, "status": "disabled",
            "reason": "run_script is disabled in this install: it executes agent-written Python "
                      "with full file and process access",
            "enable": enable_hint()}


def report() -> dict:
    """``{allowed, enable?}`` — or ``{error}`` for an invalid switch value — for doctor."""
    try:
        ok = current()
    except ValueError as e:
        return {"allowed": False, "error": str(e)}
    return {"allowed": True} if ok else {"allowed": False, "enable": enable_hint()}


def apply(mcp) -> dict:
    """Unregister the ``run_script`` tool from ``mcp`` when not allowed. Returns report()."""
    if not current():
        mcp._tool_manager._tools.pop(TOOL, None)
    return report()
