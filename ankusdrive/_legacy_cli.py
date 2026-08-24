"""Deprecated ``driftpin`` entry point — the pre-0.5 name of this CLI.

The project was renamed DriftPin -> AnkusDrive (#295). The command came with
it, but the old name is baked into things this package cannot edit: MCP-host
``mcpServers`` blocks, shell aliases, CI steps, and the scripts users wrote
against it. Dropping it outright turns every one of those into
``command not found`` with nothing pointing at the new name.

So ``driftpin`` stays installed for one minor release as a shim that says what
happened and then does exactly what it always did. Deprecated: delete this
module and its ``[project.scripts]`` entry in 0.6.
"""
from __future__ import annotations

import sys


def main() -> int:
    sys.stderr.write(
        "driftpin: renamed to `ankusdrive` in 0.5 — this shim runs it for you, "
        "and goes away in 0.6. Update your MCP host config, scripts and aliases "
        "(see MIGRATION.md).\n"
    )
    from .cli import main as _main
    return _main()


if __name__ == "__main__":
    raise SystemExit(main())
