# sc — the SwarmCloud plugin

Makes the cluster's state readable from inside a Claude Code session:

* `/sc` — the overview, or `/sc accounts`, `/sc agents`, `/sc capacity`,
  `/sc trouble`, `/sc task <id>`
* a skill that teaches the session how to **read** that output — chiefly that
  `~12%` is a projection and `—` is "not measured", never zero

Everything here is read-only.

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
