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

The plugin lives in this repository at `plugin/`, and the repository root is
the marketplace: `.claude-plugin/marketplace.json` lists `sc` with `plugin/` as
its source. Point Claude Code at the repository root as a marketplace, then
install `sc` from it.

That file is small and easy to overlook, so it is worth saying what it is for:
**without it none of the rest of this directory is reachable.** A plugin is
installed from a marketplace, so a repository with skills, commands, a manifest
and no marketplace entry has a plugin that is entirely correct and entirely
uninstallable. It was missing until 2026-09-24 and everything here was in that
state. `tests/unit/mcp/test_plugin_commands.py` now asserts it exists and points
at a directory that really holds a `plugin.json`.

The CLI is the same surface without the session wrapping, and is what to reach
for when diagnosing the plugin itself:

```bash
uv run sc            # the same output the plugin shows
uv run sc trouble
uv run swarm profiles
```

## What it needs

`swarm-mcp` from this repository, and a working auth tier —
`uv run swarm doctor` says which one this machine has and what it reaches.

The `delegate` skill additionally needs the MCP server itself registered, since
it calls tools rather than shelling out: `.mcp.json` at the repository root
registers it as **`swarmcloud`**, which is where the `mcp__swarmcloud__*` names
in its `allowed-tools` come from.

**`swarm` and `sc` are not on your PATH**, and nothing here should ever tell you
they are. They are console scripts of `swarm-mcp`, installed into the uv-managed
environment, so every command this plugin hands back carries the `uv run`
prefix. Three places used to hand a model the bare string `swarm tail <id>`; the
model ran it, got `command not found`, and had every reason to report the
platform as broken.

## Choosing a runner profile

A caller names a **profile** and nothing else — never an image, a command, a
resource spec or a backend. That is invariant 10 and it is the rule that stops
an authenticated caller turning the swarm into arbitrary compute.

So the names have to come from somewhere a session can reach, and they come
from `swarm_profiles` (the tool) and `uv run swarm profiles` (the terminal),
which are the same function over `swarm_common.profiles.RUNNER_PROFILES` — the
frozen catalogue itself, not a list copied into a skill. They read nothing from
the network, which is why they still answer when the cluster does not.

Neither shows an image or a command. That is deliberate: a field a session can
see is a field a session will eventually offer to set.

A name the catalogue refuses is refused **before anything is dispatched**, and
the two refusals are kept apart because they need different answers — an unknown
name is a typo and lists the real names, while a known-but-disabled one quotes
the catalogue's own reason. `codex` is the live example: disabled rather than
deleted, so the runs that name it stay readable.

## What keeps these honest

Two files, both in `make test`, both offline:

`tests/unit/mcp/test_plugin_skills.py` parses every `SKILL.md` here and asserts
that each tool named in `allowed-tools` or in the prose is one
`swarm_mcp.server.TOOLS` actually serves, that `sc` never gains a tool that
writes, and that the skill never tells a session a tool which exists is missing.

`tests/unit/mcp/test_plugin_commands.py` covers the half that had nothing: the
**shell commands**. Every `swarm` or `sc` subcommand named in a skill, a command
file or this README must be one the argparse parsers really accept, nothing the
bridge returns may tell a model to run a bare `swarm`, and the marketplace
manifest must exist and agree with `plugin.json` about this plugin's name.
