# Runbook: cut tenant workers over off direct Firestore (design B)

**When to use this.** The owner is taking tenant workers off direct Firestore,
one workspace at a time, and then removing the worker's Firestore role. This
cutover closes the cross-tenant workspace-settings write issue, where a
tenant's worker can write another tenant's `workspace_settings`. The design is
[`docs/design/tenant-settings-integrity.md`](../design/tenant-settings-integrity.md)
§4.6 ("Changes by component"), §4.7 ("Failure cases") and §6 ("Build plan").
The order is owner decision Q7 (same doc, the Q7 row of the decisions table
and "Q7. Cutover"): dev first, then **a personal workspace, then a group
workspace, then every tenant**, and only then is the role removed.

**Who runs it.** The owner. Every step below either changes IAM or is a
release, and none of it is the release's to decide.

**Revised 2026-11-24.** This revision puts the runbook in the standard order
(what and why, impact, preconditions, steps, rollback, verification, open
decisions). It also corrects three facts against `main`:

* the `runs_no_work` exception on three of the switches;
* the probe's wiring, which is now on `main`;
* the path of the refusal codes.

It records what the epic reports about dev and replaces deployment names with
placeholders. No setting, code or Terraform was changed by this revision.

**Placeholders.**

| placeholder | what it is |
|---|---|
| `<project>` | the deployment's own project |
| `<region>` | its region |
| `<env>` | `dev` or `prod` |
| `<state bucket>` | the Terraform state bucket that holds `config/<env>/<env>.tfvars` |
| `<personal workspace>` | step a's tenant: operator- or job-registered, **not** a key of `tenants` in the tfvars |
| `<group workspace>` | step b's tenant: a key of `tenants` |
| `<workspace id>` | the workspace record's id (private; read it from Admin › People) |

`smoke` is the acceptance suite's tenant as the repository's acceptance
scripts name it (`scripts/acceptance/groups/tenant-isolation.sh`).

---

## 1. What changes and why

Each tenant's worker today holds `swarmTenantWorkerFirestore` across the whole `swarm` database, so an agent in one tenant's pod can read and write another tenant's documents by id ([`docs/ci.md`, "The cross-tenant Firestore probe is report-only until the cutover"](../ci.md)). The cutover moves each tenant's worker onto swarm-api's attempt-signed state routes (`/v1/attempts/{attempt_id}/state/*`) and then removes the role, per tenant and finally for all, through the four switches in the [appendix](#appendix-the-switches-what-the-plan-refuses-and-where-dev-stands). The cross-tenant write issue is closed **only when no worker holds the role**, at the end of step c2.

---

## 2. Impact inventory

### Work in flight it can disturb

* **Attempts of the tenant being switched.** An attempt keeps the
  environment it was dispatched with for its whole life.
  * One dispatched **unswitched** before a state-switch release keeps the
    direct path. If the tenant's role is then removed, its first Firestore
    read fails `PermissionDenied`, and the attempt fails retryably
    ([design §4.7](../design/tenant-settings-integrity.md)). Hence the drain
    before every removal (a2, b2, c2).
  * One dispatched **switched** keeps the routes, even after a rollback of the
    switch.
  * PARKED attempts hold nothing (invariant 1) and resume as new attempts,
    which are dispatched under the switch as it is then.
* **Child tasks, workflows and issue runs of that tenant.** Children are
  dispatched under the same switch (`state_via_api_tenants`'s description,
  `child_tasks.tf:94`). A workflow step is an attempt like any other.
* **Schedules.** **Not checked:** how a scheduled run (`docs/schedules.md`)
  reaches dispatch was not traced for this runbook. The expectation, not
  verified, is that its attempts are dispatched by the scheduler like any
  other and follow the switch at dispatch time. Before each removal, read the
  tenant's schedules in the console and treat a run due inside the drain as
  in-flight work.
* **Releases.** Every step is a release of `terraform/infra` (`release.yml`
  runs on every push to `main`, `.github/workflows/release.yml:123-135`).
  * A step that changes IAM holds at `dev-iam` for the owner's approval
    ([`docs/ci.md`, "A dev release that changes IAM waits for the owner"](../ci.md#a-dev-release-that-changes-iam-waits-for-the-owner)).
  * While it holds, any other change merged to `main` waits behind it.
* **Acceptance runs.** `smoke` is the suite's tenant. Switching it (c1) makes
  the next acceptance run the gate for that switch. Its binding's removal (c2)
  turns the `acceptance (tenant-isolation)` probe green
  ([`docs/ci.md`, the probe section](../ci.md)).

### Stateful things it touches

| kind | name | changed by |
|---|---|---|
| setting | `config/<env>/<env>.tfvars` in `<state bucket>`: `state_via_api_tenants`, `worker_firestore_off_tenants`, `worker_firestore_off_tenants_iam`, `worker_firestore_access`, `tenant_job_vpc_egress`, and later `var.tenants[*].runs_no_work` | the owner's edit, a, b, c |
| running service | swarm-scheduler's environment (`SWARM_STATE_VIA_API_TENANTS`, `TENANT_JOB_VPC_EGRESS`) and swarm-api's (`WORKER_FIRESTORE_OFF_TENANTS`), each a new revision | the release |
| running jobs | the scheduler-made Cloud Run Jobs `swarm-job-<personal workspace>-<profile>`, rebuilt with `vpc_access` at their next dispatch (`scheduler.dispatch._refresh_image`, `apps/scheduler/scheduler/dispatch.py:2558`) | the scheduler, a1 pre-step |
| access grant | `swarmTenantWorkerFirestore` on `serviceAccount:swarm-agent-worker-<tenant>@<project>.iam.gserviceaccount.com`, per tenant | a2 by hand; b2 and c2 by plan |
| access grant | `roles/compute.networkUser` for swarm-scheduler on the swarm subnetwork (`google_compute_subnetwork_iam_member.scheduler_tenant_job_subnet`, `terraform/infra/tenant_job_vpc.tf`) | a1 pre-step, by plan |
| service identities | `swarm-agent-worker-<tenant>` (loses a role, is not deleted), swarm-scheduler, swarm-api | — |
| stored documents | the job-made workspace record's `worker_firestore_access` field (`People.set_ceiling`, `apps/swarm-api/swarm_api/people.py:1024`; `_access_patch` at :628 writes it) | a2, c1 |
| secrets (names only) | `swarm-child-key`, read by the scheduler and swarm-api to issue and check the registration nonce. It is read, never written | — |
| repository variable | `ISO_PROBE_REQUIRED` (`docs/ci.md`, the probe section) | the owner, after c2 (open decision D3) |
| custom role definition | `swarmTenantWorkerFirestore` (`terraform/bootstrap/platform_roles.tf:286`) and its grantable entries (`deployer_conditions.tf:406`, `workspace_deployer.tf:167`) | d, owner's bootstrap apply |

### Outside the deployment's own project

It touches nothing there. That is guaranteed three ways:

* **Members.** Every member named is a `swarm-agent-worker-*` account, and
  every role is this platform's own `swarmTenantWorkerFirestore`.
* **Bindings.** Every Terraform binding is on `var.project_id`
  (`terraform/modules/tenancy/main.tf`, `worker_firestore`).
* **The subnetwork grant** is on the swarm subnetwork alone, never at project
  level. A project-level grant would reach the other team's network in a
  shared project (`terraform/infra/tenant_job_vpc.tf`, the "WHY AT THE
  SUBNETWORK" comment).

The release's plan guard and deny-list (`scripts/lib/common.sh`) refuse a plan
that names a never-touch resource. Every hand command below names only
`<project>` and a `swarm-agent-worker-*` member. A plan or command that names
anything else is a stop.

### Steps that cannot be undone

* a, b and c can all be undone (re-grant, then unswitch). What is lost is any
  attempt that failed `PermissionDenied` in the window. It fails retryably and
  is retried by the platform; its partial work is lost to its last checkpoint
  (invariant 8).
* **d cannot be fully undone.** A deleted custom role can be undeleted for
  seven days. After that its id cannot be reused for 37 days
  (`docs/design/day-one-install.md`, "Custom role ids").

### Downtime and degraded window

No planned downtime for any tenant.

| step | degraded window | cause |
|---|---|---|
| a1, b1, c1 | from the release until the rollback release, should swarm-api's state routes fail (`state_unavailable`) | switched workers of that tenant cannot start: exit 69 for an outage, `ExitCode.UNAVAILABLE`, `apps/agent-worker/agent_worker/errors.py:24`; exit 78 for a refusal, `ExitCode.CONFIG`, :43 |
| a2, b2, c2 | from the removal until the re-grant, for any attempt that was not drained | `PermissionDenied` on that attempt |

Each release holds dev's release lock while it changes dev (`docs/ci.md`, "The
release timeline").

---

## 3. Preconditions

Each one is read-only. Stop at the first that fails.

**P1. The live switch values are known.**
`scripts/lib/resolve.sh tfvars <env>` fetches the bucket copy and refuses if it
is unreadable (`docs/ci.md`, "A new account exists before the release that adds
it", step 2):

```bash
current="$(scripts/lib/resolve.sh tfvars <env>)"
grep -nE '^(state_via_api_tenants|worker_firestore_off_tenants|worker_firestore_off_tenants_iam|worker_firestore_access|tenant_job_vpc_egress|enable_child_tasks)\b|runs_no_work' "${current}"
```

Record every line it prints, and say how many lines. A line not printed is the
variable's default.

Resume where those values say:

* nothing listed: a1;
* the personal workspace switched: a2;
* and so on.

If the "IAM only" tenant the epic reports is listed in an `_iam` or off list
without being in `state_via_api_tenants`, its `var.tenants` entry must carry
`runs_no_work = true`. Confirm that it never runs agents
(`docs/multi-tenancy.md`, "A tenant that runs no work": marking a tenant that
does run agents is "the mistake to avoid").

**P2. The live services carry what P1 says.** The command form is from
`docs/runbooks/onboarding-acceptance.md`:

```bash
gcloud run services describe swarm-scheduler --project <project> --region <region> \
  --format='value(spec.template.spec.containers[0].env)' \
  | tr ';' '\n' | grep -E 'SWARM_STATE_VIA_API_TENANTS|TENANT_JOB_VPC_EGRESS' | redact
gcloud run services describe swarm-api --project <project> --region <region> \
  --format='value(spec.template.spec.containers[0].env)' \
  | tr ';' '\n' | grep WORKER_FIRESTORE_OFF_TENANTS | redact
```

A difference from P1 means a release has not landed. Stop until it has.

**P3. Child tasks are on and the nonce key has a version.**

* P1 printed `enable_child_tasks = true` (`child_tasks.tf:113` refuses
  `state_via_api_tenants` without it).
* This command prints at least one version number. It prints names, never
  values, in the form `docs/runbooks/github-app.md` uses:

  ```bash
  gcloud secrets versions list swarm-child-key --project <project> --filter=state=ENABLED --format='value(name)'
  ```

**P4. Who holds the role today.** The command is the one this runbook has
always used, now with the placeholder:

```bash
gcloud projects get-iam-policy <project> --format=json \
  | jq -r '.bindings[] | select(.role | endswith("/roles/swarmTenantWorkerFirestore")) | .members[]'
```

Count the lines and record them. Every member must be
`serviceAccount:swarm-agent-worker-*`. Anything else is a stop: report it, do
not touch it.

**P5. The probe is wired.** `scripts/acceptance/groups/tenant-isolation.sh`
exists, and `.github/workflows/accept.yml` has the `tenant-isolation` job
(checked on `main` 2026-11-24). `gh variable list` shows whether
`ISO_PROBE_REQUIRED` is set. It shows names and values of variables, never
secrets (`docs/runbooks/merge-app.md`).

**P6. No release is holding.** The latest `release.yml` run for `<env>` is
finished, and none waits at `dev-iam`. Read this on the Actions page. Each step
below must be the only change in its release's plan.

---

## 4. Steps

### What to watch, at every step

Read these in the hour after each release and each removal. Each is read
live: none of the numbers below is a baseline anyone has measured yet.

1. **swarm-api's state routes.** The codes are the `code` attributes in
   `apps/swarm-api/swarm_api/attemptstate.py` (:255-289), and the routes are
   in `apps/swarm-api/swarm_api/routes/attemptstate.py`. Read swarm-api's
   request log for every 4xx and 5xx on a state route:

   ```bash
   gcloud logging read \
     'resource.type="cloud_run_revision" AND resource.labels.service_name="swarm-api" AND httpRequest.requestUrl:"/state/" AND httpRequest.status>=400' \
     --project <project> --freshness=1h --format='value(timestamp,httpRequest.status,httpRequest.requestUrl)' | redact
   ```

   `redact` is `scripts/lib/common.sh`'s. The URL carries an attempt id and no
   credential, but the rule is to pipe it anyway.

   | answer | code | what it means here | act |
   |---|---|---|---|
   | 401 | `attempt_unauthenticated` | the caller's token is not `swarm-agent-worker-<tenant>` for the tenant the attempt belongs to: another tenant's worker naming this attempt, or an identity misconfigured | **any** is a stop: name the caller from the log before going on |
   | 403 | `attempt_unproven` | a token without the attempt key's proof: the agent calling with a token it minted (§4.7) | **any** is a stop: an agent is trying the routes |
   | 409 | `attempt_fenced` | a stale generation: the attempt was reclaimed and a newer one owns the agent | expected now and then after a reclaim. The worker exits without running the agent (invariant 5) |
   | 409 | `state_conflict` | a write raced another | occasional is expected; a steady rate is a bug to file |
   | 422 | `transition_refused` | a transition the route refuses (RUNNING→LEASED among them) | **any** from a worker is a bug to file before the next step |
   | 403 | `upstream_not_declared` | a read of an agent the attempt's signed spec does not declare | **any** is either a missed reader (file it) or a probe for another agent |
   | 403 | `merge_wake_refused` | a merge-slot wake of another tenant's waiter | **any** is a stop |
   | 503 | `state_unavailable` | swarm-api cannot reach its state | a rate above zero for more than a few minutes is the rollback trigger: switched workers cannot start |

   **Not checked:** the HTTP status of each code. It is the one this runbook
   recorded on 2026-10-21, and the codes themselves were re-read on
   2026-11-24.

2. **The worker.** A switched worker that cannot start exits:
   * 78 for a 401 or 403 at `state/open`, or the switch on with no nonce. The
     no-nonce error is raised in `apps/agent-worker/agent_worker/config.py:569`.
     **Not checked:** the 401/403-to-78 path end to end.
   * 69 for an outage before the runner (`Worker._exit_control_plane_outage`,
     `apps/agent-worker/agent_worker/control.py:266-277`).

   A refusal that is neither fence nor outage is a `ControlPlaneError`
   (`StateRefused`, naming the code). A module still reading Firestore
   directly on a switched worker raises one naming `db.<attribute>`. Any of
   those in the tenant's job or pod logs after its state switch is a stop.

3. **`PermissionDenied` after a removal.** Any `PermissionDenied` naming
   Firestore in that tenant's worker logs means an attempt was dispatched
   unswitched. Read the scheduler's live `SWARM_STATE_VIA_API_TENANTS` (P2)
   before anything else.

4. **The ISO-ACCEPT probe**,
   `tests/acceptance/fixtures/tenant-isolation/test_cross_tenant_firestore.py`.
   * **What it asks.** From inside a tenant's worker, with that worker's own
     credential, it asks Firestore to update `workspace_settings/<B>`, get
     `tenants/<B>` and create under `leases/`. It passes only when each is a
     403.
   * **It writes nothing**, whatever IAM answers.
   * **It is the only check that reads what the role actually allows.**
   * **Run by hand** as a program inside a tenant pod:
     `python3 test_cross_tenant_firestore.py --results "$TMPDIR/iso.json"`.
   * **In acceptance**, `accept.yml`'s `acceptance (tenant-isolation)` job
     runs it in `smoke` on every run. It is report-only until
     `ISO_PROBE_REQUIRED` is `true` (`docs/ci.md`, "The cross-tenant
     Firestore probe is report-only until the cutover"; that section's "What
     is on `main`, read 2026-10-20" paragraph predates the wiring, which P5
     found on `main`).
   * It goes green in acceptance only at c2, when `smoke`'s binding goes.

5. **The workspace deployer's alert**,
   `swarm-<env>-workspace-deployer-outside-personal-workers`
   (`terraform/modules/monitoring/workspace_alerts.tf:226`, CRITICAL).
   * It pages on any binding the deployer **removes** (rule C9: the job only
     ever adds).
   * The owner's hand removals below are made by the owner, not the deployer,
     so they must **not** page it.
   * A page during this cutover means the deployer removed a binding: stop,
     and treat it as that alert says.

### How a tfvars change is made, in every step

1. Copy the bucket file out and edit the copy:

   ```bash
   current="$(scripts/lib/resolve.sh tfvars <env>)"
   cp "${current}" "${TMPDIR}/new-<env>.tfvars"
   diff -u "${current}" "${TMPDIR}/new-<env>.tfvars"
   ```

   This is `docs/ci.md`, "A new account exists before the release that adds
   it", step 2.

2. Have the diff, not the file, reviewed.
3. Write the reviewed copy to `gs://<state bucket>/config/<env>/<env>.tfvars`.
   The bucket is versioned, so the previous generation is the rollback (same
   section, step 4). That section names the path and not a write command, and
   neither does this runbook.
4. Start a release. Either merge to `main`, or open Actions › `release.yml` ›
   Run workflow with `environment` = `<env>` and `skip_build` = true (the
   `workflow_dispatch` inputs, `.github/workflows/release.yml:136-146`).
5. Read the plan in the release's `terraform plan` step **before** approving
   anything.

### a. The personal workspace (not a Terraform tenant)

The personal workspace is not a key of `tenants`. Lane W9's `removed` blocks
forgot its instances with `destroy = false`; the blocks were deleted on
2026-11-11, once the state held none of their addresses. So its binding is
live but unmanaged, and its switch is swarm-api's list, not the tenancy
module's.

#### a0. `tenant_job_vpc_egress = true`, and the scheduler-made Jobs refreshed

Who: the owner (the tfvars and the `dev-iam` approval), then the release.

The first a1, on 2026-10-21, was rolled back (the VPC-egress rollback PR).

* The personal workspace's Cloud Run Jobs (`swarm-job-<personal
  workspace>-*`) are made by swarm-scheduler, and they had no `vpc_access`.
* So its switched workers called swarm-api from the public internet.
* swarm-api's `internal-and-cloud-load-balancing` ingress answered
  `state/open` with a 404 before the app saw the request.

Its GKE attempts passed on the same release.

1. **The change.** Set `tenant_job_vpc_egress = true`
   (`terraform/infra/tenant_job_vpc.tf:31`, default `false` at :47).
   * **It worked when** the plan shows an in-place update of
     `module.cloud_run.google_cloud_run_v2_service.this["swarm-scheduler"]`
     (`TENANT_JOB_VPC_EGRESS` from `"false"` to `"true"`, `locals.tf:1478`).
     It also shows one created
     `google_compute_subnetwork_iam_member.scheduler_tenant_job_subnet[0]`:
     `roles/compute.networkUser` for swarm-scheduler on the swarm subnetwork
     and nowhere else.
   * It is IAM, so the release holds at `dev-iam`.
   * **Stop** on a grant on any other subnet, or at project level.
2. **Refresh the Jobs.** A new scheduler revision checks each Job it made once,
   at that Job's next dispatch, and rebuilds one that lacks the block
   (`_refresh_image`, `apps/scheduler/scheduler/dispatch.py:2558`). Dispatch
   one `mock` agent in the personal workspace for each `swarm-job-<personal
   workspace>-*`. **It worked when** the scheduler log reads `workspace,
   identity or network changed: vpc_access` (`dispatch.py:2669`).
3. **Check each Job:**
   `gcloud run jobs describe swarm-job-<personal workspace>-<profile> --project <project> --region <region>`.
   * **It worked when** it shows `run.googleapis.com/vpc-access-egress:
     all-traffic` and the `swarm-worker` tag, as a Terraform tenant's
     `swarm-job-<group workspace>-mock` does.
   * **Stop** on a Job without them: its switched worker is refused exactly
     as on 2026-10-21.

#### a1. Switch its state path

Who: the owner (tfvars), then the release.

1. **The change.** `state_via_api_tenants = ["<personal workspace>"]`.
2. **It worked when** the plan shows exactly one in-place update:
   `module.cloud_run.google_cloud_run_v2_service.this["swarm-scheduler"]`,
   whose environment gains `SWARM_STATE_VIA_API_TENANTS = "<personal
   workspace>"`. Nothing is created or destroyed, and there is no IAM change,
   so the release does not stop at `dev-iam`.
   * **Stop:** anything else in the plan is someone else's change. Hold.
3. **The regression check:** `make verify-remote` (`Makefile:444`, which runs
   `scripts/verify-remote.sh`).
   * **What it does not exercise.** It runs the `swarm-verify` job with no
     environment and sends no `X-Swarm-Tenant`. That was read 2026-10-20 at
     `4589544`. **Not re-checked** on 2026-11-24.
   * So it runs as its own identity's tenant, and it cannot select a personal
     workspace.
   * It is the regression check on admission, concurrency and fencing for this
     release, **not** a run of the personal workspace's switched path.
   * **Stop** on a red run, whatever the four items below say.
4. **The personal workspace's own gate** (owner decision, 2026-10-20). All four
   are required:
   1. **one agent to SUCCEEDED**: one of the workspace's own agents, submitted
      by the owner. Its worker's log shows that no Firestore client was built,
      and its `state/open` answered 200 in swarm-api's request log (watch item
      1);
   2. **one park and resume**: an agent that parks on a long provider wait and
      resumes, its `park` going through the routes and releasing the lease
      (invariant 4);
   3. **one cancel** of a running agent, its `release` going through the
      routes and releasing the lease;
   4. **the probe from one of its pods** (watch item 4). It can pass only once
      the role is gone, so it closes a2.

   Items 1-3 come before a2 starts.
5. **Stop** if any watch-item stop appears, or any of items 1-3 fails: roll a1
   back.

#### a2. Withhold and remove its Firestore role

Who: the owner, entirely. A release first, then an admin action, then a hand
IAM removal.

Only after a1's items 1-3 are green and watch items 1-2 are quiet.

**Drain first.** No attempt of the personal workspace that was LEASED before
a1's release's deploy time may still be LEASED, DISPATCHED, STARTING or
RUNNING. PARKED attempts hold nothing and resume switched.

1. **The change.** `worker_firestore_off_tenants = ["<personal workspace>"]`.
   * **It worked when** the plan shows one in-place update of
     `module.cloud_run.google_cloud_run_v2_service.this["swarm-api"]`, whose
     environment gains `WORKER_FIRESTORE_OFF_TENANTS = "<personal
     workspace>"`.
   * There is no IAM change.
2. **The admin action, so the record takes the switch.** The workspace is
   `ready`, so the one admin action that writes the record is a ceiling
   change.
   * Read its current `max_active` and its `<workspace id>` from **Admin ›
     People**.
   * Set the ceiling to the **same** value there, or with
     `scripts/api.sh PUT /admin/workspaces/<workspace id>/limits '{"max_active": <the same value>}'`
     (the route is `set_workspace_limits`,
     `apps/swarm-api/swarm_api/routes/people.py:732`).
   * `People.set_ceiling` (`people.py:1024`) writes `worker_firestore_access`
     from the list (`_access_patch`, :628). **Not checked:** the call from
     `set_ceiling` to `_access_patch`, read past on 2026-11-24.
   * The same value changes no limit. The `limits` run it starts re-applies
     the namespace and the documents and **makes no IAM call**.
3. **Verify, expecting red:**
   `scripts/register-tenant.sh --workspace <workspace id> --mode verify`
   (A9, `scripts/register-tenant.sh:1495-1508`).
   * **It worked when** A9 fails `VERIFY_FAILED` on object `project grant`,
     printing that the worker still holds
     `projects/<project>/roles/swarmTenantWorkerFirestore` and that its record
     says `worker_firestore_access false`.
   * **Stop** if it passes instead: the record did not take the switch. Read
     the record before removing anything.
4. **Remove the binding by hand.** Rule C9: the job never removes one.

   ```bash
   gcloud projects remove-iam-policy-binding <project> \
     --member=serviceAccount:swarm-agent-worker-<personal workspace>@<project>.iam.gserviceaccount.com \
     --role=projects/<project>/roles/swarmTenantWorkerFirestore \
     --condition=None
   ```

   * `--condition=None` names the unconditioned binding, which is the only
     shape this role was ever granted in (`scope_firestore_to_database`
     defaults off).
   * If step 3 printed the role as `(conditioned)`, pass that binding's
     condition instead.
   * If it printed a `roles/datastore.*` role too, remove that the same way:
     A9 refuses any Firestore role.
5. **Verify, expecting green.**
   1. Run the same `--mode verify`: it prints `verified: …` and the workspace
      is `ready`.
   2. Run the probe from one of the workspace's pods: all three checks PASS.
   3. Run `make verify-remote`: green.
6. **Stop** on any `PermissionDenied` (watch item 3) or a red step 5: roll a2
   back.

### b. The group workspace (a Terraform tenant)

#### b1. Switch its state path

Who: the owner (tfvars), then the release.

1. **The change.** `state_via_api_tenants = ["<personal workspace>", "<group workspace>"]`.
   The plan, the release and the rollback are a1's, with both names as the
   rendered value.
2. **It worked when** `SWARM_API_TENANT=<group workspace> make verify-remote`
   is green.
   * This one exercises the switched path, if the verify job's tenant is the
     group workspace. Read that from the job's transcript.
   * Its smoke, concurrency, race and e2e suites then submit, admit, fence and
     release the group workspace's attempts through the routes. That is the
     `make smoke concurrency-test race-test` that §6's order (and `CLAUDE.md`)
     asks for after a dispatch change.
3. **Stop** on a failure of any suite: roll b1 back and file it.

#### b2. Its Firestore role, by plan

Who: the owner (tfvars and the `dev-iam` approval), then the release.

The group workspace's binding is
`module.tenancy.google_project_iam_member.worker_firestore["<group workspace>"]`,
which is in Terraform state. It goes by plan, never by hand: removed by hand,
the next release would re-create it as `1 to add`.

Start only after b1's verify-remote is green and watch items 1-2 are quiet.
**Drain first**, as a2 says.

1. **The change.** `worker_firestore_off_tenants_iam = ["<group workspace>"]`.
2. **It worked when** the plan shows exactly one row, a destroy:
   `module.tenancy.google_project_iam_member.worker_firestore["<group workspace>"]`
   (role `projects/<project>/roles/swarmTenantWorkerFirestore`, member
   `serviceAccount:swarm-agent-worker-<group workspace>@<project>.iam.gserviceaccount.com`).
   * It is an IAM change, so the release holds at `dev-iam`.
   * **Stop:** a plan that touches another tenant's binding, any account,
     object grant or telemetry grant, or swarm-api's environment, is wrong.
     Hold.
3. **After the apply**, read:
   * watch items 1-3;
   * P4's policy command: one line fewer, and every other Terraform tenant
     still named. Count the lines; an empty answer from a command that did
     not run is not an empty policy;
   * the probe from one of the group workspace's pods: all three PASS;
   * `SWARM_API_TENANT=<group workspace> make verify-remote`: green.
4. **Stop** on any `PermissionDenied` or a red check: roll b2 back.

`worker_firestore_off_tenants` (swarm-api's list) is not this step. It writes
job-made workspace records, and a Terraform tenant has none.

### c. Every remaining tenant, then the role

#### c1. Switch every remaining tenant

Who: the owner (tfvars, admin actions, hand removals), then the release.

1. **List every tenant in the environment:** every key of `tenants` in the
   tfvars (P1) and every job-made workspace's tenant (Admin › People).
   * A tenant that never runs an agent may be marked `runs_no_work = true`
     instead of being switched (open decision D2).
2. **Switch the rest.** Add them to `state_via_api_tenants`, in one change or
   several, each with a1's plan and rollback.
   * `smoke` is the acceptance suite's tenant, so the release's acceptance run
     after it is the gate for that switch.
3. **For every job-made tenant, a2 in full:**
   * list it in `worker_firestore_off_tenants`;
   * the same-value ceiling change;
   * the red verify;
   * the hand removal with its own member
     `serviceAccount:swarm-agent-worker-<tenant>@<project>.iam.gserviceaccount.com`;
   * the green verify.

   Drain each first.
4. **Stop** on any watch-item stop, or a red acceptance run after `smoke`'s
   switch.

#### c2. `worker_firestore_access = false`

Who: the owner (tfvars and the `dev-iam` approval), then the release.

1. **The change.** `worker_firestore_access = false`.
   * The plan refuses it until every key of `tenants` is in
     `state_via_api_tenants` or marked `runs_no_work`.
   * Drain the Terraform tenants' unswitched attempts first.
   * Leave `worker_firestore_off_tenants_iam` as it is in this change.
     Emptying it at the same time is harmless while `worker_firestore_access`
     is false, but it makes c2's rollback re-grant those tenants too.
2. **It worked when** the plan shows a destroy of exactly one binding per
   Terraform tenant that still has one, and nothing else.
   * In dev, after b2, that is
     `module.tenancy.google_project_iam_member.worker_firestore["smoke"]` and
     any other tenant not already in `worker_firestore_off_tenants_iam`.
   * Every other `module.tenancy` resource (the accounts, the object grants,
     the telemetry grants) is unchanged.
   * It is an IAM change, so the release holds at `dev-iam`.
3. **Then:**
   * the probe goes green in acceptance, because `smoke`'s binding is gone;
   * `make verify-remote` is green;
   * P4's command prints no `swarm-agent-worker-*` member. Count the lines
     and say the count when reporting.
4. **Stop** on any `PermissionDenied`, a red verify-remote, or a member still
   printed.

### d. The role definition (owner, bootstrap)

Who: the owner, with `make bootstrap` (`Makefile:87`, which runs
`scripts/bootstrap.sh`). The release never applies the bootstrap root
(`docs/operations.md`).

Only after c2 has been live long enough that nobody expects to roll it back
(open decision D4). Then nothing should hold `swarmTenantWorkerFirestore`, and
the definition can go. It is in three places:

* `terraform/bootstrap/platform_roles.tf:286`, the `worker_firestore` entry of
  the platform roles (its five permissions);
* `terraform/bootstrap/deployer_conditions.tf:406`, `"worker_firestore"` in
  the list of roles the release deployer may grant. Off it, CI can no longer
  hand the role to anyone;
* `terraform/bootstrap/workspace_deployer.tf:167`,
  `module.custom_role_ids.names.worker_firestore` in
  `workspace_worker_project_roles`, the roles the workspace job's conditioned
  `projectIamAdmin` may add. Off it, the workspace job cannot grant the role
  even from a forged record.

1. **The change**, in two bootstrap applies:
   1. Remove the deployer-condition and workspace-deployer entries, so nothing
      can grant the role.
   2. Then remove the definition.

   Each is a reviewed pull request merged to `main` first. `scripts/bootstrap.sh`
   applies `main`'s code after a typed confirmation (`confirm ... "apply"`,
   `scripts/bootstrap.sh:348-350`).
2. **It worked when** the bootstrap plan shows only those changes.
3. **Stop** on any other row.

The workspace job's guard and A9 still name the role
(`scripts/register-tenant.sh`, `scripts/lib/workspace-calls.json`), and
`terraform/modules/tenancy` still has the switches. Their cleanup is its own
lane after the definition is gone, and its tests change with it
(`tests/terraform/platform_roles.tftest.hcl`, `deployer_iam.tftest.hcl`,
`custom_roles_out_of_ci.tftest.hcl`).

---

## 5. Rollback

The order is always **re-grant first, then unswitch**, never the other way
round. A switched tenant with no role is safe; an unswitched worker with no
role is not.

| to undo | do, in order | what is lost |
|---|---|---|
| a0 | 1. Set `tenant_job_vpc_egress = false`.<br>2. Release (through `dev-iam`, a destroy of the subnetwork grant). | nothing. Only after a1 is rolled back: a switched personal workspace needs the egress. |
| a1 / b1 / c1 switch | 1. Remove the tenant from `state_via_api_tenants`.<br>2. Release. | nothing. Its role is still bound, so its next attempt takes the direct path. An attempt dispatched switched keeps its environment and finishes through the routes. |
| a2 (or a c1 hand removal) | 1. Re-grant by hand (below).<br>2. Remove the tenant from `worker_firestore_off_tenants`.<br>3. Release.<br>4. Make the same-value ceiling change, so the record reads `true` again. The `limits` run grants nothing; the hand grant is what restores access.<br>5. Only then a1's rollback, if it is wanted. | attempts that failed `PermissionDenied` before the re-grant: retried, partial work back to the last checkpoint |
| b2 | 1. Remove the tenant from `worker_firestore_off_tenants_iam`.<br>2. Release. The plan re-creates the binding, `1 to add`, through `dev-iam`. | the same |
| c2 | 1. Set `worker_firestore_access = true`.<br>2. Release. It re-creates every binding not in `worker_firestore_off_tenants_iam`, through `dev-iam`. | the same |
| d | 1. Revert the bootstrap change.<br>2. Apply it with `make bootstrap`.<br>3. Within seven days of a deletion, `gcloud iam roles undelete` (`docs/design/day-one-install.md`, "Custom role ids"). | after seven days the role id cannot be reused for 37 days |

**Faster in an outage**, for b2 and c2: re-grant by hand per tenant, then
release. The release's create of an existing member is a no-op.

The hand re-grant:

```bash
gcloud projects add-iam-policy-binding <project> \
  --member=serviceAccount:swarm-agent-worker-<tenant>@<project>.iam.gserviceaccount.com \
  --role=projects/<project>/roles/swarmTenantWorkerFirestore \
  --condition=None
```

A tfvars rollback can also restore the previous generation of the bucket
object, which is versioned (`docs/ci.md`, "A new account exists before the
release that adds it", step 4), and then release.

---

## 6. Verify afterwards

* **The probe group.** `acceptance (tenant-isolation)` in `accept.yml`
  (`scripts/acceptance/groups/tenant-isolation.sh`;
  `./scripts/verify-remote.sh acceptance/tenant-isolation`, `docs/acceptance.md`
  "Running it", and its `### tenant-isolation` section).
  * It passes only with three `ISO-ACCEPT-RESULT` lines that say `PASS`.
  * It is green in acceptance only after c2.
  * Per workspace, run it by hand from that workspace's pod after a2 and b2.
* **The regression gate.** `make verify-remote` after each step, and
  `SWARM_API_TENANT=<group workspace> make verify-remote` after b1 and b2.
  The latter is `CLAUDE.md`'s `make smoke concurrency-test race-test` for a
  dispatch change.
* **The live policy.** P4's command prints no `swarm-agent-worker-*` member
  after c2.
* **Every workspace's own A9.** `scripts/register-tenant.sh --workspace
  <workspace id> --mode verify` is green for every job-made workspace.

### Invariants

* Invariants 1-3 (capacity) are untouched: no switch changes admission or
  leasing.
* Invariant 4 is exercised by a1's park item.
* Invariant 5 holds: a fenced attempt is `attempt_fenced` on the routes and
  exits without running the agent.
* Invariant 9 is strengthened: per-tenant isolation now holds for Firestore
  too.

---

## 7. Open decisions for the owner

Also in `questions.json` beside the pull request that wrote this revision.

**D1. Where does dev resume?** The epic says one workspace is switched for the
worker and one more for access rights only. That was read from code, not live.
* (a) Read P1 and resume from the first step not done. **Recommended.**
* (b) Treat every step as not done and start from a0. Harmless, since each
  plan is a no-op when already applied, but slower.

**D2. Workspaces that never run agents.**
* (a) Mark them `runs_no_work = true` and take their role away before c2.
  This is the owner's 2026-11-21 decision for the roles and join acceptance
  workspaces. **Recommended.**
* (b) Switch them like every other tenant.

**D3. When to set `ISO_PROBE_REQUIRED=true`.**
* (a) In the same hour as c2's green probe. **Recommended:** a regression is
  then S0 at once.
* (b) After one week of green report-only runs.

**D4. How long c2 stays live before d removes the role definition.**
* (a) 14 days of no `PermissionDenied` and a green probe on every acceptance
  run. **Recommended:** it matches the window the graph-service retirement
  uses.
* (b) 30 days, the role's undelete window plus margin.
* (c) Never remove it; keep the definition ungranted.

---

## Appendix: the switches, what the plan refuses, and where dev stands

### The four switches

All four default to today's behaviour. Checked on `main` 2026-11-24.

| switch | where | default | what it switches |
|---|---|---|---|
| `state_via_api_tenants` | `terraform/infra/child_tasks.tf` (variable at :94, default `[]`), rendered onto swarm-scheduler as `SWARM_STATE_VIA_API_TENANTS` (`terraform/infra/locals.tf:1470`) | `[]` | The scheduler dispatches every attempt of a listed tenant, children included, with `SWARM_STATE_VIA_API=true`, `SWARM_API_URL` and a registration nonce. The worker then builds no Firestore client and reaches its state only through the state routes. |
| `worker_firestore_off_tenants` | `terraform/infra/variables.tf:1536`, rendered onto swarm-api as `WORKER_FIRESTORE_OFF_TENANTS` (`locals.tf:926`) | `[]` | swarm-api writes `worker_firestore_access: false` on a listed tenant's **job-made** workspace record at its next approval, retry or ceiling change. The workspace job then grants that worker no Firestore role, and its A9 check requires it to hold none ([`docs/workspaces.md` §2.7](../workspaces.md#27-the-workers-firestore-switch-follows-its-tenant-design-b)). |
| `worker_firestore_off_tenants_iam` | `terraform/infra/variables.tf:1504`, passed to `module "tenancy"` (`terraform/infra/main.tf:243`; module variable `terraform/modules/tenancy/variables.tf:43`) | `[]` | Removes `module.tenancy.google_project_iam_member.worker_firestore[<tenant>]` for **each listed Terraform tenant alone**. Every other tenant's binding keeps its key (the `for_each` filter in `terraform/modules/tenancy/main.tf`, resource `worker_firestore`), so it is not touched. |
| `worker_firestore_access` | `terraform/infra/variables.tf:1472`, passed to the module (`main.tf:238`) | `true` | `false` removes `module.tenancy.google_project_iam_member.worker_firestore[<tenant>]` for **every Terraform tenant at once**. |

### What the plan refuses

The plan refuses each switch out of order, so a mistyped tfvars stops at plan
rather than failing workers:

* `state_via_api_tenants` needs `enable_child_tasks`, because a switched worker
  spends the child path's nonce at `state/open`. It takes tenant ids only,
  never `*` (`child_tasks.tf:113`, `:118`).
* Every `worker_firestore_off_tenants` entry must already be in
  `state_via_api_tenants` **or be a tenant marked `runs_no_work`**, and `*` is
  refused (`variables.tf:1553`, `:1558`).
* Every `worker_firestore_off_tenants_iam` entry must be:
  * a tenant id (`*` fails the id pattern, `variables.tf:1517`);
  * in `state_via_api_tenants`, **or marked `runs_no_work`** (`:1522`);
  * a key of `var.tenants` (`terraform/modules/tenancy/variables.tf:50`).
* `worker_firestore_access = false` needs every key of `var.tenants` either in
  `state_via_api_tenants` **or marked `runs_no_work`** (`variables.tf:1486`).

`runs_no_work` (`var.tenants[*].runs_no_work`, `variables.tf:367`, default
false) is the owner's decision of 2026-11-21
([`docs/multi-tenancy.md`, design B step 4, "A tenant that runs no work"](../multi-tenancy.md)).
A workspace that never dispatches an agent may drop the role before any state
switch. The two acceptance probe workspaces, roles and join, are the cases
named there.

`tests/terraform/worker_firestore_switches.tftest.hcl`,
`worker_firestore_off_tenants_iam.tftest.hcl` and `state_via_api_env.tftest.hcl`
hold all of that (all three present on `main`, 2026-11-24).

### Where dev stands

**Not read live.** The epic (2026-11-14, read from code, not live state) says
the cutover "covers one tenant for the worker and one more for IAM only". That
is one tenant in `state_via_api_tenants` and one more in
`worker_firestore_off_tenants_iam` (or `worker_firestore_off_tenants`). By
the validations above, the second can only be a `runs_no_work` tenant or one
that is also switched.

The real values live only in the state bucket. Precondition P1 reads them, and
the step to resume from follows from what it prints. Do not assume the
history:

* The first a1, on 2026-10-21, was rolled back (the VPC-egress rollback PR,
  below).
* No later record in this repository says which steps were taken again.
