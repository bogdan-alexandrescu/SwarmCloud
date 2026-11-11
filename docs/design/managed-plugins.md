# Managed plugins: any Claude Code plugin added, configured, updated and removed from the console

**Status: design, 6 November 2026. Nothing here is built.** Part of the Day One install program. It extends
[agent-plugins.md](agent-plugins.md) (the per-step `plugins` field, as built) from a catalogue written in code and
baked into the images to a catalogue that admins manage from the console. Every fact below was read on `main` at
`33861db` on 6 November 2026; files are named, lines are not, because they move. Every question that is the owner's
to answer is in the lane's `questions.json` (Q1-Q20 below) with options and a recommendation; where this document
depends on an answer it says "recommended, pending Qn" and never treats it as decided.

**Revision, 9 November 2026.** On 6 November 2026 the owner decided Q13 and Q15 against this document's
recommendations: gstack and security-audit **migrate to the managed path** with a build step that publishes a new
image version bundling them (§1.4), and Admin › Setup offers an **optional** "add a plugin" step (§15). This revision
rewrites §1.4, §15, the lane plan (§17, lanes 13-23) and the questions (§18, Q16-Q19), read on `main` at `6e150ec`.
Lanes 1-12 have since merged in part (`pluginstore.py`, `pluginload.py`, `AdminPlugins.tsx` exist); the rest of the
document is not re-audited here, and where it still says "built-in" it means the two bundled installs of §1.4.

**Revision, 11 November 2026.** After an independent security review of the bootstrap rows that would have let
swarm-api start Cloud Build builds, the owner decided that each bundle build runs as **one Cloud Run job execution**
under the builder account, with swarm-api allowed to run only that job, granted on the job itself; the Cloud Build
rows are reverted. This revision changes §1.4.2 (the job, its caller, its grants, and the review's six findings),
§1.4.3, §1.4.4's upgrade rebuild, §1.4.7, §13, §15.2, lanes 14-16 and Q16; every other decided answer stands.

## The ask

The owner (the owner) asked on 6 November 2026, at about 01:18 and 01:20 UTC, for "a really well built and all inclusive
way to add plugins and skills directly from the Console UI", "super generic so it supports any plugin and allows
customization where needed depending on the plugin", "add and remove, basically fully managed plugins directly from
console", proven by installing and configuring jev-belay and jev-enforce. His answers, at about 01:30 UTC, which this
design follows:

1. **Who installs: both.** A platform admin installs into a **platform catalogue**, which each workspace opts into
   with its existing Allowed list (Config › Plugins). A workspace admin may also install plugins **private to that
   workspace**.
2. **Sources:** a GitHub repository (a private one through the workspace's GitHub connection) or a Claude Code
   marketplace, at a tag or a commit, or an uploaded `.zip` or `.tar.gz`. **Every installed copy is pinned:** fetched
   once, its commit and sha256 digest recorded, and every step runs exactly that copy until an admin updates it.
   **Update** shows what changed (manifest, hooks, settings) before it applies. **Remove** names the workflows and
   steps that use it.
3. **Scope:** a workspace admin can mark a plugin **on for every step**; a step can still add plugins in the builder
   or turn a default one off. **Settings are set once per workspace**, and a step may override the non-secret ones.
4. **The deployment switches** (`SWARM_WORK_CONTRACTS`, `SWARM_CONTRACT_REVIEW`, today Terraform-set environment
   variables) become **signed platform settings** on the admin plugins page, the Terraform value being the default
   when no setting is stored.

## What exists today

* **The catalogue is code.** `swarm_api/plugincatalogue.py` holds two `PluginEntry` values, gstack (1.92.55.0) and
  security-audit (a pinned commit), each with an install path in the image, the five claude-code runner profiles and,
  per mode, the skills a step may name. `agent_worker/plugins.py` restates it; a unit test holds the two equal.
* **A step names plugins in its own `plugins` field** (`schemas.StepPluginCreate`: `{name: {mode, skills}}`).
  `plugins.check_step` checks it against the catalogue, the workspace's signed `plugins.allowed` block and the step's
  runner profile; `plugins.with_step_plugins` carries it in the step's work contract (`capabilities.plugins`), whose
  digest rides in the signed dispatch block (`metadata.dispatch.work_contract`). With `SWARM_WORK_CONTRACTS` off a
  step naming plugins is refused. The frozen contract document (`swarm_common.workcontract.WorkContract.to_document`)
  carries only `mode` and `skills` per plugin: no version, no digest, no settings.
* **The workspace's Allowed list** is `workspace_settings/{tenant}.plugins.allowed`, written through
  `PUT /v1/workspaces/{tenant_id}/plugins` (`routes/plugin_settings.py`) by the workspace's owner or admin or a
  platform admin, audited in `admin_audit` (`workspace_plugins_set`), and MAC'd under `swarm-settings-key` as block
  `plugins` (`specsign.SETTINGS_MAC_FIELDS["plugins"] = ("allowed",)`). The console page is `ConfigPlugins.tsx`; the
  builder's picker is `PluginSheet.tsx`.
* **The worker links skills only.** `agent_worker/plugins.py` links `work/.claude/skills/...` to the read-only install
  under `/opt/swarm/plugins/`. It writes no settings file and passes no `--plugin-dir`, so **a plugin's hooks,
  commands, agents and MCP servers never load**. The claude-code runner (`runners/claude_code.py`, CLI 2.1.283) writes
  its own `--settings` file (`write_headless_settings`) and one `--mcp-config` file (`write_mcp_config`). `HOME` is the
  attempt's `work/`, which is checkpointed; the root filesystem is read-only; the worker and the agent run as the
  same uid in one container (`scheduler/dispatch.py`, `runAsUser: WORKER_UID`).
* **jev-belay and jev-enforce are already in the image, as quality gates, not as plugins.** The base Dockerfile
  vendors jev-belay v0.2.0 (`valentynkit/jev-belay` at `ef719db7...`, three files: `belay.mjs`, `package.json`,
  `LICENSE`) and downloads jev-enforce v1.1.1 from the npm registry by sha512, under `/opt/swarm/jev-gates`. Neither
  ships a `.claude-plugin/plugin.json` or a `hooks/hooks.json`. The runner registers them in its own `--settings`
  hooks (Stop: `belay` and `enforce-stop`; PostToolUse on `Edit|Write|MultiEdit`: `enforce-edit`) through the
  launcher `swarm-jev-gate.mjs`, which holds the workspace's TypeSafe key in its own process, deletes the variables
  that would point the tools elsewhere, and scrubs registered secrets. They run only when the signed workspace
  setting `quality_gates.jev` is on.
* **Images may not fetch from GitHub at build time.** Every outside source comes through `build/tool-mirror`
  (`docs/runbooks/build-mirror.md`) and is checked by hash. The base image carries Node 24, Bun 1.4.2, Python 3,
  `git` and `jq`.
* **Secrets.** Provider keys are `swarm-tenant-<t>-<provider>` (`Tenant.secret_name`), the TypeSafe key among them
  (`swarm-tenant-<t>-typesafe`, set through `PUT /v1/provider-keys/typesafe/tenant` by a one-time form). Workspace
  store secrets are `swarm-tenant-<t>-wsec-<16 hex>`; the tenant's GSA holds **no** grant on them and swarm-api
  releases each one to an attempt that is entitled to it (`routes/attempts_secrets.py`). A secret reaches the runner
  as a 0600 file under `private/` (never checkpointed, never uploaded), never as the pod's environment. Redaction
  (`agent_worker/redact.py`) replaces registered literal values, longest first.
* **Egress.** A tenant namespace denies everything, then allows DNS, the metadata server, Private Google Access and
  the internet minus private ranges (`kubernetes/network-policies/allow-egress.yaml`). An FQDN policy exists only for
  TypeSafe, and only on a cluster that supports FQDN network policy (`network-policies/fqdn/`).
* **The switches.** `SWARM_WORK_CONTRACTS` is read by swarm-api (`workcontract.enabled`: on only when exactly `on`);
  no Terraform variable sets it today, so it is off. `SWARM_CONTRACT_REVIEW` (`off|report|enforce`) is read by
  `contractreview.mode` and set from `var.contract_review_mode` (`terraform/infra/locals.tf`); the Day One installer
  renders it `off` (`swarm_install/tfvars.py`). A per-workspace `contract_review.enabled` block is already signed.

### Claude Code's plugin format, as this design uses it

A plugin is a directory. `.claude-plugin/plugin.json` names it (`name`, `version`, `description`, `author`, ...) and
may declare `userConfig`: named fields, each with a `type`, a `title`/description, a default, `required`, and
`sensitive: true` for a secret. Beside it: `skills/<skill>/SKILL.md`, `commands/*.md`, `agents/*.md`,
`hooks/hooks.json` (event → matcher → `{type: "command", command, timeout}`), and `.mcp.json` (MCP servers). Commands
and server definitions refer to the plugin's own files through `${CLAUDE_PLUGIN_ROOT}`, and plugins get a writable
`${CLAUDE_PLUGIN_DATA}`. `claude --plugin-dir <dir>` (repeatable) loads a plugin from a directory for one session,
with every component, without a marketplace or an install. `userConfig` values reach hook commands and MCP server
definitions as `${user_config.<key>}` and the plugin's subprocesses as `CLAUDE_PLUGIN_OPTION_<KEY>`; the CLI keeps a
sensitive value in the system keychain, or a credentials file under `~/.claude` where there is none. A marketplace is
a repository whose `.claude-plugin/marketplace.json` lists plugins, each with a `source`: a path inside the
marketplace repository, a `github` repository or a git URL, optionally at a `ref` or a `sha`, or a package registry.

What this design must not rely on without proof: how CLI 2.1.283, headless (`claude -p`), with no keychain and
`HOME` in a checkpointed folder, takes a sensitive `userConfig` value. Lane 2 (the loading spike) measures it; §4.4
says what is built either way.

## The design in one screen

1. **The catalogue is Firestore data.** A plugin is an *install* (who owns it, its source, which version is current)
   and immutable *versions* (each a pinned copy: commit, source digest, runnable digest, parsed manifest). Platform
   installs serve every workspace that allows them; workspace installs serve one workspace. gstack and
   security-audit become **bundled** platform installs (§1.4): pinned and updated like any other, but each update
   goes through a build step that publishes a new image version with the pinned copy bundled in it.
2. **Fetch and pin once.** swarm-api downloads a GitHub archive at a resolved commit (or receives an upload) into a
   quarantine object as bytes only; an isolated **plugin intake job** with no network and no secrets unpacks it,
   refuses what escapes or exceeds the caps, parses the manifest, and writes one normalised tarball and a report to a
   staging name only it knows; swarm-api re-hashes that tarball and copies it, named by its sha256, into the scope's
   GCS prefix.
3. **At a step's start** the worker downloads each signed plugin's tarball, checks its sha256 against the **signed**
   digest, unpacks it outside the checkpointed tree, and passes `--plugin-dir` per plugin, so hooks, skills, commands,
   agents and MCP servers all load. Every hook and MCP server command runs through one launcher,
   `swarm-plugin-exec`, which gives that plugin, and only that plugin, its secrets, scrubs its output, and counts its
   runs for the result.
4. **Settings are generic.** `userConfig` renders as a form. Non-secret values live in the signed workspace settings;
   secret values live in Secret Manager under a name derived from the workspace, the plugin and the field, or are a
   **binding** to an existing workspace key (TypeSafe). A plugin with no `userConfig` gets **declared settings**:
   environment variables and files (a rules file), declared by the admin at install time in the version's overlay,
   their values set per workspace in the form (a file's content released to the step like a secret).
5. **Defaults reach steps at submission.** A workspace's "every step" plugins and its settings are merged by swarm-api
   into the step's **signed plugin set** (`metadata.dispatch.plugins`), so the worker trusts only what was signed. A
   step may add a plugin, turn a default off, or override a non-secret value.
6. **Update and remove never touch a step already submitted:** it keeps the version it was signed with.
7. **Every change is audited**, and every plugin a step ran is in its result, with its version, digest and hook
   counts.
8. **The two deployment switches become signed platform settings** on Admin › Plugins, defaulting to Terraform.

## 1. The catalogue as data

### 1.1 Documents

| Collection / document | Written by | Holds |
|---|---|---|
| `plugin_installs/{install_id}` | swarm-api only | `scope` (`platform` or `workspace`), `tenant_id` (workspace scope only), `name` (the manifest's), `display_name`, `source` (§2.1), `current_version`, `built_in`, `state` (`active`, `removed`), `created_by/at`, `updated_by/at`, `mac` |
| `plugin_installs/{install_id}/versions/{version_id}` | swarm-api only, from the intake job's report | `source` (as resolved: repository, ref asked for, commit; or upload name and size), `source_sha256`, `runnable_sha256`, `object` (the GCS name), `manifest` (§1.3), `overlay` (§6.3), `requires` (§5), `files`, `bytes`, `intake` (the job's checks), `created_by/at`, `mac` |
| `workspace_settings/{tenant}.plugins` | swarm-api only | `allowed` (as today, now install ids), `defaults` (install ids on for every step), `configs_digest` (§6.2), `set_by/at`, `mac` |
| `workspace_settings/{tenant}/plugin_configs/{install_id}` | swarm-api only | per field: a non-secret `value`, or `secret: {kind: "plugin", set_at}`, or `binding: {kind: "provider_key", provider: "typesafe"}`; `set_by/at` |
| `platform_settings/switches` | swarm-api only | `work_contracts` (`on`/`off`), `contract_review` (`off`/`report`/`enforce`), `set_by/at`, `mac` (§11) |
| `plugin_intake/{intake_id}` | swarm-api only | one fetch or upload in flight: `scope`, `tenant_id`, `install_id` (an update) or none (a new install), requested source, quarantine and staging names, state, refusal code; `mac` (request P2, kind `intake`); deleted a day after it ends |

**Why install and version are two documents.** Pinning is the owner's rule: a step runs exactly one copy. A version
is never edited after it is written; an update writes a new version and moves `current_version`, so a step signed
yesterday can still name yesterday's version by id, and the Update dialog can compare two stored versions instead of
re-fetching.

**Why every document carries a MAC.** The tenant GSAs hold project-wide Firestore access (this is why the workspace
blocks are MAC'd today), so any tenant's worker can write any of these documents. A forged version document could
swap the digest every workspace's steps would be signed with. swarm-api therefore acts on an install, a version or a
setting only when its MAC verifies under `swarm-settings-key`, and the worker never reads these documents at all: it
trusts only the step's signed plugin set and the bytes' sha256. The canonical form of an install, a version and an intake needs
a contract change (request P2, §14).

**Install ids.** `p-<12 hex>` for platform scope and `w-<12 hex>` for workspace scope, random, never derived from a
name, so two workspaces may each install their own `jev-enforce` without colliding and a name can be reused after a
removal without resurrecting old settings. The console shows the manifest `name`; a step and the builder show the
name with its scope ("jev-enforce · this workspace" or "· platform").

### 1.2 Who sees what

* A workspace's catalogue is: the two bundled installs (§1.4), the platform installs the workspace **allows**, and its own workspace
  installs (allowed by being installed; a workspace admin may still switch one off without removing it).
* A platform install is invisible to a workspace until a workspace admin allows it, as the Allowed list works today.
* A workspace install is invisible to every other workspace and to the platform catalogue. A platform admin sees it
  on Admin › Plugins in a per-workspace list (read-only except **Remove**, for an incident).

### 1.3 The parsed manifest

The intake job parses and stores, so neither the console nor submission ever reads the archive:

* `plugin.json`'s `name`, `version`, `description`, `author`, `homepage`, `license`, and `userConfig` (each field's
  key, type, title, description, default, `required`, `sensitive`);
* the **components**: skill names (each `skills/<name>/SKILL.md` and its frontmatter `name` and `description`),
  command names, agent names, every hook (event, matcher, command, timeout) and every MCP server (name, command,
  arguments, environment keys, never values); a server with a `url` transport is listed with its host;
* `requires` (§5), from the overlay.

### 1.4 Bundled entries: gstack and security-audit, managed, built into an image version

**Decided by the owner on 6 November 2026 (Q13), against this document's earlier recommendation that they stay code
entries:** gstack and security-audit migrate to the managed path, and "it has to gain a build step that publishes a
new image version in which it ships bundled in". So they become catalogue entries like any other plugin: installed
(by the platform, at the pins the image carries today), pinned, allowed per workspace, made a default, and updated
by a platform admin after the diff (§9.1). What differs is only **how a version becomes runnable**. Any other plugin's
version is a tarball the worker downloads and unpacks per attempt (§4.2). A bundled version is **built** into a new
image version, and a step runs on that image.

**Why a build, and not a tarball like the rest.** The intake job (§2.3) may not run plugin code, and both of these
need code run before they work. gstack's `./setup` compiles its binaries with Bun, renders its skills and runs
`bun install --frozen-lockfile`, and the compiled `browse` embeds its install path, `/opt/swarm/plugins/gstack`
(`images/agent-runtime-base/Dockerfile`, stage `gstack-build`), so it cannot be unpacked under a per-attempt
`private/plugins/<install_id>/` and work. security-audit needs no compile, but `security_review.py` runs the skill's own
validators (`validate-findings.cjs`, `validate-coverage-ledger.cjs`) from a fixed path after the agent finishes, and
those validators are what decide whether a review's findings count. A copy the agent's own container unpacked into a
writable folder could be edited by the agent before the validators run. A copy in the image's read-only root
filesystem cannot.

#### 1.4.1 How they leave `plugincatalogue.py`

Today both are `PluginEntry` values in `swarm_api/plugincatalogue.py`, restated in `agent_worker/plugins.py` and held
equal by `tests/unit/worker/test_plugins_start.py`. They reach a step through the work contract's
`capabilities.plugins` (so only with `SWARM_WORK_CONTRACTS` on), and `builtin_entries()` shows them in the managed
catalogue as `builtin:gstack` and `builtin:security-audit`, `removable: false`, `updatable: false`.

After the migration:

* **Two platform installs with reserved ids.** `plugin_installs/builtin:gstack` and
  `plugin_installs/builtin:security-audit`, `scope: platform`, `built_in: true` and a new field `build: "image"`.
  The ids are the ones every Allowed list and every default already names (`pluginconfig.py` accepts the bare name
  and the `builtin:` id today), so **no workspace setting is rewritten**: a list that allows `gstack` allows the
  managed install. Install ids are otherwise random (§1.1); these two are reserved and never minted.
* **Their first version is what the image carries now.** gstack `1.92.55.0` at its pinned commit and archive sha256,
  security-audit at commit `c1c8a8c1...` and its archive sha256: the same values the Dockerfile's ARGs and
  `images/agent-runtime-base/tool-mirror.sha256` hold. The per-mode skill lists (gstack's `unattended`, `planner`,
  `designer`; security-audit's one skill) move from the code entry into the version's overlay (§6.3) as `modes`, so the
  builder still offers a step the same skills per mode and `SUPPORTED_MODES` still refuses `planner` and `designer` until
  the question bridge lands. The runner profiles a version may load on become its `requires` (§5): the claude-code
  profiles, as today.
* **Seeded by swarm-api, not by hand.** On its first catalogue read with the switch of §1.4.5 on, swarm-api writes the
  two installs and their first versions in one transaction, MAC'd like every catalogue document, from a release file
  that states the release image's pins (`images/agent-runtime-base/bundled-plugins.json`, new, the one place a
  release's default pins are written; the Dockerfile's ARGs are read from it). A second instance's identical write
  finds the documents and does nothing.
* **The code entries go one release later.** For one release both routes exist: the code entries stay so a step
  signed through `capabilities.plugins` before the switch still runs, and the worker reads both. After a release in
  which no unfinished step carries `capabilities.plugins` naming them, the entries, `builtin_entries()` and the
  equality test are deleted, and a new test holds `bundled-plugins.json` equal to the seeded first versions.

#### 1.4.2 Where the build runs

Images are built today only by `scripts/build-images.sh`, on Cloud Build (a pull request's `--build-only --local`
excepted), tagged with the git SHA, and promoted by digest by `scripts/push-images.sh` (`:dev`, `:prod`, and a
non-moving `promoted-<channel>-<tag>`); `scripts/lib/image-refs.sh` turns a promotion manifest into Terraform's
`image_refs` (digests only, never a tag); `terraform/infra/locals.tf` writes the runner images' digests into
`WORKER_IMAGE_REFS`, and the scheduler resolves a runner profile's image **name** through it. A Day One deployment
builds nothing: stage 4 of `swarm install` copies the release's signed images, by digest, after `cosign verify`, into
the deployment's own registry, and after the install nothing in the deployment reads the release home.

So there are two builds, and neither is an Actions job (CI builds, tests and merges; it is never a reviewer or an
auditor, and it never builds a deployment's plugin):

1. **The release build, unchanged in where it runs.** `build-images.sh` still builds `agent-runtime-base` (and the
   images built from it: `-nocodex`, `agent-runtime-browser`, `agent-runtime-lightpanda`) with the release's default
   pins from `bundled-plugins.json`. The image now also carries, per bundled plugin, a manifest
   `/opt/swarm/plugins/<name>/.swarm-bundle.json`: the install id, the source commit and sha256, the recipe digest and
   the sha256 of the built tree. A fresh deployment and the switch-over therefore need **no build**: the release image
   is the bundle of the first versions.
2. **The bundle build, new, in the deployment's own project, as one Cloud Run job execution (changed by the owner,
   11 November 2026; until then a Cloud Build build).** When a platform admin applies an update to a bundled install
   (§1.4.3), swarm-api runs **one execution** of one Cloud Run job, `swarm-plugin-bundle`, whose identity is the
   builder account `swarm-plugin-bundler` and whose image is the **bundler image** (`swarm-plugin-bundler`, new,
   built and signed by the release build like every other image, and copied by stage 4). The bundler image holds the
   recipes factored out of today's Dockerfile stages: `recipes/gstack` (`./setup --host claude --no-browser` and
   `gstack-layers check`/`stage` with the checked-in file lists, the browser image's list for the browser variant)
   and `recipes/security-audit` (stage `skills/security-audit/` and `LICENSE`, then run the skill's validator tests on
   the staged copy, as `tests/unit/scripts/test_security_audit_image.py` does today). Its output is one image per
   runner image a claude-code profile uses: the deployment's copy of the live release image, by digest, with **one new
   layer per bundled plugin** on top, each holding `/opt/swarm/plugins/<name>/` (opaque over the release's own copy)
   and its new `.swarm-bundle.json`.
   Dependencies come only through mirror repositories, never straight from a public registry, as in the release
   build; which repositories a deployment holds for that is Q19.

**Why a job and not Cloud Build (the owner, 11 November 2026).** An independent security review of the bootstrap
rows that would have let swarm-api start Cloud Build builds (a custom role holding `cloudbuild.builds.create` and
`cloudbuild.builds.get`, granted to swarm-api project-wide, and swarm-api's `actAs` on the builder) found that on a
shared project those rows reach further than one build; the rows are reverted. The job follows the precedent of the
workspace deployer's job ([workspaces.md](../workspaces.md), "The identity" and option (i) of "How an approval starts
the job"): a job created by Terraform with a fixed identity and image, and its caller's right to run it granted **on
the job resource itself**, because Cloud Run exposes no `resource.name` to IAM Conditions and a project-level grant
could not be narrowed to one job.

**The job.** `swarm-plugin-bundle`, in the deployment's region, `terraform/infra/plugin-bundles.tf`, planned only
with `builtins_managed` on, as every bundle resource is:

* **Identity** `swarm-plugin-bundler`; **image** the bundler image **by digest**, from the release's `image_refs`
  like every other job, so a release upgrade moves it and nothing else can.
* **One agent, no retries** (`max_retries = 0`: a retry is an admin's Apply again), parallelism 1, an agent timeout of
  3600 seconds, and the entrypoint's own deadline of 3000 seconds, after which it exits non-zero having pushed
  nothing it has not finished.
* **No container daemon, no Dockerfile, no BuildKit.** The entrypoint runs each recipe in a scratch directory of the
  job's own memory-backed filesystem, packs the staged tree as one tar layer (fixed owners and times, so the layer is
  reproducible from the tree), and **appends** it with a registry library (the append operation of
  go-containerregistry, the library `crane append` is built on) to the release image's manifest, read by digest from
  the platform repository. The base layers are mounted across repositories by digest, never downloaded, unpacked or
  rewritten; the job writes only the new layers, a new config and a new manifest, pushed to
  `swarm-plugin-bundles/<runner image name>:<bundle_id>`.
* **What reaches it from a caller: the arguments, and nothing else the job uses.** swarm-api passes
  `overrides.containerOverrides[0].args` = the bundle id, then per bundled plugin its name, its source sha256 and its
  recipe digest, then per runner image its name and the base digest. The entrypoint re-validates each against a closed
  pattern (`^[a-z0-9-]{1,63}$` for names and the id, `^[0-9a-f]{64}$` for a sha256, `^sha256:[0-9a-f]{64}$` for a
  digest; a plugin name must be one of the recipes it holds and a runner image name one the job's template lists)
  before anything runs. As the workspace deployer's entrypoint does (workspaces.md §2.2 step 0), it then **discards
  every environment variable** that is not in the job's own template, refuses to work unless
  `CLOUD_RUN_TASK_COUNT` is `1` (an agent with a higher index exits 0 having done nothing), and keeps its own deadline
  whatever timeout the call set. Arguments do not need to be trusted: they can only name pinned sources the intake
  already checked and release images the deployment already holds, and swarm-api checks the result (below).
* **Its network.** The job runs upstream code (gstack's `./setup`, `bun install`, security-audit's tests), and a Cloud
  Run job with no VPC access has direct internet egress, from which that code could send the builder's token out or
  fetch code the admin never reviewed (the security review of 11 November 2026). So every packet it sends leaves
  through its own network, `<name_prefix>-plugin-bundle` (`vpc_access` with `egress = "ALL_TRAFFIC"`), built as the
  intake network is (§2.2) and neither that network nor the swarm network: no router and no Cloud NAT, Private Google
  Access on its subnetwork, an egress firewall that admits TCP 443 from the job's tag and denies the rest, and flow
  logs. It can therefore reach Google's APIs and nothing else: Artifact Registry (`<region>-docker.pkg.dev`, where the
  release images, the bundle repository and the npm mirror `<region>-npm.pkg.dev` are) and Cloud Storage, through
  Private Google Access on the default domains; not the internet, the cluster or the internal services. Google's APIs
  answer for every project, so this network alone does not stop a request to another project's resource; a VPC
  Service Controls perimeter would, and the deployment has none.

**What swarm-api holds, and what it cannot do (review finding 1).** One custom role, `swarmPluginBundleRunner`
(`run.jobs.run` and `run.jobs.runWithOverrides`, the two Cloud Run checks a run carrying arguments passes, and
`run.executions.get`, to read the execution it started), bound **on the job `swarm-plugin-bundle`'s IAM policy** and
nowhere else. With it swarm-api can start that job with the override fields the run method has
(`containerOverrides[].args`, `env` and `clearArgs`, `taskCount`, `timeout`), of which the job uses the arguments
and neutralises the rest (above), and read that job's executions. It **cannot** change the job's image, command,
identity, network or volumes (`run.jobs.update` is not in the role), run or read any other job, cancel or delete an
execution, read the job's logs, or act as the builder (running a job checks no `actAs` on its identity, and swarm-api
holds none). **Cloud Build is out of swarm-api's reach entirely**: no `cloudbuild.*` permission, so no trigger is
created, run or read, no build is retried, and no build can name another account, the legacy Cloud Build account or
the Compute Engine default (which holds Editor) included. **Residual, accepted:** the scheduler
(`swarmJobDispatcher`), the acceptance account and the release deployer already hold `run.jobs.runWithOverrides`
project-wide, unscopably (workspaces.md §2.1), so they too can start this job; none of them can change what it runs,
and the argument check and the environment scrub are what hold for every caller.

**Where upstream code runs, and what it can reach (review finding 2).** `./setup`, `bun install` and the validator
tests are upstream plugin code, and they run **inside the job's own container**, as the builder. There is no build
daemon and no socket to reach, but the metadata server is there, so that code can obtain **the builder's token**, and
with it do exactly the builder's grants (below) and nothing more: read pinned sources under `platform/sources/`
(get, no list), read the deployment's release images, read the one pinned Bun object, fetch through the npm mirror,
and push images into `swarm-plugin-bundles` (no delete; tags are immutable, so a pushed tag never moves). It cannot
reach Firestore, Secret Manager, a tenant's prefix, the cluster, the network of the steps, or another team's
resources. Pushing is the lever that matters, so **swarm-api trusts nothing the job says about its output**. Before
it marks a bundle `ready` it reads each pushed manifest by the tag `<runner image name>:<bundle_id>` (as a control
plane reader of the repository) and refuses the bundle (`bundle_check_failed`) unless:

* every layer of the base, in order, is the release image's layer **by digest**, and the config's `rootfs.diff_ids`
  for those layers are the release image's own;
* exactly one new layer per bundled plugin follows, and each holds only paths under that plugin's
  `/opt/swarm/plugins/<name>/` (no `..`, no link whose target leaves it, no device file; its one whiteout is the
  opaque marker on that directory, which hides the release's older copy, and no whiteout names a path outside it),
  which swarm-api reads from the layer itself, a bounded transfer (a cap of 512 MiB per layer);
* the tree's sha256 swarm-api computes from each layer equals the one in that layer's `.swarm-bundle.json`, and that
  manifest's source sha256 and recipe digest equal the bundle key's;
* security-audit's layer equals, file by file, the `skills/security-audit/` and `LICENSE` entries of its pinned
  source archive, which swarm-api wrote itself (§1.4.3): its recipe copies and tests, it compiles nothing, so its
  validators cannot be bytes the source did not carry. **Residual, accepted:** the two bundled plugins' upstream code
  runs in one execution, so gstack's setup could alter gstack's own built tree in any way it likes; that tree is the
  plugin's code, pinned by a platform admin after the diff (§9.1), and runs in every step that loads it anyway.

The digest swarm-api records is the manifest's digest it read and checked, never a value the job reported. The
worker's own check against the signed pins (§1.4.4) stays as the second, independent one.

**The builder's grants, after the rework (review finding 4).** `swarm-plugin-bundler` holds exactly:

1. `roles/storage.objectViewer` on the plugins bucket, conditioned on `platform/sources/` (get, no list), as today;
2. `roles/artifactregistry.reader` on the platform repository: the release images it appends to and mounts from, as
   today (it pulls no bundler image itself: Cloud Run pulls the job's image as its service agent);
3. `roles/storage.objectViewer` on the build mirror bucket, **conditioned on the one pinned Bun object**
   (`resource.name ==` that object), no longer the whole bucket;
4. `roles/artifactregistry.reader` on the npm mirror repository (Q19), which `bun install --frozen-lockfile` fetches
   through; a read of a remote repository can pull any public package through the cache, which is what the frozen
   lockfile and the recipe's byte-for-byte lockfile check bound;
5. `roles/artifactregistry.writer` on `swarm-plugin-bundles` alone: push, no delete, immutable tags.

Gone: the Docker Hub mirror read (no Dockerfile frontend without a daemon) and `roles/logging.logWriter` at the
project. **Logs:** the job writes to stdout and stderr, which Cloud Run collects for the job itself, so the builder
needs no log-write role; project-wide log write was needed only because Cloud Build refuses a build as a
user-specified account that cannot write its log. Narrower still, and part of the same Terraform: a log sink with the
filter `resource.type="cloud_run_job" AND resource.labels.job_name="swarm-plugin-bundle"` routes the executions' logs
to the platform's own log bucket (`terraform/infra/log_bucket.tf`). The sink copies them: they also stay in the
project's `_Default` bucket at its own retention, where anyone holding a project-level log read role sees them, so
upstream output is not confined to the platform's readers. That
stdout reaches Cloud Logging with no role on the runtime account is not verified live here; the first execution in a
deployment is the proof, and if it is wrong the fallback is the old project-level `roles/logging.logWriter` row, the
owner's call.

**Quota and reads (review finding 3).** The job uses **no Cloud Build quota**, so no build of another team's waits
behind a bundle build or competes with one. It draws on the region's Cloud Run job quotas, which the platform's
worker jobs already use: one execution per Apply, one at a time per deployment (swarm-api refuses a second Apply while
a bundle is `building`, and `swarm upgrade` runs its rebuilds one after another). swarm-api reads a bundle's state from
**its own bundle record**; it calls `executions.get` only for a record in `building`, at most once per bundle per 15
seconds, the time of the last call kept on the record and claimed in a transaction, so any number of open pages and
reloads make at most one call per bundle per 15 seconds. The record leaves `building` when the execution has ended
(`ready` after the check above, else `failed`), and a record still `building` an hour after it started is failed
without a call.

**The default build account checks (review finding 5).** No Cloud Build build runs for a bundle, so the checks on the
project's default build account (whether it is the legacy Cloud Build account, which skips `actAs`, or the Compute
Engine default, which holds Editor) are **no longer needed for bundle builds**; the installer and Terraform lanes stop
carrying them for this feature. (The CI add-on's own build account check in `swarm_install/addons.py` is another
feature's and is unchanged.)

**Day One, rollback and the owner's rows (review finding 6).**

* **A fresh install creates**, with `builtins_managed` on: the builder account (bootstrap, `plugin_bundler.tf`, as
  today), the custom role `swarmPluginBundleRunner` (bootstrap: custom roles are defined only there,
  `tests/terraform/custom_roles_out_of_ci.tftest.hcl`), the release deployer's `actAs` on the builder (bootstrap: it
  is not on the per-account list `deployer_service_accounts.tf` grants admin on, and creating a job that runs as an
  account needs `actAs` on it), then, in `terraform/infra`, the job, its binding of the role to swarm-api on the job,
  the repository, the mirror grants and the log sink. Day One's bootstrap pass applies the first three; on an existing
  deployment they, and the removal of the reverted Cloud Build rows (`terraform/bootstrap/plugin_bundle_builds.tf`),
  go to the owner **as a runbook**, never applied by a lane or by CI.
* **Removing it** (the switch off, or the job's rows reverted) deletes the job and its binding; Cloud Run deletes a
  job's execution records with the job. It **leaves**: the executions' logs, in the log bucket until its retention;
  the pushed images in `swarm-plugin-bundles`, which the retention sweep of §9.3 owns and which the repository's own
  removal (the switch off) deletes; and the bundle records, which with the switch off nothing reads. The bootstrap
  rows (the role, the deployer's `actAs`) stay until the owner removes them by the same runbook; an `actAs` on an
  account whose grants run nothing, and a role bound on a job that is gone, reach nothing.

**Why in the deployment and not in the release home.** The owner's rule is that every installed copy is pinned and
updated by an admin after the diff. A release-home build would let a deployment's admin pick only the versions a
release ships. Building in the deployment lets a bundled install update like any other, while every other part of
the image stays the release's signed bytes (the bundle adds one layer per bundled plugin over a verified digest, and
swarm-api checks that it adds nothing else). That reading, and the other one, are Q16; the owner's answer of 11
November 2026 keeps the build in the deployment's own project and changes only what runs it (a Cloud Run job, not
Cloud Build).

#### 1.4.3 What "publishes a new image version" means

* **An update** runs §9.1 as for any plugin: swarm-api resolves the ref, the intake job checks the **source** archive
  (caps, paths, the manifest, the skills' frontmatter) and writes it under `platform/sources/<sha256>.tar`, and the
  diff is shown. Nothing is built before an admin asks; nothing is current before the build passes.
* **Apply** on a bundled install writes a **bundle** record and runs one execution of the job `swarm-plugin-bundle`
  (§1.4.2) instead of moving `current_version` (a Cloud Build build until the owner's change of 11 November 2026).
  The catalogue's own Apply route does this itself: for a bundled install it delegates to the bundle route's start
  (one code path, the same diff check, `202` and the bundle's status), so the console keeps one Apply button and
  nothing can reach the old move-at-once path (the owner's pick on Q21).
  `plugin_bundles/{bundle_id}` (swarm-api only, MAC'd, request P6): the bundle key (every bundled install's
  `version_id`, source sha256 and recipe digest: a bundle carries **both** bundled plugins, because one step may load
  both and runs on one image), the base (the release image digests it appended to), the execution's name, the time
  of the last execution read (§1.4.2's cap), the output digest per runner image name **as swarm-api read and checked
  it** from the repository, the built tree's sha256 per plugin as swarm-api computed it from the layer (and found equal
  to the layer's `.swarm-bundle.json`), and `state` (`building`, `ready`, `failed`, `retired`). The page shows the
  build's progress from the record; a failure is a fixed code and sentence (`bundle_build_failed`: the execution
  failed or timed out; `bundle_check_failed`: the pushed manifest failed §1.4.2's check) with the execution's name,
  never the execution's log text, which is upstream output.
* **Only a ready bundle moves `current_version`**, in the transaction that marks it ready, and only after swarm-api's
  check of every pushed manifest (§1.4.2) passed. A step submitted after that gets the new version.
* **Tags and digests.** The bundle images are
  `<region>-docker.pkg.dev/<project>/swarm-plugin-bundles/<runner image name>:<bundle_id>`. The tag is a name for a
  person reading the registry, written once and never moved; everything that runs a bundle names its **digest**, as
  every image here is named (`docs/audits/2026-09-20/tag-vs-digest.md`).

#### 1.4.4 Which image a step runs on, and a step that names an older pin

* **What is signed.** A bundled plugin's entry in `metadata.dispatch.plugins` (§8) carries `build: "image"`, the
  version id, the source sha256 and the built tree's sha256, and the set carries one `bundle` field: the bundle key.
  Not an image digest: a digest signed at submission would name the base live then, and a step queued across a
  release upgrade would run the old worker against the new control plane.
* **The scheduler** resolves a step with no bundled plugin exactly as today (`WORKER_IMAGE_REFS`). For a step with a
  bundle key it reads the bundle record whose key matches and whose base is the **live** base (the digest
  `WORKER_IMAGE_REFS` names for that runner image), and runs that record's output digest. It accepts only a digest
  under `swarm-plugin-bundles`, so a forged record (the tenant GSAs can write Firestore) can point only at another
  bundle image, built by the builder account; and the release image counts as the bundle of the pins it carries. The
  caller still names a runner profile and nothing else (invariant 10): the digest comes from the catalogue's own
  records, never from a submission.
* **The worker** trusts only the signed set: before the agent starts it reads `.swarm-bundle.json` of each bundled
  plugin from its own read-only image and refuses the step (`plugin_bundle_mismatch`, a fixed code with the install
  and version ids) unless the source sha256, the recipe digest and the built tree's sha256 equal the signed ones. A
  record pointing at the wrong bundle therefore stops a step; it never runs one with other bytes. Nothing is
  downloaded for a bundled plugin.
* **An older pin.** A step keeps the version it was signed with (§9.1), through every retry and resume. It is served
  by the bundle with its key on the live base. Two things keep that bundle present:
  * **Retention.** A bundle is retired (and its images deleted by §9.3's sweep) only when it is not current, no
    unfinished step names its key, and it is not the previous current bundle (kept for rollback, §1.4.7).
  * **An upgrade rebuilds before it switches.** A release upgrade changes the base. `swarm upgrade`
    (`swarm_install/upgrade.py`) pauses dispatch to drain, copies the new images, then applies infra, which writes the
    new digests into `WORKER_IMAGE_REFS`. It gains one step between the copy and the infra apply: one bundle build per
    retained key on the new base, and infra waits until every one is ready (§15). Since 11 November 2026 a build is an
    execution of the job `swarm-plugin-bundle` (§1.4.2), whose image is pinned by Terraform, so the step first applies
    the job alone (a targeted apply of the job resource, which moves its image to the new release's bundler digest
    and changes nothing else), then runs one execution per retained key, **one after another** (§1.4.2's quota rule),
    each checked by swarm-api before it is `ready`. A queued step therefore finds its pins on the new base, built from
    the same source and recipe. The previous base's bundles stay until the next
    upgrade succeeds, so the upgrade's own rollback (the previous release's digests applied again) finds them too.
* **When it is missing anyway** (a build that failed during the upgrade, a record deleted by hand), the dispatch is
  refused with `plugin_bundle_missing` before any pod exists, and the step takes the platform-refusal path it takes
  today for a dispatch the platform cannot make. A queued step costs nothing while it waits (invariant 1); nothing
  sleeps (invariant 4).

#### 1.4.5 The switch-over: no step loses gstack or security-audit

The switch is one swarm-api setting, `SWARM_BUILTINS_MANAGED` (`on`/`off`, Terraform `var.builtins_managed`, `off` by
default for an existing deployment so nothing changes at merge, `on` in a fresh deployment's tfvars, which has no
queued step to carry across). In order:

1. **The release that carries the bundle manifests ships first**, with the switch off. Nothing changes for a step.
2. **Switch on.** swarm-api seeds the two installs (§1.4.1). From then on submission resolves a step's `gstack` or
   `security-audit` (named in its `plugins` field as `{mode, skills}`, or a workspace default) to the managed install's
   current version, writes it into `metadata.dispatch.plugins` with the step's mode and skills, and no longer writes
   it into `capabilities.plugins`. A saved workflow is not edited: the same JSON is resolved the new way.
3. **Workspace defaults and Allowed lists are untouched**: they already name these ids.
4. **Steps already submitted** carry `capabilities.plugins`. The worker of that release reads both routes, and the
   bytes are the same (the release image holds the same pins at the same paths), so a step queued before the switch
   runs the same skills from the same files.
5. **Built-ins no longer need `SWARM_WORK_CONTRACTS`.** A managed plugin is outside the work contract (§8), so a
   deployment with work contracts off, where a step naming gstack is refused today, accepts it after the switch.
   The plugins switch (§11) still refuses every step that names any plugin when it is off.
6. **One release later** the code entries go (§1.4.1).

#### 1.4.6 security-audit's validators at a known path

`security_review.py` keeps reading `/opt/swarm/plugins/security-audit/skills/security-audit` (today
`plugins.CATALOGUE[PLUGIN].install_path`, falling back to `DEFAULT_INSTALL`). The recipe fixes that path, and the
path is in the image's read-only root filesystem, so a bundle image puts the pinned validators exactly where the
review expects them and the agent cannot edit them. Two changes only: the path becomes `security_review.py`'s own
constant (the code entry it reads goes in §1.4.1), and `applies()` sees security-audit through either route (the
work contract's capabilities, or a signed managed entry named `security-audit` with `build: "image"`). Before it runs
the validators, the review checks the plugin's `.swarm-bundle.json` against the signed entry, so the verdict is
always from the pinned copy's own validators.

#### 1.4.7 Rollback

* **A bad update.** "Roll back" on the plugin's page applies the previous version. Its bundle is retained (§1.4.4),
  so it moves `current_version` at once, with the diff and the typed confirmation of §9.1, and audited
  (`plugin_updated`). Steps signed with the bad version keep it, as any pinned step does; cancel them to stop them.
* **A failed build** (an execution that failed or timed out, or a pushed manifest that failed swarm-api's check,
  §1.4.2) changes nothing: `current_version` never moved. What the execution pushed stays in `swarm-plugin-bundles`
  under its never-reused `bundle_id` tag, named by no ready record, so nothing runs it, and §9.3's sweep deletes it
  with the failed record.
* **The job itself** (since 11 November 2026). Reverting the job's Terraform removes the job and swarm-api's binding
  on it; bundles already `ready` keep serving steps, because a step runs a bundle by digest and never touches the job.
  Only Apply on a bundled install and the upgrade's rebuild stop, each refused with `bundle_build_failed` until the
  job is back (§1.4.2, Day One and rollback).
* **The switch-over.** Turn `SWARM_BUILTINS_MANAGED` off: submission writes `capabilities.plugins` again from the code
  entries, which stay for the release after the switch for this. Steps signed through the managed route still run,
  because the release image is their bundle and the worker reads both routes. The seeded installs stay, unused, so
  turning it on again changes nothing in Firestore.
* **A release rollback.** Rolling the deployment back to a release from before the bundle manifests needs the switch
  off first; `swarm upgrade` refuses to put a release without `.swarm-bundle.json` live while it is on, and says so
  in its plan.

#### 1.4.8 What a bundled install can and cannot do

Only platform scope: a bundle image is built once per deployment and serves every workspace that allows it, so a
workspace-private gstack would be a per-workspace image on a shared registry path (Q18). No Remove: a workspace
disallows it instead, and `security_review.py` and the step templates that name it keep working (Q17). Declared
settings and `userConfig` work as for any plugin. gstack's own environment (`GSTACK_HOME`, `GSTACK_EPHEMERAL` and the
rest, set by `agent_worker/plugins.py` today) stays the worker's, keyed on the reserved install id: its values are the
attempt's own paths, not settings an admin writes.

## 2. Sources, fetch and pin

### 2.1 What an admin may name

| Source | Form | Resolves to |
|---|---|---|
| GitHub repository | `owner/repo`, a tag, a branch or a commit, an optional subdirectory | one 40-hex commit, through `GET /repos/{o}/{r}/commits/{ref}` |
| Claude Code marketplace | a GitHub `owner/repo` holding `.claude-plugin/marketplace.json`, a ref, and the plugin's name in it | the marketplace's commit, then the entry's `source`: a relative path (same commit, that subdirectory); a `github` source at a `sha` (that commit) or a `ref` (resolved as above, and the resolution recorded). A `url`, `npm` or `pip` source is refused, recommended pending Q10 |
| Upload | one `.zip`, `.tar.gz` or `.tgz` | its own bytes; "commit" is recorded as `upload` with the uploader and the file's name |

A private repository is read through the **workspace's GitHub connection**: the App installation token, minted by
swarm-api for this one repository with `contents: read` only (GitHub lets an installation token be narrowed to
named repositories and permissions), used once, never stored. A platform install from GitHub uses public
repositories, or the platform's own App installation when the repository belongs to the deployment's organisation.
A person's own GitHub token is never used to fetch.

### 2.2 Who fetches, and through which network path

The rule that images may not fetch from GitHub at build time is about the image's reproducibility; fetching a
plugin is a runtime act. Three places could do it (Q7):

* **A: swarm-api downloads bytes, an isolated intake job checks them (recommended, pending Q7).** swarm-api (Cloud
  Run, which already holds the GitHub App's key and reaches GitHub) streams the archive of exactly the resolved
  commit (`GET /repos/{o}/{r}/tarball/{commit}`) into `gs://<plugins bucket>/quarantine/<intake_id>`, stopping at
  the compressed cap, **without unpacking it**. It then starts one execution of the Cloud Run job
  `swarm-plugin-intake`, whose service account can read one named object under `quarantine/`, create objects under
  `staging/`, and nothing else: no list, no delete, no write to `platform/` or `tenants/`, no Firestore, no Secret
  Manager, no GitHub, and **no egress** (the job runs with all traffic through the VPC and no route out except
  Private Google Access). The untrusted archive is parsed only there, and §2.3's last step says why an execution the
  archive takes over can spoil only its own intake.
* **B: swarm-api does all of it.** One service fewer, but archive parsing (zip bombs, path tricks, parser bugs) would
  run in the process that holds the settings key, the KMS signing permission and every workspace's data.
* **C: a SwarmCloud agent job.** Reuses the dispatch machinery, but puts the forge token in a pod and costs a
  container start per install.

An **upload** takes the same path from the quarantine object on: the console asks swarm-api for a V4 signed upload
URL to `quarantine/<intake_id>`, bound to the content type and to `x-goog-content-length-range: 0,<cap>`, valid ten
minutes, so the bytes never pass through swarm-api and a larger file is refused by Cloud Storage itself.

### 2.3 The intake job's checks

In this order, no partial result written. **Every refusal is a fixed code and a fixed sentence** (`entry_escapes_root`:
"An entry's path leaves the plugin's folder."), with nothing interpolated; the only details it carries are ids the
platform minted, as separate structured fields (`intake_id`, and the offending entry's index in the archive's own
order). An upstream string (an entry path, a manifest name or version, a hook command) never enters a refusal, a log
line or an audit row, because it is the attacker's text; the console renders the plugin by looking up its id.

1. **Size before unpacking:** the compressed object at most 25 MiB (recommended, pending Q8).
2. **Every entry, read from the archive's index before any byte is written:** a relative path; no `..` segment; no
   absolute path, drive letter or backslash; no NUL; at most 32 segments and 255 bytes per segment; no duplicate
   after Unicode NFC normalisation and case folding (a checkout on a case-insensitive disk must not differ); only
   regular files, directories and **symbolic links whose target, resolved inside the archive, stays inside it and is
   not itself a link** (no hard links, devices, FIFOs or sockets).
3. **Caps while unpacking, counted from bytes actually written, not from headers:** at most 100 MiB unpacked, 5,000
   files, 50 MiB per file, and a compression ratio of at most 100:1 per entry (recommended, pending Q8). The job
   unpacks into its own in-memory filesystem; the caps also bound its memory.
4. **Modes are normalised:** files `0444`, or `0555` when any execute bit was set; directories `0555`; no setuid,
   setgid or sticky bit; owner and timestamps zeroed.
5. **The plugin root:** the archive's single top folder (GitHub's tarballs wrap the tree in one) and then the
   requested subdirectory. It must hold `.claude-plugin/plugin.json`, **or** the admin must have supplied an overlay
   (§6.3) that declares one.
6. **The manifest parses** (`plugin.json` is a JSON object; `hooks/hooks.json` and `.mcp.json`, when present, parse
   and use only the documented keys); every path a hook or a server names through `${CLAUDE_PLUGIN_ROOT}` exists in
   the tree. `plugin.json` may itself declare `hooks` and `mcpServers` inline, or point `hooks`, `mcpServers`,
   `commands`, `agents` or `skills` at custom paths: the job reads every such declaration and every file it points
   to, refuses a path outside the plugin root, and rewrites the commands found there exactly as those in
   `hooks/hooks.json` and `.mcp.json` (§4.4). A declaration in a shape it does not know is refused
   (`manifest_component_unknown`), never passed through, so no hook can load outside `swarm-plugin-exec`.
7. **Dependencies are shipped, never installed:** a `package.json` that lists `dependencies` with no `node_modules`
   beside it is refused with that reason (recommended, pending Q9). The job never runs `npm install`, a build or
   any file from the archive.
8. **Digests.** `source_sha256` is over the quarantined bytes as fetched or uploaded. The **runnable tree** is the
   normalised tree plus the overlay; it is written as one tar in a canonical order (paths sorted, fixed modes, zeroed
   owners and times), and `runnable_sha256` is over that tar. The job writes it to `staging/<intake_id>/runnable.tar`
   and its report (the parsed manifest, the counts, the digests or the refusal code) to
   `staging/<intake_id>/report.json`.

**Why a staging name, and why swarm-api copies.** One job account parses every workspace's and the platform's
untrusted archives, so it must not be able to write where a pinned copy lives or touch another intake's result.
`intake_id` is 128 random bits (`secrets.token_hex(16)`) minted by swarm-api and passed to that one execution only;
the account holds a custom role with `storage.objects.get` and `storage.objects.create`, IAM-conditioned to
`quarantine/` and `staging/` respectively, and **no `list` and no `delete`** (Cloud Storage needs delete to overwrite),
so an execution cannot find, guess, overwrite or remove another intake's objects, and cannot write under `platform/`
or `tenants/` at all. The job has no route to swarm-api, so the bucket's finalize notification on `staging/`
(Pub/Sub, pushed to swarm-api with an OIDC token, as the deployment's other push subscriptions are) tells swarm-api
the intake ended; an execution that fails without a report is found by the same notification's absence after the
job's 10-minute timeout, through Cloud Run's execution state, read once. swarm-api then:

1. reads `plugin_intake/{intake_id}` and acts only when its MAC verifies; the **scope and tenant come from that
   document, never from the report**, and the version is written under that document's install;
2. parses the report as untrusted data (a closed schema, a size cap, unknown keys refused);
3. streams `staging/<intake_id>/runnable.tar`, computing its sha256 and size itself, and refuses the intake
   (`intake_digest_mismatch`) unless both equal the report's;
4. copies it to `<scope prefix>/<runnable_sha256>.tar` with `ifGenerationMatch=0`. Only swarm-api writes the scope
   prefixes, and only after hashing, so an object already at that name is the same bytes from an earlier intake in
   that scope (swarm-api still compares its stored hash), and nobody can pre-plant an object (an empty one, say)
   under a future digest to make a later intake fail;
5. writes the version document, then deletes the staging and quarantine objects, as it does on a refusal.

An execution the archive takes over can therefore lie only about its own intake: the bytes the same admin chose to
install, under the scope that admin's intake document names, which Update's diff and the review of the manifest then
show. It cannot reach a concurrent intake of another workspace or of the platform.

**Why one canonical tar and not the original archive.** The worker must verify one digest and unpack one format with
no choice to make; a zip's central directory and a tar's headers can disagree, and the intake job has already proven
this tree safe. `source_sha256` keeps the link to what upstream shipped, for the audit and for an admin who wants to
check it against a release.

## 3. Where pinned copies live

* One bucket, `<project>-swarm-plugins`, uniform bucket-level access, no public access, labelled
  `managed-by=swarm-terraform`, in the deployment's region.
* `platform/<runnable_sha256>.tar` for platform installs; `tenants/<tenant_id>/<runnable_sha256>.tar` for workspace
  installs; `quarantine/` and `staging/` for intake.
* **Reads:** each tenant GSA gets a get-only custom role (`storage.objects.get`, no `list`: an IAM condition on
  `resource.name` cannot grant listing, and the worker needs only the object it was signed with) on `platform/` and
  on its own `tenants/<tenant_id>/` only (IAM conditions on the object name prefix), so a worker of workspace A
  cannot download workspace B's private plugin even with B's digest in hand. **Writes:** `platform/` and `tenants/`
  only by swarm-api, after it re-hashed the staged tar (§2.3); `quarantine/` by swarm-api and the signed upload URL;
  `staging/` by the intake job's account, create-only; deletes by swarm-api (§9.3).
* By digest, so two workspaces installing the same bytes privately each hold a copy in their own prefix (no
  cross-workspace sharing of private objects), while two versions with the same bytes in one scope share one object.

## 4. The start of a step: install and load

### 4.1 What the worker reads

Only `metadata.dispatch.plugins`, the step's signed plugin set (§8): per plugin its install id, version id, scope,
`runnable_sha256`, the object name, the non-secret values, the secret fields' references, and the skills a step chose
when the plugin offers skills by mode. It reads no catalogue document.

### 4.2 Install

For each plugin, before the agent starts:

1. Download the object as the tenant GSA into `private/plugins/<runnable_sha256>.tar` (private: never checkpointed,
   never uploaded), streaming its sha256.
2. **Refuse the step** (`PluginUnavailable`, CANNOT_START, a fixed sentence with `install_id` and `version_id` as
   separate fields, never the plugin's name or version string, never the prompt) when the digest is not the signed one, the object is missing, or it exceeds the version's recorded size.
3. Unpack into `private/plugins/<install_id>/` with the same entry checks as the intake job (defence in depth: the
   bytes are already proven, the check costs milliseconds), then make the tree read-only.
4. Write the plugin's data folder, `private/plugin-data/<install_id>/`, for `${CLAUDE_PLUGIN_DATA}`.
5. Pass `--plugin-dir private/plugins/<install_id>` per plugin to the CLI, after the runner's own `--settings` and
   `--mcp-config`.

A plugin's copy is downloaded and verified again by every attempt, never restored from a checkpoint. **Why not in
`work/`:** `work/` is checkpointed, so a copy there would be restored into the next attempt unverified, and every
checkpoint would carry it. **Why "read-only" is not a security boundary here:** the agent runs as the worker's uid
and could `chmod` the tree back, but anything the agent can do to the plugin it can already do directly; what matters
is that a modified copy never outlives the attempt, and it does not.

Built-ins are linked as today (`plugins.link_skills`); nothing changes for them.

### 4.3 Load: every component, not only skills

`--plugin-dir` loads the plugin's skills, commands, agents, hooks and MCP servers. Two things the runner adds:

* **Skills chosen per step.** A plugin whose manifest offers skills by mode (gstack's shape) keeps the per-step skill
  choice: the worker links only the chosen skills, as today. A plugin loaded whole by `--plugin-dir` brings all its
  skills; the builder shows them, and a step may turn off any of them (`disabled_skills` in the signed set). Lane 2
  measures whether the CLI's permission rules can deny one plugin skill; if they cannot, the builder offers no
  per-skill switch for a whole-loaded plugin and says so behind an (i), rather than promise a switch that does not
  hold.
* **The runner's settings still win.** The runner's own hooks (background refusal, guards, handoff, budget) stay in
  its `--settings` file. A plugin hook runs in addition, never instead.

### 4.4 Settings and secrets into the plugin: `swarm-plugin-exec`

When the intake job builds the runnable tree (§2.3, step 8) it changes exactly one thing in the plugin's own files:
every hook command and every MCP server command, from upstream or from the overlay (§6.3), becomes

```
/opt/swarm/bin/swarm-plugin-exec <install_id> -- <the original command>
```

The launcher is a small Python program in `agent-runtime-base` (it imports `agent_worker.redact`; the worker's
Python is in every image that runs a claude-code profile). For one hook or server process it:

1. reads `private/plugin-env/<install_id>.json` (0600, written by the worker at start: that plugin's non-secret
   environment and the paths of its secret files) and sets exactly that plugin's variables, nothing of any other
   plugin;
2. reads that plugin's secret files and sets them as the variables the plugin expects (`CLAUDE_PLUGIN_OPTION_<KEY>`
   for a `userConfig` field, or the declared variable name, e.g. `TYPESAFE_API_KEY`), registering each value for
   redaction;
3. deletes from its child's environment the variables no plugin may inherit (the worker's own, the runner's
   credentials, other plugins');
4. runs the command, passes stdin through, and scrubs the child's stdout and stderr of every registered value
   (longest first, `redact.scrub_text`) before they reach the CLI, so a hook that prints its key, or the GitHub
   token it found in the checkout's git config, returns `[REDACTED]` into the transcript;
5. appends one line of counts (event, matcher, exit code, duration) to `private/plugin-runs/<install_id>.jsonl`.

**Why a launcher and not `${user_config.*}`.** CLI substitution puts a value into the command line (visible in the
process table to the agent and every other plugin) or keeps it under `~/.claude`, which is the checkpointed `work/`.
The launcher keeps each secret in one process's environment, for one plugin. The jev gates' launcher
(`swarm-jev-gate.mjs`) already works this way; this generalises it. Lane 2 measures whether CLI 2.1.283 needs the
`userConfig` values present in its own settings for a plugin with a required field to load at all; if so, the worker
writes **non-secret** values into the runner's settings (`pluginConfigs`) and a placeholder for each sensitive field,
and the launcher still supplies the real value.

The rewrite is part of the runnable tree, so its digest covers it, and the Update dialog shows it.

### 4.5 The record

`record` gains, per managed plugin: install id, name, scope, version id, `runnable_sha256`, the source commit, and
per hook event the runs and non-zero exits counted by the launcher; per MCP server whether it started; skills called
(as today, from `Skill` tool calls). Names and counts only, never arguments, output or settings values.

## 5. What a plugin needs, and what a runner profile can run

A version's `requires` (from the overlay, filled in by the admin at install time, with what the intake job found as a
suggestion: a `package.json` `engines.node`, a `.py` hook, a `bun` command):

* **runtimes:** a closed list, `node`, `bun`, `python3`, `bash`, with a minimum version for `node` and `bun`;
* **network hosts:** the hosts its hooks or servers call (`api.typesafe.ai` for both Jev plugins), shown at install,
  in the Allowed switch and in the builder;
* **profiles it supports:** any claude-code profile by default; an admin may narrow it (a browser-driving plugin to
  `claude-code-browser`).

**What a profile can run** is declared once per runner profile in its `runtime_capabilities`
(`swarm_common.profiles.RunnerProfile`, contract request 136), today `()` for every profile: request P4 (§14) sets
`claude_plugins`, `node`, `bun`, `python3` and `bash` on the five claude-code profiles, matching
`agent-runtime-base`. Until it is accepted, swarm-api keeps the same table beside `CLAUDE_CODE_PROFILES` in
`plugincatalogue.py`, with a unit test that reads the Dockerfile's `ARG` lines so the table cannot claim a runtime the
image lacks. Submission refuses a step whose profile lacks a plugin's runtime with the fixed code `plugin_runtime_missing`, the
`install_id`, the profile name and the runtime (from the closed list) as separate fields. A codex step names no
managed plugin (codex does not load Claude Code plugins).

**Egress** is not narrowed per plugin today: the agent, every plugin and anything in the checkout share the pod's
network namespace, and a NetworkPolicy cannot tell a hook from the agent. Whether a step that loads a non-built-in
plugin gets an FQDN policy limited to the platform's hosts plus the plugins' declared hosts is Q1 (recommended:
declared and shown in the first release, enforcement a follow-up lane once the cluster supports FQDN policy).

## 6. Generic settings

### 6.1 The form

Admin › Plugins (platform) and Config › Plugins (workspace) render each plugin's settings as a form generated from its
fields, `userConfig` or declared (§6.3): `string` a text box, `number` a number box, `boolean` a switch, `directory`
or `file` a path inside the checkout (validated as relative, no `..`), an enum a select; `required` marked; the
field's description behind an (i). A **sensitive** field is a write-only box with **Set**, **Replace**, **Remove**
and **Use a workspace key**; once saved it shows "set by <who>, <when>" and never its value.

Settings are **per workspace**, also for a platform plugin: a platform admin installs the code; each workspace
supplies its own values and secrets (recommended, pending Q2: no platform-held secret shared across workspaces,
because every workspace's agents could read it).

### 6.2 Where values live

* **Non-secret values:** `workspace_settings/{tenant}/plugin_configs/{install_id}`, written only by swarm-api for
  the workspace's owner or admin, audited. Each document's canonical bytes are digested; the sorted digests of all
  the workspace's config documents make `configs_digest` in the signed `plugins` block, so a forged value does not
  verify and swarm-api refuses to merge it (request P1).
* **Secret values:** Secret Manager, `swarm-tenant-<t>-plg-<16 hex>`, the hex being the first 16 of
  `sha256(install_id + "/" + field)`: derived from the workspace, the plugin and the field, never from anything a
  caller types, so no value can be addressed into another workspace's or plugin's name. Labelled
  `managed-by=swarm-api`, `tenant=<t>`, `plugin=<install_id>`. The tenant GSA holds **no** grant on them; swarm-api
  releases each one to an attempt whose signed plugin set names it (the `attempts_secrets` pattern), so a step that
  does not load the plugin cannot read the plugin's secret even though it runs in the same workspace. The value is
  entered through a one-time form (the `secrets-scopes.md` §6.3 link: 32 random bytes, ten minutes, one use, bound to
  the caller, the workspace and the destination) and never returns: not in Firestore, not in a log, not in the
  console after saving, not in the audit row.
* **A binding instead of a value.** "Use a workspace key" stores `binding: {kind: "provider_key", provider:
  "typesafe"}` instead of a secret: the step's set carries the binding, and the worker delivers the workspace's
  TypeSafe key exactly as `jevkey.resolve_workspace` resolves it for the quality gates (the workspace's key, never a
  person's), into that plugin's secret file. With the TypeSafe forwarder on (`SWARM_TYPESAFE_INJECT=on`), the worker
  never holds the key: the binding then delivers the forwarder's shim (its base URL and the attempt's ticket) and
  sets the plugin's declared base-URL variable to it, as `swarm-jev-gate.mjs` does today. Bindable keys are a closed
  list (TypeSafe first; any provider key the workspace already holds can follow), so a binding never names an
  arbitrary secret.
* **A binding brings its adapter.** The workspace's TypeSafe key is never handed to a plugin bare: a plugin bound to
  it runs its `node` hooks through the **Jev adapter**, the baked gates' launcher logic factored out of
  `swarm-jev-gate.mjs` (the key in that process only, `fetch` replaced by one that sends only to TypeSafe or the shim
  through the outbound filter, the variables that would point it elsewhere removed, and every call recorded in the
  calls file the worker already sends to `state/jev-usage`). So a managed jev-belay sends, and is accounted, exactly
  as the baked one does. Replacing `fetch` guards against **accidental** leakage only: a malicious `node` hook can
  still reach the network through `node:https`, `net` or `child_process`; malicious exfiltration is egress's (Q1). A plugin bound to TypeSafe whose hooks are not `node` commands is refused at the binding,
  with that reason. Adapters are platform code, one per bindable key, never supplied by an admin.

A secret field's name must be in `swarm-tenant-<t>-plg-...`; its consumer kind `plugin` is request P3, because
`Tenant.secret_name` and the secret-consumer kinds are frozen.

### 6.3 Declared settings and the overlay (a plugin with no `userConfig`, or no manifest)

jev-belay and jev-enforce ship no `plugin.json`, no `hooks/hooks.json` and no `userConfig`; they read environment
variables (`JEV_ENFORCE_RULES_ONLY`, `JEV_BELAY_CHECK`, `TYPESAFE_API_KEY`, ...) and a rules file. "Customisation
where needed" is the **overlay**, a small JSON document an admin writes in the install dialog, stored on the version
and part of its runnable tree:

* `plugin`: the `plugin.json` to use when the source has none (name, version, description);
* `hooks`: hook entries when the source has no `hooks/hooks.json`: event, matcher, timeout, and a command that is a
  runtime from §5 and a path **inside the plugin** plus fixed arguments (`node ${CLAUDE_PLUGIN_ROOT}/belay.mjs`),
  never a free shell line; the intake job checks the path exists;
* `settings`: declared fields, each with a type, a description, `sensitive`, and where it goes: an **environment
  variable** name, or a **file** (its content set in the form, written by the worker to
  `private/plugin-files/<install_id>/<name>` and its path put in a named variable, e.g. a rules file);
* `requires` (§5).

An overlay may add to a plugin that has its own manifest (declare `requires`, add a file setting) but never replace
its hooks, so what upstream ships is what runs. Variable names are checked against a deny list
(`PATH`, `HOME`, `LD_*`, `NODE_OPTIONS`, `PYTHON*`, `CLAUDE_*` except `CLAUDE_PLUGIN_OPTION_*`, `SWARM_*`,
`GOOGLE_*`, `GIT_*`) so a setting cannot redirect the runtime that runs the hook.

### 6.4 Per-step overrides

A step may override a **non-secret** value of a plugin it loads, through the builder's plugin sheet, as a typed value
against the same field definitions (invariant 10: values for fields the plugin declares, never a command, path outside
the checkout, image or resource spec). A secret is never overridden per step: a workflow is shared across members and
its JSON is visible to them. A **file** setting is not overridden per step either: it is released like a secret
(§8), so a per-step copy would put its content into the workflow's JSON.

## 7. Workspace defaults ("on for every step") and how they reach a step

* A workspace admin marks an allowed plugin **on for every step** (`plugins.defaults`, signed, request P1).
* **At submission** swarm-api computes the step's plugin set: the workspace defaults, plus the plugins the step adds,
  minus the defaults the step turns off (`plugins_off`), each resolved to the install's **current version at that
  moment**, with the workspace's values, the step's overrides and the secret references. It writes the set into the
  step's `metadata.dispatch.plugins` before the step is signed.
* A default the step's runner profile cannot run (a codex step, a missing runtime) is skipped for that step and
  recorded as skipped in the step's dispatch record; it does not refuse the step (recommended, pending Q6), because a
  workspace default must not break every non-Claude step.
* The builder shows defaults on every step card as chips with an "off for this step" switch, so the step's JSON says
  what it turned off and an exported workflow carries no copy of the defaults.

**Why at submission and not at the worker.** The worker trusts only what was signed. A default resolved at start from
Firestore would be a value the tenant can write; resolved at submission, it is inside the KMS-signed spec, and a step
keeps the defaults it was submitted with even if the workspace changes them while it waits in the queue.

## 8. What is signed, and what the worker trusts

The step's plugin set rides in `metadata.dispatch`, which the step spec signs whole (`specsign.
SIGNED_METADATA_KEYS`). So **no spec format bump** and **no change to the work contract document** are needed for the
step:

```
metadata.dispatch.plugins = {
  "<install_id>": {
    "name": "jev-enforce", "scope": "workspace", "version_id": "...",
    "runnable_sha256": "<64 hex>", "object": "tenants/<t>/<64 hex>.tar",
    "env": {"JEV_ENFORCE_RULES_ONLY": "1"},
    "files": {"JEV_ENFORCE_RULES_FILE": {"sha256": "<64 hex>"}},
    "secrets": {"TYPESAFE_API_KEY": {"binding": "provider_key:typesafe"}},
    "skills": null, "disabled_skills": []
  }
}
```

A file setting's content travels as a released value like a secret (it may be long, and a rules file may say more
than a member should read in a workflow's JSON), and its sha256 is what is signed. The work contract keeps
`capabilities.plugins` for the built-ins until §1.4.5's switch moves them onto this set, and a managed plugin is outside it, so managed plugins **do not
need `SWARM_WORK_CONTRACTS`**. The worker refuses a set it cannot verify exactly as it refuses an unsigned contract.

What still needs the frozen contract (§14): the workspace block's new fields (P1), the install, version and intake MAC form
(P2), the plugin secret name and consumer kind (P3), the profiles' runtime capabilities (P4) and the platform
switches' MAC form (P5).

## 9. Update and remove

### 9.1 Update

1. An admin opens a plugin and picks **Check for update**: for a GitHub or marketplace source, swarm-api resolves the
   same ref (or a new tag the admin picks) to a commit; for an upload, the admin uploads the new archive. Intake runs
   as in §2; the result is a **candidate** version, not yet current.
2. **The diff**, from two stored versions, with no fetch: `plugin.json` fields; hooks added, removed or changed
   (event, matcher, command); MCP servers added, removed or changed; skills, commands and agents added or removed;
   settings fields added, removed, retyped or newly sensitive; `requires` and network hosts; the file list with sizes
   (added, removed, changed); upstream commit range with a link to GitHub's compare page. A change to hooks, servers,
   network hosts or sensitivity is marked, because that is where a malicious update acts.
3. **Apply** (a typed confirmation of the plugin's name when hooks, servers or hosts changed) moves
   `current_version`. A new required setting with no value blocks the apply until it is set.

**What happens to steps:** a step already submitted, queued, parked (waiting for an answer or quota) or running keeps
the version it was signed with, through every retry and resume, because its digest is in its signed spec. A step
submitted after the apply gets the new version. A scheduled workflow's next run is a new submission, so it gets the
new one. There is no automatic update (a malicious push upstream changes nothing until an admin applies it).

### 9.2 Remove

**Remove** first lists who uses it: workspaces that allow it (platform scope), workspaces where it is a default,
saved workflows and agents whose steps name it, schedules that run those workflows, and steps queued or running with
it now. Confirming (typed name) sets `state: removed`: it leaves the catalogue, the Allowed lists and the defaults; a
saved workflow naming it is refused at submission with the fixed code `plugin_removed` and its `install_id` as a
field (recommended, pending Q5); queued
and running steps keep their copy. The plugin's secrets (`-plg-` names for that install) are destroyed when the
install is removed. A bundled install (§1.4) has no Remove (Q17).

### 9.3 Garbage collection

A pinned object is deleted when no version that is current, no candidate, and no step that is not finished names its
digest, and it is older than 7 days. The reconciler's existing sweep runs this daily; the delete is by
`ifGenerationMatch`, so a concurrent intake of the same bytes is never lost.

## 10. Audit events

Each written to `admin_audit` in the same transaction as the change it records (`{action, by, at, target, detail}`),
`detail` naming ids, version ids, digests, field names and counts, never a value and never an upstream string:

`plugin_intake_started`, `plugin_intake_refused` (the fixed code and the entry's index, §2.3), `plugin_installed`, `plugin_update_checked`,
`plugin_updated` (old and new version and digest), `plugin_removed`, `plugin_allowed_set` (as today's
`workspace_plugins_set`), `plugin_defaults_set`, `plugin_config_set` (field names), `plugin_secret_set` and
`plugin_secret_removed` (field name), `plugin_secret_bound` (field and provider), `platform_switch_set` (switch, old
and new value). The step's result (§4.5) is the run-time half.

## 11. The platform switches as console settings

`SWARM_WORK_CONTRACTS`, `SWARM_CONTRACT_REVIEW` and `SWARM_PLUGINS` become three controls on Admin › Plugins ›
Platform: two switches and a three-way select, each showing its Terraform default beside it, and "Reset to default".

* **The plugins switch** (the owner's pick on PR 3648) is new: `on` lets a step name plugins as before (work
  contracts on, the plugin in the workspace's signed allowed list); `off` refuses every step that names a plugin at
  submission, before any other plugin check (`swarm_api.plugins.enabled`, `check_step`). Its Terraform value is
  `var.plugins_switch`, **`on`** by default and read as `on` when unset, so it refuses nothing a caller relied on
  until a platform admin turns it off.
* **Two MACs.** The contract's canonical form (request P5, contract request 144) covers only `work_contracts` and
  `contract_review`, and `swarm_common` is frozen. So the document carries a second MAC, `switches_mac`, under the
  same key ring over swarm-api's own canonical form (`platformswitches.canonical_switches`, purpose
  `swarm.platform-switches/v1`, every switch); a document is acted on only when both verify, so a tenant worker that
  writes `plugins` alone leaves the contract's MAC good and is still refused. Contract request 150 asks to add
  `plugins` to `PLATFORM_SETTINGS_MAC_FIELDS`.

* **Stored** in `platform_settings/switches` with a MAC under `swarm-settings-key` over a canonical form with its own
  purpose (`swarm.platform-settings/v1`, request P5), so a value copied from a workspace block never verifies.
* **Read** by swarm-api through one function per switch that returns the stored value when the document exists and
  its MAC verifies, else the environment variable (the Terraform value), else `off`. The read is cached for 30
  seconds per instance, so a change reaches every instance within half a minute without a redeploy. The environment
  read stays (`scripts/lib/check-env-parity.sh` still sees swarm-api read what Terraform sets).
* **A MAC that fails** reads as the Terraform default, never as the stored value, and logs `platform_switch_unsigned`
  once per instance.
* **Who:** a platform admin; audited (`platform_switch_set`); a confirmation dialog naming what changes (turning
  contract review to `enforce` refuses steps; turning work contracts off, before §1.4.5's switch, makes steps that name built-in plugins
  refused) (recommended, pending Q11).
* Each workspace's own `contract_review.enabled` block stays; the platform mode composes with it as today.

## 12. Threats, read as an attacker

A plugin's hooks and MCP servers run code in every agent container that loads it, with that workspace's secrets as
the step holds them, its GitHub token, its checkout and the tenant GSA's metadata-server token.

| Attack | What stops it |
|---|---|
| **A workspace member installs a plugin** to run code in every step | Only a workspace owner or admin installs, updates, removes, allows, sets defaults or settings (recommended, pending Q3); a member only picks allowed plugins in the builder. A plugin runs nowhere until an admin allows it, and in no step that does not name it or have it as a default. |
| **A workspace admin installs a platform plugin** | Platform scope needs a platform admin; the route checks the role server-side. |
| **A platform plugin reads a workspace's secrets** | It runs only inside a workspace's own step container, so it sees what that step sees: the secrets that step's plugins and slots release, its forge token, its tenant GSA. It cannot reach another workspace's secrets (the tenant GSA has no grant on them, and the plugin-secret release checks the step's signed set) or another workspace's objects (IAM conditions per prefix). What it can read in the step is the honest residual: allowing a platform plugin is trusting it with that workspace's steps, which the Allow switch says behind its (i). |
| **A malicious update upstream** (a compromised maintainer, a moved tag, a force-push) | Nothing updates on its own; a tag is resolved to a commit once, and the copy is ours. Update shows the diff with hooks, servers, hosts and sensitive fields marked, and needs a typed confirmation. A second approver for platform updates is Q4. |
| **A tampered pinned copy** | The intake job's account writes only create-only, unlistable `staging/<intake_id>/` names (128 random bits, no list, no delete), so a take-over of one execution cannot plant a copy under `platform/` or `tenants/` or forge a concurrent intake's report. swarm-api binds each report to its MAC'd intake document's scope and tenant, re-hashes the staged tar itself, and alone copies it into the scope prefix, never overwriting (`ifGenerationMatch=0`); the worker checks the sha256 against the signed digest before unpacking. |
| **Attacker text in a refusal** (an entry path or manifest name crafted to spoof a message or a log line) | Every refusal is a fixed code and sentence; only platform-minted ids (install, version, intake, an entry's index) travel, as separate fields (§2.3). |
| **A forged catalogue document** (a tenant worker writes a version document with its own digest) | swarm-api acts only on documents whose MAC verifies (P1, P2); the worker reads none of them. |
| **An archive escaping its folder** (`../`, absolute paths, symlinks out, hard links, devices, zip bombs, case collisions) | §2.3: refused from the index before writing, caps counted on bytes written, unpacked by a job with no network, no secrets and no Firestore; the worker checks the entries again. |
| **A hook printing a secret** | The launcher scrubs every registered value from the hook's output before the CLI sees it; the worker's existing scrub runs over the transcript and logs at the end. A secret the step never registered (one the hook found on disk) is not caught by a literal scrub: that residual is the same as the agent's own today. |
| **A hook reading another plugin's secret** | Each plugin's secrets are set only in its own launcher's child; files are per plugin. Within one container the same uid can read another plugin's 0600 file: plugins loaded in the same step trust each other, as the agent and its tools already do. |
| **Egress to exfiltrate** | Today an agent step reaches the internet minus private ranges whatever plugins it loads; per-step FQDN restriction is Q1. A plugin's declared hosts are shown before an admin allows it. |
| **A workspace plugin reaching another workspace** | Its object sits under its workspace's prefix (other GSAs cannot read it), its install id is refused at submission for any other tenant, it runs in its own workspace's namespace under that workspace's GSA, and nothing on the platform catalogue lists it. |
| **A setting redirecting the runtime** (`NODE_OPTIONS=--require ...`, `PATH`, `LD_PRELOAD`) | §6.3's deny list, checked at install and at every settings write and override. |
| **A plugin hook that blocks a step forever, or answers "ask"** | Every hook has a timeout (the manifest's, capped at 120 s); the step's own wall-clock and turn limits stand. Nobody can answer "ask" in a headless step: lane 2 measures what CLI 2.1.283 does with a plugin hook's "ask" under `-p`, and the launcher rewrites an "ask" decision to "deny" if the CLI would otherwise wait; the result counts it. |
| **A hook sending a secret or the code out** (in its request to its own API) | Output scrubbing does not cover what a hook sends. For a plugin bound to the TypeSafe key, the Jev adapter (§6.2) applies the baked gates' outbound filter: `fetch` only to TypeSafe or the shim, secret-shaped files sent by name only, the redaction rule families dropped, registered secrets replaced (`swarm-jev-gate.mjs`, "THE OUTBOUND FILTER"; `docs/jev-data-flow.md`). The adapter is not a boundary against a malicious hook (`node:https`, `net`, `child_process` bypass a replaced `fetch`): it stops accidental leakage only, and stopping deliberate exfiltration is Q1's egress. Any other plugin's requests are not filtered: a plugin in another runtime, or one with its own HTTP client, sends what it sends to the hosts it declared, and allowing it is trusting it with that. Q1 decides whether egress is narrowed to those hosts. |
| **Planting state across attempts** (`${CLAUDE_PLUGIN_DATA}`) | The data folder is in `private/`, not checkpointed; a plugin that needs state across a park says so, and is refused until the question bridge design covers plugin state. |

## 13. CONTRACT.md invariants

1. **Demand:** an intake job is not a step and creates no agent demand; a step loading plugins is sized and
   admitted exactly as before. A queued step holds a digest, not a container.
2. **All-or-nothing reservation:** untouched.
3. **Concurrency from LEASED:** untouched; intake jobs are bounded by the job's own parallelism (one per intake, at
   most 4 at once per deployment), not by tenant slots.
4. **No sleeping through a wait:** the worker downloads at start (a bounded transfer of at most 100 MiB per plugin);
   nothing waits on an outside party. Intake polls nothing: the report object's finalize notification (Pub/Sub push
   to swarm-api) ends the intake.
5. **Fencing:** a stale attempt exits before it downloads or runs anything, as before; the launcher starts only
   under the runner, after fencing.
6. **Spot:** the intake job is a Cloud Run job; nothing changes.
7. **Requests equal limits:** no pod spec changes; downloads land on the attempt's existing private storage, counted
   against its existing ephemeral-storage request, and the cap per plugin keeps the sum predictable (at most 8
   plugins per step).
8. **Checkpointing:** unchanged; the plugin copy is deliberately outside the checkpoint and re-verified per attempt.
9. **Per-tenant isolation:** workspace plugins live under the tenant's own GCS prefix and its own Secret Manager
   names, run in its own namespace under its own GSA; platform plugins hold no shared secrets (Q2).
10. **Callers name, never send:** a step names install ids and values for declared fields. Code, commands, paths and
    hosts come only from a pinned copy an admin installed through a privileged, audited catalogue route, the way a
    platform admin registers an MCP server (`registered-mcp-servers.md`); a step's submission can never supply them.

**The bundled installs (§1.4) against the same invariants.** A bundle build is not a step: it runs as one execution of
the Cloud Run job `swarm-plugin-bundle` (since 11 November 2026; Cloud Build before), outside every tenant's namespace,
holds no tenant slot and no lease, and creates no agent demand (1, 3). A step naming a bundled plugin is sized and
admitted as before; only the image digest it runs differs, and a step whose bundle is missing is refused at dispatch
before any pod exists, so nothing waits in a pending pod (1). The pod spec is unchanged apart from the image: requests,
limits and the CPU ratio are the runner profile's, as today (7); Spot stays off (6). Fencing is untouched: a stale
attempt exits before it reads a bundle manifest (5). Checkpointing is unchanged: a bundled copy is in the image, never
in the checkpoint (8). A bundle carries no secret and no tenant's data, and its images are pulled the way the release
images are (9). The image digest comes from swarm-api's MAC'd bundle record and is checked against
the bundle repository by the scheduler and against the signed pins by the worker; a caller still names a runner
profile and nothing else (10). The Setup step (§15.1) touches none of them.

## 14. Contract change requests (requests, not changes: `swarm_common` is frozen)

* **P1. `specsign.SETTINGS_MAC_FIELDS["plugins"]`** becomes `("allowed", "defaults", "configs_digest")`: the
  workspace's every-step plugins and the digest of its plugin settings are signed with the Allowed list. An absent key
  projects as null, so every block stored today still verifies.
* **P2. A canonical form for a plugin install, a plugin version and a plugin intake** (`specsign.canonical_plugin_record(kind,
  fields)`, purpose `swarm.plugin-record/v1`, kinds `install`, `version` and `intake` (§2.3 binds a report to its
  intake's scope), with the scope and the tenant id inside
  the bytes), MAC'd under `swarm-settings-key` by swarm-api.
* **P3. A plugin secret:** `Tenant.plugin_secret_name(install_id, field)` returning
  `swarm-tenant-<t>-plg-<16 hex>`, and the secret-consumer kind `plugin` so a released secret's record says which
  plugin it was for.
* **P4. `RunnerProfile.runtime_capabilities`** on the five claude-code profiles: `("claude_plugins", "node", "bun",
  "python3", "bash")`.
* **P5. A canonical form for platform settings** (`specsign.canonical_platform_settings(fields)`, purpose
  `swarm.platform-settings/v1`, fields `work_contracts` and `contract_review`).
* **P6. A plugin record kind `bundle`** (§1.4.3): `specsign.PLUGIN_RECORD_KINDS` gains `"bundle"`, so a
  `plugin_bundles/{bundle_id}` record is MAC'd in P2's canonical form, platform scope, no tenant id. Without it a
  tenant worker could forge a bundle record; the scheduler's repository check and the worker's manifest check would
  still stop a wrong image from running a step, but a forged `ready` could move `current_version` to an unbuilt pin.
  Lane 13 writes it into `docs/contract-change-requests.md`; it is in `questions.json` as Q20.

Each goes into `docs/contract-change-requests.md` through the lane that builds on it, and is in `questions.json` as
Q12 for the owner to accept.

## 15. The Day One installer and the optional Setup step

### 15.1 The optional "add a plugin" step

**Decided by the owner on 6 November 2026 (Q15), against this document's earlier recommendation of no step:**
Admin › Setup offers an **optional** "add a plugin" step.

**Where the checklist lives.** The page is `apps/swarm-ui/src/AdminSetup.tsx`, which polls `GET /v1/admin/setup`
(`useSetupState` in `AdminSetupSteps.tsx`). The compact checklist beside the working panel is `finishLines` in
`consolefinish/ChecklistFinish.tsx`: eight lines (`LineKey`: `cloud`, `sign-in-client`, `github`, `team`, `accounts`,
`smoke`, `verify`, `send`), each derived on every read and never marked by hand. The panel's rows are built in
`AdminSetupSteps.tsx` (`signInRow`, `githubRow`, `teamRow`, `accountsRow`, `verifyRow`, ...), and one of them is
already optional: `addonsRow`, `by: 'optional'`. The server side is `apps/swarm-api/swarm_api/routes/setup.py`
(`setup_state`, `steps_view`), with the console's documents under `swarm-install/{env}/console/` in the table
`install_record.SWARM_API_DOCUMENTS`, which restates the installer's `swarm_install.record.WRITERS`.

**What the step is.**

* **A ninth line, last, after "Send to your team"**: `plugins`, titled "Add a plugin", marked **Optional** the way
  `addonsRow` is. Last, because the eight lines are the order a deployment becomes usable in, and a line placed
  between them would read as a gate.
* **Its state is derived, like every line:** `done` once the deployment holds at least one active install that is
  not bundled (a platform install, or any workspace's own), counted by swarm-api from `pluginstore`; `skipped` when
  an admin skipped it and none exists; else `todo`. The two bundled installs (§1.4) never count, because they are
  there without anyone adding them.
* **The panel shows** one sentence of what is there already ("gstack and security-audit are installed"), the count
  of plugins added, and two actions: **Add a plugin**, a link to Admin › Plugins › Add (`ADMIN_PLUGIN_ADD_PATH`,
  `/admin/plugins/add`, the page `AdminPluginInstall.tsx` already renders, unchanged; the line turns `done` on the
  next poll once the install is active), and **Skip**. Once done it lists the plugins added, each linking to its page.
* **Its helper text goes behind a tooltip.** What a plugin is, that the platform catalogue serves only the
  workspaces that allow it, and that it can be done any time from Admin › Plugins sit behind the line's (i), not in
  the panel: the console's prose ratchet (`src/__tests__/prose.budget.test.tsx`) holds the page's visible words where
  they are.
* **Skip** is a platform admin's choice, remembered in a new console document
  `swarm-install/{env}/console/plugins_step` (`{skipped_at, skipped_by}`), written by
  `POST /v1/admin/setup/plugins/skip` and removed by `DELETE` of the same route ("Add one anyway" on a skipped line),
  audited as `setup_step_skipped`. Remembered, because a line that came back as `todo` on every poll would be a nag on
  a page whose job is to say what is left.
* **It never blocks ready.** "Send to your team" unlocks on `verify` passing, as now; nothing in the engine's or the
  console's readiness reads `plugins`; `nextLineKey` (the line the panel follows by itself) is taught to pass over an
  optional line, so the page never moves a person there unasked. A one-person install shows the same line.

### 15.2 What the installer gains

The rule is that anything a fresh deployment needs also updates the installer. What this wave needs:

* **Terraform, applied by the passes `swarm install` already runs** (so no prompt): the plugins bucket and its IAM
  (§3), the intake job and its account (§2.2), the reconciler's GC grant (§9.3), the Secret Manager grants for `-plg-`
  names, and now the bundle build's resources (§1.4.2): the `swarm-plugin-bundles` repository, the job
  `swarm-plugin-bundle` and its log sink, the builder account's grants as §1.4.2 lists them after the change of 11
  November 2026 (read `platform/sources/`, read the deployment's release images, read the one pinned Bun object, read
  the npm mirror, push to that repository only), swarm-api's `swarmPluginBundleRunner` bound on that job and nowhere
  else, and the mirror repositories of Q19. No Cloud Build permission for swarm-api and no `actAs` on the builder.
* **Terraform the owner applies** (bootstrap, from a runbook on an existing deployment; Day One's bootstrap pass on a
  fresh one): the builder account, the custom role `swarmPluginBundleRunner`, the release deployer's `actAs` on the
  builder, and the removal of the reverted Cloud Build rows (§1.4.2, Day One and rollback). No default build account
  check is needed for bundle builds any more: no Cloud Build build runs for one.
* **The bundler image** in the release's `images.json`, so stage 4 (`steps/images.py`) copies and verifies it like
  every other image.
* **`builtins_managed = "on"`** rendered by `tfvars.py` for a fresh deployment, which has no queued step to carry
  across; an existing deployment keeps `off` until a platform admin turns it on (§1.4.5).
* **No seeding by the installer.** swarm-api seeds the two bundled installs on its first catalogue read (§1.4.1),
  from the release image's own `bundled-plugins.json`, so the installer writes no catalogue document.
* **The console document** `console/plugins_step` added to `swarm_install.record.WRITERS` beside swarm-api's
  `SWARM_API_DOCUMENTS`, so the record's writer table and swarm-api's agree; the installer reads it as it reads the
  others and copies nothing from it.
* **Checks.** `swarm doctor` and the install's verify stage (`steps/verify.py`, `verify.plugins`) gain: the bucket
  exists, the intake job is deployed, a tenant GSA can read `platform/`, the builder account can push only to
  `swarm-plugin-bundles`, the job `swarm-plugin-bundle` exists with the builder as its identity and the release's
  bundler digest as its image, swarm-api's binding on it is the only non-owner binding in its policy, and, with the switch on, the two bundled installs exist and their current versions equal
  the live release image's `.swarm-bundle.json`.
* **`swarm upgrade`** gains the bundle rebuild between copying images and applying infra (§1.4.4), shown in its plan
  ("bundles to rebuild: N"), applying the job alone first so it runs the new bundler image, and refuses a release
  without bundle manifests while the switch is on.

The platform switches keep their Terraform defaults (`contract_review_mode` stays `off` in `tfvars.py`) until a
platform admin changes them in the console. The Setup step needs no installer change beyond the document's row: it
reads swarm-api's catalogue.

## 16. The test: jev-belay and jev-enforce, managed

**Done means** one workflow's result shows both plugins' hooks ran, from copies installed through the console.

1. **jev-belay 0.2.0 from GitHub.** A platform admin installs `valentynkit/jev-belay` at tag `v0.2.0`. swarm-api
   resolves it to `ef719db7eaadc56aa4def86c4da4ffff5bcbca35` (the commit the image vendors today; the test asserts the
   resolution equals it). There is no `plugin.json`, so the admin writes the overlay: `plugin` (name `jev-belay`,
   version `0.2.0`); one hook, `Stop`, `node ${CLAUDE_PLUGIN_ROOT}/belay.mjs`, timeout 25 s (today's gate value);
   `requires`: `node >= 20`, host `api.typesafe.ai`; a declared secret `TYPESAFE_API_KEY` and a declared value
   `JEV_BELAY_CHECK`. The test asserts the intake's `belay.mjs` sha256 equals the file `tests/unit/worker/
   test_quality_gates.py` already pins.
2. **jev-enforce 1.1.1-owner.3 as an upload.** The owner's fork of `erkamyaman/jev-enforce`, on no remote, uploaded as
   a `.tar.gz` by a workspace admin as a **workspace** plugin. The admin's overlay: `plugin` (name `jev-enforce`,
   version `1.1.1-owner.3`); hooks `Stop` → `node ${CLAUDE_PLUGIN_ROOT}/dist/src/cli.js hook stop` and `PostToolUse`
   matching `Edit|Write|MultiEdit` → `... hook post-edit`, 30 s each; declared settings `JEV_ENFORCE_RULES_ONLY`
   (boolean), `JEV_ENFORCE_MODE` (`end`), a **file** setting `JEV_ENFORCE_RULES_FILE` (the rules file, entered in the
   form), and the secret `TYPESAFE_API_KEY`. The intake refuses the upload if its `package.json` lists dependencies
   without `node_modules` (jev-enforce 1.1.1 has none). The exact variable names come from the fork's own README and
   source at intake time; the test reads them from the stored manifest, never from this document.
3. **Configure both with the workspace's TypeSafe key:** the workspace admin allows jev-belay (platform) and, on each
   plugin's `TYPESAFE_API_KEY`, picks **Use a workspace key › TypeSafe**. No key is pasted.
4. **jev-enforce on for every step.** The workspace admin marks it a default.
5. **Run one workflow:** one claude-code step that adds jev-belay in the builder and edits one file. Its result's
   `plugins` record shows jev-belay (`Stop` ≥ 1 run) and jev-enforce (`PostToolUse` ≥ 1 run, `Stop` ≥ 1 run), each
   with its version and digest, exit codes counted; the attempt's Jev usage shows the hooks' calls (the adapter's
   calls file, §6.2); and the transcript and logs hold no TypeSafe key (the test scans them).
6. **The step's quality-gates switch is off** for this run (`quality_gates.jev`), so the baked gates do not also run
   and the counts are the managed plugins' alone. What happens when both are on is Q14.

The test is a `make smoke`-style check against a deployed environment (lane 12), plus the offline unit tests each
lane carries (intake checks on crafted archives, digest refusal, launcher scrub and env isolation, submission merge
and signing, MAC refusal).

## 17. The lane plan

In order. Territories are disjoint; "needs" names what must merge first. Console lanes hold about three UI files each
and **wait for the console mock-ups** the owner picks from (a separate lane renders two directions now); every console
lane keeps the prose guard's count where it is or lower, explanations behind (i) and tooltips.

| # | Lane | Territory | Needs | Gives |
|---|---|---|---|---|
| 1 | **Contract requests P1-P5** | `docs/contract-change-requests.md`; then `apps/common/swarm_common/specsign.py`, `models.py`, `profiles.py` only by the owner's acceptance | the owner's answer to Q12 | the signed shapes |
| 2 | **Loading spike** (no merge to the product path) | `docs/design/managed-plugins-spike.md`, `images/agent-runtime-base/spikes/plugin-dir/` | nothing | whether CLI 2.1.283 headless loads hooks, MCP servers, commands and agents from `--plugin-dir`, takes `userConfig` without a keychain, and expands `${CLAUDE_PLUGIN_ROOT}` inside a wrapped command; jev-belay and jev-enforce hand-wrapped as plugins and run |
| 3 | **Infrastructure** | `terraform/infra/plugins.tf` (bucket, IAM conditions, intake job and account, Secret Manager grants), `terraform/infra/variables.tf` (the bucket name), `apps/swarm-install/swarm_install/steps/verify.py` and `verbs/doctor.py` (the check) | nothing | the bucket and the job's slot, in every new deployment |
| 4 | **Intake job** | `apps/plugin-intake/` (new: unpack, checks, normalise, digest, report), its Dockerfile, its tests | 3 | safe pinned copies |
| 5 | **Catalogue store and routes** | `apps/swarm-api/swarm_api/pluginstore.py` (new), `routes/plugin_catalogue.py` (new), `plugincatalogue.py` (built-ins as entries in the data view) | 1 (P2), 3 | installs, versions, audit; the GitHub/marketplace resolution and the fetch into quarantine; the signed upload URL; intake callback |
| 6 | **Settings and secrets** | `apps/swarm-api/swarm_api/pluginconfig.py` (new), `routes/plugin_config.py` (new), `routes/attempts_secrets.py` (plugin releases) | 1 (P1, P3), 5 | forms' data, one-time secret links, bindings, defaults |
| 7 | **Submission merge and signing** | `apps/swarm-api/swarm_api/plugins.py`, `schemas.py` (`plugins_off`, overrides), `service.py` (the merge before signing) | 5, 6 | `metadata.dispatch.plugins` on every step |
| 8 | **Worker install, load and launcher** | `apps/agent-worker/agent_worker/pluginload.py` (new), `runners/claude_code.py` (`--plugin-dir`), `images/agent-runtime-base/swarm-plugin-exec` and its Dockerfile lines, `agent_worker/plugins.py` (`record`), the Jev adapter factored out of `images/agent-runtime-base/jev-gates/bin/swarm-jev-gate.mjs` (the quality-gates lane's file, crossed with the reason in the commit) | 2, 7 | hooks, skills, commands, agents and MCP servers running from pinned copies |
| 9 | **Platform switches** | `apps/swarm-api/swarm_api/platformswitches.py` (new), `routes/platform_switches.py` (new), the two readers in `workcontract.py` and `contractreview.py` | 1 (P5) | console-set switches with Terraform defaults |
| 10a | **Console: Admin › Plugins** (catalogue, install dialog with source and overlay, platform switches) | `apps/swarm-ui/src/AdminPlugins.tsx` (new), `AdminPluginInstall.tsx` (new), `styles/admin-plugins.css` (new) | 5, 9, **the mock-ups** | |
| 10b | **Console: a plugin's page** (versions, Update with the diff, Remove with its users) | `apps/swarm-ui/src/PluginDetail.tsx` (new), `PluginDiff.tsx` (new), `styles/plugin-detail.css` (new) | 5, **the mock-ups** | |
| 10c | **Console: Config › Plugins** (Allowed, every step, the generated settings form, secrets and bindings, workspace installs) | `apps/swarm-ui/src/ConfigPlugins.tsx`, `PluginSettingsForm.tsx` (new), `styles/plugins.css` | 6, **the mock-ups** | |
| 10d | **Console: the builder** (defaults as chips with "off for this step", added managed plugins, non-secret overrides) | `apps/swarm-ui/src/PluginSheet.tsx`, `SubmitWorkflow.tsx`, `types.ts` (plugin types only) | 7, **the mock-ups** | |
| 10e | **Console: shared wiring** (routes, API calls, navigation) | `apps/swarm-ui/src/api.ts` (plugin calls only), `App.tsx`, `Spine.tsx` | 10a-10d's shapes agreed | |
| 11 | **Garbage collection and audit views** | `apps/reconciler/reconciler/plugingc.py` (new) | 5, 8 | §9.3 |
| 12 | **The test** | `scripts/smoke-managed-plugins.sh` (new), `tests/smoke/managed_plugins/` (new) | 8, 10a-10d, 11; and the owner's jev-enforce 1.1.1-owner.3 archive, which is on no remote: he uploads it through Admin › Plugins himself (the step that proves the upload path), and the smoke reads the stored version | §16 against a deployed environment |

Two lanes never edit the same file: 7 owns `schemas.py` and `service.py` for this wave, 8 owns `agent_worker/plugins.py`,
10e alone touches `App.tsx`, `Spine.tsx` and `api.ts` (the other console lanes export their pages and calls for it).
Lanes 1, 2, 3 and 4 start together. 5 needs P2 accepted; until then it can build everything but the MAC, which it
must not ship without. The console lanes wait for the mock-ups and for the routes they draw.

### 17.1 Lanes for the two decisions of 6 November 2026 (Q13, Q15)

Same rules as above: territories are disjoint for this wave, "needs" names what must merge first, a UI lane holds
about three files. The last column says what an operator must push by hand after the owner sees the diff (IAM,
Terraform, a workflow file, which a lane writes as `workflow-lines.patch` and never pushes) and what is a contract
change **request** (an entry in `docs/contract-change-requests.md`, never an edit under `apps/common/swarm_common/`).

| # | Lane | Territory | Needs | IAM / Terraform / workflow / contract | Gives |
|---|---|---|---|---|---|
| 13 | **Contract request P6** | `docs/contract-change-requests.md` (the P6 entry) | the owner's answer to Q12 for P6 | contract: request only; `specsign.PLUGIN_RECORD_KINDS` gains `"bundle"` only by the owner's acceptance | a MAC'd bundle record |
| 14 | **Bundle recipes and the bundler image** | `images/plugin-bundler/` (new: Dockerfile, `recipes/gstack/`, `recipes/security-audit/`, the `.swarm-bundle.json` writer, and, since 11 November 2026, the job's entrypoint: the argument check, the environment scrub, the one-agent and deadline rules, and the daemonless layer append and push of §1.4.2), `images/agent-runtime-base/Dockerfile` (its two build stages run the recipes and write the manifests), `images/agent-runtime-base/bundled-plugins.json` (new), `images/agent-runtime-browser/Dockerfile` (the browser variant's recipe call), `scripts/build-images.sh` and `scripts/push-images.sh` (the `swarm-plugin-bundler` target), `tests/unit/scripts/test_plugin_bundle_recipes.py` (new), `test_gstack_image.py` and `test_security_audit_image.py` (pins read from `bundled-plugins.json`) | nothing | none expected; if a release workflow lists image names itself, the line goes in `workflow-lines.patch` | release images with bundle manifests, byte-equal plugin trees to today's (the test holds the staged trees' digests equal before and after) |
| 15 | **Infrastructure and installer** | `terraform/infra/plugin-bundles.tf` (new), `terraform/infra/variables.tf` (`builtins_managed`, default `off` with its why beside it), `terraform/infra/locals.tf` (the two env vars: `SWARM_BUILTINS_MANAGED` to swarm-api, `PLUGIN_BUNDLE_REPOSITORY` to swarm-api and the scheduler), `apps/swarm-install/swarm_install/tfvars.py`, `record.py` (`console/plugins_step`), `steps/verify.py`, `verbs/doctor.py`, `upgrade.py` (the rebuild step and its refusal) | 14 (the bundler image's name) | **IAM and Terraform** (changed 11 November 2026): in `plugin-bundles.tf`, the job `swarm-plugin-bundle`, swarm-api's binding of `swarmPluginBundleRunner` on that job alone, the builder's five grants of §1.4.2 (the build mirror read conditioned on the one Bun object; the Docker Hub mirror read and the project-level `roles/logging.logWriter` row removed), the log sink, the repository, the npm mirror (Q19); in `terraform/bootstrap`, by the owner from a runbook: the role `swarmPluginBundleRunner`, the release deployer's `actAs` on the builder, and the removal of `plugin_bundle_builds.tf` (the reverted Cloud Build rows: no `cloudbuild.*` role and no `serviceAccountUser` for swarm-api). No default build account check (§1.4.2, finding 5); `upgrade.py`'s rebuild step applies the job alone first | §1.4.2's slot and §15.2 in every new deployment and upgrade |
| 16 | **Bundle builds in swarm-api** | `apps/swarm-api/swarm_api/pluginbundle.py` (new: bundle records; since 11 November 2026 one `jobs.run` of `swarm-plugin-bundle` with the arguments of §1.4.2 and no other override, the capped `executions.get` read, the pushed-manifest check before `ready`; retention list; no Cloud Build client), `routes/plugin_bundles.py` (new: build status, Roll back), `pluginstore.py` (`build: "image"`, Apply that starts a build, `current_version` moved only by a ready bundle) | 13, 14, 15 | none (uses 15's binding on the job) | §1.4.3, §1.4.7 |
| 17 | **Seed and the switch at submission** | `apps/swarm-api/swarm_api/plugincatalogue.py` (the seed source; the code entries kept one release), `pluginconfig.py` (bundled installs merged as managed entries with the step's mode and skills), `plugins.py` (no `capabilities.plugins` for them with the switch on), `routes/plugin_catalogue.py` (Update allowed on a bundled install, Remove refused) | 16 | none | §1.4.1, §1.4.5 |
| 18 | **Scheduler: the bundle image** | `apps/scheduler/scheduler/dispatch.py` (the image lookup for a bundle key, the repository check, `plugin_bundle_missing`), `scheduler/settings.py` (`PLUGIN_BUNDLE_REPOSITORY`) | 16 (the record shape), 15 | none; a dispatch change, so it also runs `make smoke concurrency-test race-test` against a deployed environment | §1.4.4's image choice |
| 19 | **Worker: bundled entries and the review** | `apps/agent-worker/agent_worker/pluginload.py` (a `build: "image"` entry checked in place against `.swarm-bundle.json`, nothing fetched), `agent_worker/plugins.py` (both routes for one release; gstack's environment keyed on the reserved id), `agent_worker/security_review.py` (the path constant, `applies()` on either route, the manifest check before the validators) | 14, 17 | none | §1.4.4's worker check, §1.4.6 |
| 20a | **Console: a bundled plugin's page** (Update with the diff, then the build's progress and its refusal, Roll back) | `apps/swarm-ui/src/PluginDetail.tsx`, `PluginBundleBuild.tsx` (new), `styles/plugin-detail.css` | 16, 20b | none | |
| 20b | **Console: catalogue words and the calls** | `apps/swarm-ui/src/AdminPlugins.tsx` (the built-in card's words and (i), which today say an image release updates it), `types.ts` (bundled install and bundle fields only), `api.ts` (bundle calls, and the Setup step's `plugins` state and skip call for lane 21) | 16, 22 | none | |
| 21 | **Console: Setup's optional plugin step** | `apps/swarm-ui/src/consolefinish/ChecklistFinish.tsx` (the ninth line, optional, `nextLineKey`), `AdminSetupSteps.tsx` (the row: panel, Add a plugin, Skip, "Add one anyway"), `styles/console-finish.css` | 20b, 22, **a mock-up first** | none | §15.1 |
| 22 | **swarm-api: Setup's plugin step** | `apps/swarm-api/swarm_api/routes/setup.py` (`plugins` in the setup state, the skip routes, the audit), `install_record.py` (`console/plugins_step` in `SWARM_API_DOCUMENTS`) | nothing (reads `pluginstore` only); its writer row lands with 15's `record.py` | none | §15.1's state |
| 23 | **The test, bundled** | `tests/smoke/managed_plugins/test_bundled.py` (new), `scripts/smoke-managed-plugins.sh` (the bundled pass) | 18, 19, 20a | none | a step naming gstack and security-audit runs from the seeded installs with the work-contracts switch off; an update to a test pin builds, applies, runs, and rolls back; a step queued before the apply keeps the old pin |

**The mock-up rule.** Lane 21 needs the owner's pick from a mock-up **first**: it adds a line and two states
(optional, skipped) to Admin › Setup, a page built from a picked frame (`docs/web-ui/mockups/PICKS.md`: build the picked
variant only), and no picked frame draws an optional line or a skip. Lane 20a adds one state (a build in progress or
refused) to the plugin page lanes 10a-10b built from their mock-ups; it extends those frames and needs no new one
unless the owner asks. 20b changes words only.

**Order.** 13, 14 and 22 start at once; 15 follows 14; 16 needs 13, 14 and 15 (it must not ship the bundle record
without P6's MAC); 17 and 18 follow 16; 19 follows 17; the console lanes follow their routes; 23 is last. Lane 15
crosses into lane 3's `steps/verify.py` and `verbs/doctor.py`, and lane 17 into lane 5's `plugincatalogue.py` and
`routes/plugin_catalogue.py`; both say so in their commits. `api.ts` is 20b's alone in this wave, so 21 imports its
calls rather than adding its own.

## 18. Questions for the owner

In the lane's `questions.json`, each with options and a recommendation:

* **Q1** Egress for steps that load a non-built-in plugin.
* **Q2** Whether a platform plugin may hold a platform-wide secret.
* **Q3** Who may install workspace-private plugins.
* **Q4** A second approver for platform plugin updates.
* **Q5** What a saved workflow naming a removed plugin does.
* **Q6** A default the step's profile cannot run: skip or refuse.
* **Q7** Who fetches and unpacks.
* **Q8** The size and file caps.
* **Q9** Plugins whose dependencies must be installed.
* **Q10** Marketplace entries whose source is a package registry or a bare git URL.
* **Q11** How a platform switch change is confirmed.
* **Q12** Accepting contract change requests P1-P5.
* **Q13** Whether gstack and security-audit stay image-baked built-ins. **Decided by the owner, 6 November 2026:**
  they migrate to the managed path; "it has to gain a build step that publishes a new image version in which it ships
  bundled in". The recommendation (stay code entries) was not taken; §1.4 is the design.
* **Q14** The baked Jev quality gates and the managed jev-belay and jev-enforce in one step.
* **Q15** Whether Admin › Setup offers an optional plugin step. **Decided by the owner, 6 November 2026:** yes, an
  optional "add a plugin" step. The recommendation (no step) was not taken; §15.1 is the design.

New with this revision (9 November 2026), each in the lane's `questions.json` with options and a recommendation,
none decided here:

* **Q16** Where a bundled install's build step runs: in the deployment's own project, so an admin may update to any
  pin (built, §1.4.2), or only in the release home, so an admin picks among the pins a release ships. **Answered in
  part by the owner, 11 November 2026:** still the deployment's own project, now as one Cloud Run job execution under
  the builder account (`swarm-plugin-bundle`, §1.4.2), never a Cloud Build build.
* **Q17** Whether a platform admin may remove a bundled install (built: no; a workspace disallows it instead).
* **Q18** Whether a workspace may install its own bundled plugin (built: platform scope only).
* **Q19** How a deployment's bundle build reaches its dependencies (built: the deployment's own mirror
  repositories, checked against the pinned lockfile).
* **Q20** Accepting contract change request P6 (the `bundle` plugin record kind, §14).
