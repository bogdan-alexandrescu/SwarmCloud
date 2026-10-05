# The lane queue: implement → review → fix as a platform object

**Status: PROPOSED 2026-10-05, design and mock-ups only (functionality wave
8, lane LQD, epic #640; the lane outcome ledger of #639 is folded in, owner
decision 2026-10-05).** Nothing described here is built. Owner decision
2026-10-05 (history V1): design first, no product code. The screens are
drawn, in two or three variants each, in
[web-ui/mockups/lane-queue.html](web-ui/mockups/lane-queue.html). The one
frozen-contract consequence is written out in §14 for filing; it is not
filed by this lane. Build lanes are filed on #640 after the owner's picks.

What the design settles, in one line each:

* **A lane is a tenant's Firestore document** that generalises an issue run
  (#454): the same planner-or-brief → implement → review → fix workflow, the
  same CI loop after the pull request opens, plus what the laptop did by
  hand (§2).
* **A lane may wait for another lane's pull request to MERGE**, not merely
  to finish, and the wait is stored, so a restart re-types nothing (§3).
* **A lane holds a file-territory lock** derived from its plan's touched
  files, with seam files (`swarm_api/main.py`, `App.tsx`) handled by a
  policy the owner picks (§4).
* **After its pull request opens, a lane runs the existing CI-fix loop
  (#545), labels `ready` when green and reviewed, and closes or comments
  on its issue** (§5).
* **A verify-only lane succeeds with an empty diff** (`allow_empty_diff`,
  §6), and **review minors go to a sink**, not to the fix step (§7).
* **The laptop's spec generator, PR watchers, merge watcher, watchdog and
  memory-note launch queue are retired**; what is kept is named (§8).
* **One ledger record per lane**: cost over every attempt, wall and waiting
  time, the review, the pull request, CI reds by job, the merge, the issues
  closed or left (§13).

---

## 1. Why

The 2026-10-05 history analysis (sc-history-analysis.md, an artifact of that
day's session, not a file in this repository) counted **141
implement/review/fix lanes orchestrated from the operator's laptop against
3 native issue runs**. The laptop's tooling was a spec generator that wrote each
lane's workflow, PR watchers that waited for CI, a merge watcher, a watchdog
that restarted what died, and a launch queue kept in memory notes. What that
cost, measured in the same analysis:

* **Concurrency was bounded by the operator, not the platform.** claude-code
  peaked at **12 of 40** allowed slots. The other 28 were idle while lanes
  waited for a person to notice a merge and type the next launch.
* **Watchers died on network errors.** A watcher is a loop in a session; a
  dropped connection ended it, and the lane it watched sat green and
  unmerged, or red and unfixed, until someone looked.
* **Sibling lanes conflicted on shared files.** #588 and #589 both edited
  `apps/swarm-api/swarm_api/main.py` (router registration), and the second to
  merge needed a hand rebase. Nothing knew both lanes were in the same file.
* **Dependencies were re-typed after every restart.** "Launch X after PR Y
  merges" lived in memory notes and in the operator's head; every session
  restart re-typed them, and a mistyped one launched X against a main that
  did not have Y yet.

Issue runs (#454) already do a third of a lane natively: plan, compile,
run, watch CI, fix, merge. None of the four costs above is something an issue
run addresses, because an issue run knows nothing about any other run. The
lane queue is that missing knowledge: what this lane waits for, what it is
holding, and what to do when its pull request is green.

---

## 2. The lane

### 2.1 What it generalises

An issue run (#454) is `issue_runs/{run_id}` (`apps/swarm-api/swarm_api/issueruns.py:1178`,
`IssueRun`), moved by `RunState` (`apps/swarm-api/swarm_api/issueruns.py:225`)
through PLANNING → PLANNED → APPROVED → RUNNING → CHECKING ⇄ FIXING → DONE.
Its routes are `apps/swarm-api/swarm_api/routes/runs.py`; every read advances
it (`advance_run`, `apps/swarm-api/swarm_api/routes/runs.py:282`) and so does a
per-tenant Cloud Scheduler tick (`advance_tenant_runs`,
`apps/swarm-api/swarm_api/routes/runs.py:357`). The plan becomes a workflow
through `compile_plan` (`apps/swarm-api/swarm_api/issueruns.py:917`): the
plan's implementer steps, a review that writes `verdict.json`, and a fix gated
on `NOT_YET` that is the workflow's one publisher (`integrate`).

A lane keeps every one of those mechanisms and adds four things an issue run
does not have: **a dependency on another lane's merge**, **a territory
lock**, **a decision after green** (label `ready` or merge, then close or
comment), and **membership of a group** (a wave), which is what the board, the
minors sink and the ledger aggregate by.

A lane has one of three **sources**:

| source | where its plan comes from | what it compiles to |
|---|---|---|
| `issue` | a planner task, exactly as an issue run's (`planner_task`) | `compile_plan` over the planner's plan |
| `brief` | the caller's brief: title, work items, territory | `compile_plan` over a ONE-step plan built from the brief, `requirements` = the work items, `files` = the territory |
| `verify` | the caller's brief, read as "check, do not change" | a `collect` workflow (§6) |

`brief` is the laptop's lane: what the spec generator wrote by hand is now a
plan the API builds, validated by the same `PlanSpec`, so it is data and
never a profile, image or command (invariant 10). An `issue` lane with
`plan_approval: required` pauses at PLANNED holding nothing, as an issue run
does.

### 2.2 The states

The lane machine is the lane's own, stored on its own document. It is
**not** the frozen `TaskState`, and no lane state is written to a task: a
lane's tasks are ordinary tasks of ordinary workflows, in ordinary task
states.

| state | meaning | holds |
|---|---|---|
| `WAITING` | at least one `depends_on` is not merged yet | nothing |
| `PLANNING` | (`issue` only) the planner task runs | the planner task's lease, like any task |
| `PLANNED` | (`issue` only) the plan waits for its approval | nothing |
| `QUEUED` | dependencies met, plan known; waiting for its territory | nothing |
| `RUNNING` | territory held; the implement → review → fix workflow runs | territory lock; its tasks' leases while they run |
| `CHECKING` | the pull request is open; CI is read at its head | territory lock |
| `FIXING` | one continuation is fixing a red CI reading or rebasing (§4.3, §5.1) | territory lock; the round's lease while it runs |
| `READY` | green at the head, reviewed `MERGE`, keyword block written; labelled `ready` or merge submitted | territory lock |
| `MERGED` | GitHub says merged; `merge_commit_sha` recorded | nothing (released) |
| `VERIFIED` | a verify-only lane, or `allow_empty_diff` with nothing to change, finished | nothing (released) |
| `BLOCKED` | a dependency can no longer merge (failed, cancelled, closed without merging) | nothing |
| `FAILED` | the workflow, a round, the cap, the final review or the merge failed; `error` says which | territory lock while its pull request is open (§4.2); nothing once it is closed |
| `CANCELLED` | a person cancelled it | nothing (released) |

Transitions, each a Firestore transaction that re-checks the state it moves
from, as `IssueRuns.transition` does:

    WAITING  --every depends_on merged/verified------------> PLANNING (issue) | QUEUED
    WAITING  --a depends_on BLOCKED/FAILED/CANCELLED/closed-> BLOCKED
    PLANNING --plan.json read-------------------------------> PLANNED | QUEUED (auto)
    PLANNED  --digest approved (D3)-------------------------> QUEUED
    QUEUED   --territory acquired, workflow stored----------> RUNNING
    RUNNING  --integrator opened its pull request-----------> CHECKING
    RUNNING  --verify lane succeeded / empty diff allowed---> VERIFIED
    CHECKING --red, rounds left | merge conflict on a seam--> FIXING
    FIXING   --the round's continuation ended---------------> CHECKING
    CHECKING --green, reviewed MERGE, keyword written-------> READY
    CHECKING --green, final review NOT_YET (§5.2)-----------> FAILED (pull request left open)
    READY    --GitHub says merged---------------------------> MERGED
    READY    --head moved / CI turned red / label removed---> CHECKING
    any live --pull request closed without merging----------> FAILED
    FAILED | BLOCKED --POST :retry--------------------------> WAITING | QUEUED
    BLOCKED  --its blocked_by lane retried (§3)-------------> WAITING
    FAILED   --its pull request closed or merged------------> FAILED (lock released)
    any live --POST :cancel---------------------------------> CANCELLED

`BLOCKED → WAITING` needs no request on the dependant: the transaction that
retries the lane named in its `blocked_by` moves every dependant `BLOCKED` on
it back to `WAITING` in the same commit.

`MERGED`, `VERIFIED` and `CANCELLED` are terminal. `FAILED` is ended but
retryable: `:retry` starts a new lane attempt on the same document
(`lane_attempt` + 1), so its dependants, which are `BLOCKED` on it, return to
`WAITING` without being re-typed — the exact restart cost §1 measured.

### 2.3 Issue runs become lanes

An issue run is an `issue` lane with no `depends_on`, no group and the
`merge` policy it already has. The build plan (§15, LQ8) moves
`/v1/runs` onto the lane machine and keeps the route as a view, so the MCP
tools (`swarm_run_issue`, `swarm_runs`) and the console's Runs tab keep
working while one machine moves both.

---

## 3. Depending on another lane's merge

`depends_on` is a list of up to 8 entries, each one of:

* `{"lane_id": "lane_..."}` — another lane of the **same tenant**; satisfied
  when that lane is `MERGED`, or `VERIFIED` (a verify lane has no merge).
* `{"pull_request": "owner/repo#N"}` — a pull request no lane owns (the
  laptop's "after PR Y merges" where Y was opened by hand); satisfied when
  GitHub reports it merged, read with the tenant's own forge credential.

**A merge, not a finish.** A lane whose dependency merely SUCCEEDED (its
workflow ended green, its pull request open) is still `WAITING`: the code it
needs is not on the base branch until the merge, and starting from a base
without it is the mistyped-launch failure of §1. When a dependency merges the
lane records the dependency's `merge_commit_sha`, and its workflow is compiled
against a base at or after every recorded sha — the base branch's head at
submission, which GitHub has just told us contains them.

**When a dependency can no longer merge** — its lane is `FAILED`,
`CANCELLED` or `BLOCKED`, or its pull request was closed without merging —
the dependant moves to `BLOCKED` with `blocked_by` naming it. `BLOCKED` holds
nothing. A person retries the dependency (the dependant goes back to
`WAITING` by itself), edits the dependant's `depends_on`
(`POST /v1/lanes/{lane_id}:edit`), or cancels it. Nothing is cancelled in a
cascade: a dependant is often still wanted with a different dependency, and a
cascade would make the person re-add it.

**Cycles are refused at submission.** `POST /v1/lanes` takes several lanes at
once, naming each other by a request-local `key`; the API resolves keys to ids
and refuses a cycle, naming the path, with the same walk
`issueruns._dependency_path` does for plan steps. An edit that would close a
cycle is refused the same way. A dependency on another tenant's lane reads as
missing, never as a refusal that confirms it exists (invariant 9).

**Plan before or after the dependency?** An `issue` lane plans when it leaves
`WAITING`, not when it is added, so its planner reads a repository that
already contains its dependencies' code. The price is that its territory is
unknown while it waits; the board shows the brief's declared territory, if
any, with "from the plan, once planned".

---

## 4. Territory locks

### 4.1 Where the territory comes from

A lane's territory is the union of:

* every plan step's `files` (`PlanStep.files`,
  `apps/swarm-api/swarm_api/issueruns.py:357` — "a plan, not a fence"), and
* the paths the lane was given at submission (`territory`), which a `brief`
  lane always has.

Each entry is a repository path or a directory **prefix** ending in `/`,
normalised (no `..`, no leading `/`, no glob). Two entries conflict when one
is a prefix of the other, so `apps/swarm-ui/src/` conflicts with
`apps/swarm-ui/src/App.tsx`. At most 60 entries per lane (`MAX_STEP_FILES`);
a lane that needs more holds a prefix.

**Seam files** are the per-repository list of files nearly every lane must
touch to register what it built: in this repository
`apps/swarm-api/swarm_api/main.py` (routers) and `apps/swarm-ui/src/App.tsx`
(`SECTIONS`), plus `apps/swarm-api/swarm_api/schemas.py`. The list lives on
the repository's registration (docs/repo-index.md §1), seeded from the
index's `hot_spots`, and a lane's `seams` are its territory's entries that
are on it. A seam is the reason #588 and #589 conflicted, and the reason a
plain exclusive lock would serialise almost every lane.

### 4.2 Acquiring and releasing

The locks of one repository live in ONE document,
`lane_territories/{tenant_id}:{repo_key}`, mapping each holding lane to its
paths and seams. `QUEUED → RUNNING` reads that document, checks the lane's
territory against every other holder, writes the lane's entry and moves the
lane, all in **one Firestore transaction**; a conflict leaves the lane
`QUEUED` with `waiting_on: [{lane_id, path}]` for the board. All-or-nothing,
as capacity is: a lane never holds half its territory. One document per
repository serialises launches in that repository, which at lane rates (a
few a minute) is the point, not a cost; at 40 lanes × 60 entries it stays far
under Firestore's 1 MiB.

The lock is held from launch until the lane is `MERGED`, `VERIFIED` or
`CANCELLED`, or is `FAILED` with no open pull request, and is **released** in
the same transaction that moves it there. A `FAILED` lane does **not** close
its pull request: like `issueci._merge` today, a CI cap, a final review of
`NOT_YET` or a refused merge leaves the pull request open and green for a
person, and a lock released beside it would let a sibling start on the same
files. So a `FAILED` lane keeps its lock while its pull request is open, and
the tick releases it in the visit that reads the pull request closed or
merged. `POST :cancel` closes the lane's pull request with a comment, then
releases. Not at `READY`: until GitHub merges, the branch can still go
red and be fixed, and a sibling that started on the same file in the
meantime would conflict exactly as #589 did.

**The plan is not a fence.** When the pull request opens, the lane reads its
changed files (the same read `forge.read_open_work` makes) and compares them
with its territory. A file outside it that no other lane holds is added to the
lane's lock in one transaction. A file another lane holds sets
`territory_breach` on the lane: it may not go `READY` until the holder has
merged, and then takes a rebase round (§4.3) before it may.

### 4.3 Seam policy: three options for the owner

**(T1) A seam is an ordinary exclusive lock.** Safe and simple: two lanes
never touch `main.py` at once. It serialises nearly every lane in this
repository, which is the 12-of-40 ceiling of §1 rebuilt in software.

**(T2) A seam is shared while lanes run and serialised at the merge.**
Recommended. Any number of lanes may hold a seam; only ONE of them may be
`READY` at a time per seam (the seam's merge token, held in the same
`lane_territories` document). When a lane whose diff touched a seam merges,
every other holder of that seam whose pull request GitHub now reports
unmergeable gets a **rebase round**: one continuation of its integrator, the
same mechanism as a CI fix round (§5.1), whose prompt is "merge the base
branch into this branch and resolve the conflict keeping both sides' intent;
run the tests of the files you touched". Under T2, #589 would have been given
its rebase round the minute #588 merged, with nobody watching. Rebase rounds
are capped separately (`rebase_rounds`, default 2), because a conflict that
two rounds could not resolve needs a person.

**(T3) No seam lock; conflicts are found by GitHub.** Seams are recorded and
shown, nothing waits on them; a pull request GitHub reports unmergeable gets a
rebase round. Cheapest; it allows two `READY` lanes to race the same seam,
and the loser always pays a round.

The rebase round needs the continuation's clone to have the base branch to
merge from. The CI fix round today clones `swarm/<task id>` only
(`apps/agent-worker/agent_worker/continuation.py`); LQ3 makes the worker
fetch the base as well. That is worker code (Track B), not the frozen
contract.

---

## 5. After the pull request opens

### 5.1 The CI-fix loop (#545), on the lane's pull request

The lane does not grow a second CI loop. `issueci`
(`apps/swarm-api/swarm_api/issueci.py`) already reads the pull request's
required checks at its head (`from_checks`,
`apps/swarm-api/swarm_api/issueci.py:504`), claims a round and submits ONE
continuation of the integrator per red reading at a new head, at most
`fix_rounds` (1-5, default 3), and fails with the redacted excerpt at the cap.
LQ4 makes it operate on a narrow protocol (the fields it reads and patches:
`pull_request`, `pr_task_id`, `ci_fix_round`, `ci_fix_workflows`,
`ci_round_sha`, `green_sha`, `failure_excerpt`, `fix_rounds`) that both
`IssueRun` and the lane satisfy, so one loop moves both.

`scripts/ci-fix.sh` and `.github/workflows/ci-fix.yml` stay as they are. They
fix `direct-pr` branches (`swarm/<task id>`) with their own continuation-scoped
account, which is refused an integrator (docs/ci.md, "The CI fixer"); a
lane's pull request is an `integrate` integrator's, so it is fixed by the
lane's loop and never by both.

Every red reading records the failing checks' names on the lane
(`ci_reds`), which the ledger counts by job (§13).

### 5.2 Ready when green AND reviewed

A lane in `CHECKING` moves to `READY` only when all of these hold, read in
the same visit:

1. every required check is green at the head (`green_sha`, as `issueci` sets
   it);
2. the **final review** says `MERGE` (see "Reviewed after a fix" below);
3. the keyword block is written on the pull request body
   (`issuesync.sync_pull_request`,
   `apps/swarm-api/swarm_api/issuesync.py:232`);
4. no `territory_breach`, and under T2 the lane holds the merge token of
   every seam its diff touched.

**Reviewed after a fix.** `compile_plan` today builds implement → `review` →
`fix`, with `fix` gated on `review` saying `NOT_YET` and no review after it,
so the one `verdict.json` an issue run has is never re-written after the fix
and `issueci._merge` FAILs any run whose review said `NOT_YET` — the usual
reason the fix step exists. The lane decides it this way (option (a) of the
LQD review):

* a lane's workflow compiles a fourth step, `rereview`, after `fix`:
  `depends_on: [fix]`, `builds_on: fix`, `input_from: {review: verdict.json,
  fix: swarm-work.patch}`, gated on the fix having run, and told to write its
  own `verdict.json` judging the branch against the plan AND whether the fix
  addressed every finding of the first review. It does not edit files and
  publishes nothing; `fix` stays the workflow's one publisher;
* the **final review** is `rereview` when `fix` ran and `review` when it was
  skipped (`review` said `MERGE`). `review.round` on the lane records which
  (1 or 2) and `review.task_id` the task whose `verdict.json` was read;
* final review `MERGE` → condition 2 holds. Final review `NOT_YET` → the lane
  goes `FAILED` with `error` "the re-review after the fix still says NOT_YET",
  its findings on the lane, the pull request left open for a person — today's
  `issueci._merge` rule kept, now reached only after one fix and one
  re-review instead of on every lane that needed a fix. There is no second
  fix round inside the workflow: two disagreeing agents looping is a person's
  call, and `:retry` is how a person makes it;
* a CI-fix round (§5.1) changes the branch after the final review. It is
  bounded to making the required checks pass ("change nothing else"), so the
  verdict stands for the reviewed change, as it does for `issueci` today; a
  CI round whose diff touches a file outside the reviewed patch's files sets
  `unexpected_diff` and holds `READY` for a person.

Then the lane acts on its `merge` setting:

* **`label`** — adds the `ready` label to the pull request with the tenant's
  forge credential. `.github/workflows/auto-merge.yml` enables GitHub's
  native auto-merge on it; GitHub merges at green. This is this repository's
  path today, and it needs the merge App configured
  (docs/runbooks/merge-app.md); until then the workflow refuses with a
  comment, and the lane shows that comment's state rather than waiting
  silently. auto-merge.yml refuses a `[swarm] task_` title, so the lane
  titles its pull request from its own title before labelling.
  A label added by a PAT or an App starts `pull_request_target`; one added by
  a `GITHUB_TOKEN` would not, and the lane never holds one.
* **`step`** — submits the one merge-only continuation `issueci._merge`
  submits (#295, contract request 47), pinned to `green_sha`.
* **`none`** — stops at `READY`; a person merges.

A lane created without saying takes the platform's `merge_by_default`, as
an issue run does, and records what it resolved. `READY` returns to
`CHECKING` when the head moves, a required check turns red, or a person
removes the label: `ready` is bound to the commit it was put on, as
auto-merge.yml already insists.

### 5.3 Closing or commenting its issue

A lane with an `issue` keeps the issue run's write-back: the plan comment
and ONE status comment, edited in place (`issuesync.sync_issue`). Whether the
pull request says `Closes #N` or `part of #N` is decided as today: `Closes #N`
only when the review confirmed every requirement
(`issueci.evaluate_requirements`); otherwise `part of #N`, naming what is left.
GitHub closes the issue on the merge from that keyword; the lane does not
close it a second time. On `MERGED` the status comment says merged, with the
sha; on `part of #N` it lists `requirements_unmet`, which is what the next
lane for that issue starts from.

A lane never closes an issue labelled `epic`: an epic closes on its comments
(CLAUDE.md, "Issues"). A lane whose issue is an epic comments on it, and
ticks nothing.

---

## 6. Verify-only lanes

Some lanes exist to check, not change: "prove the guard still refuses",
"confirm #N is fixed on main". On the laptop they ended FAILED, because the
worker fails a pull-request step that published nothing
(`published_nothing`, `apps/agent-worker/agent_worker/lifecycle.py:4182`), or
the patch harvest's `empty_diff` cause
(`apps/agent-worker/agent_worker/expected_outputs.py:373`) failed the
reviewer's expected `swarm-work.patch`.

A lane carries `allow_empty_diff` (default false):

* **`source: verify`** requires `allow_empty_diff: true` and compiles to a
  `collect` workflow (`apps/swarm-api/swarm_api/validation.py:544`): a verify
  step whose expected output is `verify.md`, then a review that reads it and
  writes `verdict.json`. Nothing opens a pull request, so nothing can fail for
  publishing nothing. `swarm-work.patch` is not an expected output. The lane
  ends `VERIFIED` when the review says `MERGE` (the verification holds) and
  `FAILED` otherwise, quoting the findings. A verify step that DID change
  files has its patch kept and the lane flagged `unexpected_diff`: the board
  offers "promote to a brief lane", which adds a `brief` lane with the patch
  as context.
* **`source: brief` or `issue` with `allow_empty_diff: true`** compiles as
  normal. If the integrator ends with `published_nothing` and the worker's
  `git` summary says zero commits and zero dirty files, the lane reads that
  as "nothing needed changing" and ends `VERIFIED`, recording the reason.
  The TASK stays `FAILED`, honestly, until the frozen request in §14 lets the
  worker end it `SUCCEEDED`; the ledger records the lane's outcome, not the
  task's.

A `VERIFIED` lane with an issue comments the verification on it, and closes
it only when the lane says `close_on_verified: true` AND the review confirmed
every requirement — the `Closes #N` rule, without the keyword.

---

## 7. The review-minors sink

CLAUDE.md: one review "reports blockers and majors; minors go to the wave
epic". Today the compiled review's `findings` is one blocker per entry, and
there is nowhere for a minor to go, so reviews either drop minors or report
them as blockers and spend a fix on them.

The lane's review prompt asks for `minors: [{file, call_site, finding}]` in
`verdict.json` beside `verdict` and `findings`; the gate ignores the extra
key, so no workflow changes. When the lane ends, each minor is stored as
`lane_minors/{tenant_id}:{digest}` (digest of file + call site + text, so the
same minor from two lanes is one entry), redacted, with the lane and pull
request that found it.

The sink is the lane group's epic. When the group names one
(`minors_issue`), the advance tick posts **one comment per finding** on it,
in the epic form CLAUDE.md requires:

    - [ ] **<the defect, as a fact>** · `<file>` `<call site>` · found by lane <lane>, PR #<n>

and records the comment id, so a minor is posted once. A group without an
epic keeps its minors on the board's "Review minors" panel, with "File as
epic" — which opens the epic form; the platform does not create issues on its
own. A minor is never written into a pull request body (CLAUDE.md: findings
about other issues go in a comment).

---

## 8. The laptop today, and what is retired

| Laptop piece (2026-10-05) | What it did | Lane queue | Fate |
|---|---|---|---|
| spec generator | turned a brief into an implement → review → fix workflow spec | `POST /v1/lanes` with `source: brief`; `compile_plan` builds the workflow | **Retired** |
| launch queue in memory notes | "launch X after PR Y merges", re-typed after every restart | `depends_on` on the lane document (§3) | **Retired** |
| PR watcher (one per lane) | waited for CI on a pull request, started a fixer | the advance tick + `issueci` (§5.1) | **Retired** |
| merge watcher | labelled or merged green pull requests | `READY` → `ready` label or merge step (§5.2) | **Retired** |
| watchdog | restarted watchers that died on network errors | nothing to restart: the tick is Cloud Scheduler's, a failed visit is retried by the next one; `stalled_since` on a lane that has not moved | **Retired** |
| the operator choosing what to launch next | concurrency bounded at 12 of 40 | the queue launches every lane whose dependencies and territory allow; admission decides capacity | **Retired** |
| `.github/workflows/ci-fix.yml`, `scripts/ci-fix.sh` | fix a red `direct-pr` branch | unchanged; a lane's pull request is an integrator's and is fixed by the lane | **Kept** |
| `.github/workflows/auto-merge.yml` | GitHub merges a `ready` pull request | the lane's `merge: label` path | **Kept** |
| `scripts/publish-scan.sh` | credential scan before a SwarmCloud step finishes | unchanged | **Kept** |
| MCP `swarm_workflow_launch`, `swarm_run_issue` | launch by hand | kept; `swarm_lanes_add`, `swarm_lanes` added (LQ8) | **Kept** |

The laptop's pieces are not in this repository (they were the operator's
session tooling), so "retired" is a runbook change, not a deletion: LQ8
writes the runbook that says which session habits stop.

---

## 9. Data model

All documents are tenant-scoped exactly as `issue_runs` is: every read
compares the stored `tenant_id` with the caller's and answers a mismatch with
the same 404 as a missing document. None goes through `store.py` or
`codec.py`; none is a frozen type.

**`lanes/{lane_id}`** (id prefix `lane_`):

| field | type | note |
|---|---|---|
| `id`, `tenant_id`, `created_by`, `created_at`, `updated_at` | | as `IssueRun` |
| `group` | text ≤ 64 | the wave; board, sink and ledger aggregate by it |
| `title` | text ≤ 200 | the pull request's title; never `[swarm] task_` |
| `source` | `issue` \| `brief` \| `verify` | §2.1 |
| `repository`, `base_branch` | `owner/repo`, text | checked by `check_repository_url`, as issue runs are |
| `issue` | `owner/repo#N` or null | |
| `brief` | text ≤ 16 000 | `brief`/`verify` only |
| `requirements` | list of text ≤ 40 | the work items; what `Closes #N` is decided against |
| `depends_on` | list ≤ 8 of `{lane_id}` \| `{pull_request}` | §3 |
| `dependency_merges` | map dep → `merge_commit_sha` | recorded as each merges |
| `territory` | list ≤ 60 of path or prefix | declared + from the plan, `territory_source` says which |
| `seams` | list of path | the territory's entries on the repository's seam list |
| `allow_empty_diff`, `close_on_verified` | bool | §6 |
| `merge` | `label` \| `step` \| `none` | §5.2 |
| `fix_rounds`, `rebase_rounds` | int | caps |
| `minors_issue` | `owner/repo#N` or null | from the group |
| `state`, `history[]`, `error`, `blocked_by`, `waiting_on`, `stalled_since` | | the machine |
| `lane_attempt` | int | `:retry` increments |
| `plan`, `plan_digest`, `planner_task_id`, `plan_approval` | | as `IssueRun` |
| `workflow_id`, `workflows[]` | | the main workflow; every workflow the lane submitted (CI rounds, rebase rounds, merge), for the ledger |
| `pr_task_id`, `pull_request`, `ci_fix_round`, `ci_fix_workflows`, `ci_round_sha`, `green_sha`, `failure_excerpt` | | the `issueci` protocol (§5.1) |
| `pull_request.changed_files`, `pull_request.merge_commit_sha`, `pull_request.merged_at` | | read from GitHub |
| `review` | `{round, task_id, verdict, findings[], minors_count, requirements_met, requirements_unmet[]}` | from the final review's `verdict.json` (§5.2); `round` 1 = `review`, 2 = `rereview` |
| `ci_reds` | map check name → count | §5.1 |
| `territory_breach`, `unexpected_diff` | bool | §4.2, §5.2, §6 |

**`lane_territories/{tenant_id}:{repo_key}`** — `holders: {lane_id:
{paths, seams, acquired_at}}`, `seam_tokens: {path: lane_id}` (T2 only), and
`version`. One document per repository; written only inside a lane
transition.

**`lane_groups/{tenant_id}:{group}`** — `minors_issue`, `created_by`, the
default `merge` and `fix_rounds` for lanes added to it.

**`lane_minors/{tenant_id}:{digest}`** — `group`, `lane_id`, `pull_request`,
`file`, `call_site`, `finding` (redacted), `state` (`open` \| `posted` \|
`dismissed`), `comment_id`.

**`lane_ledger/{lane_id}`** — §13.

Indexes: `lanes (tenant_id, state, created_at)` for the tick,
`lanes (tenant_id, group, created_at)` for the board,
`lane_ledger (tenant_id, ended_at)` and `lane_ledger (tenant_id, group,
ended_at)` for the ledger.

---

## 10. Routes

The tenant on every route comes from `tenant_scope`, never from the body, as
on `/v1/runs`.

    POST /v1/lanes                       {group?, lanes: [{key, title, source, repository, issue?,
                                          brief?, requirements?, depends_on?, territory?,
                                          allow_empty_diff?, merge?, fix_rounds?, plan_approval?}]}
                                         -> 201 {lanes: [{key, lane_id, state}]}; all or none
    GET /v1/lanes                        ?group=&state=&page_token=   the board's rows
    GET /v1/lanes/{lane_id}              one lane, advanced first (as GET /v1/runs/{run_id})
    POST /v1/lanes/{lane_id}:edit        {revision, depends_on?, territory?}  WAITING/QUEUED/BLOCKED only
    POST /v1/lanes/{lane_id}:cancel      {reason?}
    POST /v1/lanes/{lane_id}:retry       FAILED or BLOCKED -> WAITING or QUEUED
    POST /v1/lanes/{lane_id}/plan:approve|edit|reject     the issue-run plan routes, unchanged
    GET /v1/lanes/territory              ?repository=   who holds what, who waits on whom
    GET /v1/lanes/minors                 ?group=&state=
    POST /v1/lanes/minors/{digest}:dismiss
    GET /v1/lanes/ledger                 ?group=&from=&to=&page_token=   §13
    GET /v1/lanes/{lane_id}/ledger
    POST /v1/admin/lanes/advance         ?tenant_id=   the Cloud Scheduler tick (§11)

`POST /v1/lanes` takes several lanes in one request because that is how the
laptop launched them — a wave at a time — and because `depends_on` between
lanes of the same request needs both to exist. It is all or none: one lane
refused (unknown repository, a cycle, a territory entry that is not a path)
refuses the request, naming the key. No field selects a runner profile,
image, command, resource class or backend; every step is `claude-code`,
chosen by the API (invariant 10). `:edit` carries the `revision` it was shown,
as plan edits carry the digest (D3), so two editors cannot overwrite each
other.

---

## 11. Scheduler changes

**One new Cloud Scheduler job**, `lane_queue_advance`, per tenant, in
`terraform/modules/scheduler/jobs.tf` beside `issue_run_advance`: every
minute, OIDC as the existing sweeper account, calling
`POST /v1/admin/lanes/advance?tenant_id=<tenant>`. A Cloud Scheduler job has
no labels, so its description carries `managed-by=swarm-terraform`, as every
job in that file does. The sweeper account's description, which lists the
admin routes it is admitted to, gains this one; it gets no project role.

The tick visits a tenant's live lanes oldest first, bounded by the page
limit (`advance_tenant_runs` is the pattern, `apps/swarm-api/swarm_api/routes/admin.py:700`
the route it copies), one lane's failure being that lane's only. After a
merge, the tick moves that lane's dependants in the same visit, so a chain of
lanes advances one link per merge, not one link per minute. A healthy lane
that has not moved costs one document read; a lane in `CHECKING` or `READY`
reads GitHub at most once per `CI_READ_SECONDS`, as an issue run does.

**`apps/scheduler` does not change.** A lane submits its workflows through
`submit_tasks`, as an issue run does, and its tasks are admitted, leased and
dispatched exactly like any other's. The lane queue decides *which lanes may
start* (dependencies, territory); admission decides *when their tasks run*
(capacity). Keeping those apart is what lets the queue launch every eligible
lane at once without touching the concurrency invariant: a lane launched into
a full pool is a set of `QUEUED` tasks, which cost nothing. A lane's priority,
if the owner wants one, rides on the existing workflow `priority` (-100 to
100), which its tasks carry and `apps/scheduler/scheduler/fairness.py`
already orders by.

When LQ8 moves issue runs onto lanes, `issue_run_advance` is removed in the
same change that makes `/v1/runs` a view, so one tick moves each document.

---

## 12. The invariants

### Invariant 1: only LEASED/DISPATCHED/STARTING/RUNNING create demand

A lane in `WAITING`, `PLANNED`, `QUEUED`, `BLOCKED`, `CHECKING` or `READY` is
a Firestore document and nothing else: no task, no lease, no pending pod. The
lane's work exists as tasks only while a workflow or a round is submitted,
and those tasks hold capacity only in the four demand states, like every
other task. A territory lock is not capacity and never touches a pool; a lane
waiting for a lock waits as a document, never as a submitted task parked on a
lock, so the queue cannot become a backlog of pending pods.

### Invariant 2: capacity is reserved all-or-nothing

The lane queue reserves no capacity at all; every reservation is still the
admission transaction's, across every pool, for each task as it is admitted.
The territory lock borrows the same discipline for its own resource: a lane's
whole territory, and under T2 its seam tokens, are acquired in one Firestore
transaction with the lane's move to `RUNNING`, or not at all, so a lane never
holds half its files while waiting for the rest.

### Invariant 3: concurrency counts from LEASED

Unchanged and untouched. The queue's own question — "may this lane start" —
counts lanes holding territory, which is not concurrency, and the answer
submits a workflow whose tasks are then counted from `LEASED` by admission,
not from `RUNNING`. The 12-of-40 ceiling measured in §1 was the operator's;
the lane queue removes it by launching every eligible lane, and admission
remains the only thing that bounds how many of their tasks hold a slot.

### Invariant 4: workers never sleep through a long wait

Every wait in this design — for a dependency's merge, for a territory lock,
for CI, for GitHub's merge — is a lane document state that holds no worker.
No worker polls GitHub, waits for a merge or holds a lease across a CI run:
the advance tick reads, moves and submits, and a fix or rebase round is a new
continuation submitted only when there is work for it. That is the same
reason a `PLANNED` issue run is a document and not a parked step.

### Invariant 5: every attempt carries a fencing generation

Lane tasks are ordinary tasks; every attempt keeps its generation and a stale
worker exits without running the agent. The lane adds its own fence of the
same shape: every transition re-checks the state it moves from in the
transaction, so the tick and a reader racing produce one move, and a round's
claim (`CHECKING → FIXING`) is written before the round is submitted, so two
readers of one red head submit one continuation, as `issueci` already
guarantees.

### Invariant 6: Spot is disabled platform-wide

The lane queue chooses no backend, no node pool and no provisioning model;
every task it submits is `claude-code` resolved by the frozen catalogue
(`resolve_backend`), which never selects Spot. Nothing in a lane's fields can
express a Spot preference, and the add-lanes form has no such control, so a
lane cannot reintroduce the preemption that Spot would bring to Autopilot
extended run time.

### Invariant 7: requests == limits

The lane queue sets no resources. Every task it submits takes `claude-code`'s
resource class from the frozen catalogue, where requests equal limits; a fix
or rebase round is a continuation of the same profile and class. Nothing a
caller sends to `POST /v1/lanes` can name a resource class or a size, so no
lane can ask for a burstable container.

### Invariant 8: mandatory periodic checkpointing

Every lane task checkpoints like every task: the worker's periodic
checkpoints are not optional and the lane does not change their interval. The
lane queue adds a second layer of survivability above them: a lane's state is
its document, so a fix round lost to a crash, a cancellation or a reconciler
reclaim costs that round, and the next tick resubmits from where the document
says the lane is — never from a watcher's memory, which is what §1 measured
dying.

### Invariant 9: per-tenant isolation

Every lane document, territory document, minor and ledger record carries
`tenant_id`, is read under `tenant_scope`, and answers another tenant's id as
missing. `depends_on` may only name a lane of the same tenant; the tick for a
tenant reads and moves only that tenant's lanes and submits as the lane's
creator in that tenant, as `run_owner_auth` does. Every GitHub read and write —
CI, labels, comments, the merge state of an external pull request — uses the
lane's own tenant's `swarm-tenant-<tenant>-git` credential, held in the frame
and dropped, never logged, stored or returned.

### Invariant 10: callers pick a runner profile by name

`POST /v1/lanes` has no profile, image, command, resource or backend field.
A lane's brief, requirements, territory and minors are text, validated as
text, and reach agents as data inside delimited prompt sections, exactly as
an issue run's plan does; the API chooses `claude-code` for every step and
the `collect` or `integrate` strategy from the lane's `source`. A territory
entry is a path the lock compares, never something executed.

---

## 13. The lane outcome ledger

#639, folded into this design (owner decision 2026-10-05). It answers "what
did this lane cost, how long did it take, where did it wait, and what came
of it", one record per lane, and "what did this wave cost and where did its
time go" by summing them.

It is **not** `GET /v1/outcomes` (docs/outcomes.md,
`apps/swarm-api/swarm_api/outcomes.py`), which buckets TASKS by when they
ended. A lane is many tasks across several workflows; its outcome is the
lane's (`MERGED` is a success even when one of its CI rounds' tasks failed),
and its cost is every one of those tasks'. The two ledgers share the
honesty rules: **null is not known, 0 is measured**, and a figure is placed by
the lane's end.

### 13.1 One record: `lane_ledger/{lane_id}`

| field | derived from | null when |
|---|---|---|
| `tenant_id`, `group`, `title`, `source`, `outcome` (`MERGED` \| `VERIFIED` \| `FAILED` \| `CANCELLED`), `lane_attempts` | the lane | never |
| `created_at`, `ended_at`, `wall_seconds` | the lane's history | never once ended |
| `cost_usd` | sum of `Attempt.cost_usd` over every attempt of every task of every workflow in `workflows[]`, plus the planner | every attempt unreported |
| `cost_complete`, `cost_unreported_attempts` | count of attempts whose `cost_usd` is null | never; says whether `cost_usd` is a floor |
| `input_tokens`, `output_tokens`, `cache_read_input_tokens` | the same sum | as `cost_usd` |
| `attempts`, `tasks`, `workflows` | counts | never |
| `working_seconds` | sum of attempts' `started_at` → `completed_at` | no attempt started |
| `waiting.dependencies_seconds` | time in `WAITING` | never |
| `waiting.plan_approval_seconds` | time in `PLANNED` | not an `issue` lane |
| `waiting.territory_seconds` | time in `QUEUED` | never |
| `waiting.capacity_seconds` | per task, first attempt's `created_at` − task `created_at`, plus parked spans | a task never admitted |
| `waiting.ci_seconds` | `CHECKING` with checks pending | no pull request |
| `waiting.merge_seconds` | `READY` → `MERGED` | not merged |
| `review.round`, `review.verdict`, `review.findings` (≤ 20, redacted), `review.minors_count`, `review.requirements_met` | the final review's `verdict.json` (§5.2) | the review never ran |
| `pull_request` (`number`, `url`) | the lane | no pull request |
| `ci_reds` | `{check name: red readings}` | no pull request |
| `ci_fix_rounds`, `rebase_rounds` | the lane | never (0 is measured) |
| `merged_at`, `merge_commit_sha`, `merge_seconds` | GitHub, read by the lane | not merged |
| `issues_closed` | `Closes #N` on a merged pull request, or `close_on_verified` | none closed: `[]` |
| `issues_left` | `[{issue, unmet: [...]}]` from `part of #N` | none left: `[]` |
| `derived_at`, `derive_version` | the writer | never |

### 13.2 Derive, write, drift check

The record is written in the transaction that ends the lane, and re-derived
by the tick once more after `CI_READ_SECONDS × 2` (a task's spend is recorded
before its terminal write, docs/outcomes.md "One basis per figure", so a
re-read after the end is complete). A `:retry` re-opens the lane, and its next
end rewrites the record with `lane_attempts` + 1 and the costs of both
attempts — the lane cost what all of it cost. `derive_version` moves when a
rule changes, and the route re-derives a record whose version is older,
as `outcomes.py` does.

### 13.3 Route

    GET /v1/lanes/ledger?group=&from=&to=&page_token=
      -> {lanes: [record...], totals: {cost_usd, cost_complete, wall_seconds,
          waiting: {...}, ci_reds: {...}, outcomes: {MERGED: n, ...}}, next_page_token}
    GET /v1/lanes/{lane_id}/ledger -> one record

Totals are sums over the page's span, not over the page, and say
`cost_complete: false` when any record is a floor. Placed by `ended_at`.

### 13.4 Console tab

**Work › Lanes**, with the tabs Board and Ledger. The ledger tab is drawn in
two variants in the mock-up (`ledger-a`, a table with a totals row and
waiting stacked per lane; `ledger-b`, a group summary first, then where the
time went). A null figure is a dash with its reason in the title, never 0.

---

## 14. The frozen contract

Nothing in §2-§13 requires a change to `apps/common/swarm_common/`: the lane
machine and every new document are swarm-api's own, as `issue_runs` is, and
lanes submit ordinary workflows.

One request is needed to finish §6, and is written here **for filing as the
next free number in docs/contract-change-requests.md; it is
not filed by this lane**:

> **`profiles.py`: `claude-code` does not declare `allow_empty_diff`.**
> *What is true today:* a pull-request step that ends with no commit beyond
> its base is FAILED `published_nothing` by the worker
> (`agent_worker.lifecycle._published_nothing`, request 46), with no way for
> the step to say an empty diff is its correct outcome. A lane that verifies
> "already done" therefore ends with a FAILED task.
> *Requested change:* `claude-code` declares an optional boolean runner input
> `allow_empty_diff` (default false). When true, the worker ends a
> pull-request step whose branch has nothing beyond its base `SUCCEEDED`,
> with `git.published: false, git.empty_diff: true` in its result summary, and
> opens no pull request. It is covered by the step-spec signature like every
> input, so only the API can set it.
> *What it would break:* nothing that exists; the input is optional. The
> parity script's runner-input section follows it.
> *If declined:* the interim of §6 stays: the lane reads the FAILED task as
> `VERIFIED`, and the outcome ledger of tasks counts it as a failure.
> *Invariants:* 10 — a boolean input chosen by the API, not by a caller.

---

## 15. Build plan

Each row is one lane. Territory decides the split (CLAUDE.md: two issues that
edit the same file are one lane). The seam files `swarm_api/main.py` and
`App.tsx` are each touched by exactly one lane here (LQ1 and LQ7), so the
build itself does not need §4.

| Lane | What | Territory | Depends on |
|---|---|---|---|
| LQ1 | lane document, `LaneState` machine, `POST`/`GET /v1/lanes`, `brief` lanes compiled through `compile_plan`; no dependencies or locks yet | `swarm_api/lanes.py` (new), `routes/lanes.py` (new), `schemas.py`, `main.py`, unit tests | picks |
| LQ2 | `depends_on` (lanes and external pull requests), `BLOCKED`, `:retry`, `:edit`; the advance tick and its admin route; the Cloud Scheduler job | `lanes.py`, `routes/lanes.py`, `routes/admin.py`, `terraform/modules/scheduler/jobs.tf` (Track C) | LQ1 merged |
| LQ3 | territory locks, the picked seam policy, `territory_breach`, the rebase round; the worker fetches the base in a continuation | `lanes_territory.py` (new), `agent_worker/continuation.py` (Track B) | LQ2 merged |
| LQ4 | `issueci` over a protocol; the `rereview` step and the final-review rule (§5.2); `READY`; `ready` label (`forgewrite` gains `add_labels`); merge step path; issue comments; verify-only lanes and `allow_empty_diff` interim | `issueci.py`, `issueruns.py` (`compile_plan`), `forgewrite.py`, `lanes.py` | LQ3 merged |
| LQ5 | the review's `minors`; `lane_minors`; posting to the group's epic | `lanes_minors.py` (new), `issueruns.py` (review prompt), `lanes.py` (lane-ending transition), `routes/lanes.py` (minors routes) | LQ4 merged |
| LQ6 | the lane ledger: derive, write, drift, `GET /v1/lanes/ledger` | `lanes_ledger.py` (new), `lanes.py` (lane-ending transition), `routes/lanes.py` (ledger route) | LQ5 merged: both edit `lanes.py` and `routes/lanes.py`, so they are serialised, not parallel |
| LQ7 | console: Work › Lanes — board, lane detail, add-lanes form, ledger tab (the picked variants) | `apps/swarm-ui/src/` lanes screens, `App.tsx` | LQ5, LQ6 merged |
| LQ8 | issue runs become lanes; `/v1/runs` a view; `issue_run_advance` removed; MCP `swarm_lanes_add`/`swarm_lanes`; the runbook retiring the laptop pieces | `routes/runs.py`, `issueruns.py`, `apps/swarm-mcp/`, `jobs.tf`, `docs/runbooks/` | LQ7 merged |

The frozen request of §14 is independent of every lane; LQ4 ships the interim
and a later change removes it once the request is accepted.

---

## 16. Open questions for the owner

1. **Seam policy**: T1, T2 (recommended) or T3 (§4.3).
2. **Default `merge`**: `label` (this repository's path, needs the merge App)
   or `step` (pins the reviewed sha) when a lane does not say.
3. **Plan after the dependency merges** (recommended, §3) or as soon as the
   lane is added, so its territory shows earlier.
4. **A per-tenant cap on lanes holding territory**, beyond what admission
   already bounds. The design has none: a lane waiting for capacity costs
   nothing.
5. **`close_on_verified`** default: off (recommended) or on.
6. **Where minors go** when a group has no epic: the board only
   (recommended), or the platform opens the epic.
7. **The mock-up picks**: board, lane detail, add-lanes form and ledger,
   listed in the mock-up's "All screens to pick".
