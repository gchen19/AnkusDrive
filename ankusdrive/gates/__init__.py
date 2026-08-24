"""AnkusDrive deterministic merge-time gates (one module per gate).

Each gate is a pure-Python module exposing a small, host-agnostic surface that
the FreeCAD worker wraps with a thin handler (append-only, never interleaved into
``merge_assembly``'s body). The first inhabitant is the Liskov-substitutability
gate (issue #147, DESIGN_HIERARCHY §7.1) — Form/Fit/Function as code.

``modal`` is the odd one out: the ``min_first_mode_hz`` gate needs a live FEM
solve, so its BODY stays in the worker. What lives here is the part that must be
shared verbatim between the worker and every consumer of the report — how a
first-mode outcome is classified, and in particular the difference between "the
part is too floppy" and "the solve never finished" (issue #248).

``performance`` generalizes that discipline to #226's quantitative contracts: it
is the single judgement ``merge_assembly``, ``substitutability_check`` and
``component_contract_check`` all read a ``AD_Performance`` contract with, so
"met", "not met" and "nobody has measured it yet" can never collapse into one
another at any of the three gates (issue #261).
"""

from . import modal  # noqa: F401
from . import performance  # noqa: F401
from . import substitutability  # noqa: F401

__all__ = ["modal", "performance", "substitutability"]
