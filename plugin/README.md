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

The agents in `plugin/agents/` do **not**: each lists ONLY the plugin's scoped
names (`mcp__plugin_sc_swarmcloud__*`). A skill's list is a permission grant,
where a second spelling costs nothing; an agent's `tools` is the set of tools
it can call, and in this repository's own checkout both servers load — Claude
Code merges two servers only when their commands are identical, and
`uv tool run --from …@sc-v<version>` is not `uv run --directory . swarm-mcp`.
The two are not the same program: the checkout's server runs whatever branch
is checked out, and reads its deployment from the tfvars, while the plugin's
runs the pinned tag against the deployment in its settings. An agent granted
both would call either, per call — `sc:workflow` could submit through one
deployment and `sc:step` follow through the other, reading 404 on every task —
and a stale checkout bridge silently drops arguments it does not know, such as
`strategy`. Listing only the plugin's own server means a plugin agent runs
exactly where its plugin's server runs, and when that server is down it fails
loudly rather than falling through to another one: Claude Code refuses to
launch a subagent none of whose `tools` resolve, and each agent's instructions
say what to answer when its SwarmCloud tools are missing. The scoped form is
`mcp__plugin_<plugin>_<server>__<tool>`; a session with the Slack plugin
installed lists its tools as `mcp__plugin_slack_slack__*`, the same shape.
`tests/unit/mcp/test_plugin_agents_and_workflows.py` holds each agent to
exactly the scoped set it needs.

The bridge itself now refuses an argument it does not know, rather than
ignoring it, so a caller newer than the bridge it reaches is told so instead of
getting a task that quietly ran without the option it asked for.

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

### Acting as one of several tenants

A person in more than one registered tenant group acts as the **first**
matching group, in the order the admin registered them, unless they choose
another (#447). The choice is sent as `X-Swarm-Tenant` on every request, and
the API honours it only when your verified membership includes that tenant's
group: the header selects among your memberships, it never grants one. A
tenant you are not a member of is refused with 403 `tenant_not_member`, and
that refusal is shown as the API wrote it.

| Surface | List your tenants | Choose one |
|---|---|---|
| terminal | `swarm tenants` (`--json`), the current one marked `*` | `swarm --tenant <id> ...`, or `SWARM_TENANT=<id>`; the flag wins |
| MCP | `swarm_tenants` (read-only) | `SWARM_TENANT` in the bridge's environment, read once when it starts |

Nothing chosen sends no header at all, so behaviour is exactly what it was
before. The bridge's setting is per session, not per call: a session whose
calls could each name a tenant would read one tenant's rows and act on
another's. `sc` does not take a tenant yet; it acts as your default.

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
restated here. `claude-code` and `codex` declare one input, `issue`: the
number of a GitHub issue in the repository the step clones (contract request
28). The worker fetches its title, body and comments read-only into
`issue.md` in the workspace, beside the checkout and never among the
artifacts, and names that file in the prompt, so a prompt need not restate
the issue. A step that sends it with no repository is refused by the API.
A key the profile does not declare is refused by name, never dropped, and
never an image, a command, a resource spec, a backend or a model: the model a
`claude-code` agent runs is its Job's own `MODEL`, set in Terraform, and the
runner reads no `input.model` (#226). The declarations are the frozen
catalogue's own, `RunnerProfile.inputs` (contract request 25), and for a
profile that declares, the API refuses an undeclared key with 422
`invalid_input` whoever sends it, so the bridge's refusal is only the earlier
of two identical answers. **`browser` and `generic` declare too**, since
contract request 32 ([#218](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/218)):
`browser` takes a `url` and `actions`, and `generic` a required `command`
from its runner's catalogue, so the bridge sends a browser task its page and
steps, and refuses a private address, a missing `command` or an action its
shape does not take before anything is dispatched.

What each declaring profile takes, as `swarm_profiles` lists it:

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

<!-- runner-inputs:browser generated from RUNNER_PROFILES["browser"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the browser runner does with it |
|---|---|---|
| `url` | url | opened first, before any action |
| `actions` | list of 0..200, each object, `type` one of goto \| click \| fill \| press \| wait_for \| wait \| screenshot \| extract | run in order, after `url` |
| `actions` `goto` | `url` (required) url, `wait_until` string, one of load \| domcontentloaded \| networkidle \| commit | `url`: the page to open; `wait_until`: when the load counts as done; default load |
| `actions` `click` | `selector` (required) string 0..4096 | `selector`: the element to click |
| `actions` `fill` | `selector` (required) string 0..4096, `text` string 0..4096 | `selector`: the field to fill; `text`: what to type into it; default empty |
| `actions` `press` | `selector` (required) string 0..4096, `key` string 0..4096 | `selector`: the element to press a key in; `key`: the key; default Enter |
| `actions` `wait_for` | `selector` (required) string 0..4096, `timeout_ms` integer 1..300000 | `selector`: the element to wait for; `timeout_ms`: how long to wait; default the task's timeout_ms |
| `actions` `wait` | `seconds` number 0..60 | `seconds`: how long to pause; default 1 |
| `actions` `screenshot` | `name` filename, `full_page` boolean | `name`: the artifact's file name; default by position; `full_page`: the whole page, not the viewport; default true |
| `actions` `extract` | `selector` string 0..4096, `name` filename | `selector`: the element whose text is kept; default body; `name`: the artifact's file name; default by position |
| `timeout_ms` | integer 1..300000 | how long any one action may take; default 30000 |
| `launch_timeout_ms` | integer 1..180000 | how long Chromium may take to start; default 60000 |
| `viewport_width` | integer 320..3840 | pixels; default 1280 |
| `viewport_height` | integer 240..2160 | pixels; default 900 |
| `user_agent` | header 0..512 | the User-Agent sent; default Chromium's |
| `extract_text` | boolean | keep the final page's text as page.txt; default true |
| `screenshot` | boolean | keep a final full-page screenshot; default true |
<!-- /runner-inputs:browser -->

<!-- runner-inputs:generic generated from RUNNER_PROFILES["generic"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the generic runner does with it |
|---|---|---|
| `command` | string, one of make \| npm-build \| npm-ci \| npm-test \| pytest \| uv-sync, required | the platform catalogue entry to run; the platform owns its argv |
| `paths` | list of 0..32, each argument | pytest only: what to run; default everything |
| `target` | argument | make only: the target; default all |
| `working_directory` | argument | a directory inside the workspace to run in; default the workspace |
| `timeout_seconds` | number 1..3600 | lowers the command's wall clock |
| `grace_seconds` | number 1..20 | lowers the wait between SIGTERM and SIGKILL |
| `max_stdout_bytes` | integer 1..33554432 | lowers the stdout kept |
| `max_stderr_bytes` | integer 1..8388608 | lowers the stderr kept |
<!-- /runner-inputs:generic -->

What `claude-code` and `codex` declare, as `swarm_profiles` lists it:

<!-- runner-inputs:claude-code generated from RUNNER_PROFILES["claude-code"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the claude-code runner does with it |
|---|---|---|
| `issue` | integer 1..999999 | an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt |
<!-- /runner-inputs:claude-code -->

<!-- runner-inputs:codex generated from RUNNER_PROFILES["codex"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the codex runner does with it |
|---|---|---|
| `issue` | integer 1..999999 | an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt |
<!-- /runner-inputs:codex -->

`claude-code-review` is `claude-code` under its own Job and service account,
for the merge chain's review step (contract request 36, #295). It is
disabled until #295 is enabled, and declares what `claude-code` does:

<!-- runner-inputs:claude-code-review generated from RUNNER_PROFILES["claude-code-review"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the claude-code-review runner does with it |
|---|---|---|
| `issue` | integer 1..999999 | an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt |
<!-- /runner-inputs:claude-code-review -->

`indexer` is `claude-code` on `agent-runtime-indexer`, the image that carries
the repository index's toolchain (contract request 48, #625). swarm-api runs
index runs on it; it declares what `claude-code` does:

<!-- runner-inputs:indexer generated from RUNNER_PROFILES["indexer"].inputs; tests/unit/mcp/test_runner_input_prose.py fails when it differs -->
| input | kind and bounds | what the indexer runner does with it |
|---|---|---|
| `issue` | integer 1..999999 | an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt |
<!-- /runner-inputs:indexer -->

`--input issue=<number>` on `swarm dispatch`, or `"inputs": {"issue": <number>}`
on a step, points a `claude-code` step at an issue of its repository. The
issue's text is data for the agent: the worker scrubs it of every secret the
attempt holds and reads nothing in it as an instruction to the platform. The
tenant's forge token is used for the fetch in the worker's memory only, as for
the clone, and never reaches the workspace. A fetch that fails -- no such
issue, a pull request's number, a repository the token cannot read -- fails
the attempt before the agent starts, with `INPUTS_UNAVAILABLE`.

Every step's spec -- its prompt and inputs, profile, class, timeout, attempt
cap, repository, dependencies and the platform's own `metadata` blocks -- is
signed by swarm-api when it is submitted (contract request 34). A worker
that finds the stored spec no longer matches its signature, or finds it
unsigned outside the rollout window, ends the task FAILED with
`SPEC_SIGNATURE_INVALID` before the agent starts or any credential is read,
and does not retry it: another attempt would read the same rewritten
document. Every occurrence is either another agent rewriting the step or a
platform bug, and both are alerted on. Resubmit the work.

## A task's input comes back masked

The API serves a task's `input` and `metadata` masked at read time, to every
caller, the submitter included (owner decision, 2026-09-26, on #184), with a
count of what it masked. The bridge passes the count on and prints nothing of
the input itself: `swarm status` ends each line with `masked N`, `swarm result`
prints `masked 3  (input 2 · metadata 1)`, `swarm workflow-status` puts it on
each step, and `swarm_status`, `swarm_result`, `swarm_wait` and
`swarm_workflow_status` carry `masked: {input, metadata}`. A count the API did
not send is `—` in the terminal and `null` in JSON, never 0: that deployment is
older than the change and serves the input unmasked. The masking is at read
time only; the runner still reads what was submitted.

## What a task produced

A step that clones no repository still produces something: the files it wrote
into `$SWARM_ARTIFACTS_DIR`, and the runner's own summary. `swarm result`,
`sc task` and `swarm_result` (its `outputs` block) list each artifact by name
and size, the runner's summary, the exit code, the duration, and the inputs
staged into the step from upstream ones — before the code, which is "this task cloned
no repository" for such a step. They read the API's `/v1/tasks/{id}/artifacts`
route, whose `complete` tells "uploaded when the attempt ends" (the task has not
finished) from "no artifacts"; an unreadable listing is said, never shown as
none.

`swarm artifact <task> <name>` prints one of them, or writes it to a file
with `-o <file>`; `swarm_artifact` is the same read for a session. Both go through
`/v1/tasks/{id}/artifacts/content`: the name is matched against the task's own
manifest (a path is never accepted), the content is redacted at read time, and
a file larger than one window is read window by window from `next_offset` —
the terminal command prints all of it; the tool returns one window and says
where the next starts. An artifact that is not text is refused with a reason,
not served: nothing can scan its bytes for a credential.

`swarm tail` prints every event of every task it follows, each once. It read
the task's oldest 50 events on every poll, so it printed no event after the
50th (#164); it now reads newest first and pages back to the last event it
printed, one request per poll while it keeps up.

## Running a Claude Code workflow's steps in SwarmCloud

Owner decision, 2026-09-26: a Claude Code workflow shows as running in Claude
Code while every step executes in SwarmCloud. Claude Code has no hook that
replaces how `agent()` runs, so the plugin ships agents whose only job is to BE
the row: each one dispatches or adopts a SwarmCloud task, follows it, and
returns what it produced. `/workflows` then lists the row with its label,
phase, state and elapsed time, and its transcript shows the remote agent's
progress — narrated by the bridge, a line per thing the agent did.

There are two modes.

**One step: the `sc:remote` agent.** In any workflow script, give a step
`agentType: 'sc:remote'` and its prompt is what the remote agent is told:

```js
const notes = await agent('Survey apps/swarm-mcp for blocking calls and write up each one.',
  { agentType: 'sc:remote', label: 'survey-mcp' })

const fix = await agent('strategy: direct-pr\nFix the flaky test in tests/unit/mcp/test_x.py.',
  { agentType: 'sc:remote', label: 'fix-flake' })

// With a schema: give it room to say it failed, and catch the call.
const counts = await agent('Count the TODO markers per package.', {
  agentType: 'sc:remote',
  schema: {
    type: 'object',
    properties: {
      state: { type: 'string' },
      error: { type: ['string', 'null'] },
      counts: { type: ['object', 'null'] },
    },
    required: ['state', 'error', 'counts'],
  },
}).catch((error) => ({ state: 'ROW_FAILED', error: String(error), counts: null }))
if (!counts || counts.error) log('TODO count did not come back: ' + (counts ? counts.error : 'row stopped'))
```

It dispatches the prompt as ONE `claude-code` task on this session's
repository and pushed branch, with strategy `collect` — or `direct-pr` when
the prompt's first line is `strategy: direct-pr`, which it removes. It follows
the task until it finishes and returns the remote agent's answer. When the call
passes a `schema`, it appends one instruction to the prompt asking the remote
agent to END its answer with a JSON object matching that schema, and returns
the object the bridge parses out of the answer.

**When the task does not succeed, nothing is invented — and in schema mode
that means the call can THROW.** Without a schema, the row's answer is a short
failure report: the state, the last error and the task id. With a schema,
Claude Code requires a schema-valid object and asks the agent again, up to five
times; if it never gets one, the `agent()` call fails with an error ("If the
subagent's output still fails validation after five attempts, the call fails
with an error" — Claude Code's workflow docs, read 2026-09-25). `sc:remote` is
told never to build an object just to pass validation, because `{counts: {}}`
would read as "no TODOs" rather than "the count failed". So:

* **Give the schema a state and an error** (`state`, and `error` or
  `last_error`) and make every other required property nullable, as above.
  Then a failed step comes back as an object — `state` the task's end state,
  `error` its last error, everything else `null` — and the call does not throw.
* **Otherwise the call throws**, which aborts a script that does not catch it.
  Wrap a schema-mode `sc:remote` call in `.catch()` or `try`, or run it inside
  `parallel()` or `pipeline()`, which turn a thrown call into `null`.
* **A stopped row resolves to `null`**, like any agent, whatever the schema.

A row is `null` or a failure report in these cases, never the remote agent's
answer: the dispatch was refused (an unpushed branch, say), the task ended
`FAILED`, `CANCELLED` or `DEAD_LETTERED`, the remote agent's answer did not end
with the requested object, the task could not be read (a 404 or 403, or three
calls in a row that read nothing), or the sc plugin's server is not connected.

**A whole SwarmCloud workflow: `/sc:swarmcloud`.** Its argument is a SwarmCloud
workflow spec — the same object `swarm workflow` reads — as an object, as JSON
text, or as the path of the spec file, relative to the session's checkout. A
workflow script has no filesystem, so given a path one `sc:workflow` agent
(label `read spec`) has the bridge read the file with `swarm_workflow_spec`,
which checks it as `swarm_workflow` would, submits nothing and returns the spec
with its digest; the script submits the relayed spec only when it digests to
what the bridge read. Text that begins like JSON and does not parse is reported
as broken JSON, not looked for as a file. One `sc:workflow` agent then
submits the spec (phase `Submit`), and SwarmCloud owns the DAG from then on:
dependencies, `input_from` staging, `on_step_failure`, retries. The script
starts one `sc:step` row per step, labelled
`[SwarmCloud] <spec label or workflow id> · stage <n> · <step_id>` (cut at 80
characters from the name, so the stage and step survive), under phase
`Level N` — its depth in the DAG — or under the step's `stage` when the spec
gives one (`stage` is display-only and never sent). The submit and result rows
carry the same prefix. A row follows its own task only, so it may start before
its parents finish; it then says it is waiting, and why, in its first line,
and a waiting task holds no capacity. Each finished step is one narrator line, `scan-03 SUCCEEDED · 4m12s · $0.21 · PR #231 · produced findings.md`,
which names the artifacts the step produced, or says `produced no artifacts`
for a success that made none.
The run returns every step's result and the workflow's state as
`swarm_workflow_status` reads it — derived by the server, never by the script.

**What crosses the relay is checked.** A workflow script cannot call a tool, so
the spec reaches `swarm_workflow` through the `sc:workflow` agent, which
retypes it, and the step-to-task map comes back the same way. `run.js`
computes the spec's digest (`fnv1a32:` over its sorted-key compact JSON — the
same function as `swarm_mcp.workflows.spec_digest`, held equal by a test) and
the agent passes it beside the spec; `swarm_workflow` submits NOTHING when the
spec it received digests differently. The reply must carry the digest back,
every step of the spec and no other, each step's `depends_on` as the spec has
it, and a distinct task per step — or no row starts and the run returns
`SUBMITTED_UNVERIFIED` with the workflow id and what differed. Each `sc:step`
row passes its `step_id` to `swarm_follow`, which will not follow a task that
is a different step.

**How `/sc:swarmcloud` can end before any row starts**, and what each means:

| `state` | What happened | What to do |
|---|---|---|
| `NOT_SUBMITTED` | `swarm_workflow` refused the spec, with `error`; or, given a path, the file could not be read as a spec, or the spec relayed from it does not digest to what the bridge read; nothing was sent | fix what `error` names and run again |
| `SUBMISSION_UNKNOWN` | the Submit row stopped, failed, or answered with neither an id nor an error — possibly AFTER the workflow was created | look for it in the console's workflow list before running again: the API has no idempotency key, so a second run submits a second copy |
| `SUBMITTED_UNVERIFIED` | SwarmCloud accepted workflow `workflow_id`, but the reply relayed back does not match the spec | it runs regardless: read it with `swarm_workflow_status`, or cancel it with `swarm_workflow_cancel` |

A Result row that fails does not lose the steps: the run returns every row with
`state` null and a `state_note` saying why.

All four agents run on **haiku at low effort** (`model` and `effort` in their
frontmatter), load no `CLAUDE.md`, and can call only the SwarmCloud tools they
need — `sc:remote` dispatch and follow, `sc:step` follow, `sc:workflow` read a
spec file, submit and read — and only through the sc plugin's own server (above).
`sc:wait` holds no SwarmCloud tool: it is the pause between two tries of a step
row whose bridge was not connected or not responding, one `sleep 30` through
Bash, because a workflow script has no timer of its own. A session that asks
before running Bash will ask for it; refused, the next try comes at once and
the run logs that the wait could not be made.

### Every running workflow, shown without asking for it

Owner decision, 2026-10-02: a workflow running in SwarmCloud shows in Claude
Code as live `[SwarmCloud]` rows without anyone attaching it by id. Three
pieces do it, and none of them submits anything.

* **The list.** `swarm_workflows` (MCP) and `uv run sc workflows` (in a
  checkout) read `GET /v1/workflows?state=...` for the caller's own tenant
  and keep only the workflows that are not finished — every state outside
  `SUCCEEDED`, `FAILED`, `CANCELLED` and `DEAD_LETTERED`, plus `UNKNOWN`. Each
  comes with its `workflow_id`, its spec's `label`, its state, its current
  (unfinished) steps with their states, its age, and the console link the API
  served. The route filters on the state it DERIVED from the steps, never the
  stored cache, and the bridge filters again so a deployment older than the
  filter still lists only running ones. The 20 newest are read step by step;
  any more are listed with `steps_unread_because`.
* **`/sc attach --all`** runs `/sc:swarmcloud` with `{attach: "all"}`: one
  `sc:workflow` row lists them (`LIST`), and each of the newest **10** is
  attached exactly as `/sc attach <wf_id>` attaches one — finished steps said
  once, one slim `sc:step` row per unfinished step, all workflows at once. The
  cap is 10 because every unfinished step is an agent of its own in the
  session: ten workflows of three to five steps is already 30–50 rows, and past
  that `/workflows` is unreadable and watching costs more than the work. The
  rest are logged and returned as `not_followed`, each with the `/sc attach`
  that follows it. The run ends `ATTACHED`, `NOTHING_RUNNING`, or
  `NOT_ATTACHED` with the list's error verbatim; one workflow that cannot be
  attached is `NOT_ATTACHED` on its own and does not stop the others.
* **The SessionStart hook** (`hooks/hooks.json`, `hooks/session-start.sh`).
  On a new or resumed session where the plugin is configured, it runs the
  plugin's own bridge — the same `${SWARM_MCP_FROM:-<pin>}` the MCP server
  runs, read out of `plugin.json` — as `sc workflows --session-start`, and when
  workflows are running it hands the session `additionalContext` naming them
  and telling it to run `/sc attach --all` first. A hook cannot start a
  workflow itself; this makes the attach the session's first action. It is
  read-only, gives up after 10 seconds (`SWARM_SESSION_START_TIMEOUT`), and is
  silent and exits 0 when none run, when the bridge or the API fails, and when
  the plugin's **`auto_attach`** option is off (`/plugin configure
  sc@swarmcloud`; on by default). The first session after a new plugin version
  may get no notice while uv fetches the new bridge; `/sc attach --all` works
  by hand at any time.

### What a row shows, and what it costs

A row writes one short line when its task's state or progress changes, and
nothing otherwise:

    [review 4674b39f] READY · waiting 3m · waits: dependency
    [review 4674b39f] READY → RUNNING
    [review 4674b39f] RUNNING · 4m10s · attempt 1/3 · checkpoint 1m ago · 210k tok · $0.31

That is the state, the elapsed time, attempt n/m, the age of the last
checkpoint, and the tokens and cost recorded so far. For a waiting task it
says what the task waits for: a dependency, capacity, quota, and so on. A
figure that is not known is one word: `unrecorded`, `unreadable` or `none`.
It is never estimated, and an unrecorded cost is never `$0`. When the task
finishes, the row answers with the same seven fields as before: `state`,
`answer_excerpt`, `cost_usd`, `duration_s`, `pr_url`, `artifacts` and
`last_error`.

**Why it is this thin (measured 2026-10-01).** Until then each row followed
`swarm_follow` with `format: "lines"` and `wait_seconds: 90`. Six wave-1
workflows of three steps each made eighteen rows. Every reply carried 5–16 KB
of the remote agent's narrated log, and a row re-reads its whole transcript
on every turn. One row used 4.0M tokens over 41 calls, and the six workflows
used about 22M tokens. Rows whose parents had not started polled the whole
time. Yet what the workflow acts on is one terminal line per step.

A row now follows `format: "progress"` instead:

* **No log.** A reply for one unfinished task is a few hundred bytes, bounded
  at 768 by a test, and it repeats nothing an earlier reply said.
* **One long call per turn.** A running task is followed with
  `wait_seconds: 120`. A task still waiting on its parents is followed with
  `wait_seconds: 600`. The bridge holds the call until the state, attempt or
  wait reason changes, or until the wait ends. While a task waits, the bridge
  reads only the task document, at most every 30 s, with no log, event or
  attempt reads.

The full log is still there. `swarm_follow` with `format: "lines"` narrates
it (`sc:remote` still uses that), and so do `swarm tail`, `swarm follow
<task_id>` and the console.

### What differs from a local step — read before swapping one in

| | A local `agent()` step | The same step through `sc:remote` or `/sc:swarmcloud` |
|---|---|---|
| What it knows | the prompt, the session's files, its own tools | **only its prompt**. No conversation, no other step's output — except, under `/sc:swarmcloud`, the `input_from` files SwarmCloud stages |
| Its prompt | handed to the agent as written | **retyped by a relay** — `sc:remote`, a haiku row, copies the prompt into `swarm_dispatch`, and a long prompt can arrive changed, with nothing after it able to tell. The prompt the remote agent got is the task's input: read it in the console, or in the row's transcript. Under `/sc:swarmcloud` the spec is checked by digest (above) |
| Which code it sees | the working tree, uncommitted edits included | a **depth-1 clone of the pushed branch**, cloned by the branch's OWN name on its remote — never its upstream: no history, no uncommitted work. The bridge refuses a branch that is not on its remote under its own name or has commits that are not there, naming `git push -u <remote> <branch>`, and names uncommitted changes as invisible |
| Tools | the session's tools, MCP servers and permission rules | the `claude-code` runner's own tools inside its container; none of the session's MCP servers or permission rules |
| Model | the workflow's `model` option, or the session's | **pinned on the job**: the profile's model. A caller cannot choose it (invariant 10), so a `model` option on the `agent()` call changes only the local row's model |
| Tokens in `/workflows` | the step's own | **the row's** — haiku relaying the remote run. The remote agent's spend is the outcome's `cost_usd`, drawn from the shared subscription pool; `null` means not recorded, never $0 |
| Stopping the row | stops the step | stops the ROW only. The SwarmCloud task keeps running; cancel it with `swarm_cancel`, or `swarm workflow-cancel` for a workflow |
| Relaunching the run | re-runs agents that did not finish | the same, and for `sc:remote` a re-run row DISPATCHES AGAIN — a second task. Under `/sc:swarmcloud` the finished `Submit` is replayed from cache, so rows re-follow the same tasks |
| Concurrency | the workflow's agent cap | rows beyond the cap start later; their tasks run on SwarmCloud's schedule regardless |

**Which checkout is inferred.** The bridge reads the git checkout of the
directory its MCP server was started in — the session's working directory at
start. A session that later moved into a worktree can point it elsewhere with
`SWARM_CHECKOUT_DIR` in the environment Claude Code starts from. Outside any
checkout nothing is cloned, and the dispatch reply says so rather than leaving
a null.

**Inference is opt-in: `infer: true`, not the default.** A plain
`swarm_dispatch` or `swarm_workflow` call names a repository only when given,
exactly as before this feature existed. `sc:remote` and `/sc:swarmcloud` pass
`infer: true` on every call; anything else naming neither `repo` nor `infer`
clones nothing.

**Which branch is checked: its own name, never its upstream.** This
repository's lane recipe, `git checkout -b <lane> origin/main`, leaves the
lane's upstream at `origin/main`. The bridge ignores the upstream for this: it
checks `<remote>/<lane>` — the remote being the one `git push` would use.
A lane pushed without `-u` is accepted; a lane never pushed is refused with
`git push -u origin <lane>`. The upstream is only reported, in the reply's
`repository.notes`. (Reading the upstream instead once measured a pushed lane
against main, refused it, and recommended `git push origin <lane>:main`.)

**What is actually SENT is the commit, not the branch name.** A branch name is
a moving pointer; a remote task can sit QUEUED for a while, and a caller who
pushes again to the same branch before it starts must not silently move what
an already-sent dispatch clones. So `infer: true` sends the commit the branch
was pushed AT, pinned, and the reply's `repository.commit` and `repository.ref`
are the same value.

### What the bridge gained for it

* `swarm_dispatch` takes `strategy` (`collect` | `direct-pr`) and `infer`, and
  with `infer: true` and neither `repo` nor `ref` given, infers both from the
  checkout: the ref sent is the commit the checkout's pushed branch is pinned
  at (never the branch name, which can move after the call returns), the URL
  is made https and stripped of any credential, and the reply carries a
  `repository` block saying what will be cloned and how that was decided.
* `swarm_follow` takes `since` — the cursor as one opaque token, ending in a
  dot and a CRC-32 checksum: a relay copies it by hand on every call, and a
  token changed on the way back is refused with an error saying so, instead
  of being read as a different position — and
  `format: "lines"`: claude-code's `stream-json` narrated as short lines,
  capped per call with the count left out stated, a `wait_seconds` window that
  returns early when a task finishes or starts, and, for a finished task, an
  `outcome` with the whole answer (from the API's `/v1/tasks/{id}/answer`,
  which reads the agent stream's final `result` event, because the runner's
  summary keeps only its first 2000 characters), `answer_json`, `cost_usd`,
  `duration_s`, `pr_url`, `artifacts`, `last_error` and, on a failure,
  `failure`. The narrated stream is the AGENT's own (`agent_stdout`), not the
  runner's JSON log; a runner with no agent CLI, such as `mock`, is read from
  its own streams instead. The reply says `stop: true` when calling again
  cannot change anything: every task finished, or the row gave up on one — a
  404 or 403 at once, any other failed read after three calls in a row that
  read nothing (the streak rides in `since`), or, given `step_id`, a task that
  is a different step — with that task's `abandoned_because`. A row that can
  never read its task used to poll it until its turn limit: about 400 windows
  of 90 seconds.
* `swarm_workflow` takes `spec`, a whole `swarm workflow` spec, read by the
  same function the terminal uses, and infers the repository the same way.
  With `spec_digest` it refuses, before sending, a spec that arrived
  different; its reply carries the digest of what it received.
* `swarm_workflow_spec` reads a spec file from the checkout, checks it, and
  returns it with its digest, submitting nothing — the half of `/sc:swarmcloud` that
  takes a path. A file that is not a spec is refused without its content being
  repeated.
* `swarm_follow` `format: "progress"` (2026-10-01): no log, one progress line
  per task only when it changed, the state `transitions` since `since`, and a
  finished task's `outcome` reduced to the step result's fields. A call holds
  for up to 600 s (see "What a row shows, and what it costs" above).
* Every tool refuses an argument it does not declare, instead of ignoring it.
* The stdio loop answers tool calls concurrently. It answered one at a time,
  so a single `swarm_wait` held every other call, and a dozen rows each
  following their task for a window would have queued behind one another.

None of it adds a model, an image, a command or a resource parameter.

## Planning and running a GitHub issue

`/sc run --issue owner/repo#N` (#454) hands an issue to SwarmCloud. A planner
task reads the issue and the repository's open issues and pull requests, with
the tenant's own forge credential, and writes a plan: the requirements, any
overlap with work already in flight, and the steps with their files, tests and
estimates. The plan waits, holding no capacity, until someone approves it;
then it compiles into one workflow that opens a pull request, and the platform
reads that pull request's CI and runs up to three fix rounds (`--fix-rounds`).
The issue carries a plan comment and one status comment the platform keeps up
to date. Why it works this way -- the digest, the tick, the fix-round cap,
`Closes` against `part of`, and why auto-merge is refused -- is in
[docs/issue-runs.md](../docs/issue-runs.md).

| Surface | Create | Read | Act on the plan |
|---|---|---|---|
| `/sc` | `/sc run --issue o/r#N [--plan auto] [--auto-merge]` | `/sc runs`, `/sc plan show <run>` | `/sc plan approve\|edit\|reject <run>` |
| MCP | `swarm_run_issue` | `swarm_runs`, `swarm_run` | `swarm_plan_approve`, `swarm_plan_edit`, `swarm_plan_reject` |
| terminal | `uv run sc run --issue o/r#N [--follow]` | `uv run sc runs`, `uv run sc run show <run>`, `uv run sc plan show <run>` | `uv run sc plan approve <run>`, `edit --file plan.json`, `reject --reason ...` |

**An approval sends the digest of the plan it showed.** `sc plan approve`
prints the whole plan and its digest and asks for `approve` typed back
(`SWARM_ASSUME_YES` is ignored), and `swarm_plan_approve` takes the digest
from its caller, who was shown it. A plan edited in between is refused with
`plan_changed`, said plainly, and nothing is retried. `sc plan edit` sends the
digest of the plan it opened, from `--file` or `$EDITOR`.

**`--auto-merge` is visible but disabled.** It is accepted and passed to the
API, which refuses it until #295 lands; the refusal is printed as it is, and
nothing is created.

**Live rows.** Once a run is `RUNNING` (or `FIXING`, a CI fix round),
`swarm_run` answers `attach_with: /sc attach <workflow_id>` and `sc run show`
prints the same line: the run's workflow attaches exactly as any other
(#448). `sc run --follow` prints one row per compiled step as it moves, and
stops at a plan waiting for approval.

`run --issue` and `plan approve|edit|reject` write, so no skill or command is
granted them: `tests/unit/mcp/test_plugin_commands.py` holds them out of every
grant, and `tests/unit/mcp/test_issue_runs.py` holds the digest rule.

## What keeps these honest

Eight files, all in `make test`, all offline — bar one test, which CI runs:

`tests/unit/mcp/test_follow_progress_format.py` holds the row's cost down:
`format: "progress"` returns no log line and stays under 768 bytes for a task
with large logs, repeats nothing on an unchanged call, and for a step waiting
on its parents reads only the task and holds one call for the whole wait. It
also checks that a finished task still carries every field of the step result.

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

`tests/unit/mcp/test_plugin_agents_and_workflows.py` covers the agents and
`/sc:swarmcloud`, whose loader fails quietly: a plugin agent whose frontmatter does not
parse loads with every field ignored, and `mcpServers`, `permissionMode`,
`hooks` and `initialPrompt` do nothing in a plugin agent. Each
`plugin/agents/*.md` must parse strictly — and to the same values under a real
YAML parser (PyYAML), so the file is not merely consistent with the test's own
grammar — use only the keys a plugin agent honours, pin haiku at low effort,
and grant exactly the SwarmCloud tools it needs, under the plugin's scoped
names only. `run.js` must open with a pure-literal `meta` and read only the
workflow globals — and it is RUN, under node with those globals stubbed, to
prove it submits once, starts one `sc:step` row per step by level or stage,
narrates each step in one line and returns the state SwarmCloud derived; that
its spec digest is byte-for-byte the bridge's; that a reply missing a step,
changing a dependency, reusing a task or carrying the wrong digest starts no
row; that a stopped or failing Submit is reported as UNKNOWN, not as not
submitted; that given a spec file's path it has the bridge read the file and
submits nothing unless the relayed spec digests to what the bridge read; that
each step's line names what it produced; that a failing Result row keeps
every step's result; that every row is labelled `[SwarmCloud] … · stage <n> ·
<step>` within 80 characters; and that `sc:step` follows `format: "progress"`,
never `"lines"`, with its seven-field result unchanged. CI fails
rather than skips when node is missing. Claude Code's own frontmatter parser
and workflow runtime are not run by any of this.

`tests/unit/mcp/test_remote_steps_bridge.py` covers what those agents need from
the bridge: the inferred repository and every refusal of an unpushed branch —
including a lane branched from `origin/main`, pushed without `-u` and not
pushed at all — the dispatch `strategy`, `since`, the narrated lines, the
gathering window and the outcome against the real logs, events and attempts
routes, a row that stops on a task it cannot read or that is another step, the
spec-shaped workflow submit and its digest check, the refusal of an undeclared
argument, and a stdio loop in which one blocked call does not hold the next.
