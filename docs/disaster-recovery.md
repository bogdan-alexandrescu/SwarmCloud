# Disaster recovery

What survives what, and how to get back.

The platform is designed so that almost everything is **replayable**: images
rebuild from git, infrastructure re-applies from Terraform, tasks re-run from
their inputs. Three things are not replayable, and they are the ones this
document is really about:

1. **Terraform state** — without it, Terraform no longer knows which resources in
   a *shared* project are ours, and `make destroy`'s label assertion has nothing
   to assert against.
2. **Tenant provider keys** — Secret Manager holds the only copy; they are
   deliberately outside Terraform so the plaintext is never in a state file.
3. **In-flight agent work** — bounded to one checkpoint interval by design.

---

## 1. Recovery objectives

| Loss | RPO | RTO | Mechanism |
|---|---|---|---|
| A single worker/execution | ≤ 120 s of agent work | minutes | checkpoint restore on a new attempt |
| A control-plane service | 0 | minutes | redeploy the previous digest |
| Firestore data | last export | ~1 h | `purge-data.sh` pre-delete export, or scheduled export |
| Terraform state file | last write | minutes | bucket object versioning |
| Whole environment | as above | ~1–2 h | `make bootstrap` -> `up` |
| Provider keys | n/a | manual | re-registered by an admin |

---

## 2. A worker died

Nothing to do. This is the designed path:

```
execution disappears
  -> lease goes stale (no heartbeat within lease_timeout_seconds)
  -> reconciler: invalidate generation -> terminate -> confirm -> release
  -> task returns to READY
  -> scheduler admits a new attempt, new generation
  -> worker restores the newest checkpoint ACROSS ALL ATTEMPTS
  -> work resumes, having lost at most one checkpoint interval
```

Verify with the task's event stream: `generation_fenced` (if the old worker was
still alive), then `lease_acquired`, `checkpoint_restored`, `running`.

If the task instead sits in `LEASED`, termination could not be confirmed on the
backend, and the slot is deliberately **not** released — see
[troubleshooting.md](troubleshooting.md#a-task-is-stuck-in-leased-or-dispatched).

---

## 3. A control-plane service is broken

Roll back to the previous digest. `build/deployed-images-<env>.json` records what
each deploy promoted:

```bash
jq '.images[] | {name, digest}' build/deployed-images-dev.json

gcloud run services update swarm-api \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "us-central1-docker.pkg.dev/$PROJECT_ID/swarm-images/swarm-api@sha256:<previous>"
```

Or revert the commit and let `.github/workflows/application.yml` redeploy.

**Never roll `swarm-api` back to a build from before PR #44 (contract request
17), and never revert #44.** From that release on, `swarm-api` records a
cancel that is only requested as a `cancel_requested` event. No `swarm-api`
image built before it can decode one: `EventType(data["type"])` raises
`ValueError`, and `GET /v1/tasks/{id}/events` returns 500 for every page that
holds such an event. That means every task cancelled while it held capacity
since the deploy. The events are permanent, so this lasts as long as the
rollback does, not just one rollout. The console Timeline, `swarm_follow` and
`swarm tail` report that history as unreadable, in the middle of the incident
you are rolling back for. Roll forward instead: revert the change that broke
the service, not #44, and let the release deploy it.

Check a rollback target before deploying it. The manifest's `tag` is the commit
its images were built from:

```bash
tag="$(jq -r .tag path/to/the/previous/deployed-images-dev.json)"
git fetch origin
git grep -q CANCEL_REQUESTED "${tag}" -- apps/common/swarm_common/states.py \
  && echo "reads cancel_requested: safe to roll back to" \
  || echo "from before contract request 17, or unknown here: do NOT roll back to it"
```

While it is broken:

* **the API being down stops submissions, not the fleet** — running tasks keep
  running, and the scheduler keeps draining the queue;
* **the scheduler being down stops admission** — running tasks keep running, the
  queue grows, and it costs nothing;
* **the reconciler being down leaks capacity slowly** — stale leases accumulate,
  so the platform admits less over time. Not urgent for an hour; not something to
  leave for a week.

`status.sh` reads Firestore directly, so it works while the API is down.

---

## 4. Firestore data loss

### Prevention

`purge-data.sh` exports the database to GCS before deleting anything, unless
`--no-backup`. For scheduled protection, run managed exports:

```bash
gcloud firestore export "gs://${ARTIFACT_BUCKET}/backups/$(date -u +%Y%m%d)" \
  --database=swarm --project "$PROJECT_ID"
```

### Restore

```bash
gcloud firestore import "gs://${ARTIFACT_BUCKET}/backups/20260915" \
  --database=swarm --project "$PROJECT_ID"
```

**Import is additive and does not remove documents written since the export.**
That matters for the pool documents: importing an old `pools/*` over a live
platform restores stale `active` counts and desynchronises capacity accounting
from the live leases. If you must restore while work is in flight:

1. `./scripts/pause-swarm.sh --all --drain` and wait for `RUNNING` to reach zero;
2. import;
3. reconcile pools against live leases — with nothing running, every `active`
   should be 0;
4. `./scripts/resume-swarm.sh`.

### What is actually lost

| Collection | Impact of loss |
|---|---|
| `tasks` | task history and queue; resubmittable from inputs if the caller kept them |
| `leases` | capacity accounting; harmless once nothing is running |
| `pools` | limits; re-created by `terraform apply` |
| `tenants` | tenant records; re-created by `register-tenant.sh` |
| `quota` | provider state; rebuilds itself from observation within minutes |
| `attempts`, `events` | audit trail; not recoverable, not operationally required |

Checkpoints and artifacts live in GCS, not Firestore, so agent work survives a
total Firestore loss — but the tasks that would resume from them do not, which is
why an export before any destructive operation is the habit.

---

## 5. Terraform state loss

The worst case, because in a **shared** project state is what distinguishes our
resources from twelve other service accounts' resources.

### Prevention

The state bucket (created by `terraform/bootstrap`) has:

* object versioning on — previous state files are still there;
* `prevent_destroy` — `terraform destroy` on the bootstrap root cannot delete it;
* a 30-day soft-delete policy;
* UBLA and enforced public access prevention;
* access logging to a separate bucket;
* archived versions kept: 20 generations / 365 days.

`bootstrap.sh` warns loudly if versioning is ever off.

### Recovery from a bad write

```bash
gsutil ls -a "gs://swarm-tfstate-$PROJECT_ID/infra/dev/default.tfstate"
gsutil cp "gs://swarm-tfstate-$PROJECT_ID/infra/dev/default.tfstate#1726...." ./recovered.tfstate

terraform -chdir=terraform/infra state push ./recovered.tfstate
terraform -chdir=terraform/infra plan -var-file=../environments/dev/dev.tfvars
```

An empty plan means you recovered the right generation.

### Recovery from total loss

Do **not** re-apply into a shared project with empty state — Terraform will try to
create resources that already exist, and some of those creates fail in ways that
leave duplicates. Import instead:

```bash
terraform -chdir=terraform/infra import \
  -var-file=../environments/dev/dev.tfvars \
  module.storage.google_storage_bucket.artifacts \
  "swarm-artifacts-$PROJECT_ID"
```

The bucket is `swarm-artifacts-<project>`, not `<project>-swarm-artifacts`.
`terraform/modules/storage` composes it as
`"${var.name_prefix}-artifacts-${var.bucket_suffix}"` with `bucket_suffix =
var.project_id`, and an import of a name that has never existed fails with
"Cannot import non-existent remote object" — at the one moment when the
obvious reading of that message is that the bucket was lost too.

Every swarm resource carries `managed-by=swarm-terraform`, so the inventory to
import is discoverable:

```bash
gcloud asset search-all-resources \
  --scope="projects/$PROJECT_ID" \
  --query="labels.managed-by=swarm-terraform" \
  --format="table(assetType, name)"
```

Until state is restored, **`make destroy` must not be run**. Its guard asserts on
a destroy plan, and a plan built from empty state asserts on nothing.

---

## 6. Secrets

Provider keys are deliberately not in Terraform: a Terraform-managed secret
version puts the plaintext in a state file that several people can read.
Consequently, **Secret Manager holds the only copy**.

Secret Manager keeps previous versions unless they are destroyed, so a bad
rotation is recoverable:

```bash
gcloud secrets versions list swarm-tenant-eng-anthropic --project "$PROJECT_ID"
gcloud secrets versions enable 3 --secret=swarm-tenant-eng-anthropic --project "$PROJECT_ID"
```

If a secret is destroyed outright, the key is gone and the tenant re-registers
it. Meanwhile their tasks park as `CREDENTIAL_MISSING` and cost nothing — they
resume automatically when the key returns.

---

## 7. Rebuilding an environment

```bash
make prerequisites
make bootstrap             # state bucket + init
make tf-plan               # READ IT. Shared project.
make tf-apply
make build push deploy
make smoke

# Tenants and keys are not in Terraform:
make register-tenant GROUP=eng@saga.xyz
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
make add-provider TENANT=eng PROVIDER=anthropic   # one per provider the tenant had
```

Each provider goes back with `make add-provider`, one at a time, never with a
second `make register-tenant ... PROVIDERS=`: a re-run of the registration
**replaces** the tenant's credentials list with the one it is given and resets
its limits and display name to the defaults, which on a rebuild quietly drops
every provider the command did not name. See
[operations.md §1](operations.md#1-first-deployment).

Roughly an hour, most of it Cloud Build and GKE provisioning. Then, if you have a
Firestore export, import it (paused, per §4).

---

## 8. Shared-project incidents

If something in this repo appears to have affected another team's resources:

1. `./scripts/pause-swarm.sh --all` — stop admitting immediately.
2. Establish what was touched:
   ```bash
   gcloud logging read \
     'protoPayload.authenticationInfo.principalEmail=~"swarm-"' \
     --project "$PROJECT_ID" --freshness=24h --limit 200 \
     | ./scripts/lib/redact-stream.sh
   ```
3. Confirm the deny-listed resources still exist (`destroy.sh` does this check
   after every apply, and `configure-kubectl.sh` refuses them up front).
4. Tell the owning team before they discover it.

The guards make this unlikely — `destroy.sh` fails closed on any resource lacking
`managed-by=swarm-terraform`, `purge-data.sh` refuses the `(default)` database and
deny-listed buckets, and `configure-kubectl.sh` will not fetch credentials for
another team's cluster — but "unlikely" is not "impossible", and the first move is
always to stop admitting.

---

## 9. Drills worth running

| Drill | Command | Expected |
|---|---|---|
| Guard still catches offenders | `./scripts/destroy.sh --self-test` | all assertions pass |
| Destroy plan is clean | `./scripts/destroy.sh --dry-run` | only swarm-labelled resources |
| Checkpoint resume works | `make failure-test` | attempts resume, capacity returns |
| Capacity returns on every failure | `make failure-test` | no leaked leases |
| State recovery | list generations, `state push` into a scratch environment | empty plan |

Run the first two before any teardown, and the rest after any change to
admission, dispatch or the reconciler.
