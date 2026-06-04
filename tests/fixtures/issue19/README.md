# issue #19 golden fixtures — router → shop-vac adapter

Real FreeCAD source files for the enclosed-flow adapter from
[issue #19](https://github.com/gchen19/DriftPin/issues/19), provided by the
reporter **@mpetne** for use as regression fixtures. They are the actual models
the LLM agent produced — not reductions — so they exercise the real geometry
(curved shells, oblique cuts, snap features) that the synthetic fixtures in
`tests/test_worker.py` only approximate.

Inbound contributions to this repo are Apache-2.0 (LICENSE §5); these files are
included with that understanding and credited to their author.

## Provenance

Downloaded from the issue gist
`https://gist.github.com/mpetne/16dc4d2119ce41a24faf3d2c449ee79d`. To re-fetch:

```bash
gid=16dc4d2119ce41a24faf3d2c449ee79d
for v in 1 2 3; do
  url=$(gh api gists/$gid --jq ".files[\"router_vac_adapter_v${v}.FCStd\"].raw_url")
  curl -sSL "$url" -o "router_vac_adapter_v${v}.FCStd"
done
```

## What each file is (verified in a FreeCAD 1.1 worker)

| File | "The part" (top-level result object) | Notes |
|------|--------------------------------------|-------|
| `router_vac_adapter_v1.FCStd` | `Adapter` — 54.6 cm³, 17 faces | flat-plug design; can't seat on the curved housing (v1 defect) |
| `router_vac_adapter_v2.FCStd` | `AdapterV2` — 162.9 cm³, 8 faces, 1 shell | curved-shell design; the "almond-slit" cavity (v2 defect) |
| `router_vac_adapter_v3.FCStd` | `AdapterV2` (**byte-identical to v2**) | doorway work is NOT in the tip — see anomaly below |

Every file is a **48–68-object feature tree** with several visible top-level
solids; "the part" is not the first shaped object. Select it as the top-level
result with an empty `InList` (nothing consumes it) — same rule `add_part` uses.

## Key findings

1. **Watertight ≠ correct.** `check_shape` reports `valid=True, closed=True,
   1 solid` → `watertight_solid=True` for **all three**, including the broken
   v2/v3. This is the gap `check_airtight_path` closes.

2. **v3 anomaly.** The v3 doorway is *not* fused into the delivered `AdapterV2`
   tip (identical to v2's). It lives in separate, never-integrated objects
   (`Box004/005` + `Cut002/Cut003`, the ~200 KB BREPs present only in v3).
   `Cut002` even splits into **2 solids / 2 shells** — the reporter's "the cut
   disconnected the snap-tab stems" failure, captured concretely. So the
   *delivered* v3 part is geometrically the v2 part.

3. **Ports must be declared.** `AdapterV2` is a closed solid with no enclosed
   void and only 8 faces; there is no geometric way to know which face is the
   inlet window vs. the barb-tip outlet. Running `check_airtight_path` on these
   needs `annotate_face` (Slice 2) to designate ports.

## Test wiring

Wired as skip-if-absent golden tests in
[`tests/test_golden_issue19.py`](../../test_golden_issue19.py) (run by
`tests/run_all.sh`). Because the *delivered* parts are solid blanks with no
realized cavity (see finding 1–3), the tests pin the pathology rather than a
working flow path: all three read `watertight_solid=True`; `check_airtight_path`
correctly refuses to certify the v2 solid blank ("does not open into a void");
and the v3 doorway-not-integrated anomaly. Each test skips (not fails) if its
fixture is absent. The synthetic fixtures in
`tests/test_worker.py::test_check_airtight_*` remain the always-on positive/
negative regression.
