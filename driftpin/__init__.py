"""DriftPin — CLI + MCP layer over FreeCAD's Python API."""

# Single source of truth for the package version. pyproject.toml reads this
# attr (setuptools dynamic version), publish.yml greps this line at tag
# push, and the CLI exposes it via --version.
__version__ = "0.3.0"

from .client import Worker, WorkerError

__all__ = ["Worker", "WorkerError", "__version__"]
