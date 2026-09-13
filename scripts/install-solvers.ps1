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
  NO env var. ~1 GB, GPL-3.0, run only as a subprocess; never part of 'all'.
  'blender-msi' is the PER-MACHINE variant: the same official .msi, fetched from the
  mirrors with the pinned SHA-256 and installed silently into Program Files\Blender
  Foundation\Blender X.Y (also auto-discovered). Needs an ELEVATED shell.
  'blender-winget' would do the same through winget, but CURRENTLY FAILS: winget fetches
  the MSI from download.blender.org, which 403s scripted clients, and winget cannot be
  pointed at a mirror. Prefer 'blender' (no admin) or 'blender-msi'.
  Both arches are pinned - Blender ships Windows arm64 builds from 5.x.

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
  Any of: core pip su2 prusaslicer elmer wsl blender blender-msi blender-winget all list.
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
  pwsh scripts\install-solvers.ps1 blender               # studio render backend (no admin)
  pwsh scripts\install-solvers.ps1 blender-msi           # same, per-machine (needs admin)
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
# Blender 5.2 LTS. The VERSION must match BLENDER_VERSION in scripts/install-renderers.sh
# (tests/test_blender_render.py::test_installers_pin_the_same_blender); the hashes differ
# because the two scripts pin different artifacts of the same release.
$BLENDER_VER = '5.2.1'
$BLENDER_SERIES = '5.2'
# Every pinned Windows artifact, straight out of the official blender-5.2.1.sha256
# manifest (fetched from a mirror; download.blender.org 403s scripted clients). The
# x64 .msi hash is corroborated by the winget manifest's own Installer SHA256.
# Blender DOES ship Windows arm64 builds from 5.x, so both arches are pinned.
$BLENDER_SHA = @{
    'x64'   = @{
        zip = '0e631dad7d0cad6d5d18abdd2e2550f6c0213215334eda00ddbd3d22b96ecb2c'
        msi = 'bebb90fc5bf7e3ec7ab4eb34f4c5a5b54e28e582a722152a47fd4ee66ec3c6fa'
    }
    'arm64' = @{
        zip = '62047464d21ea954b997db94bf5518eac9240b900d98bb324bc02416ad892860'
        msi = '7c87fadd9611739111b71e2cb6c083d976876aa71cbb1f80e63bfc81d175da25'
    }
}
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

function Get-BlenderArch {
    # PROCESSOR_ARCHITECTURE reports the *process* arch, so a 32-bit or x64-emulated
    # PowerShell on an ARM64 box says x86/AMD64; PROCESSOR_ARCHITEW6432 carries the
    # real machine arch in exactly that case.
    if ('ARM64' -in @($env:PROCESSOR_ARCHITECTURE, $env:PROCESSOR_ARCHITEW6432)) { return 'arm64' }
    return 'x64'
}

function Get-BlenderDownload([string]$file, [string]$expected, [string]$outFile) {
    # Fetch one official Blender artifact, mirrors first, and verify the pinned SHA-256.
    # download.blender.org challenges scripted clients (403), hence the mirror list.
    $ProgressPreference = 'SilentlyContinue'
    $got = $false
    foreach ($m in $BLENDER_MIRRORS) {
        try {
            Write-Host "  downloading $file from $m"
            Invoke-WebRequest -Uri "$m/Blender$BLENDER_SERIES/$file" -OutFile $outFile -UserAgent 'ankusdrive-install-solvers'
            $got = $true; break
        } catch { Write-Warning "  mirror failed: $m ($($_.Exception.Message))" }
    }
    if (-not $got) { throw "could not download $file from any mirror" }
    $hash = (Get-FileHash -Algorithm SHA256 $outFile).Hash.ToLower()
    if ($hash -ne $expected) {
        Remove-Item $outFile
        throw "checksum mismatch for ${file}: expected $expected, got $hash"
    }
    Write-Host "  sha256 $hash"
}

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Install-Blender {
    Write-Host "== Blender $BLENDER_VER (studio render backend for render_photoreal) =="
    $arch = Get-BlenderArch
    $name = "blender-$BLENDER_VER-windows-$arch"
    $dest = Join-Path $Dir "blender-$BLENDER_VER"
    $exe  = Join-Path $dest "$name\blender.exe"
    if ((Test-Path $exe) -and -not $env:FORCE) {
        Write-Host "  already installed: $exe  (set FORCE=1 to reinstall)"
    } else {
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        $zip = Join-Path $Dir "$name.zip"
        Get-BlenderDownload "$name.zip" $BLENDER_SHA[$arch].zip $zip
        if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        Write-Host '  extracting (tar is much faster than Expand-Archive for ~20k files)'
        if (Get-Command tar -ErrorAction SilentlyContinue) { & tar -xf $zip -C $dest }
        else { Expand-Archive -Path $zip -DestinationPath $dest -Force }
        Remove-Item $zip
        if (-not (Test-Path $exe)) { throw "blender.exe not found at $exe after extract" }
    }
    Write-Host "  $(Get-BlenderVersion $exe) -> $exe"
    Write-Host '  auto-discovered under -Dir (no env var needed); verify: render_capabilities or ankusdrive doctor (studio_render)'
    if ($Dir -ne (Join-Path $env:LOCALAPPDATA 'AnkusDrive\solvers')) {
        Set-SolverEnv 'ANKUSDRIVE_BLENDER_PATH' $exe          # non-default -Dir is not globbed
    }
}

function Get-BlenderVersion([string]$exe) {
    # Windows PowerShell 5.1 wraps a native command's stderr in an ErrorRecord when it
    # is redirected, which $ErrorActionPreference='Stop' turns TERMINATING - so a
    # Blender that prints any driver/GPU warning on startup would fail the install here,
    # AFTER a perfectly good install. Same guard Install-Pip uses.
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $ver = (& $exe --background --factory-startup --version 2>$null | Select-Object -First 1)
    $ErrorActionPreference = $prev
    if (-not $ver) { throw "$exe did not run (missing VC++ runtime? install it, then retry)" }
    return $ver
}

function Install-BlenderMsi {
    # The per-machine install 'blender-winget' was meant to give, without winget: the
    # SAME official .msi, fetched from the mirrors with the pinned SHA-256 (winget can
    # only fetch it from download.blender.org, which 403s scripted clients - see
    # Install-BlenderWinget). Lands in Program Files\Blender Foundation\Blender <series>,
    # which solvers.py already globs, so no env var. NEEDS ADMIN; never part of 'all'.
    Write-Host "== Blender $BLENDER_VER per-machine MSI (Program Files) =="
    $arch = Get-BlenderArch
    $expected = Join-Path $env:ProgramFiles "Blender Foundation\Blender $BLENDER_SERIES\blender.exe"
    if ((Test-Path $expected) -and -not $env:FORCE) {
        Write-Host "  already installed: $expected  (set FORCE=1 to reinstall)"
        Write-Host "  $(Get-BlenderVersion $expected) -> $expected"
        return
    }
    if (-not (Test-Elevated)) {
        throw ("a per-machine MSI needs an elevated shell. Either re-run this target from " +
               "an Administrator PowerShell, or use the no-admin portable target instead " +
               "(same Blender, auto-discovered):  scripts\install-solvers.ps1 blender")
    }
    $name = "blender-$BLENDER_VER-windows-$arch.msi"
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $msi = Join-Path $Dir $name
    Get-BlenderDownload $name $BLENDER_SHA[$arch].msi $msi
    Write-Host '  msiexec /i /qn /norestart (silent, per-machine)'
    # Not `& msiexec` - msiexec returns immediately and the real exit code only comes
    # back by waiting on the process.
    $log = Join-Path $Dir 'blender-msi.log'
    $p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @(
        '/i', "`"$msi`"", '/qn', '/norestart', '/L*v', "`"$log`"")
    Remove-Item $msi -ErrorAction SilentlyContinue
    if ($p.ExitCode -eq 3010) {
        Write-Warning '  msiexec asked for a reboot (3010); the files are in place'
    } elseif ($p.ExitCode -ne 0) {
        throw "msiexec failed ($($p.ExitCode)); verbose log: $log"
    }
    if (-not (Test-Path $expected)) {
        throw "MSI reported success but blender.exe is not at $expected (log: $log)"
    }
    Remove-Item $log -ErrorAction SilentlyContinue
    Write-Host "  $(Get-BlenderVersion $expected) -> $expected"
    Write-Host '  auto-discovered via the Program Files glob (no env var needed); verify: ankusdrive doctor (studio_render)'
}

function Install-BlenderWinget {
    # KNOWN TO FAIL as of 2026-09 (verified on Windows 11): the BlenderFoundation.Blender
    # manifest points its installer at download.blender.org, the SAME host that answers
    # 403 to every scripted client (the Cloudflare challenge that made Install-Blender
    # try mirrors first). winget cannot be pointed at a mirror, so it dies with
    #   0x80190193 : Forbidden (403)
    # after resolving the package. Kept because it starts working again the moment
    # Blender's CDN stops challenging winget, but 'blender' is the target that works
    # today - so the failure below names it instead of surfacing a bare exit code.
    Write-Host '== Blender via winget (per-machine install) =='
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'winget not available - use the portable target instead: install-solvers.ps1 blender'
    }
    & winget install --id BlenderFoundation.Blender --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw ("winget install failed ($LASTEXITCODE). winget downloads the MSI from " +
               "download.blender.org, which returns 403 to scripted clients, so this " +
               "target is expected to fail until Blender's CDN stops challenging it. " +
               "Use the portable target, which tries official mirrors first and is " +
               "auto-discovered all the same:  scripts\install-solvers.ps1 blender")
    }
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
        'blender-msi' { Install-BlenderMsi; $did = $true }
        'blender-winget' { Install-BlenderWinget; $did = $true }
        'all'         { Install-Pip; Install-SU2; Install-Prusa; Install-Elmer; $did = $true }
        'list'        { Show-List; $did = $true }
        default       { Write-Warning "unknown target '$t' (use: core pip su2 prusaslicer elmer wsl blender blender-msi blender-winget all list)" }
    }
}
if (-not $did) { Install-Pip }

# CalculiX is never 'installed' here - it rides on FreeCAD's bundled ccx.exe.
Write-Host ''
Write-Host 'CalculiX (warpage): auto-detected from FreeCAD''s bundled ccx.exe - nothing to install.'
Write-Host 'Verify everything with:  pwsh scripts\install-solvers.ps1 list   (== ankusdrive doctor)'
