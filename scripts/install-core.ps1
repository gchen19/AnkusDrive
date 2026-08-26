<#
.SYNOPSIS
  One-command CORE install of AnkusDrive on Windows: venv -> pip -> doctor -> the exact
  MCP registration line, with resolved absolute paths.

.DESCRIPTION
  Issue #279: a real user on Windows 11 / Python 3.14 followed the README by hand,
  "had some issues with the MCP step", and gave up on the local install. The core
  install (venv + pip + MCP wiring) was the only part of Windows with NO script -
  scripts\install-solvers.ps1 covers the optional SOLVERS, which is the step AFTER
  this one. This is that missing first step, and it fixes the three things that
  actually bite:

    1. PYTHON VERSION, up front. `requires-python = ">=3.10"` has no ceiling, so a
       brand-new CPython used to fail deep inside pip's resolver (or silently
       source-build) with nothing pointing at the interpreter as the cause. This
       script states the supported range BEFORE touching pip.
    2. THE PINS. `pip install -e .` carries pyproject's `mcp>=1.2,<2` ceiling
       (issue #277 - mcp 2.0.0 drops mcp.server.fastmcp, installs clean, then
       `ankusdrive mcp` dies with ImportError while ping/doctor still pass). The
       script re-verifies the resolved mcp/numpy/Pillow AFTER the install and
       proves `mcp.server.fastmcp` actually imports, so a bad resolve is caught
       here rather than by the MCP host.
    3. THE MCP STEP. It prints `claude mcp add ankusdrive -- <abs>\ankusdrive.exe mcp`
       and the claude_desktop_config.json block with ABSOLUTE paths, plus the
       resolved location of that config file on THIS machine. A GUI MCP host does
       not inherit the shell PATH, so a bare "ankusdrive" is exactly the shape that
       fails after a venv install.

  Idempotent - safe to re-run (an existing venv is reused, pip re-resolves).
  Nothing is installed system-wide and no admin rights are needed.

  AFTER this: `scripts\install-solvers.ps1` for the optional solver families
  (SU2, Elmer, PrusaSlicer, WSL-backed OpenFOAM). See docs\WINDOWS.md.

.PARAMETER Python
  Interpreter to build the venv from. Default: auto-discovered (py -3, python,
  common install dirs). Point it at a specific one to pick a version:
  `-Python 'C:\Program Files\Python313\python.exe'`.

.PARAMETER VenvDir
  Where the venv goes. Default: .venv inside the repo checkout.

.PARAMETER Extras
  Optional pip-wheel solver extras to add (mbd, topology, optics, fluids). Each is
  installed independently and best-effort - a family with no wheel for this Python
  degrades cleanly instead of aborting the core install.

.PARAMETER SkipDoctor
  Skip the `ankusdrive doctor` / `ankusdrive ping` verification (offline / no-FreeCAD box).

.PARAMETER Persist
  Also persist the resolved freecadcmd.exe path via `ankusdrive setup --yes`, which
  writes %APPDATA%\ankusdrive\config.toml. MCP hosts launch the server with a minimal
  environment, so a `$env:` set in your terminal does NOT carry over - the config
  file is the layer that does (issue #199).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-core.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install-core.ps1 -Extras mbd,fluids -Persist
#>
[CmdletBinding()]
param(
    [string]$Python,
    [string]$VenvDir,
    [string[]]$Extras = @(),
    [switch]$SkipDoctor,
    [switch]$Persist
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
if (-not $VenvDir) { $VenvDir = Join-Path $repo '.venv' }

# Supported interpreter range. The FLOOR mirrors pyproject.toml's `requires-python`;
# the CEILING is the newest CPython AnkusDrive has actually been installed and smoke-
# tested on, which is the highest `Programming Language :: Python :: 3.x` classifier.
# tests/test_windows_support.py pins both against pyproject so the three cannot drift -
# that drift is what left #279's reporter with a pip stack trace instead of an answer.
# The host interpreter is INDEPENDENT of FreeCAD's bundled Python: freecadcmd.exe runs
# as a subprocess, so a 3.14 host driving FreeCAD's own 3.11 is fine.
$PY_MIN = [version]'3.10'
$PY_MAX_VERIFIED = [version]'3.14'

function Write-Step([string]$text) {
    Write-Host ''
    Write-Host "== $text =="
}

# --- 1. interpreter -------------------------------------------------------------
# Same validated-candidate approach as tests\setup_local.ps1: `python` alone is not
# enough (the Windows Store stub is a 0-byte shim that exits nonzero), so every
# candidate is proven by RUNNING it before it is used.
function Find-Python {
    $candidates = @()
    if ($Python)          { $candidates += ,@($Python) }
    if ($env:ANKUSDRIVE_PY) { $candidates += ,@($env:ANKUSDRIVE_PY) }
    if (-not $Python) {
        $candidates += ,@('py', '-3')
        $candidates += ,@('python')
        $globs = @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
            'C:\Program Files\Python3*\python.exe',
            'C:\Python3*\python.exe'
        )
        $globs | ForEach-Object { Get-ChildItem -Path $_ -ErrorAction SilentlyContinue } |
            Sort-Object FullName | ForEach-Object { $candidates += ,@($_.FullName) }
    }
    # Probe with `--version` rather than `-c "import sys; ..."`: PowerShell 5.1 strips
    # the double quotes when handing an argument to a native exe, so an inline -c
    # snippet arrives mangled and every candidate "fails" (observed on the real box).
    # Stderr must not be merged into the pipeline while $ErrorActionPreference is
    # 'Stop' either - that turns a chatty candidate into a terminating error.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        foreach ($cand in $candidates) {
            $rest = @($cand | Select-Object -Skip 1)
            try {
                $out = (& $cand[0] @rest --version 2>&1 | Out-String)
                if ($LASTEXITCODE -eq 0 -and $out -match '(\d+)\.(\d+)\.(\d+)') {
                    # a hashtable survives the return intact - PS 5.1 unrolls returned ARRAYS
                    return @{ Exe = $cand[0]; Args = $rest
                              Version = [version]("{0}.{1}.{2}" -f $matches[1], $matches[2], $matches[3]) }
                }
            } catch {}
        }
    } finally { $ErrorActionPreference = $prevEAP }
    throw ("no working Python found (tried -Python, `$env:ANKUSDRIVE_PY, py -3, python, " +
           'common install dirs). Install Python 3 from https://www.python.org/downloads/windows/')
}

Write-Step 'Python interpreter'
$py = Find-Python
$pyLabel = (@($py.Exe) + $py.Args) -join ' '
Write-Host "  $pyLabel  ->  Python $($py.Version)"
$short = [version]("{0}.{1}" -f $py.Version.Major, $py.Version.Minor)
if ($short -lt $PY_MIN) {
    throw ("Python $($py.Version) is below AnkusDrive's floor ($PY_MIN). " +
           'Install a newer Python, or pass -Python <path to python.exe>.')
} elseif ($short -gt $PY_MAX_VERIFIED) {
    # Not fatal - the deps are wheels-or-pure-Python and usually catch up fast. But say
    # so BEFORE pip does, so a resolver failure is attributable (#279).
    Write-Warning ("Python $($py.Version) is NEWER than the newest version AnkusDrive has been " +
                   "verified on ($PY_MAX_VERIFIED). The install may fail if mcp/numpy/Pillow have " +
                   "no wheels for it yet - if so, re-run with -Python pointing at a Python " +
                   "$PY_MAX_VERIFIED install, and please file an issue.")
} else {
    Write-Host "  supported (verified range $PY_MIN - $PY_MAX_VERIFIED)"
}

# --- 2. FreeCAD (report only; the client resolves it at runtime) ----------------
Write-Step 'FreeCAD'
function Find-Freecadcmd {
    if ($env:ANKUSDRIVE_FREECADCMD) { return $env:ANKUSDRIVE_FREECADCMD }
    # The same per-OS locations ankusdrive/client.py globs - version-globbed and derived
    # from the environment, never one hardcoded path (a 1.2 or per-user install works).
    $globs = @(
        'C:\Program Files\FreeCAD *\bin\freecadcmd.exe',
        'C:\Program Files (x86)\FreeCAD *\bin\freecadcmd.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\FreeCAD *\bin\freecadcmd.exe')
    )
    $hit = $globs |
        ForEach-Object { Get-ChildItem -Path $_ -ErrorAction SilentlyContinue } |
        Sort-Object FullName |          # 'FreeCAD 1.0' < 'FreeCAD 1.1' -> newest last
        Select-Object -Last 1
    if ($hit) { return $hit.FullName }
    $onPath = Get-Command freecadcmd, FreeCADCmd -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return $onPath.Source }
    return $null
}
$freecadcmd = Find-Freecadcmd
if ($freecadcmd) {
    Write-Host "  $freecadcmd"
} else {
    Write-Warning ('freecadcmd.exe not found. Install FreeCAD 1.1 from https://www.freecad.org/ ' +
                   '(default C:\Program Files\FreeCAD 1.1), or set $env:ANKUSDRIVE_FREECADCMD. The ' +
                   'install below still completes; `ankusdrive ping` will fail until FreeCAD exists.')
}

# --- 3. venv + editable install --------------------------------------------------
Write-Step "Virtual environment  ($VenvDir)"
$venvPy = Join-Path $VenvDir 'Scripts\python.exe'
if (Test-Path $venvPy) {
    Write-Host '  reusing existing venv'
} else {
    & $py.Exe @($py.Args) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed ('$pyLabel')" }
    Write-Host '  created'
}

Write-Step 'Installing ankusdrive (editable) + pinned host deps'
& $venvPy -m pip install --upgrade --quiet pip
if ($LASTEXITCODE -ne 0) { throw 'pip self-upgrade failed' }
# The pins live in pyproject.toml - notably `mcp>=1.2,<2` (#277). Deliberately no
# --upgrade/--pre here: the point is to resolve exactly what pyproject allows.
& $venvPy -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    throw ("pip install failed on Python $($py.Version). If this is a brand-new CPython, one of " +
           'mcp/numpy/Pillow likely has no wheel for it yet: re-run with -Python pointing at a ' +
           "Python $PY_MAX_VERIFIED (or older, >= $PY_MIN) install.")
}

# Prove the RESOLVED dependency set is the working one. `pip install` succeeding is not
# the same as `ankusdrive mcp` booting: mcp 2.x installs cleanly and then fails to import
# mcp.server.fastmcp (#277) while ping/doctor keep passing.
Write-Step 'Verifying the resolved host dependencies'
$probe = @'
import sys
from importlib.metadata import version     # the mcp package exposes no __version__
import numpy, PIL
from mcp.server.fastmcp import FastMCP     # the symbol ankusdrive/mcp_server.py needs (#277)
import ankusdrive
print("  python   %s" % sys.version.split()[0])
print("  ankusdrive %s" % ankusdrive.__version__)
print("  mcp      %s   (mcp.server.fastmcp imports OK)" % version("mcp"))
print("  numpy    %s" % numpy.__version__)
print("  Pillow   %s" % PIL.__version__)
'@
# Pipe over stdin, not -c: PowerShell 5.1 mangles embedded quotes handed to a native exe.
$probe | & $venvPy -
if ($LASTEXITCODE -ne 0) {
    throw ('the installed dependency set does not import - `ankusdrive mcp` would fail. Check the ' +
           'mcp version above against pyproject''s `mcp>=1.2,<2` (issue #277).')
}

# --- 4. optional pip-wheel solver extras ----------------------------------------
if ($Extras.Count -gt 0) {
    Write-Step 'Optional pip-wheel solver extras'
    # Best-effort and INDEPENDENT: one missing wheel must not abort the rest, and an
    # absent family degrades cleanly to {ok:false, reason, install}. $ErrorActionPreference
    # 'Stop' would turn a failing extra's stderr into a terminating NativeCommandError, so
    # drop to 'Continue' for the loop and gate on $LASTEXITCODE alone.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    foreach ($extra in $Extras) {
        & $venvPy -m pip install --quiet -e ".[$extra]" *> $null
        if ($LASTEXITCODE -eq 0) { Write-Host "  [$extra] installed" }
        else { Write-Warning "  [$extra] no wheel for Python $($py.Version) - skipped (family degrades cleanly)" }
    }
    $ErrorActionPreference = $prevEAP
}

# --- 5. verify against the real FreeCAD ------------------------------------------
if (-not $SkipDoctor) {
    Write-Step 'ankusdrive doctor'
    # doctor never fails on a missing SOLVER; it exits nonzero only when FreeCAD is
    # unresolved, which is a real and actionable failure to surface here. Native exit
    # codes must not abort the script before the MCP block prints, so stay on 'Continue'.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $venvPy -m ankusdrive doctor
    $doctorRc = $LASTEXITCODE
    if ($freecadcmd) {
        Write-Step 'ankusdrive ping (boots freecadcmd.exe)'
        & $venvPy -m ankusdrive ping
        if ($LASTEXITCODE -ne 0) { Write-Warning 'ping failed - see the FreeCAD item in the doctor report above.' }
    }
    $ErrorActionPreference = $prevEAP
    if ($doctorRc -ne 0) {
        Write-Warning 'doctor reported FreeCAD unresolved. Fix that before wiring the MCP host.'
    }

    # Prove the MCP surface itself, not just the CAD path. `ping`/`doctor` pass even when
    # `ankusdrive mcp` cannot boot (#277), which is precisely the gap #279's reporter fell
    # into - so complete a real stdio initialize + tools/list before printing the
    # registration block. Only present in a checkout; a pipx/PyPI install skips it.
    $bootTest = Join-Path $repo 'tests\test_mcp_boot.py'
    if (Test-Path $bootTest) {
        Write-Step 'MCP stdio boot check'
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & $venvPy $bootTest
        if ($LASTEXITCODE -ne 0) {
            $mcpBootFailed = $true
            Write-Warning 'the MCP server did not complete a stdio handshake - registering it with a host will fail.'
        }
        $ErrorActionPreference = $prevEAP
    }
}

if ($Persist) {
    Write-Step 'Persisting config (%APPDATA%\ankusdrive\config.toml)'
    # MCP hosts spawn the server with a minimal environment; the config file is the only
    # layer that survives that (#199).
    & $venvPy -m ankusdrive setup --yes
}

# --- 6. the MCP step (where #279's reporter got stuck) ---------------------------
Write-Step 'Register with your MCP host'
$ankusdriveExe = Join-Path $VenvDir 'Scripts\ankusdrive.exe'
if (-not (Test-Path $ankusdriveExe)) {
    Write-Warning "expected launcher at $ankusdriveExe - falling back to '$venvPy -m ankusdrive mcp'"
}
# `ankusdrive setup --print-mcp-config` (ankusdrive/setup_cmd.py, #201) is the single source
# of truth for this block: it resolves the launcher to an ABSOLUTE path, because a GUI MCP
# host does not inherit the shell PATH and a bare "ankusdrive" will not be found there.
& $venvPy -m ankusdrive setup --print-mcp-config
Write-Host ''
Write-Host 'On THIS machine the Claude Desktop config file is:'
Write-Host ('  ' + (Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'))
Write-Host '  (create it if it does not exist, then restart Claude Desktop)'
Write-Host ''
Write-Host 'Backslashes must be escaped inside JSON - "C:\\Users\\you\\AnkusDrive\\.venv\\Scripts\\ankusdrive.exe"'
Write-Host 'or just use forward slashes, which Windows accepts too.'

Write-Step 'Done'
Write-Host "  venv       $VenvDir"
Write-Host "  launcher   $ankusdriveExe"
Write-Host '  next       optional solvers:  powershell -ExecutionPolicy Bypass -File scripts\install-solvers.ps1'
Write-Host '             full Windows guide: docs\WINDOWS.md'

# Exit code discipline: a MISSING FreeCAD is a warning (install it and re-run doctor -
# the Python half of this script did its job), but an MCP server that cannot complete a
# stdio handshake means the thing the user came for does not work, so fail loudly. CI
# depends on this: GitHub Actions propagates the last $LASTEXITCODE, which would
# otherwise be the harmless `setup --print-mcp-config` above.
if ($mcpBootFailed) {
    Write-Error 'core install FAILED the MCP stdio boot check (see above)'
    exit 1
}
exit 0
