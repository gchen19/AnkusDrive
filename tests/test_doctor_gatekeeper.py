"""``doctor``'s macOS Gatekeeper diagnosis (issue #310).

A quarantined FreeCAD whose code seal broke before its first run is killed by
Gatekeeper behind a dialog nobody at an MCP host sees, and the boot check only
reported ``WorkerDied``. These tests pin the diagnosis that now rides on a failed boot:

  * it names Gatekeeper only when the bundle is quarantined, ``spctl`` rejects it, and
    syspolicyd's log doesn't show the boot going through unblocked: a bundle that
    passed once before its seal broke (the documented solver install) keeps running,
    so its unrelated boot failures must not be blamed on Gatekeeper,
  * it reports the probes' results (never an absence) when that isn't the case,
  * its fix NEVER spells the command that clears the quarantine flag — the report is
    read by an agent with a shell, and trusting a modified bundle is the user's call,
  * it is inert off macOS and outside an ``.app``.

``xattr`` / ``spctl`` / ``log`` are INJECTED through ``doctor._run``; nothing here reads or
changes a real bundle, so the file runs on every platform.

Run:  python3 tests/test_doctor_gatekeeper.py
"""
import os
import plistlib
import sys
import tempfile
import time
import traceback
import types
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import client, doctor  # noqa: E402

SEALED = "/Applications/FreeCAD.app: a sealed resource is missing or invalid"
ACCEPTED = "/Applications/FreeCAD.app: accepted\nsource=Notarized Developer ID"
FLAG = "0083;6ab0d827;Brave;B934D3BB-DBDB-42FB-9A1C-06906229DD74"


REJECTION = ("2026-09-21 00:15:47.982 E  syspolicyd[494:71d523] [com.apple.syspolicy.exec:default] "
             "Terminating process due to Gatekeeper rejection: 76861, <private>")
PROMPT = ("2026-09-21 00:15:31.094 Df syspolicyd[494:71d525] [com.apple.syspolicy.exec:default] "
          "Prompt shown (1, 0), waiting for response: PST: (path: bdda4ff98db90fc7), "
          "(team: 289DJRF23X), (id: org.freecad.FreeCAD), (bundle_id: org.freecad.FreeCAD)")
OTHER_PROMPT = PROMPT.replace("org.freecad.FreeCAD", "com.example.Other")


def _fake_run(quarantine, spctl_rc, spctl_text, calls, log=None):
    def run(argv, timeout=60.0):
        calls.append(argv)
        tool = os.path.basename(argv[0])
        if tool == "xattr":
            ok = quarantine is not None
            return types.SimpleNamespace(returncode=0 if ok else 1,
                                         stdout=(quarantine or "") + "\n", stderr="")
        if tool == "spctl":
            return types.SimpleNamespace(returncode=spctl_rc, stdout="",
                                         stderr=spctl_text + "\n")
        if tool == "log":
            # log=None: the unified log could not be read.
            return types.SimpleNamespace(returncode=1 if log is None else 0,
                                         stdout=log or "", stderr="")
        raise AssertionError(f"unexpected probe {argv}")
    return run


@contextmanager
def _probes(quarantine, spctl_rc, spctl_text, log=None):
    calls = []
    saved = doctor._run
    doctor._run = _fake_run(quarantine, spctl_rc, spctl_text, calls, log)
    try:
        yield calls
    finally:
        doctor._run = saved


@contextmanager
def _fake_bundle():
    """A real on-disk freecadcmd inside a *.app, so path resolution is exercised."""
    with tempfile.TemporaryDirectory() as d:
        exe = Path(d) / "FreeCAD.app" / "Contents" / "Resources" / "bin" / "freecadcmd"
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")
        with open(Path(d) / "FreeCAD.app" / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "org.freecad.FreeCAD"}, f)
        yield str(exe), str((Path(d) / "FreeCAD.app").resolve())


BUNDLE_EXE = "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd"
# _app_bundle resolves the path, which on the Windows lane gains a drive letter.
BUNDLE = str(Path("/Applications/FreeCAD.app").resolve())


def test_quarantined_and_rejected_names_gatekeeper():
    with _probes(FLAG, 3, SEALED) as calls:
        gk = doctor.gatekeeper_report(BUNDLE_EXE, system="Darwin")
    assert gk["bundle"] == BUNDLE, gk
    assert gk["quarantined"] is True and gk["quarantine"] == FLAG, gk
    assert gk["assessment"] == "rejected", gk
    assert "Gatekeeper" in gk["fix"] and "code seal is broken" in gk["fix"], gk
    assert "codesign --verify --deep --strict -v" in gk["fix"], gk
    assert "SHA-256" in gk["fix"], gk
    # Both probes target the bundle, not the inner binary.
    assert all(argv[-1] == BUNDLE for argv in calls), calls


def test_probes_use_absolute_paths():
    # MCP hosts spawn the server with a minimal PATH (no /usr/sbin, where spctl
    # lives); a bare name there turned the live diagnosis into "unknown".
    with _fake_bundle() as (exe, _), _probes(FLAG, 3, SEALED, log=REJECTION) as calls:
        doctor.gatekeeper_report(exe, system="Darwin", since=time.time())
    assert {a[0] for a in calls} == {"/usr/bin/xattr", "/usr/sbin/spctl", "/usr/bin/log"}, calls


def test_fix_never_spells_the_quarantine_bypass():
    # Every rejected shape — sealed or not — must stay free of the command that
    # clears the flag. The report reaches an agent that can run it.
    for text in (SEALED, "/Applications/FreeCAD.app: rejected"):
        with _probes(FLAG, 3, text):
            fix = doctor.gatekeeper_report(BUNDLE_EXE, system="Darwin")["fix"]
        assert "xattr" not in fix, fix
        assert "-d com.apple.quarantine" not in fix and "-dr" not in fix, fix
        assert "spctl --add" not in fix and "--master-disable" not in fix, fix


def test_rejected_without_seal_text_quotes_spctl():
    with _probes(FLAG, 3, "/Applications/FreeCAD.app: rejected"):
        gk = doctor.gatekeeper_report(BUNDLE_EXE, system="Darwin")
    assert "(/Applications/FreeCAD.app: rejected)" in gk["fix"], gk


def test_accepted_bundle_is_reported_without_a_fix():
    with _probes(FLAG, 0, ACCEPTED):
        gk = doctor.gatekeeper_report(BUNDLE_EXE, system="Darwin")
    assert gk["assessment"] == "accepted" and gk["quarantined"] is True, gk
    assert gk["detail"] == "/Applications/FreeCAD.app: accepted", gk
    assert "fix" not in gk, gk


def test_unquarantined_rejected_bundle_is_not_blamed_on_gatekeeper():
    # Without the quarantine flag Gatekeeper does not gate the exec, so a failed spctl
    # assessment is reported but is not offered as the cause.
    with _probes(None, 3, SEALED):
        gk = doctor.gatekeeper_report(BUNDLE_EXE, system="Darwin")
    assert gk["quarantined"] is False and "quarantine" not in gk, gk
    assert gk["assessment"] == "rejected", gk
    assert "fix" not in gk, gk


def test_probe_failure_is_reported_not_raised():
    def boom(argv, timeout=60.0):
        raise FileNotFoundError(argv[0])
    saved = doctor._run
    doctor._run = boom
    try:
        gk = doctor.gatekeeper_report(BUNDLE_EXE, system="Darwin")
    finally:
        doctor._run = saved
    assert gk["quarantined"] is None and "FileNotFoundError" in gk["quarantine_error"], gk
    assert gk["assessment"] == "unknown" and "FileNotFoundError" in gk["detail"], gk
    assert "fix" not in gk, gk


def test_inert_off_macos_and_outside_a_bundle():
    with _probes(FLAG, 3, SEALED) as calls:
        assert doctor.gatekeeper_report(BUNDLE_EXE, system="Linux") is None
        assert doctor.gatekeeper_report(BUNDLE_EXE, system="Windows") is None
        assert doctor.gatekeeper_report("/usr/local/bin/freecadcmd", system="Darwin") is None
    assert calls == [], calls


class _DeadWorker:
    def __init__(self, *a, **kw):
        raise RuntimeError("worker closed stdout (process likely died)")


@contextmanager
def _failed_boot(exe, system):
    saved = (doctor._resolve_freecadcmd, doctor._resolution_source, client.Worker,
             doctor.gatekeeper_report)
    real_gk = doctor.gatekeeper_report
    doctor._resolve_freecadcmd = lambda: exe
    doctor._resolution_source = lambda: "test"
    client.Worker = _DeadWorker
    doctor.gatekeeper_report = lambda p, since=None: real_gk(p, system=system, since=since)
    try:
        yield
    finally:
        (doctor._resolve_freecadcmd, doctor._resolution_source, client.Worker,
         doctor.gatekeeper_report) = saved


def test_failed_boot_carries_the_diagnosis_and_fix():
    with _fake_bundle() as (exe, bundle), _probes(FLAG, 3, SEALED, log=REJECTION) as calls, \
            _failed_boot(exe, "Darwin"):
        fc = doctor.freecad_report(probe_version=True)
    assert fc["error"].startswith("resolved but did not boot"), fc
    assert fc["gatekeeper"]["bundle"] == bundle, fc
    assert fc["fix"] == fc["gatekeeper"]["fix"], fc
    assert fc["gatekeeper"]["blocked"] is True, fc
    # The log window opens at the boot, not at some fixed look-back.
    log_argv = next(a for a in calls if a[0].endswith("/log"))
    assert "--start" in log_argv, log_argv
    text = "\n".join(doctor._fmt_freecad(fc))
    assert "gatekeeper: rejected" in text and "fix: macOS Gatekeeper" in text, text


def test_failed_boot_of_a_bundle_gatekeeper_let_through_is_not_blamed_on_it():
    # The reference-Mac shape: quarantined, seal broken by the solver pip install,
    # spctl rejects — but it passed before, so the boot wasn't blocked. No fix.
    with _fake_bundle() as (exe, _), _probes(FLAG, 3, SEALED, log=""), \
            _failed_boot(exe, "Darwin"):
        fc = doctor.freecad_report(probe_version=True)
    gk = fc["gatekeeper"]
    assert gk["blocked"] is False and "fix" not in gk and "fix" not in fc, fc
    assert "did not block this boot" in gk["note"], gk
    assert "did not block this boot" in "\n".join(doctor._fmt_freecad(fc))


def test_dialog_for_this_bundle_counts_as_blocked():
    with _fake_bundle() as (exe, _), _probes(FLAG, 3, SEALED, log=PROMPT):
        gk = doctor.gatekeeper_report(exe, system="Darwin", since=time.time())
    assert gk["blocked"] is True and "fix" in gk, gk


def test_dialog_for_another_app_does_not_count():
    with _fake_bundle() as (exe, _), _probes(FLAG, 3, SEALED, log=OTHER_PROMPT):
        gk = doctor.gatekeeper_report(exe, system="Darwin", since=time.time())
    assert gk["blocked"] is False and "fix" not in gk, gk


def test_unreadable_log_keeps_the_hint():
    # No evidence either way: the quarantined + rejected pair still stands.
    with _fake_bundle() as (exe, _), _probes(FLAG, 3, SEALED, log=None):
        gk = doctor.gatekeeper_report(exe, system="Darwin", since=time.time())
    assert gk["blocked"] is None and "fix" in gk, gk


def test_log_is_not_read_unless_gatekeeper_could_be_the_cause():
    with _fake_bundle() as (exe, _), _probes(FLAG, 0, ACCEPTED, log=REJECTION) as calls:
        gk = doctor.gatekeeper_report(exe, system="Darwin", since=time.time())
    assert "blocked" not in gk and not any(a[0].endswith("/log") for a in calls), (gk, calls)


def test_failed_boot_off_macos_has_no_gatekeeper_key():
    with _fake_bundle() as (exe, _), _probes(FLAG, 3, SEALED) as calls, \
            _failed_boot(exe, "Linux"):
        fc = doctor.freecad_report(probe_version=True)
    assert "gatekeeper" not in fc and "fix" not in fc, fc
    assert calls == [], calls


def test_no_probe_without_a_boot():
    # The probes ride only on a FAILED boot; a status call that doesn't boot never
    # shells out to xattr/spctl.
    with _fake_bundle() as (exe, _), _probes(FLAG, 3, SEALED) as calls, \
            _failed_boot(exe, "Darwin"):
        fc = doctor.freecad_report(probe_version=False)
    assert "gatekeeper" not in fc and calls == [], (fc, calls)


# --- runner -------------------------------------------------------------------

def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    tests = _discover()
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({time.time() - t_suite:.1f}s) ==")


if __name__ == "__main__":
    main()
