# Runbook: apply the GKE dispatcher RBAC and redispatch `wf_7e2ee6c3075d43228e5a`

**When to use this.** `browser` tasks fail with `jobs.batch is forbidden`, or
you are landing the GKE dispatch fix for the first time.

**How long.** Two minutes of commands, plus however long the workflow takes.

**What it changes.** One tenant namespace on the swarm's own Autopilot cluster
(`swarm-autopilot`). Nothing outside it. If any command here mentions
`agents-staging`, `agents-staging-vpc` or anything else on the deny-list in
`scripts/lib/common.sh`, stop — you are on the wrong cluster and the guards in
`kubernetes/apply.sh` should already have refused.

Background, and why the error message lies: [docs/gke-dispatch-403.md](../gke-dispatch-403.md).

Every command below was checked against the script or `Makefile` target it
names. Where one takes an argument you have to choose, the choice is called
out rather than assumed.

---

## 0. Point kubectl at the swarm cluster

```bash
make kubectl
export KUBECONFIG="$PWD/build/kubeconfig-dev.yaml"     # ENVIRONMENT=dev
```

`make kubectl` runs `scripts/configure-kubectl.sh`, which writes an **isolated**
kubeconfig at `build/kubeconfig-<env>.yaml` rather than merging into
`~/.kube/config`, and refuses to fetch credentials for any cluster on the shared
deny-list. It prints the `export` line itself when it finishes; the one above is
that line for `ENVIRONMENT=dev`.

Optional, and worth it if you are going to type `kubectl` by hand:

```bash
eval "$(make kubectl-guard)"
```

That puts `bin/kubectl` — a wrapper that refuses any context which is not the
swarm cluster, read-only commands included — ahead of the real binary on
`$PATH`. `bin/kubectl` is also why you should not reach for a `kubectl` you
found on `$PATH`: 1.22 and 1.25 shadow the current one on the reference
workstation and an old client **silently drops** manifest fields it does not
understand.

Confirm you are where you think you are:

```bash
kubectl config current-context
kubectl config view -o jsonpath='{.contexts[*].context.cluster}'
```

The cluster must contain `swarm-autopilot`. Anything else: stop.

---

## 1. Look before you apply

```bash
kubernetes/apply.sh --tenant eng
```

Without `--confirm` this renders the manifests, validates them client-side, and
prints a `kubectl diff`. Nothing is written. Read the diff: you should see the
namespace, the ResourceQuota and LimitRange, two ServiceAccounts
(`swarm-worker` and `swarm-agent-worker`), the worker Role/RoleBinding, the
**`swarm-dispatcher` and `swarm-reaper`** Roles and RoleBindings, and two
NetworkPolicies.

`swarm-dispatcher` is the object this whole exercise is about. If it is not in
the diff, you are on an older checkout — it is added by
`kubernetes/rbac/dispatcher-rbac.yaml`, which is listed in `TENANT_FILES` in
`kubernetes/render.py`.

Check the subjects in the diff name real service accounts:

* `swarm-dispatcher` → `swarm-scheduler@<project>.iam.gserviceaccount.com`
* `swarm-reaper` → `swarm-reconciler@<project>.iam.gserviceaccount.com`

A RoleBinding to a subject that does not exist applies cleanly and grants
nothing, so a typo here fails looking exactly like success. The renderer
validates both against a pinned pattern and refuses to emit an unsubstituted
placeholder, but read them anyway — this is the one step where reading is
cheaper than the failure.

---

## 2. Apply

```bash
kubernetes/apply.sh --tenant eng --confirm
```

One command. It creates or updates the namespace and everything in it. It is
idempotent: run it twice and the second run changes nothing.

For a tenant other than `eng`, change `--tenant`. For every tenant at once
there is no loop target — run it once per tenant id, and note that
`scripts/register-tenant.sh` calls this same script for new tenants.

---

## 3. Confirm the RBAC took effect

Existence first:

```bash
kubectl get namespace swarm-tenant-eng
kubectl get rolebinding swarm-dispatcher -n swarm-tenant-eng -o wide
kubectl get serviceaccount -n swarm-tenant-eng
```

Expect the namespace, a `swarm-dispatcher` RoleBinding whose subject is the
scheduler's service account, and both `swarm-worker` and `swarm-agent-worker`.

`swarm-agent-worker` is the one the dispatcher's pod spec actually names. If it
is missing, the Job will be created and no pod will ever appear —
`kubectl describe job` will show `serviceaccount "swarm-agent-worker" not
found`, which reads as a scheduling problem rather than a missing object.

Then authorisation, asked as the scheduler rather than as you:

```bash
kubectl auth can-i create jobs \
  --namespace swarm-tenant-eng \
  --as swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com
```

Expect `yes`. This needs impersonation rights on your own account; if it comes
back `error: ... cannot impersonate`, that is a limit on **your** credentials
and says nothing about the binding. Fall back to reading the RoleBinding's
subjects, and let step 5 be the proof.

Deliberately **not** `kubectl auth can-i --as` against `swarm-reaper`: the
reconciler's binding is verified the same way, by reading it.

---

## 4. Prove one browser task works before spending a workflow

```bash
scripts/smoke-test.sh --profile browser --timeout 900
```

`scripts/smoke-test.sh` now covers **one profile per backend**, derived from the
frozen catalogue, so a plain `make smoke` exercises `GKE_AUTOPILOT` too. Running
it with `--profile browser` additionally makes the final single-task case a
browser task, which is the cheapest possible end-to-end proof of this fix.

If a backend case fails, the suite prints that task's `last_error`. That field
is the whole diagnosis and it is one line: it is the difference between "browser
failed" and "403 on jobs.batch in swarm-tenant-eng".

`--timeout 900` rather than the default 600 because the browser image is large
and a cold Autopilot node can take several minutes to admit the first pod.

---

## 5. Redispatch `wf_7e2ee6c3075d43228e5a`

There is no retry endpoint — a workflow is immutable once submitted, and its
failed steps have already consumed their attempts. Redispatching means
**submitting the same DAG again**, which produces a new `workflow_id`.

Read the original:

```bash
scripts/api.sh GET /workflows/wf_7e2ee6c3075d43228e5a > /tmp/wf-original.json
jq '.workflow.state, (.tasks | group_by(.runner_profile)
    | map({profile: .[0].runner_profile, states: (map(.state) | group_by(.) | map({(.[0]): length}) | add)}))' \
  /tmp/wf-original.json
```

That prints the split by profile — the shape that made the cause obvious the
first time.

Build the resubmission from it. `WorkflowCreate` forbids unknown fields
(`extra="forbid"`), and the read shape carries `task_id` on each step, which the
write shape does not accept — so it is dropped:

```bash
jq '{
  steps: [ .workflow.steps[] | {
      step_id, runner_profile, input, depends_on,
      resource_class, input_from, timeout_seconds
    } ],
  priority: .workflow.priority,
  on_step_failure: .workflow.on_step_failure,
  strategy: .dispatch.strategy,
  carrier: .dispatch.carrier
}' /tmp/wf-original.json > /tmp/wf-redispatch.json

scripts/api.sh POST /workflows @/tmp/wf-redispatch.json
```

The response carries the new `workflow_id` and echoes the dispatch options that
were **accepted**, not the ones you sent.

If the original named a repository, add `repository_url` and `repository_ref`
to `/tmp/wf-redispatch.json`: they are workflow-level fields and are not served
back on the step objects, so `jq` cannot recover them from the read. Check the
original submission if the steps are meant to clone anything.

Watch it:

```bash
scripts/api.sh GET /workflows/<new_workflow_id> \
  | jq '{state: .workflow.state, source: .workflow.state_source,
         by_state: (.tasks | map(.state) | group_by(.) | map({(.[0]): length}) | add)}'
make status
```

`state_source` says `derived` when the state was computed from the step tasks
this request loaded, and `stored` when it is the cached field. Both read routes
derive; `stored` on a GET of one workflow would itself be worth a question.

`make status` is the one screen: cluster, jobs, agents, queues, leases, pools
and quota.

---

## 6. If a browser step fails again

Read the task's own error first — it is the only field that distinguishes the
cases:

```bash
scripts/api.sh GET /tasks/<task_id> | jq '{state, last_error, park_reason, blocked_by, attempt_count}'
scripts/api.sh GET /tasks/<task_id>/events | jq -r '.events[] | "\(.at) \(.type) \(.detail // "")"'
```

Then the scheduler's own log line, which carries the upstream message that
`last_error` deliberately does not:

```bash
make logs SERVICE=swarm-scheduler LINES=200
```

Look for `dispatch failed ... backend=GKE_AUTOPILOT`, and for `dispatch ok
... backend=GKE_AUTOPILOT` — the success line exists precisely so that "has GKE
ever dispatched?" is answerable from logs instead of by reading task documents.

| what the error says | what it means | what to do |
|---|---|---|
| `gke_create_job_forbidden`, message names `swarm-tenant-<id>` | **either** the namespace does not exist **or** the RBAC is missing. Kubernetes authorises before it resolves, so you cannot tell from the message | go back to step 3. `kubectl get ns` first, IAM last |
| `gke_create_job_failed` with a connection or TLS error | the cluster endpoint or CA in the scheduler's environment is wrong | `GKE_ENDPOINT` / `GKE_CA_CERT_B64` come from terraform; re-run `make deploy` after `make infra` |
| `backend_disabled` | `ENABLE_GKE_AUTOPILOT` is false on the scheduler | a deployment setting, not a cluster problem |
| task reaches RUNNING, no pod ever appears | the KSA the pod names does not exist | `kubectl describe job -n swarm-tenant-<id> <job>` and look for `serviceaccount ... not found`; step 3 |
| pod appears and is `Pending` forever | Autopilot has not admitted it — resource class, or a quota on the namespace | `kubectl describe pod`, then `kubectl get resourcequota -n swarm-tenant-<id>` |
| `CreateContainerConfigError` | the pod is asking for a Kubernetes Secret that does not exist | nothing in this repository creates one; the tenant key lives in Secret Manager and the worker fetches it itself. Read `kubernetes/render.py`'s header |

If it is none of these, stop and capture the Job's events before deleting
anything — they are lost when the Job is collected:

```bash
kubectl get events -n swarm-tenant-eng --sort-by=.lastTimestamp | tail -40
kubectl describe job -n swarm-tenant-eng <job-name>
```

---

## What this runbook does not cover

* **Applying to every tenant.** There is no bulk target; run step 2 per tenant.
* **Tenants provisioned before 2026-09-24.** Their tenant document may carry
  `namespace: swarm-<id>`, the old spelling, which the dispatcher *prefers*
  over its own template. There is no per-tenant read route — `/v1/tenants/me`
  answers only for the caller — so check them all at once, which needs admin:

  ```bash
  scripts/api.sh GET /admin/tenants \
    | jq '.tenants[]
          | select(.namespace and (.namespace | startswith("swarm-tenant-") | not))
          | {tenant_id, namespace}'
  ```

  Anything printed is carrying the short form. Re-run
  `scripts/register-tenant.sh` for that tenant and it writes the derived value.
  The old namespace, if it was ever created, is empty and the reconciler
  collects it once `empty_namespace_ttl_seconds` has passed.
* **Rolling back.** Deleting the RBAC returns you to the broken state; there is
  nothing to roll back to. If the apply itself is wrong, fix the manifest and
  re-apply — `kubectl apply` converges.
