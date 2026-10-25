# Workspace secrets: one store, granted per consumer by checkbox

**Status: designed 2026-10-15 (lane SECRETS-DESIGN, part of #1347); the
store, its grants, the release and the console are built.** Built: §3.3's IAM
(lane SEC-IAM, `terraform/bootstrap/workspace_secrets.tf`, applied
2026-10-15 with `enable_workspace_secrets = true`), phase 1's management API
(SEC-STORE-API, §5), the release and its admission park (SEC-RELEASE,
SEC-ADMIT-PARK, §4), grant attestation (SEC-GRANT-ATTEST), the TypeSafe
forwarder (SEC-INJECT-A, SEC-INJECT-B, §3.5) and the console pane (SEC-UI-LIST,
SEC-UI-GRANTS, `apps/swarm-ui/src/WorkspaceSecrets.tsx`). Not built: the
migrations and the retirement of §8's phases 4 and 5 are partial or open.
SEC-MIGRATE-TYPESAFE is built behind `SWARM_TYPESAFE_INJECT`, which is off
(§3.5 says what remains), and SEC-MIGRATE-FORGE and SEC-RETIRE are not
built. Forge tokens appear only as Config › Secrets' status rows (SSC-OVERVIEW). The owner's answers are in §10; where they differ from §9's
recommendation (Q5, Q8), the answer wins.

**Extended 2026-10-24 by [secrets-scopes.md](secrets-scopes.md) (#2158).**
A secret now has a third scope, **repository**: an ordinary `-wsec-` secret of
this store, bound to one registered repository and released only to attempts
that clone it, under a grant signed form `"v2"`. When a person's, a
repository's and the workspace's secret share a name, the person's wins, then
the repository's. The store also gained a write-only destination it does not
keep, a repository's GitHub Actions secrets, and one read of every scope,
`GET /v1/secrets/overview`. §4 and §5 here record what SSC-REPO-SCOPE,
SSC-ADMIT-REPO and SSC-OVERVIEW built inside this store; secrets-scopes.md
§12 records the rest. This store's console moves to Config › Secrets with
lane SSC-UI, which is not built.

**Amended 2026-10-19 (ROLES-DOCS, part of #1518): Q4 is superseded.** A
group workspace's secrets are managed by its workspace `admin`, a role that
[workspace-roles.md](workspace-roles.md) added (its Q6), and still by any
platform admin. Members keep reading names and grants. §5's "Who may do
what" says how the code decides it.

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
* **Enforcement, recommended: swarm-api releases an agent's granted secrets to
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
| `swarm-tenant-u-<user>-typesafe` | a person's own TypeSafe key | that person, `PUT /v1/provider-keys/typesafe/user` | as above, when the submitter's **confirmed** personal tenant holds one; it wins over the tenant key (`provider_keys.resolve_provider_key`, restated by `jevkey.candidates`). On a group-tenant agent it is readable only where `terraform/bootstrap/typesafe_user_keys.tf` grants that group's worker account on that one secret (an owner apply per row); otherwise `jev_key_unreadable` and the tenant key is tried | as above |
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
| `attestation` | `v1.<base64url HMAC-SHA256>` over the canonical form of `(secret_id, tenant_id, consumer, slot, enabled)` under `swarm-workspace-grant-key` (SEC-GRANT-ATTEST). Null or absent on a grant written without the key |

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
   that the agent is STARTING or RUNNING at this lease and **this generation**
   with the lease unreleased (the browser-session slot's fence). A stale
   generation gets 409 and nothing, so a stale worker never holds a secret
   (invariant 5);
2. derives the consumer set from the **signed** task spec (§2.3), never from
   the request;
3. queries `workspace_secret_grants` where `tenant_id == task.tenant_id`,
   `enabled == true`, `consumer in [the set]`. A personal secret on a group
   agent is included only under §9 Q3's rule;
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
    top-level agent and for a child whose profile offers git tools
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

### 3.5 Option D: the secret never enters the pod (`inject`, first for TypeSafe)

The egress forwarder's shape: a platform proxy holds the secret and adds the
header to requests the agent makes to one host. The agent can use the API but
cannot read the key. It needs a per-provider adapter (which host, which
header), a forwarder in the request path, and agreement that the provider sees
the platform's proxy. It is worth it for a key whose leak is expensive; it is
not the base mechanism. Recorded as a delivery mode, `inject`, beside `file`
(§9 Q8). **The owner chose it for the first build, for TypeSafe (§10 Q8)**,
so the build is split in three lanes: SEC-INJECT-A (swarm-api's ticket, the
Kubernetes objects, the Terraform; built, below), SEC-INJECT-B (the forwarder,
`apps/swarm-typesafe-fwd/`, to this spec) and SEC-MIGRATE-TYPESAFE (jev and
the judge switched onto it).

**The spec both halves implement.** SEC-INJECT-A built swarm-api's side of it;
SEC-INJECT-B builds the forwarder to exactly this. A change to any line is a
change to both, and `swarm_api.typesafe_ticket` is the reference.

* **The MAC key** is the secret `swarm-typesafe-fwd-key`: its payload, read as
  UTF-8 and stripped of surrounding whitespace, is the HMAC key. swarm-api gets
  it as `SWARM_TYPESAFE_FWD_KEY` (a Cloud Run secret reference); the forwarder
  gets a version reference, `SWARM_TYPESAFE_FWD_KEY_SECRET`
  (`projects/<project>/secrets/swarm-typesafe-fwd-key/versions/latest`), and
  reads it with its own identity. Its value is generated by
  `scripts/create-secrets.sh --typesafe-fwd-key` and never shown. It is its own
  key, never `swarm-egress-key` or `swarm-live-key`.
* **The ticket** is `<payload>.<mac>`, both base64url without padding.
  `payload` is the JSON object, keys sorted, separators `,` and `:`, with
  exactly these claims: `tenant_id` (the attempt's tenant), `task_id`,
  `attempt_id`, `generation` (an integer), `scope` (`user` or `tenant`),
  `secret_id` (`swarm-tenant-<tenant>-typesafe` or
  `swarm-tenant-u-<user>-typesafe`), `secret_version` (decimal digits, or
  `latest`), `exp` (integer Unix seconds) and `nonce` (16 hex). `mac` is
  HMAC-SHA256 over `swarm-typesafe-ticket/v1\0` followed by the payload's
  ASCII. It carries no key value.
* **Verification.** The MAC is compared in constant time against each key in
  the ring; the claim set must be exactly the nine above; `exp` must be an
  integer later than now and no more than 900 s ahead (`MAX_TICKET_SECONDS`).
  Then, as defence in depth behind the MAC: `scope: tenant` must name
  `swarm-tenant-<tenant_id>-typesafe`, `scope: user` must name a
  `swarm-tenant-u-<user>-typesafe`, `secret_version` must be digits or
  `latest`; anything else is refused. Generation fencing is in memory, as in
  swarm-egress: the highest generation seen per `task_id`, a lower one refused
  `stale_generation`.
* **The key ring.** The forwarder re-reads `latest` of `swarm-typesafe-fwd-key`
  every 5 minutes. When it changes, the previous key stays in the ring until
  the next change, so a ticket a not-yet-rolled swarm-api instance minted
  still verifies for its life and longer. Rotation is: add the version, wait
  one poll, roll swarm-api, then disable the old version by hand.
* **The key read.** The forwarder reads
  `projects/<SWARM_TYPESAFE_FWD_PROJECT>/secrets/<secret_id>/versions/<secret_version>`.
  The project comes from its environment and never from a ticket, so no ticket
  reaches another project. A value read is held in memory for at most 60 s,
  so deleting a key (which destroys its versions) stops it within a minute.
* **The request.** The forwarder listens on plain HTTP, port 8080, at
  `http://swarm-typesafe-fwd.swarm-system.svc.cluster.local:8080`. It serves
  exactly `POST /v1/systemone`, the one path jev-browser 0.2.0 and the judge
  call (`TYPESAFE_URL` in `agent_worker/judge/settings.py`, `API_URL` in the
  image's `jev.mjs`), with no query string, origin-form only, and a `Host`
  that is the Service's name, with or without `:8080`. Every other method,
  path, host or absolute-form target is 404. The upstream is the constant
  `https://api.typesafe.ai/v1/systemone` over verified TLS, never derived from
  the request, so the forwarder is no SSRF path. The ticket arrives as the
  bearer credential of the `Authorization` header, where jev-browser already
  sends its key, so switching jev is a URL and a variable. The forwarder forwards only
  `Content-Type`, `Accept` and `User-Agent`, and sets the `Authorization`
  header itself, bearer scheme, to the key: every other header the caller sent,
  any other authorization, cookie or forwarding header included, is dropped.
  A body over `max_request_bytes` is 413. The answer is streamed back with
  `Content-Type` alone.
* **Refusals** carry `X-Swarm-Typesafe-Refused: <code>`: 401
  `ticket_invalid` (missing, malformed, forged or expired), 403 `tenant_off`,
  409 `stale_generation`, 429 `rate_limited`, 503 `disabled` (the kill switch,
  or a setting that is missing, not JSON, or has an unknown key: fail closed),
  502 `key_unreadable` or `upstream_failed`, 504 `upstream_timeout`, 404
  `not_found`, 413 `too_large` (the body over `max_request_bytes`) and 400
  `bad_request` (a chunked or unparseable body length, or a request line the
  server cannot parse; both codes added by SEC-INJECT-B, swarm-api unchanged).
  Nothing is ever forwarded without a key, and never with the caller's.
* **The setting** is `config.json` in the ConfigMap `swarm-typesafe-fwd-config`,
  re-read every 5 s, with exactly the keys `enabled` (bool), `tenants` (the
  tenant ids served; a ticket for any other is `tenant_off`),
  `requests_per_minute` (per attempt), `max_request_bytes` and
  `max_response_seconds` (the upstream timeout). terraform renders it from
  `var.typesafe_forwarder`; all off by default.
* **Logs** are JSON event lines carrying the code, `attempt_id`, `tenant_id`,
  `task_id`, `scope`, `secret_id`, the status, byte counts and duration.
  Never a header, a body, a ticket, a key or a value; each line goes through
  `swarm_redaction` with the key registered.

**What SEC-INJECT-A built (2026-10-15, part of #1347).**

* `POST /v1/attempts/{attempt_id}/typesafe-ticket`
  (`apps/swarm-api/swarm_api/typesafe_ticket.py`, route
  `routes/typesafe_ticket.py`). The worker proves itself with its tenant's ID
  token and the attempt key's signature under its own purpose,
  `swarm-typesafe-ticket-request/v1`, so a live-session, egress or child
  signature never verifies. The fence is the egress ticket's: RUNNING under
  this attempt's live lease at this generation, no cancel requested. A stale
  generation is 409 and nothing is minted. Then the workspace (the fenced
  agent's tenant, never a field of the request) must have chosen the `typesafe`
  judgment backend (403 `typesafe_backend_off`), and
  `provider_keys.resolve_provider_key` must find a key from the status records
  (403 `typesafe_no_key`): the submitter's confirmed personal key, else the
  tenant's, with one narrowing (§10 Q3): on a GROUP workspace's agent the
  submitter's own key is never named, because a personal key reaches a group
  agent only by per-secret opt-in and none exists for it yet; the group's key is
  named or the ticket is refused. The answer is `201 {ticket, expires_in, scope}`. The ticket lives
  `TYPESAFE_TICKET_SECONDS` (300 by default, clamped to 60-900). swarm-api
  reads no secret value to mint it. With no `SWARM_TYPESAFE_FWD_KEY` the route
  is 503 `typesafe_unavailable`. The five refusals ship off, as every new
  refusal does: until `REFUSAL_TYPESAFE_*=on`, the same status and message are
  served under `conflict` or `forbidden`, and nothing is minted either way.
* `kubernetes/swarm-system/swarm-typesafe-fwd.yaml`: Namespace, KSA
  `swarm-typesafe-fwd` (no token, no RBAC), the ingress NetworkPolicy (TCP 8080
  from a pod labelled `swarm-typesafe: inject` in a tenant namespace, as one
  peer with both selectors), the ConfigMap, a headless Service, and a
  one-replica Deployment with `requests == limits` (invariant 7) that names
  the image `swarm-typesafe-fwd` SEC-INJECT-B will publish.
* `terraform/infra/typesafe_fwd.tf`: `enable_typesafe_forwarder` (default
  false) creates `swarm-typesafe-fwd-key` with no version, read by swarm-api
  and the KSA's Workload Identity principal; `typesafe_forwarder.serve` adds
  the key reference to swarm-api and renders `swarm_typesafe_fwd_manifest`;
  `typesafe_forwarder.enabled` and `tenants` are the kill switch.
  `tests/terraform/typesafe_fwd.tftest.hcl` holds it.
* `terraform/bootstrap/typesafe_forwarder.tf`, which the owner applies:
  `enable_typesafe_forwarder_read` (default false) defines
  `swarmTypesafeForwarderAccessor` (`secretmanager.versions.access` only) and
  grants it to the forwarder's principal under
  `resource.name.startsWith("projects/<NUMBER>/secrets/swarm-tenant-") && resource.name.extract("/secrets/swarm-tenant-{t}-typesafe/versions/") != ""`.
  `tests/terraform/typesafe_forwarder.tftest.hcl` evaluates it: it admits the
  versions of `swarm-tenant-a-typesafe` and `swarm-tenant-u-x-typesafe` and
  refuses `-git`, `-wsec-`, `swarm-github-app-*`, `agents-*` and the rest. Its
  residual, a saved browser login whose name ends in `typesafe`, is in the
  file's header. Not verified live: lint it with `iamPolicies:lintPolicy`
  before applying.
* `scripts/create-secrets.sh --typesafe-fwd-key`.

**What SEC-INJECT-B built (2026-10-16, part of #1347).**

* `apps/swarm-typesafe-fwd/`, the forwarder, to the spec above. The standard
  library serves (`http.server`'s threading server) and calls upstream
  (`http.client` over `ssl.create_default_context()`); google-auth reads
  Secret Manager over REST; swarm-redaction filters every line. Its own
  `uv.lock`, seeded from swarm-egress's: the third-party closure is the same,
  so nothing new enters the platform.
  * `ticket.py` verifies the ticket with its own code (the image carries no
    swarm-api). The MAC is compared with `hmac.compare_digest` against every
    ring key, all of them each time, before the payload is parsed. Then the
    claims are checked as the spec says: exactly the nine, `exp` live and at
    most 900 s (+1 s, the reference's rounding) ahead, `scope: tenant` naming
    exactly `swarm-tenant-<tenant_id>-typesafe`, `scope: user` naming a
    `swarm-tenant-u-<user>-typesafe`, and the version digits or `latest`.
    Every id is held to a shape that cannot carry a `/`, so a claim cannot
    widen the Secret Manager name it becomes.
  * `keyring.py`: the ring is the current key and the one before it, until
    the next change (no timer), re-read every 300 s (`main.SECRET_POLL_SECONDS`).
    The secret cache reads `projects/<SWARM_TYPESAFE_FWD_PROJECT>/secrets/<id>/versions/<v>`
    and holds a value for at most 60 s. A value that is not one printable
    ASCII line is `key_unreadable`, so a value can never split the upstream
    request's headers.
  * `forwarder.py` runs the checks in the order its docstring lists, and
    nothing is read or forwarded until all of them pass: route (404), kill
    switch (503), ticket (401), tenant (403), generation (409), rate (429),
    body length (413/400), key (502), upstream (502/504). It forwards only
    `Content-Type`, `Accept` and `User-Agent`, and sets the Authorization
    header itself, bearer scheme, to the key. It answers with the upstream's status and
    `Content-Type` alone, streamed. Fencing and the per-attempt sliding-minute
    rate are in memory, which is why the Deployment has one replica.
  * `upstream.py`: the upstream is the constant
    `https://api.typesafe.ai/v1/systemone`. Nothing in the request names it.
    The overall deadline is `max_response_seconds`, checked between chunks as
    well as per socket read.
  * `server.py` overrides what the standard library would leak: an unknown
    method is 404, not 501; a parse error is 400 `bad_request` with no echo,
    not an HTML page quoting the request line; the request line is never
    logged; no `Server` version. One request per connection.
  * `main.py` reads the MAC key before it opens the port, because the
    readiness probe is a TCP check.
  * The log is JSON events (`typesafe_refused`, `typesafe_forwarded`,
    `typesafe_config`, `typesafe_key_rotated`, ...). They carry only the code,
    status, ids, scope, secret id, byte counts, duration and a cause word.
    Each line goes through `swarm_redaction.redact` with the ring keys and
    every held value registered as literals.
* `apps/swarm-typesafe-fwd/Dockerfile`: swarm-egress's posture (digest-pinned
  bases through the mirrors, hash-locked installs, uid 10001,
  `PYTHONDONTWRITEBYTECODE` for the read-only root, tini) on port 8080.
* The image `swarm-typesafe-fwd` in `ALL_TARGETS` of
  `scripts/build-images.sh` and `scripts/push-images.sh`, after swarm-egress.
  Its promoted digest becomes `image_refs["swarm-typesafe-fwd"]`, which
  `typesafe_fwd.tf` already substitutes for `__SWARM_TYPESAFE_FWD_IMAGE__`.
  swarm-system images are rendered by terraform, not `kubernetes/render.py`,
  so render.py is unchanged.
* Tests, all offline: `tests/unit/typesafe_fwd/`. The real server runs over
  loopback against a fake upstream and a fake Secret Manager, with every key
  built at runtime. A ticket `swarm_api.typesafe_ticket` mints verifies. Every
  bad ticket, tenant, generation, route, method and host is refused with
  nothing read and nothing forwarded. The caller's authorization is replaced.
  The kill switch, the rate limit and the ring all hold. No log line or
  response carries a key, a value or a ticket. The image is in the build path.

**Still to do.**

* **SEC-INJECT-B, what is left:** the release and hotfix step only. Its
  apply is built (TS-ROLL, PR 1414): `kubernetes/apply.sh --swarm-typesafe-fwd`
  is its own mode, as `--swarm-graph` is, not part of `--swarm-system`. It
  reads `terraform output -raw swarm_typesafe_fwd_manifest` and refuses a
  manifest with a placeholder, RBAC, a Secret or an object outside
  swarm-system. `scripts/roll-typesafe-fwd.sh`, in the shape of
  `scripts/roll-graph.sh`, applies through it, waits for the rollout and
  proves the pods run the rendered digest. While the output is null (until
  `typesafe_forwarder.serve` is set) it prints one notice and exits 0.
  `swarm-typesafe-fwd` is in `security.yml`'s scheduled image scan. The step
  that runs the roll script in `release.yml` and `hotfix.yml` came with PR
  1417, a workflow change the owner pushed (lanes cannot push workflow
  files); both workflows name `roll-typesafe-fwd.sh` on main (read
  2026-10-23).
* **SEC-MIGRATE-TYPESAFE, what is built:** both halves, behind
  `SWARM_TYPESAFE_INJECT`, which is **off**. The pod side: the dispatcher
  labels a TypeSafe-backed pod `swarm-typesafe: inject` (TS-MIGRATE-POD, PR
  1409) and sets the variable on the same pods (TS-INJECT, PR 1456; until it
  merged, a labelled pod's worker never saw the switch), so the label and the
  variable always arrive together. The tenant-side egress policy
  (`network-policies/allow-typesafe-fwd.yaml`, built like
  `allow-judge-egress.yaml`) is in `render.py`'s `TENANT_FILES` and in the
  workspace deployer's scope policy
  (`kubernetes/policies/workspace-provisioner-scope.yaml`). The worker side
  (TS-MIGRATE-WORKER, PR 1411, `agent_worker.typesafe_shim`): the worker asks
  swarm-api for the ticket (`POST /v1/attempts/{attempt_id}/typesafe-ticket`)
  and renews it the way it renews the egress ticket. Because jev-browser reads
  `TYPESAFE_API_KEY` once at start and a ticket lives minutes, the ticket
  reaches jev per request through a loopback shim in the worker: jev is handed
  the shim's URL and a local token the shim checks and replaces, so no
  TypeSafe key is in its process. The switch is honoured only on GKE; a Cloud
  Run worker with it set keeps today's key file. Switched off, the key file,
  the judge's key and `JEV_API_URL` are exactly what they were. The jev
  wrapper (`images/agent-runtime-browser/jev-browser/bin/jev-browser-mcp`)
  passes the shim's loopback URL through for the TypeSafe backend since PR
  1418 (merged 2026-10-16 18:24Z) and carries `SHIM_PASSTHROUGH_MARKER`;
  `runners/jev_launch.py` still refuses to start jev against a wrapper
  without the marker, so an image built before PR 1418 gets no TypeSafe
  judgment with the switch on rather than a keyless call to api.typesafe.ai.

  **What is left**, none of it optional:
  * The owner re-applies `kubernetes/apply.sh --policies` before the release
    that renders `allow-typesafe-fwd.yaml`, or the deployer is refused the
    new policy.
  * The owner adds the tenant to `typesafe_forwarder.tenants` and sets
    `enabled`. `typesafe_forwarder.serve` (which renders the forwarder's
    manifest and puts `SWARM_TYPESAFE_FWD_KEY` on swarm-api) is already set
    in dev, after `swarm-typesafe-fwd-key` got its first version on
    2026-10-16 (`terraform/environments/dev/dev.tfvars`, read 2026-10-23);
    `enabled` is false there and no tenant is listed. Before that nothing is
    forwarded and no pod is labelled: the dispatcher reads the same `enabled`
    and `tenants`, rendered "false" unless the forwarder is served.
  * The forwarder is reachable only from GKE pods, so a judge running on
    Cloud Run cannot use it: that needs a decision before the judge moves.
  * Only then does `jevkey.prepare` stop writing the key file, and SEC-RETIRE
    remove the tenant workers' `-typesafe` grants. Until both happen, every
    tenant worker can still read its TypeSafe key, whatever the switch says.

  What the built forwarder asks of that lane:
  * The URL is exactly
    `http://swarm-typesafe-fwd.swarm-system.svc.cluster.local:8080/v1/systemone`.
    The `Host` must be that full name: `swarm-typesafe-fwd.swarm-system` and
    other short forms are 404.
  * The ticket is the bearer credential of the one Authorization header
    (two Authorization headers are 401).
  * The body is sent with a `Content-Length`, never chunked.
  * One request per connection.
  * The answer to a refusal carries `X-Swarm-Typesafe-Refused`. A 401
    `ticket_invalid` means renew the ticket. A 409 `stale_generation` means
    exit without judging (invariant 5).
  * The worker's ticket renewal must stay ahead of `exp`, because the
    forwarder allows no grace past it.
  * The owner adds the tenant to `typesafe_forwarder.tenants` and sets
    `enabled`.

### 3.6 Recommendation

**B, with C as defence in depth, and D as a per-provider upgrade later.** It is
the only option where minting the GSA token gets the agent nothing, where a
tool-level checkbox means something (a `claude-code-browser` attempt with
`browser_tool: playwright` is not released the jev-browser key), where a toggle
takes effect at the next attempt without an IAM round-trip, and where personal
secrets on a group agent no longer need an owner-applied per-person IAM row
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
`required` (§7 request A). A required slot with no enabled grant parks the agent
`CREDENTIAL_MISSING` **before** it leases, at no cost, and it starts by itself
once the grant is ticked, which is today's rule for a missing provider key.
Admission counts a grant as present when it is enabled. A grant's
`attestation` is enforced by the release, not by admission, so a forged grant
can at worst admit an agent that the worker's backstop then parks ("What
SEC-ADMIT-PARK built", end of §4).

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
  release is computed per attempt, and a parked agent that resumes is a new
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
| `workspace_secret_grant_attested` | `{tenant_id, secret_id, consumer, slot, enabled, grant_id, reason}`: the attest route signed a grant written before SEC-GRANT-ATTEST (`audit_match`) or under the previous key (`previous_key`). Never the MAC |

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
  naming the path's attempt id, takes the tenant and the agent from it, and
  verifies the worker identity and the attempt-key signature under
  `swarm-secrets-release/v1` against the registration for that whole tuple. A
  wrong generation, a wrong key, another route's purpose, another tenant's
  identity or an unknown attempt is the same 403.
* **Fenced twice.** The agent must be STARTING or RUNNING at this lease and
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
  **Forged grants inside a workspace** were the gap this lane left: an
  agent that learned one of its own workspace's secret ids could write an
  enabled grant for it. SEC-GRANT-ATTEST closes it. "What SEC-GRANT-ATTEST
  built" below says what is now stopped and what still is not.
* **Q3, as decided.** A personal secret reaches a group agent only when its
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
  `slot`, and an `expire_at` 90 days out. The marks have one too. **Both
  expire by a Firestore TTL policy on `expire_at`:** one
  `google_firestore_field` per collection, in
  `terraform/modules/firestore/workspace_secrets_ttl.tf` (SECRETS-RELEASE-TTL),
  held to swarm_api's collection and field names by
  `tests/unit/control_plane/test_workspace_secrets_ttl_parity.py`.
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
* **The scheduler's pre-lease park** was left to a later lane, which built it
  next (below).

### What SEC-ADMIT-PARK built (2026-10-15, part of #1347)

§4.2's pre-lease park. `apps/scheduler/scheduler/secret_slots.py` states the
rule: `missing_required_slots(profile, inputs, granted)` is pure, and
`WorkspaceGrants` reads it. `credentials.credential_for` asks it as the last
half of its one question, after the provider key and the GitHub connection.
So `loop.Scheduler._admit_one` parks `CREDENTIAL_MISSING` before the capacity
transaction, and `_promote_credentials` returns the park to `READY` once every
required slot has an enabled grant. Held by
`tests/unit/control_plane/test_secret_slot_admission.py`.

* **The consumers are the release's.** `profile:<name>` when the profile
  declares a slot, and `tool:<t>` for each tool `tools_launched` says its
  `browser_tool` launches, spelt by `consumer_name`. The scheduler's image does
  not carry swarm-api, and `swarm_common` is frozen, so `attempt_consumers`
  could not be moved. The derivation is composed from the same frozen pieces,
  and a test holds it to `attempt_consumers` for every profile and every
  `browser_tool` value. The components (`merge`, `indexer`) declare no slot,
  so they can require nothing.
* **A grant counts when it is enabled** and its own document names the agent's
  workspace. A grant naming no slot fills its consumer's only slot. **The
  release, not admission, is where a grant is proven:** admission reads no
  record of a workspace secret and no `attestation`, so a forged
  grant can at worst admit an agent that the worker's backstop then parks.
* **Q3 at admission.** A group agent counts the submitter's personal secret
  under the release's rule: the personal tenant names them, and the record is
  theirs, `personal`, `active` and opted in to this workspace. The one check
  admission cannot make is the directory's "a member of the group NOW", so a
  person who has left the group is admitted and then parked by the worker.
* **The event** keeps `credentials.py`'s fields (`provider`,
  `account_pool`) and adds `workspace_secrets: [{consumer, slot}]`. It holds
  names only, never a secret id.
* **The sweep asks every `CREDENTIAL_MISSING` park,** the worker's backstop
  park included. A park for a key or an account that is still missing stays
  parked whatever the grants say. A backstop park whose grant is ticked is
  promoted at once, even when the worker parked because swarm-api was
  unreachable, as it was before this lane.
* **One query per admission pass**, of every enabled grant, made lazily: only
  an agent that requires a slot reads it. No catalogue slot is required yet
  (request 80), so today no pass reads anything extra.

### What SEC-GRANT-ATTEST built (2026-10-15, part of #1347)

Owner decision 2026-10-15, answering SEC-RELEASE's open question "Attest
grants": swarm-api MACs every grant it writes, and the release ignores a grant
whose attestation fails. Built: `apps/swarm-api/swarm_api/grant_attest.py`
(the canonical form, the key ring, the release policy), the grant writers in
`workspace_secrets.py` (`set_grant`, `delete`, `attest_grants`), the policy in
`release_plan`, the admin route `POST /v1/admin/workspace-secrets/attest-grants`
in `routes/workspace_secrets.py`, three settings, the
`--workspace-grant-key` slot of `scripts/create-secrets.sh`, and
`terraform/infra/workspace_grant_key.tf`. Held by
`tests/unit/control_plane/test_grant_attest.py`,
`tests/unit/scripts/test_create_secrets_workspace_grant_key.py` and
`tests/terraform/workspace_grant_key.tftest.hcl`.

* **The form is stated once**, in `grant_attest.canonical`:
  `["v1", secret_id, tenant_id, consumer, slot or null, enabled]`, compact
  JSON, ASCII. Every field the release decides on is in it: the secret, the
  workspace the grant is queried under, the consumer it is matched against,
  the slot it is delivered into and the checkbox. A wrong type (an `enabled`
  of `1`, an empty slot) is not canonical and never verifies. `"v1"` comes
  first so the form can change. A test greps that no other module builds it.
* **The key** is `swarm-workspace-grant-key`. Only swarm-api reads it, as a
  ring like `swarm-child-key`'s: `SWARM_WORKSPACE_GRANT_KEY` signs and
  verifies, and `SWARM_WORKSPACE_GRANT_KEY_PREVIOUS`, set only during a
  rotation, only verifies. Each ring key is compared with
  `hmac.compare_digest`.
* **The release drops** a grant whose attestation is malformed, made for
  other fields or made by neither ring key. It logs
  `grant_attestation_invalid` with the grant id, the tenant id and the secret
  id, never the MAC or the key. A dropped grant is treated exactly like a
  disabled one, so a required slot it would have filled parks
  CREDENTIAL_MISSING through the worker's existing path.
* **`grant_attestation_required` is off by default.** No grant written
  before this lane carries an attestation. Switched on first, all of them
  would stop being released at once, in every workspace. While it is off, an
  UNATTESTED grant is still released, with a `grant_attestation_missing`
  warning. A grant whose attestation is PRESENT but wrong is dropped either
  way. The only way to make one is to alter a grant swarm-api signed, so the
  migration gives no reason to accept it.
* **The migration** is `POST /v1/admin/workspace-secrets/attest-grants`,
  for platform admins only (the rotation sweep's auth). It signs a grant only
  when the grant's fields equal its newest `workspace_secret_grant_enabled`
  or `_disabled` audit row, and lists every other grant by id. A grant that
  verifies only under the previous key gets re-signed under the current one,
  which is what lets a rotation end. Each signature gets its own
  `workspace_secret_grant_attested` audit row. The operator sequence is in
  [docs/runbooks/workspace-grant-attestation.md](../runbooks/workspace-grant-attestation.md).
* **The writers.** `set_grant` signs every grant it writes. With no key it
  writes `attestation: null`, never a stale MAC. It refuses to ENABLE a grant
  while attestation is required and there is no key. `delete` re-signs the
  grants it disables, as disabled.
* **Rotation.** Add a version with `scripts/create-secrets.sh
  --workspace-grant-key` and set `workspace_grant_attestation.previous_version`
  to the replaced version number. Apply, then run the attest route, which
  re-signs every previous-key grant. Then clear `previous_version`, apply
  again, and disable the old version. The previous key must stay in the ring
  until the route has run. Otherwise every grant it signed is dropped.

**What is now stopped.** With the setting on, an agent that can write
Firestore cannot get a secret released that a manager did not grant. Each of
these fails the MAC and is dropped: a new grant document, an attestation
copied from another grant, a disabled grant flipped to enabled, and a grant
whose consumer, secret, slot or workspace has been rewritten. With the
setting off, every one of those that carries an attestation is still
dropped. Only an unattested grant gets through, and it is logged.

**What still is not stopped:**

* **Replaying a grant's own earlier state.** A MAC over the grant's fields
  cannot tell its current state from an earlier one swarm-api also signed.
  Suppose an agent reads a grant while it is enabled (it can read any grant
  whose document id it can guess), and a manager then disables it. The agent
  can write the grant back as it was, attestation and all. After revoking a
  sensitive grant, the workaround is to rotate the key twice, following the
  runbook's rotation each time, so the old MAC verifies under neither ring
  key. A lasting fix needs state the agent cannot write, and Firestore offers
  no such state to a server-side identity (docs/multi-tenancy.md, "The
  Firestore row, in full").
* **The migration window.** Until `grant_attestation_required` is on, an
  unattested grant is released. Turn it on as soon as the attest route has
  run and its unsigned list is dealt with.
* **The attest route trusts `admin_audit`**, which an agent can write to as
  easily as it writes grants (a new document under a random id). If an agent
  forged a grant AND a matching audit row before the route ran, that grant
  would be signed. Run the route once, soon after the key is served. Check its
  `signed` list against the console's matrix before turning the setting on.
* **The record is not attested.** A record's `current_version` and
  `use_in_workspaces` can still be written by the tenant. `owned_by` keeps
  the version inside the workspace's own `-wsec-` secrets, and Q3 re-checks
  membership and the creator, so neither crosses a workspace. Inside a
  workspace, an agent can point a granted record at another version of the
  same secret, if the sweep has not yet disabled it.

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
* **Who may do what** was §9 Q4, and is now workspace-roles.md's Q6
  (amended 2026-10-19). A personal workspace's secrets are its person's. A
  group workspace's are its `admin`'s: a person a workspace admin or a
  platform admin granted `admin` there, or any platform admin, who is
  `admin` in every group workspace (workspace-roles.md §4.2).
  `apps/swarm-api/swarm_api/workspace_secrets.py::require_manager` decides
  it for secrets and for MCP servers alike: its person for a personal
  workspace, then
  `apps/swarm-api/swarm_api/workspace_secrets.py::workspace_admin`, which
  reads the role fresh, then a platform admin. A `member` who manages
  nothing gets 403 `role_admin_required` once that code's switch is on; while
  it is off, the refusal is logged and the member gets the 403 this route
  gave before roles, because a member never managed a group workspace's
  secrets. `can_manage` follows the same rule. Whoever may not manage sees
  the list and the matrix read-only.

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

### What SSC-REPO-SCOPE built (2026-10-24, part of #2158)

The repository scope of [secrets-scopes.md](secrets-scopes.md) §2, with the
owner's answers of its §11 (Q1, Q2, Q9). It is built in this store, not
beside it: `apps/swarm-api/swarm_api/workspace_secrets.py`,
`grant_attest.py`, `routes/attempts_secrets.py`, the new
`routes/repository_secrets.py` and `github_repo_admin.py`, held by
`tests/unit/control_plane/test_workspace_secrets.py`, `test_grant_attest.py`,
`test_secret_release.py` and `test_github_repo_admin.py`.

* **A repository is a field of the record, not a consumer (Q9).** A
  repository secret is an ordinary `-wsec-` secret of the workspace, with
  `scope: repository`, the registration's `repo_id` and `repository`
  (`owner/repo`, for display). Its name is unique per (workspace,
  repository): the name index key is `<tenant>~<repo_id>~<name>`. The
  workspace's own list (`GET /v1/workspace/secrets`) no longer shows
  repository secrets; `GET /v1/repositories/{repo_id}/secrets` does.
  `SECRET_CONSUMER_KINDS` and everything under `apps/common/swarm_common/`
  are unchanged.
* **Grant form `"v2"`.** A grant on a repository secret copies the
  record's `repo_id` and is signed
  `["v2", secret_id, tenant_id, consumer, slot, enabled, repo_id]`; every
  other grant is still signed `"v1"`, and a `"v1"` attestation still
  verifies. The release takes a repository grant only when its `"v2"`
  attestation verifies **whatever `grant_attestation_required` says**, its
  `repo_id` is the record's, and the attempt clones that repository. A `"v1"`
  MAC never vouches for a binding: it does not cover `repo_id`. With no grant
  key, enabling such a grant is `grant_key_required` (422, behind its switch;
  off, the grant is written unattested and the release drops it).
* **Which repositories an attempt clones** is `attempt_repositories`, from
  the signed spec: `repository_url`'s registration id, then each
  `additional_repositories` entry's, whose `repo_id` is recomputed from its
  `repository` and the attempt's tenant. One mismatch withholds every repository
  secret of that attempt (`repository_binding_mismatch`, logged). A
  repository secret whose registration is gone is withheld too.
* **One `(slot, name)` in several scopes is released once (Q1)**: the
  person's, then the repository's (the primary repository's before an
  additional one's), then the workspace's. Each other item is in `withheld`
  with `shadowed_by_personal`, `shadowed_by_repository` or
  `shadowed_by_workspace`, so the worker never receives one file twice.
* **Who may write (Q2).** `require_manager` as before (a workspace admin, a
  platform admin, or a personal workspace's person), **or** a member whom
  GitHub calls admin of that repository, asked at each write with that
  person's own GitHub user token (`github_repo_admin.check`, through
  `AccessService.user_token`), never with the App's installation token or
  the tenant's forge token, and never from a stored role. No connection, an
  expired token, a 404, a refusal or a GitHub error is a 403 with
  `detail.reason` (`github_not_connected`, `github_token_expired`,
  `repository_not_visible`, `github_not_admin`, `github_unavailable`); its
  code is `github_admin_required` with that switch on and `forbidden` while
  it is off. Every audit row names the rule that let the writer through
  (`detail.authority`: `workspace_admin` or `github_repo_admin`; a
  registration's removal, `repository_delete`).
* **What it refuses.** Another workspace's `repo_id` is 404 on every route,
  and so is a registration whose `owner/repo` no longer derives its id. An
  MCP server is never granted a repository secret (422): `mcp:attach` matches
  no repository, so such a grant would reach every repository's attempts.
* **A removed registration.** The release withholds its secrets while the
  registration is gone. That stops an honest removal, not an agent, which can
  write the registration document back.
  `POST /v1/repositories/{repo_id}/secrets:disable-grants` (a manager) turns
  every grant on them off, re-signed off, so re-registering the same
  repository revives nothing. `DELETE /v1/repositories/{repo_id}` (a
  member, as before) runs the same disable before it deletes, and keeps the
  registration when the disable fails (SSC-REPO-DELETE); its audit rows say
  `detail.authority: repository_delete`.
* **What a repository secret IS does not rest on its record.** Its Secret
  Manager id is marked: of the 16 hex in `-wsec-<16 hex>`, the last 8 are
  sha256 over a fixed tag and the first 8 (`repository_hex`), and a workspace
  secret's id is drawn until it is not marked. An agent cannot create a
  Secret Manager secret, so stripping `scope` and `repo_id` from a record and
  writing itself an unattested grant (released for a workspace secret while
  `grant_attestation_required` is off) releases nothing: `owned_by` keeps a
  marked id off the workspace path, the person's opt-in and `mcp:attach`.

### What SSC-ADMIT-REPO built (2026-10-24, part of #2158)

The admission half of [secrets-scopes.md](secrets-scopes.md) §2.3:
SEC-ADMIT-PARK's pre-lease park counts a repository secret only for an agent
that clones its repository, so a required slot filled only by another
repository's secret parks `CREDENTIAL_MISSING` before any lease, holding no
capacity (invariant 1). Built in `apps/scheduler/scheduler/secret_slots.py`
and `credentials.py`, held by
`tests/unit/control_plane/test_secret_slot_admission.py`.

* **The repository set is the release's.** `credentials.attempt_repositories`
  restates swarm-api's `attempt_repositories` (the scheduler's image does not
  carry swarm-api): the https `repository_url`'s registration id, then each
  `additional_repositories` entry's, its `repo_id` recomputed from
  `repository` and the tenant, and one mismatch counts no repository secret
  at all. A test holds the two equal over the same task documents. The
  scheduler's codec does not decode `additional_repositories`, so
  `WorkspaceGrants` reads them from the agent's own document, by id, once per
  pass, and only for an agent whose workspace has a repository-bound grant.
* **A repository grant counts when the release would take it, less the
  MAC.** The grant carries a `repo_id` in that set; its secret id carries the
  repository mark (`is_repository_secret_id`, restated and held to
  swarm-api's); the record is this workspace's, `active`, `scope: repository`
  and bound to the same `repo_id`; and `repositories/{repo_id}` still names
  this workspace. The `"v2"` attestation is still the release's alone. A grant
  carrying a `repo_id`, or on a marked id, never counts as the workspace's
  own or as a person's opt-in, so admission now reads a secret record for a
  repository grant (and its registration), never for a workspace one.
* **Q1's order** (§11): the person's, then the repository's, then the
  workspace's. `WorkspaceGrants.missing` asks each scope only while a required
  slot is still empty. The order decides which scope the release delivers
  into the slot, never whether the slot is filled: shadowing keeps one item
  per `(slot, name)` and the worker counts a slot filled by any item in it.
* No catalogue slot is required yet (request 80), so this changes no
  admission today; it is built so the first required slot does not admit
  wrongly.

### What SSC-OVERVIEW built (2026-10-24, part of #2158)

Config › Secrets' one read, [secrets-scopes.md](secrets-scopes.md) §4 and
§6.2, with the owner's Q1 of its §11: `GET /v1/secrets/overview?scope=
workspace|personal|repository[&repo_id=..]`, in
`apps/swarm-api/swarm_api/secrets_overview.py` and
`routes/secrets_overview.py`, held by
`tests/unit/control_plane/test_secrets_overview.py`.

* **A view, not a store.** No store migrates. Each row names its `store`,
  its console `section`, and the API route that writes it (`writer`) or the
  page it is still managed on (`page`). The route reads Firestore only: no
  Secret Manager payload, no broker and no forge call.
* **What each scope lists.** *Workspace*: this store's own secrets, the
  TypeSafe tenant key read from `provider_keys` (the tenant's `credentials`
  never lists it), the runner LLM keys the tenant document lists, each MCP
  server in `workspace_secret` mode with the store secrets granted to it,
  one "subscription accounts" line linking to Capacity › Accounts, and the
  tenant's GitHub token. *Personal*: the caller's own store secrets
  (`opted_in` to the selected workspace or not), their TypeSafe key, their
  saved browser logins and their GitHub user slots. *Repository*: that
  repository's secrets for agents, each of its QA targets' sign-in
  credential (and challenge bypass, where one is configured) per
  environment, its `-git-r-` token, and SwarmCloud's own record of the
  Actions secrets it wrote there (`actions_secret_writes`; GitHub's live list
  stays `GET /v1/repositories/{repo_id}/actions/secrets`). Not listed, as §4
  says: `dispatch`-mode MCP credentials, QA identity grants and platform
  secrets. The answer's `not_listed` names them.
* **Never a value, a length or `last4`.** Every row is an allow-list of
  fields. A git token row carries kind, state, login, verified and expiry
  times, never `last4`. No row carries a Secret Manager version name.
* **Personal rows are only the caller's.** The personal scope reads the
  personal workspace derived from the caller's verified email and confirmed
  by its tenant document (`own_personal_tenant`). A narrowed session
  (`member_scope`) has none here. Another person appears only as the winner
  on a workspace or repository row, for a secret they opted in to this
  workspace.
* **Shadowing (Q1).** Two secrets of one name shadow where an enabled grant
  of each delivers into the same slot (the release's `_deliverable`, less the
  attestation check). The order is the person's, then the repository's, then
  the workspace's. The loser carries `shadowed_by`, the winner `shadows`, each
  entry with its `slot` and `where` ("agents of <person>", "agents that clone
  <owner/repo>"). Two repositories' secrets of one name are not compared,
  because which one wins depends on the order the attempt clones them in.
* **Who reads.** Every member of the workspace reads the workspace and
  repository scopes (§2.5). `can_manage` is per row: the manager rule
  (`can_manage`) for the workspace's and the repository's rows, true for
  the caller's own personal rows, and false for the status-only rows (GitHub
  tokens, subscription accounts). Each writer stays the gate. Another workspace's
  `repo_id` is 404. A `repo_id` without `scope=repository`, or
  `scope=repository` without one, is 422, and so is any query parameter
  other than `scope` and `repo_id`.

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

**Q3. May a person's own secret be used in a group workspace's agents they
submit?** (a) never; (b) **per secret, opt-in by its owner, only on agents that
person submits** (recommended; it replaces the per-person IAM rows of
`typesafe_user_keys.tf`, and the group's other members' agents never get it);
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
| Q4 who manages a group's secrets | **Platform admins only**; superseded 2026-10-19 by workspace-roles.md's Q6: the group workspace's `admin`, and platform admins |
| Q5 rotation | **The previous version stays enabled for 24 hours**, then is disabled (not destroyed). This differs from the recommendation: agents already running keep working across a rotation |
| Q6 release audit rows | **90 days, by a Firestore TTL**, for every release row |
| Q7 runner provider keys and pool accounts | **Listed read-only** on the Secrets page, not moved |
| Q8 inject mode | **In the first build, for TypeSafe.** This differs from the recommendation: a platform forwarder adds the TypeSafe key to jev's requests, so the TypeSafe key never enters an agent pod. File delivery remains for other providers |
| Q9 default grants for migrated TypeSafe keys | **`tool:jev-browser` and `component:judge` enabled** |

**Phase 2: user-signed submissions** (owner, 2026-10-14 ~22:40 PDT, issue #1347):
- each person gets a signing key (a console passkey, or a key the sc plugin keeps in the OS keychain);
- every agent submission is signed, building on #342's signed step specs;
- a personal secret is released only to an agent whose spec carries a valid signature from the secret's owner, and a group-workspace secret needs a current member's signature;
- headless jobs (schedules, issue runs, CI-fix rounds) use owner-signed standing grants.

This comes after the attempt-signed release of phase 1, so the build plan gains a phase after SEC-RELEASE.

**What Q5 and Q8 change in §8:**
- **Q5:** SEC-STORE-API disables the old version 24 h after a rotation, from a scheduled sweep.
- **Q8:** the TypeSafe forwarder becomes a lane of its own (SEC-INJECT-TYPESAFE) beside SEC-RELEASE. SEC-MIGRATE-TYPESAFE then moves jev to the forwarder, not to a key file.
