# Command backlog — parallel dispatch packets

This directory drafts the next wave of MCP commands (Tiers 1–3 from the design
discussion) as **self-contained task packets**, one per command, so each can be
handed to a separate agent with no shared context.

- [`tier1-components.md`](tier1-components.md) — parametric standard-component
  generators (the `add_gear` family).
- [`tier2-features.md`](tier2-features.md) — everyday feature ops that currently
  force `run_script`.
- [`tier3-inspection.md`](tier3-inspection.md) — measurement & inspection (high
  value because the agent is blind).

Read this file first — it is the shared contract every packet assumes.

---

## The pattern every command follows (`add_gear` is the reference)

Two edits + one test, mirroring `worker.py:_h_add_gear` / `mcp_server.py:add_gear`:

1. **Worker handler** in `driftpin/worker.py`:
   ```python
   @handler("my_command")
   def _h_my_command(p):
       """One-paragraph docstring: what it does, units, what it returns."""
       doc = _active_doc()                 # or: App.ActiveDocument + None-check
       obj, shape = _shape_of(p["handle"]) # resolve inputs
       size = float(p.get("size", 1.0))    # coerce + default every param
       ...                                 # FreeCAD work
       doc.recompute()                     # after every geometry mutation
       h = _register("mycmd", out_obj)     # geometry producers only
       return {"handle": h, "name": out_obj.Name, "volume": out_obj.Shape.Volume}
   ```

2. **MCP tool** in `driftpin/mcp_server.py` — a thin marshaller, no logic:
   ```python
   @mcp.tool()
   def my_command(handle: str, size: float = 1.0, name: str = "MyCmd") -> dict:
       """Same docstring, written for the calling agent. State units + the
       return keys explicitly — this text is the only spec the LLM sees."""
       params = {"handle": handle, "size": size, "name": name}
       return _call("my_command", **params)
   ```
   Only forward optional/None params when set (see `add_gear`'s `placement`).

3. **Test** appended to `tests/test_worker.py` — auto-discovered by name:
   ```python
   def test_my_command():
       with Worker() as w:
           w.call("new_document", name="t")
           ...
           r = w.call("my_command", handle=..., size=2.0)
           assert r["handle"].startswith("mycmd_"), r
           assert r["volume"] > 0, r
   ```
   Run just the worker suite with `python3 tests/test_worker.py`.

### Conventions (non-negotiable, enforced in review)
- **Units:** mm, degrees, kg, kg/mm³. Always. State them in the docstring.
- **Validate up front:** coerce types, bounds-check, raise `ValueError` /
  `RuntimeError` with a message an agent can act on (`"teeth must be >= 3"`).
- **`doc.recompute()`** after each geometry change.
- **Geometry producers** register a handle (`_register`) and return
  `{handle, name, ...}` plus any *mating/reference dimensions* a coordinator
  needs to place neighbors — this is what made `add_gear` usable (it returns
  `pitch_radius`/`tip_radius`). Inspection/query commands return measured data,
  no handle.
- **Hide consumed inputs** with `_set_visibility(input_obj, False)` when your
  command turns one solid into another (see `boolean_op`).
- **Reference geometry by tag**, not raw index: accept `e_*`/`f_*` tags and
  resolve via `_h_resolve_edge` / `_h_resolve_face` (mirror `fillet_edges`,
  worker.py:493). Bare `EdgeN`/`FaceN` strings and ints are an accepted fallback.

### Helpers already in `worker.py` (don't reinvent)
| Helper | Purpose |
|---|---|
| `_active_doc()` | active doc, auto-picks if one open, raises if ambiguous |
| `_register(prefix, obj)` → handle | register result object, returns `"prefix_N"` |
| `_resolve(h)` → obj | handle → FreeCAD object |
| `_shape_of(h)` → (obj, shape) | handle → object + non-null `Shape` |
| `_solid_of(shape)` → solid | first/closed solid of a shape (see `mass_properties`) |
| `_resolve_body(h)` / `_resolve_plane_ref(body, ref)` | PartDesign body + 'XY'/datum plane |
| `_h_resolve_face` / `_h_resolve_edge` | tag/index → sub-shape descriptor |
| `_outward_normal(face)` | orientation-corrected unit normal |
| `_set_visibility(obj, bool)` | persistent Visibility flag |
| `HANDLERS["name"](params)` | call another handler in-process (see `merge_assembly`) |
In scope at module level: `App` (FreeCAD), `Part`, `ObjectsFem`, `Sketcher`.

---

## Parallelization & zero-conflict assembly

Every command edits the **same two files** (`worker.py`, `mcp_server.py`), so
naive parallel appends collide. Two supported modes:

### Mode A — return-then-assemble (recommended for a Workflow run)
Each agent **does not edit the shared files**. It reads the repo, then returns a
structured payload:
```json
{ "command": "...", "handler_code": "<full @handler block>",
  "tool_code": "<full @mcp.tool block>", "test_code": "<full def test_*>",
  "anchor_handler": "<existing handler name to insert after>", "notes": "..." }
```
A single assembler (the orchestrator) splices the blocks in at each anchor and
runs the suite. One writer ⇒ no merge conflicts; agents parallelize the
expensive part (FreeCAD API research + drafting).

### Mode B — worktree per command (independent agents/humans)
Each agent works in its own git worktree and inserts at the **distinct anchor**
named in its packet. Anchors are spread across the file on purpose, so git's
3-way merge auto-resolves disjoint insertions. The test file auto-discovers, so
appended `test_*` functions never collide. Add the MCP tool next to its sibling
in the same region. Merge order doesn't matter.

Each packet names its anchor. Stay within your packet's anchor — that is the
whole contract that keeps the merge clean.

### Definition of done (every packet)
- Handler + tool + test written, units & return keys documented in both docstrings.
- `python3 tests/test_worker.py` passes (your new test + no regressions).
- Docstring on the MCP tool is complete enough to use blind (it is the agent's
  only spec).
- If the command needed a non-obvious FreeCAD call, leave a one-line code comment
  explaining the *why* (match the surrounding comment density).
