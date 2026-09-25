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

The plugin is a **client for your deployment**, not for this repository's.
Ask your platform operator for three values first: the deployment URL, and —
for a deployment behind IAP — its Desktop OAuth client ID and secret.

**Today, install it from a checkout of this repository.** In a Claude Code
session:

```text
/plugin marketplace add /path/to/your/SwarmCloud/checkout
/plugin install sc@swarmcloud
```

A marketplace added from a local directory loads the plugin **in place**, so
its MCP server — `uv run --directory ${CLAUDE_PLUGIN_ROOT}/.. swarm-mcp` —
finds the checkout's `pyproject.toml`
([plugin loading reference](https://code.claude.com/docs/en/plugins/loading),
"In-place and copied plugins"). `claude --plugin-dir <checkout>/plugin` does
the same for one session.

**From GitHub it does not work yet.** `/plugin marketplace add
bogdan-alexandrescu/SwarmCloud` installs, prompts and loads the skills, but
Claude Code copies only `plugin/` into `~/.claude/plugins/cache/…`, so
`${CLAUDE_PLUGIN_ROOT}/..` holds no project and the MCP server fails to start
(measured 2026-09-25 on 0.4.0: the cache held `0.4.0/` and nothing else, and
`plugin:sc:swarmcloud` failed to connect). Nothing the plugin was configured
with then reaches a running bridge, and `sc login` in a terminal finds no
deployment. Running the bridge from the cache is the plugin-standalone-bridge
change (#62); until it is merged and its tag pushed, use the checkout.

Either way you are prompted for the deployment URL (required), the OAuth
client ID and the OAuth client secret. The secret is `sensitive` in the
manifest, so Claude Code keeps it in your system's secure credential store,
never in `settings.json`. Then:

```text
/reload-plugins
```

and sign in, from a terminal in the checkout:

```bash
uv run sc login    # a browser window opens; pick your work account
uv run sc whoami   # context, URL, you, your tenant
```

`sc login` finds the deployment because the plugin's MCP server writes it to
your config file when it starts — so start the server first (`/reload-plugins`,
then `/mcp` shows `plugin:sc:swarmcloud` connected), or add it yourself with
`uv run sc context add`. `sc login` ends by calling the API once **with the
sign-in it just made**, whatever else is set on the machine, and warns you if
`SWARM_IMPERSONATE_SA`, `SWARM_ID_TOKEN` or a GCP metadata server means every
other command will still act as a service account rather than as you.

From then on every tool acts **as you**. A tool called before you sign in
answers `sign-in required for <context>: run sc login (a browser window
opens)` rather than failing some other way. Change a value later with
`/plugin configure sc@swarmcloud`.

**More than one deployment** is a context each, `kubectl`-style —
`uv run sc context add`, `uv run sc context use`, `uv run sc context list`,
`uv run sc context remove` — and CI overrides all of it with `SWARM_URL` and
`SWARM_IMPERSONATE_SA`. [docs/plugin-setup.md](../docs/plugin-setup.md) has
the whole of it: what each command stores where, the override order, and
Saga's `dev` values as a worked example. The operator's one-time step — one
Desktop OAuth client per deployment, allowlisted on IAP — is
[docs/runbooks/iap-desktop-client.md](../docs/runbooks/iap-desktop-client.md).

**Nothing in the plugin names a deployment.** Until 2026-09-25 the bridge read
`terraform/environments/<env>/<env>.tfvars` out of whatever checkout it ran
from, so every install pointed at Saga's cluster. It now reads Terraform only
in developer mode (`SWARM_MCP_CONFIG_FROM=repo`), for working inside this
repository, and `tests/unit/mcp/test_contexts.py` fails if it opens a tfvars
file otherwise.

### Where the plugin comes from

The plugin lives in this repository at `plugin/`, and the repository root is
the marketplace: `.claude-plugin/marketplace.json` lists `sc` with `plugin/` as
its source.

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
commands Claude runs through the Bash tool. So when the plugin is loaded in
place from a checkout, the MCP half works from any working directory and the
**shell half does not**: `uv run sc` resolves `uv`'s project against the
session's own directory, and outside a checkout it answers that there is no
`pyproject.toml`. That is a real limit, stated here rather than papered over,
because a model that meets it without warning reports the platform as broken.
The `delegate` skill calls tools and is unaffected; `/sc` and the `sc` skill
want the repository.

Installed from the **GitHub** marketplace, the MCP half does not start either:
the server's `uv run --directory ${CLAUDE_PLUGIN_ROOT}/..` points at the
plugin cache, not a checkout (see Install). That is #62's to fix.

The `sc` skill and `/sc` are granted each **view** by name —
`uv run sc accounts`, `uv run sc task`, and so on — and never `sc` as a
prefix. `sc login`, `sc logout` and `sc context` share that prefix, and a
prefix grant would let a session sign the developer out or move every later
dispatch to another cluster without asking.
`tests/unit/mcp/test_plugin_commands.py` compiles each grant the way Claude
Code matches it and runs every command it allows through the real parsers.

The CLI is the same surface without the session wrapping, and is what to reach
for when diagnosing the plugin itself:

```bash
uv run sc            # the same output the plugin shows
uv run sc trouble
uv run swarm profiles
```

## What it needs

`swarm-mcp` from this repository, a configured deployment (see Install), and a
working auth tier — `uv run swarm doctor` says which deployment it resolved
and from where, which tier this machine has, **which door it will use**, and
what that door takes.

### Where the API actually is

Wherever **you** configured it: `--context`, `SWARM_URL`, the plugin's
deployment URL or the current context, in that order ([docs/plugin-setup.md](../docs/plugin-setup.md)
has the full order). On a **team** deployment that is the load balancer: the
`*.run.app` address is internal-only — its ingress is
`internal-and-cloud-load-balancing`, so Google's frontend refuses an outside
caller and renders the refusal as HTTP 404, the one status a reader takes for
"missing route on a broken deployment". A context on any host other than
`*.run.app`, or with an OAuth client ID, is treated as that IAP front door.

In **developer mode** (`SWARM_MCP_CONFIG_FROM=repo`) the bridge instead reads
`frontend_hostname` out of `terraform/environments/<env>/<env>.tfvars`, the same
three-source order `scripts/lib/common.sh` uses, with `API_HOST` overriding it.
That is for working inside this repository and is never the default.

The doors take **different credentials**, which is the other half:

| Door | Who you are | Credential |
|---|---|---|
| the IAP load balancer | a developer, signed in with `sc login` | an **ID** token for the deployment's Desktop OAuth client |
| the IAP load balancer | CI, `SWARM_IMPERSONATE_SA` | the service account's OAuth **ACCESS** token |
| Cloud Run directly | an in-VPC caller, or CI on a solo deployment | a Google **ID** token for the service URL |
| a solo deployment's `*.run.app` address | a developer on ordinary gcloud credentials | gcloud's own **ID** token (`gcloud auth print-identity-token`), which Cloud Run takes from an account holding `run.routes.invoke` |

Measured on 2026-09-24 against the live front door: a gcloud **user** access
token is refused **401, IAP error code 900**, because the deployment's IAP uses
a Google-managed OAuth client, which admits only allowlisted programmatic
clients — that is why `sc login` exists. An impersonated service account's
access token is refused **403 naming that service account** — IAP saying
"authenticated, not authorised", one `roles/iap.httpsResourceAccessor` grant
from working. That grant is `frontend_iap_members` in
`terraform/bootstrap/terraform.tfvars`, applied by the owner rather than by CI.
`SWARM_IAP_CLIENT_ID` applies only where a deployment configured its own IAP
OAuth client, which this one deliberately does not (a client id there means a
client secret in Terraform state).

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
