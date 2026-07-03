# Publishing plan: from clone-and-edit to MCP marketplace

Goal: turn DriftPin into something a stranger can install and wire into
Claude Desktop / Claude Code / Cursor without reading the source. Today
setup requires a clone, a venv, and editing a hardcoded path in
`driftpin/client.py`. Marketplace conversion dies on any of those.

The phases below are sequenced by payoff-per-effort. Each is independently
shippable — stop after any phase if the next one isn't worth the cost.

---

## Phase A — Pip-installable from a clone

**Scope:** make `pip install -e .` (or `pipx install .`) work end-to-end.

**Deliverables**
- [x] `pyproject.toml` at repo root (PEP 621, setuptools backend, declares
  `mcp` / `Pillow` / `numpy` deps and the `driftpin` console script).
- [x] `DRIFTPIN_FREECADCMD` env-var override in `driftpin/client.py`, with
  `shutil.which` auto-discovery and a small list of cross-platform default
  paths (macOS app bundle, `/usr/bin`, `/usr/local/bin`, snap).
- [x] Smoke-test on a fresh clone (macOS 2026-05-11, FreeCAD 1.1.1):
  ```
  python3 -m venv .venv
  .venv/bin/pip install -e .
  .venv/bin/driftpin ping  →  ping=pong freecad=1.1.1
  ```
  Console-script entry point resolves; worker boots; FreeCAD reachable.
  Also validated `python -m build` → wheel → install-into-clean-venv path:
  `driftpin/worker.py` ships inside the wheel via `package-data`, and
  `driftpin ping` works from the wheel-installed location too.
- [x] Smoke-test on Linux (CI runs the editable install + import on Linux
  daily — `nightly-hosted-freecad.yml` is green; `freecadcmd` auto-discovery
  resolves the system binary via `shutil.which`).

**Success criteria**
- `pipx install -e .` from the repo root produces a working `driftpin`
  binary on `$PATH`.
- An MCP host pointed at that binary (`command: "driftpin"`,
  `args: ["mcp"]`) sees all ~72 tools.
- No edits to repo files required for non-default FreeCAD installs —
  setting `DRIFTPIN_FREECADCMD` is enough.

**Open questions**
- ~~Should `worker.py` be importable as data inside the package, or remain
  a script invoked by path?~~ Resolved 2026-05-11: current `Path(__file__)
  / worker.py` works in BOTH editable and wheel installs;
  `[tool.setuptools.package-data]` includes `worker.py` in the wheel.
- ~~Pin floor versions for `mcp` / `Pillow` / `numpy`?~~ Resolved 2026-07-02:
  floors are now the oldest verified-working versions (`pyproject.toml` carries
  the rationale). `mcp>=1.2` — `mcp.server.fastmcp.FastMCP` (used by
  `driftpin/mcp_server.py`) first shipped in 1.2.0; 1.0/1.1 lack the module
  (confirmed by installing each into a throwaway venv). `Pillow>=10.0` and
  `numpy>=1.24` are conservative floors — the code uses only long-stable APIs
  (`Image.{fromarray,new,open,convert}`, basic ndarray/dtype), so these bounds
  are safe rather than empirically minimal.

---

## Phase B — PyPI release

**Scope:** ship a wheel that anyone can `pipx install driftpin` without
cloning.

**Deliverables**
- [ ] **(human)** Reserve the `driftpin` name on PyPI + TestPyPI (register
  account). No placeholder upload needed — trusted publishing claims the name
  on the first real upload.
- [ ] **(human)** Configure the PyPI/TestPyPI trusted-publisher binding
  (owner `gchen19`, repo `DriftPin`, workflow `publish.yml`, environments
  `pypi` / `testpypi`) and create the matching GitHub environments. See the
  header comment in `.github/workflows/publish.yml`.
- [x] CI workflow (`.github/workflows/publish.yml`) that runs on git tag
  (`v*`), builds with `python -m build`, verifies the tag matches
  `driftpin/__init__.py` `__version__`, `twine check --strict`s the metadata,
  smoke-installs the wheel, and uploads via OIDC trusted publishing
  (`pypa/gh-action-pypi-publish`) — no stored token. `workflow_dispatch`
  supports a TestPyPI dry-run.
- [ ] **(human)** First real release: tag `v0.4.0` (matches
  `driftpin/__init__.py`), push it, verify the wheel installs cleanly in a
  fresh venv on macOS and Linux.
- [x] Update README setup section: `pipx install driftpin` is documented as
  the primary path, marked "on release", with the `git+https` and clone paths
  retained as the interim/contributor routes.

**Success criteria**
- `pipx install driftpin` on a clean machine (with FreeCAD already
  installed) produces a working `driftpin` binary in <30s.
- The MCP host config block in the README references the pipx-installed
  binary (`/Users/<user>/.local/bin/driftpin` or just `driftpin` if PATH is
  set), not a venv-relative path.

**Risks**
- Name squatting: confirm `driftpin` is free on PyPI before announcing
  anywhere. Fallback names: `driftpin-mcp`, `freecad-driftpin`.
- FreeCAD versioning: if 1.2 ships during this phase and breaks the API
  surface, hold the release and fix forward.

---

## Phase C — Smithery listing

**Scope:** show up in the most-trafficked third-party MCP marketplace,
with a one-click install button.

**Deliverables**
- [x] `smithery.yaml` at repo root declaring:
  - the `driftpin mcp` start command (stdio),
  - the one optional user-supplied env var (`DRIFTPIN_FREECADCMD`, with a
    note that it's only needed on non-default installs),
  - a short description and the supported MCP host platforms.
- [ ] **(human)** Submit at smithery.ai (their flow scans the repo and renders
  the install button automatically once the yaml is detected). Requires the
  PyPI release (Phase B) to be live first.
- [ ] **(human)** Verify the install button writes a correct config block into
  Claude Desktop, Cursor, and Claude Code on a fresh machine.

**Success criteria**
- The Smithery install button works without manual config edits for the
  default-FreeCAD-path case.
- The yaml schema correctly prompts for `DRIFTPIN_FREECADCMD` when the
  user opts into a custom path.

**Dependencies**
- Phase B must be complete — Smithery's install template assumes a
  pip-installable command, not a clone-based setup.

---

## Phase D — Official MCP servers list

**Scope:** PR DriftPin into `github.com/modelcontextprotocol/servers`'s
community section. Lowest effort, highest trust signal.

**Deliverables**
- [ ] **(human)** Branch on a fork of `modelcontextprotocol/servers`, add a
  one-line entry under "Community Servers" pointing to the DriftPin repo.
- [ ] **(human)** PR description: 2–3 sentences on what it does, link to
  install docs, mention FreeCAD as the upstream dependency.
- [ ] **(human)** Respond to maintainer feedback (typical asks: clearer README
  intro, evidence the server actually works, license confirmation — all
  already satisfied).

**Success criteria**
- PR merged. Aggregator sites (mcp.so, pulsemcp, glama.ai) typically
  scrape this list within a week, so no further submission work is
  needed for those.

**Dependencies**
- Phases A + B should be merged before submission so the README's install
  flow is `pipx install driftpin`, not a clone walkthrough. Maintainers
  push back on the latter.

---

## What we are explicitly not doing

- **Hosted/cloud MCP server**: DriftPin needs a local FreeCAD install to
  run. A SaaS version would mean shipping FreeCAD in a container and
  exposing it over the network — a different product, not a distribution
  channel.
- **Bundling FreeCAD**: out of scope. Users install FreeCAD themselves;
  DriftPin only wraps it.
- **Windows support hardening**: the env-var override in Phase A makes
  Windows technically possible, but the test loop is macOS + Linux until
  someone with a Windows machine takes ownership.

---

## Sequencing summary

```
Phase A (pyproject + env var) ──► Phase B (PyPI release)
                                       │
                                       ├──► Phase C (Smithery)
                                       │
                                       └──► Phase D (official list PR)
```

Phases C and D are independent of each other once B is done; do them in
parallel.
