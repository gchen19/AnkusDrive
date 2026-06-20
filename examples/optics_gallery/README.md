# Optics gallery — visual test results

Rendered by `examples/optics_gallery.py` (headless, `.venv/bin/python`). Each PNG is a
visual check of one of the two optics engines wired into the MCP surface.

| File | Engine | What it shows | Oracle |
|------|--------|---------------|--------|
| `01_lens_layout.png` | optiland (MIT, in-process) | N-BK7 equiconvex singlet, collimated rays converging at f≈49 mm; marginal rays cross early — visible spherical aberration | thick-lens EFL 49.04 mm |
| `02_spot_diagram.png` | optiland | On-axis geometric spot, concentric rings of spherical aberration | RMS 45.7 µm |
| `03_optimize_before_after.png` | optiland optimizer | Singlet radii optimized to hit a 100 mm EFL target — focus moves 78→100 mm | converges in 12 evals to EFL 100.000 mm |
| `04_prism_tir.png` | KrakenOS (GPL-3.0, **subprocess**) | A +Z ray bundle through a 45° BK7 prism STL totally-internally-reflects at the hypotenuse and turns exactly 90° | mean turn 90.0°, 4/4 rays valid |

### 3D (`examples/optics_gallery_3d.py`)

| File | Engine | What it shows | Oracle |
|------|--------|---------------|--------|
| `05_lens_3d.png` | optiland | The singlet's full 3D ray cone (hexapolar pupil) converging to focus, with the lens element drawn translucent — built from optiland's per-surface global ray coordinates | f≈49 mm |
| `06_prism_tir_3d.png` | KrakenOS (**subprocess**) | The 45° prism as a solid, with a genuinely 3D bundle (rays spread in X and Y) folding 90° by TIR — ray polylines are `S.XYZ` from the production runner | mean turn 90.0°, 12/12 rays |

The prism figures are produced by calling the **production** GPL runner
(`driftpin/optics_gpl_runner.py`) as a subprocess — KrakenOS is never imported into this
process — exactly as `optics_solid_trace` does in the worker.

Regenerate: `.venv/bin/python examples/optics_gallery.py`
