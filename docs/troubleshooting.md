# Troubleshooting

Ordered roughly by how often each one happens. Start with `make status` — it
already summarises most of what is below in its **Attention** section.

---

## Nothing is running, but the queue is full

```bash
make status
```

Work sits in `READY` while pools are idle. Three causes, in order of likelihood:

**1. Admission is paused.** `status.sh` prints paused pools. Undo with
`./scripts/resume-swarm.sh`, which re-enables exactly what `pause-swarm.sh`
recorded.

**2. The scheduler is not being woken.** Every submission publishes to
`swarm-scheduler-wake`, and a 1-minute Cloud Scheduler tick is the floor. Check:

```bash
gcloud scheduler jobs list --project "$PROJECT_ID" --location "$REGION"
gcloud pubsub subscriptions describe swarm-scheduler-wake-push --project "$PROJECT_ID"
make logs SERVICE=swarm-scheduler
```

A paused tick plus a broken push means nothing drains. If the tick is doing all
the work, the push path is broken — latency will be up to a minute per task.

**3. A pool you did not expect is the binding one.** Look at `blocked_by`:

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     "$API/v1/tasks/$TASK" | jq .blocked_by
```

It names the pool and the reason. `provider:anthropic:tenant:eng` at its limit
looks identical to "the platform is full" from the outside and is not.

---

## Everything is PARKED

Check `park_reason`:

| Reason | Meaning | Fix |
|---|---|---|
| `CREDENTIAL_MISSING` | the tenant has no key for this profile's provider | `create-secrets.sh --tenant <t> --provider <p> --stdin` |
| `PROVIDER_QUOTA_EXHAUSTED` | quota spent | wait for `next_eligible_at`, or raise the cap |
| `PROVIDER_COOLDOWN` | backing off after 429s | wait; check AIMD state |
| `DEPENDENCY_INCOMPLETE` | an upstream workflow step has not finished | check the workflow |
| `BUDGET_EXHAUSTED` | tenant budget spent | raise it or wait for the period |
| `MANUAL_PAUSE` | an operator parked it | `resume-swarm.sh` |

Parked tasks cost nothing, so this is rarely an emergency — but
`CREDENTIAL_MISSING` across a whole tenant usually means registration was never
completed.

---

## A task is stuck in LEASED or DISPATCHED

It holds capacity and nothing is running. The reconciler is designed for exactly
this and runs on a tick:

```bash
make logs SERVICE=swarm-reconciler
gcloud scheduler jobs run swarm-reconciler-tick --project "$PROJECT_ID" --location "$REGION"
```

The reconciler deliberately does **not** treat "LEASED with a fresh lease and no
execution yet" as a fault — dispatch takes time and image pulls take minutes.
A task becomes a finding only after `dispatch_timeout_seconds` (300).

If the reconciler is running and the task stays stuck, the usual cause is that
**termination could not be confirmed** on the backend. That is intentional: the
slot is not released until the execution is confirmed dead, because releasing
first would put a second agent on the same workspace. Check the reconciler's logs
for the termination error, and the backend directly:

```bash
gcloud run jobs executions list --project "$PROJECT_ID" --region "$REGION" \
  --filter="metadata.labels.swarm-task-id=$TASK"
```

---

## Pool `active` looks wrong

`active` is only ever mutated inside the admission and release transactions, so a
wrong value means a lease was leaked (released never ran) rather than a counting
bug.

```bash
./scripts/status.sh --json | jq '.leases[] | select(.released_at == null)'
```

Compare live leases against pool `active`. A lease with `expires_at` in the past
and no `released_at` is the leak; the reconciler reclaims it on the next pass.
**Do not "fix" `active` by writing it directly** — a manual write races with
concurrent admissions and loses the race under exactly the load that made you
look.

---

## Workers exit immediately with `generation_fenced`

Working as designed: another attempt owns that task. The worker checked its
generation before creating a workspace, reading a secret or starting a runner,
and exited without touching the lease — the live lease belongs to the replacement.

It becomes a problem when it is **frequent**. That means the reconciler keeps
deciding live workers are dead, which almost always means:

* `lease_timeout_seconds` is too short for real start-up latency, or
* heartbeats are not reaching Firestore (check worker logs for write errors), or
* image pulls are slower than `dispatch_timeout_seconds` — usually a large image
  or a registry in the wrong region.

---

## An agent was OOM-killed

This should not happen: `requests == limits`, no bursting, no Spot. When it does:

```bash
curl -s "$API/v1/stats" | jq '.resource_usage'   # peak RSS by profile
```

* `oom_near_miss` on recent attempts -> the class is genuinely too small; raise it
  in `swarm_common/profiles.py` (a frozen-module change — raise it rather than
  editing).
* Peaks well under the limit -> something else killed the container. Check for pod
  restarts in `make status`, and for GKE node events.

For reference, the measured baseline for one Claude Code lane was ~2.5 GiB
(claude 1,532 MB + pytest 769 MB + node 207 MB), and `standard` is 8 GiB.

---

## Provider 429s everywhere

```bash
./scripts/status.sh            # provider quota section
curl "$API/v1/providers" | jq
```

Expected behaviour: AIMD halves the adaptive target immediately, work parks
rather than sleeping, and the target climbs back after 20 consecutive successes.

If it is **not** recovering: check `quota/{provider}:{tenant}` for a `reset_at`
in the future or a `state` stuck at `EXHAUSTED`, and confirm the quota-refresh
tick is running. If one tenant's 429s appear to throttle everyone, the binding
pool is the provider-wide one rather than the per-tenant one — compare
`provider:X` and `provider:X:tenant:Y` in `status.sh`.

---

## Cannot authenticate

```
401 { "error": "domain gmail.com is not permitted" }
```

The token's `hd` claim must be an allowed domain. There is no shared platform
token to fall back on, by design.

```
403 { "error": "caller has no tenant" }
```

Nothing registered for this caller. They get a personal fallback tenant
`u-<local-part>` automatically, so this usually means the tenant document was
never created — run `register-tenant.sh --user <email>`.

**Cloud Identity 403 `SERVICE_DISABLED`** naming gcloud's shared client project:
the `x-goog-user-project: <project>` header is missing. It reads like a
permissions problem and is a billing-project problem.

**Cloud Identity 403 Error(4013) "Insufficient permissions to retrieve
memberships"**: something is calling `groups/-/memberships:searchTransitiveGroups`.
That call does not work in this project, and the platform must not use it — see
[multi-tenancy.md](multi-tenancy.md#group-resolution).

---

## `kubectl` is pointed at the wrong cluster

The scripts refuse to act through a context that is not the swarm's:

```
kubectl is pointed at 'gke_saga-agents-staging_us-central1-a_agents-staging',
which is not the swarm cluster.
```

That is another team's live cluster. Fix:

```bash
./scripts/configure-kubectl.sh     # writes build/kubeconfig-<env>.yaml
```

It writes an **isolated** kubeconfig rather than merging into `~/.kube/config`,
and refuses to fetch credentials for any deny-listed cluster.

Also: three `kubectl` binaries exist on the reference machine and the two that
win `$PATH` are 1.22 (EKS) and 1.25 (Docker Desktop). A 1.22 client against a
modern control plane does not fail loudly — it **silently drops fields it does
not understand** from manifests it applies. `scripts/lib/common.sh` resolves
kubectl explicitly and consults `$PATH` last; set `SWARM_KUBECTL` to override.

---

## `make dev` will not start the emulator

```
the Firestore emulator needs a Java 8+ runtime, and this machine has none
```

Verified on the reference workstation: `/usr/bin/java` is Apple's stub that only
offers to install a JRE, so `command -v java` succeeding proves nothing.

```bash
brew install --cask temurin       # then make dev
# or, if your Docker works:
docker compose up firestore
```

On this particular machine the Docker daemon is also broken, so neither local
path runs without installing a JRE first. That is a workstation gap, not a
platform one — the unit tests (`make test`) need neither.

---

## Images will not build

`make build` submits to **Cloud Build**, never the local daemon, because this Mac
is arm64 and every target is amd64 (and the local daemon is broken). If a build
fails:

```bash
gcloud builds list --project "$PROJECT_ID" --region "$REGION" --limit 5
gcloud builds log <BUILD_ID> --project "$PROJECT_ID" --region "$REGION"
```

The generated config is kept in `build/cloudbuild-<target>.yaml` so a failed
build can be reproduced exactly.

`Artifact Registry repository swarm-images does not exist`: run `make infra`
first, or `scripts/build-images.sh --create-repo`.

---

## `make push` refuses to promote

```
trivy found unfixed-excluded HIGH,CRITICAL vulnerabilities; refusing to promote
```

Working as intended — the scan runs **before** a digest is allowed a channel tag.
Rebuild on updated bases (the Dockerfiles pin digests, so bump them
deliberately), or scope the severity for a genuine false positive. Do not use
`--no-scan` to get a release out; that is how an unscanned digest becomes the
thing running in prod.

---

## `make destroy` aborted

That is the guard doing its job. It prints every offender:

```
OFFENDERS -- resources marked for deletion that are not ours:
  theirs.cluster  google_container_cluster  agents-staging  (deny-listed)
  unlabelled.topic google_pubsub_topic      swarm-scheduler-wake (no managed-by label)
```

Two distinct cases:

* **deny-listed** — the plan targets another team's resource. Stop. Something is
  wrong with state or with the target, and no flag overrides this.
* **no `managed-by=swarm-terraform` label** — one of ours that Terraform created
  without the label. Fix the label in Terraform and re-plan; do not weaken the
  guard.

Verify the guard itself still works with `./scripts/destroy.sh --self-test`.

---

## Terraform state problems

```
Error: Backend initialization required
```

```bash
make bootstrap        # init against gs://<bucket>/infra/<env>
```

There is **one root** (`terraform/infra`) with a tfvars file per environment, and
the state prefix is derived from `ENVIRONMENT`. If you suspect two environments
share a prefix, check before applying:

```bash
gsutil ls "gs://swarm-tfstate-$PROJECT_ID/"
```

State bucket versioning is on, so a corrupted state file is recoverable:

```bash
gsutil ls -a "gs://swarm-tfstate-$PROJECT_ID/infra/dev/default.tfstate"
gsutil cp "gs://...#<generation>" ./recovered.tfstate
```

See [disaster-recovery.md](disaster-recovery.md).

---

## Where to look next

| Symptom | Doc |
|---|---|
| Capacity accounting, limits, fairness | [concurrency.md](concurrency.md) |
| 429s, parking, AIMD | [quota-management.md](quota-management.md) |
| Resume behaviour, lost work | [checkpointing.md](checkpointing.md) |
| Cloud Run vs GKE, dispatch failures | [execution-backends.md](execution-backends.md) |
| Tenant isolation, secrets, groups | [multi-tenancy.md](multi-tenancy.md) |
| Spend | [cost-control.md](cost-control.md) |
| Rebuilding after a loss | [disaster-recovery.md](disaster-recovery.md) |
| Tool and provider versions | [versions.md](versions.md) |
