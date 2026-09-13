# Changelog

All notable changes to AnkusDrive are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), with one addition: each
release opens with a short paragraph saying what it was *for*. At this project's
rate of change a bare list of bullets is not something anyone finishes reading,
and the paragraph is what makes a release legible.

Every entry ends with the pull request or issue number that carried it.

## Versioning

AnkusDrive is **pre-1.0, and a minor release may break compatibility.** That is not
an accident of the numbering — 0.5.0 renamed the project and 0.6.0 will delete the
shims that made the rename survivable. Read a minor bump as "check the entry below
before upgrading", and pin an exact version if you need the guarantee.

Only the latest release is patched; there are no maintenance branches. See
[`SECURITY.md`](SECURITY.md).

GitHub release notes are generated from the sections below rather than written
separately, so this file is the source and the release page is the copy.

## [0.5.3] — 2026-09-12

The release that makes AnkusDrive a one-click install. Every GitHub release now
carries an `.mcpb` bundle for Claude Desktop, the same file the Smithery listing
distributes, and it was verified end to end in Claude Desktop — including on a machine
with no `uv`, where the host downloads its own. Installing that way exposed a quieter
defect: the install hints assumed a plain venv, and every managed install (the
extension, `uvx` from the MCP Registry, pipx, `uv tool`) was being told to run a `pip`
that could not reach it. Both land here, with a reproducible FEM mesh behind them.

### Added

- **Blender backend for `render_photoreal`**: whole assemblies (an assembly handle or a
  `parts` list) with a different appearance per part — the 14 Render card names or a
  neutral PBR dict (`color`, `metallic`, `roughness`, `emission`, `transmission`,
  `finish`: `fdm_layers` / `brushed`) — rendered in a `studio` scene (cyclorama, soft
  area lights, contact shadows) at `quality` `draft`/`preview`/`final` with Cycles, OIDN
  denoising and GPU when present. Full Blender 4.2+ runs headless on a checked-in scene
  script, meshing each B-rep face with exact surface normals, and the result carries
  `blend_path` for refinement in Blender (optionally via Blender's own MCP server,
  which AnkusDrive does not depend on). **`render_photoreal`'s default `renderer` is now
  `"auto"`** (was `"Povray"`): Blender when it resolves, else POV-Ray, else any add-on
  renderer, with `auto_selected` in the result; pass `renderer="Povray"` for the old
  behaviour. Blender is discovered like a solver (`studio_render` family,
  `ANKUSDRIVE_BLENDER_PATH`), so `render_capabilities`, `setup_status` / `ankusdrive
  doctor` and a fallback render's `suggestion` all hand out the install command.
  Installers: `scripts/install-renderers.sh blender` (Linux tarball / macOS cask or DMG)
  and `scripts/install-solvers.ps1 blender` (Windows portable zip, no admin; `blender-msi`
  for the per-machine `Program Files` install, elevated), pinned to 5.2.1 LTS — x64 **and
  arm64** — with SHA-256 checks and official mirrors first: `download.blender.org` returns
  403 to scripted clients, which also breaks `winget install BlenderFoundation.Blender`
  ([#335](https://github.com/gchen19/AnkusDrive/issues/335)).
- An MCPB bundle, `ankusdrive-<version>.mcpb`, attached to every GitHub release — the
  one-click Claude Desktop install and the artifact the Smithery listing distributes.
  It carries no AnkusDrive code: `mcpb/pyproject.toml` pins the matching PyPI release
  and the host resolves it with `uv` (manifest 0.4, `server.type: "uv"`), so the bundle
  is ~34 kB and handles numpy/Pillow on every platform. One optional install field,
  the FreeCAD command path. A launcher shim drops `ANKUSDRIVE_*` values that arrive as
  unexpanded `${user_config.*}` text, which the MCPB reference host leaves in place for
  an unset option and AnkusDrive would otherwise read as an explicit, nonexistent
  FreeCAD path. `publish.yml` builds it after the PyPI upload and creates the GitHub
  release from the changelog section when it does not exist yet
  ([#201](https://github.com/gchen19/AnkusDrive/issues/201)).

### Fixed

- Install hints now match how AnkusDrive was installed. `setup_status`, `doctor`,
  `solve_capabilities` and every `*_submit` degradation used to say
  `pip install 'ankusdrive[X]'`, which under pipx, `uv tool`, `uvx` (the MCP Registry
  launch) and the Claude Desktop extension installs into a different Python and
  leaves the family missing with no explanation. `ankusdrive/install_kind.py`
  detects the install (`ANKUSDRIVE_INSTALL_KIND`, else the interpreter path) and
  rewrites pip hints: `pipx runpip ankusdrive install …`, `uv tool install
  --reinstall 'ankusdrive[X]'`, `uvx --from 'ankusdrive[X]' ankusdrive mcp`, and for
  the extension a plain statement that extras cannot be added, with the pipx route.
  Non-pip hints and plain venvs are unchanged. The report gains `install: {kind,
  source}`; the FreeCAD worker receives the host's kind, since its own interpreter is
  FreeCAD's; the MCPB launcher announces `mcpb`. A fast-lane test fails any hint
  string that bypasses the routing
  ([#347](https://github.com/gchen19/AnkusDrive/issues/347)).
- `solve_capabilities`, `setup_status` and `doctor` no longer report openEMS, Bempp
  and KrakenOS absent on an install where they run. All three run under a dedicated
  interpreter (`ANKUSDRIVE_OPENEMS_PYTHON` / `_BEMPP_PYTHON` / `_OPTICS_GPL_PYTHON`),
  but discovery asked whether the module imports in its *own* process, so agents were
  told the `em_fullwave`, `acoustics_bem` and `optics_nonseq` families could not run.
  Discovery and the worker now share one resolver, `solvers.solver_python()`: the
  override, then the venv that owns the module, then the dedicated venv beside the
  repo, then the current interpreter, each probed with find_spec in that interpreter.
  An available solver reports that interpreter as `path`
  ([#351](https://github.com/gchen19/AnkusDrive/issues/351)).

- `fem_mesh` and `fem_cantilever_demo` mesh with serial Gmsh, like the Elmer and
  warpage bridges already did. Gmsh's threaded 3-D Delaunay is nondeterministic, so
  the same model meshed to a different tet count run to run (390–406 tets, max
  displacement 0.0497–0.0512 mm on one 24-core box) and a FEM test flaked; the result
  is now identical across runs, for ~3% more meshing time on a 440k-tet part
  ([#344](https://github.com/gchen19/AnkusDrive/pull/344)).

### Changed

- The FreeCAD-path config key is `freecadcmd` everywhere: `smithery.yaml` said
  `freecadCmd` while `config.toml` and the bundle say `freecadcmd`
  ([#201](https://github.com/gchen19/AnkusDrive/issues/201)).

## [0.5.2] — 2026-09-12

The release that puts AnkusDrive on the official MCP Registry. The registry proves
PyPI ownership by reading an `mcp-name:` token out of the README *as uploaded*, and
0.5.1's upload predates that token — so, as with 0.5.1's frozen project page, only a
new version can carry it. This is the first release whose tag push lists itself.

### Added

- AnkusDrive is listed on the official [MCP Registry](https://registry.modelcontextprotocol.io/)
  as `io.github.gchen19/ankusdrive`. `server.json` describes the PyPI package
  (`uvx ankusdrive mcp`, optional `ANKUSDRIVE_FREECADCMD`); the README carries the
  `mcp-name:` ownership token the registry reads from the PyPI upload; and
  `publish.yml` publishes to the registry after every PyPI release, via GitHub OIDC
  with a pinned, checksum-verified `mcp-publisher`. A fast-lane contract test
  fails any release PR whose `server.json` version drifts from `__version__`
  ([#201](https://github.com/gchen19/AnkusDrive/issues/201)).

### Changed

- `fem_run` and `fem_cantilever_demo` default their workdir to
  `<TMPDIR>/ankusdrive_fem` instead of a literal `/tmp` path, so the default follows
  the platform's temp directory. Callers that pass `workdir` see no change. The FEM
  tests now remove their CalculiX scratch dirs at exit instead of stranding
  multi-MB result files on the runners; `ANKUSDRIVE_KEEP_SCRATCH=1` keeps them for
  debugging ([#336](https://github.com/gchen19/AnkusDrive/pull/336)).

## [0.5.1] — 2026-09-10

The first release cut after the repository became public. Its reason for existing
is unglamorous: a PyPI project page's description is frozen at upload time, so
0.5.0's page keeps a broken wordmark and ~30 dead links to a repository that was
private when it was published. Only a new version picks up a working README.
Everything else here is hardening that accumulated behind it.

### Added

- `ankusdrive doctor` certifies the MCP server itself — a real stdio handshake —
  rather than stopping at FreeCAD resolution ([#289](https://github.com/gchen19/AnkusDrive/pull/289)).
- `scripts/install-core.ps1`: the whole Windows first run as one script — venv,
  the pins that matter, `doctor`, an MCP handshake, and a registration block with
  absolute paths filled in ([#290](https://github.com/gchen19/AnkusDrive/pull/290)).
- A documented install path for Ubuntu 24.04+ and containers, where `apt` has no
  FreeCAD candidate and snap/flatpak both fail: the official AppImage, extracted
  ([#291](https://github.com/gchen19/AnkusDrive/pull/291)).
- `SECURITY.md` — a private reporting route, a scope statement for a project whose
  headline feature is executing Python, and what is handled upstream
  ([#305](https://github.com/gchen19/AnkusDrive/pull/305)).
- Per-platform solver install guides — macOS, Linux, and Windows — transcribed
  from the reference machines and scrubbed to placeholders
  ([#320](https://github.com/gchen19/AnkusDrive/pull/320)).
- `CONTRIBUTING.md` (the seven registration points a new tool touches, the CI
  lanes, and a Releasing section), a Contributor Covenant 2.1
  `CODE_OF_CONDUCT.md`, and this changelog
  ([#318](https://github.com/gchen19/AnkusDrive/pull/318)).
- An issue chooser whose contact link routes a suspected vulnerability to
  private reporting instead of a public issue — plus bug / question /
  tool-request forms and a PR template
  ([#322](https://github.com/gchen19/AnkusDrive/pull/322)).

### Changed

- `bounding_box` reports the analytic box as the upper bound it is, and can measure
  the tight one instead of implying it already did
  ([#294](https://github.com/gchen19/AnkusDrive/pull/294)).
- Three `docs/` plans whose work had shipped moved to `docs/archive/`; three more
  that survived the sweep were rewritten, because surviving an archive pass is not
  the same as having been read ([#305](https://github.com/gchen19/AnkusDrive/pull/305)).
- Every PR-triggered CI lane runs on GitHub-hosted runners. A green Windows run
  now certifies the documented install path end to end — the FreeCAD portable
  archive plus `scripts/install-solvers.ps1 su2 elmer`, landing exactly where
  solver discovery probes ([#326](https://github.com/gchen19/AnkusDrive/pull/326)).
- `tests/run_all.sh` falls back to `python3` when there is no `.venv`, so a
  machine where one interpreter satisfies every import runs the whole suite
  under it ([#326](https://github.com/gchen19/AnkusDrive/pull/326)).

### Fixed

- `boolean_op` names the two degenerate cuts — no intersection, and a cut that
  removes everything — instead of returning a volume that looks like a result
  ([#292](https://github.com/gchen19/AnkusDrive/pull/292)).
- `fillet_edges` validates the blend before handing back a handle, and names the
  edge that broke it ([#293](https://github.com/gchen19/AnkusDrive/pull/293)).
- The macOS heavy-solver lane reports the stale-rename failure by name, and stops
  reading a stderr warning as a verdict
  ([#304](https://github.com/gchen19/AnkusDrive/pull/304)).
- The MCP test suite forwards `ANKUSDRIVE_*` to the server it spawns. The stdio
  client's scrubbed default environment silently dropped every override, so the
  suite passed only where FreeCAD was findable by PATH or glob luck
  ([#326](https://github.com/gchen19/AnkusDrive/pull/326)).
- Two tests asserted environment variables the CI box merely happened not to
  export ([#319](https://github.com/gchen19/AnkusDrive/pull/319)).

### Security

- Every commit on every ref is scanned for credentials in CI, by a gitleaks binary
  pinned to a version and verified against its published checksum rather than
  trusted by tag ([#300](https://github.com/gchen19/AnkusDrive/pull/300)).
- All 18 GitHub Action references are pinned to commit SHAs, each resolving the tag
  already in use rather than silently taking a major bump
  ([#301](https://github.com/gchen19/AnkusDrive/pull/301)).
- Fork pull requests cannot reach a persistent machine, structurally. First the
  self-hosted lanes learned to skip them ([#302](https://github.com/gchen19/AnkusDrive/pull/302));
  then every PR-triggered lane moved to hosted runners outright, so fork PRs run
  the FULL suite and the exposure class is gone rather than trigger-guarded
  ([#326](https://github.com/gchen19/AnkusDrive/pull/326)). Self-hosted machines
  serve only scheduled solver regressions.
- The repository is public as of 2026-09-10, with the protections that only
  exist on public repos turned on the same hour: branch protection with five
  required hosted checks and linear history, a `v*` tag ruleset, secret scanning
  with push protection, SHA-pinning enforcement for every action, private
  vulnerability reporting, and Dependabot
  ([#303](https://github.com/gchen19/AnkusDrive/issues/303)).
- A fast-lane guard sweeps every tracked file and path for machine-identifying
  content — home directories, hostnames, tailnet names, ANY contributor's, in
  any encoding — without itself naming what it forbids
  ([#323](https://github.com/gchen19/AnkusDrive/pull/323)); self-hosted job logs
  mask the runner's identity before the first step prints
  ([#325](https://github.com/gchen19/AnkusDrive/pull/325)).

## [0.5.0] — 2026-08-24

**The project was renamed from DriftPin to AnkusDrive, and published to PyPI.**
Under that headline sits the largest release so far: the simulation surface grew
past structural FEM into molding, full-wave EM, granular mechanics, exterior
acoustics and coupled FSI; a design-control layer arrived so an agent can manage
*designs* rather than files; Windows and Apple Silicon became first-class rather
than aspirational; and the CFD work grew a trust layer, so a number now arrives
with the evidence that it converged.

Upgrading from 0.4.x is a rename, and every surface keeps accepting the old
spelling for exactly this one release. Read
[`MIGRATION.md`](MIGRATION.md) first — especially the part about `DP_*` document
properties, whose failure mode if the shims are ignored is silent.

### Added

**Simulation — new physics.**

- Nonlinear structural analysis via CalculiX: plasticity, large deflection, contact
  ([#94](https://github.com/gchen19/AnkusDrive/pull/94)).
- Full-wave FDTD electromagnetics via openEMS — waveguide cutoff, dipole resonance
  ([#95](https://github.com/gchen19/AnkusDrive/pull/95)).
- Granular and powder mechanics via YADE (DEM): random close packing, Beverloo
  discharge, angle of repose ([#96](https://github.com/gchen19/AnkusDrive/pull/96)).
- Exterior acoustics via Bempp (BEM): monopole radiation, Mie scattering
  ([#97](https://github.com/gchen19/AnkusDrive/pull/97)).
- Partitioned fluid–structure interaction via preCICE, coupling OpenFOAM to
  CalculiX ([#98](https://github.com/gchen19/AnkusDrive/pull/98)).
- Injection molding end to end: analytic moldability screen, an interFoam fill
  solve, the openInjMoldSim OF7 fill case, the packing/cooling stage, and warpage
  as a CalculiX thermo-elastic post-step
  ([#109](https://github.com/gchen19/AnkusDrive/pull/109),
  [#112](https://github.com/gchen19/AnkusDrive/pull/112),
  [#114](https://github.com/gchen19/AnkusDrive/pull/114),
  [#115](https://github.com/gchen19/AnkusDrive/pull/115),
  [#133](https://github.com/gchen19/AnkusDrive/pull/133)).
- Optics: lens design and optimization via optiland, non-sequential STL tracing via
  KrakenOS ([#89](https://github.com/gchen19/AnkusDrive/pull/89)).
- Laminates: classical lamination theory effective properties, bimetal warp,
  first-ply failure ([#129](https://github.com/gchen19/AnkusDrive/pull/129)).

**Simulation — trust and search.**

- The virtual wind tunnel: arbitrary solids in external flow, not just the library
  of canonical shapes ([#253](https://github.com/gchen19/AnkusDrive/pull/253)).
- A CFD trust layer — convergence history, `checkMesh`, measured y+, Richardson
  extrapolation and GCI — so a drag number arrives with its own evidence
  ([#255](https://github.com/gchen19/AnkusDrive/pull/255)).
- A verified oracle for turbulent external flow: "gated" became per (model, case
  family) rather than a single blanket claim
  ([#266](https://github.com/gchen19/AnkusDrive/pull/266)).
- Performance contracts: `declare_performance` / `verify_performance`, which then
  became gates consulted by merge, substitutability and the builder brief
  ([#256](https://github.com/gchen19/AnkusDrive/pull/256),
  [#267](https://github.com/gchen19/AnkusDrive/pull/267)).
- `study_submit` (DOE / parameter sweeps) and `optimize_submit` (vary parameters
  until the spec is met, then prove it), the latter running over a recipe on a
  main-thread work queue ([#258](https://github.com/gchen19/AnkusDrive/pull/258),
  [#259](https://github.com/gchen19/AnkusDrive/pull/259),
  [#273](https://github.com/gchen19/AnkusDrive/pull/273)).
- `fem_result_probe` — stress and displacement at a point or a face
  ([#126](https://github.com/gchen19/AnkusDrive/pull/126)).

**Designs, not just parts — the design-control layer.**

- Item model and part numbering ([#150](https://github.com/gchen19/AnkusDrive/pull/150)),
  part recipes ([#151](https://github.com/gchen19/AnkusDrive/pull/151)), a
  driving/driven parameter DAG ([#153](https://github.com/gchen19/AnkusDrive/pull/153)),
  revision and lifecycle state ([#154](https://github.com/gchen19/AnkusDrive/pull/154)),
  a Liskov substitutability gate ([#155](https://github.com/gchen19/AnkusDrive/pull/155)),
  variant families from a design table ([#156](https://github.com/gchen19/AnkusDrive/pull/156)),
  declared-input feature recipes ([#157](https://github.com/gchen19/AnkusDrive/pull/157)),
  a project container with a master skeleton and reference-integrity guard
  ([#158](https://github.com/gchen19/AnkusDrive/pull/158)), versioned interface types
  with a conformance gate ([#159](https://github.com/gchen19/AnkusDrive/pull/159)), and
  ECO records with where-used impact and baselines
  ([#160](https://github.com/gchen19/AnkusDrive/pull/160)).

**Manufacturing and release.**

- Sheet metal: bends and flanges, K-factor unfold, flat pattern, layered DXF
  ([#243](https://github.com/gchen19/AnkusDrive/pull/243)).
- An honest CNC machining time, and a reason to loosen a tolerance rather than a
  bare cost number ([#242](https://github.com/gchen19/AnkusDrive/pull/242)).
- Inspection: ballooned drawings, an inspection plan, an AS9102-shaped FAI report
  ([#240](https://github.com/gchen19/AnkusDrive/pull/240)).
- `release_package` — a one-call vendor/RFQ bundle, gated by lifecycle state, ECO
  and the title block ([#241](https://github.com/gchen19/AnkusDrive/pull/241)).
- Orderable standard parts: canonical designations and an off-the-shelf catalog
  ([#244](https://github.com/gchen19/AnkusDrive/pull/244)).
- Headless 2D drawings: multi-view PDF/SVG/DXF with dimensions, a manufacturability
  and legibility gate, lane packing, a title block, an iso pictorial and an
  automatic cross-section ([#80](https://github.com/gchen19/AnkusDrive/pull/80),
  [#86](https://github.com/gchen19/AnkusDrive/pull/86),
  [#87](https://github.com/gchen19/AnkusDrive/pull/87)).

**Reference corpora.**

- Mechanical properties with FreeCAD's own material cards first-class, plus
  provenance and basis ([#119](https://github.com/gchen19/AnkusDrive/pull/119)).
- ISO threads and fasteners, bearing C/C0 ratings, ISO 286 fits, stock tables
  ([#118](https://github.com/gchen19/AnkusDrive/pull/118)).
- Thermophysical fluid properties via CoolProp
  ([#117](https://github.com/gchen19/AnkusDrive/pull/117)).
- A typed units and quantity layer at the tool boundary
  ([#128](https://github.com/gchen19/AnkusDrive/pull/128)).

**Platforms.**

- **Windows is a first-class target**: FreeCAD and `ccx` discovery, `doctor`, native
  solvers, a CI lane, a native bash-free SU2 runner, verified Elmer thermal
  families, and the OpenFOAM-backed families routed through WSL
  ([#206](https://github.com/gchen19/AnkusDrive/pull/206),
  [#207](https://github.com/gchen19/AnkusDrive/pull/207),
  [#209](https://github.com/gchen19/AnkusDrive/pull/209),
  [#214](https://github.com/gchen19/AnkusDrive/pull/214)).
- **Apple Silicon**: a Darwin solver path and discovery glob, a per-OS substrate
  launcher with Multipass discovery, the coupled FSI solve routed through the VM,
  the molding and CFD families reaching the same in-VM trust, and openInjMoldSim
  arm64-enabled and live-verified
  ([#213](https://github.com/gchen19/AnkusDrive/pull/213),
  [#217](https://github.com/gchen19/AnkusDrive/pull/217),
  [#218](https://github.com/gchen19/AnkusDrive/pull/218),
  [#221](https://github.com/gchen19/AnkusDrive/pull/221),
  [#275](https://github.com/gchen19/AnkusDrive/pull/275),
  [#286](https://github.com/gchen19/AnkusDrive/pull/286)).
- A persistent config file as a resolution layer — env → `config.toml` → auto —
  because MCP hosts launch with a minimal environment and a shell `export` never
  reaches them ([#210](https://github.com/gchen19/AnkusDrive/pull/210)).
- Agent-guided setup: a `setup_status` tool, a `diagnose_setup` prompt, an
  `ankusdrive://setup` resource, and an interactive provisioner with
  `--print-mcp-config` ([#211](https://github.com/gchen19/AnkusDrive/pull/211),
  [#212](https://github.com/gchen19/AnkusDrive/pull/212)).

**Multi-agent design.**

- A motion oracle — does the assembly actually move? (RFC §11.9,
  [#76](https://github.com/gchen19/AnkusDrive/pull/76)) — and
  geometry-realizes-declaration, which asks whether the metal backs the claim
  (RFC §11.10, [#78](https://github.com/gchen19/AnkusDrive/pull/78)).
- Agents built an oracle-certified functional gearbox, with a train-ratio gate
  ([#77](https://github.com/gchen19/AnkusDrive/pull/77)).

### Changed

- **The project is named AnkusDrive.** Package, console script, environment
  variables, config directory, MCP tool prefix, document properties and repository
  all changed, with a CI guard that keeps it renamed
  ([#295](https://github.com/gchen19/AnkusDrive/issues/295),
  [#297](https://github.com/gchen19/AnkusDrive/pull/297)).
- The MCP server is safe for a team of agents: the shared `Worker` is serialized and
  each agent gets an isolated workspace
  ([#184](https://github.com/gchen19/AnkusDrive/pull/184)).
- Solver discovery distinguishes "unwired" from "absent", so a solver you have
  installed stops being reported as missing
  ([#181](https://github.com/gchen19/AnkusDrive/pull/181)).
- All text file I/O opens with an explicit UTF-8 encoding, guarded by a contract
  test ([#215](https://github.com/gchen19/AnkusDrive/pull/215)).
- Errors that told you what to do now tell you something you can actually run —
  `cost_estimate`, `slice_estimate`, and `press_fit_stress`, which additionally
  refuses an assumed modulus rather than passing an unrun check
  ([#263](https://github.com/gchen19/AnkusDrive/pull/263),
  [#268](https://github.com/gchen19/AnkusDrive/pull/268),
  [#270](https://github.com/gchen19/AnkusDrive/pull/270)).

### Deprecated

Everything below still works in 0.5.x and **is removed in 0.6.0**. See
[`MIGRATION.md`](MIGRATION.md).

- The `driftpin` console script. Installed, prints a rename notice, delegates.
- `DRIFTPIN_*` environment variables, promoted to their `ANKUSDRIVE_*` names at
  startup unless the new name is already set.
- `~/.config/driftpin/config.toml` and `%APPDATA%\driftpin\config.toml`, still read
  when no `ankusdrive` config exists. Writes always go to the new path.
- Solvers provisioned under a `DriftPin/solvers` directory, still discovered on
  Windows and macOS.
- `DP_*` document properties. Reads accept either spelling and a write updates the
  old property in place. **This is the shim worth understanding**: had the old names
  simply been dropped, an annotated part would open fine and read back as though it
  had never been annotated — no interfaces, no intent, no verdict, and no error.
  Removing the fallback in 0.6 will come with an explicit document migration.

### Fixed

- `mcp` is pinned below 2.0, which installs cleanly and then breaks `ankusdrive mcp`
  by dropping `mcp.server.fastmcp`
  ([#285](https://github.com/gchen19/AnkusDrive/pull/285)).
- `add_primitive` honours `name=` by setting the object's Label
  ([#250](https://github.com/gchen19/AnkusDrive/pull/250)).
- A modal solve that never finished is reported as its own outcome rather than as a
  floppy part ([#252](https://github.com/gchen19/AnkusDrive/pull/252)).
- The built wheel and sdist are tested for the runtime corpora they must carry, so a
  package-data omission fails in CI instead of at a pip-installed user's first
  `thread()` ([#251](https://github.com/gchen19/AnkusDrive/pull/251)).

## [0.4.0] — 2026-06-13

The release where AnkusDrive stopped being a CAD wrapper with FEM attached and
became a simulation surface. Two large bodies of work landed in parallel: the
physical-simulation tool families (thermal, CFD, EM, acoustics, multibody
dynamics, topology optimization, optics), each gated against an analytic oracle
rather than trusted because a solver returned; and the multi-agent RFC, which
turned "partition a product across agents and merge it back" from an experiment
into a mechanism with typed interfaces and merge gates.

### Added

- 21 MCAD commands across three tiers — the gears family, feature operations,
  inspection ([#7](https://github.com/gchen19/AnkusDrive/pull/7)).
- The physical-simulation tool families, P2 and P3: random vibration, contact,
  topology optimization, transient thermal, CFD, radiation thermal, external flow,
  conjugate heat transfer, low-frequency EM, optics, multibody dynamics, plus a
  geometry-driven meshing bridge from a FreeCAD solid to Gmsh/ElmerGrid and
  snappyHexMesh ([#26](https://github.com/gchen19/AnkusDrive/pull/26)–[#59](https://github.com/gchen19/AnkusDrive/pull/59)).
- Elmer and OpenFOAM case builders, with heavy solves gated against analytic
  oracles so the cheap half still runs on every push
  ([#34](https://github.com/gchen19/AnkusDrive/pull/34)).
- A screening tier — `acoustic_screen`, `plate_check`, `beam_buckling`,
  `molding_screen`, `drop_impact` — and a fidelity contract with an `h_estimate`
  convection screen ([#49](https://github.com/gchen19/AnkusDrive/pull/49),
  [#56](https://github.com/gchen19/AnkusDrive/pull/56)).
- Photoreal rendering through the FreeCAD Render workbench, with capability
  discovery, an installer, and externally built pbrt-v4, Cycles and OSPRay Studio
  ([#18](https://github.com/gchen19/AnkusDrive/pull/18),
  [#23](https://github.com/gchen19/AnkusDrive/pull/23),
  [#24](https://github.com/gchen19/AnkusDrive/pull/24)).
- G-code slicing via PrusaSlicer (`slice_gcode_submit`,
  [#45](https://github.com/gchen19/AnkusDrive/pull/45)).
- Multi-agent RFC §11.1–§11.8: global constraints resolved into literal slices,
  typed interfaces as merge gates, builder-side `verify_contract`, hierarchical
  manifests, standard parts generated at merge, requirements gates over the merged
  product, a formalized manifest schema with contract-drift hashing, and a pipelined
  coordinator ([#62](https://github.com/gchen19/AnkusDrive/pull/62)–[#70](https://github.com/gchen19/AnkusDrive/pull/70)).
- Functional invariants for enclosed-flow parts — is the airtight path actually
  airtight? ([#20](https://github.com/gchen19/AnkusDrive/pull/20)).

### Changed

- CI grew a FreeCAD-free fast lane: ruff, byte-compilation, and static contract
  tests over the two parallel command registries, giving PRs signal in seconds even
  when the FreeCAD box is busy ([#8](https://github.com/gchen19/AnkusDrive/pull/8)).
- A producer smoke test asserts every solid-producing command yields a watertight
  solid, and a golden table pins the mating dimensions of the parametric generators
  ([#9](https://github.com/gchen19/AnkusDrive/pull/9),
  [#11](https://github.com/gchen19/AnkusDrive/pull/11)).
- A nightly hosted-FreeCAD lane on conda-forge, so CI is not a single point of
  failure on one self-hosted machine
  ([#12](https://github.com/gchen19/AnkusDrive/pull/12)).

### Fixed

- Centre of mass is computed robustly for compound shapes, which the `gear_mesh`
  interface and the requirements gates both depend on
  ([#72](https://github.com/gchen19/AnkusDrive/pull/72)).

## [0.3.0] — 2026-05-10

The first tagged release, and the point at which the tool surface started
*encoding design intent* rather than just exposing FreeCAD operations — a tool that
knows what a hole is for can refuse to make a wrong one, which a generic
`pad`/`pocket` pair cannot.

### Added

- Design intent encoded across the tool surface: visibility hygiene, explicit
  direction, `verify_feature`, `through=wall`, `register_handle`, `intended_for`
  with `list_thread_options`, and a revolve pre-check.
- A single-source `__version__` with a `--version` CLI flag.
- The initial CI workflow, running the full suite on pushes to `main` and on PRs.

### Fixed

- A stale worker self-heals its active document, and `restart_worker` exists for
  when it cannot.

### Earlier

Before the first tag, in April 2026: the initial CLI and MCP scaffold over FreeCAD
1.1.1, and Phase 2 — the full core mechanical-design surface, roughly 72 MCP tools.
Those commits are in the git history rather than in this file.

[Unreleased]: https://github.com/gchen19/AnkusDrive/compare/v0.5.3...HEAD
[0.5.3]: https://github.com/gchen19/AnkusDrive/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/gchen19/AnkusDrive/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/gchen19/AnkusDrive/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/gchen19/AnkusDrive/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/gchen19/AnkusDrive/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/gchen19/AnkusDrive/releases/tag/v0.3.0
