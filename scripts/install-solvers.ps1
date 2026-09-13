<#
.SYNOPSIS
  Provision AnkusDrive's external solvers on Windows - the Windows analog of
  scripts/install-solvers.sh (which is Linux/apt/conda + source builds).

.DESCRIPTION
  Windows splits into three install shapes, and this script automates the two that
  DON'T need a compiler or WSL:

    * pip-wheel solvers (MBD: pybullet/mujoco; topology: solidspy; optics: rayoptics +
      optiland; fluids: CoolProp) - a plain `pip install '.[<extra>]'` into the venv that
      launches `python -m ankusdrive mcp`. Same as Linux; verified to install on py3.13.

    * self-contained native binaries (CFD: SU2; slicing: PrusaSlicer) - downloaded as a
      portable zip, extracted under -Dir, and wired via a ANKUSDRIVE_<SOLVER>_PATH env var.
      No installer, no admin. SU2_CFD.exe and prusa-slicer-console.exe run standalone.

  It also REPORTS the two families that don't have a turnkey Windows path:

    * CalculiX (warpage) - already auto-detected: FreeCAD BUNDLES ccx.exe in its bin\, and
      ankusdrive/solvers.py discovers it there. Nothing to install; `ankusdrive doctor` shows
      `warpage ... ready` once FreeCAD is installed.
    * Elmer (thermal/CHT/EM/acoustic FEM: CFD-adjacent families) is ALSO a portable
      zip now - the 'elmer' target downloads the no-GUI build and wires
      ANKUSDRIVE_ELMER_PATH (there is no Elmer winget package).
    * OpenFOAM families (CFD solve / FSI / injection molding) - WSL2-backed since
      issue #193: the 'wsl' target provisions them INSIDE the default distro (the
      Linux install-solvers.sh runs verbatim there) and ankusdrive discovers/launches
      them through \wsl$ + `wsl -e bash` automatically. One-time prerequisite:
      `wsl --install -d Ubuntu` + reboot.
    * YADE, openEMS, bempp - still need Linux mechanisms; same WSL route works
      manually (see docs/WINDOWS.md).

  STUDIO RENDER (issue #335): the 'blender' target installs full Blender for
  render_photoreal's studio backend - the pinned official portable zip (SHA-256
  checked) extracted under -Dir, where ankusdrive/solvers.py discovers blender.exe with
  NO env var. 'blender-winget' uses `winget install BlenderFoundation.Blender` instead
  (per-machine, lands in Program Files\Blender Foundation\Blender X.Y - also
  auto-discovered). ~1 GB, GPL-3.0, run only as a subprocess; never part of 'all'.

  DISCOVERY: a family resolves its solver via ankusdrive/solvers.py - a wheel must import in
  the venv; a binary is found via ANKUSDRIVE_<SOLVER>_PATH -> PATH -> per-OS dirs -> (for
  ccx) FreeCAD's bundled bin. So the wheel installs MUST target the same venv that runs
  `ankusdrive mcp`, and a downloaded binary must be on PATH or named by its env var. Verify
  with `ankusdrive doctor` (or this script's `list`).

  CORE INSTALL FIRST: this script provisions the OPTIONAL solvers and assumes the venv
  that runs `ankusdrive mcp` already exists. The venv + pip + doctor + MCP-registration
  step is scripts\install-core.ps1 (issue #279); the 'core' target below just forwards
  to it so a user who found only this script isn't stranded.

.PARAMETER Targets
  Any of: core pip su2 prusaslicer elmer wsl blender blender-winget all list.
  Default (no args) = pip + guidance.

.PARAMETER Dir
  Where portable binaries are extracted. Default: %LOCALAPPDATA%\AnkusDrive\solvers.

.PARAMETER Persist
  Also `setx` the ANKUSDRIVE_<SOLVER>_PATH vars so they survive new shells and MCP-host
  launches (else they are only set for THIS shell and the script prints the setx lines).

.EXAMPLE
  pwsh scripts\install-solvers.ps1 core                  # -> scripts\install-core.ps1
  pwsh scripts\install-solvers.ps1                       # pip extras + guidance
  pwsh scripts\install-solvers.ps1 su2 prusaslicer -Persist
  pwsh scripts\install-solvers.ps1 blender               # studio render backend
  pwsh scripts\install-solvers.ps1 list                 # ankusdrive doctor
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Targets = @('pip'),
    [string]$Dir = (Join-Path $env:LOCALAPPDATA 'AnkusDrive\solvers'),
    [switch]$Persist
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# Pinned, verified-on-Windows releases (override by editing these).
$SU2_VER  = '8.5.0'
$SU2_URL  = "https://github.com/su2code/SU2/releases/download/v$SU2_VER/SU2-v$SU2_VER-win64-omp.zip"
$PRUSA_VER = '2.9.6'
$PRUSA_URL = "https://github.com/prusa3d/PrusaSlicer/releases/download/version_$PRUSA_VER/PrusaSlicer-$PRUSA_VER.zip"
# Elmer ships a PORTABLE no-GUI zip (no installer, no admin; there is NO winget package
# despite older guidance). nogui-nompi is all AnkusDrive needs: ElmerSolver + ElmerGrid +
# ViewFactors as plain subprocesses.
$ELMER_URL = 'https://www.nic.funet.fi/pub/sci/physics/elmer/bin/windows/ElmerFEM-nogui-nompi-Windows-AMD64.zip'
# Blender 5.2 LTS portable zip; the hash is the official blender-5.2.1.sha256 manifest
# (must match BLENDER_SHA_* in scripts/install-renderers.sh). download.blender.org
# challenges scripted clients, so official mirrors are tried first.
$BLENDER_VER = '5.2.1'
$BLENDER_SERIES = '5.2'
$BLENDER_SHA_WIN_X64 = '0e631dad7d0cad6d5d18abdd2e2550f6c0213215334eda00ddbd3d22b96ecb2c'
$BLENDER_MIRRORS = @(
    'https://mirrors.ocf.berkeley.edu/blender/release',
    'https://ftp.nluug.nl/pub/graphics/blender/release',
    'https://mirror.clarkson.edu/blender/release',
    'https://download.blender.org/release'
)

$venvPy = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Warning "No .venv at $venvPy - run 'scripts\install-core.ps1' first (pip targets need it)."
}

function Install-Core {
    # The core install has its own script (#279) - venv, pinned pip install, doctor, and
    # the MCP registration block with resolved absolute paths. Forwarded, not duplicated.
    Write-Host '== core install (venv + pip + doctor + MCP registration) =='
    & (Join-Path $PSScriptRoot 'install-core.ps1')
}

function Set-SolverEnv([string]$name, [string]$value) {
    # This shell now; optionally persist for future shells + MCP hosts.
    Set-Item -Path "Env:$name" -Value $value
    if ($Persist) {
        & setx $name $value | Out-Null
        Write-Host "  persisted (setx) $name"
    } else {
        Write-Host "  to persist across shells/MCP hosts:  setx $name `"$value`""
    }
}

function Get-Portable([string]$label, [string]$url, [string]$destName) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $zip  = Join-Path $Dir ("{0}.zip" -f $destName)
    $dest = Join-Path $Dir $destName
    Write-Host "Downloading $label ..."
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $url -OutFile $zip
    Write-Host ("  {0:N1} MB -> extracting" -f ((Get-Item $zip).Length / 1MB))
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Expand-Archive -Path $zip -DestinationPath $dest -Force
    # Some release zips nest a second zip (SU2) or a single top dir (PrusaSlicer).
    $innerZip = Get-ChildItem -Path $dest -Filter '*.zip' -Recurse | Select-Object -First 1
    if ($innerZip) { Expand-Archive -Path $innerZip.FullName -DestinationPath $dest -Force; Remove-Item $innerZip.FullName }
    Remove-Item $zip
    return $dest
}

function Install-Pip {
    if (-not (Test-Path $venvPy)) { throw 'pip targets need the .venv (run tests\setup_local.ps1)' }
    Write-Host '== pip-wheel solver extras (mbd, topology, optics, fluids) =='
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    foreach ($extra in 'mbd', 'topology', 'optics', 'fluids') {
        & $venvPy -m pip install --quiet -e ".[$extra]" *> $null
        if ($LASTEXITCODE -eq 0) { Write-Host "  [$extra] installed" }
        else { Write-Warning "  [$extra] no wheel for this Python - skipped (family degrades cleanly)" }
    }
    $ErrorActionPreference = $prev
}

function Install-SU2 {
    Write-Host '== SU2 (CFD, steady) =='
    $d = Get-Portable "SU2 $SU2_VER win64-omp" $SU2_URL "SU2-$SU2_VER"
    $exe = Get-ChildItem -Path $d -Recurse -Filter 'SU2_CFD.exe' | Select-Object -First 1
    if (-not $exe) { throw "SU2_CFD.exe not found under $d" }
    Write-Host "  SU2_CFD.exe -> $($exe.FullName)"
    Set-SolverEnv 'ANKUSDRIVE_SU2_PATH' $exe.FullName
}

function Install-Prusa {
    Write-Host '== PrusaSlicer (slicing) =='
    $d = Get-Portable "PrusaSlicer $PRUSA_VER" $PRUSA_URL "PrusaSlicer-$PRUSA_VER"
    $exe = Get-ChildItem -Path $d -Recurse -Filter 'prusa-slicer-console.exe' | Select-Object -First 1
    if (-not $exe) { throw "prusa-slicer-console.exe not found under $d" }
    Write-Host "  prusa-slicer-console.exe -> $($exe.FullName)"
    Set-SolverEnv 'ANKUSDRIVE_PRUSASLICER_PATH' $exe.FullName
}

function Install-Elmer {
    Write-Host '== Elmer (transient/radiation thermal, CHT, low-freq EM, acoustic/harmonic FEM) =='
    $d = Get-Portable 'Elmer nogui-nompi' $ELMER_URL 'ElmerFEM-nogui-nompi'
    $exe = Get-ChildItem -Path $d -Recurse -Filter 'ElmerSolver.exe' | Select-Object -First 1
    if (-not $exe) { throw "ElmerSolver.exe not found under $d" }
    Write-Host "  ElmerSolver.exe -> $($exe.FullName)"
    foreach ($companion in 'ElmerGrid.exe', 'ViewFactors.exe') {
        if (-not (Test-Path (Join-Path $exe.DirectoryName $companion))) {
            Write-Warning "  $companion not found next to ElmerSolver.exe"
        }
    }
    Set-SolverEnv 'ANKUSDRIVE_ELMER_PATH' $exe.FullName
}

function Install-Blender {
    Write-Host "== Blender $BLENDER_VER (studio render backend for render_photoreal) =="
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        throw 'pinned zip is x64; on Windows ARM64 use: winget install BlenderFoundation.Blender'
    }
    $name = "blender-$BLENDER_VER-windows-x64"
    $dest = Join-Path $Dir "blender-$BLENDER_VER"
    $exe  = Join-Path $dest "$name\blender.exe"
    if ((Test-Path $exe) -and -not $env:FORCE) {
        Write-Host "  already installed: $exe  (set FORCE=1 to reinstall)"
    } else {
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        $zip = Join-Path $Dir "$name.zip"
        $ProgressPreference = 'SilentlyContinue'
        $got = $false
        foreach ($m in $BLENDER_MIRRORS) {
            try {
                Write-Host "  downloading $name.zip from $m"
                Invoke-WebRequest -Uri "$m/Blender$BLENDER_SERIES/$name.zip" -OutFile $zip -UserAgent 'ankusdrive-install-solvers'
                $got = $true; break
            } catch { Write-Warning "  mirror failed: $m ($($_.Exception.Message))" }
        }
        if (-not $got) { throw "could not download $name.zip from any mirror" }
        $hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
        if ($hash -ne $BLENDER_SHA_WIN_X64) {
            Remove-Item $zip
            throw "checksum mismatch for $name.zip: expected $BLENDER_SHA_WIN_X64, got $hash"
        }
        Write-Host "  sha256 $hash"
        if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        Write-Host '  extracting (tar is much faster than Expand-Archive for ~20k files)'
        if (Get-Command tar -ErrorAction SilentlyContinue) { & tar -xf $zip -C $dest }
        else { Expand-Archive -Path $zip -DestinationPath $dest -Force }
        Remove-Item $zip
        if (-not (Test-Path $exe)) { throw "blender.exe not found at $exe after extract" }
    }
    $ver = (& $exe --background --factory-startup --version 2>$null | Select-Object -First 1)
    if (-not $ver) { throw "$exe did not run (missing VC++ runtime? install it, then retry)" }
    Write-Host "  $ver -> $exe"
    Write-Host '  auto-discovered under -Dir (no env var needed); verify: render_capabilities or ankusdrive doctor (studio_render)'
    if ($Dir -ne (Join-Path $env:LOCALAPPDATA 'AnkusDrive\solvers')) {
        Set-SolverEnv 'ANKUSDRIVE_BLENDER_PATH' $exe          # non-default -Dir is not globbed
    }
}

function Install-BlenderWinget {
    Write-Host '== Blender via winget (per-machine install) =='
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'winget not available - use the portable target instead: install-solvers.ps1 blender'
    }
    & winget install --id BlenderFoundation.Blender --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget install failed ($LASTEXITCODE)" }
    Write-Host '  installed under Program Files\Blender Foundation - auto-discovered; verify with ankusdrive doctor'
}

function Install-WslSolvers {
    Write-Host '== OpenFOAM families (CFD / FSI / injection molding) via WSL2 (issue #193) =='
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
        throw 'WSL not installed: run "wsl --install -d Ubuntu", reboot, then re-run this target'
    }
    # The repo is visible in-distro at /mnt/<drive>/... and *.sh are LF-normalized
    # (.gitattributes), so the Linux provisioning scripts run verbatim inside the
    # distro. The cfd/fsi targets PRINT the validated apt + source-build recipes
    # (they need sudo + network); run those inside the distro, then re-run
    # `ankusdrive doctor` here - discovery probes \wsl$ automatically.
    wsl -e bash -lc 'bash scripts/install-solvers.sh cfd fsi'
    Write-Host ''
    Write-Host 'Injection molding (openInjMoldSim, needs a parallel OpenFOAM-7 .org source'
    Write-Host 'build - multi-hour): inside the distro run:'
    Write-Host '  bash tools/build_openinjmoldsim.sh --build'
    Write-Host 'IMPORTANT: clone/build INSIDE the distro filesystem (~), not /mnt/c - the'
    Write-Host '9P mount is slow for compiles. Case dirs staying on /mnt/c is fine.'
    Write-Host 'Override the probed distro with ANKUSDRIVE_WSL_DISTRO (env or config.toml).'
}

function Show-List {
    if (Test-Path $venvPy) { & $venvPy -m ankusdrive doctor }
    else { Write-Warning 'no .venv - cannot run ankusdrive doctor' }
}

$did = $false
foreach ($t in $Targets) {
    switch ($t.ToLower()) {
        'core'        { Install-Core; $did = $true }
        'pip'         { Install-Pip; $did = $true }
        'su2'         { Install-SU2; $did = $true }
        'prusaslicer' { Install-Prusa; $did = $true }
        'prusa'       { Install-Prusa; $did = $true }
        'elmer'       { Install-Elmer; $did = $true }
        'wsl'         { Install-WslSolvers; $did = $true }
        'blender'     { Install-Blender; $did = $true }
        'blender-winget' { Install-BlenderWinget; $did = $true }
        'all'         { Install-Pip; Install-SU2; Install-Prusa; Install-Elmer; $did = $true }
        'list'        { Show-List; $did = $true }
        default       { Write-Warning "unknown target '$t' (use: core pip su2 prusaslicer elmer wsl blender blender-winget all list)" }
    }
}
if (-not $did) { Install-Pip }

# CalculiX is never 'installed' here - it rides on FreeCAD's bundled ccx.exe.
Write-Host ''
Write-Host 'CalculiX (warpage): auto-detected from FreeCAD''s bundled ccx.exe - nothing to install.'
Write-Host 'Verify everything with:  pwsh scripts\install-solvers.ps1 list   (== ankusdrive doctor)'
