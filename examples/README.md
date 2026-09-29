# AnkusDrive examples

Runnable scripts that exercise AnkusDrive end to end. None of them ship in the
package. Run them from the repo root with the repo `.venv`, e.g.
`.venv/bin/python3 examples/design/phase0_walkthrough.py`.

| Folder | What's in it |
|---|---|
| [`design/`](design/) | Design and multi-agent workflows: the Phase 0 multi-agent walkthrough, the host-agnostic builder demo (also run by `tests/run_all.sh`), the design-hierarchy demo and the hand-rolled gearbox manifest that `ankusdrive/families.py` replaces. See [`design/README.md`](design/README.md). |
| [`optics/`](optics/) | Optics gallery scripts (2D, 3D and the ball-lens case) and the PNGs they render into [`optics/optics_gallery/`](optics/optics_gallery/). |
| [`simulation/`](simulation/) | `run_simulation_examples.py` (the heavy-solver acceptance examples, each gated against its analytic oracle) and `plot_advanced_toys.py` (figures for the advanced toys), both writing into [`simulation/results/`](simulation/results/). |
