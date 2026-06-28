"""DriftPin deterministic merge-time gates (one module per gate).

Each gate is a pure-Python module exposing a small, host-agnostic surface that
the FreeCAD worker wraps with a thin handler (append-only, never interleaved into
``merge_assembly``'s body). The first inhabitant is the Liskov-substitutability
gate (issue #147, DESIGN_HIERARCHY §7.1) — Form/Fit/Function as code.
"""

from . import substitutability  # noqa: F401

__all__ = ["substitutability"]
