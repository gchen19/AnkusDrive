"""PII contract (issue #303) — nobody's machine gets into the tree.

`gitleaks` gates every PR, but it hunts credentials: a home-directory path, a CI
box's hostname, or a tailnet name sails through it, because none of those is a
secret — they are personal and infrastructure data. That is not hypothetical.
#320 shipped docs transcribed verbatim from the reference machines and passed the
secret scan; a later hand-sweep then missed a *dash-encoded* home path
(`-Users-<name>-...`) that every `/Users/`-anchored grep walked past. Hand-audits
have now failed twice; the grep lives here and runs in the fast lane, for exactly
the reason `test_naming.py` exists.

Two layers, because this file is itself public and a guard that spells out the
strings it forbids would BE the leak:

  * **Structural rules** (plaintext, name nobody): a shared local folder path is
    a leak no matter whose machine it is — any `/Users/<name>`, `C:\\Users\\<name>`
    or `/home/<name>` that is not a documented placeholder, the dash-encoded
    `-Users-<name>-` form, and any concrete `*.ts.net` tailnet host. This is the
    layer that protects future contributors too, not just today's maintainer.
  * **Fingerprints** (SHA-256, recover nothing by reading): the known machine
    identifiers that have no structure a rule could key on — bare hostnames and
    the local username. Files are tokenized and candidate tokens hashed against
    the digest table, so the tree never carries the strings in greppable OR
    readable form. Honest caveat: a digest of a guessable string yields to a
    determined targeted guess; fingerprints stop casual reading and grep, while
    the durable fix for the hostnames is the runner rename tracked on #303.

Findings are reported as *file + rule + count only* — never the matched text,
because on a public repository CI logs are public and echoing the match would
leak it a second time (the same reason the gitleaks lane runs `--redact`).

Plus the usual guards on the guard, mirroring `test_naming.py`: a
self-validating allowlist (empty, deliberately) and a planted-canary self-test
so a broken rule table or an empty `git ls-files` cannot pass vacuously.

Like ``test_naming.py`` this imports NEITHER the package nor any third-party
module: it shells out to ``git ls-files`` and reads bytes. Pure stdlib, seconds,
no FreeCAD.

Run:  python3 tests/test_pii.py
"""
import hashlib
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --- layer 1: structural rules ----------------------------------------------
# Each rule matches a CLASS of leak without naming anyone. Documented
# placeholders (`/Users/you/...`, `/home/user`, `<name>`, `$USER`, CI service
# accounts) are excluded so docs can still show what a path looks like.
_PLACEHOLDER = rb"(?:you|user|username|name|owner|admin|shared|public|default|runner|ubuntu|openfoam|ci)\b|[<$%{*\"']"
STRUCTURAL = {
    "macos home dir":   re.compile(rb"/users/(?!" + _PLACEHOLDER + rb")[a-z0-9._-]{2,}", re.I),
    "windows home dir": re.compile(rb"(?:[a-z]:[/\\]|\\)users\\(?!" + _PLACEHOLDER + rb")[a-z0-9._-]{2,}", re.I),
    "linux home dir":   re.compile(rb"/home/(?!" + _PLACEHOLDER + rb")[a-z0-9._-]{2,}", re.I),
    "dash-encoded home dir": re.compile(rb"-users-(?!" + _PLACEHOLDER + rb")[a-z0-9]{2,}-", re.I),
    "tailnet hostname": re.compile(rb"[a-z0-9][a-z0-9-]*\.ts\.net", re.I),
}
# A match whose immediate left context is a URL is a web path (github.com/users/
# <login>), not a filesystem leak.
_URLISH = re.compile(rb"(?:://|\w\.\w{2,3}$)")


def _url_context(blob, start):
    return bool(_URLISH.search(blob[max(0, start - 24):start]))


# --- layer 2: fingerprints --------------------------------------------------
# SHA-256 of each known machine identifier, lowercased. The canary entry proves
# the tokenizer + hash path works (see the self-test); every other digest is an
# identifier this tree must never contain. Lengths are stored so only tokens
# that could possibly match get hashed.
FINGERPRINTS = {
    "71d9bf0367ca8c557520efcbde866dfec12a6246e98c2e48b1cef54319ecc6d2",  # 10
    "9ba206e223b07d4397d09ebc49fdfd209c474a28d43b013b96585f3c90803733",  # 11
    "b491b7a48f675f631fcefd7b32d44bacc17c55f56ddefa29c7ca6c952f860a4f",  # 15
    "3a7c901ab48ec0aa8a7c6ff19b00279f8156fde8a8232c2185c1ef5abfefa205",  # 14
    "3bb7c1052e1e0f35d929b79cd7c372e1001c189ae3795623f05088fca07f09aa",  # 12
    "e6a2e776ba3ffffc21701fff4e87a847422c3a8b5a6985d48fef332f6b1d5865",  # 10
    "475a5d1f239ef2cda25bc38cc6f98127d2c30611f0a6dea916b1f22cadbbccac",  # 11
    "da64653c6354143ea7ac6d1dbcd6e9775b01fe4ddeb214d6dd39b65e404c5cc6",  # 10
    "da89b1adaa20d1c7f641a84b17c6e9dcb492aa3001766b4c0d0988e82bafa204",  # 21 canary
}
_FP_LENGTHS = frozenset({10, 11, 12, 14, 15, 21})
_CANARY = "ankusdrive-pii-canary"
_TOKEN = re.compile(rb"[a-z0-9][a-z0-9-]{3,62}[a-z0-9]")


def _candidates(blob):
    """Hostname-shaped candidate strings: every [a-z0-9-] token, plus every
    dash-bounded span inside it (so the username inside `-Users-<name>-<proj>`
    and a hostname inside `<host>.<tailnet>.ts.net` both surface as spans)."""
    for m in _TOKEN.finditer(blob.lower()):
        parts = m.group().split(b"-")
        for i in range(len(parts)):
            span = parts[i]
            if len(span) in _FP_LENGTHS:
                yield span
            for j in range(i + 1, len(parts)):
                span = span + b"-" + parts[j]
                if len(span) in _FP_LENGTHS:
                    yield span


def _fingerprint_hits(blob):
    n = 0
    for span in _candidates(blob):
        if hashlib.sha256(span).hexdigest() in FINGERPRINTS:
            n += 1
    return n


# --- the deliberate survivors ----------------------------------------------
# Tracked paths ALLOWED to trigger a rule, each with a falsifiable reason.
# One entry today, and it should take an argument on #303 to grow, because
# nothing in a public tree legitimately shares a machine's folders.
ALLOWED = {
    "tests/test_pii.py":
        "this file — it plants FAKE samples (plantedname, planted-net) to "
        "prove each rule fires; nothing real, but the rules cannot know that",
}


def _tracked():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True,
                         capture_output=True).stdout
    return [p.decode() for p in out.split(b"\0") if p]


def _allowed(path):
    return any(path == k or path.startswith(k) for k in ALLOWED)


def _scan(blob):
    """All rule violations in a blob, as redacted labels (never the match)."""
    labels = []
    for label, rx in STRUCTURAL.items():
        n = sum(1 for m in rx.finditer(blob) if not _url_context(blob, m.start()))
        if n:
            labels.append(f"{label} x{n}")
    n = _fingerprint_hits(blob)
    if n:
        labels.append(f"known machine identifier x{n}")
    return labels


def test_no_pii_in_content():
    """No tracked file's CONTENT trips any rule, outside the allowlist."""
    offenders = []
    for rel in _tracked():
        if _allowed(rel):
            continue
        try:
            blob = (REPO / rel).read_bytes()
        except OSError:
            continue
        labels = _scan(blob)
        if labels:
            offenders.append(f"{rel} ({', '.join(labels)})")
    assert not offenders, (
        "machine-identifying content in %d tracked file(s) — replace real "
        "paths/hosts with placeholders (`/Users/you/...`, a neutral hostname), "
        "or add an allowlist entry in tests/test_pii.py with the reason:\n  %s"
        % (len(offenders), "\n  ".join(sorted(offenders))))


def test_no_pii_in_paths():
    """No tracked PATH trips any rule, outside the allowlist."""
    offenders = []
    for rel in _tracked():
        if _allowed(rel):
            continue
        labels = _scan(rel.encode())
        if labels:
            offenders.append(f"{rel} ({', '.join(labels)})")
    assert not offenders, (
        "%d tracked path(s) carry a machine identifier:\n  %s"
        % (len(offenders), "\n  ".join(sorted(offenders))))


def test_allowlist_is_honest():
    """Every allowlist entry still exists and still trips a rule.

    Without this, an exemption outlives its reason and the next file at that
    path is exempt for free."""
    stale = []
    for entry, reason in sorted(ALLOWED.items()):
        target = REPO / entry
        if not target.exists():
            stale.append(f"{entry} — gone from the tree ({reason})")
            continue
        files = ([p for p in target.rglob("*") if p.is_file()]
                 if target.is_dir() else [target])
        if not any(_scan(p.name.encode()) or (p.is_file() and _scan(p.read_bytes()))
                   for p in files):
            stale.append(f"{entry} — no longer trips any rule ({reason})")
    assert not stale, (
        "stale allowlist entries in tests/test_pii.py — delete them:\n  %s"
        % "\n  ".join(stale))


def test_the_sweep_actually_swept():
    """The guard guards something.

    There is deliberately NO tracked file where a violation legitimately lives,
    so the machinery is proven against planted samples: one per structural rule,
    and the canary token for the fingerprint path. A broken regex, a broken
    tokenizer, a bad cwd, or an empty `git ls-files` fails here rather than
    passing everything vacuously."""
    tracked = _tracked()
    assert len(tracked) > 200, f"only {len(tracked)} tracked files — sweep is broken"
    assert any(p.startswith("ankusdrive/") for p in tracked), "package tree not seen"

    planted = {
        "macos home dir":        b"see /Users/plantedname/proj",
        "windows home dir":      b"C:\\Users\\plantedname\\proj",
        "linux home dir":        b"/home/plantedname/proj",
        "dash-encoded home dir": b"x/-Users-plantedname-Proj/memory",
        "tailnet hostname":      b"ssh box.planted-net.ts.net",
    }
    for label, blob in planted.items():
        got = _scan(blob)
        assert any(g.startswith(label) for g in got), \
            f"rule {label!r} missed its planted sample (got {got})"
    assert _scan(b"host = " + _CANARY.encode()) == ["known machine identifier x1"], \
        "fingerprint path cannot find the planted canary"
    # Placeholders must stay legal, and URL logins are not filesystem paths.
    for ok in (b"/Users/you/AnkusDrive", b"/home/user/x", b"C:\\Users\\<name>",
               b"https://github.com/users/plantedname"):
        assert not _scan(ok), f"placeholder tripped a rule: {ok!r}"
    digests = FINGERPRINTS - {hashlib.sha256(_CANARY.encode()).hexdigest()}
    assert len(digests) >= 5 and all(re.fullmatch(r"[0-9a-f]{64}", d) for d in digests), \
        "fingerprint table looks wrong"


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
