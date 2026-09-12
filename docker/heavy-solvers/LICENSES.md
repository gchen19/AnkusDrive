# Third-party components in `ankusdrive-heavy`

This image is CI infrastructure for AnkusDrive's heavy solver regressions (#339). It
bundles third-party solvers under their own licences. AnkusDrive invokes the
copyleft ones only as separate processes; bundling them in one image is aggregation,
not linking. Source for every component is available from its upstream at the
revision below. The exact build recipe is `docker/heavy-solvers/Dockerfile` in
<https://github.com/gchen19/AnkusDrive>.

| Component | Licence | Source | Revision / version |
|---|---|---|---|
| FreeCAD (AppImage, incl. bundled CalculiX + Gmsh) | LGPL-2.1+ (CalculiX GPL-2.0, Gmsh GPL-2.0+) | https://github.com/FreeCAD/FreeCAD | 1.1.3 (pinned in `scripts/install-freecad-appimage.sh`) |
| OpenFOAM (ESI, apt `openfoam2512-dev`) | GPL-3.0 | https://develop.openfoam.com/Development/openfoam | v2512 |
| OpenFOAM-7 (.org) + ThirdParty-7 | GPL-3.0 | https://github.com/OpenFOAM/OpenFOAM-7 | `7458f48c2fb109e8dede5d9aec5ecbb904dc1b18` |
| openInjMoldSim | GPL-3.0 | https://github.com/krebeljk/openInjMoldSim | `v7.2` |
| Elmer FEM (PPA `elmerfem-csc`) | GPL-2.0+ / LGPL-2.1 (ElmerSolver libs) | https://github.com/ElmerCSC/elmerfem | PPA build at image build time |
| CalculiX (apt `calculix-ccx`) | GPL-2.0 | http://www.dhondt.de/ | Ubuntu 24.04 package |
| CalculiX 2.20 source (in `ccx_preCICE`) | GPL-2.0 | http://www.dhondt.de/ccx_2.20.src.tar.bz2 | 2.20 |
| preCICE | LGPL-3.0 | https://github.com/precice/precice | `v3.4.0` |
| preCICE calculix-adapter | GPL-3.0 | https://github.com/precice/calculix-adapter | `ff3950b9a23925d223d4cb45606272cbf57d626f` |
| preCICE openfoam-adapter | GPL-3.0 | https://github.com/precice/openfoam-adapter | `f6d7928c52df4bf109a4d5abd245531d6eeb136c` |
| YADE | GPL-2.0+ | https://gitlab.com/yade-dev/trunk | `a48efeefb6318fc82d474f36aa2d97697f0adf8c` |
| openEMS / CSXCAD (openEMS-Project) | GPL-3.0 / LGPL-3.0 | https://github.com/thliebig/openEMS-Project | `112b5f492c1a3a08ff69338d7ef39a0cf3a5b7bc` (with submodules) |
| PrusaSlicer (apt `prusa-slicer`) | AGPL-3.0 | https://github.com/prusa3d/PrusaSlicer | Ubuntu 24.04 package |
| Bempp-cl, Gmsh, meshio (venv) | MIT / GPL-2.0+ / MIT | PyPI | resolved at image build time |
| KrakenOS (driver venv) | GPL-3.0 | PyPI | resolved at image build time |
| PyBullet, solidspy, rayoptics, optiland, CoolProp | zlib / MIT / BSD-3 / MIT / MIT | PyPI | resolved at image build time |

Ubuntu base packages carry their own licences, recorded under `/usr/share/doc/*/copyright`.

If a licence above is stated incorrectly, open an issue — the upstream's own
licence file is authoritative.
