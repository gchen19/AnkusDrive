# FSI case template — attribution

These files are a parameterised copy of the **perpendicular-flap** tutorial from
the [preCICE tutorials](https://github.com/precice/tutorials) repository,
licensed **LGPL-3.0**. They provide the validated fluid (OpenFOAM `pimpleFoam`)
and solid (CalculiX `ccx_preCICE`) participant cases plus the `precice-config.xml`
that `driftpin/analysis/fsi_case.py` substitutes physics into and runs as the real
coupled FSI solve (issue #91).

The two heavy solvers run **out-of-process** (the arm's-length boundary DriftPin
already uses for OpenFOAM/Elmer); preCICE (LGPL-3.0) is never imported into
DriftPin's permissive code. Only these data files are vendored — no LGPL source is
linked. Upstream: https://github.com/precice/tutorials (LGPL-3.0).
