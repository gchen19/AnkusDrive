<#
.SYNOPSIS
  Set up THIS Windows machine to run the DriftPin test suite - the Windows analog of
  tests/setup_local.sh (which is Linux/bash-only: AppImage extraction + PATH symlinks +
  a bundled-python .venv). Windows needs none of that.

.DESCRIPTION
  On Windows the model is simpler than on Linux, so this is NOT a port of setup_local.sh:

    * FreeCAD is a normal installer, not an AppImage. `driftpin/client.py` already
      discovers `freecadcmd.exe` under `C:\Program Files\FreeCAD *\bin\` (or via
      $env:DRIFTPIN_FREECADCMD) - no symlink onto PATH is needed. The worker runs
      freecadcmd.exe as a SUBPROCESS, so the test-driver interpreter never imports
      FreeCAD and doesn't have to be FreeCAD's bundled python.

    * There is therefore no two-interpreter split (Linux uses system python3 for the
      worker tests and a .venv pointed at FreeCAD's bundled python for the numpy/Pillow
      tests). One ordinary venv with `pip install -e .` gives the driver mcp + numpy +
      Pillow AND can drive freecadcmd.exe - it covers the whole Windows suite.

  Idempotent - safe to re-run. Honors:
    $env:DRIFTPIN_FREECADCMD   a specific freecadcmd.exe to use (else auto-discovered)
    $env:DRIFTPIN_PY           python launcher to build the venv from (default: python)

.EXAMPLE
  pwsh tests\setup_local.ps1
  pwsh tests\run_all.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# Resolve the interpreter that builds the venv. `python` alone is not enough on a
# self-hosted runner: the Actions service runs as NETWORK SERVICE, whose PATH has no
# per-user install (a Store/WindowsApps python is invisible to it — exactly how the CI
# lane first failed). Candidates are VALIDATED by running `--version`, which also
# rejects the 0-byte Store stub. Last resort is FreeCAD's bundled python.exe — always
# present on a box that can run this suite, and the same trick the Linux lane's venv
# uses.
function Find-Python([string]$freecadcmd) {
    $candidates = @()
    if ($env:DRIFTPIN_PY) { $candidates += ,@($env:DRIFTPIN_PY) }
    $candidates += ,@('python')
    $candidates += ,@('py', '-3')
    $globs = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        'C:\Program Files\Python3*\python.exe',
        'C:\Python3*\python.exe'
    )
    $globs | ForEach-Object { Get-ChildItem -Path $_ -ErrorAction SilentlyContinue } |
        Sort-Object FullName | ForEach-Object { $candidates += ,@($_.FullName) }
    if ($freecadcmd) {
        $bundled = Join-Path (Split-Path -Parent $freecadcmd) 'python.exe'
        if (Test-Path $bundled) { $candidates += ,@($bundled) }
    }
    foreach ($cand in $candidates) {
        try {
            & $cand[0] @($cand | Select-Object -Skip 1) --version *> $null
            if ($LASTEXITCODE -eq 0) { return $cand }
        } catch {}
    }
    throw ('no working Python found (tried python, py -3, common install dirs, ' +
           "FreeCAD's bundled python.exe) - install Python 3 or set `$env:DRIFTPIN_PY")
}

# --- 1. locate FreeCAD (report only; the client resolves it at runtime) ---------
function Find-Freecadcmd {
    if ($env:DRIFTPIN_FREECADCMD) { return $env:DRIFTPIN_FREECADCMD }
    $globs = @(
        'C:\Program Files\FreeCAD *\bin\freecadcmd.exe',
        'C:\Program Files\FreeCAD*\bin\freecadcmd.exe',
        'C:\Program Files (x86)\FreeCAD *\bin\freecadcmd.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\FreeCAD *\bin\freecadcmd.exe')
    )
    $hit = $globs |
        ForEach-Object { Get-ChildItem -Path $_ -ErrorAction SilentlyContinue } |
        Sort-Object FullName |          # 'FreeCAD 1.0' < 'FreeCAD 1.1' -> newest last
        Select-Object -Last 1
    if ($hit) { return $hit.FullName }
    $onPath = Get-Command freecadcmd, FreeCADCmd -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($onPath) { return $onPath.Source }
    return $null
}

$freecadcmd = Find-Freecadcmd
if ($freecadcmd) {
    Write-Host "FreeCAD:  $freecadcmd"
} else {
    Write-Warning "freecadcmd.exe not found. Install FreeCAD 1.1 from https://www.freecad.org/"
    Write-Warning "or set `$env:DRIFTPIN_FREECADCMD to the freecadcmd.exe path. Pure-Python"
    Write-Warning "tests will still run; the FreeCAD/worker tests will fail to boot."
}

# --- 2. build/refresh the venv and install the package + lint pin ---------------
$venvPy = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    $py = Find-Python $freecadcmd
    Write-Host "Creating venv (.venv) from '$($py -join ' ')'..."
    & $py[0] @($py | Select-Object -Skip 1) -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed (tried '$($py -join ' ')')" }
}

Write-Host "Installing driftpin (editable) + ruff + Windows-viable solver extras..."
& $venvPy -m pip install --upgrade --quiet pip
if ($LASTEXITCODE -ne 0) { throw 'pip self-upgrade failed' }

# Base package (mcp + Pillow + numpy) + pinned ruff (same version CI fast-checks uses).
# This one MUST succeed.
& $venvPy -m pip install --quiet -e . 'ruff==0.15.15'
if ($LASTEXITCODE -ne 0) { throw 'pip install of the base package failed' }

# Solver extras that ship real Windows wheels - installed INDEPENDENTLY and best-effort.
# Not every extra has a wheel for every Python (e.g. pybullet ships none for 3.13 and
# fails to source-build without a compiler); installing them as one atomic set would let
# that one failure abort the rest. Each family degrades cleanly when absent - its test
# skips - so a failed extra is a warning, not a fatal error. The Linux-only source-built
# families (OpenFOAM, YADE, openEMS, bempp, preCICE) have no wheel and are omitted.
# NB: `$ErrorActionPreference='Stop'` (top of script) turns a native command's stderr
# into a TERMINATING NativeCommandError the moment it's merged into the pipeline (`2>&1`).
# A failing extra (pybullet's source build) writes to stderr, so that would abort the whole
# setup instead of skipping. Drop to 'Continue' for the loop and gate purely on $LASTEXITCODE;
# `*>$null` discards every stream so nothing reaches the pipeline.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
foreach ($extra in 'mbd', 'topology', 'optics', 'fluids') {
    & $venvPy -m pip install --quiet -e ".[$extra]" *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  extra [$extra]: installed"
    } else {
        Write-Warning "  extra [$extra]: no Windows wheel for this Python - skipped (its tests degrade cleanly)"
    }
}
$ErrorActionPreference = $prevEAP

# --- 3. verify ------------------------------------------------------------------
# Pass the probe over stdin (`python -`), not `-c`: PowerShell 5.1 mangles double quotes
# when it hands an argument to a native exe, so an inline `-c` with an f-string breaks.
# A single-quoted here-string is literal (no PS interpolation) and piped to stdin verbatim.
Write-Host ''
Write-Host '== verify =='
$verify = @'
import sys, numpy, PIL
print('  .venv python', sys.version.split()[0], ' numpy', numpy.__version__, ' Pillow', PIL.__version__)
from driftpin import client
print('  driftpin resolves freecadcmd ->', client._resolve_freecadcmd())
'@
$verify | & $venvPy -
if ($LASTEXITCODE -ne 0) { throw 'verification import failed' }

Write-Host ''
Write-Host 'Setup complete. Run the suite with:  pwsh tests\run_all.ps1'
