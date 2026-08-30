#!/usr/bin/env bash
# Run the full AnkusDrive test suite. Two interpreters:
#   - system python3 for FreeCAD-loading tests (worker, integration, etc.)
#   - .venv python3 for tests that need Pillow + numpy (render, integration)
# Both are wired so the same test files just work under either.
#
# Reliability suites (Layers A-D, real models) are GATED behind RUN_RELIABILITY=1
# because they cost API credits. See tests/RELIABILITY.md. The Layer D *harness
# validator* (grader negative control + scripted stub) runs unconditionally — it
# needs no API, so a broken harness fails CI before any paid run.
#
# Usage:  bash tests/run_all.sh
set -e
cd "$(dirname "$0")/.."

VENV_PY=".venv/bin/python3"

# Lint first — mirrors the CI "Fast checks" workflow (.github/workflows/fast-checks.yml)
# so a ruff E9/pyflakes error (unused name, bad import, syntax) is caught locally
# before it reaches CI. Same command + pyproject.toml config CI uses. Resolve ruff
# from the venv, else PATH; if it isn't installed, warn loudly and continue (so the
# suite still runs) rather than silently passing the lint gate.
echo "== Lint (ruff E9,F — mirrors CI fast-checks; config in pyproject.toml) =="
if [ -x ".venv/bin/ruff" ]; then RUFF=".venv/bin/ruff"
elif command -v ruff >/dev/null 2>&1; then RUFF="ruff"
else RUFF=""; fi
if [ -n "$RUFF" ]; then
    "$RUFF" check ankusdrive tests
else
    echo "  WARNING: ruff not found — 'pip install ruff==0.15.15' to lint locally as CI does (skipping)"
fi

echo
echo "== Static contracts (registry parity + docstrings + escalate_to integrity + determinism-class coverage; no FreeCAD) =="
python3 tests/test_contracts.py

echo
echo "== Naming: the pre-rename name stays gone (#295; no FreeCAD) =="
# Greps every tracked file and path for the old project name against a small,
# self-validating allowlist. Cheap insurance against a pre-rename branch merging
# a second name for one thing back in.
python3 tests/test_naming.py

echo
echo "== Rename compatibility shims: legacy env / config / DP_* props (#295) =="
# The shims that keep a <=0.4.x install working. Every one of them fails SILENTLY
# if it regresses (an unset override auto-discovers; an unread property reads as
# un-annotated), so they are asserted, not assumed. Delete with the shims in 0.6.
python3 tests/test_compat_rename.py

echo
echo "== Packaging: the BUILT wheel/sdist carries every runtime corpus (no FreeCAD) =="
# Builds a real distribution into a temp dir and re-derives the expected data-file
# list from the loaders themselves, so a package-data omission (#234, #249) fails
# here instead of at a pip-installed user's first thread()/pipe()/write_fsi_case().
# Picks its own backend: `python -m build` when importable, else setuptools' PEP 517
# hooks (what the system python3 has), else `pip wheel`; SKIPs if none is available.
python3 tests/test_package_data.py

echo
echo "== Cross-platform FreeCAD discovery + doctor + the Windows core installer (no FreeCAD) =="
# Fakes each OS's discovery branch, so the Windows/macOS paths are covered from Linux —
# including the static contract on scripts/install-core.ps1 and pyproject's Python
# range (#279), which must fail HERE rather than on a user's first run.
python3 tests/test_windows_support.py

echo
echo "== MCP server boots over stdio with FreeCAD unresolved (#279) =="
# Needs the venv (the `mcp` client SDK); the system python3 has no deps, and the test
# SKIPs cleanly there rather than failing.
if [ -x "$VENV_PY" ]; then "$VENV_PY" tests/test_mcp_boot.py; else python3 tests/test_mcp_boot.py; fi

echo
echo "== MCP tool suite over stdio, driving the real worker (#288) =="
# The other half of the MCP surface: test_mcp_boot proves the server SPEAKS, this
# proves the tools WORK — documents, primitives, restart-clears-state, a CalculiX
# cantilever through the MCP layer. Needs FreeCAD as well as the `mcp` SDK, which
# is why it is here and not in the hosted core-install lane. It resolves its own
# interpreter (#288) and SKIPs, saying so, when `mcp` is importable nowhere.
if [ -x "$VENV_PY" ]; then "$VENV_PY" tests/test_mcp.py; else python3 tests/test_mcp.py; fi

echo
echo "== Typed units / quantity layer (pure-Python; no FreeCAD) =="
python3 tests/test_units.py

echo
echo "== Materials DB toys (pure-Python; no FreeCAD) =="
python3 tests/test_materials.py

echo
echo "== Machine-element rating toys (pure-Python; no FreeCAD) =="
python3 tests/test_machine_elements.py

echo
echo "== Tolerance & GD&T toys (pure-Python; no FreeCAD) =="
python3 tests/test_tolerance.py

echo
echo "== Standard reference tables: threads/fasteners, bearings, ISO 286 fits, stock (pure-Python; no FreeCAD) =="
python3 tests/test_standards.py

echo
echo "== Wear / fatigue / fracture toys (pure-Python; no FreeCAD) =="
python3 tests/test_durability.py

echo
echo "== Lumped transient thermal toys (pure-Python; no FreeCAD) =="
python3 tests/test_thermal.py

echo
echo "== Convection-coefficient screening toys (pure-Python; no FreeCAD) =="
python3 tests/test_convection.py

echo
echo "== Fluid thermophysical corpus — CoolProp (oracle always; EOS leg gated) =="
python3 tests/test_fluids.py

echo
echo "== Tier A screening toys (acoustics · plates · buckling · molding · impact) =="
python3 tests/test_acoustics.py
python3 tests/test_plates.py
python3 tests/test_buckling.py
python3 tests/test_molding.py
python3 tests/test_molding_fill.py
python3 tests/test_impact.py

echo
echo "== Design-for-X toys (DfM/DfA/packaging; pure-Python; no FreeCAD) =="
python3 tests/test_dfx.py

echo
echo "== Design-for-Cost toys (pure-Python; no FreeCAD) =="
python3 tests/test_cost.py

echo
echo "== Tolerance-cost coupling (issue #235 — IT-grade cost curves, loosen-to-save) =="
python3 tests/test_tolerance_cost.py

echo
echo "== CNC machinability + machining time (issue #231 — pure core) =="
python3 tests/test_machining.py

echo
echo "== FDM slice-estimate toys (pure-Python; no FreeCAD) =="
python3 tests/test_slicing.py

echo
echo "== Async job-registry toys (pure-Python; no FreeCAD) =="
python3 tests/test_jobs.py

echo
echo "== SU2 plane-channel case (#237 item 3; live gate when SU2 resolves) =="
# The NATIVE CFD path: builds its own .su2 mesh + config and solves plane Poiseuille
# against the exact closed form. Needs no OpenFOAM and, on Apple Silicon, no Multipass
# VM — which is what makes docs/MACOS.md's "CFD degrades to SU2" true. SKIPs the live
# leg when the SU2 binary is absent; the construction tests always run.
python3 tests/test_su2_case.py

echo
echo "== Main-thread work queue (#260; pure-Python; no FreeCAD) =="
# The primitive under adaptive SHAPE search: a background job asking the request loop
# to run FreeCAD work. The load-bearing test is the failure mode the design accepts —
# nobody drains the queue -> a loud, correctly-diagnosed timeout, never a silent hang.
python3 tests/test_mainthread.py

echo
echo "== Random-vibration toys (Miles; pure-Python; no FreeCAD) =="
python3 tests/test_vibration.py

echo
echo "== P2 solver degradation contract (pure-Python; no FreeCAD) =="
python3 tests/test_solve_degradation.py

echo
echo "== WSL routing for the OpenFOAM families (#193; monkeypatched, runs anywhere) =="
python3 tests/test_wsl_routing.py

echo
echo "== Extracted-AppImage FreeCAD: discovery + installer script (#280; no network, no FreeCAD) =="
python3 tests/test_freecad_appimage.py

echo
echo "== Persistent config layer (env -> config.toml -> auto; pure-Python; no FreeCAD) =="
python3 tests/test_config.py

echo
echo "== doctor's MCP preflight (#278; injects the mcp-2.0.0 break, spawns the server) =="
# The mcp-2.0.0 failure path is INJECTED (fake module + version), never installed —
# this test must not touch the interpreter it runs in. The live half spawns
# `ankusdrive mcp` over stdio for one initialize+ping, and self-skips without an mcp SDK.
python3 tests/test_doctor_mcp.py

echo
echo "== Planar-kinematics toys (Grashof / stroke / DOF; pure-Python; no FreeCAD) =="
python3 tests/test_kinematics.py

echo
echo "== Internal-flow / Hagen-Poiseuille toys (pure-Python; no FreeCAD) =="
python3 tests/test_cfd.py

echo
echo "== Optics toys (Snell/Fresnel/TIR oracle always; rayoptics gate when present) =="
# Run under the venv so the rayoptics gate actually exercises the wheel (the optics
# extra installs into .venv); falls back to system python3 if the venv is absent.
if [ -x "$VENV_PY" ]; then "$VENV_PY" tests/test_optics.py; else python3 tests/test_optics.py; fi

echo
echo "== Elmer transient-thermal (case gen always; ElmerSolver gate when present) =="
python3 tests/test_elmer.py

echo
echo "== Acoustic FEM / HelmholtzSolve (case gen always; ElmerSolver gate when present) =="
python3 tests/test_acoustic_fem.py

echo
echo "== Harmonic FRF / StressSolve (case gen always; ElmerSolver gate when present) =="
python3 tests/test_harmonic_fem.py

echo
echo "== OpenFOAM pipe CFD (case gen always; blockMesh+simpleFoam gate when present) =="
python3 tests/test_openfoam.py

echo
echo "== SU2 native runner (bash-free chain always; live SU2_CFD gate when present) =="
python3 tests/test_su2_native.py

echo
echo "== Geometry bridge (case gen always; ElmerGrid/snappyHexMesh gates when present) =="
python3 tests/test_meshbridge.py

echo
echo "== Performance contracts (three-state verdict; live contract on a part) =="
python3 tests/test_performance.py

echo
echo "== Solution verification (Richardson/GCI on constructed sequences; no solver) =="
python3 tests/test_verification.py

echo
echo "== DOE studies (sampling; swept Hagen-Poiseuille D^-4 oracle; async fan-out) =="
python3 tests/test_study.py

echo
echo "== Optimize-to-spec (bounded Nelder-Mead; closed-form binding-constraint oracle) =="
python3 tests/test_optimize.py

echo
echo "== Virtual wind tunnel + CFD trust layer (screen always; solves when present) =="
python3 tests/test_wind_tunnel.py

echo
echo "== Conjugate heat transfer (composite-wall oracle always; Elmer gate when present) =="
python3 tests/test_cht.py

echo
echo "== Low-frequency EM (skin/DC oracles always; Elmer gates when present) =="
python3 tests/test_em.py

echo
echo "== MBD dynamics (PyBullet; skip if the mbd extra is absent) =="
$VENV_PY tests/test_mbd.py

echo
echo "== Topology-optimization toys (SIMP 2-D + 3-D; needs numpy) =="
$VENV_PY tests/test_topology.py

echo
echo "== Advanced cross-family toys (exact-identity limit probes; needs numpy) =="
$VENV_PY tests/test_toys_advanced.py

echo
echo "== Worker / FreeCAD-side tests =="
python3 tests/test_worker.py

echo
echo "== Drawing-is-manufacturable gates (issue #85/#108 — completeness, legibility, curved/periodic) =="
python3 tests/test_drawing_gate.py
python3 tests/test_drawing_gate_worker.py
python3 tests/test_drawing_gate_curved.py
python3 tests/test_drawing_thumbnail_section.py
python3 tests/test_drawing_legibility_regression.py

echo
echo "== Sheet metal (issue #230 — K-factor unfold, flat pattern, layered DXF, press-brake screen) =="
python3 tests/test_sheetmetal.py
python3 tests/test_sheetmetal_worker.py

echo
echo "== Inspection artifacts (issue #232 — balloons, inspection plan, AS9102-shaped FAI report) =="
python3 tests/test_inspection.py
python3 tests/test_inspection_worker.py

echo
echo "== Tight bounding box (issue #284 — the analytic BoundBox over-estimates a trimmed face) =="
python3 tests/test_bbox_tight.py

echo
echo "== CNC machinability screen + tolerance-cost handle path (issue #231/#235 — worker) =="
python3 tests/test_machining_worker.py
echo "== Release packages (issue #233 — vendor/RFQ bundle, lifecycle/ECO/title-block gates, byte-identical re-release) =="
python3 tests/test_release_package.py
python3 tests/test_release_package_worker.py
echo "== Orderable standard parts (issue #234 — designations + the off-the-shelf catalog) =="
python3 tests/test_orderable.py
python3 tests/test_orderable_worker.py

echo
echo "== Nonlinear structural FEM (plastic/elastica/Hertz oracles always; ccx gate when present) =="
python3 tests/test_fem_nonlinear.py

echo
echo "== Laminate / composite stack (CLT/transformed-section/Timoshenko oracles always; layered FEM gate when present) =="
python3 tests/test_laminate.py

echo
echo "== Exterior acoustics BEM (monopole/Mie oracles always; bempp solve gated when present) =="
python3 tests/test_acoustics_bem.py
echo "== Granular DEM (RCP/Beverloo/repose oracles always; live YADE solve gated when present) =="
python3 tests/test_granular.py
echo "== Full-wave EM (waveguide-cutoff/dipole oracles always; openEMS FDTD solve gated when present) =="
python3 tests/test_em_fullwave.py

echo
echo "== FSI via preCICE (plate-deflection/interface-balance oracles always; coupled OpenFOAM<->CalculiX solve gated when present) =="
python3 tests/test_fsi.py

echo
echo "== Golden fixtures (issue #19 real adapters) =="
python3 tests/test_golden_issue19.py

echo
echo "== Mating-dimension golden table =="
python3 tests/test_mating_dims.py

echo
echo "== Render tests =="
$VENV_PY tests/test_render.py

echo
echo "== Photoreal render tests (skip if Render addon / renderer absent) =="
$VENV_PY tests/test_render_photoreal.py

echo
echo "== Cross-slice integration =="
$VENV_PY tests/test_integration.py

echo
echo "== Determinism (geometry bitwise + table-driven analysis-tool sweep + bounded submits; see determinism_registry.py) =="
$VENV_PY tests/test_determinism.py

echo
echo "== Edit stability =="
python3 tests/test_edit_stability.py

echo
echo "== Fillet/chamfer post-apply validation (issue #283: OCC's silent corruption) =="
$VENV_PY tests/test_fillet_validation.py

echo
echo "== Negative paths =="
$VENV_PY tests/test_negative_paths.py

echo
echo "== Degenerate subtractions (issue #282 — cut that ate the base / cut that missed; warnings + strict) =="
python3 tests/test_boolean_degenerate.py

echo
echo "== Multi-agent partition+merge (Layer M1) =="
$VENV_PY tests/test_multiagent_m1.py

echo
echo "== Manifest resolve step (RFC 11.1, pure python) =="
$VENV_PY tests/test_manifest_resolve.py

echo
echo "== Relations: driving/driven parameter DAG (issue #137, arithmetic+lookup, no key) =="
$VENV_PY tests/test_relations.py

echo
echo "== Typed-interface merge gates (RFC 11.2, scripted, no key) =="
$VENV_PY tests/test_typed_interfaces.py

echo
echo "== Liskov substitutability gate (§7.1, #147 — F3 as code; same-interface swap stays green, off-interface fails naming the broken gate; no key) =="
$VENV_PY tests/test_substitutability.py

echo
echo "== verify_contract self-check (RFC 11.3, scripted, no key) =="
$VENV_PY tests/test_verify_contract.py

echo
echo "== Hierarchical manifests (RFC 11.4, nested merge/gate/lock, no key) =="
$VENV_PY tests/test_hierarchical_manifests.py

echo
echo "== Standard / library parts (RFC 11.5, generate/gate/lock, no key) =="
$VENV_PY tests/test_standard_parts.py

echo
echo "== Requirements gates (RFC 11.6, mass / CG over the merged product, no key) =="
$VENV_PY tests/test_requirements_gates.py

echo
echo "== Merge-time physics gate (RFC 11.6 physics tier, #172 — min_first_mode_hz via FEM modal, live ccx) =="
python3 tests/test_merge_modal_gate.py

echo
echo "== Manifest schema + validation (RFC 11.7, contract-drift detection, no key) =="
$VENV_PY tests/test_manifest_schema.py

echo
echo "== Item model + part numbering (C1, #140 — items.json identity, no key, no FreeCAD) =="
python3 tests/test_items.py

echo
echo "== Part recipes (issue #136 — declared-input build templates: contract + door + determinism, no key) =="
$VENV_PY tests/test_recipes.py

echo
echo "== Interface-type registry + conformance gate (issue #146 — implements + bus + unknown-is-loud, no key) =="
$VENV_PY tests/test_iface_registry.py
echo "== Project / workspace container (D1, #143 — scaffold + ref-integrity guard + master-skeleton, no key) =="
$VENV_PY tests/test_project_container.py
echo "== Feature templates (issue #139 B2 — PowerCopy/UDF: ref-by-name + door + determinism, no key) =="
$VENV_PY tests/test_feature_templates.py
echo "== Design tables / variant families (issue #138, B1 — a gear family + a bearing catalog from one table each, no key) =="
$VENV_PY tests/test_families.py
echo "== Revision + lifecycle state machine + F3 predicate (C2, #141 — state machine, immutability, rename-vs-revise; no key, no FreeCAD) =="
python3 tests/test_lifecycle.py
echo "== Modularity eval ladder (E1, #148 — Layer M1: family regen / substitutability / encapsulation / interface-break, each vs. its negative control; no key) =="
$VENV_PY tests/test_modularity_eval.py
echo "== Change orders + where-used impact + baselines (C3, #142 — ECO record + §9 blast radius + reproducible-rebuild baseline; no key, no FreeCAD) =="
python3 tests/test_change.py

echo
echo "== Host-agnostic builder contract (RFC 11.11, #169 — builder brief + component_contract_check, no key) =="
$VENV_PY tests/test_component_contract_check.py
$VENV_PY example/host_agnostic_builder_demo.py

echo
echo "== Orchestration coordinator (RFC 11.8 — round-0 review, pipelined fan-in, isolation) =="
$VENV_PY -m orchestration.selftest
$VENV_PY -m orchestration.dryrun
$VENV_PY tests/test_coordinator.py

echo
echo "== Motion oracle (RFC 11.9 — mobility/ratio gate; rigid lock vs selective, no key) =="
$VENV_PY tests/test_mechanism.py

echo
echo "== Geometry-realizes-declaration (RFC 11.10 — does the metal back the claim?, needs FreeCAD) =="
$VENV_PY tests/test_realize.py

echo
echo "== Experiment-readiness gate (free GO/NO-GO for a billed emergent-property run) =="
$VENV_PY tests/test_experiment_readiness.py

echo
echo "== Reliability Layer D — harness validator (free, scripted stub) =="
# The real Layer D benchmark needs an API key; this runs its grader negative
# control + scripted-stub plumbing check, which require no API, so the harness
# itself is guarded on every CI run. Force dry mode even when RUN_RELIABILITY=1.
RUN_RELIABILITY= $VENV_PY tests/test_reliability_tasks.py

echo
if [[ "$RUN_PERF" == "1" ]]; then
    echo "== Perf baselines =="
    $VENV_PY tests/test_perf.py
else
    echo "(Skipping perf suite — set RUN_PERF=1 to enable. See tests/TEST_PLAN.md tier 5.)"
fi

echo
if [[ "$RUN_RELIABILITY" == "1" ]]; then
    echo "== Reliability Layer A (classification) =="
    $VENV_PY tests/test_reliability.py
    echo
    echo "== Reliability Layer B (diff detection) =="
    $VENV_PY tests/test_reliability_diff.py
    echo
    echo "== Reliability Layer C (agent-loop closure) =="
    $VENV_PY tests/test_reliability_agent_loop.py
    echo
    echo "== Reliability Layer D (LLM-drives-MCP, real models) =="
    $VENV_PY tests/test_reliability_tasks.py
else
    echo "(Skipping reliability suites — set RUN_RELIABILITY=1 to enable. See tests/RELIABILITY.md)"
fi

echo
echo "== ALL DONE =="
