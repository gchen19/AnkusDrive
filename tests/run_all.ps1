<#
.SYNOPSIS
  Run the AnkusDrive test suite on Windows - the Windows-native analog of tests/run_all.sh.

.DESCRIPTION
  This is NOT a line-for-line port of run_all.sh. Two structural differences make the
  Windows lane its own thing ("testing unique for Windows machines"):

    1. ONE interpreter. run_all.sh splits work across system python3 (FreeCAD worker
       tests) and a .venv python pointed at FreeCAD's bundled python (numpy/Pillow
       tests). On Windows the worker runs freecadcmd.exe as a SUBPROCESS, so the driver
       never imports FreeCAD; a single `.venv` from `pip install -e .` has mcp + numpy +
       Pillow and drives freecadcmd.exe - it covers everything. See setup_local.ps1.

    2. A documented SKIP list. The live external-solver families - OpenFOAM (CFD / FSI /
       injection molding), YADE (DEM), openEMS (full-wave EM), bempp (exterior acoustics
       BEM), preCICE - are Linux-only (source-built, .so adapters, `bash`/LD_LIBRARY_PATH
       glue). Their heavy solves are already gated off by RUN_HEAVY_SOLVES, but a few of
       their test *modules* assume a POSIX toolchain even in the analytic-oracle half, so
       they are skipped outright here rather than run-and-crash. CalculiX (ccx.exe) and
       gmsh.exe ARE bundled with FreeCAD on Windows, so structural FEM runs for real.

  Unlike run_all.sh (`set -e`, stops on the first failure) this runs EVERY selected
  module and prints a summary, so one Windows regression doesn't mask the rest. Exit
  code is nonzero if any selected module failed.

  Gates (same env vars as run_all.sh):
    RUN_HEAVY_SOLVES=1   also run live external-solver solves where the solver exists
    RUN_PERF=1           run the perf baselines (noisy)
    RUN_RELIABILITY=1    run the paid reliability layers (needs an API key)

.EXAMPLE
  pwsh tests\run_all.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$Py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) {
    Write-Error "No .venv found at $Py - run 'pwsh tests\setup_local.ps1' first."
    exit 1
}

# Force UTF-8 so the tests' non-ASCII output (arrows, em-dashes) doesn't trip the
# console's cp1252 default and abort a step with a UnicodeEncodeError. This is now
# purely for console OUTPUT: file reads/writes carry explicit encoding="utf-8" at
# the call sites (issue #204), so a decode crash no longer depends on this being set.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

# Headless CI: pin matplotlib to Agg. With PySide on the box (rayoptics pulls it),
# matplotlib would otherwise select QtAgg — whose QtCore import dies against the
# FreeCAD-bundled-python venv's Qt DLLs ("DLL load failed", the 1/80 lane failure
# in rayoptics' import of its mpl-based environment module).
$env:MPLBACKEND = 'Agg'

$script:failures = @()
$script:ran = 0

function Invoke-Step {
    <#  Run one test module (or arbitrary python args) under the venv interpreter,
        print a header, and record pass/fail without aborting the run. #>
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string[]]$PyArgs
    )
    Write-Host ''
    Write-Host "== $Label ==" -ForegroundColor Cyan
    & $Py @PyArgs
    $code = $LASTEXITCODE
    $script:ran++
    if ($code -ne 0) {
        $script:failures += $Label
        Write-Host "   FAILED ($Label) exit=$code" -ForegroundColor Red
    }
}

function Test-Module {
    param([Parameter(Mandatory)][string]$Label, [Parameter(Mandatory)][string]$Rel)
    Invoke-Step -Label $Label -PyArgs @((Join-Path 'tests' $Rel))
}

# --- lint (mirrors CI fast-checks; config in pyproject.toml) --------------------
Write-Host '== Lint (ruff E9,F - mirrors CI fast-checks) ==' -ForegroundColor Cyan
$ruff = Join-Path $repo '.venv\Scripts\ruff.exe'
if (Test-Path $ruff) {
    & $ruff check ankusdrive tests
    if ($LASTEXITCODE -ne 0) { $script:failures += 'ruff'; Write-Host '   FAILED (ruff)' -ForegroundColor Red }
    $script:ran++
} else {
    Write-Warning "ruff not in .venv - 'pip install ruff==0.15.15' to lint locally as CI does (skipping)"
}

# --- Windows-native contract (the piece unique to this lane) --------------------
Test-Module 'Windows FreeCAD resolution + doctor (cross-platform discovery, #191/#198)' 'test_windows_support.py'
Test-Module 'WSL routing for OpenFOAM families (#193)' 'test_wsl_routing.py'
# The MCP half of the first-run path (#279): the server boots over stdio and lists its
# tools with FreeCAD deliberately unresolved, so "MCP is broken" is distinguishable from
# "FreeCAD isn't found" - the confusion that ended #279's install.
Test-Module 'MCP stdio boot without FreeCAD (#279)' 'test_mcp_boot.py'
# The other half: the tools actually work over MCP, driving the real worker. Needs
# FreeCAD, so it belongs to THIS lane and not the hosted core-install one. Could not
# run on Windows at all until #288 stopped it hardcoding .venv/bin/python3.
Test-Module 'MCP tool suite driving the worker (#288)' 'test_mcp.py'

# --- static contracts + pure-Python toys (all cross-platform, no FreeCAD) -------
Test-Module 'Static contracts (registry parity + docstrings + determinism coverage)' 'test_contracts.py'
# Builds a wheel + sdist into a temp dir and asserts every runtime corpus is inside
# (#234, #249). Cross-platform: it shells out to the SAME interpreter it runs under,
# needs no FreeCAD, and SKIPs cleanly if that interpreter has no build backend.
Test-Module 'Naming: the pre-rename name stays gone (#295)' 'test_naming.py'
Test-Module 'PII: nobody''s machine gets into the tree (#303)' 'test_pii.py'
Test-Module 'Rename compatibility shims: env / config / DP_* props (#295)' 'test_compat_rename.py'
Test-Module 'Packaging: built wheel/sdist carries its data corpora (#249)' 'test_package_data.py'
Test-Module 'Typed units / quantity layer'          'test_units.py'
Test-Module 'Materials DB toys'                      'test_materials.py'
Test-Module 'Machine-element rating toys'            'test_machine_elements.py'
Test-Module 'Tolerance & GD&T toys'                  'test_tolerance.py'
Test-Module 'Standard reference tables'              'test_standards.py'
Test-Module 'Wear / fatigue / fracture toys'         'test_durability.py'
Test-Module 'Lumped transient thermal toys'          'test_thermal.py'
Test-Module 'Convection-coefficient screening toys'  'test_convection.py'
Test-Module 'Fluid thermophysical corpus (CoolProp oracle)' 'test_fluids.py'
Test-Module 'Tier A: acoustics'                      'test_acoustics.py'
Test-Module 'Tier A: plates'                         'test_plates.py'
Test-Module 'Tier A: buckling'                       'test_buckling.py'
Test-Module 'Tier A: molding'                        'test_molding.py'
Test-Module 'Tier A: molding fill'                   'test_molding_fill.py'
Test-Module 'Tier A: impact'                         'test_impact.py'
Test-Module 'Design-for-X toys'                      'test_dfx.py'
Test-Module 'Design-for-Cost toys'                   'test_cost.py'
Test-Module 'Tolerance-cost coupling (#235)'         'test_tolerance_cost.py'
Test-Module 'CNC machinability + time (#231, pure)'  'test_machining.py'
Test-Module 'FDM slice-estimate toys'                'test_slicing.py'
Test-Module 'Async job-registry toys'                'test_jobs.py'
Test-Module 'SU2 plane-channel case (#237)'           'test_su2_case.py'
Test-Module 'Main-thread work queue (#260)'          'test_mainthread.py'
Test-Module 'Random-vibration toys'                  'test_vibration.py'
Test-Module 'Solver degradation contract'            'test_solve_degradation.py'
# Blender studio-render contract (#335). Pure: no Blender and no FreeCAD, and it
# asserts install-solvers.ps1 and install-renderers.sh pin the SAME Blender - a
# check that is only meaningful if the Windows lane actually runs it.
Test-Module 'Blender studio-render contract (#335)'  'test_blender_render.py'
Test-Module 'Persistent config layer'                'test_config.py'
# mcp-2.0.0 break is injected, never installed; the live half spawns the server.
Test-Module "doctor's MCP preflight (#278)"          'test_doctor_mcp.py'
Test-Module 'Planar-kinematics toys'                 'test_kinematics.py'
Test-Module 'Internal-flow / Hagen-Poiseuille toys'  'test_cfd.py'
Test-Module 'Solution verification (Richardson/GCI)'  'test_verification.py'
Test-Module 'Performance contracts (declare/verify)'  'test_performance.py'
Test-Module 'DOE studies (sampling; swept D^-4 oracle)' 'test_study.py'
Test-Module 'Optimize-to-spec (Nelder-Mead; binding-constraint oracle)' 'test_optimize.py'
Test-Module 'Optics toys (Snell/Fresnel oracle; rayoptics gate when present)' 'test_optics.py'

# --- external-solver families: analytic-oracle / case-gen halves ----------------
# Elmer + OpenFOAM CASE GENERATION is pure-Python file emission and runs anywhere; the
# actual solve is gated on the solver being present (absent on Windows -> skipped).
Test-Module 'Elmer transient-thermal (case gen; solve gated)'       'test_elmer.py'
Test-Module 'Acoustic FEM / HelmholtzSolve (case gen; solve gated)' 'test_acoustic_fem.py'
Test-Module 'Harmonic FRF / StressSolve (case gen; solve gated)'    'test_harmonic_fem.py'
Test-Module 'Conjugate heat transfer (oracle; Elmer gated)'         'test_cht.py'
Test-Module 'Low-frequency EM (oracles; Elmer gated)'               'test_em.py'

# --- numpy-backed toys ----------------------------------------------------------
Test-Module 'MBD dynamics (PyBullet; skips if the mbd extra has no Windows wheel)' 'test_mbd.py'
Test-Module 'Topology-optimization toys (SIMP; numpy)'   'test_topology.py'
Test-Module 'Advanced cross-family toys (numpy)'         'test_toys_advanced.py'

# --- FreeCAD worker / geometry (spawns freecadcmd.exe; CalculiX bundled) --------
Test-Module 'Worker / FreeCAD-side tests'                'test_worker.py'
Test-Module 'Drawing gate: completeness'                 'test_drawing_gate.py'
Test-Module 'Drawing gate: worker'                       'test_drawing_gate_worker.py'
Test-Module 'Drawing gate: curved/periodic'              'test_drawing_gate_curved.py'
Test-Module 'Drawing gate: thumbnail section'            'test_drawing_thumbnail_section.py'
Test-Module 'Drawing gate: legibility regression'        'test_drawing_legibility_regression.py'
Test-Module 'Sheet metal: bends/unfold/DXF (pure core)'   'test_sheetmetal.py'
Test-Module 'Sheet metal: bends/unfold/DXF (worker)'      'test_sheetmetal_worker.py'
Test-Module 'Inspection: balloons/plan/FAI (pure core)'  'test_inspection.py'
Test-Module 'Inspection: balloons/plan/FAI (worker)'     'test_inspection_worker.py'
Test-Module 'CNC machinability + tolerance-cost (worker)' 'test_machining_worker.py'
Test-Module 'Release package: gates/manifest (pure core)' 'test_release_package.py'
Test-Module 'Release package: vendor/RFQ bundle (worker)' 'test_release_package_worker.py'
Test-Module 'Orderable parts: designations + catalog'    'test_orderable.py'
Test-Module 'Orderable parts: catalog (worker)'          'test_orderable_worker.py'
Test-Module 'Nonlinear structural FEM (oracles; ccx.exe bundled)' 'test_fem_nonlinear.py'
Test-Module 'Laminate / composite stack (oracles; layered FEM gated)' 'test_laminate.py'
Test-Module 'Golden fixtures (issue #19)'                'test_golden_issue19.py'
Test-Module 'Mating-dimension golden table'              'test_mating_dims.py'
Test-Module 'Render tests'                               'test_render.py'
Test-Module 'Photoreal render tests (skip if renderer absent)' 'test_render_photoreal.py'
Test-Module 'Cross-slice integration'                    'test_integration.py'
Test-Module 'Determinism (geometry bitwise + analysis sweep)' 'test_determinism.py'
Test-Module 'Edit stability'                             'test_edit_stability.py'
Test-Module 'Negative paths'                             'test_negative_paths.py'

# --- manifest / orchestration / lifecycle (pure-Python, no FreeCAD) -------------
Test-Module 'Multi-agent partition+merge (M1)'           'test_multiagent_m1.py'
Test-Module 'Manifest resolve step'                      'test_manifest_resolve.py'
Test-Module 'Relations: parameter DAG'                   'test_relations.py'
Test-Module 'Typed-interface merge gates'                'test_typed_interfaces.py'
Test-Module 'Liskov substitutability gate'               'test_substitutability.py'
Test-Module 'verify_contract self-check'                 'test_verify_contract.py'
Test-Module 'Hierarchical manifests'                     'test_hierarchical_manifests.py'
Test-Module 'Standard / library parts'                   'test_standard_parts.py'
Test-Module 'Requirements gates (mass / CG)'             'test_requirements_gates.py'
Test-Module 'Merge-time physics gate (FEM modal, live ccx.exe)' 'test_merge_modal_gate.py'
Test-Module 'Manifest schema + validation'               'test_manifest_schema.py'
Test-Module 'Item model + part numbering'                'test_items.py'
Test-Module 'Part recipes'                               'test_recipes.py'
Test-Module 'Interface-type registry + conformance'      'test_iface_registry.py'
Test-Module 'Project / workspace container'              'test_project_container.py'
Test-Module 'Feature templates (PowerCopy/UDF)'          'test_feature_templates.py'
Test-Module 'Design tables / variant families'           'test_families.py'
Test-Module 'Revision + lifecycle state machine'         'test_lifecycle.py'
Test-Module 'Modularity eval ladder'                     'test_modularity_eval.py'
Test-Module 'Change orders + where-used impact'          'test_change.py'
Test-Module 'Host-agnostic builder contract'             'test_component_contract_check.py'
Invoke-Step 'Host-agnostic builder demo' @('example\host_agnostic_builder_demo.py')

Invoke-Step 'Orchestration selftest'  @('-m', 'orchestration.selftest')
Invoke-Step 'Orchestration dryrun'    @('-m', 'orchestration.dryrun')
Test-Module 'Orchestration coordinator'                  'test_coordinator.py'
Test-Module 'Motion oracle (mobility/ratio gate)'        'test_mechanism.py'
Test-Module 'Geometry-realizes-declaration (needs FreeCAD)' 'test_realize.py'
Test-Module 'Experiment-readiness gate'                  'test_experiment_readiness.py'

# Layer D harness validator - free scripted stub (force dry even if RUN_RELIABILITY=1)
$savedRel = $env:RUN_RELIABILITY
$env:RUN_RELIABILITY = ''
Test-Module 'Reliability Layer D - harness validator (free stub)' 'test_reliability_tasks.py'
$env:RUN_RELIABILITY = $savedRel

# The OpenFOAM-backed suites run on Windows since #193 (launches route through the
# WSL distro; solves self-gate on RUN_HEAVY_SOLVES + in-distro availability, so the
# default lane stays fast). test_meshbridge's Elmer leg is native (#205); its snappy
# leg rides the same WSL route.
Test-Module 'OpenFOAM CFD (structure + WSL-gated solves)'  'test_openfoam.py'
Test-Module 'Mesh bridge (ElmerGrid native / snappy via WSL)' 'test_meshbridge.py'
Test-Module 'Virtual wind tunnel + CFD trust layer (WSL-gated)' 'test_wind_tunnel.py'
Test-Module 'FSI preCICE (degradation + WSL-gated solve)'  'test_fsi.py'

# --- Linux-only families: skipped on Windows (documented, not silent) -----------
$linuxOnly = @(
    @{ f = 'test_acoustics_bem.py'; why = 'bempp-cl (OpenCL ICD + meshio>=5 dedicated venv)' },
    @{ f = 'test_granular.py';      why = 'YADE (GPL, no native Windows build); WSL only' },
    @{ f = 'test_em_fullwave.py';   why = 'openEMS FDTD (source-built, not wired on Windows)' }
)
Write-Host ''
Write-Host '== Skipped on Windows (Linux-only solver toolchains; see docs/WINDOWS.md) ==' -ForegroundColor Yellow
foreach ($s in $linuxOnly) {
    Write-Host ("   SKIP  {0,-24} {1}" -f $s.f, $s.why) -ForegroundColor Yellow
}

# Optional heavy/perf/reliability lanes ------------------------------------------
if ($env:RUN_PERF -eq '1') { Test-Module 'Perf baselines' 'test_perf.py' }
else { Write-Host ''; Write-Host '(Skipping perf suite - set RUN_PERF=1 to enable.)' }

if ($env:RUN_RELIABILITY -eq '1') {
    Test-Module 'Reliability Layer A (classification)' 'test_reliability.py'
    Test-Module 'Reliability Layer B (diff detection)' 'test_reliability_diff.py'
    Test-Module 'Reliability Layer C (agent-loop closure)' 'test_reliability_agent_loop.py'
    Test-Module 'Reliability Layer D (LLM-drives-MCP)' 'test_reliability_tasks.py'
} else {
    Write-Host '(Skipping reliability suites - set RUN_RELIABILITY=1 to enable.)'
}

# --- summary --------------------------------------------------------------------
Write-Host ''
if ($script:failures.Count -gt 0) {
    Write-Host "== $($script:failures.Count)/$($script:ran) steps FAILED ==" -ForegroundColor Red
    foreach ($f in $script:failures) { Write-Host "   - $f" -ForegroundColor Red }
    exit 1
}
Write-Host "== ALL $($script:ran) steps passed ==" -ForegroundColor Green
