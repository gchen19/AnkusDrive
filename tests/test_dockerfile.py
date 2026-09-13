"""Introspection-image contract (#201) — the root Dockerfile must keep building.

Glama builds every listed server from its Dockerfile and drops a server whose build
fails out of search; punkpeye/awesome-mcp-servers requires a passing Glama listing.
The ways this image breaks are all static and none fails locally: a file the build
needs that is not COPYed (pyproject's ``readme`` or a PEP 639 ``license-files``
entry — setuptools refuses to build without them), a COPY source the ``Dockerfile.dockerignore``
allow-list hides, an unpinned base, or an entrypoint that is not the stdio server.
The fast-checks ``introspection-image`` job builds and probes the real image; this
file catches the drift before that job has to.

Stdlib only; reads files; no Docker.

Run:  python3 tests/test_dockerfile.py
"""
import fnmatch
import json
import re
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _dockerfile():
    return (REPO / "Dockerfile").read_text(encoding="utf-8")


def _copied() -> set:
    out = set()
    for line in _dockerfile().splitlines():
        m = re.match(r"^COPY\s+(.+?)\s+\S+\s*$", line)
        if m:
            out.update(m.group(1).split())
    return out


def _pyproject():
    return (REPO / "pyproject.toml").read_text(encoding="utf-8")


def test_build_inputs_are_copied():
    toml = _pyproject()
    readme = re.search(r'^readme\s*=\s*"([^"]+)"', toml, re.M).group(1)
    licenses = re.findall(r'"([^"]+)"', re.search(r"^license-files\s*=\s*\[(.*?)\]", toml, re.M).group(1))
    need = {"pyproject.toml", readme, *licenses, "ankusdrive"}
    missing = need - _copied()
    assert not missing, f"Dockerfile does not COPY {sorted(missing)} — `pip install .` would fail"
    for src in _copied():
        assert (REPO / src).exists(), f"Dockerfile COPYs {src}, which does not exist"


def test_dockerignore_admits_every_copy_source():
    rules = [ln.strip() for ln in (REPO / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    assert rules[0] == "*", "Dockerfile.dockerignore is expected to be an allow-list starting with `*`"
    allowed = [r[1:] for r in rules if r.startswith("!")]
    for src in _copied():
        probe = src if (REPO / src).is_file() else f"{src}/__init__.py" if src == "ankusdrive" else f"{src}/x"
        assert any(fnmatch.fnmatch(probe, pat) or fnmatch.fnmatch(src, pat) for pat in allowed), \
            f"{src} is COPYed but hidden by Dockerfile.dockerignore"


def test_base_is_digest_pinned_and_entrypoint_is_the_stdio_server():
    df = _dockerfile()
    froms = re.findall(r"^FROM\s+(\S+)", df, re.M)
    assert froms and all(re.search(r"@sha256:[0-9a-f]{64}$", f) for f in froms), \
        f"unpinned base image(s): {froms}"
    assert re.search(r'^ENTRYPOINT \["ankusdrive", "mcp"\]\s*$', df, re.M), "entrypoint is not `ankusdrive mcp`"
    assert re.search(r"^USER\s+(?!root\b)\S+", df, re.M), "image runs as root"


def test_root_dockerignore_is_left_to_the_heavy_image():
    """The introspection image must not repurpose .dockerignore: it is the build
    context filter for docker/heavy-solvers/Dockerfile (#339), which COPYs far more
    than this image does. Its own allow-list lives in Dockerfile.dockerignore."""
    root = (REPO / ".dockerignore").read_text(encoding="utf-8")
    assert "heavy-solvers" in root, ".dockerignore no longer reads as the heavy image's context filter"
    assert not re.search(r"^\*\s*$", root, re.M), ".dockerignore became an allow-list — that starves the heavy image's build"


def test_glama_json():
    g = json.loads((REPO / "glama.json").read_text(encoding="utf-8"))
    assert g.get("$schema") == "https://glama.ai/mcp/schemas/server.json", g
    assert "gchen19" in g.get("maintainers", []), g


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
            print(f"  FAIL {name:58s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:58s} ({time.time() - t0:.2f}s)")
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
