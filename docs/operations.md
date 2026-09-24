# Operations

The day-to-day runbook. Every command here is a script in `scripts/`; the
Makefile is a thin wrapper over them, and the scripts run identically from a
laptop and from CI.

---

## 1. First deployment

```bash
make prerequisites      # tools, credentials, APIs, shared-project guards
make bootstrap          # state bucket, .env, terraform init
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

**Read the plan.** `saga-agents-staging` holds another team's live GKE cluster,
their VPC and 12 of their service accounts. A plan that proposes to change
anything you do not recognise is a stop-and-ask, not a scroll-past.

Then register a tenant and give it a key:

```bash
make register-tenant GROUP=eng@saga.xyz PROVIDERS=anthropic
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
make register-tenant GROUP=eng@saga.xyz PROVIDERS=anthropic   # re-run to bind the secret
```

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
| Tasks | `PARKED` climbing -> quota or credentials; `READY` climbing with idle pools -> scheduler not draining |
| Leases | expired leases still holding capacity -> the reconciler is behind |
| Concurrency pools | `active/effective_limit`; a pool at its limit is the one to raise |
| Provider quota | `EXHAUSTED`/`COOLDOWN` per (provider, tenant) |
| Cloud Run | service readiness, job resources, recent executions |
| GKE | browser pods, and **any restart count above zero** |
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

Lowering a limit never kills anything: running tasks keep their leases and the
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
make register-tenant GROUP=eng@saga.xyz PROVIDERS=anthropic,openai
make register-tenant USER_EMAIL=alice@saga.xyz

./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
./scripts/create-secrets.sh --list --tenant eng

# Rotation: add the new version, let workers pick it up, then disable the old
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin --disable-previous
```

The key is never an argument (argv is readable through `ps`) and is never echoed.

A tenant without a key for a profile's provider is not an error: tasks park as
`CREDENTIAL_MISSING`, cost nothing, and start by themselves when the key
arrives.

---

## 6. Investigating one task

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
`build and promote`, `terraform apply`, `deploy and smoke` — each behind a
GitHub environment with its approval. The sequence below is the same thing
driven by hand, for an operator who already holds the credentials and is doing
a break-glass deploy or a dev-environment push. It is not what someone
authoring a change runs; see [where the gates run](ci.md).

```bash
make lint test          # shellcheck, terraform fmt/validate, tflint, unit tests
make build push deploy
make smoke
```

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
| `job-executions-failing` | agent executions failing | inspect one task's events and worker logs |
| `generation-fencing` | workers exiting on stale generations | the reconciler is racing live workers; check lease timeouts |
| `wake-messages-dead-lettered` | the scheduler is not acking pushes | scheduler errors, or Firestore unreachable |
| `scheduler-not-draining` | wake backlog sustained | raise scheduler instances or leases/run |
| `tasks-dead-lettered` | tasks exhausting every attempt | a broken runner or a bad repository; also a cost problem |

`generation-fencing` firing repeatedly deserves attention even though the system
is behaving correctly — it means the reconciler keeps deciding live workers are
dead, which usually means heartbeat or lease timeouts are too tight for real
start-up latency.

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
