# Provider keys a person or an admin sets: TypeSafe

TypeSafe ([docs.typesafe.ai](https://docs.typesafe.ai)) serves the Jev judgment
models. Owner request, 2026-10-14: *"a way to set the TypeSafe api key in the
UX/UI and in the claude code plugin /sc in a safe way"*. This document covers
the API half (lane KEYS-API). The console form (`apps/swarm-ui`) and the `/sc`
command (`apps/swarm-mcp`, `plugin/`) are follow-on lanes.

**Nothing uses these keys yet.** Jev itself is not being built (owner: later,
gated). No runner profile names `typesafe`, no secret here is mounted into any
runner, and nothing sends page content anywhere. The API stores a key, says
whether one is set, and says which one a task *would* use. `GET` status
answers `consumed_by: []` so nobody reads the resolution as a live wiring.

**A TypeSafe key is needed only when a workspace judges with TypeSafe**
(JUDGE-SELECT, owner decisions 2026-10-14). Each workspace chooses Jev's
judgment backend: Internal Jev (the default) or Internal Laya -- Saga's own
decision API, which needs no TypeSafe key and is never sent one -- or TypeSafe.
`GET /v1/provider-keys/typesafe` carries the choice as `jev` (`backend`,
`label`, `available`, `reason`, every option); TypeSafe is `available` exactly
when the resolution below finds a key. See
[design/live-browser.md §3.2.2](design/live-browser.md#322-judgment-backends-judge-select).

Code: `apps/swarm-api/swarm_api/provider_keys.py` (record, audit, links,
resolution) and `apps/swarm-api/swarm_api/routes/provider_keys.py` (routes).
The Secret Manager write is still `apps/swarm-api/swarm_api/credentials.py`,
the same write-only store every tenant provider key goes through.

## 1. Routes

| route | who | what |
|---|---|---|
| `GET /v1/provider-keys/typesafe` | any member | your key's status, your selected tenant's, and which your tasks there would use |
| `PUT /v1/provider-keys/typesafe/user` | anyone with a ready personal workspace | store **your own** key, in `u-<you>` |
| `DELETE /v1/provider-keys/typesafe/user` | the same | destroy it |
| `PUT /v1/provider-keys/typesafe/tenant` | admins | store the selected tenant's key |
| `DELETE /v1/provider-keys/typesafe/tenant` | admins | destroy it |
| `POST /v1/provider-keys/typesafe/links` | as the write it is for | a one-time link to the console's key form (§3) |

**No route names a person or a tenant.** The user routes act on the caller's
own personal tenant, derived from the verified email; the tenant routes act on
the caller's selected tenant (`X-Swarm-Tenant`, through `tenant_scope`'s
collision guard). The bodies forbid every field they do not name, so sending
`user` or `tenant_id` is a 422 rather than silently ignored. "Admin" is the
platform's `is_admin`, as on the Git token routes: there is no per-tenant
admin role to ask instead.

A status is `{available, set, set_at, set_by, version, secret_id}` (plus
`tenant_id` on the tenant's). **Never** a payload, its length, a prefix or a
suffix. `set_by` is the email of whoever stored it, from `provider_keys/`
(below), not from the secret.

### Why `typesafe` is not in `known_providers()`

`known_providers()` is what `POST /v1/tenants/me/credentials` accepts from
**any** tenant member, and what the admin limit, drain and enable routes accept
as a quota pool. A TypeSafe tenant key is an admin's to set and has no pool, so
adding it there would open a second, unguarded way to set the tenant key. The
allow-list is `validation.PROVIDER_KEY_PROVIDERS` instead. `typesafe` is not a
forge or App key either (`credentials.refuse_unbindable_provider`): the
tenant's worker account is exactly the reader it is bound to.

No key prefix is assumed: TypeSafe does not document one. The shape check is the
store's own (`credentials.validate_key_material`): 8 bytes to 8 KiB, one
line, no NUL.

## 2. Lookup order: the person's key wins

`provider_keys.resolve_provider_key(task tenant, submitter's personal tenant,
provider, present)` is a pure function:

1. the submitter's own key, `swarm-tenant-u-<submitter>-typesafe`, if set;
2. else the task tenant's, `swarm-tenant-<tenant>-typesafe`, if set;
3. else none.

Only the submitter's own key is ever a candidate, and the function takes the
submitter's **confirmed** personal tenant (`own_personal_tenant`), never an
email. The frozen slug keeps only the local part, so `dave@partner.com` and
`dave@saga.xyz` both derive `u-dave`, and a resolver that derived it would hand
one person's key to their namesake. Someone with no confirmed personal tenant,
a service identity included, falls to the tenant key. In a person's own
workspace the two are the same secret, which is why the tenant routes refuse a
personal workspace (422) and point to `.../user`.

**Why a person's key lives in their personal tenant, not the group's.** A
secret is bound to a tenant's worker account, and every member of a group
tenant runs as that one account. A key stored "for alice" in the group's
tenant would be readable by every member's tasks. `u-<alice>` has an account
only alice's work runs as. The user routes refuse with 404 until that
workspace is `ready` and its tenant document names this email (the same check
that offers it as the default tenant, `auth.PersonalTenants`), because the
frozen slug drops the domain: `alice@partner.com` derives `u-alice` too. `DELETE .../user` needs only the tenant document to
name this email: a person whose workspace has left `ready` can still destroy
their own key.

**What a future consumer must not do.** A task alice submits in a group tenant
runs as the group's account, which cannot read `swarm-tenant-u-alice-*`. The
fix is **not** to grant the group account on alice's secret; that would give her
key to every member. The consumer lane has to fetch it through a path that acts
for the submitter. That design is still open and belongs to that lane.

## 3. The one-time link

`/sc` must never take the key itself: it would be an MCP tool argument, in a
transcript, and in Claude's context. So `/sc` mints a link and the person
pastes the key into the console:

    POST /v1/provider-keys/typesafe/links {"scope": "user"|"tenant"}
    -> {"url": "https://<console>/settings/keys/typesafe?scope=user&t=<token>",
        "path": "/settings/keys/typesafe?scope=user&t=<token>", "expires_at": ...}

`url` is null when `SWARM_CONSOLE_URL` is unset; `path` is always there.

* The token is 32 random bytes. Firestore keeps only `sha256(token)`, as the
  document id of `provider_key_links/`, along with who minted it, the scope,
  the provider, the tenant, `expires_at` (ten minutes) and `used_at`. The
  token itself is never stored and never logged.
* A link is minted only for a write the caller could make anyway: a `user`
  link needs the caller's ready workspace, a `tenant` link needs an admin.
* The token is in the URL's query, as the owner specified, so it can reach the
  console's request log, browser history and a Referer. It grants nothing, so
  that is tolerable. The console form should still drop it from the address
  bar once it has read it (`history.replaceState`), which is the UI lane's job.
* A link is consumed before the key is written, so a write that then fails
  (Secret Manager 503) needs a new link. The other order would leave a key
  stored while its link was still live.
* The form sends it back as `link_token` on the PUT. It is consumed (in one
  transaction) only if it is unused, unexpired, and minted by this caller for
  this scope, provider and tenant. Someone else presenting it does not burn it.
* A bad link is refused 403 `KEY_LINK_INVALID`, a new refusal that **ships
  off** (`refusals.SWITCHES`, `REFUSAL_KEY_LINK_INVALID=on` turns it on). Off,
  the refusal is logged and the write goes ahead on the caller's own session,
  as a direct visit to the settings page does. That is safe because **the
  link grants nothing.** The admin check and the personal-workspace check
  both run before the link is looked at, and the write always lands in the
  caller's own tenant, whatever the link says.

## 4. Threat model

| attacker | what they get |
|---|---|
| **Another member of the tenant** | Status only: whether the tenant key is set, when and by whom. They cannot set or delete it (admin), and cannot reach anyone's personal key, because no route takes a user. Their own `GET` shows their own key, not a colleague's. |
| **A member of another tenant** | Nothing about this tenant: `tenant_scope` only lets a caller select tenants they belong to. |
| **Whoever finds a leaked link** | A dead link after ten minutes or one use. Before then it still needs **the minter's own console session**: a link presented by anyone else is refused (switch on) or ignored (switch off), and it is not consumed. |
| **A log reader** | No key. It exists only as a local in `credentials.put_credential`. It is not logged, not written to Firestore, not echoed by the validation handler, and not returned. `tests/unit/control_plane/test_provider_keys.py` stores a runtime-built fake key and asserts it is absent from every captured log line, every response and every Firestore document. |
| **Claude** (`/sc`) | Never the key. It sees a link and a status. |

## 5. Delete and audit

`DELETE` **destroys** every version that is not already destroyed
(`credentials.delete_credential`). It does not disable them, because a disabled
version can be re-enabled by anyone holding `secretVersionManager`, and a
person who deletes a key means for it to be gone. The secret resource and its
IAM binding stay, so storing a key again is the ordinary write. Deleting a key
that was never set is a 404. If destroying fails part-way, the request is a 503 before
anything is recorded and the status still says `set`. Retrying the delete
destroys what remains.

Every set and delete writes `provider_keys/{secret_id}` (the status) and one
`admin_audit` entry, `provider_key_set` / `provider_key_deleted`, in the same
transaction: `{by, at, target_secret_id, detail: {tenant_id, provider, scope,
version, via_link}}`. The value never appears in either.

## 6. Deployment: swarm-api's Secret Manager grant (KEYS-IAM)

These routes write through the same `credentials.SecretManagerCredentials` as
`POST /v1/tenants/me/credentials`. KEYS-API found that swarm-api's account held
none of the permissions that write needs
([web-ui/06-accounts.md](web-ui/06-accounts.md) records the same of
`terraform/modules/iam/bindings.tf`), so a deployed `PUT` answered 503 "could
not create the tenant secret". Owner decision 2026-10-14: *"Yes, scoped
bootstrap role"*.

`terraform/bootstrap/provider_keys.tf`, switched on by `enable_provider_keys`
(set in `terraform/bootstrap/terraform.tfvars`), defines
**`swarmProviderKeyWriter`** and grants it to swarm-api under one condition. The
owner applies it; CI cannot (below).

### The permissions are credentials.py's calls, and nothing else

| call in `credentials.py` | permission |
|---|---|
| `create_secret` | `secretmanager.secrets.create` |
| `get_iam_policy` | `secretmanager.secrets.getIamPolicy` |
| `set_iam_policy` | `secretmanager.secrets.setIamPolicy` |
| `add_secret_version` | `secretmanager.versions.add` |
| `list_secret_versions` | `secretmanager.versions.list` |
| `destroy_secret_version` | `secretmanager.versions.destroy` |

Not `secrets.get`: the module never reads a secret's metadata. Storing a second
key is `create_secret` answering `ALREADY_EXISTS`, which `create` authorises on
the project. Not `versions.access`: there is no read path (the module
docstring, point 3). Not `secrets.delete`: delete destroys versions and keeps
the secret (§5). `tests/terraform/provider_keys.tftest.hcl` reads
`credentials.py` and fails when a Secret Manager call is added or removed there
without the role following. So a new call cannot ship and then 403 in
production.

### The condition, and why `create` is not narrowed

```
resource.type == "cloudresourcemanager.googleapis.com/Project"
  || resource.name.startsWith("projects/209012342332/secrets/swarm-tenant-")
```

* **The name arm uses the project NUMBER.** IAM evaluates Secret Manager names
  with the number, and the project ID "can't be substituted" there. The ID
  spelling also passes `lintPolicy`, but it would match nothing. 209012342332
  is `gcloud projects describe saga-agents-staging --format='value(projectNumber)'`,
  read 2026-10-14. getIamPolicy, setIamPolicy and the three `versions.*`
  permissions are checked on the secret or version, so this arm limits them to
  `swarm-tenant-*`. They never reach `swarm-github-app-*` or the other team's
  `agents-*` / `promptlab-*`.
* **A resource-name condition cannot gate `secrets.create`.** Create is
  evaluated on the parent project, whose name has no secret id in it. With the
  name arm alone every create would be refused and the routes would still 503.
  The project arm lets create through, so the residual is that **swarm-api may
  create an empty secret with any name**. It cannot read, version or set IAM on
  any secret outside the name arm. While `enable_forge_user_slots` is on,
  swarm-api already holds an unconditioned `secrets.create`
  (`swarmForgeSlotCreator`), so this grant adds nothing there.
* **Pre-created secrets would remove that residual, but they cannot work
  here.** Terraform would have to name every key's secret. A personal tenant
  `u-<user>` is created by the workspace job outside Terraform, and
  `PUT /v1/provider-keys/typesafe/user` writes into exactly those tenants.
  `credentials.py` would also have to stop creating secrets.

**What the name arm still reaches.** Every `swarm-tenant-*` secret, not only
provider keys: a tenant's forge token (`-git`), its GitHub user and repository
slots, and its browser sessions. On any of these, swarm-api can add a version,
destroy versions and rewrite the IAM policy. setIamPolicy amounts to
`versions.access`, because swarm-api can grant read to anyone, itself included.
The guard against misuse is in the code: before calling Secret Manager,
swarm-api refuses the forge and App providers
(`credentials.refuse_unbindable_provider`), and it only ever names
`Tenant.secret_name(provider)` for an allow-listed provider. A narrower
condition, limited to the provider suffixes, would repeat validation's provider
lists in Terraform with nothing to keep the two in sync. That is the owner's
call, asked in the KEYS-IAM pull request.

**Why bootstrap and not terraform/infra.** A custom role cannot be defined in
terraform/infra (#79). Adding a role that carries `secrets.setIamPolicy` to the
CI deployer's grantable list would let CI grant itself rewrite access to any
secret's policy in the shared project (#69). `hasOnly()` limits which roles can
be granted. It does not limit who receives them.

### Applying it (the owner, dev-iam)

Lint the condition first. On 2026-10-14 it linted clean (`{}`), and a
deliberately broken expression came back as a `ConditionCompileCheck` ERROR,
so the clean result means something:

```bash
curl -sS -X POST \
  --oauth2-bearer "$(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" -H "x-goog-user-project: saga-agents-staging" \
  https://iam.googleapis.com/v1/iamPolicies:lintPolicy \
  --data '{"fullResourceName":"//cloudresourcemanager.googleapis.com/projects/saga-agents-staging","condition":{"title":"swarm tenant provider keys","expression":"resource.type == \"cloudresourcemanager.googleapis.com/Project\" || resource.name.startsWith(\"projects/209012342332/secrets/swarm-tenant-\")"}}'
```

Then plan and apply `terraform/bootstrap`. The plan is **2 to add, 0 to change,
0 to destroy**: `google_project_iam_custom_role.provider_key_writer[0]` and
`google_project_iam_member.provider_key_writer[0]`. The `provider_key_grants`
output shows the condition to read the plan against. After applying, the check
is a live `PUT /v1/provider-keys/typesafe/user` that answers 200 instead of
503.

**Not verified live:** how IAM evaluates the name forms and the create
resource type (the header of `provider_keys.tf`). The unit tests run against
in-memory and fake Secret Manager clients, and the Terraform tests use a mock
provider. Neither can show what IAM itself answers.
