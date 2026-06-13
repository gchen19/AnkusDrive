#!/usr/bin/env bash
# Run the full DriftPin test suite. Two interpreters:
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
    "$RUFF" check driftpin tests
else
    echo "  WARNING: ruff not found — 'pip install ruff==0.15.15' to lint locally as CI does (skipping)"
fi

echo
echo "== Static contracts (registry parity + docstrings; no FreeCAD) =="
python3 tests/test_contracts.py

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
echo "== Wear / fatigue / fracture toys (pure-Python; no FreeCAD) =="
python3 tests/test_durability.py

echo
echo "== Lumped transient thermal toys (pure-Python; no FreeCAD) =="
python3 tests/test_thermal.py

echo
echo "== Convection-coefficient screening toys (pure-Python; no FreeCAD) =="
python3 tests/test_convection.py

echo
echo "== Tier A screening toys (acoustics · plates · buckling · molding · impact) =="
python3 tests/test_acoustics.py
python3 tests/test_plates.py
python3 tests/test_buckling.py
python3 tests/test_molding.py
python3 tests/test_impact.py

echo
echo "== Design-for-X toys (DfM/DfA/packaging; pure-Python; no FreeCAD) =="
python3 tests/test_dfx.py

echo
echo "== Design-for-Cost toys (pure-Python; no FreeCAD) =="
python3 tests/test_cost.py

echo
echo "== FDM slice-estimate toys (pure-Python; no FreeCAD) =="
python3 tests/test_slicing.py

echo
echo "== Async job-registry toys (pure-Python; no FreeCAD) =="
python3 tests/test_jobs.py

echo
echo "== Random-vibration toys (Miles; pure-Python; no FreeCAD) =="
python3 tests/test_vibration.py

echo
echo "== P2 solver degradation contract (pure-Python; no FreeCAD) =="
python3 tests/test_solve_degradation.py

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
echo "== Geometry bridge (case gen always; ElmerGrid/snappyHexMesh gates when present) =="
python3 tests/test_meshbridge.py

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
echo "== Determinism =="
$VENV_PY tests/test_determinism.py

echo
echo "== Edit stability =="
python3 tests/test_edit_stability.py

echo
echo "== Negative paths =="
$VENV_PY tests/test_negative_paths.py

echo
echo "== Multi-agent partition+merge (Layer M1) =="
$VENV_PY tests/test_multiagent_m1.py

echo
echo "== Manifest resolve step (RFC 11.1, pure python) =="
$VENV_PY tests/test_manifest_resolve.py

echo
echo "== Typed-interface merge gates (RFC 11.2, scripted, no key) =="
$VENV_PY tests/test_typed_interfaces.py

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
echo "== Manifest schema + validation (RFC 11.7, contract-drift detection, no key) =="
$VENV_PY tests/test_manifest_schema.py

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
