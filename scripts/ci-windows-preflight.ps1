<#
.SYNOPSIS
  ci-windows-preflight.ps1 - assert the Windows test lane's solvers RESOLVE before the
  suite runs (#316). The Windows counterpart of ci-linux-preflight.sh / ci-macos-preflight.sh.

.DESCRIPTION
  WHY THIS EXISTS
    Every live SU2 / Elmer / CalculiX test SKIPs when its solver does not resolve. That is
    the right contract on a partly provisioned dev box, and the wrong one for CI: the
    Windows lane ran green for weeks while testing neither SU2 nor Elmer, because the
    overrides lived in HKCU\Environment, which the runner service never inherited (#316).
    The lane now installs both itself, but an installer that silently lands somewhere
    discovery does not look, or a release asset that changes shape, would put it straight
    back into that state. This script turns "absent" into a named failure, using the SAME
    predicates the tests gate on (ankusdrive.solvers / ankusdrive.client), never a separate
    re-implementation of discovery.

  WHAT IT CHECKS
    1. no legacy DRIFTPIN_* vars in the environment (renamed in 0.5, #295)
    2. freecadcmd resolves to a file
    3. every solver in REQUIRED resolves with status ok (not absent, not unwired)

    All checks run; the exit is non-zero if any failed, and each failure names itself.

  Windows PowerShell 5.1-compatible, ASCII-only (5.1 reads a BOM-less script as ANSI).

.EXAMPLE
  .\scripts\ci-windows-preflight.ps1                       # uses .venv\Scripts\python.exe
  .\scripts\ci-windows-preflight.ps1 -Python C:\py\python.exe
#>
param(
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $repo '.venv\Scripts\python.exe' }
if (-not (Test-Path $Python)) {
    Write-Host "::error::preflight interpreter not found: $Python (run tests\setup_local.ps1 first, or pass -Python)"
    exit 1
}

$check = @'
import os
import sys

sys.path.insert(0, os.getcwd())

GHA = os.environ.get("GITHUB_ACTIONS") == "true"
failed = 0


def ok(msg):
    print("  ok    " + msg)


def fail(msg):
    global failed
    failed += 1
    print(("::error::" if GHA else "error: ") + msg)


print("== Environment naming ==")
legacy = sorted(k for k in os.environ if k.upper().startswith("DRIFTPIN_"))
if legacy:
    fail("legacy DRIFTPIN_* vars set: %s - renamed to ANKUSDRIVE_* in 0.5 (#295)" % ", ".join(legacy))
else:
    ok("no legacy DRIFTPIN_* vars")

from ankusdrive import client, solvers  # noqa: E402  (after the path insert)

print("\n== FreeCAD ==")
fc = client.FREECADCMD
if fc and os.path.isfile(fc):
    ok("freecadcmd -> %s" % fc)
else:
    fail("freecadcmd does not resolve to a file (got %r) - set ANKUSDRIVE_FREECADCMD" % (fc,))

# Every solver the Windows lane runs live. The Linux-only families (OpenFOAM, YADE,
# openEMS, bempp, preCICE) are a documented SKIP in tests\run_all.ps1 and are
# deliberately NOT here. calculix is FreeCAD's bundled ccx.exe; su2 + elmer come from
# scripts\install-solvers.ps1. Adding a live Windows family means adding its solver here.
REQUIRED = ("calculix", "elmer", "su2")
print("\n== Solvers ==")
caps = solvers.capabilities()["solvers"]
for name in REQUIRED:
    info = caps.get(name)
    if info is None:
        fail("%s: not a known solver - REQUIRED is stale against ankusdrive.solvers" % name)
        continue
    status = info.get("status", "ok" if info["available"] else "absent")
    where = info.get("path") or info.get("module") or info.get("found_at") or ""
    if status == "ok" and info["available"]:
        ok("%-9s %s" % (name, where))
    elif status == "unwired":
        fail("%s: installed but unwired at %s - %s" % (name, info.get("found_at"), info.get("wire_hint")))
    else:
        fail("%s: absent - %s" % (name, info.get("install") or "see scripts\\install-solvers.ps1"))

if failed:
    print("\nPreflight FAILED: %d check(s). The suite would have SKIPped these, not failed." % failed)
    sys.exit(1)
print("\nPreflight passed.")
'@

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("ankusdrive-preflight-{0}.py" -f $PID)
# UTF-8 without BOM: Set-Content -Encoding UTF8 on 5.1 writes a BOM, which Python tolerates,
# but WriteAllText keeps the file byte-identical across 5.1 and pwsh.
[IO.File]::WriteAllText($tmp, $check, (New-Object Text.UTF8Encoding $false))
Push-Location $repo
# 'Continue' across the native call: under 'Stop', PowerShell 5.1 can promote any stderr
# line (an import-time warning) to a terminating error. Gate on the exit code instead.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & $Python $tmp
    $rc = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEAP
    Pop-Location
    Remove-Item $tmp -ErrorAction SilentlyContinue
}
exit $rc
