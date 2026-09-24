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

### The bridge ships with the plugin — and arrives under two names

`plugin.json` declares the `swarmcloud` MCP server, so installing the plugin
installs the bridge. That was missing: the only registration was `.mcp.json` at
the repository root, which a session in any other directory does not have, so
`delegate` was eighteen tool permissions for a server that existed in one
directory on one machine.

A plugin's own MCP server is **scoped**. Its tools arrive as
`mcp__plugin_sc_swarmcloud__*`, not `mcp__swarmcloud__*` — those are the
project server's. A permission rule is matched and not resolved, so the wrong
spelling grants nothing, silently, and the session behaves as though delegation
does not work. `delegate` therefore lists **both** spellings of all eighteen
tools, and `tests/unit/mcp/test_plugin_skills.py` holds the two lists in
lockstep and derives the scoped prefix from `plugin.json` itself.

### What is still repository-bound, and why it cannot be fixed here

`${CLAUDE_PLUGIN_ROOT}` is exported to MCP server subprocesses and **not** to
commands Claude runs through the Bash tool. So the MCP half of this plugin
works from any working directory and the **shell half does not**: `uv run sc`
resolves `uv`'s project against the session's own directory, and outside a
checkout it answers that there is no `pyproject.toml`. That is a real limit,
stated here rather than papered over, because a model that meets it without
warning reports the platform as broken. The `delegate` skill calls tools and is
unaffected; `/sc` and the `sc` skill want the repository.

The CLI is the same surface without the session wrapping, and is what to reach
for when diagnosing the plugin itself:

```bash
uv run sc            # the same output the plugin shows
uv run sc trouble
uv run swarm profiles
```

## What it needs

`swarm-mcp` from this repository, and a working auth tier —
`uv run swarm doctor` says which one this machine has, **which door it will
use**, and what that door takes.

### Where the API actually is

On a **team** deployment the API is behind IAP at a load balancer, and the
`*.run.app` address is internal-only: its ingress is
`internal-and-cloud-load-balancing`, so Google's frontend refuses an outside
caller and renders the refusal as HTTP 404 — the one status a reader takes for
"missing route on a broken deployment". Until 2026-09-24 the bridge asked Cloud
Run for the address and therefore called a healthy control plane UNREACHABLE.
It now reads `frontend_hostname` out of `terraform/environments/<env>/<env>.tfvars`
(Track C's input, read rather than copied), the same three-source order
`scripts/lib/common.sh` uses, and `API_HOST` overrides it.

The two doors take **different credentials**, which is the other half:

| Door | Credential |
|---|---|
| Cloud Run directly | a Google **ID** token |
| the IAP load balancer | an OAuth **ACCESS** token |

Sending the ID token to IAP produces `Invalid IAP credentials: Invalid JWT
audience`, which reads like an IAM problem and is not. Measured on 2026-09-24
against the live front door: a user access token is refused **401, IAP error
code 900**, and an impersonated service account's access token is refused
**403 naming that service account** — which is IAP saying "authenticated, not
authorised", one `roles/iap.httpsResourceAccessor` grant from working. So on a
team deployment set `SWARM_IMPERSONATE_SA`; `SWARM_IAP_CLIENT_ID` applies only
where the deployment configured its own OAuth client, and this one deliberately
does not (a client id means a client secret in Terraform state).

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
