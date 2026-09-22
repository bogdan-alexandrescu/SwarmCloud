# sc — the SwarmCloud plugin

Makes the cluster readable from inside a Claude Code session, and makes running
a session's work out there a decision the session takes on its own.

* `/sc` — the overview, or `/sc accounts`, `/sc agents`, `/sc capacity`,
  `/sc trouble`, `/sc task <id>`
* the **`sc` skill** teaches the session how to **read** that output — chiefly
  that `~12%` is a projection and `—` is "not measured", never zero. It is
  read-only and lists no tool that writes.
* the **`delegate` skill** owns the question the developer should not have to
  ask every time: does this unit of work run here or in SwarmCloud? It covers
  when remote is right, when local is right, what a remote agent cannot see
  (a **clean, shallow checkout** — none of your uncommitted work), how to turn
  a dead agent into something actionable, and when it may apply a patch into
  your tree without asking. It **writes**, which is the whole difference
  between the two.

The delegation skill carries the honesty rule with it: remote work spends a
**shared** subscription pool rather than your own session budget, so the
session must always be able to say what is running remotely and what the pool
is at. Seamless is not the same as hidden.

## Install

The plugin lives in this repository at `plugin/`. Point Claude Code at the
repository root as a marketplace, then install `sc` from it.

The repository root needs a `.claude-plugin/marketplace.json` naming this
directory as a plugin source. That file is Track D's (`scripts/`, `.github/`,
root files); it is **not** included here, so until it lands the supported path
is the CLI itself:

```bash
uv run sc            # the same output the plugin shows
uv run sc trouble
```

## What it needs

`swarm-mcp` from this repository, and a working auth tier —
`uv run swarm doctor` says which one this machine has and what it reaches.

The `delegate` skill additionally needs the MCP server itself registered, since
it calls tools rather than shelling out: `.mcp.json` at the repository root
registers it as **`swarmcloud`**, which is where the `mcp__swarmcloud__*` names
in its `allowed-tools` come from.

## What keeps these honest

`tests/unit/mcp/test_plugin_skills.py`, in `make test`. It parses every
`SKILL.md` here and asserts that each tool named in `allowed-tools` or in the
prose is one `swarm_mcp.server.TOOLS` actually serves, that `sc` never gains a
tool that writes, and that the tools `delegate` documents as *not yet built*
are still not built — so the day another lane ships one, the paragraph telling
sessions to work around its absence goes red instead of quietly costing the
platform the feature.
