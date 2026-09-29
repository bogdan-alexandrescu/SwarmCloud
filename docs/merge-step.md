# The `merge` step: a workflow that merges its own pull request

**Status: PROPOSED, 2026-09-29, for #295.** Nothing described here is built.
This is the design the owner asked for before any code: how a SwarmCloud
workflow ends in a squash merge made by the **worker**, with a credential no
agent can reach. The frozen-contract half is contract request 29 in
[contract-change-requests.md](contract-change-requests.md); the example spec is
in [workflows.md](workflows.md#proposed-a-chain-that-merges-its-own-pull-request).

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
   checks each claim against GitHub (§4, §5). Where GitHub cannot confirm a
   claim, which is the case for the verdict, the claim needs a record that
   only the control plane can write (§4.3).
3. **Today, the `-git` token may already be able to merge.** `eng`'s `-git`
   secret holds the owner's classic PAT
   ([merge-strategy-live-proof.md](merge-strategy-live-proof.md) §3). The
   repository owner is an administrator, and `main` is protected with
   `enforce_admins: false` ([ci.md](ci.md)). GitHub lets an administrator's
   merge bypass required checks. So an agent that reads that secret can
   probably merge a pull request into `main` without the checks, today, with
   or without this design. This design does not close that. Only a
   branch-protection change does (§7 T12, §11 Q3).

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
[contract request 29](contract-change-requests.md#29-profilespy--modelspy-a-merge-profile-that-runs-no-agent-and-two-end-causes-for-it):

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
    one small upload. What actually makes an interrupted merge survivable is
    that it is idempotent (§6, "the worker died after the merge").
* `EndCause.MERGE_REFUSED` and `EndCause.MERGE_FAILED` (§6).

What this needs **outside** the frozen contract (none of it is built; each is
its own change, listed in §10):

* a service account per tenant for the merge Job (Terraform, labelled
  `managed-by=swarm-terraform`). Its grants are the worker's Firestore role, a
  read of the tenant's GCS prefix, and accessor on
  `swarm-tenant-<tenant>-git-merge` **alone**. Nothing grants the tenant's
  worker account `iam.serviceAccountTokenCreator` or `actAs` on it;
* `register-tenant.sh --add-provider git-merge` must **refuse**. Its job is to
  bind the tenant's worker account, and for this one provider that binding is
  the hole §0 describes;
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
   staged artifacts parse, the verdict reads `MERGE`, and the shas agree with
   each other and with the control-plane attestation. A refusal here costs no
   secret read at all.
6. **Read the secret.** Secret Manager `access`, as `swarm-<tenant>-merge`,
   into a local variable. It is registered with the logger's redaction at once,
   the same way `resolve_git_token` does it.
7. **Mint the installation token.** The worker signs a JWT with the key (valid
   for at most 10 minutes), resolves the installation, and exchanges the JWT for
   an installation token scoped to one repository and two permissions. The PEM
   and the JWT are then dropped (`del`). They were never in a file, an
   environment variable or argv, and they reached no subprocess.
8. **Re-check fencing and `cancel_requested`.** A cancel or a reclaim that
   arrived while the worker was reading is honoured before the forge is
   touched.
9. **The forge reads and the merge** (§5), all over HTTPS from the worker
   process through `forge._request`. **No git runs.** The merge step does no
   clone, no fetch and no push, so there is no credential helper, no credential
   file and no repository configuration that anything could have written.
10. **Record** (§5.4), with the same token.
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
| `reader` | the author's branch, `swarm/<author task id>` | **nothing**, whatever the strategy | review, proof |
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
* the live pull request's `head.sha` (§5.1).

The chain's four workers write the first four values under the tenant
identity. By §0 those are claims, so they are checked against GitHub:

* the pull request number comes from the author's `result_summary.git`. The
  live pull request must have `head.ref == "swarm/<author task id>"` (derived,
  not read), `head.repo.full_name == base.repo.full_name` (no fork),
  `base.ref ==` the repository's default branch, and `state == "open"`;
* its live `head.sha` must equal `pinned`, and the merge call carries `sha`, so
  GitHub itself refuses the merge if the head moves between the read and the
  merge (§5.3).

What that proves: the merge lands exactly the tree the review and the proof
**say** they saw, from this workflow's own branch. What it cannot prove is that
the verdict was really written by the review step. §4.3 covers that.

### 4.3 The verdict needs a record the tenant cannot write

The fix and proof agents both run **after** the review and **before** the
merge. Either one can mint the tenant token (§0), learn the review task's id
from the workflow document, and overwrite `review.json` in GCS or
`result_summary` in Firestore with `"verdict": "MERGE"`. The sha checks do not
catch this, because the forger writes the right sha. Nothing on GitHub catches
it either, since any review comment the review worker posted could equally be
posted by an agent holding `-git`.

**Proposed: a control-plane attestation.** The scheduler's dependency sweep is
the thing that promotes the step after the review, so it is the natural place.
Before it moves the first child of a `single-pr` review step out of
`DEPENDENCY_INCOMPLETE`, it reads the review's `review.json` object once and
writes `{review task id, GCS generation, sha256, verdict, sha, at}` to
`gs://<attestations bucket>/<tenant>/<workflow>/<review task>.json`. Only the
scheduler's service account can write that bucket, and only the tenant's merge
account can read its prefix. The merge worker requires three things of the
attestation: the staged `review.json`'s sha256 equals the attested one, the
attested verdict is `MERGE`, and the attested `sha` is `pinned`. A missing or
different attestation is `MERGE_REFUSED` / `verdict_not_attested`.

Why this closes the window:

* the fix agent starts only after the attestation is written, because the same
  sweep writes it and then promotes;
* an implement-step agent that pre-planted `review.json` before the review ran
  was overwritten by the review worker's upload. A review that uploads nothing
  fails its own attempt, so it never reaches the sweep;
* the scheduler's account cannot be minted by an agent. It is a control-plane
  account (invariant 9's own boundary).

What it leaves open: an agent of **another** workflow in the same tenant that
already knows this workflow's review task id could write in the seconds between
the review's upload and the sweep. It cannot list ids, since
`datastore.entities.list` is not granted, so it would have to be told them.
That is recorded as residual risk R2 (§7).

The attestation is a scheduler change plus a Terraform bucket. It is not a
frozen change. It is also the largest piece of this design that is not the
merge itself, so whether to build it is open question Q2.

---

## 5. What the merge step asks GitHub, and what counts as green

Every call goes to the forge host of the task's `repository_url` through
`forge._request`, with the installation token, API version `2022-11-28`. Every
list call is **paginated to the end** (`per_page=100`, follow `Link: rel="next"`).
A page that is not the last one is not the answer.

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

`GET /repos/{o}/{r}/pulls/{n}/files` (paginated): a pull request that touches
`.github/workflows/` is refused with `touches_workflows` (§7, T6).

### 5.2 The checks

1. **The required set.** `GET /repos/{o}/{r}/branches/{base}` gives
   `protection.required_status_checks`: `checks[] {context, app_id}` and the
   legacy `contexts[]`. This is the same read as `auto-merge.yml` gate 3, and it
   needs only read access. **An empty set is a refusal** (`no_required_checks`),
   for the reason gate 3 gives: with nothing to require, "green" would mean
   nothing.
2. **Each required check, at `pinned`.**
   `GET /repos/{o}/{r}/commits/{pinned}/check-runs?check_name=<context>&filter=latest`.
   Only runs whose `app.id` equals the rule's `app_id` count, when the rule pins
   one (every rule in [ci.md](ci.md) pins GitHub Actions, `15368`). That makes
   a check run of the same name, posted by any other App or token, invisible to
   this step, which is the same guarantee that pinning gives GitHub. A required
   `context` with no `app_id` is looked up in
   `GET /repos/{o}/{r}/commits/{pinned}/status` as well, and only `state ==
   "success"` counts there.
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

The refusal names every check that was not green, with its conclusion or
status, and the sha it was read at.

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
Everything else is §6.

### 5.4 The pull request records which task merged it

In two places, because they fail differently:

* **the squash commit's body**, which is the permanent record and is written
  atomically with the merge:

  ```
  Merged by SwarmCloud task <merge task id> (workflow <workflow id>),
  attempt <attempt id>, tenant <tenant>.
  Reviewed at <pinned> by task <review task id>: MERGE.
  Proved at <pinned> by task <proof task id>: PROVED.
  Required checks green at <pinned>: <context>, <context>, ...
  ```

* **a comment on the pull request**, with the same lines, posted after the
  merge. If the comment fails, the merge still succeeded. The step ends
  SUCCEEDED with `result_summary.merge.recorded_on_pull_request: false` and
  the reason, because the commit body already holds the record.

No line in either one carries `Co-Authored-By` or any other attribution. The
commit is the App's.

---

## 6. Every failure mode, and its end cause

Two new end causes (contract request 29), which keep two different questions
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
cause shown only when `max_attempts` is spent.

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
| 9 | the verdict is not exactly `MERGE` | `verdict_not_merge` | `MERGE_REFUSED` |
| 10 | the proof is not exactly `PROVED` | `not_proved` | `MERGE_REFUSED` |
| 11 | no attestation, or its sha256 differs (§4.3) | `verdict_not_attested` | `MERGE_REFUSED` |
| 12 | two of §4.2's shas disagree (the message names which) | `heads_disagree` | `MERGE_REFUSED` |
| 13 | the fix step pushed after the review | `review_not_at_head` | `MERGE_REFUSED` |
| 14 | the secret does not exist, or the merge account cannot read it | — | `CANNOT_START` (exit 78: the credential is missing or refused) |
| 15 | the App key was rejected (401), or the App is not installed on the repository (404) | — | `CANNOT_START` |
| 16 | the token cannot write to the repository (`permissions.push` is not `true`) | — | `CANNOT_START` |
| 17 | the pull request is closed and not merged | `pull_request_closed` | `MERGE_REFUSED` |
| 18 | its head branch is not `swarm/<author task id>`, or it comes from a fork | `pull_request_not_this_workflows` | `MERGE_REFUSED` |
| 19 | its base is not the default branch | `base_not_default` | `MERGE_REFUSED` |
| 20 | it is a draft | `draft` | `MERGE_REFUSED` |
| 21 | its live head is not `pinned` (read, or the `409` from the merge) | `head_moved` | `MERGE_REFUSED` |
| 22 | its title is the placeholder, or not the title the review saw | `title_placeholder` / `title_changed` | `MERGE_REFUSED` |
| 23 | it touches `.github/workflows/` | `touches_workflows` | `MERGE_REFUSED` |
| 24 | the base has no required checks | `no_required_checks` | `MERGE_REFUSED` |
| 25 | a check is pending or missing at `pinned` | `checks_pending` | `MERGE_REFUSED` |
| 26 | a check failed at `pinned` | `checks_failed` | `MERGE_REFUSED` |
| 27 | `mergeable` is `false` (a conflict with the base) | `conflict` | `MERGE_REFUSED` |
| 28 | `mergeable` is still `null` after the bounded reads | `mergeability_unknown` | `MERGE_REFUSED` |
| 29 | it is **already merged**, and its head was `pinned` | — | **SUCCEEDED**, `merged_by_this_task: false`, with who merged it |
| 30 | it is already merged, and its head was not `pinned` | `merged_at_other_head` | `MERGE_REFUSED`. Something else landed a tree nobody reviewed, and this step says so |
| 31 | the merge call answered `405` (branch protection or GitHub refused) | `forge_refused` | `MERGE_FAILED` |
| 32 | the forge is unreachable, answered `5xx`, or rate-limited with a short `retry-after` | `forge_unavailable` | *retryable*, then `MERGE_FAILED` |
| 33 | rate-limited with a `retry-after` longer than `max_in_worker_retry_delay_seconds` | `forge_unavailable` | *retryable*, with `next_eligible_at` set to the reset. It does not sleep (invariant 4) |
| 34 | the worker died after the merge and before `finish` | — | the reconciler requeues it, the next attempt hits row 29, and it ends SUCCEEDED |
| 35 | the merge succeeded and the comment did not | — | SUCCEEDED, `recorded_on_pull_request: false` (§5.4) |

Row 34 is why the merge step can tolerate a lost attempt without a meaningful
checkpoint. The pin makes a repeated merge either a no-op (row 29) or a refusal
(row 30). It can never be a second, different merge.

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
| T3 | forge `review.json` or the review's `result_summary` saying `MERGE` | the control-plane attestation (§4.3): the sha256 of what the review uploaded, written by the scheduler before any later agent starts. **Without Q2 this is not stopped** (R1) |
| T4 | the fix agent pushes after a `MERGE` verdict, so an unreviewed change rides along | `review_not_at_head`: the author's or amender's last `pushed_head` must be `pinned` |
| T5 | push to the branch after every step finished, using `-git` | `head_moved`: the live head is compared with `pinned`, and the merge call carries `sha`, so GitHub refuses a moved head atomically |
| T6 | land a change to `.github/workflows/` that the chain's own review then waves through | the merge App has no `workflows` permission, and the step refuses first with `touches_workflows`. A workflow change lands only by a human's `ready` |
| T7 | post a fake green check (a commit status or a check run with a required name) using `-git` | required checks are pinned to GitHub Actions' `app_id` (15368). A status or check from any other token or App does not count (§5.2) |
| T8 | retitle the pull request after the review, to change the headline on `main` | `title_changed`: the live title must equal `review.json.title`. The subject is taken from the live pull request at the same read that pins the head |
| T9 | point the merge at a different pull request by rewriting the author's `result_summary.git.pull_request` | the live pull request's `head.ref` must be `swarm/<author task id>`, a name derived from the author's task id, and `head.sha` must be `pinned` (§4.2). Another pull request cannot satisfy both |
| T10 | rewrite the merge task's own dispatch block (`merges`), which is a Firestore document, to name different upstream tasks | the block is a claim like every other (§0). What holds is the chain of equalities: the merge lands only a pull request whose live head is `pinned`, and only when an **attestation** says a review answered `MERGE` at exactly that sha. Pointing the block elsewhere can at most land a head that some attested review passed. It cannot land the attacker's unreviewed head (§4.3) |
| T11 | prompt-inject the reviewer, with text in the diff that tells it to answer MERGE | **not stopped by this design.** The verdict is only as good as the reviewer model reading attacker-authored text. What limits the damage is the rest of the gate: required checks green at the exact head, a proof at that head, no workflow changes, and a human's standing ability to revert. This is residual risk R3 |
| T12 | merge it directly with `-git`, bypassing the step completely | **not stopped by this design** (§0.3). Only a rule on `main` that `-git`'s identity cannot bypass stops it: `enforce_admins: true` and a restriction on who may merge. That is Q3 |

**Residual risks, stated plainly:**

* **R1.** Without the attestation (Q2 declined), a later agent in the chain can
  forge the verdict. In that case the merge step guarantees "green at the
  reviewed head" but **not** "reviewed".
* **R2.** With the attestation, another workflow's agent in the same tenant
  that was told this workflow's ids could forge in the seconds between the
  review's upload and the sweep. The sweep also finds the review step by
  reading the workflow and task documents, which are Firestore and so
  tenant-writable. An agent that ran **before** the review (implement) could
  try to re-label which task is the review. A build of §4.3 has to take the
  review's identity from the workflow as the API created it: for example, the
  API could write the attestation's expected path when the workflow is
  submitted, into the same control-plane-only bucket.
* **R3.** T11, prompt injection of the reviewer.
* **R4.** T12 is open today, whatever is built here, as long as `-git` holds an
  administrator's classic PAT and `enforce_admins` is `false`.

---

## 8. Coexistence with `auto-merge.yml`

**Branch protection on `main` is the authority** on whether anything may land.
Neither merge path can bypass it: both merge through GitHub's merge endpoint as
an App, and an App is not an administrator. Below that authority, the two paths
answer different questions, and the design keeps them apart:

* **`auto-merge.yml` is authoritative for a pull request a person labels
  `ready`.** The label is the human review decision. Nothing here changes it.
* **The merge step is authoritative for a `single-pr` chain's pull request.** It
  merges directly, in the same squash form with the same subject. It never
  adds `ready` and never enables auto-merge. It does not need to: it does not
  wait for anything.

If both act on one pull request (a person adds `ready` to a chain's pull
request while the chain is running), the first merge wins and the other path
finds it merged. `auto-merge.yml`'s `gh pr merge` then fails harmlessly on a
merged pull request. The merge step ends SUCCEEDED with
`merged_by_this_task: false` when the head was `pinned` (row 29), or
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
every other conclusion. The merge step also refuses `touches_workflows`.

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

---

## 10. What has to be built, and in what order

None of it is built. Each item is its own pull request, and the order is the
dependency order.

1. **Contract request 29 decided** (the owner): `worker_action`, the `merge`
   profile, `MERGE_REFUSED` and `MERGE_FAILED`.
2. **Terraform (Track C):** the per-tenant merge service account with its three
   grants, and the merge Job running as it. If Q2 is accepted, also the
   attestation bucket.
3. **Scripts (Track D):** `register-tenant.sh --add-provider git-merge` refuses,
   and a separate binding path grants the merge account accessor on
   `-git-merge` alone.
4. **swarm-api (Track A):** the `single-pr` strategy, `pr_role`, the `merges`
   block, and the submission refusals in §3.
5. **Worker (Track B):** the author's `pr-title.txt` title, the reader and
   amender roles, `clone_commit`, and the merge action in §2.2 and §5, with the
   parity test (§8).
6. **Scheduler (Track A), if Q2 is accepted:** the attestation in §4.3.
7. **The owner, once:** create the merge App, install it on the tenant's
   repositories, store its key with `create-secrets.sh --stdin`, and decide Q3.

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
* **Q2. Build the control-plane attestation (§4.3)?** Without it, the merge
  step proves the checks were green at the reviewed head, but not that the
  review really said `MERGE` (R1). *Recommendation:* yes, before the merge step
  is enabled for any tenant. It is the only part of the gate that stops the
  owner's own named threat: an upstream agent getting its pull request merged
  unreviewed.
* **Q3. Restrict who may merge into `main`.** Today an agent that reads `-git`
  can probably merge without checks (§0.3, R4). *Recommendation:* set
  `enforce_admins: true`, and allow merges into `main` only from the merge Apps
  (the chain's and `auto-merge.yml`'s). Keep the owner's emergency path as a
  deliberate, logged bypass. Classic branch protection's push restriction
  exists only for organisation repositories, and `bogdan-alexandrescu/SwarmCloud`
  is a personal one. A ruleset with a bypass list is the likely mechanism, but
  whether it can name an App on a personal repository is **not verified**.
* **Q4. When `main` moved (§9).** *Recommendation:* merge if it is cleanly
  mergeable and never update the branch, the same bar as `auto-merge.yml`. The
  alternative is refusing whenever `main` moved past the proof's base, which
  would need the proof worker to record that base.
* **Q5. The credential kind (§2.1).** *Recommendation:* a GitHub App,
  separate from the `auto-merge.yml` App, with `contents` and `pull_requests`
  write, `checks` and `statuses` read, and **no** `workflows`. The secret holds
  `{app_id, private_key}`. A fine-grained PAT is the alternative, and it gives
  up the distinct identity that Q3 needs.
* **Q6. Should the proof carry an outcome of its own?** A `claude-code` proof
  step succeeds when its agent exits cleanly, whether or not the proof held.
  *Recommendation:* yes. Require `proof.json` with `"outcome": "PROVED"` (row
  10), exactly as the review carries a verdict.
* **Q7. A pending check.** The owner's rule makes it a refusal, and invariant 4
  forbids waiting for it. *Recommendation:* refuse (`checks_pending`), and do
  not add a new `ParkReason` for "waiting on CI" now. A chain whose proof step
  runs after CI has reported (for example because the proof reads the checks)
  rarely meets this. A park reason would be a second frozen change for a case
  not yet observed.
* **Q8. Pull requests that touch `.github/workflows/`.** *Recommendation:*
  refuse them in the chain (T6), so they land only through a human's `ready`.
  The alternative is to grant the merge App `workflows`, which gives an
  unattended chain the ability to rewrite CI and release.
