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

### Ball lens — a hard case worth understanding (`examples/optics_ball_lens.py`)

| File | Engine | What it shows |
|------|--------|---------------|
| `07_ball_lens_caustic_3d.png` | KrakenOS (**subprocess**) | A collimated bundle through a meshed BK7 sphere does **not** meet at a point — it folds into a caustic. Marginal rays cross the axis short of the paraxial focus (★). That smear *is* spherical aberration. |
| `08_ball_lens_mesh_convergence.png` | analysis | Focus spread vs sphere facet count. The **near-axis scatter (facet noise)** falls toward zero as the mesh refines; the **total spread** plateaus on the physical spherical-aberration floor. |

**Why a ball lens is hard:**
- **Spherical aberration is enormous.** One strong radius on both faces means marginal rays over-bend and focus well short of paraxial ones — there is no single focus, only a caustic. The usable aperture is a small fraction of the diameter; ball lenses suit low-NA jobs (fiber-to-fiber coupling), not imaging.
- **Back focal distance is tiny.** `BFD = R(2−n)/(2(n−1))` from the rear vertex (4.675 mm for BK7 R=10). As `n→2` the focus lands on the back surface — which is exactly why high-index (n≈1.9–2.0) ball lenses are the standard for fiber collimation. `EFL = nR/(2(n−1))` = 14.67 mm here (from the sphere center).
- **Meshing is a trap.** A triangulated sphere only approximates the smooth surface. A naive lat-long (UV) sphere degenerates into slivers **at the poles — i.e. on the optical axis**, corrupting the most important rays; an icosphere keeps facets uniform. Either way coarse facets scatter rays, so meshed spheres are for illustration and **analytic spherical surfaces for precision** (the analytic ball-lens trace nails BFD to 0.04 mm; the meshed one shows why you'd reach for it). It's the "validate the artifact, not the model" theme in reverse: a faithful CAD mesh can still be *optically* wrong if under-tessellated.

The prism and ball-lens figures are produced by calling the **production** GPL runner
(`driftpin/optics_gpl_runner.py`) as a subprocess — KrakenOS is never imported into this
process — exactly as `optics_solid_trace` does in the worker.

Regenerate: `.venv/bin/python examples/optics_gallery.py`
