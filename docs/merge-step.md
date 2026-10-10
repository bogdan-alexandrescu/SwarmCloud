# The `merge` step: a workflow that merges its own pull request

**Status: ACCEPTED 2026-09-29 by the owner, as the design; build gated on
#342, for #295.** Nothing described here is built yet — acceptance of the
design is not acceptance of a build, and `merge` (with `post-verdict` and
`claude-code-review`) must not be enabled for any tenant until #342 (signed
step specs) ships (§0 consequence 4, §10). This is the design the owner
asked for before any code: how a SwarmCloud workflow ends in a squash merge
made by the **worker**, with a credential no agent can reach. The
frozen-contract half is contract request 33 in
[contract-change-requests.md](contract-change-requests.md); the example spec is
in [workflows.md](workflows.md#proposed-a-chain-that-merges-its-own-pull-request).
**Superseded in part on 2026-10-04: the merge step is built, on the tenant's
`-git` token -- "Revised 2026-10-04 (owner)" says what changed and why.**
**Revised again on 2026-10-06: merging becomes its own step that parks while
CI runs, updates a branch that is behind, and replaces `auto-merge.yml` --
the "Revised 2026-10-06" section below is that design and its build plan
(lane MS0); the section after it is what is built today.**
**Revised 2026-10-10: merge steps against one repository take a merge slot,
one at a time, in submission order -- the first section below.**

## Revised 2026-10-10: one merge step per repository at a time (merge race, part of #295)

**What was measured.** On 2026-10-09, 40 SwarmCloud-opened pull requests
were left unmerged across the eng tenant. Of the merge steps that did run, 7
refused `behind_too_often`: the repository's ruleset sets
`strict_required_status_checks_policy`, so a branch must be up to date with
`main`; 16 merge steps ran against `main` at once; each merge that landed
put the others behind, each update cost a full CI run, and three updates was
the cap. A user-owned repository has no GitHub merge queue (docs/ci.md), and
`merge_pr` takes one merge step per workflow, so an operator drove the
queue from a laptop, one `merge_pr` at a time.

**What was built.** `apps/agent-worker/agent_worker/mergeslot.py`: one
Firestore document per (tenant, repository, base), `merge_slots/{id}`, held
by at most one merge step.

* A step takes the slot only when its own checks are **green**, so every
  pull request's first CI run still runs in parallel. Only the
  update-and-merge tail is serial, and a strict up-to-date rule makes that
  tail serial anyway: one merge per CI run is the most the base allows.
* A step that cannot have the slot **parks** (`CI_PENDING`, code
  `merge_slot_wait`), holding no lease and no pool count (invariant 1). Its
  wait is counted on `merge_wait.slot_waits` (bound
  `MERGE_SLOT_MAX_WAITS`), not on the CI wakes, and does not run the
  `checks_timeout` clock. swarm-api's wake tick skips these parks: their
  checks are green, so a reading would wake them for nothing.
* Waiters are taken **in submission order** (the task's `created_at`). A
  release hands the slot to the first waiter whose task has not ended and
  writes that task's wake marker, so the scheduler promotes it on its next
  drain; the fallback instant covers a lost mark.
* The slot is a **lease**: `MERGE_SLOT_LEASE_SECONDS` (45 min, three CI
  fallbacks), renewed by every attempt of the holder. A crashed holder's
  slot frees when the lease runs out, or at once when its task is read
  terminal. Every slot write is fenced on the attempt's task and lease
  (invariant 5), and the holder re-reads the slot's generation immediately
  before each update and merge call.
* Released on every exit: merged, refused (a red head included), failed and
  cancelled release it; a CI-fix round or a merge queue gives it back; a CI
  wait while holding keeps it.

**Why the update allowance changed.** Holding the slot, no sibling merges.
The updates already on a head when the step took the slot were caused by
the merges it queued behind, and the first update after taking it catches
up with them. Only updates beyond that one are counted against
`MERGE_MAX_BRANCH_UPDATES`, so the count measures a base moved by someone
outside the slot -- normally zero -- and `behind_too_often` is an anomaly
again. The cap rose from 3 to 5 because what it counts is no longer the
platform's own traffic; the reason is beside the value in `merge.py`.

**Contract request 62** asks for a park reason of its own; until then the
code in `merge_wait.code` is what tells a slot wait from a CI wait.

## Revised 2026-10-06 (owner): merging is its own step, parked while CI runs

**Status: DESIGN AND BUILD PLAN, lane MS0 (functionality wave 10), part of
#352 and #295. Nothing in this section is built yet.** What runs today is the
2026-10-04 step described in the next section: it waits for CI by failing its
attempt retryably and sitting READY for `CHECKS_PENDING_RETRY_SECONDS`, at most
`MERGE_STEP_MAX_ATTEMPTS` times, and refuses a branch that is behind. The
lanes in §6 below replace that, smallest first.

The owner decided on 2026-10-06 (#352, last comment) that merging is a
distinct workflow step -- **implement → review → fix → merge** -- that:

* parks while the pull request's CI runs, holding no capacity (invariant 4);
* is woken when the checks complete;
* updates the branch and re-checks when the branch is behind;
* squash-merges with the tenant's `-git` token (the 2026-10-04 decision);
* closes the issues its pull request names;
* replaces `.github/workflows/auto-merge.yml` for SwarmCloud's own pull
  requests, and works on any repository a workflow runs on, registered or
  not (the repository registry, [repo-index.md](repo-index.md) §1, and
  swarm-api's `/v1/repositories` routes);
* leaves `auto-merge.yml` in place until the step has merged about ten pull
  requests cleanly. Then `auto-merge.yml` is retired.

The 2026-10-04 decisions all still hold. The credential is the tenant's
`-git` token. Merging is a platform default (`merge_by_default`) that a job
overrides with `metadata.merge`. Any repository the workflow names can be
merged. The token is read at merge time only, under #219's rules (the next
section, decision 1).

What this revision changes in the BUILT step is how it waits and what it
does about a branch that is behind. Who merges, with what token, and which
pull request it merges all stay the same.

### Owner decisions on this plan (2026-10-06)

The four questions this revision raised were answered by the owner on 2026-10-06:

1. **CI wait park reason:** add `ParkReason.CI_PENDING`. Frozen-contract request (A) is accepted by the owner (2026-10-06). It is applied in MS2 with the phrase the frozen-contract guard reads. A CI wait refunds the attempt, up to the maximum wake count.
2. **PRs no workflow opened:** a merge-only workflow that names a pull request and its head sha runs just the merge step. It is a separate lane after MS3. **Built 2026-10-07 (lane C5J, part of #352)**: the field is `merge_pr: {number, head_sha}` on the workflow submission (owner, 2026-10-07, triage Q1); "A pull request no workflow opened" below says what it accepts and refuses.
3. **The close-issues job:** it survives `auto-merge.yml`'s retirement as its own `close-merged-issues.yml`, which runs `scripts/close-merged-issues.sh` on every merge into main.
4. **The unused #295 pieces:** the per-tenant review, post-verdict and merge service accounts, the `worker_objects` split and the disabled single-pr catalogue entries are removed in a cleanup lane. The Terraform IAM change goes to the owner at dev-iam, and frozen-contract request (B) retires the catalogue entries.

### 1. Lifecycle

**Submitted with the workflow.** The merge step is submitted with the
workflow in one of two ways. A caller can state it (`runner_profile:
"merge"`). Or swarm-api appends it before signing, when `metadata.merge` is
`"on"`, or when it is absent and `merge_by_default` is on
(`apps/swarm-api/swarm_api/validation.py::resolve_merge_choice`,
`apps/swarm-api/swarm_api/validation.py::plan_merge`).

Its signed dispatch block names its target by task id (`merge_target`), so
it never follows a pointer the signed spec does not name. Until the step
that opens the pull request (and the review, when there is one) has ended,
it is PARKED on `DEPENDENCY_INCOMPLETE`, like every step. None of this
changes.

**First run.** The step runs as a worker action: no agent, no workspace, a
lease only while it reads. It does every check that needs no credential, and
then reads the token. It then reads the pull request and the base branch's
required checks
(`apps/agent-worker/agent_worker/merge.py::run_merge`,
`apps/agent-worker/agent_worker/merge.py::required_check_state`). The
outcome is one of three:

* a verdict -- merged, or refused;
* an update of the branch (below);
* or **a wait**. A wait is any of: a required check still queued or in
  progress; no check reported yet on a branch that requires none; or
  GitHub's mergeability not yet computed.

**A wait is a park, not a retry.** In one fenced transaction the worker
writes PARKED with **`CI_PENDING`** as the park reason. It records three
things on the task:

* the head it is waiting at;
* the pending check names, in `blocked_by`;
* `metadata.merge_wait`: `first_parked_at`, `wakes`, `updates`.

It refunds the attempt that admission counted, up to `MERGE_CI_MAX_WAKES`
(proposed 60). Then it releases the lease and exits 75.

This is the await park's shape exactly
(`apps/agent-worker/agent_worker/control.py::ControlPlane.park_awaiting_children`):
waiting is not failing, so a slow CI does not use up the step's attempts.
Past the bound, a wake counts like any attempt, so a pull request whose CI
never settles still ends at `max_attempts`.

A `CI_PENDING` task is PARKED. It holds **no capacity**: no lease, no pool
count, no Job execution (invariants 1 and 3). The park reason is new, and it
is a frozen-contract change: request (A) at the end of this section.

**Woken by the periodic tick, not by a webhook.**

The platform has **no webhook receiver**. No route verifies a GitHub
delivery. The repository index's push webhook is phase 3 (RI8,
[repo-index.md](repo-index.md) §3.3) and unbuilt. A webhook would need three
things this design avoids:

* an HMAC secret per registration;
* a hook installed in every repository, which needs `admin:repo_hook` on the
  token -- a wider token than §2 needs;
* a public unauthenticated route.

So the signal is a **cheap re-read on the periodic tick the platform already
runs**. A new per-tenant Cloud Scheduler job, `merge_wake`, runs every
minute. It has the same shape and OIDC identity as `issue_run_advance`
(`terraform/modules/scheduler/jobs.tf` (`resource "google_cloud_scheduler_job" "issue_run_advance"`)):

* its description carries `managed-by=swarm-terraform`, since a Cloud
  Scheduler job has no labels;
* it calls `POST /v1/admin/merges/wake?tenant_id=<tenant>` as
  `swarm-rollup-sweeper`;
* swarm-api admits that account to this one route besides its three
  (`apps/swarm-api/swarm_api/auth.py::ROLLUP_SWEEPER_ROUTES`).

For each of the tenant's `CI_PENDING` tasks, the route makes one indexed
query: `tenant_id`, `state`, `park_reason`. It reads the pull request's
check runs and statuses at the recorded head. It reads with the tenant's own
`-git` token, through
`apps/swarm-api/swarm_api/forge.py::SecretManagerForgeTokens`. That is the
same credential, and the same reads, that the issue-run CI loop already makes
(`apps/swarm-api/swarm_api/issueci.py::from_checks`). A task is read at most
once per `apps/swarm-api/swarm_api/issueci.py::CI_READ_SECONDS`.

When nothing it waits on is still pending, the route writes
`merge_wait.wake_requested_at` in a transaction guarded on the task still
being PARKED/`CI_PENDING`. "Nothing still pending" covers every check
completed, the head moved, the pull request closed or merged, or
mergeability computed. That write is the route's only write to the task.

**Why the tick, and why swarm-api rather than the scheduler:**

* **The scheduler holds no forge token, and must not.** A GitHub outage must
  never slow admission. swarm-api already reads each tenant's `-git` token
  for exactly these reads, under that tenant only (invariant 9).
* **A blind time-based wake costs a Job execution and a lease every time.**
  A cold start takes tens of seconds; SwarmCloud's own CI takes about 10-20
  minutes. A check read through the API costs one GET and holds nothing.
* **A webhook can be added later without redesign.** The webhook, if RI8
  ever builds it, writes the same marker.

**The scheduler promotes; it never reads GitHub.** A new sweep,
`_promote_ci_waits`, beside
`apps/scheduler/scheduler/loop.py::Scheduler._promote_child_awaits`, returns
a `CI_PENDING` park to READY in either of two cases:

* `merge_wait.wake_requested_at` is set;
* `next_eligible_at` has passed. The worker sets it to the park instant plus
  `MERGE_CI_FALLBACK_SECONDS` (proposed 900), so a dead tick or a broken
  token never strands a merge.

The promotion is guarded on the state and park reason it read, and writes
READY alone (invariant 1). A task that has used its last attempt is
dead-lettered, as `_end_exhausted_retry` does.

**A forged marker is harmless.** Firestore has no document-level IAM, so any
tenant identity can write the marker. At worst that causes an early wake:
the worker re-reads every fact and trusts nothing the tick saw.

**On wake, the worker reads everything again** and takes the first row that
matches:

| what GitHub says now | what the step does |
|---|---|
| merged at the pinned head (another merger won) | close the issues (below); SUCCEEDED, `merged_by_this_task: false` |
| merged at another head, or closed | MERGE_REFUSED `merged_at_other_head` / `pull_request_closed` |
| the head moved, and the step did not move it, or a CI-fix round of this workflow did not move it (below) | MERGE_REFUSED `head_moved` |
| a required check **failed** | **if the workflow has CI-fix rounds left** (§6 MS7), the tick hands the red reading to the **CI-fix loop** first, so this row only runs once the rounds are spent or there were none. Otherwise MERGE_REFUSED `checks_failed`, naming every failing check and its conclusion |
| still pending, and `first_parked_at` is older than `MERGE_CI_MAX_SECONDS` (proposed 6 h) | MERGE_REFUSED `checks_timeout`, naming the checks still pending |
| still pending, or mergeability unknown | park `CI_PENDING` again |
| **behind** the base: GitHub's `mergeable_state` is `behind` (the base requires an up-to-date branch) | **update the branch** (below), then park `CI_PENDING` at the new head |
| `mergeable` false (`dirty`) | MERGE_REFUSED `merge_conflict` |
| green at the pinned head and mergeable | **squash-merge** (below); SUCCEEDED |

**Updating a branch that is behind.** Before the call, the worker runs the
same fencing and cancel recheck as before the merge call, because this is a
write to the forge. A stale worker exits without making it (invariant 5).

It calls `PUT /repos/{o}/{r}/pulls/{n}/update-branch` with
`expected_head_sha` set to the pinned head, so GitHub refuses if anyone
pushed in between. GitHub, not an agent, makes the merge commit of the base
into the branch. The responses map as follows:

* a 422 that says the update conflicts → `merge_conflict`;
* any other 422 → `head_moved`;
* more than `MERGE_MAX_BRANCH_UPDATES` (5 since 2026-10-10, was 3) updates
  made while the step holds its repository's merge slot, past the one
  catch-up update after taking it → `behind_too_often`, because a base
  moving under a held slot faster than CI is a question for a person (see
  "Revised 2026-10-10" below for why only those are counted).

The new head becomes the pinned head. That is a fact about GitHub, never
about the tenant-writable task document. On every wake the worker walks
first parents from the live head back to the head the opening step pushed
(the signed target's recorded `pushed_head`), at most
`MERGE_MAX_HEAD_UPDATES` (12) steps. It accepts only these chains:

* the head is the pushed head itself;
* each step down the chain is a two-parent merge commit whose second parent
  is already on the base branch (`compare` reports `behind` or
  `identical`).

Anything else is `head_moved`.

The review's verdict covered the code before the update. What the update
adds is commits already on the base branch, each merged through its own
pull request, and the required checks run again on the merged head before
the merge. This is the owner's 2026-10-06 decision, and it supersedes §9's
"never update the branch" for a base that requires an up-to-date branch. On
a base that does not require one, §9 stands: if GitHub says the branch
merges cleanly, it is merged, with no update.

**The merge.** The worker runs the fencing recheck, then makes a squash
merge (`apps/agent-worker/agent_worker/merge.py::GitHubMerger.merge`):

* the commit title is the pull request's own title, as `<title> (#<n>)`;
* `sha` is pinned to the head the checks were green at;
* the body is the provenance text, with no attribution line.

The merge call is never resent. A merge whose answer was lost ends
`merge_unanswered` (MERGE_FAILED), because only GitHub can say whether it
merged. A 405 or 422 whose message is GitHub's branch-protection refusal is
**`protection_refused`**, carrying GitHub's message. Examples: a required
review is missing, a required signature is missing, or a rule the token
cannot satisfy. A 405 because the branch is behind takes the update row
above. A 409 is `head_moved`.

**Then the issues.** The worker reads GitHub's own `closingIssuesReferences`
and closes every one that is still OPEN and in the same repository. It
comments `Closed by #<pr>, merged by SwarmCloud task <task_id>` on each.

These are the rules of `scripts/close-merged-issues.sh`, kept equal to it
(`apps/agent-worker/agent_worker/merge.py::_close_issues`, built):

* it never reads the pull request's text, so `part of #N` closes nothing;
* an issue already closed gets no second comment;
* an issue in another repository is recorded and left alone;
* one refused close does not stop the others.

MS3 adds the script's page rule: a pull request with more references than
one page (`scripts/close-merged-issues.sh` (`PAGE=100`)) closes that page
and records `issues_beyond_page`. It never silently closes only part. The
step ends **SUCCEEDED** whatever the closes answer, because the merge
stands; every close that failed is in `result_summary.merge`.

**The CI-fix loop.** An issue run keeps its own loop, unchanged
(`apps/swarm-api/swarm_api/issueci.py::_merge`). It submits its merge-only
continuation only once CI is green and its `Closes #N` block is written.
That step therefore parks only on mergeability or a behind branch.

A workflow that is not an issue run gets the same loop on request, as MS7.
`metadata.merge_fix_rounds` is 0-5 and defaults to 0. When the tick reads a
red required check for a `CI_PENDING` merge with rounds left, it claims the
round in the merge task's metadata in a guarded transaction, then submits
the continuation the issue-run loop already builds
(`apps/swarm-api/swarm_api/issueci.py::ci_fix_workflow`): `continues_task`
names the task that pushed the head, under the tenant. The merge stays
parked until that continuation ends and CI settles at the head it pushed.

On wake, the worker accepts the new head only if the task that pushed it
continues the signed target's task. That is the rule
`apps/swarm-api/swarm_api/issueci.py::_pushing_task` applies, rechecked by
the worker from GitHub and the task store.

**Refusals: each ends MERGE_REFUSED, with its code in
`result_summary.merge.refusal`, and changes nothing on the forge.**

| refusal | code | read from |
|---|---|---|
| a pull request from a fork | `from_fork` (today folded into `pull_request_not_this_workflows`; MS3 splits it out) | the head repository differs from the base repository |
| a base that is not the repository's default branch | `base_not_default` (new) | the registered repository's `default_branch` when the tenant registered it, else GitHub's `default_branch` |
| the worker's placeholder title, `[swarm] task_` in any case or with leading space | `title_placeholder` (built) | `apps/agent-worker/agent_worker/merge.py::title_is_placeholder`, the same rule as `.github/workflows/auto-merge.yml` (`if [[ "${lower_title}" == "[swarm] task_"* ]]; then`) |
| GitHub's branch protection refused the merge | `protection_refused` (new, split out of `not_mergeable`) | the merge call's 405/422 message |
| a merge conflict, at merge time or on update | `merge_conflict` (new, split out of `not_mergeable`) | `mergeable: false`, or update-branch's 422 |
| a failing required check, no fix rounds left | `checks_failed` (built) | the check runs and statuses at the pinned head |
| CI never settled | `checks_timeout` (new) | `merge_wait.first_parked_at` and `MERGE_CI_MAX_SECONDS` |
| the base moved under a held merge slot faster than CI, more than `MERGE_MAX_BRANCH_UPDATES` times | `behind_too_often` (new) | the first-parent walk's count, less the slot's `holder.updates_at_acquire` |

The other built refusals stand, each with the same code: `verdict_not_merge`,
`head_moved`, `pull_request_closed`, `token_lacks_rights`, and the rest. A
host no `ForgeMerger` serves is still refused at submission
(`apps/swarm-api/swarm_api/validation.py::refuse_unmergeable_forge`).

### 2. Identity

The token is the tenant's existing forge token,
**`swarm-tenant-<tenant>-git`**, read by the merge Job, which runs as the
tenant's worker account. Where [git-tokens.md](git-tokens.md) resolves a
per-repository token for the workflow's repository, that token is used
instead, by the same resolution order and never by anything a caller sends.
It is stored only with `scripts/create-secrets.sh --stdin`. It is read at
merge time only: after the reap and every check that needs no credential,
registered with the redaction, and sent only in the Authorization header to
github.com (#219, #307).

The swarm-api wake route reads the same secret for its check reads, as
`issueci` does.

**The GitHub permissions it needs, for a fine-grained token on each
repository it merges in:**

| permission | for |
|---|---|
| **Contents: write** | the squash merge (`PUT .../pulls/{n}/merge`) |
| **Pull requests: write** | reading the pull request, `update-branch`, and the provenance comment |
| **Issues: write** | closing the named issues and commenting on them |
| **Checks: read** | check runs at the head |
| **Commit statuses: read** | legacy commit statuses, which a required check with no App is matched against by name |
| **Workflows: write** | only when the pull request, or the base merged in by `update-branch`, changes `.github/workflows/`: GitHub refuses any token without it to write those paths |
| Metadata: read | mandatory on every fine-grained token; the branch rules (`rules/branches/{b}`) and protection summary (`branches/{b}`) |

A classic personal access token needs the `repo` and `workflow` scopes for
the same calls.

The step **does not need Administration**. It never reads or changes a
ruleset's configuration: it reads the rules that apply to a branch, which
Metadata covers.

**Branch protection still applies.** GitHub enforces the base branch's
rulesets and classic protection on the merge call itself, whoever makes it:

* required checks;
* required reviews;
* signed commits;
* linear history;
* merge-method restrictions.

The step reads the required checks first, so that it can say which check is
red rather than relay a 405. But GitHub's refusal is the final word, and it
ends `protection_refused`.

**The residual is bypass.** If the token's owner is a repository admin, or
is on a ruleset's bypass list, GitHub lets that token merge through
protection. The step never asks for a bypass -- it sends no admin flag, and
it does its own required-check read before every merge -- so for an admin
token, the step's own check is the gate rather than GitHub's. The owner can
close this by making the token's account a non-bypassing collaborator.
Recorded, not closed.

### 3. What this supersedes

Superseded, in the 2026-09-29 sections below and on epic #352, by the
2026-10-04 decisions that this revision builds on:

* **The per-tenant review App and merge App**, their keys `-git-review` and
  `-git-merge`, and the owner-side creation and installation of both (§1.3,
  §2.1, §8, epic item 8). The tenant's `-git` token can already merge (#476),
  so an App identity buys no isolation from an agent that holds it.
* **The `single-pr` strategy and everything only it needs:**
  * `pr_role`, the `merges` block, the proof step and its anchor (§1, §3,
    §4.1);
  * the `post-verdict` worker action and its GitHub-review verdict anchor
    (§4.3, §5.2a, §6a);
  * the `claude-code-review` profile and its review-only-writable prefix,
    and residual **R8**, which only that prefix had;
  * the `swarm-<tenant>-merge`, `-post-verdict` and `-review` accounts.

  The built step anchors the verdict on the review's verdict file and on
  green required checks (next section). The code that was built for
  `single-pr` stays disabled (`available=False`); removing it is a frozen
  change (request (B) below), not this plan's.
* **§2.1b's Terraform-rendered `FORGE_*` Job values for the merge.** Owner
  and repository come from the signed `repository_url`.
* **The M2 `restrict-updates` ruleset dry run (§7 Q3, §8).** It restricted
  merging to the Apps, and there are no Apps.
* **§9's "never update the branch"**, for a base that requires an up-to-date
  branch (§1 above).
* **The 10-attempt READY wait** (`MERGE_STEP_MAX_ATTEMPTS`,
  `CHECKS_PENDING_RETRY_SECONDS`), replaced by the `CI_PENDING` park once MS2
  lands.

**Kept, each with its reason:**

* **Signed step specs (#342, CR 34), enforced.** They are no longer a gate
  on building, but they remain what makes the step safe to leave parked. A
  `CI_PENDING` step sits for the length of a CI run. Through all of that
  time, any tenant identity can write its Firestore document (§0), and only
  the signature stops a rewrite of its `merge_target` or `repository_url`
  aiming the merge at another pull request (T14). A longer park makes the
  signature matter more, not less.
* **#219's token rules.** They are unchanged (the next section, decision 1).
* **The verdict file.** It must say `MERGE` when the workflow has a review.
* **`head_moved`, `sha` pinning and `merge_unanswered`.** These do not
  depend on any identity choice.

### 4. Invariants

* **1. Only LEASED/DISPATCHED/STARTING/RUNNING create demand.** The step
  holds a lease only while it reads and acts, which takes seconds. A
  `CI_PENDING` task is PARKED and costs nothing, and the wake tick runs in
  swarm-api and holds no lease. The scheduler's promotion writes READY
  alone; only admission takes capacity.
* **2. All-or-nothing reservation.** This is unchanged. Each wake is admitted
  like any task, in admission's one transaction.
* **3. Concurrency counts from LEASED.** This is unchanged. A parked merge is
  not counted; a woken one is counted from its lease.
* **4. Never sleep through a long wait.** This is the point of the
  revision. The step parks, releases and exits 75 instead of waiting.
  Today's in-worker sleep is bounded by `MERGEABLE_REREADS`, a few seconds
  for mergeability; past that, the step parks.
* **5. Fencing.** The park and the merge-wait counters are written in the
  fenced transaction every park uses. The update-branch call and the merge
  call are each preceded by the fencing and cancel recheck. A stale worker
  exits with neither call made and without touching the lease.
* **6. Spot disabled.** This is unchanged. The merge Job is an ordinary
  Cloud Run Job.
* **7. `requests == limits`.** This is unchanged. The merge profile's
  resource class is as request 47 left it.
* **8. Checkpointing.** A worker action keeps no workspace, so there is
  nothing to checkpoint, and it never restores one (CR 36's rule). Its
  durable state is the fenced park record, plus GitHub itself, which every
  wake re-reads. Nothing a lost attempt did is lost: a pinned update is
  re-derived from the first-parent walk.
* **9. Per-tenant isolation.** The step uses the tenant's own token, in the
  tenant's own namespace and Job, and acts only on the workflow's own signed
  `repository_url`. The wake route runs per tenant (`tenant_id` on the
  Scheduler job). It reads that tenant's parks with that tenant's token and
  no other.
* **10. Profiles by name.** A caller asks for the step by naming the `merge`
  profile or setting `metadata.merge`, and for CI-fix rounds with a bounded
  integer, `metadata.merge_fix_rounds`. Nothing a caller sends picks an
  image, a command, a token, a branch or a pull request. The target is
  resolved by swarm-api into the signed block.

### 5. Retiring `auto-merge.yml`

**While both run.** A pull request that a merge step owns must not also
carry the GitHub `ready` label, which `auto-merge.yml` merges on. Without
that, two mergers race. The race is benign -- the loser finds the pull
request merged at the pinned head, and the close script skips closed issues
-- but the step's merges would never be the ones counted. Nothing in this
repository adds that label: it is added by operator watchers and briefs
outside it, and they stop adding it to a pull request whose workflow has a
merge step (owner decision 2026-10-06, #352). MS1 had instead dropped a
workflow label (`pr_label`) of `ready` at submission and recorded it as
`metadata.merge_label_dropped`; that was reverted, because `pr_label` is
only the pull request's fallback title text
(`apps/swarm-api/swarm_api/validation.py::workflow_label`) and never a
GitHub label, so dropping it changed a title and stopped no race. Pull
requests SwarmCloud did not open keep `auto-merge.yml`, unchanged, until
the gate below.

**The gate is 10 clean step merges in SwarmCloud's own repository.** Each
must meet every one of these:

* `result_summary.merge.merged_by_this_task` is true;
* no `issues_not_closed`;
* `application.yml` and `release.yml` started on the merge commit. A merge
  made with the `-git` token starts workflows, which a `GITHUB_TOKEN` merge
  does not -- the reason `auto-merge.yml` needed an App;
* no operator intervention;
* no MERGE_FAILED among them.

MS6 lists them with their run links in its pull request.

**Then MS6 removes:**

* `.github/workflows/auto-merge.yml`;
* `tests/unit/scripts/test_auto_merge_workflow.py`, whose parity cases have
  moved to the step's tests;
* its entry in `.github/workflows/application.yml`'s actionlint list and path
  filter, and in `tests/unit/scripts/test_workflow_step_reachability.py`;
* `docs/runbooks/merge-app.md`;
* the `ready` paragraph of `CLAUDE.md` and of `docs/ci.md`, rewritten to say
  a SwarmCloud pull request is merged by its workflow's merge step.

The `close-issues` job also closes issues for merges a person makes, so it
moves to its own workflow running `scripts/close-merged-issues.sh`, unless
the owner drops it (owner question MS0-Q3).

The owner then uninstalls the `swarmcloud-merge` App and deletes
`MERGE_APP_ID` and `MERGE_APP_PRIVATE_KEY`, which are owner-side steps.

Pull requests a person or a laptop lane opened, with no workflow behind
them, needed an owner decision before the retirement: merge by hand, or a
merge-only workflow naming a pull request (owner question MS0-Q2). The owner
chose the second; it is built (2026-10-07), as below.

#### A pull request no workflow opened (`merge_pr`, built 2026-10-07)

`WorkflowCreate.merge_pr: {number, head_sha}` beside ONE `merge` step under
`direct-pr` (`swarm merge <pr> --sha <head>`, or `swarm_workflow`'s
`merge_pr`). It is the merge-only continuation's shape with the caller naming
the pull request instead of the task that opened it, and it reuses
everything after submission: the signed `merge_target` names `{number,
head_sha, base}`, and the worker's merge action pins `head_sha` exactly as it
pins a pushed head, then runs the same gate -- open, not from a fork, the
registered base, the head (or GitHub's own update of it), every required
check green there, the CI_PENDING park while they run, the merge-queue
enqueue, the issue closing. The one check it skips is the branch name, which
no SwarmCloud task chose.

**Which repository -- the rule chosen, and why.** The one `repository_url`
names, which must be a registration of the caller's own tenant; when the
request names none, the tenant's only registration. A tenant with several
and none named is refused, not guessed at, because pull request numbers
repeat across repositories and a guess would merge the wrong #N. A
repository the tenant never registered is refused even when its token could
reach it: the registration is the tenant's statement that its `-git` token
is meant to act there, and a merge is the widest thing that token does.

**What is refused at submission, before anything is written**
(`continuation.resolve_merge_pr`, `tests/unit/control_plane/test_merge_pr_submission.py`):
a `head_sha` that is not the pull request's current head (the refusal names
the current head, so the caller looks at what changed); a closed or merged
pull request; one not in the registered repository, from a fork, or on a
base other than the registered default branch; any step but the one merge
step; a `continues_task` (two pull requests), a `repository_ref` (the head is
the ref) or `merge_fix_rounds` (a CI-fix round continues the task that pushed
the branch, and no task pushed this one) beside it; any strategy but
`direct-pr`. The pull request is read ONCE, with the tenant's own `-git`
token (invariant 9); a tenant with no `-git` secret is 409. A
continuation-scoped account is refused before any read: `merge_pr` is new
work, which that scope may not submit.

**Not built:** the console's Submit forms do not offer `merge_pr` (another
lane was editing them); the API and the bridge are the way in until then.

### 6. Build plan

Smallest first. Each lane is one pull request, and a dependent lane launches
only after its dependency has merged.

#### MS1 -- API/spec: the step's knobs, its validation, and the frozen request

**What:**

* File request (A) (`ParkReason.CI_PENDING`) and request (B) in
  `docs/contract-change-requests.md`.
* Add `metadata.merge_fix_rounds` (0-5, default 0; refused without a merge
  step).
* Drop `pr_label: "ready"` beside a merge step, recorded as
  `metadata.merge_label_dropped`. (Built, then reverted by owner decision
  2026-10-06, #352: `pr_label` is title text, not the GitHub label; see §5
  "While both run".)
* Write the `base_not_default` input into `merge_target`: the registered
  repository's `default_branch` when the tenant has registered it, so the
  worker never reads the registry itself.

**Territory:**

* `apps/swarm-api/swarm_api/validation.py`: `plan_merge`, `merge_step_for`,
  `workflow_label`;
* `apps/swarm-api/swarm_api/schemas.py`;
* `apps/swarm-api/swarm_api/repositories.py` (read only);
* `docs/contract-change-requests.md`;
* `docs/workflows.md` (merge section);
* `tests/unit/control_plane/test_merge_step_submission.py`.

**Depends on:** this design (MS0).

**Tests:** in `test_merge_step_submission.py`:

* `merge_fix_rounds` out of range, or without a merge step, is a 422;
* `ready` is dropped only beside a merge step;
* `merge_target.base` is the registration's branch for a registered
  repository and absent otherwise.

#### MS2 -- worker/scheduler: the `CI_PENDING` park and its wake

**What:**

* Apply request (A) once the owner accepts it.
* Worker: park `CI_PENDING` with refund, in place of `run.wait`.
* Scheduler: `_promote_ci_waits`, which promotes on the marker or the
  fallback, and dead-letters an exhausted park.
* swarm-api: `POST /v1/admin/merges/wake`, plus the route in
  `ROLLUP_SWEEPER_ROUTES`.
* Terraform: the `merge_wake` Cloud Scheduler job per tenant.

**Territory:**

* `apps/common/swarm_common/states.py` (request (A) only, after acceptance);
* `apps/agent-worker/agent_worker/merge.py`: `_Run.wait`;
* `apps/agent-worker/agent_worker/control.py` (a `park_ci_pending` beside
  `park_awaiting_children`);
* `apps/scheduler/scheduler/loop.py`;
* `apps/swarm-api/swarm_api/routes/admin.py`;
* `apps/swarm-api/swarm_api/auth.py`;
* a new `apps/swarm-api/swarm_api/mergewake.py`;
* `terraform/modules/scheduler/jobs.tf`;
* `tests/terraform/`;
* `scripts/lib/check-contract-parity.sh` (the new reason's mirrors).

**Depends on:** MS1, and the owner's acceptance of request (A).

**Tests:**

* `tests/unit/worker/test_merge_action.py`: pending checks produce a fenced
  park with a refund, and a refund past `MERGE_CI_MAX_WAKES` counts as an
  attempt.
* A new `tests/unit/control_plane/test_merge_wake.py`: the route writes the
  marker only when settled, at most once per `CI_READ_SECONDS`, and only for
  its own tenant's parks with its own tenant's token.
* `tests/unit/control_plane/test_scheduler_ci_wait.py`: promotion happens on
  the marker or the fallback and writes READY only; an exhausted park is
  dead-lettered.
* A `terraform test` assertion: the job's description carries
  `managed-by=swarm-terraform`, and its OIDC account is the sweeper.

#### MS3 -- the merge action: branch update, split refusals, issue closing

**What:**

* `update-branch` with `expected_head_sha`, behind the fencing recheck.
* The first-parent walk.
* `MERGE_MAX_BRANCH_UPDATES`.
* The refusals `from_fork`, `base_not_default`, `protection_refused`,
  `merge_conflict`, `checks_timeout` and `behind_too_often`, split out of
  today's codes.
* The close script's page rule (`issues_beyond_page`).

**Territory:**

* `apps/agent-worker/agent_worker/merge.py`: `GitHubMerger`,
  `_with_forge`, `_close_issues`;
* `tests/unit/worker/test_merge_action.py`;
* `tests/unit/worker/merge_world.py`.

**Depends on:** MS2.

**Tests:** in `test_merge_action.py`, against `merge_world.py`'s fake forge:

* behind leads to an update with `expected_head_sha`, and a re-park at the
  new head;
* an update 422 for a conflict gives `merge_conflict`;
* a fourth update gives `behind_too_often`;
* a head that is not a GitHub base-merge of the pushed head gives
  `head_moved`;
* a stale generation makes no update call;
* each new refusal code;
* 101 closing references close 100 and record `issues_beyond_page`;
* a parity case with `tests/unit/scripts/test_close_merged_issues.py` over
  the same references.

#### MS4 -- console: the merge card on the run and workflow pages

**What:** a card on the merge step showing three things:

* the state: waiting for CI (with the pending checks and the head), behind
  and updated (n of 3), merged (commit and issues closed), or refused (code
  and reason);
* the pull request link;
* `merge_wait.first_parked_at`.

A `CI_PENDING` park reads "waiting for CI", never "stalled".

**Territory:**

* `apps/swarm-ui/src/RunSteps.tsx`;
* `apps/swarm-ui/src/WorkflowViews.tsx`;
* `apps/swarm-ui/src/stepviews.ts`;
* `apps/swarm-ui/src/types.ts`;
* `apps/swarm-ui/src/__tests__/` (a new `merge.card.test.tsx`).

**Depends on:** MS2 (the park reason exists); MS3 for the update and
refusal fields.

**Tests:** in `merge.card.test.tsx`, one render per state; no
`stalled`/`blocked` wording on `CI_PENDING`; the pull request link is built
from the result, never from caller text.

#### MS5 -- plugin rows: `swarm_workflow`, `swarm_trouble`, `swarm_follow`

**What:**

* `CI_PENDING` gets its plain-language row: "waiting for the pull request's
  checks".
* `swarm_trouble` leaves it out, as it leaves out dependency parks.
* The merge result gets a compact row: merged / refused `<code>`, and the
  issues closed.

**Territory:**

* `apps/swarm-mcp/swarm_mcp/progress.py`: `_PARKED_BECAUSE`;
* `apps/swarm-mcp/swarm_mcp/compact.py`;
* `apps/swarm-mcp/swarm_mcp/render.py`;
* `tests/unit/mcp/`.

**Depends on:** MS2.

**Tests:**

* every `ParkReason` member has a `_PARKED_BECAUSE` entry;
* `swarm_trouble` omits `CI_PENDING`;
* the merge row renders each refusal code.

#### MS6 -- retire `auto-merge.yml`

**What:** §5's removals, after its gate.

**Territory:**

* `.github/workflows/auto-merge.yml`;
* `.github/workflows/application.yml`;
* a new `.github/workflows/close-merged-issues.yml` (if Q3 keeps it);
* `tests/unit/scripts/test_auto_merge_workflow.py`;
* `tests/unit/scripts/test_workflow_step_reachability.py`;
* `docs/ci.md`;
* `docs/runbooks/merge-app.md`;
* `CLAUDE.md`.

**Depends on:** MS3, plus 10 clean step merges (§5), plus Q2 answered.

**Tests:**

* the reachability test no longer lists `auto-merge.yml`;
* `test_close_merged_issues.py` runs against the new workflow if it is kept;
* a doc test that no doc still tells a reader to label `ready` to merge.

#### MS7 -- the CI-fix hand-off for workflows that are not issue runs

**What:** the tick claims a round when it reads red with rounds left, and
submits `issueci.ci_fix_workflow`'s continuation. The worker accepts the
fix round's head by the continuation rule.

**Territory:**

* `apps/swarm-api/swarm_api/mergewake.py`;
* `apps/swarm-api/swarm_api/issueci.py`: `ci_fix_workflow` and
  `_pushing_task` made callable from it, behaviour unchanged;
* `apps/agent-worker/agent_worker/merge.py` (the head-acceptance rule);
* `tests/unit/control_plane/test_merge_wake.py`.

**Depends on:** MS3.

**Tests:**

* red with rounds left claims once and submits once, even across two racing
  ticks;
* red with none left wakes the step to `checks_failed`;
* the head a fix round pushed is accepted, and any other head is
  `head_moved`;
* `tests/unit/control_plane/test_issue_run_auto_merge.py` is unchanged and
  green.

### 7. Epic #352, re-triaged against this plan

| box | triage |
|---|---|
| 1. CR 33 decided (`worker_action`, `merge` profile, MERGE_REFUSED/MERGE_FAILED) | done: accepted 2026-09-29, applied 2026-10-01, enabled by CR 47 on 2026-10-04; the end causes are this step's |
| 2. CR 35 (`post-verdict`) decided | superseded because the verdict is no longer anchored on an App's GitHub review (§3); decided and applied 2026-10-01, disabled, removal is request (B) |
| 3. CR 36 (`claude-code-review`, `restore_on_retry`) decided | superseded because the review-only-writable prefix it served is gone with the review App (§3); decided and applied 2026-10-01, disabled |
| 4. Terraform per-tenant review/post-verdict/merge accounts and the `worker_objects` split | superseded because the merge Job runs as the tenant's worker account on `-git` (CR 47); the accounts exist on main, unused by the merge; their removal is a Track C follow-up (owner question MS0-Q4) |
| 4a. Never restore a checkpoint on `claude-code-review`/`post-verdict`/`merge` | done: `never_restore_checkpoint` is read by the lifecycle (`tests/unit/worker/test_never_restore_checkpoint.py`); a worker action keeps no workspace either way |
| 5. `register-tenant.sh` refuses `git-merge`/`git-review` on the worker account; the bucket split | superseded because there are no `git-merge`/`git-review` credentials; the refusal is harmless and stays until request (B) |
| 6. swarm-api `single-pr`, `pr_role`, the `merges` block, the refusals, #342 verification | superseded because the merge is appended to any `integrate` or one-step `direct-pr` workflow (CR 47); the #342 verification half is done (CR 34); the new submission work is kept as MS1 |
| 7. Worker: `pr-title.txt`, `pr_role`s, `post-verdict`, the merge action, the `auto-merge.yml` parity test | superseded because `single-pr` and `post-verdict` are (§3); the merge action is built (CR 47); the branch update, the split refusals and the close parity are kept as MS3; the wait is kept as MS2 |
| 8. Owner: create the review and merge Apps, the ruleset dry run, Q3/Q4 | superseded because there are no Apps (§3); the owner-side step left is retiring the `swarmcloud-merge` App, kept as MS6 |
| 9. Move `release.yml`'s `id-token: write` to job level | done: `release.yml` grants it per job (`tests/unit/scripts/test_release_id_token_scope.py`); separate from this plan, and never gated on it |

### Frozen-contract requests this plan needs (requests, not changes)

**(A) `ParkReason.CI_PENDING`** in `apps/common/swarm_common/states.py`, after
`CHILDREN_INCOMPLETE`, with the docstring "a merge step waits for its pull
request's checks; promoted by the scheduler's CI-wait sweep on the wake
marker or the fallback instant."

None of the existing members fits:

* `SCHEDULED_RETRY` is promoted by time alone and counts every wake as an
  attempt, which is today's behaviour;
* `DEPENDENCY_INCOMPLETE` is promoted when the workflow's upstream steps end,
  which they already have;
* `CHILDREN_INCOMPLETE` names child tasks, and the observer tools treat it
  that way (`apps/swarm-mcp/swarm_mcp/progress.py::_PARKED_BECAUSE`).

MS1 files it in `docs/contract-change-requests.md`. MS2 applies it on
acceptance, with its mirrors: the UI's park list, the MCP rows, and
`check-contract-parity.sh`. The owner can instead choose to keep
`SCHEDULED_RETRY` (owner question MS0-Q1). If so, MS2 parks on it with a
`blocked_by` reason of `CI_PENDING` and no refund, and nothing frozen
changes.

**(B) Retire the disabled `single-pr` catalogue entries.** These are the
`post-verdict` and `claude-code-review` profiles,
`WorkerAction.POST_VERDICT`, and `VERDICT_REFUSED`/`VERDICT_FAILED` --
dead since 2026-10-04 and kept disabled. It is not in this plan's lanes. It
is listed so that the decision is the owner's rather than an omission.

## Revised 2026-10-04 (owner)

**Status: BUILT for the merge itself, on new terms.** The owner decided on
2026-10-04 (recorded on #295; contract request 47 in
[contract-change-requests.md](contract-change-requests.md)) that merging moves
off the GitHub-side App (`.github/workflows/auto-merge.yml` and the
`swarmcloud-merge` App) into a workflow `merge` step. The trigger was #569:
the App merged #564 and #562 and left #72 and #560 open although GitHub listed
both as their closing references. #342 (signed step specs) is closed, so the
gate this document set in §0 consequence 4 and §10 is lifted. The decisions,
and what each one overrides below:

1. **The credential is the tenant's EXISTING `-git` token**
   (`swarm-tenant-<tenant>-git`), not a merge App's installation token.
   Overrides §2.1 (the App key, `-git-merge`, and the minted, revoked
   installation token), §1.3's own service account for the merge Job (it now
   runs as the tenant's worker account, `terraform/infra/locals.tf`), and
   requirement 3 of the list below. **Why:** the owner accepted, on #476, that
   an agent holding `-git` can also merge. That token already pushes the
   branch; a separate merge identity bought no isolation from an agent that
   holds it, at the cost of an App per tenant that was never created. **What
   §2.2 keeps:** the #219 ordering. The token is read at merge time only,
   after the reap and every check that needs no credential, is registered
   with the log redaction before use, travels only in the Authorization
   header, and is never written to the workspace, a file, an agent's
   environment, an event, a log line or `result_summary`
   (`tests/unit/worker/test_merge_token_isolation.py` runs the production
   worker and looks for it everywhere).
2. **Any repository a workflow runs on.** Owner and repo are parsed from the
   workflow's own `repository_url`, which the signed spec covers. Overrides
   §2.1b's Terraform-rendered `FORGE_HOST`/`FORGE_OWNER`/`FORGE_REPO` on the
   Job (the merge Job carries none now); `post-verdict` keeps them. The one
   forge served is GitHub (`agent_worker.merge.GitHubMerger`, the only
   `ForgeMerger`), on github.com, the only host the tenant's token is ever
   sent to (#307). Another host is refused at SUBMISSION
   (`validation.refuse_unmergeable_forge`), never at merge time.
3. **It closes the issues the pull request closes.** After a merge -- or on
   finding the pull request already merged at the pinned head, so a lost
   attempt's issues are not left open -- it reads GitHub's
   `closingIssuesReferences` (GraphQL) and closes each one in the same
   repository that is still open, with the comment
   `Closed by #<pr>, merged by SwarmCloud task <task_id>`. A `part of #N`
   pull request names no closing reference, so it closes nothing. New; §5.4
   only recorded the merging task.
4. **An opt-in final step, a platform default or per job.** `merge_by_default`
   (Firestore `control/settings`, served and set by `/v1/admin/settings`,
   default **off**), and a workflow's `metadata.merge` `"on"` | `"off"`,
   absent meaning the platform default. When the choice is on, the spec states
   no merge step and the workflow opens ONE pull request (an `integrate`
   workflow, or a one-step `direct-pr` one), swarm-api appends one `merge`
   step before signing, depending on the step that opens the pull request and
   on the review when there is one. **The rule for a stated step:** it is
   honoured unless `metadata.merge` is `"off"`, which contradicts it and is
   refused; `"on"` beside it appends nothing more. Overrides §1's `single-pr`
   chain (implement, review, post-verdict, fix, proof, merge) as the only way
   to merge: that chain stays proposed and disabled, with `post-verdict` and
   `claude-code-review`. [workflows.md](workflows.md#ending-in-a-merge-the-merge-step)
   has the table and the example spec.

What the built step checks, and how this differs from §4-§5:

* **Which pull request (§3, §4.1).** The signed dispatch block
  `merge_target` names the task that opened it -- the integrator, or the one
  `direct-pr` step -- and the review. The number and the pushed head are that
  task's recorded result: claims, so the live pull request must be that
  task's own `swarm/<id>` branch (or the branch it continues), from no fork.
  No `merges` block, no proof step.
* **The pinned sha (§4.2).** The head the opening step pushed. The pull
  request's head must equal it (`head_moved` otherwise, and on a 409 from
  the merge call), and the squash merge sends it as `sha`.
* **The verdict (§4.3, §5.2a).** The review's verdict FILE, staged by the
  merge step from the review exactly as the gated step stages it (#264), must
  say `MERGE`; `NOT_YET` is `verdict_not_merge`. It is not an App's
  GitHub review: there is no review App. This is the weaker anchor §4.3
  rejected -- the file is written under the tenant identity -- and it is
  accepted with the `-git` decision: an agent that could forge the file
  holds a token that can already merge.
* **The checks (§5.2).** The required set is the base branch's rulesets
  (`rules/branches/{base}`) AND its classic protection (`branches/{base}`),
  because a repository may use either. Each required check must be success
  or skipped at the pinned head (neutral is not); a rule that pins an App
  (`integration_id`) is matched to that App's runs. **M5 is relaxed:** a
  required check with no App is matched by name -- check runs and commit
  statuses -- rather than refused, because a repository that is not
  SwarmCloud's commonly requires checks that way. A base branch that requires
  NO check needs every reported check green (success, skipped or neutral) and
  at least one to exist.
* **Waiting for CI (invariants 1 and 4).** A required check still running, no
  check reported yet on an unprotected branch, or mergeability not computed
  fails the ATTEMPT retryably; the step waits READY for 300 s, holding
  nothing, and reads every fact again. The step gets 10 attempts
  (`MERGE_STEP_MAX_ATTEMPTS`). Every other refusal is final.
* **The merge (§5.3).** Squash, the pull request's title as
  `<title> (#<n>)`, `sha` pinned. Never resent: a merge call that did not
  answer ends `merge_unanswered`, MERGE_FAILED, because whether it merged is
  GitHub's to say. 405/422 are `not_mergeable`; 401/403/404 are
  `token_lacks_rights`.
* **The record (§5.4).** A pull request comment, `Merged by SwarmCloud task
  <task_id> ...`, the same text as the squash commit's body, with no
  attribution line.

**Where the default does not apply.** On a repository on a host no
`ForgeMerger` serves, `merge_by_default` appends nothing: the worker harvests
a patch there and opens no pull request, so an admin turning the default on
must not refuse every such tenant's workflow. An explicit `"on"`, or a stated
merge step, on such a host is still refused at submission.

**Issue runs merge from the CI loop, not from their workflow.** A run's
`auto_merge` takes the platform default when the run does not say. A merge
appended inside the run's compiled workflow would run before the run reached
CHECKING: before the API writes the `Closes #N` block into the pull request
(every compiled prompt forbids agents to write one), so the merge would read
no closing reference and close nothing -- #569 again -- and before the CI loop
could fix a red check, so a red or slow CI would fail the workflow, and the
run with it. So the compiled workflow and every CI fix round say
`metadata.merge: "off"`, and the CI loop (`issueci._merge`) submits ONE
merge-only continuation -- `continues_task` naming the run's task that pushed
the green head (the integrator, or the newest fix round's task), and one
`merge` step -- once CI is green at that head AND the keyword block is
recorded written, and only when the review's verdict is `MERGE`. The run
stays CHECKING while the merge runs and is DONE when GitHub reports the pull
request merged; a review verdict other than `MERGE`, a head no task of the run
pushed, or a merge the step refused is FAILED with the reason, the pull
request left open and green for a person, and no merge is resubmitted for
that head. A CI fix round is not re-reviewed (#454): the merge after one
rests on the review's verdict of the code before the round and on green
required checks. A merge-only continuation is a tenant member's: the
continuation-scoped CI-fixer account (request 30) is refused one.

**A merge-only continuation verifies its task against THAT task's workflow
(#900).** The task it merges ran in an earlier workflow -- the run's, or a
fix round's -- and the merge runs in its own. Verifying the task's signed
spec against the merge's own workflow, as every other upstream is, made
every issue run's auto-merge end `spec_unverified ... workflow_mismatch`. So
the signed `merge_target` also names `pull_request_workflow`, the task's
workflow as swarm-api read it from the task's record at submission
(`continuation.resolve_continuation`), and the worker verifies the task's
spec against that (`merge._verify_opener`): a forged or edited spec is still
`signature_mismatch`, an honest spec of any other workflow still
`workflow_mismatch`. Authority is bound twice, separately from the
signature. The issue-run loop refuses to submit unless the run's record names
that task and workflow (`issueci.merge_target_unbound`: `run.pr_task_id` of
`run.workflow_id`, or a CI-fix task of one of `run.ci_fix_workflows`), and
the worker refuses (`workflow_mismatch`) unless its own signed dispatch block
`continues` the branch the task's pull request is on. A merge inside the
workflow that opened the pull request names no `pull_request_workflow` and is
checked against its own workflow exactly as before.

**Operator step: the merge Job needs `git` in the tenant's providers.** The
merge profile now runs on the tenant's `-git` token, so its Job exists only
for a tenant whose tfvars `providers` lists `"git"`
(`terraform/infra/locals.tf`'s job matrix). Until an operator adds it
(`scripts/register-tenant.sh --tenant <tenant> --add-provider git`, then apply), there is no
Job to dispatch a merge step to, for that tenant.

What these decisions remove, and the residuals that follow:

* **The protected-path refusal (§5.1, §7 T6, M4) is gone.** It refused a pull
  request touching `.github/`, `scripts/`, `terraform/` and the build files of
  SwarmCloud's own repository; in an arbitrary repository it would refuse
  ordinary work, and with `-git` it was never a barrier to an agent. The
  repository's own branch protection is what holds a sensitive change.
* **The human gate (B13r) is not wired into this step.** A person who wants
  to merge by hand sets `metadata.merge` to `"off"`.
* **The platform default is a Firestore document.** `control/settings` sits
  beside `control/dispatch`, and like it is writable by any identity with
  project-level Firestore write, tenant worker accounts included (Firestore
  has no document-level IAM, §1.3). Such an identity could turn
  `merge_by_default` on for workflows submitted afterwards. A job that says
  `metadata.merge: "off"` is unaffected, and a merge still needs the
  workflow's own review verdict and green required checks. Recorded here
  rather than closed.
* **`auto-merge.yml` stays until the step is proven**, then is retired
  ([ci.md](ci.md#a-ready-pull-request-is-merged-by-github-not-by-a-session)).
  Its parity cases still hold `title_is_placeholder`, `other_check_blocks`
  and `required_check_state`, which the new step uses.

The sections below are the design as accepted on 2026-09-29, and stand
except where this section overrides them.

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
  §11 open question (b)). **Status, 2026-10-01: closed for every workflow
  once #457 is merged and the bootstrap root is applied** — see R4.
  The bootstrap apply, and the check that it took effect, is
  [runbooks/deployer-trust-pin.md](runbooks/deployer-trust-pin.md). It is still
  pending.
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
* Minors, round 4, joint review with CR 34, **corrected in later rounds
  where noted:** the real bucket layout is a shared bucket under
  `tenants/<t>/`, not a per-tenant bucket (§4.3, §10); GCS IAM is
  allow-only, so the review-only-writable prefix is two conditioned
  bindings replacing `worker_objects`, not a deny rule (§4.3, §10); **the
  review SA does NOT get `storage.objects.delete` — round 5's owner
  decision made its grant `objectCreator` only, and this note's earlier
  claim that delete was needed for a legitimate retry is stale (§4.3)**; CR
  33 keeps its own `*_REFUSED`/`*_FAILED` vocabulary — the two designs' end
  causes are not unified — so an upstream spec failure reports at
  **`result_summary.spec_check.reason = "upstream:<task id>:<why>"`, CR
  34's own field, not a `{code, message}` pair on the refusal itself as an
  earlier draft of this note said (§6)**; the checkpoint-restore fix this
  design needs (no restore on `review`/`post-verdict`/`merge`, on any
  attempt, as its own build item — not merely inherited from S0 #347, which
  does not cover these profiles) is §10's own item, not a ride on #347's
  fix (§1.3); the ordinary worker account's real name is
  `swarm-agent-worker-<tenant>`, not `swarm-<tenant>` (throughout); `merge`
  verifies the **union** of upstream signed specs — `author`, `review`,
  `post-verdict`, `fix`, `proof` — not only the ones named in its own
  `input_from` (§4.2, §6 row 42); and rows 42 and 6a-5 no longer phrase
  "#342 not built" as a runtime-checkable condition — it is a precondition
  for the feature existing at all, not something a worker asks at request
  time (§6, §6a).

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
    merge"), not the checkpoint. **Corrected, round-5 re-review, 2026-09-29:
    "no attempt-1 restore" and "never restore on `claude-code-review`,
    `post-verdict`, `merge`" are two different fixes, not one.** No
    attempt-1 restore for any profile (attempt 1 has nothing to restore
    from) is a platform-wide bug tracked separately as S0 #347, fixed
    elsewhere, not by this design. **#347 does NOT cover
    `claude-code-review`, `post-verdict` or `merge` specifically — never
    restoring on those three profiles, on *any* attempt including retries,
    is this design's own requirement, and needs its own §10 build item with
    its own test (§10), not a ride on #347's fix.** `merge` and
    `post-verdict` need it because there is no runner to restore for, ever.
    `review` needs it for a different reason: it must never resume an agent
    inside a workspace a previous, possibly-compromised attempt left behind
    — its whole judgement depends on seeing the checked-out head honestly,
    and a carried-over workspace is exactly the kind of tampering vector
    §0's own threat model already takes seriously elsewhere. **ACCEPTED
    RESIDUAL, round-5 re-review, 2026-09-29: the checkpoint-plant-on-retry
    path that goes through Firestore is not closed by any of the above, and
    this design does not close it.** A checkpoint's own metadata (what to
    restore, and from where) is Firestore-capable — read and written through
    the same worker path every other Firestore access in this design
    already treats as tenant-writable (§0) — so a tenant identity that could
    plant a malicious checkpoint record could, in principle, have it
    "restored" the next time a profile that *does* restore retries. This is
    a platform-wide exposure, not specific to `review`/`post-verdict`/`merge`
    (which never restore at all, closing it for exactly those three), and
    the real fix — moving checkpoint metadata off a store any tenant worker
    can write — waits on workers leaving Firestore for that purpose
    entirely, which is out of scope here. Recorded, not built.
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
  `review.json` lands. **Corrected (2026-09-30, #364): this is not the "no
  portable secret" case it was first written as.** A prompt-injected review
  agent (T11) does not need to exfiltrate anything — it can mint
  `swarm-<tenant>-review`'s own token from the metadata server (§0) while it
  runs, and use that token, while it is valid, against the `objectCreator`
  grant the account already holds on the whole `tenants/<t>/verdicts/`
  subtree, to write a fabricated verdict at another workflow's path within
  the same tenant's subtree. This is exactly the residual §7 T3b / **R8**
  records, and the owner has accepted R8 as open, not closed by running
  `review` on its own profile;
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
| (none, no clone) — settled, round-6 re-review: `post-verdict` clones nothing, so its `pr_role` is `none`; CR 34 changed its own value to match this, not the other way around | nothing — it runs no agent | submits the GitHub PR review with the review App's key (§4.3) | `post-verdict` (worker-action profile, new 2026-09-29) |
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
* the `post-verdict` step has **no `input_from` at all** — fixed, round-6
  re-review: an earlier draft of this list said `post-verdict`'s
  `input_from` names the review's `review.json`, contradicting §4.3, where
  `post-verdict` reads only the verdicts-prefix path it derives from its own
  signed `dispatch.verdict_source` (§4.3's `verdict_source: {review: <review
  task id>}`), never through `input_from`'s ordinary staging. It still
  depends on `review` directly (`"depends_on": ["review"]`) and on nothing
  else, for DAG ordering alone;
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
is the upstream task id named in `post-verdict`'s own dispatch block, under
the key **`verdict_source: {review: <review task id>}`** — named here,
round-5 re-review, so the key is not left implicit — the same way
`merges: {author, review, ...}` names task ids for `merge` (§4.2).
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

**Real bucket layout and real grant, corrected (round-5 re-review, joint with
CR 34, 2026-09-29):** this platform's artifacts live in one shared bucket
per environment, under `tenants/<t>/` — not a per-tenant bucket, and not
`<tenant-bucket>/<tenant>/` as an earlier draft of this section said. The
tenant worker SA's existing grant is the **`worker_objects` binding**,
defined in Terraform at `terraform/modules/tenancy/main.tf:164-183` — that
is the primary grant; `register-tenant.sh:961` is the separate `gcloud` path
that applies the equivalent binding outside Terraform's own apply cycle, not
a second, independent grant. **Binding 1 is NOT unconditioned — an earlier
draft of this section said it was, at what were then lines :865/:872/
:1644/:1666, and that was wrong.** On a bucket shared by every tenant,
an unconditioned `objectViewer` grant is cross-tenant read, which breaks
invariant 9 outright. `worker_objects` today carries **two** clauses, both
kept: the `resource.name` prefix (`startsWith(".../objects/tenants/<t>/")`)
*and* an `objectListPrefix` clause (GCS's own mechanism for scoping `list`
calls to a prefix, without which a conditioned binding still permits listing
outside the prefix). **IAM on Cloud Storage is allow-only — there is no deny
binding to carve an exception out of a broad grant** — so closing off
`tenants/<t>/verdicts/` means replacing `worker_objects` with two narrower
bindings, not adding a third:

1. **`roles/storage.objectViewer` on `tenants/<t>/`, keeping both of
   `worker_objects`'s existing condition clauses** (the `resource.name`
   prefix and the `objectListPrefix` clause) — the tenant worker SA keeps
   ordinary read and list everywhere under its own tenant prefix, including
   `verdicts/`, so `fix`'s `input_from` staging of `review.json` (§4.1) is
   unaffected, and cross-tenant read stays closed exactly as
   `worker_objects` already closes it;
2. **`roles/storage.objectUser` on `tenants/<t>/`, conditioned to
   `resource.name.startsWith("projects/_/buckets/<bucket>/objects/tenants/<t>/") && !resource.name.startsWith("projects/_/buckets/<bucket>/objects/tenants/<t>/verdicts/")`**
   — write everywhere under the tenant's prefix *except* `verdicts/`. This
   binding carries **no `objectListPrefix` clause of its own** — it relies on
   binding 1 for listing, which is why binding 1 must exist and stay
   conditioned rather than being dropped once binding 2 is added. This
   replaces `worker_objects`; it is not an addition alongside it, since two
   overlapping write grants would defeat the exclusion;
3. **the review SA, `swarm-<tenant>-review`, gets `roles/storage.objectCreator`
   — not `objectUser`, and with NO delete — on `tenants/<t>/verdicts/`
   alone**, per the owner's decision, round-5 re-review: `objectCreator`
   grants only `storage.objects.create`, never `update` or `delete`, so the
   review SA cannot overwrite an object once written, by design, not merely
   by convention. **A failed review becomes a new task, at a new path, not a
   retry that overwrites the old one** — task ids are unique, so a fresh
   attempt at review (dispatched as a new task, however the caller chooses
   to recover from a failure) lands at
   `tenants/<t>/verdicts/<workflow_id>/<new review task id>/review.json`, a
   path the old attempt, and any attacker who only knew the old task id,
   never touched. This is why no delete grant is needed at all — there is no
   legitimate reason to overwrite an existing verdicts object, ever;
4. **the review SA's other grants — its own artifacts and checkpoints
   (round-5 re-review, minor named MAJOR):** `review` is still an
   agent-running `claude-code-review` profile (§1.3), so its worker needs
   the *same* two-binding shape the ordinary tenant SA has — `objectViewer`
   and the excluding-`verdicts/` `objectUser` — scoped to its own task's
   ordinary artifact and checkpoint paths under `tenants/<t>/`, for the same
   reasons every other agent-running profile needs them (staging its own
   inputs, writing its own checkpoint, §1.3's restore note). The review SA
   is not exempt from the platform's ordinary per-task GCS access merely
   because it also holds the `verdicts/` grant; it needs both.

**Conditions of this shape require Uniform bucket-level access on the
bucket** (fine-grained ACLs disabled) — already true, since `worker_objects`
is itself conditioned today, so this is not a new bucket-level prerequisite
this design introduces.

**Removing the old broad grant is its own step, not a side effect of adding
the new ones (round-5 re-review, MAJOR 1c).** Every tenant already
registered has `worker_objects` bound as the old, unconditioned-on-`verdicts/`
form; replacing the Terraform resource for *new* tenants does nothing for
those already applied. §10 needs an explicit removal step, in **both**
places the grant is set: the Terraform module (a `terraform apply` that
retires the old `google_storage_bucket_iam_member`/`iam_binding` resource
for every existing tenant, not only stops creating new ones) and
`register-tenant.sh` (whose current idempotency check — "already binds
`objectUser`, skip" — would otherwise leave an already-registered tenant's
old broad binding in place forever, silently defeating the split for every
tenant that isn't newly registered after this change ships).

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
* **in-chain forgery (the fix agent, in this workflow) is closed by the
  PREFIX, not by ordering — corrected, round-5 re-review, 2026-09-29.**
  `fix` runs as the ordinary tenant worker account, which binding 2's
  exclusion leaves with no write grant on `tenants/<t>/verdicts/` at all
  (§4.3 above); `fix` cannot overwrite `review.json` whether it runs before
  or after `post-verdict`. `fix` depending on `post-verdict` rather than
  `review` (§3) still matters, but for a different reason than file
  integrity: it keeps `fix` from acting on a verdict before that verdict is
  anchored on GitHub, the same principle the original attestation design
  used ("the fix agent starts only after the attestation is written"),
  applied here to *readiness*, not to *write access*;
* **cross-workflow verdict laundering (R8) — the write-path attack is
  closed, round-5 re-review; the App-token half is not, and the owner still
  did not choose to bind a review to a specific pull request or task id.**
  `swarm-<tenant>-review` is one identity shared by *every* `single-pr`
  workflow of the tenant, not scoped per-workflow (the same "one Job per
  tenant per profile" constraint that shapes every other identity in this
  design, §1.1). Its GCS grant (`objectCreator` on the whole
  `tenants/<t>/verdicts/` subtree) is likewise not workflow-scoped — but
  combined with write-once and no delete (below), a review agent of
  workflow A that writes to workflow B's path either loses the race outright
  or wins one that workflow B's own `post-verdict` never ends up depending
  on, because a review that lost its write never reaches `SUCCEEDED` (§4.1,
  R8 below has the full argument). **A minted `-git-review` installation
  token is still a portable bearer credential**, though: it authorises
  `pull_requests: write` on the whole installed repository, not one PR, so
  even a correctly-anchored review from `post-verdict` proves "the App
  approved this exact commit," not "the App approved it *on behalf of this
  workflow specifically*" — the same tenant-wide, not workflow-scoped,
  granularity problem, one layer up, and the part R8 still records as open;
* **write-once (`ifGenerationMatch=0`) is a PARTIAL mitigation — corrected,
  final review, 2026-09-29: the previous draft's "no window left" claim was
  wrong on all three of its premises.** `review` publishes `review.json`
  with GCS's compare-and-swap precondition, so whichever write reaches a
  given exact path *first* wins, and every later write to that same path
  fails outright. What this buys is real but narrow: **the legitimate side
  of a lost race fails loudly** — a genuine `review` task whose write
  collides with an attacker's prior write gets a precondition failure it can
  surface as an attempt failure, rather than silently losing to an
  overwrite. It does not close R8. Three premises the earlier draft relied
  on to claim full closure are false:
  * **a same-task retry reuses the same path, it does not move to a new
    one.** The path is `f(workflow_id, review task id)` (§4.3), and a task's
    id is stable across its own attempts — only the attempt id changes on
    retry. "A failed review becomes a new task and a new path" was true only
    for a *caller-initiated* fresh dispatch, not for the platform's own
    retry of the same failed attempt, which is the ordinary recovery path
    and lands at the identical, already-poisoned path;
  * **"`post-verdict` only starts once `review` succeeded" rests on the
    task's `state` field, which #342 does not sign and which is itself
    tenant-writable (CR 34 lists this gap).** Dependency promotion reads
    `state`, not the spec signature; #342 protects the *prompt* a step
    executes, not the *execution-outcome record* the scheduler trusts to
    decide who runs next. A tenant identity that can write `review.json` to
    an unpoisoned path can, by the same §0 grant, also write `review`'s task
    document's `state` straight to `SUCCEEDED` — a **plant-then-state-
    rewrite** attack: plant a forged `review.json` at the correct path, then
    set `state: SUCCEEDED` on the review task directly, without ever running
    a real review agent or winning any GCS race at all;
  * **misdirecting `post-verdict` needs only a Firestore write, not a GCS
    race, because the pull request number it acts on comes from the
    author's `result_summary`** (§4.2), which is exactly as tenant-writable
    as every other `result_summary` field §0 already treats as a claim. An
    attacker does not need to forge `review.json`'s *content* convincingly
    if it can instead redirect which pull request the anchored review lands
    on.

  None of this is closed by write-once, by `objectCreator`-only, or by "no
  delete." **R8 stays OPEN** (§7);
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

**Interop with CR 34, corrected round-5 re-review, 2026-09-29: CR 33 keeps
its own `*_REFUSED`/`*_FAILED` vocabulary as the `end_cause` — the owner did
not unify the two designs' end causes — but the failure detail goes in
`result_summary.spec_check.reason`, the SAME field CR 34 itself writes, not
a CR-33-only field.** Row 42 and §6a row 5 both set `end_cause` to
`MERGE_REFUSED`/`VERDICT_REFUSED` as already described, and separately write
`result_summary.spec_check.reason = "upstream:<task id>:<why>"` — for
example `upstream:task_review_abc123:unsigned` or, **corrected, final
review, 2026-09-29: `upstream:task_fix_def456:signature_mismatch`, not
`SPEC_SIGNATURE_INVALID`.** **`<why>` is CR 34's own reason vocabulary,
verbatim — `unsigned` for a spec with no signature at all,
`signature_mismatch` for one that fails verification, and whatever others
CR 34 defines — not a CR-33-invented term like the earlier draft's
`spec_missing`, and not `SPEC_SIGNATURE_INVALID` either, which was the
wrong case and the wrong term for this field.** CR 33 only adopts the field name and the
`upstream:<task id>:<why>` format; a reader or a test correlating a
`MERGE_REFUSED`/`VERDICT_REFUSED` row against CR 34's own `spec_check`
reporting reads the identical field, the identical task id and the
identical reason string in both
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
| T14 | rewrite a downstream step's own `prompt`/`input`/`input_from` in its Firestore task document before that step starts — for example, `implement` rewrites the still-`PARKED` `review` task's `prompt` to bias its judgement, or rewrites what `post-verdict` or `merge` would read as their own effective instructions | **BLOCKER, round-3 re-review, 2026-09-29 — not stopped until #342 ships.** §0 already grants any tenant identity write access to any Firestore document whose id it knows; a step's own spec was never excepted. This is a different attack from T10 (rewriting the *merge task's* dispatch block) and from T11 (influencing the review agent through diff *content* it reads): T14 rewrites the *instructions* a later step is handed before that step ever starts, so the later step's own execution is completely honest and still wrong. **Owner decision: signed step specs (S0 issue #342).** swarm-api signs each step's canonical spec at submission with a KMS key only it holds; every worker verifies its own spec's signature before starting an agent or a worker action, and `post-verdict` and `merge` additionally verify the **upstream** specs the artifacts they trust depend on (`post-verdict` verifies `review`'s spec; `merge` verifies the union — `author`'s, `review`'s, `post-verdict`'s, `fix`'s and `proof`'s — §4.2, §6 row 42). This design uses CR 34's own field names verbatim, not new ones: the signed spec's `spec_signature`, `spec_key_version` and `spec_format` fields on the task document, and `signature_mismatch` (corrected, final review — not `SPEC_SIGNATURE_INVALID`) as one of the `<why>` values a failed verification reports (§6, §6a). This residual is **R7**, and it is the reason B1 is not fully closed by §4.3 alone (§0 consequence 4, §10) |

**Residual risks, stated plainly:**

* **R1.** T11, prompt injection of the review agent, so that the *real*
  review step, running as the *real* review identity, reaches the wrong
  conclusion and honestly writes `"verdict": "MERGE"` about a change it
  should not have approved. `post-verdict` carries whatever `review.json`
  says; it does not, and cannot, audit the judgement behind it. **This is not
  closed by B1's corrected design, and was never claimed to be** — B1 raises
  the bar for *fabricating* an approval without a real review step, not for a
  real one reaching the wrong answer.
* **R2. In-chain half only — closed, by design; cross-workflow half stays
  OPEN, as R8 — corrected 2026-09-29, five times now, since four earlier
  revisions of this document each overstated or misattributed part of
  this.** T3a — the `fix` agent, *this* workflow, forging `review.json` —
  is closed once the review-only-writable prefix is built exactly as
  specified (§4.3). It is the PREFIX's IAM write restriction that holds
  here, not DAG ordering: `fix` runs as the ordinary tenant worker account,
  which has no write grant on `tenants/<t>/verdicts/` at all under binding
  2's exclusion — `fix` cannot write there whether it acts before or after
  `post-verdict`. `fix` depending on `post-verdict` rather than `review`
  (§3) still matters, but for a different reason: it keeps `fix` from
  acting on a verdict that is not yet anchored, not from overwriting the
  file, which it could never do regardless. This claim is conditional on
  the build, because the status here is still PROPOSED — nothing is built
  yet (§10). **T3b — a different workflow's agent of the same tenant
  forging a verdict, or misdirecting `post-verdict` onto the wrong pull
  request — is R8, below, and stays OPEN.** A round-5 draft of this
  document claimed write-once plus `objectCreator`-only closed the
  write-path attack; that claim rested on three false premises (a same-task
  retry moving to a new path; dependency promotion depending on a signed
  state; `post-verdict`'s PR number being trustworthy) all corrected in
  §4.3, and the owner decided R8 stays open rather than building the fix
  that would actually close it. The first correction of this document
  asserted T3 closure while describing a mechanism (`claude-code-review`)
  that did not actually achieve it; the second closed only the in-chain
  half before the prefix existed and still called the whole thing closed;
  the third stated the cross-workflow half honestly as open; the fourth
  wrongly claimed round 5's owner decisions closed it after all; this, the
  fifth, restores it as open and states why plainly.
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

  **Status, 2026-10-01: closed for all workflows once #457 is merged and the
  bootstrap root is applied.** #455 moved `release.yml`'s grant to job level,
  onto the six jobs that authenticate to Google. Its review found the same
  exposure in three more places: `application.yml`, `terraform.yml` and
  `security.yml` granted `id-token: write` at workflow level and run on every
  push to `main`, and the deployer's binding (`attribute.repo_ref/<repo>@<ref>`)
  accepted a token from **any** workflow on `main` — so `application.yml`'s
  `python` and `integration` jobs, which run `pytest` over the merged tree,
  could still become the deployer. #457 closes it with two independent
  controls (owner decision, 2026-10-02, both):

  * **job-level grants** — every workflow that authenticates as the deployer
    declares `id-token: write` only on the jobs that run
    `google-github-actions/auth`, never at workflow level, and never through a
    string `permissions:` such as `write-all`
    (`tests/unit/scripts/test_workflow_id_token_scope.py`,
    `tests/unit/scripts/test_release_id_token_scope.py`);
  * **a trust pin** — the deployer's workload identity binding names
    `attribute.job_workflow_ref` = `<repo>/.github/workflows/<file>@refs/heads/main`
    for exactly the files that authenticate as it (`release.yml`,
    `application.yml`, `terraform.yml`, `security.yml`,
    `iam-refusal-probe.yml` and, since 2026-10-08, `hotfix.yml`;
    `terraform/bootstrap/wif.tf`
    `deployer_workflows`), so a workflow added later is refused whatever it
    grants itself.

  The first holds from the merge. The second is IAM in the bootstrap root,
  which only the owner applies; until that apply, a workflow file that is not
  on the list and grants itself `id-token` (for example `ci-fix.yml`, which
  does at workflow level) can still present a token the deployer accepts.
  The owner applies it, and checks the result against the live IAM policy, with
  [runbooks/deployer-trust-pin.md](runbooks/deployer-trust-pin.md):
  `scripts/verify-deployer-trust.sh` before (it fails, naming the `repo_ref`
  member), `scripts/bootstrap.sh --target
  'google_service_account_iam_member.deployer_wif'`, then the verifier again
  (`checked N members, all pinned to workflow files`) and the next run on `main`.
  This closes only when that runbook's log has a row.
  What remains by design: every job that legitimately authenticates runs
  merged code as the deployer, which is what `touches_protected_paths` (T6)
  is for. Release `verify` does not authenticate; these do, each from a
  checkout of the merged tree: `build` in `application.yml` (runs
  `build-images.sh`); `plan` in `terraform.yml` (the merged Terraform);
  `images` in `security.yml` (scheduled or dispatched, runs its scan
  script as the merged workflow file writes it); `probe` in `iam-refusal-probe.yml` (dispatch-only, runs
  `scripts/iam-refusal-probe.sh` from `main`); and in `release.yml`
  `build`, `acceptance`, `infrastructure`, `infrastructure-iam`, `deploy`
  and `promote`. Read 2026-10-07 by listing every job that runs
  `google-github-actions/auth` in those five files. Since 2026-10-08 the hotfix
  lane adds `images`, `promote`, `infrastructure` and `deploy` in `hotfix.yml`
  (docs/ci.md, "Hotfix releases"); its `gate` does not authenticate.
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
* **R8. OPEN — corrected, final review, 2026-09-29: an earlier draft claimed
  the write-path half was closed. It was not, on three false premises (§4.3
  has the full argument), and the owner decided R8 stays open rather than
  building the fix that would close it.** `swarm-<tenant>-review` is one
  identity shared by every `single-pr` workflow of the tenant, and its GCS
  grant (`objectCreator` on the whole `tenants/<t>/verdicts/` subtree, §4.3)
  is not workflow-scoped. What does **not** close this, and why:
  * write-once (`ifGenerationMatch=0`) only makes the *losing* side of a
    same-path race fail loudly instead of silently losing — it does not
    decide who wins, and does not stop an attacker who wins from having
    their write consumed;
  * a same-task retry reuses the same path (task id is stable across
    attempts), so "the retry lands somewhere the attacker doesn't know" does
    not hold;
  * dependency promotion (`post-verdict` starting only once `review`
    "succeeded") reads the task's `state` field, which #342 does not sign
    and which is tenant-writable like every other Firestore field §0
    already treats as a claim — a **plant-then-state-rewrite** attack plants
    a forged `review.json` at the correct path, then sets the review task's
    `state` to `SUCCEEDED` directly, with no race and no need to beat a real
    review agent to anything;
  * `post-verdict` can be misdirected onto the *wrong pull request* with
    only a Firestore write, no GCS access at all: the PR number it acts on
    comes from the author task's `result_summary` (§4.2), which is exactly
    as tenant-writable as `review`'s own.

  The minted `-git-review` installation token compounds all of this: it is
  a portable bearer credential authorising `pull_requests: write` on the
  whole repository, not one PR, so even a correctly-anchored review from
  `post-verdict` proves "the App approved this commit," not "the App
  approved it on behalf of this workflow specifically." Closing R8 would
  need per-workflow write and token scoping, or PR/task-id binding on the
  review and on `post-verdict`'s own dependency reads — considered, and the
  owner decided not to build it. This design ships with R8 open, accepted
  as a residual, not closed by anything above.

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
   (§4.3, real grant and layout, joint review with CR 34):** replace
   `worker_objects` (`terraform/modules/tenancy/main.tf:164-183`, the
   primary grant; `register-tenant.sh:961` applies the equivalent binding
   outside Terraform's apply cycle and needs the identical split, item 5)
   with two conditioned bindings — `objectViewer` on `tenants/<t>/` keeping
   both of `worker_objects`'s existing condition clauses (the
   `resource.name` prefix and the `objectListPrefix` clause, so read/list
   stays tenant-scoped), and `objectUser` on `tenants/<t>/` with the same
   prefix clause plus `&& !startsWith(.../tenants/<t>/verdicts/)`, no list
   clause of its own — and grant the review SA `objectCreator` (create
   only, no update, no delete) on `tenants/<t>/verdicts/` alone, plus the
   review SA's own ordinary `objectViewer`/excluding-`objectUser` pair for
   its own artifacts and checkpoints, the same as any other agent-running
   profile. **Include the explicit removal of the old, unconditioned-on-
   `verdicts/` `worker_objects` binding for every already-registered
   tenant** — a new Terraform resource definition does nothing for tenants
   applied under the old one. Requires Uniform bucket-level access, already
   true today. The Terraform-rendered `host`/`owner`/`repo`/`review_app_id`/
   `review_app_bot_id` Job environment values for `post-verdict` and `merge`
   (§2.1b, §11 open question (a), resolved). Also the `terraform test`
   assertion that no tenant identity holds `run.jobs.run`,
   `run.jobs.runWithOverrides` or `run.jobs.update` on the merge Job (§1.3),
   and its counterpart for the `review` and `post-verdict` Jobs.
4a. **Worker (Track B), its own item with its own test — round-5 re-review:**
   never restore a checkpoint on `claude-code-review`, `post-verdict` or
   `merge`, on any attempt, not only attempt 1. This is not covered by S0
   #347 (attempt-1 restore generally), which is being fixed separately;
   this design needs its own guard and its own regression test that these
   three profiles specifically never restore (§1.3).
5. **Scripts (Track D):** `register-tenant.sh --add-provider git-merge` and
   `--add-provider git-review` both refuse to bind the tenant's ordinary
   worker account; separate binding paths grant the `review` and
   `post-verdict` accounts accessor on their own secret alone. The
   `git-review` registration path also resolves `review_app_bot_id` using
   the App's own freshly-minted JWT, and emits it into the tenant's
   Terraform variables for the next `terraform apply` (§2.1b, §5.2a), rather
   than writing it anywhere at runtime. **`register-tenant.sh:961`'s own
   `worker_objects` binding gets the same split as item 4, at the same call
   site — corrected, round-5 re-review: the tenant worker account's grant
   changes from `worker_objects` (already conditioned to the tenant's own
   prefix, never cross-tenant) to the `objectViewer`-plus-excluding-
   `objectUser` pair**, so the script and the Terraform module it invokes do
   not drift from each other the way CLAUDE.md's own warning about restated
   rules describes. **The script's idempotency check must also change
   (MAJOR 1c): today it skips re-applying the binding when the tenant
   "already binds `objectUser`," which would leave an already-registered
   tenant's old, unconditioned-on-`verdicts/` binding in place forever.**
   The check needs to distinguish the old binding's shape from the new
   split and actively replace the old one, not treat any existing
   `objectUser` grant as already-done. **`register-tenant.sh`'s refusal is
   not the only place that binds these providers (found 2026-09-30, #364):
   the API credential routes (`POST /me/credentials`, `routes/tenants.py`;
   the admin route, `routes/admin.py`) accept whatever `known_providers()`
   lists, and `terraform/modules/secret_manager`'s `iam_binding` writes
   `members=[cfg.accessor]` — the tenant's ordinary worker account — for
   every listed provider, authoritatively, so it would also strip a
   separately-granted merge or review account on the next apply. Both need
   the same refusal as the script: the routes reject `git-merge`/`git-review`
   from a tenant-facing credential call, and `secret_manager` gains a
   per-provider accessor override so the merge/review account is the sole
   `secretAccessor` member. See contract request 33's amendment.**
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
