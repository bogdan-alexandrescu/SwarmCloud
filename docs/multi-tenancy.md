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
| Identity | `swarm-agent-worker-<id>@<project>.iam.gserviceaccount.com` | IAM |
| Control-plane data | custom role `swarmTenantWorkerFirestore`, **unconditioned** | IAM, on the shape of the access only — **see the caveat below** |
| Artifacts & checkpoints | GCS prefix `tenants/<id>/`, granted by IAM condition | IAM condition on the binding |
| Provider keys | `swarm-tenant-<id>-<provider>`, secret-level IAM | Secret Manager resource policy |
| GKE workloads | namespace `swarm-tenant-<id>` + workload identity binding | Kubernetes RBAC + default-deny NetworkPolicy |
| Capacity | `tenant:<id>` and `provider:<p>:tenant:<id>` slot pools | admission transaction |
| Record | `tenants/<id>` document | control plane |

Removing a tenant is the same list in reverse, in an order that matters, and
the namespace is removed by hand: the reconciler holds no ClusterRole and does
not collect namespaces (owner decision, 2026-09-24). See
[the tenant offboarding runbook](runbooks/tenant-offboarding.md), which also
lists what a tenant accumulates at runtime and this table does not show. All
of that is deleted at offboarding, not kept (owner decision, 2026-09-24), by
`scripts/offboard-tenant.sh`
([step 6](runbooks/tenant-offboarding.md#6-delete-everything-the-tenant-left)).
A tenant id is derived from its principal and the GCS grant is a prefix
condition on that id, so anything kept would be readable by the next
registration that derives the same id.

The namespace is `swarm-tenant-<id>`. The GKE row said `swarm-<id>` until
2026-09-24, which is the spelling behind the 2026-09-23 dispatch outage
([the dispatch 403 note](gke-dispatch-403.md)).

The service account name is `swarm-agent-worker-<id>`, and it is the one name to
grant or audit against. It is what `terraform/modules/tenancy` creates, what
`terraform/modules/cloud_run_jobs` binds to each `(tenant, profile)` Job, and
what `kubernetes/render.py` puts in the Workload Identity annotation. Two other
spellings used to appear — the Identity row said `swarm-tenant-<id>` and
`register-tenant.sh` created `swarm-t-<id>` — and neither existed in a deployed
project, so an operator checking a boundary found nothing and concluded
registration had failed.

**The Firestore row is the weakest boundary in this table, and it is worth
reading before relying on it.** Firestore IAM has no collection- or
document-level granularity: the smallest resource a binding or a condition can
name is the *database*. And the binding carries **no condition at all**, because
Firestore does not evaluate IAM Conditions on the data plane — attaching one
denies document access outright rather than scoping it (verified live; see
[docs/security.md](security.md#firestore-database-scoping-does-not-exist-verified-2026-09-16)).
So every swarm identity shares one authorization scope over *every* Firestore
database in this project — and a worker running attacker-controlled code is
inside that scope. What is available
at this layer is the *shape* of the access, so the role is built rather than
borrowed: `swarmTenantWorkerFirestore` drops `datastore.entities.delete` (a
hostile worker cannot destroy another tenant's tasks, leases or the pool
documents the platform admits against) and `datastore.entities.list` (no
queries, so no enumerating every tenant's prompts and repository URLs — a
document can only be fetched by an id already known, and ids are
`<prefix>_<20 hex>`). The residual is stated in section 4 rather than implied.

```bash
./scripts/register-tenant.sh --group eng@saga.xyz \
    --providers anthropic,openai --max-active 40 --capacity-units 80
```

Re-running updates limits and wiring and fills in whatever is missing. It will
**refuse** if `tenants/<id>` already names a different principal or kind: that
is not idempotency, it is re-pointing a tenant, and one
`--group contractors@saga.xyz --tenant eng` would otherwise hand every member of
`contractors@` eng's provider keys, artifact prefix and budget.

The script creates the GKE side by calling `kubernetes/apply.sh`, so a
registered tenant gets its NetworkPolicy, ResourceQuota, LimitRange, Pod
Security Admission labels and RBAC in the same run as its namespace. It used to
create only the namespace and a service account, which left the namespace on
Kubernetes' default allow-all pod networking — one tenant's agent could open a
socket to another's, traffic no IAM scoping ever sees. If the cluster is not
reachable the script says so and skips that step; nothing on the Cloud Run path
needs it, and browser-class runners cannot be dispatched for that tenant until
it is done.

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

Admin routes (`/v1/admin/*`) are gated on admin — `ADMIN_GROUPS` membership or
an address on `ADMIN_USERS` — which is kept separate from `TENANT_GROUPS` so an
admin still belongs to a normal tenant for their own tasks. One narrower list,
`ADMIN_POOL_USERS`, reaches only the routes in `swarm_api.auth.POOL_ADMIN_ROUTES`
(today the runner-ceiling `PUT`) and is not admin; see
[security.md](security.md#authentication).

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
| Read another tenant's checkpoints | GCS binding is conditioned on the tenant prefix, listing included |
| Reach another tenant's pod | per-tenant namespace + default-deny NetworkPolicy |
| Have work dispatched under another tenant's identity | Job resource is per tenant and carries the SA |
| Escalate via a runner profile | callers name profiles; images and commands are catalogue-only |
| Spoof a tenant claim | tenant is derived from a verified ID token, never from a header or body field |
| **Read or write another tenant's control-plane documents** | **partially. See below — this row is the one that does not have two mechanisms.** |

### The Firestore row, in full

Every row above needs two independent mechanisms to fail. This one does not have
two, and pretending otherwise would be worse than saying so.

Firestore exposes no collection-level IAM to a server-side service account. The
smallest resource a binding or a condition can name is the database — and on the
data plane it cannot even name that, because Firestore ignores IAM Conditions
there. A `resource.name == .../databases/swarm` condition does not scope the
grant, it denies it: every identity that carried one reported `firestore
unavailable: PermissionDenied`, so both Terraform (`scope_firestore_to_database`,
default false) and `register-tenant.sh` now grant the role unconditioned.
`docs/security.md` concedes that an agent can mint its own
workload's metadata token and argues that the boundary is what that identity can
do — so here is what it can do:

* **It cannot delete anything.** `datastore.entities.delete` is not in the role.
  Another tenant's tasks, attempts, leases and the `pools/*` documents the whole
  platform admits against cannot be destroyed.
* **It cannot enumerate anything.** `datastore.entities.list` is not in the role,
  so there are no queries: no dumping every tenant's prompts, repository URLs and
  artifact paths. A document can only be fetched by an id already known, and
  task, attempt and lease ids are `<prefix>_<20 hex>`.
* **It CAN read and write a document whose id it can guess.** `pools/global`,
  `pools/tenant:<victim>`, `tenants/<victim>` and `quota/<provider>:<victim>` are
  all guessable names. So a hostile worker can disable another tenant's pool,
  raise its own `hard_limit` past the admin's ceiling, or rewrite a `tenants/<id>`
  document whose `service_account` and `gcs_prefix` the control plane trusts as
  configuration. `attempts/<id>` is not guessable, so bumping another attempt's
  fencing `generation` needs an id it was never given.

Closing this needs one of two changes, neither expressible in IAM: the worker off
direct Firestore access entirely, reaching state through a control-plane service
that scopes by caller identity; or one database per tenant. Until then the honest
statement is that the Firestore boundary is *narrowed*, not *closed*, and the
grant is identical whether a tenant was created by Terraform or by
`register-tenant.sh` — the script refuses to run at all if the custom role is
missing rather than falling back to `roles/datastore.user`.

The one place to be careful when extending the platform: **anything that accepts
a tenant id as input**. Admin routes do, and they are gated on admin
(`ADMIN_GROUPS` membership or `ADMIN_USERS`). A non-admin route that took a
`tenant_id` parameter would be the single change that undoes this table.

The one non-admin caller on the admin surface is an `ADMIN_POOL_USERS` entry
(the verification gate), and it reaches only the routes in
`swarm_api.auth.POOL_ADMIN_ROUTES`. Today that is
`PUT /v1/admin/limits/runner/{runner_profile}`, which takes a runner profile,
not a tenant id, so it opens no path from one tenant's id to another's data.
It is not harmless across tenants: a ceiling of 0 on a profile stops every
tenant's work on it being admitted until someone puts it back. Adding a route
that takes a tenant id to that allow-list would be exactly the change
described above. See [security.md](security.md#authentication) and
[the dated decision](audits/2026-09-22/race-test-needs-a-write.md).

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

# Limits. monthly_budget_usd is refused (422): nothing attributes cost, so it
# could be stored but never enforced -- see routes/admin.py.
./scripts/api.sh PUT /admin/tenants/eng/limits \
    '{"max_active": 25, "capacity_units": 50}'

# Pause one tenant without touching anyone else. --keep-scheduler is what makes
# that true: without it pause-swarm.sh also pauses swarm-scheduler-tick, the
# one-minute backstop that keeps admission moving for every tenant when a wake
# message is missed.
./scripts/pause-swarm.sh --tenant eng --keep-scheduler
./scripts/resume-swarm.sh --tenant eng

# Purge runtime data by collection or age (never infrastructure). Not the
# offboarding path: it exports the whole database first, and removes only the
# live version of each object.
./scripts/purge-data.sh --tenant eng --dry-run

# Offboarding a tenant's DATA: every object version under tenants/<id>/, every
# Firestore record, then the tenant document, with a proof that each is gone.
# Dry run by default. Refuses a tenant that is enabled, holds capacity or is
# still lent an account.
./scripts/offboard-tenant.sh --tenant eng
./scripts/offboard-tenant.sh --tenant eng --apply     # type "eng" to confirm

# Offboard a tenant entirely -- identity, keys, jobs, namespace, records:
#   docs/runbooks/tenant-offboarding.md
```

Offboarding is a runbook rather than a script because two of its steps are
decisions or waits, not commands: whether its people also lose access, and
draining its running work. Whether its data is kept is not a decision any
more. The owner decided on 2026-09-24 that it is deleted, and
`scripts/offboard-tenant.sh` is the runbook's step for that. Its terraform half
is one tfvars edit. Its GKE half, deleting `swarm-tenant-<id>`, is manual by
design, because the reconciler holds no ClusterRole and no longer tries to
collect namespaces (owner decision, 2026-09-24).
[The runbook](runbooks/tenant-offboarding.md) has the order and the checks.

New tenants start small — `default_tenant_max_active` 20, `capacity_units` 40 —
and an admin raises them. The failure mode of starting large is a new tenant
consuming the platform on their first bad loop.

---

## 6. The personal fallback tenant

A caller in no registered group gets `u-<local-part>` (so `alice@saga.xyz` ->
`u-alice`). Three properties:

* the `u-` prefix cannot collide with a group-derived tenant id, because group
  ids are the group's local part with no prefix;
* slugging is **not** allowed to be lossy. `eng.team@` and `eng-team@` both
  reduce to `eng-team`, and two distinct Google groups silently becoming one
  tenant — one namespace, one service account, one set of provider keys — is the
  exact cross-tenant merge this design exists to prevent. So whenever the slug is
  not a faithful rendering of the local part, `swarm_common.identity` appends a
  6-character digest of the full principal. `eng.team@saga.xyz` becomes
  `eng-team-9aef5b`, not `eng-team`;
* the tenant is real — own GSA, own prefix, own pools — so a personal task is
  isolated exactly like a group's.

This exists so that onboarding never requires a group change first. It is also
why `tenant:<id>` pools are created on demand rather than only by Terraform: a
personal tenant appears the first time its owner submits.

### Tenant ids are capped at 11 characters, and that cap is currently too small

A GCP service account id is capped at 30 characters. The identity is
`swarm-agent-worker-<id>`, and that prefix is 19 characters, so a tenant id may
be at most **11**. Both provisioning paths enforce it —
`terraform/modules/tenancy/variables.tf` and `register-tenant.sh` — and neither
truncates to fit. Truncating is what turns a length limit into a security bug:
with a 30-character cut, every tenant id agreeing in its first 11 characters
collapses onto one service account, and both tenants' `secretAccessor` bindings
and both tenants' GCS prefix conditions then accumulate on it, each able to read
the other's provider keys.

**This is an unresolved conflict between two tracks, recorded here rather than
smoothed over.** `swarm_common.identity` caps a tenant id at 22, sized for the
prefix `swarm-t-` (8 characters). Terraform uses a 19-character prefix. Ids
between 12 and 22 characters are therefore resolvable by the API and
provisionable by nobody:

| Principal | Resolves to | Length | Provisionable |
|---|---|---|---|
| `eng@saga.xyz` | `eng` | 3 | yes |
| `research@saga.xyz` | `research` | 8 | yes |
| `platform-eng@saga.xyz` | `platform-eng` | 12 | **no** |
| `eng.team@saga.xyz` | `eng-team-9aef5b` | 15 | **no** |
| `alice@saga.xyz` | `u-alice` | 7 | yes |
| `alice.smith@saga.xyz` | `u-alice-smi-4c1f2a` | 18 | **no** |

The last row is the one that bites: any dot in a local part makes the slug lossy,
the digest is appended, and the result is over 11 — so the personal fallback
tenant this section describes does not work for most real names today.
`register-tenant.sh` refuses with this conflict spelled out rather than creating
a tenant the API will never resolve to. Closing it means shortening the service
account prefix in `terraform/modules/tenancy` to the `swarm-t-` the frozen module
already assumes; the frozen module cannot be the side that moves.
