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
Sonnet). Subsequent runs are instant from cache.

**Choosing the model.** Every harness resolves its model through
`orchestration/claude_models.py`. Set `ANKUSDRIVE_EVAL_MODEL` to a tier alias
(`opus` / `sonnet` / `haiku`, which resolve to the current generation) or to a
full model ID. The default is `sonnet` for the reliability suites and `haiku`
for M2 (`M2_MODEL` still works). Cached responses are keyed by model
(`cube_iso.<model>.txt`), so switching models re-queries instead of silently
re-grading the old model's answers. Results recorded before 2026-11 used
`claude-sonnet-4-5` / `claude-haiku-4-5-20251001`. Pass those IDs to reproduce
them. Opus costs in MULTI_AGENT_EVAL.md rows from that period were computed at
the old $15/$75 rate, so they are 3× too high.

**Tool selection.** A separate paid eval checks whether each tool's description
makes Claude pick that tool over its look-alikes (`fem_modal` vs `beam_modal`,
`section_view` vs `add_section_view`, and so on). Its free tier runs in
fast-checks. See [TOOL_SELECTION.md](TOOL_SELECTION.md) for how to run it, what it
costs, and how to turn its confusion list into description rewrites.

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

### LLM judge (opt-in, issue #507)

The keyword grader has two known failure modes: it fails correct paraphrases
("rectangular prism" for a box, "bore" for a hole), and it fails answers that
mention an anti-keyword in a negation ("not a sphere" trips
`must_not_match: sphere`). Growing the suite then means maintaining synonym
lists. An opt-in judge grades on meaning instead.

| `RELIABILITY_GRADER` | What runs | What the 70% gate uses |
|---|---|---|
| `keyword` (default) | keyword grader only, no extra API calls | keyword |
| `judge` | keyword + judge | **judge** (an ungraded verdict counts as a fail) |
| `both` | keyword + judge, plus a disagreement section | keyword (the report says so) |

```bash
RUN_RELIABILITY=1 RELIABILITY_GRADER=both .venv/bin/python3 tests/test_reliability.py
```

- **Judge model:** `RELIABILITY_JUDGE_MODEL` (alias or full ID), default
  `opus`. It does not fall back to `ANKUSDRIVE_EVAL_MODEL`, so the model under
  test never grades its own answers unless you ask for that.
- **What the judge sees:** the question asked, the shape's ground-truth
  `description`, the keyword spec (as a hint about which features matter, not
  a required word list), and the answer. It does not see the image, so a
  judgement is a short text-only call. The prompt is in
  `tests/reliability_judge.py` (`JUDGE_SYSTEM`).
- **Structured verdict:** the call uses structured outputs
  (`output_config.format` with a `{pass: bool, reason: str}` JSON schema), so
  the verdict is parsed with `json.loads`, with no free-text matching.
- **Ungraded results:** a `refusal` or `max_tokens` stop, or output that
  doesn't match the schema, is recorded as `status: "ungraded"` with
  `pass: null`. It is never a pass and never a disagreement. The report counts
  ungraded results separately.
- **`both` mode** lists every shape where the judge and the keyword grader
  differ, with the judge's reason and the keyword grader's reasons. A
  judge-pass/keyword-fail row usually means a keyword spec needs a synonym or
  a narrower anti-keyword. A judge-fail/keyword-pass row is worth reading: the
  answer matched a word without describing the part.
- **Cost:** one extra text call per shape on a verdict-cache miss.

Offline coverage: `tests/test_reliability_judge.py` (fake client; runs in the
fast-checks lane). Layers B and C still use their own keyword /
MATCHES-VIOLATES parsers.

---

## Cache mechanics

`tests/reliability_cache/` (gitignored):

```
cube_iso.png                    ← Layer A: single-shape render
cube_iso.<model>.txt            ← Layer A: model response
cube_iso.<model>.judge.<judge>.json ← Layer A: judge verdict (judge/both modes)
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
- **Judge cache** is keyed by the answered model and the judge model. A
  cached verdict is reused only while the answer text, the shape's
  `description` and the judge prompt (`JUDGE_PROMPT_VERSION` plus the prompt
  itself) are unchanged, so editing any of them re-judges automatically.
  Editing a keyword list does not, because the judge only uses it as a hint and
  fixing lists after a `both` run should stay free. Ungraded verdicts are
  not cached, so the next run retries them.

This is intentional: the cost of running the suite is in the API call, not
the render. Caching them separately means you can refine the grader on real
responses without paying again.

---

## When to run

- After any change to `ankusdrive/render.py` or `ankusdrive/worker.py::_h_tessellate`.
- After upgrading the model in the harness (top of `test_reliability.py`).
- Before declaring "agent-eyes works" for any new shape category — add the
  shape to the library, run the suite, watch what Claude actually says.

Don't run it on every commit. Run it on every renderer change.

### Scheduled run (#517)

`.github/workflows/paid-evals.yml` runs Layers A-D, the tool-selection eval and a
fixed M2 panel every Monday at 09:00 UTC on the current Haiku, and on demand from
the Actions tab (`workflow_dispatch` takes the suites, the model, the M2 toys and
the M2 trial count). It needs an `ANTHROPIC_API_KEY` repository secret. A free
preflight probes the key against the models endpoint and stops the run before any
paid call if the key is missing or rejected.

Each job runs on a fresh runner, so every answer is a new API call and nothing is
graded from a stale cache. Layer A is graded by the judge
(`RELIABILITY_GRADER=judge`, judge on Opus). The M2 job runs `M2_SELFTEST` on its
panel first and stops if a gate no longer catches its scripted negatives.

`scripts/paid_eval_report.py` posts each suite's pass rate and what it billed to
the run's summary, and it is the only gate:

- **Red** when a suite crashed before writing its report, or its pass rate fell
  below `tests/paid_eval_baseline.json` by more than the allowed drop:
  `max(margin, 2 × standard error of the baseline rate)`. The default margin is
  0.15. An entry can set its own. The standard-error term keeps ordinary sampling
  noise on the 7- and 10-item layers from going red.
- **Not red** when a harness misses its own bar (Layer A's 70% and so on). The
  exit code is logged as a warning and the number is in the table.
- **No baseline yet**, a new model ID, or a different M2 panel: reported and
  green. Baselines are keyed by the full model ID, so a new Haiku generation starts
  fresh instead of being compared with the old one.

The summary ends with a baseline block built from that run. To start comparing,
paste it under `suites` in `tests/paid_eval_baseline.json` once the numbers look
right.

**Cost.** Estimated at list prices (`orchestration/claude_models.PRICING`) for
Haiku 5.5. It thinks by default, so output is assumed at 2-3× the Haiku 4.5
rounds' token counts:

| Suite | Calls per run | Estimate |
|---|---|---|
| Layers A-C | 27 single-image calls | under $0.02 |
| Layer A judge (Opus) | 10 short text calls | ~$0.10-0.20 |
| Layer D | 4 tasks × 5 trials, cached tool block | ~$0.05-0.10 |
| Tool selection (`bundle`) | 63 cases, cached tool blocks | ~$0.10 |
| M2 panel | 9 toys × 2 conditions × 5 trials = 90 agent runs | ~$0.90-1.50 |
| **Total** | | **~$1-3 a week** |

The M2 figure scales the billed Haiku 4.5 rounds in
[MULTI_AGENT_EVAL.md](MULTI_AGENT_EVAL.md) ($0.03-0.08 a trial on these toys) to
a tenth of the price. Every run's summary has the real figure, so check the first
one against this table. A manual run on `sonnet` costs about 20× as much, and on
`opus` about 40×.

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
model call AnkusDrive tools. Layer D tests the other half: *can the model DRIVE the
tools to produce geometry that meets a goal.* The agent gets an English design
goal and the AnkusDrive tool surface, runs a real tool-use loop against a real
FreeCAD worker (via `orchestration/agentkit.py`), then the resulting SOLID is
graded with AnkusDrive's own Tier-3 inspection tools (`bounding_box` /
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

**Changing the grader:** set `RELIABILITY_GRADER` (see "LLM judge" above).

**Changing the prompt:** edit `PROMPT` at the top of `test_reliability.py`.
This invalidates all cached `.txt` files (since the question changed); delete
them or accept that the grader is now scoring stale responses.

**Changing the model:** edit `MODEL`. Like the prompt, this should clear the
text cache.

**Lowering the accuracy bar:** edit `bar = 0.70` near the bottom of `main()`.
Don't lower it casually — the bar is the only thing forcing renderer
improvements when accuracy regresses.
