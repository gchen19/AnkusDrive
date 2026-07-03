# Reliability tests — "can the agent see what it built?"

The renderer-invariant tests (`tests/test_render.py`) prove pixels are
*correct*: silhouette is centered, sphere projects to a disc, concave hole is
empty. Those are necessary but not sufficient. They cannot answer the question
that actually matters for an agent loop:

> When we hand the rendered image back to a Claude model, can it correctly
> describe what it's looking at?

A renderer can pass every pixel-invariant test and still produce images the
model misinterprets — wrong shading direction, ambiguous projections,
triangulation artifacts that read as features. The reliability suite catches
that class of failure by closing the loop end-to-end.

This is **gated behind paid API calls**, so it's an opt-in suite, not part of
the default test run.

---

## Quick start

Prereqs:
- Anthropic API key in `ANTHROPIC_API_KEY`.
- Host venv has `anthropic` installed (`pip install anthropic` — already
  pinned in this project's venv).

Run:

```bash
ANTHROPIC_API_KEY=sk-... RUN_RELIABILITY=1 .venv/bin/python3 tests/test_reliability.py
```

First run renders + hits the API for all 10 shapes (~30s, a few cents on
Sonnet 4.5). Subsequent runs are instant from cache.

---

## What it tests today (Layer A)

Single-image classification. Render N known shapes, ask Claude what each is,
grade by keyword match. Catches "renderer broken" and "model can't recognize
basic primitives" but not "model is hallucinating features that aren't there".

**10 shapes** in `tests/reliability_shapes.py`:

| Shape | What it proves |
|---|---|
| cube | basic primitive recognition |
| rect_box (40×20×10) | not-a-cube discrimination |
| cylinder | curved-surface primitive |
| sphere | full curved surface |
| cube with through-hole | concave geometry visible (painter's-algo killer) |
| cube with filleted edge | small feature recognition |
| stacked stepped boxes | multi-feature composition |
| L-bracket | recognizable mechanical form |
| plate with 4 corner holes | common mechanical feature pattern |
| PartDesign pad+pocket | parametric pipeline through to render |

**Grader** (per shape):
- `must_match_any`: at least one synonym appears in the response (e.g. cube →
  {"cube", "box", "block", "square"})
- `must_match_all`: list of groups; each group must have ≥1 hit. Lets us
  require *both* a primary-shape word AND a feature word (e.g. "cube" AND
  "hole" for the holed cube)
- `must_not_match`: none of these appear. Catches false positives like
  identifying a cube as a sphere.

**Hard gate:** suite fails (exit 1) if accuracy drops below 70%. That's well
above chance, below "indistinguishable from a human" (~95%), and gives the
renderer headroom to improve without making the bar arbitrary.

---

## Cache mechanics

`tests/reliability_cache/` (gitignored):

```
cube_iso.png                    ← Layer A: single-shape render
cube_iso.txt                    ← Layer A: model response
hole_removed_diff.png           ← Layer B: paired A/B render
hole_removed_diff.txt           ← Layer B: model response
cube_30mm__correct_loop.png     ← Layer C: spec-matching render
cube_30mm__correct_loop.txt     ← Layer C: model verdict
cube_30mm__wrong_loop.png       ← Layer C: spec-violating render
cube_30mm__wrong_loop.txt       ← Layer C: model verdict
report.json                     ← Layer A
report_diff.json                ← Layer B
report_agent_loop.json          ← Layer C
```

- **PNG cache** survives renderer changes silently — delete a file (or the
  whole dir) when you actually want a re-render.
- **TXT cache** survives across grader tweaks. Iterating on synonym lists is
  free; only deleting the .txt forces a re-billed API call.

This is intentional: the cost of running the suite is in the API call, not
the render. Caching them separately means you can refine the grader on real
responses without paying again.

---

## When to run

- After any change to `driftpin/render.py` or `driftpin/worker.py::_h_tessellate`.
- After upgrading the model in the harness (top of `test_reliability.py`).
- Before declaring "agent-eyes works" for any new shape category — add the
  shape to the library, run the suite, watch what Claude actually says.

Don't run it on every commit. Run it on every renderer change.

---

## Layers B and C — scaffolded 2026-04-25

Both are wired and ready; same gating + cache mechanics as Layer A. Each
runs as part of `bash tests/run_all.sh` when `RUN_RELIABILITY=1` is set.

### Layer B — diff detection (`tests/test_reliability_diff.py`)

Renders baseline + modified shape side-by-side into a single PNG with A/B
labels. Asks "what changed?". Grades by keyword.

7 paired shapes in `tests/reliability_diffs.py`:

| Pair | Expected keywords |
|---|---|
| cube 20mm → 30mm | bigger / larger / scaled |
| cube with hole → without | hole removed / no through |
| centered hole → offset to corner | moved / off-center / shifted |
| cylinder r=5 → r=15 | thicker / wider / fatter |
| 2-tier stack → bottom slab only | removed / single / no top |
| L-bracket leg 40mm → 80mm | longer / extended |
| plate with 4 holes → 3 holes | fewer / missing / three |

Hard gate: ≥60% accuracy (lower than Layer A's 70% — diff detection is
genuinely harder).

### Layer C — agent-loop closure (`tests/test_reliability_agent_loop.py`)

The production test. For each design spec, build TWO renders — one matching
the spec, one deliberately violating it (different size, missing feature,
wrong shape). For each:
1. Run a `checker(handle)` that reads geometry via `mass_properties` and
   `query_faces` to produce **ground truth**.
2. Show the model the render + the spec text. Ask MATCHES/VIOLATES.
3. Compare model verdict to ground truth.

5 spec/wrong-build pairs in `tests/reliability_specs.py`. The harness
self-checks at runtime that each `correct_builder` actually passes its
checker and each `wrong_builder` actually fails — a builder bug fails
loudly *before* billing the API.

Hard gate: ≥80% agreement. The report breaks down false positives
(model says "matches" on a wrong build → agent can't self-correct) vs.
false negatives (model says "violates" on a correct build → agent
over-rejects).

This is the test that determines whether an agent can self-correct during
a design iteration. If the model can't tell when its own output diverges
from the spec, it can't fix mistakes.

### Layer D — full agent tool-use (`tests/test_reliability_tasks.py`)

**Shipped.** Layer C tests *visual judgment under spec* — it doesn't let the
model call DriftPin tools. Layer D tests the other half: *can the model DRIVE the
tools to produce geometry that meets a goal.* The agent gets an English design
goal and the DriftPin tool surface, runs a real tool-use loop against a real
FreeCAD worker (via `orchestration/agentkit.py`), then the resulting SOLID is
graded with DriftPin's own Tier-3 inspection tools (`bounding_box` /
`check_shape` / `min_clearance` / `mass_properties`) as the ground-truth oracle.
We never assert *which* tools it called — only that the artifact is correct.

Same gating as B/C: real runs need `RUN_RELIABILITY=1` + `ANTHROPIC_API_KEY`; the
scripted `ScriptedClient` path runs free as the harness self-test / grader
negative-control. Multi-trial (pass *rate*, not pass/fail) × multi-model
(haiku/sonnet/opus), with per-miss tool-trace + oracle-reason capture.

---

## Tweaking the suite

**Adding a shape:** append a `(builder, ShapeSpec)` to `SHAPES` in
`tests/reliability_shapes.py`. Builder takes a Worker, returns a handle. Run
the suite, eyeball the response, iterate on the spec's keyword lists.

**Changing the prompt:** edit `PROMPT` at the top of `test_reliability.py`.
This invalidates all cached `.txt` files (since the question changed); delete
them or accept that the grader is now scoring stale responses.

**Changing the model:** edit `MODEL`. Like the prompt, this should clear the
text cache.

**Lowering the accuracy bar:** edit `bar = 0.70` near the bottom of `main()`.
Don't lower it casually — the bar is the only thing forcing renderer
improvements when accuracy regresses.
