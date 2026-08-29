# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** A public issue is a
disclosure, and it is visible to everyone from the moment you file it.

Use GitHub's private vulnerability reporting instead — the **Report a vulnerability**
button under this repository's **Security** tab. That opens a private advisory
visible only to the maintainer, where a fix can be prepared and a patched release cut
before anything becomes public.

Please include enough to reproduce: the AnkusDrive version (`ankusdrive --version`),
the FreeCAD version (`ankusdrive doctor`), your OS, and the smallest input or tool
call sequence that triggers it.

Expect an acknowledgement within about a week. This is a small project with a single
maintainer, so please read that as a good-faith target rather than a guarantee.
Coordinated disclosure is welcome; tell us the timeline you have in mind and we will
try to meet it.

## Supported versions

| Version | Supported |
|---|---|
| 0.5.x | Yes |
| < 0.5 | No — upgrade; the project was renamed at 0.5 (see `MIGRATION.md`) |

Only the latest released version is patched. There are no long-term-support branches.

## What is, and is not, a vulnerability here

AnkusDrive's whole purpose is to let an agent drive FreeCAD's Python API, so some
things that look alarming are the documented design.

**Working as intended — not vulnerabilities:**

- **Arbitrary code execution through the intended entry points.** `ankusdrive run
  script.py` and the `run_script` MCP tool execute Python inside FreeCAD's embedded
  interpreter, on purpose. That is the feature. The MCP server runs as a local stdio
  process with the privileges of whoever launched it and trusts its host — it has no
  authentication layer because it was never a network service.
- **An MCP host running tools without asking.** Whether a tool call requires approval
  is the host's policy (Claude Desktop, Claude Code, Cursor), not AnkusDrive's.
- **External solvers doing what solvers do.** OpenFOAM, CalculiX, Elmer, SU2,
  openEMS, YADE and the rest are invoked out-of-process and are separately installed,
  separately maintained software. Report vulnerabilities in them upstream.

**In scope — please do report:**

- Anything that escapes the boundaries above: a path outside the working directory
  written or read through a tool that takes a filename; traversal via a document,
  mesh, or material path.
- Injection into a shell or solver command line via a value that a caller could
  plausibly control (a document name, a material key, a job id).
- A crafted input file — `.FCStd`, `.step`, `.stl`, a material or standards JSON —
  that causes something worse than a clean error.
- Credential or token exposure: anything written into a log, an artifact, a job
  record, or a returned dict.
- A weakness in the release pipeline itself — the trusted-publishing workflow, the
  pinned actions, the built wheel or sdist.
- A dependency vulnerability that AnkusDrive actually reaches, rather than one that
  merely appears in a resolved tree.

## What we do on our side

- Every commit on every ref is scanned for credentials in CI (gitleaks, pinned and
  checksum-verified) — see `.github/workflows/fast-checks.yml`.
- Every GitHub Action is pinned to a commit SHA rather than a moving tag.
- Releases are published to PyPI by OIDC trusted publishing. No long-lived API token
  exists to be stolen.
- Copyleft solvers (KrakenOS, openEMS, YADE) are invoked only out-of-process, which
  keeps a process boundary between them and this code.

## Third parties

AnkusDrive drives **FreeCAD**, which is an independent project. A vulnerability in
FreeCAD, in its bundled Python, or in CalculiX/gmsh as shipped inside it belongs to
those projects — report it there. If you are unsure which side a problem falls on,
report it here and we will help route it.
