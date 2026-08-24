# DriftPin → AnkusDrive

The project was renamed in **0.5.0** ([#295](https://github.com/gchen19/AnkusDrive/issues/295)).
Same tool, same tools, new name.

Nothing on your machine breaks the moment you upgrade: every surface below keeps
accepting the old spelling for one minor release, with a deprecation notice on
stderr. **All of it is removed in 0.6**, so treat this as a to-do list, not a
permanent guarantee.

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
ankusdrive doctor          # reports anything still resolving through a shim
```

Then edit your MCP host config — the `mcpServers` key, the command, and any
`env` block — or just re-run `ankusdrive setup` and paste what it prints.

## What keeps working until 0.6

- **`driftpin` on the command line.** Still installed; prints a rename notice and
  runs `ankusdrive`.
- **`DRIFTPIN_*` environment variables.** Promoted to their `ANKUSDRIVE_*` name at
  startup unless you have already set the new one, which always wins. One stderr
  line lists what was promoted.
- **`~/.config/driftpin/config.toml`.** Still read when no `ankusdrive/config.toml`
  exists. Writes always go to the new path, so the next `ankusdrive setup`
  migrates you, contents included.
- **Solvers provisioned under `DriftPin/solvers`.** Still discovered on Windows and
  macOS. New provisioning uses the new directory.

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

Removing the fallback in 0.6 will come with an explicit document migration, not
a quiet deletion.

## The logo

The DriftPin mark — a drift pin threading a reticle — is name-derived artwork, so
the rename needs a redesign rather than a re-export. The old assets stay in
`logo/` until that lands, and the README shows no wordmark in the meantime.
