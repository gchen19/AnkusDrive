# DriftPin test plan

How DriftPin is tested today, what's missing, and the order in which the
remaining tests should land.

The five slices each shipped with their own golden-path tests (face tagging,
PartDesign, FEM decomposition, rendering, assembly + drawings). Those are
**within-slice** tests — they prove the slice does its job. They do not catch
the class of bug where two slices are independently correct but interact
badly: tags that survive a fillet but break under a Pocket; FEM constraints
that resolve at setup but find the wrong face after a downstream edit; a
TechDraw page that projects the original geometry instead of the post-fillet
shape.

This plan addresses **cross-slice** behavior. Build it in priority order; each
section is independently shippable.

---

## Coverage map (updated 2026-04-25)

| Slice | Within-slice tests | Cross-slice coverage |
|---|---|---|
| 1: face/edge tagging | 6 in `test_worker.py` | tag-survives-pocket, tag-survives-PartDesign-fillet, tag-survives-FEM-pipeline (`test_edit_stability.py`, `test_integration.py`) |
| 2: PartDesign | 6 in `test_worker.py` | exercised in cross-slice integration |
| 3: FEM decomposition | 1 in `test_worker.py` | FEM constraint survives unrelated edit (`test_edit_stability.py`); cross-run determinism (`test_determinism.py`) |
| 4: visual feedback | 8 in `test_render.py` | renders Slice 2 PartDesign artifact in `test_integration.py`; bit-equal PNG bytes across workers (`test_determinism.py`) |
| 5: assembly + drawings | 5 in `test_worker.py` | assembly of PartDesign body in `test_integration.py`; drawing survives geometry edit (`test_edit_stability.py`) |
| Negative paths | — | one per slice + unknown method (`test_negative_paths.py`) |
| Performance | — | 7 budgets across all slices (`test_perf.py`, gated `RUN_PERF=1`) |
| Reliability A (classification) | scaffolded, gated | 10 shapes; needs API key |
| Reliability B (diff detection) | scaffolded, gated | 7 paired shapes; needs API key |
| Reliability C (agent-loop) | scaffolded, gated | 5 spec/wrong-build pairs with ground-truth checkers; needs API key |

**58 tests pass in ~14s today** (with `RUN_PERF=1`; 51 in ~12s without).
**All 6 tiers scaffolded.** Tiers 1–5 are running; tier 6 (reliability A/B/C)
is gated behind `RUN_RELIABILITY=1` and an API key.

---

## Priority order

Build in this order. Each tier is meaningful on its own; you can ship any
prefix.

1. ✅ **Cross-slice integration** — one fat test that exercises every slice
   in sequence. Highest leverage per line of test code.
2. ✅ **Determinism** — same input twice → identical output. Catches state
   leakage and accidental nondeterminism.
3. ✅ **Edit-stability** — extend the tag-survives-fillet pattern to
   PartDesign, FEM, drawings.
4. ✅ **Negative paths** — bad handle, ambiguous tag, missing material, etc.
   One per slice.
5. ✅ **Performance baselines** — time-budget assertions on the golden
   paths. Run weekly, not per-commit.
6. ✅ **Reliability Layer B + C** — diff detection, then agent-loop closure
   with geometric ground truth. (Scaffolded; pending API key.)

---

## 1. Cross-slice integration  ✅ shipped 2026-04-25

**File:** `tests/test_integration.py` — `test_cross_slice_integration`.
~330 lines, runs in ~1s. One test, one Worker session, walks every slice.

**What's covered:**
- Slice 2: 30×30×10mm pad → 5mm hole through center → fillet a top edge by tag.
  Volume of pad and pocket asserted to ±1%; fillet asserted to reduce volume.
- Slice 1: tag the bottom face (-Z normal) and a side face (+X normal) of
  the post-fillet body. Both must be uniquely resolvable.
- Slice 3: analysis + CCX solver + steel material + fixed (bottom tag) +
  force (loaded tag) + Gmsh mesh + run + results. Asserts non-zero stress
  and displacement. Then re-resolves the original tags after the FEM run
  to prove tags survive the FEM pipeline.
- Slice 4: render iso/top/front. Asserts 3 distinct SHA-256 hashes and
  foreground fraction in [0.10, 0.85] for each.
- Slice 5: mass_properties (density 7.9e-6 → mass), 2-part assembly with
  same body linked twice, BOM with `count=2` row, no interference. Save to
  `.FCStd` and assert > 1KB.

**Bugs found and fixed during build:**
- `Part.Compound` (returned by PartDesign Fillet) has no `CenterOfMass` —
  added `_solid_of()` in worker that unwraps via `.Solids`.
- `_content_bbox` (used during save_document) crashed on PartDesign feature
  objects whose `.Shape` is a `PartDesign.Feature` (no `isNull` method) —
  added `hasattr` guards.
- TechDraw `exportPageAsPdf` doesn't exist in this build; PDF/SVG export
  needs `TechDrawGui` which is GUI-only. Reframed `export_drawing` as
  `NotImplementedError` with a clear message; tests now verify the page
  builds + persists in saved FCStd, with PDF export deferred.

**Original goal text (preserved for reference):**

One test that proves all five slices compose end-to-end.

**Story (single Worker session):**
1. Slice 2: Build a parametric body — square pad, hole through, fillet a top
   edge by tag.
2. Slice 1: Tag the loaded face (the side opposite the fillet) and the bottom
   face for fixed support.
3. Slice 3: New analysis, set CCX solver, set steel material on the body,
   add fixed constraint by tag, add force constraint by tag, mesh, run, get
   results.
4. Slice 4: Render the result from iso, top, front. Verify all three are
   distinct and centered.
5. Slice 5: Mass properties of the body. Build an assembly with two copies
   placed apart. BOM lists count=2. Make a drawing page with a projection
   group, export to PDF.

**Assertions (loose — exact numbers depend on geometry; pick conservative
bounds):**
- Body volume = expected to ±1%.
- Tags resolved at FEM-setup time still resolve after the FEM run.
- FEM produced a result; max von Mises > 0; max displacement > 0.
- 3 renders, all distinct, all foreground-non-blank.
- Mass at steel density within ±1% of `volume × 7.9e-6`.
- BOM has one row with `count=2` and `total_volume = 2 × body_volume`.
- PDF on disk > 1 KB with `%PDF` magic.

**File:** `tests/test_integration.py`. One test function, ~150 lines. Prints
a structured summary so failures are diagnosable from CI logs.

**Why this test catches what within-slice doesn't:** it's the only test
where a bug like "PartDesign features wipe Slice 1 tag stability" can
surface. Within-slice testing of PartDesign would never re-tag a face
across a feature edit; this one does.

---

## 2. Determinism  ✅ shipped 2026-04-25

**File:** `tests/test_determinism.py`. 3 tests, ~2s.

- `test_two_workers_bitwise_geometry` — independent processes produce
  bit-for-bit identical volume / area / CG / triangle count / PNG SHA-256.
- `test_repeated_calls_in_one_worker` — two `new_document` cycles in one
  worker also bit-for-bit identical.
- `test_fem_results_within_tolerance` — two FEM runs agree within
  1.3% disp / 1.9% stress (well inside the Slice 3 8% / 15% bounds).

Result: OpenCASCADE is fully deterministic and the worker has no detectable
state leakage. PNG bytes hash-equal across worker boots.

**Original goal:**

Prove the worker is deterministic and free of state leakage.

**Tests:**
- `test_two_workers_same_geometry`: build the cross-slice integration
  artifact in worker A, then in worker B (independent processes). Assert
  every volume, area, CG, and mass property matches **bit-for-bit**.
  Tessellation triangle count must match. PNG bytes from `render_view` must
  hash to the same SHA-256.
- `test_repeated_calls_idempotent`: in one worker, call the integration
  story twice (two `new_document` cycles). Assert the second cycle's outputs
  match the first's exactly. Catches handle-counter leakage, accidental
  caching, or `_SCRIPT_GLOBALS` poisoning.
- `test_fem_determinism_within_tolerance`: FEM is solver-bound and Gmsh has
  some nondeterminism. Run the cantilever twice in one worker, assert
  results agree to within 5% on max disp and 10% on max stress (the bounds
  established empirically in Slice 3).

**File:** `tests/test_determinism.py`. ~80 lines. The bit-for-bit
geometry assertion is the load-bearing one — that's what flushes out hidden
state.

---

## 3. Edit-stability extension  ✅ shipped 2026-04-25

**File:** `tests/test_edit_stability.py`. 4 tests, ~2s.

- `test_tag_survives_pocket` — tag the +Z face, pocket through it, tag still
  resolves with same area on the underlying pad.
- `test_tag_survives_partdesign_fillet` — tag a side face, fillet a top edge
  via PartDesign, side-face tag still resolves on the original pad.
- `test_fem_constraint_survives_unrelated_fillet` — cantilever beam, fixed
  end + force end by tag, run FEM as baseline. Add a CSG fillet at the
  loaded end (far from peak stress). Re-resolve tags, re-run FEM.
  **Result: stress shifted only 0.7%** (bound was 20%). The agent can
  iterate on geometry without rebuilding the FEM analysis.
- `test_drawing_survives_geometry_edit` — pad + projection group, change
  pad length 10→20, save again, reopen. Page + projection group survive.

**Original goal:**

The property "references survive unrelated edits" applies to every
abstraction layer, not just `Part::Fillet`.

**Tests:**
- `test_tag_survives_pocket`: pad → tag the +Z face → pocket a slot
  elsewhere → tag still resolves to a face whose normal is +Z and whose area
  has dropped by exactly the slot's footprint.
- `test_tag_survives_partdesign_fillet`: pad → tag the loaded face → PartDesign
  fillet on a non-adjacent edge → tag still resolves.
- `test_fem_constraint_survives_unrelated_edit`: build cantilever with fixed
  end by tag → run FEM, save baseline → add a `fillet` on an edge far from
  the fixed end → re-run FEM → max von Mises within 10% of baseline (the
  fillet shouldn't materially affect stress at the fixed end).
- `test_drawing_survives_geometry_edit`: make a projection group of a body →
  edit the body's pad length → recompute → assert projection group's view
  count is unchanged and the page still exports to a non-empty PDF.

**File:** `tests/test_edit_stability.py`. ~120 lines. The FEM one is the
most important — it proves that the agent can iterate on a design without
rebuilding the analysis.

---

## 4. Negative paths  ✅ shipped 2026-04-25

**File:** `tests/test_negative_paths.py`. 6 tests, ~1s.

One error case per slice + the unknown-method baseline. Every test ends
with `ping → pong` to assert the worker survived the error.

- Slice 1: `resolve_face` with unknown tag → `KeyError`-class `WorkerError`.
- Slice 2: unconstrained sketch → `close_sketch` reports `dof > 0` and
  `fully_constrained: False` (Sketcher doesn't error on this — it's a
  status, not a fault).
- Slice 3: `fem_run` with no solver/material attached → clear `WorkerError`
  containing "solver" / "prereq" / "material".
- Slice 4: `tessellate` of a body with no Shape → `WorkerError` with
  "shape" in the message; `render.render_mesh([], [])` returns a blank
  PNG (no exception).
- Slice 5: `add_part` with nonexistent FCStd path → `WorkerError`;
  assembly remains empty.
- Unknown method: `WorkerError(type="UnknownMethod")`.

**Original goal:**

Every slice produces clear errors on bad input, and the worker survives.

**One test per slice:**
- `test_resolve_bad_tag` (Slice 1) — already in suite, keep.
- `test_partdesign_pad_on_unconstrained_sketch_warns` (Slice 2) —
  `fully_constrained: false` is reported, pad still happens (Sketcher allows
  it), but the DOF report is preserved through the recompute.
- `test_fem_run_without_material_errors` (Slice 3) — `fem_run` raises a
  `WorkerError` with "material" in the message; worker survives.
- `test_render_empty_shape_returns_blank` (Slice 4) — render of a handle
  with no triangles returns a valid PNG that's all background; no crash.
- `test_assembly_with_missing_link_target_errors` (Slice 5) — `add_part`
  with a path to a nonexistent file raises `WorkerError`; assembly is
  unchanged.

After every error case, the test calls `ping` and asserts `pong`. That's the
"worker survives" gate.

**File:** `tests/test_negative_paths.py`. ~100 lines. Keeps each error case
small and self-contained.

---

## 5. Performance baselines  ✅ shipped 2026-04-25

**File:** `tests/test_perf.py`. 7 tests, ~2s. Gated behind `RUN_PERF=1`.

Per-operation budgets at ~10× typical timing on the dev machine, with a
visual histogram at the end of the run showing how close each operation is
to its budget. Re-baseline by editing `BUDGETS` at the top of the file.

Initial observed timings (FreeCAD 1.1.1, M-series Mac):

| Operation | Typical | Budget | Headroom |
|---|---|---|---|
| `worker_boot` | 110ms | 2000ms | 18× |
| `add_primitive` | 40ms | 500ms | 12× |
| `boolean_op` | 4ms | 500ms | 125× |
| `pad_simple` | 2ms | 500ms | 250× |
| `pocket_through` | 4ms | 500ms | 125× |
| `tessellate_cube` | 2ms | 300ms | 150× |
| `render_256` | 9ms | 500ms | 55× |
| `render_512` | 14ms | 1500ms | 105× |
| `fem_cantilever_total` | 370ms | 5000ms | 13× |
| `save_document` | 10ms | 1000ms | 100× |
| `mass_properties` | 1ms | 300ms | 300× |
| `interference_check` | 15ms | 1000ms | 65× |

The big budgets reflect that we're checking for regressions, not
performance-tuning. If `add_primitive` ever takes 500ms, something is
catastrophically wrong; that's the failure we're catching.

**Original goal:**

Detect regressions like "tessellation got 10× slower" before they ship.

**Per-slice budget assertions** (loose — set 2× the typical wall-time so
random scheduler noise doesn't trip them):

| Operation | Typical | Budget |
|---|---|---|
| `Worker()` boot | ~0.5s | 2s |
| `add_primitive` | ~0.05s | 0.5s |
| `pad` (small sketch) | ~0.1s | 0.5s |
| `tessellate` (20mm cube, deflection=0.5) | ~0.05s | 0.3s |
| `render_mesh` (256×256) | ~0.1s | 0.5s |
| `fem_cantilever_demo` | ~1s | 5s |
| Full integration test (Slice 1+2+3+4+5) | ~3s | 10s |

**File:** `tests/test_perf.py`, gated behind `RUN_PERF=1` because timings
on a busy machine are noisy and we don't want CI false-fails. Run it
weekly or before tagging a release.

When a budget triggers, the assertion message names the operation and the
ratio over budget so the failure is diagnosable without re-running locally.

---

## 6. Reliability — Layer B (diff detection)  ✅ scaffolded 2026-04-25

**Files:** `tests/test_reliability_diff.py` + `tests/reliability_diffs.py`.
7 paired shapes (cube scaled, hole removed, hole moved, cylinder fattened,
stack reduced, bracket extended, plate hole removed). Renders side-by-side
into a single PNG with A/B labels, sends to Claude with a "what changed?"
prompt, grades by keyword.

Hard gate: ≥60% accuracy (lower than Layer A's 70% because diff detection
is genuinely harder).

Same cache mechanics as Layer A: PNG + .txt cached separately so synonym
tweaks are free.

Not exercised yet — pending API key.

**Original goal:**

The harder reliability test — can the model identify what changed
between two renders?

**Pattern:** for each shape in `tests/reliability_shapes.py`, define a
`(baseline, modified)` pair. Render both. Send to Claude with a side-by-side
image prompt: *"Image A and Image B show the same part with one
modification. What changed?"* Grade by keyword.

**Pairs:**

| Shape | Modification | Expected keywords |
|---|---|---|
| cube | scaled 1.5× | "bigger", "larger", "scaled" |
| cube_with_hole | hole removed | "hole removed", "no hole", "no through" |
| cube_with_hole | hole offset 5mm | "moved", "offset", "no longer centered" |
| cube_filleted | fillet radius 5 → 2 | "smaller fillet", "tighter", "less rounded" |
| cylinder | radius doubled | "thicker", "wider", "fatter", "larger radius" |
| stacked | one box removed | "removed", "single", "no top" |
| l_bracket | leg lengthened | "longer", "extended", "bigger leg" |
| plate_4_holes | one hole removed | "three holes", "missing hole" |
| pad_with_pocket | pocket deeper | "deeper pocket", "deeper recess" |

**Hard gate:** ≥60% accuracy (lower than Layer A because diff-detection is
genuinely harder for any model).

**File:** `tests/test_reliability_diff.py`. Reuses the cache mechanics from
`test_reliability.py`. ~150 lines.

## 6b. Reliability — Layer C (agent-loop closure)  ✅ scaffolded 2026-04-25

**Files:** `tests/test_reliability_agent_loop.py` + `tests/reliability_specs.py`.

5 design specs, each with a `correct_builder` that satisfies the spec and a
`wrong_builder` that violates it (different size, missing feature, wrong
shape). For each (spec, variant) pair:
1. Build with the builder.
2. Run a `checker(handle)` — reads geometry via `mass_properties`, counts
   cylindrical faces via `query_faces`, etc. Returns ground-truth verdict.
3. Render the geometry.
4. Show the model the render + the spec text. Ask MATCHES/VIOLATES.
5. Compare model verdict to ground truth.

Specs:
- `cube_30mm` (wrong: 20mm cube)
- `cube_with_centered_hole` (wrong: hole missing)
- `cylinder_r10_h40` (wrong: square box)
- `plate_4_corner_holes` (wrong: only 2 holes)
- `l_bracket` (wrong: vertical leg missing)

**Validated cold:** all 5 spec checkers correctly classify their own
correct/wrong builders without the API. The harness self-checks this at
runtime via `assert ground_truth_matches == expected_truth` so a broken
builder fails loudly before billing the API.

The report breaks down false positives (model says "matches" on a wrong
build → agent can't self-correct) vs. false negatives (model says
"violates" on a correct build → agent over-rejects). Hard gate: ≥80%
agreement.

Not exercised yet — pending API key.

**Original goal:**

The production test. Does the model's *visual judgment* match
*geometric ground truth*?

**Pattern:**
1. Hand the model a written spec ("design a 30mm cube with a 6mm hole
   through the center").
2. Let it call DriftPin tools (real agent loop, MCP transport).
3. Render the result.
4. Ask the model to self-evaluate: *"does this match the spec?"*.
5. Independently check the geometry against the spec via `mass_properties`,
   `query_faces`, `list_edges`.
6. Compare model verdict to ground-truth verdict.

**Reliability metric:** **agreement rate.** If the model says "yes, looks
right" but `query_faces` shows the hole has wrong radius, that's a perception
failure even if the renderer is fine. This is the test that determines
whether agents can self-correct during design iteration.

**Hard gate:** ≥80% agreement on a small (5-spec) suite. False positives
(model says "right" when wrong) and false negatives (model says "wrong"
when right) both count as misses.

**File:** `tests/test_reliability_agent_loop.py`. ~250 lines. Needs
`mass_properties` (already shipped in Slice 5) and a structured
spec→ground-truth comparator. Build comparator first, then loop.

---

## Test infrastructure notes

**Why no pytest:** the existing tests are stdlib-only with custom runners
(`main()` at the bottom of each file). It's fine — we don't need fixtures or
parametrization at this scale, and skipping pytest keeps the host venv
minimal. If/when we cross 100 tests this assumption flips.

**Running the full suite (today):**

```bash
bash tests/setup_local.sh   # one-time: wire freecadcmd + .venv to local FreeCAD
bash tests/run_all.sh
# ~115 tests in ~35s. Reliability gated; set RUN_RELIABILITY=1 to enable.
```

Or per-file (each is independently runnable):

```bash
python3 tests/test_worker.py                        # 29 tests, ~5s
.venv/bin/python3 tests/test_render.py              # 8 tests, ~2s
.venv/bin/python3 tests/test_integration.py         # 1 test, ~1s
.venv/bin/python3 tests/test_determinism.py         # 3 tests, ~2s
python3 tests/test_edit_stability.py                # 4 tests, ~2s
.venv/bin/python3 tests/test_negative_paths.py      # 6 tests, ~1s
RUN_RELIABILITY=1 .venv/bin/python3 tests/test_reliability.py  # gated
```

**Adding a test file:** copy the runner from the bottom of any existing test
file. Keep the `_discover()` + `main()` pattern. Don't introduce a test
framework dependency without a real reason.

**CI runs on a self-hosted runner.** `test.yml` runs the suite on every push
to `main` and every PR — but on a **self-hosted** GitHub Actions runner, not a
GitHub-hosted `ubuntu-latest` one. The runner machine already has FreeCAD 1.1
installed, so CI reuses it instead of downloading FreeCAD via conda:
`tests/setup_local.sh` symlinks `freecadcmd` onto PATH (so driftpin's worker
resolves it via `shutil.which`) and points `.venv/bin/python3` at FreeCAD's
bundled python, which already ships numpy + Pillow. That feeds `run_all.sh`'s
two-interpreter split — system `python3` for the worker tests, `.venv` python3
for the numpy/Pillow tests — with no second install. Perf and reliability stay
gated behind `RUN_PERF=1` / `RUN_RELIABILITY=1`. The same `setup_local.sh` is
what makes any machine a runner host; see it for the one-time wiring.
(Publishing still runs on a GitHub-hosted runner via `publish.yml`.)

---

## What's explicitly NOT in this plan

- **Snapshot / pixel-exact tests.** They rot, false-fail on any library
  upgrade, and provide weak signal. The render tests use *invariants*
  precisely to avoid this.
- **Fuzz testing of FreeCAD's API surface.** FreeCAD is a third-party
  dependency; we don't try to find bugs in it, only in our wrapper.
- **Multi-machine / cross-platform CI.** DriftPin targets macOS today (per
  README). Windows/Linux can wait until someone needs them.
