# The `merge` step: a workflow that merges its own pull request

**Status: PROPOSED, 2026-09-29, for #295.** Nothing described here is built.
This is the design the owner asked for before any code: how a SwarmCloud
workflow ends in a squash merge made by the **worker**, with a credential no
agent can reach. The frozen-contract half is contract request 33 in
[contract-change-requests.md](contract-change-requests.md); the example spec is
in [workflows.md](workflows.md#proposed-a-chain-that-merges-its-own-pull-request).

**Revised 2026-09-29, four times.** Round 1: against a security review's two
blockers and five majors. Round 2, same day: B1 was found not yet closed by
round 1, and corrected. Round 3, same day: B1 was found **still** open
against a different attack, closed further, and three owner decisions
resolved what round 2 had left open. **Round 4, same day, a joint review with
CR 34 (#344, signed step specs):** the round-3 staging mechanism was found to
route `post-verdict`'s one critical read through a tenant-writable pointer
after all, the real bucket layout and IAM primitives were corrected, the
cross-workflow verdict-forgery claim was found to be still open rather than
closed, and six smaller mismatches with CR 34 were reconciled. Still nothing
here is built, and the status stays PROPOSED. What changed, in one line each,
with the section that carries the detail:

* **B1, corrected four times, and still not fully closed — round 4 found the
  round-3 fix still routed through an attacker-writable pointer, and found
  the "closes cross-workflow unconditionally" claim was false.** Round 1: the
  verdict became a GitHub PR review from a `swarm-review` App instead of a
  control-plane attestation, held by a dedicated
  `claude-code-review`/`claude-code-proof` profile. **Round 2: found
  insufficient** — the review agent reads attacker-controlled diffs and the
  proof agent executes the pull request's own code, so either one, sharing a
  container with the App key regardless of profile, could read the key and
  carry it off. Fix: a new no-agent worker-action profile, `post-verdict`,
  holds `-git-review` and posts the review after `review` writes
  `review.json` and before `fix` can run; no proof anchor any more (§4.3,
  §11 Q6). **Round 3: found to close only half the forgery surface, and to
  miss a different attack entirely.** `post-verdict`'s DAG-ordering closes
  same-workflow forgery of `review.json` but not a **different** workflow's
  agent racing the shared tenant account against no ordering at all — closed,
  round 3 claimed, by a **review-only-writable prefix**, written under
  `review`'s own identity (a `claude-code-review` profile after all, but
  holding no App key, only a GCS write grant — safe for a different reason
  than round 2 rejected) and read by `post-verdict`. Separately, and more
  fundamentally: **nothing above protects the *instructions* a step acts
  on**, only the *artifacts* it produces. Any tenant identity can rewrite a
  still-`PARKED` step's `prompt` before that step starts, and an honest agent
  following corrupted instructions produces an honest, unstoppable forgery.
  **Owner decision: signed step specs, S0 issue #342. This design depends on
  it, and B1 is closed only once #342 ships** (§0 consequence 4, §7 T14).
  **Round 4, joint review with CR 34: two more corrections, not just one
  more mechanism.** First, `post-verdict`'s read of `review.json` was still
  declared as ordinary `input_from`, which resolves through the upstream
  task's tenant-writable `result_summary` — exactly the kind of
  attacker-writable pointer B1 exists to route around. Fixed:
  `post-verdict` computes the object's GCS path itself from its own signed
  spec (#342) and reads it directly, never through the generic staging
  resolver (§4.1, §4.3, §6a). Second, round 3's claim that the prefix
  "closes cross-workflow forgery unconditionally" was false: the owner did
  not choose to bind a review to one pull request or task id, the review
  identity is shared tenant-wide, and its write grant covers every
  workflow's verdict path at once — recorded now as a **named residual, R8**
  (§7), with write-once (`ifGenerationMatch=0`) noted as a partial
  mitigation, not a close.
* **B2.** The merge action is pinned to a single forge host — `api.github.com`,
  or a per-tenant host the tenant cannot write — and follows no redirect on a
  credentialed request. `owner/repo` come from a record no tenant identity
  can write, never from the task document. **Round 3 resolved where: the
  owner chose Terraform-rendered `post-verdict`/`merge` Job environment
  values**, not the merge secret's own JSON (§2.1b).
* **M1.** `main` is now protected by ruleset `main-protection` (§0.3, §8).
* **M2.** Restricting who may use the PR-merge route needs a **second**
  ruleset, separate from `main-protection`, whose bypass list names the Apps —
  and that naming is unverified on a personal repository, so a dry run comes
  first (§7 Q3, §8).
* **M3.** The owner keeps `-git`'s PAT for agent pushes. An agent labelling its
  own pull request `ready` is an accepted risk, not a closed one (§7 T13, §8).
* **M4.** Merging to `main` is code execution as the deployer, because
  `release.yml`'s `id-token: write` sits at workflow level and its verify job
  runs the merged tests. The chain refuses a pull request that touches a wider
  set of paths than `.github/workflows/` alone (§5.1, §6, §7 T6). **Round 3
  resolved whether moving that permission to job level is a precondition:
  the owner decided it is not** — recorded as an accepted residual, R4 (§7,
  §11 open question (b)).
* **M5.** A required check with no `app_id` is refused, never satisfied by a
  legacy commit status (§5.2, §6).
* Minors, round 1–2: worker-action profiles skip checkpoint restore (§1.3);
  the base ref is re-checked immediately before and again after the merge
  call (§5.1, §5.3, §9); the forge client never auto-follows a redirect
  (§2.2, §5); the `pulls/{n}/files` read is paginated to GitHub's own
  3000-file cap and refuses past it (§5.1, §6); no tenant identity holds
  `run.jobs.run`, `run.jobs.runWithOverrides` or `run.jobs.update` on the
  merge Job, which gets a `terraform test` assertion (§1.3, §10); each
  tenant gets its own review and merge Apps — never one App shared across
  tenants (§1.3, §2.1, invariant 9); `review_app_bot_id` is pinned at
  registration with the review App's own JWT, never re-resolved live
  (§5.2a); and required checks are read through
  `GET .../rules/branches/{branch}`, GitHub's combined view, rather than
  separate ruleset and classic-protection calls (§5.2). **Corrected, round
  4/joint review: the merge account's Firestore role is NOT narrowed — GCP
  Firestore has no document- or field-level server-side IAM, so "a narrowed
  Firestore role" was never buildable (§1.3).**
* Minors, round 3: `post-verdict`'s own end causes are pinned —
  `VERDICT_REFUSED`/`VERDICT_FAILED` (§6a) — rather than left implicit; and
  contract request 33 now points to the not-yet-filed `post-verdict` and
  `claude-code-review` requests instead of staying silent about them
  ([contract-change-requests.md](contract-change-requests.md) #33).
* Minors, round 4, joint review with CR 34: the real bucket layout is a
  shared bucket under `tenants/<t>/`, not a per-tenant bucket (§4.3, §10);
  GCS IAM is allow-only, so the review-only-writable prefix is two
  conditioned bindings replacing `register-tenant.sh:961`'s one, not a deny
  rule (§4.3, §10); overwriting the review write needs
  `storage.objects.delete`, for a legitimate retry under write-once
  semantics (§4.3); CR 33 keeps its own `*_REFUSED`/`*_FAILED` vocabulary —
  the two designs' end causes are not unified — so an upstream spec failure
  reports as `{"code": "spec_unverified", "message":
  "upstream:<task id>:<why>"}`, matching CR 34's own format (§6); the checkpoint-restore
  fix this design needs (no restore on `review`/`post-verdict`/`merge`, no
  attempt-1 restore for any profile) is inherited from S0 #347, fixed
  separately, not restated here as this design's own fix (§1.3); the
  ordinary worker account's real name is `swarm-agent-worker-<tenant>`, not
  `swarm-<tenant>` (throughout); `merge` verifies the **union** of upstream
  signed specs — `author`, `review`, `post-verdict`, `fix`, `proof` — not
  only the ones named in its own `input_from` (§4.2, §6 row 42); and rows 42
  and 6a-5 no longer phrase "#342 not built" as a runtime-checkable
  condition — it is a precondition for the feature existing at all, not
  something a worker asks at request time (§6, §6a).

The owner's requirements (2026-09-29, #295), which every section below answers:

1. The chain is **implement → review → fix → proof → merge**. The merge is a
   worker action taken after every agent process is reaped. No agent makes it.
2. **Squash**, with the pull request's fact-style title as the subject, and
   `sha` pinned to the head the review and the proof ran on.
3. The merge credential is its **own** Secret Manager secret: either
   `swarm-tenant-<tenant>-git-merge` or a GitHub App installation token the
   worker mints. It is stored with `scripts/create-secrets.sh --stdin`, read
   only at merge time, and never put in the workspace, a file, an agent's
   environment or a log (#219).
4. Merge only when **every required check is green at that head** and the
   review's verdict is **MERGE**. Otherwise the step fails and says why.
5. The pull request records **which task merged it**.

---

## 0. The constraint that shapes everything else

Read from source on 2026-09-29. This was not tested live.

**Any agent can act as its tenant's service account.** An agent runs in the
worker's container as the worker's uid. Nothing between it and
`169.254.169.254` is filtered: `agent_worker` sets no metadata override for the
child, and a Cloud Run container has no network policy of its own. So it can
mint a token for the tenant's workload identity.
[security.md](security.md#cloud-metadata-abuse) concedes this and argues that
the boundary is what that identity can do. For this design, that identity can
do three things that matter:

* **Read `swarm-tenant-<tenant>-git`.** The tenant's service account is that
  secret's accessor (`register-tenant.sh --add-provider git`). #219 keeps the
  token out of the agent's environment, files and argv, and out of the
  worker's heap (`PR_SET_DUMPABLE`). None of that stops an agent from asking
  Secret Manager for the token directly.
* **Write any Firestore document whose id it knows**
  ([multi-tenancy.md](multi-tenancy.md), "It CAN read and write a document
  whose id it can guess"). A step learns its workflow's id from its own task
  document, and the workflow document lists every step's task id.
* **Write objects under the tenant's GCS prefix**, which is where every
  artifact that `input_from` stages comes from.

Three consequences follow, and the design does not pretend otherwise:

1. **The merge credential cannot be held by the tenant's service account.** If
   it were, then "a separate secret" would be a separate name for the same
   exposure. §2 gives it a different identity, one that no agent ever runs as.
2. **Anything produced under the tenant identity is a claim, not a fact.** This
   covers the pull request number, the pinned sha, the verdict and the proof,
   whether they come from Firestore or from a staged artifact. The merge worker
   checks each claim against GitHub (§4, §5). Where GitHub itself cannot be
   asked to confirm a claim from the artifact alone — the verdict — the claim
   is anchored to an object only a no-agent worker-action step's own identity
   can create: a PR review, posted by `post-verdict` (§4.3, B1). The proof
   stays a plain staged-artifact claim, checked by sha equality alone — §4.3
   explains why anchoring it would not be worth building.
3. **`main` is now protected, and this design assumes that protection stays
   in place.** Ruleset `main-protection` (id `24160219`), created 2026-09-29,
   requires a pull request and the four `security.yml` checks, blocks
   force-push and branch deletion, and lets an administrator bypass **only by
   merging a pull request** — there is no direct-push bypass any more, which
   is what `enforce_admins: false` used to allow (§7 T12, §11 Q3 correct this
   against the ruleset; §8 restates coexistence against it). An always-run
   `ci-gate` job is coming, which will let the required-checks rule cover every
   pull request rather than only the four checks that report on every one; this
   design does not depend on it and is not blocked by its absence. **This
   protection is a precondition for enabling the merge step at all** (§10):
   without it, `eng`'s `-git` secret — the owner's classic PAT
   ([merge-strategy-live-proof.md](merge-strategy-live-proof.md) §3) — could
   still merge past every check the way an administrator's direct push once
   could, and an agent that reads that secret (§0, this section) inherits
   that. With the ruleset in place, that same agent can still *open* a merge
   via the API, but GitHub itself refuses it unless the required checks are
   green and the PR route is used — the ruleset is what closes the gap, not
   this design. What the ruleset does **not** by itself decide is *who* may
   use the PR-merge route at all: today that is still "anyone with write
   access to the repository," which includes any identity holding `-git`.
   §7 T12/R5 and §11 Q3 track that residual exposure and its own follow-on
   ruleset (M2/§8).
4. **A step's own instructions are also a claim, not a fact — and today
   nothing checks that (BLOCKER, round-3 re-review, 2026-09-29).** Every
   worker reads its step's `prompt`, `input` and `input_from` from its task
   document in Firestore before running an agent or a worker action. §0's own
   second bullet says a tenant identity can write any Firestore document
   whose id it knows, and a step learns every sibling step's task id from the
   workflow document. So an `implement` agent — the very first thing to run —
   can rewrite the still-`PARKED` `review` task's `prompt` before `review`
   ever starts, and the review agent would then honestly follow corrupted
   instructions and reach a genuine, unforced `MERGE` verdict about a change
   it was never actually asked to judge. **This reopens B1**: `post-verdict`'s
   isolation (§4.3) protects the *object* the verdict is anchored to from
   forgery after the fact; it does nothing about the *instructions* the
   review agent acted on in good faith. **Owner decision, 2026-09-29: signed
   step specs, filed as S0 issue #342.** swarm-api signs each step's
   canonical spec with a KMS key only swarm-api holds, at submission; every
   worker verifies that signature before starting any agent or worker action,
   and a step whose live Firestore document disagrees with its signed spec
   refuses rather than runs. `post-verdict` and `merge` additionally verify
   the **upstream** specs the artifacts they trust were produced under — not
   only their own — so a tampered `review` spec is caught even though
   `post-verdict` never executes it itself. **This design depends on #342.
   B1 is closed only once #342 is built** (§7 T14, §10, §11).

---

## 1. How a merge step is expressed

There were three candidates. They are compared against the frozen contract
because the contract decides which one is possible.

### 1.1 A strategy (`metadata.dispatch.strategy: "merge"`)

A strategy says what happens to the work **after** the agent has produced it.
`direct-pr` and `integrate` are both implemented inside the publishing step's
own worker, after its agent is reaped (`lifecycle._publish_git`). A merging
strategy would do the same: the proof step's worker would merge after the proof
agent exited.

**Rejected, because of §0.** That worker runs as the tenant's service account,
and an agent has just run in the same container under that identity. To read
the merge credential, the worker's service account must be an accessor of it.
Then every agent of the tenant is also an accessor. A strategy also has no
attempt of its own. The merge could not fail with its own end cause, be retried
by itself, or be cancelled separately from the proof it would be part of.

### 1.2 A stage (a platform-inserted step kind, e.g. `WorkflowStep.kind: "merge"`)

The scheduler, or a control-plane service, would perform the merge when the
workflow's other steps succeeded, with no task and no worker.

**Rejected.** It needs a new field on the frozen `WorkflowStep` and on `Task`.
It needs a second execution path in the scheduler for a step that is not a
task: no lease, no fencing generation (invariant 5), no attempt, no end cause.
The worst problem is that it puts a tenant's merge credential in a shared
control-plane process that serves every tenant. That makes a tenant's key
reachable from outside the tenant, which invariant 9 forbids.

### 1.3 A runner profile (`runner_profile: "merge"`) — CHOSEN

A merge step is an ordinary task whose profile names **no runner**. The worker
performs the merge itself. Everything true of a task stays true:

* admission holds it to the same pools and the same all-or-nothing transaction
  (invariants 1–3). Until its parents succeed it is `PARKED(DEPENDENCY_INCOMPLETE)`
  and costs nothing;
* it carries a fencing generation, so a stale merge worker exits without
  touching the forge (invariant 5);
* it has attempts, a timeout, a cancel, and a typed `end_cause`;
* a caller names it **by name** and sends no image, command or parameter
  (invariant 10). Its declared inputs are empty, so its `input` is `{}`.

The reason it is the only one of the three that works is the contract's own
platform decision: *"Cloud Run sets the service account on the JOB resource and
it cannot be overridden per execution, so the dispatcher creates
per-tenant-per-profile Job resources."* **The profile is the one place where a
step's identity can differ from its tenant's other steps.** A `merge` profile
gets the Job `swarm-job-<tenant>-merge`. That Job can run as a separate service
account, `swarm-<tenant>-merge`, which is the only accessor of the merge
credential. No agent ever runs as that account, because the profile starts no
agent.

What this needs from the frozen contract, requested in
[contract request 33](contract-change-requests.md#33-profilespy--modelspy-a-merge-profile-that-runs-no-agent-and-two-end-causes-for-it):

* `RunnerProfile.worker_action`, a new field. When set, the lifecycle runs that
  platform action **instead of** a runner child, and `runner_argv` must be
  empty. It is a typed field and not a check for `profile.name == "merge"`
  because a behaviour keyed on a name gets restated in each place that needs
  it, and this repository has been bitten by that repeatedly
  (`retries_exhausted`, the exit codes).
* The catalogue entry `merge`: `image="agent-runtime-base"`,
  `resource_class="standard"`, `backend=CLOUD_RUN_JOB`, `runner_argv=()`,
  `provider="git-merge"`, `secrets=()`, `inputs={}`, `timeout_seconds=600`.
  * `provider="git-merge"` is what already parks a task
    `CREDENTIAL_MISSING`, at no cost, for a tenant that has not registered
    the credential. It is also what keeps Terraform's `job_matrix` from
    creating a merge Job for that tenant.
  * `secrets=()` because the credential is **never mounted**. Terraform's
    `secret_env` for this profile is empty, exactly as it is for `git`.
  * `standard` is the smallest class that exists: 1 unit, held for well under
    a minute. A smaller class would be a separate request.
  * Checkpointing stays on (`supports_checkpoint=True`, invariant 8). The step
    has no work product, so its checkpoint is an empty workspace, which costs
    one small upload. **A `worker_action` profile skips checkpoint *restore*
    on a retried attempt**, though: restore exists to put a runner's workspace
    back the way an agent left it, and no runner ever starts under this
    profile, so there is nothing for a restore to populate. The lifecycle
    branches on `worker_action` the same way it does for the argv (below)
    before deciding whether to restore. What actually makes an interrupted
    merge survivable is that it is idempotent (§6, "the worker died after the
    merge"), not the checkpoint. **This design inherits, rather than
    reinvents, a platform-wide restore fix tracked separately as S0 #347
    (round-3/joint-review note, 2026-09-29): no attempt-1 restore for *any*
    profile, since attempt 1 has nothing to restore from, and no restore at
    all on `review`, `post-verdict` or `merge` specifically** — `merge` and
    `post-verdict` for the reason just given (no runner, nothing to restore);
    `review` because it must never resume an agent inside a workspace a
    previous, possibly-compromised attempt left behind — its whole judgement
    depends on seeing the checked-out head honestly, and a carried-over
    workspace is exactly the kind of tampering vector §0's own threat model
    already takes seriously elsewhere. #347 is being fixed separately, not by
    this design; this section states what this design needs from it rather
    than restating its fix.
* `EndCause.MERGE_REFUSED` and `EndCause.MERGE_FAILED` (§6).

What this needs **outside** the frozen contract (none of it is built; each is
its own change, listed in §10):

* a service account per tenant for the merge Job (Terraform, labelled
  `managed-by=swarm-terraform`). **Its Firestore access is the SAME
  `roles/datastore.user`-class, database-scoped grant every worker Job
  already gets — corrected, joint review with CR 34, 2026-09-29: "a narrowed
  Firestore role" is not buildable.** GCP Firestore has no document-level or
  field-level server-side IAM for a service account using the Admin/server
  client libraries; `roles/datastore.user` (or an equivalent custom role) is
  granted at the project/database level, full stop, the same limitation §0
  already leans on to explain why any tenant identity can write any
  document whose id it knows. An earlier draft of this bullet claimed "read
  on its own task, attempt and workflow documents, and write only to its own
  task's `result_summary` and `control.finish` fields" as an IAM-enforced
  boundary — it was not one, and could not have been built as described.
  What actually isolates the merge account from the rest of the tenant's
  Firestore reach is nothing at the IAM layer: it is that the merge worker's
  own code only ever reads and writes the documents its logic names, the
  same convention-not-enforcement gap that already applies to every other
  Firestore access in this design (§0). The merge account's *real*
  isolation is elsewhere: the GCS read of the tenant's prefix, and accessor
  on `swarm-tenant-<tenant>-git-merge` **alone**. Nothing grants the
  tenant's worker account `iam.serviceAccountTokenCreator` or `actAs` on it.
  **No tenant identity — not the tenant's worker account, not another
  tenant's merge account — holds `run.jobs.run`, `run.jobs.runWithOverrides`
  or `run.jobs.update` on the merge Job itself.** Only the platform's own
  deploy identity does; a tenant that could invoke or override another
  tenant's merge Job would defeat the whole point of a separate identity.
  This gets a `terraform test` assertion (§10) the way the destroy-guard and
  plan-guard already do, so a future grant that widens it fails CI rather
  than being noticed in an audit;
* `register-tenant.sh --add-provider git-merge` must **refuse**. Its job is to
  bind the tenant's worker account, and for this one provider that binding is
  the hole §0 describes;
* **one review App and one merge App per tenant — never one App shared
  across tenants** (proof has no App; §4.3 says why). Invariant 9 is
  per-tenant isolation: own GSA, own secrets, own GCS prefix, own namespace. A
  shared review or merge App would put every tenant's verdict-signing or
  merge-signing key behind one installation, so a compromise of one tenant's
  Secret Manager access (§0) would let it forge or merge for every other
  tenant too. `swarm-tenant-<tenant>-git-review` and
  `swarm-tenant-<tenant>-git-merge` are two distinct secrets per tenant, each
  readable by exactly the identity that needs it and no other (§2.1, §4.3);
* **`review` runs on its own profile too — `claude-code-review` — but ONLY
  for a GCS write grant, never for an App key (round-3 correction,
  2026-09-29; do not conflate with the rejected bullet immediately below).**
  Its Job's service account, `swarm-<tenant>-review`, is the only identity
  with write access to the review-only-writable prefix (§4.3) where
  `review.json` lands. This is safe for exactly the reason the App-key
  version was not: a scoped GCS write grant is not a portable secret an agent
  can exfiltrate and reuse outside its own container, so the review agent
  running there — reading attacker-controlled diffs — gains no new attack
  surface by running as this identity instead of the ordinary tenant one;
* **a `claude-code-review`/`claude-code-proof` profile *for holding an App
  key* is NOT the fix, and this design does not propose that — a second
  review corrected this (2026-09-29).** A dedicated profile only changes
  *which* Job and service account a step's **agent** runs under; it does
  nothing about the fact that the agent still runs in the same container an
  App key would sit in. The review agent **reads attacker-controlled diffs**,
  so it is a prompt-injection target; the proof agent **executes the pull
  request's own code**, a direct path to the metadata server (§0: "nothing
  between it and `169.254.169.254` is filtered"). Either one, on its own
  profile or not, could read a PEM sitting in its container's Secret Manager
  grants and carry it off — a dedicated profile would have stopped a
  *different* step's agent from reading `-git-review` (§1.1's argument,
  restated) but does nothing about the review step's **own** agent doing it,
  which is exactly the agent B1 needs to keep the key away from. **The only
  place an
  App key may sit is a worker-action step that runs no agent and executes no
  pull-request code at all — the same reason `merge` itself is a profile and
  not a strategy (§1.1).** §4.3 gives the corrected design: a new
  `post-verdict` worker-action profile, the sole accessor of `-git-review`,
  that runs after the review agent and before `fix`. There is no
  `post-proof` counterpart, and no `-git-proof` secret — §4.3 says why;
* the chain's single-pull-request roles (§3).

---

## 2. The credential's life

### 2.1 Which credential — recommendation: a GitHub App the worker mints a token from

| | fine-grained PAT in `-git-merge` | GitHub App key in `-git-merge`, installation token minted per merge |
|---|---|---|
| lifetime of what the worker holds | the PAT itself, for as long as it is valid (months) | a token that expires in one hour and is revoked when the merge ends |
| scope per use | whatever the PAT has | `repositories=[<this repo>]`, `permissions={contents: write, pull_requests: write}` on each mint |
| attribution on GitHub | the PAT owner, the same person as `-git` | the App's bot account, **distinct from** the identity every agent can read |
| starts `push` workflows on `main` | yes | yes (only the `GITHUB_TOKEN` does not; see `auto-merge.yml`'s header) |
| what a leak costs | a merge-capable token until someone rotates it | the private key. The App is installed on the tenant's repositories only and has no `workflows` permission |

A distinct bot identity is what makes Q3 possible: a rule that says only the
merge App (and `auto-merge.yml`'s App) may merge into `main`. A PAT belonging to
the same person as `-git` cannot be told apart from `-git` by any rule.

The App is **not** the `auto-merge.yml` App. That App holds `workflows: write`
([ci.md](ci.md)), because a human's `ready` can reasonably land a workflow
change. An unattended chain should not be able to (§7, T6). The secret holds
one JSON document, `{"app_id": <int>, "private_key": "<PEM>"}`. The installation
id is not stored: the worker reads it with `GET /repos/{owner}/{repo}/installation`,
which also proves the App is installed on **this** repository.

Stored exactly as the owner specified, and never by Terraform:

```bash
scripts/create-secrets.sh --tenant eng --provider git-merge --stdin < merge-app.json
```

The accessor binding goes to `swarm-eng-merge` and never to the tenant's
worker account (§1.3).

### 2.1b The host, and `owner`/`repo` — B2

Every call the merge worker makes goes to `https://api.github.com`, or, for a
tenant registered against an Enterprise Server instance, to that tenant's forge
host as recorded outside anything a tenant identity can write. A task document
can carry a `repository_url`, and §0 already treats everything a tenant
identity writes as a claim, so the merge worker never reads a host, an owner
or a repo name out of the task, the workflow document or `result_summary.git`.
It refuses to start (`CANNOT_START`, `forge_host_invalid`, §6 row 14) if that
record is missing or its host does not match the pinned form
(`api.github.com`, or the registered Enterprise Server host exactly). This is
what stops a forged `repository_url` — for example one pointing at a
look-alike host that would happily accept the installation token and hand back
a fabricated "green" response — from ever being asked anything.

**DECIDED, round-3 re-review, 2026-09-29 (open question (a) resolved): the
host, `owner`, `repo`, `review_app_id` and `review_app_bot_id` are
Terraform-rendered values baked into the `post-verdict` and `merge` Jobs' own
environment at deploy time — not Secret Manager, not Firestore.** An earlier
draft left this open between the merge secret's own JSON and a
Terraform-rendered Job environment; the owner chose the latter for both
Jobs. This still satisfies the constraint that mattered — the value must not
sit anywhere a tenant identity can write, and Terraform's own state and
`.tfvars` are outside any tenant identity's reach, the same guarantee
CLAUDE.md's Terraform section already relies on for every other resource.
Two of the five values (`host`, `owner`, `repo`) are static per-tenant
registration facts an operator already knows when running
`register-tenant.sh`. `review_app_id` is likewise static — the integer the
owner is given when creating the App. `review_app_bot_id` is not: it is the
App's bot **user** id, which only GitHub can answer, resolved once by
`register-tenant.sh --add-provider git-review` calling
`GET /repos/{owner}/{repo}/installation` with the App's own freshly-minted
JWT (§5.2a), and the script then emits the resolved value into the tenant's
Terraform variables (a generated `.tfvars` fragment, or a documented
variable the operator supplies to the next `terraform apply`) rather than
writing it anywhere at runtime. `post-verdict` needs only `host`/`owner`/`repo` (it authenticates with its own
App key and does not need to know its own bot id to post a review); `merge`'s
Job needs `host`/`owner`/`repo` too, plus `review_app_bot_id`, since `merge`
is the step that reads reviews back and must filter them to the trusted
App (§5.2a). `review_app_id` is provisioned to both, for a defense-in-depth
sanity check — `post-verdict` can refuse to post if the key it read does not
match the registered `review_app_id` — though only `merge`'s read is what the
design's guarantee actually depends on. None of these five values is a forge
token or an App key — CLAUDE.md's
Terraform section forbids those in a tfvars file or a Job environment, and
this design does not put them there: the `-git-review` and `-git-merge`
secrets, holding `{app_id, private_key}`, stay exactly where §2.1 and §4.3
already put them, in Secret Manager, read only at runtime by the one
identity each is scoped to.

The JWT and every minted token go to that pinned host and nowhere else: the
worker's forge client is built with no default trust of redirects, and a `3xx`
response to any credentialed request (the JWT exchange, the installation-token
mint, every read and the merge call itself) is treated as a failure
(`forge_refused`, §6) rather than followed. A redirect is exactly how a
credential ends up leaving the pinned host, and GitHub's own API has no
legitimate reason to answer any of these calls with one.

The same host and owner/repo record is what the `-git` publish path (used by
`_publish_git` for `direct-pr` and `integrate`) needs too, and does not have
today — it reads `repository_url` from the task. That gap is not fixed by
this design; #307 covers the clone side of it, and the publish side should be
filed the same way (§7 lists it as out of scope for the merge step
specifically, since a merge step that trusts its own control-plane record
gains nothing if the step that pushed the branch it merges did not).

### 2.2 Where it is fetched, when, and how it is dropped — the #219 ordering

In order. A step that fails stops everything after it, and nothing that follows
it runs.

1. **Entrypoint hardening first.** `prctl(PR_SET_DUMPABLE, 0)`, read back from
   the kernel (`hardening.py`), happens before the configuration is read, as it
   does for every worker. If it failed, the credential is never read, and the
   step ends `MERGE_REFUSED` / `worker_unprotected`.
2. **Fencing and lease.** The ordinary start. A stale generation exits here
   without reading anything (invariant 5).
3. **No runner is started.** There is no agent, so there is nothing to wait
   for, stop or harvest.
4. **Reap anyway, and verify it.** `procman.reap_foreign_processes` runs before
   the credential is read, exactly as `_publish_git` calls it before the `-git`
   token. In a merge Job it should find nothing. If it finds something, that
   means something the design says cannot happen did happen, so the step ends
   `MERGE_REFUSED` / `processes_alive` rather than going ahead.
5. **Stage and check every claim that needs no credential** (§4.1, §4.2): the
   staged artifacts parse, the outcome and evidence fields are readable, and
   the shas agree with each other. A refusal here costs no secret read at all.
6. **Read the secret.** Secret Manager `access`, as `swarm-<tenant>-merge`,
   into a local variable. It is registered with the logger's redaction at once,
   the same way `resolve_git_token` does it.
7. **Mint the installation token.** The worker signs a JWT with the key (valid
   for at most 10 minutes), resolves the installation, and exchanges the JWT for
   an installation token scoped to one repository and two permissions. The PEM
   and the JWT are then dropped (`del`). They were never in a file, an
   environment variable or argv, and they reached no subprocess.
8. **Re-check fencing, `cancel_requested`, and the pull request's `base.ref`.**
   A cancel or a reclaim that arrived while the worker was reading is honoured
   before the forge is touched. `base.ref` is re-read here too, immediately
   before the merge call, closing the window between §5.1's first read and the
   call itself in which the pull request could be retargeted to an
   unprotected branch (§5.1, §5.3, §9); a mismatch refuses `base_retargeted`.
9. **The forge reads and the merge** (§5), all over HTTPS from the worker
   process through `forge._request`, to the pinned host only (§2.1b), with no
   redirect followed. **No git runs.** The merge step does no clone, no fetch
   and no push, so there is no credential helper, no credential file and no
   repository configuration that anything could have written.
10. **Record** (§5.4), with the same token, and **re-read `base.ref` once more**
    (§5.3, §9) to confirm the merge commit landed on the branch this design
    pinned rather than one retargeted in the instant between step 8's check
    and the merge call.
11. **Revoke and drop.** `DELETE /installation/token`, then drop the reference,
    in a `finally`, whatever happened in 9–10. An unrevoked token expires within
    the hour anyway, and the revoke is what makes the window seconds long.
12. **Finish.** `control.finish` with the outcome. The result summary names the
    App, the installation and the token's expiry. It never contains the token.

The credential is therefore in memory from step 6 to step 11, in a process that
is non-dumpable. It runs as an identity no agent has held, in a container where
no agent has run. It is never on disk, never in an environment variable and
never in a log.

---

## 3. The chain has to produce ONE pull request first

The merge step is the last link. The first four links are **not built yet** in
the shape the merge step needs. Today every step of a `direct-pr` workflow
pushes its own `swarm/<task id>` branch and opens its own pull request, and
`repository_ref` is workflow-wide. So a review step cannot check out the
implement step's branch, and a fix step would open a second pull request.

The design therefore depends on a new dispatch strategy, **`single-pr`**,
recorded in `metadata.dispatch` the same way `integrate`'s roles are. It is
swarm-api and worker code, and needs **no** frozen-contract change. Each step
declares a `pr_role` in the submission, and the API writes it, with the task ids
it resolves, into that step's dispatch block:

| `pr_role` | clones | publishes | the chain's step |
|---|---|---|---|
| `author` | the workflow's `repository_ref` | pushes `swarm/<own task id>` and opens the pull request | implement |
| `reader` | the author's branch, `swarm/<author task id>` | **no git push**, whatever the strategy. Neither review nor proof talks to GitHub itself any more (§4.3, corrected 2026-09-29): review only writes `review.json`; proof only writes `proof.json` | review, proof |
| (none, no clone) | nothing — it runs no agent | submits the GitHub PR review with the review App's key (§4.3) | `post-verdict` (worker-action profile, new 2026-09-29) |
| `amender` | the author's branch | fast-forward pushes to the **author's** branch and opens nothing | fix |
| (none) | nothing | the merge (§5) | merge (profile `merge`) |

The chain is **implement → review → post-verdict → fix → proof → merge**.
`post-verdict` sits between `review` and `fix`, not beside them, and `fix`
depends on `post-verdict`, not on `review` directly: this ordering — not a
storage permission — is what keeps `fix`'s agent from ever being able to act
before the verdict is already an immutable GitHub review object (§4.3 explains
why a permission alone would not be enough here).

The API refuses a `single-pr` workflow unless all of the following hold:

* exactly one `author`, and it is an ancestor of every other step;
* exactly one step on the `merge` profile, and exactly one on the
  `post-verdict` profile, and `merge` is the workflow's only sink;
* the `post-verdict` step's `input_from` names the review's `review.json`,
  and depends on `review` directly and on nothing else;
* every step downstream of `review` other than `post-verdict` — concretely,
  `fix` — depends on `post-verdict`, not on `review`, even though it needs
  nothing `post-verdict` stages. This is an ordering-only dependency
  (`depends_on` with no matching `input_from` entry), which `validate_dag`
  already allows;
* the merge step's `input_from` names the proof's `proof.json`, and it
  depends on `post-verdict` and `proof` directly. `validate_dag` already
  requires every `input_from` source to be a direct dependency; the
  dependency on `post-verdict` carries no `input_from` entry, for the same
  ordering-only reason as `fix`'s.

**The pull request needs a fact-style title before the review sees it.** The
worker opens every pull request as `[swarm] <task id>` (`_publish_git`), and
both merge paths refuse that placeholder. So the author's worker titles the
pull request from a `pr-title.txt` its agent writes to the artifacts directory.
The file must be one line, no longer than GitHub's limit, and must not start
`[swarm] task_`. If it does not meet those rules, the worker keeps the
placeholder and says why. The review then sees and records the title
(`review.json.title`), and the merge refuses any other title (§5.1). Nothing in
the repository reads `pr-title.txt` today. This is part of the author role.

Every branch name is **derived** by the worker from a task id in its dispatch
block, with the `swarm/` prefix that `push_branch` re-checks. It is never read
as a name, the same rule the integrator follows.

Each repository step's worker also records **the commit it cloned** in
`result_summary.git.clone_commit`, beside `pushed_head` and `pull_request`,
which it already records. That is the only way to know which head a reader
step actually saw.

---

## 4. How the worker finds the pull request and the pinned sha

### 4.1 What is staged, and by what

| from | artifact | written by | carries |
|---|---|---|---|
| review | `review.json` | the review **agent** | `{"verdict": "MERGE" \| "CHANGES", "sha": "<40 hex>", "title": "<the title it reviewed>", "summary": "..."}` |
| proof | `proof.json` | the proof **agent** | `{"outcome": "PROVED" \| "NOT_PROVED", "sha": "<40 hex>", "evidence": "..."}` |

`proof.json` is staged the ordinary way: `merge` declares
`"input_from": {"proof": "proof.json"}`, and swarm-api records
`metadata.expected_outputs` on the `proof` task, so a proof that does not
write its outcome fails **its own** attempt, retryably, and the merge never
starts ([workflows.md](workflows.md#artifacts-pass-by-reference)). `fix`
stages `review.json` the ordinary way too — `"input_from": {"review":
"review.json"}` — for the same reason, and because `review.json`'s general
read grant is unrestricted (§4.3), that staging is unaffected by anything
below. **`post-verdict` does *not* stage `review.json` through
`input_from`** — §4.3 (BLOCKER, joint review with CR 34) explains why the
ordinary mechanism is unsafe for the one read this design's verdict anchor
depends on, and what it does instead. A missing file at staging is
`INPUTS_UNAVAILABLE`, as for any step that does use `input_from`. The names
are distinct, as `validate_staged_filenames` requires.

**`review.json` remains the human-readable record — the summary — but is not
the trust anchor for the verdict any more.** §4.3 anchors the verdict outside
GCS and outside Firestore entirely, in a GitHub PR review object that only the
**`post-verdict`** worker-action step's own App can create — not the review
step's own App, because the review step still runs an agent (§1.3, §4.3). The
merge worker still reads `review.json` (its `sha` feeds §4.2's equality chain,
and its `summary` goes into the provenance comment, §5.4), but treats its
`verdict` field as descriptive, not authoritative: the authoritative verdict
is the GitHub review object `post-verdict` created.

**`proof.json` is not anchored the same way, and stays a plain staged
artifact, checked only by sha equality (§4.2).** §4.3 says why: unlike the
verdict, there is no later agent in *this* chain that could forge it (`proof`
runs immediately before `merge`, which runs no agent), and anchoring an
agent's self-graded test run would not change what "PROVED" means anyway.

**The pull request number and the pinned sha are NOT staged artifacts.** They
are facts only a worker knows: which commit it cloned and what it pushed. They
are read from the upstream tasks' `result_summary.git`, by the task ids that
the merge step's dispatch block names — **corrected, joint review with CR 34,
2026-09-29: `merges: {author, review, post-verdict, fix, proof}`, not
`{author, review, fix, proof}`.** `post-verdict` names no git operation and
so contributes nothing to the `result_summary.git` equality chain (§4.2), but
`merge`'s signed-spec verification (§0 consequence 4, #342) needs its task id
too: `merge` verifies the **union** of every upstream task's signed spec —
`author`, `review`, `post-verdict`, `fix`, `proof` — not only the subset that
happens to feed `merge`'s own `input_from` (which today is `proof` alone; §6
row 42 states this explicitly). Two reasons the pull request number and sha
are not staged artifacts in the first place:

* written as files, they would be one fixed worker name from four parents,
  which `input_from` refuses by design (one filename per parent per step);
* an agent-authored file would be the wrong source for them. The review agent
  says what it **judged**; the review worker says what it **cloned**. The merge
  needs both, and needs them to agree.

### 4.2 The pinned sha, and how every claim is checked

`pinned` is defined as `review.result_summary.git.clone_commit`, the commit the
review worker checked out. The merge proceeds only if **all** of these are
equal to it:

* `review.json.sha`, which is the head the review agent says it judged;
* `proof.result_summary.git.clone_commit`, the head the proof worker checked
  out;
* `proof.json.sha`;
* the last `pushed_head` recorded by the author or the amender. If the fix step
  pushed anything, this differs and the merge refuses;
* the live pull request's `head.sha` (§5.1);
* the `commit_id` on the GitHub review the merge requires (§4.3), which
  `post-verdict` set from `review.json.sha`, not from anything it re-derives.

The chain's four workers write the first four values under the tenant
identity. By §0 those are claims, so they are checked against GitHub:

* the pull request number comes from the author's `result_summary.git`. The
  live pull request must have `head.ref == "swarm/<author task id>"` (derived,
  not read), `head.repo.full_name == base.repo.full_name` (no fork),
  `base.ref ==` the repository's default branch, and `state == "open"`;
* its live `head.sha` must equal `pinned`, and the merge call carries `sha`, so
  GitHub itself refuses the merge if the head moves between the read and the
  merge (§5.3).

**Every one of those reads is also bound to the repository and the pull
request number, not to the sha alone (B2).** `owner`/`repo` come from the
tenant's control-plane record (§2.1b), never from a task document, and every
GitHub call in this section is scoped to that `owner/repo` and to the one pull
request number the author's `result_summary.git` names — so an equal sha on a
*different* pull request, or on the same-numbered pull request of a
different, forged repository claim, satisfies nothing. A sha is a 40-hex
string with no idea which repository it came from; the pull request and
repository binding is what makes "equal to `pinned`" mean "the same commit, on
the one pull request this chain opened," not merely "some commit somewhere
with this hash."

What that proves: the merge lands exactly the tree the review and the proof
**say** they saw, from this workflow's own branch. What it cannot prove is that
the verdict was really written by the review step, or that the outcome was
really written by the proof step. §4.3 covers both.

### 4.3 The verdict is anchored on GitHub itself, by a step that runs no agent — B1, corrected 2026-09-29

**Re-reviewed 2026-09-29: the first revision of B1 (a dedicated
`claude-code-review`/`claude-code-proof` profile per step) was found NOT
sufficient and is withdrawn.** A dedicated profile changes which Job and
service account a step's *agent* runs under, but the App key still sits in
that same container, and the agent that runs there is exactly the thing B1
needs to keep it away from:

* **the review agent reads attacker-controlled diffs.** It is the review's
  whole job to read the pull request's content, which is exactly the surface
  a prompt-injection attack uses. A `claude-code-review` profile does not
  change that; it only stops a *different* step's agent from reading
  `-git-review` (§1.1's argument), not the review step's own;
* **the proof agent executes the pull request's own code.** Running the
  repository's tests *is* the proof. That is a direct path to
  `169.254.169.254` (§0), a much more direct one than prompt injection: no
  persuasion is needed, only a `Makefile` target or a test fixture that reads
  the metadata server, which the proof agent runs with the same lack of
  network policy every agent has.

Either agent, on its own profile or not, could mint its own container's
service-account token and read a PEM sitting in that container's Secret
Manager grants. **The only place an App key may safely sit is a step that
runs no agent and executes no pull-request code at all — the same reason
`merge` itself had to become a profile rather than a strategy tacked onto
`proof`'s own execution (§1.1).** This section is the corrected design.

**The fix and proof agents both run after the review and before the merge.**
Either one can mint the tenant token (§0), learn the review task's id from the
workflow document, and overwrite `review.json` in GCS or `result_summary` in
Firestore with `"verdict": "MERGE"`. The sha checks alone do not catch this,
because the forger writes the right sha. The design's first draft proposed a
scheduler-written attestation; its second, an App key held by the review
step's own agent-running container. **The owner's decision, 2026-09-29:
neither.** The review agent still writes `review.json`, exactly as before —
that file is still the record of its judgement. But nothing with an App key
runs in the same container as that agent, or as any other. A new, no-agent
worker-action profile, **`post-verdict`**, reads `review.json` after the
review step succeeds and posts the GitHub review.

**`post-verdict`: a worker-action profile, structured exactly like `merge`
(§1.3).** Its own Job, `swarm-job-<tenant>-post-verdict`; its own service
account, `swarm-<tenant>-post-verdict`, the **sole** accessor of
`swarm-tenant-<tenant>-git-review`; `runner_argv=()`; no agent ever runs
there. It depends on `review` alone.

**BLOCKER, joint review with CR 34, 2026-09-29: `post-verdict` must never
stage `review.json` the ordinary way.** An earlier draft of this paragraph
said `input_from`, and so did §4.1 and §6a row 4. The ordinary `input_from`
staging path (`agent_worker/inputs.py:12,298,387`) resolves an artifact's GCS
location using fields the upstream task's own `result_summary` carries — a
Firestore field the tenant identity writes, and so, by §0, a claim, not a
fact. Routing the one read this whole design depends on through a
tenant-writable pointer would mean a `fix` or `proof` agent could redirect
`post-verdict`'s staging at an object of its own choosing, without ever
touching the review-only-writable prefix's IAM at all. **Instead,
`post-verdict` computes the object's path itself, from facts only its own
signed spec (#342) can supply — never from a live Firestore read of anything
mutable:**

```
gs://<bucket>/tenants/<tenant>/verdicts/<workflow_id>/<review task id>/review.json
```

`workflow_id` is `post-verdict`'s own task's workflow id; `<review task id>`
is the upstream task id named in `post-verdict`'s own dispatch block, the
same way `merges: {author, review, ...}` names task ids for `merge` (§4.2).
Once #342 verifies `post-verdict`'s signed spec, both values are facts, not
claims — the signature is exactly what makes them trustworthy inputs to a
path computation, where the ordinary staging mechanism's `result_summary`
read is not. `post-verdict` then issues one `storage.objects.get` at that
exact path — a direct GCS read, not a call into `agent_worker/inputs.py`'s
general resolver — and validates the schema itself (§4.1, §6a row 4). The
workflow spec still declares `"depends_on": ["review"]` for DAG ordering
(§3), but nothing about *how* `post-verdict` finds `review.json` goes through
that dependency edge's ordinary staging. `workflows.md`'s example spec
reflects this (no `input_from` on the `post-verdict` step).

After that read, and after the same #219-ordered read-secret/mint/revoke
sequence §2.2 gives the merge credential, over the same host-pinned,
no-redirect forge client (§2.1b), `post-verdict` submits:

```
POST /repos/{owner}/{repo}/pulls/{n}/reviews
{
  "commit_id": "<review.json.sha>",
  "event": "APPROVE" | "REQUEST_CHANGES",
  "body": "<review.json.summary>"
}
```

`event` is `APPROVE` exactly when `review.json.verdict == "MERGE"`, and
`REQUEST_CHANGES` otherwise. The review agent still decides; `post-verdict`
only carries that decision onto GitHub, deterministically, with no model and
no attacker-reachable input in the loop — it reads one JSON file whose shape
it validates and makes one API call. `commit_id` pins the review to the sha
the review agent itself judged; the merge step separately confirms that sha
is `pinned` (§4.2), so a stale or mismatched `review.json` cannot be laundered
through `post-verdict` into an approval that means something it does not.

**The review-only-writable prefix (owner decision, round-3 re-review,
2026-09-29, layout corrected in a joint review with CR 34): `review.json` is
written to a GCS prefix the tenant's ordinary worker service account cannot
write.** Ordering (above) closes the *in-chain* race — `fix`, in this same
workflow, cannot act before `post-verdict` has consumed `review.json`. It
does not by itself close a *same-tenant, different-workflow* one: any agent
of **another** workflow of the same tenant runs as the same shared
`swarm-agent-worker-<tenant>` account (§0), which is not ordered against this
workflow's DAG at all. The prefix's write restriction closes the
*write-path* half of that — a different workflow's agent, running as the
ordinary tenant account, cannot write here regardless of timing. **It does
not close the whole threat: §7 T3 and the residual below say what remains,
because the review identity itself, not just the ordinary tenant account, is
shared tenant-wide.**

**Real bucket layout, corrected (CR 33/CR 34 joint review):** this platform's
artifacts live in one shared bucket per environment, under `tenants/<t>/` —
not a per-tenant bucket, and not `<tenant-bucket>/<tenant>/` as an earlier
draft of this section said. The tenant worker SA's existing grant
(`register-tenant.sh:961`, the conditioned `worker_objects` binding) already
gives it `roles/storage.objectUser` on everything under `tenants/<t>/`. **IAM
on Cloud Storage is allow-only — there is no deny binding to carve an
exception out of that grant** — so closing off `tenants/<t>/verdicts/`
specifically means replacing that one broad binding with two narrower ones,
not adding a third:

1. **`roles/storage.objectViewer` on `tenants/<t>/`**, unconditioned within
   that prefix — the tenant worker SA keeps ordinary read everywhere,
   including `verdicts/`, so `fix`'s `input_from` staging of `review.json`
   (§4.1) is unaffected;
2. **`roles/storage.objectUser` on `tenants/<t>/`, conditioned to
   `resource.name.startsWith("projects/_/buckets/<bucket>/objects/tenants/<t>/") && !resource.name.startsWith("projects/_/buckets/<bucket>/objects/tenants/<t>/verdicts/")`**
   — write everywhere under the tenant's prefix *except* `verdicts/`. This
   replaces the tenant worker SA's current unconditioned `objectUser`
   binding; it is not an addition alongside it, since two IAM bindings
   granting overlapping write access would defeat the exclusion;
3. **the review SA, `swarm-<tenant>-review`, gets `roles/storage.objectUser`
   on `tenants/<t>/verdicts/` alone**, the complement of binding 2 — this is
   what makes it, not the ordinary tenant SA, the only identity able to write
   there. It also needs `storage.objects.delete` on the same conditioned
   prefix: **overwriting an existing object is a delete-then-create under
   GCS's own permission model whenever the write must be conditional**
   (below), so a *legitimate* retry of the `review` task — which needs to
   replace its own prior attempt's `review.json` — cannot do so with
   `objectUser` (create/update) alone.

**Conditions of this shape require Uniform bucket-level access on the
bucket** (fine-grained ACLs disabled) — already true, since the existing
`worker_objects` binding in `register-tenant.sh:961` is itself conditioned,
so this is not a new bucket-level prerequisite this design introduces.

`post-verdict`'s own service account keeps the same read grant every other
tenant identity has under `tenants/<t>/` (binding 1, unaffected) — nothing
narrower, nothing wider — plus, separately, the App-key secret access §1.3
already scopes to it alone. What makes `post-verdict` trustworthy is not
that it is the only *reader* of `review.json` — several identities can read
it — but that it is provably the only identity that can **post the GitHub
review**, because only it holds `-git-review`.

**Why this closes the in-chain window, and why it does NOT close the
cross-workflow one unconditionally — corrected, joint review with CR 34,
2026-09-29:**

* `post-verdict`'s container never runs an agent, so there is nothing for a
  prompt injection or a malicious test fixture to reach — the credential
  isolation §0 gives `merge` applies here for the identical reason, not by
  analogy;
* the fix and proof agents — and any other tenant workflow's agents — cannot
  mint `swarm-<tenant>-post-verdict`'s token, because that service account
  is not theirs and nothing grants their account `iam.serviceAccountTokenCreator`
  or `actAs` on it (the same clause §1.3 gives the merge account);
* **in-chain forgery (the fix agent, in this workflow) is closed by
  ordering:** `fix` depends on `post-verdict`, not on `review` (§3), so
  `fix`'s agent cannot start — and so cannot overwrite `review.json` — until
  `post-verdict` has already read it and posted an immutable GitHub review.
  This is the same principle the original attestation design used ("the fix
  agent starts only after the attestation is written"), applied to a
  no-agent step instead of a scheduler sweep;
* **cross-workflow verdict laundering is NOT closed. The owner did not
  choose to bind a review to a specific pull request or task id
  (round-3/joint-review decision), so this is recorded as a NAMED RESIDUAL,
  R8, below** — `swarm-<tenant>-review` is one identity shared by *every*
  `single-pr` workflow of the tenant, not scoped per-workflow (the same
  "one Job per tenant per profile" constraint that shapes every other
  identity in this design, §1.1), and its write grant covers the whole
  `tenants/<t>/verdicts/` subtree, not one workflow's slice of it. A review
  agent compromised by prompt injection (T11) in workflow A can write a
  fabricated `review.json` to workflow B's path,
  `tenants/<t>/verdicts/<workflow-B id>/<review-task-B id>/review.json`, if
  it is told (or can derive) workflow B's identifiers — the same "would have
  to be told them" caveat the pre-B1 design's R2 already carried. Workflow
  B's own, legitimate `post-verdict` has no way to tell a forged object at
  that path from a genuine one: the write identity is shared, so only the
  *path* indicates provenance, and the path is exactly what an attacker who
  has the ids can compute too. **A minted `-git-review` installation token
  is also a portable bearer credential in the same sense:** it authorises
  `pull_requests: write` on the whole installed repository, not one PR, so
  even a correctly-anchored review from `post-verdict` proves "the App
  approved this exact commit," not "the App approved it *on behalf of this
  workflow specifically*" — the same tenant-wide, not workflow-scoped,
  granularity problem, one layer up;
* **partial mitigation, not a close: write-once (`ifGenerationMatch=0`) on
  the review write.** `review` publishes `review.json` with GCS's
  compare-and-swap precondition, so whichever write reaches a given path
  *first* wins, and every later write to that same path fails outright. This
  does not prevent the race — it only changes what losing it looks like.
  If the legitimate write wins, an attacker's later write to the same path
  is rejected, not silently accepted as an overwrite. If an attacker's write
  wins first, the *legitimate* `review` task's own write then fails the same
  precondition — which the review step can detect and fail its own attempt
  on, turning a silent forgery into a loud, attributable one (the review
  task ends in error rather than the workflow quietly anchoring a verdict
  nobody in it wrote). It does not stop the forged verdict from existing at
  that path in the meantime, or from being read by a `post-verdict` that
  races ahead of the detection;
* what is **not** closed, by any of the above or by #342: **T11**, prompt
  injection of the review agent itself, so that the *real* review step
  reaches the wrong conclusion and writes `"verdict": "MERGE"` honestly, on
  its own judgement, about a change it should not have approved.
  `post-verdict` faithfully carries whatever `review.json` says; it cannot
  audit the judgement behind it. This is residual risk **R1** (§7).

**A fourth mechanism, not yet built, closes a different window: the
instructions the review agent acted on.** Everything above protects
`review.json` — the review agent's **output** — from tampering, before or
after it is written. None of it protects the review agent's **input**: the
`prompt` its task document carries, which any tenant identity can rewrite
while the review task still sits `PARKED` (§0, consequence 4, BLOCKER). An
`implement` agent that rewrites `review`'s prompt before `review` starts gets
an honest `post-verdict`, honestly anchoring an honest verdict — about
instructions nobody who matters actually wrote. **This design depends on
#342** (signed step specs): `post-verdict` must verify the `review` task's
signed spec, not only stage its `review.json`, before trusting either the
verdict or the sha it claims to have judged. §7 T14 names this threat
directly; B1 is closed only once #342 ships.

**Why there is no `post-proof` step, and no `-git-proof` secret.** The
`proof` agent has the identical exposure — it executes the pull request's own
code — so if it held an App key the same argument that ruled out
`claude-code-proof` would rule out a `post-proof` step holding one *for the
proof agent to feed*, except that a `post-proof` step, like `post-verdict`,
would run no agent itself, so that specific objection does not apply to it.
The reason a `post-proof` step is not built here is different: **there is no
later agent in this chain for it to protect against.** `fix` runs *before*
`proof`, and nothing but the no-agent `merge` step runs *after* it, so unlike
`review.json`, there is no same-chain step that could overwrite `proof.json`
between its write and the merge's staging read. The only forger left is a
different tenant workflow racing the staging read with a Firestore- or
GCS-write it can already make (§0) — a narrower threat than the one B1 exists
to close, and one this design accepts rather than builds a second no-agent
step to close (§7 records it as a residual risk). **This is deliberately
asymmetric with the review-only-writable prefix above:** the same
cross-workflow race exists for `proof.json`, and a `proof`-only-writable
prefix would close it the identical way. The owner's round-3 decision built
the prefix for the verdict specifically, not for the proof; extending it to
`proof.json` is a small, mechanical follow-on if the owner wants the same
closure there, not a design gap this document is unaware of. **And, more
fundamentally:
a proof step that executes the pull request's own code and reports its own
outcome anchors only that the code ran and produced that outcome — never
that the code, or the change, is safe.** An App-signed check run would make
that claim harder to *forge*; it would not make the claim mean more. The
required GitHub Actions checks (§5.2) are the actual safety gate; `proof.json`
is evidence attached to the record, the same status it had before B1, and
`proof.json.outcome == "PROVED"` stays a plain, sha-checked field (§4.2), not
an App-anchored one.

---

## 5. What the merge step asks GitHub, and what counts as green

Every call goes to the pinned forge host (§2.1b: `api.github.com`, or the
tenant's registered Enterprise Server host — never a host read from the task),
scoped to the control-plane `owner`/`repo` and the one pull request number
(§4.2), through `forge._request`, with the installation token, API version
`2022-11-28`. No credentialed request follows a redirect (§2.1b): a `3xx`
response is `forge_refused`, not a hop. Every list call is **paginated to the
end** (`per_page=100`, follow `Link: rel="next"`) — with one bound: `GET
.../pulls/{n}/files` stops, and the step refuses `too_many_files`, at
GitHub's own 3000-file cap on that endpoint. Past that cap GitHub's response
is itself incomplete — a file that would show a workflow or path-guard change
could be sitting past page 30 with no way for another page to reveal it — so
the step treats "the diff is too large to read completely" as a refusal, not
as "checked and clean." A page that is not the last one, up to that bound, is
not the answer.

### 5.1 The pull request

`GET /repos/{o}/{r}/pulls/{n}` returns `state`, `merged`, `draft`, `head.ref`,
`head.sha`, `head.repo`, `base.ref`, `title` and `mergeable`. The step refuses
(§6) unless:

* it is open, not a draft, from this workflow's branch, not from a fork, and
  based on the default branch;
* `head.sha == pinned`;
* `title == review.json.title`, so the subject that lands on `main` is the one
  the review saw. The title must also not be the worker's placeholder: the
  same rule as `auto-merge.yml` gate 1, `[swarm] task_` compared case- and
  leading-whitespace-insensitively;
* `mergeable == true`. GitHub computes it lazily, so `null` is read again at
  most twice, 2 s apart. That is a bounded pause measured in seconds, not a
  provider wait (invariant 4). If it is still `null`, the step refuses with
  `mergeability_unknown`.

`base.ref` from this same read is re-checked twice more: immediately before
the merge call (§2.2 step 8) and immediately after it (§2.2 step 10, §5.3,
§9), so a retargeting of the pull request's base branch in either window is
caught rather than silently landing a squash on a ref this design never
evaluated.

`GET /repos/{o}/{r}/pulls/{n}/files` (paginated, capped as above): a pull
request that touches any of `.github/workflows/`, the rest of `.github/**`,
`scripts/**`, `terraform/**`, `kubernetes/**`, `images/**`, `Makefile`,
`pyproject.toml`, `uv.lock` or any `**/conftest.py` is refused with
`touches_protected_paths` (§6, §7 T6, M4) — broadened from the original
`.github/workflows/`-only check the same day as B1 and B2, because a security
review found the narrower list did not match the actual exposure (§7 T6): a
chain that lands *any* of these paths on `main` is exercising the deploy
identity `release.yml` runs the merge under, not just CI's own configuration.

### 5.2 The checks

1. **The required set.** `GET /repos/{o}/{r}/rules/branches/{base}` — GitHub's
   single endpoint for "every rule that applies to this branch," which merges
   a ruleset's rules and any classic branch protection into one list, rather
   than reading `.../rulesets` and `.../branches/{base}/protection` separately
   and having to reconcile them by hand (corrected 2026-09-29; the two-call
   form risked reading one without the other and calling the result complete).
   It gives the required-status-checks rule: `checks[] {context, app_id}`.
   This is the same read as `auto-merge.yml` gate 3, and it needs only read
   access. **An empty set is a refusal** (`no_required_checks`), for the
   reason gate 3 gives: with nothing to require, "green" would mean nothing.
2. **Each required check, at `pinned`.**
   `GET /repos/{o}/{r}/commits/{pinned}/check-runs?check_name=<context>&filter=latest`.
   Only runs whose `app.id` equals the rule's `app_id` count (every rule in
   [ci.md](ci.md) pins GitHub Actions, `15368`). That makes a check run of the
   same name, posted by any other App or token, invisible to this step, which
   is the same guarantee that pinning gives GitHub. **M5: a required check
   with no `app_id` is refused outright, `required_check_unpinned`.** The
   original design fell back to `GET /repos/{o}/{r}/commits/{pinned}/status`,
   GitHub's legacy commit-status API, for an unpinned required check. That
   fallback is removed: a legacy status carries no `app_id` at all, so any
   token that can write commit statuses — which includes `-git`, which §0
   says any agent can read — could satisfy an unpinned required check by
   posting one directly. Refusing unpinned checks rather than accepting a
   status is the only way to keep the merge step's guarantee — "green means a
   check from the App the rule names" — true for every required check, not
   just the ones the rule happens to pin today.
3. **What counts as green:** `status == "completed"` **and** `conclusion` is
   `success` or `skipped`. Nothing else counts:
   * `queued`, `in_progress`, `waiting`, `requested` or `pending` means
     **pending, and pending is not green.** The step does not wait for it
     (invariant 4: a worker does not sleep through a wait it cannot bound)
     and refuses with `checks_pending`;
   * a required check with **no** run at `pinned` is pending too. GitHub treats
     it the same way, as pending for ever;
   * `failure`, `cancelled`, `timed_out`, `action_required`, `neutral`,
     `stale` or `startup_failure` gives `checks_failed`. `neutral` is on this
     list deliberately: the owner's rule is "success or skipped".
4. **Every other check run at `pinned`** (the unfiltered list, paginated): any
   one that is not completed, or completed with a conclusion other than
   `success`, `skipped` or `neutral`, also refuses. This is `auto-merge.yml`
   gate 5's rule. A path-filtered workflow is not required, but when it ran
   and failed, that is real signal. Here `neutral` is tolerated only for a
   check that is **not** required, which is gate 5's own tolerance. The merge
   step must never be looser than the authoritative gate (§8), and nowhere is
   it looser.

### 5.2a The verdict — B1, corrected 2026-09-29

One more read, not part of branch protection, required before the merge call:
`GET /repos/{o}/{r}/pulls/{n}/reviews` (paginated), the latest review by
`user.id == review_app_bot_id`, must have `state == "APPROVED"` and
`commit_id == pinned`. Anything else — no review from that app, the latest one
`CHANGES_REQUESTED` or `COMMENTED`, or an `APPROVED` review at a different
commit — refuses `verdict_not_approved` (§4.3). A dismissed review does not
count: GitHub reports a dismissed review's `state` as `DISMISSED`, not
`APPROVED`, so this check needs no separate dismissal handling.

**`review_app_bot_id` is pinned at registration, never re-resolved live
(corrected 2026-09-29).** The original draft read it at merge time from
`GET /repos/{o}/{r}/installation`, using whatever token happened to be at
hand — which begs the question of which identity is trusted to answer, every
single merge. Instead, `register-tenant.sh --add-provider git-review` reads it
**once**, at registration, using the review App's **own** freshly-minted JWT
(the same key being registered, self-attesting its own installation's bot user
id). **DECIDED, round-3 re-review (open question (a)): the resolved value is
emitted into the tenant's Terraform variables and rendered into the `merge`
Job's environment at deploy time (§2.1b), not written anywhere at runtime.**
The merge step reads that Terraform-rendered value; it never asks GitHub "who
is the review App" again. This closes a class of confusion the live-read form
invited: a live read has to trust *something* to make the call, and pinning
at registration means that something is only ever the App's own key, once,
under the owner's hand.

There is no equivalent read for a proof outcome (§4.3): `proof.json.outcome`
is checked directly, by sha equality (§4.2), not through a GitHub object.

The refusal names every check or review that was not green, with its
conclusion or status, and the sha it was read at.

### 5.3 The merge

```
PUT /repos/{o}/{r}/pulls/{n}/merge
{
  "merge_method": "squash",
  "sha": "<pinned>",
  "commit_title": "<PR title> (#<n>)",
  "commit_message": "<provenance, §5.4>"
}
```

`commit_title` is set explicitly and matches `auto-merge.yml`'s `--subject`.
Without it, a one-commit squash takes that commit's message, which is how #238
put `swarm: work from task_...` on `main`. `sha` is the pin, and GitHub answers
`409` when the head is no longer that commit. That is the atomic form of §4.2's
last check: no window exists between "the head is what was reviewed" and "this
is what was merged".

A `200` with `merged: true` carries the merge commit's sha, which is recorded.
**The worker then re-reads the pull request once more** (§2.2 step 10, §9)
and confirms `base.ref` still names the default branch: GitHub does not
change a merge's actual target after the fact, so this read is a consistency
check on the record the step is about to write, not a way to undo a merge
that already happened — but a mismatch here means something this design did
not model occurred, and the step says so in `result_summary` rather than
recording provenance that claims more certainty than the worker actually has.
Everything else is §6.

### 5.4 The pull request records which task merged it

In two places, because they fail differently:

* **the squash commit's body**, which is the permanent record and is written
  atomically with the merge:

  ```
  Merged by SwarmCloud task <merge task id> (workflow <workflow id>),
  attempt <attempt id>, tenant <tenant>.
  Reviewed at <pinned> by task <review task id>: APPROVED by swarm-review
  via task <post-verdict task id> (review id <review database id>).
  Proved at <pinned> by task <proof task id>: <proof.json.outcome> (staged
  artifact, not App-anchored — §4.3).
  Required checks green at <pinned>: <context>, <context>, ...
  ```

  The review line names the GitHub object the merge step actually trusted
  (§4.3, §5.2a) — the review's own id, and the `post-verdict` task that
  posted it, distinct from the `review` task that judged — rather than
  restating the tenant-writable `review.json` claim as if it were the proof.
  The proof line says plainly that it is not the same kind of claim: it names
  what `proof.json` reported, not a GitHub object, because §4.3 does not
  anchor one. The repository and pull request number are implicit in where
  the comment is posted, but the commit body carries them explicitly too
  (`<owner>/<repo>#<n>`), since a squash commit can be read outside its
  originating pull request once history is rewritten or the repository is
  forked.

* **a comment on the pull request**, with the same lines, posted after the
  merge. If the comment fails, the merge still succeeded. The step ends
  SUCCEEDED with `result_summary.merge.recorded_on_pull_request: false` and
  the reason, because the commit body already holds the record.

No line in either one carries `Co-Authored-By` or any other attribution. The
commit is the App's.

---

## 6. Every failure mode, and its end cause

Two new end causes (contract request 33), which keep two different questions
apart:

* **`MERGE_REFUSED`**: a condition for merging was not met, and nothing was
  changed on the forge. The platform is doing its job. Running the step again
  will not help until something upstream changes: a new head, a new verdict, a
  green check.
* **`MERGE_FAILED`**: the merge was allowed and the forge did not do it. This
  one is for an operator to look at.

The specific reason goes in `result_summary.merge.refusal`, a code from the
table below with a message. It is a worker vocabulary, not a frozen one, just
as `publish_reason` is today. Refusals are **not retryable**: the next attempt
would read the same facts and spend an attempt doing it. Rows marked
*retryable* fail the attempt through `control.fail_retryably`, and take the
cause shown only when `max_attempts` is spent. A secret-access failure on the
review App's own key is not in this table: it fails `post-verdict`'s own
attempt (an ordinary `CANNOT_START`, §6a), not the merge step's — this table
is the merge step's end causes only.

| # | what happened | code | end cause |
|---|---|---|---|
| 1 | stale fencing generation at start or before the merge | — | none: exits without touching the forge (invariant 5) |
| 2 | cancel requested before the merge call | — | `CANCEL_REQUESTED` |
| 3 | cancel requested after the merge call returned | — | the merge stands. SUCCEEDED, and the ignored cancel is recorded |
| 4 | the step ran past its timeout | — | `TIMEOUT` |
| 5 | `review.json` or `proof.json` could not be staged | — | `INPUTS_UNAVAILABLE` |
| 6 | the worker is not non-dumpable | `worker_unprotected` | `MERGE_REFUSED` |
| 7 | a process survived the reap | `processes_alive` | `MERGE_REFUSED` |
| 8 | `review.json` or `proof.json` is not the schema in §4.1 | `verdict_unreadable` | `MERGE_REFUSED` |
| 9 | `review.json.verdict` disagrees with the GitHub review's own `APPROVED`/`CHANGES_REQUESTED` state at `pinned` — a defense-in-depth consistency check on `post-verdict`'s own translation, not an attacker-facing one (§4.3, §5.2a) | `verdict_mismatch` | `MERGE_REFUSED` |
| 10 | no `APPROVED` review from the review App's `app_id` at `pinned` (§4.3) | `verdict_not_approved` | `MERGE_REFUSED` |
| 11 | `proof.json.outcome` is not exactly `PROVED` (a plain staged-artifact check, not App-anchored — §4.3) | `not_proved` | `MERGE_REFUSED` |
| 12 | two of §4.2's shas disagree (the message names which) | `heads_disagree` | `MERGE_REFUSED` |
| 13 | the fix step pushed after the review | `review_not_at_head` | `MERGE_REFUSED` |
| 14 | the Job's Terraform-rendered `host`/`owner`/`repo`/App-id environment values are missing or malformed, or the host is not the pinned form (§2.1b) | `forge_host_invalid` | `CANNOT_START` |
| 15 | the merge secret does not exist, or the merge account cannot read it | — | `CANNOT_START` (exit 78: the credential is missing or refused) |
| 16 | the App key was rejected (401), or the App is not installed on the repository (404) | — | `CANNOT_START` |
| 17 | the token cannot write to the repository (`permissions.push` is not `true`) | — | `CANNOT_START` |
| 18 | the pull request is closed and not merged | `pull_request_closed` | `MERGE_REFUSED` |
| 19 | its head branch is not `swarm/<author task id>`, or it comes from a fork | `pull_request_not_this_workflows` | `MERGE_REFUSED` |
| 20 | its base is not the default branch | `base_not_default` | `MERGE_REFUSED` |
| 21 | it is a draft | `draft` | `MERGE_REFUSED` |
| 22 | its live head is not `pinned` (read, or the `409` from the merge) | `head_moved` | `MERGE_REFUSED` |
| 23 | its title is the placeholder, or not the title the review saw | `title_placeholder` / `title_changed` | `MERGE_REFUSED` |
| 24 | it touches a protected path: `.github/**`, `scripts/**`, `terraform/**`, `kubernetes/**`, `images/**`, `Makefile`, `pyproject.toml`, `uv.lock`, any `**/conftest.py` (§5.1, M4) | `touches_protected_paths` | `MERGE_REFUSED` |
| 25 | the `pulls/{n}/files` read hit GitHub's 3000-file cap before it could finish checking row 24 | `too_many_files` | `MERGE_REFUSED` |
| 26 | the base has no required checks | `no_required_checks` | `MERGE_REFUSED` |
| 27 | a required check has no `app_id` (M5) | `required_check_unpinned` | `MERGE_REFUSED` |
| 28 | a check is pending or missing at `pinned` | `checks_pending` | `MERGE_REFUSED` |
| 29 | a check failed at `pinned` | `checks_failed` | `MERGE_REFUSED` |
| 30 | `mergeable` is `false` (a conflict with the base) | `conflict` | `MERGE_REFUSED` |
| 31 | `mergeable` is still `null` after the bounded reads | `mergeability_unknown` | `MERGE_REFUSED` |
| 32 | `base.ref` was retargeted between §5.1's read and the merge call (§2.2 step 8, §9) | `base_retargeted` | `MERGE_REFUSED` |
| 33 | a credentialed read, or the merge call itself, received a redirect that the client refused to follow (§2.1b) | `forge_redirect_refused` | `MERGE_REFUSED` if it was a read; `MERGE_FAILED` if it was the merge call |
| 34 | it is **already merged**, and its head was `pinned` | — | **SUCCEEDED**, `merged_by_this_task: false`, with who merged it |
| 35 | it is already merged, and its head was not `pinned` | `merged_at_other_head` | `MERGE_REFUSED`. Something else landed a tree nobody reviewed, and this step says so |
| 36 | the merge call answered `405` (branch protection or GitHub refused) | `forge_refused` | `MERGE_FAILED` |
| 37 | the forge is unreachable, answered `5xx`, or rate-limited with a short `retry-after` | `forge_unavailable` | *retryable*, then `MERGE_FAILED` |
| 38 | rate-limited with a `retry-after` longer than `max_in_worker_retry_delay_seconds` | `forge_unavailable` | *retryable*, with `next_eligible_at` set to the reset. It does not sleep (invariant 4) |
| 39 | the worker died after the merge and before `finish` | — | the reconciler requeues it, the next attempt hits row 34, and it ends SUCCEEDED |
| 40 | the merge succeeded and the comment did not | — | SUCCEEDED, `recorded_on_pull_request: false` (§5.4) |
| 41 | the merge succeeded, and the post-merge re-read of `base.ref` (§5.3, §9) disagrees with the pre-merge one | `base_mismatch_recorded` | SUCCEEDED, with the discrepancy flagged in `result_summary` rather than asserted away |
| 42 | the signed spec of `author`, `review`, `post-verdict`, `fix` or `proof` does not verify — the union `merge` depends on (§4.2), whether or not that task's output feeds `merge`'s own `input_from` (§0 consequence 4, §7 T14/R7) | `spec_unverified` | `MERGE_REFUSED` — checked early, alongside row 8, before any credential is read |

**Interop with CR 34 (joint review, 2026-09-29): CR 33 keeps its own
`*_REFUSED`/`*_FAILED` vocabulary — the owner did not unify the two designs'
end causes — so row 42's `spec_unverified` and §6a row 5's `spec_unverified`
report the failing upstream task explicitly, in the same shape CR 34's own
reporting uses, rather than a bare code.** `result_summary.merge.refusal`
(and `result_summary.verdict.refusal` for `post-verdict`) carries
`{"code": "spec_unverified", "message": "upstream:<task id>:<why>"}` — for
example `upstream:task_review_abc123:signature_mismatch` or
`upstream:task_fix_def456:spec_missing`. `<why>` is CR 34's own short
vocabulary for a verification failure, not a new one invented here; CR 33
only adopts the format, `upstream:<task id>:<why>`, so a reader (or a test)
correlating a `MERGE_REFUSED`/`spec_unverified` row against CR 34's own
failure log finds the same task id and the same reason string in both
places.

Row 39 is why the merge step can tolerate a lost attempt without a meaningful
checkpoint. The pin makes a repeated merge either a no-op (row 34) or a refusal
(row 35). It can never be a second, different merge.

### 6a. `post-verdict`'s own failure modes

A separate, much shorter table: `post-verdict` is its own task, with its own
attempts and its own typed end causes. **Pinned here, round-3 re-review
(minor):** `EndCause.VERDICT_REFUSED` and `EndCause.VERDICT_FAILED`, the same
two-way split `MERGE_REFUSED`/`MERGE_FAILED` makes (§6) — a condition for
posting was not met and nothing was sent to the forge, versus the post was
allowed and the forge did not do it. Both are requested through the same
further, not-yet-filed contract request `post-verdict` itself needs (§10;
§1.3, §4.3), alongside `WorkerAction.POST_VERDICT` and the `post-verdict`
catalogue entry — naming the causes here now, rather than leaving them purely
implicit, is what "pin" means: the shape of that request is settled even
though it has not been filed.

| # | what happened | code | end cause |
|---|---|---|---|
| 1 | stale fencing generation | — | none: exits without touching the forge (invariant 5) |
| 2 | cancel requested before the review is posted | — | `CANCEL_REQUESTED` |
| 3 | the step ran past its timeout | — | `TIMEOUT` |
| 4 | no object exists at the computed verdicts-prefix path (§4.3), or it is not the schema §4.1 requires | `verdict_unreadable` | `INPUTS_UNAVAILABLE`, retryable only if the read itself failed transiently (not if the object is simply absent — `review` fails its own attempt first in that case) |
| 5 | the `review` task's signed spec does not verify (§0 consequence 4, §7 T14) | `spec_unverified` | `VERDICT_REFUSED` |
| 6 | the `-git-review` secret is missing, or this account cannot read it | — | `CANNOT_START` |
| 7 | the review App key was rejected, or the App is not installed on this repository | — | `CANNOT_START` |
| 8 | the verdicts prefix's bucket or path cannot be resolved at all (a deploy-time misconfiguration, not a per-request condition) | `verdict_prefix_unavailable` | `CANNOT_START` |
| 9 | the forge is unreachable or `5xx` | `forge_unavailable` | *retryable*, then `VERDICT_FAILED` |
| 10 | the forge rejects the review submission outright (a `4xx` other than rate-limiting) | `forge_refused` | `VERDICT_FAILED` |
| 11 | the POST succeeded | — | **SUCCEEDED**, the review's id recorded in `result_summary` |
| 12 | a retried attempt runs after a previous one already posted a review at this `commit_id` | — | **SUCCEEDED**, treated as already done — GitHub allows more than one review from the same App on a pull request, so a second `APPROVE` at the same commit is redundant, not harmful, and the merge step's read (§5.2a) already takes the *latest* one |

Row 5 is what makes #342 a real dependency rather than a cross-reference:
`post-verdict` will not carry a verdict forward from a `review` task whose
signed spec it cannot verify, whatever `review.json` says. **This row exists
only once #342 ships — before that, `post-verdict` cannot run at all, because
the merge step is not enabled for any tenant (§10's precondition). "#342 is
not built" is therefore never itself a runtime refusal reason; it is why
`post-verdict` has no attempts to fail in the first place.** Full parity with
`merge`'s own table (§6) — every refusal reason `merge` itself might need
from a malformed `review.json`, beyond the schema check in row 4 — is a
build-time detail this design leaves open rather than exhaustively
enumerates here; the shape above, plus the pinned end-cause pair, is enough
to show `post-verdict` is not exempt from typed outcomes merely because it
is simpler.

A merge step that fails behaves like any failed step under
`on_step_failure`. It is the sink, so there is nothing below it to cancel.

---

## 7. The threat model

The adversary is **an agent in an upstream step** of this workflow (implement,
review, fix or proof), or in another workflow of the same tenant. It has
everything §0 lists: code execution as the worker's uid in its own container,
the tenant's service-account token, and so the tenant's `-git` token, write
access to Firestore documents it can name, and write access to the tenant's GCS
prefix. It wants one of two things: **its pull request merged without a real
review**, or **the merge credential**.

| # | the attempt | what stops it |
|---|---|---|
| T1 | read `-git-merge` from Secret Manager with the tenant token | the secret's only accessor is `swarm-<tenant>-merge`; the tenant's account has no token-creator or `actAs` on it (§1.3) |
| T2 | leave a process behind that reads the merge worker's memory, or its secret | the merge step runs in its **own** execution, a different container, and no agent ever ran there. The reap (§2.2 step 4) and `PR_SET_DUMPABLE` are the second and third layers |
| T3 | forge `review.json` or the review's `result_summary` saying `MERGE` — **split into two sub-cases, corrected in the joint review with CR 34, 2026-09-29:** T3a, the `fix` (or `proof`) agent of **this** workflow overwriting the file; T3b, an agent of **another** workflow of the same tenant writing a fabricated verdict at this or another workflow's path. Neither is T14 (corrupting the prompt that produced the file) | **B1, corrected three times, 2026-09-29.** The merge step does not trust `review.json.verdict`; it requires an `APPROVED` PR review from the review App's `app_id`, submitted at `pinned` (§4.3, §5.2a). That review is posted by `post-verdict`, a worker-action step that runs **no agent at all** — not by a dedicated `claude-code-review` profile, which a second review found insufficient: an agent sharing a container with the App key can mint that container's own token regardless of which profile it runs on. `post-verdict`'s credential isolation (only its own service account reads `-git-review`) closes the App-key theft. **T3a is closed** by DAG ordering: `fix` is gated behind `post-verdict`, not beside `review`, so no later agent in *this* workflow can act before the verdict is already posted. **T3b is NOT closed.** The review-only-writable prefix closes only the *ordinary tenant account's* write path; the review identity itself, `swarm-<tenant>-review`, is shared by every `single-pr` workflow of the tenant, and a review agent of one workflow, compromised by prompt injection (T11), can write a fabricated verdict at another workflow's path within the same `tenants/<t>/verdicts/` subtree (§4.3). This is a named residual, **R8** — write-once (`ifGenerationMatch=0`) is a partial mitigation, not a close |
| T4 | the fix agent pushes after a `MERGE` verdict, so an unreviewed change rides along | `review_not_at_head`: the author's or amender's last `pushed_head` must be `pinned` |
| T5 | push to the branch after every step finished, using `-git` | `head_moved`: the live head is compared with `pinned`, and the merge call carries `sha`, so GitHub refuses a moved head atomically |
| T6 | land a change on `main` that widens the chain's own reach — a `.github/workflows/` edit the chain's own review waves through, or any change to `scripts/**`, `terraform/**`, `kubernetes/**`, `images/**`, `Makefile`, `pyproject.toml`, `uv.lock` or `**/conftest.py`, since **merging to `main` is already code execution as the deployer**: `release.yml`'s `id-token: write` is granted at **workflow** level, not job level, so every job in that run — including the `verify` job, which executes the merged repository's own tests — can mint a deploy-scoped OIDC token, whether or not that job is the one that needs it (M4) | the merge App has no `workflows` permission, and the step refuses first with `touches_protected_paths` (§5.1, §6), a broadened form of the original `.github/workflows/`-only check. Such a change lands only through a human's `ready`. **The underlying exposure — `id-token: write` at workflow level — is not closed by this design.** Moving that grant to job level, behind the environment gate, is recommended as its own change (§11 Q8), not built here |
| T7 | post a fake green check (a commit status or a check run with a required name) using `-git` | required checks are pinned to GitHub Actions' `app_id` (15368), and (M5) any required check with **no** `app_id` is refused outright rather than falling back to a legacy commit status. A status or check from any other token or App does not count (§5.2) |
| T8 | retitle the pull request after the review, to change the headline on `main` | `title_changed`: the live title must equal `review.json.title`. The subject is taken from the live pull request at the same read that pins the head |
| T9 | point the merge at a different pull request by rewriting the author's `result_summary.git.pull_request` | the live pull request's `head.ref` must be `swarm/<author task id>`, a name derived from the author's task id, and `head.sha` must be `pinned` (§4.2), both read against the control-plane `owner`/`repo` record (§2.1b, B2), never a repository name the task document supplies. Another pull request cannot satisfy both |
| T10 | rewrite the merge task's own dispatch block (`merges`), which is a Firestore document, to name different upstream tasks | the block is a claim like every other (§0). What holds is the chain of equalities: the merge lands only a pull request whose live head is `pinned`, and only when a **GitHub review object**, not a Firestore or GCS claim, says `APPROVED` at exactly that sha. Pointing the block elsewhere can at most land a head some genuinely-approved review passed. It cannot land the attacker's unreviewed head (§4.3) |
| T11 | prompt-inject the reviewer, with text in the diff that tells it to answer MERGE | **not stopped by this design.** The verdict is only as good as the reviewer model reading attacker-authored text; B1 makes the resulting `APPROVE` harder to *fabricate* without a real review step, but does nothing about a real review step reaching the wrong conclusion. What limits the damage is the rest of the gate: required checks green at the exact head, a proof at that head, no protected-path changes, and a human's standing ability to revert. This is residual risk **R1** |
| T12 | merge it directly with `-git`, bypassing the step completely | **narrowed by M1, not closed.** Ruleset `main-protection` (§0.3) removed the direct-push bypass `enforce_admins: false` used to allow: an administrator can no longer push straight past the required checks. What it did **not** yet restrict is *who* may use the ordinary PR-merge route — today that is still anyone with write access, which includes any identity holding `-git`. M2's second ruleset (restrict-updates, with a bypass list naming the Apps) is what would close that, and whether GitHub honours an App name in a bypass list on a personal repository is **not verified**; a dry run comes first (§8, §11 Q3). This residual is **R5** |
| T13 | an agent adds the `ready` label to its own pull request, which `auto-merge.yml` trusts as the human review decision | **not stopped by this design — an accepted risk (M3, owner decision 2026-09-29: keep `-git`'s PAT for agent pushes).** Any agent can read `-git` (§0) and so can call the label API directly, without needing the merge step at all. Two things limit what it buys: the label's effect is **head-bound** (`auto-merge.yml`'s `--match-head-commit`), so a later, un-reviewed push does not silently ride an old `ready`; and #219's ordering means the *credential a running agent could otherwise scrape from a worker's memory* is held only by the **worker**, and only after the agent has been reaped — which stops a memory-leak style theft of a token the agent does not already have, but does nothing for `-git`, which the agent can read directly regardless. This residual is **R6** |
| T14 | rewrite a downstream step's own `prompt`/`input`/`input_from` in its Firestore task document before that step starts — for example, `implement` rewrites the still-`PARKED` `review` task's `prompt` to bias its judgement, or rewrites what `post-verdict` or `merge` would read as their own effective instructions | **BLOCKER, round-3 re-review, 2026-09-29 — not stopped until #342 ships.** §0 already grants any tenant identity write access to any Firestore document whose id it knows; a step's own spec was never excepted. This is a different attack from T10 (rewriting the *merge task's* dispatch block) and from T11 (influencing the review agent through diff *content* it reads): T14 rewrites the *instructions* a later step is handed before that step ever starts, so the later step's own execution is completely honest and still wrong. **Owner decision: signed step specs (S0 issue #342).** swarm-api signs each step's canonical spec at submission with a KMS key only it holds; every worker verifies its own spec's signature before starting an agent or a worker action, and `post-verdict` and `merge` additionally verify the **upstream** specs the artifacts they trust depend on (`post-verdict` verifies `review`'s spec; `merge` verifies the union — `author`'s, `review`'s, `post-verdict`'s, `fix`'s and `proof`'s — §4.2, §6 row 42). This residual is **R7**, and it is the reason B1 is not fully closed by §4.3 alone (§0 consequence 4, §10) |

**Residual risks, stated plainly:**

* **R1.** T11, prompt injection of the review agent, so that the *real*
  review step, running as the *real* review identity, reaches the wrong
  conclusion and honestly writes `"verdict": "MERGE"` about a change it
  should not have approved. `post-verdict` carries whatever `review.json`
  says; it does not, and cannot, audit the judgement behind it. **This is not
  closed by B1's corrected design, and was never claimed to be** — B1 raises
  the bar for *fabricating* an approval without a real review step, not for a
  real one reaching the wrong answer.
* **R2. In-chain half only — closed, by design; cross-workflow half moved to
  R8, NOT closed — corrected 2026-09-29, three times, since each of two
  earlier revisions of this document overstated part of this as already
  true.** T3a — the `fix` agent, *this* workflow, forging `review.json` — is
  closed once `post-verdict` is built exactly as specified (§4.3): DAG
  ordering gates `fix` behind `post-verdict`, not beside `review`, so no
  later agent in this workflow can act before the verdict is already posted.
  This claim is conditional on the build, because the status here is still
  PROPOSED — nothing is built yet (§10). T3b — a different workflow's agent
  of the same tenant forging a verdict, since the review identity itself is
  tenant-wide, not workflow-scoped — is a **separate, named residual, R8**,
  below; it is not closed by the prefix, ordering, or anything else in this
  design. The first correction of this document asserted T3 closure while
  describing a mechanism (`claude-code-review`) that did not actually
  achieve it; the second closed only the in-chain half before the prefix was
  added and still called the whole thing closed; this, the third, is the
  first version to state the cross-workflow half honestly as open rather
  than folding it into a claim of full closure.
* **R3.** Proof has no App anchor (§4.3): `proof.json` is a plain staged
  artifact, checked only by sha equality. Unlike the verdict, there is no
  later agent in *this* chain to forge it — `proof` runs immediately before
  the no-agent `merge` step — so the only forger is a different tenant
  workflow racing the staging read with a Firestore- or GCS-write it can
  already make (§0). Accepted, not closed: building a `post-proof` step would
  close the race but would not change what "PROVED" means (§4.3), and the
  threat it would close is narrower than the one T3 named for the verdict.
* **R4. Accepted, not a precondition — decided, round-3 re-review, 2026-09-29
  (§11 open question (b), now resolved).** T6 restated (M4): a chain's pull
  request that lands on `main` is code execution as the deployer, because
  `release.yml`'s `id-token: write` is a workflow-level grant and its
  `verify` job runs the merged tests. The broadened `touches_protected_paths`
  refusal keeps a chain from landing the paths most likely to matter (CI
  itself, Terraform, Kubernetes, images, the build and test tooling) without
  a human's `ready`, but it explicitly does **not** cover `tests/**` or the
  rest of `apps/**` — squarely inside what a chain is *for* — so a chain
  whose pull request only touches those paths can still land a test change
  that runs under the unnarrowed, workflow-level OIDC grant, before `dev`'s
  auto-promotion even reaches the environment gate. The reviewer's
  recommendation was to make moving `id-token: write` to job level a
  **precondition** for enabling the merge step. **The owner decided
  otherwise: it is not a precondition.** The merge step may be enabled before
  that change lands, and this exposure — a chain's own in-scope,
  non-malicious pull request exercising an unnarrowed deploy-token grant
  through `apps/**`/`tests/**` — is an accepted residual, not a blocker.
  Moving `id-token: write` to job level, behind the environment gate, still
  closes it and is recommended as its own change (§11 Q8), just not a gate on
  this one.
* **R5.** T12: today, any identity with write access — including `-git` — can
  still use the ordinary PR-merge route on `main`, even though `main-protection`
  (M1) has closed the direct-push bypass. M2's restrict-updates ruleset, with
  an App-named bypass list, is the proposed close; it needs a dry run first,
  since naming an App in a personal repository's ruleset bypass list is
  unverified (§8, §11 Q3).
* **R6.** T13: an agent can label its own pull request `ready`, since it can
  read `-git` directly. Accepted per M3. Mitigated, not prevented, by the
  label's head-binding and by #219 keeping the credential out of a process an
  agent could scrape after the fact — neither mitigation stops the agent's own,
  direct read of `-git` while it is still running.
* **R7. BLOCKER, open until #342 ships.** T14: any tenant identity can
  rewrite a downstream step's own `prompt`/`input`/`input_from` in Firestore
  while that step is still `PARKED`, so a step can execute honestly against
  dishonest instructions. This is not a variant of R2 — R2 is about
  `review.json` (the review's **output**) being overwritten after the fact;
  R7 is about the review's **input** being corrupted before it ever runs, a
  window neither `post-verdict`'s isolation, the review-only-writable
  prefix, nor DAG ordering touches, because all three assume the step that
  produced the artifact was honestly instructed. Signed step specs (#342)
  close it. **This design depends on #342, and B1 is closed only once #342
  is built — not merely once `post-verdict` and the prefix are (§0
  consequence 4, §10).**
* **R8. NAMED RESIDUAL, not closed — the owner did not choose to bind a
  review to a specific pull request or task id (joint review with CR 34,
  2026-09-29).** T3b: `swarm-<tenant>-review` is one identity shared by
  every `single-pr` workflow of the tenant, and its GCS write grant covers
  the whole `tenants/<t>/verdicts/` subtree, not one workflow's slice. A
  review agent of workflow A, compromised by prompt injection (T11), can
  write a fabricated `review.json` at workflow B's path if it is told (or
  can derive) workflow B's identifiers, and workflow B's own `post-verdict`
  has no way to distinguish that object from a genuine one — the write
  identity is shared, so only the path indicates provenance, and an
  attacker who has the ids can compute the same path. The minted
  `-git-review` installation token compounds this: it is a portable bearer
  credential authorising `pull_requests: write` on the whole repository, not
  one PR, so an anchored review proves "the App approved this commit," not
  "the App approved it on behalf of this workflow." Write-once
  (`ifGenerationMatch=0`) on the review's publish is a **partial**
  mitigation (§4.3): it turns a successful silent overwrite into a loud,
  attributable write-conflict failure on whichever side loses the race, but
  does not prevent the race or guarantee the honest side wins it. Closing
  this fully would need per-workflow write scoping or PR/task-id binding on
  the review itself — considered, and explicitly not chosen.

---

## 8. Coexistence with `auto-merge.yml`

**Two rulesets on `main` are the authority** on whether anything may land, not
the single classic branch-protection record the original draft described
(M1, M2). Neither merge path can bypass either one: both merge through
GitHub's merge endpoint as an App, and an App is not an administrator and
cannot be named an owner-level bypass.

* **`main-protection` (id `24160219`, created 2026-09-29).** Requires a pull
  request and the four `security.yml` checks, blocks force-push and branch
  deletion, and lets an administrator bypass **only by merging a pull
  request** — the direct-push bypass `enforce_admins: false` used to allow is
  gone. **No App is on its bypass list**; the owner's own break-glass path is
  the only bypass, and it still goes through a PR merge, not a direct push
  (§0.3, §7 T12).
* **`restrict-updates` (M2, proposed here, not yet created).** A **second**,
  narrower ruleset, whose job is to say which identities may push an update to
  `main` **at all** — as distinct from "which checks must be green," which
  `main-protection` already covers. Its bypass list is what would name the
  Apps: the tenant's `swarm-<tenant>-merge` App and `auto-merge.yml`'s own App,
  so that a squash merge from either path is itself the only way an update
  reaches `main`. **Whether GitHub honours an App in a ruleset bypass list on a
  personal repository — `bogdan-alexandrescu/SwarmCloud` is not an
  organisation — is not verified.** A dry run against a disposable ruleset
  (bypass list naming one App, confirm the App's own merge succeeds and a
  plain PAT push is blocked) is the precondition for creating this ruleset for
  real; §11 Q3 tracks it, and §7 T12/R5 is the residual risk while it is
  outstanding.
* An always-run `ci-gate` job is coming, per M1, which will let a
  required-checks rule cover every pull request rather than only the four
  checks that report on every one today. This design does not depend on it.

Below that authority, the two merge paths answer different questions, and the
design keeps them apart:

* **`auto-merge.yml` is authoritative for a pull request a person labels
  `ready`.** **This is no longer unconditionally "the human review decision"
  (M3 corrects the original draft's claim here).** The owner has kept `-git`'s
  PAT for agent pushes, and any agent can read it (§0), so an agent can call
  the label API itself — `ready` is the label a person is *expected* to set,
  not one only a person *can* set. §7 T13/R6 records this as an accepted risk:
  the label stays head-bound, which limits what riding it buys, and the merge
  credential itself is unaffected either way, since `ready` only ever drives
  `auto-merge.yml`'s path, never this one.
* **The merge step is authoritative for a `single-pr` chain's pull request.** It
  merges directly, in the same squash form with the same subject. It never
  adds `ready` and never enables auto-merge. It does not need to: it does not
  wait for anything.

If both act on one pull request (a person, or an agent per T13, adds `ready`
to a chain's pull request while the chain is running), the first merge wins
and the other path finds it merged. `auto-merge.yml`'s `gh pr merge` then
fails harmlessly on a merged pull request. The merge step ends SUCCEEDED with
`merged_by_this_task: false` when the head was `pinned` (row 34), or
`merged_at_other_head` when it was not. Neither path can merge a head the other
did not intend, because both pin the head: `--match-head-commit` there, `sha`
here.

**One rule, two statements.** The merge step restates `auto-merge.yml`'s gates:
1 (the placeholder title), 3 (required checks exist) and 5 (no failing or
running check). CLAUDE.md's warning applies, since every rule restated here has
drifted. So the build must include a parity test in the style of
`test_auto_merge_workflow.py`. It should read the gate's shell and the merge
step's Python from one table of cases (title prefixes, conclusions,
pending states) and fail when they answer differently. Where they **deliberately**
differ, the merge step is the stricter one and the table says why: a required
check must be `success` or `skipped` (the owner's rule). Gate 5 holds the merge
only on `failure`, `timed_out`, `action_required` and `cancelled`, and tolerates
every other conclusion. The merge step also refuses `touches_protected_paths`,
a broader set than gate 1's placeholder-title check covers at all (M4) — the
two gates are not required to agree here, since `auto-merge.yml` is the human
path and this design deliberately holds the unattended one to a stricter bar.

Read while writing this: gate 5 calls `gh api .../commits/<sha>/check-runs`
without `--paginate`, so it sees only the first page. The API's default page
size is 30. A head with more than 30 check runs would have the rest ignored by
the gate. The merge step paginates (§5). This is a mechanical fix to
`auto-merge.yml` and is out of scope here.

Both merge paths use an App token, so the push to `main` starts
`application.yml` and `release.yml` exactly as a human merge does.

---

## 9. When `main` moved after the proof — recommendation: merge if GitHub says it merges cleanly; never update the branch

The two options the owner named:

* **Update the branch and re-run.** `PUT /pulls/{n}/update-branch` makes a new
  head. That head is not `pinned`, so neither the review nor the proof ever saw
  it. By the owner's own rule it cannot be merged without a new review and a
  new proof, and a merge step cannot run those: a workflow's DAG is fixed at
  submission. Updating would also make the step **write to the branch** with
  the merge credential, a second capability it otherwise never needs. So
  "update and re-run" really means "fail, and a person dispatches a new review →
  proof → merge". That is the same as refusing, with an extra push.
* **Refuse when `main` moved at all.** This matches `strict: true`, and `main`
  is protected with `strict: false` on purpose ([ci.md](ci.md): auto-merge
  never updates a branch). On a repository where `main` moves several times a
  day, this would refuse most merges for a reason the authoritative gate does
  not recognise.

**Recommended:** merge when `mergeable == true` at `pinned` and every check is
green. Refuse on `conflict`. Never update the branch. This is exactly the policy
`auto-merge.yml` and branch protection already apply to every human-labelled
pull request, so a chain's pull request is held to the same bar as a person's,
no lower and no higher. The risk it accepts is the one `strict: false` already
accepts: a semantic conflict between the pull request and a newer commit on
`main` that neither touches textually. That is caught by the build of `main`
after the merge, not before it. Q4 offers the stricter option.

`main` moving is also exactly the case the `base.ref` re-checks (§2.2 step 8,
§5.1, §5.3) exist for: `main` moving is a change to which **commits** sit
ahead of the pull request, not to which **branch** the pull request targets,
so those two checks and this section answer different questions. A `main`
that moved past the proof is read here, through `mergeable`; a pull request
whose `base.ref` itself changed — retargeted to a different branch entirely,
protected or not — is refused by the pre- and post-merge `base.ref` checks
regardless of whether `main` also moved.

---

## 10. What has to be built, and in what order

None of it is built. Each item is its own pull request, and the order is the
dependency order.

**Preconditions:**

* **Already satisfied:** `main-protection` (M1) exists (§0.3) — the merge
  step must not be enabled before some form of branch protection is in
  place, and now it is.
* **Not yet satisfied, and BLOCKING (round-3 re-review, 2026-09-29): #342,
  signed step specs (S0).** B1 is not closed — not "closed pending a build
  item," genuinely open — until swarm-api signs every step's canonical spec
  and every worker (including `post-verdict` and `merge`, verifying their
  upstream specs) checks it before running. §0 consequence 4 and §7 T14/R7
  say why: without it, an `implement` agent can rewrite `review`'s prompt
  before `review` ever starts, and every mechanism below this line protects
  an artifact's *integrity after it is written*, not the *honesty of the
  instructions* that produced it. **The merge step must not be enabled for
  any tenant until #342 ships**, independent of and in addition to
  `main-protection`.
* **NOT a precondition (§11 open question (b), resolved; §7 R4):** moving
  `release.yml`'s `id-token: write` to job level. The owner decided this
  explicitly; item 9 below is real work, recommended, and not gating.

1. **Contract request 33 decided** (the owner): `worker_action`, the `merge`
   profile, `MERGE_REFUSED` and `MERGE_FAILED`.
2. **A second, not-yet-filed contract request decided** (the owner) — a
   pointer to it belongs in contract request 33 itself (minor, round-3
   re-review): the `post-verdict` catalogue entry, its own `WorkerAction`
   value, and its pinned end causes `VERDICT_REFUSED`/`VERDICT_FAILED`
   (§1.3, §4.3, §6a) — a worker-action profile structured exactly like
   `merge`: `runner_argv=()`, its own Job, its own service account, no agent
   ever runs there. **This gates B1's App-key guarantee**, not just an
   optimisation: without it there is nothing to hold the `-git-review`
   secret that is not also holding an agent (§1.3, §4.3). There is no
   equivalent App-holding request for proof — §4.3 explains why none is
   needed.
3. **A third, not-yet-filed request, or an amendment to the frozen catalogue
   the same way `post-verdict`'s is:** the `claude-code-review` catalogue
   entry (§1.3) — identical to `claude-code` except its own Job and service
   account, `swarm-<tenant>-review`, holding no App key, only the
   review-only-writable prefix's write grant.
4. **Terraform (Track C):** the per-tenant `review`, `post-verdict` and merge
   service accounts, each with its own narrowed grants (§1.3) — three Jobs,
   three service accounts. **The review-only-writable prefix's IAM split
   (§4.3, corrected layout, joint review with CR 34):** replace
   `register-tenant.sh:961`'s single conditioned `worker_objects` binding
   (currently `roles/storage.objectUser` on all of `tenants/<t>/`) with two —
   `roles/storage.objectViewer` on `tenants/<t>/` unconditioned, and
   `roles/storage.objectUser` on `tenants/<t>/` conditioned to exclude
   `tenants/<t>/verdicts/` — and grant the review SA its own
   `roles/storage.objectUser` **plus** `storage.objects.delete` scoped to
   `tenants/<t>/verdicts/` alone (delete is needed for a legitimate retry to
   replace its own prior write under write-once semantics, §4.3). Requires
   Uniform bucket-level access, already implied by the existing conditioned
   binding. The Terraform-rendered `host`/`owner`/`repo`/`review_app_id`/
   `review_app_bot_id` Job environment values for `post-verdict` and `merge`
   (§2.1b, §11 open question (a), resolved). Also the `terraform test`
   assertion that no tenant identity holds `run.jobs.run`,
   `run.jobs.runWithOverrides` or `run.jobs.update` on the merge Job (§1.3),
   and its counterpart for the `review` and `post-verdict` Jobs.
5. **Scripts (Track D):** `register-tenant.sh --add-provider git-merge` and
   `--add-provider git-review` both refuse to bind the tenant's ordinary
   worker account; separate binding paths grant the `review` and
   `post-verdict` accounts accessor on their own secret alone. The
   `git-review` registration path also resolves `review_app_bot_id` using
   the App's own freshly-minted JWT, and emits it into the tenant's
   Terraform variables for the next `terraform apply` (§2.1b, §5.2a), rather
   than writing it anywhere at runtime. **`register-tenant.sh:961`'s own
   `worker_objects` binding gets the same split as item 4** — the tenant
   worker account's grant changes from unconditioned `objectUser` on
   `tenants/<t>/` to the `objectViewer`-plus-excluding-`objectUser` pair, at
   the same call site that grants it today, so the script and whatever
   Terraform module it invokes do not drift from each other the way
   CLAUDE.md's own warning about restated rules describes.
6. **swarm-api (Track A):** the `single-pr` strategy, `pr_role`, the
   `merges` block, the submission refusals in §3 (including the
   `post-verdict` placement and the ordering-only `depends_on` edges it and
   `fix` need), and — the part that gates B1, not merely completes it —
   **#342's signature verification path**, called by every worker before it
   starts an agent or a worker action, with `post-verdict` and `merge` also
   verifying the upstream specs they depend on.
7. **Worker (Track B):** the author's `pr-title.txt` title, the reader and
   amender roles, `clone_commit`, `review`'s publish to the review-only-
   writable prefix (§4.3), `post-verdict`'s review submission and its own
   spec verification (§4.3, §6a), and the merge action in §2.2 and §5 —
   including the pinned-host, no-redirect forge client (§2.1b), the
   `/rules/branches/{branch}` read (§5.2), and the pagination cap (§5) —
   with the parity test (§8).
8. **The owner, once:** create the review App and the merge App (one each,
   per tenant — §1.3; no proof App), install them on the tenant's
   repositories, store their keys with `create-secrets.sh --stdin`, run the
   `restrict-updates` ruleset dry run (§8, §11 Q3), and decide Q3 and Q4.
9. **Its own change, not part of this design, and — per the owner's round-3
   decision — not a precondition for enabling the merge step (§7 R4, §11
   open question (b)):** move `release.yml`'s `id-token: write` from
   workflow level to job level, behind the environment gate.

The tests each item owes are the ones the refusal table already names: one per
row, each proved red first in CI.

---

## 11. Open questions for the owner

Each has a recommendation. None of them is decided here.

* **Q1. What the chain does when the review says CHANGES — corrected, joint
  review with CR 34, 2026-09-29: the originally recommended follow-on
  workflow does not work, and no replacement is recommended in its place
  yet.** With the pin, the six-step chain (implement → review →
  `post-verdict` → fix → proof → merge) merges only when the review said
  `MERGE` **and** the fix step changed nothing. When the fix does change
  something, the merge refuses (`review_not_at_head`), and the pull request
  stays open with the fix pushed and unreviewed. The original
  recommendation was to treat that refusal as the designed outcome and
  follow it with a **second workflow** of review → `post-verdict` → proof →
  merge against the same, still-open pull request, with the API accepting
  an existing pull request as the author. **That does not work once #342
  ships: CR 34's signed specs bind every step's spec to the `workflow_id` it
  was signed under, so a second workflow's `post-verdict` and `merge` cannot
  verify a spec — the first workflow's `author` — signed for a different
  `workflow_id` (§0 consequence 4, §7 T14/R7).** The follow-on-workflow
  design was written before that binding was decided, and CR 34's guarantee
  is exactly what makes it infeasible: a signature that could be presented
  across workflow boundaries would not be binding anyone to anything. The
  original alternative — a six-step chain within **one** workflow
  (implement → review → `post-verdict` → fix → review → `post-verdict` →
  proof → merge, an eight-step chain once `post-verdict` is counted
  explicitly) — still works, at the cost of a second review every time, even
  when the first said `MERGE`. Which of these the owner wants is still
  open; this correction only removes the option that cannot be built, it
  does not pick between what remains.
* **Q2. RESOLVED, by B1, not merely answered — corrected 2026-09-29.** The
  original question was whether to build a control-plane attestation over the
  staged `review.json`. The owner's decision replaces that mechanism
  entirely: the verdict is a GitHub PR review, posted by `post-verdict`, a
  no-agent worker-action step and the sole accessor of `-git-review` (§4.3).
  A first attempt at this answer (a dedicated `claude-code-review` profile)
  was found insufficient on re-review, because it still ran the review agent
  in the same container as the key; the corrected answer is a step with no
  agent at all, the same shape as `merge` itself. No scheduler change and no
  attestation bucket are needed; §10's Terraform item is `post-verdict`'s own
  service account and Job, not a bucket. There is no equivalent for proof — no
  attestation was ever proposed for it, and B1 does not add one (§4.3).
* **Q3. Restrict who may use the ordinary PR-merge route into `main`.**
  `main-protection` (M1) already closed the direct-push bypass an
  administrator's PAT used to have. What is still open is that any identity
  with write access — including `-git` — can merge a pull request the
  ordinary way once checks are green (§7 T12/R5). *Recommendation:* create
  `restrict-updates` (M2, §8), a second ruleset whose bypass list names only
  the merge Apps (the chain's, per tenant, and `auto-merge.yml`'s), after a
  dry run confirms GitHub honours an App name in a bypass list on a personal
  repository — this is **not verified**, and the dry run is the way to verify
  it before depending on it (§10 item 8).
* **Q4. When `main` moved (§9).** *Recommendation:* merge if it is cleanly
  mergeable and never update the branch, the same bar as `auto-merge.yml`. The
  alternative is refusing whenever `main` moved past the proof's base, which
  would need the proof worker to record that base.
* **Q5. The credential kind (§2.1).** *Recommendation:* a GitHub App,
  separate from the `auto-merge.yml` App, with `contents` and `pull_requests`
  write, `checks` and `statuses` read, and **no** `workflows`. The secret holds
  `{app_id, private_key}`. A fine-grained PAT is the alternative, and it gives
  up the distinct identity that Q3 needs. The same shape now applies to the
  review App, held by `post-verdict` (`pull_requests: write` only), its own
  installation per tenant (§1.3). There is no proof App (§4.3).
* **Q6. Should the proof carry an outcome of its own?** A `claude-code` proof
  step succeeds when its agent exits cleanly, whether or not the proof held.
  *Recommendation:* yes — `proof.json` carries `"outcome": "PROVED"` (schema
  checked at row 8, value checked at row 11), exactly as originally answered.
  **Corrected 2026-09-29: this stays a plain staged-artifact check, not an
  App-anchored one.** An earlier revision of this design added a
  `swarm-proof` App and check run to answer this the same way the verdict is
  answered; that mechanism is withdrawn (§4.3): the proof agent executes the
  pull request's own code, so any App key in its container would be at least
  as exposed as the review App key was before B1's correction, and — unlike
  the verdict — there is no later agent in this chain for an anchor to
  protect against. State plainly what "PROVED" can and cannot mean, App
  anchor or not: **a proof step that executes pull-request code and reports
  its own outcome anchors only that the code ran and produced that
  outcome — never that the change is safe.** The required GitHub Actions
  checks (§5.2) are the actual safety gate.
* **Q7. A pending check.** The owner's rule makes it a refusal, and invariant 4
  forbids waiting for it. *Recommendation:* refuse (`checks_pending`), and do
  not add a new `ParkReason` for "waiting on CI" now. A chain whose proof step
  runs after CI has reported (for example because the proof reads the checks)
  rarely meets this. A park reason would be a second frozen change for a case
  not yet observed.
* **Q8. Pull requests that touch protected paths — restated per M4.** The
  original question was `.github/workflows/` alone. A security review found
  the actual exposure is broader: merging to `main` is code execution as the
  deployer, because `release.yml` grants `id-token: write` at workflow level
  and its `verify` job runs the merged repository's own tests, so *any* change
  to the tooling that job trusts — not only a workflow file — reaches that
  same execution surface. *Recommendation, both parts:* refuse the chain on a
  pull request touching `.github/**`, `scripts/**`, `terraform/**`,
  `kubernetes/**`, `images/**`, `Makefile`, `pyproject.toml`, `uv.lock` or any
  `**/conftest.py` (T6, `touches_protected_paths`), so such a change lands
  only through a human's `ready`; and, as a change of its own and not part of
  this design, move `release.yml`'s `id-token: write` to job level behind the
  environment gate, which is what actually closes the exposure rather than
  routing around it. The alternative this design rejects, as before: granting
  the merge App `workflows`, which gives an unattended chain the ability to
  rewrite CI and release directly.
* **Open question (a). RESOLVED, round-3 re-review, 2026-09-29.** Where the
  host/owner/repo/App-id record lives (§2.1b, §5.2a, §6 row 14,
  `forge_host_invalid`) was open pending the owner; it no longer is. **The
  owner chose Terraform-rendered Job environment values for both
  `post-verdict` and `merge`** — the reviewer's other option, the
  `-git-merge` secret's own JSON, was not chosen. §2.1b has the full
  specification: which of the five values (`host`, `owner`, `repo`,
  `review_app_id`, `review_app_bot_id`) each Job needs, and how
  `review_app_bot_id` — the one value that needs a live GitHub call to
  resolve — reaches Terraform through `register-tenant.sh`'s own
  registration-time read rather than being computed by Terraform itself.
* **Open question (b). RESOLVED, round-3 re-review, 2026-09-29 — the owner
  decided AGAINST the reviewer's recommendation.** Whether moving
  `release.yml`'s `id-token: write` to job level is a precondition for
  enabling the merge step (§7 R4, §11 Q8, §10) is no longer open: **it is
  not a precondition.** The reviewer's reasoning stands as the analysis of
  the exposure, recorded in full at §7 R4: `touches_protected_paths` does
  not cover `tests/**` or the rest of `apps/**`, so a chain's own ordinary
  pull request can still exercise the unnarrowed, workflow-level OIDC grant
  before the environment gate applies. The owner's decision accepts that as
  a residual, not a blocker — the merge step may be enabled before job-level
  `id-token: write` lands. Moving it remains recommended (Q8, §10 item 9),
  just not gating.
