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

  DISCOVERY: a family resolves its solver via ankusdrive/solvers.py - a wheel must import in
  the venv; a binary is found via ANKUSDRIVE_<SOLVER>_PATH -> PATH -> per-OS dirs -> (for
  ccx) FreeCAD's bundled bin. So the wheel installs MUST target the same venv that runs
  `ankusdrive mcp`, and a downloaded binary must be on PATH or named by its env var. Verify
  with `ankusdrive doctor` (or this script's `list`).

.PARAMETER Targets
  Any of: pip su2 prusaslicer elmer wsl all list. Default (no args) = pip + guidance.

.PARAMETER Dir
  Where portable binaries are extracted. Default: %LOCALAPPDATA%\AnkusDrive\solvers.

.PARAMETER Persist
  Also `setx` the ANKUSDRIVE_<SOLVER>_PATH vars so they survive new shells and MCP-host
  launches (else they are only set for THIS shell and the script prints the setx lines).

.EXAMPLE
  pwsh scripts\install-solvers.ps1                       # pip extras + guidance
  pwsh scripts\install-solvers.ps1 su2 prusaslicer -Persist
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

$venvPy = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Warning "No .venv at $venvPy - run 'pwsh tests\setup_local.ps1' first (pip targets need it)."
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
        'pip'         { Install-Pip; $did = $true }
        'su2'         { Install-SU2; $did = $true }
        'prusaslicer' { Install-Prusa; $did = $true }
        'prusa'       { Install-Prusa; $did = $true }
        'elmer'       { Install-Elmer; $did = $true }
        'wsl'         { Install-WslSolvers; $did = $true }
        'all'         { Install-Pip; Install-SU2; Install-Prusa; Install-Elmer; $did = $true }
        'list'        { Show-List; $did = $true }
        default       { Write-Warning "unknown target '$t' (use: pip su2 prusaslicer elmer wsl all list)" }
    }
}
if (-not $did) { Install-Pip }

# CalculiX is never 'installed' here - it rides on FreeCAD's bundled ccx.exe.
Write-Host ''
Write-Host 'CalculiX (warpage): auto-detected from FreeCAD''s bundled ccx.exe - nothing to install.'
Write-Host 'Verify everything with:  pwsh scripts\install-solvers.ps1 list   (== ankusdrive doctor)'
