# DriftPin → AnkusDrive

The project was renamed in **0.5.0** ([#295](https://github.com/gchen19/AnkusDrive/issues/295)).
Same tool, same tools, new name.

0.5.x kept accepting the old spelling on every surface below, with a deprecation
notice on stderr. **0.6 removed all of it except the `.FCStd` properties**, so a
machine that never migrated now behaves as though those settings were never made.
Run `ankusdrive doctor`: it names each leftover it finds and how to move it.

## What you have to change

| Where | Before | After |
|---|---|---|
| Install | `pipx install driftpin` | `pipx install ankusdrive` |
| Command | `driftpin ping` | `ankusdrive ping` |
| MCP registration | `claude mcp add driftpin -- driftpin mcp` | `claude mcp add ankusdrive -- ankusdrive mcp` |
| MCP tool names (agent-visible) | `mcp__driftpin__*` | `mcp__ankusdrive__*` |
| Environment | `DRIFTPIN_FREECADCMD`, `DRIFTPIN_SU2_PATH`, … | `ANKUSDRIVE_*`, same suffixes |
| Config file | `~/.config/driftpin/config.toml`<br>`%APPDATA%\driftpin\config.toml` | `…/ankusdrive/config.toml` |
| Python import | `import driftpin` | `import ankusdrive` |
| Repository | `gchen19/DriftPin` | `gchen19/AnkusDrive` |

Fastest path:

```bash
pipx uninstall driftpin && pipx install ankusdrive
ankusdrive doctor          # names anything still on the old spelling
```

Then edit your MCP host config — the `mcpServers` key, the command, and any
`env` block — or just re-run `ankusdrive setup` and paste what it prints.

## What 0.6 stopped reading

- **`driftpin` on the command line.** No longer installed. An MCP host config,
  script or alias that still names it gets `command not found`.
- **`DRIFTPIN_*` environment variables.** No longer promoted to `ANKUSDRIVE_*`.
  A leftover one is simply unset, so the solver it pointed at falls through to
  auto-discovery.
- **`~/.config/driftpin/config.toml`.** No longer read. Move it to
  `…/ankusdrive/config.toml`.
- **Solvers provisioned under `DriftPin/solvers`.** No longer discovered on
  Windows or macOS. Move them to `AnkusDrive/solvers`, or re-run the solver
  installer.

`ankusdrive doctor` reports each of these as `IGNORED` with its fix, and the CI
preflight scripts still fail an environment that sets a `DRIFTPIN_*` variable.

## Your existing `.FCStd` files

**They keep working, and you do not have to re-save them.**

Everything AnkusDrive stamps onto a part — published interfaces, face roles,
design intent, performance contracts and verdicts, dimension truth values, title
blocks, balloons — is a FreeCAD property whose name carried the old initials
(`DP_Intent`, `DP_Interfaces`, …). New parts are stamped `AD_*`. Every read
accepts either, and a write to a part that carries the old spelling updates that
property in place rather than adding a second one, so a document never ends up
with two competing stamps.

This is the one shim worth understanding, because its failure mode is silent:
had the old names simply been dropped, an annotated part would have opened fine
and read back as though it had never been annotated — no interfaces, no intent,
no verdict, no error. `tests/test_compat_rename.py` pins the behaviour.

That is why this fallback **stays in 0.6**. It goes only together with an
explicit document migration, not a quiet deletion.

## The logo

The DriftPin mark — a drift pin threading a reticle — is name-derived artwork, so
the rename needs a redesign rather than a re-export. The old assets stay in
`logo/` until that lands, and the README shows no wordmark in the meantime.
