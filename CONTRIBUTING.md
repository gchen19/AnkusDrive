# Contributing to AnkusDrive

Thanks for looking. Two things about this project are unusual enough that they are
the reason this file exists; everything else is a link to the README.

1. **FreeCAD is a prerequisite you install by hand.** There is no pip dependency
   that gives you one. The reflex `pip install -e '.[dev]' && pytest` will not
   work here, and the failure is confusing rather than informative.
2. **CI is five lanes, three of them on self-hosted machines.** A pull request from
   a fork will see checks on `main` that never appear on the PR. That is deliberate
   and is explained below, so you do not have to wonder whether something is broken.

If you only want to file a bug or ask a question, none of this applies — open an
issue. For a **security** problem, do not open an issue: see
[`SECURITY.md`](SECURITY.md).

By contributing you agree that your contribution is licensed under Apache-2.0, per
section 5 of [`LICENSE`](LICENSE). There is no CLA and no DCO sign-off. The
AnkusDrive word mark is separate from the code licence — see
[`TRADEMARKS.md`](TRADEMARKS.md) if you plan to fork and rename.

## Setting up a machine that can run the suite

Install FreeCAD 1.1.x and AnkusDrive as the [README's Setup
section](README.md#setup) describes — that part is the same for contributors as for
users. Then the two contributor-specific steps:

```bash
git clone https://github.com/gchen19/AnkusDrive.git && cd AnkusDrive
python3 -m venv .venv && .venv/bin/pip install -e .
pip install "ruff==0.15.15"          # same version CI pins

bash tests/setup_local.sh            # wires this machine for the test suite
bash tests/run_all.sh                # the whole thing
```

`tests/setup_local.sh` is idempotent and downloads nothing. It reuses a FreeCAD
AppImage you already have (or `$FREECAD_HOME` / `$ANKUSDRIVE_APPIMAGE` if you point
it at one), symlinks `freecadcmd` onto `PATH`, and points `.venv/bin/python3` at
FreeCAD's *bundled* Python — which already ships numpy and Pillow.

### The two-interpreter split — read this before running pytest directly

`tests/run_all.sh` is not a thin wrapper around `pytest`. It routes each test file
to one of two interpreters:

- **system `python3`** for tests that load FreeCAD (worker, integration, geometry),
  and for the static contract tests, which import nothing and need no dependencies;
- **`.venv/bin/python3`** for tests that need Pillow and numpy (render, some
  integration).

Running `pytest tests/` yourself will collect both halves under one interpreter and
produce a mess of import errors that look like real failures. Run `run_all.sh`, or
run a single file under the interpreter that file needs. Tests that cannot run under
the interpreter they got are written to **SKIP cleanly** rather than fail, so a
clean run with skips is a normal result on a partially provisioned machine.

### Gated suites

Two things do not run by default, and you should leave them that way unless you are
specifically working on them:

- `RUN_HEAVY_SOLVES=1` enables the live external-solver cases — OpenFOAM, Elmer,
  openEMS, Bempp, YADE, preCICE↔CalculiX, CalculiX warpage. Minutes each, a few
  flaky under load. The cheap analytic-oracle and case-structure halves run either
  way.
- `RUN_RELIABILITY=1` enables the reliability suites, which **call a paid LLM API**.
  Do not set it casually. See `tests/RELIABILITY.md`. The Layer D harness
  *validator* runs unconditionally and needs no API key, so a broken harness fails
  before any billable run.

## What CI runs, and how to reproduce each lane locally

| Check | Where it runs | Reproduce locally |
|---|---|---|
| `Lint + contracts (no FreeCAD)` | hosted | `ruff check ankusdrive tests`, `python3 -m compileall -q ankusdrive tests`, then `python3 tests/test_contracts.py`, `tests/test_naming.py`, `tests/test_compat_rename.py`, `tests/test_package_data.py` |
| `Secret scan (git history)` | hosted | `gitleaks git --log-opts="--all" --config .gitleaks.toml --redact --exit-code 1` |
| `Worker suite (hosted FreeCAD)` | hosted | `bash tests/run_all.sh` (the FreeCAD-side subset) |
| `Test suite (FreeCAD 1.1, self-hosted)` | self-hosted Linux | `bash tests/run_all.sh` |
| `Test suite (FreeCAD 1.1, self-hosted Windows)` | self-hosted Windows | the suite on a Windows box with FreeCAD 1.1 |
| `Live external-solver regressions` | self-hosted | `RUN_HEAVY_SOLVES=1 bash tests/run_all.sh` |

The first three are hosted, always available, and are the ones branch protection
requires. Nothing that runs on a machine in someone's house can block a merge.

### Why your fork's PR sees fewer checks

Two separate mechanisms, and it is worth knowing which is which:

- **`Tests` and `Tests (Windows)` skip fork PRs on purpose.** They run on
  self-hosted runners — real machines with a home directory, keys, and a network —
  and a `pull_request` workflow runs the *fork's* code. Letting an arbitrary PR
  reach them is arbitrary code execution. Fork PRs get their FreeCAD signal from
  the hosted lane instead, which covers the same worker tests.
- **`Heavy solver regressions` is not on `pull_request` for anyone**, fork or not.
  Those solves take minutes and a couple are flaky under load, so gating merges on
  them would re-introduce exactly the flakiness the split removed. They run on
  pushes to `main`, nightly, and on demand via `workflow_dispatch`.

So: fewer checks on a fork PR is not a judgement about you. If a change needs the
self-hosted or heavy lanes to be believed, say so in the PR and the maintainer will
run them.

## Adding a tool

This is the part you cannot guess from reading one file, so here it is explicitly.

Every capability is registered **more than once, by hand**, and the registries are
authored independently. `tests/test_contracts.py` enforces the whole contract by
static parsing — it imports neither `worker.py` nor `mcp_server.py`, so it needs no
FreeCAD and runs in seconds. Miss one of these and a test fails, usually far from
the code you wrote.

1. **The pure core** — `ankusdrive/<name>.py`, or `ankusdrive/analysis/<name>.py` for
   an analysis family. FreeCAD-free, plain stdlib import. *All* the arithmetic lives
   here, so it unit-tests in milliseconds without a CAD kernel. The worker layer
   reads geometry and delegates every decision down to this.
2. **`ankusdrive/worker.py`** — the `@handler("your_tool")` that runs inside
   FreeCAD's interpreter.
3. **`ankusdrive/mcp_server.py`** — the `@mcp.tool()` wrapper, **same name**,
   dispatching via `_call("your_tool", ...)`. A copy-pasted `_call` target is the
   classic silent break and is a test failure here. The docstring is the only spec
   the calling agent ever sees: it needs a real summary line and it **must describe
   what the tool returns** — name the keys. There is a grandfather list for legacy
   tools; it is closed to new entries.
4. **`tests/determinism_registry.py`** — classify it, or
   `test_every_tool_has_a_determinism_class` trips. A kwargs-only closed-form tool
   goes in `EXACT_TOOLS` **and** gets an `ANALYSIS_SWEEP` entry (that is the bitwise
   sweep). A solver or `*_submit` result goes in `BOUNDED_TOOLS` with a documented
   envelope. Geometry and page-reading tools go in `NOT_YET_CLASSIFIED` with a
   comment — a last resort, and a ratchet: names come off it, not onto it.
5. **`tests/run_all.sh` *and* `tests/run_all.ps1`** — both. Adding only the first
   makes the Windows CI lane silently skip your suite.
6. **The README capability table** — the domain table under *Layer 1 — typed MCP
   tools*. One row per family. It is how anyone finds out the tool exists.
7. **`tests/test_worker.py`** — any `add_*`-prefixed tool must appear in
   `_producer_specs` or in `_ADD_NOT_SOLID`, or
   `test_every_add_command_has_a_producer_smoke` fails. Anything in
   `_producer_specs` is then asserted to come back as a watertight solid.

Three more that apply when they apply:

- **Every optional parameter of the underlying analysis function must be exposed**
  by the wrapper, or the divergence must be listed with a written reason in the
  wrapper-drift allowlist. A silently swallowed parameter is a capability the agent
  cannot reach.
- **`escalate_to` targets** must be registered tools, and the docstring must name
  the escalation — for a screen that hands off to a heavier analysis.
- **Package data**, if the tool loads a corpus (materials, standards, rheology, an
  FSI case template). `tests/test_package_data.py` builds a real wheel and sdist and
  re-derives the expected file list *from the loaders themselves*, so an omission
  fails in CI rather than at a pip-installed user's first call.

**Conventions worth copying rather than re-deriving.** Tests here are standalone
scripts, not pytest: `_check(label, got, want)` / `_ok(label, cond, detail)` helpers
printing PASS/FAIL, and a `main()` that lists every test function and returns 1 on
failure. Test both sides — a case that must pass and a case that must fail. Every
estimator returns a `fidelity` of `"exact"` or `"correlation"`, and for a
correlation an honest `band_pct`; say in the docstring which part is exact
arithmetic and which part is convention. `ankusdrive/inspection.py` with
`tests/test_inspection.py` is the closest thing to a canonical template.

## Pull requests

- **One issue per PR.** Reference it: `Fixes #123`.
- **Subject line style** follows what is already in the log:
  `scope: what changed (#123)`. It is not Conventional Commits and does not need to
  be. Write the *effect*, not the mechanism — `boolean_op: name the two degenerate
  cuts instead of reporting a volume` rather than `fix boolean_op`.
- **`main` is squash-merged and linear.** Force-push and deletion are blocked. Your
  PR title becomes the commit subject, so it is worth writing.
- **Add a `CHANGELOG.md` entry** under `## [Unreleased]` for anything a user would
  notice. Skip it for pure refactors and CI plumbing that changes no behaviour.
- **Explain the failure, not just the fix.** The commit log here is written on the
  assumption that the next person needs to know what was actually wrong. A PR body
  that names the failure mode is worth more than one that lists the diff.

Before pushing, the fast lane takes seconds and catches most of what CI would:

```bash
ruff check ankusdrive tests && python3 tests/test_contracts.py
```

## Releasing

For the maintainer, recorded here so it is not folklore:

1. Bump `__version__` in `ankusdrive/__init__.py` — the single source; `pyproject.toml`
   reads it dynamically. Four copies must follow it in the same PR, and the fast lane
   fails the PR if any lags: both `version` fields in `server.json`
   (`tests/test_mcp_registry.py`), and `version` in `mcpb/manifest.json` plus the
   `version` and `ankusdrive==` pin in `mcpb/pyproject.toml` (`tests/test_mcpb_bundle.py`).
2. Move `## [Unreleased]` in `CHANGELOG.md` to the new version with today's date,
   and add the compare link at the bottom.
3. Tag `vX.Y.Z` and push the tag. That triggers `publish.yml`, which verifies the
   tag matches the version and uploads to PyPI by OIDC trusted publishing — there is
   no API token to leak — then publishes `server.json` to the MCP Registry and builds
   `ankusdrive-X.Y.Z.mcpb`. `workflow_dispatch` with `target=testpypi` dry-runs the
   PyPI half.
4. The GitHub release is created by that same run, with the changelog section as its
   notes and the `.mcpb` (plus its `.sha256`) attached. The changelog is the source; the
   release page is a copy of it, not a second draft. The run titles it `X.Y.Z` — add
   the descriptive half by hand. A release you create before the run finishes keeps
   its notes; the run only attaches the bundle.
5. Publish to Smithery (`ankusdrive/ankusdrive`), by hand — it needs a logged-in
   Smithery CLI. Until Smithery accepts MCPB `server.type: "uv"`
   ([smithery-ai/cli#801](https://github.com/smithery-ai/cli/issues/801)), publish a
   derived copy of the release bundle, not the bundle itself:
   ```
   gh release download vX.Y.Z -p 'ankusdrive-X.Y.Z.mcpb' -D dist
   scripts/smithery_mcpb.py dist/ankusdrive-X.Y.Z.mcpb
   npx --yes @smithery/cli@4.11.1 mcp publish dist/ankusdrive-X.Y.Z-smithery.mcpb -n ankusdrive/ankusdrive
   ```
   The script explains the two fields it changes and why; when #801 ships, delete it
   and `tests/test_smithery_mcpb.py` and publish the release bundle directly.

## Code of conduct

Participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
