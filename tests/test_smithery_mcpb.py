"""Smithery stopgap contract (#201) — the Smithery copy of a bundle differs in exactly the
two fields Smithery cannot take, and in nothing else.

``scripts/smithery_mcpb.py`` exists only until Smithery accepts MCPB ``server.type:
"uv"`` (smithery-ai/cli#801). It rewrites a released ``.mcpb`` so ``smithery mcp
publish`` accepts it: ``server.type`` uv -> python, and the sample ``tools`` dropped
(Smithery forwards them as a ServerCard that requires ``inputSchema``, #805). The
relabel is only safe because ``mcp_config`` still launches through ``uv run`` — so
the assertions that matter most are the ones about what must NOT change.

Loads the script by path; stdlib only; no node, no network, no FreeCAD.

Run:  python3 tests/test_smithery_mcpb.py
"""
import importlib.util
import json
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location("smithery_mcpb", REPO / "scripts" / "smithery_mcpb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest():
    return json.loads((REPO / "mcpb" / "manifest.json").read_text(encoding="utf-8"))


def test_relabel_changes_exactly_the_blocked_fields():
    src = _manifest()
    out = _mod().relabel(src)
    assert out["server"]["type"] == "python", out["server"]
    assert "tools" not in out, "sample tools would be forwarded as a ServerCard without inputSchema (#805)"
    assert out["tools_generated"] is True
    assert out["server"]["mcp_config"] == src["server"]["mcp_config"], \
        "mcp_config changed — the relabel is only safe while Smithery still launches via `uv run`"
    assert out["server"]["entry_point"] == src["server"]["entry_point"]
    assert out["long_description"].startswith("**Requires `uv` on PATH**"), out["long_description"][:80]
    assert out["long_description"].endswith(src["long_description"])
    untouched = set(src) - {"server", "tools", "long_description", "tools_generated"}
    changed = [k for k in untouched if out.get(k) != src[k]]
    assert not changed, f"relabel touched {changed}"
    assert src["server"]["type"] == "uv" and "tools" in src, "relabel mutated its input"


def test_refuses_what_it_is_not_for():
    m = _mod()
    src = _manifest()
    for bad, why in ((m.relabel(src), "already relabelled"),
                     ({**src, "server": {**src["server"], "type": "node"}}, "not a uv bundle"),
                     ({**src, "server": {**src["server"], "mcp_config": {"command": "python", "args": ["src/server.py"]}}},
                      "no longer launched by uv")):
        try:
            m.relabel(bad)
        except ValueError:
            continue
        raise AssertionError(f"relabel accepted a manifest that is {why}")


def test_derive_rewrites_only_the_manifest_entry():
    m = _mod()
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "a.mcpb", Path(tmp) / "a-smithery.mcpb"
        payload = {"manifest.json": json.dumps(_manifest()).encode(),
                   "pyproject.toml": b'dependencies = ["ankusdrive==9.9.9"]\n',
                   "src/server.py": b"print('shim')\n",
                   "icon.png": bytes(range(256)) * 4}
        with zipfile.ZipFile(src, "w") as z:
            for name, data in payload.items():
                z.writestr(name, data)
        m.derive(src, dst)
        with zipfile.ZipFile(dst) as z:
            assert z.namelist() == list(payload), z.namelist()
            for name, data in payload.items():
                if name != "manifest.json":
                    assert z.read(name) == data, f"{name} changed in the Smithery copy"
            assert json.loads(z.read("manifest.json")) == m.relabel(_manifest())


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
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")
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
