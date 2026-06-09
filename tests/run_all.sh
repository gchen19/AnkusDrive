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
