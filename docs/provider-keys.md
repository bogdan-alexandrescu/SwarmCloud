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

## 6. Deployment: the API's Secret Manager grant is not verified

These routes write through the same `credentials.SecretManagerCredentials` as
`POST /v1/tenants/me/credentials`, which needs `secrets.create`,
`secrets.getIamPolicy`, `secrets.setIamPolicy` and `versions.add` on
`swarm-tenant-*`. Delete also needs `versions.list` and `versions.destroy`.
[web-ui/06-accounts.md](web-ui/06-accounts.md) already records that
`terraform/modules/iam/bindings.tf` grants swarm-api none of these, and no
grant for them was found in `terraform/bootstrap` either. Until an owner-applied
bootstrap role grants them, scoped by name to the platform's own tenant
secrets (as `browser_logins.tf` scopes its roles), a live `PUT` would get 503
"could not create the tenant secret" rather than store a key. This lane
(KEYS-API) does not touch IAM. The unit tests run against in-memory and fake
Secret Manager clients, so they cannot catch a missing grant.
