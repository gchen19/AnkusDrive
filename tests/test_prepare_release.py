"""Release preparation contract — scripts/prepare_release.py.

Merging a PR that changes ``__version__`` now IS a release: release-tag.yml tags
it and publishes. So the script that writes that PR must either produce a tree
the fast lane accepts, or refuse and change nothing. Each test runs the script
on a temp copy of the real version-bearing files, so a change in those files'
shape breaks these tests before it reaches a release.

Pure stdlib, no package import, milliseconds.

Run:  python3 tests/test_prepare_release.py
"""
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FILES = ["ankusdrive/__init__.py", "server.json", "mcpb/manifest.json",
         "mcpb/pyproject.toml", "CHANGELOG.md"]

_spec = importlib.util.spec_from_file_location("prepare_release",
                                               REPO / "scripts/prepare_release.py")
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)


def _tree(unreleased="The release that tests the release script.\n\n### Added\n\n- a thing (#1)\n"):
    root = Path(tempfile.mkdtemp())
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
    if unreleased is not None:
        cl = root / "CHANGELOG.md"
        text = cl.read_text(encoding="utf-8")
        first = re.search(r"^## \[\d", text, re.M).start()
        cl.write_text(text[:first] + "## [Unreleased]\n\n" + unreleased + "\n" + text[first:],
                      encoding="utf-8")
    return root


def _next(v):
    a, b, c = (int(x) for x in v.split("."))
    return f"{a}.{b}.{c + 1}"


def _snapshot(root):
    return {rel: (root / rel).read_text(encoding="utf-8") for rel in FILES}


def test_bumps_every_copy_the_fast_lane_checks():
    root = _tree()
    old = pr.current_version(root)
    new = _next(old)
    pr.prepare(root, new, "2026-01-02")
    assert pr.current_version(root) == new
    server = json.loads((root / "server.json").read_text(encoding="utf-8"))
    assert server["version"] == new and server["packages"][0]["version"] == new
    assert json.loads((root / "mcpb/manifest.json").read_text(encoding="utf-8"))["version"] == new
    toml = (root / "mcpb/pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{new}"' in toml and f'"ankusdrive=={new}"' in toml
    for rel in FILES[:-1]:
        assert old not in (root / rel).read_text(encoding="utf-8"), f"{rel} still mentions {old}"


def test_changelog_heading_and_links_roll():
    root = _tree()
    old = pr.current_version(root)
    new = _next(old)
    pr.prepare(root, new, "2026-01-02")
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" not in text
    assert f"## [{new}] — 2026-01-02\n" in text
    assert f"[Unreleased]: {pr.COMPARE}/v{new}...HEAD\n[{new}]: {pr.COMPARE}/v{old}...v{new}\n" in text
    # publish.yml's release-notes awk keys on this exact prefix.
    assert any(ln.startswith(f"## [{new}]") for ln in text.splitlines())


def test_refuses_without_unreleased_section():
    root = _tree(unreleased=None)
    before = _snapshot(root)
    try:
        pr.prepare(root, _next(pr.current_version(root)), "2026-01-02")
    except pr.ReleaseError as e:
        assert "Unreleased" in str(e)
    else:
        raise AssertionError("prepared a release with no changelog section")
    assert _snapshot(root) == before, "a refusal wrote files"


def test_refuses_bullets_without_release_paragraph():
    root = _tree(unreleased="### Added\n\n- a thing (#1)\n")
    before = _snapshot(root)
    try:
        pr.prepare(root, _next(pr.current_version(root)), "2026-01-02")
    except pr.ReleaseError as e:
        assert "paragraph" in str(e)
    else:
        raise AssertionError("accepted a release with no opening paragraph")
    assert _snapshot(root) == before, "a refusal wrote files"


def test_refuses_same_or_lower_version_and_bad_format():
    root = _tree()
    old = pr.current_version(root)
    for bad in (old, "0.0.1", "1.0", "v9.9.9"):
        try:
            pr.prepare(root, bad, "2026-01-02")
        except pr.ReleaseError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_release_tag_workflow_reads_version_the_same_way():
    # release-tag.yml greps __version__ itself (no package import on the runner);
    # if the line's shape changes, both readers must change together.
    wf = (REPO / ".github/workflows/release-tag.yml").read_text(encoding="utf-8")
    assert "^__version__ = \"([^\"]+)\"$" in wf
    assert re.search(r'^__version__ = "([^"]+)"$',
                     (REPO / "ankusdrive/__init__.py").read_text(encoding="utf-8"), re.M)


def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


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
