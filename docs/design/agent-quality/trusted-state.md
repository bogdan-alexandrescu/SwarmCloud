# Trusted state: what a worker and its agent may change, and through what

**Status, 2026-11-03 (part of #2688, the owner's agent quality program).**
Read at `3e3a987` (main, 3 November 2026), and every citation names a symbol, not
a line, so search for it. This package builds on #1457's design B
([tenant-settings-integrity.md](../tenant-settings-integrity.md)) and its cutover
runbook ([isolation-cutover.md](../../runbooks/isolation-cutover.md)). It does not
repeat either. It records the inventory the program asked for, the refusals that
are now tested, the two state routes it added, how a parked attempt resumes, and
the privilege removal as a plan. **Nothing in §7 has been applied.**

The program's rule for status words applies throughout. Each claim is one of:
*proposed*, *implemented*, *behaviourally tested* (a unit test that ran), *enabled
in configuration* (a tfvars value at main), or *live-verified* (seen working on a
deployment). Where a value is unknown it says **unknown**, never zero.

| Item | Status |
|---|---|
| State routes for every worker write and read of Firestore (§2) | implemented, behaviourally tested (before this package) |
| The Jev ledger over the state routes (§3) | owned by the Jev accounting pull request, not by this package (§3.2) |
| The four refusals: another tenant, another attempt, a stale generation after a newer one, a peer or policy document (§4) | behaviourally tested in this package |
| Checkpoint, park, resume, cancellation, publication over the routes, as one cycle (§5) | behaviourally tested in this package |
| `state_via_api_tenants = ["u-bogdan", "eng"]` in dev | enabled in configuration (`terraform/environments/dev/dev.tfvars`); whether it is applied in dev is **unknown** here |
| `worker_firestore_off_tenants_iam = ["eng"]` in dev | enabled in configuration; applied **unknown** |
| Any tenant's role removal in prod | not proposed for a date; no prod tfvars sets either variable |
| Live verification of any of the above | **none done by this package** |

---

## 1. The three actors and what each can reach

* **swarm-api** holds the control plane's Firestore role. The state routes,
  `/v1/attempts/{attempt_id}/state/...` (`swarm_api/routes/attemptstate.py`,
  logic in `swarm_api/attemptstate.py`), take every write a switched worker
  makes. Each route checks three proofs: the worker's ID token (the tenant),
  the attempt key registered once at `state/open` with the one-use nonce (the
  attempt), and the fence (the generation). Every document id is derived from
  the attempt's tuple and the task spec as signed, never taken from a body.
* **The worker** (`apps/agent-worker`) runs one attempt. When its tenant is in
  `SWARM_STATE_VIA_API_TENANTS`, the scheduler dispatches it with
  `SWARM_STATE_VIA_API=true` (`config.py`, `state_via_api`). The entrypoint
  (`__main__.py`, the `StateCall` branch) then builds a `StateCall` and a
  `RemoteControlPlane` in place of a Firestore client. Every module that took
  `db` takes the `StateCall` instead (`indexrun.state_routes`).
* **The agent** (the Claude process) has **no** Firestore, GCS or Secret
  Manager client of its own. It can mint the worker's ID token from the
  metadata server (`child-tasks.md` §3.2), so the token alone is never
  authority. It cannot sign with the attempt key, which lives only in the
  worker's non-dumpable heap. What it writes are files the worker reads and
  then acts on, each bounded by the worker:
  * `.swarm-children/requests/*.json` (child submissions, relayed signed) and
    `.swarm-children/await`, which parks the agent CHILDREN_INCOMPLETE;
  * `$SWARM_ARTIFACTS_DIR/questions.json` (a count is recorded),
    `await-answers` (with a blocking question, parks HUMAN_REQUIRED with
    `question_wait`), `await-release.json` (parks DEPENDENCY_INCOMPLETE as a
    release wait);
  * `work/`, which goes into the checkpoint, `.claude/` included;
  * the swarm-git MCP (`/git/{tool}`: push a branch, never forced; issues) and
    the read-only swarm-state MCP.

  **A model's file is a request, never an authorization.** Every park the
  agent can cause is bounded by a counter read from the STORED task document
  (`_park_payload`), never from the body; §4, item 4, tests that a body claiming zero
  resumes refunds nothing past the bound.

## 2. The inventory: every operation on authoritative state

"Route" is the switched path. "Direct" is what the same operation does with the
switch off, which is still every tenant not listed.

| Operation | Worker call site | Switched: route | Direct: client and documents |
|---|---|---|---|
| Open the attempt, check the generation, read the task document, lease, attempt and carry | `control.RemoteControlPlane._open` | `POST state/open` (no proof; spends the nonce, registers the key) | Firestore `tasks`, `leases`, `attempts` |
| Poll for cancellation, the fence and quota | `RemoteControlPlane.poll` | `GET state/poll` | `tasks`, `quota/{provider}:{tenant}` |
| Parent states, upstream inputs, owner questions | `RemoteControlPlane`, `inputs.py` | `GET state/upstream` (only declared or signed-chain ids) | `tasks/{id}` |
| CI-fix agents | `inputs.py` | `POST state/ci-fix-tasks` | `tasks` query |
| Event | `RemoteControlPlane` | `POST state/event` (once per id) | `tasks/{t}/events` |
| Transition | `RemoteControlPlane` | `POST state/transition` (only `worker_may_transition` edges) | `tasks` |
| Attempt start, end | `RemoteControlPlane` | `POST state/attempt-start`, `state/attempt-end` | `attempts` |
| Checkpoint pointer and digest | `RemoteControlPlane.record_checkpoint` | `POST state/checkpoint` (URI under this agent's prefix) | `attempts.checkpoints`, `tasks.latest_checkpoint` |
| Usage: resources, CPU, spend | `RemoteControlPlane` | `POST state/usage` (named `Attempt` fields only) | `attempts` |
| Heartbeat | `RemoteControlPlane.heartbeat` | `POST state/heartbeat` | `leases` |
| Release | `RemoteControlPlane.release_lease` | `POST state/release` (this tuple's lease only) | `leases`, `pools` |
| Park, every reason, with carry | `RemoteControlPlane.park*`, `handoff.py` | `POST state/park` (counters from the stored task document) | `tasks`, `attempts`, `leases`, `pools`, `tasks/{t}/carry/*` |
| Finish, retry | `RemoteControlPlane` | `POST state/finish`, `state/retry` | `tasks`, `attempts`, `leases`, `pools` |
| Merge slot | `mergeslot.RemoteMergeSlots` | `POST state/merge-slot` (named ops; this tenant's slot) | `merge_slots`, waiter wake marker on `tasks` |
| Merge train | `mergetrain.RemoteTrainEntries` | `POST state/merge-train` (entry id from the tuple) | `merge_train_entries` |
| Tenant, workspace and tenant settings, forge grant, TypeSafe key records | `secrets.py`, `qualitygates.py`, `jevkey.py`, `judge/attempt.py` | `GET state/context` (own tenant only) | `tenants`, `workspace_settings`, `tenant_settings`, grants, key records |
| Repository registration and index versions | `qualitygates.py`, `indexrun.py`, `graphstage.py`, `judge/attempt.py` | `GET state/repository` (the signed repository's) | `repositories` and its versions |
| **Jev ledger: month and agent spend** | `judge/attempt._carried` | **the Jev accounting pull request's route (§3)** | `jev_usage` month document and attempt rows |
| **Jev ledger: settle the attempt** | `judge/attempt.settle` | **the Jev accounting pull request's route; settled even when fenced (§3.2)** | `jev_usage` |
| Platform settings: extra-repository size budget | `extrarepos.budget_bytes` | `GET state/context`'s `platform_settings` (that one field, read only) | `control/settings` |
| Quota report, account error | `control.py` | quota broker HTTP on both paths (no Firestore) | the same |
| Child agents, child key | `children.py` | `/v1/attempts/{id}/child-key`, `/v1/tasks/{id}/children` (signed, child purpose) | the same |
| Git tools, hand-off, accounts, secret release, state reads for the MCP | `gittools.py`, `handoff.py`, `accountlease.py`, `secretrelease.py`, `statemcp/relay.py` | their own signed swarm-api routes on both paths | the same |
| **Checkpoints, artifacts, inputs, child results** | `objectstore.GcsObjectStore`, `checkpoint.py` | **direct GCS on both paths** | `gs://<bucket>/tenants/<t>/...` |
| **Tenant credentials, Jev key value, browser login secret** | `secrets.SecretManagerClient`, `jevkey.py`, `browser_login.py` | **direct Secret Manager on both paths** | `swarm-tenant-<t>-*` secrets |

What still needs a direct privilege, then:

1. **Firestore, for an unswitched tenant.** The `swarmTenantWorkerFirestore`
   role, per tenant (`terraform/modules/tenancy`, `worker_firestore`). The
   runbook removes it tenant by tenant.
2. **GCS, for every tenant.** `roles/storage.objectViewer` on the tenant's
   prefix (read and list) and `roles/storage.objectUser` on the prefix less
   `tenants/<t>/verdicts/` (`worker_objects_read`, `worker_objects_write`).
   This one stays. The bytes are the tenant's own, the prefix condition keeps
   other tenants out, and what the control plane trusts about a checkpoint is
   the digest the route recorded (`state/checkpoint`'s `archive_sha256`,
   checked at restore), not the object.
3. **Secret Manager, for every tenant.** A per-secret `secretAccessor` on the
   tenant's own secrets (the forge slot through `scripts/lib/forge-slot.sh`;
   the provider secrets through `register-tenant.sh`). This one stays. No route
   serves a credential by design (`jevkey.py`), and a grant per secret already
   makes another tenant's secret Google's refusal, not this code's.
4. **`control/settings`.** No privilege is needed. `GET state/context` serves
   its `additional_repositories_budget_gib` field, and nothing else of the
   document, as `platform_settings` (§3.3).

## 3. The Jev ledger over the routes (owned by the Jev accounting pull request)

### 3.1 Why it matters

On the switched path, `judge/attempt._carried` returns the workspace's monthly
cap as the month's use ("an unread month counts as spent, never as free"). That
is the correct fallback, but it means **no Jev judgment runs for a switched
tenant whose workspace set a cap**. `judge/attempt.settle` returns None, so
**the spend of any Jev call a switched attempt makes is never recorded**. dev
lists `u-bogdan` and `eng` in `state_via_api_tenants`, so both apply to them
there. The program's rules say missing verification is not a pass and cost
accounting covers every attempt. Both gaps break those rules.

### 3.2 Where it is built

The owner decided on 3 November 2026 (answer to this package's question on
#3110) that the **Jev accounting pull request** owns the Jev ledger's state
routes: it owns Jev spend and already wires `judge/attempt.py` to them. This
package therefore adds **no** `state/jev-usage` or `state/jev-settle` route and
no `StateCall` method for them; an earlier draft did, and it was dropped so
that the platform carries one set of Jev routes, not two.

The owner also decided, on the same day, that **a fenced (superseded)
attempt's Jev spend is settled**: the settle writes the stale generation's own
row (`<tenant>_<task>_g<generation>`, its own generation's id) and the month's
total even when the fence refuses every other write, so cost accounting covers
reclaimed attempts. It still touches no agent document, lease or pool, and a stale
generation still never publishes, merges or changes the agent (invariant 5
holds for every write that is not its own ledger row). That rule is for the
Jev accounting pull request's settle route to carry; nothing in this package
writes `jev_usage`.

### 3.3 The extra-repository size budget, and what is still degraded

The owner decided on 3 November 2026 that the state context read serves the
platform's extra-repository size budget. Before that, a switched worker
applied the 6 GiB default whatever the admin set, while swarm-api enforced
the admin's figure on the forge's estimate at submission: the two checks of
one limit disagreed. Now:

* `GET state/context` answers `platform_settings`: the fields of
  `control/settings` named in `attemptstate.PLATFORM_SETTINGS_SERVED`, which
  is `additional_repositories_budget_gib` alone, each as stored and left out
  when absent. Prices and the budget ceiling are the control plane's own, so
  none is served. Read-only: no route writes `control/settings` (§4, item 4).
* `extrarepos.budget_bytes` reads that field for a switched worker, at every
  call, and applies the same rules as the Firestore read. An absent field
  gives the default. So does a malformed field or a failed read, and the log
  says so. A fenced answer (409 `attempt_fenced`) is the one exception: it is
  re-raised, never turned into the default, so a superseded worker exits
  without cloning (invariant 5).
* Tested in `tests/unit/control_plane/test_attemptstate_routes.py` (the field
  served, nothing else of the document) and
  `tests/unit/worker/test_state_routes_extra_repo_budget.py` (the worker
  reads the admin's figure through the real routes; an absent field keeps
  the default; a fenced answer is the fence).

Still degraded: **the Jev ledger**, until the Jev accounting pull request
merges (§3.2).

## 4. The refusals, tested

`tests/unit/control_plane/test_attemptstate_refusals.py` runs each attack
through swarm-api's real app, over the in-memory Firestore, and asserts that
nothing changed. `test_attemptstate.py` already held each proof's refusal on
its own (another tenant's token, no proof, a child proof, a proof for another
tuple, a stale generation simulated by editing the task document).

1. **Another tenant's agent.** Tenant `eng`'s worker uses its own token and its
   own registered key, and names `research`'s task id in the body, on every POST
   route: 401, nothing written. Every GET route gives the same.
2. **Another attempt's agent, in the same tenant.** The token cannot tell two
   `eng` attempts apart. The key registered for the peer's tuple does, at the
   peer's path and at the attempt's own: 403 on every POST and GET route,
   nothing written. A second `state/open` for the peer's tuple with the
   forger's key is refused, and the peer's key keeps working.
3. **A stale generation after a newer one.** Generation 1 opens. Generation 2
   is leased and opens with its own key. Generation 1's lease is left
   unreleased, so only the fence can refuse it. Every fenced write by
   generation 1 is a 409 with nothing written, while generation 2 still beats
   and finishes. After generation 2 parks, generation 1's finish, retry,
   transition, checkpoint and merge-train register are each
   refused, and the park stands. A mutation that disables `_fenced`'s check
   turns 12 of these 13 tests red; the 13th, re-opening, is refused by the key
   registration independently.
4. **A peer's document or a policy document.**
   * A body naming `document`, `collection`, `tenant_settings`, `metadata`,
     `runner_profile` or `pools` is a 422 on every POST route (strict bodies,
     invariant 10).
   * Every POST route, accepted (200) with its well-formed body, changes only
     documents in `tasks`, `leases`, `attempts`, `pools`, `merge_slots`,
     `merge_train_entries` and `child_keys`. It never changes
     `tenants/*`, `tenant_settings/*`, `workspace_settings/*`, `quota/*`,
     `control/settings`, `repositories/*`, or a peer agent's task document, lease or
     attempt.
   * `state/usage` refuses `state`, `current_generation`, `tenant_id`,
     `attempt_count`, `budget_usd` and `runner_profile` for every kind.
   * A checkpoint URI under a peer agent's prefix is a 422.
   * A park whose detail claims zero child or human resumes refunds nothing
     past the stored bound.

## 5. The cycle over the routes, tested

`tests/unit/worker/test_state_routes_cycle.py` drives the worker's own
`ChildApi`, `StateCall` and `RemoteControlPlane` against swarm-api's real app
(`state_routes_harness`).

* **Checkpoint, park, resume.** Generation 1 checkpoints (digest recorded) and
  parks with uploads. Generation 2 opens, names the attempt it restores, and is
  served that attempt's `checkpoint_sha256` and the carry's uploads, then walks
  to RUNNING.
* **After the resume**, generation 1's checkpoint and finish are
  `FencedWriteRefused` and its heartbeat is refused, with the task document and events
  unchanged.
* **Cancellation.** swarm-api's `cancel_requested` reaches the worker through
  `poll()`. The worker's finish CANCELLED, with `cancel_cause()`, ends the
  agent, releases the lease and writes the `cancelled` event.
* **A human park** counts `human_wait_resumes` on the stored task document, and the next
  generation opens and runs.
* **Publication.** The parked generation's merge-slot acquire is refused with
  no slot written. The current generation takes the slot.

## 6. How a parked attempt resumes today, and what the plugins plan needs

### 6.1 Today

* A park checkpoints `work/` to GCS, records the pointer and digest
  (`state/checkpoint`), writes the carry (`tasks/{t}/carry/{attempt}` and the
  `carry/parks` index, inside `state/park`), releases the lease, and the
  process exits (invariant 4).
* The next attempt is a new lease, a new generation and a new process. It
  opens naming `restore_attempt_id`, is served that attempt's document, and
  restores `latest_checkpoint` only if its archive matches the recorded
  digest.
* **The answers to a question-wait park** come from the TASK document
  (`metadata.question_wait`, written by swarm-api when a person answers),
  served by `state/open`. The worker writes `work/answers.json` whole and
  closes the prompt with an OWNER ANSWERS block between nonce lines
  (`questions.resume_with_answers`). A stale `answers.json` restored from the
  checkpoint is replaced, never trusted.
* **The Claude session is not resumed after a question-wait park.** The agent
  starts again from its prompt in the restored `work/`. The session id
  (`_note_session`, passed to the runner as `SWARM_RESUME_SESSION`, which
  becomes `--resume <id>`) is carried in only three cases:
  1. an account move within the same lease;
  2. an in-place credential reload;
  3. the time-limit continuation, which writes `{continuation, session_id}`
     to a note in `work/` (`_write_continuation_note`), carried by the
     checkpoint and read back after restore (`_read_continuation_note`).

  The session id is registered as a secret, never logged, and never written
  to Firestore.

### 6.2 What the routes must offer for "park mid-interview, resume the same session"

The plan is to park a step mid-interview and resume the same Claude session
with the answer. Three things are needed, and only the first touches the
routes' write set:

1. **The park names what it resumes.** `state/park` with `HUMAN_REQUIRED`
   (question wait) needs an optional `resume` block, stored on the carry
   record: the checkpoint id the session lives in and the question ids it
   waits on. It must not contain the session id. The session's files are in
   `work/.claude`, inside the checkpoint, whose digest is already bound, so
   the id can stay in `work/`, as the time-limit note keeps it. Today
   `ParkBody` is strict, so this is a new field, and swarm-api must check
   that the named checkpoint is the one `state/checkpoint` recorded for this
   attempt.
2. **The resume proves it is that checkpoint.** `state/open` with
   `restore_attempt_id` already serves the digest. The worker must refuse to
   pass `--resume` unless the restored archive is the one the park named. A
   mismatch starts a fresh session, never a guessed one (bounded fallback).
3. **The answer comes from the task document, not the workspace.** It already does
   (§6.1). The resumed session gets the answer as a new user turn built from
   `metadata.question_wait`, under the same nonce-delimited data framing.

Contract impact: none. The park body is swarm-api's own model, and the carry
record is not a frozen type. The resume counter `human_wait_resumes` and its
bound already apply.

## 7. Privilege removal: rollout, rollback and operator actions (a plan; NOT applied)

The Firestore role's removal is already planned step by step in
[isolation-cutover.md](../../runbooks/isolation-cutover.md) (u-bogdan, eng, every
tenant, then `worker_firestore_access = false`, then the definition in
bootstrap). This package adds one prerequisite and leaves GCS and Secret
Manager as they are (§2).

### 7.1 The settings

| Variable (terraform/infra) | Renders as | Default | dev at main |
|---|---|---|---|
| `state_via_api_tenants` | scheduler `SWARM_STATE_VIA_API_TENANTS` | `[]` | `["u-bogdan", "eng"]` |
| `worker_firestore_off_tenants` | swarm-api `WORKER_FIRESTORE_OFF_TENANTS` (job-made workspaces) | `[]` | `["u-bogdan"]` |
| `worker_firestore_off_tenants_iam` | `module.tenancy` drops that tenant's `worker_firestore` binding | `[]` | `["eng"]` |
| `worker_firestore_access` | `module.tenancy`, every tenant | `true` | not set (true) |

Plan-time guards already refuse an `*_off_*` entry that is not in
`state_via_api_tenants`.

### 7.2 Rollout

0. **Prerequisite (new): merge the Jev accounting pull request (§3.2)**, which
   wires `judge/attempt.py` to its Jev routes, and let CI pass. Until then a switched tenant runs no capped Jev judgment and
   records no Jev spend. That is a quality regression, not a security one,
   but it should not spread to every tenant.
1. **dev, the tenants already listed.** Confirm in the dev project that the
   state switch is applied (scheduler revision env) and that attempts of
   `u-bogdan` and `eng` open with `state/open` (swarm-api request logs,
   `state_open`). Run one Jev-enabled step and read its `jev_usage` row.
2. **dev, every remaining tenant**, per the runbook's c1 and c2.
3. **prod**, in the runbook's order, one tenant per apply, each watched as
   the runbook says.

The Terraform the owner applies at step 3 for one tenant, as a plan, never
applied by this lane:

```hcl
# terraform/environments/prod/prod.tfvars (proposed; owner applies)
state_via_api_tenants            = ["<tenant>"]
# after a clean day on the routes:
worker_firestore_off_tenants_iam = ["<tenant>"]
```

```
$ make plan ENV=prod
  # module.tenancy.google_project_iam_member.worker_firestore["<tenant>"] will be destroyed
  # (and the scheduler revision's SWARM_STATE_VIA_API_TENANTS updated in place)
Plan: 0 to add, 1 to change, 1 to destroy.
```

### 7.3 Rollback

* **A route regression for one tenant:** take the tenant out of
  `worker_firestore_off_tenants_iam` first (its binding is created again),
  then out of `state_via_api_tenants`. The scheduler's next dispatch is direct
  again. Running attempts finish on the path they started on.
* **The Jev routes alone:** revert the Jev accounting pull request's
  `judge/attempt.py` wiring. The switched path falls back to "month counted as spent; nothing settled", which is
  today's behaviour.
* **Never roll back by restoring the role while the tenant stays switched.**
  It restores the privilege and buys nothing.

### 7.4 Operator action list

1. Nothing to answer: the owner answered the extra-repository budget (§3.3)
   and the Jev questions (§3.2) on 3 November 2026.
2. After merge, in dev: confirm the state switch and the `eng` role removal
   are applied (the runbook's "What to watch"), and record the date. Until
   then they are "enabled in configuration", not live.
3. Run one capped Jev step for `eng` in dev after the Jev accounting pull
   request merges, and read
   the `jev_usage/eng_<task>_g<n>` row (Admin › Jev usage).
4. Continue the runbook at c1 when (3) holds.
5. The GCS prefix grants and the per-secret accessors stay. No action.

## 8. CONTRACT.md invariants

* **1 and 3** (only leased states cost; concurrency counts from LEASED): no
  route here creates or releases demand.
* **2** (all-or-nothing reservation): untouched. Release still goes through
  `release_lease_in_transaction`.
* **4** (no sleeping through a wait): unchanged. §6.2 keeps park, release and
  exit, and resumes in a new attempt.
* **5** (fencing): every write this package's routes take is fenced. §4.3 tests that a stale generation is refused after a newer one
  opened.
* **6 and 7** (Spot, resources): untouched.
* **8** (checkpointing): untouched. §5 tests that the next generation resumes
  from the recorded digest.
* **9** (per-tenant isolation): every id is derived from the tuple. §4, items 1 and 4,
  test that tenant and policy documents are unreachable.
* **10** (callers name a profile only): the bodies are strict, and a
  `runner_profile` or `pools` key is a 422.
