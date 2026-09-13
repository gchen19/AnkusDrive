# Introspection image for MCP directories (#201) — NOT a solver image.
#
# Glama (glama.ai/mcp/servers) builds each listed server from a Dockerfile, starts
# it over stdio, and reads the tool list; a server whose build fails drops out of
# search, and punkpeye/awesome-mcp-servers requires a passing Glama listing. This
# image is exactly that much: the package from this checkout and `ankusdrive mcp`.
#
# It carries no FreeCAD and no solvers, so every tool that needs one returns the
# structured "not installed" result with its install hint rather than crashing —
# which is the documented degradation contract, and all a directory scan exercises.
# A container that actually runs CAD/FEM (FreeCAD 1.1 + CalculiX + Gmsh, and the
# GPL solver tiers with their licensing decision) is epic #296's deploy/ image, and
# a different artifact from this one.
#
# Base pinned by digest, same policy as the SHA-pinned actions (#301).
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE TRADEMARKS.md MANIFEST.in MIGRATION.md ./
COPY ankusdrive ./ankusdrive
COPY scripts ./scripts
RUN pip install . \
 && useradd --uid 10001 --home-dir /work --create-home ankusdrive

USER ankusdrive
WORKDIR /work
ENTRYPOINT ["ankusdrive", "mcp"]
