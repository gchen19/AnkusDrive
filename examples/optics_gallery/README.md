# Optics gallery — visual test results

Rendered by `examples/optics_gallery.py` (headless, `.venv/bin/python`). Each PNG is a
visual check of one of the two optics engines wired into the MCP surface.

| File | Engine | What it shows | Oracle |
|------|--------|---------------|--------|
| `01_lens_layout.png` | optiland (MIT, in-process) | N-BK7 equiconvex singlet, collimated rays converging at f≈49 mm; marginal rays cross early — visible spherical aberration | thick-lens EFL 49.04 mm |
| `02_spot_diagram.png` | optiland | On-axis geometric spot, concentric rings of spherical aberration | RMS 45.7 µm |
| `03_optimize_before_after.png` | optiland optimizer | Singlet radii optimized to hit a 100 mm EFL target — focus moves 78→100 mm | converges in 12 evals to EFL 100.000 mm |
| `04_prism_tir.png` | KrakenOS (GPL-3.0, **subprocess**) | A +Z ray bundle through a 45° BK7 prism STL totally-internally-reflects at the hypotenuse and turns exactly 90° | mean turn 90.0°, 4/4 rays valid |

The prism figure is produced by calling the **production** GPL runner
(`driftpin/optics_gpl_runner.py`) as a subprocess — KrakenOS is never imported into this
process — exactly as `optics_solid_trace` does in the worker.

Regenerate: `.venv/bin/python examples/optics_gallery.py`
