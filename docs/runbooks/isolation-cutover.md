# Runbook: cut tenant workers over off direct Firestore (#1457, design B)

**When to use this.** The owner is taking tenant workers off direct Firestore,
one tenant at a time, and then removing the worker's Firestore role: the cutover
that closes #1457 (a tenant's worker can write another tenant's
`workspace_settings`). The design is
[`docs/design/tenant-settings-integrity.md`](../design/tenant-settings-integrity.md)
§4.6, §4.7 and §6. The order is owner decision Q7: dev first, **u-bogdan, then
eng, then every tenant**, and only then is the role removed.

**Who runs it.** The owner. Every step below either changes IAM or is a
release, and none of it is the release's to decide.

**What exists, as of 2026-10-20 (lane ISO-IAM-WIRE).** Three switches, all off:

| switch | where | default | what it switches |
|---|---|---|---|
| `state_via_api_tenants` | `terraform/infra/child_tasks.tf`, rendered onto swarm-scheduler as `SWARM_STATE_VIA_API_TENANTS` | `[]` | the scheduler dispatches every attempt of a listed tenant, children included, with `SWARM_STATE_VIA_API=true`, `SWARM_API_URL` and a registration nonce; the worker builds no Firestore client and reaches its state only through `/v1/attempts/{attempt_id}/state/*` |
| `worker_firestore_off_tenants` | `terraform/infra/variables.tf`, rendered onto swarm-api as `WORKER_FIRESTORE_OFF_TENANTS` | `[]` | swarm-api writes `worker_firestore_access: false` on a listed tenant's **job-made** workspace record at its next approval, retry or ceiling change; the workspace job then grants that worker no Firestore role, and its A9 check requires it to hold none ([`docs/workspaces.md` §2.7](../workspaces.md)) |
| `worker_firestore_access` | `terraform/infra/variables.tf`, passed to `module "tenancy"` | `true` | false removes `module.tenancy.google_project_iam_member.worker_firestore[<tenant>]` for **every Terraform tenant at once** |

The plan refuses each switch out of order, so a mistyped tfvars stops at plan
rather than failing workers:

* `state_via_api_tenants` needs `enable_child_tasks` (a switched worker spends
  the child path's nonce at `state/open`) and takes tenant ids only, never `*`;
* every `worker_firestore_off_tenants` entry must already be in
  `state_via_api_tenants`, and `*` is refused;
* `worker_firestore_access = false` needs every key of `var.tenants` in
  `state_via_api_tenants`.

`tests/terraform/worker_firestore_switches.tftest.hcl` and
`state_via_api_env.tftest.hcl` hold all of that.

**What it does not do.** Nothing here touches the other team's resources in
`saga-agents-staging`: every member named below is a `swarm-agent-worker-*`
account and every role is the swarm's own `swarmTenantWorkerFirestore`.

---

## What a cut-over tenant is protected from, and when

Read this before step a. **A tenant's own cutover protects other tenants from
that tenant's worker. It does not protect that tenant from the others.** While
any worker anywhere still holds `swarmTenantWorkerFirestore`, that worker can
still read and write the cut-over tenant's documents by id, its
`workspace_settings` and its workspace record included. On the record it can
write `worker_firestore_access: true`, which the workspace job would act on at
its next create run; swarm-api rewrites the field from its list at every admin
action that starts a run, which narrows that to a race, not nothing
([`docs/workspaces.md` §2.7](../workspaces.md), "A forged value").

So #1457 is closed **only at the end of step c**, when no worker holds the role.
Steps a and b prove the path one tenant at a time; they are not the fix.

---

## What to watch, at every step

Read these in the hour after each release and each removal. Each is read live:
none of the numbers below is a baseline anyone has measured yet.

1. **swarm-api's state routes** (`swarm_api/attemptstate.py`, "Refusals"). Its
   request log, every 4xx and 5xx on a state route:

   ```bash
   gcloud logging read \
     'resource.type="cloud_run_revision" AND resource.labels.service_name="swarm-api" AND httpRequest.requestUrl:"/state/" AND httpRequest.status>=400' \
     --project saga-agents-staging --freshness=1h --format='value(timestamp,httpRequest.status,httpRequest.requestUrl)' | redact
   ```

   (`redact` is `scripts/lib/common.sh`'s; the URL carries an attempt id and no
   credential, but the rule is to pipe it anyway.)

   | answer | code | what it means here | act |
   |---|---|---|---|
   | 401 | `attempt_unauthenticated` | the caller's token is not `swarm-agent-worker-<tenant>` for the tenant the attempt belongs to: another tenant's worker naming this attempt, or an identity misconfigured | **any** is a stop: name the caller from the log before going on |
   | 403 | `attempt_unproven` | a token without the attempt key's proof: the agent calling with a token it minted (§4.7) | **any** is a stop: an agent is trying the routes |
   | 409 | `attempt_fenced` | a stale generation: the attempt was reclaimed and a newer one owns the task | expected now and then after a reclaim; the worker exits without running the agent (invariant 5) |
   | 409 | `state_conflict` | a write raced another | occasional is expected; a steady rate is a bug to file |
   | 422 | `transition_refused` | a transition the route refuses (RUNNING→LEASED among them) | **any** from a worker is a bug to file before the next step |
   | 403 | `upstream_not_declared` | a read of a task the attempt's signed spec does not declare | **any** is either a missed reader (file it) or a probe for another task |
   | 403 | `merge_wake_refused` | a merge-slot wake of another tenant's waiter | **any** is a stop |
   | 503 | `state_unavailable` | swarm-api cannot reach its state | a rate above zero for more than a few minutes is the rollback trigger: switched workers cannot start |

2. **The worker.** A switched worker that cannot start exits 78 (a 401/403 at
   `state/open`, or the switch on with no nonce); an outage before the runner is
   69. A refusal that is neither fence nor outage is a `ControlPlaneError`
   (`StateRefused`, naming the code), and a module still reading Firestore
   directly on a switched worker raises one naming `db.<attribute>`. Any of
   those in the tenant's job or pod logs after its state switch is a stop.

3. **`PermissionDenied` after a removal.** A worker still on the direct path
   when its role goes fails its first Firestore read with `PermissionDenied`,
   and its attempt fails retryably (§4.7). After a tenant's removal, any
   `PermissionDenied` naming Firestore in that tenant's worker logs means an
   attempt was dispatched unswitched: a release that did not land, or an
   attempt from before it. Check the scheduler's live
   `SWARM_STATE_VIA_API_TENANTS` before anything else.

4. **The ISO-ACCEPT probe**,
   `tests/acceptance/fixtures/tenant-isolation/test_cross_tenant_firestore.py`.
   From inside a tenant's worker, with that worker's own credential, it asks
   Firestore to update `workspace_settings/<B>`, get `tenants/<B>` and create
   under `leases/`, and passes only when each is a 403. It writes nothing
   whatever IAM answers (its docstring says how). It is the **only** check that
   reads what the role actually allows. Run as a program it is always
   explicit: `python3 test_cross_tenant_firestore.py --results /tmp/iso.json`.
   The acceptance suite runs it in the `smoke` tenant once lane ISO-ACCEPT-WIRE
   merges, so in acceptance it can go green only at step c, when `smoke`'s
   binding goes.

5. **The workspace deployer's alert**, `swarm-dev-workspace-deployer-outside-personal-workers`
   (`terraform/modules/monitoring/workspace_alerts.tf`). It pages on any
   binding the deployer **removes** (rule C9: the job only ever adds). The
   owner's hand removals below are made by the owner, not the deployer, so
   they must **not** page it. A page during this cutover means the deployer
   removed a binding: stop, and treat it as that alert says.

---

## a. u-bogdan

u-bogdan is not a Terraform tenant (lane W9: `terraform/infra/removed.tf`
forgot its instances with `destroy = false`), so its binding is live but
unmanaged, and its switch is swarm-api's list, not the tenancy module's.

### a1. Switch its state path

In `terraform/environments/dev/dev.tfvars` (`enable_child_tasks = true` is
already set there):

```hcl
state_via_api_tenants = ["u-bogdan"]
```

Merge it; the release applies it.

**The plan must show** exactly one in-place update:
`module.cloud_run.google_cloud_run_v2_service.this["swarm-scheduler"]`, whose
environment gains `SWARM_STATE_VIA_API_TENANTS = "u-bogdan"`. Nothing
created or destroyed, and no IAM change (so the release does not stop at
`dev-iam`). Anything else in the plan is someone else's change: hold.

**Then run the gate:**

```bash
SWARM_API_TENANT=u-bogdan make verify-remote
```

**What that run does and does not exercise, said plainly.** Read on
2026-10-20 at `4589544`: `scripts/verify-remote.sh` passes no environment to
the `swarm-verify` job (it runs `gcloud run jobs execute swarm-verify --args
scripts/<target>.sh`), and the suites it runs do not send `X-Swarm-Tenant`. The
job runs in its own identity's tenant (eng, by #1457's 2026-10-20 baseline
comment; read it from the job's transcript before relying on it), and that
identity could not select a personal tenant even if the header were sent: the
tenant switcher selects only among the caller's memberships. So this run is the
regression check on admission, concurrency and fencing for the release that
switched u-bogdan, **not** a run of u-bogdan's switched path. That is a question
in lane ISO-IAM-WIRE's pull request. What exercises u-bogdan's path, until it
is answered:

* a task of u-bogdan's own, submitted as the owner, that runs to SUCCEEDED,
  with its worker's log showing no Firestore client was built and its
  `state/open` answered 200 in swarm-api's request log (watch item 1);
* a task that parks and resumes (a long provider wait), and a cancellation:
  both go through the routes (`park`, `release`) and must release the lease.

**Rollback.** Remove `u-bogdan` from `state_via_api_tenants`; release. Its role
is still bound, so its next attempt takes the direct path as before. An
attempt dispatched switched keeps its environment and finishes through the
routes.

### a2. Withhold and remove its Firestore role

Only after a1's tasks are green and watch items 1-2 are quiet.

**Before you start, drain the attempts dispatched before a1's release.** An
attempt that was dispatched unswitched keeps the direct path for its whole
life and would fail with `PermissionDenied` the moment the role goes. Wait until
no u-bogdan attempt that was LEASED before the a1 release's deploy time is
still LEASED, DISPATCHED, STARTING or RUNNING. PARKED attempts hold nothing
(invariant 1) and resume as new, switched attempts.

1. In `dev.tfvars`:

   ```hcl
   worker_firestore_off_tenants = ["u-bogdan"]
   ```

   **The plan must show** one in-place update:
   `module.cloud_run.google_cloud_run_v2_service.this["swarm-api"]`, whose
   environment gains `WORKER_FIRESTORE_OFF_TENANTS = "u-bogdan"`. No IAM change.

2. **An admin action, so the record takes the switch.** u-bogdan's workspace is
   `ready`, so the one admin action that writes the record is a ceiling
   change. Read its current `max_active` (and its workspace id; it is private,
   so not here) from **Admin › People**, and set the ceiling to the **same**
   value there, or with `PUT /v1/admin/workspaces/<workspace id>/limits`
   `{"max_active": <the same value>}`. Every ceiling change writes
   `worker_firestore_access` from the list (`People.set_ceiling`), and the
   same value changes no limit. That starts a `limits` run, which re-applies
   the namespace and the documents and **makes no IAM call**: it removes
   nothing.

3. **Verify, expecting red.**

   ```bash
   scripts/register-tenant.sh --workspace <workspace id> --mode verify
   ```

   A9 must fail `VERIFY_FAILED` on object `project grant`, printing that the
   worker still holds `projects/saga-agents-staging/roles/swarmTenantWorkerFirestore`
   and that its record says `worker_firestore_access false`. That red is the
   proof the record took the switch. If it passes instead, the record did not:
   stop and read the record before removing anything.

4. **Remove the binding by hand** (rule C9: the job never removes one):

   ```bash
   gcloud projects remove-iam-policy-binding saga-agents-staging \
     --member=serviceAccount:swarm-agent-worker-u-bogdan@saga-agents-staging.iam.gserviceaccount.com \
     --role=projects/saga-agents-staging/roles/swarmTenantWorkerFirestore \
     --condition=None
   ```

   `--condition=None` names the unconditioned binding, which is the only shape
   this role was ever granted in (`scope_firestore_to_database` defaults off).
   If step 3 printed the role as `(conditioned)`, pass that binding's condition
   instead. If it printed a `roles/datastore.*` role too, remove that the same
   way: A9 refuses any Firestore role.

5. **Verify again, expecting green**, with the same `--mode verify` command:
   `verified: …` and the workspace `ready`. Then run the ISO-ACCEPT probe from a
   u-bogdan worker (watch item 4): all three checks PASS. Then
   `SWARM_API_TENANT=u-bogdan make verify-remote` again, read as in a1.

**Rollback.** Re-grant first, then unswitch, never the other way round: a
switched list with no role is safe, an unswitched worker with no role is not.

```bash
gcloud projects add-iam-policy-binding saga-agents-staging \
  --member=serviceAccount:swarm-agent-worker-u-bogdan@saga-agents-staging.iam.gserviceaccount.com \
  --role=projects/saga-agents-staging/roles/swarmTenantWorkerFirestore \
  --condition=None
```

then remove `u-bogdan` from `worker_firestore_off_tenants`, release, and make
the same-value ceiling change so the record reads `true` again (the `limits`
run grants nothing; the hand grant above is what restores access). Only after
that, a1's rollback if it is wanted.

---

## b. eng

eng **is** a Terraform tenant, and that changes step b2.

### b1. Switch its state path

```hcl
state_via_api_tenants = ["u-bogdan", "eng"]
```

The plan, the release and the rollback are a1's, with `"u-bogdan,eng"` as the
rendered value. Then:

```bash
SWARM_API_TENANT=eng make verify-remote
```

This one does exercise the switched path, if the verify job's tenant is eng (see
a1: read it from the transcript). Its smoke, concurrency, race and e2e suites
then submit, admit, fence and release eng attempts through the routes, which
is the `make smoke concurrency-test race-test` §6's order asks for after a
dispatch change. A failure of any suite is a stop: roll b1 back and file it.

### b2. Its Firestore role

**eng's binding cannot be removed on its own today, and a hand removal would
come back.** It is `module.tenancy.google_project_iam_member.worker_firestore["eng"]`,
in Terraform state, and `worker_firestore_access` is one switch for every
Terraform tenant. Removed by hand, the next release's plan shows it as `1 to
add` and re-creates it. That is a question in lane ISO-IAM-WIRE's pull request
(a per-tenant switch in `terraform/modules/tenancy`). Until it is answered:

* `worker_firestore_off_tenants = ["u-bogdan", "eng"]` is harmless and does
  what it says for a job-made `eng` workspace record, if one exists; eng has
  none today (it is provisioned by Terraform), so it changes nothing;
* eng's role goes in step c, with every other Terraform tenant's.

---

## c. Every remaining tenant, then the role

### c1. Switch every remaining tenant

List every tenant in the environment: every key of `tenants` in the tfvars
(dev: `eng`, `smoke`; prod: `eng`, `research`, `smoke`) and every job-made
workspace's tenant (Admin › People). Add them to `state_via_api_tenants` in
one change or several, each a1's plan and rollback. `smoke` is the acceptance
suite's tenant, so the release's acceptance run after it is the gate for that
switch.

Then, for every **job-made** tenant, a2 in full: list it in
`worker_firestore_off_tenants`, the same-value ceiling change, the red verify,
the hand removal with its own member
`serviceAccount:swarm-agent-worker-<tenant>@saga-agents-staging.iam.gserviceaccount.com`,
the green verify. Drain each first, as a2 says.

### c2. `worker_firestore_access = false`

```hcl
worker_firestore_access = false
```

The plan refuses this until every key of `tenants` is in
`state_via_api_tenants`. Drain the Terraform tenants' unswitched attempts first,
as a2 says.

**The plan must show** a destroy of exactly one binding per Terraform tenant
and nothing else. In dev:

* `module.tenancy.google_project_iam_member.worker_firestore["eng"]`
* `module.tenancy.google_project_iam_member.worker_firestore["smoke"]`

In prod, also `module.tenancy.google_project_iam_member.worker_firestore["research"]`.
Each is role `projects/saga-agents-staging/roles/swarmTenantWorkerFirestore`
on `serviceAccount:swarm-agent-worker-<tenant>@…`. Every other
`module.tenancy` resource — the accounts, the object grants, the telemetry
grants — is unchanged; a plan that touches one of them is wrong. It is an IAM
change, so the dev release stops at `dev-iam` for the owner's approval
([`docs/ci.md`](../ci.md#a-dev-release-that-changes-iam-waits-for-the-owner)).

**Then:** the ISO-ACCEPT probe goes green in acceptance (smoke's binding is
gone), and the gate runs once more:
`make verify-remote`. Read the live project policy for any worker still holding
the role:

```bash
gcloud projects get-iam-policy saga-agents-staging --format=json \
  | jq -r '.bindings[] | select(.role | endswith("/roles/swarmTenantWorkerFirestore")) | .members[]'
```

It must print no `swarm-agent-worker-*` member. Count the lines and say the
count when reporting: an empty answer from a command that did not run is not
an empty policy.

**Rollback.** `worker_firestore_access = true`; release (it re-creates the
bindings, through `dev-iam`). Faster in an outage: re-grant by hand with the
`add-iam-policy-binding` command above, per tenant, then the release, whose
create of an existing member is a no-op.

---

## d. The role definition (owner, bootstrap)

Only after c2 has been live long enough that nobody expects to roll it back.
Then nothing should hold `swarmTenantWorkerFirestore`, and the definition can
go. It is in the **bootstrap** root, which the owner applies (`make
bootstrap`), never the release, in three places:

* `terraform/bootstrap/platform_roles.tf`, the `worker_firestore` entry of the
  platform roles (its five permissions);
* `terraform/bootstrap/deployer_conditions.tf`, `"worker_firestore"` in the
  list of roles the release deployer may grant: off it, CI can no longer hand
  the role to anyone;
* `terraform/bootstrap/workspace_deployer.tf`,
  `module.custom_role_ids.names.worker_firestore` in
  `workspace_worker_project_roles`, the roles the workspace job's conditioned
  `projectIamAdmin` may add: off it, the workspace job cannot grant it even
  from a forged record.

Remove the deployer-condition and workspace-deployer entries **first**, in one
bootstrap apply, so nothing can grant the role; then the definition in a
second. The workspace job's guard and A9 still name the role
(`scripts/register-tenant.sh`, `scripts/lib/workspace-calls.json`) and
`terraform/modules/tenancy` still has the switch: their cleanup is its own
lane after the definition is gone, and its tests change with it
(`tests/terraform/platform_roles.tftest.hcl`, `deployer_iam.tftest.hcl`,
`custom_roles_out_of_ci.tftest.hcl`).

**Rollback.** Revert the bootstrap change and apply it. A deleted custom role
is recoverable with `gcloud iam roles undelete` for seven days; after that its
id cannot be reused for 37 days.
