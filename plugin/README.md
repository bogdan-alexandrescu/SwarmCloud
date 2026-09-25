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

```text
/plugin marketplace add bogdan-alexandrescu/SwarmCloud
/plugin install sc@swarmcloud
```

### What an install copies: only `plugin/`

`/plugin marketplace add` clones this repository into
`~/.claude/plugins/marketplaces/swarmcloud/`. `/plugin install sc@swarmcloud`
then copies **only `plugin/`** into
`~/.claude/plugins/cache/swarmcloud/sc/<version>/`, and that copy is
`${CLAUDE_PLUGIN_ROOT}`. Nothing above it exists there — Claude Code's plugin
loading reference: "Files outside the plugin directory aren't copied" — and a
symlink that leads out of the plugin is refused too.

Until 0.4.1 the server was declared as
`uv run --directory ${CLAUDE_PLUGIN_ROOT}/.. swarm-mcp`. In the cache,
`${CLAUDE_PLUGIN_ROOT}/..` is `.../cache/swarmcloud/sc/`, which holds no
`pyproject.toml`, so uv answered
`error: Failed to spawn: swarm-mcp -- No such file or directory` and the
session showed `plugin:sc:swarmcloud failed to connect` (measured 2026-09-25 on
0.4.0, commit ea1355d). It had only ever worked inside a checkout — where the
root `.mcp.json` registers the bridge anyway.

### The bridge ships with the plugin — fetched, not copied

`plugin.json` declares the `swarmcloud` MCP server as

```text
uv tool run --from 'swarm-mcp @ git+https://github.com/bogdan-alexandrescu/SwarmCloud@sc-v<version>#subdirectory=apps/swarm-mcp' swarm-mcp
```

`uv tool run` builds a cached environment from a requirement and needs no
project on disk, so the server starts wherever the session is. That line
commits to four things:

* **The ref is the plugin's own version.** The tag is `sc-v<version>`, where
  `<version>` is `plugin.json`'s `version`. Claude Code keeps every user on
  their cached copy until `version` changes, so the version decides which
  skills a user has and the tag decides which bridge those skills talk to. They
  move together, or a skill describes tools the running bridge does not have.
  The ref is written once, in `plugin.json`;
  `tests/unit/mcp/test_plugin_bridge_install.py` fails when it is not `sc-v`
  followed by the version, and when it is restated anywhere else.
* **Not `main`.** A branch would pair the skills copied at install time with
  whatever `main` was at each server start, and make every start depend on
  `main` being releasable at that moment.
* **swarm-common comes from the same commit.** The bridge depends on the frozen
  contract in `apps/common`, which is not on PyPI — and the name is unclaimed
  there, so a bare dependency would install whoever registers it.
  `apps/swarm-mcp/pyproject.toml` gives it a relative `path` source, and
  uv ≥ 0.5.6 rewrites a relative path inside a git dependency into a git source
  at the same commit, with the subdirectory relative to the checkout. One ref,
  and no second pin to drift from it. pip does not read `[tool.uv.sources]`;
  installing the bridge with pip is not supported.

  **uv's documentation does not describe this case**, so here is what the
  claim rests on. The docs cover the pieces: a `path` source may be relative
  and installs "from a directory relative to the project root", and "Sources
  are only respected by uv"
  ([Dependency sources](https://docs.astral.sh/uv/concepts/projects/dependencies/#dependency-sources));
  `uvx --from` takes a git URL
  ([Requesting different sources](https://docs.astral.sh/uv/guides/tools/#requesting-different-sources)).
  Neither page says what a relative path means once the package declaring it
  was fetched from git. uv's 0.5.6 release notes do — "Respect path
  dependencies within Git dependencies
  ([#9594](https://github.com/astral-sh/uv/pull/9594))" — and the code is
  `path_source` in `crates/uv-distribution/src/metadata/lowering.rs`. Because
  that is a release note and not a documented contract, CI checks it on every
  run: `uv pip compile` of the pinned requirement must resolve swarm-common
  to `git+https://github.com/bogdan-alexandrescu/SwarmCloud@<the same commit>#subdirectory=apps/common`.
* **Never shorten it to `uvx swarm-mcp`.** PyPI's `swarm-mcp` is an unrelated
  project (a Foursquare Swarm check-in server). The name before ` @ ` binds the
  URL; a bare name is a PyPI lookup.

**Releasing a version** is three steps, and the third is the easy one to
forget: bump `version` and the ref in `plugin.json` together (the test holds
them together), merge, then tag the merge commit and push the tag.

```bash
git fetch origin
git tag sc-v<version> origin/main
git push origin sc-v<version>
```

Until that tag exists the new version's server cannot start: uv reports that
it cannot find the ref.

**What it takes on the machine the session runs on:**

* **`uv` ≥ 0.5.6** on the `PATH` Claude Code was started with. A session
  started from a desktop launcher may not have `~/.local/bin` or Homebrew's
  `bin` on its `PATH`, and `/mcp` then shows the server failing to spawn `uv`.
* **Network to github.com and pypi.org at the first start.** uv fetches the
  repository at the tag, fetches hatchling to build two small wheels, and
  downloads a Python ≥ 3.11 if none is installed. Later starts reuse uv's
  cached environment, but resolving the tag is still one request to GitHub, so a
  start with no network fails.
* **The first start can outlast Claude Code's MCP startup timeout**
  (`MCP_TIMEOUT`, 30 seconds by default). How long it takes depends on the
  network and on whether a Python has to be downloaded; CI installs the bridge
  from a cold cache on every run and prints how long that took, which is a
  datacentre's number rather than a laptop's. If the first connect fails,
  reconnect the server in `/mcp` — uv resumes from what it has already cached —
  or start that first session with `MCP_TIMEOUT=120000 claude`.

**Where the API is, outside a checkout.** In a checkout the bridge finds a
team deployment's front door by reading `frontend_hostname` from
`terraform/environments/<env>/<env>.tfvars` (see *Where the API actually is*,
below), and that one fact decides both the address and the credential.
Installed from git it has no repository to read: the running code is in uv's
cache, with no `terraform/` anywhere above it. So tell it, in the environment
Claude Code starts from (the MCP server inherits it):

* **`API_HOST=<hostname>`** (or `SWARM_API_HOST`) for a team deployment. This
  declares the front door. The bridge sends every call there and presents an
  OAuth access token, which is the kind IAP takes.
* **`SWARM_API_URL=https://<hostname>`** works the same way. With no front door
  declared, the bridge takes an `https` address that is not `*.run.app` to be
  the load balancer. Without that rule, which #62's first version lacked, it
  presents a Google ID token for the URL. IAP refuses that as
  `Invalid JWT audience`, and a laptop with only user credentials cannot mint
  one at all. A `*.run.app` address still gets an ID token. That is the in-VPC
  case, where the run.app address does answer. Use `SWARM_API_URL`, not
  `API_URL`: on the user-credentials tier the bridge reads only the `SWARM_`
  spelling before it starts a proxy.
* **`SWARM_REPO_ROOT=<a checkout>`**: the bridge reads that checkout's tfvars,
  as it would had it been installed from there.

At this deployment's front door the token also has to be one IAP admits. A
user's own access token is refused with 401, IAP error code 900, so set
`SWARM_IMPERSONATE_SA` as *Where the API actually is* explains.

**With none of these set, the bridge does not find the front door.** What
happens next depends on the auth tier:

* **A laptop that has only run `gcloud auth login`** (the `user-credentials`
  tier) starts `gcloud run services proxy` against the project's `swarm-api`
  Cloud Run service. The project comes from `PROJECT_ID`, or from gcloud's
  configured project. That can take up to 25 seconds. On a team deployment the
  service's ingress refuses callers outside the VPC, so every call comes back
  as an HTML 404. The bridge reports it as an ingress refusal, not a missing
  route, and says to go through the load balancer. It does not name the
  variable. The variable is `API_HOST`.
* **Every other tier** (a service account, the metadata server, an explicit
  token) asks Cloud Run for the address, which needs `PROJECT_ID`. It sees the
  internal-only ingress and refuses before any call is made, and that refusal
  does name `API_HOST`. Without `PROJECT_ID` it refuses at once and asks for
  `SWARM_API_URL` or `PROJECT_ID`.

### Running your own bridge: `SWARM_MCP_FROM`

The `--from` value in `plugin.json` is
`${SWARM_MCP_FROM:-<the pinned requirement>}`, and Claude Code expands
`${VAR:-default}` in a plugin server's arguments. Set it before starting
Claude Code:

```bash
# your working copy -- an absolute path to apps/swarm-mcp in a checkout
SWARM_MCP_FROM="$PWD/apps/swarm-mcp" claude

# an unmerged branch
SWARM_MCP_FROM='swarm-mcp @ git+https://github.com/bogdan-alexandrescu/SwarmCloud@<branch>#subdirectory=apps/swarm-mcp' claude
```

* A working copy is rebuilt when its source changes: `swarm_mcp/**/*.py` is one
  of the bridge's uv cache keys, so an edit is served once the server restarts
  (reconnect it in `/mcp`). `swarm-common` is installed editable from the
  sibling `apps/common`.
* **It reads the tfvars of the checkout it was built from.** The install is not
  editable. The code is your working copy's, but the files are in uv's cache,
  where nothing above them is your checkout. Going by file location alone, the
  bridge would lose the front door that `uv run` in the same checkout finds, and
  in #62's first version it did. It finds the checkout from the install's own
  record of its source. That is
  the `direct_url.json` uv writes for a directory install (PEP 610). CI's install
  step checks it against a real install. `SWARM_REPO_ROOT` still overrides it.
* **Not `local`, and not any bare word.** `--from` reads a bare word as a PyPI
  name — the trap above. Leave the variable **unset** rather than empty: an
  empty value is passed through as an empty `--from`.

Inside a checkout there is also the project `.mcp.json`, which registers
`swarmcloud` as `uv run --directory . swarm-mcp` — the editable working tree,
no variable needed. Its tools arrive under a different name, which is the next
section.

### The bridge arrives under two names

A plugin's own MCP server is **scoped**. Its tools arrive as
`mcp__plugin_sc_swarmcloud__*`, not `mcp__swarmcloud__*` — those are the
project server's. A permission rule is matched and not resolved, so the wrong
spelling grants nothing, silently, and the session behaves as though delegation
does not work. `delegate` therefore lists **both** spellings of all eighteen
tools, and `tests/unit/mcp/test_plugin_skills.py` holds the two lists in
lockstep and derives the scoped prefix from `plugin.json` itself.

### What is still repository-bound, and why

**The MCP half is not.** Since 0.4.1 the server is fetched by uv rather than
found on disk, so it starts from any working directory. Outside a checkout it
has to be told where a team deployment's front door is: `API_HOST` or
`SWARM_API_URL`, as *Where the API is, outside a checkout* explains above. Before 0.4.1 this
README said the MCP half worked from anywhere, and that was false for every
marketplace install.

**The shell half is.** `${CLAUDE_PLUGIN_ROOT}` is exported to MCP server
subprocesses and **not** to commands Claude runs through the Bash tool, and
`uv run sc` resolves `uv`'s project against the session's own directory:
outside a checkout it answers that there is no `pyproject.toml`. That is a real limit,
stated here rather than papered over, because a model that meets it without
warning reports the platform as broken. `/sc` and the `sc` skill want the
repository, and the plugin's two descriptions say so. The `delegate` skill's
tools come from the MCP server and are unaffected. It also suggests two shell
commands, and those want the repository too: `uv run swarm tail`, to stream in
a background shell, and `uv run swarm doctor`, to diagnose.

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
authorised", one `roles/iap.httpsResourceAccessor` grant from working. That
grant is `frontend_iap_members` in `terraform/bootstrap/terraform.tfvars` —
moved out of `terraform/infra` on 2026-09-24, applied by the owner rather than
by CI — and `swarm-verify` was added to it the same day. So on a
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

Four files, all in `make test`, all offline — bar one test, which CI runs:

`tests/unit/mcp/test_plugin_bridge_install.py` covers whether the server can
**start** on a machine that has only the plugin: the declaration names nothing
outside `plugin/`, the bridge is a named git requirement whose
`#subdirectory` really is the package that declares `swarm-mcp`, its ref is
`sc-v<version>` and is written down once, and every in-repository dependency
resolves from the same commit. Its last test installs the bridge for real —
`plugin.json`'s own command, at the pushed commit, from a cold cache, outside
the checkout — and talks MCP to it; it is skipped unless
`SWARM_BRIDGE_INSTALL_REF` is set, which only CI's
`the plugin's bridge installs from git` step does. That test also runs each
install's own interpreter to ask where it thinks its repository is. The git
install must find none, and must still class an `https` front-door URL as the
front door. The escape hatch must find this checkout and its `frontend_hostname`.
The same file holds both descriptions to naming what still needs a checkout.

`tests/unit/mcp/test_bridge_outside_a_checkout.py` holds the same two facts
offline, against a simulated uv-cache layout: which door an address is, and
which checkout an install reads, when the bridge is not running from a
checkout.

`tests/unit/mcp/test_plugin_skills.py` parses every `SKILL.md` here and asserts
that each tool named in `allowed-tools` or in the prose is one
`swarm_mcp.server.TOOLS` actually serves, that `sc` never gains a tool that
writes, and that the skill never tells a session a tool which exists is missing.

`tests/unit/mcp/test_plugin_commands.py` covers the half that had nothing: the
**shell commands**. Every `swarm` or `sc` subcommand named in a skill, a command
file or this README must be one the argparse parsers really accept, nothing the
bridge returns may tell a model to run a bare `swarm`, and the marketplace
manifest must exist and agree with `plugin.json` about this plugin's name.
