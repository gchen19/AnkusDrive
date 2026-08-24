# AnkusDrive — Logo

An **ankus** is the hooked steel goad used to *guide* an elephant — a small, precise instrument
directing something far more powerful. The mark stages that: a slate line **pierces two
overlapping holes** and brings them onto one shared axis.

- the **generative hole** (teal) — an attention-grid matrix with a scattered point cloud of
  samples around it: the probabilistic / LLM side;
- the **precise CAD hole** (ink) — a dimensioned datum with dash-dot centerlines: the
  deterministic constraint side.

They overlap like a Venn — the shared, aligned region — and the slate line threads through
both. Slate gray because that's the bare steel such a tool is made of.

**About the Ø14 callout:** the diameter dimension is a real draftsman's annotation, and the
number is the brand's initials — **A = 1, D = 4**. (It replaced a placeholder `Ø34` that was
only an artifact of the original drawing geometry.)

## Files

```
icon/       Primary square icon. SVG + PNG 512/256/128. Light, dark, and transparent.
favicon/    Stripped reduction (two rings + line) for 16–48px. SVG, PNG, .ico. Light + dark.
wordmark/   Horizontal lockups: full (with tagline) and compact, light + dark.
            *-alt versions set "Ankus" in slate instead of "Drive".
ankusdrive-brand-sheet.png   One-page overview.
```

## Usage

- **GitHub org / repo avatar:** `icon/ankusdrive-icon-512.png` (tile), or the transparent
  version to float on a colored profile.
- **README header:** `wordmark/ankusdrive-wordmark-1280.png` (light) /
  `ankusdrive-wordmark-dark-1280.png` (dark). GitHub supports `<picture>` with
  `prefers-color-scheme` to auto-swap:

  ```html
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="logo/ankusdrive/wordmark/ankusdrive-wordmark-dark-1280.png">
    <img src="logo/ankusdrive/wordmark/ankusdrive-wordmark-1280.png" alt="AnkusDrive" width="420">
  </picture>
  ```

- **Favicon / docs:** `favicon/favicon.ico` or the sized PNGs.
- Prefer the **SVG** wherever possible — resolution-independent, and the wordmark text is
  outlined so it renders identically without the brand font installed.
- Clear space: keep at least one hole-radius of margin around the mark. Don't recolor the
  line away from slate — it's the element that carries the tool.

## Palette

| Role | Name | Light | Dark ground |
|------|------|-------|-------------|
| The pin / goad (the line) | Slate | `#5E6A78` | `#93A0AE` |
| Generative / LLM | Teal | `#2E8B84` | `#4FB3AA` |
| CAD datum / precise | Ink | `#1E2A2A` | lines `#DCE7E5` |
| Ground | Bone / dark | `#F2EFE7` | `#12201E` |

Wordmark type: NimbusSans Bold (Helvetica-metric grotesk), "Drive" set in slate; glyphs are
outlined in the SVGs. Swap in Inter / Helvetica Neue if you rebuild.

## Trademark

The AnkusDrive name and logo are trademarks of their owner and are **not** covered by the
project's Apache-2.0 code license. See `NOTICE` in the repository root.
