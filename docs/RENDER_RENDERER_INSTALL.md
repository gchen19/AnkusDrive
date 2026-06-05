# Provisioning the external renderers (planning doc — for a future session)

**Status: plan only. Nothing here is implemented yet.** This is a handoff so another
session can install the five non-default renderers and make them usable by the agent,
without re-deriving the wiring. The *code* side is already done and merged (PR #18):
all six renderers are wired into `driftpin/worker.py`'s `_RENDERERS` registry and
scene-export-verified headless. What's missing is the **binaries** — so today
`render_photoreal(renderer="Luxcore"|"Appleseed"|"Cycles"|"Ospray"|"Pbrt")` returns
*"could not locate the … renderer binary"* and the gated tests SKIP. POV-Ray is the
only one installed (via `apt`) and verified end-to-end.

Goal: install the other five and make them resolve for the agent, then watch the
gated tests flip SKIP → PASS. See also [`RENDER_WORKBENCH.md`](RENDER_WORKBENCH.md)
(architecture) and [`RENDER_TEXTURE_CHECK.md`](RENDER_TEXTURE_CHECK.md).

---

## 1. How "available to the agent" resolves (read first)

The agent never references binaries directly. For `render_photoreal(renderer="X")`:

```
agent → MCP tool → worker handler → _resolve_renderer_exec("X")
       → proj.Proxy.render() → addon → Popen(<binary> …)   # inherits the worker's env
```

`_resolve_renderer_exec` (in `driftpin/worker.py`) finds the binary in this order and
then writes it into the FreeCAD param the plugin reads:

1. `DRIFTPIN_<RENDERER>_PATH` env var (e.g. `DRIFTPIN_LUXCORE_PATH`)
2. an exec path already set in FreeCAD prefs
3. **`PATH`** via `shutil.which`
4. per-OS standard dirs in the `_RENDERERS[...]["dirs"]` table

**Environment inheritance is the crux.** Neither `driftpin/client.py` (which spawns
`freecadcmd`) nor the Render addon's `RendererWorker` (which `Popen`s the renderer)
passes an explicit `env=`, so the renderer subprocess inherits the **worker's** env,
which inherits the **MCP server's** env. Therefore:

> Making a renderer "available to the agent" = making it discoverable by one of the
> four mechanisms above **in the environment that launches the MCP server**
> (`python -m driftpin mcp`). Setting it only in an interactive shell does nothing.

## 2. The two hurdles

1. **The binary.** POV-Ray was trivial (`apt` → on `PATH`, libs in system paths).
   The other five ship as **standalone builds** (or must be compiled).
2. **Bundled shared libraries.** The standalone tarballs carry their own `lib/` dir.
   `_resolve_renderer_exec` finds the binary but does **not** set `LD_LIBRARY_PATH`,
   so a *found* binary can still fail to launch. This must be handled at install time.

## 3. Recommended approach — wrapper scripts on `PATH`

One small wrapper per renderer solves discovery (mechanism #3) *and* the lib path,
with **zero code or MCP-config changes**. The wrapper name must match the
`binaries` tuple in `_RENDERERS` (table below):

```sh
# /usr/local/bin/luxcoreconsole   (chmod +x)
#!/bin/sh
export LD_LIBRARY_PATH="/opt/luxcore/lib:$LD_LIBRARY_PATH"
exec /opt/luxcore/luxcoreconsole "$@"
```

`shutil.which("luxcoreconsole")` finds the wrapper, libs load, the render runs.
Alternative (no wrapper): set `DRIFTPIN_<R>_PATH` to the real binary **and** export
`LD_LIBRARY_PATH` in the MCP server's launch env — but the wrapper is self-contained
and preferred.

## 4. Per-renderer facts (already wired in code) + where to get the binary

All five use **batch/headless mode** (already handled — `_RENDERERS[r]["batch"]=True`
makes the worker set `proj.BatchMode=True`). Param keys / binary names below are what
the addon plugins read and what the registry expects.

| Renderer (`renderer=`) | FreeCAD param | Binary name (wrapper) | Get it | Effort |
|---|---|---|---|---|
| `Ospray` | `OspPath` | `ospStudio` | prebuilt — RenderKit/ospray_studio releases | easy |
| `Luxcore` | `LuxCoreConsolePath` | `luxcoreconsole` | prebuilt tarball — LuxCoreRender/LuxCore releases | easy |
| `Appleseed` | `AppleseedCliPath` | `appleseed.cli` | prebuilt — appleseedhq/appleseed releases (older Linux build) | moderate |
| `Pbrt` | `PbrtPath` | `pbrt` | **build from source** (CMake) — mmp/pbrt-v4; experimental upstream | moderate |
| `Cycles` | `CyclesPath` | `cycles` | **build from source** — Blender's engine; no standalone CLI ships | hard |

(POV-Ray, for reference: `PovRayPath` → `povray`, `apt install povray`.)

Each plugin's exact exec/batch handling is in
`<App.getUserAppDataDir()>/Mod/Render/Render/renderers/<Renderer>.py` if details are
needed; the `install_hint` strings in `_RENDERERS` already capture the gist.

## 5. Making it available to the agent specifically

Pick one and apply it to the **MCP server launch env**:

- **PATH wrappers (recommended):** drop the §3 wrappers in a dir on the MCP server's
  `PATH`. Nothing else to configure.
- **MCP client config `env` block:** if the agent launches the server via an MCP
  config (Claude Desktop/Code `mcpServers` entry), set `DRIFTPIN_<R>_PATH` and
  `LD_LIBRARY_PATH` there so the spawned server (and its worker) inherit them.
- **Service/shell env:** if the server runs from a shell or unit file, export the
  vars there.

## 6. Verification

- **Pre-check without the binary (wiring only):** the addon's DryRun mode renders the
  scene files but skips the binary — used in PR #18 to confirm each renderer emits a
  valid scene. Good for sanity before a real install.
- **Real check (the source of truth):** `\.venv/bin/python3 tests/test_render_photoreal.py`.
  Each renderer's gated test flips **SKIP → PASS** once its binary resolves
  (`test_photoreal_{luxcore,appleseed,cycles,ospray,pbrt}_renders`).
- **Agent check:** `render_photoreal(handle, renderer="X")` returns a PNG instead of
  "could not locate". A montage like `docs/render_gallery.png` can be regenerated to
  compare renderers side by side.

## 7. Proposed work items (suggested order)

1. **`scripts/install-renderers.sh`** — fetch the three prebuilt renderers (OSPRay
   Studio, LuxCore, Appleseed) into `/opt/<renderer>/`, then write the §3 wrappers to
   `/usr/local/bin`. Idempotent; checksum the downloads. Cross-platform stubs for
   macOS (Homebrew/dmg) and Windows (installer/zip) as follow-ons.
2. **`render_capabilities` MCP tool** — list which renderers actually resolve right
   now (plus whether the addon imports), so the agent *discovers* availability instead
   of learning by trial-and-error. New worker handler + MCP tool; mirror the existing
   registry-parity/contract conventions (`tests/test_contracts.py`). High value, small.
3. **pbrt-v4 + Cycles from source** — CMake builds; document the build flags. These are
   the long-pole items; treat as a separate phase.
4. **Dockerfile (optional)** — an image with all renderers + libs baked in. Most
   reproducible "make them available" path; sidesteps `LD_LIBRARY_PATH` entirely and is
   the natural home for a CI lane that exercises real renders.

### Optional code hardening (only if wrappers prove insufficient)
- Teach `_resolve_renderer_exec` to also set `LD_LIBRARY_PATH` from a registry
  `lib_dirs` field (removes the need for wrappers), **or** generate the wrappers.
- Have the worker pass an explicit `env=` to the render subprocess rather than relying
  on inheritance (more predictable; larger change — touches the addon boundary).

## 8. Risks & open questions

- **CI placement.** Real renders need the binaries; the hosted nightly lane has no
  renderer and the render tests already skip there (and need Pillow + the two-interp
  `.venv`). Put a real-render lane on the self-hosted box (where POV-Ray already runs),
  or behind a renderer-provisioned Docker job. Don't gate the main suite on it.
- **Binary size / storage.** Each standalone is ~100–250 MB. Don't commit them; fetch
  in the install script / Docker build. Pin versions + checksums.
- **Licensing.** Confirm redistribution terms before baking binaries into an image.
- **Determinism.** Already settled — photoreal output is presentation-only and stays
  out of the reliability/golden tests. The new lane asserts invariants (non-blank,
  per-renderer), not pixels.
- **Upstream maturity.** pbrt-v4 support is flagged experimental in the addon, and the
  addon itself is unmaintained (see `RENDER_WORKBENCH.md` §2). Cycles has no standalone
  CLI shipped — the build is non-trivial. Budget accordingly.
- **Sandbox caveat.** In an agent sandbox the classifier may block *executing* a
  hand-fetched binary (untrusted external code). Installing/wiring is fine; the final
  render needs a trusted/provisioned environment.

## 9. Files this work will touch

- `scripts/install-renderers.sh` (new), wrappers in `/usr/local/bin` (host).
- `driftpin/worker.py` — only if adding `render_capabilities` or `LD_LIBRARY_PATH`
  support; the `_RENDERERS` registry is already complete.
- `driftpin/mcp_server.py` — `render_capabilities` tool (if pursued).
- `tests/test_render_photoreal.py` — the gated renderer tests already exist; they'll
  start passing as binaries land. Add a `render_capabilities` test if that tool ships.
- `docs/` — update `RENDER_WORKBENCH.md` §6/§7 once renderers are provisioned; add a
  Docker/CI note.
