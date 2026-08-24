"""AnkusDrive — CLI + MCP layer over FreeCAD's Python API."""

# Single source of truth for the package version. pyproject.toml reads this
# attr (setuptools dynamic version), publish.yml greps this line at tag
# push, and the CLI exposes it via --version.
__version__ = "0.5.0"

# Normalise the environment ONCE, before anything reads it. The DriftPin ->
# AnkusDrive rename (#295) renamed ~40 env vars; this promotes any surviving
# DRIFTPIN_* var to its new name so the ~40 read sites downstream only ever
# name the new spelling. Import-time on purpose: the CLI, the MCP server and
# the worker inside freecadcmd all reach their env through this package.
from . import config as _config

_config.adopt_legacy_env()

from .client import Worker, WorkerError  # noqa: E402

__all__ = ["Worker", "WorkerError", "__version__"]
