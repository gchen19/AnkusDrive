"""MCP Registry contract (issue #201) — server.json must describe the release it ships with.

AnkusDrive is listed on the official MCP Registry as ``io.github.gchen19/ankusdrive``.
``publish.yml`` publishes ``server.json`` right after the PyPI upload on every
``v*`` tag, and the registry accepts it only if three things line up — none of
which fails anywhere *before* that tag is pushed, at which point the PyPI
version is already burned:

  * **version** — ``server.json`` ``version`` and ``packages[0].version`` both
    equal ``ankusdrive.__version__``. A release PR that bumps ``__init__.py`` and
    forgets this file would publish a registry entry pointing at the previous
    release, or fail the job after PyPI has the new one.
  * **ownership** — PyPI ownership is proven by an ``mcp-name: <name>`` token in
    the README as uploaded, followed by a boundary (whitespace or ``-->``). Delete
    that comment in a README tidy-up and every later publish is refused.
  * **identity** — the package identifier is the PyPI project name, the launch
    argument is the real stdio subcommand, the env var is one the client actually
    reads, and the description fits the registry's 100-character limit.

Like ``test_naming.py`` this imports NEITHER the package nor any third-party
module: it reads files as text. Pure stdlib, milliseconds, no FreeCAD.

Run:  python3 tests/test_mcp_registry.py
"""
import json
import re
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NAME = "io.github.gchen19/ankusdrive"


def _server():
    return json.loads((REPO / "server.json").read_text(encoding="utf-8"))


def _package():
    pkgs = _server()["packages"]
    assert len(pkgs) == 1, f"expected exactly one package entry, found {len(pkgs)}"
    return pkgs[0]


def _pkg_version():
    src = (REPO / "ankusdrive" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', src, re.M)
    assert m, "no __version__ line in ankusdrive/__init__.py"
    return m.group(1)


def test_versions_match_the_package():
    want = _pkg_version()
    s = _server()
    assert s["version"] == want, \
        f"server.json version {s['version']!r} != __version__ {want!r} — bump both together"
    assert _package()["version"] == want, \
        f"server.json packages[0].version {_package()['version']!r} != __version__ {want!r}"


def test_readme_proves_ownership():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    # The registry's rule: the token must be followed by a boundary, so a name
    # glued to punctuation ("…/ankusdrive.") does not count. Mirror it exactly.
    hits = re.findall(r"mcp-name:\s*(\S+?)(?=\s|-->|<|$)", readme)
    assert hits, "README.md has no `mcp-name:` token — the registry will refuse the publish"
    assert set(hits) == {NAME}, f"README mcp-name token(s) {hits} != {NAME!r}"
    assert _server()["name"] == NAME, f"server.json name {_server()['name']!r} != {NAME!r}"


def test_readme_is_the_uploaded_readme():
    """The token only proves anything if README.md is what PyPI receives."""
    toml = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^readme\s*=\s*"README\.md"', toml, re.M), \
        "pyproject.toml no longer uploads README.md — the mcp-name token would not reach PyPI"


def test_package_identity():
    toml = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    project = re.search(r'^name\s*=\s*"([^"]+)"', toml, re.M).group(1)
    pkg = _package()
    assert pkg["registryType"] == "pypi"
    assert pkg["identifier"] == project, f"identifier {pkg['identifier']!r} != PyPI name {project!r}"
    assert pkg["transport"] == {"type": "stdio"}
    args = [a.get("value") for a in pkg.get("packageArguments", [])]
    assert args == ["mcp"], f"launch args {args} — the stdio server is `ankusdrive mcp`"
    assert re.search(r'^ankusdrive\s*=\s*"ankusdrive\.cli:main"', toml, re.M), \
        "the `ankusdrive` console script moved — uvx would no longer launch the server"


def test_env_vars_are_real():
    """An advertised variable the code never reads is a silent no-op for the user."""
    client = (REPO / "ankusdrive" / "client.py").read_text(encoding="utf-8")
    for var in _package().get("environmentVariables", []):
        assert f'"{var["name"]}"' in client, \
            f"server.json advertises {var['name']} but ankusdrive/client.py never reads it"
        assert var.get("isRequired") is False, \
            f"{var['name']} marked required — FreeCAD is auto-discovered, nothing is mandatory"


def test_description_fits():
    d = _server()["description"]
    assert 1 <= len(d) <= 100, f"description is {len(d)} chars; the registry schema caps it at 100"


def test_the_checks_can_fail():
    """Guard the guard: the ownership regex must reject what the registry rejects."""
    pat = re.compile(r"mcp-name:\s*(\S+?)(?=\s|-->|<|$)")
    assert pat.findall(f"<!-- mcp-name: {NAME} -->") == [NAME]
    assert pat.findall(f"mcp-name: {NAME}\n") == [NAME]
    assert pat.findall(f"see mcp-name: {NAME}.") != [NAME], "trailing punctuation must not match"


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
            print(f"  FAIL {name:44s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:44s} ({time.time() - t0:.2f}s)")
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
