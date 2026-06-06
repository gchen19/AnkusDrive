# Rendering in DriftPin — support, installation & limitations

DriftPin has **two** render paths. This guide covers which renderers are supported,
how to install them, how to confirm they work, and the known limitations.

- **Fast preview** — `render_view` / `render_views`: a host-side software rasterizer
  ([`driftpin/render.py`](../driftpin/render.py), NumPy + Pillow). Deterministic, no
  external dependencies, flat shading. The right tool for "can the agent *see* what it
  built." Nothing to install beyond `Pillow`/`numpy` (regular pip deps).
- **Photoreal** — `render_photoreal` (+ async `render_photoreal_submit` / `render_job`):
  materials, lighting, global illumination, perspective, via the third-party
  [FreeCAD Render workbench](https://github.com/FreeCAD/FreeCAD-render) shelling out to
  an external renderer. Requires the addon **and** a renderer binary (this guide).

For design/architecture see [`RENDER_WORKBENCH.md`](RENDER_WORKBENCH.md); for the
provisioning detail and pinned download checksums see
[`RENDER_RENDERER_INSTALL.md`](RENDER_RENDERER_INSTALL.md).

---

## 1. Renderer support matrix

`render_photoreal(handle, renderer="…")` — `Povray` is the default. All six are wired
into the worker's `_RENDERERS` registry; the table is about whether a usable **binary**
is obtainable.

| `renderer=` | Status | How to get the binary | Notes |
|---|---|---|---|
| **`Povray`**     | ✅ works (default) | `apt install povray` · `brew install povray` · Windows installer | Single CLI; fast; lower photoreal ceiling. |
| **`Luxcore`**    | ✅ works | `scripts/install-renderers.sh` (LuxCore **v2.6 SDK**) | Highest quality + PBR; GI sampler noise on quick renders. Needs the `-sdk` build — the plain standalone lacks `luxcoreconsole`. |
| **`Appleseed`**  | ✅ works (one material caveat — §6) | `scripts/install-renderers.sh` (Appleseed **2.1.0-beta**, 2019 final build) | Console renderer `appleseed.cli`. |
| **`Cycles`**     | ✅ works | `scripts/build-renderers.sh cycles` (CPU standalone, built against system libs) | Blender's engine; no standalone CLI ships, so we compile it. |
| **`Ospray`**     | ⚠️ builds + renders, but dim | `scripts/build-renderers.sh ospray` (OSPRay Studio vs the OSPRay 3.2.0 SDK) | Renders correct geometry but low-contrast on the stock template — see §6. |
| **`Pbrt`**       | ✅ works | `scripts/build-renderers.sh pbrt` ([mmp/pbrt-v4](https://github.com/mmp/pbrt-v4)) | pbrt-v4 support is experimental upstream. |

To see the live status on a given box, call the **`render_capabilities`** tool (§4) —
it never renders, just reports what resolves right now.

---

## 2. Prerequisite: the Render addon

Install the workbench into the Mod dir FreeCAD reads (derive it from
`App.getUserAppDataDir()`; don't hardcode — it varies by OS and sandbox):

- Linux: `~/.local/share/FreeCAD/Mod/Render` (honors `$XDG_DATA_HOME`)
- macOS: `~/Library/Application Support/FreeCAD/Mod/Render`
- Windows: `%APPDATA%\FreeCAD\Mod\Render`

```bash
git clone https://github.com/FreeCAD/FreeCAD-render "<Mod dir>/Render"
git -C "<Mod dir>/Render" checkout 08be2fe94b8a998323c8a5443f7f0afd0d05bed5   # pinned
```

The addon is unmaintained (maintainer discontinued 2025-11-12), so we **pin a
known-good commit** rather than tracking `master`. Bump deliberately and re-run the
photoreal tests. DriftPin imports the addon lazily, so the worker boots fine without it.

---

## 3. Installing renderer binaries

### POV-Ray (default)
```bash
sudo apt install povray        # Linux ·  brew install povray (macOS) ·  installer (Windows)
```
Nothing else — DriftPin finds it on `PATH` and sets the FreeCAD param itself.

### LuxCore + Appleseed (prebuilt, automated)
```bash
sudo scripts/install-renderers.sh            # both ·  or: … luxcore  /  … appleseed
scripts/install-renderers.sh --list          # show what's pinned + current status
```
The script downloads the pinned release, **verifies a hardcoded SHA-256**, unpacks under
`$PREFIX` (default `/opt`), and writes a small wrapper to `$BINDIR` (default
`/usr/local/bin`) named exactly as the registry expects (`luxcoreconsole`,
`appleseed.cli`). The wrapper exports `LD_LIBRARY_PATH` so the build's bundled libraries
load. Idempotent; re-running skips what's already installed (`FORCE=1` to redo).
Rootless: `PREFIX=~/r BINDIR=~/bin scripts/install-renderers.sh` (put `BINDIR` on `PATH`).

> macOS/Windows are not automated yet — the script prints guidance. Install the binary
> and put it on `PATH` under the registry name, or set `DRIFTPIN_<RENDERER>_PATH`.

### Cycles / OSPRay Studio / pbrt-v4 (build from source, automated)
No usable prebuilt CLI is published, so these are compiled:
```bash
sudo scripts/build-renderers.sh              # all three ·  or: … pbrt / … cycles / … ospray
scripts/build-renderers.sh --list            # show what's pinned + current status
sudo scripts/build-renderers.sh --deps-only  # just apt-install the build dependencies
```
Like the prebuilt installer, it builds under a scratch dir, installs under `$PREFIX`
(default `/opt`), and writes a `$BINDIR` wrapper named as the registry expects (`pbrt`,
`cycles`, `ospStudio`); the OSPRay wrapper also exports `LD_LIBRARY_PATH` for the SDK's
bundled libs. Idempotent (`FORCE=1` to rebuild). Pinned revisions + the OSPRay SDK
checksum and the exact CMake flags are in
[`RENDER_RENDERER_INSTALL.md`](RENDER_RENDERER_INSTALL.md) §7. Linux x86_64; needs git,
cmake, ninja, a C++17 compiler, and apt for Cycles' system-library dependencies.

### How DriftPin finds a binary (and how to override)
`render_photoreal` resolves a renderer in this order: **`DRIFTPIN_<RENDERER>_PATH` env →
FreeCAD prefs → `PATH` (`shutil.which`) → per-OS install dirs**, then writes the path
into the FreeCAD param the plugin reads. The renderer subprocess inherits the worker's
env, which inherits the **MCP server's** env — so do discovery in the environment that
launches `python -m driftpin mcp` (a wrapper in a system `PATH` dir covers this; a value
set only in an interactive shell does not).

---

## 4. Verifying

```python
render_capabilities()           # MCP tool — never renders; pure discovery probe
```
Returns `addon_importable`, and per renderer `available` + the resolved `path` (or an
`install_hint`), plus the material card list. Run it before/after installing to watch a
renderer flip to `available: true`.

```bash
.venv/bin/python3 tests/test_render_photoreal.py     # gated tests SKIP→PASS as binaries land
```
A renderer with no binary makes its test **skip** (not fail), so CI stays green on boxes
that provision nothing.

---

## 5. Materials & galleries

`render_photoreal(..., material="Gold")` applies a Render material library card. Omit it
for a neutral default; an unknown name errors with the full list. 14 cards ship
(Aluminium, Brass, Carpaint, Disney, Emission, Glass, GlossyPlastic, Gold, GreenMarble,
Iron, Magnetite, Matte, RoughPlastic, Terrazzo).

The library rendered through each working renderer (one grid per renderer):

| Renderer | Gallery | Cards |
|---|---|---|
| POV-Ray   | [`render_gallery_povray.png`](render_gallery_povray.png)       | 14/14 |
| LuxCore   | [`render_gallery_luxcore.png`](render_gallery_luxcore.png)     | 14/14 |
| Appleseed | [`render_gallery_appleseed.png`](render_gallery_appleseed.png) | 13/14 (§6) |
| Cycles    | [`render_gallery_cycles.png`](render_gallery_cycles.png)       | 14/14 |
| OSPRay    | [`render_gallery_ospray.png`](render_gallery_ospray.png)       | 14/14 (dim — §6) |
| pbrt      | [`render_gallery_pbrt.png`](render_gallery_pbrt.png)           | 14/14 |

Regenerate: `.venv/bin/python3 scripts/render-material-gallery.py` (all renderers) or
`scripts/render-gallery.py` (one part across renderers). Both adapt to what's installed.

---

## 6. Known limitations

- **Terrazzo material fails on Appleseed.** Rendering the `Terrazzo` card with
  `renderer="Appleseed"` produces no image (the tool raises *"produced no output
  image"*). The cause is **not** the texture: that render path trips the Render addon's
  optional Python **virtual-environment bootstrap**, which downloads over the network and
  fails in offline / certificate-restricted environments —
  `ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED` in `virtualenv.py
  _create_virtualenv`, aborting Appleseed with exit code -6. The other image-textured
  card (`GreenMarble`) renders fine on Appleseed, and Terrazzo renders fine on POV-Ray
  and LuxCore — so it is specific to Terrazzo **on Appleseed**. The gallery marks it with
  a "✗ did not render" cell. *Likely fix:* pre-provision the addon's render venv (or fix
  the host certificate chain) so the bootstrap succeeds without network access.
- **OSPRay renders dim / low-contrast on the stock template.** The addon's
  `ospray_standard.sg` template lights the scene with a single *ambient* light at
  `color [0.2, 0.2, 0.2]` and no directional/area light, so `renderer="Ospray"` produces a
  correct but globally dark, low-contrast image. The same box renders bright on the other
  five renderers (which ship brighter template lighting), so this is an addon-template
  trait, not a problem with the built `ospStudio` binary. Consequence: OSPRay's gated test
  (`test_photoreal_ospray_renders`) does **not** clear the non-blank threshold (image
  std-dev ≈ 2.3 vs. the required > 3) — unlike the other renderers it would *fail* rather
  than skip once `ospStudio` resolves. *Likely fix:* brighten the ambient light (or add a
  sun/area light) in the OSPRay template — out of scope here since the template is a
  shipped third-party addon resource. The two image-mapped cards
  (`GreenMarble`, `Terrazzo`) show their colour map; normal / displacement maps are
  dropped (a POV-Ray-plugin limitation). See
  [`RENDER_TEXTURE_CHECK.md`](RENDER_TEXTURE_CHECK.md).
- **Photoreal output is not bit-reproducible** (sampler noise, thread count), so it is
  *presentation-only* and stays out of the reliability/golden tests; the gated tests
  assert invariants (valid PNG, non-blank, view/material changes the image), not pixels.
- **Appleseed is a 2019 build** (2.1.0-beta, the project's final release). It works but
  is frozen. Its zip ships some files at mode `600`; the install script normalizes
  permissions (`chmod -R a+rX`) so a non-root user can load the libraries.
- **The Render addon is unmaintained** (§2). We pin a commit and may fork/re-host. A
  harmless SSL traceback can appear on `import Render` from the same venv-bootstrap
  thread; it never reaches the protocol channel and is non-fatal.
- **Long / orphaned renders.** Renderers run as worker child processes. DriftPin spawns
  the worker in its own process group and group-kills it on shutdown, so a render in
  flight when the worker exits doesn't leak a runaway process. For long renders use the
  async API (`render_photoreal_submit` + `render_job`).
- **Sandbox/execution caveat.** Installing + wiring a hand-fetched renderer is fine; an
  agent sandbox may block *executing* it (untrusted external code). Real renders need a
  trusted/provisioned environment.

---

## 7. See also
- [`RENDER_WORKBENCH.md`](RENDER_WORKBENCH.md) — architecture / design record + the API.
- [`RENDER_RENDERER_INSTALL.md`](RENDER_RENDERER_INSTALL.md) — provisioning detail, pinned
  assets + checksums, source-build phase, the install script internals.
- [`RENDER_TEXTURE_CHECK.md`](RENDER_TEXTURE_CHECK.md) — textured-material verification runbook.
