#!/usr/bin/env python3
"""Derive the Smithery-publishable copy of a released AnkusDrive .mcpb (#201).

STOPGAP — delete this script once Smithery accepts MCPB ``server.type: "uv"``
(smithery-ai/cli#801). Until then ``smithery mcp publish`` refuses the release
bundle twice over:

  1. ``Could not determine bundle runtime from manifest`` — the CLI and the API's
     ``StdioDeployPayload.runtime`` know only node | binary | python | bun.
  2. Relabelled ``python``, the publish then fails ``400 Invalid input`` because the
     manifest's sample ``tools`` (name + description, all MCPB allows) are forwarded
     as a Smithery ServerCard, whose tools require ``inputSchema`` (smithery-ai/cli#805).

So the copy changes exactly two things in ``manifest.json`` and nothing else in the
archive: ``server.type`` "uv" -> "python", and ``tools`` removed (``tools_generated``
stays true — the server lists its 280+ tools at runtime). ``mcp_config`` is untouched:
Smithery's runner executes ``command``/``args`` verbatim (``uv run --directory
${__dirname} src/server.py``), so dependencies are still resolved by uv from the
pinned pyproject — the relabel does not turn this into a vendored-deps python bundle.
What is lost is the host provisioning uv: a Smithery user needs ``uv`` on PATH, which
``long_description`` now says first.

Usage:  scripts/smithery_mcpb.py dist/ankusdrive-X.Y.Z.mcpb   [out.mcpb]
        -> dist/ankusdrive-X.Y.Z-smithery.mcpb
        then: smithery mcp publish <out> -n ankusdrive/ankusdrive
"""
import json
import sys
import zipfile
from pathlib import Path

UV_NOTE = ("**Requires `uv` on PATH** (https://docs.astral.sh/uv/getting-started/installation/) "
           "— this Smithery copy launches with `uv run`, which resolves AnkusDrive's pinned "
           "PyPI release on first start. (Claude Desktop installs the release `.mcpb` from "
           "GitHub and provides uv itself.)\n\n")


def relabel(manifest: dict) -> dict:
    """The Smithery manifest for a release manifest. Refuses anything that is not the
    uv bundle this workaround exists for, so it cannot silently rewrite a bundle that
    has already moved on (or run twice)."""
    server = manifest.get("server", {})
    if server.get("type") != "uv":
        raise ValueError(f"expected a server.type 'uv' bundle, got {server.get('type')!r} — "
                         "nothing to work around (or already relabelled)")
    if server.get("mcp_config", {}).get("command") != "uv":
        raise ValueError("mcp_config.command is not `uv`; the relabel is only safe because "
                         "Smithery runs mcp_config verbatim with uv resolving the deps")
    out = json.loads(json.dumps(manifest))
    out["server"]["type"] = "python"
    out.pop("tools", None)
    out["tools_generated"] = True
    out["long_description"] = UV_NOTE + manifest.get("long_description", "")
    return out


def derive(src: Path, dst: Path) -> None:
    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        if "manifest.json" not in names:
            raise ValueError(f"{src}: no manifest.json at the archive root")
        manifest = json.loads(zin.read("manifest.json"))
        new = relabel(manifest)
        with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
            for info in zin.infolist():
                data = (json.dumps(new, indent=2, ensure_ascii=False) + "\n").encode() \
                    if info.filename == "manifest.json" else zin.read(info.filename)
                zout.writestr(info, data)


def main(argv) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.split("Usage:")[1].strip(), file=sys.stderr)
        return 2
    src = Path(argv[1])
    dst = Path(argv[2]) if len(argv) == 3 else src.with_name(src.stem + "-smithery.mcpb")
    derive(src, dst)
    print(f"built {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
