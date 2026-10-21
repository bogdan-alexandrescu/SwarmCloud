# Tenant settings integrity: a tenant's worker cannot change what the control plane does for another tenant

**Status: designed 2026-10-17 (lane ISO-SETTINGS-DESIGN, part of #1457), and
answered by the owner the same day (§7, "The owner's answers").** The ten build
lanes of §6 have merged (PRs 1473-1487); ISO-IAM, PR 1484, merged as
`1d7780d4`, giving `terraform/modules/tenancy` its `worker_firestore_access`
switch and `register-tenant.sh` its `--worker-firestore-access`. Follow-up
lanes ISO-MAC-TF, ISO-API-SETTINGS, ISO-UPSTREAM, ISO-SEARCH and ISO-WIRE
merged as PRs 1490-1502. ISO-SCHED-2 (PR 1752) built the scheduler's half of
the per-tenant switch: terraform `state_via_api_tenants`, empty by default.
ISO-IAM-WIRE (2026-10-20) made the two role switches settable from an
environment's tfvars: terraform/infra's `worker_firestore_access` (true by
default, passed to the tenancy module) and `worker_firestore_off_tenants`
(empty by default, rendered onto swarm-api as `WORKER_FIRESTORE_OFF_TENANTS`
for job-made workspaces), each refused at plan ahead of the state switch.
[`docs/runbooks/isolation-cutover.md`](../runbooks/isolation-cutover.md) is
the cutover in Q7's order: u-bogdan, eng, every tenant, then the role's
removal from every Terraform tenant and, last, the owner's removal of its
definition from bootstrap, each step with its plan, what to watch and its
rollback. No tenant has been cut over yet.

Every file and symbol cited below was read on 2026-10-17 at `35f2a61`. Lines
move, so each citation names the symbol; search for it.

What the design settles, in one line each:

* **The problem is wider than the issue's instance.** A tenant worker can
  create, get and update any document in the `swarm` database whose id it can
  name. `workspace_settings/<B>.judgment_backend` is one such document. The
  inventory (§1.3) finds others with a worse blast radius. Tenant A's worker can
  rewrite its own `tenants/A.namespace` or `.service_account`, and its next
  attempt runs as tenant B's service account. It can also reset `pools/global.active`
  and the platform over-admits.
* **Signing settings (design A) cannot close it.** A worker must write `tasks`,
  `attempts`, `leases` and `pools.active` today: it moves its own agent's state,
  heartbeats its lease and gives capacity back. A signature cannot tell a
  legitimate write of those documents from a forged one.
* **Recommended: design B.** The worker loses direct Firestore access. It
  reaches its state through worker-only swarm-api routes,
  `/v1/attempts/{attempt_id}/state/...`. Each call is checked against the
  caller's ID token (which proves the tenant) and the attempt key (which proves
  the attempt), which is how `child-key`, `secrets:release` and
  `context/inflight` already work. Then the `swarmTenantWorkerFirestore` binding
  is removed.
* **Interim, while B is built:** the dispatcher derives each tenant's identity,
  namespace and prefix instead of reading them (phase 0). swarm-api also MACs
  the `workspace_settings` fields the control plane acts on (owner question
  Q2).
* **The boundary is the attempt, not the process.** The agent can mint its
  worker's ID token, as `child-tasks.md` §3.2 records. B therefore does not rest
  on the token alone. Within its own attempt, an agent that read the attempt
  key could do what its worker does, and nothing for another tenant.

---

## 1. The problem, as measured

### 1.1 Who can write what

The `swarm` database has one custom role,
`swarmTenantWorkerFirestore`
(`terraform/bootstrap/platform_roles.tf`, `worker_firestore = {`). It carries
five permissions: `datastore.databases.get`, `resourcemanager.projects.get`,
`datastore.entities.get`, `datastore.entities.create` and
`datastore.entities.update`. It has no `delete` and no `list`. It is bound once
per tenant to that tenant's worker account `swarm-agent-worker-<t>`, project-wide
(`terraform/modules/tenancy/main.tf`, `resource "google_project_iam_member"
"worker_firestore"`). The same binding is made for script-registered tenants
(`scripts/register-tenant.sh`, `FIRESTORE_ROLE_ID=`).

That binding is the **whole** boundary. IAM Conditions cannot narrow it:

* Firestore IAM has no collection- or document-level resource. The smallest
  resource a condition can name is the database.
* The database condition itself does not work on the data plane. The CORRECTION
  in `terraform/modules/tenancy/main.tf`, "verified live 2026-09-16", says so:
  Firestore evaluates IAM Conditions only for administrative operations, so the
  condition denied document access outright and now defaults off.

So every tenant's worker shares one authorization scope over every document in
`swarm`, and over every other Firestore database in the project. Today `swarm`
is the only one.

**The agent holds that scope as well as its worker.** It runs in the worker's
container as the worker's uid, and nothing filters `169.254.169.254`. It can
mint the worker account's token
(`docs/design/child-tasks.md` §3.2; `docs/multi-tenancy.md`, "It CAN read and
write a document whose id it can guess"). The tenant checks the worker makes
(`apps/agent-worker/agent_worker/control.py::ControlPlane._assert_tenant`) run
in a process the agent can bypass with `curl`. They are a convenience, not a
boundary.

### 1.2 The instance #1457 reports

An agent in tenant A updates `workspace_settings/<B>` with
`judgment_backend: "typesafe"`. Three readers act on that value:

* the scheduler's `apps/scheduler/scheduler/judgment.py::read_backend`;
* the scheduler's `apps/scheduler/scheduler/dispatch.py::read_judge_backend`;
* swarm-api's `apps/swarm-api/swarm_api/judgment.py::stored_backend`, read by
  `TypesafeTicketService.mint`.

If B is listed in `TYPESAFE_FORWARDER_TENANTS`, the edit has three effects:

* B's live-browser pods get the `swarm-typesafe: inject` label and the switch;
* swarm-api mints B's forwarder tickets;
* B's judgments go to TypeSafe on B's key.

This was read from the source and not exercised against the live database (the
issue says the same).

The scheduler and swarm-api do not even read the field the same way, which
matters once a fix starts verifying it:

* `read_backend` and `stored_backend` read only the top-level `judgment_backend`;
* `read_judge_backend` reads `code_intelligence.judgment_backend` first.

No swarm-api path writes that nested field. Only a direct Firestore write can
produce it, so a forger can make the two schedulers' decisions disagree.

### 1.3 The inventory: every document the control plane acts on that a tenant worker can write

This table is the heart of the document. It was built by grepping every
`.collection(` and `.document(` in `apps/scheduler`, `apps/reconciler`,
`apps/quota-broker`, `apps/swarm-api`, `apps/swarm-mcp`, `apps/swarm-live`,
`apps/swarm-github-hooks`, `apps/swarm-graph`, `apps/swarm-typesafe-fwd`,
`apps/swarm-egress`, `apps/workflow-rollup` and `apps/common/swarm_common`.
Each hit was then followed to the decision it drives.

The services with no Firestore client are not in the table. They act only on
MAC'd tickets, so there is nothing for a worker to forge:

* `workflow-rollup`, `swarm-mcp`, `swarm-live`, `swarm-graph`,
  `swarm-typesafe-fwd` and `swarm-egress`;
* `swarm-github-hooks`, which only `create()`s `github_deliveries`.

How to read the columns:

* **"guessable"** says whether a worker that was not handed the id can compute
  it. The role has no `list`, so an id that cannot be computed cannot be reached.
* **"worker writes it"** says whether the worker's legitimate code path writes
  it today. If it does, a signature cannot separate the legitimate write from a
  forged one.
* **A, B, C** say whether each design in §3 closes the row.

| # | document | guessable | worker writes it | what a forging worker achieves | who acts on it | A | B | C |
|---|---|---|---|---|---|---|---|---|
| 1 | `tenants/<own>` `.service_account` | yes (own id) | no | **A's next Cloud Run attempt runs as B's account.** `assert_tenant_identity` checks only that the value is non-empty, the Job uses it as written, and the scheduler holds `actAs` on every tenant's worker account (`terraform/modules/tenancy/main.tf`, "Dispatch: actAs"). | `scheduler/dispatch.py::assert_tenant_identity`, the Cloud Run template | yes | yes | yes |
| 2 | `tenants/<own>` `.namespace` | yes | no | **A's next GKE pod runs in `swarm-tenant-B`, as B's KSA and so as B's account.** `verified_namespace` keeps a stored value that starts with the prefix, and so accepts another tenant's namespace. | `scheduler/dispatch.py::verified_namespace`, `namespace_for`; `reconciler/store.py::tenant_namespaces` (which namespaces it scans and protects) | yes | yes | yes |
| 3 | `tenants/<own>` `.gcs_prefix` | yes | no | A's `GCS_PREFIX` names B's prefix. IAM still refuses B's objects, because the bucket grant is prefix-conditioned, so this costs failed writes rather than a leak. | `scheduler/dispatch.py::worker_env` | yes | yes | yes |
| 4 | `tenants/<B>` `.enabled`, `.max_active`, `.credentials[]`, `.kind`, `.principal` | yes | no | Pause B (`MANUAL_PAUSE`). Make B's agents park `CREDENTIAL_MISSING` or take the account pool. Change B's personal-secret opt-in. Disable B in the broker, which caps the shared provider pool. | `scheduler/store.py` `enabled_tenant_ids`; `scheduler/credentials.py::credential_for`; `scheduler/secret_slots.py`; `quota_broker/service.py` `_enabled_tenant_ids`; swarm-api `auth.py`, `attempts_secrets.py` | yes | yes | yes |
| 5 | `workspace_settings/<B>` `.judgment_backend` and `code_intelligence.judgment_backend` | yes | no | #1457: B's judgments go to TypeSafe on B's key; B's pods get the inject label; swarm-api mints B's ticket. | §1.2's three readers | yes | yes | yes |
| 6 | `workspace_settings/<B>` `.merge.*` | yes | no | B's merge mode at submission, and its merge train's size and age. | swarm-api `MergeSettingsStore.mode_for`, `mergetrain.py` | yes | yes | yes |
| 7 | `workspace_settings/<B>` `.code_intelligence.{jev,context,context_ab,graph_delivery}` | yes | no | Turn B's graph tickets off. Switch B's Jev and context delivery. | swarm-api `graph_ticket.py` (`CodeIntelligenceStore.get`); worker `judge/attempt.py`, `graphstage.py` | yes | yes | yes |
| 8 | `tenant_settings/<B>` | yes | no | B's graph context. The worker reads it (`graphstage.py`), and no swarm-api path writes it today, so its only possible writer is a forger. | worker only | yes | yes | yes |
| 9 | `pools/<name>` `.hard_limit`, `.adaptive_target`, `.quota_derived_limit`, `.enabled` | yes (`global`, `tenant:<t>`, `provider:<p>`, ... `swarm_common/models.py`) | no | Raise A's own ceiling past the admin's. Disable `pools/global` (the whole platform parks `MANUAL_PAUSE`) or B's pool. Delete a limit field, and `evaluate_capacity` treats a missing pool as unlimited. | `swarm_common/admission.py::evaluate_capacity`; `scheduler/loop.py` fairness and pause | yes (limits signed by their writers) | yes | no (§3.3) |
| 10 | `pools/<name>` `.active` | yes | **yes**: `release_lease_in_transaction` decrements it on every release | Set `pools/global.active = 0` and the platform admits past its global ceiling (invariants 2 and 3, and money). Set it huge and nothing is admitted. | `admission.py::evaluate_capacity` | **no** | yes | no |
| 11 | `leases/<id>` `.pools[]`, `.units`, `.released_at`, heartbeat fields | no for another tenant's (`<prefix>_<20 hex>`); yes for its own and for any lease it creates | **yes**: heartbeat, release | `create` a lease document with a chosen `pools[]` and `units` and run `release_lease_in_transaction` on it itself (the worker already holds every permission that needs), or leave it for the reconciler's stale-lease release: either way the pools it names are decremented. Or keep its own lease alive past cancel. | `admission.py::release_lease_in_transaction` (reconciler, scheduler); `reconciler/detect.py` stale-lease detection; swarm-api `children.py::_lease_is_live` before every ticket and secret | no | yes | no |
| 12 | `tasks/<own>` state fields (`state`, `current_lease_id`, `attempt_count`, `next_eligible_at`, `metadata.*`) | yes (own) | **yes**: every transition, park and finish | Within A: reset `attempt_count` (unbounded retries, A's money); mark its own agent SUCCEEDED; set unsigned `metadata.after_prs` or `startup_refunds`, which the reconciler acts on. | scheduler `loop.py::_admit_one`; reconciler `model.py`, `detect.py` | no | yes (the route permits only the worker's transitions, §4.4) | no |
| 13 | `tasks/<own>` spec fields (`runner_profile`, `resource_class`, `provider`, `input`, ...) | yes (own) | no (swarm-api writes them signed) | The scheduler sizes and routes from these unverified (`acquire_lease_in_transaction` takes pools from the caller's copy). A could rewrite its own READY agent's `resource_class` or `runner_profile`. The worker's `specverify` then refuses to run it, but capacity was already reserved by the forged size. Invariant 10 is held by the worker, not by the scheduler. | scheduler `loop.py::_admit_one`, `dispatch.py::worker_env` | partly (already signed; the scheduler would have to verify) | yes | no |
| 14 | `tasks/<other tenant>` | no (`<prefix>_<20 hex>`), unless the id leaks through a PR body, a log or a URL | `mergeslot.FirestoreMergeSlots.wake` updates a waiter agent's `metadata`, but only within its tenant | With a leaked id: everything in rows 12 and 13 for B. | as rows 12 and 13 | no | yes | yes |
| 15 | `attempts/<own>` | yes (own) | **yes**: start, checkpoints, peaks, spend, end | Forge its own `cost_usd` and tokens, which feed budgets. Forge `checkpoints`, though the agent pointer is fenced. | quota broker `_attempt_ended`; reconciler orphan detection (`generation`, `execution_name`) | no | yes | no |
| 16 | `merge_slots/<B>--<sha256[:40]>` | yes: `slot_id()` is a hash of the tenant, repository and base, all of which are knowable | yes (own tenant's slot) | Hold B's merge slot forever, or wake B's waiters. | worker `mergeslot.py`; the scheduler's wake path | no | yes | no |
| 17 | `merge_train_entries/mte_<sha256(t\ntask)[:32]>` | yes, given a task id | yes (own) | Enqueue or alter a B entry, given a B task id. | worker `mergetrain.py`; swarm-api merge-train routes | no | yes | no |
| 18 | `quota/<provider>:<B>` | yes | no (the worker reads it in `ControlPlane.poll`; reports go over HTTP to the broker) | Shape the caps the broker derives for B, or for the shared provider pool. | `quota_broker/service.py` (`_mutate`, the sweep) | yes | yes | yes |
| 19 | `accounts/<owner>:<label>` `.lend_to[]`, `.secret`, `.state` | yes (labels are human) | no | Lend B's subscription account to A (`lend_to`). Point `.secret` at another secret name, which the assign route returns to a worker. The worker's IAM then decides whether it can read it. | `scheduler/credentials.py` `AccountPool.serves`; broker `accountstore.py` `secret_for` | yes | yes | yes |
| 20 | `forge_grants/<t>__<userhash>__<repo>`, `forge_connections/conn_<16hex>`, `forge_orgs/...` | grants: yes, given a user email and a repo id; connections: no | no | Make B's agents see a forge credential as present or absent, which parks or admits them. | `scheduler/credentials.py` `ForgeConnections` (checks the document's own `tenant_id`, which the forger also writes) | yes | yes | yes |
| 21 | `workspace_secret_grants/*`, `workspace_secrets/<id>` | secret ids: no (`wsec-<hex>`) | no | With a known id: tick B's grant, so a required slot appears filled. The release route re-checks the grant attestation (`workspace-secrets.md` §3.3, SEC-GRANT-ATTEST), so a forged grant releases nothing. | `scheduler/secret_slots.py::missing_required_slots` | yes (already attested at release) | yes | yes |
| 22 | `provider_keys/swarm-tenant-<B>-typesafe` | yes | no | Point B's ticket at a different secret version, or make B's key look absent. | swarm-api `typesafe_ticket.py` key resolution | yes | yes | yes |
| 23 | `schedules/<id>` | no (`sch_` + random, `schedules.py` `ID_PREFIX`), unless an id leaks | no | Fire work as the stored `owner` in the stored `tenant_id`, with stored `params`. The tick re-checks directory membership (`schedulefire.py` `_finish`), which bounds it to a real member of that tenant. | swarm-api `schedulefire.py` | yes | yes | yes |
| 24 | `workflows/<wf_id>` | no (`wf_` + hex) | no | With a leaked id: fail B's workflow or stall it. | `scheduler/store.py` `workflow_on_step_failure`; `reconciler/store.py` | yes | yes | yes |
| 25 | `control/dispatch` | yes | no | **`dispatch_paused: true` stops all dispatch for every tenant.** | `scheduler/store.py::dispatch_paused` | yes | yes | no (one shared document) |
| 26 | `control/settings` | yes | no | Platform settings that swarm-api reads. | swarm-api | yes | yes | no |
| 27 | `child_keys/<HMAC>` | no (HMAC'd id) | the worker registers through swarm-api, not directly | Nothing: the registration is attested, so a rewrite becomes a refusal. | swarm-api `children.py` | already | already | already |
| 28 | `approvals/apr_<24hex>`, `schedule_firings/<sid>:<slot>` | approvals: no; firings: yes | no | The decision transaction checks a digest, so a forged firing must still match `params_digest`. | swarm-api `approvals.py`, `schedulefire.py` | already | yes | no |
| 29 | `reconciler_state/checkpoint_gc` | yes | no | Throttle or rush the GC sweep. | reconciler | yes | yes | no |
| 30 | `jev_usage/<t>_<YYYY-MM>` | yes | defined, but no production caller constructs it (`judge/budget.py`) | None today. A future budget check would trust it. | none yet | n/a | yes | yes |

**Reads, for completeness.** Invariant 9 covers confidentiality, not only
integrity. Every guessable document above is readable too: `tenants/<B>` (the
principal, the budget), `workspace_settings/<B>`, `quota/*:<B>`, `accounts/*`.
So is any agent whose id leaks, including its prompt and repository. Design B
closes the reads together with the writes once the role is removed (owner
question Q5).

**Three worker reads are queries**, which the role cannot run, because it has
no `entities.list`:

* `agent_worker/inputs.py` `find_ci_fix_tasks`;
* `agent_worker/indexrun.py` `kept_commits`;
* `agent_worker/graphstage.py` `_candidates`.

`ControlPlane.parked_uploads`'s docstring records that the same kind of query
returned 403 in dev. Whether these three fail live was not measured. Under
design B they become routes, so the question disappears.

*Measured since (lane GRAPH-STAGING, part of #1694):* the third one fails
live. On 2026-10-19/20, 73 of 74 implement steps staged no graph, 33 with
`staging failed (PermissionDenied)` from `_candidates`' `index_versions`
list. It no longer lists: an unswitched tenant's step reads its registration,
kept versions and settings from swarm-api's
`POST /v1/attempts/{attempt_id}/context/graph` (`swarm_api.attemptcontext`,
the worker's `agent_worker.graphroute`), signed with the child path's attempt
key, and a worker with no key reads the promoted version by id. The worker's
role gains nothing. `kept_commits` (#1693) is the same refused query and
still lists on the direct path.

### 1.4 What "done" lets someone do that they cannot today

* **An operator can offer a workspace to a second team without trusting the
  first team's agents.** Today any tenant's agent can stop or redirect the
  others (rows 1-11). "Done" means no tenant identity can change, or read,
  another tenant's control-plane state.
* **A security review can state the Firestore boundary as closed, not
  narrowed.** That rewrites the residual paragraphs in
  `docs/multi-tenancy.md` ("Until then the honest statement is that the
  Firestore boundary is *narrowed*, not *closed*") and in
  `terraform/modules/tenancy/main.tf`.
* **The control plane's readers can trust what they read** without each
  reader re-deriving or re-verifying it. Every writer is then a platform
  identity: swarm-api, the scheduler, the reconciler or the quota broker.

---

## 2. Constraints that bind the design

### 2.1 Facts no design can change

* **No IAM condition scopes a Firestore document** (§1.1). A design has to
  remove the grant, move it to another database the worker cannot reach, or
  make the bytes unforgeable.
* **Firestore transactions are per database.** Admission reserves capacity
  across `pools/global`, `pools/tenant:<t>`, `pools/resource:<rc>`,
  `pools/runner:<p>`, the provider pools, the agent and the new lease in **one**
  transaction (`admission.py::acquire_lease_in_transaction`). Those pools are
  shared across tenants, so they live in one database.
* **The agent can mint its worker's token and read its container's
  environment and files** (`child-tasks.md` §3.2). Anything that authorises a
  state write must not reach the agent through the environment, a file or
  `work/` while the agent runs. **What the agent cannot read is the worker's
  heap after `hardening.py::make_non_dumpable` reports `PROTECTED`.** It also
  cannot act before it is spawned.
* **Firestore Security Rules do not apply.** They govern the mobile and web
  client SDKs. A server client authenticated by IAM bypasses them, and the
  worker uses `google-cloud-firestore`. This is recorded so that no lane
  proposes them.

### 2.2 The CONTRACT.md invariants it touches, and how B keeps each

* **1. Only LEASED/DISPATCHED/STARTING/RUNNING create demand.** Unchanged. The
  state routes move the same transitions the worker makes today. A park still
  releases the lease in the same call, so a PARKED or READY agent holds nothing.
  No new state, no pending pod, nothing queued in Kubernetes.
* **2. All-or-nothing reservation in one transaction.** Admission is untouched:
  the scheduler still runs `acquire_lease_in_transaction`. The release moves
  from the worker's process to swarm-api's, and it is the same
  `release_lease_in_transaction`, imported from the frozen contract and run in
  one transaction. **B strengthens this invariant.** Once no tenant identity can
  write `pools/*` or create a `leases/*` document, rows 9-11 are gone, and the
  only writers of `active` are the scheduler (acquire) and a platform identity
  (release).
* **3. Concurrency counts from LEASED.** Unchanged. The lease is created at
  admission, as now.
* **4. Workers never sleep through a long wait.** Unchanged. A park is one call
  that writes PARKED and releases the lease, and then the worker exits. A
  swarm-api outage cannot make a worker wait: the client's retry budget is
  bounded by the existing `ControlPlane.startup_budget` and `call_options`
  windows, and an exhausted budget ends the attempt the way a Firestore error
  does today (§4.7).
* **5. Every attempt carries a fencing generation.** **This is the invariant
  the design must not weaken.** Today the fence is `ControlPlane._fenced_task`,
  a transaction the worker runs. Under B every state route runs that same check
  server-side, in the transaction that writes. The check covers the agent's
  `current_generation`, `current_lease_id` and state, the lease's `generation`,
  `task_id` and `released_at`, and the attempt tuple in the request. A stale
  generation gets `409 attempt_fenced` and nothing is written. The worker
  treats `409 attempt_fenced` exactly as it treats `ControlSignals.fenced`
  today: it exits without running the agent and without touching the lease. The
  attempt proof is bound to the generation (`swarm_api/childkey.py::AttemptTuple`),
  so a stale worker's key cannot sign for the new generation.
* **8. Checkpointing is mandatory and periodic.** The checkpoint *bytes* still
  go to GCS under the tenant prefix, exactly as now. Only the
  `attempts/<id>.checkpoints` record and the agent's `latest_checkpoint` pointer
  move behind `POST .../state/checkpoint`. A failed record call is handled the
  way a failed Firestore write is handled today
  (`ControlPlane.record_checkpoint`). The interval is still the catalogue's.
* **9. Per-tenant isolation.** This is what the design is for. The tenant is
  taken from the ID token's email (`identity.worker_service_account_id`, the
  rule contract request 43 made public for this purpose). It must equal the
  tuple's `tenant_id`, and the tuple must be the one the lease records. A
  request never names a document id outside its own attempt's tuple. The route
  derives every id it touches from the tuple and from documents only platform
  identities write.
* **10. Callers pick a runner_profile by name.** No route accepts a spec field.
  The state routes write only state, timing, usage and checkpoint fields. A
  body carrying a spec field (`runner_profile`, `resource_class`, `input`, ...)
  is a 422 from a strict model (`extra="forbid"`, as `children.py` `_Strict`
  does). That closes row 13 for writes. The scheduler's admission is still
  unverified against the signature, but the only writer left is swarm-api,
  which signed it.

Invariants 6 (no Spot) and 7 (`requests == limits`) are not touched. Nothing
here changes a pod or a Job's resources.

### 2.3 What it needs from `apps/common/swarm_common/`

Design B needs **no change to a frozen type**. It imports these as they are:

* `admission.release_lease_in_transaction`;
* `states.can_transition` and `assert_transition`;
* `identity.worker_service_account_id`.

§5 requests two additions and one documentation edit. Each is a request, and
none is applied here.

---

## 3. Designs that could work

### 3.1 Design A: swarm-api signs the settings the control plane acts on

**What it is.** Every writer of a configuration document computes a MAC over
the fields it wrote and stores it beside them:

* swarm-api for `workspace_settings`, `tenants`, the pool limits, `schedules`,
  `forge_*` and `provider_keys`;
* the quota broker for `accounts` and the quota-derived pool fields.

The MAC is computed over a canonical form, as `specsign.canonical_step_spec`
does for an agent. Every reader recomputes it and treats a missing or bad value
as "unset", which means the platform default.

The readers are all platform services, so a symmetric MAC is enough. The key
is a Secret Manager secret no tenant account reads, like `swarm-child-key`. KMS
asymmetric signing, which `specsign` uses, is needed only when a worker
verifies, and none does here.

**What it costs.**

* *Code:* a canonical form per document kind. It must live in the frozen
  contract, because the scheduler, the broker and swarm-api must compute
  identical bytes (§5 request B). Then every writer must sign (about ten
  swarm-api modules, plus `quota_broker/service.py` and `accountstore.py`), and
  every reader must verify (about fifteen call sites in §1.3).
* *Migration:* every existing document is unsigned. Either a backfill job
  re-signs them, or readers honour unsigned values until a cutover date, the way
  `SPEC_LEGACY_UNTIL` worked for agents. That window is a window in which the
  forgery still works.
* *Latency:* negligible. One HMAC per read.
* *Money:* one secret, one key rotation procedure.

**What it breaks or leaves open.**

* **It cannot close rows 10-17.** Those documents are written by the worker's
  legitimate path, so a MAC over them would need a key the worker holds, and the
  agent can read anything its worker holds unless it is in the protected heap.
  Over-admission through `pools/global.active` (row 10) and forged leases
  (row 11) stay open. Those are S0 classes of their own: "over-admission or a
  slot released under a running worker".
* It does not stop reads (§1.3, "Reads").
* Every new configuration field must remember to join the canonical form. The
  day one does not, the forgery is back, silently. This is the same failure
  shape as `specsign`'s uncovered `metadata` keys (row 13).
* **A forger can still deny service.** A bad MAC means "unset", so a forger
  cannot set a value, but it can reset one to the default. If the default is
  safe this is a nuisance. If the default is permissive, it is not: a missing
  pool is unlimited (`evaluate_capacity`). So every reader also needs a
  fail-closed default per field.

### 3.2 Design B: the worker reaches its state only through swarm-api, scoped by caller identity and attempt key (recommended)

**What it is.** swarm-api gains worker-only routes under
`/v1/attempts/{attempt_id}/state/`, one per operation the worker performs
today (§4.2). Each route:

1. verifies the ID token with the pinned worker audience
   (`routes/children.py::_worker_verifier`), and requires the email to equal
   `worker_service_account_id(tuple.tenant_id)@<project>.iam.gserviceaccount.com`
   (`children.py::worker_email`);
2. verifies the attempt proof against the attested registration
   (`childkey.verify_proof`), over method, path, the body hash and the
   timestamp, as `children.py` does. `POST .../state/open` (§4.3) is the
   exception: it verifies the one-use registration nonce instead;
3. runs the operation in one Firestore transaction. The transaction re-reads
   the agent and the lease, applies the fence, and writes only documents derived
   from the tuple.

Once every worker uses the routes, the `worker_firestore` binding is removed
from `modules/tenancy` and from `register-tenant.sh`. The worker account then
holds no Firestore permission at all.

**Why the attempt key and not the token alone.** The token proves the tenant,
and that alone closes every cross-tenant row. But the agent holds the token, so
on its own it would let an agent in A do anything A's workers can do to A's
agents: mark its own agent SUCCEEDED, reset its retries, release its lease early
and keep running (rows 12, 15). The key is generated in the protected heap
after `make_non_dumpable` and registered before the agent exists
(`child-tasks.md` §3.2). With it, the route acts only for the worker of this
attempt. The machinery exists and is in production for child submission,
secret release, git tools and in-flight context. B extends it to state.

**What it costs.**

* *Code:* **the largest of the three.** It means:
  * one swarm-api module that holds the fenced transactions, moved from
    `agent_worker/control.py`, `mergeslot.py`, `mergetrain.py` and
    `handoff.py`;
  * about 20 routes;
  * a worker client that implements the same `ControlPlane` and `MergeSlots`
    method signatures over HTTP, so `lifecycle.py` and the runners do not
    change;
  * a nonce for every attempt (today only some get one, `dispatch.py::worker_env`,
    `keyed =`).
* *Migration:* a per-tenant switch (`SWARM_STATE_VIA_API`), dev first. Both
  paths write identical documents, so a tenant can be switched back by flipping
  the switch. The role is removed only when no tenant uses the direct path.
* *Latency:* every state call becomes an HTTPS call to swarm-api in the same
  region, on top of the Firestore transaction swarm-api then runs. Expect tens
  of milliseconds more per call. That is an estimate; phase 2 measures it. A
  worker makes a few dozen state calls per attempt plus one heartbeat every 30
  s (`HEARTBEAT_INTERVAL_SECONDS`), so the added time per attempt is seconds,
  not minutes.
* *Load and money:* 200 concurrent attempts heartbeating every 30 s is about
  400 requests a minute, which is well inside one swarm-api instance. The real
  cost is availability (next bullet). If the owner raises swarm-api's minimum
  instances to 2, that is about $65 a month per always-on 1 vCPU / 1 GiB
  instance at list price. That too is an estimate, not a quote.
* *Availability:* **swarm-api becomes a dependency of every running attempt.**
  The lease timeout is 120 s against a 30 s heartbeat (`swarm_common/config.py`,
  `lease_timeout_seconds`). An outage longer than about 90 s lets the reconciler
  reclaim running attempts, which then resume from their last checkpoint.
  Workers already depend on swarm-api for children, secrets, git tools and
  tickets, and on the quota broker, so this widens a dependency rather than
  creating one. Owner questions Q3 and Q6 decide how much to spend against it.

**What it breaks.**

* Any code that reads Firestore from the worker and that the build misses
  starts failing with `PermissionDenied` once the role is removed. §4.6's
  worker file list is the inventory, and phase 3's tftest plus an acceptance
  probe hold it.
* The scheduler and reconciler keep their direct access. They are platform
  identities, and nothing changes for them.

### 3.3 Design C: one database per tenant

**What it is.** Each tenant's agents, attempts, settings and so on live in their
own Firestore database. Each worker account is granted only its own database.

**Why it does not work here.**

* **The grant cannot be scoped to a database.** The only per-database scoping
  IAM offers is a condition on `resource.name`, and §1.1's correction is that
  Firestore does not evaluate conditions on the data plane. That condition
  denied everything. A database per tenant gives no isolation unless it is also
  **a project per tenant**: a separate GCP project, Firestore, service
  accounts, quotas and billing for every workspace, including every personal
  `u-<user>` tenant.
* **Shared pools cannot move.** Invariant 2 needs the global, resource, runner
  and provider pools in the same transaction as the tenant's agent and lease. A
  transaction spans one database. So the pools and leases stay in a shared
  database, and the worker must not write it (row 10). Its release therefore
  has to move behind a control-plane call anyway, which is design B's hardest
  part, with C's cost on top.
* **Every cross-tenant reader must fan out.** That means
  `scheduler/store.py`'s ready queues, the reconciler's snapshot and the
  broker's sweep, across N databases or N projects. The scheduler's fairness
  across tenants becomes a cross-database merge.
* **Money:** a project per tenant multiplies fixed costs, and changes
  onboarding from a Firestore write to a project creation.

C is recorded because the residual comment names it. It is not recommended.

### 3.4 Recommendation: B, with a small interim

**B is the only design that closes every row of §1.3.** It closes rows 10-17,
which A cannot reach and which include over-admission. It does this with
machinery that is already in production (the attempt key, `_lease_is_live`,
the fenced transaction pattern in `attempts_secrets.py`). C cannot be enforced
without a project per tenant, and it still needs B's release route.

B takes several lanes (§6). Rows 1 and 2 let one tenant run as another today,
and that should not wait for B. So before B:

* **Phase 0, no design choice needed: derive, don't read.** The dispatcher
  derives the tenant's service account (`worker_service_account_id`), namespace
  (the template) and GCS prefix. It never reads them from `tenants/<t>`, and it
  logs any disagreement. `verified_namespace` stops trusting a stored value that
  merely starts with the prefix. The reconciler's `tenant_namespaces` derives
  too. This closes rows 1-3 permanently, and it stays correct after B.
* **Interim A on `workspace_settings` only (owner question Q2).** swarm-api
  MACs the fields the control plane acts on: `judgment_backend`, `merge.mode`
  and `code_intelligence.*`. The scheduler and swarm-api treat a missing or bad
  MAC as unset, and an unset `judgment_backend` is `internal-jev`, which fails
  closed (`read_backend`'s default). This closes #1457's instance (rows 5-7)
  within a lane or two, while B is built. It costs request B in §5, and it can
  stay after B as defence in depth or be removed. The owner decides.

---

## 4. The recommended design, in enough detail to build

### 4.1 Identity: three proofs, each for what it can prove

| proof | proves | carried by | the agent can |
|---|---|---|---|
| ID token for `SWARM_API_AUDIENCE` | the caller acts as `swarm-agent-worker-<t>` | `Authorization` or `X-Serverless-Authorization` | mint it (metadata server) |
| registration nonce `HMAC(child_key, "swarm-child-nonce/v1\n" tuple)` | the dispatcher started this tuple | `SWARM_CHILD_NONCE` env, used once in `state/open` | read it after it was spent (`/proc/1/environ`) |
| attempt proof, Ed25519 over `"swarm-state-req/v1\n" method "\n" path "\n" sha256(body) "\n" timestamp` | the request comes from the worker of this attempt | `X-Swarm-Attempt-Proof`, `X-Swarm-Attempt-Timestamp` | not forge it: the private key is in the protected heap |

**The prefix is domain-separated from `swarm-child-req/v1`.** A child-submission
proof can therefore never be replayed as a state write. The key, its
registration and its attestation are the existing ones (`child_keys/<HMAC>`).
One key per attempt signs both kinds of request.

### 4.2 Routes

All routes are `POST`, except `GET .../state/context`, `GET .../state/poll`
and `GET .../state/upstream`, and all are worker-only. They declare neither
`current_auth` nor `tenant_scope`, as `routes/attemptcontext.py` does, because
no person may call them. Every body includes the attempt fields
(`tenant_id`, `task_id`, `lease_id`, `generation`), and its model is strict.
Every answer that writes carries `{"ok": true, "task": <fence view>}`. The fence
view is `{state, current_generation, current_lease_id, cancel_requested,
next_eligible_at}`, so the worker's `ControlSignals` can be built from it
without a second call.

| route | replaces | writes (in one transaction, after the fence) |
|---|---|---|
| `state/open` (nonce, public key) | `ControlPlane.validate_generation`, `fetch_task`, `fetch_lease`, `fetch_attempt`, and the child-key registration | registers the attempt key once per generation (`409 child_key_taken` after that); returns the task document (spec included, as the worker verifies its signature anyway), the lease and the restore attempt's checkpoint binding |
| `state/transition` `{to: DISPATCHED\|STARTING\|RUNNING}` | `transition`, `advance_to_starting`, `advance_to_running` | `tasks/<id>.state`, `started_at`, `updated_at`, plus the event |
| `state/attempt-start` `{backend, execution_name}` | `record_attempt_start` | `attempts/<id>` create |
| `state/checkpoint` `{uri, digest, at, ...}` | `record_checkpoint` | `attempts/<id>.checkpoints`, `tasks/<id>.latest_checkpoint` |
| `state/usage` `{kind: resource\|cpu\|spend, fields}` | `record_resource_usage`, `record_cpu_usage`, `record_spend` | `attempts/<id>` named fields only. Each `kind` has an allow-list of fields. `cost_usd` is recomputed server-side from tokens where a price is known. |
| `state/heartbeat` | `heartbeat` | `leases/<id>.heartbeat_at`, `expires_at`, only while unreleased and matching |
| `state/park` `{reason, next_eligible_at, blocked_by, carry}` | `park`, `park_awaiting_children`, `park_ci_pending`, `handoff.py`'s park | PARKED and the lease release (`release_lease_in_transaction`) in one transaction, plus the carry index |
| `state/finish` `{state: SUCCEEDED\|FAILED\|CANCELLED\|DEAD_LETTERED, end_cause, last_error, result_summary}` | `finish` | the terminal state and the lease release |
| `state/retry` `{state: READY\|FAILED\|CANCELLED, ...}` | `fail_retryably` | as now, plus the release |
| `state/attempt-end` `{exit_code, error}` | `record_attempt_end` | `attempts/<id>.completed_at`, `exit_code`, `error` |
| `state/release` `{reason}` | `release_lease` | `release_lease_in_transaction` on **this tuple's** lease only |
| `state/event` `{type, detail}` | `emit` | `tasks/<id>/events/<evt_*>`; `type` must be an `EventType` member |
| `GET state/poll?provider=` | `poll` | none; returns the fence view and `quota/<provider>:<own tenant>` |
| `GET state/context` | `secrets.load_tenant`, `jevkey` reads, `judge/attempt.py`, `graphstage.py` settings reads | none; returns the own tenant's `tenants/<t>` subset, `workspace_settings/<t>`, `tenant_settings/<t>`, the `provider_keys` record and the forge grant for the signed `submitted_by` and `repository_url` |
| `GET state/upstream?task_id=` | `fetch_parent_states`, `inputs.py` upstream reads | none; only ids in the signed `depends_on`, or reached through the agent's own `metadata.input_from`; each must carry the same `tenant_id` |
| `GET state/repository` | `indexrun.py`, `graphstage.py` repository and index reads | none; `repositories/<repo_id_for(signed repository_url)>` and its `index_versions` (a server-side query, so the three refused queries of §1.3 become possible) |
| `state/merge-slot` `{op: acquire\|renew\|confirm\|release\|wake}` | `mergeslot.FirestoreMergeSlots` | `merge_slots/<slot_id(own tenant, signed repository, base)>`; `wake` only for waiter agents of the same tenant listed in that slot |
| `state/merge-train` `{op, ...}` | `mergetrain.py` writes | `merge_train_entries/<entry_id_for(own tenant, own task)>` |
| `state/ci-fix-tasks` | `inputs.find_ci_fix_tasks` | none; the query runs server-side, restricted to `tenant_id == own` |

The error answers follow the child routes' shapes:

* `401 attempt_unauthenticated`: bad or absent token, or wrong audience;
* `403 attempt_unproven`: bad proof, skewed timestamp, or no registration;
* `409 attempt_fenced`: stale generation, released lease, or wrong state;
* `422`: a strict-model refusal, or a transition `states.can_transition`
  refuses;
* `503 state_unavailable`: Firestore error, retryable.

**Idempotency.** The worker retries a call whose answer it did not receive. Two
cases need an id:

* **Transitions, parks and finishes** carry `request_id`. The transaction is a
  no-op success when the agent is already in the target state for this
  generation. That is how `ControlPlane.transition` treats a repeat today.
* **Events and usage** carry an `event_id` or a sequence number, and a repeat
  of a seen id is a no-op.

No new collection is needed for this.

### 4.3 Ordering: the key exists before the first write

Today the worker reads the fence, writes DISPATCHED and STARTING, and only
then registers the child key, while STARTING (`child-tasks.md` §3.2, step 3).
Under B, `state/open` is the worker's **first** call, made right after
`make_non_dumpable`. It registers the key, so every later call is signed.

So the registration window widens from "task is STARTING" to "task is LEASED,
DISPATCHED or STARTING, with this live lease". That is still before RUNNING,
and the agent is still spawned only after RUNNING
(`lifecycle.py::Worker._prepare`). It can neither win the race nor register
later: the slot is taken once per generation.

**Every attempt gets a nonce.** Today `worker_env` mints one only for a
top-level agent, a child with git tools, or a child that consumes workspace
secrets (`keyed =`). Under B the condition becomes `True` whenever
`SWARM_STATE_VIA_API` is on for the tenant.

**A worker whose heap is not protected** (`make_non_dumpable` does not report
`PROTECTED`) today registers a tombstone and runs without children. Under B it
could not write state at all. Owner question Q4 decides what happens:

* register the key anyway, flagged `unprotected`, so the agent could read the
  key, which is within-tenant exposure equal to today's;
* or end the attempt retryably before the agent exists.

### 4.4 What the routes refuse: a worker's transitions, and nothing else

The route permits only the transitions the worker makes today. Each must be
legal under `states.can_transition` **and** in the worker's subset:

* LEASED→DISPATCHED;
* DISPATCHED→STARTING;
* STARTING→RUNNING;
* STARTING or RUNNING to PARKED, READY, SUCCEEDED, FAILED, CANCELLED or
  DEAD_LETTERED;
* PARKED→READY, but only by the handoff path's "person returned control",
  which the route checks against `metadata.human_wait`.

§5 request A asks for that subset as a frozen constant. The route alone writes
`attempt_count` refunds, and only on the park reasons that refund today
(CHILDREN_INCOMPLETE, CI_PENDING, HUMAN_REQUIRED). The worker cannot send a
number.

**This closes rows 12 and 15 against the agent** wherever the key is
protected. What remains within a tenant is the worker's own power, exercised
by the worker.

### 4.5 Firestore document shapes

**No document shape changes.** Every route writes the fields the worker writes
today, with the same names and types, so the scheduler, reconciler, quota broker
and console read them unchanged. This is deliberate: it is what lets the switch
be per tenant and reversible.

There are two additions:

* `child_keys/<HMAC>` gains `purpose: ["child", "state"]` and
  `unprotected: bool` (if Q4 picks the first option). The attestation's
  message becomes `"swarm-child-reg/v2\n" tuple "\n" public_key "\n" flags`.
  A v1 attestation is still read as child-only.
* With interim A (Q2), each MAC'd `workspace_settings` block gains
  `mac: {v: 1, key: "<key version>", sig: "<base64>"}` beside the fields it
  covers. There is one MAC per writer's block (top level, `merge`,
  `code_intelligence`), because three modules write those blocks independently
  with `merge=True`.

### 4.6 Changes by component, and the files each lands in

**swarm-api**

* `swarm_api/attemptstate.py` (new). It holds the fenced transactions, moved
  from `agent_worker/control.py`, `mergeslot.py`'s `FirestoreMergeSlots`,
  `mergetrain.py` and `handoff.py`'s park. It calls
  `admission.release_lease_in_transaction` and `states.can_transition`, and it
  reuses `children.py::_lease_is_live` and `childkey.verify_proof`.
* `swarm_api/routes/attemptstate.py` (new): the routes of §4.2, raw-body
  handlers like `routes/attemptcontext.py`.
* `swarm_api/children.py`: the registration window (§4.3) and the v2
  attestation.
* `swarm_api/childkey.py`: the `swarm-state-req/v1` message.
* `swarm_api/main.py`: mount the router beside `attemptcontext.router`.
* `tests/unit/control_plane/test_no_route_serves_credentials.py`: this route
  serves the task document, which is no credential. The test should already
  pass, and the lane confirms that it does.

**Worker** (`apps/agent-worker/agent_worker/`)

* `statecall.py` (new): the signed HTTP client, its retry budget and error
  mapping.
* `control.py`: a `RemoteControlPlane` with `ControlPlane`'s public methods,
  chosen in `__main__.py` by `SWARM_STATE_VIA_API`. `lifecycle.py` and the
  runners call the same methods.
* `__main__.py`: no `firestore.Client` when the switch is on.
* `lifecycle.py`: `state/open` first (§4.3).
* `children.py`: uses the key `state/open` registered instead of registering
  its own.
* `mergeslot.py`: `RemoteMergeSlots`, implementing the `MergeSlots` protocol.
* `mergetrain.py`, `handoff.py`, `secrets.py`, `jevkey.py`, `inputs.py`,
  `indexrun.py`, `graphstage.py`, `judge/attempt.py`: read and write through
  `statecall`.

**Scheduler**

* `scheduler/dispatch.py`:
  * phase 0: `assert_tenant_identity` returns the derived account and logs a
    disagreement; `verified_namespace` stops trusting a stored namespace;
    `worker_env` derives `GCS_PREFIX`;
  * phase 2: `keyed` is true for every attempt when the tenant's switch is on,
    and `SWARM_STATE_VIA_API` is set;
  * interim A: `read_judge_backend` verifies the MAC.
* `scheduler/judgment.py`: `read_backend` verifies the MAC (interim A).
* `scheduler/settings.py`: the per-tenant switch list
  (`STATE_VIA_API_TENANTS`, or `*`). **Built** (ISO-SCHED-2) as
  `state_via_api_tenants`, read from `SWARM_STATE_VIA_API_TENANTS` by
  `parse_state_via_api_tenants`. `*` is not built: an entry that cannot be a
  tenant id, `*` included, is dropped with one warning and never stops the
  scheduler, and terraform refuses it at plan. Switching every tenant (step 5
  of §6's order) lists them.

**Reconciler:** `reconciler/store.py` `tenant_namespaces` derives the
namespace (phase 0). Nothing else changes, because it reads the same documents
the routes write.

**Quota broker:** no change. Rows 18 and 19 close because their only forgers
lose the role.

**Terraform** (phase 3):

* `terraform/modules/tenancy/main.tf` gains a `worker_firestore_access` variable
  that defaults to on until the last tenant is switched. The binding becomes
  `count`-gated, and the residual comment is rewritten.
* `variables.tf` and `tests/terraform/tenancy*.tftest.hcl` follow.
* `scripts/register-tenant.sh` stops granting the role.
* Removing the role from `terraform/bootstrap/platform_roles.tf` is a later,
  owner-applied step.
* `terraform/infra/main.tf`: swarm-api's minimum instances (Q6).

**UI:** no change. The console reads through swarm-api's people routes, which
do not change.

**Plugin and MCP:** no change. No person-facing route changes.

**Docs:** `docs/multi-tenancy.md` (the "It CAN read and write" paragraph),
`docs/security.md`, and this document's status line.

### 4.7 Failure cases

| case | what happens |
|---|---|
| swarm-api unreachable at `state/open` | retried within `startup_budget`; then the worker exits. The lease expires at `dispatch_deadline`, and the reconciler returns the agent to READY. Nothing ran. |
| swarm-api unreachable mid-run | heartbeats fail; the worker's existing rule for a failed heartbeat applies. If the outage outlasts the lease timeout, the reconciler fences the attempt and the next one resumes from the last checkpoint (invariant 8). |
| a `409 attempt_fenced` answer | the worker treats it as `ControlSignals.fenced` and exits without running or continuing the agent, and without a release call (invariant 5). |
| the agent calls a route with the token it minted | `403 attempt_unproven`: it has no private key. |
| the agent replays a captured request | the timestamp skew window and `request_id` make it a no-op (§4.2). |
| another tenant's worker names this attempt | `401`: its token's email is not `worker_service_account_id(this tenant)`. |
| a worker on the old path after the role is removed | `PermissionDenied` on its first read; the attempt fails retryably. Phase 3 removes the role only after `STATE_VIA_API_TENANTS` covers every tenant. |

### 4.8 What this does not do

* **It does not make the agent unable to act as its own attempt** if
  `make_non_dumpable` failed and Q4 chose to run anyway. That is today's
  posture, recorded rather than implied.
* **It does not verify spec signatures at admission** (row 13). The forger
  loses write access, which is what makes that unnecessary. Verifying at
  admission is still worth a separate issue as defence in depth. That belongs
  in the wave epic, not this lane.
* **It does not change who can call swarm-api's person-facing routes.**

---

## 5. Frozen contract: change requests

`apps/common/swarm_common/` is frozen. These are requests in the shape of
[contract-change-requests.md](../contract-change-requests.md). The operator
adds them there, numbered, once the owner approves the design. Nothing here
edits the contract.

### Request A. `states.py`: `WORKER_TRANSITIONS`, the transitions a worker may request

**Why.** The state routes enforce the worker's subset of `_ALLOWED` (§4.4).
The worker's client checks it before calling, and the scheduler and reconciler
tests assert that the worker's subset and theirs never both claim an edge
without the fence. The set therefore needs one spelling. Today it is implied by
`control.py`'s call sites, and a route that restated it would drift exactly as
CLAUDE.md warns.

**The change.**

```python
#: The edges a tenant worker may ask swarm-api to apply, each also legal in
#: _ALLOWED. Everything else is the scheduler's, the reconciler's or a person's.
WORKER_TRANSITIONS: frozenset[tuple[TaskState, TaskState]] = frozenset({
    (TaskState.LEASED, TaskState.DISPATCHED),
    (TaskState.DISPATCHED, TaskState.STARTING),
    (TaskState.STARTING, TaskState.RUNNING),
    *((s, t) for s in (TaskState.STARTING, TaskState.RUNNING)
             for t in (TaskState.PARKED, TaskState.READY, TaskState.SUCCEEDED,
                       TaskState.FAILED, TaskState.CANCELLED, TaskState.DEAD_LETTERED)),
    (TaskState.PARKED, TaskState.READY),   # the handoff's "person returned control" only
})

def worker_may_transition(frm: TaskState, to: TaskState) -> bool:
    return (frm, to) in WORKER_TRANSITIONS and can_transition(frm, to)
```

The building lane reads `control.py` and `handoff.py` again and lists exactly
the edges they use. Any edge `_ALLOWED` does not permit is dropped, not added
to `_ALLOWED`.

**Rollback.** Delete the constant; nothing else reads it.

**What it would break.** Nothing. It is new.

**Invariants.** **5**: the routes enforce the fence on every edge in the set.
**1**: every edge into PARKED or READY is paired with a release in the same
transaction (§4.2). 2, 3, 4 and 6-10 are untouched.

### Request B (only if Q2 chooses interim A). `specsign.py`: a canonical form for a settings block

**Why.** The scheduler (`judgment.py`, `dispatch.py`) and swarm-api
(`judgment.py`, `typesafe_ticket.py`, `mergesettings.py`, `codeintel.py`)
must compute identical bytes, and they share only `swarm_common`. `specsign`
already owns JCS canonicalisation and purpose separation.

**The change.**

```python
def canonical_settings_block(*, tenant_id: str, block: str,
                             fields: Mapping[str, Any]) -> bytes:
    """JCS of {"purpose": "swarm.settings/v1", "tenant_id", "block", "fields"}.

    `block` is "top", "merge" or "code_intelligence". `fields` is exactly the
    keys that block's writer owns; a key outside the writer's list is not
    covered and must not be acted on.
    """

SETTINGS_MAC_FIELDS: Mapping[str, tuple[str, ...]] = {
    "top": ("judgment_backend",),
    "merge": ("mode", "train_size", "max_train_age_minutes"),
    "code_intelligence": ("jev", "context", "context_ab", "graph_delivery",
                          "judgment_backend"),
}
```

The MAC is `HMAC-SHA256(settings_key, canonical_settings_block(...))`. The key
is the Secret Manager secret `swarm-settings-key`, its value created by
`scripts/create-secrets.sh`, and readable by swarm-api and the scheduler only.
The MAC function is not in the contract, because its key is not.

**Rollback.** Delete the function; readers fall back to "unset".

**What it would break.** Nothing until readers verify. Once they do, every
unsigned document reads as unset. So the building lane runs a re-sign pass
over `workspace_settings` before switching the readers on.

**Invariants.** **9**: it is the point of the request. 1-8 and 10 are
untouched.

### Request C. `models.py`: `Tenant.service_account`, `.namespace` and `.gcs_prefix` are records, not authority

**Why.** Phase 0 makes every reader derive these values. The frozen `Tenant`
type is where a future reader would learn whether it may trust them. Contract
request 43 already made `identity.worker_service_account_id` public for this
reason.

**The change.** Comments only:

```python
service_account: str | None = None  # RECORD ONLY: dispatch derives it with
                                    # identity.worker_service_account_id. A
                                    # tenant identity can rewrite this field.
gcs_prefix: str | None = None       # RECORD ONLY: derived from tenant_id.
namespace: str | None = None        # RECORD ONLY: derived from the template.
```

**Rollback, breakage:** none.

**Invariants.** **9**.

**Not requested:**

* no new `identity` function: the reverse map from email to tenant is a string
  compare against `worker_service_account_id`, as `children.py::worker_email`
  does;
* no change to `admission.py`: the routes call `release_lease_in_transaction`
  as it is;
* no change to `config.py`.

---

## 6. Build plan

Each lane owns the files named in its row, and no file appears in two lanes'
rows. Every lane:

* pushes its tests alone to the PR branch first, and its fix to
  `<branch>-fix`;
* runs `scripts/changed-guards.sh` before finishing.

The CLAUDE.md review applies to every lane but ISO-DOCS, because every one
touches tenant isolation or IAM.

| phase | lane | files | red-first tests (each fails before the change) |
|---|---|---|---|
| 0 | **ISO-CONTRACT** (owner applies) | `apps/common/swarm_common/states.py`, `models.py`, `specsign.py` (if Q2 picks the interim), `docs/contract-change-requests.md` | `worker_may_transition(RUNNING, LEASED)` is false; every edge in `WORKER_TRANSITIONS` is in `_ALLOWED`; `canonical_settings_block` differs for two tenants with the same fields, and ignores a key outside `SETTINGS_MAC_FIELDS` |
| 0 | **ISO-SCHED** | `apps/scheduler/scheduler/dispatch.py`, `judgment.py`, `settings.py`, `tests/unit/scheduler/` files for each | a `Tenant` with `service_account` set to another tenant's account dispatches as the derived one; `namespace="swarm-tenant-other"` renders the derived namespace; `GCS_PREFIX` is derived; (interim) a `workspace_settings` with `judgment_backend: typesafe` and no or a bad MAC yields `internal-jev` in both `read_backend` and `read_judge_backend`, and a nested `code_intelligence.judgment_backend` without a MAC is ignored; (phase 2, same lane, later PR) with the tenant switched, every attempt's env carries `SWARM_CHILD_NONCE`. **Phase 2 built** by lane ISO-SCHED-2's PR (2026-10-20): `tests/unit/scheduler/test_dispatch_state_via_api.py` (a switched tenant's every attempt, children included, carries `SWARM_STATE_VIA_API=true`, `SWARM_API_URL` and a nonce for its own tuple on both backends; an unswitched tenant's GKE manifest and Cloud Run requests are byte-for-byte unchanged) and `tests/terraform/state_via_api_env.tftest.hcl` |
| 0 | **ISO-RECON** | `apps/reconciler/reconciler/store.py`, its test | `tenant_namespaces` returns the derived namespace when the stored one names another tenant |
| 0 | **ISO-SETTINGS-MAC** (if Q2 picks the interim) | `apps/swarm-api/swarm_api/judgment.py`, `mergesettings.py`, `codeintel.py`, `typesafe_ticket.py`, a re-sign job under `scripts/`, `scripts/create-secrets.sh` (the key), their tests | a forged `judgment_backend` (no MAC) makes `mint` answer 403 `TypesafeBackendOff`; a write through each route stores a MAC that the scheduler's verifier accepts; the re-sign job is idempotent and signs only fields in `SETTINGS_MAC_FIELDS` |
| 1 | **ISO-STATE-API** | `apps/swarm-api/swarm_api/attemptstate.py` (new), `routes/attemptstate.py` (new), `children.py`, `childkey.py`, `main.py`, `tests/unit/control_plane/test_attemptstate*.py` | another tenant's token gets 401 on every route; a token without a proof gets 403; a stale generation gets 409 and writes nothing (compare the documents before and after); a `runner_profile` in any body gets 422; RUNNING→LEASED gets 422; `park` releases the lease and writes PARKED in one transaction (an injected failure leaves neither); `release` releases only the tuple's lease, whatever pool names the body carries; a repeated `finish` with one `request_id` writes once; `state/upstream` refuses an id not in the signed `depends_on`; `merge-slot wake` refuses a waiter of another tenant; a v1 child attestation still verifies for child routes and is refused for state routes |
| 2 | **ISO-WORKER-CORE** (after ISO-STATE-API) | `apps/agent-worker/agent_worker/statecall.py` (new), `control.py`, `__main__.py`, `lifecycle.py`, `children.py`, `config.py`, `tests/unit/worker/` files for each | with the switch on, no `firestore.Client` is built; `state/open` is called before any other state call and before the agent is spawned; a 409 maps to `ControlSignals.fenced`, and the worker exits without running the agent; a `RemoteControlPlane` passes the existing `ControlPlane` method tests through a fake transport |
| 2 | **ISO-WORKER-READS** (after ISO-WORKER-CORE) | `mergeslot.py`, `mergetrain.py`, `handoff.py`, `secrets.py`, `jevkey.py`, `inputs.py`, `indexrun.py`, `graphstage.py`, `judge/attempt.py`, their tests | with the switch on, each module makes no Firestore call (a fake client that raises on any call); `RemoteMergeSlots` passes the `MergeSlots` protocol tests |
| 3 | **ISO-IAM** (owner applies) | `terraform/modules/tenancy/main.tf`, `variables.tf`, `tests/terraform/tenancy*.tftest.hcl`, `scripts/register-tenant.sh`, `terraform/infra/main.tf` (min instances, Q6) | with `worker_firestore_access = false`, the plan has no `worker_firestore` binding for any tenant; `register-tenant.sh` grants no Firestore role (its shell test) |
| 3 | **ISO-ACCEPT** | `tests/acceptance/` (one new probe), its workflow wiring | from a tenant-A pod, a direct Firestore update of `workspace_settings/<B>`, a get of `tenants/<B>` and a create under `leases/` each get `PermissionDenied`; the probe is red until ISO-IAM is applied |
| each | **ISO-DOCS** | `docs/multi-tenancy.md`, `docs/security.md`, this file | `test_docs_describe_what_was_built`, where it reads these files |

Order:

1. ISO-CONTRACT, then ISO-SCHED, ISO-RECON and ISO-SETTINGS-MAC in parallel.
2. ISO-STATE-API.
3. ISO-WORKER-CORE, then ISO-WORKER-READS.
4. Switch dev's tenants, then run `make smoke concurrency-test race-test`
   against dev. The change touches dispatch and reconciliation.
5. Switch every tenant.
6. ISO-IAM, then ISO-ACCEPT goes green.

ISO-SCHED's phase-2 change (`keyed`) lands in a second PR of the same lane,
after ISO-STATE-API merges, so `dispatch.py` stays one lane's file.

---

## 7. Owner questions

Each is also in this lane's `questions.json`. The recommendation is first.

**The owner's answers, 2026-10-17 00:54Z (PDT 2026-10-16 ~17:40),** recorded on
PR 1464. Each took the recommendation:

| Q | Answer |
|---|---|
| Q1 design | (a) B. The worker reaches its state only through worker-only swarm-api routes scoped by ID token and attempt key, and its Firestore binding is removed |
| Q2 before B | (a) phase 0 (the dispatcher derives identity, namespace and GCS prefix instead of reading them) plus a MAC on `workspace_settings`, so Request B, the canonical settings block, is in scope |
| Q3 routes | (a) in swarm-api |
| Q4 unprotected heap | (a) register the key flagged `unprotected` and run |
| Q5 reads | (a) they move too, and the role is removed entirely |
| Q6 outage tolerance | (a) swarm-api minimum instances 2; the 30 s / 120 s timings stay |
| Q7 cutover | (a) a per-tenant switch, dev first (`u-bogdan`, then `eng`, then all), then the role is removed |

The owner asked for all ten lanes of §6. Contract requests A, B and C (§5) were
accepted with the design and are recorded in `docs/contract-change-requests.md`.

**Q1. Which design closes #1457?**

* (a) **B: the worker off direct Firestore, through attempt-signed swarm-api
  routes.** Recommended: it is the only option that closes every row of §1.3,
  including over-admission.
* (b) A alone, signed settings. It closes rows 1-9 and 18-26 and leaves
  `pools.active`, leases, agents and attempts forgeable.
* (c) C, a project per tenant. It is not enforceable as a database per tenant,
  and it still needs B's release route.

**Q2. What ships before B lands?**

* (a) **Phase 0 (derive, don't read) plus a MAC on `workspace_settings`.**
  Recommended: it closes rows 1-3 and #1457's instance in a few lanes.
* (b) Phase 0 only. #1457's instance stays open until B.
* (c) Nothing interim.

**Q3. Where do the state routes run?**

* (a) **In swarm-api.** Recommended: it already holds the worker verifier, the
  attempt keys and the fenced pattern.
* (b) A new dedicated service, `swarm-state`. It isolates the routes from
  swarm-api's people traffic and outages, at the cost of one more service and
  about $65 a month for each always-on instance.

**Q4. A worker whose heap is not protected (`make_non_dumpable` is not
`PROTECTED`):**

* (a) **Registers a key flagged `unprotected` and runs.** Recommended: its agent
  could act as its own attempt, which is today's posture within the tenant, and
  never another tenant's.
* (b) Ends the attempt retryably before the agent exists. Safer, but it is an
  outage on any node where `prctl` fails.
* (c) Falls back to token-only scoping. That is cross-tenant safe, but not
  within-attempt safe.

**Q5. Do reads move too?**

* (a) **Yes: the role is removed entirely.** Recommended: `tenants/<B>`,
  `workspace_settings/<B>` and any leaked agent are readable today.
* (b) No: keep a get-only role and move only the writes. That needs fewer
  routes, but cross-tenant reads by a guessable id remain.

**Q6. swarm-api outage tolerance once every heartbeat goes through it:**

* (a) **Minimum instances 2, and keep the 30 s / 120 s timings.** Recommended,
  at about $65 a month per instance.
* (b) Raise the lease timeout to 300 s. A dead worker's capacity is then held
  longer.
* (c) Change nothing.

**Q7. Cutover:**

* (a) **A per-tenant switch: dev tenants first, then all, then remove the
  role.** Recommended: it is reversible per tenant.
* (b) One switch for all tenants.
