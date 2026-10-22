# Operations

The day-to-day runbook. Every command here is a script in `scripts/`; the
Makefile is a thin wrapper over them, and the scripts run identically from a
laptop and from CI.

---

## 1. First deployment

```bash
make prerequisites      # tools, credentials, APIs, shared-project guards
make bootstrap          # state bucket, .env, bootstrap layer (state in the bucket), terraform init
make tf-plan            # READ THIS. It is a shared project.
make tf-apply
make build              # Cloud Build, never the local Docker daemon
make push               # trivy scan, then promote by digest to :dev
make deploy             # terraform apply with every image pinned by digest
make smoke              # prove a task runs end to end
```

or `make up`, which is those steps in order.

**On a fresh project the first `make tf-plan` plans the registry alone.**
Terraform deploys every image by digest and refuses to plan one that has none,
and before the first build there are none — the registry they are pushed to is
created by this same root. So when terraform state holds no registry,
`scripts/plan.sh` plans only `module.project_services` and
`module.artifact_registry`. It does the same when state holds a registry that
nothing has been pushed to yet. `make deploy` then plans everything else with
the digests `make push` promoted.

"Fresh" is decided by terraform **state**, never by asking the registry. On a
fresh project the registry does not exist, so a tag listing fails with
NOT_FOUND, and that error has to stay an error: on a live project it means
drift or the wrong project. The first version asked the registry, and so it
refused to plan the bootstrap described here.

**A laptop `make tf-plan` refuses to plan a gated service away.**
`scripts/plan.sh` stops and names the image when state's `image_refs` output
has no digest for an image in `terraform/infra/images.json` whose service is
still in state, because without that digest the plan would destroy the service:
the GitHub push receiver was nearly lost this way (#1878). The next release's
apply records the digest.

**Read the plan.** `saga-agents-staging` holds another team's live GKE cluster,
their VPC, buckets, service accounts and Cloud Run services, every one listed
in `SHARED_DENY_LIST` in `scripts/lib/common.sh`. A plan that proposes to change
anything you do not recognise is a stop-and-ask, not a scroll-past.

Then register a tenant and give it a key:

```bash
make register-tenant GROUP=eng@saga.xyz
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
make add-provider TENANT=eng PROVIDER=anthropic   # let eng's worker read it, and list it
```

Adding a provider is `make add-provider` (`register-tenant.sh --add-provider`),
never a second `make register-tenant ... PROVIDERS=`. A re-run of the
registration writes the whole tenant record: `--providers` **replaces** the
tenant's `credentials`, so a tenant holding openai comes out holding anthropic
alone, and `max_active`, `capacity_units` and `display_name` go back to their
defaults unless typed again. `--add-provider` makes the only two changes adding
a provider needs: it grants the tenant's worker `secretAccessor` on that one
secret, and only after that adds the provider to `credentials`. The secret has
to be labelled as this tenant's key for this provider and hold an enabled
version before anything is granted. Add `DRY_RUN=1` to see what it would do.

**A tenant terraform will manage needs the owner's bootstrap step before its
release** (#334). The release deployer holds `roles/iam.serviceAccountAdmin`
on each account `terraform/infra` manages, not on the project, and the release
sets the new worker account's IAM in the same apply that adds the tenant. So:
register the tenant (which creates `swarm-agent-worker-<tenant>`), add it to
`terraform/environments/dev/dev.tfvars` on the pull request's branch, have the
owner run the targeted bootstrap apply the script prints — from `main`, with the
branch's `dev.tfvars` copied out by `git show` and passed as
`-var infra_tenants_tfvars`, never from the branch itself — and only then merge.
Skipping the apply fails the release with a 403 on that account. If the worker
account already existed, the script refuses to go on when it carries IAM
bindings the platform does not make or a user-managed key: terraform would
adopt that account, and whoever made it with them.
[docs/ci.md](ci.md#a-new-account-exists-before-the-release-that-adds-it) has
the commands and why.

`register-tenant.sh` creates the service account, the IAM conditions, the
Firestore documents *and* the whole GKE namespace — it calls
`kubernetes/apply.sh` for the namespace, so the NetworkPolicy, ResourceQuota,
LimitRange, Pod Security Admission labels and RBAC land with it. If kubectl is
not pointed at the swarm cluster it says so and skips that step; run
`make kubectl` and re-run. Nothing on the Cloud Run path needs the cluster, but
browser-class runners cannot be dispatched for a tenant that has no namespace.

To re-apply or inspect just the Kubernetes side of an existing tenant, without
touching IAM or Firestore:

```bash
kubernetes/apply.sh --tenant eng             # dry run, prints a diff
kubernetes/apply.sh --tenant eng --confirm
kubernetes/apply.sh --policies --confirm     # cluster-scoped admission policies
```

### The bootstrap layer's state

`make bootstrap` applies `terraform/bootstrap`, the root the owner applies: the
state bucket, the WIF pool, the deployer service account
(`swarm-tf-deployer`) and every grant it holds. **Its state lives in the state
bucket, at `gs://swarm-tfstate-<project>/bootstrap`** (#827), next to
`infra/<env>`. `terraform/bootstrap/backend.tf` fixes the prefix;
`scripts/bootstrap.sh` passes the bucket with `-backend-config`, as it does for
`terraform/infra`.

**Why it moved.** Until 2026-10 the root had only `backend.tf.example`, so its
one state was a local `terraform/bootstrap/terraform.tfstate` in one laptop
checkout (serial 118, 86 resource instances, last written 2026-10-04). Every
other checkout planned against nothing. On 2026-10-07 `scripts/bootstrap.sh`
run from a git worktree of `main` planned **9 to import, 91 to add**: the live
deployer, every WIF binding and every deployer role. Only the typed `apply`
stood between that plan and an attempt to re-create the identity every release
runs as. One local file is also one disk: losing it means re-importing the
layer by hand. In the bucket, the state is versioned and soft-deleted like the
environments' ([disaster recovery](disaster-recovery.md#5-terraform-state-loss)).

**The guard.** `scripts/bootstrap.sh` lists the bootstrap state before it plans.
If the state is empty, it refuses to plan when either:

* the checkout still holds a local `terraform.tfstate` with resources in it; or
* `swarm-tf-deployer@<project>` exists (`gcloud iam service-accounts
  describe`). This root is the only thing that creates it, so an empty state
  next to a live deployer means the state is somewhere else.

Either way it says to migrate first. It plans from an empty state only when
gcloud answers NOT_FOUND for the deployer: a project that has never been
bootstrapped. If gcloud cannot answer (an expired session, a permission, the
network), it refuses rather than guess. A bare `terraform -chdir=terraform/bootstrap plan`
skips the guard, so go through the script.

**The one-time migration**, run once by the owner **from the checkout that
holds `terraform/bootstrap/terraform.tfstate`** (the main checkout on the
owner's laptop), before any other `make bootstrap`:

```bash
cd <the checkout holding terraform/bootstrap/terraform.tfstate>
git pull --ff-only                    # main, with this change
scripts/bootstrap.sh --migrate-state  # type "migrate" at the prompt
```

What it does, in order. It refuses when there is no local
`terraform.tfstate` or the file holds no resources. It refuses when
`gs://<bucket>/bootstrap/default.tfstate` already exists, because the migration
has been done and the local file is older than the bucket's copy. It refuses
when it cannot tell whether that object exists. It copies the local file to
`build/bootstrap-local-<UTC time>.tfstate`. It asks for the typed word
`migrate`, which **`--yes` and `SWARM_ASSUME_YES` never skip**, and which
needs a terminal. It runs `terraform init -migrate-state -force-copy` against
the bucket. It checks that `terraform state list` on the bucket's copy has as
many instances as the local file had. Then it stops: no plan, no apply.

After it:

1. Run `scripts/bootstrap.sh --environment dev` from any checkout, a worktree
   included. The plan must show **no create of `google_service_account.deployer`**
   and no bulk of creates. Anything else is a stop-and-ask.
2. Once that plan is clean, move `terraform/bootstrap/terraform.tfstate` and
   `terraform.tfstate.backup` out of the main checkout. Terraform no longer
   reads them. While they stay, the script warns that they are a stale copy.
   The copy in `build/` is the rollback.

---

## 2. Every day

```bash
make status                       # one screen: is the platform healthy?
make logs                         # recent control-plane logs, redacted
make logs FOLLOW=1                # tail
make logs SERVICE=swarm-api LINES=200
make smoke                        # after any deploy
```

### Reading `make status`

```
Tasks
  demand-free : QUEUED 412  PARKED 31  READY 88  SUBMITTED 0
  holding cap : LEASED 3  DISPATCHED 2  STARTING 4  RUNNING 34
```

The split is the platform's core claim made visible. The first line costs
nothing. The second line is the bill.

| Section | What to look for |
|---|---|
| Agents | `PARKED` climbing -> quota or credentials; `READY` climbing with idle pools -> scheduler not draining |
| Leases | expired leases still holding capacity -> the reconciler is behind |
| Concurrency pools | `active/effective_limit`; a pool at its limit is the one to raise |
| Provider quota | `EXHAUSTED`/`COOLDOWN` per (provider, tenant) |
| Cloud Run | service readiness, job resources, recent executions |
| GKE | worker pods (every agent profile but `mock`), and **any restart count above zero** |
| Attention | the same findings, summarised |

A pod restart is always worth investigating: the platform promises no restarts,
so a restart means something evicted or OOM-killed a workload that should have
been immune.

`status.sh` reads Firestore **directly**, not through the API, on purpose — the
moment you most need it is the moment the API is unhealthy. It prints no secrets:
no environment variables, no payloads, no tokens. `--json` gives the same data
for scripting; `--watch [seconds]` refreshes; `--tenant <id>` narrows.

---

## 3. Changing capacity

Limits live in Firestore, so this needs no redeploy:

```bash
./scripts/api.sh PUT /admin/limits/global             '{"hard_limit": 150}'
./scripts/api.sh PUT /admin/limits/provider/anthropic '{"hard_limit": 40}'
./scripts/api.sh PUT /admin/limits/tenant/eng         '{"hard_limit": 25}'
./scripts/api.sh PUT /admin/limits/resource/large     '{"hard_limit": 4}'
./scripts/api.sh PUT /admin/limits/runner/claude-code '{"hard_limit": 60}'
```

Lowering a limit never kills anything: running agents keep their leases and the
pool admits nothing until `active` falls below the new ceiling. Fold changes you
mean to keep into `terraform/environments/<env>/<env>.tfvars`, or the next apply
will revert them.

---

## 4. Pausing

```bash
./scripts/pause-swarm.sh                      # global pool + safety tick
./scripts/pause-swarm.sh --tenant eng
./scripts/pause-swarm.sh --provider anthropic
./scripts/pause-swarm.sh --all --drain        # everything, then wait for zero
./scripts/resume-swarm.sh                     # undo exactly that
```

Pausing flips `enabled=false` on pools and pauses the Cloud Scheduler tick. It
does **not** kill running work: a running agent holds a lease, a workspace and
partial progress that checkpointing can resume but killing would waste.

Every change is recorded in `build/pause-state-<env>.json`, and `resume-swarm.sh`
re-enables exactly that set — never "everything", because a pool may have been
paused separately by another operator or by the quota broker.

---

## 5. Tenants and keys

```bash
# First registration. A re-run rewrites the whole record (see section 1), so it
# is not how a provider is added.
make register-tenant GROUP=eng@saga.xyz
make register-tenant USER_EMAIL=alice@example.com

./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
make add-provider TENANT=eng PROVIDER=anthropic   # grant + list; keeps eng's other providers
./scripts/create-secrets.sh --list --tenant eng

# Rotation: add the new version, let workers pick it up, then disable the old
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin --disable-previous
```

The key is never an argument (argv is readable through `ps`) and is never echoed.

A tenant without a key for a profile's provider is not an error: agents park as
`CREDENTIAL_MISSING`, cost nothing, and start by themselves when the key
arrives. A tenant with no key is still admitted if a pool account it owns or is
lent can run the profile. That is `claude-code` only, never `browser`: see
[quota management, section 4](quota-management.md).

---

## 6. Investigating one agent

```bash
TASK=tsk_...

./scripts/api.sh GET "/tasks/$TASK"           | jq '{state, park_reason, blocked_by, attempt_count, last_error}'
./scripts/api.sh GET "/tasks/$TASK/events"    | jq -r '.events[] | "\(.at) \(.type)"'
./scripts/api.sh GET "/tasks/$TASK/artifacts"
```

`blocked_by` names the exact pool and reason. `TENANT_LIMIT` and
`GLOBAL_CONCURRENCY_LIMIT` are different problems: the first is yours to raise,
the second is the platform being full.

The event stream is the truth about an attempt: `lease_acquired`, `dispatched`,
`starting`, `running`, `checkpoint_*`, `quota_exhausted`, `generation_fenced`,
`succeeded`/`failed`.

Worker logs for one attempt:

```bash
gcloud logging read \
  'resource.type="cloud_run_job" AND labels.swarm_task_id="'"$TASK"'"' \
  --project "$PROJECT_ID" --limit 100 --format='value(timestamp,jsonPayload.message)' \
  | ./scripts/lib/redact-stream.sh
```

---

## 7. Deploying a change

**The normal path is CI.** A merge to `main` runs `release.yml` — `verify`,
`images and scan`, `approval`, `promote`, `terraform apply`, `deploy and
smoke`. Only `approval` names a GitHub environment: a prod release waits there
for its required reviewer before anything is promoted or applied, and dev has
no reviewer ([why](ci.md#a-prod-release-waits-for-approval-before-anything-prod-facing)).
The sequence below is the same thing
driven by hand, for an operator who already holds the credentials and is doing
a break-glass deploy or a dev-environment push. It is not what someone
authoring a change runs; see [where the gates run](ci.md).

**A run of `cancelled` dev releases means one is waiting at `dev-iam`.** It
holds the `release-dev` group, and each newer release cancels the one pending
behind it, so nothing deploys until it ends. Find it with `gh run list
--workflow release.yml --status waiting`; reject it if main has moved past it or
its plan would fail ([why, and how to tell](ci.md#a-held-dev-iam-plan-cancels-every-release-behind-it)).

```bash
make lint test          # shellcheck, terraform fmt/validate, tflint, unit tests
make build push deploy
make smoke
```

**A new swarm-api refusal deploys switched off.** It logs `refusal report-only`
with the code it would have refused, and lets the request through until
`api_refusals` in the environment's tfvars turns it on. On 2026-10-07 a
refusal deployed ahead of the change that let callers satisfy it locked out
submissions for two hours. See [api-refusals.md](api-refusals.md).

Images are tagged with the immutable git SHA; `push` promotes a **digest** to the
channel tag after a trivy scan. Nothing downstream deploys a mutable tag, so
"what is running" is always a digest that was actually built and scanned.

Rollback is the previous manifest, deployed the same way as any other:

```bash
scripts/lib/deploy.sh --manifest path/to/the/previous/deployed-images-dev.json
```

`build/deployed-images-<env>.json` records what each deploy promoted, and the
release keeps every one it applied as the `pinned-images-<env>` artifact for 30
days, so the previous digests are there rather than in someone's shell history.

**Not `gcloud run services update --image`.** It used to be the documented
rollback, and it is now wrong in two ways: it moves one service while the
worker jobs and the scheduler's `WORKER_IMAGE_REFS` stay on the new digests,
and the next `terraform apply` — which pins what terraform last applied —
silently moves that service forward again. `scripts/plan.sh` warns when
swarm-api is serving something other than what terraform applied, which is
what an out-of-band change looks like from there.

**Not to a manifest from before PR #44, and never a revert of #44.** Any
`swarm-api` image built before #44 cannot decode the `cancel_requested` events
written since. It returns 500 on those agents' event pages for as long as it
serves, not just one rollout. Roll forward instead.
[disaster-recovery.md §3](disaster-recovery.md#3-a-control-plane-service-is-broken)
says why, and gives the one-line check for a rollback target.

---

## 8. Verification suite

```bash
make test              # unit tests + the destroy-guard self-test. No cloud needed.
make smoke             # end to end, mock profile, no provider key required
make concurrency-test  # no pool ever exceeds its limit, sampled continuously
make race-test         # last free slot, cancel-during-dispatch, stale generation
make quota-test        # exhaustion parks instead of paying to wait
make failure-test      # every failure path returns its capacity
make load-test         # latency percentiles and throughput
```

Run `smoke` after every deploy. Run `concurrency-test` and `race-test` after any
change to admission, dispatch or the reconciler. `quota-test` restores whatever
quota state it changed on every exit path, as does `race-test` with pool limits.

---

## 9. The danger zone

```bash
make pause-swarm       # reversible, kills nothing
make purge-data        # deletes RUNTIME DATA, never infrastructure
make destroy           # deletes swarm infrastructure, aborts on anything not ours
```

`purge-data.sh` exports Firestore to GCS before deleting anything (unless
`--no-backup`), refuses to touch the `(default)` database, and refuses any
deny-listed bucket. It leaves `pools`, `quota` and `tenants` alone unless
`--all`, because deleting a pool document with an active lease leaks that lease's
capacity permanently.

`destroy.sh` is the safety-critical one:

1. `terraform plan -destroy -json`;
2. assert **every** resource marked for deletion carries
   `managed-by=swarm-terraform`, or is of a type that physically cannot carry a
   label — unknown unlabelable types are treated as offenders, so it fails
   **closed**;
3. assert nothing in the plan names anything on the shared deny-list;
4. abort loudly, printing every offender, if either assertion fails;
5. refuse in prod without `--allow-prod`;
6. require a typed confirmation — `SWARM_ASSUME_YES` is explicitly ignored;
7. after applying, re-check that every shared resource is still there.

Always start with `./scripts/destroy.sh --dry-run`. The guard has a self-test
(`--self-test`, also run by `make test`) so it is verifiable without a live plan.

---

## 10. Alerts and what to do about them

Terraform creates these when `create_alerts = true`:

| Alert | Means | First action |
|---|---|---|
| `safety-tick-stopped` | no scheduler tick in 10 minutes | check Cloud Scheduler job and the scheduler's Cloud Run service |
| `control-plane-5xx` | a service is failing requests | `make logs SERVICE=<name>` |
| `job-executions-failing` | agent executions failing | inspect one agent's events and worker logs |
| `generation-fencing` | workers exiting on stale generations | the reconciler is racing live workers; check lease timeouts |
| `wake-messages-dead-lettered` | the scheduler is not acking pushes | scheduler errors, or Firestore unreachable |
| `scheduler-not-draining` | wake backlog sustained | raise scheduler instances or leases/run |
| `tasks-dead-lettered` | agents exhausting every attempt | a broken runner or a bad repository; also a cost problem |

`generation-fencing` firing repeatedly deserves attention even though the system
is behaving correctly — it means the reconciler keeps deciding live workers are
dead, which usually means heartbeat or lease timeouts are too tight for real
start-up latency. It does not count the `smoke` and `u-sw-c90291`
(swarm-verify's) tenants (`fencing_alert_excluded_tenants`): their own runs
fence at startup by design, and they were 215 of the 270 fences in the seven
days #1817 measured. The `generation_fenced` metric still counts them.

Nor does it count fences of agents submitted by the service accounts in
`fencing_alert_excluded_submitters` (default `swarm-verify`). A tenant filter
was not enough. verify-remote's race and concurrency tests fence on purpose,
but verify-remote submits as `eng`. Over the seven days measured on
2026-10-22, eng had 347 fences and smoke 57, and no tenant filter could drop
verify-remote's without hiding eng's real races. So the worker's
`generation_fenced` event line carries `submitter_service`, which the metric
extracts as a label. It holds the submitting service account's short name
(`swarm-verify` for `swarm-verify@<project>.iam.gserviceaccount.com`), or
`person` for anyone else. **It never holds a person's email:** logs and metric
labels are read widely, and an address there is personal data
(`agent_worker.logs.submitter_service`). The worker takes the submitter from
the document it already read for the agent, and a fence line with no submitter is
still counted.

`pr-stranded` notifies once a pull request has been reported stranded for an
hour, as one incident per tenant and reason that stays open while any stays
stranded and closes about seven hours after the last is merged. Before #1817
every sweep entry (520 in seven days) opened its own incident.

### Alert notification channels

Until 2026-10-21 all 21 policies had an empty channel list and reached nobody
(#1817): `alert_emails` was `[]` and nothing said so. Routing is now by
severity, the owner's choice of 2026-10-21:

| Severity | Policies | Notifies |
|---|---|---|
| CRITICAL (6) and ERROR (8) | the tick, the isolation and IAM tripwires, 5xx, dispatch, the reconciler, the wake backlog and dead letters | `urgent_notification_channels` (Slack) **and** `extra_notification_channels` (email) |
| WARNING (7) | `generation-fencing`, `job-executions-failing`, `pr-stranded`, `schedule-needs-attention`, `tasks-dead-lettered`, `worker-action-ended`, `workspace-stuck` | `extra_notification_channels` (email) only |

Each policy takes its channels from its own `severity` field
(`channels_by_severity` in `terraform/modules/monitoring/alerts.tf`), so a
policy changes route by changing its severity, never by joining a list.

**The channels are made in the console, not in Terraform**: Monitoring →
Alerting → *Edit notification channels*. Add an **Email** channel and a
**Slack** channel there (Slack needs a workspace admin to allow "Google Cloud
Monitoring", and a private Slack channel also needs `/invite @Google Cloud
Monitoring`), and press *Send test* on each. A Slack channel made in Terraform
would put its auth token in state, which the repository forbids.

**Only the channel IDs are committed**, in
`terraform/environments/<env>/<env>.tfvars`:

```hcl
extra_notification_channels  = ["projects/<project>/notificationChannels/<email channel id>"]
urgent_notification_channels = ["projects/<project>/notificationChannels/<slack channel id>"]
require_alert_channels       = true
```

Never an email address, a phone number or a token: the repository is public,
and `alert_emails` publishes the address it is given. `TF_VAR_alert_emails` is
not a way round that: the `-var-file` overrides it, and a plan run without it
deletes the channel.

`require_alert_channels = true` belongs in the same change as the IDs. With it,
alerts on and every channel list empty fails the plan (a precondition on
`control-plane-5xx`) instead of quietly reaching nobody again. It is false by
default only so that it cannot turn the release plan red before the IDs exist.

---

## 11. Local development

```bash
make dev                 # Firestore emulator + seeded pools + the API
make dev TARGET=scheduler
make dev TARGET=emulator
```

The emulator is a Java program; on a machine with no JRE `dev.sh` says so and
points at `docker compose up firestore`. See [workflows.md](workflows.md) for the
full local loop and for why `docker-compose.yml` is the secondary path.

---

## 12. Issue runs: the advance tick, and reading a stuck run

An issue run (#454, [issue-runs.md](issue-runs.md)) moves when something looks
at it: every read of the run, and a Cloud Scheduler job per registered tenant,
`swarm-issue-run-advance-<tenant>` (`google_cloud_scheduler_job.issue_run_advance`,
every minute, `var.issue_run_advance_schedule`). It calls
`POST /v1/admin/runs/advance?tenant_id=<t>` as the `swarm-rollup-sweeper` OIDC
identity, the same account as the workflow rollup, which swarm-api admits to
those two routes and nothing else. Without it an `auto` run never leaves
`PLANNED` and no unwatched run's issue comments move.

```bash
gcloud scheduler jobs describe "swarm-issue-run-advance-$TENANT" \
  --project "$PROJECT_ID" --location "$REGION" --format='value(state,schedule,lastAttemptTime,status)'

# One tick by hand, as an admin. The report counts what it visited and moved;
# a run it could not advance is named with its error code, never its message.
./scripts/api.sh POST "/admin/runs/advance?tenant_id=$TENANT"
```

A personal `u-` tenant has no job (it is not in `var.tenants`); its runs move
only when read.

**Reading one run.** A read advances it first, so what it says is current:

```bash
RUN=run_...
./scripts/api.sh GET "/runs/$RUN" | jq '.run | {state, error, planner_task_id,
  workflow_id, pull_request, ci_fix_round, fix_rounds, ci_fix_workflows,
  writeback_error, history: [.history[] | "\(.at) \(.from) -> \(.to) by \(.by)"]}'
```

Where it is waiting tells you whose agent to read:

| state | it is waiting on | read next |
|---|---|---|
| `PLANNING` | the planner agent | `GET /tasks/<planner_task_id>`: a planner `QUEUED`/`PARKED` is capacity, not the run ([section 6](#6-investigating-one-agent)) |
| `PLANNED` | a person: approve, edit or reject | nothing is running and nothing is held (invariant 1) |
| `RUNNING` | the compiled workflow | `GET /workflows/<workflow_id>` |
| `CHECKING` | CI on the pull request's head | `pull_request.checks`; `check_counts` (passed/failed/pending/skipped), `check_list` (each check's name, state and link, capped at 50 with `check_list_truncated`) and `ci_url` say which checks; `merged`/`merged_at` once GitHub reports it merged; `pull_request.read_error` if CI could not be read. On a run stored before these were recorded each serves `null`, never `0` |
| `FIXING` | the newest CI fix round | `GET /workflows/<last of ci_fix_workflows>` |

Stuck in `PLANNING` or `CHECKING`, and `writeback_error`, are in
[troubleshooting](troubleshooting.md#an-issue-run-is-stuck-in-planning-or-checking).
The swarm-api log lines are `issue run <id> tenant=<t> not advanced by the tick
(<code>)` and `issue run <id> tenant=<t>: CI not read (<code>)`.

---

## 13. Schedules: the S9 migration (SWEEP removed 2026-10-12)

**State from 2026-10-11 (Schedules lane S9, [schedules.md §8.1-§8.2](schedules.md#8-migration)).**
Recurring tenant work is a schedule, not a Cloud Scheduler job per purpose.

| what | state | why |
|---|---|---|
| `issue-sweep` schedule in `eng` (`sch_6ecbe1307511`, firing since 2026-10-11 19:40Z), plus a weekly `repo-index-refresh` | **live** once an operator posts them (below) | they replace lane SWEEP's job and the operator's session cron |
| `google_cloud_scheduler_job.issue_sweep` (`swarm-issue-sweep-<t>`) | **paused** 2026-10-11 (§8.1 step 3), **deleted** 2026-10-12 | two triggers of one sweep must not run side by side |
| `POST /v1/admin/issues/sweep` and its `auth.ROLLUP_SWEEPER_ROUTES` entry, `GET`/`PUT /v1/admin/tenants/{id}/issue-sweep`, `SWEEP_ENABLED` / `var.enable_issue_sweep`, the reads of the tenant's `issue_sweep` settings | **deleted** 2026-10-12 (lane SWEEP-RM, §8.1 step 4) | the owner approved the second S9 change on 2026-10-12 and **waived the planned 7-day watch**: the schedule replaced all of them, and a paused auto-merging fallback beside it was a second trigger waiting to happen |
| session cron `bdb47d30` | the operator's to delete, per §8.2, once a real firing has created issue runs | it is a session object, not code |

The release that carries SWEEP-RM's commit plans one **destroy** per
registered tenant (`module.scheduler.google_cloud_scheduler_job.issue_sweep[...]`),
an in-place update of the `swarm-rollup-sweeper` account's description, and a
new swarm-api revision without `SWEEP_ENABLED`. Each job carries
`managed-by=swarm-terraform` in its description, so the plan guard admits the
deletions. A tenant document that still holds an `issue_sweep` map keeps it;
nothing reads it.

The bodies are checked in as [`schedules-migration.json`](schedules-migration.json);
`tests/unit/control_plane/test_schedules_migration_seed.py` builds each through
the create route's own `schedules.build_schedule`, so a body the API would refuse
fails CI first. A schedule is data, not infrastructure, so the release does not
create it: an operator posts it, **as the member who will own it** (§2.7: the
schedule fires as its owner). For `eng` that was the member SWEEP submitted as,
its `issue_sweep.submit_as`. The route that read it was deleted on 2026-10-12;
the member is now the posted schedule's own `owner`:

```bash
./scripts/api.sh GET "/schedules/sch_6ecbe1307511" | jq -r .schedule.owner
```

Signed in as that member, in tenant `eng`:

```bash
SEED=docs/schedules-migration.json
mkdir -p build
posted=0
while read -r i; do
  jq ".schedules[$i].body" "$SEED" > "build/schedule-$i.json"
  ./scripts/api.sh POST "/schedules" "@build/schedule-$i.json" | jq '.schedule | {schedule_id, name, state}'
  posted=$((posted + 1))
done < <(jq '.schedules | keys[]' "$SEED")
echo "posted $posted of $(jq '.schedules | length' "$SEED")"
```

Each body carries a `client_request_id`, so posting it again returns the first
schedule rather than a second one. Check the count: a loop that printed nothing
has not posted anything.

**The sweep is created paused, and its first firing is dry** (§8.1 step 2, §2.8):

```bash
SCH=sch_...   # the issue-sweep schedule's id, from the post above
./scripts/api.sh POST "/schedules/$SCH:run" '{"dry_run": true}' | jq .firing
./scripts/api.sh GET "/schedules/$SCH" | jq '.firings[0].dry_run'
```

Compare the candidate list with SWEEP's last real sweep: the issue runs with
`created_by: issue-sweep`. Then
`./scripts/api.sh POST "/schedules/$SCH:resume"`. It fires at `:07` and `:37`,
SWEEP's old cadence. Until it is resumed, nothing in the platform sweeps, and
session cron `bdb47d30` is the only sweep, which is why §8.2 deletes that cron
only after a real firing.

**What differs from SWEEP, on purpose.** The schedule's merge gate is
`approve`, so each swept pull request waits in Automate › Approvals instead of
merging unattended. Unattended merge is SD3's audited switch (`POST
/v1/schedules/{id}:merge-mode`, a second member), never a create. It starts at
most `max_new_per_firing` (3) new runs a firing, under the same cap of 8 live
runs. It skips `needs-owner`, `wontfix` and `question` as well as SWEEP's `epic`,
`blocked` and `security`. A tenant's old `issue_sweep.exclude_issues` has no
schedule parameter: label the issue and add the label to `labels_exclude`.

**There is no falling back to SWEEP** since 2026-10-12: the job, the route and
the switch are gone. To stop the sweep, pause the schedule (`sc schedules pause
<name>`). Bringing SWEEP back means reverting SWEEP-RM's commit and releasing,
and pausing the schedule first, so that two triggers never run together.
