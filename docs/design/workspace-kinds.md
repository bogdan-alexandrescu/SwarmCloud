# Workspace kinds: an admin creates a personal, group or service workspace from Admin › Tenants, and the in-cloud job builds it

**Status: built 2026-10-19 (part of #847), switched off.** Designed by lane
WORKSPACE-KINDS-DESIGN, with every §7 question answered (a) by the owner the
same day. Every lane of §6 is on `main`, and §8 records what each landed and
where the code departs from this text. Nothing of it reaches the cloud until
the owner's one-time steps of §6 run: O1 (not yet recorded,
`docs/workspaces.md` §0), then the bootstrap apply with
`enable_workspace_kinds` set to true, then the policy apply. Until then an admin
can file and approve a group or service record, but the deployer holds none
of K1 to K3, so the job cannot finish one: a service whose S2 IAP refuses
stops for the owner as `FRONT_DOOR_OWNER` (`ws_front_door_failed` in
`scripts/register-tenant.sh`), and nothing half-made runs (§4.8).

The owner asked for this on 2026-10-19, on #847:

> "why can't we create a way to launch new workspaces without using CI or some
> operator laptop for terraform but instead swarmcloud gets an integrated way to
> bootstrap new personal workspaces as part of adding a new tenant via the admin
> ui as a full end to end flow"

They chose to have it designed first and built after. The decision recorded
on #847 the same day sets the scope: one **New workspace** flow in
Admin › Tenants for all three kinds, personal, group (a Google group) and
service (a service account). The same in-cloud job, `swarm-workspace-apply`,
runs it, with no CI, no operator laptop and no Terraform. **For new
workspaces this reverses WD3** (`docs/workspaces.md` §3.2, "Terraform owns
group and service tenants"). #1590's namespace gap becomes the job's step A7,
for every kind.

Every file and symbol cited below was read on 2026-10-19 at `d2b2d14`. Lines
move, so each citation names the symbol. Search for it.

What the design settles, one line each:

* **One record, three kinds.** `workspaces/{tenant_id}` gains `kind`
  (`personal`, `group` or `service`) and keeps its states. A personal record
  works exactly as today. The new kinds use the same `requested → approved →
  applying → ready` path, the same approve, deny and retry routes and the same
  progress view (§4.1, §4.2).
* **An admin starts every kind from Admin › Tenants › New workspace.** For a
  personal workspace the admin names the person, which today's §1.3 rule 4
  forbids. That is the owner's call (Q1). A group workspace is approved by
  any admin. A service workspace, which admits a new principal at the front
  door, is approved by the platform owner (Q2).
* **The job's steps are A1 to A9 for every kind**, with one step added for
  each new kind: S1 checks the service account, and S2 admits it at the front
  door. A6 binds a different forge slot for each kind. A7, the namespace,
  runs for every kind (§4.3).
* **A group workspace is registered by being `ready`.** swarm-api reads the
  ready group records as registered groups, after `TENANT_GROUPS`, and checks
  membership one registered group at a time with `x-goog-user-project`, the
  same way as today. No call lists a caller's groups (§4.4).
* **The front-door admit is one IAM row, narrowed as far as IAP allows:**
  `roles/iap.httpsResourceAccessor` on the API backend only. The condition
  names that backend by project **number** and numeric backend id, as an
  exact match and not a prefix. The guard allows only the approved service
  principal as the member. Every admit is audited in `admin_audit` and
  alerted on (§4.5).
* **Secrets stay user-scoped.** A group workspace gets an empty team forge
  slot. Its members' own slots are served only through the credential broker.
  A service workspace gets one team forge slot. Neither kind gets a provider
  key. Each runs on an account lent to it, or one an admin adds (§4.6).
* **The four Terraform tenants stay in Terraform** (recommended, Q8), and
  `var.tenants` is frozen to those four keys. A migration plan that destroys
  nothing is written out in case the owner chooses it (§4.7).
* **Nothing half-made can run.** A group nobody can resolve to, or a service
  account swarm-api does not admit, holds no capacity and runs nothing until
  A9 has read every object back (§4.8).
* **No frozen-contract change** in the recommended design (§5).

---

## 1. The problem, as measured

### 1.1 A new service tenant took four steps, three of them the owner's

Read on 2026-10-19 from #847 and from the `u-or-9a0792` entry in
`terraform/environments/dev/dev.tfvars`. Making Oracle's service tenant
`u-or-9a0792` for `oracle-api@saga-agents-staging.iam.gserviceaccount.com`
(#1568) took these steps:

1. **A `dev.tfvars` entry**, keyed by the id the frozen
   `identity.tenant_id_for_user` computes (lossy and too long, so it carries a
   digest). A test recomputes that id (`tests/unit/scripts/test_oracle_api_tenant.py`).
   Nobody types it in from memory. The entry also needed an `ALLOWED_USERS`
   line in `terraform/infra/locals.tf`, because a service account's domain is
   never `saga.xyz`.
2. **A bootstrap IAM apply** by the owner. It added
   `serviceAccount:oracle-api@…` to `frontend_iap_members` in
   `terraform/bootstrap/terraform.tfvars`. That grants
   `roles/iap.httpsResourceAccessor` on **both** IAP backends
   (`google_iap_web_backend_service_iam_member.frontend_accessors`,
   `terraform/bootstrap/wif.tf`). It also made the deployer's per-account grant
   for the new worker account (`deployer_service_accounts.tf`).
3. **An IAM release**, approved in `dev-iam`, which created the account, its
   grants, its secret, its tenant and pool documents and its Cloud Run jobs.
4. **`kubernetes/apply.sh` by hand**, because releases do not apply tenant
   namespaces (`APPLY_TENANT_NAMESPACES` is off). That is #1590.

`docs/multi-tenancy.md` ("Each service tenant has to be admitted in three
places, and the three must agree") records that the front door, swarm-api's
domain check and the tenant entry must agree. Today a person keeps them in
step, across a public file, a private apply and a release.

### 1.2 A group tenant takes the same steps, and one more

A group tenant (`eng`, `smoke`) is a `dev.tfvars` entry with
`directory_group = true`. Terraform turns that into `TENANT_GROUPS`, which
swarm-api reads at start (`settings.py::ApiSettings.tenant_groups`). So a new
group needs a release to reach the tenant-resolution code at all, then the
namespace by hand. `smoke`'s comment in `dev.tfvars` records the failure this
invites. Its first principal, `swarm-smoke@saga.xyz`, did not exist. A
registered group whose lookup fails 503s every caller who is in no higher
group, by design.

### 1.3 A personal workspace already works this way

Since 2026-10-11 a person's workspace has been made in the cloud. The person
requests it, an admin approves, and `swarm-workspace-apply` runs
`register-tenant.sh --workspace`, steps A1 to A9 (`docs/workspaces.md` §2.2).
It takes no tfvars entry, no release and no hand apply. The owner's w-752763
read `verified: 25 of 25 objects` on 2026-10-11 (#847). So the question is
not whether an in-cloud path can work. It is what the group and service kinds
need that a person does not:

* a group needs **registering**: swarm-api must start treating a Google group
  as a tenant. Today only a deploy-time list does that;
* a service needs **admitting**: past IAP at the front door, and past
  swarm-api's domain check;
* a group's id has **no `u-` prefix**. Every bound on the deployer today is
  written against `swarm-tenant-u-` (§2.1). That is the hard part.

### 1.4 What "done" lets someone do that they cannot today

* An admin opens Admin › Tenants, clicks **New workspace**, chooses
  **Service**, types `reports-bot@saga-agents-staging.iam.gserviceaccount.com`
  and submits. The platform owner approves it in the same screen. A few
  minutes later the row reads `ready`, and the bot's first `POST /v1/tasks`
  is admitted in its own tenant. No tfvars line, no bootstrap apply, no
  release and no `kubernetes/apply.sh` were needed.
* An admin chooses **Group**, types `research@saga.xyz` and submits. Members
  of the group see `research` in their workspace switcher once it is `ready`.
  Their work runs under research's own account, GCS prefix and namespace.
* An admin chooses **Personal**, types `carol@saga.xyz` and submits (if Q1
  answers yes). Carol signs in for the first time to a ready workspace.
* Before `ready`, none of them can run anything, and nothing of theirs holds
  capacity.

---

## 2. Constraints that bind the design

### 2.1 Facts no design can change

* **The deployer's bounds are written against the `u-` prefix.** Four of
  them are:
  * `swarmWorkspaceSecretBinder`'s condition,
    `resource.name.startsWith("projects/<number>/secrets/swarm-tenant-u-")`
    (`terraform/bootstrap/workspace_deployer.tf`, `workspace_deployer_conditions.secret_binder`);
  * the admission policy, which admits only a `Namespace` matching
    `^swarm-tenant-u-[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, and namespaced objects
    only inside one (`kubernetes/policies/workspace-provisioner-scope.yaml`);
  * swarm-api's personal forge-slot grants, `forge_personal_any`
    (`terraform/bootstrap/forge_user_slots.tf`);
  * the browser-login revoker's condition (`browser_logins.tf`), which this
    design does not touch.

  A **service** tenant's id comes from `tenant_id_for_user`, so it is in the
  `u-` namespace (`u-or-9a0792`) and fits every one of these bounds as it
  stands. A **group** tenant's id is the bare slug of the group's local part
  (frozen `identity.tenant_id_for_group`: `eng`), so it fits none of them.
  Widening them is the cost of the group kind. §3 compares the ways to pay
  it.
* **IAM cannot narrow service-account administration by name** (#334,
  `docs/workspaces.md` §2.4). The deployer's `swarmWorkspaceAccountAdmin` is
  project-wide and bounded only by the call guard, as accepted on 2026-10-08.
  This design adds no power there. A group's or a service's worker is one
  more `swarm-agent-worker-<tenant>` account, created by rule C2 and alerted
  on like the rest.
* **IAP names a backend by project number and numeric id.**
  `terraform/modules/frontend/main.tf` ("Who may pass IAP") records that a
  `resource.name` condition written another way "matched nothing", and
  release 35972131246 failed on it. It also records that `roles/iap.admin`
  bound directly on the two backends still returned 403 on `getIamPolicy`. A
  front-door grant must therefore be a project-level binding whose condition
  names the backend as `projects/<number>/iap_web/compute/services/<id>`.
  It is **not yet verified live** (§4.5, owner step O1).
* **`searchTransitiveGroups` returns 403 in this project** (CONTRACT.md,
  CLAUDE.md). swarm-api never lists a caller's groups. It asks "is this caller
  in THAT group?" with `checkTransitiveMembership`, one registered group at a
  time, and every call sends `x-goog-user-project`
  (`apps/swarm-api/swarm_api/groups.py`). Reading a group at all also needs
  the domain-wide delegation swarm-api holds (`dev.tfvars`, `eng`'s comment).
  **The deployer has no Cloud Identity access and must never get any**, so
  every directory question in this design is swarm-api's.
* **A failed lookup of a registered group is fatal, by design.**
  `Authenticator` raises `UpstreamUnavailable` when a failed lookup could have
  changed the answer (`auth.py`, the `GroupLookupError` branch), because a
  misfiled task cannot be recovered. A new registered group adds that failure
  mode for every caller below it in priority.
* **A group slug can collide.** `tenant_id_for_group` slugs the local part
  only, so `eng@saga.xyz` and `eng@other.example` share `eng`. A group
  `u-eng@saga.xyz` derives `u-eng`, the id of `eng@saga.xyz`'s personal
  workspace (`store.py::assert_tenant_scope`). One tenant's user-slot names can
  also start with another's (`eng` and `eng-git-u-x`), which
  `forge_nested_tenants` refuses for Terraform tenants only.
* **`frontend_iap_members` is written additively.** `frontend_accessors` is a
  `google_iap_web_backend_service_iam_member`, so a bootstrap apply never
  removes a member the job added. Nothing else writes those policies. The
  release deployer holds no IAP role (`modules/frontend/main.tf`).
* **The job takes two arguments and nothing else.** The Workflows definition
  (`terraform/bootstrap/workspace_dispatch.tf`) and `entry.py` let through only
  `^w-[0-9a-f]{6}$` and the mode. The kind is therefore read from the record
  by A1. It never rides in the message, so this design changes nothing in
  dispatch.
* **Secrets are user-scoped** (owner decision 2026-10-11,
  `docs/design/user-scoped-secrets.md`). A person's credential is used only on
  that person's tasks. The broker that enforces this inside a group tenant is
  built and switched off (`FORGE_BROKER`). Until it is on, a group worker
  reads its members' slots through `forge_slot_reader`, which Terraform grants
  per `var.tenants` key, so a group made outside Terraform has no such grant.

### 2.2 The CONTRACT.md invariants it touches, and how each is kept

* **1. Only LEASED/DISPATCHED/STARTING/RUNNING cost anything.** A workspace
  record in any state creates no task, no lease and no pod. A group that is
  not `ready` is in no caller's `tenant_choices`, and a service account whose
  record is not `ready` is refused at authentication. So no submission can
  reach a tenant that is still being built, and nothing waits on a build. The
  job is a Cloud Run job execution, not a task, and holds no pool capacity.
  The pool document A8 writes holds `capacity_units` as a ceiling, which is
  not a reservation. **Kept.**
* **2. All-or-nothing reservation.** Admission (`swarm_common/admission.py`)
  is not touched. A8 writes the tenant and pool documents with the record's
  limits, as it does for a person. A new tenant's first reservation is the
  same single transaction across every pool. **Kept, untouched.**
* **3. Concurrency counts from LEASED.** Untouched.
* **4. Workers never sleep through a provider wait.** Untouched. The job is
  not a worker. It retries a transient cloud error three times (4, 16 and 64
  seconds) inside a 1800-second deadline, and anything longer ends the run
  `failed` and retryable (§4.8).
* **5. Fencing.** Untouched. Worker routes, attempt proofs and generations do
  not read the workspace record. A tenant made by the job is fenced exactly
  like one made by Terraform, because fencing is per task, not per tenant.
* **6. Spot is disabled.** A7 renders the tenant namespace with
  `kubernetes/render.py`, the same render as today. Nothing here selects a
  node class.
* **7. `requests == limits`.** The same render, the same `LimitRange`, and
  the quota from the record's limits (§8 of `docs/workspaces.md`). **Kept.**
* **8. Checkpoints.** Untouched. A group or service workspace's work
  checkpoints like any other.
* **9. Per-tenant isolation.** This is the invariant the design exists to
  extend to two more kinds. Each of them gets:
  * **its own GSA**, `swarm-agent-worker-<tenant>`, created by A3. A2's squat
    check refuses an existing account with foreign bindings or a key
    (`IDENTITY_NOT_OURS`);
  * **its own secrets**, named through the frozen `Tenant.secret_name`, with
    the worker bound per secret by A6 to its own slot only (§4.6);
  * **its own GCS prefix**, `tenants/<tenant>/`, by A5's two string-equal
    conditioned grants;
  * **its own namespace**, `swarm-tenant-<tenant>`, by A7, which also closes
    #1590 for every new workspace.

  Two new risks are closed by refusals, not trust:
  * **a group that would derive someone else's id** (a `u-` slug, a slug equal
    to or nested in an existing tenant's) is refused at the request
    (§4.2.3);
  * **a service account that already resolves somewhere** (a registered
    group's member, a `service_accounts` listing, a platform account, an
    account on the shared deny-list) is refused at the request and again in
    S1 (§4.3).

  The widened namespace bound (§4.3, A7) is held by a label only the job
  writes on CREATE. So the deployer can still never write into `eng`'s,
  `smoke`'s or any hand-made namespace. **Kept.**
* **10. Callers pick a `runner_profile` by name.** `POST /v1/admin/workspaces`
  takes a kind, a principal, a display name and the four limit numbers. It
  takes no image, command, resource spec or backend parameter. The limits are
  the same four numbers People already edits (`limits` in §1.1 of
  `docs/workspaces.md`), each bounded by validation, and they become a
  namespace quota and a pool ceiling, not a pod spec. **Kept.**

### 2.3 What it needs from `apps/common/swarm_common/`

Nothing, in the recommended design (§5):

* `Tenant.kind` stays `"group" | "user"`. A service tenant is `kind = "user"`
  with a service-account principal, exactly as `u-sw-c90291` and `u-or-9a0792`
  are today. The record's `kind` is swarm-api's, not the contract's.
* `tenant_id_for_group`, `tenant_id_for_user`, `worker_service_account_id`
  and `resolve_tenant(principal, group_priority)` are called as they are. The
  priority tuple swarm-api passes gets longer (§4.4). Its type and meaning do
  not change.
* Design C (§3.3) would need a change to `tenant_id_for_group`. That is
  written out as a conditional request in §5, so the owner can see its cost.

---

## 3. Designs that could work

All three keep personal workspaces exactly as they are, the same job, the
same record and the same call guard. They differ in how a group's id meets
the deployer's bounds.

### 3.1 Design A: one job, every kind, with the `u-` bounds widened behind a job-only marker (recommended)

The record gains `kind`. The job's steps are A1 to A9 for every kind, plus S1
and S2 for a service (§4.3). For a group, three bounds are widened, and each
is narrowed again by something only the job can satisfy:

* **The namespace policy** admits a `Namespace` `swarm-tenant-<id>` for any
  id. It does so only on CREATE, and only when the namespace carries
  `swarm.saga.xyz/workspace: w-<6 hex>`. An UPDATE is allowed only when the
  old object already carries that label. A namespaced object is allowed only
  when `namespaceObject` carries it. `eng`'s, `smoke`'s and every hand-made
  namespace lack the label, so the deployer can never touch them. The policy
  also refuses, by name, the namespaces of the tenants `terraform/infra`
  declares, so a missing `swarm-tenant-eng` cannot be created with the label.
  The `swarm-tenant-u-*` arm stays as it is for personal namespaces made
  before the label existed (w-752763's, w-7f9285's).
* **The secret binder** gains a second conditioned binding:
  `resource.name.startsWith("projects/<number>/secrets/swarm-tenant-") &&
  (resource.name.endsWith("-git") || resource.name.endsWith("-git-refresh"))`.
  That is a team forge slot and its twin, of any tenant, and nothing else: no
  provider key, no `-wsec-`, no user slot. Rule C6 of the guard admits only
  the expected names. A new alert pages on any `SetIamPolicy` by the deployer
  on a secret of a `var.tenants` key.
* **swarm-api's user-slot grants** (version adder, version manager and
  refresh reader) gain one any-tenant binding each,
  `resource.name.extract("/secrets/swarm-tenant-{t}-git-u-") != ""`. swarm-api
  already holds these on every Terraform tenant's prefix and on every
  personal one, so the union only adds tenants that do not exist yet. This is
  needed only for Q6 (a), the broker.

| | cost |
|---|---|
| **code** | swarm-api: the record's kind, one admin route, the registry read in `auth.py`. Job: S1, S2, kind-aware A6/A8/A9, new guard rules. Console: one dialog and one button. About 9 lanes (§6) |
| **migration** | none for the four Terraform tenants, if they stay (Q8) |
| **latency** | today's job run: minutes, with a 1800 s cap. From request to `ready` is one approval plus that |
| **money** | one job execution per workspace, a few minutes of Cloud Run CPU (cents). No standing resource. An IAP binding is free |
| **breaks** | nothing that exists. The widened bounds are owner-approved bootstrap and policy changes, each with a test. The residual risks are in §4.5 and §4.9 |

### 3.2 Design B: personal and service in the job; groups stay in Terraform

Service tenants fit every `u-` bound as they stand, so the job learns only S1
and S2. Groups keep WD3: a tfvars entry, a release and the namespace.

| | cost |
|---|---|
| **code** | about two thirds of A: no namespace-policy change, no secret-binder widening, no registry for groups. The service admit in `auth.py` is still needed |
| **migration** | none |
| **breaks** | **the owner's request.** "All three kinds … with no CI, no operator laptop and no Terraform". A new group still takes a public file, a release and #1590's hand apply. It is offered as a fallback if the owner rejects A's widening (Q3) |

### 3.3 Design C: group workspaces made by the job get a `g-` prefixed id

A job-made group's tenant id would be `g-<slug>`, so every bound becomes a
fixed prefix again (`swarm-tenant-g-`), with no marker label.

| | cost |
|---|---|
| **code** | A's code, plus a change to the frozen `identity.tenant_id_for_group` (or a second derivation beside it) and a registry lookup of which groups take the prefix. Every reader of a group's id (scheduler, reconciler, worker, UI) is reviewed against two spellings |
| **migration** | none for `eng` and `smoke`, which keep their bare ids |
| **breaks** | the rule that a group's id is a pure function of its email. That rule underlies `resolve_tenant`, the collision check and every test that recomputes an id. Two groups whose slugs differ only by the prefix are a new collision class. It is a contract change (§5, request WK-C), for bounds that A gets with one label |

### 3.4 Recommendation: A

A is the only design that meets the request in full without a frozen-contract
change. Its widening is narrowed again by a label the job writes on CREATE
and nothing else can add to an existing namespace. The widening is also
bounded by suffix (`-git`) on secrets, and alerted on. B is the safe fallback
if the owner judges the widening too much (Q3). C buys tidier prefixes at the
price of the one derivation every component trusts.

---

## 4. The recommended design, in enough detail to build

### 4.1 The record: `workspaces/{tenant_id}`, extended

The collection, its states (§1.2 of `docs/workspaces.md`), `workspace_ids/`,
the opaque `w-` id and the privacy rule are unchanged. New fields:

| field | type | written by | meaning |
|---|---|---|---|
| `kind` | string | swarm-api | `personal`, `group` or `service`. **Absent reads as `personal`**, so every existing record is unchanged and needs no backfill |
| `principal` | string | swarm-api | personal: the person's email, as today. group: the group's email, lower-cased. service: the service account's email, lower-cased. Always from the request after validation, never derived from a header |
| `requested_by` | string | swarm-api | the admin who filed it. For a personal request the person makes, the person. Never leaves Firestore |
| `display_name` | string | swarm-api | at most 60 characters, shown in the switcher and Admin › Tenants. Defaults to the group's or account's local part |
| `directory` | map, group only | swarm-api | `{checked_at, readable: true}`, written by the directory check at the request and again at approval (§4.2.3). A1 refuses a group record whose check is older than 24 hours (`DIRECTORY_CHECK_STALE`, retryable: an admin's retry re-checks) |
| `service` | map, service only | the job | `{unique_id, front_door: {backend, admitted_at}}`. S1 writes `unique_id`. S2 writes `front_door`. A9 re-reads both |

The **document id** is the tenant id for every kind:
`tenant_id_for_user(email)` for personal and service, and
`tenant_id_for_group(group)` for group. That keeps the rule that a record is
keyed by the only id its principal can resolve to.

**Step ids** on `steps` gain `S1` and `S2` (§4.3). The console draws whatever
step ids the record carries, in the order the API returns
(`docs/workspaces.md` §6.4), so a new id needs no UI change beyond its label.

### 4.2 The flow: Admin › Tenants › New workspace

#### 4.2.1 Who may request and who may approve each kind

| kind | who may request | where | who may approve | automatic approval |
|---|---|---|---|---|
| **personal** | the person (Setup, `/sc:setup`), as today; **and**, if Q1 is (a), any admin, naming the person | Setup checklist; Admin › Tenants › New workspace › Personal | any admin (`is_admin`) | an admin's own request, as today (§1.3 of `docs/workspaces.md`). An admin's request **for someone else** is approved in the same transaction too: the requester is an admin |
| **group** | any admin | Admin › Tenants › New workspace › Group | any admin | yes, the requester is an admin (the same `People._approve_in`) |
| **service** | any admin | Admin › Tenants › New workspace › Service | **the platform owner only** (`is_owner`), if Q2 is (a) | only when the owner is the requester |

Why service is different: it is the only kind that **admits a new principal
at the front door**. A personal or group workspace admits nobody new. The
domain check already admits every `saga.xyz` person, and a group only selects
among people who are already admitted. A service account is outside the
domain, so its workspace widens who can reach swarm-api at all. That is the
same posture decision the owner makes today when they apply
`frontend_iap_members`. Q2 keeps it theirs.

#### 4.2.2 The API

All routes are additive and need `is_admin`, except approve for a service
(`is_owner`). They are audited in `admin_audit` in the same transaction as
the write, as People's are.

`POST /v1/admin/workspaces`

```json
{
  "kind": "service",
  "principal": "reports-bot@saga-agents-staging.iam.gserviceaccount.com",
  "display_name": "Reports bot",
  "limits": {"max_active": 4, "capacity_units": 8}
}
```

* `kind` is required, one of the three.
* `principal` is required. Its shape is checked per kind (§4.2.3).
* `display_name` is optional.
* `limits` is optional and partial. It defaults per kind: personal §8's 8/8;
  group 8/8; service 4/8, the pattern of `u-sw-c90291` and `u-or-9a0792`. An
  extra field is 422.

Responses:

* **202** with the record view (`workspaces.py::view`, extended with `kind`,
  `principal` and `display_name`) when a record is created. When the
  requester may also approve, it is created already `approved`.
* **200** with the record when one already exists for that tenant id in
  `requested`, `approved`, `applying`, `needs_owner` or `ready`. This is the
  same idempotency as `POST /v1/workspace`.
* **409** `WORKSPACE_FAILED` with the failure copy when the record is
  `failed`. The answer is Retry, as today.
* **403** with one of the codes in §4.2.3.

`GET /v1/admin/workspaces?kind=group|service|personal&state=…`. Records,
newest first, at most 200. Each row is the view plus `kind`, `principal` and
`display_name`. Admin › Tenants reads this for the rows that are not yet a
tenant. It sends no email in a URL.

`GET /v1/admin/workspaces/{workspace_id}`. One record, for the progress view.

`POST /v1/admin/workspaces/{workspace_id}/approve | deny | retry`. These are
today's routes (`routes/people.py`). They accept every kind. Approve of a
`service` record answers 403 `WORKSPACE_APPROVAL_OWNER_ONLY` to a caller who
is not the owner. Retry re-runs the group's directory check before it
publishes.

`PUT /v1/admin/workspaces/{workspace_id}/limits`. Today's route, any kind. It
publishes `--mode limits`, which runs A1, A7 and A8 only.

`GET /v1/admin/tenants` (`routes/admin.py`) gains `kind` and `workspace_id`
on each tenant that has a record. Admin › Tenants can then show which
tenants the job made and which Terraform made.

**Every new refusal ships report-only**, as `refusals.py` requires
(`tests/unit/control_plane/test_new_refusals_ship_off.py`), except the ones
on the new route. A new route has no existing caller to break, so its
refusals are on from the first build, and they are listed in
`NOT_SWITCHABLE_CODES` with that reason.

#### 4.2.3 What a request must pass, per kind

These checks are swarm-api's, in `POST /v1/admin/workspaces`, before any
write. The job repeats every one it can (§4.3), because swarm-api writes
`workspaces/`, so its checks alone are not a bound on the deployer.

**Every kind**

1. The tenant id is not taken by a different principal or kind. This is
   `Store.assert_tenant_scope`'s collision check, unchanged:
   `WORKSPACE_ID_TAKEN`.
2. The id is not a `var.tenants` key. The Terraform tenants are known to
   swarm-api through `TENANT_GROUPS` and `ALLOWED_USERS`, and the
   tenant documents carry `managed_by = "swarm-terraform"`. Otherwise:
   `WORKSPACE_ID_TERRAFORM`.
3. **The forge-slot nesting rule**: no existing tenant id `s` with
   `<id>` starting with `s-git-`, or the reverse
   (`forge_user_slots.tf::forge_nested_tenants`, applied over every
   `tenants/` id rather than `var.tenants` only). Otherwise:
   `WORKSPACE_ID_NESTED`.

**Personal (requested by an admin, Q1 (a))**

4. The email is in an allowed domain (`assert_allowed_domain`) and is not a
   service account (`WORKSPACE_NOT_FOR_SERVICE_ACCOUNTS`), as today's rules 1
   and 2.
5. It is not a secret-admin principal (`WORKSPACE_PRINCIPAL_FORBIDDEN`), as
   today's rule 3.

**Group**

6. The address is a group in an allowed domain. Its slug must not start with
   `u-` (`WORKSPACE_GROUP_ID_RESERVED`): a `u-` id is a person's, and
   §2.1's collision would hand it to the group.
7. **The directory check.** swarm-api asks Cloud Identity
   `checkTransitiveMembership` for **the requesting admin** in **that one
   group**, with `x-goog-user-project`, through `groups.py::CloudIdentityGroups.is_member`.
   Any answer, true or false, proves the group exists and is readable under
   swarm-api's delegation. A lookup error is `WORKSPACE_GROUP_UNREADABLE`
   (503-shaped copy, retryable), and nothing is written. No call lists
   groups: `searchTransitiveGroups` 403s here. The check is repeated at
   approval and at every retry, and its time is stored in `directory`.
   Without it, a group that later fails to resolve would 503 every caller
   below it (§2.1).
8. The group is not an admin group (`ADMIN_GROUPS`), because an admin group
   as a tenant would mix the two lookups `auth.py` keeps apart:
   `WORKSPACE_GROUP_IS_ADMIN`.

**Service**

9. The address matches the frozen service-account pattern
   (`identity.py`) **and is in the platform's project** (Q7 (a)):
   `WORKSPACE_SERVICE_NOT_IN_PROJECT`.
10. It is not a platform account. The account id must not start with
    `swarm-` (this covers `swarm-api`, `swarm-scheduler`, `swarm-reconciler`,
    `swarm-workspace-deployer`, `swarm-workspace-dispatch`, every
    `swarm-agent-worker-*` and `swarm-verify`), and it must not be the
    project's default compute or Cloud Build account:
    `WORKSPACE_SERVICE_PLATFORM`.
11. It is not a `service_accounts` listing of any tenant, and not in
    `ALLOWED_USERS`. Either already resolves it somewhere:
    `WORKSPACE_SERVICE_ALREADY_RESOLVES`.

The **shared deny-list** is not restated in Python. S1 enforces it in the job
through `scripts/lib/common.sh`'s `SHARED_DENY_LIST`, the one place it lives
(rule 2). A request naming an account on it is accepted by swarm-api and then
stopped by S1 as `needs_owner` before any write. A second copy of the list in
swarm-api is the copy that would drift (CLAUDE.md, "Writing scripts").

#### 4.2.4 The console

Admin › Tenants (`TenantsScreen`, in `apps/swarm-ui/src/Activity.tsx`) gains:

* a **New workspace** button, admin only, which opens the dialog below;
* **a section above the roster, "Being created"**, listing records from
  `GET /v1/admin/workspaces` whose state is not `ready`. Each row shows kind,
  display name, `w-` id, state, and the live steps (the same step list People
  draws), with Approve, Deny and Retry where the caller may use them.
  `needs_owner` shows the stopped step, word for word from the API.

The dialog (`apps/swarm-ui/src/NewWorkspace.tsx`, new) has three tabs, one
per kind. Each takes one address field and an optional display name. Each
shows, before submit, what the kind will get:

> **Service** · `reports-bot@…` gets its own tenant, worker account, storage
> prefix and namespace, and is admitted at the API's front door. **The
> platform owner approves it.** It runs nothing until a Claude account is lent
> to it or added.

On submit it posts once and then shows the record's progress, polling `GET
/v1/admin/workspaces/{id}` every 5 s while not `ready`. Polling stops when
the dialog closes. This is the poll idiom `Onboarding.tsx` already uses.

### 4.3 The job's steps, per kind

`scripts/register-tenant.sh --workspace <w-id> --mode create`. A1 reads
`kind` from the record and the expectation file (§2.5 of
`docs/workspaces.md`) carries it. Every other step reads the kind from the
expectation file, never from an argument.

| id | personal | group | service |
|---|---|---|---|
| **A1** claim | as today | as today, plus: refuse a `directory.checked_at` older than 24 h (`DIRECTORY_CHECK_STALE`) | as today, plus: refuse unless `decision.by` is the platform owner when Q2 is (a). A1 reads the owner from the job's fixed environment (`PLATFORM_OWNER`, written into `entry.py`'s environment by the bootstrap). It does not read it from the record |
| **S1** check the account | — | — | `gcloud iam service-accounts describe <principal>`, which the deployer's `.get` already allows. It must exist, be enabled, be in this project, carry no `swarm-` id, and not be on `SHARED_DENY_LIST`. Its `uniqueId` is recorded. **The job never writes this account's IAM**: it is the caller, not the worker. Any failure is `needs_owner` with `SERVICE_ACCOUNT_REFUSED` |
| **A2** name free | as today | as today | as today |
| **A3** account | `swarm-agent-worker-<tenant>` | the same | the same |
| **A4** its own IAM | Workload Identity for the namespace's two KSAs, act-as for scheduler and reconciler | the same | the same |
| **A5** bucket | `tenants/<tenant>/` grants and marker | the same | the same |
| **A6** forge slot | the person's App slot `-git-u-<hash>` and twin | the **team** slot `swarm-tenant-<g>-git` and twin, empty (§4.6) | the team slot `swarm-tenant-<t>-git`, empty; no twin (§4.6) |
| **A7** namespace | `kubernetes/apply.sh`, as today | the same, rendered with `swarm.saga.xyz/workspace: <w-id>` | the same, with the label |
| **A8** documents | `tenants/<t>` `kind=user`, pool | `tenants/<g>` `kind=group`, `principal=<group>`, `directory_group=true`, pool | `tenants/<t>` `kind=user`, `principal=<service account>`, pool |
| **S2** front-door admit | — | — | add `serviceAccount:<principal>` as `roles/iap.httpsResourceAccessor` on the API backend (§4.5). Read-before-write: a member already present is done |
| **A9** verify | as today | as today, plus the namespace label and `directory_group` | as today, plus the IAP member read back, and `service.unique_id` unchanged |

**Shared by every kind:** A1 to A9, the guard, the retries, the progress
writes and the restricted log. **A7 for every kind is what closes #1590 for
new workspaces.** No workspace the job makes can be `ready` without its
namespace, because A9 reads every object `kubernetes/render.py::TENANT_FILES`
produces.

**Order matters for the service kind.** S2 is the last write, after A8 and
before A9. Even so, the account is not admitted by swarm-api until the record
is `ready` (§4.4). The front door is the outer gate and swarm-api's check is
the inner one, and the inner one opens last.

**New guard rules** (`scripts/lib/workspace-calls.json`):

* **C10**: `gcloud iap web add-iam-policy-binding --resource-type=backend-services
  --service=<the API backend>` with role `roles/iap.httpsResourceAccessor`
  and member exactly `serviceAccount:<the expected principal>`. Only for a
  `service` expectation. Its read twin `get-iam-policy` goes under C1. Any
  other role, member, backend or resource type stops the run.
* **C6** gains the team slot names for group and service expectations.
* **C7** gains the label check for group and service expectations.
* **C9** is unchanged. No removal, ever, including `iap web
  remove-iam-policy-binding`.

Each new shape gets an allowed and a refused case in
`scripts/lib/workspace-guard-cases.json`. The walk
(`test_workspace_deployer_walk.py`) maps C10 to its permission and its
condition.

### 4.4 Registering a group, and admitting a service, in swarm-api

**A new module, `apps/swarm-api/swarm_api/registry.py`**, reads the `ready`
records of kind `group` and `service` (two equality queries on `workspaces`,
cached 60 s). It answers two questions:

* `registered_groups() -> tuple[str, ...]`: the group emails, in `ready_at`
  order;
* `admitted_services() -> frozenset[str]`: the service-account emails.

**`auth.py` uses them in exactly two places:**

1. **Tenant groups.** Everywhere `self._settings.tenant_groups` is read
   today (`tenant_candidates`, `resolve_tenant`, `_tenant_principal`,
   `_tenant_choices`), it reads `tenant_groups + registered_groups()`. That
   is `TENANT_GROUPS` first, in their admin order, then the job's groups.
   * Membership is still asked **one registered group at a time**, through
     `groups_for` and `checkTransitiveMembership`, with
     `x-goog-user-project`, and cached per (caller, group) for 120 s. Each new
     group adds one lookup per caller per cache window. Nothing lists a
     caller's groups.
   * A job-made group therefore becomes a member's **default** tenant when
     they are in no Terraform group above it. That is today's rule for any
     registered group. A default the person chose still comes first
     (`docs/multi-tenancy.md`). Q4 asks whether the owner wants that, or
     wants a job-made group selectable only.
   * A failed lookup of a job-made group is fatal exactly as for a Terraform
     group (Q5 (a)), for the reason `auth.py` gives: a misfiled task cannot
     be recovered.
2. **Admission of a service account.** Beside the `ALLOWED_USERS` check: an
   email in `admitted_services()` is admitted, and its domain is taken from
   the address, exactly as `ALLOWED_USERS` does. A service account with a
   record that is **not** `ready` gets 403 `WORKSPACE_NOT_READY` with the
   record's state, instead of the generic "not in an allowed domain" message.
   Its tenant resolves through today's rule, `tenant_id_for_user` of its own
   email. It is in no registered group (check 11), so no `X-Swarm-Tenant` can
   select anything else.

**If the registry read fails**, swarm-api keeps serving from the last good
read for up to 10 minutes and logs `registry_stale`. With no good read since
start, an authenticated request is 503 `registry_unavailable`. Without the
registry, swarm-api cannot know whether a caller belongs to a job-made group,
so serving would risk a misfiled task, the same reasoning as the group-lookup
rule.

**Nothing in `TENANT_GROUPS` or `ALLOWED_USERS` changes.** The Terraform
tenants keep working if the registry is empty, and a release that ships the
registry before any job-made workspace exists changes nothing.

**The scheduler, the reconciler and the worker read nothing new.** They see
a tenant document and a pool, as they do for a person's workspace.
`GkeJobDispatcher.namespace_for` prefers `Tenant.namespace`. A8 writes the
canonical `swarm-tenant-<tenant>`, the spelling #847's 2026-10-11 comment
found and corrected for `u-bogdan`.

### 4.5 The front-door admit: how narrowly the deployer may grant it

**The new IAM rows**, each an owner-approved bootstrap change
(`terraform/bootstrap/`) behind one switch, `enable_workspace_kinds`, off by
default:

| # | role | to | on | condition | why |
|---|---|---|---|---|---|
| **K1** | custom `swarmFrontDoorAdmitter`: `iap.webServices.getIamPolicy`, `iap.webServices.setIamPolicy` | `swarm-workspace-deployer` | the project | `resource.name == "projects/<number>/iap_web/compute/services/<id of the API backend>"`. **Exact equality, by number**: `<number>` from `data.google_project`, `<id>` from `data.google_compute_backend_service.frontend_iap["<api backend>"].generated_id`. With O1's proof, `&& api.getAttribute("iam.googleapis.com/modifiedGrantsByRole", []).hasOnly(["roles/iap.httpsResourceAccessor"])` | S2. The API backend only, never `-ui-backend`. A service calls the API, and the console is for people |
| **K2** | `swarmWorkspaceSecretBinder` (exists), a **second** binding | `swarm-workspace-deployer` | the project | `resource.name.startsWith("projects/<number>/secrets/swarm-tenant-") && (resource.name.endsWith("-git") \|\| resource.name.endsWith("-git-refresh"))` | A6 for a group's team slot. A service's `u-` slot is already inside the existing binding |
| **K3** | the three forge-slot roles swarm-api holds (`forge_slot_version_adder`, `forge_slot_version_manager`, `forge_refresh_reader`), one any-tenant binding each | `swarm-api` | the project | `resource.name.startsWith("projects/<number>/secrets/swarm-tenant-") && resource.name.extract("/secrets/swarm-tenant-{t}-git-u-") != ""` | a job-made group's members' user slots, under Q6 (a). swarm-api holds the same per tenant today |
| **K4** | `swarmWorkspaceProjectReader` (exists) | unchanged | | | listed so the role-list validation in `terraform/bootstrap/variables.tf` is updated in the same change |

**Not granted:** any IAP role on `-ui-backend`, `roles/iap.admin`, any
`iap.web.*` or `iap.webTypes.*` permission (all backends), anything on the
other team's eight IAP backends, and any Cloud Identity permission.

**What still bounds it beyond IAM:**

* **The member.** IAM cannot restrict which member a `setIamPolicy` adds.
  Guard rule C10 admits exactly `serviceAccount:<the approved principal>`,
  and C9 refuses every removal.
* **The approval.** A1 refuses a service record not approved by the
  platform owner (Q2 (a)).
* **The audit.** Every admit is recorded three ways. swarm-api writes an
  `admin_audit` row at approval. S2 writes a `front_door` entry on the record.
  A new log alert in `terraform/modules/monitoring/workspace_alerts.tf` pages
  on **every** `SetIamPolicy` by `swarm-workspace-deployer` on an IAP
  resource, whoever it names. Admits are rare enough that each is read by a
  person.
* **The inner gate.** swarm-api admits the account only once its record is
  `ready` (§4.4). An IAP member alone gets a caller to swarm-api, not into it.

**Owner step O1, before K1 is applied (not agent work).** Check the three
things IAP has never been shown to accept here, on one throwaway binding to a
test service account, removed afterwards:

1. the permission names, read from `gcloud iam roles describe roles/iap.admin`;
2. that a project binding conditioned by the number-form name **matches**,
   meaning `getIamPolicy` succeeds on the API backend and fails on
   `-ui-backend`;
3. whether IAP evaluates `modifiedGrantsByRole`.

If (2) fails, K1 cannot be narrowed to one backend. S2 then becomes an
**owner step**: the job stops at `needs_owner` with `FRONT_DOOR_OWNER`, and
the owner adds the line to `frontend_iap_members`, as today. Everything else
in the flow still holds. This is Q3's fallback for the front door alone.

**Residual risk, stated so it is not mistaken for zero:**

* **R7. A stolen deployer token can rewrite the API backend's accessor
  list** for the token's life. It can widen it, for example to
  `allAuthenticatedUsers`. It can also narrow it, which removes
  `domain:saga.xyz` and locks everyone out. The alert pages on it at once.
  Widening admits nobody to swarm-api, whose domain, `ALLOWED_USERS` and
  registry checks are unchanged. Narrowing is an outage until the owner's
  bootstrap apply restores the members, because `frontend_accessors` is
  additive and re-adds its own. This is the same shape as §2.4 R2 of
  `docs/workspaces.md`, on one more resource.

**Amendment, 2026-10-19: R8, and the owner's decision to sign approvals
(lane WK-SIGNED-APPROVAL, part of #847).**

* **R8, as WK-JOB measured it.** Tenant workers hold Firestore create, get
  and update across the whole database (`terraform/modules/tenancy/main.tf`,
  "the residual is real"). So a worker can write `workspaces/{id}` with
  `decision.by` set to the platform owner. A1's checks are of *who the record
  names*: the image's `platform_owner`, and that address being `owner` in
  `admin_roles/` now. They are not proof that the owner acted. Once K1 is
  granted, the job would admit that record's service account at the front
  door. "The approval" in the list above therefore bounded nothing a worker
  could not write.
* **The decision.** swarm-api signs every approval and every retry, and the
  job verifies the signature before any step that grants.
  `enable_workspace_kinds` stays off until this is merged.
  * **The signed bytes.** RFC 8785 of `{purpose: "swarm.workspace-approval/v1",
    workspace_id, tenant_id, kind, principal, decided_by, decided_at,
    request_id}` (`apps/swarm-api/swarm_api/workspaces.py::canonical_approval`).
  * **The key.** The existing step-spec KMS key (`SPEC_SIGNING_KEY_VERSION`).
    Only swarm-api holds `signer` on it. The purpose sits inside the bytes, so
    no step-spec signature can be read as an approval, and no approval as a
    step spec.
  * **Where it is stored.** The record's `approval` map holds the signature,
    the key version, and the signed `decided_by`, `decided_at` and
    `request_id`. It is written in the same write as the decision
    (`People._approve_in`, `People.retry`). A KMS failure is a 503, and the
    record is not approved.
  * **A retry.** It runs a new request id, so it is signed afresh by whoever
    retried it. But a retry of a group or service record first verifies the
    record's stored `approval` against the platform's `SPEC_VERIFY_KEYS`
    (`workspaces.approval_refusal`, the job's own checks). It re-signs only a
    decision swarm-api signed before, over the record as it stands now. A
    worker can write `decision.by` and a waiting state, so a record "approved
    by" the owner that carries no valid signature is refused, as
    `WORKSPACE_WRONG_STATE` with `detail.reason: approval_unverified`. A
    retry is never how a forged decision gets signed (review of this lane,
    2026-10-19).
* **What the job checks.** `images/workspace-apply/entry.py` runs before step
  1, so before A1, S2 and every group or service step. It reads the record
  and rebuilds the payload from the record's own fields. It requires three
  things:
  * the signed request id is the record's;
  * the signed decider is the record's `decision.by` or `retry.by`;
  * the key version is a version of the key the image pins for the project
    (`SIGNING_KEYS`: dev's step-spec key).

  It then fetches that version's public key from Cloud KMS, never from the
  environment or the record, and verifies P-256 ECDSA with the standard
  library. A missing or bad signature, or a payload that is not the record,
  stops the run as `needs_owner` with `APPROVAL_UNSIGNED`. It also appends a
  `front_door_audit` entry with `admitted: false`. Both are one write,
  conditional on the record being the one read. Nothing is granted, because
  step 1 never runs. The remedy is a fresh decision, not a Retry: an admin
  (the owner, for a service) denies the stopped record, which `deny` accepts
  for a `needs_owner` record whose code is `APPROVAL_UNSIGNED`. They then
  approve it as the console shows it, and that approval is signed. A cloud that
  does not answer writes nothing and runs nothing.
* **What closes the race.** entry.py pins what it verified (workspace id,
  tenant id, kind, principal) in `SWARM_APPROVAL_PIN`, which is in its fixed
  environment. The guard's `expect` (`scripts/lib/workspace-guard.sh`)
  writes no expectation for a record that differs, or for a group or service
  the pin does not call verified. A1's claim re-reads the tenant, kind and
  principal inside its transaction. So a record swapped after the check grants nothing.
* **Personal records are not verified.** A person's steps never touch the
  front door, and approved personal records from before this change keep
  working. New personal approvals are signed all the same, so verifying them
  later is one entry in `VERIFIED_KINDS`. This is the lane's recommended
  answer in its questions to the owner.
* **The one grant this needs (owner, not agent work).** The job's identity,
  `swarm-workspace-deployer`, must hold `roles/cloudkms.publicKeyViewer` on
  dev's step-spec key (`terraform/bootstrap/spec_signing.tf`). Without it,
  every group or service run stops at the KMS read and writes nothing. That
  fails closed, and the lane's questions to the owner carry the Terraform.
* **Residual, stated so it is not mistaken for zero.** The signature proves
  that swarm-api approved the record as it stood at the click. A worker that
  rewrites a `requested` or `denied` record's principal *before* an admin
  approves it changes what gets signed. A retry no longer carries this risk:
  a principal rewritten after signing no longer verifies, and the retry is
  refused. The signature also does not cover `limits`, `directory` or
  `service.unique_id`, which `scripts/register-tenant.sh` reads. A worker can
  still edit those on a signed record before the job reads them. That risk
  predates this change and is not closed here. The console shows the
  principal on the row being approved, and the guard's S1 account rules and
  the page on every IAP `SetIamPolicy` still hold. A signature also carries no
  revocation. A denial drops the record's `approval`, but a worker that read
  the signed map before the denial can write it back together with the
  approved verdict. The job then runs what was once signed for that exact
  workspace, tenant, kind and principal, and nothing else. The same holds for
  re-running a workspace after it is taken down. Closing this needs a
  revocation the workers cannot write. It is not built here.

### 4.6 Secrets and forge slots for group and service workspaces

The 2026-10-11 decision stands: **a person's credential is used only on that
person's tasks.** So a group or service workspace gets team credentials, and
never a person's:

| | personal (today) | group | service |
|---|---|---|---|
| **forge, team** | — | `swarm-tenant-<g>-git` and `-git-refresh`, empty, the worker bound per secret by A6. An admin stores the token with `scripts/create-secrets.sh --stdin` or the tenant git-token route. **Never in a tfvars file, a Job environment or a log** (CLAUDE.md, owner rule 2026-09-25) | `swarm-tenant-<t>-git`, empty, worker bound by A6. Filled the same way, as `u-or-9a0792`'s is today. No refresh twin: an App refresh token belongs to a person |
| **forge, per member** | the person's App slot, made by A6 | `swarm-tenant-<g>-git-u-<hash>`, made by swarm-api on first use as for a Terraform group. **Read only through the credential broker** (Q6 (a)): the job cannot grant a group worker a prefix read on its members' slots without a per-tenant project binding, which Terraform makes and the job must not | none: a service account is not a person |
| **provider key** | none (WD6) | none. It runs on a lent pool account, or a key an admin adds through `POST /v1/tenants/me/credentials` | none, as `u-or-9a0792`: the owner lends it a pool account |
| **workspace secrets** (`-wsec-`) | yes | yes. `workspace_secrets.tf`'s condition already covers every `swarm-tenant-` name, and swarm-api releases them per attempt. No per-tenant IAM | yes, the same |
| **browser logins** | yes | no: they live only in a personal workspace (#1030) | no |

**Until `FORGE_BROKER` is on in an environment**, a member of a job-made
group has no readable user slot there. Their submissions use the team token,
and a submission that asks for the member's own credential is refused
`FORGE_USER_SLOT_NEEDS_BROKER`, a new refusal that ships report-only. Q6 asks
whether the owner would rather block the group kind until the broker is on.

### 4.7 The four Terraform tenants: `eng`, `smoke`, `u-sw-c90291`, `u-or-9a0792`

**Option (a), recommended: they stay in Terraform.**

* Nothing in their state, their `TENANT_GROUPS` or `ALLOWED_USERS` entries,
  or their `frontend_iap_members` lines changes.
* `terraform/infra/variables.tf` gains a validation that **`var.tenants`'s
  keys are a subset of these four**, with a comment saying why: a new tenant
  is the job's (this design). `personal_workspaces_absent.tftest.hcl`'s
  pattern holds it.
* swarm-api's checks 2 and 11 keep the job from ever making a record for one
  of their ids or principals.
* Their namespaces stay outside the job. #847 records Oracle's as applied by
  hand on 2026-10-19. This lane did not read the cluster for the other
  three. A rebuilt environment still needs `APPLY_TENANT_NAMESPACES` or a
  hand apply for these four, which is the part of #1590 this design does not
  close.

Why: they work, and they carry things the job never makes. `eng` and `smoke`
have authoritative per-provider secret bindings (`secret_manager` module),
Terraform-made Cloud Run jobs and scheduler jobs. `u-bogdan` alone was 33
infra instances and 5 bootstrap ones (W9). Moving them buys no capability and
costs two owner-reviewed plans per tenant.

**Option (b): migrate, with a plan that never destroys anything.** This is
W9's pattern, per tenant, one tenant per pull request:

1. Remove the key from `dev.tfvars`. In `terraform/infra/removed.tf` and
   `terraform/bootstrap/removed.tf`, `moved` each of its instances to an
   address nothing declares, then `removed { lifecycle { destroy = false } }`
   that address. Terraform 1.16 refuses an instance key in `removed`, which
   W9 checked on 2026-10-09. For `eng` and `smoke` this includes the
   `frontend_iap_members`-made bindings only if they are the tenant's.
   `domain:saga.xyz` stays.
2. Owner: the bootstrap plan, then the release's infra plan. Each must read
   **0 to add, 0 to change, 0 to destroy**, with only forgets. One exception
   is the Nearline `aged_prefixes` in-place change W9 met. Any destroy or
   replace naming the tenant is a stop.
3. Move registration into the registry in the same release. Write the record
   with `scripts/workspace-migrate-record.sh`, extended with `--kind` and
   keeping `migrated = true`, then drop the group from `TENANT_GROUPS` or the
   account from `ALLOWED_USERS`. The record must be `ready` before the env
   line goes, or every member resolves to their personal tenant for a
   release.
4. Relabel its Cloud Run jobs `managed-by=swarm-scheduler`, as W9 did. Then
   run `register-tenant.sh --workspace <id> --mode verify`, under the guard,
   which must read every object back.

Option (c) is the two service tenants only, which are the cheapest to move
(no group registration). It is in Q8.

### 4.8 Failure, retry and teardown

**The existing rule holds for every kind: nothing half-made can run.**

* **Personal**: the submission gate refuses until `ready`
  (`WORKSPACE_NOT_READY`).
* **Group**: the group is in no caller's `tenant_choices` until `ready`,
  because the registry reads only `ready` records. A half-made group is not a
  tenant anyone can name.
* **Service**: the account is not admitted by swarm-api until `ready`. If S2
  ran and A9 failed, an IAP member exists for an account swarm-api still
  refuses. That holds nothing and runs nothing.

**Retry** is an admin's action (the platform owner's, for a service under
Q2 (a)), from Admin › Tenants or People, as today. Every step reads before it
writes, so a re-run makes only what is missing. A group's retry re-runs the
directory check first.

**A guard stop** sets `needs_owner` with the step and the code. The owner's
path is `docs/workspaces.md` §2.5: read the refused call in the restricted
log, then run the script with the guard in `--report-only`, or deny.

**Deny after a partial apply** leaves what was made, and it is inert by the
rules above. The job never deletes (C9).

**Teardown** stays a separate, owner-approved run (`docs/workspaces.md` §7),
not built here. This design adds two things to its list for the new kinds:
remove the service account's IAP member (S2's inverse), and drop the group
from the registry, which is the record leaving `ready`. What can be done
immediately, without that run: an admin sets the tenant's `enabled = false`
(`tenant_for` then refuses new work). For a service, the record moves to the
reserved state `deprovisioned`, which removes the account from
`admitted_services()` at once. Q9 asks whether the owner wants the
deprovisioning mode in this build instead.

**New failure codes** (`docs/workspaces.md` §4.3, word for word in the
build): `SERVICE_ACCOUNT_REFUSED`, `DIRECTORY_CHECK_STALE`,
`FRONT_DOOR_OWNER`, `FRONT_DOOR_FAILED` (retryable), and
`NAMESPACE_LABEL_MISSING` (A9).

### 4.9 Changes by component, and the files each lands in

| component | change | files |
|---|---|---|
| swarm-api, record | `kind`, `principal`, `requested_by`, `display_name`, `directory`, `service`; per-kind validation; the admin request | `swarm_api/workspaces.py`, `swarm_api/people.py`, `swarm_api/routes/people.py`, `swarm_api/errors.py`, `swarm_api/refusals.py` |
| swarm-api, directory | a single-group readability check (`is_member` of the requester) | `swarm_api/groups.py` |
| swarm-api, resolution | `registry.py`; `auth.py` reads registered groups and admitted services; settings for the cache | `swarm_api/registry.py` (new), `swarm_api/auth.py`, `swarm_api/settings.py` |
| swarm-api, tenants list | `kind`, `workspace_id` | `swarm_api/routes/admin.py` |
| job | kind-aware A1, A6, A7, A8, A9; S1; S2 | `scripts/register-tenant.sh`, `scripts/lib/forge-slot.sh` (team slot without twin) |
| guard | C6, C7, C10 and the expectation's `kind`, `principal`, `front_door_backend` | `scripts/lib/workspace-guard.sh`, `scripts/lib/workspace-calls.json`, `scripts/lib/workspace-guard-cases.json`, `tests/unit/scripts/fixtures/workspace_guard_corpus.json` |
| image | `PLATFORM_OWNER` and the API backend's name in the fixed environment | `images/workspace-apply/entry.py` |
| render | the workspace label on job-made namespaces | `kubernetes/render.py`, `kubernetes/apply.sh` (passes `--workspace-id`) |
| cluster policy | the label arm and the refused Terraform names | `kubernetes/policies/workspace-provisioner-scope.yaml` |
| IAM | K1 to K4 behind `enable_workspace_kinds` | `terraform/bootstrap/workspace_deployer.tf`, `terraform/bootstrap/forge_user_slots.tf`, `terraform/bootstrap/variables.tf` |
| alerts | IAP change by the deployer; secret IAM by the deployer on a `var.tenants` key | `terraform/modules/monitoring/workspace_alerts.tf` |
| Terraform freeze | `var.tenants` keys ⊆ the four | `terraform/infra/variables.tf` |
| console | the dialog; Admin › Tenants' button and "Being created" | `apps/swarm-ui/src/NewWorkspace.tsx` (new), `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/types.ts`, `apps/swarm-ui/src/Activity.tsx` |
| docs | WD3's reversal, the three admission places, offboarding | `docs/workspaces.md`, `docs/multi-tenancy.md`, `docs/runbooks/tenant-offboarding.md`, this file |

`.github/CODEOWNERS` already names the owner on the guard, the script,
`common.sh`, `entry.py` and the Dockerfile (§2.4 safeguard 2 of
`docs/workspaces.md`). It needs no change, so the job lane gets the owner's
review by existing rule.

### 4.10 What this does not do

* **No plugin verb** to create a group or service workspace. Creating them is
  an admin's act in the console. `/sc:setup` keeps the personal request.
* **No change to the Terraform tenants** under Q8 (a).
* **No deprovisioning mode** under Q9 (a).
* **No acceptance probe that creates a real workspace.** Each would create a
  service account that nothing may delete (C9). The first real group and
  service workspace are the owner's proof, recorded with their date in
  `docs/workspaces.md`, as the first personal approval was (WD8).

---

### 4.11 Where the provisioner runs (owner decision, 2026-10-19)

**The job stays a Cloud Run job, `swarm-workspace-apply`, running as
`swarm-workspace-deployer`, triggered by an approval through Pub/Sub, Eventarc
and Workflows, exactly as it runs personal workspaces today.** The owner asked
whether it should instead run in a Kubernetes namespace of its own under a
service account, or as tasks in a SwarmCloud workspace of its own. The three
were weighed on one property above all: the provisioner is the most
privileged identity in SwarmCloud. It creates service accounts and grants
them IAM, creates namespaces with RBAC (`escalate`, `bind`), and with this
design admits principals at the front door (§4.5, K1).

| | Cloud Run job (chosen) | its own namespace in `swarm-autopilot` | its own SwarmCloud workspace |
|---|---|---|---|
| runs agent code nearby | no: outside the cluster that runs agent pods | yes: same cluster as every agent pod | yes: it would *be* an agent runtime |
| what a single escape reaches | the job's own sandbox | a cluster escape, a Workload Identity or RBAC slip reaches the IAM-granting identity | the worker identity holds provisioning powers, and a worker can rewrite its own tenant document (contract request 87) |
| new capability gained | none needed: it reaches GKE through the API and the `swarm-workspace-deployer` ClusterRole | none: the same API calls, from inside | console rows and retries, already available another way (below) |
| consolidation | the control plane already runs on Cloud Run, so nothing is consolidated by moving it | | needs a Terraform bootstrap of the system workspace itself |

What the alternatives offered, visibility and one familiar runtime, is kept
without moving the job: every step (A1 to A9 and the new S and G steps of
§4.3) is already written to the workspace record, and Admin › Tenants draws a
run as a timeline of those steps, the way a workflow's steps are drawn (§4.2).

## 5. Frozen contract: change requests

**None, for the recommended design.** These are considered and not
requested:

* **A `kind = "service"` value on `Tenant.kind`.** A service tenant is
  `kind = "user"` with a service-account principal today (`u-sw-c90291`,
  `u-or-9a0792`), and the scheduler and worker treat it correctly.
  `workspaces/{id}.kind` is swarm-api's.
* **A registered-groups field anywhere in the contract.** `resolve_tenant`
  already takes the priority tuple as an argument. swarm-api passes a longer
  one.
* **A label constant in `swarm_common`** for `swarm.saga.xyz/workspace`. It
  is written by `kubernetes/render.py` and read by the admission policy, and
  neither imports the contract for labels today.

**Only if the owner chooses design C (Q3 (c)), this request would be filed**,
in the shape of `docs/contract-change-requests.md`:

> **WK-C. `identity.py`: a job-made group's tenant id carries a `g-` prefix.**
>
> *What is true today.* `tenant_id_for_group(email)` returns the slug of the
> local part, with no prefix. `eng@saga.xyz` gives `eng`.
>
> *The change.* A second function, `tenant_id_for_workspace_group(email)`,
> returns `g-` + the same slug. `resolve_tenant` takes, beside
> `group_priority`, the set of groups that use it. `tenant_id_for_group` is
> unchanged for `eng` and `smoke`.
>
> *Rollback.* Delete the function. A `g-` tenant would then resolve as its
> bare slug, so every `g-` workspace must be deprovisioned first. In
> practice, no rollback once one exists.
>
> *What it would break.* Every caller that recomputes a group's id from its
> email (`auth.py`, `store.py::assert_tenant_scope`, `test_oracle_api_tenant.py`'s
> pattern, `forge_user_slots.tf`'s parse) must learn which function applies.
> Two groups `g-x@` and `x@` become a new collision class.
>
> *Invariants.* **9**: the prefix makes the deployer's bounds fixed prefixes
> again. That is the only gain over design A's label.

---

## 6. Build plan

Each lane owns the files in its row, and **no file appears in two rows**.
Every lane:

* pushes its tests alone to the PR branch first, and its fix to
  `<branch>-fix`;
* runs its area's unit tests, then `scripts/changed-guards.sh` and
  `scripts/publish-scan.sh`, before finishing.

The UI is split up front into two lanes of a few files each.

**Security review** (CLAUDE.md: credentials, tenant isolation, IAM) is
**blocking** for WK-RESOLVE (it changes who resolves to which tenant and who
is admitted), WK-IAM (front-door IAM) and WK-JOB (what the deployer will do).
No lane that depends on one starts until its review reports no blocker or
major.

| order | lane | files | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **O1** (the owner, not an agent) | none | the three live reads of §4.5, dated, written to `docs/workspaces.md` §0 by WK-DOCS |
| 1 | **WK-RECORD** | `apps/swarm-api/swarm_api/workspaces.py`, `people.py`, `routes/people.py`, `errors.py`, `refusals.py`, `groups.py`, `tests/unit/control_plane/test_workspace_kinds_record.py` (new), `tests/unit/control_plane/test_new_refusals_ship_off.py` (only `NOT_SWITCHABLE_CODES` entries, with reasons) | a record with no `kind` reads `personal` and every existing People test passes unchanged; `POST /v1/admin/workspaces` by a non-admin is 403 and writes nothing; a group `u-eng@saga.xyz` is `WORKSPACE_GROUP_ID_RESERVED`; a group whose fake directory raises is `WORKSPACE_GROUP_UNREADABLE` with no document written, and the fake records exactly one `checkTransitiveMembership` carrying `x-goog-user-project`; a group equal to an `ADMIN_GROUPS` entry is refused; a service `swarm-api@…` and a service listed in `ALLOWED_USERS` are each refused; a service account outside the project is refused; ids `eng` and `eng-git-u-x` collide as `WORKSPACE_ID_NESTED`; an admin's group request is `approved` with one `admin_audit` row in the same transaction (an injected failure leaves neither); a service request by a non-owner admin stays `requested`, and approve by that admin is 403 `WORKSPACE_APPROVAL_OWNER_ONLY`; a second identical request is 200 and writes nothing; no response body names `requested_by` |
| 1 | **WK-IAM** (Track C, security review) | `terraform/bootstrap/workspace_deployer.tf`, `terraform/bootstrap/forge_user_slots.tf`, `terraform/bootstrap/variables.tf`, `tests/terraform/workspace_kinds_iam.tftest.hcl` (new) | with `enable_workspace_kinds = false` the plan adds nothing; with it true: K1's condition is `==` (not `startsWith`) on `projects/<number>/iap_web/compute/services/<generated_id>` of the API backend only, and no binding names `-ui-backend`; K1's role holds exactly its two permissions; K2's expression admits `swarm-tenant-eng-git` and `-git-refresh` and refuses `swarm-tenant-eng-anthropic`, `-wsec-x` and `-git-u-…` (the startsWith/endsWith emulation the existing tests use); K3 admits `swarm-tenant-research-git-u-<hash>` and refuses `swarm-tenant-research-git`; the deployer role-list validation fails on a role outside the list |
| 1 | **WK-CLUSTER** (Track C) | `kubernetes/policies/workspace-provisioner-scope.yaml`, `kubernetes/render.py`, `kubernetes/apply.sh`, `tests/unit/worker/test_workspace_provisioner_scope.py` | the policy admits CREATE of `swarm-tenant-research` with the label, and refuses it without; refuses UPDATE adding the label to an unlabelled namespace; refuses a namespaced object in `swarm-tenant-eng` (unlabelled); refuses CREATE of each name in the rendered Terraform list even with the label; the `swarm-tenant-u-*` arm still admits an unlabelled personal namespace; `render.py` with `--workspace-id w-000000` puts the label on the Namespace and nowhere else |
| 2 | **WK-RESOLVE** (after RECORD; security review, blocking) | `apps/swarm-api/swarm_api/registry.py` (new), `auth.py`, `settings.py`, `tests/unit/control_plane/test_workspace_registry.py` (new), `tests/unit/control_plane/test_tenant_switcher.py` (new cases only) | with a fake store holding a `ready` group record `research@saga.xyz`: a confirmed member gets `research` in `tenant_choices`, and the directory fake records one membership call per registered group, each with `x-goog-user-project`, and **no** list call; an `applying` group record is in nobody's choices; a member of `eng` keeps `eng` as default (Terraform groups first); a failed lookup of `research` for a caller in no higher group is 503, not a personal-tenant fallback; a `ready` service record admits its account (tenant `tenant_id_for_user` of it) and an `applying` one is 403 `WORKSPACE_NOT_READY`; `X-Swarm-Tenant: eng` from that account is `tenant_not_member`; a registry read failing after a good read serves the cached set and logs `registry_stale`, and with no good read is 503 `registry_unavailable`; an empty registry leaves every existing auth test unchanged |
| 2 | **WK-JOB** (after IAM, CLUSTER and RECORD; security review, blocking; CODEOWNERS) | `scripts/register-tenant.sh`, `scripts/lib/forge-slot.sh`, `scripts/lib/workspace-guard.sh`, `scripts/lib/workspace-calls.json`, `scripts/lib/workspace-guard-cases.json`, `images/workspace-apply/entry.py`, `tests/unit/scripts/test_register_tenant_workspace.py`, `tests/unit/scripts/test_workspace_deployer_walk.py`, `tests/unit/scripts/test_workspace_firestore_permissions.py`, `tests/unit/scripts/fixtures/workspace_guard_corpus.json` | a full dry-run of a `service` record makes S1, A2-A8, S2, A9 in that order and every call matches a rule; S1 refuses an account on `SHARED_DENY_LIST` and a `swarm-` account with `needs_owner`, before any write; C10 refuses a member other than the approved principal, the `-ui-backend`, and any `remove-iam-policy-binding`; a `group` record's A6 creates `swarm-tenant-<g>-git` and twin and binds only the worker; its A7 renders the label; A1 refuses a group record whose `directory.checked_at` is 25 h old, and a service record approved by a non-owner; the walk maps C10 to K1's permission and condition and fails on a C10 shape no call makes; a `personal` record's run is call-for-call identical to today's (the existing corpus unchanged) |
| 3 | **WK-UI-DIALOG** (after RECORD) | `apps/swarm-ui/src/NewWorkspace.tsx` (new), `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/src/types.ts`, `apps/swarm-ui/src/__tests__/tenants.new-workspace.test.tsx` (new) | each tab posts once with `{kind, principal, display_name}` only; a 403 renders the API's message word for word; the service tab says the platform owner approves; after submit the steps render and polling runs every 5 s, stops at `ready` and on close |
| 4 | **WK-UI-TENANTS** (after UI-DIALOG) | `apps/swarm-ui/src/Activity.tsx`, `apps/swarm-ui/src/__tests__/tenants.test.tsx` (new cases only) | a non-admin sees no New workspace button; "Being created" lists non-ready records with kind and `w-` id and no email; Approve on a service row is absent for a non-owner admin; a `needs_owner` row shows the stopped step |
| 4 | **WK-TF-FREEZE** (Track C; only under Q8 (a)) | `terraform/infra/variables.tf`, `tests/terraform/tenants_frozen.tftest.hcl` (new) | a fifth `var.tenants` key fails validation with the message naming this design; the four existing keys pass |
| 5 | **WK-DOCS** (after JOB and RESOLVE) | `docs/workspaces.md`, `docs/multi-tenancy.md`, `docs/runbooks/tenant-offboarding.md`, `docs/design/workspace-kinds.md` | `test_docs_describe_what_was_built`, where it reads these files: each claim names code on main |

Then the owner's one-time steps, in order. No agent runs them:

1. O1.
2. The bootstrap apply with `enable_workspace_kinds = true`.
3. `kubernetes/apply.sh --policies --confirm`.
4. One real group and one real service workspace, approved in Admin ›
   Tenants and recorded with their dates.

Each changes tenant resolution or admission, so the release that ships
WK-RESOLVE also runs `make smoke concurrency-test race-test` against dev, in
CI.

---

## 7. Owner questions

**All nine answered by the owner on 2026-10-19, each with its recommended option (a)**, in Claude Code after the design was opened as PR 1600. The answers are marked on each question below. They bind the build plan in §6, including WK-TF-FREEZE (Q8 (a)). Where the provisioner runs was decided the same day (§4.11).

Also in this lane's `questions.json`. The recommended option is listed first.

**Q1. May an admin create a personal workspace for a named person?** **Answered 2026-10-19 by the owner: (a).**
Today's §1.3 rule 4 says nobody can request one for anyone else.
* **(a) Yes (recommended).** An admin names an allowed-domain email in
  Admin › Tenants. The record is `approved` at once, and the person's first
  sign-in finds it ready. This is the owner's "bootstrap new personal
  workspaces as part of adding a new tenant".
* (b) No. Admin › Tenants' Personal tab only explains that people request
  their own, and links to People.

**Q2. Who approves each kind?** **Answered 2026-10-19 by the owner: (a).**
* **(a) Recommended.** Personal and group: any admin, automatic when the
  requester is an admin. Service: the platform owner only, because it admits
  a new principal at the front door.
* (b) Any admin for every kind.
* (c) A second admin, never the requester, for group and service.

**Q3. How a group's id meets the deployer's `u-` bounds.** **Answered 2026-10-19 by the owner: (a).**
* **(a) Design A (recommended).** Widen the namespace policy behind a
  job-only label, the secret binder to team-slot suffixes, and swarm-api's
  user-slot grants to any tenant. Each is narrowed again and alerted on.
* (b) Design B. Groups stay in Terraform. Only personal and service go
  through the job.
* (c) Design C. Job-made groups get `g-` ids. This is a frozen-contract change
  (WK-C).

**Q4. Does a job-made group become its members' default tenant?** **Answered 2026-10-19 by the owner: (a).**
* **(a) Yes, as any registered group does today (recommended).** It sorts
  after every `TENANT_GROUPS` entry, and a default the person chose still
  wins.
* (b) No. It is selectable in the switcher only, and headerless requests keep
  their current default.

**Q5. A failed lookup of a job-made group.** **Answered 2026-10-19 by the owner: (a).**
* **(a) Fatal, as for every registered group (recommended).** 503 for a
  caller it could have changed the answer for. A misfiled task cannot be
  recovered.
* (b) Non-fatal for job-made groups. The caller lands in their next tenant.
  This risks filing work under the wrong tenant.

**Q6. Members' own forge slots in a job-made group.** **Answered 2026-10-19 by the owner: (a).**
* **(a) Served only through the credential broker (recommended).** Until
  `FORGE_BROKER` is on, members use the team token, and a request for their
  own credential is refused `FORGE_USER_SLOT_NEEDS_BROKER` (report-only at
  ship).
* (b) The group kind ships only once `FORGE_BROKER` is on in that
  environment.
* (c) Team token only, never per-member slots, in job-made groups.

**Q7. Which service accounts may get a service workspace?** **Answered 2026-10-19 by the owner: (a).**
* **(a) Only accounts in the platform's project (recommended),** with no
  `swarm-` account and none on the shared deny-list.
* (b) Any project's service account. S1 cannot read an account in another
  project with the deployer's grants, so its existence and key checks become
  the requester's word.

**Q8. Today's four Terraform tenants.** **Answered 2026-10-19 by the owner: (a).**
* **(a) They stay in Terraform (recommended),** and `var.tenants` is frozen to
  those four keys.
* (b) Migrate all four with W9's plan that destroys nothing (§4.7), one per
  pull request.
* (c) Migrate only the two service tenants, `u-sw-c90291` and `u-or-9a0792`.

**Q9. Teardown for the new kinds.** **Answered 2026-10-19 by the owner: (a).**
* **(a) Not in this build (recommended).** Disable is immediate (tenant
  `enabled = false`; a service record set `deprovisioned` drops its
  admission). The deletions stay `docs/workspaces.md` §7's separate
  owner-approved run, which gains the IAP removal.
* (b) Build the deprovisioning mode, with its own guard mode, in this build.

---

## 8. What was built

Read on `main` at `eaeef95` on 2026-10-19, by lane WK-DOCS. One entry per
lane of §6, then where the code departs from §4. Each entry names code, so a
reader can check it rather than trust it.

* **WK-RECORD (swarm-api, the record and the admin request).**
  `POST /v1/admin/workspaces` files any kind
  (`apps/swarm-api/swarm_api/routes/people.py::create_workspace`, through
  `apps/swarm-api/swarm_api/people.py::People.create`), and
  `GET /v1/admin/workspaces?kind=&state=` lists records for Admin › Tenants.
  Every check of §4.2.3 is one function,
  `apps/swarm-api/swarm_api/workspaces.py::validate_kind_request`, run before
  any write. A record with no `kind` reads `personal`
  (`apps/swarm-api/swarm_api/workspaces.py::kind_of`). The refusals are
  `WORKSPACE_ID_NESTED`, `WORKSPACE_GROUP_ID_RESERVED`,
  `WORKSPACE_GROUP_IS_ADMIN`, `WORKSPACE_GROUP_UNREADABLE`,
  `WORKSPACE_SERVICE_NOT_IN_PROJECT`, `WORKSPACE_SERVICE_PLATFORM`,
  `WORKSPACE_SERVICE_ALREADY_RESOLVES` and `WORKSPACE_APPROVAL_OWNER_ONLY`
  (`apps/swarm-api/swarm_api/errors.py`). A service request by an admin who
  is not the owner stays `requested`, with one `admin_audit` row.
* **WK-IAM (bootstrap IAM, K1 to K4).** All behind `enable_workspace_kinds`,
  off by default (`terraform/bootstrap/variables.tf`). K1 is
  `google_project_iam_custom_role.front_door_admitter` and
  `google_project_iam_member.front_door_admitter` in
  `terraform/bootstrap/workspace_deployer.tf`, on an exact-equality condition
  over the API backend's number-form name. It needs a second key too: the
  role must also be added to `workspace_deployer_roles`, whose default does
  not hold it. The `modifiedGrantsByRole` clause sits behind
  `front_door_admit_role_bound`, off until O1's third read proves IAP
  evaluates it. K2 is `workspace_team_slot_binder`, and K3 is
  `forge_member_slots_any_tenant` in `terraform/bootstrap/forge_user_slots.tf`.
  Tested by `tests/terraform/workspace_kinds_iam.tftest.hcl`.
* **WK-CLUSTER (the admission policy and the label).**
  `kubernetes/policies/workspace-provisioner-scope.yaml` admits a namespace
  carrying `swarm.saga.xyz/workspace: w-<6 hex>` beside the
  `swarm-tenant-u-*` arm, and refuses by name every namespace
  `terraform/infra` declares, whatever its labels. Its
  `swarm-workspace-deployer-namespaces` policy refuses an UPDATE that adds the
  label to an unlabelled namespace. `kubernetes/render.py::label_workspace`
  puts the label on the Namespace and nowhere else, and `kubernetes/apply.sh`
  passes `--workspace-id`.
* **WK-RESOLVE (tenant resolution and admission).**
  `apps/swarm-api/swarm_api/registry.py::WorkspaceRegistry` reads the `ready`
  group and service records, cached 60 s, and serves the last good read for
  600 s with `registry_stale` before a 503 `registry_unavailable`.
  `apps/swarm-api/swarm_api/auth.py::Authenticator._tenant_groups` puts its
  groups after every `TENANT_GROUPS` entry (Q4 (a)), so membership is still
  asked one registered group at a time. A failed lookup is a 503, as for any
  registered group (Q5 (a)).
* **WK-JOB (the job's steps and the guard).** A service runs
  `scripts/register-tenant.sh` (`printf '%s\n' A1 S1 A2 A3 A4 A5 A6 A7 A8 S2 A9`);
  personal and group run A1 to A9. S1 is `ws_s1`, S2 is `ws_s2`, and a
  service's A6 makes one team slot with no twin
  (`scripts/lib/forge-slot.sh` (`forge_slot_ensure_single() {`)). The guard
  gained C10 in `scripts/lib/workspace-calls.json`, the one front-door call,
  for a `service` expectation only. C9 still refuses every removal.
* **WK-UI-DIALOG (the New workspace dialog).**
  `apps/swarm-ui/src/NewWorkspace.tsx` posts `{kind, principal,
  display_name}` once per tab, says on the service tab that the platform
  owner approves, and polls the record's steps until it settles.
* **WK-UI-TENANTS (Admin › Tenants).** `apps/swarm-ui/src/Activity.tsx`
  draws New workspace and "Being created" for admins only. A row names its
  kind and `w-` id, never an email, and Approve and Retry on a service row are
  the owner's.
* **WK-TF-FREEZE (Q8 (a)).** `terraform/infra/variables.tf` (`VAR.TENANTS IS FROZEN`)
  holds `var.tenants` to each environment's keys today. In dev those are the
  four of §4.7, and in prod `eng`, `research` and `smoke`. A fifth key fails
  with a message naming this design (`tests/terraform/tenants_frozen.tftest.hcl`).
* **WK-DOCS (this entry).** `docs/workspaces.md` §3.2 records WD3 reversed
  for new workspaces, §4.3 the new codes, and §0 the owner's check O1, not yet
  recorded. `docs/multi-tenancy.md` covers the three admission places a
  job-made service passes, and
  `docs/runbooks/tenant-offboarding.md` disabling and removing a group or
  service workspace. Held by `tests/unit/scripts/test_docs_describe_what_was_built.py`.

**Where the code departs from §4**, each found by reading it:

1. **The platform owner A1 checks is read from the guard's rules, not from
   `entry.py`.** §4.3 put `PLATFORM_OWNER` in the image's fixed environment.
   The job reads `platform_owner` from `scripts/lib/workspace-calls.json`,
   keyed by project, and A1 also requires that email to be `owner` in
   `admin_roles/` now (`ws_platform_owner`, `ws_is_owner`). Both ship in the
   image the release builds, and nothing a caller sends can change either.
2. **A service account is admitted by the unique id S1 recorded, not by its
   address alone.** GCP lets a deleted account be made again under the same
   address, so `apps/swarm-api/swarm_api/auth.py::Authenticator._admits_service`
   admits a token only when its subject is the record's `service.unique_id`.
   A `ready` record without one admits nobody.
3. **The registry re-checks each record.** A `ready` record must still pass
   `validate_kind_request` under today's settings, and its derived id must be
   its document id, or it is left out and logged by workspace id. The record
   is the only thing between a Firestore document and a tenant boundary, so
   the registry checks it again rather than trusting it.
4. **The five new failure codes got their copy after this section's lanes.**
   `SERVICE_ACCOUNT_REFUSED`, `DIRECTORY_CHECK_STALE`, `FRONT_DOOR_OWNER`,
   `FRONT_DOOR_FAILED` and `NAMESPACE_LABEL_MISSING` were first served the
   fallback sentence. Lane WK-FAILCOPY gave each, and `APPROVAL_UNSIGNED`, a
   sentence of its own in
   `apps/swarm-api/swarm_api/workspaces.py::FAILURE_COPY`, quoted in
   `docs/workspaces.md` §4.3, and a test fails on any code the job writes
   without one.
5. **The tenants list serves `workspace_kind`, not `kind`.** `kind` on
   `GET /v1/admin/tenants` is already the frozen `Tenant.kind`, so the
   record's kind is served beside it
   (`apps/swarm-api/swarm_api/routes/admin.py::list_tenants`).

**What is not verified:** no group or service workspace has been made in any
environment. The first of each is the owner's step 4 of §6, recorded with its
date in `docs/workspaces.md` as the first personal approval was.
