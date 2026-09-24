# Incident, 2026-09-24: every `browser` task this platform ever accepted

**Impact.** Every task whose runner profile resolved to `GKE_AUTOPILOT` failed,
from the day the backend shipped until 2026-09-24. `browser` is the only such
profile, so the platform's entire browser capability was dead. It was never a
regression: there is no commit that broke it and no earlier state to roll back
to. The capability shipped broken.

**Not affected.** The `CLOUD_RUN_JOB` path, which is every other profile. On
2026-09-23 a thirty-step workflow (`wf_7e2ee6c3075d43228e5a`) split exactly along
the backend line:

| profile | backend | result |
|---|---|---|
| `claude-code` | `CLOUD_RUN_JOB` | SUCCEEDED 14 |
| `mock` | `CLOUD_RUN_JOB` | SUCCEEDED 8 |
| `browser` | `GKE_AUTOPILOT` | FAILED 6, CANCELLED 2 |

**Seven causes, stacked.** Each one was real, each one was fixed, and each fix
left the symptom completely unchanged — because all seven produce the same
error. That is the substance of this record and the reason it exists: the story
is not carelessness, it is a failure mode that makes progress invisible.

**Correction, later on 2026-09-24.** Cause 7 was not a cause. It was the first
visible symptom of an eighth, found while investigating the stuck workflow
`wf_ebb3ab2d65664707a559`: the scheduler's GKE Job overrode the container
`command`, which replaced the worker lifecycle with the bare runner. This record
originally explained Cloud Run's immunity as "identical image, identical command"
on a writable root. That sentence was false, and it is what hid cause 8. See
cause 8 in §2; the cause 7 text below is corrected in place.

**Duration.** Two days of active investigation (2026-09-23 to 2026-09-24), seven
failed browser tasks, three investigations that went to IAM. Causes 1, 3, 5 and 7
below had been in the repository since GKE dispatch was written; cause 6 arrived
on 2026-09-23 **inside the file written to fix cause 3**, which is the sharpest
single fact in this record.

Companion pages: [the 403-that-means-404 mechanism](../gke-dispatch-403.md) is
the page to read when this error appears again, and
[the redispatch runbook](../runbooks/gke-dispatch-redispatch.md) has the commands
for applying the RBAC and resubmitting the workflow. Neither is duplicated here.

---

## 1. Why every fix looked like it had failed

Kubernetes **authorises before it resolves**. The API server runs the authorizer
against the *request* — verb, group, resource, namespace, user — before anything
looks the namespace up. The authorizer has nothing to say about a namespace that
does not exist; it answers only "may this user create `jobs` in `batch` in
namespace X". When the answer is no, the request stops there, and what comes
back is this, verbatim, as measured on 2026-09-24:

```
jobs.batch is forbidden: User "117405034245659033603" cannot create resource
"jobs" in API group "batch" in the namespace "swarm-tenant-eng": requires one
of ["container.jobs.create"] permission(s) in Cloud IAM or a Kubernetes RBAC
role with verb "create" for resource "jobs".
```

**There is no 404 coming.** The namespace's existence is never tested, so its
absence cannot be reported.

That single message is what the following four conditions all look like from the
outside — they are indistinguishable:

* the namespace does not exist (it was created under another spelling);
* the namespace exists but holds no `Role` granting `create jobs`;
* the `Role` exists but no `RoleBinding` names our subject;
* the `RoleBinding` exists and names our identity **by the wrong one of its two
  names**.

So each of causes 1, 3 and 6 below was found, fixed, verified applied — and the
next dispatch produced a byte-identical error. Three separate investigations
went to IAM, because the message names an IAM permission and the scheduler's
grant for that permission carries an IAM *condition*, which is exactly the shape
of thing that gets evaluated wrongly. It was the only lead the message offered.

**The lesson.** An error message is evidence about what the system *checked*, not
about what is *wrong*. Any layer that validates in a fixed order behaves this
way, and the earliest check is always the one that speaks. When a message names a
specific cause, confirm the things it could not have looked at before acting on
the thing it did.

---

## 2. The timeline

Each item states what was wrong, where the evidence is, and why fixing it did not
end the outage. Line numbers are as of commit `ffa82e3`.

### 1 — Two spellings of the tenant namespace

`kubernetes/render.py` built the namespace as `swarm-<tenant>`;
`apps/scheduler/scheduler/dispatch.py` dispatched into
`swarm-tenant-<tenant>`. The provisioner created one namespace and the
dispatcher wrote into the other, which did not exist. The cluster held no tenant
namespace at all under the dispatcher's spelling.

*Evidence.* The diff of commit `2846b19` on `kubernetes/render.py`:
`-NAMESPACE_PREFIX = "swarm-"` / `+NAMESPACE_PREFIX = "swarm-tenant-"` (now at
`render.py:107`). The dispatcher's side is `GkeTarget.namespace_template` at
`dispatch.py:740`, unchanged throughout: `"swarm-tenant-{tenant}"`.
`swarm-tenant-` is the spelling the rest of the platform already agreed on,
including the FROZEN `swarm_common.models.Tenant.secret_name`, so `render.py`
was the outlier and is the side that moved.

*Why it was not sufficient.* Fixed, and dispatch still failed — cause 2 was
already preventing the corrected manifests from ever reaching the cluster, and
causes 3 and 6 were behind that.

*And it was worse than two copies.* Commit `d6474f0` found three more: the
reconciler's `config.py` and `backends.py` both defaulted to `swarm-`, which is
not harmless merely because `swarm-tenant-eng` starts with `swarm-` — the
backend *slices the prefix off* to recover a tenant id, so an orphan in
`swarm-tenant-eng` was filed against a tenant called `tenant-eng`, which exists
nowhere. `scripts/register-tenant.sh` built the short spelling and **wrote it
into the tenant document's `namespace` field**, and `namespace_for`
(`dispatch.py:848`) *prefers* that field over its own template — so the record
that script wrote was overriding the one spelling that was correct. A test
fixture in `tests/unit/worker/conftest.py` also used the short form, so the suite
agreed with the bug.

### 2 — The context guard refused the kubeconfig its own setup script produces

`kubernetes/apply.sh` compared the kubectl context **label** against the cluster
name. `scripts/configure-kubectl.sh` deliberately renames the generated context
`gke_<project>_<region>_swarm-autopilot` to the friendlier `swarm-<env>`
(`configure-kubectl.sh:132-137`), so the label is `swarm-dev`, the comparison
failed every time, and the documented path — "run `scripts/configure-kubectl.sh`"
— produced exactly the state the error told you to fix by running it.

This is why the fix to cause 1 could not take: tenant namespaces are created by
`apply.sh`, and with it refusing, `scripts/register-tenant.sh` took its "not
connected to the swarm cluster" branch, logged an INFO, skipped the entire GKE
half and **still reported the tenant registered**. "The tenant exists" was never
evidence that its namespace did.

*Evidence.* `kubernetes/apply.sh:121-153`, which now resolves the cluster the
context *points at* (`kubectl config view -o jsonpath=...context.cluster`) and
refuses an unresolvable one, so the guard still refuses another team's cluster
while accepting any honest name for ours. Commit `2846b19`.

*Why it was not sufficient.* Fixed, and dispatch still failed: with the namespace
now genuinely created, cause 3 took over.

### 3 — There was no dispatcher RBAC at all

Nothing in the repository granted the scheduler `create jobs` in a tenant
namespace. The only Kubernetes `Role` that existed was the worker's, which is
deliberately empty. `kubernetes/rbac/dispatcher-rbac.yaml` was written in
`2846b19`: `swarm-dispatcher` (create/get/list/watch, never delete) and
`swarm-reaper` (delete, never create), mirroring the
`swarmGkeDispatcher`/`swarmGkeReaper` IAM split so that a mistake in one is not
an escalation into the other.

*Evidence.* `kubernetes/rbac/dispatcher-rbac.yaml`, and its entry in
`TENANT_FILES` at `kubernetes/render.py:281` — which is what makes it impossible
for `apply.sh --tenant X` to create a namespace without it.

*Why it was not sufficient.* Applied, confirmed present with `kubectl get
rolebinding`, and dispatch still failed — cause 6 was inside this very file.

### 4 — The IAM condition cannot authorise a namespaced Kubernetes request

`swarmGkeDispatcher` holds `container.jobs.create` and is bound to the scheduler,
but with an IAM condition —
`resource.name.startsWith("projects/<p>/locations/<l>/clusters/swarm-autopilot")`
— added so that a project-level grant in a shared project could not reach
`agents-staging`, another team's live cluster.

The intent is right and the condition stays. What it cannot do is authorise a
**namespaced** Kubernetes request: the resource in that check is the namespaced
object, not the cluster path the expression tests, so the condition does not
match and GKE reports the permission as simply absent. This is why RBAC — which
scopes by *where the object lives* rather than by a string comparison — is the
mechanism that fits, and it is a stronger guarantee than the condition was
attempting: those objects exist only inside `swarm-autopilot`.

*Evidence.* `terraform/modules/iam/bindings.tf:116-125`, whose own comment
explains `startsWith` rather than `==` for the same reason. The full argument is
in the header of `kubernetes/rbac/dispatcher-rbac.yaml` (lines 23-38).

*Why it was not sufficient.* This one was never a cause. It was documented rather
than changed, and it is in this timeline because it is where three investigations
went, and because deleting the condition — the obvious "fix" — would have
widened a grant in a shared project for no benefit.

### 5 — Smoke covered only `CLOUD_RUN_JOB`

`make smoke` submitted one `mock` task and called the platform proven. `mock`
resolves to Cloud Run. `browser` is the only profile on GKE, so the gate could
not have failed while the entire GKE backend was dead — and did not, for as long
as it was.

*Evidence.* `scripts/smoke-test.sh:235-345`: the matrix is now derived from the
frozen catalogue through `resolve_backend` (not `profile.backend`, which would
file an AUTO profile under a "backend" with no API behind it), prints a row for
every concrete backend, FAILS on a backend no available profile reaches, reads
the matrix on fd 3 so nothing in the loop body can consume it, and reports the
count it actually visited. Pinned by
`tests/unit/scripts/test_smoke_covers_every_backend.py:70`, which asserts
`GKE_AUTOPILOT` is covered by `browser`.

*Why it was not sufficient.* It is not a cause; it is the reason causes 1-5 were
not caught by CI on the day they were written. It is listed here because it is the
single change that would have shortened this incident the most.

### 6 — THE CAUSE: the RoleBinding named the right identity by the wrong one of its two names

This cause did not exist before 2026-09-23. It was introduced by the fix for
cause 3, in the file written to grant the access that was missing — which is why
the sequence did not converge: fixing a defect created the next one, and the
error message could not tell them apart.

`dispatcher-rbac.yaml` said, in a comment: "A Google service account is a `User`
to Kubernetes RBAC, named by its email." The very next paragraph of the same file,
about a different risk, described the consequence without noticing it applied to
itself: such a binding "would bind a subject that does not exist and fail
open-looking — the RoleBinding applies cleanly and nothing can use it."

That is what happened. The scheduler reaches the Kubernetes API with a Google
OAuth **access token** (`install_google_bearer_token` in `dispatch.py`), and on
that path GKE resolves the caller to the service account's numeric `uniqueId`,
**not** its email. The namespace was right, the Role was right, the verbs were
right, and the subject authorised nobody.

*Evidence.* Measured 2026-09-24 with the namespace present and both RoleBindings
confirmed applied:

```
jobs.batch is forbidden: User "117405034245659033603" cannot create
resource "jobs" in API group "batch" in the namespace "swarm-tenant-eng"

$ gcloud iam service-accounts describe \
    swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com \
    --format='value(uniqueId)'
117405034245659033603
```

The digits in the error are the account named in the manifest. Nothing connected
them, because nothing printed both spellings in the same place.

*The fix.* `dispatcher-rbac.yaml:89-118` and `:154-164` now carry a second
`kind: User` subject per binding, `__SCHEDULER_UID__` / `__RECONCILER_UID__`.
Both spellings are kept, because the email **is** the subject on other paths
(kubectl with an impersonated GSA, and Workload Identity), and a binding that
authorises one caller and not another is the exact defect this file was written
to fix. `kubernetes/apply.sh:182-198` resolves both uniqueIds with `gcloud` and
**dies** if either lookup fails: `render.py` falls back to the email when a
uniqueId is absent, so a silent fallback there would recreate this bug exactly.
The renderer's default is deliberately the email and never a fabricated number,
because a plausible-looking numeric default reads as a grant and authorises
nobody — worse than a duplicate.

*Result.* A Job was created in a tenant namespace for the first time in this
platform's history. Which produced cause 7.

### 7 — `/artifacts` on a read-only root filesystem

The Job was created, the node scaled up, the image was pulled, the container
STARTED, and then:

```
OSError: [Errno 30] Read-only file system: '/artifacts'
  File ".../agent_worker/runners/base.py", line 124, in from_env
    ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
```

`runners/base.py:116` defaults `artifacts_dir` to `work.parent / "artifacts"`
when `SWARM_ARTIFACTS_DIR` is unset. The work dir is the container's cwd,
`/workspace`, so the default resolves to `/artifacts`, at the root. GKE sets
`readOnlyRootFilesystem: true` (`dispatch.py:146`), so the runner died on its
first line.

*Why Cloud Run never showed it. CORRECTED.* This paragraph originally said:
"Identical image, identical `python -m agent_worker.runners.<x>` command out of
the frozen catalogue, and a container that does **not** harden the root
filesystem". **The commands were not identical.** Terraform's Cloud Run Jobs set
`command = null` (`terraform/modules/cloud_run_jobs/main.tf`), and
`gcloud run jobs describe swarm-job-eng-mock` confirms it, so Cloud Run ran the
image ENTRYPOINT, `tini -- python -m agent_worker`. That is the worker
lifecycle, and it gives its runner child `SWARM_ARTIFACTS_DIR` itself, set per
attempt to `<WORKSPACE_ROOT>/<attempt>/artifacts` (`workspace.py:136`), so the
runner never reaches the default. The GKE Job carried
`command: ["python", "-m", "agent_worker.runners.browser"]`, which replaced the
lifecycle, so nothing set the variable. The read-only root is what made the
missing lifecycle *loud*; it was never what made the two backends differ. See
cause 8.

*What `ffa82e3` actually changed.* It set `SWARM_ARTIFACTS_DIR` in `worker_env`
(`dispatch.py:288`), the builder both backends share, and in the three worker
templates. On a container that runs the lifecycle, that value is **inert**: the
lifecycle builds its child's environment from an allowlist and never passes it
through. On the GKE Job as it then was, it would have got the bare runner past
`mkdir` and into running with no fencing, no checkpoint, no heartbeat and no
lease release. It is kept only as a writable fallback for a runner started by
hand inside a worker container.

*Why it was only reachable now.* Every earlier attempt failed at `jobs.batch is
forbidden` before a container existed.

### 8 — The Job replaced the worker lifecycle with the bare runner

Found later on 2026-09-24, while investigating `wf_ebb3ab2d65664707a559`. Four
of that workflow's browser steps and one standalone task sat in `DISPATCHED`
with their leases unreleased, holding all 10 `resource:browser` slots, and no
cancel could finish them.

*What was wrong.* `GkeJobDispatcher._manifest` put
`"command": list(profile.command)` on the worker container. `RunnerProfile.command`
is the **runner's** argv, `python -m agent_worker.runners.<x>`. A Kubernetes
`command` replaces the image ENTRYPOINT, and the ENTRYPOINT of both worker images
is `tini -- python -m agent_worker`, the worker **lifecycle**. The lifecycle is
what checks the fencing generation (invariant 5), honours a requested cancel,
moves the task through STARTING and RUNNING, heartbeats, checkpoints (invariant
8), starts the runner as a supervised child, writes the terminal state and
releases the lease. None of it ran on GKE, ever. A pod that dies without the
lifecycle leaves its lease for the reconciler, and the reconciler could not read
GKE (a separate defect in the same incident). So the leases stayed.

*Evidence (measured).*

* The `jobs.create` audit request bodies for check-2..5 at 03:55:07Z show
  `command=['python','-m','agent_worker.runners.browser']` and no `args`.
* The pods logged seven lines each, all one runner traceback, and not one
  lifecycle JSON line. Under the lifecycle a child's stderr goes to a workspace
  file, not to container stdout.
* The `/artifacts` path itself (cause 7) can only occur without the lifecycle.

*Why nothing caught it.* `kubernetes/worker-templates/worker-job.yaml` had said
in its header since it was written that `command` is **absent**, and why.
`tests/unit/worker/test_kubernetes_manifests.py` asserted it, **of the rendered
YAML**. Nothing dispatches the rendered YAML. The Job that reached the cluster
was built in Python, and no test looked at it. The same defect sat latent on
Cloud Run: `CloudRunJobDispatcher._build_job` set the same `command` on any Job
the scheduler creates itself when `get_job` returns NotFound (a new tenant,
profile or resource class). Only the Terraform-created Jobs, which leave
`command` unset, kept Cloud Run working. `test_dispatch_manifests.py:183`
asserted the override.

*The fix.* Neither dispatcher sets `command` or `args` any more. The lifecycle
finds its runner by the profile *name* in `RUNNER_PROFILE`, which `worker_env`
sets, and starts `profile.command` as its child (`lifecycle._runner_argv`). Tests:

* every profile, `_manifest` and `_build_job` (and the Cloud Run execution
  override), no `command` or `args`;
* the GKE manifest's own environment fed to `WorkerConfig.from_env` resolves the
  catalogue's runner argv;
* `test_the_rendered_job_and_the_dispatched_job_are_the_same_job` renders the
  YAML and builds the dispatcher's Job from the same inputs and fails on any
  field that changes what the pod does.

That last test also found that the dispatcher put
`cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` on the Job only. The
autoscaler reads it from the pod, so dispatched pods were evictable in a
scale-down. It is now on the pod template too. The contract request to document
`RunnerProfile.command` as child argv is request 18 in
[contract-change-requests.md](../contract-change-requests.md). The owner
accepted it on 2026-09-24 in its stronger form: the field is now
`RunnerProfile.runner_argv` (PR #44). This record keeps the old name where it
describes what the code did at the time.

*Two things the first version of the fix missed (review of PR #31).*

* **Jobs created before the fix kept the override.** Removing `command=` from
  `_build_job` changes only Jobs created afterwards. `ensure_job` returned as
  soon as `get_job` succeeded, and a Cloud Run per-execution `ContainerOverride`
  has no `command` field, so `run_job` could not clear it. Every Job the
  scheduler had already created (a non-default resource class, or a provider
  outside the tenant's Terraform `providers`) would have gone on running the bare
  runner. `ensure_job` now reads the Job it fetched, clears `command` and `args`
  with `update_job` before the first execution, and refuses the dispatch
  (`cloud_run_job_overrides_entrypoint`) if the update fails. Whether any such
  Job exists in the deployed project was not measured.
* **Once the lifecycle ran, the backend's deadline pre-empted its timeout.** The
  Job's `activeDeadlineSeconds` and the Cloud Run execution timeout were both
  `task.timeout_seconds`. The Job's deadline counts from Job creation, including
  node provisioning and the image pull. The lifecycle's deadline counts from its
  own start, minutes later. So the backend always SIGTERMed first. On SIGTERM
  the lifecycle parks the task as `SCHEDULED_RETRY` rather than failing it, and
  nothing promotes that park (see §5). A task that ran out of time would have
  stayed PARKED, and its workflow would have stuck again, this time because of
  the fix. The backend deadline is now the task timeout plus
  `dispatch_timeout_seconds` (the latest a lifecycle can start before the
  reconciler reclaims it) plus a 300s budget for the lifecycle's work after its
  own deadline. `backend_deadline_seconds` in `dispatch.py` is the only place
  that computes it, and `render.py` imports it.

*What is still unproven.* The lifecycle has never run on GKE. Workload Identity
from the pod to Firestore and Secret Manager, egress through the tenant
NetworkPolicy, and Workload Identity with `automountServiceAccountToken: false`
are all untested there. See §5.

---

## 3. What would have caught it earlier

Specific, and honest about which half of the incident each would have covered.

**Causes 1 through 5: smoke covering every backend.** One `browser` task in
`make smoke` — that is, one task per *backend* rather than one task — would have
failed on the day GKE dispatch was written and on every day after. This is the
highest-value change in the whole incident and it is now in place
(`scripts/smoke-test.sh`, a row per concrete backend, failing loudly when a
backend has no available profile instead of dropping it from the matrix).

**Cause 1 specifically: one authority for the namespace prefix.** The prefix
lived in seven components because it is in no frozen module.
`scripts/lib/check-contract-parity.sh` section 6 (from line 536) now makes the
scheduler's `GkeTarget.namespace_template` the authority, compares every declared
prefix in the repository against it, and sweeps every namespace *literal* — test
fixtures included — for one that does not start with it. A fixture that wants the
wrong spelling on purpose must say `# namespace-prefix-exempt:` and why.

**Cause 6: nothing offline could have caught it.** This has to be said plainly,
because the temptation is to claim a test would have. The manifest was valid
YAML, every placeholder was substituted, the RoleBinding applied without error,
the `Role` granted the right verbs on the right resource, and the service account
it named genuinely existed. There is no static property of that file that is
wrong. What *can* be caught offline is the **asymmetry** — a binding that names a
Google service account by only one of its two names — and that is now asserted
for every RoleBinding the tenant render produces
(`tests/unit/worker/test_kubernetes_manifests.py:952-1008`), with a
`checked == 2` floor so that a change in subject shape fails loudly instead of
matching nothing. A second case there refuses a render that emits a *made-up*
uniqueId when none was supplied, because that fails exactly as silently.

**What would actually have caught cause 6: a dispatch failure that says who it
authenticated as.** The one thing the operator could not see was the mapping
between the numeric subject in the error and the email in the manifest. The
error carried the digits; the scheduler knew the address; nothing printed both.
So, as of this incident, a 403 on `jobs.batch` names **both the namespace it
tried and the identity it presented**, and states that the binding must name that
account by email *and* by numeric uniqueId:

* `apps/scheduler/scheduler/dispatch.py` — the `gke_create_job_forbidden` branch,
  built from `presented_identity()`, which reads the identity off the credentials
  that minted the token (`authenticated_identity`, `dispatch.py:696`) and never
  invents one: an unresolved identity is reported as such rather than derived
  from the project id, because a plausible wrong address sends the reader to
  check a binding against an account that was never presented.
* The message reaches an operator through `loop.py`'s `dispatch failed` line. The
  tenant receives `exc.code` only — `task.last_error` is returned to callers
  verbatim, so the upstream text never goes there.
* Asserted in `tests/unit/control_plane/test_dispatch_403_names_the_subject.py`,
  including one case that drives the real `google.auth` path offline so the email
  in the message is provably the one the client authenticated with rather than a
  value a test handed in.

**Cause 7: nothing short of running it.** A Job that reaches `STARTED` is the
first moment a read-only root filesystem can be observed, and no manifest check
sees a runner's default path. What is now in place instead is an assertion that
the two Job specs — the Python dict the dispatcher builds and the YAML templates
`make lint` validates — agree on the environment keys that decide where the
runner *writes* (`tests/unit/worker/test_kubernetes_manifests.py:1213`, keys and
not values, because the values are per-task and the templates carry
placeholders). The comment in `dispatch.py` claiming the two "mirror each other
field for field" had been true and had stopped being true, which is the third
instance of that same defect in this incident, after the namespace prefix and the
RBAC subject: **a file documented as mirroring another, with no assertion holding
it there, is a copy waiting to drift.**

**Cause 8: comparing the two copies of the Job, not just their environments.**
The environment-key assertion above compared one slice of the two Job specs, and
the defect was in the slice it skipped. The YAML's `command is ABSENT` rule was
asserted of the YAML alone. What now holds them together is one test that builds
both from the same inputs and compares everything that changes what the pod does:
command and args, security contexts, resources, volumes and mounts, retries,
grace period, service account, and the eviction annotation on the Job and on the
pod. Had it existed, cause 8 would have failed CI on the day `_manifest` was
written. The eviction annotation, found missing from the dispatcher's pods by the
same test, would have failed with it. It is the fourth instance of the mirrored-copy
defect in this incident.

---

## 4. Diagnosing the next one

In order. The point of the order is that each step rules out a cause the message
cannot distinguish, cheapest and most likely first. Do **not** start at IAM;
three investigations did.

Point kubectl at the swarm cluster first — `make kubectl`, then export the
`KUBECONFIG` it prints. Everything below assumes it.

**Step 1: read the scheduler's own message, not the task's error.** The tenant-
facing `last_error` is a code; the upstream text is in the log.

```bash
scripts/api.sh GET /tasks/<task_id> | jq '{state, last_error, park_reason, attempt_count}'
make logs SERVICE=swarm-scheduler LINES=200
```

Look for `dispatch failed ... backend=GKE_AUTOPILOT`. That line now contains the
two facts that separate the causes: **the namespace it tried** and **the identity
it presented**. Also look for `dispatch ok ... backend=GKE_AUTOPILOT`, which
exists so that "has GKE ever dispatched?" is answerable from logs rather than by
reading task documents — its absence is what let a total outage look like an
absence of browser work.

**Step 2: does the namespace exist, with the spelling from step 1?**

```bash
kubectl get namespace <the namespace the log line named>
kubectl get namespace | grep swarm-tenant-
```

Missing, or present under another spelling → the tenant was never provisioned on
the cluster. `kubernetes/apply.sh --tenant <id>` to read the diff, then
`--confirm`. Note that a tenant document can carry a stale `namespace` field,
which the dispatcher *prefers* over its own template — the runbook's last section
has the query that finds those.

**Step 3: does the namespace carry the RBAC, and does the binding name our
identity BOTH ways?**

```bash
kubectl get rolebinding swarm-dispatcher -n <namespace> -o yaml
```

The `subjects` list must contain two `kind: User` entries for the scheduler: the
email `swarm-scheduler@<project>.iam.gserviceaccount.com`, and a 15-25 digit
number. Compare that number against the account the log line named:

```bash
gcloud iam service-accounts describe \
  swarm-scheduler@<project>.iam.gserviceaccount.com --format='value(uniqueId)'
```

One subject only, or a number that does not match → this is cause 6 again. The
binding applied cleanly and authorises nobody. Re-run `kubernetes/apply.sh
--tenant <id> --confirm`, which resolves both uniqueIds itself and refuses to
render if it cannot.

**Step 4: ask the API server, as the scheduler rather than as yourself.**

```bash
kubectl auth can-i create jobs --namespace <namespace> \
  --as swarm-scheduler@<project>.iam.gserviceaccount.com
```

Expect `yes`. `error: ... cannot impersonate` is a limit on **your** credentials
and says nothing about the binding; fall back to reading the subjects in step 3.
Note that this asks about the *email* subject, so a `yes` here does not by itself
prove the access-token path is authorised — step 3 is the check that does.

**Step 5: only now, IAM.** If the namespace exists, the binding names both
spellings, and it still 403s, read the condition on `swarmGkeDispatcher`
(`terraform/modules/iam/bindings.tf`) — and read section 4 above first, because
that condition cannot authorise a namespaced request either way.

**Step 6: the Job exists but nothing runs.** A different failure with a different
shape, and the two seen so far:

```bash
kubectl describe job -n <namespace> <job-name>
kubectl get events -n <namespace> --sort-by=.lastTimestamp | tail -40
kubectl logs -n <namespace> job/<job-name>
```

* `serviceaccount "swarm-agent-worker" not found` — the pod names a KSA nobody
  created, so it is admitted and never scheduled. No pod appears at all, which
  reads as a capacity problem. `apply.sh --tenant <id> --confirm` creates it.
* A container that starts and dies immediately on `Read-only file system` — cause
  7's family. Every path the runner writes to needs a volume, and the env var
  that points at it must be set in **both** Job specs.

Capture the events before deleting anything: they are lost when the Job is
collected.

The runbook's table at
[step 6](../runbooks/gke-dispatch-redispatch.md) maps the remaining error codes
(`gke_create_job_failed`, `backend_disabled`, `CreateContainerConfigError`) to
what to do about them.

---

## 5. What is not yet proven

Recorded because an unverified claim in this repository becomes a runbook step
someone follows at 3am.

* **That a browser task now completes.** As of `ffa82e3` the deployed scheduler
  was still the pre-merge image (tag `7c5276212251`), so causes 1, 6 and 7 are
  fixed in the repository and not yet in the running deployment. The claim these
  commits make is that each named defect was real and is now absent from the
  specs — measured against the cluster's own error text and the pod's own
  traceback — not that the path is green end to end.
* **The 30-node redispatch cannot answer it either.** Its six browser steps were
  PARKED on `DEPENDENCY_INCOMPLETE` behind `claude-code` steps parked on
  `PROVIDER_QUOTA_EXHAUSTED`, so they never reached a backend at all. A parked
  step proves nothing about dispatch.
* **Whether cause 8 is the last one.** Every fix in this incident revealed the
  next defect behind it, and there is no reason to believe the sequence is
  exhausted. Cause 8's fix means the worker lifecycle runs on GKE for the first
  time. Everything it depends on is unproven there: Workload Identity from the
  pod to Firestore and Secret Manager, egress through the tenant
  NetworkPolicy, and Workload Identity with `automountServiceAccountToken:
  false`. One further defect is predicted from the code and not yet fixed:
  `lifecycle._build_child_env` does not pass `PLAYWRIGHT_BROWSERS_PATH` (set
  only as an image `ENV` in `agent-runtime-browser`) through to the runner
  child, whose `HOME` is its work directory. Playwright should therefore look
  for Chromium under the workspace rather than `/opt/playwright`. The cheapest
  proof is one task:
  `scripts/prove-gke-dispatch.sh --timeout 900`.

  *Added 2026-09-24:* that script now exists and the release runs it after
  every deploy, after the smoke test. It submits one `browser` task and asserts
  four things separately — dispatched, and to `GKE_AUTOPILOT`; `SUCCEEDED`; an
  artifact under the tenant's own prefix; its lease released — and on failure
  prints the attempt's own error, which is where cause 7's traceback appears. A
  task PARKED on `CREDENTIAL_MISSING` fails at once, saying it proves nothing
  about GKE, rather than waiting out its timeout.

  *The lease check, corrected.* The first version read the lease from the
  task's `current_lease_id`. The worker clears that field in the same write
  that makes the task terminal (`control.finish()`), so on the real platform
  the check failed on every successful run. The fake platform it was tested
  against kept the field, a state the platform never produces, so the tests
  stayed green. The script now names every lease the task held from its
  events: the scheduler writes `lease_id` on `lease_acquired` and on
  `dispatched`. It then waits up to 60 seconds for each one to read as
  released, because `finish()` writes the terminal state before it releases
  the lease. `smoke-test.sh` and `failure-test.sh` read the same field.
  One skipped its check without a word; the other printed PASS "nothing to
  release". Both now use the same helper, `t_check_leases_released` in
  `scripts/lib/testlib.sh`.

  *And a defect in the command this line used to name.* `smoke-test.sh
  --profile browser` — and the smoke suite's `GKE_AUTOPILOT` row — submitted
  `{message, run_id}`, which the browser runner refuses before Chromium starts
  ("browser runner needs input.url or at least one action"). Both could fail
  with dispatch working perfectly. Every suite now submits through
  `profile_input` in `scripts/lib/testlib.sh`, which gives `browser` one
  screenshot of `about:blank`.

  *Still not proven:* the script has not been run against the deployment; it is
  exercised only against a fake platform
  (`tests/integration/test_gke_proof_can_fail.py`). The first release after it
  merges is the first real run — and it needs the release identity's tenant to
  hold an anthropic credential, or it will fail on `CREDENTIAL_MISSING`, by
  design.
* **A worker that is SIGTERMed still strands its task.** The lifecycle's SIGTERM
  path (`_handle_interruption`) parks the task `PARKED/SCHEDULED_RETRY` with
  `next_eligible_at=now` and releases the lease. Nothing in the platform moves a
  `SCHEDULED_RETRY` park back to READY. The scheduler sweeps only
  `DEPENDENCY_INCOMPLETE`, `CREDENTIAL_MISSING` and the three `PROVIDER_*`
  reasons, `ready_tasks` selects READY only, and the reconciler has no
  promoter. [quota-management.md](../quota-management.md) §4 says this reason is
  "unparked by `next_eligible_at`", and no code does that. The deadline fix in
  cause 8 removes the one trigger that was certain to hit. Any other SIGTERM (an
  instance or node going away) still leaves the task PARKED for ever. That
  promoter is not in this change. The frozen state machine allows PARKED to go
  only to READY, CANCELLED or DEAD_LETTERED, and admission does not check the
  retry cap, so whoever builds it has to decide what an interrupted last attempt
  becomes.
