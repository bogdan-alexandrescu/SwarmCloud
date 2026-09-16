# Multi-tenancy

Multi-tenant from V1, not retrofitted. Retrofitting tenancy means retrofitting
every IAM binding, every storage path and every secret name at once, while
production data already exists in the single-tenant shapes.

**A tenant is a Google group.** `eng@saga.xyz` is one tenant whose members share
a quota budget, provider keys and artifacts. A user in no registered group falls
back to a personal tenant `u-<local-part>`, so nobody is ever hard-blocked from
the platform.

---

## 1. What a tenant owns

Registering a tenant creates every boundary at once — there is no
"half-registered" state worth having:

| Boundary | Resource | Enforced by |
|---|---|---|
| Identity | `swarm-tenant-<id>@<project>.iam.gserviceaccount.com` | IAM |
| Control-plane data | Firestore access scoped to the `swarm` database | IAM |
| Artifacts & checkpoints | GCS prefix `tenants/<id>/`, granted by IAM condition | IAM condition on the binding |
| Provider keys | `swarm-tenant-<id>-<provider>`, secret-level IAM | Secret Manager resource policy |
| GKE workloads | namespace `swarm-<id>` + workload identity binding | Kubernetes RBAC + network policy |
| Capacity | `tenant:<id>` and `provider:<p>:tenant:<id>` slot pools | admission transaction |
| Record | `tenants/<id>` document | control plane |

```bash
./scripts/register-tenant.sh --group eng@saga.xyz \
    --providers anthropic,openai --max-active 40 --capacity-units 80
```

Idempotent: re-running updates limits and fills in whatever is missing.

---

## 2. Group resolution — the constraint that shapes the API

Tested live against `saga-agents-staging` on 2026-09-15:

```
groups:lookup?groupKey.id=eng@saga.xyz                      WORKS
groups/{id}/memberships:checkTransitiveMembership           WORKS
groups/-/memberships:searchTransitiveGroups                 403 Error(4013)
                              "Insufficient permissions to retrieve memberships"
```

So the API **must not** resolve a caller's tenant by enumerating the groups they
belong to. That call is the one that 403s, and fixing it would need an
org-level IAM grant.

Instead the question is inverted. For each admin-registered tenant group, in
priority order, the API asks:

```
GET https://cloudidentity.googleapis.com/v1/{group_id}/memberships:checkTransitiveMembership
    ?query=member_key_id == '<caller email>'
-> {"hasMembership": true|false}
```

This needs no org-level grant and is least-privilege: the platform only ever
learns about groups an admin deliberately registered. It also required no change
to the frozen contract — `identity.resolve_tenant(principal, group_priority)`
already takes the admin-ordered group list, so the API just populates
`Principal.groups` from these per-group checks.

**Every Cloud Identity call requires the header `x-goog-user-project: <project>`.**
Without it the call fails 403 `SERVICE_DISABLED` naming gcloud's shared client
project as the consumer, which reads like a permissions problem and is not.

`checkTransitiveMembership` is a network round trip on a request path that must
stay fast, so answers are cached per `(caller, group)` with a short TTL
(`GROUP_CACHE_TTL_SECONDS`, default 120).

### Priority order matters

`TENANT_GROUPS` is ordered, and the **first match wins**. A user in both
`eng@saga.xyz` and `research@saga.xyz` must land in the same tenant on every
request — the tenant determines which secrets they get, which GCS prefix their
artifacts land in, and which budget they spend. Non-deterministic resolution
would scatter one person's work across two tenants.

---

## 3. Isolation, layer by layer

### Authentication

Google ID token, `hd` claim must be an allowed domain. **There is no shared
platform bearer token.** A shared secret carries no identity; without identity
there is no tenant to attribute a task to, no way to scope a list response, and
no boundary to enforce. Multi-tenancy that starts after authentication is
decorative.

### Data

Every query the API issues is filtered by the caller's resolved tenant. A task id
from another tenant returns 404 — not 403 — because confirming existence is
itself a leak.

Admin routes (`/v1/admin/*`) are gated on `ADMIN_GROUPS`, which is kept separate
from `TENANT_GROUPS` so an admin still belongs to a normal tenant for their own
tasks.

### Secrets

Three independent mechanisms, all in `swarm_api/credentials.py` and
`agent_worker/secrets.py`:

1. **Name.** `swarm-tenant-<tenant>-<provider>`, from the frozen
   `Tenant.secret_name()` — one spelling of a tenant's secret anywhere in the
   platform, never assembled from caller input.
2. **IAM.** The secret's own resource policy names exactly one accessor: that
   tenant's service account. A project-level `secretmanager.secretAccessor`
   grant would make every tenant's key readable by every tenant's workload, so
   the binding is never made at project level.
3. **No read path.** The API can write a version and report metadata. There is no
   function that returns payload bytes, so no future route can accidentally
   expose one.

The plaintext key exists only as a local variable during `put_credential`, is
registered with the logger for redaction the moment a worker reads it, and
reaches the agent through the child's environment only — never argv, never the
workspace, never an artifact.

Secret **material** is deliberately not managed by Terraform: a Terraform-managed
secret version puts the plaintext in the state file, and state lives in a bucket
several people can read. `scripts/create-secrets.sh` owns both the container and
its versions, which is also why `make destroy` never sees them in a plan.

### Storage

Each tenant's GCS access is granted with an IAM **condition** on the object
prefix `tenants/<id>/`. Tenant A's service account cannot read tenant B's
artifacts even by guessing the path, because the denial happens in Google's
authorization layer rather than in our code.

### Compute

* Cloud Run: one Job resource per `(tenant, profile)`, bound to that tenant's
  service account — forced by Cloud Run setting the SA on the Job, not the
  execution. See [execution-backends.md](execution-backends.md).
* GKE: namespace `swarm-<id>`, tenant KSA workload-identity-bound to the tenant
  GSA, default-deny NetworkPolicy so one tenant's pod cannot reach another's.

### Capacity

`tenant:<id>` caps how much of the platform one tenant can hold.
`provider:<p>:tenant:<id>` caps their provider concurrency separately, which is
what stops one tenant's 429 storm from throttling everyone else — their AIMD
target falls, nobody else's does.

---

## 4. Cross-tenant escape: what would have to go wrong

The failure this design treats as the worst outcome. Every path needs at least
two independent mechanisms to fail together:

| Attack | Blocked by |
|---|---|
| Ask the API for another tenant's task | tenant-scoped queries; 404 not 403 |
| Name another tenant's secret in a request | secret name comes from the frozen contract, never from input |
| Read another tenant's secret from a worker | worker runs as the tenant GSA; secret IAM names one accessor |
| Read another tenant's checkpoints | GCS binding is conditioned on the tenant prefix |
| Reach another tenant's pod | per-tenant namespace + default-deny NetworkPolicy |
| Have work dispatched under another tenant's identity | Job resource is per tenant and carries the SA |
| Escalate via a runner profile | callers name profiles; images and commands are catalogue-only |
| Spoof a tenant claim | tenant is derived from a verified ID token, never from a header or body field |

The one place to be careful when extending the platform: **anything that accepts
a tenant id as input**. Admin routes do, and they are gated on `ADMIN_GROUPS`
membership. A non-admin route that took a `tenant_id` parameter would be the
single change that undoes this table.

---

## 5. Tenant lifecycle

```bash
# Register (or update)
./scripts/register-tenant.sh --group eng@saga.xyz --providers anthropic,openai

# Add a provider key. Never passed as an argument: argv is readable via ps.
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin

# Rotate, keeping the previous version enabled until workers pick up the new one
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
# then, once nothing is using it:
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin --disable-previous

# Limits
curl -X PUT "$API/v1/admin/tenants/eng/limits" \
     -d '{"max_active": 25, "capacity_units": 50, "monthly_budget_usd": 2000}'

# Pause one tenant without touching anyone else
./scripts/pause-swarm.sh --tenant eng
./scripts/resume-swarm.sh --tenant eng

# Remove a tenant's data (never their infrastructure)
./scripts/purge-data.sh --tenant eng --dry-run
```

New tenants start small — `default_tenant_max_active` 20, `capacity_units` 40 —
and an admin raises them. The failure mode of starting large is a new tenant
consuming the platform on their first bad loop.

---

## 6. The personal fallback tenant

A caller in no registered group gets `u-<local-part>` (so `alice@saga.xyz` ->
`u-alice`). Two properties:

* the `u-` prefix cannot collide with a group-derived tenant id, because group
  ids are the group's local part with no prefix;
* the tenant is real — own GSA, own prefix, own pools — so a personal task is
  isolated exactly like a group's.

This exists so that onboarding never requires a group change first. It is also
why `tenant:<id>` pools are created on demand rather than only by Terraform: a
personal tenant appears the first time its owner submits.
