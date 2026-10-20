# User-scoped secrets in group tenants: a credential broker in swarm-api

**Status: BUILT, SWITCHED OFF (#1041, steps 1-4).** Designed 2026-10-11:
the broker route, the scheduler's nonce and the worker's client are in the
repository, behind their own switch, `forge_broker` (FORGE_BROKER,
`terraform/infra/child_tasks.tf`), which is **off by default and off in
every environment, dev included**. Dev has had `enable_child_tasks = true`
since 2026-10-11 (for the live browser), so the child key alone is not the
broker's switch: without `forge_broker` the broker would have gone live in
dev on the next deploy. Off, everything is as it was before #1041: the
worker reads a person's slot with its own account through
`forge_slot_reader`, which **stays** in
`terraform/bootstrap/forge_user_slots.tf`. Step 5, removing that binding,
is a later, separate owner step, taken only after `forge_broker` has been on
in dev and a live user-slot run passed (§5, "The rollout order"). Until
then the guarantee below is built, not in force, and today's exposure --
Bob's agent can read Alice's slot with its worker's token -- stands.
§9 names the file and the test that holds each guarantee.

**The owner's decision, 2026-10-11: secrets are user-scoped, not
tenant-scoped.** A person's credential is used only on the agents that person
submitted, and no other member's agent can read it, even though every worker
of a group tenant runs as the same service account. Browser logins already
follow this rule: they live only in the person's personal workspace (#1030,
LB-7). This document covers the rest, starting with forge tokens, and reviews
provider keys the same way (§6).

What the design settles, in one line each:

* **A broker, option (a).** swarm-api releases a person's user slot only to
  an attempt that proves it is the worker of an agent that person submitted,
  under the agent's current lease and generation (§3, §4).
* **Then IAM takes the read away from the worker.** Once the broker has been
  on in an environment and a live run passed, the tenant worker's
  `secretAccessor` on `-git-u-` slots is removed, so it is IAM, and not the
  resolver's good behaviour, that keeps Alice's slot from Bob's attempt (§5).
  Not yet done: the binding stands while `forge_broker` is off.
* **Team credentials stay team credentials.** The tenant token, repository
  tokens, the App keys, service submissions and every provider key in use
  today stay as they are. Which provider credentials are personal is
  recommended in §6, for the owner to confirm.
* **No frozen-contract change.** `Task.submitted_by` and
  `Task.forge_credential` exist, and spec format 3 already signs both (§8).

---

## 1. The defect

**What exists (#780).** A person's forge credential is a user slot,
`swarm-tenant-<tenant>-git-u-<16 hex>`. There are two kinds:

* the App slot, `git-u-` plus 16 hex of sha256 of the lower-cased email
  (`apps/swarm-api/swarm_api/gittokens.py::provider_suffix`);
* a fallback slot per GitHub owner, `git-u-` plus 16 hex of sha256 of
  `email|owner` (`apps/swarm-api/swarm_api/gittokens.py::owner_suffix`).

Submission picks the slot from the authenticated submitter and signs it into
the agent as `forge_credential`
(`apps/swarm-api/swarm_api/service.py::SubmissionService._resolve_forge`).
The worker reads the slot that the signed agent names
(`apps/agent-worker/agent_worker/secrets.py::resolve_git_token`).

**What was wrong.** `terraform/bootstrap/forge_user_slots.tf`
(`resource "google_project_iam_member" "forge_slot_reader"`, to be removed in step 5)
grants the tenant's worker account `secretAccessor` on every member's base
slot, one prefix-conditioned binding per tenant. That was owner decision D7, option U1
of [git-tokens.md §2](../git-tokens.md#2-secret-manager-naming-and-who-can-read-each-secret):
"a user token is a tenant secret attributed to a person". Every member of
`eng` runs agents as `swarm-agent-worker-eng`. An agent runs as its worker's
uid and can mint its worker's Google token from the metadata server
(`apps/swarm-api/swarm_api/childkey.py`, its module docstring;
[child-tasks.md §3.2](child-tasks.md#32-what-makes-only-the-worker-true-the-attempt-key)).
So an agent Bob submitted can call Secret Manager with that token and
read Alice's slot by name. The name is not secret either: it is a hash of
Alice's email.

Only the worker's code chooses the right slot. The 2026-10-11 decision
supersedes D7.

## 2. Three options, and the choice

The issue names three options. **The choice is (a), the broker.**

### (a) A broker in swarm-api: chosen

swarm-api already reads every user slot. It refreshes the App tokens and,
since 2026-10-08, reuses the current access token instead of refreshing per
call (the `forge_refresh_reader` binding in the same file). The broker adds one
route. It releases a slot only to an attempt whose agent's server-side
`submitted_by` owns the slot, and the attempt has to prove two things:

* that it is that agent's **worker**, not the agent beside it, with the
  attempt key of child-agents §3.2;
* that it is the **current** attempt (invariant 5).

Then, once the broker has been on and a live run passed, the worker's own
IAM read is removed (§5).

What it costs:

* one route;
* one more caller of the key registration that already exists;
* one HTTPS round trip per token read (clone, push, worker action, 401
  re-read);
* swarm-api as a dependency of every user-slot agent. It already is one,
  through the 15-minute refresh sweep, without which an App token expires
  within eight hours anyway.

It needs no new identity and no new IAM role.

### (b) Per-person worker identities inside a group tenant: rejected

Each member of each group tenant would get their own worker service account,
the only accessor of their slots. That multiplies, **per person per tenant**:

* service accounts;
* Workload Identity bindings on GKE;
* Cloud Run Job identities. A job's service account is fixed when the job is
  created, so this means one job definition per person per profile and
  resource class, or a job created per dispatch;
* every grant the tenant worker holds today: its bucket prefix, its provider
  keys and its Firestore role.

It still needs a per-attempt identity switch at dispatch, chosen from
`submitted_by`. The dispatcher would then pick which identity runs, from a
document the tenant can write, which is the same trust problem in a new place.
It also does not make Alice's slot safe from Alice's other agents, so it buys
nothing over (a) on that point. Onboarding a member would become a Terraform
apply, against D3's "self-service is the point of #780".

### (c) Personal work only in the personal workspace: rejected

A user slot would exist only in the person's personal tenant, `u-<user>`,
whose worker account is theirs alone (git-tokens.md U2). This is the choice
the owner made for browser logins (#1030, LB-7). It is the right choice for a
login that cannot be scoped to one repository.

It fails this issue's acceptance criterion: **an agent Alice submits in `eng`
pushes as Alice.** Under (c), Alice's agents in `eng` would push with the
tenant token or not at all. Under enforcement (`REPOSITORY_GRANTS_ENFORCED`)
that means "not at all", because a person's agent never falls back to `git`.
It would also make personal workspaces (#847) a dependency of every group
tenant's pushes.

## 3. The protocol

### 3.1 Which attempts register an attempt key

The attempt key is the one in
[child-tasks.md §3.2](child-tasks.md#32-what-makes-only-the-worker-true-the-attempt-key):

* an Ed25519 key that the worker generates in its heap after
  `make_non_dumpable` reports `PROTECTED`;
* registered by the worker while the agent is `STARTING`, with the scheduler's
  one-use nonce (`POST /v1/attempts/{attempt_id}/child-key`), so before the
  agent exists;
* attested by swarm-api under `swarm-child-key`.

**Without the broker only an agent that is not a child gets the nonce**
(`apps/scheduler/scheduler/dispatch.py::worker_env`, the CHILD PATH block).
The broker needs a registered key for **every attempt whose signed
`forge_credential` is a user slot, children included**. So while
`forge_broker` is on, the scheduler issues the nonce when the agent is not a
child **or** its `forge_credential` matches `git-u-<16 hex>` (step 3), and
hands every worker `FORGE_BROKER=true`. Off, it does neither.

A child that gets a nonce only for this reason is told that the key is for
credentials only (`SWARM_CHILD_PATH=false`): its worker registers the key and
offers its agent no child path.

What keeps depth at 1 is the CHILD'S WORKER, from its verified spec, not
swarm-api's read of the stored `parent_task_id`. A worker may update any task
document (`terraform/bootstrap/platform_roles.tf`, `datastore.entities.update`),
so an agent can erase its own agent's `parent_task_id`; swarm-api's check of
that stored field (`ChildService.submit`, `MAX_CHILD_DEPTH`) is a courtesy.
The guard is that `parent_task_id` is inside the spec signature (format 2):
the worker reads it from the verified agent and, when it is set, offers its
agent no child path and submits nothing
(`apps/agent-worker/agent_worker/children.py::ChildPath.prepare`, `is_child`,
and `ChildPath.offered`). A child agent cannot sign a request itself, because
the private key is in its worker's protected heap.

**A child's slot is its parent's SIGNED submitter's, or nothing.** The child
route copies the parent's `submitted_by`, `repository_url`,
`forge_credential` and `forge_access` into the child and signs the child with
swarm-api's own key, and the broker then trusts the child's signature. So
swarm-api verifies the parent's spec signature on the exact document it
builds the child from, and refuses anything else with `403
child_parent_unsigned`, never a switch
(`apps/swarm-api/swarm_api/children.py::ChildService._read_parent`). Before
that check, Bob's agent could rewrite its own agent as Alice's and obtain a
freshly signed child naming her slot, which the broker released (security
review of PR 1070, 2026-10-18;
`tests/unit/control_plane/test_forge_credential_broker.py::test_a_rewritten_parent_gets_no_child_and_no_user_slot`).
The same check makes an erased `parent_task_id` fail as a forged parent
rather than pass as a root.

The nonce derivation does not change. The scheduler restates it, and
`test_the_scheduler_mints_the_nonce_swarm_api_verifies` holds the scheduler's
copy and swarm-api's equal.

### 3.2 The route: `POST /v1/attempts/forge-credential` (worker only)

The worker calls it whenever it needs the token of a user slot: the clone, a
push, a worker action, and the re-read after a 401. Every call is a fresh
release. The worker keeps the value only in its own heap, as it keeps the
token it reads today.

**The request:**

* `POST /v1/attempts/forge-credential`;
* the `Authorization` header carries the worker's Google ID token as a
  bearer credential, with swarm-api as its audience;
* `X-Swarm-Attempt-Proof` carries a base64url Ed25519 signature;
* `X-Swarm-Attempt-Timestamp` carries unix seconds;
* the body is the attempt tuple and the access it needs:

```json
{"tenant_id": "eng", "task_id": "tsk_…", "attempt_id": "att_…",
 "lease_id": "lease_…", "generation": 3, "access": "write"}
```

The signature is over
`apps/swarm-api/swarm_api/childkey.py::request_message`:
`"swarm-child-req/v1\nPOST\n/v1/attempts/forge-credential\n" sha256(body)
"\n" timestamp`.

The body names **no secret**. swarm-api derives the slot from the agent. A
worker cannot ask for a slot by name.

**swarm-api checks, in this order, and reads no secret before the last
step:**

1. **The ID token proves the tenant.** Its email is
   `swarm-agent-worker-<tenant_id>@…` for the body's `tenant_id`,
   `email_verified` is exactly `True`, and the audience is swarm-api. These
   are the same checks as the child route's verifier
   (`apps/swarm-api/swarm_api/routes/children.py::_worker_verifier`). A person
   is refused here: a person has no attempt. Refusal: `403
   forge_credential_unauthenticated`.
2. **The attempt key proves the worker.** swarm-api fetches the registration
   at its derived id
   (`apps/swarm-api/swarm_api/childkey.py::ChildKeys.registration_ids`, under
   the current and previous `swarm-child-key`). It requires:
   * that the registration's attestation holds
     (`apps/swarm-api/swarm_api/childkey.py::ChildKeys.attestation_holds`);
   * that it carries a key, not the `worker_unprotected` tombstone
     (`apps/swarm-api/swarm_api/childkey.py::WORKER_UNPROTECTED`);
   * that the timestamp is within `child_proof_skew_seconds` of swarm-api's
     clock;
   * that the signature verifies
     (`apps/swarm-api/swarm_api/childkey.py::verify_proof`).

   A missing registration, a tombstone, a bad attestation, a stale timestamp
   or a bad signature is `403 forge_credential_unproven`.
3. **The agent belongs to the token's tenant (invariant 9).** swarm-api reads
   the agent by the body's `task_id`. A missing agent, or an agent whose
   `tenant_id` is not the token's tenant, is `404 task_not_found`, the same
   answer as for an agent that does not exist. Another tenant's agent is not
   confirmed to exist.
4. **The tuple is the agent's CURRENT attempt (invariant 5).** The agent is
   `STARTING` or `RUNNING`. Its lease is this `lease_id`, `attempt_id` and
   `generation`, and the lease is live, read the way the child route fences
   (`apps/swarm-api/swarm_api/children.py::_lease_is_live`). A superseded
   generation, a reclaimed lease or a finished agent is `403
   attempt_superseded`. A stale worker receives nothing and touches nothing.
5. **The task's spec signature verifies, at format 3.** A tenant identity can
   rewrite any task document of its tenant, `submitted_by` and
   `forge_credential` included. So swarm-api trusts those two fields, and
   `repository_url`, only under the signature it made at submission
   (`apps/common/swarm_common/specsign.py::canonical_step_spec`, format 3
   covers all three). It verifies with the same public keys the worker
   verifies with (`apps/agent-worker/agent_worker/specverify.py::verify_step_spec`),
   given to swarm-api as configuration. A deployment with no verify keys
   releases no user slot: this check fails closed. An unsigned document, a
   format below 3, or a signature that does not verify is `403
   spec_unverified`.

   Without this check, Bob's agent could rewrite its own agent to
   `submitted_by: alice@…` and `forge_credential: <Alice's slot>`. Its worker
   would then be handed Alice's token at the next push or 401 re-read.
6. **The credential is a user slot.** `forge_credential` matches
   `git-u-<16 hex>`. `git` and `git-r-<hex>` are team credentials, which the
   worker reads directly (§4). Anything else is `403 credential_not_user_slot`.
7. **The slot is the submitter's.** The suffix must equal one of two values:
   * the submitter's App slot,
     `apps/swarm-api/swarm_api/gittokens.py::provider_suffix` of
     `submitted_by`;
   * the submitter's fallback slot for the owner of the signed
     `repository_url`,
     `apps/swarm-api/swarm_api/gittokens.py::owner_suffix` of `submitted_by`
     and that owner.

   Both are recomputed from the signed fields, never read from a document.
   Anything else is `403 credential_not_submitters`.
8. **The submitter's grant still covers the repository.** The `forge_grants`
   document for (tenant, `submitted_by`, the repository's `repo_id`) is read
   by id and checked on its own fields, as
   `apps/swarm-api/swarm_api/service.py::SubmissionService._grant_mode`
   checks it. If there is none, the person lost access since submission:
   `403 grant_removed`. If `access` is `write` and the grant's mode is not
   `write`, the answer is `403 grant_read_only`. An `access` of `write` on an
   agent whose signed `forge_access` is `read` is also `grant_read_only`.
9. **Read and return.** swarm-api reads the latest version of
   `Tenant.secret_name(forge_credential)` under the token's tenant only. It
   uses the reader it already has
   (`apps/swarm-api/swarm_api/forge.py::SecretManagerForgeTokens.read_slot`),
   reused rather than restated. A slot with no enabled version is `403
   credential_unavailable`. This is the case after "Disconnect GitHub" or
   "remove owner", which disable every version.

**The answer:**

A `200` with `Cache-Control: no-store`. Its JSON body has two fields:
`secret`, the slot's name (for example `swarm-tenant-eng-git-u-` and its 16
hex), and `token`, the slot's current value.

Every refusal is a JSON error with a code and a sentence, and **never a
value**. swarm-api logs the decision code, the task id and the secret's
**name**. It never logs the token, puts it in an event, or writes it to
Firestore, a response header or an error message.

### 3.3 What the worker does with it

* **It reads a `git-u-` slot only through the broker**, signed with the key
  its `ChildPath` holds in memory. The key is never serialised. It never
  calls Secret Manager for a `git-u-` name -- while `FORGE_BROKER` is on.
  With it off (the default, and dev's setting although dev hands its
  workers `SWARM_API_URL`) the worker reads the slot with its own account,
  exactly as before #1041, which IAM allows while `forge_slot_reader`
  stands (§5, "The rollout order"). The primary repository and every
  additional repository (#1434) take the same path. `git` and `git-r-` stay direct
  reads (`apps/agent-worker/agent_worker/secrets.py::resolve_git_token`).
* **With the broker on, it fails closed**, a missing `SWARM_API_URL` included. A user-slot agent fails with a named error
  (`CredentialMissing` or `SecretError` in
  `apps/agent-worker/agent_worker/secrets.py`) in these cases:
  * no key could be registered (no nonce, registration refused, or the
    `worker_unprotected` tombstone);
  * the broker is unreachable;
  * the broker refuses.

  It **never** falls back to the tenant token. That would run Alice's agent as
  somebody the submission did not resolve it to, as `forge_suffix`'s
  docstring already says
  (`apps/agent-worker/agent_worker/secrets.py::forge_suffix`).
* **Broker refusals map to the outcomes that exist.** `grant_removed` or
  `grant_read_only` on a write is `ForgeWriteRefused`. `attempt_superseded`
  is the fence (the worker exits without running or continuing the agent,
  and without touching the lease).
* **The value is registered for redaction before it is returned**, and
  reaches git the way today's token does. It is never put in the Job
  environment, argv, the workspace or a log line.
* **The 401 re-read goes through the broker again**
  (`apps/agent-worker/agent_worker/secrets.py::call_reading_again`), so a
  token swarm-api refreshed mid-run is picked up the same way as today.

## 4. Team credentials: unchanged

These are deliberately **not** user-scoped. Each authenticates as the tenant,
or as an App installation the tenant chose, and the worker keeps reading them
directly with its own account:

| secret | what it is | why it is a team credential |
|---|---|---|
| `swarm-tenant-<tenant>-git` | the tenant token | Service submissions run on it: indexing, schedules, the acceptance suite. With enforcement off, so does an ungranted person's agent. git-tokens.md "How to retire the tenant token" is its exit. |
| `swarm-tenant-<tenant>-git-r-<hex>` | a repository token | Registered for one repository of the tenant, not for a person. |
| `swarm-tenant-<tenant>-git-merge`, `-git-review`, `-git-checks` | GitHub App keys | Each has its own sole accessor ([merge-step.md](../merge-step.md)). They sign as the App, never as a member. |
| service submissions | `forge_credential = git` | `_resolve_forge` gives them the tenant token with write, by design (D4). |

## 5. IAM: the worker loses its read of user slots

**Not yet.** The binding `forge_slot_reader` STAYS in
`terraform/bootstrap/forge_user_slots.tf`: the tenant worker's
`secretAccessor` on its tenant's base `-git-u-` slots, never a `-refresh`
twin. While `forge_broker` is off it is how every user-slot agent clones, so
it is removed only as step 5 of the rollout order below -- a separate owner
change and apply, after the broker has been on in dev and a live run passed.
`tests/terraform/forge_app.tftest.hcl`, run
`the_tenant_worker_reads_only_its_tenants_base_user_slots`, holds it no
wider than that, and holds every other `secretAccessor` on a `-git-u-`
condition to swarm-api (`forge_refresh_reader`). The change that removes it
also turns that run into "no tenant worker holds `secretAccessor` on a
`-git-u-` name". Destroying the binding will need no `removed` block; the
one `removed` pair that names `forge_slot_reader` is W9's
(`terraform/bootstrap/removed.tf`), which forgets u-bogdan's personal grant
with `destroy = false` and is unchanged.

A personal tenant's worker (`u-<user>`) still reads its own slots, through
the binding the workspace job makes (docs/workspaces.md, A6). That account is
the person's alone, so the read reaches nobody else's credential.

**This is what makes the guarantee IAM's, once the binding is gone and
the broker is on** (the rollout order below). An agent in Bob's agent that
mints its worker's token and calls Secret Manager for Alice's slot gets
`PERMISSION_DENIED`. The same agent calling the broker cannot sign: its
worker's private key is in a heap it cannot read, and Alice's task's key is in
another container. Signing Bob's own tuple gets it Bob's agent's checks, which
end at step 5, 7 or 8. All of that holds only on the precondition below.

**The precondition this rests on: no other container reads the nonce.**
The attempt key is only as good as its registration, and the registration
is only as good as the one-use nonce the scheduler puts in Alice's
execution's environment (`SWARM_CHILD_NONCE`, §3.1). Alice's own agent
reading it later is harmless: it is spent before that agent exists. But if
an agent in **another** container of the tenant -- Bob's -- can read Alice's
execution overrides (Cloud Run `run.executions.get`) or her pod spec (GKE,
`get pods` in the tenant namespace), it can race Alice's worker in the
window between dispatch and her lifecycle step 2: register a key of its own
for Alice's tuple first, sign Alice's tuple with it, and pass every check of
§3.2, because every check is about **the agent**, and the agent is Alice's.
Alice's worker then gets `409 child_key_taken` and fails closed, too late.
That would hand Bob Alice's slot, which is exactly what this design exists
to prevent.

This is [child-tasks.md §9](child-tasks.md#9-phase-2) item 2's measurement.
**In dev it has been made, and passes** (2026-10-11 ~22:30 PDT, recorded
beside `enable_child_tasks = true` in `terraform/environments/dev/dev.tfvars`:
the tenant pod identity cannot get or list pods, jobs or their logs, and the
tenant worker GSA holds no `run.*` or `container.*` role), which is why dev
has child agents on. **In prod it has not been made**, and `prod.tfvars` does
not set `enable_child_tasks`. Child agents on is still not the broker on: that
is `forge_broker`, a separate switch, off in both. Nothing in this design
weakens the measurement's gate; it adds a second reason to keep it:

* the measurement is a precondition of `enable_child_tasks` (child-agents
  §9 item 2) **and** of the broker, which needs the same key
  (`forge_broker` refuses to plan without `enable_child_tasks`);
* the broker is a precondition of removing `forge_slot_reader` in an
  environment (the rollout order below);
* so in an environment where the measurement has not been made, the
  worker account keeps today's read (U1), and today's exposure -- Bob's
  agent can read Alice's slot with its worker's token -- is stated, not
  hidden. The broker narrows nothing there until it is on.

If the measurement finds the read possible, the nonce cannot travel in the
execution's environment, the broker ships off, and the choice in §2 goes
back to the owner with the measurement (the same stop child-agents §9 item 2
prescribes), rather than shipping the race.

**The rollout order.** Each step a precondition of the next, per
environment:

1. **The release, switched off.** Safe to deploy anywhere, dev included:
   `forge_broker` is off by default and `dev.tfvars` sets it `false`. Off,
   swarm-api's route answers `503 forge_credential_unavailable` before
   reading anything, the scheduler keys no child for its slot and hands no
   worker `FORGE_BROKER`, and the worker reads a person's slot with its own
   account, exactly as before #1041
   (`apps/agent-worker/agent_worker/secrets.py::resolve_git_token`,
   `forgebroker.ForgeBroker.deployed`;
   `tests/unit/worker/test_forge_broker_client.py::test_a_deployment_without_the_broker_reads_the_slot_with_its_own_account`,
   which covers dev's case of child agents on). That read is IAM's to allow
   through `forge_slot_reader`, and Alice's agent in dev
   (`repository_grants_enforced = true`) keeps pushing as Alice.
2. **Child agents on** in that environment, in child_tasks.tf's order: the
   §9 item 2 measurement above; `make apply` with `enable_child_tasks`
   false (the secret and its accessor bindings); `scripts/create-secrets.sh
   --child-key` (the first version -- a revision referencing a secret with
   no version fails to start); then `enable_child_tasks = true` and
   `swarm_api_url` from `terraform output swarm_api_url`, applied. Done in
   dev on 2026-10-11.
3. **The broker on**: `forge_broker = true` in
   `terraform/environments/<env>/<env>.tfvars`, applied -- the owner's
   decision. Every user-slot attempt then gets a nonce and an API address,
   its worker is handed `FORGE_BROKER=true`, and it asks the broker and
   nothing else, failing closed (§3.3). `forge_slot_reader` still stands, so
   turning the switch back off is the undo.
4. **A live run passes** with the broker on in dev: a user-slot agent, and a
   user-slot child of one, clone and push through the broker, and an agent
   another member submitted is refused that slot.
5. **The IAM removal**, a separate owner change: delete `forge_slot_reader`
   from `terraform/bootstrap/forge_user_slots.tf` (and update the forge_app
   run with it), merged and applied by the owner's bootstrap apply. Applied
   before step 3, a worker with the broker off reads its slot directly, gets
   `PERMISSION_DENIED`, and every agent that resolved to a person's slot
   fails closed -- a named `SecretError`, never the tenant token
   (`::test_without_the_broker_a_removed_read_fails_closed_without_the_tenant_token`).
   Turning the broker off, or rolling the worker back, after the removal has
   the same effect; re-adding the binding is the undo.

The operator's step is in [runbooks/github-app.md](../runbooks/github-app.md),
step 2.

**What it does not stop, stated plainly.** An agent can obtain its own
submitter's token, which is no more than today:

* The worker uses the token on the agent's behalf. An agent can already push
  as its own submitter through its worker. That is the point of the slot.
* The residual of child-agents §3.2 still applies. If neither a key nor a
  tombstone was registered, an agent that rewrites its own agent back to
  `STARTING` could register a key of its own. It still cannot spend another
  attempt's nonce, so the most it gains is its **own** submitter's slot, for
  its **own** agent.

Nothing in this design lets one person's agent obtain another person's
credential, once the broker is on and the binding is gone -- and neither is
true anywhere yet: the broker is off in every environment and the binding
stands (step 1 of the rollout order).

## 6. Provider keys: team or personal

**This classification is a recommendation, awaiting the owner's
confirmation.** The owner confirms it on #1041. Nothing built in
steps 2-5 depends on the answer: every provider key below is read exactly as
before.

Every provider credential the platform reads today:

| secret | where it comes from | what it authenticates as | recommendation |
|---|---|---|---|
| `swarm-tenant-<tenant>-<provider>` (`anthropic`, `openai`, …), listed in `Tenant.credentials` | `scripts/register-tenant.sh --add-provider` and `scripts/create-secrets.sh --stdin`; read by `apps/agent-worker/agent_worker/secrets.py::resolve_credentials` | an API key the tenant pays for | **team** |
| `swarm-tenant-<tenant>-<provider>-refresh` | a subscription tenant's refresh twin, refreshed by the quota broker | the subscription registered for that tenant | **team**: it was registered as the tenant's key |
| `swarm-account-<tenant>--<label>` and `-refresh` | the quota broker's account pool ([account-holders.md](../account-holders.md), [quota-management.md](../quota-management.md)), read through `apps/agent-worker/agent_worker/accountlease.py::credential_env_from_account` | a subscription account, which an owner tenant pools and may lend to others | **team**: putting an account in the pool, or lending it, is the act of sharing it |

Why each of these is a team credential:

* A key in a **personal** tenant (`u-<user>`) is already personal by IAM. Its
  only reader is that person's own worker account.
* A key in a **group** tenant was registered by an admin as the group's.
* A **pooled account** is shared by an explicit `lend_to`, which the owner
  tenant's admin sets.

**Personal credentials.** A personal credential is anything that
authenticates as one human and was not deliberately shared. One example is a
member's own Claude subscription token registered for their use alone inside a
group tenant. **None exists today**: no route or script registers a provider
credential per person. If one is ever added, it must be stored under a
per-person name, like the forge slots, and released only through this broker,
with the same checks. That means the same attempt key, the current
generation, and the submitter owning the slot. The tenant worker must not hold
IAM on it. Browser logins are personal and are already confined to the
person's personal workspace (#1030).

## 7. Invariants this preserves

* **Invariant 5 (fencing).** Check 4 refuses any tuple that is not the agent's
  current lease and generation before anything is read, and the worker treats
  `attempt_superseded` as the fence.
* **Invariant 9 (per-tenant isolation).** The ID token names the tenant, the
  agent is read only under that tenant, and the secret name is built by the
  frozen `Tenant.secret_name` for that tenant. Within the tenant, this design
  adds per-person isolation, which the invariant never claimed.
* **Invariant 10 (callers choose nothing).** The body names no secret, the
  slot is recomputed from signed fields, and a person cannot call the route.
* **Invariant 1.** Unchanged. An agent without its credential fails or parks as
  it does today. Nothing here holds capacity.

## 8. Frozen contract: no change needed

`Task.submitted_by`, `Task.forge_credential` and `Task.forge_access` exist.
Spec format 3 (contract request 54) already signs all three, together with
`repository_url`. `Tenant.secret_name` names the slot. The attempt-key
registration lives in `child_keys`, which only swarm-api writes and which is
not frozen. So `apps/common/swarm_common/` is not touched, and no entry is
added to [contract-change-requests.md](../contract-change-requests.md).

## 9. As built: where each guarantee lives

| step | what | files |
|---|---|---|
| 1 | this design; git-tokens.md and multi-tenancy.md point at it | `docs/` |
| 2 | the broker route and its decision function, checks 1-9 above | `apps/swarm-api/swarm_api/credbroker.py`, `apps/swarm-api/swarm_api/routes/credentials.py` |
| 3 | the nonce for every user-slot attempt, `SWARM_CHILD_PATH=false` for a child | `apps/scheduler/scheduler/dispatch.py` (`worker_env`) |
| 4 | the worker reads `git-u-` only through the broker and fails closed | `apps/agent-worker/agent_worker/forgebroker.py`, `apps/agent-worker/agent_worker/secrets.py` (`resolve_git_token`) |
| 4a | the off switch, `forge_broker` (FORGE_BROKER), off by default and in dev; a child is built only from a parent whose signature verifies | `terraform/infra/child_tasks.tf`, `apps/swarm-api/swarm_api/children.py` (`_read_parent`) |
| 5 | `forge_slot_reader` removed -- NOT DONE: a later owner step, after the broker has been on in dev and a live run passed | `terraform/bootstrap/forge_user_slots.tf` |

The tests that hold each guarantee:

| guarantee | test |
|---|---|
| Alice's attempt receives Alice's slot | `tests/unit/control_plane/test_forge_credential_broker.py::test_alices_attempt_receives_alices_slot`, and through the emulator `tests/integration/test_forge_credential_broker.py::test_alices_attempt_receives_alices_slot_through_the_emulator` |
| Bob's attempt is refused Alice's slot, and Secret Manager is never read | `tests/unit/control_plane/test_forge_credential_broker.py::test_bobs_attempt_is_refused_alices_slot_and_reads_no_secret`, `::test_bobs_agent_rewriting_the_submitter_fails_the_spec_check`, `::test_bob_cannot_sign_alices_tuple_with_his_own_key`; `tests/integration/test_forge_credential_broker.py::test_bobs_attempt_is_refused_alices_slot_through_the_emulator` |
| a superseded generation is refused before any read | `tests/unit/control_plane/test_forge_credential_broker.py::test_a_superseded_generation_is_refused_before_any_read`; the worker fences on it, `tests/unit/worker/test_forge_broker_client.py::test_a_superseded_attempt_is_fenced` |
| the token is in no log line, no header and no environment | `tests/unit/control_plane/test_forge_credential_broker.py::test_the_token_is_in_no_log_and_no_header`, `tests/unit/worker/test_forge_broker_client.py::test_the_token_is_in_no_env_and_no_log` |
| every user-slot attempt gets a nonce, a child one with no child path | `tests/unit/scheduler/test_dispatch_forge_nonce.py::test_a_child_with_a_user_slot_gets_a_nonce_and_no_child_path` |
| the worker never reads a user slot from Secret Manager, and fails closed without the tenant token | `tests/unit/worker/test_forge_broker_client.py::test_a_user_slot_is_never_read_from_secret_manager`, `::test_a_refused_attempt_fails_closed_without_the_tenant_token`, `::test_an_attempt_without_a_key_fails_closed_before_asking` |
| with FORGE_BROKER off (dev's child agents on included) the worker reads the slot with its own account, and fails closed without the tenant token once IAM refuses | `tests/unit/worker/test_forge_broker_client.py::test_a_deployment_without_the_broker_reads_the_slot_with_its_own_account`, `::test_without_the_broker_a_removed_read_fails_closed_without_the_tenant_token` |
| the switch ships off: the route, the scheduler and terraform | `tests/unit/control_plane/test_forge_credential_broker.py::test_the_broker_ships_off_and_releases_nothing_while_off`, `tests/unit/scheduler/test_dispatch_forge_nonce.py::test_with_the_broker_off_dispatch_is_what_it_was_before_it`, `tests/terraform/child_tasks_infra.tftest.hcl`, runs `dev_leaves_the_forge_broker_off` and `the_forge_broker_needs_child_tasks` |
| a parent rewritten after signing gets no child, and no user slot is released | `tests/unit/control_plane/test_forge_credential_broker.py::test_a_rewritten_parent_gets_no_child_and_no_user_slot`, with its control `::test_a_child_of_bobs_signed_task_receives_bobs_slot` |
| an additional repository's user slot comes from the broker | `tests/unit/worker/test_forge_broker_client.py::test_an_additional_repositorys_user_slot_comes_from_the_broker` |
| until step 5, the worker reads only its tenant's base user slots | `tests/terraform/forge_app.tftest.hcl`, run `the_tenant_worker_reads_only_its_tenants_base_user_slots` |

What the tests do not prove: that no other container of a tenant can read
an attempt's `SWARM_CHILD_NONCE` (§5, "The precondition this rests on";
measured in dev on 2026-10-11, not in prod), on which the whole guarantee
rests; that the broker works live (step 4 of the rollout order); and that
IAM answers `PERMISSION_DENIED` live once the binding is destroyed. That is the Policy Troubleshooter check in
[runbooks/github-app.md](../runbooks/github-app.md), step 8, after the
owner's bootstrap apply.
