"""DriftPin — CLI + MCP layer over FreeCAD's Python API."""

from .client import Worker, WorkerError

__all__ = ["Worker", "WorkerError"]
