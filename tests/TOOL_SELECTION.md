# Tool selection eval (#508)

A client model picks an AnkusDrive tool from its name and description alone. With
280+ tools, several of them look alike: `fem_modal` / `beam_modal` /
`fem_modal_results`, `section_view` / `add_section_view`, `render_view` /
`render_photoreal`, and every `*_screen` / `*_submit` pair.
`test_tool_annotations.py` checks that annotations exist and
`test_toolsets_budget.py` checks the list's size. Neither checks whether a
description tells two tools apart. This eval does.

- Corpus: `tests/tool_selection_cases.py`. `CLUSTERS` groups the tools that are
  easy to confuse. `CASES` holds about 60 plain-language requests, each with the
  tool(s) that count as a correct first call. The `controls` cluster holds
  unambiguous requests.
- Harness: `tests/test_tool_selection.py`.

## Free mode (default; runs in fast-checks)

```bash
python3 tests/test_tool_selection.py
```

No FreeCAD, no API key, under a second. It checks:

- The corpus is well formed. Ids are unique, each `accept` tool belongs to its
  cluster, and no request names its answer.
- Every tool the corpus names is a registered `@mcp.tool()` in exactly one
  toolset family. This parses `mcp_server.py` with `ast` and doesn't import it.
- No tool description is empty or shorter than 40 characters.
- Every case's accepted tools are in the toolset the paid run would expose.
- The paid path runs offline against a fake client. The checks cover the request
  shape (tools sent as `name`/`description`/`input_schema` only, in `tools/list`
  order, `cache_control` on the last tool only, `tool_choice: auto`), grading,
  confusion counting, cost math, and response-cache keying.
- When `mcp` is importable (locally, via `run_all.sh`), the same checks run against
  the **live** `tools/list` served over stdio. This tier also prints the size of
  each tool block the paid run will send.

## Paid mode

```bash
ANTHROPIC_API_KEY=sk-... RUN_RELIABILITY=1 .venv/bin/python3 tests/test_tool_selection.py
```

Each case makes one `messages.create` call with these inputs:

- the server's real `tools/list` schemas;
- a short system prompt that frames a CAD assistant and says the named objects
  exist;
- the request as the user message;
- `tool_choice: auto` and `max_tokens` 4096. Current models think before they
  call a tool.

The grade is the **first `tool_use` block's name**. It is `pass` if that name is in
`accept` and `wrong` otherwise. `no_tool` means the model answered in text.
`refusal` and `max_tokens` stop reasons are recorded as their own outcomes.

| Env | Meaning |
|---|---|
| `TOOL_SELECTION_MODEL` | alias (`opus`/`sonnet`/`haiku`) or full ID; default `sonnet` (via `orchestration/claude_models.resolve_model`) |
| `TOOL_SELECTION_TOOLSETS` | `bundle` (default) or `all`. See below |
| `TOOL_SELECTION_CASES` | comma list of case ids and/or cluster names |
| `TOOL_SELECTION_MIN` | exit 1 if overall accuracy is below this (e.g. `0.8`) |
| `TOOL_SELECTION_REFRESH=1` | ignore cached responses |

### Which tools the model sees

The default, `bundle`, sends the Claude Desktop extension's default families
(`toolsets.BUNDLE_DEFAULT`: core, drawings, fem) **plus the family of every tool in
the case's cluster**. This is the lean surface a directory user actually gets, and
adding the cluster's families means every confusable member is on screen together.
`beam_modal` (hand_calcs) is therefore visible next to `fem_modal` (fem) even when
the case expects `fem_modal`. Without that, a confusion could never show up. Each
family set is the server's `tools/list` filtered by family, which is what the
server serves with only those families enabled. `test_toolsets_budget.py` asserts
that equality for the bundle default.

`all` sends every family. That is what a pip/uvx/clone install serves by default,
and the hardest case: every distractor from every family is present. Run both
before and after a description rewrite. `bundle` matches most users. `all` is the
worst case.

### Cost

The tool block is large: 115–208 tools, about 31k–86k tokens per family set in
`bundle` mode, and about 128k tokens in `all`. The last tool carries
`cache_control: {"type": "ephemeral"}`, so the block is written to the prompt
cache once per distinct family set and read back by every later case that uses it.
Cases that share a family set run back to back, so they land inside the 5-minute
cache window.

Rough cost of a fresh full run (63 cases) at list prices, with about 1k
output/thinking tokens per case:

| Model | `bundle` (10 tool blocks) | `all` (1 tool block) |
|---|---|---|
| Sonnet 5.5 | ~$2.2 | ~$1.8 |
| Haiku 5.5 | ~$0.1 | ~$0.1 |
| Opus 5.5 | ~$4.5 | ~$3.5 |

Without caching, `bundle` on Sonnet would cost about 3–4× as much. The report
prints both numbers. A rerun with unchanged descriptions is free, because
responses are cached on disk.

### Response cache

Responses are saved under `tests/tool_selection_cache/` (gitignored) as
`<case>.<hash>.<model>.json`. The hash covers the whole request: model, system
prompt, user message, and every tool schema in the block. Editing a description
therefore re-queries exactly the clusters whose block contains that tool. Changing
a tool outside a cluster's families leaves that cluster cached. A run where every
response is already cached never constructs an API client.

## Reading the report

The run prints the following, and writes the same data to
`tests/tool_selection_cache/report.<toolsets>.<model>.json`:

- overall accuracy and the outcome counts;
- per-cluster accuracy, worst first;
- a **confusion list**: `asked for X -> picked Y  xN  [case ids]`;
- token usage, cost via `orchestration/claude_models.cost_of`, the cost without
  prompt caching, the saving, and what this run actually billed.

## Using the confusion list to rewrite descriptions

1. Start with the confusion row that has the highest count. `fem_modal -> beam_modal x2`
   means `fem_modal`'s description fails to say when it is the right choice. It can
   also mean `beam_modal`'s description claims too much.
2. Read both descriptions side by side, as the model sees them. Find the
   distinguishing fact the user's request carried, such as "no meshing", "already
   solved", "on the drawing" or "don't block". Make that fact appear **early** in
   the right tool's description. Add a "use X instead when …" line to the wrong
   one. The `beam_buckling` → `fem_buckling` cross-reference ("the closed-form twin
   … as `beam_modal` is to `fem_modal`") is the pattern.
3. `<no_tool>` rows mean the model answered in text or asked a question. The text
   is in the report's `cases[].text`. That usually points at the request, not the
   description: the request was ambiguous, so rewrite the case.
4. A miss in `controls` means the harness or the system prompt is wrong. Fix that
   before you trust any other number.
5. Rerun. Only the clusters whose tool block changed are re-queried. Check that
   `test_toolsets_budget.py` still passes, because longer descriptions spend the
   bundle's token budget.
6. When a new confusable pair turns up, add a cluster and two or more cases to the
   corpus. Write each request as a user would. Don't use the tool's name or copy
   its description's wording.
