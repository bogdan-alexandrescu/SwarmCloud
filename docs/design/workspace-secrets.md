# Workspace secrets: one store, granted per consumer by checkbox

**Status: designed 2026-10-15 (lane SECRETS-DESIGN, part of #1347).** Built so
far: §3.3's IAM (lane SEC-IAM, `terraform/bootstrap/workspace_secrets.tf`, not
yet applied: `enable_workspace_secrets` turns on when SEC-RELEASE merges) and
phase 1's management API (lane SEC-STORE-API, §5 "What SEC-STORE-API built").
The release (SEC-RELEASE), the console (SEC-UI) and the migrations are not
built. The owner's answers are in §10; where they differ from §9's
recommendation (Q5, Q8), the answer wins.

Owner request, 2026-10-14 ~21:50 PDT, verbatim:

> "we need to be able to set and manage typesafe and other apis from api
> providers we use, I think we need a secrets store management solution that
> will store any credential and based on a set of check boxes, we can enable
> every secret, one at a time to be used in different scenarios with different
> components. For example we can store an api key and only allow access to it
> for a specific runtime job type and image like jev-browser or
> claude-code-browser. All these secrets will be in the workspace of the user
> and it will provide a way to grant access to specific secrets in different
> components of the swarmcloud and runner profiles"

Every file, symbol and line cited below was read on 2026-10-15 at `b38df0b`.
Lines move, so search for the symbol named beside each one.

What the design settles, in one line each:

* **A secret belongs to one workspace** (a tenant: a personal `u-<user>` or a
  group). It is one Secret Manager secret named through the frozen
  `Tenant.secret_name`, plus one Firestore record that never holds the value
  (§2.1).
* **A grant is (secret × consumer), with an enabled flag, who and when.**
  A consumer is a NAME from a platform catalogue: a runner profile, a runtime
  tool, or a platform component. Never an image, a command or a path
  (§2.2, §2.3).
* **Enforcement, recommended: swarm-api releases a task's granted secrets to
  its worker once per attempt**, on a request signed with the attempt key and
  fenced on the generation. The tenant's worker account holds no read grant on
  a workspace secret, so an agent that mints that account's token reads
  nothing (§3).
* **Delivery is one 0600 file per secret in `private/`**, never the
  environment, `work/` or a log. A revoked grant takes effect at the next
  attempt (§4).
* **The boundary is the attempt, not the process.** Whatever an attempt is
  given, its agent can read, because the agent runs as the same uid. This
  document says so wherever it matters rather than implying otherwise (§3.5).

---

## 1. Inventory: every credential the platform stores today

"Read by" names the identity Google checks; "how" names the code. A row marked
**agent-readable** is one whose value the agent of an attempt can read today,
whether or not it is meant to.

### 1.1 Per-tenant secrets (`swarm-tenant-<tenant>-*`)

| secret | what | written by | read by, and how | agent-readable today |
|---|---|---|---|---|
| `swarm-tenant-<t>-anthropic`, `-openai` (any `known_providers()` name) | the runner's provider key (metered API key or Claude subscription token) | any tenant member, `POST /v1/tenants/me/credentials` → `apps/swarm-api/swarm_api/credentials.py::SecretManagerCredentials.put_credential`, which grants the tenant's worker account `secretAccessor` **on that secret** (`_grant_accessor`) | the worker, as the tenant's GSA, `apps/agent-worker/agent_worker/secrets.py::resolve_credentials`; only the variable names the frozen `RunnerProfile.secrets` lists are exported, into **the runner child's environment only** (`kubernetes/README.md`, "No provider key in a Job's environment") | **yes**, by design: the CLI reads it from its environment, and its Bash tool inherits that environment |
| `swarm-tenant-<t>-typesafe` | the tenant's TypeSafe key, for jev-browser | an admin, `PUT /v1/provider-keys/typesafe/tenant` (`apps/swarm-api/swarm_api/routes/provider_keys.py`), through the same `put_credential` | the worker, as the tenant's GSA, `apps/agent-worker/agent_worker/jevkey.py::resolve`, only on a live-browser attempt whose workspace judges with TypeSafe; written to `private/jev-typesafe.key` (0600, `O_EXCL`, `O_NOFOLLOW`); `runners/jev_launch.py` puts it in the jev MCP server's environment only. Also the worker's Jev code-judgment client (`agent_worker/judge/register.py`), when the workspace opted in to Jev for code with the TypeSafe backend: `private/jev-key` | **yes**: the agent can read the MCP config and both files (same uid), as `jevkey.py` and `judge/register.py` record |
| `swarm-tenant-u-<user>-typesafe` | a person's own TypeSafe key | that person, `PUT /v1/provider-keys/typesafe/user` | as above, when the submitter's **confirmed** personal tenant holds one; it wins over the tenant key (`provider_keys.resolve_provider_key`, restated by `jevkey.candidates`). On a group-tenant task it is readable only where `terraform/bootstrap/typesafe_user_keys.tf` grants that group's worker account on that one secret (an owner apply per row); otherwise `jev_key_unreadable` and the tenant key is tried | as above |
| `swarm-tenant-<t>-git` | the tenant forge token | the operator only, `scripts/create-secrets.sh --stdin` | the worker, `secrets.py::resolve_git_token`, written to a git credentials file the agent is never told about (`gitops.py`); swarm-api, `forge.py::SecretManagerForgeTokens`, for issue preview and open work (`var.forge_readers`, `terraform/modules/secret_manager/main.tf` accessor binding) | not through the git tools: since runner-git-tools ([runner-git-tools.md](../runner-git-tools.md)) the agent's container holds no forge token for them |
| `swarm-tenant-<t>-git-u-<16 hex>` | a person's GitHub App user token (kind `app_user`) and, one per owner, a fallback personal token | swarm-api at onboarding (`gittokens.py::provider_suffix`, `owner_suffix`; `forgeapp.py::ForgeApp.store_owner_token`); refreshed by the 15-minute sweep | the worker, the slot named by the signed `Task.forge_credential`; swarm-api; under one prefix-conditioned grant per tenant, `terraform/bootstrap/forge_user_slots.tf` | as the tenant forge token |
| `swarm-tenant-<t>-git-merge`, `-git-review` | the retired #295 App keys | none now | `credentials.refuse_unbindable_provider` refuses them before any write, so a worker account is never bound to them | no |
| `swarm-tenant-u-<user>-browser-login-<name>`, `-browser-wf-<workflow>` | a signed-in browser session (cookie jar), named login ≤ 30 days, workflow carry 24 h | the worker, into a slot swarm-api provisions per secret (`POST /v1/tasks/<task>/browser-session/slot`, attempt-key signed, generation-fenced; `terraform/bootstrap/browser_logins.tf`) | the worker of that person's own tenant (accessor and versionAdder on that one secret) | the session is loaded into the browser the agent drives |

### 1.2 Platform secrets (not a tenant's)

| secret | what | read by |
|---|---|---|
| `swarm-account-<t>--<label>[-refresh]` | a pool account's subscription token, registered with the quota broker | the broker writes (`terraform/modules/iam/custom_roles.tf`, the `swarm-account-` prefix arm); the worker of an attempt that holds that account's lease, into the runner's environment (`runners/claude_code.py` docstring) |
| `swarm-github-app-private-key`, `swarm-github-app-client-secret` | the platform GitHub App | swarm-api (`terraform/infra/github_app.tf`) |
| `swarm-search-api-key` | the search provider's key | swarm-api only, on every call; never in a pod ([search-tool.md](../search-tool.md)) |
| `swarm-egress-credential` | the residential-proxy vendor credential | the `swarm-egress` forwarder's KSA alone; **not** the tenant GSA, because "the tenant GSA is the agent's identity (every process in the pod can mint its token)" (`terraform/infra/egress.tf`) |
| `swarm-live-key`, `swarm-child-key`, `swarm-egress-key` | platform HMAC keys that mint tickets | swarm-api and the verifier named in each `.tf` (`live.tf`, `child_tasks.tf`, `egress.tf`) |
| `swarm-spec-verify-keys` | a ConfigMap of **public** keys, not a secret | each tenant namespace (`terraform/infra/spec_signing.tf`) |

### 1.3 What the inventory says about the request

1. **There is already one boundary and it is the tenant.** Every row in §1.1
   is readable by the tenant's worker account, and every attempt of that tenant
   runs as that account. The owner's "only allow access to it for a specific
   runtime job type" is a boundary *inside* a tenant, which nothing today draws.
2. **Two patterns already keep a credential from the tenant GSA**, and both
   are the model for §3: `swarm-search-api-key` (swarm-api acts, the pod holds
   nothing) and `swarm-egress-credential` (a platform forwarder holds it).
   The browser-session slot is the model for an attempt-key-signed,
   generation-fenced worker request to swarm-api.
3. **The TypeSafe key is the first secret with a consumer narrower than the
   tenant** (`consumed_by: ["jev"]`), but the narrowing is in the worker's code
   path, not in IAM. Any attempt of the tenant can read it with the GSA's token.

---

## 2. Model

### 2.1 The secret record

One Firestore document per secret, `workspace_secrets/{secret_id}`, where
`secret_id` is the Secret Manager secret id. It never holds the value, a
length, a prefix or a suffix (the rule `provider_keys/` already follows).

| field | meaning |
|---|---|
| `secret_id` | `Tenant.secret_name("wsec-<16 hex>")`, i.e. `swarm-tenant-<tenant>-wsec-<16 hex>`. The hex is random, minted by swarm-api. **The display name is not in the resource name**: a person's label can carry anything, and a name would leak into IAM policies, audit logs and the 255-character limit. `wsec-` is a marker no existing suffix uses, so an IAM condition can find it (§3.3). |
| `tenant_id` | the owner workspace. Never changes. |
| `name` | the display name, unique within the workspace, 1-64 characters, `[a-z0-9-]` (it becomes a file name, §4.1) |
| `provider` | from a catalogue (`typesafe`, `openai`, `brave`, `github`, …, and `other`). Drives the console's help text and a shape check, never behaviour |
| `kind` | `api_key` · `bearer_token` · `basic_auth` (a JSON object of exactly `username`, `password`) · `oauth_client` (`client_id`, `client_secret`) · `opaque`. The shape check is the store's own (`credentials.validate_key_material`: 8 bytes to 8 KiB, no NUL), plus the object's keys for the two structured kinds |
| `scope` | `workspace` or, in a personal tenant, `personal` (§9 Q3) |
| `current_version` | the Secret Manager version name the next release uses |
| `versions` | `[{version, added_at, added_by, state}]`, newest first, at most the last 10 |
| `created_at`, `created_by`, `rotated_at`, `rotated_by` | emails from the verified session, never from a body |
| `managed` | `store` (written through the store's routes) or `legacy` (a §6 migrated record whose value is still set by its old path) |

### 2.2 The grant

One document per pair, `workspace_secret_grants/{secret_id}~{consumer}`:

| field | meaning |
|---|---|
| `secret_id`, `tenant_id` | the secret, and its workspace (copied so a query by tenant needs no join) |
| `consumer` | a catalogue name, §2.3, e.g. `tool:jev-browser` |
| `enabled` | the checkbox |
| `slot` | where the consumer takes it (§2.4); defaults to the consumer's only slot when it has one |
| `changed_at`, `changed_by` | the last toggle |

A grant is **never deleted**, only disabled, so its history stays readable in
`admin_audit` beside it. Deleting the secret disables every grant on it in the
same transaction.

### 2.3 Consumers are names from a catalogue

Three kinds, one spelling (`<kind>:<name>`), served by
`GET /v1/workspace/secrets/consumers` so the console renders the matrix from
the catalogue rather than from a list of its own:

| kind | names (2026-10-15) | where the name comes from |
|---|---|---|
| `profile:` | `claude-code`, `codex`, `browser`, `claude-code-review`, `indexer`, `claude-code-browser` | `apps/common/swarm_common/profiles.py`, the catalogue's names. `merge` and `post-verdict` are `worker_action` profiles that "mount no secret" (`RunnerProfile.__post_init__`, contract request 60), so they are offered as components instead |
| `tool:` | `jev-browser`, `dev-browser`, `playwright` | the values of the frozen `browser_tool` input (`profiles.py`, `"browser_tool": RunnerInput(... choices=("auto", "playwright", "dev-browser", "jev"))`); `jev` is spelled `jev-browser` here because that is the program the secret reaches |
| `component:` | `judge`, `merge`, `indexer`, `git` | platform steps. `git` is fixed to forge tokens and is never offered to anything else (§6.2) |

**Which consumers an attempt is.** The worker does not choose. swarm-api
computes it from the **signed** task spec: `profile:<task.runner_profile>`;
`tool:<x>` for each tool the profile's signed `browser_tool` input will launch
(`auto` launches all three); and `component:<c>` for each platform step the
attempt runs. A secret is released to the attempt when an **enabled** grant
names any one of them.

**Why a consumer is a name and never an image or a command (invariant 10).**
"API callers pick a `runner_profile` by name. Never accept images, commands,
resource specs or backend parameters from a caller." A grant is caller input:
a workspace member writes it. If it could say "image
`…/agent-runtime-browser@sha256:…`" or "command `jev-browser-mcp`", then

* the person granting would be stating what runs, which only the platform's
  catalogue may state; an image reference is a digest a release changes, so a
  grant would silently stop matching (or start matching a different build)
  without anyone toggling it;
* nothing could check it honestly. The worker learns what it is from its
  signed spec, so a *name* in that spec can be compared. A command is
  something the agent itself can start inside the pod, so "only `jev-browser`
  may read it" could never be enforced against a process name;
* the owner's own examples are already names: `claude-code-browser` is a
  profile, `jev-browser` is a tool. **An image is not a consumer.** The
  `agent-runtime-browser` image serves `browser` and `claude-code-browser`;
  granting by image would widen a grant to profiles nobody ticked.

### 2.4 Slots: how a consumer takes a secret

A consumer declares the slots it reads, so the console can say "jev-browser
reads a TypeSafe key here" and refuse a `basic_auth` secret in an `api_key`
slot:

| consumer | slot | kind accepted | delivered as |
|---|---|---|---|
| `tool:jev-browser` | `typesafe` | `api_key` | the key file `jev_launch.py` already reads, into that one process's environment |
| `profile:*` (any agent profile) | `files` | any | one file per secret, listed in a manifest of names and paths for the agent (§4.1) |
| `component:judge` | `typesafe` | `api_key` | the file `judge/register.py` already reads (`private/jev-key`), when the workspace judges code with TypeSafe |
| `component:git` | `forge` | forge kinds only | unchanged: the worker's git credentials file (§6.2) |

Slots for profiles and tools belong in the frozen catalogue (§7, request A).

---

## 3. Enforcement: an attempt reads only its granted secrets

### 3.1 The fact every option has to answer

The agent runs as the same uid as the worker, in the same pod, with the same
metadata server. So:

* **The agent can mint the tenant GSA's access token**
  (`docs/design/live-browser.md` §5.4.1: "The agent can mint the tenant's ID
  token and read the container's environment"; `terraform/infra/egress.tf`
  says the same of access). Anything the tenant GSA can read, a compromised
  agent can read, in every attempt of that tenant.
* **The agent can read any file the worker writes for a runner child**, and
  `/proc/<pid>/environ` of any child (`kubernetes/README.md`).
* **The agent cannot sign as the worker.** The attempt key is an Ed25519 key
  the worker generates after `hardening.make_non_dumpable` and registers
  before the agent exists (`docs/design/child-tasks.md` §3.2,
  `live-browser.md` §5.4.1). This is the one thing in the pod the agent
  cannot use, and the git tools, the search tool, the live session and the
  browser-session slot already rest on it.

### 3.2 Option A: per-secret IAM grants on the tenant GSA (or conditions on it)

swarm-api writes `secretAccessor` on each workspace secret to the tenant GSA
when any grant on it is enabled, and removes it when none is. The worker reads
only the granted ones.

* **What a compromised agent reads:** every workspace secret with **any**
  enabled grant, in **every** attempt of the tenant, whatever its profile. A
  key ticked only for `claude-code-browser` is readable from a `codex`
  attempt. The checkbox is advisory.
* **Conditions do not rescue it.** An IAM condition sees the resource (its
  name and type), the time, and a few request attributes; it does not see
  which profile's pod is asking, because every profile's pod presents the same
  GSA. A condition can only narrow *which secrets* the GSA reaches, and the
  matrix is per consumer, so that is the wrong axis.
* **Option A′, one GSA per (tenant, consumer).** This is the only IAM form
  that enforces the matrix: secret X is bound to `swarm-agent-worker-<t>-<p>`
  for each ticked profile. It fails here:
  * a grant to a *tool* cannot be an identity: `jev-browser` runs inside a
    `claude-code-browser` pod as the same uid, so a tool-level checkbox still
    collapses to the profile;
  * every per-tenant grant invariant 9 needs (Firestore, the GCS prefix,
    logging, Workload Identity, the namespace's KSA) multiplies by the
    profile count, for personal tenants the workspace job creates outside
    Terraform (`browser_logins.tf` "WHY NOT TERRAFORM" records why that job
    cannot bind per person);
  * listing many secrets in one condition runs into the documented limit: a
    `hasOnly()` list holds at most 10 items and two `hasOnly()` calls cannot
    be combined (`terraform/bootstrap/deployer_conditions.tf`, #275), and
    `matches()` is refused on GCS and some other services
    (`terraform/modules/iam/bindings.tf`, release run 38114832799). Per-secret
    bindings avoid lists but then each toggle is a `setIamPolicy`, with IAM's
    propagation delay between the checkbox and the effect.
* **Invariant 9:** kept (bindings name only the tenant's own identities).
* **Verdict:** A enforces nothing finer than today. A′ enforces profiles, not
  tools, at a cost in identities this platform has twice decided not to pay.

### 3.3 Option B: swarm-api releases each attempt's granted secrets (recommended)

The tenant GSA holds **no** read grant on any `-wsec-` secret. swarm-api is
the only reader. Once per attempt, before the agent starts, the worker calls

    POST /v1/attempts/{attempt_id}/secrets:release
    body: {generation}            (nothing else; the body forbids every other field)
    signed with the attempt key, domain "swarm-secrets-release/v1"

and swarm-api, in order:

1. verifies the signature against the key registered for this attempt, and
   that the task is STARTING or RUNNING at this lease and **this generation**
   with the lease unreleased (the browser-session slot's fence). A stale
   generation gets 409 and nothing, so a stale worker never holds a secret
   (invariant 5);
2. derives the consumer set from the **signed** task spec (§2.3), never from
   the request;
3. queries `workspace_secret_grants` where `tenant_id == task.tenant_id`,
   `enabled == true`, `consumer in [the set]`. A personal secret on a group
   task is included only under §9 Q3's rule;
4. reads each secret's `current_version` and returns
   `[{name, slot, kind, version, value}]` over TLS;
5. writes one release row per secret (§4.4) in the same request.

The worker writes the files (§4.1) and registers every value for redaction
before the agent is spawned.

* **What a compromised agent reads:** exactly the secrets released to **its
  own attempt**, which are the ones granted to its profile, its tools and its
  components, because they are files in its pod. It cannot ask for more: the
  release takes no names, and it cannot sign a second request. Minting the GSA
  token gets it nothing. It cannot reach another attempt's release, another
  consumer's secrets, or another workspace's.
* **What a compromised swarm-api reads:** every workspace secret. That is not
  new reach: swarm-api already holds `secrets.setIamPolicy` over every
  `swarm-tenant-*` secret, which "amounts to `versions.access`"
  ([provider-keys.md §6](../provider-keys.md#6-deployment-swarm-apis-secret-manager-grant-keys-iam)).
  B makes that reach an explicit, audited, narrow path instead of a latent one.
* **The IAM it needs** is one conditioned grant to swarm-api of
  `secretmanager.versions.access` (a custom role holding that one permission),
  in `terraform/bootstrap`, under the provider-key condition's shape and no
  list:

      resource.name.startsWith("projects/<NUMBER>/secrets/swarm-tenant-")
        && resource.name.extract("/secrets/swarm-tenant-{t}-wsec-") != ""

  using the project **number** (IAM evaluates Secret Manager names with it)
  and `extract()` rather than `matches()`. One condition covers every
  workspace, personal tenants created at runtime included, because the tenant
  is found in the name, not listed. **Not verified live:** it must be linted
  with `iamPolicies:lintPolicy` and shown to refuse a broken expression first,
  as provider-keys.md §6 did.
* **Built (lane SEC-IAM, 2026-10-15, part of #1347):** this grant exists as
  `terraform/bootstrap/workspace_secrets.tf`, off until the owner sets
  `enable_workspace_secrets = true` and applies bootstrap. It is the custom
  role `swarmWorkspaceSecretAccessor` (`secretmanager.versions.access` and
  nothing else) and one `google_project_iam_member` to swarm-api carrying the
  condition above byte for byte; the output `workspace_secret_grants.api_read`
  prints it for the lint. Nothing in it names a tenant worker.
  `tests/terraform/workspace_secrets.tftest.hcl` evaluates the condition
  (admits `swarm-tenant-<t>-wsec-<hex>` and its versions; refuses `-git`,
  `-typesafe`, the forge slots, `swarm-github-app-*`, `agents-*`, `promptlab-*`,
  another project and the project ID), fails if it is dropped or widened, and
  checks that no bootstrap grant to a tenant worker admits a `-wsec-` name.
  **What the condition still reaches:** `extract()` finds the *first* `-wsec-`,
  so it also admits a non-workspace secret that carries `-wsec-` after a
  non-empty tenant: a saved browser login named like `x-wsec-y` (the login
  name rule accepts it today), or any secret of a tenant whose id contains
  `-wsec-`. swarm-api already reaches those names through
  `secrets.setIamPolicy`, so this is not new reach. The fix belongs where the
  names are made: refuse `wsec` as a hyphen-separated token in a login name
  and a tenant id. That is in apps/ and the frozen contract, and the owner
  has it as a question. Linting and IAM's live answer are still the owner's
  to check before apply.
  **Residual accepted (owner, 2026-10-15: "Accept the residual").** The
  owner accepted that `extract()` matches the first `-wsec-`, so a login name
  or a tenant id containing `-wsec-` stays admitted by the condition. No code
  change is made for it, here or where the names are made.
* **Invariant 9:** the route answers only for the tenant of the attempt whose
  key signed it, and the query is bounded by `tenant_id == task.tenant_id`.
  An attempt key of tenant X cannot name tenant Y: the request carries no
  tenant.
* **What it costs:**
  * it is a route that **serves credential material**, which
    `credentials.py` ("There is no read path") and
    `test_no_route_serves_credentials.py` were written to forbid. That test
    sweeps GET routes for sentinels, so a signed POST is outside its sweep; that
    is a reason to amend it explicitly, not to rely on it. It is the owner's
    call (§9 Q2). The narrowing is that the only caller who can present a
    signature is a worker, for its own attempt, once;
  * the attempt key must be registered for every attempt that has any grant.
    Today the scheduler hands the worker a registration nonce only when the
    deployment has the child key and swarm-api's address, and then for every
    top-level task and for a child whose profile offers git tools
    (`scheduler/dispatch.py::worker_env`, "THE ATTEMPT KEY"). A deployment
    with the child path off registers no key, so B needs it on wherever
    secrets are used, and a child without git tools needs one when it has a
    grant. A worker that cannot protect its heap gets no release, the live
    path's rule;
  * swarm-api is on the start path. If it is down, an attempt with a
    `required` slot parks (§4.2) at no cost (invariant 1); one with only
    optional grants starts without them.

### 3.4 Option C: worker-side filtering

The tenant GSA reads every workspace secret; the worker reads the grants and
fetches only the granted ones.

* **What a compromised agent reads:** every secret in the workspace, by
  minting the GSA token. C protects against a worker bug, not against the
  agent. It is what `secrets.py` and `jevkey.py` do today.
* **Kept as defence in depth under B:** the worker refuses any released item
  whose slot its own catalogue does not have, and never writes one twice.

### 3.5 Option D, for later: the secret never enters the pod

The egress forwarder's shape: a platform proxy holds the secret and adds the
header to requests the agent makes to one host. The agent can use the API but
cannot read the key. It needs a per-provider adapter (which host, which
header), a forwarder in the request path, and agreement that the provider sees
the platform's proxy. It is worth it for a key whose leak is expensive; it is
not the base mechanism. Recorded as a later delivery mode, `inject`, beside
`file` (§9 Q8).

### 3.6 Recommendation

**B, with C as defence in depth, and D as a per-provider upgrade later.** It is
the only option where minting the GSA token gets the agent nothing, where a
tool-level checkbox means something (a `claude-code-browser` attempt with
`browser_tool: playwright` is not released the jev-browser key), where a toggle
takes effect at the next attempt without an IAM round-trip, and where personal
secrets on a group task no longer need an owner-applied per-person IAM row
(the open item in provider-keys.md §2). Its limit, stated once more: the
boundary is the attempt. Two consumers in one attempt (the profile and its
tool) share what was released to either.

---

## 4. Delivery, rotation, revocation, audit, redaction

### 4.1 Into the runner: a file, never the environment or a log

* The worker writes each released secret to
  `private/secrets/<slot>/<name>`, mode 0600, created with
  `O_EXCL | O_NOFOLLOW` after unlinking anything there, as
  `jevkey._write_private` does. `private/` is "the WORKER's own scratch"
  (`workspace.py`): never checkpointed, never uploaded. Nothing goes in
  `work/`, which is checkpointed.
* `private/secrets/manifest.json` lists `{name, slot, kind, path}` and **no
  value**. An agent profile's runner gets one variable, the manifest's path
  (`SWARM_SECRETS_MANIFEST`), and its prompt preamble names the available
  secrets by name. A tool's launcher (as `jev_launch.py` does) reads its one
  file and puts it in **that process's** environment only, which is the one
  place a third-party binary that reads only its environment can get it.
* Never: the runner's environment, argv, the MCP config file, a checkpoint,
  an artifact, a log line, Firestore, a response other than the release.
* At attempt end the worker removes `private/secrets/`
  (`_remove_jev_key_file`'s pattern), on every exit path the lifecycle has.

### 4.2 Required and optional

A grant to a profile slot is optional unless the profile declares the slot
`required` (§7 request A). A required slot with no enabled grant parks the task
`CREDENTIAL_MISSING` **before** it leases, at no cost, and it starts by itself
once the grant is ticked, which is today's rule for a missing provider key.

### 4.3 Rotation and revocation

* **Rotate** adds a Secret Manager version and moves `current_version` in one
  transaction with its audit row. Attempts already running keep the value they
  were given; the next attempt gets the new one. The previous version is
  disabled at once by default (§9 Q5), which `versions enable` can undo.

  **As decided and built (owner decision Q5, #1347).** The previous version
  stays **enabled for 24 hours**, then is **disabled, never destroyed**. Two
  pieces make that happen with nobody watching. Each is held by a test:
  * `POST /v1/admin/workspace-secrets/rotation-sweep` disables every version
    past its `disable_after`, with one audit row each (SEC-STORE-API).
  * `swarm-workspace-secrets-rotation-sweep`, a Cloud Scheduler job
    (`terraform/modules/scheduler/jobs.tf`, `secrets_rotation_sweep`), calls
    it **hourly at :41** as the rollup-sweeper account. Hourly is enough: a
    version outlives its grace by at most an hour. That account may call this
    one route and its other sweeps, nothing else
    (`auth.ROLLUP_SWEEPER_ROUTES`,
    `tests/unit/control_plane/test_rollup_sweeper_is_narrow.py`). It is not an
    admin, so a leaked token can disable only a version whose grace has passed.
    It can read, add, grant or delete nothing.

  The disable runs as swarm-api, under **`secretmanager.versions.disable` on
  `swarmProviderKeyWriter`** (`terraform/bootstrap/provider_keys.tf`, held by
  `tests/terraform/provider_keys.tftest.hcl`). It is the role's only
  addition, under its existing `swarm-tenant-*` condition, and the owner
  applies it. Until that apply, each due disable answers 403 and is counted in
  the response's `failed`. The next hour retries it. The version stays enabled
  past its grace, and is never destroyed.
* **Revoke a grant** (untick) takes effect at the **next attempt**: the
  release is computed per attempt, and a parked task that resumes is a new
  attempt. A running attempt keeps what it holds, because a file cannot be
  unread. The console says so beside the checkbox, and offers "untick and
  rotate" for the case where a running attempt must stop holding a working
  value; only rotating at the provider makes the copy worthless.
* **Delete** destroys every version not already destroyed and disables every
  grant (§5).

### 4.4 Audit

Management rows go to `admin_audit`, in the same transaction as the record
they describe, as `provider_key_set` does:

| action | detail |
|---|---|
| `workspace_secret_created`, `_rotated`, `_deleted` | `{tenant_id, secret_id, name, provider, kind, version}` |
| `workspace_secret_grant_enabled`, `_disabled` | `{tenant_id, secret_id, consumer, slot}` |

Release rows go to their own collection, `workspace_secret_releases`, because
there is one row for each secret in each attempt. A row's fields are `at`,
`task_id`, `attempt_id`, `generation`, `tenant_id`, `secret_id`, `version`,
`consumers` and `submitted_by`. The console's
secret page shows "last used" from it. Retention is §9 Q6. **No row, in either
collection, holds a value.**

### 4.5 Redaction

* The worker registers each released value with its logger
  (`logger.register_secret`) the moment it parses the release, before the
  agent exists, as `jevkey.resolve` does.
* The worker's publish-time credential scan adds the released values as
  literal patterns, so a diff that echoes one is refused by value, not only by
  shape.
* swarm-api never logs the release body; its handler builds the response from
  locals and returns it. The release route is excluded from the request
  logger's body capture by construction, and a unit test (§8, SEC-RELEASE)
  asserts that a runtime-built fake value is absent from every captured log
  line, as `test_provider_keys.py` does for the write.

### What SEC-RELEASE built (2026-10-15, part of #1347)

`apps/swarm-api/swarm_api/routes/attempts_secrets.py` (the route, its
fence, the one payload read and the rows), the release queries at the end of
`swarm_api/workspace_secrets.py` (`attempt_consumers`, `release_plan`,
`owned_by`), `apps/agent-worker/agent_worker/secretrelease.py` with its
`lifecycle.py` wiring, and `scheduler/dispatch.py::consumes_workspace_secrets`
in `worker_env`. Held by `tests/unit/control_plane/test_secret_release.py`,
`tests/unit/worker/test_secretrelease.py`,
`tests/unit/control_plane/test_attempt_key_every_attempt.py` and the named
carve-out in `test_no_route_serves_credentials.py`. Where the build differs
from §3.3, and why:

* **The attempt is found from its id.** The body is `{generation}` and
  nothing else (an extra field is 422), so swarm-api finds the one lease
  naming the path's attempt id, takes the tenant and the task from it, and
  verifies the worker identity and the attempt-key signature under
  `swarm-secrets-release/v1` against the registration for that whole tuple. A
  wrong generation, a wrong key, another route's purpose, another tenant's
  identity or an unknown attempt is the same 403.
* **Fenced twice.** The task must be STARTING or RUNNING at this lease and
  generation, the lease unreleased, before anything is read, and again in the
  transaction that writes the rows. A worker superseded while Secret Manager
  answered gets 409 and nothing.
* **Once per attempt.** A mark, `workspace_secret_release_marks/{attempt}`,
  is written with the rows; a second call is 410. The next attempt asks again.
* **The signed spec is verified first** (`verdictpublish.spec_refusal`): a
  tenant's agent can rewrite its task document, and the profile, the
  `browser_tool` and the submitter all widen what is released.
* **A record cannot name another workspace's secret.** Grants and records
  are documents a tenant's agent can write (docs/multi-tenancy.md, "The
  Firestore row, in full"), so a record is released only when its secret id
  is `swarm-tenant-<the signed tenant>-wsec-<16 hex>` and its version is a
  version of that secret. The read is made under swarm-api's own project.
  **What this does not stop:** an agent that learns one of its own
  workspace's secret ids (the name index's id is `<tenant>~<name>`) can write
  an enabled grant for it. That widens the matrix inside its own workspace,
  never across one. Closing it needs grants that the agent cannot forge,
  for example an attestation like the attempt key's. That is a
  `workspace_secrets.py` write-path change, outside this lane.
* **Q3, as decided.** A personal secret reaches a group task only when its
  opt-in list names the group, its record's creator and the personal tenant's
  principal are both the signed submitter, and the submitter is a member of
  the group NOW. That is asked of the directory for the group in
  `ApiSettings.tenant_groups`, never the tenant document's principal, and
  only when there is something to release. A lapsed or unresolved membership
  withholds them, logged by name.
* **Q8, as decided: the `typesafe` slot is matched and withheld**
  (`INJECT_ONLY_SLOTS`, reason `inject_only`). The TypeSafe key reaches jev
  through the forwarder (SEC-INJECT-TYPESAFE), never as a file in the pod.
  Every other slot is delivered as §4.1 says.
* **The components.** `component:merge` and `component:indexer` are derived
  for the merge action and the indexer profile, and declare no slot yet.
  `component:judge`'s key is the forwarder's (Q8), and `component:git` takes
  no workspace secret, so neither is derived.
* **The rows** carry §4.4's fields plus `secret_tenant_id`, `name` and
  `slot`, and an `expire_at` 90 days out. The marks have one too. **The
  Firestore TTL policy on `expire_at` is not built:** it is a Terraform field
  on each collection, outside this lane.
* **Before SEC-IAM is applied** every read is PERMISSION_DENIED. The route
  answers 503 `secrets_release_unavailable` with that reason, and no name
  and no value. The worker treats it as unreachable.
* **The worker** asks only when the profile, or a tool its `browser_tool`
  launches, declares a slot, and only with a registered attempt key. It
  registers every value, and every field of a structured one, before writing
  any file. That registered set is also the publish scan's literal list
  (`Worker._leaks_in_added_text`). It refuses an item whose slot, kind or
  name its own catalogue does not allow, or that arrives twice. The runner
  gets `SWARM_SECRETS_MANIFEST` only, and `private/secrets/` is removed first
  in cleanup on every exit. A required slot with nothing in it parks
  CREDENTIAL_MISSING before the runner starts. Optional-only starts without.
  A 409 stands the worker down without touching the lease.
* **The scheduler** hands the registration nonce to a child whose profile or
  launched tools declare a slot, git tools or not
  (`consumes_workspace_secrets`).
* **Not built here: the scheduler's pre-lease park.** No catalogue slot is
  `required` today (request 80), so a required slot with no enabled grant
  never reaches admission. The worker's park above is the backstop. Admission
  reading the grants is `scheduler/loop.py` and `credentials.py`, outside
  this lane's territory, and is the owner's question in this lane's PR.

---

## 5. Console: Settings › Workspace › Secrets

Settings is reached from the panel's foot (`App.tsx`, `SETTINGS`), beside the
existing `settings/keys` (API keys) and `settings/workspace` (Workspace)
pages. The new page is `settings/workspace/secrets`, a pane of the Workspace
page, for the selected workspace.

* **List.** Name, provider, kind, set by, set at, current version, last used
  (from releases), and the consumers it is granted to. Never a value, a
  length, or the last characters.
* **Add.** A form: name, provider, kind, the value in a password field (or
  the structured kind's two fields). **The value is pasted into the console
  form only.** No MCP tool, `/sc` command, chat, or agent prompt accepts it.
  `/sc` may mint a one-time link to this form, the provider-keys §3 flow
  (32 random bytes, `sha256` as the document id, ten minutes, one use, minted
  by and only for this caller, and granting nothing). The form drops the token
  from the address bar on load.
* **The grant matrix.** Rows are the workspace's secrets; columns are the
  catalogue's consumers, grouped Profiles · Tools · Components. Each cell is a
  checkbox. A cell whose slot cannot take the secret's kind is disabled with
  the reason ("jev-browser reads an API key"); `component:git` cells exist
  only on forge rows and are locked (§6.2). Ticking a box writes one grant and
  one audit row; the page says "takes effect at the next attempt".
* **Rotate.** Paste the new value; the old version is disabled (or kept, per
  §9 Q5) and the row shows the new version.
* **Delete.** A typed confirmation: the person types the secret's **name**.
  The dialog lists every enabled grant it will disable and says that running
  attempts keep their copy.
* **Who may do what** is §9 Q4. Whoever may not manage sees the list and the
  matrix read-only.

The API under it (sketch; swarm-api, every route scoped by `tenant_scope`,
bodies forbidding unnamed fields):

| route | what |
|---|---|
| `GET /v1/workspace/secrets` | records and grants, no values |
| `POST /v1/workspace/secrets` | create (value in the body, once) |
| `PUT /v1/workspace/secrets/{name}/value` | rotate |
| `DELETE /v1/workspace/secrets/{name}` | destroy versions, disable grants; the body repeats the name |
| `PUT /v1/workspace/secrets/{name}/grants/{consumer}` | `{enabled}` |
| `GET /v1/workspace/secrets/consumers` | the catalogue, with slots and kinds |
| `POST /v1/workspace/secrets/links` | a one-time link to the add or rotate form |
| `POST /v1/attempts/{attempt_id}/secrets:release` | the worker only (§3.3) |

No route takes a tenant, a person, an image or a command.

### What SEC-STORE-API built (2026-10-15, part of #1347)

`apps/swarm-api/swarm_api/workspace_secrets.py` (records, grants, the
catalogue, the Secret Manager writer) and `routes/workspace_secrets.py`,
held by `tests/unit/control_plane/test_workspace_secrets.py`. Every route in
the table above is built **except the release**, which is SEC-RELEASE's.
Where the build differs from the sketch, and why:

* **Owner decisions applied.** Q3: a personal secret carries
  `use_in_workspaces`, set by its person on `PUT .../{name}/use-in`, naming
  only group workspaces they are a confirmed member of (`tenant_choices`);
  SEC-RELEASE reads it. Q4: a group workspace's writes are a full platform
  admin's (`require_admin` with no route); members read names and grants;
  a personal workspace is its person's, by the tenant document's principal.
  **Q5: the previous version stays ENABLED for 24 hours** (`ROTATION_GRACE`),
  then `POST /v1/admin/workspace-secrets/rotation-sweep` disables it and
  never destroys it; only a delete destroys. Q7: `GET` lists the tenant's
  runner provider keys and its pool accounts read-only, from an allow-list of
  fields, never a value.
* **The writer binds no one.** `SecretManagerWorkspaceSecrets` creates the
  secret and adds, disables and destroys versions; it has no read path and no
  IAM call, so the tenant's worker account never holds a read on a `-wsec-`
  secret (§3.3). The `secretmanager.versions.disable` it needs, and the
  sweep's hourly Cloud Scheduler job, came with SEC-ROTATE-SWEEP (§4.3).
* **No `github` provider.** A forge token is never readable by an agent
  (owner rule 2026-09-25), and this store exists to hand values to agents'
  profiles and tools; forge rows arrive read-only with SEC-MIGRATE-FORGE.
* **Slots.** `tool:dev-browser`, `tool:playwright`, `component:merge` and
  `component:indexer` declare no slot in §2.4, so they are listed and refuse
  a grant (422) until SEC-CONTRACT gives them one; `component:git` is locked.
* **A name that is part of its own value is refused**: it is a value pasted
  into the name field, and a name is served, logged and audited.
* **Delete** keeps the record as `state: deleted` (its audit trail names it),
  disables every grant and frees the name. A refused form link is a 403
  `SECRET_LINK_INVALID` outright: these routes are new, so no switch is
  needed to keep an older behaviour.

---

## 6. Migration, without breaking today's readers

### 6.1 The TypeSafe provider key

1. **Index, do not move.** A one-off, idempotent job writes a
   `workspace_secrets` record (`managed: legacy`) for each set
   `provider_keys/{secret_id}`, pointing at the **same** Secret Manager secret
   (`swarm-tenant-<t>-typesafe`, `swarm-tenant-u-<user>-typesafe`). No rename,
   no copy. It writes the grants today's behaviour implies:
   `tool:jev-browser` and `component:judge`, enabled. Nothing reads them yet.
2. **Both paths write both records.** `PUT /v1/provider-keys/typesafe/*`
   updates the store record in the same transaction as `provider_keys/`, so
   the API keys page and the Secrets page agree.
3. **The worker prefers the release, behind a switch.** With
   `SWARM_SECRETS_RELEASE=on`, `jevkey.prepare` takes the `typesafe` slot from
   the release; off, or when the release has no item for that slot, it reads
   as today. The personal-wins order (`resolve_provider_key`) becomes the
   release's rule for that slot: the submitter's confirmed personal key first.
   `test_jev_runner.py`'s equality between the two orders stays until step 5.
4. **Name rule.** The legacy secrets keep their `-typesafe` names; the IAM
   condition in §3.3 does not cover them. swarm-api gets `versions.access` on
   the `-typesafe` suffix too, in the same bootstrap file, as a second arm.
5. **Retire the GSA read.** After a release-only period with no
   `jev_key_unreadable` and no fallback reads logged, the tenant GSA's
   accessor binding on `-typesafe` secrets is removed (credentials.py stops
   adding it for `typesafe`; the owner removes the `typesafe_user_keys.tf`
   rows), and the fallback code goes. That is the step that makes the
   checkbox real for this key. It is the owner's apply.

### 6.2 Forge tokens

A forge token is never readable by the agent (owner rule 2026-09-25,
CLAUDE.md "Writing Terraform"), so it is **never grantable** to a profile or
tool.

* The store **lists** `-git` and `-git-u-<hex>` as records of kind
  `forge_token`, `managed: legacy`, with their status (set, set at, kind
  `app_user` / owner fallback) read from the records `gittokens.py` already
  keeps. Their only consumer is `component:git`, shown ticked and locked.
* **Their writers do not change**: `scripts/create-secrets.sh --stdin` for the
  tenant token, onboarding and the 15-minute sweep for user slots. The
  console's Secrets page links to Access and Git tokens for them, and does not
  offer Add, Rotate or Delete on a forge row.
* **Their readers do not change**: the worker's `resolve_git_token` and
  swarm-api's `SecretManagerForgeTokens` keep their IAM. Moving the worker's
  forge read onto the release is possible later but buys nothing the agent
  could notice, since the token is already outside its reach.

### 6.3 Runner provider keys and pool accounts

`swarm-tenant-<t>-anthropic` and its siblings, and `swarm-account-*`, are
listed read-only in the store (`managed: legacy`, consumer the profiles whose
`provider` they serve) so the inventory is in one place. Moving them onto the
release changes nothing a compromised agent can read, because the CLI needs
the key in its own environment. It is §9 Q7.

---

## 7. Contract change requests (requests, not changes)

`apps/common/swarm_common/` is frozen. These go to
[contract-change-requests.md](../contract-change-requests.md) as numbered
requests once the owner approves the design; nothing here edits the contract.

**Filed 2026-10-15 by lane SEC-CONTRACT** as requests
[80](../contract-change-requests.md#80-profilespy-a-runner-profile-declares-the-secret-slots-it-may-consume-and-the-vocabulary-of-secret-kinds-sec-contract-workspace-secrets-request-a) (A),
[81](../contract-change-requests.md#81-profilespy-runtime_tools-the-catalogue-of-runtime-tools-their-secret-slots-and-the-browser_tool-values-that-launch-them-sec-contract-workspace-secrets-request-b) (B),
[82](../contract-change-requests.md#82-modelspy-the-secret-consumers-spelling-consumer_name-and-the-platform-component-list-sec-contract-workspace-secrets-request-c) (C) and
[83](../contract-change-requests.md#83-statespy-no-new-park-reason-for-a-missing-workspace-secret-a-required-slot-with-no-grant-parks-credential_missing-sec-contract-workspace-secrets-request-d) (D).
Request 80 corrects one point below: `browser` declares `files` only.
It has no `browser_tool` input and launches none of the tools, so only
`claude-code-browser` reaches the `typesafe` slot.

**Request A. `profiles.py`: a runner profile declares the secret slots it may
consume.** Add to `RunnerProfile`

    secret_slots: Mapping[str, SecretSlot] = field(default_factory=dict, hash=False)

with `SecretSlot(kinds: tuple[str, ...], required: bool = False, means: str)`.
Every agent profile declares `files`; `claude-code-browser` and `browser`
declare `typesafe` through their tools (request B). `__post_init__` refuses a
`worker_action` profile with any slot, as it refuses `secrets` today. Why in
the contract: the console, swarm-api's release and the worker's filter must
read one declaration, as `inputs` is one declaration for swarm-api and the
plugin (request 25).

**Request B. `profiles.py`: a tool catalogue, with its slots.** A frozen
`RUNTIME_TOOLS: Mapping[str, RuntimeTool]` keyed `jev-browser`, `dev-browser`,
`playwright`, each with its `secret_slots` and the `browser_tool` values that
launch it. Why: today the tool names exist only as `browser_tool`'s choices;
a grant to `tool:jev-browser` must resolve to the same spelling in swarm-api
and the worker.

**Request C. `models.py`: the consumer spelling and the component list.**
`SECRET_CONSUMER_KINDS = ("profile", "tool", "component")` and
`PLATFORM_COMPONENTS = ("judge", "merge", "indexer", "git")`, plus
`consumer_name(kind, name) -> str`. Why: one spelling across Firestore, the
API and the worker, the reason `Tenant.secret_name` is frozen.

**Request D. `models.py`: `ParkReason` stays as is.** No new park reason: a
required slot with no grant is `CREDENTIAL_MISSING`. Recorded so a lane does
not add one.

**Not requested:** `Tenant.secret_name` already spells `wsec-<hex>`; no change
to it, to `Task`, or to `RunnerProfile.secrets` (the runner provider key stays
as it is, §6.3).

---

## 8. Phased build plan

Each lane owns the files named; two lanes that touch one file are one lane
(CLAUDE.md, "Issues"). Every lane pushes its tests first to the PR branch and
its fix to `<branch>-fix`, and runs `scripts/changed-guards.sh` before
finishing.

| phase | lane | territory | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **SEC-CONTRACT** (owner applies) | `apps/common/swarm_common/profiles.py`, `models.py`, `docs/contract-change-requests.md` | a `worker_action` profile with a slot raises; every `browser_tool` choice but `auto` maps to a `RUNTIME_TOOLS` entry; `consumer_name` round-trips |
| 1 | **SEC-IAM** | `terraform/bootstrap/workspace_secrets.tf`, `tests/terraform/workspace_secrets.tftest.hcl` | the condition admits `swarm-tenant-a-wsec-<hex>` and its versions, refuses `swarm-tenant-a-git`, `-typesafe` (until §6.1 step 4's arm), `swarm-github-app-*`, `agents-*`, and fails if the condition is dropped or widened |
| 1 | **SEC-STORE-API** | `apps/swarm-api/swarm_api/workspace_secrets.py`, `routes/workspace_secrets.py`, `validation.py`, `tests/unit/control_plane/test_workspace_secrets.py` | a runtime-built fake value is absent from every response, log line and Firestore document; a consumer outside the catalogue is 422; a kind its slot refuses is 422; another tenant's secret is 404; a non-manager is 403; delete without the typed name is 422; every write has its audit row in the same transaction |
| 2 | **SEC-RELEASE** | swarm-api `routes/attempts_secrets.py`, `test_no_route_serves_credentials.py` (the explicit carve-out); worker `agent_worker/secretrelease.py`, `lifecycle.py` wiring; `scheduler/dispatch.py::worker_env` (the attempt key for every attempt with a grant); their unit tests | a stale generation gets nothing; an unsigned or wrongly signed call gets nothing; a secret granted only to `tool:jev-browser` is not released to a `browser_tool: playwright` attempt; an untick is honoured by the next attempt; another tenant's secret is never in a release; every file is 0600 under `private/`, none under `work/`, none in the runner's environment; every value is registered for redaction before the runner starts; the release body is in no log |
| 3 | **SEC-UI** | `apps/swarm-ui/src/WorkspaceSecrets.tsx`, its `App.tsx` route, `paths.ts`, vitest files; the issue forms if the Settings name changes (`test_issue_forms.py`) | no value is rendered after save; the delete button stays disabled until the typed name matches; a disabled cell shows its reason; the link token leaves the address bar |
| 3 | **SEC-SC** | `apps/swarm-mcp`, `plugin/` | no tool's schema accepts a value field; the link tool returns a link and nothing else |
| 4 | **SEC-MIGRATE-TYPESAFE** | `apps/swarm-api/swarm_api/provider_keys.py`, `apps/agent-worker/agent_worker/jevkey.py`, the index job, their tests | the index is idempotent; both writes keep both records equal; with the switch on, jev's key comes from the release and the GSA is never asked |
| 4 | **SEC-MIGRATE-FORGE** | read-only listing in `workspace_secrets.py` (after SEC-STORE-API merges: same file) | forge rows offer no add, rotate, delete or grant; no forge value is ever in a release |
| 5 | **SEC-RETIRE** (owner applies) | `credentials.py` (stop the `typesafe` accessor grant), `terraform/bootstrap/typesafe_user_keys.tf`, `jevkey.py` fallback | the tenant GSA holds no accessor on a `-typesafe` or `-wsec-` secret (a tftest over the plan) |
| each | **SEC-DOCS** | `docs/provider-keys.md`, this file, `docs/security.md` | `test_docs_describe_what_was_built` |

The review CLAUDE.md requires for credentials, isolation, redaction and IAM
applies to SEC-IAM, SEC-STORE-API, SEC-RELEASE and SEC-RETIRE.

---

## 9. Owner questions

Each is also in this lane's `questions.json`.

**Q1. Which enforcement?**
(a) **B, swarm-api releases per attempt** (recommended: the only option where
the GSA token reads nothing and a tool checkbox means something);
(b) A′, a GSA per tenant and profile (tools cannot be enforced; identities
multiply);
(c) C, worker-side filtering only (the checkbox is advisory against a
compromised agent).

**Q2. May swarm-api serve a secret to a worker on an attempt-signed request?**
That reverses `credentials.py`'s "no read path" rule for this one route.
(a) **yes, only `secrets:release`, signed and fenced, with the sweep test
amended to name it** (recommended); (b) no, which leaves A′ or C.

**Q3. May a person's own secret be used in a group workspace's tasks they
submit?** (a) never; (b) **per secret, opt-in by its owner, only on tasks that
person submits** (recommended; it replaces the per-person IAM rows of
`typesafe_user_keys.tf`, and the group's other members' tasks never get it);
(c) always.

**Q4. Who manages a group workspace's secrets?** (a) **platform admins only,
as the tenant TypeSafe key is today** (recommended until a per-tenant admin
role exists); (b) any member adds, admins grant; (c) any member. A personal
workspace is managed by its person in every option.

**Q5. What happens to the previous version on rotate?** (a) **disabled at
once** (recommended; reversible with `versions enable`); (b) kept enabled for
24 hours; (c) destroyed at once.

**Q6. How long are release rows kept?** (a) **90 days, by a Firestore TTL**
(recommended); (b) forever, in `admin_audit`; (c) 30 days.

**Q7. Do runner provider keys and pool accounts move into the store?** (a)
**listed read-only now, not moved** (recommended: the CLI needs the key in its
environment, so moving it narrows nothing); (b) moved in phase 5; (c) left out
of the list.

**Q8. The `inject` delivery mode (§3.5)?** (a) **later, per provider, when a
key's value justifies a forwarder** (recommended); (b) never; (c) in the first
build, for TypeSafe.

**Q9. Default grants for migrated TypeSafe keys?** (a) **`tool:jev-browser`
and `component:judge` enabled, matching today** (recommended); (b) none, so
every workspace ticks them (jev stops working until it does).

## 10. Owner decisions (2026-10-14, ~22:40–23:05 PDT)

The owner answered §9 in this session, and these answers bind the build plan in §8:

| Q | Decision |
|---|---|
| Q1 enforcement | **B**: swarm-api releases each attempt's granted secrets on an attempt-signed, generation-fenced request (§3.3) |
| Q2 release route | **Allowed**, as a consequence of Q1: `POST /v1/attempts/{id}/secrets:release` is the one route that serves credential material, and `test_no_route_serves_credentials` is amended to name it |
| Q3 personal secrets in group workspaces | **Opt-in per secret, only on the owner's own submissions** |
| Q4 who manages a group's secrets | **Platform admins only** |
| Q5 rotation | **The previous version stays enabled for 24 hours**, then is disabled (not destroyed). This differs from the recommendation: tasks already running keep working across a rotation |
| Q6 release audit rows | **90 days, by a Firestore TTL**, for every release row |
| Q7 runner provider keys and pool accounts | **Listed read-only** on the Secrets page, not moved |
| Q8 inject mode | **In the first build, for TypeSafe.** This differs from the recommendation: a platform forwarder adds the TypeSafe key to jev's requests, so the TypeSafe key never enters an agent pod. File delivery remains for other providers |
| Q9 default grants for migrated TypeSafe keys | **`tool:jev-browser` and `component:judge` enabled** |

**Phase 2: user-signed submissions** (owner, 2026-10-14 ~22:40 PDT, issue #1347):
- each person gets a signing key (a console passkey, or a key the sc plugin keeps in the OS keychain);
- every task submission is signed, building on #342's signed step specs;
- a personal secret is released only to a task whose spec carries a valid signature from the secret's owner, and a group-workspace secret needs a current member's signature;
- headless jobs (schedules, issue runs, CI-fix rounds) use owner-signed standing grants.

This comes after the attempt-signed release of phase 1, so the build plan gains a phase after SEC-RELEASE.

**What Q5 and Q8 change in §8:**
- **Q5:** SEC-STORE-API disables the old version 24 h after a rotation, from a scheduled sweep.
- **Q8:** the TypeSafe forwarder becomes a lane of its own (SEC-INJECT-TYPESAFE) beside SEC-RELEASE. SEC-MIGRATE-TYPESAFE then moves jev to the forwarder, not to a key file.
