"""Did the interchange file we just wrote actually contain geometry? (issue #434)

FreeCAD-free on purpose, so the fast lane can test it: the check is a scan of the
written bytes, and the failure it guards against is a file that looks like a
successful export — plausible size, valid header — with no faces in it at all.

`Part.export()` of an App::Part wrote exactly that: a product header, an empty
SHAPE_REPRESENTATION, 1.6 kB, and `{path, size, object}` returned as if it had
worked. The worker exports the resolved shape now, and calls this afterwards so a
silent loss can never be reported as success again.
"""

# A STEP carrying any geometry names its faces. ADVANCED_FACE covers B-rep solids
# and trimmed surfaces; FACE_SURFACE appears in surface-only files; the solid
# entity itself is listed so a manifold solid matches on its own name too.
STEP_GEOMETRY_MARKERS = (b"ADVANCED_FACE", b"MANIFOLD_SOLID_BREP", b"FACE_SURFACE")


def file_contains(path, needles, chunk_bytes=1 << 20):
    """True when any of `needles` (bytes) appears in the file.

    Streamed with an overlap, so a marker split across two chunk boundaries is
    still found and a 500 MB STEP is never read into memory whole."""
    overlap = max(len(n) for n in needles) - 1
    tail = b""
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_bytes)
            if not block:
                return False
            buf = tail + block
            if any(n in buf for n in needles):
                return True
            tail = buf[-overlap:] if overlap else b""


def step_has_geometry(path):
    """True when a STEP file names at least one face or solid."""
    return file_contains(path, STEP_GEOMETRY_MARKERS)
