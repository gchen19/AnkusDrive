# DriftPin — Logo

The mark stages what a drift pin actually does. A real **drift pin** (a "carrot taper" pin)
is the tapered rod an ironworker drives through two misaligned bolt holes to force them into
registration — it doesn't fasten, it *aligns*. Here the pin is abstracted to a single line that
**pierces two overlapping holes**:

- the **generative hole** (teal) — an attention-grid matrix with a scattered point cloud of
  samples around it, the probabilistic / LLM side;
- the **precise CAD hole** (ink) — a dimensioned datum (Ø34, dash-dot centerlines), the
  deterministic constraint side.

They overlap like a Venn — the shared, aligned region — and the slate line threads through
both, bringing them onto one axis. (Slate gray because that's the bare steel a real drift pin
is made of.)

## Files

```
icon/       Primary square icon. SVG + PNG 512/256/128. Light, dark, and transparent.
favicon/    Stripped reduction (two rings + line) for 16–48px. SVG, PNG, .ico. Light + dark.
wordmark/   Horizontal lockups: full (with tagline) and compact. SVG + PNG 1280. Light + dark.
driftpin-brand-sheet.png   One-page overview.
```

## Usage

- **GitHub org / repo avatar:** `icon/driftpin-icon-512.png` (tile) or the transparent version to float on a colored profile.
- **README header:** `wordmark/driftpin-wordmark-1280.png` (light) / `driftpin-wordmark-dark-1280.png` (dark).
  GitHub supports `<picture>` with `prefers-color-scheme` to auto-swap:

  ```html
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="logo/wordmark/driftpin-wordmark-dark-1280.png">
    <img src="logo/wordmark/driftpin-wordmark-1280.png" alt="DriftPin" width="420">
  </picture>
  ```

- **Favicon / docs:** `favicon/favicon.ico` or the sized PNGs.
- Prefer the **SVG** wherever possible — resolution-independent, and the wordmark text is
  outlined so it renders identically without the brand font installed.
- Clear space: keep at least one hole-radius of margin around the mark. Don't recolor the
  line away from copper — it's the one element that carries the name.

## Palette

| Role | Name | Light | Dark ground |
|------|------|-------|-------------|
| The pin (the line) | Slate | `#5E6A78` | `#93A0AE` |
| Generative / LLM | Teal | `#2E8B84` | `#4FB3AA` |
| CAD datum / precise | Ink | `#1E2A2A` | lines `#DCE7E5` |
| Ground | Bone / dark | `#F2EFE7` | `#12201E` |

Wordmark type: NimbusSans Bold (Helvetica-metric grotesk), "Pin" set in slate; glyphs are
outlined in the SVGs. Swap in Inter / Helvetica Neue if you rebuild.
