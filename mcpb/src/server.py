"""MCPB entry point for AnkusDrive (issue #201).

The bundle ships no AnkusDrive code of its own: ``pyproject.toml`` next to this
file pins the matching PyPI release, the host's ``uv run`` resolves it into an
isolated environment, and this shim starts the stdio server that
``ankusdrive mcp`` starts.

One thing it does before that. MCPB hosts substitute ``${user_config.KEY}`` in
``mcp_config.env`` only for keys that have a value; the reference implementation
(``mcpb/src/shared/config.ts``) leaves the placeholder text in place otherwise.
The manifest gives ``freecadcmd`` an empty default so that should not happen, but
a host that ignores defaults would hand AnkusDrive the literal string
``${user_config.freecadcmd}`` as an explicit FreeCAD path — an explicit override
beats auto-discovery, so every tool would then fail on a path that does not
exist, with nothing saying why. An unsubstituted placeholder is an absence, and
it is treated as one.
"""
import os
import re
import sys

_PLACEHOLDER = re.compile(r"^\$\{[^}]+\}$")


def scrub_unsubstituted(environ) -> list:
    """Remove ``ANKUSDRIVE_*`` variables whose value is an unexpanded ``${...}``
    placeholder or empty, and return the names removed."""
    removed = [
        name for name, value in environ.items()
        if name.startswith("ANKUSDRIVE_") and (not value.strip() or _PLACEHOLDER.match(value.strip()))
    ]
    for name in removed:
        del environ[name]
    return removed


def main() -> None:
    removed = scrub_unsubstituted(os.environ)
    # Announce the install kind before the package reads the environment: this
    # interpreter lives in a host-managed, pinned .venv, so a `pip install` hint is
    # wrong here, and ankusdrive.install_kind rewrites hints on seeing this (#347).
    os.environ["ANKUSDRIVE_INSTALL_KIND"] = "mcpb"
    if removed:
        # stderr only: stdout is the MCP stdio channel.
        print(f"ankusdrive-mcpb: ignoring unset user config {', '.join(sorted(removed))}", file=sys.stderr)
    from ankusdrive.mcp_server import run
    run()


if __name__ == "__main__":
    main()
