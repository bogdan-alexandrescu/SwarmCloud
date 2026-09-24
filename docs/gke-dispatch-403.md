# The 403 that means 404: GKE dispatch, RBAC and the tenant namespace

This page exists because a single error message cost this platform two days and
three investigations, all of them aimed at the wrong subsystem. It is the
message you will see first if GKE dispatch ever breaks again, and the first
thing it tells you is not true.

Companion pages: [the redispatch runbook](runbooks/gke-dispatch-redispatch.md)
for the commands, `kubernetes/rbac/dispatcher-rbac.yaml` for the RBAC objects
themselves, and [execution backends](execution-backends.md) for why `browser`
is on GKE at all.

---

## 1. Kubernetes authorises before it resolves

A `create` against a namespace that does not exist is **not** a 404.

The API server runs the authorizer against the *request* — verb, group,
resource, namespace, user — before anything looks the namespace up. The
authorizer has nothing to say about namespaces that are missing; it only
answers "may this user create `jobs` in `batch` in namespace X". When the
answer is no, the request stops there, and what comes back is:

```
jobs.batch is forbidden: User "117405034245659033603" cannot create resource
"jobs" in API group "batch" in the namespace "swarm-tenant-eng": requires one
of ["container.jobs.create"] permission(s) in Cloud IAM or a Kubernetes RBAC
role with verb "create" for resource "jobs".
```

**There is no 404 coming.** The namespace's existence is never tested, so its
absence cannot be reported. Every dispatch into a namespace nobody created is
reported as a permission problem, forever, in a message that even names the
IAM permission you are supposedly missing.

That is the trap. The message is a complete, confident, specific diagnosis of
something that was not wrong.

### Why it reads as an IAM problem specifically

Because two things are true at once:

* the message names `container.jobs.create`, an IAM permission, and
* the scheduler's IAM binding for that permission carries a **condition**:
  `resource.name.startsWith(".../clusters/swarm-autopilot")`, added so a
  project-level grant in a shared project could not reach `agents-staging`,
  another team's live cluster.

So the permission looks conditional, the condition looks like the sort of thing
that gets evaluated wrongly, and the investigation goes to IAM. It is not
wasted time in the sense of being careless — it is the only lead the message
offers.

The condition is genuinely unable to authorise this request, for a reason worth
knowing: the resource in a namespaced Kubernetes request is the namespaced
object, not the cluster path the expression tests, so the condition never
matches and GKE reports the permission as simply absent. That is why RBAC —
which scopes by *where the object lives* rather than by a string comparison —
is the mechanism that fits. See the header of
`kubernetes/rbac/dispatcher-rbac.yaml`, which carries the full argument.

But on 2026-09-23 the IAM condition was not the cause either.

---

## 2. What actually happened

A thirty-step workflow (`wf_7e2ee6c3075d43228e5a`) ran with three runner
profiles and split exactly along one line:

| profile | backend | result |
|---|---|---|
| `claude-code` | `CLOUD_RUN_JOB` | SUCCEEDED 14 |
| `mock` | `CLOUD_RUN_JOB` | SUCCEEDED 8 |
| `browser` | `GKE_AUTOPILOT` | FAILED 6, CANCELLED 2 |

`browser` is the only profile whose resolved backend is `GKE_AUTOPILOT`. Every
one of its attempts failed with the message above. All **seven** browser tasks
this deployment had ever accepted, over two days, had failed the same way. The
capability shipped broken; it was never a regression.

There were two causes, stacked, and the second was hidden behind the first.

**Cause 1 — the namespace did not exist.** `kubernetes/render.py` spelled the
tenant namespace `swarm-` and `apps/scheduler/scheduler/dispatch.py` spelled it
`swarm-tenant-`. The provisioner created `swarm-eng`; the dispatcher created
Jobs in `swarm-tenant-eng`. `scripts/register-tenant.sh` carried a third copy
of the short spelling and wrote it into the tenant document's `namespace`
field — which `GkeJobDispatcher.namespace_for` *prefers* over its own template,
so the Firestore record was overriding the one spelling that was correct.

**Cause 2 — the RBAC did not exist.** Even with the namespace right, the
scheduler's Google identity held no Kubernetes RBAC in it. That is what
`kubernetes/rbac/dispatcher-rbac.yaml` adds.

Both produce the identical message. Fixing either one alone leaves it
unchanged, which is the other half of why this took two days.

### The third failure, found while fixing the first two

With the namespace right and the RBAC applied, the Job is created — and the pod
is never scheduled. Nothing in `terraform/` creates a Kubernetes object (there
is no kubernetes provider in this repository), so
`kubernetes/service-accounts/worker-serviceaccount.yaml` is the only thing that
creates a tenant's KSA, and it created `swarm-worker` and `swarm-<tenant>` while
the dispatcher's pod spec asks for `swarm-agent-worker`. A pod naming a
ServiceAccount that does not exist is admitted and then never scheduled: the
Job controller reports `serviceaccount "swarm-agent-worker" not found` on the
Job's events and no pod appears at all.

That reads as a scheduling or capacity problem — the same disguise the
namespace bug wore. It is fixed (`render.py`'s `DEFAULT_KSA_NAME`), and it is
recorded here because it is the failure that would have come next.

---

## 3. What now prevents each of them

| failure | what stops it coming back |
|---|---|
| two spellings of the namespace | `scripts/lib/check-contract-parity.sh` section 6 — the scheduler's `GkeTarget.namespace_template` is the authority, every declared prefix is compared against it, and the repository is swept for namespace literals that do not start with it |
| a test fixture that agrees with the bug | the same sweep covers `tests/`; a fixture wanting the wrong spelling on purpose must carry `# namespace-prefix-exempt:` and say why |
| the RBAC missing again | `rbac/dispatcher-rbac.yaml` is in `render.py`'s `TENANT_FILES`, so `kubernetes/apply.sh --tenant X` cannot create a namespace without it, and `tests/unit/worker/test_kubernetes_manifests.py` asserts the bindings name the real service accounts |
| a placeholder rendered into a live RoleBinding | `render.substitute` refuses any leftover `__TOKEN__`, and `check_values` refuses any placeholder with no validation pattern — a RoleBinding to a literal `__SCHEDULER_GSA__` would apply cleanly and grant nothing |
| the KSA mismatch | `render.DEFAULT_KSA_NAME`, asserted against `GkeJobDispatcher.ksa_for` in the manifest tests |
| GKE failing unnoticed for two days | `scripts/smoke-test.sh` covers one profile **per backend**, derived from the frozen catalogue via `resolve_backend`, and fails loudly when a backend has no available profile instead of dropping it from the matrix |
| nobody being able to answer "has GKE ever dispatched?" | the `dispatch ok` line in `apps/scheduler/scheduler/loop.py` and the alert on `swarm_scheduler_dispatched_total{backend}` |

---

## 4. Reading the error next time

The dispatcher now says this itself. A 403 on `jobs.batch` is raised with code
`gke_create_job_forbidden` and a message that names the namespace it tried and
states plainly that the namespace may simply not exist. It reaches the log
through the `dispatch failed` line in `apps/scheduler/scheduler/loop.py`.

When you see it, check existence **before** you touch IAM:

```bash
kubectl get namespace swarm-tenant-eng
kubectl get rolebinding swarm-dispatcher -n swarm-tenant-eng
kubectl get serviceaccount -n swarm-tenant-eng
```

Three outcomes, three different problems:

* **no namespace** — the tenant was never provisioned on the cluster, or was
  provisioned under another spelling. `kubernetes/apply.sh --tenant <id>
  --confirm`. Note that `scripts/register-tenant.sh` will report a tenant as
  registered while skipping the whole GKE half if kubectl is not pointed at the
  swarm cluster, so "the tenant exists" is not evidence that its namespace does.
* **namespace, no `swarm-dispatcher` RoleBinding** — the RBAC in this
  repository has not been applied to that namespace. Same command.
* **both present and still 403** — now it is worth looking at IAM, and the
  condition on `swarmGkeDispatcher` is the first thing to read.

The runbook has the full sequence: [gke-dispatch-redispatch](runbooks/gke-dispatch-redispatch.md).

---

## 5. The rule this is an instance of

An error message is evidence about what the system *checked*, not about what is
*wrong*. Kubernetes checked authorisation and told you about authorisation; it
never checked existence, so it could not tell you about existence. Any layer
that validates in a fixed order will do this, and the earlier check will always
be the one that speaks.

The practical form: when a message names a specific cause, confirm the things
it could not have looked at before you act on the thing it did.
