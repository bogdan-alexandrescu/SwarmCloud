# User-scoped secrets in group tenants: a credential broker in swarm-api

**Status: PROPOSED 2026-10-11, design only (#1041, step 1 of 5).** Nothing
below is built yet. The broker route, the scheduler's nonce, the worker's
client and the IAM removal are steps 2-5 of the plan for #1041. Until they
land, a user slot is read exactly as
[git-tokens.md](../git-tokens.md#built-for-780-per-person-slots-the-migration-policy-retiring-the-tenant-token)
says: directly, by the tenant's worker account.

**The owner's decision, 2026-10-11: secrets are user-scoped, not
tenant-scoped.** A person's credential is used only on the tasks that person
submitted, and no other member's agent can read it, even though every worker
of a group tenant runs as the same service account. Browser logins already
follow this rule: they live only in the person's personal workspace (#1030,
LB-7). This document covers the rest, starting with forge tokens, and reviews
provider keys the same way (§6).

What the design settles, in one line each:

* **A broker, option (a).** swarm-api releases a person's user slot only to
  an attempt that proves it is the worker of a task that person submitted,
  under the task's current lease and generation (§3, §4).
* **Then IAM takes the read away from the worker.** The tenant worker's
  `secretAccessor` on `-git-u-` slots is removed, so it is IAM, and not the
  resolver's good behaviour, that keeps Alice's slot from Bob's attempt (§5).
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
the task as `forge_credential`
(`apps/swarm-api/swarm_api/service.py::SubmissionService._resolve_forge`).
The worker reads the slot that the signed task names
(`apps/agent-worker/agent_worker/secrets.py::resolve_git_token`).

**What is wrong.** `terraform/bootstrap/forge_user_slots.tf`
(`resource "google_project_iam_member" "forge_slot_reader"`) grants the
tenant's worker account `secretAccessor` on every member's base slot, one
prefix-conditioned binding per tenant. That was owner decision D7, option U1
of [git-tokens.md §2](../git-tokens.md#2-secret-manager-naming-and-who-can-read-each-secret):
"a user token is a tenant secret attributed to a person". Every member of
`eng` runs agents as `swarm-agent-worker-eng`. An agent runs as its worker's
uid and can mint its worker's Google token from the metadata server
(`apps/swarm-api/swarm_api/childkey.py`, its module docstring;
[child-tasks.md §3.2](child-tasks.md#32-what-makes-only-the-worker-true-the-attempt-key)).
So an agent in a task Bob submitted can call Secret Manager with that token and
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
route. It releases a slot only to an attempt whose task's server-side
`submitted_by` owns the slot, and the attempt has to prove two things:

* that it is that task's **worker**, not the agent beside it, with the
  attempt key of child-tasks §3.2;
* that it is the **current** attempt (invariant 5).

Then the worker's own IAM read is removed.

What it costs:

* one route;
* one more caller of the key registration that already exists;
* one HTTPS round trip per token read (clone, push, worker action, 401
  re-read);
* swarm-api as a dependency of every user-slot task. It already is one,
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

It fails this issue's acceptance criterion: **a task Alice submits in `eng`
pushes as Alice.** Under (c), Alice's tasks in `eng` would push with the
tenant token or not at all. Under enforcement (`REPOSITORY_GRANTS_ENFORCED`)
that means "not at all", because a person's task never falls back to `git`.
It would also make personal workspaces (#847) a dependency of every group
tenant's pushes.

## 3. The protocol

### 3.1 Which attempts register an attempt key

The attempt key is the one in
[child-tasks.md §3.2](child-tasks.md#32-what-makes-only-the-worker-true-the-attempt-key):

* an Ed25519 key that the worker generates in its heap after
  `make_non_dumpable` reports `PROTECTED`;
* registered by the worker while the task is `STARTING`, with the scheduler's
  one-use nonce (`POST /v1/attempts/{attempt_id}/child-key`), so before the
  agent exists;
* attested by swarm-api under `swarm-child-key`.

**Today only a task that is not a child gets the nonce**
(`apps/scheduler/scheduler/dispatch.py::worker_env`, the CHILD PATH block).
The broker needs a registered key for **every attempt whose signed
`forge_credential` is a user slot, children included**. So the scheduler
issues the nonce when the task is not a child **or** its `forge_credential`
matches `git-u-<16 hex>` (step 3).

A child that gets a nonce only for this reason is told that the key is for
credentials only (`SWARM_CHILD_PATH=false`): its worker registers the key and
offers its agent no child path.

Depth is also refused server-side. `ChildService.submit` refuses a parent that
has a `parent_task_id`
(`apps/swarm-api/swarm_api/children.py::MAX_CHILD_DEPTH`). That check reads
the stored field. A child agent cannot sign a request, because the private key
is in its worker's protected heap, so what keeps depth at 1 is that the key
does not leak. That is the same condition the child path already depends on.

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

The body names **no secret**. swarm-api derives the slot from the task. A
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
3. **The task belongs to the token's tenant (invariant 9).** swarm-api reads
   the task by the body's `task_id`. A missing task, or a task whose
   `tenant_id` is not the token's tenant, is `404 task_not_found`, the same
   answer as for a task that does not exist. Another tenant's task is not
   confirmed to exist.
4. **The tuple is the task's CURRENT attempt (invariant 5).** The task is
   `STARTING` or `RUNNING`. Its lease is this `lease_id`, `attempt_id` and
   `generation`, and the lease is live, read the way the child route fences
   (`apps/swarm-api/swarm_api/children.py::_lease_is_live`). A superseded
   generation, a reclaimed lease or a finished task is `403
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

   Without this check, Bob's agent could rewrite its own task to
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
   `write`, the answer is `403 grant_read_only`. An `access` of `write` on a
   task whose signed `forge_access` is `read` is also `grant_read_only`.
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
  calls Secret Manager for a `git-u-` name. `git` and `git-r-` stay direct
  reads (`apps/agent-worker/agent_worker/secrets.py::resolve_git_token`).
* **It fails closed.** A user-slot task fails with a named error
  (`CredentialMissing` or `SecretError` in
  `apps/agent-worker/agent_worker/secrets.py`) in these cases:
  * no key could be registered (no nonce, registration refused, or the
    `worker_unprotected` tombstone);
  * the broker is unreachable;
  * the broker refuses.

  It **never** falls back to the tenant token. That would run Alice's task as
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
  token swarm-api refreshed mid-task is picked up the same way as today.

## 4. Team credentials: unchanged

These are deliberately **not** user-scoped. Each authenticates as the tenant,
or as an App installation the tenant chose, and the worker keeps reading them
directly with its own account:

| secret | what it is | why it is a team credential |
|---|---|---|
| `swarm-tenant-<tenant>-git` | the tenant token | Service submissions run on it: indexing, schedules, the acceptance suite. With enforcement off, so does an ungranted person's task. git-tokens.md "How to retire the tenant token" is its exit. |
| `swarm-tenant-<tenant>-git-r-<hex>` | a repository token | Registered for one repository of the tenant, not for a person. |
| `swarm-tenant-<tenant>-git-merge`, `-git-review`, `-git-checks` | GitHub App keys | Each has its own sole accessor ([merge-step.md](../merge-step.md)). They sign as the App, never as a member. |
| service submissions | `forge_credential = git` | `_resolve_forge` gives them the tenant token with write, by design (D4). |

## 5. IAM: the worker loses its read of user slots

Once the worker's client and the scheduler's nonce are deployed, the binding
`terraform/bootstrap/forge_user_slots.tf` (`resource "google_project_iam_member" "forge_slot_reader"`)
is removed (step 5). After that, the only reader of a `-git-u-` base slot is
swarm-api (`forge_refresh_reader`, already the reader of the `-refresh`
twins).

**This is what makes the guarantee IAM's.** An agent in Bob's task that
mints its worker's token and calls Secret Manager for Alice's slot gets
`PERMISSION_DENIED`. The same agent calling the broker cannot sign: its
worker's private key is in a heap it cannot read, and Alice's task's key is in
another container. Signing Bob's own tuple gets it Bob's task's checks, which
end at step 5, 7 or 8.

**The rollout order matters.** The IAM removal is applied by the operator's
bootstrap apply only after a release that carries the worker's broker client
and the scheduler's nonce is deployed. Otherwise every user-slot task fails
closed (step 3.3) until that release is deployed. Rolling back the worker
after the removal has the same effect. Re-applying the binding is the undo.

**What it does not stop, stated plainly.** An agent can obtain its own
submitter's token, which is no more than today:

* The worker uses the token on the agent's behalf. An agent can already push
  as its own submitter through its worker. That is the point of the slot.
* The residual of child-tasks §3.2 still applies. If neither a key nor a
  tombstone was registered, an agent that rewrites its own task back to
  `STARTING` could register a key of its own. It still cannot spend another
  attempt's nonce, so the most it gains is its **own** submitter's slot, for
  its **own** task.

Nothing in this design lets one person's agent obtain another person's
credential.

## 6. Provider keys: team or personal

**This classification is a recommendation, awaiting the owner's
confirmation.** The question is in this step's `questions.json`.

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

* **Invariant 5 (fencing).** Check 4 refuses any tuple that is not the task's
  current lease and generation before anything is read, and the worker treats
  `attempt_superseded` as the fence.
* **Invariant 9 (per-tenant isolation).** The ID token names the tenant, the
  task is read only under that tenant, and the secret name is built by the
  frozen `Tenant.secret_name` for that tenant. Within the tenant, this design
  adds per-person isolation, which the invariant never claimed.
* **Invariant 10 (callers choose nothing).** The body names no secret, the
  slot is recomputed from signed fields, and a person cannot call the route.
* **Invariant 1.** Unchanged. A task without its credential fails or parks as
  it does today. Nothing here holds capacity.

## 8. Frozen contract: no change needed

`Task.submitted_by`, `Task.forge_credential` and `Task.forge_access` exist.
Spec format 3 (contract request 54) already signs all three, together with
`repository_url`. `Tenant.secret_name` names the slot. The attempt-key
registration lives in `child_keys`, which only swarm-api writes and which is
not frozen. So `apps/common/swarm_common/` is not touched, and no entry is
added to [contract-change-requests.md](../contract-change-requests.md).

## 9. Build plan

| step | what | files |
|---|---|---|
| 1 | this design; git-tokens.md and multi-tenancy.md point at it | `docs/` |
| 2 | the broker route and its decision function, with checks 1-9 above, including the spec-signature check | `apps/swarm-api/swarm_api/` |
| 3 | the nonce for every user-slot attempt, `SWARM_CHILD_PATH=false` for a child | `apps/scheduler/scheduler/dispatch.py` |
| 4 | the worker reads `git-u-` only through the broker and fails closed | `apps/agent-worker/agent_worker/` |
| 5 | remove `forge_slot_reader`; mark these docs as built | `terraform/bootstrap/`, `docs/` |

The tests that will hold each guarantee:

* Alice's attempt receives Alice's slot.
* Bob's attempt is refused Alice's slot, and Secret Manager is never read.
* A superseded generation is refused before any read.
* The token is in no log line and no header.
* No tenant worker holds `secretAccessor` on a `-git-u-` name.

They are named in steps 2-5 and are written before the code they test.
