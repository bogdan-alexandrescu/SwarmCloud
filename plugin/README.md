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

In a Claude Code session:

```text
/plugin marketplace add bogdan-alexandrescu/SwarmCloud
/plugin install sc@swarmcloud
```

You are prompted for the deployment URL (required), the OAuth client ID and
the OAuth client secret. The secret is `sensitive` in the manifest, so Claude
Code keeps it in your system's secure credential store, never in
`settings.json`. Then:

```text
/reload-plugins
/mcp                # plugin:sc:swarmcloud should say connected
```

The server fetches the bridge from GitHub at the tag `sc-v<version>` (see
*The bridge ships with the plugin*, below). **Between merging a version and
pushing its tag, that tag does not exist and the server cannot start** — uv
reports that it cannot find the ref. Until then, start Claude Code with the
escape hatch, `SWARM_MCP_FROM=<checkout>/apps/swarm-mcp claude`.

Then sign in, from a terminal:

```bash
uv run sc login    # in a checkout; a browser window opens; pick your work account
uv run sc whoami   # context, URL, you, your tenant
```

Outside a checkout, `sc` runs from the same package the server does:
`uv tool run --from 'swarm-mcp @ git+https://github.com/bogdan-alexandrescu/SwarmCloud@sc-v<version>#subdirectory=apps/swarm-mcp' sc login`.

`sc login` finds the deployment because the plugin's MCP server writes it to
your config file when it starts — so start the server first (the `/mcp` line
above), or add it yourself with `sc context add`. `sc login` ends by calling
the API once **with the sign-in it just made**, whatever else is set on the
machine, and warns you if `SWARM_IMPERSONATE_SA`, `SWARM_ID_TOKEN` or a GCP
metadata server means every other command will still act as a service account
rather than as you.

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

**Where the API is, outside a checkout: wherever you configured it.** The
deployment URL the install asked for reaches the bridge through the server's
environment (`SWARM_PLUGIN_DEPLOYMENT_URL`, from `${user_config.deployment_url}`),
and that one value decides both the address and the credential — see
*Where the API actually is*, below. The bridge never reads this repository's
Terraform to find it, installed from git or not, unless developer mode
(`SWARM_MCP_CONFIG_FROM=repo`) says to.

The environment still overrides the plugin, in the order
[docs/plugin-setup.md](../docs/plugin-setup.md) gives, for CI and for anyone
who exports one in the shell Claude Code starts from:

* **`SWARM_URL=https://<address>`**, or the older `SWARM_API_URL`. An `https`
  address that is not `*.run.app` is taken to be the IAP load balancer: nothing
  else in this platform serves the API over https under another name. Without
  that rule, which #62's first version lacked, the bridge presents a Google ID
  token for the URL, and IAP refuses that as `Invalid JWT audience`. A
  `*.run.app` address still gets an ID token.
* **`API_HOST=<hostname>`** (or `SWARM_API_HOST`) declares the front door.
* **`SWARM_REPO_ROOT=<a checkout>`** names the checkout whose tfvars developer
  mode reads. It does nothing outside developer mode.

At a front door the token also has to be one IAP admits. A user's own gcloud
access token is refused with 401, IAP error code 900, which is why `sc login`
exists; CI sets `SWARM_IMPERSONATE_SA`.

**With nothing configured at all**, the bridge says what to configure — the
plugin's deployment URL, `sc context add`, or `SWARM_URL` — instead of
guessing. It no longer falls back to gcloud's configured project: that
deployment was one the user never named. `PROJECT_ID` still selects the old
solo path (`gcloud run services proxy` against that project's `swarm-api`).

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
* **In developer mode it reads the tfvars of the checkout it was built from**
  (`SWARM_MCP_CONFIG_FROM=repo`; without it, no tfvars are read at all, and
  the plugin's configured deployment is used). The install is not
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
reaches the deployment the plugin was configured with, as *Where the API is,
outside a checkout* explains above. Before 0.4.1 this
README said the MCP half worked from anywhere, and that was false for every
marketplace install.

**The shell half is.** `${CLAUDE_PLUGIN_ROOT}` is exported to MCP server
subprocesses and **not** to commands Claude runs through the Bash tool, and
`uv run sc` resolves `uv`'s project against the session's own directory:
outside a checkout it answers that there is no `pyproject.toml`. That is a real limit,
stated here rather than papered over, because a model that meets it without
warning reports the platform as broken. `/sc` and the `sc` skill want the
repository, and the plugin's two descriptions say so. The `delegate` skill's
tools come from the MCP server and are unaffected. The shell commands the
bridge hands back -- `follow_live_with`, a sign-in hint, the `swarm doctor`
command a tool's error ends with when the request never reached the API -- are
spelled for the install the bridge runs from, so they need no checkout (see
*Where the API actually is*, below). The delegate skill names no launcher of
its own: it tells the model to run each command exactly as the bridge spelled it.

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

**A command the bridge hands back is spelled for the install it is running
from**, by one function (`swarm_mcp.invocation.terminal_command`), so it runs
where the bridge runs and reaches the same version of it:

| The bridge is running from | A command reads |
|---|---|
| a checkout of this repository (its `.mcp.json`, or a shell inside it) | `uv run swarm tail <id>` |
| `uv tool install`, with that install's `swarm` on your PATH | `swarm tail <id>` |
| the plugin, with nothing installed | `uv tool run --from 'swarm-mcp @ git+https://github.com/bogdan-alexandrescu/SwarmCloud@sc-v<version>#subdirectory=apps/swarm-mcp' swarm tail <id>` |
| the escape hatch, `SWARM_MCP_FROM` | `uv tool run --from <that value> swarm tail <id>` |

The plugin-only row is rebuilt from the install's own record of where it came
from, so it names the same tag the server was started with. Until 0.5.2 every
row read `uv run`, which outside a checkout answers `Failed to spawn: swarm`
(#189). A `swarm` on your PATH from some other install is not used: it is
installed on its own and can be another version. Run what the bridge hands
back rather than retyping it.

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

**Beside the prompt, a caller may send only the inputs a profile declares.**
`mock` declares its test knobs — `sleep_seconds`, `steps`, `fail`,
`artifact_text` and the rest, listed by `swarm_profiles` under `inputs` — so a
step can sleep long enough to be cancelled, or fail on purpose: `inputs` on a
`swarm_dispatch` call or a `swarm_workflow` step, `--input sleep_seconds=120`
on `swarm dispatch`, `"inputs": {...}` on a step in a `swarm workflow` spec.
`{"quota_exhausted": true}` parks a mock step once, on a simulated rate limit
with the `retry_after_seconds` you give it, and the attempt after the park
finishes: the mock parks the task's first attempt only, counted by the task's
own `attempt_count`, so the bound holds even when the park's checkpoint fails
to upload. Before 0.5.3 the rate limit fired on every attempt, a park does not
spend one, and the step parked until cancelled, which is why 0.5.2 withheld
both keys. `exit_code` refuses the codes the worker reads as a success, a rate
limit, a refused credential and a cancellation. Every key's kind and bounds
are in the table below, which is generated from the catalogue rather than
restated here. `claude-code` and `codex` declare none and take only the prompt.
A key the profile does not declare is refused by name, never dropped, and
never an image, a command, a resource spec, a backend or a model:
`input.model` is read by the CLI runners, and a caller setting it would be
choosing the model a `claude-code` agent runs. The declarations are the frozen
catalogue's own, `RunnerProfile.inputs` (contract request 25), and for a
profile that declares, the API refuses an undeclared key with 422
`invalid_input` whoever sends it, so the bridge's refusal is only the earlier
of two identical answers. **`browser` and `generic` have not declared their
inputs yet** ([#218](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/218)):
each runner's work is its input (a url or actions, a command name), and
nobody has decided which keys they take. The bridge sends them none; the API
bounds what any other caller sends them by size alone, as it bounded every
profile before 0.5.3.

What `mock` declares, as `swarm_profiles` lists it:

<!-- runner-inputs:mock generated from RUNNER_PROFILES["mock"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the mock runner does with it |
|---|---|---|
| `sleep_seconds` | number 0..3600 | how long the run sleeps, in total |
| `cpu_burn_seconds` | number 0..3600 | how long it burns CPU, in total |
| `steps` | integer 1..1000 | how many progress files, and checkpoints, it writes |
| `fail` | boolean | fail on purpose, after the steps |
| `fail_message` | string | the error a failure reports |
| `exit_code` | integer 1..255 except 77, 78, 143 | the exit code a failure uses |
| `artifact_text` | string | what the output artifact holds |
| `artifact_name` | filename | the output artifact's file name |
| `quota_exhausted` | boolean | park the first attempt on a simulated provider rate limit; the next one runs |
| `retry_after_seconds` | integer 1..3600 | the retry-after that simulated rate limit reports |
<!-- /runner-inputs:mock -->

## What keeps these honest

Five files, all in `make test`, all offline — bar one test, which CI runs:

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
front door. The escape hatch must find this checkout and, in developer mode, its
`frontend_hostname`.
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

`tests/unit/mcp/test_runner_input_prose.py` holds the runner-inputs table above,
and the one in `docs/workflows.md`, equal to the frozen catalogue, fails when a
sentence here, there or in the delegate skill states a bound the table does not
own, and requires every example input to be one the catalogue accepts.
