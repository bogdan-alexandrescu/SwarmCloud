# The `merge` step: a workflow that merges its own pull request

**Status: PROPOSED, 2026-09-29, for #295.** Nothing described here is built.
This is the design the owner asked for before any code: how a SwarmCloud
workflow ends in a squash merge made by the **worker**, with a credential no
agent can reach. The frozen-contract half is contract request 33 in
[contract-change-requests.md](contract-change-requests.md); the example spec is
in [workflows.md](workflows.md#proposed-a-chain-that-merges-its-own-pull-request).

**Revised 2026-09-29** against a security review's two blockers and five
majors, owner decisions dated the same day. Still nothing here is built, and
the status stays PROPOSED. What changed, in one line each, with the section
that carries the detail:

* **B1.** The verdict is no longer a control-plane attestation over a staged
  artifact. It is a GitHub PR review, `APPROVE` or `REQUEST_CHANGES`, posted by
  a `swarm-review` App whose key only the review step's own service account
  can read, pinned to the exact head (§4.3). The proof gets the same anchor: a
  check run from a `swarm-proof` App (§4.3, §5.2).
* **B2.** The merge action is pinned to a single forge host — `api.github.com`,
  or a per-tenant host the tenant cannot write — and follows no redirect on a
  credentialed request. `owner/repo` come from a control-plane record, never
  from the task document (§0, §2.2, §5).
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
  set of paths than `.github/workflows/` alone (§5.1, §6, §7 T6); moving that
  permission to job level is a recommendation for its own change, not built
  here (§7, §11 Q8).
* **M5.** A required check with no `app_id` is refused, never satisfied by a
  legacy commit status (§5.2, §6).
* Minors: worker-action profiles skip checkpoint restore (§1.3); the base ref
  is re-checked immediately before and again after the merge call (§5.1, §5.3,
  §9); the forge client never auto-follows a redirect (§2.2, §5); the
  `pulls/{n}/files` read is paginated to GitHub's own 3000-file cap and refuses
  past it (§5.1, §6); no tenant identity holds `run.jobs.run`,
  `run.jobs.runWithOverrides` or `run.jobs.update` on the merge Job, which gets
  a `terraform test` assertion (§1.3, §10); the merge service account's
  Firestore role is narrowed to what the step reads and writes, not the
  worker's general role (§1.3); and each tenant gets its own review, proof and
  merge Apps — never one App shared across tenants (§1.3, §2.1, invariant 9).

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
   asked to confirm a claim from the artifact alone — the verdict and the
   outcome — the claim is anchored to an object only a non-tenant identity can
   create: a PR review from the review App, a check run from the proof App
   (§4.3, B1), not a record the control plane writes on the tenant's behalf.
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
   §7 T12/R4 and §11 Q3 track that residual exposure and its own follow-on
   ruleset (M2/§8).

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
    merge"), not the checkpoint.
* `EndCause.MERGE_REFUSED` and `EndCause.MERGE_FAILED` (§6).

What this needs **outside** the frozen contract (none of it is built; each is
its own change, listed in §10):

* a service account per tenant for the merge Job (Terraform, labelled
  `managed-by=swarm-terraform`). Its grants are a **narrowed** Firestore role —
  read on its own task, attempt and workflow documents, and write only to its
  own task's `result_summary` and `control.finish` fields, not the general
  worker role every agent-running Job gets — a read of the tenant's GCS prefix,
  and accessor on `swarm-tenant-<tenant>-git-merge` **alone**. Nothing grants
  the tenant's worker account `iam.serviceAccountTokenCreator` or `actAs` on
  it. **No tenant identity — not the tenant's worker account, not another
  tenant's merge account — holds `run.jobs.run`, `run.jobs.runWithOverrides` or
  `run.jobs.update` on the merge Job itself.** Only the platform's own deploy
  identity does; a tenant that could invoke or override another tenant's merge
  Job would defeat the whole point of a separate identity. This gets a
  `terraform test` assertion (§10) the way the destroy-guard and plan-guard
  already do, so a future grant that widens it fails CI rather than being
  noticed in an audit;
* `register-tenant.sh --add-provider git-merge` must **refuse**. Its job is to
  bind the tenant's worker account, and for this one provider that binding is
  the hole §0 describes;
* **one review App, one proof App and one merge App per tenant — never one
  App shared across tenants.** Invariant 9 is per-tenant isolation: own GSA,
  own secrets, own GCS prefix, own namespace. A shared review or merge App
  would put every tenant's verdict-signing or merge-signing key behind one
  installation, so a compromise of one tenant's Secret Manager access (§0)
  would let it forge or merge for every other tenant too. `swarm-tenant-<tenant>-git-review`,
  `swarm-tenant-<tenant>-git-proof` and `swarm-tenant-<tenant>-git-merge` are
  three distinct secrets per tenant, each readable by exactly the step whose
  identity needs it and no other (§2.1, §4.3);
* **the review and proof steps also need an identity distinct from
  `claude-code`'s ordinary tenant service account — this is a consequence of
  B1 that §1.1's own argument already proves, and the design flags it rather
  than gloss over it.** §1.1 rejected making the merge a strategy tacked onto
  the proof step's own execution because that execution's worker and its
  agent share one Cloud Run Job identity, so granting the worker phase access
  to the merge credential grants it to the agent phase too, and — since every
  task on a profile shares that profile's one Job — to every other task on
  that profile as well. **The identical argument applies to §4.3's "the
  review step's worker reads `-git-review`" if the review step runs on the
  plain `claude-code` profile**: that profile's Job and service account are
  shared with `implement`, `fix` and `proof` (and every other tenant
  workflow's `claude-code` steps), so granting it `-git-review` access grants
  it to the fix and proof agents too — exactly the forgery §4.3 exists to
  stop. The review step's *own* agent minting that access is not the
  problem (it is already the trusted judge, T11's residual risk); a
  *different* step minting it is. Closing this the same way `merge` was
  closed — its own profile, `claude-code-review`, otherwise identical to
  `claude-code` (same image, same `runner_argv`) but bound to its own Job and
  its own service account, the sole accessor of `-git-review` — is the
  recommended shape, and `claude-code-proof` the same way for `-git-proof`.
  **This needs its own frozen-contract request, separate from request 33**
  (which is scoped to the `merge` profile alone, per its own heading): two
  more catalogue entries reusing `RunnerProfile`'s existing fields, no new
  type or field required. It is not built here, and the merge step must not
  be enabled until it is — a review or proof step run on the plain
  `claude-code` profile in the meantime gives up exactly the guarantee B1
  is for;
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
host as recorded in the tenant's **control-plane** registration
(`register-tenant.sh`'s own record, not a field the tenant can write through
the API or through a task's `input`). A task document can carry a
`repository_url`, and §0 already treats everything a tenant identity writes as
a claim, so the merge worker never reads a host, an owner or a repo name out
of the task, the workflow document or `result_summary.git`. It reads them from
the tenant's control-plane record and refuses to start (`CANNOT_START`) if
that record is missing or its host does not match the pinned form
(`api.github.com`, or the registered Enterprise Server host exactly). This is
what stops a forged `repository_url` — for example one pointing at a
look-alike host that would happily accept the installation token and hand back
a fabricated "green" response — from ever being asked anything.

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
| `reader` | the author's branch, `swarm/<author task id>` | **no git push**, whatever the strategy. The review step separately submits a PR review and the proof step a check run, both through their own Apps, neither of which is a git operation (§4.3) | review, proof |
| `amender` | the author's branch | fast-forward pushes to the **author's** branch and opens nothing | fix |
| (none) | nothing | the merge (§5) | merge (profile `merge`) |

The API refuses a `single-pr` workflow unless all of the following hold:

* exactly one `author`, and it is an ancestor of every other step;
* exactly one step on the `merge` profile, and it is the workflow's only sink;
* the merge step's `input_from` names the review's `review.json` and the
  proof's `proof.json`, and so it depends on both directly. `validate_dag`
  already requires every `input_from` source to be a direct dependency.

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

They are staged the ordinary way. The step declares
`"input_from": {"review": "review.json", "proof": "proof.json"}`, and swarm-api
records `metadata.expected_outputs` on the two upstream tasks. So a review that
does not write its verdict fails **its own** attempt, retryably, and the merge
never starts ([workflows.md](workflows.md#artifacts-pass-by-reference)). A
missing file at staging is `INPUTS_UNAVAILABLE`, as for any step. The names are
distinct, as `validate_staged_filenames` requires.

**These two files remain the human-readable record — the summary and the
evidence — but neither one is the trust anchor any more.** §4.3 anchors the
verdict and the outcome outside GCS and outside Firestore entirely, in objects
only the review step's and the proof step's own Apps can create. The merge
worker still reads `review.json` and `proof.json` (their `sha` fields feed
§4.2's equality chain, and their `summary`/`evidence` fields go into the
provenance comment, §5.4), but it treats their `verdict` and `outcome` fields
as descriptive, not authoritative: the authoritative verdict is the GitHub
review object, and the authoritative outcome is the check run.

**The pull request number and the pinned sha are NOT staged artifacts.** They
are facts only a worker knows: which commit it cloned and what it pushed. They
are read from the upstream tasks' `result_summary.git`, by the task ids that the
merge step's dispatch block names (`merges: {author, review, fix, proof}`).
Two reasons:

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
* the `commit_id` on the GitHub review the merge requires (§4.3), and the
  `head_sha` on the check run the merge requires (§4.3).

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

### 4.3 The verdict and the outcome are anchored outside the tenant, on GitHub itself — B1

The fix and proof agents both run **after** the review and **before** the
merge. Either one can mint the tenant token (§0), learn the review task's id
from the workflow document, and overwrite `review.json` in GCS or
`result_summary` in Firestore with `"verdict": "MERGE"`. The sha checks alone
do not catch this, because the forger writes the right sha. The earlier draft
of this design proposed a scheduler-written attestation over the staged
artifact to close that window. **The owner's decision, 2026-09-29 (B1): do not
add a control-plane attestation. Anchor the verdict with a GitHub PR review
made by a review App, and anchor the outcome with a check run made by a proof
App — objects GitHub itself keeps, that only the review and proof steps' own
identities can create, and that the merge step reads back from GitHub rather
than from anything a tenant identity wrote.**

**The verdict: a PR review from `swarm-review`.** The review step's *worker*
— not its agent, after the agent's judgement is in `review.json` — reads the
tenant's `swarm-tenant-<tenant>-git-review` secret (`{app_id, private_key}`,
stored and minted exactly as §2 describes for the merge App), mints an
installation token scoped to `pull_requests: write` on this repository only,
and submits:

```
POST /repos/{owner}/{repo}/pulls/{n}/reviews
{
  "commit_id": "<pinned>",
  "event": "APPROVE" | "REQUEST_CHANGES",
  "body": "<review.json.summary>"
}
```

`event` is `APPROVE` exactly when `review.json.verdict == "MERGE"`, and
`REQUEST_CHANGES` otherwise — the review agent still decides; the worker only
carries that decision onto GitHub, over the same host-pinned, no-redirect
forge client every other call in this design uses (§2.1b), and it revokes the
token in a `finally`, the same #219 ordering §2.2 gives the merge credential.
`commit_id` pins the review to `pinned` the same way `sha` pins the merge
call; a review submitted against any other commit does not satisfy §4.2's
equality chain.

**Only the review step's own service account can read `-git-review` — and
that requires the review step to run on its own profile, not plain
`claude-code` (§1.3 flags this as a build item, not yet a decided one).**
`-git-review` is a fourth per-tenant secret, alongside `-git`, `-git-merge`
and (below) `-git-proof`, each readable by exactly the identity that needs it.
The fix and proof agents that run after the review must not be able to read
it — they are not the review step — so neither one can mint a token to post
a fabricated `APPROVE`. That separation holds only if the review step's Job
and service account are not shared with any other step's, which is exactly
what §1.3's flagged gap is about; assuming it is built, this is the same
isolation §0 and §1.3 give the merge credential itself, applied one step
earlier in the chain, and it is why B1 closes the window the attestation only
narrowed: forging the verdict now requires the same class of access the
design already treats as the thing worth protecting, not a Firestore or GCS
write any tenant identity already has.

**The merge step's requirement:**
`GET /repos/{owner}/{repo}/pulls/{n}/reviews` (paginated to the end), filtered
to reviews whose `user.id` (the App's bot user, read once from
`GET /repos/{owner}/{repo}/installation` for the review App's installation)
matches the tenant's registered `review_app_id`, and among those, the
**latest by submission time** must have `state == "APPROVED"` and
`commit_id == pinned`. Anything else — no review from that app, the latest one
`CHANGES_REQUESTED` or `COMMENTED`, or an `APPROVED` review at a different
commit — refuses `verdict_not_approved`. A dismissed review does not count:
GitHub reports a dismissed review's `state` as `DISMISSED`, not `APPROVED`, so
this check needs no separate dismissal handling.

**The outcome: a check run from `swarm-proof`.** The same shape, for the
proof step, with its own secret `swarm-tenant-<tenant>-git-proof` and its own
app, `checks: write` only:

```
POST /repos/{owner}/{repo}/check-runs
{
  "name": "swarmcloud-proof",
  "head_sha": "<pinned>",
  "status": "completed",
  "conclusion": "success" | "failure",
  "output": {"title": "...", "summary": "<proof.json.evidence>"}
}
```

`conclusion` is `success` exactly when `proof.json.outcome == "PROVED"`. The
merge step reads `GET /repos/{owner}/{repo}/commits/{pinned}/check-runs?check_name=swarmcloud-proof&filter=latest`
(paginated), requires exactly one run from the tenant's registered
`proof_app_id` with `status == "completed"` and `conclusion == "success"` at
`pinned`; anything else refuses `not_proved_by_check`. This check run is
**not** added to `main`'s required-status-checks list (§5.2) — it is a gate
the merge step itself demands, the same way the review approval is, and it
stays out of branch protection so a person's ordinary pull request, which
never runs a `swarm-proof` step, is never held on a check that can only ever
be reported by this chain.

**Why a proof App and not the same review App, or nothing:** the owner asked
to make the proof a check run from a proof App, or state why not building one
would be safe. It would not be: without an anchor, `proof.json` is exactly as
forgeable as `review.json` was — the merge step reads it as a staged GCS
artifact, and any later tenant identity that knows the proof task's id can
overwrite it before staging, the same class of attack B1 closes for the
review. Reusing the review App's key for the proof's check run was considered
and rejected: it would mean the review step's service account (or the proof
step's, if the key were shared the other way) can act as **both** identities,
which collapses exactly the separation §1.3's "one App per tenant per role"
requires — a compromise of either step's Secret Manager access would then
forge both the verdict and the outcome, not just its own.

**Why this closes the window the attestation only narrowed, and what remains
open (T3, R1, R2 — §7 corrected):**

* the fix and proof agents cannot mint the review App's key, so neither can
  post a fabricated `APPROVE`, and cannot mint the proof App's key while
  running *as* the proof step's own identity in the normal course of writing
  `proof.json` (§0's exposure is about the **tenant's** service account; the
  review and proof Apps are not that account, and nothing grants the tenant's
  worker account `iam.serviceAccountTokenCreator` or `actAs` on either one,
  the same clause §1.3 gives the merge account);
* there is no scheduler sweep in the critical path any more, so R2's residual
  window — another workflow's agent racing a control-plane write between the
  review's upload and a sweep — has no equivalent to race against. A GitHub PR
  review and a GitHub check run exist the instant the API call returns; there
  is no second write for anything to race;
* what remains, and is not closed by B1 any more than it was by the
  attestation: **T11**, prompt injection of the reviewer or the proof agent
  itself, so that the *real* review step, running as the *real* review
  identity, is talked into posting `APPROVE` on a change it should not. B1
  raises the bar the attacker has to clear — impersonate the review step's
  judgement, not merely overwrite a file — but it cannot verify the judgement
  itself. This is restated as residual risk **R1** (§7); the old R2 is
  resolved, not merely narrowed, because the object B1 reads has no writer to
  race.

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

1. **The required set.** `GET /repos/{o}/{r}/rulesets` and
   `GET /repos/{o}/{r}/branches/{base}/protection` (§8: `main` now carries
   both a ruleset and, historically, classic protection; the step reads
   whichever is authoritative and says so in the refusal if they disagree)
   give the required-status-checks rule: `checks[] {context, app_id}`. This is
   the same read as `auto-merge.yml` gate 3, and it needs only read access.
   **An empty set is a refusal** (`no_required_checks`), for the reason gate 3
   gives: with nothing to require, "green" would mean nothing.
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
   it looser. This unfiltered list is where `swarmcloud-proof` (below) also
   appears, since it is a check run on `pinned` like any other; it is excluded
   from this tolerance list by name, because §5.2a already requires it to be
   `success`, a stricter bar than "not failing."

### 5.2a The verdict and the outcome — B1

Two more reads, neither part of branch protection, both required before the
merge call:

* **The verdict.** `GET /repos/{o}/{r}/pulls/{n}/reviews` (paginated), the
  latest review by `user.id == review_app_bot_id` (read once from
  `GET /repos/{o}/{r}/installation` scoped to the review App), must have
  `state == "APPROVED"` and `commit_id == pinned`. Anything else refuses
  `verdict_not_approved` (§4.3).
* **The outcome.** `GET /repos/{o}/{r}/commits/{pinned}/check-runs?check_name=swarmcloud-proof&filter=latest`,
  exactly one run from `app.id == proof_app_id`, `status == "completed"`,
  `conclusion == "success"`. Anything else refuses `not_proved_by_check`
  (§4.3).

The refusal names every check, review or run that was not green, with its
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
  (review id <review database id>).
  Proved at <pinned> by task <proof task id>: PROVED by swarm-proof
  (check run id <check run id>).
  Required checks green at <pinned>: <context>, <context>, ...
  ```

  Both lines name the GitHub object the merge step actually trusted (§4.3,
  §5.2a) — the review's own id and the check run's own id — rather than
  restating the tenant-writable `review.json`/`proof.json` claims as if they
  were the proof. The repository and pull request number are implicit in
  where the comment is posted, but the commit body carries them explicitly
  too (`<owner>/<repo>#<n>`), since a squash commit can be read outside its
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
review App or the proof App's own key is not in this table: it fails **that**
step's own attempt (an ordinary `CANNOT_START`), not the merge step's — this
table is the merge step's end causes only.

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
| 9 | `review.json.verdict` disagrees with the review App's own `APPROVED`/`CHANGES_REQUESTED` state at `pinned` (§4.3, §5.2a) | `verdict_mismatch` | `MERGE_REFUSED` |
| 10 | no `APPROVED` review from the review App's `app_id` at `pinned` (§4.3) | `verdict_not_approved` | `MERGE_REFUSED` |
| 11 | `proof.json.outcome` disagrees with the proof App's check-run conclusion at `pinned` (§4.3, §5.2a) | `outcome_mismatch` | `MERGE_REFUSED` |
| 12 | no successful `swarmcloud-proof` check run from the proof App's `app_id` at `pinned` (§4.3) | `not_proved_by_check` | `MERGE_REFUSED` |
| 13 | two of §4.2's shas disagree (the message names which) | `heads_disagree` | `MERGE_REFUSED` |
| 14 | the fix step pushed after the review | `review_not_at_head` | `MERGE_REFUSED` |
| 15 | the tenant's control-plane host/owner/repo record is missing, or its host is not the pinned form (§2.1b) | `forge_host_invalid` | `CANNOT_START` |
| 16 | the merge secret does not exist, or the merge account cannot read it | — | `CANNOT_START` (exit 78: the credential is missing or refused) |
| 17 | the App key was rejected (401), or the App is not installed on the repository (404) | — | `CANNOT_START` |
| 18 | the token cannot write to the repository (`permissions.push` is not `true`) | — | `CANNOT_START` |
| 19 | the pull request is closed and not merged | `pull_request_closed` | `MERGE_REFUSED` |
| 20 | its head branch is not `swarm/<author task id>`, or it comes from a fork | `pull_request_not_this_workflows` | `MERGE_REFUSED` |
| 21 | its base is not the default branch | `base_not_default` | `MERGE_REFUSED` |
| 22 | it is a draft | `draft` | `MERGE_REFUSED` |
| 23 | its live head is not `pinned` (read, or the `409` from the merge) | `head_moved` | `MERGE_REFUSED` |
| 24 | its title is the placeholder, or not the title the review saw | `title_placeholder` / `title_changed` | `MERGE_REFUSED` |
| 25 | it touches a protected path: `.github/**`, `scripts/**`, `terraform/**`, `kubernetes/**`, `images/**`, `Makefile`, `pyproject.toml`, `uv.lock`, any `**/conftest.py` (§5.1, M4) | `touches_protected_paths` | `MERGE_REFUSED` |
| 26 | the `pulls/{n}/files` read hit GitHub's 3000-file cap before it could finish checking row 25 | `too_many_files` | `MERGE_REFUSED` |
| 27 | the base has no required checks | `no_required_checks` | `MERGE_REFUSED` |
| 28 | a required check has no `app_id` (M5) | `required_check_unpinned` | `MERGE_REFUSED` |
| 29 | a check is pending or missing at `pinned` | `checks_pending` | `MERGE_REFUSED` |
| 30 | a check failed at `pinned` | `checks_failed` | `MERGE_REFUSED` |
| 31 | `mergeable` is `false` (a conflict with the base) | `conflict` | `MERGE_REFUSED` |
| 32 | `mergeable` is still `null` after the bounded reads | `mergeability_unknown` | `MERGE_REFUSED` |
| 33 | `base.ref` was retargeted between §5.1's read and the merge call (§2.2 step 8, §9) | `base_retargeted` | `MERGE_REFUSED` |
| 34 | a credentialed read, or the merge call itself, received a redirect that the client refused to follow (§2.1b) | `forge_redirect_refused` | `MERGE_REFUSED` if it was a read; `MERGE_FAILED` if it was the merge call |
| 35 | it is **already merged**, and its head was `pinned` | — | **SUCCEEDED**, `merged_by_this_task: false`, with who merged it |
| 36 | it is already merged, and its head was not `pinned` | `merged_at_other_head` | `MERGE_REFUSED`. Something else landed a tree nobody reviewed, and this step says so |
| 37 | the merge call answered `405` (branch protection or GitHub refused) | `forge_refused` | `MERGE_FAILED` |
| 38 | the forge is unreachable, answered `5xx`, or rate-limited with a short `retry-after` | `forge_unavailable` | *retryable*, then `MERGE_FAILED` |
| 39 | rate-limited with a `retry-after` longer than `max_in_worker_retry_delay_seconds` | `forge_unavailable` | *retryable*, with `next_eligible_at` set to the reset. It does not sleep (invariant 4) |
| 40 | the worker died after the merge and before `finish` | — | the reconciler requeues it, the next attempt hits row 35, and it ends SUCCEEDED |
| 41 | the merge succeeded and the comment did not | — | SUCCEEDED, `recorded_on_pull_request: false` (§5.4) |
| 42 | the merge succeeded, and the post-merge re-read of `base.ref` (§5.3, §9) disagrees with the pre-merge one | `base_mismatch_recorded` | SUCCEEDED, with the discrepancy flagged in `result_summary` rather than asserted away |

Row 40 is why the merge step can tolerate a lost attempt without a meaningful
checkpoint. The pin makes a repeated merge either a no-op (row 35) or a refusal
(row 36). It can never be a second, different merge.

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
| T3 | forge `review.json` or the review's `result_summary` saying `MERGE` | **B1**: the merge step does not trust `review.json.verdict`. It requires an `APPROVED` PR review from the `swarm-review` App's `app_id`, submitted at `pinned`, by a step whose secret (`-git-review`) no other tenant identity can read (§1.3, §4.3, §5.2a). Forging the verdict now needs that App's key, not a Firestore or GCS write. **This is stopped, not merely narrowed** (R1 corrected below) |
| T4 | the fix agent pushes after a `MERGE` verdict, so an unreviewed change rides along | `review_not_at_head`: the author's or amender's last `pushed_head` must be `pinned` |
| T5 | push to the branch after every step finished, using `-git` | `head_moved`: the live head is compared with `pinned`, and the merge call carries `sha`, so GitHub refuses a moved head atomically |
| T6 | land a change on `main` that widens the chain's own reach — a `.github/workflows/` edit the chain's own review waves through, or any change to `scripts/**`, `terraform/**`, `kubernetes/**`, `images/**`, `Makefile`, `pyproject.toml`, `uv.lock` or `**/conftest.py`, since **merging to `main` is already code execution as the deployer**: `release.yml`'s `id-token: write` is granted at **workflow** level, not job level, so every job in that run — including the `verify` job, which executes the merged repository's own tests — can mint a deploy-scoped OIDC token, whether or not that job is the one that needs it (M4) | the merge App has no `workflows` permission, and the step refuses first with `touches_protected_paths` (§5.1, §6), a broadened form of the original `.github/workflows/`-only check. Such a change lands only through a human's `ready`. **The underlying exposure — `id-token: write` at workflow level — is not closed by this design.** Moving that grant to job level, behind the environment gate, is recommended as its own change (§11 Q8), not built here |
| T7 | post a fake green check (a commit status or a check run with a required name) using `-git` | required checks are pinned to GitHub Actions' `app_id` (15368), and (M5) any required check with **no** `app_id` is refused outright rather than falling back to a legacy commit status. A status or check from any other token or App does not count (§5.2) |
| T8 | retitle the pull request after the review, to change the headline on `main` | `title_changed`: the live title must equal `review.json.title`. The subject is taken from the live pull request at the same read that pins the head |
| T9 | point the merge at a different pull request by rewriting the author's `result_summary.git.pull_request` | the live pull request's `head.ref` must be `swarm/<author task id>`, a name derived from the author's task id, and `head.sha` must be `pinned` (§4.2), both read against the control-plane `owner`/`repo` record (§2.1b, B2), never a repository name the task document supplies. Another pull request cannot satisfy both |
| T10 | rewrite the merge task's own dispatch block (`merges`), which is a Firestore document, to name different upstream tasks | the block is a claim like every other (§0). What holds is the chain of equalities: the merge lands only a pull request whose live head is `pinned`, and only when a **GitHub review object**, not a Firestore or GCS claim, says `APPROVED` at exactly that sha. Pointing the block elsewhere can at most land a head some genuinely-approved review passed. It cannot land the attacker's unreviewed head (§4.3) |
| T11 | prompt-inject the reviewer, with text in the diff that tells it to answer MERGE | **not stopped by this design.** The verdict is only as good as the reviewer model reading attacker-authored text; B1 makes the resulting `APPROVE` harder to *fabricate* without a real review step, but does nothing about a real review step reaching the wrong conclusion. What limits the damage is the rest of the gate: required checks green at the exact head, a proof at that head, no protected-path changes, and a human's standing ability to revert. This is residual risk **R1** (renumbered; the earlier R1/R2 pair about the attestation is resolved by B1, below) |
| T12 | merge it directly with `-git`, bypassing the step completely | **narrowed by M1, not closed.** Ruleset `main-protection` (§0.3) removed the direct-push bypass `enforce_admins: false` used to allow: an administrator can no longer push straight past the required checks. What it did **not** yet restrict is *who* may use the ordinary PR-merge route — today that is still anyone with write access, which includes any identity holding `-git`. M2's second ruleset (restrict-updates, with a bypass list naming the Apps) is what would close that, and whether GitHub honours an App name in a bypass list on a personal repository is **not verified**; a dry run comes first (§8, §11 Q3). This residual is **R4** |
| T13 | an agent adds the `ready` label to its own pull request, which `auto-merge.yml` trusts as the human review decision | **not stopped by this design — an accepted risk (M3, owner decision 2026-09-29: keep `-git`'s PAT for agent pushes).** Any agent can read `-git` (§0) and so can call the label API directly, without needing the merge step at all. Two things limit what it buys: the label's effect is **head-bound** (`auto-merge.yml`'s `--match-head-commit`), so a later, un-reviewed push does not silently ride an old `ready`; and #219's ordering means the *credential a running agent could otherwise scrape from a worker's memory* is held only by the **worker**, and only after the agent has been reaped — which stops a memory-leak style theft of a token the agent does not already have, but does nothing for `-git`, which the agent can read directly regardless. This residual is **R5** |

**Residual risks, stated plainly:**

* **R1.** T11, prompt injection of the reviewer. (This absorbs the earlier
  draft's R3; the earlier R1 and R2, both about the control-plane attestation
  and its forgery window, are resolved by B1 — see below.)
* **R2. Resolved by B1**, not merely narrowed. The earlier design's R1
  (declining the attestation leaves the verdict forgeable) and R2 (even with
  the attestation, a same-tenant workflow could race the scheduler's sweep) no
  longer apply: there is no scheduler-written, tenant-readable object in the
  critical path for the merge step to trust. The object it reads — a GitHub PR
  review — is created once, by an identity no tenant workflow can mint a
  token for, and has no writer for another workflow to race.
* **R3.** T6 restated (M4): a chain's pull request that lands on `main` is
  code execution as the deployer, because `release.yml`'s `id-token: write` is
  a workflow-level grant and its `verify` job runs the merged tests. The
  broadened `touches_protected_paths` refusal keeps a chain from landing the
  paths most likely to matter (CI itself, Terraform, Kubernetes, images, the
  build and test tooling) without a human's `ready`, but it does not change
  where the OIDC grant sits. Moving `id-token: write` to job level, behind the
  environment gate, closes the actual exposure and is recommended as its own
  change (§11 Q8).
* **R4.** T12: today, any identity with write access — including `-git` — can
  still use the ordinary PR-merge route on `main`, even though `main-protection`
  (M1) has closed the direct-push bypass. M2's restrict-updates ruleset, with
  an App-named bypass list, is the proposed close; it needs a dry run first,
  since naming an App in a personal repository's ruleset bypass list is
  unverified (§8, §11 Q3).
* **R5.** T13: an agent can label its own pull request `ready`, since it can
  read `-git` directly. Accepted per M3. Mitigated, not prevented, by the
  label's head-binding and by #219 keeping the credential out of a process an
  agent could scrape after the fact — neither mitigation stops the agent's own,
  direct read of `-git` while it is still running.

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
  real; §11 Q3 tracks it, and §7 T12/R4 is the residual risk while it is
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
  not one only a person *can* set. §7 T13/R5 records this as an accepted risk:
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
`merged_by_this_task: false` when the head was `pinned` (row 35), or
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
dependency order. **Precondition, already satisfied:** `main-protection`
(M1) exists (§0.3) — the merge step must not be enabled before some form of
branch protection is in place, and now it is.

1. **Contract request 33 decided** (the owner): `worker_action`, the `merge`
   profile, `MERGE_REFUSED` and `MERGE_FAILED`.
2. **A second, not-yet-filed contract request decided** (the owner): the
   `claude-code-review` and `claude-code-proof` catalogue entries (§1.3's
   flagged gap) — otherwise identical to `claude-code`, bound to their own
   Job and service account. **This gates B1's actual guarantee**, not just an
   optimisation: without it, the review and proof steps run on the plain
   `claude-code` profile, sharing its Job and service account with `implement`
   and `fix`, and granting that shared identity `-git-review`/`-git-proof`
   access reopens exactly the forgery window B1 exists to close (§1.3, §4.3).
3. **Terraform (Track C):** the per-tenant merge, review and proof service
   accounts, each with its own narrowed grants (§1.3) — three Jobs, three
   service accounts, one each. Also the `terraform test` assertion that no
   tenant identity holds `run.jobs.run`, `run.jobs.runWithOverrides` or
   `run.jobs.update` on the merge Job (§1.3), and its counterpart for the
   review and proof Jobs.
4. **Scripts (Track D):** `register-tenant.sh --add-provider git-merge` refuses,
   and separate binding paths grant the merge, review and proof accounts
   accessor on their own secret alone.
5. **swarm-api (Track A):** the `single-pr` strategy, `pr_role`, the `merges`
   block, the submission refusals in §3, and the control-plane record for
   `owner`/`repo`/host (§2.1b) that a task can never write.
6. **Worker (Track B):** the author's `pr-title.txt` title, the reader and
   amender roles, `clone_commit`, the review step's PR-review submission and
   the proof step's check-run submission (§4.3), and the merge action in §2.2
   and §5 — including the pinned-host, no-redirect forge client (§2.1b) and
   the pagination cap (§5) — with the parity test (§8).
7. **The owner, once:** create the review App, the proof App and the merge
   App (one each, per tenant — §1.3), install them on the tenant's
   repositories, store their keys with `create-secrets.sh --stdin`, run the
   `restrict-updates` ruleset dry run (§8, §11 Q3), and decide Q3, Q4 and Q8.
8. **Its own change, not part of this design (§7 T6/R3, §11 Q8):** move
   `release.yml`'s `id-token: write` from workflow level to job level, behind
   the environment gate.

The tests each item owes are the ones the refusal table already names: one per
row, each proved red first in CI.

---

## 11. Open questions for the owner

Each has a recommendation. None of them is decided here.

* **Q1. What the chain does when the review says CHANGES.** With the pin, a
  five-step chain merges only when the review said `MERGE` **and** the fix step
  changed nothing. When the fix does change something, the merge refuses
  (`review_not_at_head`), and the pull request stays open with the fix pushed
  and unreviewed. *Recommendation:* keep the five steps as decided. Treat that
  refusal as the designed outcome, and follow it with a second workflow of
  review → proof → merge against the open pull request. That workflow needs
  the API to accept an existing pull request as the author (verified against
  `swarm/<task id>` of a task in the same tenant). The alternative is a
  six-step chain (implement → review → fix → review → proof → merge), which
  merges in one workflow but runs a second review every time, even when the
  first said `MERGE`.
* **Q2. RESOLVED, by B1, not merely answered.** The original question was
  whether to build a control-plane attestation over the staged `review.json`.
  The owner's decision instead replaces that mechanism entirely: the verdict
  is a GitHub PR review from the `swarm-review` App, and the outcome a check
  run from `swarm-proof` (§4.3). No scheduler change and no attestation
  bucket are needed; §10's Terraform item is per-tenant service accounts and
  secrets for the two new Apps, not a bucket.
* **Q3. Restrict who may use the ordinary PR-merge route into `main`.**
  `main-protection` (M1) already closed the direct-push bypass an
  administrator's PAT used to have. What is still open is that any identity
  with write access — including `-git` — can merge a pull request the
  ordinary way once checks are green (§7 T12/R4). *Recommendation:* create
  `restrict-updates` (M2, §8), a second ruleset whose bypass list names only
  the merge Apps (the chain's, per tenant, and `auto-merge.yml`'s), after a
  dry run confirms GitHub honours an App name in a bypass list on a personal
  repository — this is **not verified**, and the dry run is the way to verify
  it before depending on it (§10 item 6).
* **Q4. When `main` moved (§9).** *Recommendation:* merge if it is cleanly
  mergeable and never update the branch, the same bar as `auto-merge.yml`. The
  alternative is refusing whenever `main` moved past the proof's base, which
  would need the proof worker to record that base.
* **Q5. The credential kind (§2.1).** *Recommendation:* a GitHub App,
  separate from the `auto-merge.yml` App, with `contents` and `pull_requests`
  write, `checks` and `statuses` read, and **no** `workflows`. The secret holds
  `{app_id, private_key}`. A fine-grained PAT is the alternative, and it gives
  up the distinct identity that Q3 needs. The same shape now applies to the
  review App (`pull_requests: write` only) and the proof App (`checks: write`
  only), each its own installation per tenant (§1.3).
* **Q6. Should the proof carry an outcome of its own?** A `claude-code` proof
  step succeeds when its agent exits cleanly, whether or not the proof held.
  *Recommendation:* yes, and B1 goes further than the original question asked:
  `proof.json` still carries `"outcome": "PROVED"` (schema checked at row 8),
  but the merge step now also requires that outcome to agree with an
  independently-anchored check run (rows 11–12), the same way the review's
  verdict is now anchored rather than trusted as staged.
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
