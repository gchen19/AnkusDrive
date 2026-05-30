#!/usr/bin/env bash
# Run the full DriftPin test suite. Two interpreters:
#   - system python3 for FreeCAD-loading tests (worker, integration, etc.)
#   - .venv python3 for tests that need Pillow + numpy (render, integration)
# Both are wired so the same test files just work under either.
#
# Reliability suite (Layer A) is GATED behind RUN_RELIABILITY=1 because it
# costs API credits. See tests/RELIABILITY.md.
#
# Usage:  bash tests/run_all.sh
set -e
cd "$(dirname "$0")/.."

VENV_PY=".venv/bin/python3"

echo "== Worker / FreeCAD-side tests =="
python3 tests/test_worker.py

echo
echo "== Render tests =="
$VENV_PY tests/test_render.py

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
else
    echo "(Skipping reliability suites — set RUN_RELIABILITY=1 to enable. See tests/RELIABILITY.md)"
fi

echo
echo "== ALL DONE =="
