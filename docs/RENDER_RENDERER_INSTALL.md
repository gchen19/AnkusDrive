# Provisioning the external renderers

**Status: discovery + provisioning tooling implemented; source-build renderers still
pending.** The *code* side was done in PR #18 (all six renderers wired into
`driftpin/worker.py`'s `_RENDERERS` registry, scene-export-verified headless). This
pass added the two pieces §7 called for:

- **`render_capabilities`** (MCP tool + `@handler`) — the agent now *discovers* which
  renderers resolve right now and whether the Render addon imports, instead of
  learning by trial-and-error. Done and tested (`test_render_capabilities`).
- **`scripts/install-renderers.sh`** — idempotent, checksum-verified installer for the
  prebuilt renderers; writes PATH wrappers (§3) that fix discovery *and* the bundled
  `LD_LIBRARY_PATH`. Verified end-to-end offline: install → wrapper → the resolver
  reports the renderer available via `render_capabilities`.

What changed vs. the original plan (binaries were re-checked against the real release
assets in June 2026 — see §4):

- Of the "three prebuilt" renderers, only **two** actually ship a usable headless CLI:
  **Appleseed** (`appleseed.cli`) and **LuxCore** — but LuxCore needs the **`-sdk`**
  tarball (the plain standalone ships only `luxcoreui` + `pyluxcore`; the console
  binary lives in the SDK), and v2.6 is the last release with a standalone build at all
  (newer releases are pip wheels). The script installs both.
- **OSPRay Studio has no prebuilt `ospStudio` binary** anywhere (no releases on
  `ospray_studio`; the OSPRay SDK tarball ships the library + examples, not Studio). It
  is therefore reclassified into the **build-from-source** group alongside pbrt-v4 and
  Cycles (§7).

So today, on a box with no extra binaries, `render_photoreal(renderer="Luxcore"|
"Appleseed"|"Cycles"|"Ospray"|"Pbrt")` still returns *"could not locate …"* and the
gated tests SKIP; POV-Ray (via `apt`) is the only one verified end-to-end. Run the
install script on a provisioned box to flip Luxcore/Appleseed SKIP → PASS. See also
[`RENDER_WORKBENCH.md`](RENDER_WORKBENCH.md) (architecture) and
[`RENDER_TEXTURE_CHECK.md`](RENDER_TEXTURE_CHECK.md).

> **Sandbox note.** Installing + wiring (download, checksum, unpack, wrapper, *discovery*)
> is verified here; **executing** a hand-fetched renderer is left to a trusted/provisioned
> environment (§8). The install script never runs the binaries.

The three currently-working renderers, same part + view + material, rendered through
`render_photoreal` on a provisioned box (POV-Ray via apt; LuxCore + Appleseed via
`scripts/install-renderers.sh`):

![render_photoreal across renderers — the same box ∪ cylinder in Gold, rendered by POV-Ray, LuxCore and Appleseed](render_renderers_gallery.png)

Regenerate with `.venv/bin/python3 scripts/render-gallery.py` — it renders whatever
`render_capabilities` reports available, so the montage grows as Cycles/OSPRay/pbrt land.

### The full material library, per renderer

The original `docs/render_gallery.png` rendered every Render material card under POV-Ray
only. `scripts/render-material-gallery.py` does that grid for *each* available renderer
(one image per renderer), so the library can be compared across systems. A card that
fails on a renderer is shown as a marked cell, so the grid honestly reflects support.

| Renderer | Gallery | Materials rendered |
|---|---|---|
| POV-Ray   | `render_gallery_povray.png`    | 14 / 14 |
| LuxCore   | `render_gallery_luxcore.png`   | 14 / 14 |
| Appleseed | `render_gallery_appleseed.png` | 13 / 14 (the image-mapped **Terrazzo** card produces no output on the 2019 Appleseed build) |

![POV-Ray material library](render_gallery_povray.png)
![LuxCore material library](render_gallery_luxcore.png)
![Appleseed material library](render_gallery_appleseed.png)

Regenerate all: `.venv/bin/python3 scripts/render-material-gallery.py`
(or `--renderer Luxcore` for one).

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
| `Appleseed` | `AppleseedCliPath` | `appleseed.cli` | **prebuilt** — appleseedhq/appleseed `2.1.0-beta` (2019, final build; `linux64-gcc74.zip`). Automated by `install-renderers.sh`. | easy |
| `Luxcore` | `LuxCoreConsolePath` | `luxcoreconsole` | **prebuilt SDK** — LuxCoreRender/LuxCore `v2.6` `linux64-**sdk**.tar.bz2` (the plain standalone lacks `luxcoreconsole`; v2.6 is the last standalone — newer = wheels). Automated by `install-renderers.sh`. | easy |
| `Ospray` | `OspPath` | `ospStudio` | **build from source** — no prebuilt `ospStudio` exists (no `ospray_studio` releases; the OSPRay SDK ships the lib, not Studio). CMake against the OSPRay SDK. | hard |
| `Pbrt` | `PbrtPath` | `pbrt` | **build from source** (CMake) — mmp/pbrt-v4; experimental upstream | moderate |
| `Cycles` | `CyclesPath` | `cycles` | **build from source** — Blender's engine; no standalone CLI ships | hard |

(POV-Ray, for reference: `PovRayPath` → `povray`, `apt install povray`.)

Pinned prebuilt assets (real SHA-256s, hardcoded in `scripts/install-renderers.sh`):

| Renderer | Asset | SHA-256 |
|---|---|---|
| Appleseed | `appleseed-2.1.0-beta-0-g015adb503-linux64-gcc74.zip` | `e96fc907fa95b38c7be542b796fd783870da67612eb233bd4965c2ebec7335d2` |
| LuxCore | `luxcorerender-v2.6-linux64-sdk.tar.bz2` | `c4a387ee65765b235d47c81c83e293ff584bd4da4b5e24fc89d651c8c1393393` |

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

- **Discovery check (no binary needed, never skips):** `render_capabilities` (MCP tool)
  reports `addon_importable` plus, per renderer, `available` + the resolved `path` (or an
  `install_hint`). This is the fast "what's usable right now" probe — run it before and
  after the install script to watch renderers flip to `available: true`. It resolves the
  binary exactly as `render_photoreal` does but renders nothing and mutates no prefs.
- **Pre-check without the binary (wiring only):** the addon's DryRun mode renders the
  scene files but skips the binary — used in PR #18 to confirm each renderer emits a
  valid scene. Good for sanity before a real install.
- **Real check (the source of truth):** `.venv/bin/python3 tests/test_render_photoreal.py`.
  Each renderer's gated test flips **SKIP → PASS** once its binary resolves
  (`test_photoreal_{luxcore,appleseed,cycles,ospray,pbrt}_renders`).
- **Agent check:** `render_photoreal(handle, renderer="X")` returns a PNG instead of
  "could not locate". A montage like `docs/render_gallery.png` can be regenerated to
  compare renderers side by side.

## 7. Work items

1. ✅ **`scripts/install-renderers.sh`** — done. Fetches the prebuilt renderers into
   `$PREFIX/<renderer>/` (default `/opt`), verifies pinned SHA-256s, and writes the §3
   wrappers to `$BINDIR` (default `/usr/local/bin`). Idempotent; `--list` shows status;
   macOS/Windows print guidance (follow-ons). **Scope correction:** only Appleseed +
   LuxCore (`-sdk`) have usable prebuilt CLIs (§4); OSPRay Studio has no prebuilt and
   moved to item 3.
2. ✅ **`render_capabilities` MCP tool** — done. Worker `@handler` + `@mcp.tool()` (parity
   per `tests/test_contracts.py`), tested by `test_render_capabilities`. Reports per-renderer
   availability + addon import status + material cards, side-effect-free.
3. ⬜ **OSPRay Studio + pbrt-v4 + Cycles from source** — CMake builds; document the build
   flags. The long-pole items; a separate phase. (OSPRay Studio joined this group once it
   turned out to have no prebuilt `ospStudio`, §4.)
4. ⬜ **Dockerfile (optional)** — an image with all renderers + libs baked in. Most
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

## 9. Files this work touched

- ✅ `scripts/install-renderers.sh` (new) — writes wrappers to `$BINDIR` (default
  `/usr/local/bin`) and installs under `$PREFIX` (default `/opt`).
- ✅ `scripts/render-gallery.py` (new) + `docs/render_renderers_gallery.png` — renders
  the example montage above across every available renderer.
- ✅ `scripts/render-material-gallery.py` (new) + `docs/render_gallery_{povray,luxcore,appleseed}.png`
  — the material library rendered as a grid per renderer.
- ✅ `driftpin/worker.py` — added `@handler("render_capabilities")`; refactored
  `_resolve_renderer_exec` to share a side-effect-free `_find_renderer_exec` the probe
  reuses. The `_RENDERERS` registry was already complete (unchanged).
- ✅ `driftpin/mcp_server.py` — added the `render_capabilities` tool.
- ✅ `tests/test_render_photoreal.py` — added `test_render_capabilities` (a pure probe,
  so it never skips). The gated renderer tests still SKIP until binaries land on a box.
- ⬜ `docs/` — `RENDER_WORKBENCH.md` notes the new tool + script; deeper §6/§7 updates and
  a Docker/CI note wait until a provisioned box verifies a real Luxcore/Appleseed render.

### Code hardening NOT needed (wrappers sufficed)
The §3 wrappers handle `LD_LIBRARY_PATH` per renderer, so the optional resolver/`lib_dirs`
or explicit-`env=` changes were not required. Revisit only if a wrapper-less path is wanted.
