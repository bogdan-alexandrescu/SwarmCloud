# Secrets scopes: workspace, person and repository on one Config › Secrets page, and GitHub Actions as a write-only destination

**Status: built 2026-10-24 (lane SSC-DOCS, part of #2158), except phase 0
and the console's GitHub Actions pane.** Designed 2026-10-23 (lane SECRETS-SCOPES-DESIGN, PR 2159);
BGD answered §10 the same evening (§11), and **§11 overrides this document
wherever the two differ**. Every §9 lane but two has merged, each marked
built with its PR in §9, and §12 records what each one built, the five lanes
added during the build, and where the code departs from the text above:

* **SSC-SECRETS-APP (phase 0) is not built and will not be**: Q3 put the
  Actions permissions on the existing SwarmCloud App, so there is no second
  App, no `swarm-github-secrets-app-private-key` and no
  `GITHUB_SECRETS_APP_ID`. What remained of phase 0 is BGD's step on
  GitHub, written in `docs/runbooks/github-app.md` ("Writing GitHub Actions
  secrets"). §3.5's second App, and every "under Q5 a" sentence, describe
  the design BGD declined.
* **SSC-UI is built (lane SSC-UI, 2026-10-25); SSC-UI-ACTIONS is not built.**
  Config › Secrets draws every scope and opens every store's one-time form;
  its GitHub Actions position shows SwarmCloud's record of what it wrote,
  and a GitHub Actions link opens a page that does not offer that form yet.
* **Not yet live:** the Actions writes are refused (`secrets_not_granted`)
  until BGD grants the App Secrets and Environments and each installation
  accepts them; Admin › Platform secrets reads `unreadable` until BGD sets
  `enable_platform_secret_status = true` and runs `make bootstrap`; and the
  new refusals ship off (§12).

It extends [workspace-secrets.md](workspace-secrets.md), whose store, grants,
release (Option B, §3.3) and console (SEC-UI-LIST, SEC-UI-GRANTS) are built.

BGD's decisions, 2026-10-23 (~21:20-21:30Z, #2158 and #2157), which this
design takes as given rather than asks again:

* One page, **Config › Secrets**, manages workspace secrets (today's store and
  grants), **personal** secrets (the browser-session secrets browser agents
  ask for, and personal keys), **per-repository** secrets, **keys for agents
  and MCP** (workspace LLM provider keys, TypeSafe keys for the workspace and
  the person, registered MCP servers' credentials), **GitHub tokens, status
  only** (connecting stays on Config › GitHub access), and the
  **repository-bound extras**: QA target sign-in credentials and
  challenge-bypass values, and **GitHub Actions secrets SwarmCloud writes into
  a repository or one of its environments**, including creating a main-only
  environment such as the `repo-secrets` environment PR 2128 needs.
* **Platform-level secrets and settings are not on Config.** They belong to
  Admin, which only admins see and only admins may change, on every surface.
* **A value is always set through a one-time form** (a console link), never
  shown again, and never passes through a Claude session. The API and the sc
  plugin offer the same thing as a link, the way `swarm_key_set_link` does.

Every file and symbol cited was read on 2026-10-23 at `8b9f137`. Search for
the symbol named beside each citation: lines move.

What the design settles, in one line each:

* **A repository is an attribute of a secret record, not a new kind of
  consumer.** A repository secret is an ordinary `-wsec-` secret of the
  workspace the repository is registered in, carrying `scope: repository` and
  the registration's `repo_id`. It is released only when an enabled, attested
  grant names one of the attempt's consumers **and** the attempt's signed
  spec clones that repository (§2).
* **When two scopes offer one name, the person's wins, then the
  repository's, then the workspace's**, decided in swarm-api before the
  release leaves, so the worker never receives two items for one file (§2.4,
  Q1).
* **GitHub Actions is a destination, not a store.** The value is sealed to
  GitHub's public key and PUT to GitHub. SwarmCloud keeps the name, who set
  it and when, and nothing else (§3).
* **The writer is a second, narrow GitHub App** (*declined, Q3 of §11: the
  existing SwarmCloud App writes, and §12 says how*), whose installation tokens
  are minted by swarm-api for one repository and one permission at a time and
  never stored, logged or sent to a pod. The existing SwarmCloud App does not
  gain `Secrets` or `Environments`, because its user tokens sit in slots a
  tenant worker can read (§3.5, Q3).
* **Consolidation is a view, not a migration.** Every existing store keeps
  its collection, its Secret Manager names and its writers. Config › Secrets
  reads them through one overview route and opens each store's own form (§4).
* **Admin › Platform secrets is status only**: set or not, age, and who
  manages it (Terraform, a script, the broker). It is read from Secret
  Manager metadata, never from a value (§5).

---

## 1. What exists today, by scope

The inventory of workspace-secrets.md §1 still holds. This table regroups it
by the scope BGD named, adds what was built after it, and says where each
row is managed today.

| scope | what | store (secret · Firestore) | who sets it today | reader at runtime | console today |
|---|---|---|---|---|---|
| workspace | store secrets | `swarm-tenant-<t>-wsec-<16 hex>` · `workspace_secrets`, `workspace_secret_grants` | `apps/swarm-api/swarm_api/workspace_secrets.py::require_manager`: the workspace admin or a platform admin | swarm-api's release, `apps/swarm-api/swarm_api/routes/attempts_secrets.py` | `settings/workspace/secrets` (`apps/swarm-ui/src/WorkspaceSecrets.tsx`) |
| workspace | TypeSafe key | `swarm-tenant-<t>-typesafe` · `provider_keys` | `routes/provider_keys.py`, tenant scope: the workspace admin, else `require_admin` | the worker as the tenant GSA (`agent_worker/jevkey.py`); the forwarder once SEC-MIGRATE-TYPESAFE's switch is on | `settings/keys` (`ProviderKeys.tsx`) |
| workspace | runner LLM provider keys (`anthropic`, `openai`, any `known_providers()` name) | `swarm-tenant-<t>-<provider>` · the tenant document's `credentials` | `POST /v1/tenants/me/credentials` (`routes/tenants.py`, `put_credential`), `roles.ADMIN` behind the report-only `role_admin_required` switch | the worker, into the runner child's environment (`agent_worker/secrets.py::resolve_credentials`) | no form; listed read-only on the store page (`workspace_secrets.py::legacy_rows`, Q7 of workspace-secrets.md) |
| workspace | registered MCP server credential, `workspace_secret` mode | a store secret granted to `mcp:<name>`, slot `auth` | the store's manager | swarm-api, `POST /v1/attempts/{attempt_id}/mcp:attach` (`routes/attempts_mcp.py`) | the grant matrix; the server itself on `settings/mcp-servers` (`McpServers.tsx`) |
| workspace | MCP credential, `dispatch` mode (one run) | `swarm-tenant-<t>-mcpc-<16 hex>` with a Secret Manager TTL · `mcp_dispatch_credentials` | the submitter, in the submission's `mcp_credentials` field | swarm-api, `mcp:attach` | none: it lives as long as its agent |
| person | personal store secrets | `-wsec-` in the personal tenant, `scope: personal`, opted in per group by `use_in_workspaces` | the person (`require_manager`, the tenant's `principal`) | the release, under workspace-secrets.md Q3 | `settings/workspace/secrets` in the personal workspace |
| person | personal TypeSafe key | `swarm-tenant-u-<user>-typesafe` · `provider_keys` | the person, `PUT /v1/provider-keys/typesafe/user` | as the workspace key; the person's wins (`provider_keys.resolve_provider_key`) | `settings/keys` |
| person | saved browser logins, the sessions a browser agent asks for by its `browser_login` input (`browser_logins.BROWSER_LOGIN_INPUT_KEY`) | `swarm-tenant-u-<user>-browser-login-<name>` · `browser_logins` | the person, through Save as login after a live session (`POST /v1/tasks/{task_id}/browser-login`); the worker writes the jar | the worker of that personal tenant | `access?view=logins` (`BrowserLogins.tsx`) |
| person | GitHub App user token, owner fallback token | `swarm-tenant-<t>-git-u-<16 hex>` · `git_tokens` | onboarding and the 15-minute refresh sweep (`forgeapp.py`, `gittokens.py`) | the worker, the slot `Task.forge_credential` names; swarm-api | `repositories/tokens` (`GitTokens.tsx`); connecting on `access` |
| repository | QA target sign-in credential and challenge-bypass value | `swarm-tenant-<t>-qa`, one JSON map keyed by target (VQ-Q2) · `qa_targets/{target_id}` | the workspace admin (`routes/qatargets.py`, `require_workspace_admin`); the credential through a mandatory link, the bypass by a plain `PUT` | swarm-api, `POST /v1/attempts/{attempt_id}/qa-target`, to the live gate only | `repositories?page=qa-targets` (`QaTargets.tsx`) |
| repository | repository forge token | `swarm-tenant-<t>-git-r-<16 hex>` · `git_tokens` (`gittokens.Scope`) | the operator | the worker | `repositories/tokens` |
| repository | agent secrets for one repository | **none** | | | |
| repository | GitHub Actions secrets and environments | **none**: BGD sets them on GitHub's own settings page. No code in the repository calls `actions/secrets`, and PyNaCl is in no `uv.lock` (read 2026-10-23) | | | |

Two facts from this table shape everything below.

1. **A repository registration is already tenant-bound by construction.**
   `apps/swarm-api/swarm_api/repositories.py::repo_id_for` is `repo_` plus 16
   hex of sha256 over the tenant id, `github.com/` and the lower-cased
   `owner/repo`. The same GitHub repository registered in two workspaces has
   two ids. A secret bound to a `repo_id` therefore cannot cross a workspace
   even if nothing else checked, which is invariant 9 for free.
2. **The repositories an attempt clones are already in its signed spec.**
   `Task.repository_url` is in every spec format and `additional_repositories`
   since format 4 (`apps/common/swarm_common/specsign.py::canonical_step_spec`,
   contract request 96), and the release verifies the spec before it reads
   anything (`verdictpublish.spec_refusal`). No contract change is needed to
   bind a secret to a repository (§8).

---

## 2. The scope model for agent-consumed secrets

### 2.1 Three scopes, one record shape

The record of workspace-secrets.md §2.1 gains one value of `scope` and one
field. Nothing else in it changes.

| `scope` | lives in | `repo_id` | meaning |
|---|---|---|---|
| `workspace` | a group workspace | null | today's group secret |
| `personal` | a personal tenant `u-<user>` | null | today's personal secret, opted in to a group by `use_in_workspaces` (Q3 of workspace-secrets.md) |
| `repository` (new) | the workspace the repository is registered in, group or personal | the registration's `repo_id` | released only to attempts of that workspace that clone that repository |

* **The secret id is unchanged**: `swarm-tenant-<tenant>-wsec-<16 hex>`. So
  SEC-IAM's condition (`terraform/bootstrap/workspace_secrets.tf`) already
  covers a repository secret, `workspace_secrets.py::owned_by` already keeps
  it inside its workspace, and no IAM row is added.
* **The record also carries `repository`** (`owner/repo`, for display) and is
  refused unless `repositories/{repo_id}` exists, belongs to the caller's
  workspace and is not removed. A removed registration disables every grant
  on its secrets in the transaction that removes it, as a deleted secret does
  (workspace-secrets.md §2.2).
* **A name is unique within (workspace, repository)**, not within the
  workspace: `TEST_DB_PASSWORD` may exist once for the workspace and once per
  repository. The collision rule is §2.4.
* **A person's secret is not repository-scoped.** A personal secret already
  reaches only the person's own submissions; a "personal, for repository X"
  scope would be a third axis nobody asked for. A person who wants a value for
  one repository only sets it in their personal workspace's registration of
  that repository, where it is a repository secret of that personal
  workspace.

### 2.2 How a repository secret is granted

A repository secret is released only when **both** of these hold:

1. **a grant**: an enabled grant on it names one of the attempt's consumers
   (`workspace_secrets.py::attempt_consumers`: the profile, the tools its
   `browser_tool` launches, the components, `mcp:<name>`). This is today's
   per-consumer checkbox, with today's matrix;
2. **a clone**: the attempt's signed spec clones that repository. The
   repository set of an attempt is

       { repo_id_for(task.tenant_id, owner, repo) for the owner/repo of task.repository_url }
         ∪ { e.repo_id for e in task.additional_repositories
             where e.repo_id == repo_id_for(task.tenant_id, e.repository) }

   computed by swarm-api in the release, from the spec it has just verified.
   The recomputation of each `additional_repositories` entry's id is defence
   in depth: swarm-api writes that field, but a tenant's agent can write its
   own document, and the signature is what makes the field trustworthy, so a
   mismatch withholds every repository secret of that attempt and logs
   `repository_binding_mismatch`.

"Only attempts whose workflow clones that repository" falls out of this: a
workflow's steps share the workflow's `repository_url`, a step's
`additional_repositories` replace the workflow's (multi-repository-steps.md
§4.1), and a child agent copies its parent's signed `repository_url`
(user-scoped-secrets.md §3). An attempt that clones nothing (no
`repository_url`) is released no repository secret.

**The binding must be attested, because the record is not.** SEC-GRANT-ATTEST
left the record unattested ("What still is not stopped", workspace-secrets.md
§4): an agent that can write its workspace's Firestore could rewrite a
record's `repo_id` to a sibling repository and widen the secret. So the grant
carries the binding and the MAC covers it:

* `apps/swarm-api/swarm_api/grant_attest.py::canonical` gains form `"v2"`:
  `["v2", secret_id, tenant_id, consumer, slot or null, enabled, repo_id or null]`.
  `"v1"` stays verifiable for every grant on a workspace or personal secret,
  so no existing grant needs re-signing.
* A grant on a repository secret is written with `repo_id` copied from the
  record, and signed `"v2"`. The release requires `grant.repo_id ==
  record.repo_id` and that id in the attempt's repository set.
* **A repository grant is released only when attested, whatever
  `grant_attestation_required` says.** That switch exists for the migration
  of grants written before SEC-GRANT-ATTEST; no repository grant was ever
  written unsigned, so there is nothing to migrate and no reason to accept
  one. With no `SWARM_WORKSPACE_GRANT_KEY`, a repository grant cannot be
  enabled (422 `grant_key_required`, shipped off like every new refusal).

The residual SEC-GRANT-ATTEST records (an agent replaying a grant's own
earlier signed state) is unchanged for repository grants: a disabled grant
can be written back as it was while it is the same secret and the same
repository. The same workaround applies (rotate the grant key twice).

### 2.3 How it composes with today's grants

Nothing about a workspace or personal secret changes. `release_plan` keeps
its two passes (the workspace's own, then the person's opted-in ones) and
gains a filter on the first: a record of scope `repository` is due only when
§2.2's two conditions hold. The admission side
(`apps/scheduler/scheduler/secret_slots.py::missing_required_slots`) counts a
repository grant only for an agent whose spec names that repository, so a
required slot filled only by another repository's secret still parks
`CREDENTIAL_MISSING`. No catalogue slot is required today (request 80), so
this changes no admission yet; it is built so that the first required slot
does not admit wrongly.

### 2.4 One name in several scopes

A released secret becomes the file `private/secrets/<slot>/<name>`
(workspace-secrets.md §4.1), and the worker refuses an item that arrives
twice (`agent_worker/secretrelease.py`, "What SEC-RELEASE built"). So two
scopes offering one `(slot, name)` must be resolved **before** the release
leaves, in swarm-api:

**Recommended (Q1 a): the person's, then the repository's, then the
workspace's.** It is the order `provider_keys.resolve_provider_key` already
uses for the TypeSafe key (the person's wins over the tenant's), and the
reason is the same: a person who sets their own value under a shared name
meant to use it on their own agents. A repository's value is more specific to
the work than the workspace's, the order GitHub itself uses (an environment's
secret over a repository's over an organization's).

* Each shadowed secret is withheld with reason `shadowed_by_personal` or
  `shadowed_by_repository`, logged by name with the winning scope, and has no
  release row. The console shows "shadowed on agents of <person>" or "on
  <repository>" beside it, from the overview (§6.2), so a workspace admin can
  see why their value did not reach an agent.
* A personal secret shadows only on that person's own submissions, and only
  where it is opted in (`use_in_workspaces`). It never shadows another
  member's agents.

### 2.5 Who may set each scope

| scope | may set, rotate, delete and grant | may read names and grants |
|---|---|---|
| workspace | the workspace admin, or a platform admin (`require_manager`, unchanged) | every member |
| personal | the person, in their personal tenant; they alone opt it in to a group they belong to (unchanged) | the person |
| repository, for agents | **recommended (Q2 a): the workspace admin, or a platform admin**: the same `require_manager` | every member of that workspace |
| repository, GitHub Actions | **recommended (Q6 a): a manager of the workspace who is also a GitHub admin of that repository, checked with their own token at the moment of the write** (§3.6) | names and update times: every member who may read the repository |

No route takes a tenant or a person. A repository is named by `repo_id`, and
a `repo_id` outside the caller's workspace is 404, as another tenant's secret
is today.

---

## 3. GitHub Actions as a destination

SwarmCloud writes a value into GitHub and keeps nothing. The worked case is
the issue's: BGD opens Config › Secrets, picks SwarmCloud, opens its
`repo-secrets` environment and sets `MERGE_APP_PRIVATE_KEY` through the
one-time form.

### 3.1 GitHub's API, as this design uses it

Read 2026-10-23 from GitHub's REST reference (Actions secrets, Deployment
environments, Deployment branch policies, and "Permissions required for
GitHub Apps"). An installation token (IAT) may call each:

| call | path | App permission |
|---|---|---|
| a repository's public key | `GET /repos/{owner}/{repo}/actions/secrets/public-key` → `{key_id, key}` | Secrets: read |
| set a repository secret | `PUT /repos/{owner}/{repo}/actions/secrets/{name}` with `{encrypted_value, key_id}` → 201 created or 204 updated | Secrets: write |
| list repository secrets | `GET /repos/{owner}/{repo}/actions/secrets` → `name`, `created_at`, `updated_at` | Secrets: read |
| delete a repository secret | `DELETE /repos/{owner}/{repo}/actions/secrets/{name}` → 204 | Secrets: write |
| an environment's public key | `GET /repos/{owner}/{repo}/environments/{env}/secrets/public-key` | Environments: read |
| set, list, delete an environment secret | `PUT`, `GET`, `DELETE` under `/repos/{owner}/{repo}/environments/{env}/secrets` | Environments: write (read for the list) |
| list environments | `GET /repos/{owner}/{repo}/environments` | **Administration: read** |
| create or update an environment | `PUT /repos/{owner}/{repo}/environments/{env}` with `deployment_branch_policy: {protected_branches, custom_branch_policies}` | **Administration: write** |
| add a branch rule | `POST /repos/{owner}/{repo}/environments/{env}/deployment-branch-policies` with `{name, type: "branch"}` | **Administration: write** |
| delete an environment | `DELETE /repos/{owner}/{repo}/environments/{env}` | **Administration: write** |

The last four rows matter: **creating an environment is not covered by
`Environments`**. It needs `Administration`, which is §3.7's and Q4's
subject.

### 3.2 The seal

GitHub accepts only a libsodium **sealed box** (`crypto_box_seal`) of the
value under the repository's or the environment's Curve25519 public key,
base64-encoded, with the `key_id` it was sealed to. A sealed box is
encrypt-only: whoever sealed it cannot open it, and neither can anyone but
GitHub.

**Recommended (Q5 a): the browser seals.** The one-time form asks swarm-api
for the public key (`GET .../actions/public-key`, §6.3), seals the value in
the page with a libsodium build, and sends only `{key_id, encrypted_value}`.
The plaintext never reaches swarm-api, a log, a request body or a Claude
session; "SwarmCloud keeps nothing" is then true of the server and not only of
its storage. The page keeps WorkspaceSecrets.tsx's rules: uncontrolled
fields, read once, emptied before the request leaves.

* **What the server can still check:** the name, the destination, the
  `key_id` against the key GitHub serves now (a stale key is 409
  `actions_key_rotated`, and the page re-seals), and the ciphertext's length
  (GitHub's value limit is 48 KB; a sealed box is the value plus 48 bytes,
  base64). It cannot check that the value is not empty or not the name, so
  the page does both before sealing.
* **Q5 b, the server seals**, puts the value in one `PUT` body to swarm-api,
  which seals it with PyNaCl (`nacl.public.SealedBox`) and drops it, as every
  other store's write does today. It adds PyNaCl to swarm-api's lock; the
  standard `cryptography` package has no sealed box.

### 3.3 Listing

`GET /v1/repositories/{repo_id}/actions/secrets` answers the repository's
secrets and, for each environment, its secrets: **names, `created_at` and
`updated_at` only**, which is all GitHub serves. Beside each, from
SwarmCloud's own record of its writes (§3.8): whether SwarmCloud wrote it, who
and when. A secret set on GitHub's own page is listed as "set on GitHub". The
list is read live on each open, never cached, because GitHub is the source.

### 3.4 Creating a main-only environment

`POST /v1/repositories/{repo_id}/actions/environments` with `{name}` makes,
in this order, and refuses to offer a write into the environment until both
calls have answered:

1. `PUT .../environments/{name}` with `deployment_branch_policy:
   {protected_branches: false, custom_branch_policies: true}`;
2. `POST .../environments/{name}/deployment-branch-policies` with
   `{name: "main", type: "branch"}`, where `main` is the registration's
   default branch read from GitHub, never a field of the request.

An existing environment is read before anything is written into it: its
branch rule is shown ("main only", "protected branches", or "any branch").
**Writing into an environment that any branch can deploy asks a typed
confirmation of the environment's name**, because a secret there reaches the
workflows of every branch anyone with push access creates. An environment
that already exists is never modified by this route: SwarmCloud creates
environments, it does not loosen or tighten someone else's.

### 3.5 Which credential writes

**Recommended (Q3 a): a second GitHub App, "SwarmCloud Actions secrets",
used only through installation tokens minted by swarm-api.**

* **Its permissions, exactly** (repository permissions; organization and
  account permissions none; no webhook; "Request user authorization (OAuth)
  during installation" off, and no callback URL, so **no user token for it can
  ever exist**):

  | permission | level | why |
  |---|---|---|
  | Metadata | read | mandatory; GitHub sets it |
  | Secrets | read and write | repository secrets: public key, list, set, delete |
  | Environments | read and write | environment secrets: public key, list, set, delete |
  | Administration | **write** under Q4 a, **read** under Q4 b | list environments and read their branch rule (read); create an environment and its branch rule (write) |

* **The key** is `swarm-github-secrets-app-private-key`, declared by
  Terraform with no version, its value written by `scripts/create-secrets.sh
  --github-secrets-app private-key --stdin`, readable by swarm-api's service
  account alone. No tenant worker, no forwarder and no pod is granted it. The
  App id is a setting (`GITHUB_SECRETS_APP_ID`), as `GITHUB_APP_ID` is.
* **The mint** is `forge_installation.py`'s shape: `POST
  /app/installations/{id}/access_tokens` with `repositories: [repo]` and
  **only the one permission the call needs** (`{"secrets": "write"}` for a
  repository write, `{"environments": "read"}` for an environment's key, and
  so on). The token is minted per request, used, and dropped, as
  `workflow_push_token` is; never cached, never written to Firestore, never
  logged, and registered with redaction for the request's life. A repository
  the App is not installed on is 409 `actions_app_not_installed`, with the
  install link.
* **Why not the existing SwarmCloud App (Q3 b).** That App acts through
  **user access tokens** (D1, `docs/runbooks/github-app.md`), and each
  person's token sits in a `-git-u-` slot the tenant worker account can read
  under `terraform/bootstrap/forge_user_slots.tf` until user-scoped-secrets.md
  rollout step 5 removes that grant. An agent that mints its GSA's token can
  read those slots (workspace-secrets.md §3.1). A user token carries the App's
  permissions intersected with the person's, so if that App held `Secrets:
  write`, a leaked token of any repository admin could overwrite that
  repository's Actions secrets. A second App with no user authorization has
  no token that is ever stored, and none in any pod.
* **Why not a person's PAT pasted into a form (Q3 c).** It is a forge token
  in SwarmCloud's hands, which the owner rule of 2026-09-25 keeps in Secret
  Manager under `-git` names for the worker, not for this. And it would be
  the person's whole reach, not one repository's one permission.

### 3.6 Who may cause a write

The writer is the App, not the person, so SwarmCloud must not let anyone use
the App to do what they could not do on GitHub. **Recommended (Q6 a):**

* the caller manages the workspace (`require_manager`), **and**
* GitHub says the caller is an admin of that repository, read with **their
  own** connected token at the moment of the write
  (`forge.py::RepositoryRead`, `can_admin` from `permissions.admin`, which is
  reported today and enforced nowhere). GitHub itself requires admin access
  to set an organization repository's secrets, so this keeps SwarmCloud
  exactly as strict as GitHub. A caller with no connected token, or whose
  token says no, is 403 `github_admin_required` (shipped off as every new
  refusal: while off, logged and served as `forbidden`).

Listing names needs only the workspace's read and the registration.

### 3.7 What a leaked token could do

Plainly, because Q3 and Q4 are decisions about this.

* **Secrets: write, Environments: write.** It cannot read any secret; GitHub
  serves no read. It can **overwrite** one: replace a value a privileged
  workflow trusts. On SwarmCloud that means `MERGE_APP_PRIVATE_KEY` (the merge
  App's key, `auto-merge.yml`) or anything in `repo-secrets`. Overwriting a
  signing key, a registry credential or a deploy target makes main's own
  workflow sign, push or send to the attacker's choice; overwriting the merge
  App's key makes auto-merge act as the attacker's App. It can also delete
  every secret, which stops releases. One minted token reaches one repository
  for at most an hour; the App's private key reaches every repository the
  App is installed on.
* **Administration: write** (Q4 a) is more. It can change branch protection
  and rulesets, add a deploy key with write access, and loosen an
  environment's branch rule. A write deploy key is a push credential, so a
  leaked Administration-write token is close to control of the repository.
  The existing SwarmCloud App's key already reaches `Contents: write` and
  `Workflows: write` on every repository it is installed on
  (`docs/runbooks/github-app.md`, Step 3), so whoever holds swarm-api's
  secrets can already push a workflow. **What Administration adds is getting
  past branch protection on main**, which the existing App's push cannot do
  where main is protected.
* **Where the risk lives.** Only swarm-api reads the App's key, and only
  swarm-api mints tokens. A compromised agent reads nothing of it (no grant,
  no file, no environment). A compromised swarm-api already holds
  `secrets.setIamPolicy` over every `swarm-tenant-*` secret
  (workspace-secrets.md §3.3), so the App adds reach on GitHub, not in Google
  Cloud.

### 3.8 What SwarmCloud keeps

No value and no ciphertext. One record per write, `actions_secret_writes/{id}`:
`repo_id`, `tenant_id`, `environment` (or null), `name`, `set_by`, `set_at`,
`github_status` (201 or 204), and `state` (`set` or `deleted`). One
`admin_audit` row in the same transaction: `actions_secret_set`,
`actions_secret_deleted`, `actions_environment_created`. A delete is a
`DELETE` to GitHub plus `state: deleted`, behind a typed confirmation of the
secret's name, as the store's delete is. An environment delete is offered only
for environments SwarmCloud created (its audit row says so) and only under Q4
a, with the environment's name typed.

### 3.9 Names

GitHub's rules, refused before any call: letters, digits and `_`; not
starting with a digit; not starting with `GITHUB_`; compared without case,
because GitHub compares them so. An environment name is refused if it carries
a `/` or a control character. The name is served, logged and audited, so a
name that is part of its own value is refused in the page (the store's rule,
"What SEC-STORE-API built").

---

## 4. Consolidation: what moves onto Secrets, what stays, what migrates

**No store migrates.** Config › Secrets is a view over the stores of §1. Each
row keeps its collection, its Secret Manager name, its IAM and its writer;
the page reads one overview route (§6.2) and, to set a value, opens that
store's own one-time form.

| store | on Config › Secrets | stays where it is, with a link | migrates |
|---|---|---|---|
| workspace store (`workspace_secrets`) | **moves**: the page *is* Config › Secrets' workspace and personal scopes, the grant matrix included. `settings/workspace/secrets` and its `?action=` links redirect, token and all | nothing | nothing |
| repository secrets for agents | new, in the repository scope (§2) | | nothing: new records |
| TypeSafe keys, workspace and personal | listed under Keys for agents and MCP, with Set and Delete opening the provider-key form; the overview reads `provider_keys` directly, because `legacy_rows` reads the tenant's `credentials` and the TypeSafe routes never write there | `settings/keys` redirects to it | nothing. workspace-secrets.md §6.1's index job stays SEC-MIGRATE-TYPESAFE's and is not needed for the view |
| runner LLM provider keys | listed under Keys for agents and MCP, with a **Set** that opens a new one-time form for `POST /v1/tenants/me/credentials` (a link minted by a new `POST /v1/tenants/me/credentials/links`, the provider-key link's shape) | | nothing: same secret, same writer, same reader (Q7 of workspace-secrets.md: listed, not moved) |
| pool accounts | one line, "subscription accounts", with a link | Capacity › Accounts | nothing |
| MCP credentials, `workspace_secret` mode | already store secrets granted to `mcp:<name>`: they are rows of the workspace scope, grouped under Keys for agents and MCP by that consumer | the server's registration on Config › MCP servers | nothing |
| MCP credentials, `dispatch` mode | not listed: it lives as long as its agent and is set in the submission | | nothing |
| saved browser logins | the personal scope's "Browser sessions": name, saved at, expires, Delete (`DELETE /v1/browser-logins/{name}`) | **saving** stays in the live browser (`SaveAsLoginOffer`, `LiveBrowser.tsx`), because a login is a session the worker exports, never a typed value; `access?view=logins` redirects | nothing |
| QA target credential and challenge-bypass | the repository scope's "Sign-in for QA": each QA target of that repository, credential set/at/by and bypass set/at/by, with Set opening the QA credential form | the target's registration on Repositories › QA targets | nothing. The bypass gains the credential's link flow (§6.3): today its `PUT` takes a value with no link, which BGD's rule does not allow |
| GitHub tokens | status only, in each scope: the tenant `-git` token (workspace), the person's `-git-u-` slots (personal), a `-git-r-` token (repository). Kind, state, verified at, expires at, GitHub login. **Never `last4`**, though `git_tokens` stores it: this page shows no characters of any value | connecting: Config › GitHub access; registering and verifying a token: the Git tokens page | nothing |
| QA identity grants | not on Config | Admin (`admin/tenants`, `QaIdentityGrants.tsx`): a platform admin's | nothing |
| platform secrets | not on Config | Admin › Platform secrets (§5) | nothing |

---

## 5. Admin › Platform secrets

A tab of Admin, `admin/platform-secrets`, marked `admin: true` in `SECTIONS`.
**Status only.** No value, no length, no last characters, and no form: every
platform secret is written by the operator's script or its owning system, and
this page says which.

### 5.1 What it lists

A catalogue in swarm-api, `platform_secrets.PLATFORM_SECRETS`, one entry per
secret id, each with who manages it and what reads it. Read 2026-10-23 from
`terraform/` and `scripts/create-secrets.sh`:

| secret | managed by | value written by |
|---|---|---|
| `swarm-github-app-private-key`, `swarm-github-app-client-secret` | Terraform declares | `create-secrets.sh --github-app <slot>` |
| `swarm-github-app-webhook-secret` | Terraform declares | `create-secrets.sh --github-app-webhook-secret` |
| `swarm-github-secrets-app-private-key` (new, §3.5) | Terraform declares | `create-secrets.sh --github-secrets-app private-key` |
| `swarm-search-api-key` | Terraform declares | `create-secrets.sh --search-api-key` |
| `swarm-egress-credential`, `swarm-egress-key` | Terraform declares | `create-secrets.sh --egress-credential`, `--egress-key` |
| `swarm-live-key`, `swarm-child-key`, `swarm-graph-ticket-key`, `swarm-settings-key` | Terraform declares | `create-secrets.sh --live-key`, `--child-key`, `--graph-ticket-key`, `--settings-mac-key` |
| `swarm-typesafe-fwd-key`, `swarm-workspace-grant-key` | Terraform declares | `create-secrets.sh --typesafe-fwd-key`, `--workspace-grant-key` |
| `swarm-tailscale-oauth` | Terraform declares | `create-secrets.sh --tailscale-oauth` |
| `swarm-account-<t>--<label>[-refresh]` | the quota broker | the broker; one row per account, which Capacity › Accounts links to |

Per row: **set** (an enabled version exists), **age** (the newest enabled
version's `create_time`), the number of enabled versions (two means a
rotation is in its grace), and the manager. A catalogue name Secret Manager
does not have is "not declared", which is how a deployment without
`enable_typesafe_forwarder` reads.

Platform **settings** already on Admin stay there (Pool limits, Runner sizes,
`GET`/`PUT /v1/admin/settings`). Nothing platform-level is on Config.

### 5.2 How it reads, and the IAM it needs

`GET /v1/admin/platform-secrets` calls `secrets.get` and `versions.list` for
each catalogue name: metadata only, never `versions.access`. It needs one
custom role, `swarmSecretStatusViewer`, holding exactly
`secretmanager.secrets.get` and `secretmanager.versions.list`, granted to
swarm-api in `terraform/bootstrap` under a condition that admits names
starting `projects/<NUMBER>/secrets/swarm-` (the project number, as SEC-IAM's
condition). **Recommended (Q7 a).** It reads tenant secrets' metadata too,
which is harmless (a name and dates) and which swarm-api's
`secrets.setIamPolicy` already exceeds. Not verified live: it must be linted
with `iamPolicies:lintPolicy` before the owner applies it, as every condition
here has been.

### 5.3 The admin-only rule, on every surface

| surface | rule |
|---|---|
| console | the tab is `admin: true` and is not rendered for a caller whose `/v1/me` says not admin. #2157 makes Admin visible only to admins; this tab adds nothing a workspace admin sees |
| API | every `/v1/admin/*` route is `auth.require_admin` (unchanged). `GET /v1/admin/platform-secrets` is no exception, and no route writes a platform secret |
| MCP | one read-only tool, `swarm_platform_secrets`, whose answer is the route's; a non-admin gets the route's 403, and the tool's description says so. No MCP tool writes, rotates or mints a link for a platform secret |
| plugin | `/sc admin secrets`, the same read. The value of a platform secret is set only by `scripts/create-secrets.sh --stdin` on the operator's machine, which no Claude session runs with a value |

The rule is enforced by swarm-api. The console and the bridge only hide or
describe it, so a surface that forgot to hide it would still be refused.

---

## 6. Surfaces

### 6.1 The console page

`config/secrets`, under #2157's Config section (`/config/secrets`). The
address takes `?scope=workspace|personal|repository&repo=<repo_id>&dest=agents|actions`.

* **Scope picker**, at the top: **Workspace** (the selected workspace's
  name), **Yours** (the caller's personal secrets, and for a group workspace
  which of them are opted in to it), **Repository** (a picker of the
  workspace's registered repositories, from `GET /v1/repositories`).
* **Workspace** and **Yours** show, in this order: *For agents* (the store
  list and the grant matrix, today's page), *Keys for agents and MCP*
  (TypeSafe, runner LLM keys, MCP servers' stored credentials), *Browser
  sessions* (Yours only), *GitHub* (token status).
* **Repository** has a **destination** switch:
  * **Agents**: the repository's secrets with their own matrix, a line
    "released only to agents that clone <owner/repo>", *Sign-in for QA* (its
    QA targets), and *GitHub* (a `-git-r-` token's status).
  * **GitHub Actions**: *Repository secrets* (names, updated, set by
    SwarmCloud or on GitHub), *Environments* (each with its branch rule and
    its secrets), **Set a secret** (the sealing form of §3.2), **Delete**
    (typed name), **Create environment** (name; the branch is the
    repository's default, shown, not editable). When the App is not
    installed on the repository, the pane says so and links to the install.
* **Every Set opens a one-time form**: either the link a tool minted, or a
  link the page mints for itself on click. A form drops its token from the
  address before paint (`WorkspaceSecrets.tsx`'s rule) and is never prefilled
  with a value.
* **Who may** comes from the overview's `can_manage` per row; the API is the
  gate.

### 6.2 API routes

All under swarm-api, scoped by `tenant_scope`, bodies forbidding unnamed
fields. No route serves a value. No route takes a tenant, a person, an image
or a command.

| route | what |
|---|---|
| `GET /v1/secrets/overview?scope=..&repo_id=..` | (new) one read of every store in that scope: names, status, set by and at, the consumers granted, shadowing (§2.4), `can_manage` per row. Never a value, a length or characters |
| `GET`, `POST /v1/repositories/{repo_id}/secrets` | (new) list and create repository secrets for agents: the store's shapes, `scope: repository` implied by the path |
| `PUT .../{repo_id}/secrets/{name}/value`, `DELETE .../{name}`, `PUT .../{name}/grants/{consumer}`, `POST .../{repo_id}/secrets/links` | (new) rotate, delete (typed), grant (signed `"v2"`), one-time link |
| `GET /v1/repositories/{repo_id}/actions/secrets` | (new) names and times, repository and environments (§3.3) |
| `GET /v1/repositories/{repo_id}/actions/public-key?environment=..` | (new) `{key_id, key}` for the page to seal to (Q5 a only) |
| `PUT /v1/repositories/{repo_id}/actions/secrets/{name}` | (new) `{environment, key_id, encrypted_value, link_token}` (Q5 a) or `{environment, value, link_token}` (Q5 b) |
| `DELETE /v1/repositories/{repo_id}/actions/secrets/{name}` | (new) `{environment, confirm_name}` |
| `POST /v1/repositories/{repo_id}/actions/environments` | (new) `{name}`: create, main-only (§3.4) |
| `DELETE /v1/repositories/{repo_id}/actions/environments/{name}` | (new, Q4 a only) `{confirm_name}`; only an environment SwarmCloud created |
| `POST /v1/repositories/{repo_id}/actions/links` | (new) `{environment, name}`: a one-time link to the Set form |
| `POST /v1/tenants/me/credentials/links` | (new) a one-time link to the runner-key form |
| `POST /v1/qa-targets/{id}/credential-links` | gains `{field: "credential" \| "challenge_bypass"}`; the bypass `PUT` then requires a link token as the credential's does (the refusal ships off) |
| `GET /v1/admin/platform-secrets` | (new) §5, `require_admin` |
| unchanged | `/v1/workspace/secrets/*`, `/v1/provider-keys/*`, `/v1/browser-logins`, `/v1/qa-targets/*`, `/v1/git-tokens`, `/v1/mcp-servers`, and the worker-only `secrets:release`, `mcp:attach`, `qa-target` |

`test_no_route_serves_credentials.py`'s carve-out list does not grow: the
Actions routes take a value in and serve none, and the public key is
GitHub's public key.

### 6.3 The one-time link

Every link follows the provider-key and store links: 32 random bytes
(`secrets.token_urlsafe(32)`), its sha256 as the document id, minted by and
only for the caller, bound to the caller's email, the workspace, and the
exact destination (scope, `repo_id`, environment, name), **ten minutes, one
use**, consumed before the write. A link grants nothing: the write is still
authorized as §2.5 and §3.6 say, at the moment of the write.

### 6.4 MCP and plugin tools: links and reads only

| tool | answer |
|---|---|
| `swarm_secret_set_link(scope, name?, repository?, action)` (new) | `{url, expires_at}` for the store's add or rotate form, in the workspace, personal or repository scope |
| `swarm_actions_secret_link(repository, name, environment?)` (new) | `{url, expires_at}` for the GitHub Actions Set form |
| `swarm_secrets(scope, repository?)` (new) | the overview's rows: names and status, never a value |
| `swarm_key_set_link`, `swarm_qa_target_credential_link` | unchanged; the latter gains `field` |
| `swarm_platform_secrets` (new) | §5.3, admin only |

The plugin gains `/sc secrets link <scope> [name] [--repo owner/repo]
[--actions [--env name]]` and `/sc secrets list`. **No tool's input schema
has a value field**, and the tools' tests assert it the way `keys.py`'s do:
a value in a Claude session is exactly what BGD ruled out. A tool answers
`url: null` when the deployment has no console origin, as `keys.key_set_link`
does, rather than a bare path that carries a token.

---

## 7. Invariants

* **1:** nothing here creates infrastructure demand. A repository secret
  changes what is released, not when an agent leases.
* **5:** the release keeps its two fences (STARTING or RUNNING at this
  generation, before the read and again in the transaction); a stale worker
  is released nothing, repository secrets included.
* **9:** a repository secret is a `-wsec-` secret of the workspace whose
  `repo_id` it carries, and that id is derived from the tenant. The Actions
  App's tokens are minted per repository, and only for a repository
  registered in the caller's workspace and administered by the caller.
* **10:** callers name a repository by its registration, a secret by its
  name, a consumer from the catalogue and an environment by GitHub's name. No
  image, command, resource or backend parameter is accepted.
* Redaction: the worker registers every released value before the agent
  exists, unchanged. The Actions routes register nothing because, under Q5 a, they
  never hold one.

---

## 8. Contract change requests (requests, not changes)

**None is needed under the recommendations.** `Tenant.secret_name` already
spells `wsec-<hex>`; `SECRET_CONSUMER_KINDS` (`profile`, `tool`, `component`,
`mcp`) is unchanged because a repository is an attribute of the record, not a
consumer; `Task.repository_url` and `additional_repositories` are already
signed. The grant attestation form (`grant_attest.py`) and the record are
swarm-api's, not the contract's.

Under **Q9 b** the binding would be a consumer instead, and that is a
frozen-contract change, drafted here in
[contract-change-requests.md](../contract-change-requests.md)'s form so the
owner can accept it by number; it is not filed and nothing edits the
contract.

> **114. `models.py`: a `repo` secret-consumer kind (SECRETS-SCOPES-DESIGN, part of #2158)**
>
> **Status:** not filed. Requested only if the owner answers Q9 b.
>
> **What is true today.** `apps/common/swarm_common/models.py::SECRET_CONSUMER_KINDS`
> is `("profile", "tool", "component", "mcp")`, and `consumer_name` refuses
> any other kind. No consumer names a repository.
>
> **The requested change.** Add `"repo"`, spelled `repo:<repo_id>`, where
> `<repo_id>` matches `repositories._REPO_ID` (`repo_` and 16 hex), and have
> `parse_consumer` accept it. swarm-api's `attempt_consumers` would add
> `repo:<id>` for each repository in the attempt's set (§2.2).
>
> **What it would break if accepted.** A grant to `repo:<id>` alone would
> release a secret to every profile and tool of every attempt cloning that
> repository, unless each such secret also had a profile grant, which makes
> "both conditions" an intersection of two grants the console must render as
> one. The release's consumer set and admission's
> (`scheduler/secret_slots.py`) must both derive it, from a scheduler image
> that does not carry swarm-api.
>
> **If it is declined.** The binding stays on the record and the grant
> (§2.2), with no contract change.

---

## 9. Build plan

Each lane owns the files named; **two lanes that touch one file are one
lane, or run one after the other**, never in parallel (CLAUDE.md, "Issues").
Every lane pushes its tests first to the PR branch and its fix to
`<branch>-fix`, and runs `scripts/changed-guards.sh` before finishing. Every
new refusal ships off (`test_new_refusals_ship_off.py`). The lanes waited
for BGD's answers, which are §11; each row says whether it is built, and §12
what it built.

| phase | lane | territory | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **SSC-SECRETS-APP** (not built: Q3 declined the second App; BGD's App permission step is in `docs/runbooks/github-app.md`) | `docs/runbooks/github-secrets-app.md` (new), `terraform/infra/github_secrets_app.tf` (new), `tests/terraform/github_secrets_app.tftest.hcl` (new), `scripts/create-secrets.sh` (the `--github-secrets-app` slot) and `tests/unit/scripts/test_create_secrets_github_secrets_app.py` (new) | the secret is declared with no version and `managed-by=swarm-terraform`; only swarm-api's account reads it; no tenant worker or KSA binding names it; the slot refuses a value on argv and reads `--stdin` only |
| 1 | **SSC-REPO-SCOPE** (built, PR 2225) | `apps/swarm-api/swarm_api/workspace_secrets.py` (records, `release_plan`, shadowing), `grant_attest.py` (form `"v2"`), `routes/workspace_secrets.py`, `routes/repository_secrets.py` (new), `routes/attempts_secrets.py`; `tests/unit/control_plane/test_workspace_secrets.py`, `test_grant_attest.py`, `test_secret_release.py` | a repository secret is not released to an attempt whose signed `repository_url` is another repository; it is released through a matching `additional_repositories` entry and withheld when that entry's `repo_id` is not `repo_id_for` its repository; a record whose `repo_id` was rewritten is dropped; an unattested repository grant is dropped with `grant_attestation_required` off; a `"v1"` grant still verifies; Q1's order resolves one `(slot, name)` to one item and withholds the rest with their reason; another workspace's `repo_id` is 404; a member is 403; removing a registration disables its secrets' grants |
| 1 | **SSC-ADMIT-REPO** (built, PR 2238) | `apps/scheduler/scheduler/secret_slots.py`, `apps/scheduler/scheduler/credentials.py`, `tests/unit/control_plane/test_secret_slot_admission.py` | a required slot filled only by another repository's secret parks `CREDENTIAL_MISSING`; filled by this repository's, it admits |
| 1 | **SSC-ACTIONS-API** (built, PR 2240; under Q3 it waited for no App, and mints from the existing one) | `apps/swarm-api/swarm_api/actions_secrets.py` (new), `secrets_app.py` (new: the second App's JWT and mint), `routes/actions_secrets.py` (new), the `GITHUB_SECRETS_APP_ID` setting; under Q5 b also `apps/swarm-api/pyproject.toml` and `uv.lock` (PyNaCl); `tests/unit/control_plane/test_actions_secrets.py` (new) | each mint names exactly one repository and one permission; no response, log line or Firestore document carries a value, a ciphertext or a token (runtime-built fakes); a caller GitHub does not call admin is 403; an environment is created with custom branch policies and gets its `main` rule before any write is offered; an existing environment is never modified; a `GITHUB_`-prefixed or digit-led name is 422; a stale `key_id` is 409; a link is consumed once; no App configured is 503 |
| 1 | **SSC-OVERVIEW** (built, PR 2248) | `apps/swarm-api/swarm_api/secrets_overview.py` (new), `routes/secrets_overview.py` (new), `tests/unit/control_plane/test_secrets_overview.py` (new) | every §4 store is present in its scope; no row carries a value, a length or `last4`; personal rows are only the caller's; TypeSafe rows are read from `provider_keys`; shadowing is reported |
| 1 | **SSC-LINKS** (built, PR 2219) | `apps/swarm-api/swarm_api/qatargets.py`, `routes/qatargets.py`, `routes/tenants.py` (the runner-key link), their tests | the bypass `PUT` without a link token is refused once the switch is on and logged while off; a runner-key link is single use and bound to the caller and the provider |
| 1 | **SSC-PLATFORM-STATUS** (built, PR 2218; PR 2221 fixed its 500) | `apps/swarm-api/swarm_api/platform_secrets.py` (new), its route in `routes/admin.py`, `terraform/bootstrap/platform_secret_status.tf` (new), `tests/terraform/platform_secret_status.tftest.hcl` (new), `tests/unit/control_plane/test_platform_secrets.py` (new) | a non-admin is 403; the reader never calls `versions.access`; the role holds exactly `secrets.get` and `versions.list`; the condition refuses a name outside `swarm-` and another project |
| 2 | **SSC-UI** (built, lane SSC-UI, 2026-10-25; §12) | `apps/swarm-ui/src/Secrets.tsx` (new, absorbing `WorkspaceSecrets.tsx`), `AdminPlatformSecrets.tsx` (new), `App.tsx`, `paths.ts`, `api.ts`, `types.ts`, `.github/ISSUE_TEMPLATE/*` (the Where list, held by `test_issue_forms.py`), vitest files | the old addresses redirect with their link tokens; no value is rendered after save; the Platform secrets tab is absent for a non-admin; the repository scope lists only registered repositories; a shadowed row shows its winner |
| 3 | **SSC-UI-ACTIONS** (not built: after SSC-UI, same `Secrets.tsx` and `api.ts`) | `apps/swarm-ui/src/SecretsActions.tsx` (new), the import line in `Secrets.tsx`, `api.ts`; under Q5 a, `apps/swarm-ui/package.json` and its lock (libsodium); vitest files | the request body carries `encrypted_value` and never the value; the field is empty before the request leaves; writing into an any-branch environment needs the typed name; the install link appears when the App is not installed |
| 2 | **SSC-SC** (built, PR 2251) | `apps/swarm-mcp/swarm_mcp/secrets.py` (new), `apps/swarm-mcp/swarm_mcp/server.py`, `apps/swarm-mcp/swarm_mcp/qatargets.py` (its `field`), `plugin/skills/sc/SKILL.md`, `tests/unit/mcp/test_secret_links.py` (new) | no tool's schema accepts a value field; each link tool returns a URL and its expiry and nothing else; `url` is null without a console origin; `swarm_platform_secrets` passes the route's 403 through |
| each | **SSC-DOCS** (built: this status, §12, `docs/security.md` and `docs/provider-keys.md`) | this file, `docs/design/workspace-secrets.md` (status line), `docs/security.md`, `docs/provider-keys.md` | `test_docs_describe_what_was_built` |

The review CLAUDE.md requires for credentials, isolation, redaction and IAM
applies to SSC-SECRETS-APP, SSC-REPO-SCOPE, SSC-ACTIONS-API and
SSC-PLATFORM-STATUS.

**The done check is the issue's:** BGD opens Config › Secrets, picks
SwarmCloud, chooses GitHub Actions, creates `repo-secrets` (main only), and
sets `MERGE_APP_PRIVATE_KEY` there through the one-time form; GitHub's list
shows it updated; SwarmCloud's Firestore holds its name and nothing else; and
`/sc secrets link repository MERGE_APP_PRIVATE_KEY --repo
bogdan-alexandrescu/SwarmCloud --actions --env repo-secrets` returns the same
form as a link. `auto-merge.yml` reads `MERGE_APP_PRIVATE_KEY` at repository
level today and names no environment, so moving it into `repo-secrets` is a
workflow change the owner pushes by hand.

---

## 10. Owner questions

Each is also in this lane's `questions.json`. The recommendation is first.

**Q1. When a person's, a repository's and a workspace's secret share a name,
which reaches the agent?**
(a) **the person's, then the repository's, then the workspace's**
(recommended: the TypeSafe key's existing order, and GitHub's most-specific
rule for the other two; cost: a workspace admin's value can be overridden on
one person's own agents, which the console shows);
(b) the repository's, then the person's, then the workspace's (cost: a person
cannot override a repository's shared value on their own agents);
(c) refuse a name already used in another scope the same agent could see
(cost: whether two names collide depends on who submits, so a write must ask
about every member's personal secrets, and a refusal names a secret the
writer may not see).

**Q2. Who may set a repository's secrets for agents?**
(a) **the workspace admin, or a platform admin** (recommended: the store's
rule, no new role; cost: a member who maintains one repository asks an admin);
(b) also a member whom GitHub calls admin of that repository (cost: a second
authority to check on every write, and a member can then grant a value to
every profile in the workspace);
(c) any member (cost: any member can put a value into every agent that
clones that repository).

**Q3. Which credential writes GitHub Actions secrets?**
(a) **a second GitHub App with Secrets, Environments and Administration
(§3.5), installation tokens only, its key in swarm-api alone** (recommended:
no token for it is ever stored or in a pod; cost: one more App to register,
install on each repository and rotate);
(b) add those permissions to the existing SwarmCloud App (cost: every
person's user token, which tenant workers can read until user-scoped-secrets
step 5, gains the power to overwrite Actions secrets on repositories that
person administers, and every installation's owner must accept the change);
(c) none: SwarmCloud lists names only and links to GitHub's settings page
(cost: the issue's done check is not met).

**Q4. Should SwarmCloud create environments, which needs Administration:
write?**
(a) **yes: the second App holds Administration: write, minted only for the
environment calls, one repository at a time** (recommended: it is what BGD
asked for; cost: a leaked token or a stolen App key can change branch
protection and add deploy keys, close to control of the repository, §3.7);
(b) no: the App holds Administration: read; the person creates the
environment on GitHub following the console's steps, and SwarmCloud verifies
its branch rule before writing into it (cost: one manual step per
environment, and no environment delete);
(c) no environments at all, repository secrets only (cost: `repo-secrets`
cannot be used, and a secret reaches every branch's workflows).

**Q5. Where is the value sealed to GitHub's key?**
(a) **in the browser; swarm-api receives only the sealed box** (recommended:
the value never reaches SwarmCloud's servers; cost: a libsodium build in the
console bundle, and the empty-value and name checks run only in the page);
(b) in swarm-api, with PyNaCl, dropped after the PUT (cost: the value passes
through swarm-api's memory, as every other store's does, and a new Python
dependency).

**Q6. Who may cause a write to a repository's GitHub Actions secrets?**
(a) **a manager of the workspace whom GitHub calls admin of that repository,
checked with their own token at the write** (recommended: SwarmCloud is never
a way round GitHub's own rule; cost: a connected GitHub token is needed to
write);
(b) any manager of the workspace (cost: a workspace admin without GitHub
admin on the repository can overwrite its secrets through the App);
(c) platform admins only (cost: BGD alone, in practice, for every
repository).

**Q7. Where does Admin › Platform secrets read its status from?**
(a) **Secret Manager metadata, under a new metadata-only role for swarm-api
(`secrets.get`, `versions.list`)** (recommended: live and exact; cost: an
owner-applied bootstrap grant that reads every `swarm-` secret's metadata);
(b) the release's Terraform outputs and `create-secrets.sh` runs, recorded at
deploy time (cost: stale between deploys, and a version added by hand is
never seen);
(c) the catalogue's names only, no status (cost: the page cannot say set or
not, which is what it is for).

**Q8. Must every value write come through a one-time link, or is the
terminal's `--stdin` still allowed?**
(a) **the console form (reached by its link) and the terminal's `--stdin`,
which no Claude session types into, stay; no tool takes a value**
(recommended: today's `sc key set --stdin` and `create-secrets.sh --stdin`
keep working; cost: an API caller with a session can still `PUT` a value
directly, as today);
(b) every value write requires a link token, the challenge bypass and the
provider keys included (cost: scripts that set keys from a terminal break
until they mint a link first).

**Q9. Is the repository binding a field of the record, or a consumer?**
(a) **a field of the record and of its attested grant (§2.2), with no
contract change** (recommended: one matrix, and a repository secret still
needs a consumer ticked; cost: the release and admission each gain a
repository filter);
(b) a `repo:<repo_id>` consumer kind (cost: frozen-contract request 114,
§8, and two grants per secret that the console must show as one).

---

## 11. Owner decisions (2026-10-23, ~14:57-15:17 PDT / 21:57-22:17Z)

BGD answered §10. Each answer is recorded on the design agent (`task_dffdc81d0b8944d8a499`, `owner_answers`). Where BGD's answer differs from the recommendation, the build follows BGD:

| question | BGD's answer |
|---|---|
| Q1, one name in several scopes | **person, then repository, then workspace** (recommended) |
| Q2, who sets a repository's secrets for agents | **workspace admin or platform admin, and also a member whom GitHub calls admin of that repository** (not the recommendation). The write checks GitHub's admin rule live with the person's own token. |
| Q3, which credential writes Actions secrets | **the existing SwarmCloud App gains the permissions** (not the recommendation). §3.5's second App is not built. |
| Q4, environments | **Administration: write only after user-scoped-secrets step 5.** Until step 5 removes workers' read of the `-git-u-` user-token slots, the existing App gains **Secrets and Environments read/write only**. A person creates each environment on GitHub, following the console's steps, and SwarmCloud checks its branch rule before writing into it. After step 5, the App gains Administration: write and the console's **Create environment** turns on. CLD raised the combination with BGD: Administration: write on the existing App would reach every person's user token while workers can still read them. |
| Q5, where the value is sealed | **in swarm-api, with PyNaCl** (not the recommendation). The value passes through swarm-api's memory and is dropped after the PUT. |
| Q6, who may cause an Actions write | **a workspace manager whom GitHub calls admin of the repository**, checked live (recommended) |
| Q7, Admin › Platform secrets status source | **Secret Manager metadata, under a new metadata-only role** (recommended). The bootstrap grant holds at `dev-iam` for BGD. |
| Q8, value entry | **console form by its link, plus terminal `--stdin`; no tool takes a value** (recommended) |
| Q9, repository binding | **a field of the record and of its attested grant (grant form v2)**; no contract change (recommended) |

**Residual risk BGD accepted (Q3, until user-scoped-secrets step 5).** A repository admin's App user token sits in a `-git-u-` slot that tenant workers can read. With Secrets: write on the App, such a token can overwrite that repository's Actions secrets. Step 5 closes this; the build plan names it as a dependency of the Administration step.

**The console** (§6.1) follows Q4: under GitHub Actions › Environments, **Create environment** is shown as "Create on GitHub ↗" plus **Check rule** until step 5 ships.

---

## 12. What was built (2026-10-24, lane SSC-DOCS, part of #2158)

Read on main at `6571518`, after SSC-SC (PR 2251) merged. Each entry names
the lane, its PR and what it built; where the build departs from §2-§6, the
departure is §11's answer or a reason the lane recorded, and it is said here
so that nobody "fixes" the code back to the paragraph above.

**SSC-REPO-SCOPE (PR 2225).** §2 as written, with Q2 widened by §11. A
repository secret is a `-wsec-` secret of the workspace, with `scope:
repository` and the registration's `repo_id`, managed under
`/v1/repositories/{repo_id}/secrets` (`apps/swarm-api/swarm_api/routes/repository_secrets.py`).
Its grants are signed form `"v2"`
(`apps/swarm-api/swarm_api/grant_attest.py::FORM_REPOSITORY`), which covers
the `repo_id`; a `"v1"` grant still verifies. The release hands one only to
an attempt whose signed spec clones that repository
(`apps/swarm-api/swarm_api/workspace_secrets.py::attempt_repositories`), and
resolves one name offered by several scopes in Q1's order: the person's,
then the repository's, then the workspace's. **Departure, from Q2:** a
member whom GitHub calls admin of the repository may also set its secrets for
agents, asked live with their own token
(`apps/swarm-api/swarm_api/github_repo_admin.py::require_repo_admin`). The
review added one thing §2.1 did not say: a repository secret's id is marked
(`apps/swarm-api/swarm_api/workspace_secrets.py::is_repository_secret_id`),
so a record stripped of its `scope` cannot pass as a workspace secret.

**SSC-ADMIT-REPO (PR 2238).** §2.3: admission counts a repository grant only
for an attempt that clones that repository, in Q1's order, so a required
slot filled only by another repository's secret parks `CREDENTIAL_MISSING`.
**Departure:** the scheduler's codec does not decode
`Task.additional_repositories`, so admission reads the task document for it.
A follow-up on #2158 moves that decode into
`apps/scheduler/scheduler/codec.py`; it is not built.

**SSC-ACTIONS-API (PR 2240).** §3, with §11's Q3, Q4 and Q5, which change
most of it:

* **The writer is the existing SwarmCloud App (Q3), not §3.5's second App.**
  Each call mints, from that App, a token for one repository and the one
  permission the call needs
  (`apps/swarm-api/swarm_api/forge_installation.py::ACTIONS_PERMISSIONS`):
  `secrets: write` for a repository secret, `environments: write` for an
  environment's, read levels for the name lists and `actions: read` for an
  environment's branch rule. Never `administration`. The token is never held,
  stored or logged. An environment secret's mint asks `environments: write`,
  not the `read` the build brief said, because GitHub refuses an
  environment-secret PUT or DELETE without write.
* **SwarmCloud never creates, edits or deletes an environment (Q4).** `GET
  .../actions/environments/{environment}/rule` reads whether an existing one
  lets only the default branch deploy. A missing environment is answered with
  GitHub's page to create it. A write into an environment whose rule is not
  main-only needs `confirm_environment`, the environment's name typed. §3.4's
  create route and §6.2's environment delete do not exist, and will not
  until user-scoped-secrets step 5 lets the App hold Administration: write.
* **The value is sealed in swarm-api (Q5)**, with PyNaCl's sealed box
  (`apps/swarm-api/swarm_api/actions_secrets.py::seal`), and dropped after
  GitHub's PUT. It passes through swarm-api's memory as every other store's
  value does; §3.2's browser seal and §6.2's public-key route are not built.
* **Who (Q6):** a workspace manager whom GitHub, asked with their own token
  at the write, calls admin of the repository. Both, never either.
* **What is kept (§3.8):** one `actions_secret_writes` document per
  destination and name, and one `admin_audit` row in the same transaction.
  No value, ciphertext or token.
* **Not live yet.** Until BGD gives the App Secrets and Environments read and
  write and each installation accepts them, every write is refused 409 with
  `secrets_not_granted` or `environments_not_granted` and the installation's
  review link (`docs/runbooks/github-app.md`, "Writing GitHub Actions
  secrets"). Nothing has written to a real repository.

**SSC-OVERVIEW (PR 2248).** §4 and §6.2: `GET /v1/secrets/overview` reads
every store of one scope in one call: names, status, who set it and when, the
consumers granted, `can_manage` per row, and which row a shadowed name lost
to. It serves no value, length or `last4`. **Known limits, recorded by the
lane:** shadowing is computed without checking attestation, two
repositories' secrets of one name are not compared with each other (no
attempt sees both as one repository's), and runner-key rows carry no set-by
or set-at, because their store keeps none.

**SSC-LINKS (PR 2219).** §6.3 and Q8: a QA target's credential link takes a
`field` (`credential` or `challenge_bypass`) and works only for that field;
the challenge-bypass `PUT` takes its `link_token`; and the runner key gets its
own link, `POST /v1/tenants/me/credentials/links`, which opens
`/config/secrets?scope=workspace&runner_key=<provider>&t=<token>` (BGD,
2026-10-24 06:30Z). A bypass write without a link is refused under
`qa_bypass_link_required`, which ships off; a bad runner-key link falls under
the existing `KEY_LINK_INVALID`, also off.

**SSC-PLATFORM-STATUS (PR 2218, and PR 2221).** §5 with Q7: `GET
/v1/admin/platform-secrets`, admin only, reads Secret Manager metadata
(`get_secret`, `list_secret_versions`) and never a version's payload. The
grant is a custom role holding exactly `secretmanager.secrets.get` and
`secretmanager.versions.list`, conditioned on `swarm-` names of this project
by number (`terraform/bootstrap/platform_secret_status.tf`). **It is off:**
`enable_platform_secret_status` defaults to false, BGD sets it when he runs
`make bootstrap` (his answer of 2026-10-24 06:30Z), and until then every row
reads `unreadable` with `permission_denied`. The lane corrected its brief:
no release applies `terraform/bootstrap`, so the grant does not hold at
`dev-iam`; only `make bootstrap` applies it. PR 2221 (PLATFORM-SECRETS-500)
made a deployment with no Secret Manager client answer `unreadable` with
`read_failed` instead of a 500. The catalogue dropped
`swarm-github-secrets-app-private-key`, because Q3 means it never exists.

**SSC-SC (PR 2251).** §6.4 with Q8: `swarm_secret_set_link`,
`swarm_actions_secret_link`, `swarm_secrets` and `swarm_platform_secrets`, in
`apps/swarm-mcp/swarm_mcp/secrets.py`, and `/sc secrets list` and `/sc
secrets link`. No tool's input schema has a value field. A link tool answers
`url` and `expires_at` only, and `url: null` without a console origin. The
runner-key link has no tool yet; whether it should is an open owner
question on PR 2251.

**SSC-UI (lane SSC-UI, 2026-10-25).** §6.1 as Config proposal E S4 and
S7 draw it (BGD's pick, #2157): Config › Secrets at `/config/secrets`
(`apps/swarm-ui/src/Secrets.tsx`), its address carrying the scope picker,
the repository, the destination and which one-time form is open
(`apps/swarm-ui/src/paths.ts::secretsLinkView`), never a token. The store's
list, grant matrix, Rotate and Add are `WorkspaceSecrets.tsx`'s, now handed
the store they write to (the workspace's or one repository's). Every Set
opens a one-time form that mints its own link when no tool's link brought
one, and Add a secret is the four-step side sheet (scope, destination, who
reads it, one-time form). Settings › API keys moved into Keys for agents and
MCP; `/settings/keys` and `/settings/keys/typesafe?..&t=..` land there with
their token, and the Jev backend choice moved with it. Admin › Platform
secrets is `apps/swarm-ui/src/AdminPlatformSecrets.tsx`, listed for a
platform admin only. **Departures:** Yours is written only in the person's
own workspace (the console has no per-call workspace choice), so from a
group workspace Yours is read-only with a switch; the GitHub Actions
position draws SwarmCloud's record only, its pane being SSC-UI-ACTIONS'.

**Lanes the build added (each answered on #2158 from §2.1's rule, "a
removed registration disables every grant on its secrets in the transaction
that removes it"):**

* **SSC-REPO-DELETE (PR 2239)**: `DELETE /v1/repositories/{repo_id}`
  disables the repository's secrets' grants first, and keeps the
  registration when that fails.
* **SSC-REPO-DELETE-TXN (PR 2245)**: the disable, the audit rows and the
  delete are one Firestore transaction
  (`apps/swarm-api/swarm_api/workspace_secrets.py::WorkspaceSecrets.delete_repository`).
  A delete that would need more than
  `apps/swarm-api/swarm_api/workspace_secrets.py::MAX_TRANSACTION_WRITES`
  writes (about 33 fully granted secrets) is refused whole with a 409, rather
  than split into two writes, so "one transaction" holds in every case.
* **SSC-UNREGISTER-TXN (PR 2249)**: revoking the last access grant on a
  repository (`apps/swarm-api/swarm_api/access.py::AccessService._unregister_if_last`)
  goes through the same transaction. If it fails, the revoke still answers
  200 with `unregistered: false` and keeps the registration, because the
  access grant is already gone.
* **LINK-ADMIN-WRITES (PR 2229)**: BGD's answer to SSC-LINKS: the runner-key
  and QA-target link routes are admin writes
  (`apps/swarm-api/swarm_api/roles.py::ADMIN_WRITES`), so a platform admin
  minting one where they hold less than admin leaves a `platform_admin_acted`
  audit row.
* **PLATFORM-SECRETS-500 (PR 2221)**: above, under SSC-PLATFORM-STATUS.

**What ships off, and why each is safe off.** Every new refusal ships off
(`test_new_refusals_ship_off.py`), and none of these lets through a write
that should be refused:

| switch | off means | on once |
|---|---|---|
| `github_admin_required` | the caller is still refused, as `forbidden` instead of the new code | the console shows `detail.reason`'s copy (SSC-UI) |
| `grant_key_required` | a repository grant is written unattested, and the release drops it, because a repository grant is released only when attested (§2.2) | every environment holds the grant key |
| `qa_bypass_link_required` | a bypass value sent without a link is written and logged without the value | the console's bypass form opens from a link (SSC-UI) |

**The residual risk BGD accepted (Q3) is live.** User-scoped-secrets step 5
has not shipped: `terraform/bootstrap/forge_user_slots.tf`
(`resource "google_project_iam_member" "forge_slot_reader" {`) still lets
tenant workers read the `-git-u-` user-token slots. Once the App holds
Secrets: write, a repository admin's user token read from such a slot can
overwrite that repository's Actions secrets. GitHub serves no read of a
secret, so it cannot disclose one; it can replace one a privileged workflow
trusts, such as `MERGE_APP_PRIVATE_KEY`, or delete them all. Step 5 closes
it, and Administration: write (and **Create environment**) waits for step 5
for exactly this reason. `docs/security.md` ("Secrets scopes and GitHub
Actions secrets") carries the threat model.

**The done check (§9) is not met yet.** It needs BGD's App permission step
and the console (SSC-UI, SSC-UI-ACTIONS). Until both land, the same write is
reachable as `PUT /v1/repositories/{repo_id}/actions/secrets/{name}` from a
one-time link's form, or from a terminal, and `/sc secrets link repository
MERGE_APP_PRIVATE_KEY --repo bogdan-alexandrescu/SwarmCloud --actions --env
repo-secrets` already returns the link.
