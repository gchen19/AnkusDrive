#!/usr/bin/env python3
"""Turn the working tree into release X.Y.Z, ready to commit as the release PR.

Does by rule the parts of CONTRIBUTING.md "Releasing" that were done by hand:

  * ``__version__`` in ankusdrive/__init__.py, plus the four copies the fast lane
    holds to it: both ``version`` fields in server.json, ``version`` in
    mcpb/manifest.json, and ``version`` + the ``ankusdrive==`` pin in
    mcpb/pyproject.toml.
  * ``## [Unreleased]`` in CHANGELOG.md becomes ``## [X.Y.Z] — <today>``, and the
    compare links at the bottom move with it.

It refuses rather than guesses. The version must go up. Every copy must hold the
current version exactly where expected (a count mismatch means a file changed
shape, and a blind replace would bump the wrong string). The Unreleased section
must open with the release's paragraph, which is the one part only a person can
write (CHANGELOG.md, intro).

Merging the release PR is the release: .github/workflows/release-tag.yml sees the
new ``__version__`` on main, tags it, and runs publish.yml on the tag.

Usage:  scripts/prepare_release.py X.Y.Z [--date YYYY-MM-DD]
"""
import argparse
import datetime
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPARE = "https://github.com/gchen19/AnkusDrive/compare"


class ReleaseError(Exception):
    pass


def _parse(v):
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        raise ReleaseError(f"{v!r} is not X.Y.Z")
    return tuple(int(p) for p in v.split("."))


def current_version(root):
    text = (root / "ankusdrive/__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    if not m:
        raise ReleaseError("no __version__ line in ankusdrive/__init__.py")
    return m.group(1)


def _sub_exact(root, rel, old, new, count):
    """Replace ``old`` with ``new`` in ``rel``, which must hold it exactly ``count`` times."""
    path = root / rel
    text = path.read_text(encoding="utf-8")
    found = text.count(old)
    if found != count:
        raise ReleaseError(f"{rel}: expected {count}x {old!r}, found {found}")
    return path, text.replace(old, new)


def bump_versions(root, old, new):
    edits = [
        _sub_exact(root, "ankusdrive/__init__.py",
                   f'__version__ = "{old}"', f'__version__ = "{new}"', 1),
        _sub_exact(root, "server.json", f'"version": "{old}"', f'"version": "{new}"', 2),
        _sub_exact(root, "mcpb/manifest.json", f'"version": "{old}"', f'"version": "{new}"', 1),
    ]
    path, text = _sub_exact(root, "mcpb/pyproject.toml",
                            f'version = "{old}"', f'version = "{new}"', 1)
    if text.count(f'"ankusdrive=={old}"') != 1:
        raise ReleaseError(f'mcpb/pyproject.toml: expected 1x "ankusdrive=={old}"')
    edits.append((path, text.replace(f'"ankusdrive=={old}"', f'"ankusdrive=={new}"')))
    return edits


def roll_changelog(root, old, new, date):
    path = root / "CHANGELOG.md"
    lines = path.read_text(encoding="utf-8").split("\n")
    heads = [i for i, ln in enumerate(lines) if ln.strip() == "## [Unreleased]"]
    if len(heads) != 1:
        raise ReleaseError(f"CHANGELOG.md: expected one '## [Unreleased]' heading, "
                           f"found {len(heads)}. Entries for {new} go under it.")
    i = heads[0]
    body = next((ln for ln in lines[i + 1:] if ln.strip()), "")
    if body.startswith(("#", "-", "*")):
        raise ReleaseError(
            "CHANGELOG.md: '## [Unreleased]' must open with the release paragraph "
            f"(what {new} is *for*) before its first list or subsection.")
    lines[i] = f"## [{new}] — {date}"

    old_link = f"[Unreleased]: {COMPARE}/v{old}...HEAD"
    try:
        j = lines.index(old_link)
    except ValueError:
        raise ReleaseError(f"CHANGELOG.md: no link line {old_link!r}") from None
    lines[j:j + 1] = [f"[Unreleased]: {COMPARE}/v{new}...HEAD",
                      f"[{new}]: {COMPARE}/v{old}...v{new}"]
    return path, "\n".join(lines)


def prepare(root, new, date):
    old = current_version(root)
    if _parse(new) <= _parse(old):
        raise ReleaseError(f"{new} is not above the current {old}")
    # Compute every edit before writing any, so a refusal leaves the tree untouched.
    edits = bump_versions(root, old, new) + [roll_changelog(root, old, new, date)]
    for path, text in edits:
        path.write_text(text, encoding="utf-8")
    return old, [p.relative_to(root).as_posix() for p, _ in edits]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("version")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    args = ap.parse_args(argv)
    try:
        old, changed = prepare(REPO, args.version, args.date)
    except ReleaseError as e:
        print(f"prepare_release: {e}", file=sys.stderr)
        return 1
    print(f"{old} -> {args.version}: " + ", ".join(changed))
    print(f"Next: open a PR titled 'release: {args.version}'. Merging it tags "
          f"v{args.version} and publishes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
