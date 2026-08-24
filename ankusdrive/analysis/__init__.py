"""AnkusDrive non-FreeCAD physics & analysis.

Pure-Python math that *consumes* geometry (or explicit numbers) and hands back
structured results. Importable both by the FreeCAD worker handlers and
standalone — nothing in this subpackage imports FreeCAD, so every module here is
unit-testable without spawning ``freecadcmd``.

See ``docs/SIMULATION_TOOLS.md`` (catalog + rollout) and
``docs/SIMULATION_EXAMPLES.md`` (per-family execution examples + verification
toys). ``materials`` is the foundational module — fatigue, fracture, and cost all
read from it.
"""
