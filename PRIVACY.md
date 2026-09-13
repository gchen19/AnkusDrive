# Privacy Policy

_Effective 2026-09-13. Applies to AnkusDrive: the PyPI package `ankusdrive`, the
`ankusdrive-<version>.mcpb` desktop extension, and the MCP server they run._

AnkusDrive is software you run on your own computer. It drives your local FreeCAD
installation. It has no accounts, no servers of its own, and no telemetry.

## What AnkusDrive collects

**Nothing.** AnkusDrive does not collect, transmit, sell or share personal data, usage data,
analytics, crash reports, or the contents of your models. The maintainer receives no
information about you or how you use it.

## Network access

The AnkusDrive code makes **no network requests**: it contains no HTTP client and no telemetry
or update check. Network traffic happens only in these cases, each one run by other software
and each one started by you:

- **Installing or updating.** `pip`, `pipx` or `uv` download AnkusDrive and its dependencies
  from PyPI. Claude Desktop may download `uv` from GitHub to set up the extension. These are
  governed by those services' own policies.
- **`ankusdrive setup`**, when you choose to install optional solver packages. It runs `pip` in
  your environment.
- **Your MCP host.** The application you connect AnkusDrive to, such as Claude Desktop, another
  AI client, or an IDE, sends your prompts together with AnkusDrive's tool inputs and results
  to its AI model provider. That transfer is governed by the host's and the provider's privacy
  policies, not this one. AnkusDrive itself sends nothing anywhere.
- **Third-party software you install** alongside AnkusDrive: FreeCAD, CalculiX, Gmsh, OpenFOAM,
  Elmer, SU2 and the other optional solvers. It runs locally under its own terms.

## Data AnkusDrive stores on your computer

Everything below stays on your machine:

| What | Where | How long |
|---|---|---|
| Configuration: paths to FreeCAD and optional solvers | `~/.config/ankusdrive/config.toml` (Windows: `%APPDATA%\ankusdrive\config.toml`), or the path in `ANKUSDRIVE_CONFIG` | until you delete it |
| Files you ask it to write: saved documents, exports, drawings, reports, lockfiles, project and item registries | the paths or directories you pass to a tool | until you delete them |
| Solver working directories and renders | your system temp directory, or a `workdir` / `case_dir` you pass | temp files are cleaned up by the tools or your OS; a directory you supply keeps its files |
| Background job results and open documents | the running server's memory | until the job is discarded or the server exits |

## Sharing and retention

AnkusDrive shares no data with the maintainer or any third party. It keeps no records beyond
the local files listed above, which you control and can delete at any time.

## Children

AnkusDrive is a developer and engineering tool. It collects no data from anyone, including
children.

## Changes

Changes to this policy are made in this file. Its history on GitHub is the change log:
https://github.com/gchen19/AnkusDrive/commits/main/PRIVACY.md

## Contact

Questions about this policy: open an issue at https://github.com/gchen19/AnkusDrive/issues.
For a security or privacy vulnerability, use GitHub's private vulnerability reporting, as
described in [`SECURITY.md`](SECURITY.md), not a public issue.
