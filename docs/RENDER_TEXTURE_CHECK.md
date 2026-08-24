# Verifying POV-Ray texture-map rendering (manual QA runbook)

A repeatable procedure for confirming that AnkusDrive's `render_photoreal` actually
renders the **image texture maps** of the FreeCAD Render addon's textured material
cards, under POV-Ray specifically. This complements the automated photoreal tests in
[`tests/test_render_photoreal.py`](../tests/test_render_photoreal.py) and closes the
"textured materials under POV-Ray" follow-up in
[`RENDER_WORKBENCH.md`](RENDER_WORKBENCH.md) §7.

## Why this is a manual check

The automated suite covers *solid / procedural* materials — e.g. it asserts `Gold`
renders yellow (red ≫ blue). It deliberately does **not** assert that an image-mapped
material shows its *pattern*, because "does the marble pattern actually appear" is a
visual judgement: a non-blank PNG with the right average colour can still be missing
its texture. So image-textured cards get an eyeball via this runbook. (An optional
automated *proxy* is described at the end — it catches "texture silently dropped"
regressions without judging the pattern.)

## Scope — which cards

Only two shipped library cards carry real image maps; the rest are solid or
procedural and need no texture check:

| Card (`material=`) | Image dir under the addon's `materials/` | Maps in the card |
|---|---|---|
| `GreenMarble` | `GreenMarble/` (7 images) | Color, Roughness, Normal, Displacement |
| `Terrazzo`    | `Terrazzo019/` (5 images) | Color, Roughness, Normal, … |

(Find them yourself: cards live in `<App.getUserAppDataDir()>/Mod/Render/materials/`;
a card is textured iff it has a sibling sub-directory of `.jpg`/`.png` images and
`Render.Textures.*` keys.)

## What POV-Ray supports — set expectations before you look

The Render addon's POV-Ray plugin maps **only some** texture channels
(`Render/renderers/Povray.py`):

- **Color (albedo / base colour) → rendered.** This is the one you can see: the
  marble/terrazzo *pattern* should appear on the surface.
- **Roughness → applied.**
- **Normal and Displacement → silently dropped.** The plugin prints
  *"Povray does not support 'normal' or 'displacement'"* and emits nothing for them.

So the correct result is a **flat surface carrying the right colour pattern**, *not*
bumpy relief. Absence of bump is expected, not a defect. (For normal/displacement
relief, use a renderer that supports it — e.g. LuxCore — which is a separate check.)

## Prerequisites

- The Render addon **and** POV-Ray installed (see `RENDER_WORKBENCH.md` §6).
  Confirm with: `.venv/bin/python3 tests/test_render_photoreal.py` — the renderer
  tests should PASS, not SKIP. If they SKIP, the addon/binary isn't found.
- A Python env with `ankusdrive` importable (the repo `.venv`).

## Procedure

Render the two textured cards plus a solid reference, from the repo root:

```bash
PYTHONPATH=. .venv/bin/python3 - <<'PY'
import base64
from ankusdrive.client import Worker
with Worker() as w:
    w.call("new_document", name="texcheck")
    # a wide, shallow slab shows a surface pattern better than a cube
    h = w.call("add_primitive", kind="box", w=60, d=60, h=8)["handle"]
    for mat in ("GreenMarble", "Terrazzo", "Gold"):   # Gold = solid reference
        r = w.call("render_photoreal", handle=h, view="iso", material=mat,
                   width=480, height=360, _timeout=300)
        out = f"/tmp/tex_{mat}.png"
        open(out, "wb").write(base64.b64decode(r["png_base64"]))
        print("wrote", out)
PY
```

Then open `/tmp/tex_GreenMarble.png`, `/tmp/tex_Terrazzo.png`, `/tmp/tex_Gold.png`
in any image viewer.

## Pass criteria

The check **passes** when:

1. **GreenMarble** — the slab faces show a green/teal *mottled marble* pattern with
   visible spatial colour variation (light and dark blotches), clearly not a single
   flat shade.
2. **Terrazzo** — the slab faces show a beige/cream *speckled aggregate* (terrazzo)
   pattern, again spatially varying.
3. **Reference** — both differ obviously from the solid `Gold` slab, which is a
   uniform metallic shade with only lighting gradient, no pattern.

If a "textured" slab looks like a single flat colour, the texture did **not** map —
that's a **fail** (see Troubleshooting).

### Quantitative sanity check (optional, supports the eyeball)

A textured render has far more distinct colours in the part region than a solid one.
Measured on the reference run below (central slab region):

| Material | unique colours (centre) |
|---|---|
| `Gold` (solid) | ~140 |
| `GreenMarble` | ~2,000 |
| `Terrazzo` | ~3,700 |

Rule of thumb: a correctly-textured slab shows **>~1,000 unique colours** in the part
region, an order of magnitude above a solid card (~150). Quick check:

```bash
.venv/bin/python3 - <<'PY'
from PIL import Image; import numpy as np
for m in ("Gold","GreenMarble","Terrazzo"):
    a=np.asarray(Image.open(f"/tmp/tex_{m}.png").convert("RGB")); hgt,wid,_=a.shape
    c=a[hgt//4:3*hgt//4, wid//4:3*wid//4].reshape(-1,3)
    print(f"{m:11} unique_colours={len(np.unique(c,axis=0))}")
PY
```

## Last confirmed

- **Date:** 2026-06-04 — **result: PASS** (both `GreenMarble` and `Terrazzo`).
- **Env:** FreeCAD 1.1.0, POV-Ray 3.7.0.10, Render addon commit
  `08be2fe94b8a998323c8a5443f7f0afd0d05bed5`, Linux.
- **Observed:** GreenMarble rendered visible green marble mottling; Terrazzo rendered
  beige speckled aggregate; both flat (no bump — expected, per POV-Ray limits above);
  solid `Gold` uniform metallic. Unique-colour counts 143 / 2036 / 3721 respectively.

Re-run and update this block whenever the addon commit pin or POV-Ray version changes.

## Troubleshooting

- **Flat single colour, no pattern (fail).** The image map didn't reach the renderer.
  Check, in order:
  - The image dir exists under `…/Mod/Render/materials/` (`GreenMarble/`,
    `Terrazzo019/`) and is non-empty — `import_textures` resolves images relative to
    that `materials/` dir.
  - The FreeCAD report/log (run the worker with `capture_stderr=True`, or
    `freecadcmd` directly) for "texture not found" / image-load errors.
  - `View.UvProjection` — flat shapes need usable UVs for the texture to land.
- **Pattern present but tiny / over-tiled.** Texture scale; adjust the card's
  `Render.Textures.<name>.Scale`.
- **"Povray does not support 'normal' or 'displacement'" warnings.** Expected — those
  channels are dropped by design (see above). Not a failure.

## Optional automated guard (future)

A CI-able *proxy* (not a replacement for the eyeball) could render `GreenMarble` and
assert its part-region unique-colour count is, say, ≥5× that of a solid card rendered
identically. That regression-guards "texture silently stopped mapping" without judging
the pattern. It would be renderer-gated (skip when POV-Ray/addon absent), like the rest
of `tests/test_render_photoreal.py`. Not implemented yet — this runbook is the current
source of truth.
