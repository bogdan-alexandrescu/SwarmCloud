# Issue runs: from a GitHub issue to a green pull request (#454)

An issue run takes `owner/repo#N`, has a planner agent write a plan, waits for
a person to approve it (or not, with `plan_approval: auto`), runs the plan as
one workflow that opens a pull request, then reads that pull request's CI and
spends at most `fix_rounds` fix rounds making it green. It writes back to the
issue throughout. It stops at a green, ready-to-merge pull request: it never
merges today.

This page records **why** it is built the way it is. What each piece does is in
the module docstrings, which are the reference:

| piece | where |
|---|---|
| the run document, its states, the plan schema, the compiler | `apps/swarm-api/swarm_api/issueruns.py` |
| the routes, and "every read advances" | `apps/swarm-api/swarm_api/routes/runs.py` |
| the CI loop and the `Closes`/`part of` decision | `apps/swarm-api/swarm_api/issueci.py` |
| the write-back to the issue and the pull request | `apps/swarm-api/swarm_api/issuesync.py`, `issuecomments.py`, `forgewrite.py` |
| the open-work read the planner is shown | `apps/swarm-api/swarm_api/forge.py` (`read_open_work`) |
| the tick | `POST /v1/admin/runs/advance`, `terraform/modules/scheduler/jobs.tf` (`issue_run_advance`) |
| the surfaces | console `Work › Submit from a GitHub issue` and `Work › Runs` (`apps/swarm-ui/src/IssueSubmit.tsx`, `Runs.tsx`); `sc run --issue`, `sc plan approve\|edit\|reject` and the MCP tools ([plugin/README.md](../plugin/README.md#planning-and-running-a-github-issue)) |

```
PLANNING ─► PLANNED ─► APPROVED ─► RUNNING ─► CHECKING ─► DONE (green_sha)
   │          │  ▲ edit                │        │   ▲
   │          │  └─┘                   │        ▼   │ one continues_task per round
   │          ├─► REJECTED             │      FIXING ┘
   ▼          ▼                        ▼        ▼
 FAILED / CANCELLED  ◄─────────────────┴────────┘   (FAILED at the cap, with the excerpt)
```

---

## The run is its own document, outside the frozen `TaskState`

A run lives in the `issue_runs` Firestore collection, read and written only by
`issueruns.py`, not by `store.py` or `codec.py`. Owner decision, 2026-10-01:
"a swarm-api run document … The frozen TaskState is untouched."

The reason is that a run is not a task. It *owns* tasks -- one planner task,
then one workflow, then up to `fix_rounds` one-step continuation workflows --
and its states (`PLANNED`, `CHECKING`, `FIXING`, `REJECTED`) describe things no
task does: waiting for a person, reading someone else's CI. Putting those into
`TaskState` would have meant a frozen-contract change that every component
written against the twelve task states would have to absorb, for a concept
only swarm-api reads. So the run's machine is `issueruns.RUN_TRANSITIONS`, and
a run's progress is *derived* from its tasks' and workflows' frozen states,
exactly as a workflow's state is derived from its steps.

The document is tenant-scoped the way a task is: every read compares the stored
`tenant_id` with the caller's tenant and answers a mismatch with the **same 404
as a missing run**, so a run id is never an oracle for another tenant's work
(invariant 9).

## Why `PLANNED` holds nothing, and steps exist only after approval

Invariant 1: only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING` create
infrastructure demand. A person may take a day to read a plan, and that day
must cost nothing.

So the planner is an ordinary task that **ends**. Once it has, a `PLANNED` run
is a Firestore document and nothing else: no task, no lease, no workflow, no
pending pod. The work is a *new* workflow, compiled from the approved plan and
submitted at approval. Two alternatives were rejected:

* **A workflow with a parked approval step.** A parked step is still a step of
  a workflow whose other steps exist, and a workflow cannot be edited once
  submitted -- so "edit the plan" would have been impossible.
* **Pre-created steps held at `READY`.** They would cost nothing, but an edit
  would have to delete and recreate them, and a reader would see work that may
  never run.

The tick does not even *read* a `PLANNED` run waiting for a person
(`IssueRuns.tickable`): only that person moves it.

## Approve and edit carry the digest of the plan shown (D3)

A plan can change between the moment one member reads it and the moment
another approves it. "Approve run X" would approve whatever the plan had become.
So `plan:approve` and `plan:edit` both carry `plan_digest` -- the sha256 of the
canonical plan the caller was shown -- and a mismatch is `409 plan_changed`,
naming the current digest. Edits carry the digest they *replace* for the same
reason: two editors cannot silently overwrite each other.

The digest check and the state change are **one Firestore transaction**, so two
approvals of one plan submit one workflow. The surfaces keep the rule honest at
their end: `sc plan approve` prints the whole plan and its digest before asking
for `approve` typed back, `swarm_plan_approve` takes the digest from its caller
and reads nothing first, and the console never swaps the plan under an open
Approve button when a poll brings a newer one.

An edit goes through the same validator (`PlanSpec`) as the planner's own
`plan.json`. An extra key is refused naming it, not dropped.

## The plan is data (invariant 10)

The issue text, the plan, the open-work snapshot and the CI excerpt are all
text an agent or a stranger wrote. None of them selects anything the platform
runs: every planner, implementer, review, fix and CI-fix step is
`claude-code`, chosen in `issueruns.py`/`issueci.py`; the repository is the
run's own; the plan schema has no field for a profile, image, command,
resource class, backend or branch. Text reaches agents between delimiter
lines carrying the run id or a fresh nonce, as data.

## Why the planner reads every open issue and pull request, and what caps apply

#454 asks the planner to avoid conflicts: work already in flight, two issues
editing the same files, a pull request that already does part of it. A plan
made blind to that is how two lanes overwrite each other (the
"two issues that edit the same file are one lane" rule in
[CLAUDE.md](../CLAUDE.md#issues)). So `POST /v1/runs` reads the repository's
open issues, open pull requests and their changed files **before anything is
created**, stores the masked snapshot on the run as `open_work`, and puts it in
the planner's prompt. The planner names what it finds in the plan's
`overlaps`, which the plan comment and the console render as links.

The read is bounded three ways at once -- the time `POST /v1/runs` waits on
GitHub, Firestore's 1 MiB document limit, and Linux's 128 KiB limit on one
argv string (the claude-code runner passes the prompt as one argument):

| bound | value (`forge.py` / `issueruns.py`) |
|---|---|
| open issues listed (the planned issue excluded) | `MAX_OPEN_ISSUES` = 100, over at most `MAX_ISSUE_PAGES` = 3 pages |
| open pull requests listed | `MAX_OPEN_PULLS` = 50 |
| pull requests whose changed files are read | `MAX_PULLS_WITH_FILES` = 30 |
| changed paths per pull request / in total | `MAX_PR_FILES` = 100 / `MAX_TOTAL_PR_FILES` = 1,000 |
| wall clock for the whole read | `OPEN_WORK_BUDGET_SECONDS` = 30 s |
| the planner's whole prompt | `MAX_PLANNER_PROMPT_BYTES` = 64 KiB |

Every bound that is hit is **recorded** and the planner is told the list was
cut, rather than left to believe it is whole. Titles and paths are masked, their
`@`-mentions broken and control characters folded before they are stored.

The planner reports overlaps; it does not resolve them. Whether an overlap
means "wait for that PR" is the approver's decision, which is one more reason
the default is `plan_approval: required`.

## The tenant's credential, and what it must be allowed

Every GitHub read and write -- the issue preview, the open-work read, both
comments, the pull request's keyword block, the CI reads -- uses the **run's
own tenant's** forge credential, `swarm-tenant-<tenant>-git`, read through
`swarm_api.forge.SecretManagerForgeTokens` (invariant 9). Nothing a caller
sends selects a token; an auto approval or a CI fix round submitted by the tick
is submitted as the run's creator, in the run's tenant (`run_owner_auth`),
never as the sweeper, which holds no tenant. The token is read only when there
is something to read or write, held in one frame, passed to redaction as a
known literal, and dropped; every forge error is a constant sentence that
names the missing permission and never quotes the token.

**The creator is asked about again before anything is submitted as them.**
The tick submits an auto approval, and up to `fix_rounds` CI fix rounds that
push to the tenant's repository, as the run's creator -- possibly long after
the run was created. A person removed from the tenant's group must not keep
doing that, so `run_owner_auth` asks the directory whether the creator is
*still* a member, every time (`Authenticator.is_tenant_member`: the same
per-group Cloud Identity check every request's tenant resolution makes; a
personal tenant is its own address). Removed: the run is FAILED, naming why,
and nothing is submitted -- an `auto` run goes `PLANNED → FAILED`, a CI fix
round is never claimed. A lookup that fails (Cloud Identity unavailable) is
not a guess either way: the run waits, unchanged, for the next tick.

The permissions it needs, on every repository a tenant submits issues from, are
tabled in [multi-tenancy.md](multi-tenancy.md#what-the-forge-credential-must-be-allowed):
`Contents` and `Pull requests` read/write (unchanged, for the agents' push and
the pull request), **`Issues: Read and write`** for the two comments,
`Checks: Read` and `Commit statuses: Read` for the CI loop, and optionally
`Actions: Read` for a failing job's log tail.

**A forge 403 or 404 fails the planner** (owner decision, 2026-10-01) -- in
practice it refuses the run before the planner exists: the open-work read
runs first, and a refusal (`no_access`, `not_found`, `no_forge_credential`,
…) creates nothing. A planner that cannot see the repository's open work
would plan exactly the blind plan #454 exists to stop.

**A failed write-back never fails a run.** The run's truth is its Firestore
document, not a comment. A 403 on a comment (`writeback_forbidden`, usually a
token without `Issues: write`) is recorded on the run as `writeback_error`,
redacted, and the same write is not retried for `issuesync.RETRY_SECONDS`
(5 minutes) unless what it would write changes. See
[troubleshooting](troubleshooting.md#an-issue-run-shows-writeback_error).

### Tenant reach is what the tenant's credential can read

There is no repository allowlist. A tenant can plan any issue its own token can
read, and no other (owner decision, 2026-10-01: "an allowlist may come later").
This is also why one tenant cannot submit another tenant's repository: it
would need that tenant's token, and it never has it.

### Who may approve

Any member of the run's tenant may approve, edit or reject; a member of another
tenant gets the 404 every other read gets. The approver is recorded on the run
(`approved_by`, `approved_at`, `approved_digest`), every edit's author as
`plan_edited_by`, a rejection as `rejected_by` with its reason, and every move
in `history`. An `auto` run records `approved_by: auto-approval`. The plan
comment names the approver by the local part of their address only, because
the comment may be on a public repository.

## The Cloud Scheduler tick, and why every read also advances

A run's state is a consequence of its tasks: the planner ending, the workflow
ending, CI going green. Something has to *look*. Two things do, and they run the
same code (`routes/runs.py` `advance_run`):

* **Every read.** `GET /v1/runs/{id}` (and the list) moves a run as far as its
  tasks say before answering, as every workflow read derives its state. A
  reader therefore never sees a stale run, and the console's polling is what
  makes a watched run feel live.
* **The tick.** Reads alone would leave a run nobody watches where it was --
  and every `plan_approval: auto` run, whose whole point is that nobody has to
  watch. So `google_cloud_scheduler_job.issue_run_advance` (one per registered
  tenant, named `swarm-issue-run-advance-<tenant>`, every minute) calls
  `POST /v1/admin/runs/advance?tenant_id=<t>` as the `swarm-rollup-sweeper`
  OIDC identity, which swarm-api admits to that route and the workflow rollup
  and nothing else ([workflows.md](workflows.md#workflow-state)).

Each move is a transaction that re-checks the state it moves from, so a reader
and the tick racing produce one transition and the loser re-reads. A healthy
run that has not moved costs one task or workflow read and writes nothing. A
tick visits one page of a tenant's movable runs, oldest first, and reports
`truncated` when there are more; one run failing to advance is counted and the
rest of the page is still advanced.

**A personal `u-` tenant is not ticked**: it is created at runtime and is not
in `var.tenants`. Its runs advance on every read, and an `auto` run in one waits
for someone to look.

## The fix-round cap is the only guardrail, and one round is one continuation

Owner decision on #454: "The retry cap on gates is the only guardrail chosen."
No budget, no wall-clock limit, no per-repository rate. `fix_rounds` is chosen
at submission, **1-5, default 3** (`schemas.RunCreate`), and bounds:

* **CI fix rounds after the pull request opens** (`issueci`). A red reading at
  a new head claims a round (`CHECKING → FIXING`, one transaction, so two
  readers racing submit one round), then submits it. The red reading that finds
  `fix_rounds` rounds already spent ends the run `FAILED`, with the failing
  output, redacted and bounded to 8 KiB, on the run as `failure_excerpt` and
  the reason in `error` and on the status comment.
* **Review-then-fix rounds inside the compiled workflow** -- but only **one** is
  compiled today, whatever the cap (`COMPILED_REVIEW_ROUNDS`). Under
  `integrate` only the integrator may be gated, and a second review needs a
  gated step that is not the publisher
  ([workflows.md, "What this does not do"](workflows.md#what-this-does-not-do)).

Why the cap and nothing else: a round is bounded work an agent either finishes
or fails, so N rounds is a bound on cost *and* time without inventing either
number. Why 1-5: one round is enough to fix a lint error; more than five red
rounds in a row is a plan that was wrong, which a person should look at.

**Why one `continues_task` continuation per round.** The compiled workflow is
`integrate`, and its one publisher is the gated `fix` step -- the integrator.
It pushed `swarm/<its own task id>` and opened the pull request from it. A
fix round is a one-step `direct-pr` workflow whose `continues_task` names the
integrator, so it clones that branch, pushes to it, and the existing pull
request updates; no second pull request, no rebase of someone else's branch.
Every round names the integrator, never the previous round, so each round
resolves to the same root. (Continuing an integrator needed
`continuation.resolve_continuation` to accept one; it does, for a tenant member
only, which the CI-loop step of #454 verified and changed in swarm-api -- no
frozen-contract change.)

A round is spent **only on a red reading at a new head**. Pending CI submits
nothing; a round that pushed nothing (the head did not move) is `FAILED` rather
than a second round on the same commit; a CI read that fails (GitHub down, a
token without `Checks: read`) is not a red reading -- the run stays `CHECKING`
with `pull_request.read_error` set. CI is read at most every 30 s per run, and a
head with nothing required and nothing reported for 10 minutes is read as a
repository with no CI (`DONE`, `checks: none`).

**`green_sha`** is the head sha at which every required check was green. It is
set only on `DONE`, and it is what any future merge must pin to (below).

## `Closes #N` only when the review confirmed every requirement

Repository rule ([CLAUDE.md](../CLAUDE.md#issues)): `Closes #N` only when it is
unconditionally true. **GitHub reads the keyword and drops every qualifier
around it** -- "Closes #12 except the GKE half" closes #12 on merge.

So the decision is not an agent's. The plan lists the issue's `requirements`;
the compiled review answers each one as `requirements: [{index, met, note}]`
in its `verdict.json`; swarm-api reads that list when the pull request opens
and again after every fix round, and only a complete, well-formed list with
every entry `met: true` sets `requirements_met`. Anything else -- no verdict,
malformed JSON, a missing or duplicated index, a plan with no requirements --
is *not* confirmed. swarm-api then writes one marked block into the pull
request's body: `Closes #N`, or `part of #N` naming what is left. Every other
closing keyword in the body and title is neutralised, and every compiled prompt
tells its agent not to write one (`NO_CLOSING_KEYWORD`), because a keyword an
agent wrote in a commit message or `pr-body.md` would close the issue on merge
whatever the review found. A requirement the gated fix went on to address is
still named as left: nothing confirmed it.

**A failed write of the block is written again, and holds the run.** Until
the block lands, nothing on the pull request may close the issue. The worker
titles a pull request for a step with an `issue` input "<the work> (part of
#N)" -- the issue's title or its number, never "Fixes #N", which it
used to write and which a squash merge would carry onto the default branch --
and every compiled prompt forbids the agent a closing keyword. The block is
still the only place that says `Closes #N`, so it is not left to one attempt:
`pull_request.keyword_written` is recorded only by a write that worked, every
CHECKING visit writes the block again while it is missing, and a run whose CI
is green stays CHECKING until it is written (`issueci.keyword_pending`). A pull
request a person merges before then is DONE regardless -- there is nothing
left to protect -- and `writeback_error` says why the block was not written.

## Auto-merge: built since contract request 47, not proven live

The issue's build order was **#342 (signed step specs, S0) → #295/#352 (the
merge step) → this**. A platform that merges code on its own must first be
unable to run a step spec nobody signed (#342), and then needs a merge step
that merges only a reviewed, proven sha (#295). Neither is something an issue
run should improvise, and until the `merge` profile was enabled an
`auto_merge` run was refused. Contract request 47 (2026-10-04) enabled it, and
the merge is now the CI loop's last move:

* **Whether it is offered** is one rule, `issueruns._auto_merge_refusal`: it
  returns nothing while the catalogue's `merge` profile is enabled, and a
  reason naming #295 (`AUTO_MERGE_REQUIRES`) only if a platform disables it
  again. `issueruns.auto_merge_availability`, served on the issue preview,
  reads the same rule, so the console's switch is enabled exactly when
  `POST /v1/runs` would accept it, and `422 auto_merge_unavailable` creates
  nothing when it would not. A run that does not say takes the platform's
  `merge_by_default` (default off).
* **The compiled workflow never merges.** It and every CI fix round say
  `metadata.merge: "off"`, whatever the run or the platform default says: a
  merge inside the workflow would run before the API wrote `Closes #N` into
  the pull request and before the CI loop could fix a red check.
* **The merge is submitted by the CI loop** (`issueci._merge`), and only once
  CI is green at the head, the keyword block is recorded written, and the
  review's verdict is `MERGE`. It is ONE merge-only continuation
  (`issueci.merge_workflow`) of the run's own task that pushed that head --
  the newest fix round's, else the integrator -- so the merge step pins the
  head that task pushed, and a head no task of the run pushed is not merged.
  Red CI on an `auto_merge` run is a fix round, never a failure. A merge the
  step refuses, or a verdict that is not `MERGE`, ends the run `FAILED` with
  the reason and the pull request left open for a person; it is not
  resubmitted for that head. The merge step itself is
  [merge-step.md](merge-step.md).

**A run without auto-merge never merges**: it compiles to `integrate` steps and
`direct-pr` fix rounds, all `claude-code`, none `single-pr` or `merge`, and the
CI loop submits no merge for it. A pull request a person merges by hand is read
as `DONE`, with `green_sha` set only if CI was green at its head.

---

## Deferred and blocked, as of this change

Named here so none of it is read as delivered:

* **Auto-merge, live** -- built (above) and proven offline only: nothing here
  has run it against a deployed environment, a real repository and real CI.
  See acceptance item 4.
* **More than one review round inside the workflow** -- the cap is accepted
  1-5 but one review/fix round is compiled; a second needs a gated
  non-integrator step under `integrate`, which is not built
  ([workflows.md](workflows.md#what-this-does-not-do)). Contract request 31
  ([contract-change-requests.md](contract-change-requests.md#31-modelspy-workflowstep-cannot-record-a-steps-verdict-gate-or-its-builds_on))
  is the related open request: `WorkflowStep` cannot record a verdict gate.
* **Personal `u-` tenants are not ticked** -- above.
* **A repository allowlist** -- deliberately not built (owner decision).
* **Frozen contract.** #454 needed no change to `apps/common/swarm_common/`:
  the run is a swarm-api document, and the planner's `issue` input is contract
  request 28
  ([contract-change-requests.md](contract-change-requests.md#28-profilespy-claude-code-and-codex-declare-an-issue-runner-input)),
  already applied.

---

## Acceptance

Each "How would you know it worked?" bullet of #454, mapped to the tests that
prove it. Unit tests run offline in CI (`application.yml`, python and ui jobs).
Paths are from the repository root; Python tests are in
`tests/unit/control_plane/` unless a path says otherwise.

**Proven offline** means the whole path ran against Firestore and GitHub fakes
(`tests/unit/control_plane/forge_fakes.py`): the real routes, the real state
machine, the real comment rendering, the real CI evaluation. **Not proven** means
it has not been run against a deployed environment, a real repository and real
CI, and nothing below claims it has.

### 1. "An issue submitted with defaults pauses in a planned state with a plan comment on the issue that names any overlapping open issue or PR. Approving it runs the plan and ends at a green PR whose body says `Closes #N`, and the issue's status comment shows the PR."

| claim | test |
|---|---|
| the defaults are `plan_approval: required`, no auto-merge, 3 fix rounds | `test_issue_runs.py::test_the_defaults_are_required_approval_no_auto_merge_and_three_fix_rounds` |
| a run starts with one planner task | `test_issue_runs.py::test_a_run_starts_planning_with_one_signed_planner_task` |
| it pauses at `PLANNED` holding no task, lease or workflow (invariant 1), and the tick leaves it there | `test_issue_runs.py::test_a_run_waiting_for_approval_creates_no_task_lease_or_workflow`, `test_issue_run_tick.py::test_a_required_run_stops_at_planned_and_holds_nothing` |
| the open issues and pull requests are read with the tenant's token and given to the planner | `test_issue_runs.py::test_the_open_work_is_read_with_the_tenants_token_and_stored_on_the_run`, `test_issue_open_work.py::test_a_small_snapshot_is_shown_whole`, `test_issue_open_work.py::test_the_prompt_stays_under_its_limit_with_a_huge_snapshot_and_says_it_cut` |
| a plan's overlaps validate and reach the plan comment as links | `test_issue_runs.py::test_the_new_fields_round_trip_through_the_schema`, `test_issue_writeback.py::test_the_plan_comment_carries_everything_the_issue_asks_for` |
| the plan comment is posted once and edited in place | `test_issue_writeback.py::test_the_plan_comment_is_posted_once_and_edited_by_an_edit` |
| approving the digest shown submits one workflow, records the approver | `test_issue_runs.py::test_approving_the_digest_shown_submits_one_new_workflow`, `test_issue_runs.py::test_approving_a_stale_digest_is_refused_and_submits_nothing` |
| the workflow's pull request, once CI is green at its head, is `DONE` with `green_sha` | `test_issue_run_ci.py::test_green_on_the_first_read_is_done_with_the_head_pinned` |
| the body says `Closes #N` when every requirement was confirmed, `part of #N` otherwise | `test_issue_run_keyword.py::test_all_met_closes_the_issue`, `test_issue_run_keyword.py::test_one_unmet_requirement_is_part_of_naming_it`, `test_issue_run_keyword.py::test_a_malformed_or_missing_review_is_part_of` |
| one status comment, edited in place, shows the pull request and the console links | `test_issue_writeback.py::test_the_status_comment_is_edited_in_place_through_approval_and_done`, `test_issue_writeback.py::test_the_status_comment_shows_the_pull_request_the_fix_round_and_the_failure` |
| the console draws overlaps, the plan, and approves the digest shown | `apps/swarm-ui/src/__tests__/intake.runs.test.tsx`: "the plan names its overlaps as links, with kind and note, ahead of the steps", "Approve sends the digest of the plan shown, and draws the answer", "a DONE run shows the green sha and Closes when every requirement was confirmed" |
| the CLI and MCP approve the digest they showed | `tests/unit/mcp/test_issue_runs.py::test_approve_prints_the_plan_and_sends_the_digest_it_printed`, `tests/unit/mcp/test_issue_runs.py::test_swarm_plan_approve_sends_the_callers_digest_and_reads_nothing_first` |

**Not proven:** that a real planner agent, given a real repository's open
work, names a real overlap in `plan.json` (the tests give the planner's output,
not run it); that the comments appear on a real GitHub issue with a real tenant
token; that a real workflow's integrator opens a real pull request and real CI
goes green.

### 2. "With `plan_approval: auto`, nothing pauses."

| claim | test |
|---|---|
| the plan is approved and the workflow submitted as soon as the plan arrives, approver `auto-approval` | `test_issue_runs.py::test_auto_approval_submits_the_workflow_as_soon_as_the_plan_arrives` |
| with no reader at all, the tick moves it `PLANNING → PLANNED → APPROVED → RUNNING`, submitted as the run's creator in the run's tenant | `test_issue_run_tick.py::test_an_auto_run_moves_planning_to_planned_to_running_with_no_reader`, `test_issue_run_tick.py::test_the_tick_cannot_submit_into_a_tenant_other_than_the_runs_own` |
| the tick follows the workflow to `CHECKING` | `test_issue_run_tick.py::test_a_running_run_follows_its_workflow_to_checking_on_the_tick` |

**Not proven:** that the deployed Cloud Scheduler job fires every minute and
its OIDC identity is admitted (the Terraform is validated in CI by
`terraform.yml`; the route's admission by
`test_issue_run_tick.py::test_an_admin_may_tick` and
`tests/unit/control_plane/test_rollup_sweeper_is_narrow.py`).

### 3. "A run whose CI stays red after 3 fix rounds ends FAILED with the log excerpt."

| claim | test |
|---|---|
| red three times with a cap of three: three rounds, then `FAILED` with `failure_excerpt`, and the status comment says so | `test_issue_run_ci.py::test_red_three_times_with_a_cap_of_three_fails_with_the_excerpt` |
| a chosen cap is honoured; 1-5 is enforced | `test_issue_run_ci.py::test_a_cap_of_one_is_honoured`, `test_issue_runs.py::test_fix_rounds_outside_one_to_five_is_a_422`, `test_issue_runs.py::test_fix_rounds_at_either_bound_is_accepted` |
| a red round followed by green is `DONE` | `test_issue_run_ci.py::test_red_then_green_after_round_two_is_done` |
| each round is one continuation of the integrator, in the run's tenant and repository | `test_issue_run_ci.py::test_the_fix_round_continues_the_runs_integrator_in_its_tenant_and_repository` |
| a round is spent only at a new head; pending spends nothing | `test_issue_run_ci.py::test_the_next_round_waits_for_ci_on_the_new_head`, `test_issue_run_ci.py::test_a_round_that_did_not_move_the_head_fails_instead_of_spending_another`, `test_issue_run_ci.py::test_pending_ci_stays_checking_and_submits_nothing` |
| the excerpt is redacted and bounded; the job log is read without the token | `test_issue_run_ci.py::test_the_excerpt_is_redacted_and_bounded`, `test_issue_run_ci.py::test_the_job_log_tail_is_read_without_the_tenants_token` |
| the console shows the excerpt as text | `apps/swarm-ui/src/__tests__/intake.runs.test.tsx`: "a FAILED run shows the failing excerpt as text, never as HTML" |

**Not proven:** the CI loop against a real repository's branch rules, check
runs and Actions logs, and a real `claude-code` fix round pushing to a real
integrator branch.

### 4. "With auto-merge on (after #295), the PR merges at the reviewed sha. Off is the default, and a run without it never merges."

| claim | test |
|---|---|
| off is the default | `test_issue_runs.py::test_the_defaults_are_required_approval_no_auto_merge_and_three_fix_rounds` |
| on is accepted while the `merge` profile is enabled, and recorded on the run; a run that does not say takes `merge_by_default` | `test_issue_runs.py::test_auto_merge_is_accepted_and_recorded_on_the_run`, `test_issue_runs.py::test_a_run_that_does_not_say_takes_the_platform_default` |
| the console's availability and the API's refusal are one rule: unavailable, naming #295 and the profile's reason, exactly when the profile is disabled | `test_issue_runs.py::test_auto_merge_availability_says_what_the_refusal_does` |
| the compiled workflow and every fix round carry no merge, whatever the run or the default says | `test_issue_runs.py::test_the_compiled_workflow_always_says_merge_off`, `test_issue_runs.py::test_an_auto_merge_run_submits_its_workflow_with_no_merge_step`, `test_issue_run_auto_merge.py::test_the_compiled_workflow_and_a_fix_round_never_carry_a_merge` |
| green with the keyword block written submits ONE merge continuing the task that pushed the head; not before the block is written | `test_issue_run_auto_merge.py::test_green_with_the_keyword_written_submits_one_merge_continuing_the_integrator`, `test_issue_run_auto_merge.py::test_no_merge_while_the_keyword_block_is_not_written` |
| red CI on an `auto_merge` run is a fix round, and the merge then continues the fix round's task | `test_issue_run_auto_merge.py::test_red_ci_on_an_auto_merge_run_is_a_fix_round_then_the_merge_continues_it` |
| no merge on a verdict that is not `MERGE`, or at a head no task of the run pushed; a refused merge fails the run with the step's reason and is not resubmitted | `test_issue_run_auto_merge.py::test_a_review_verdict_of_not_yet_fails_the_merge_and_submits_none`, `test_issue_run_auto_merge.py::test_a_head_no_task_of_the_run_pushed_is_not_merged`, `test_issue_run_auto_merge.py::test_a_refused_merge_fails_the_run_with_the_steps_reason_and_is_not_resubmitted` |
| a run without it ends `DONE` at a green pull request and never merges: no `merge` profile, no `single-pr`, no merge call to GitHub | `test_issue_run_ci.py::test_a_run_without_auto_merge_stops_at_a_green_pull_request_and_never_merges`, `test_issue_runs.py::test_a_run_without_auto_merge_submits_no_merge_step_even_when_the_default_is_on` |
| the console enables the switch only when the API reports it available, and shows the API's reason when it does not; the CLI prints a refusal plainly | `apps/swarm-ui/src/__tests__/intake.issue.test.tsx`: "auto-merge can be switched on only when the API reports it available, and is sent", "auto-merge stays disabled when the preview says it is unavailable, and shows the API's reason"; `tests/unit/mcp/test_issue_runs.py::test_auto_merge_is_passed_through_and_its_refusal_printed_plainly` |

**Built; not proven live:** "the PR merges at the reviewed sha".
`issueruns._auto_merge_refusal` returns nothing while the `merge` profile is
enabled, and `issueci._merge` submits the merge once CI is green at a head the
run pushed, its keyword block is written and the review says `MERGE`. Every
row above ran offline against the fakes; none has run against a deployed
environment, a real repository's branch rules and a real merge.

### The constraint check

| constraint | test |
|---|---|
| invariant 10: the caller cannot pick the planner's image or profile; the plan cannot name one | `test_issue_runs.py::test_a_caller_cannot_pick_the_planners_image_or_profile`, `test_issue_runs.py::test_an_unknown_key_is_still_refused_naming_it` |
| invariant 9: another tenant's run is a 404 on every route and is never ticked or read on GitHub | `test_issue_runs.py::test_another_tenants_run_reads_404_on_every_route`, `test_issue_run_tick.py::test_another_tenants_run_is_never_visited`, `test_issue_run_ci.py::test_another_tenants_checking_run_is_a_404_and_reads_no_github` |
| invariant 9: every read uses the run's own tenant's token, and a forge refusal creates nothing | `test_issue_open_work.py::test_the_snapshot_is_read_for_the_named_tenant_only`, `test_issue_runs.py::test_a_forge_refusal_refuses_the_run_and_creates_nothing`, `test_issue_runs.py::test_a_tenant_with_no_git_secret_cannot_create_a_run` |
| no token in any comment, error or URL | `test_issue_writeback.py::test_the_token_is_only_ever_in_the_authorization_header`, `test_issue_writeback.py::test_a_token_quoted_in_the_plan_is_masked_in_the_comment`, `test_issue_writeback.py::test_each_refusal_is_its_own_code_and_never_quotes_the_token` |
| invariant 9: nothing is submitted as a creator who has left the tenant; an unresolved lookup waits | `test_issue_run_tick.py::test_an_auto_run_whose_creator_left_the_tenant_fails_and_submits_nothing`, `test_issue_run_ci.py::test_a_creator_removed_from_the_tenant_fails_the_run_instead_of_a_fix_round`, `test_issue_run_ci.py::test_an_unresolved_membership_spends_no_round_and_the_next_read_does`, `test_issue_run_tick.py::test_membership_is_asked_of_the_directory_for_the_tenants_one_group` |
| a failed keyword write is written again, and a green run is not DONE without it | `test_issue_run_keyword.py::test_a_failed_keyword_write_is_written_again_and_a_green_run_waits_for_it` |
| a failed write-back is recorded, never raised, and not retried every read | `test_issue_writeback.py::test_a_403_is_recorded_on_the_run_and_never_raised`, `test_issue_writeback.py::test_the_same_failed_write_is_not_retried_on_every_read` |
