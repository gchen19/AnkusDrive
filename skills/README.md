# skills/

Agent **skills** — rules-as-data playbooks surfaced to whatever host drives the
DriftPin agent. Each skill is a directory containing a `SKILL.md` with YAML
frontmatter (`name`, `description`) and a Markdown body. This is the host-agnostic
Knowledge-Based-Engineering layer: guidance the agent reads *before* it acts,
**never enforced by the worker** — the deterministic gates do the enforcing.

A skill is the *inverse of a gate*: the gates (in `driftpin/` + `tests/`) are the
test suite that catches a mistake after geometry is spent; a skill is the style
guide / design review that avoids it beforehand.

| Skill | Run it… | Scoping |
|---|---|---|
| [`design-modularly`](design-modularly/SKILL.md) | before you decompose a design or publish an interface | `docs/DESIGN_HIERARCHY.md` §8 (#149) |
| [`release-control`](release-control/SKILL.md) | before you create, revise, or supersede a part number | `docs/DESIGN_HIERARCHY.md` Theme C (#140/#141/#142) |

See `docs/DESIGN_HIERARCHY.md` §6–§9 for the modularity framing, the primitives
each skill leans on, and the gates each one points at.
