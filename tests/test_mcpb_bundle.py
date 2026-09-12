"""MCPB bundle contract (issue #201) — mcpb/ must launch the release it ships with.

``scripts/build-mcpb.sh`` packs ``mcpb/`` into ``ankusdrive-<version>.mcpb``, the
one-click install for Claude Desktop and the artifact Smithery distributes. The
bundle carries no AnkusDrive code: ``mcpb/pyproject.toml`` pins the PyPI release
and the host's ``uv run`` resolves it. So everything that can go wrong is a
disagreement between files, and none of it fails before a user installs:

  * **version** — manifest ``version``, bundle ``pyproject`` version, and the exact
    ``ankusdrive==`` pin all equal ``__version__``. A stale pin installs the
    previous release under the new bundle's name.
  * **config** — every ``${user_config.KEY}`` the launch config references is
    declared, optional, and has a default. MCPB hosts leave the placeholder text
    in place for a key with no value, and AnkusDrive would read that text as an
    explicit FreeCAD path (the silent-default class, inverted: a missing value
    arriving as a present one). The shim's scrub is the second line of defence
    and is exercised here directly.
  * **one name, one meaning** — the FreeCAD-path key is ``freecadcmd`` in the
    manifest, in ``config.toml`` and in ``smithery.yaml``; the env var it feeds is
    one ``client.py`` actually reads.
  * **identity** — ``uv`` server type, the entry point exists and is what the args
    run, the listed tools exist, the icon is a 512px PNG, and the description is
    the registry's description rather than a second draft.

Like ``test_mcp_registry.py`` this imports NEITHER the package nor any third-party
module; the shim is loaded by path and its top-level imports are asserted to be
stdlib. Pure stdlib, milliseconds, no FreeCAD, no node.

Run:  python3 tests/test_mcpb_bundle.py
"""
import ast
import importlib.util
import json
import re
import struct
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "mcpb"
PLACEHOLDER = re.compile(r"\$\{user_config\.([^}]+)\}")


def _manifest():
    return json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))


def _pkg_version():
    src = (REPO / "ankusdrive" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__\s*=\s*"([^"]+)"', src, re.M).group(1)


def _shim():
    spec = importlib.util.spec_from_file_location("ankusdrive_mcpb_shim", BUNDLE / "src" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_versions_match_the_package():
    want = _pkg_version()
    assert _manifest()["version"] == want, \
        f"mcpb/manifest.json version {_manifest()['version']!r} != __version__ {want!r}"
    toml = (BUNDLE / "pyproject.toml").read_text(encoding="utf-8")
    ver = re.search(r'^version\s*=\s*"([^"]+)"', toml, re.M).group(1)
    assert ver == want, f"mcpb/pyproject.toml version {ver!r} != __version__ {want!r}"
    pins = re.findall(r'"ankusdrive\s*([=<>!~]=?)\s*([^"]*)"', toml)
    assert pins == [("==", want)], \
        f"mcpb/pyproject.toml must pin exactly ankusdrive=={want}, found {pins}"


def test_launch_config():
    server = _manifest()["server"]
    assert _manifest()["manifest_version"] == "0.4", "the uv server type needs manifest 0.4"
    assert server["type"] == "uv"
    entry = server["entry_point"]
    assert (BUNDLE / entry).is_file(), f"entry_point {entry} does not exist in mcpb/"
    cfg = server["mcp_config"]
    assert cfg["command"] == "uv"
    assert cfg["args"] == ["run", "--directory", "${__dirname}", entry], \
        f"launch args {cfg['args']} do not run the entry point from the bundle directory"
    ignore = (BUNDLE / ".mcpbignore").read_text(encoding="utf-8").split()
    assert ".venv/" in ignore, "a local .venv would be packed into the bundle"


def test_user_config_cannot_arrive_as_placeholder_text():
    m = _manifest()
    declared = m.get("user_config", {})
    referenced = set()
    for value in m["server"]["mcp_config"].get("env", {}).values():
        referenced.update(PLACEHOLDER.findall(value))
    for value in m["server"]["mcp_config"]["args"]:
        referenced.update(PLACEHOLDER.findall(value))
    assert referenced, "the launch config references no user_config — the FreeCAD path is unreachable"
    for key in referenced:
        assert key in declared, f"${{user_config.{key}}} is referenced but not declared"
        opt = declared[key]
        assert opt.get("required") is False, f"user_config.{key} is required — FreeCAD is auto-discovered"
        assert "default" in opt, \
            f"user_config.{key} has no default: a host leaves the placeholder text in place when it is unset"
    assert set(declared) == referenced, f"declared but unused user_config: {set(declared) - referenced}"


def test_shim_scrubs_unexpanded_and_empty_values():
    env = {
        "ANKUSDRIVE_FREECADCMD": "${user_config.freecadcmd}",
        "ANKUSDRIVE_SU2_PATH": "   ",
        "ANKUSDRIVE_ELMER_PATH": "/opt/elmer/bin/ElmerSolver",
        "OTHER": "${user_config.other}",
    }
    removed = _shim().scrub_unsubstituted(env)
    assert sorted(removed) == ["ANKUSDRIVE_FREECADCMD", "ANKUSDRIVE_SU2_PATH"], removed
    assert env == {"ANKUSDRIVE_ELMER_PATH": "/opt/elmer/bin/ElmerSolver", "OTHER": "${user_config.other}"}, env


def test_shim_imports_only_stdlib_at_top_level():
    """The package import must happen inside main(): the scrub has to run before
    ankusdrive/__init__.py reads the environment."""
    tree = ast.parse((BUNDLE / "src" / "server.py").read_text(encoding="utf-8"))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add((node.module or "").split(".")[0])
    assert top <= set(sys.stdlib_module_names), f"non-stdlib top-level imports in the shim: {top - set(sys.stdlib_module_names)}"


def test_one_name_for_the_freecad_path():
    m = _manifest()
    env = m["server"]["mcp_config"]["env"]
    client = (REPO / "ankusdrive" / "client.py").read_text(encoding="utf-8")
    for var, value in env.items():
        assert f'"{var}"' in client, f"bundle sets {var} but ankusdrive/client.py never reads it"
        key = PLACEHOLDER.findall(value)[0]
        # config.toml's key for a top-level ANKUSDRIVE_* var is the name, lowercased, minus the prefix.
        assert key == var.removeprefix("ANKUSDRIVE_").lower(), \
            f"user_config.{key} feeds {var}, whose config.toml key is {var.removeprefix('ANKUSDRIVE_').lower()!r}"
    smithery = (REPO / "smithery.yaml").read_text(encoding="utf-8")
    assert re.search(r"^\s{6}freecadcmd:\s*$", smithery, re.M), "smithery.yaml does not name the key `freecadcmd`"
    assert "config.freecadcmd" in smithery and "freecadCmd" not in smithery, \
        "smithery.yaml still spells the FreeCAD-path key differently"


def test_listed_tools_exist():
    mcp_src = (REPO / "ankusdrive" / "mcp_server.py").read_text(encoding="utf-8")
    for tool in _manifest()["tools"]:
        assert re.search(rf"^def {tool['name']}\(", mcp_src, re.M), f"manifest lists {tool['name']}, which the server does not define"
    assert _manifest()["tools_generated"] is True, "the list is a sample — the server defines 280+"


def test_description_is_the_registry_description():
    server_json = json.loads((REPO / "server.json").read_text(encoding="utf-8"))
    assert _manifest()["description"] == server_json["description"], \
        "mcpb/manifest.json and server.json describe the server differently — keep one sentence"


def test_icon_is_a_512px_png():
    data = (REPO / "logo" / "icon" / "ankusdrive-icon-512.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "icon source is not a PNG"
    w, h = struct.unpack(">II", data[16:24])
    assert (w, h) == (512, 512), f"icon is {w}x{h}; Claude Desktop recommends 512x512"
    build = (REPO / "scripts" / "build-mcpb.sh").read_text(encoding="utf-8")
    assert "logo/icon/ankusdrive-icon-512.png" in build, "build-mcpb.sh no longer stages this icon"


def test_the_checks_can_fail():
    """Guard the guard: the placeholder matcher and the scrub must see what a host leaves behind."""
    assert PLACEHOLDER.findall("${user_config.freecadcmd}") == ["freecadcmd"]
    kept = {"ANKUSDRIVE_FREECADCMD": "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd"}
    assert _shim().scrub_unsubstituted(kept) == [] and len(kept) == 1


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    tests = _discover()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:52s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:52s} ({time.time() - t0:.2f}s)")
    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()
