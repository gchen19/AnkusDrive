# Golden fixtures — item model (issue #140, C1)

Frozen in the first commit so the Wave-1 agents (#141 revision/lifecycle, #142
ECO/where-used, #143 project container, #146) build against a stable contract.

- **`items.json`** — a well-formed `ankusdrive.items/1` registry: two items
  (`bracket`, `pin`), each with a non-significant sequential part number
  (`DP-001001`, `DP-001002`), the **reserved** `rev` / `lifecycle` fields (held,
  not interpreted in C1 — #141 owns the state machine), artifact `files[]`, and
  free-form queryable `metadata` (where ALL "meaning" lives). The
  `part_number_format` counter (`next: 1003`) is the allocation state for the next
  sequential number.

- **`manifest_with_items.json`** — a manifest whose components reference items by
  id (`{"item": "bracket"}`) instead of bare file paths, demonstrating the
  **item-reference form**. `ankusdrive.items.validate_manifest_refs` resolves these
  against `items.json`; a dangling reference is caught.

The artifact files the items point at are illustrative paths (C1 is pure
data/text identity — it does not build geometry). See `ankusdrive/items.py` for the
full schema docstring and `tests/test_items.py` for the two-sided gate.
