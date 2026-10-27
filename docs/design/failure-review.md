# The `failure-review` schedule type

**Status: DESIGN, not built.** Lane AUTONOMY-DESIGN, 2026-10-22, part of
#1933. Built by lanes FR-1 to FR-4 of
[agent-autonomy.md](agent-autonomy.md) §6.2, after the capability lanes they
name. Nothing fires until FR-3 merges and the owner creates the schedule.

**The request (BGD, 2026-10-22):** "set up an automation job that will run
every 6 hrs and investigate all of the agents and workflows that failed in the
past 7 hrs, do an exhaustive in depth review and root cause analysis and why
it failed. Propose a solution or a fix and, for each proposed solution,
dispatch a workflow to design, plan, implement, review test and merge each
fix".

**The owner's decisions this design builds on, not reopens** (#1933, ~04:40Z):

* a **platform** schedule type only: it runs with no Claude Code session, and
  nothing runs until it is built;
* **dispatch and merge without asking**, but **hold** anything touching IAM,
  `apps/common/swarm_common/`, secrets or tenant isolation, the other team's
  resources in `saga-agents-staging`, or cost (`needs-owner`). For this job
  only, this overrides the standing "ask before each proposal" rule;
* **all tenants** are reviewed, and only **SwarmCloud causes** are fixed here;
* **no budget cap** on the number of fix runs.

The four questions this design raised (Q1, Q7, Q8 and Q9 of
[agent-autonomy.md](agent-autonomy.md) §7) were answered by the owner on
2026-10-22 (~05:42Z, recorded on task_08e785ae3e9949d6b86b). Each answer is
written below where its question was, as "Owner decision, 2026-10-22".

---

## 1. Scope and identity

**Platform-scoped, owner-only to create.** Reading every tenant's failures
crosses tenants, so the type is `scope: {mode: "platform"}` only, like
`model-prices-check`. Creating one is the owner's, not any admin's, for the
reason [schedules.md](../schedules.md) §3 gives for the `observer` platform
variant and for `merge: auto` in the platform's repository: this schedule does
both.

**As which principal.**

| part | runs as | reads | writes |
|---|---|---|---|
| the firing, the digest, the filing, the runs | **swarm-api's own account**, called by the schedule tick's identity (`schedules.md` SD10) | every tenant's agent, attempt, event, lease, workflow and issue-run documents (the cross-tenant Firestore read `observer.compute_digest(db, None, ...)` and `cost_report` already make); the stored log tails in each tenant's log prefix (the reader `GET /v1/tasks/{id}/logs` uses); platform components' Cloud Logging entries through the `swarm-workers` view (IAM row L-1, agent-autonomy.md §3.5) | `failure_reviews/{firing_id}`, `failure_causes/{fingerprint}`, issues and comments in the SwarmCloud repository with the **platform tenant's** forge slot `swarm-tenant-<platform tenant>-git`, issue runs in the platform tenant, and per-tenant findings (below) |
| the investigation | one `claude-code` agent, and its children, **in the platform tenant** (SD7: a platform-scope schedule runs in a tenant the owner names; the one whose registration of the SwarmCloud repository carries `platform: true`) | the digest, as data; the SwarmCloud repository; its own tenant's records through `swarm-state`; GitHub through swarm-git | `rca.json` and `report.md` in its artifacts, nothing else |

So the agent never reads another tenant's records itself: invariant 9's
boundary is swarm-api's digest, and what crosses it is §3, under the
owner's decision on Q1.

**A cause in another repository's own code** (a tenant's test that fails, a
build their repository breaks) is **reported, not fixed**: swarm-api writes a
**finding** into that tenant, `failure_findings/{id}` with `tenant_id`, shown
on the failed agent's detail page and as a "needs you" item in that tenant's
bell (`swarm_api/notifications.py`, topic `failure_review`), holding the
cause sentence and evidence drawn only from that tenant's own data. It is
never filed on GitHub by this schedule, because the platform tenant's
credential is not the other tenant's and nothing here may write with it.

---

## 2. Window and skip rules

**Window.** Cron `0 */6 * * *` UTC (the type's `min_interval` is 6 h). Each
firing reviews agents, workflows and issue runs that **ended** FAILED or
DEAD_LETTERED in `[fire_at − 7 h, fire_at)`: one hour of overlap, so nothing
falls between two firings. The read is a range on `completed_at` alone, the state
filtered in memory (a single-field range, as `observer._since_query`'s
reads are), so no new composite index is needed.

**A failed firing does not lose its window.** The next firing starts at the
**last succeeded firing's window end minus one hour**, not at `fire_at − 7 h`,
capped at 48 hours back. Without it, failures from the first six hours of a
failed firing's window would be reviewed by no one.

**De-duplication within the window**: one entry per agent, whatever the
overlap; a workflow's failure and its failed step's are one entry, the step's,
with the workflow named.

**Skip rules.** Applied by swarm-api before any agent spends. Each is named,
each skipped failure is counted under its rule in the summary, and no rule
exists that the agent can add:

| rule | skips | the marker |
|---|---|---|
| `acceptance_deliberate` | the acceptance suite's deliberate failures | `metadata.source == "acceptance"` (every acceptance submission carries `{source: "acceptance", acceptance_run, check}`, `scripts/acceptance/lib.sh`) **and** the `mock` profile with input `fail: true` (`scripts/acceptance/groups/mock.sh`). An acceptance agent that failed without asking to is **not** skipped: that is a real failure, and `accept.yml`'s report issue is cited in the digest so the cause is updated there, not filed twice |
| `person_cancel` | a person's cancel, and what it cascaded | a `CANCELLED` or `CANCEL_REQUESTED` event whose `detail.requested_by` is a person (`store.request_cancel`), or `metadata.child_cascade` from such a cancel (`EndCause.CHILD_CASCADE`); a workflow FAILED only because of such a step. A cancel by the reconciler or the platform is **not** skipped |
| `verify_fence` | verify-remote's deliberate fences | the submitter service `swarm-verify` (`agent_worker/logs.py` `submitter_service`, the same list Terraform's `fencing_alert_excluded_submitters` names): the race and concurrency tests fence on purpose ([operations.md](../operations.md)) |
| `fix_round_live` | failures a running fix round already covers | the agent belongs to an issue run in `CHECKING` or `FIXING`, or to a merge step parked `CI_PENDING` with rounds left. If that run then ends FAILED, the run's failure is reviewed |
| `question_unanswered` | an agent dead-lettered because its owner question went unanswered, an await-input step nobody answered in time, and what either cancelled | `last_error: "owner question unanswered"` (agent-autonomy.md §3.9); or an await-input profile step (`awaitinput.is_await_profile`) `DEAD_LETTERED` with `last_error` beginning `await_input_expired:` (`awaitinput.AWAIT_INPUT_EXPIRED_ERROR`, written by the expiry sweep). Also a step the `fail_workflow` sweep cancelled (`end_cause: workflow_sweep`) when every failed step its `CANCELLED` event names in `detail.failed_steps` is such a silence, a `cancelled_parent` step whose ended upstream steps all are, and a workflow FAILED only because of them. A person's silence, not a defect. An `await_input_spec_refused:` dead-letter (a declaration that did not verify) **is** reviewed, and so is a sweep any FAILED agent caused |
| `person_push` | a merge step a person on its pull request pushed over | `result_summary.merge.refusal.code == "superseded_by_person_push"` (`failuredigest.SUPERSEDED_BY_PERSON_PUSH`), or that string as the `end_cause` once contract request SUPERSEDED_BY_PERSON_PUSH lands; and a workflow FAILED only because of such a step. BGD, 2026-10-27 (#2505, epic #2525): the refusal is right and is not a failure. A head from an unknown branch-mate, or a platform commit the step did not accept, is `head_moved` and **is** reviewed ([merge-step.md](../merge-step.md#revised-2026-10-27-a-persons-push-supersedes-the-merge-step-person-push-end-part-of-2505)) |
| `self` | this schedule's own investigation | `metadata.schedule_type == "failure-review"` on the investigator and its children (§9). The **fix runs** it starts are not skipped: their failures are real |

**Measured, 2026-10-22 ~04:40Z, over 7 hours** (from #1933): eng 7 (three
"runner exceeded its 7200s timeout", one OOM reclaimed as "lease silent",
two "expected outputs missing" or `published_nothing`), smoke 28 (mostly
acceptance cases), u-bogdan 0. Under these rules most of smoke's 28 are
`acceptance_deliberate` and all of eng's 7 are reviewed.

---

## 3. The failure digest

**Gathered by swarm-api, not by the agent.** Three reasons:

1. **Invariant 9.** The agent's own tools (`swarm-state`, agent-autonomy.md
   §3.4) read only its own tenant, by design. Reading every tenant is the
   platform's authority, exercised once, in one place, with one redaction.
2. **Skip rules before spend.** §2's rules are deterministic and run where the
   data is; an agent would pay to read failures it must then ignore.
3. **One shape, testable offline.** `failuredigest.py` (lane FR-2) is pure over
   the documents it is handed, as the admission logic is, so the window, the
   skips and the bound are unit-tested exhaustively with no cloud.

**What one entry holds.** Per failure, with an opaque per-firing reference
(`f-001`) and an opaque tenant label (`t1`, `t2`; swarm-api keeps the map):

* profile, backend, state, `end_cause`, `last_error` (redacted, 500 bytes),
  the workflow step and the issue run it belonged to;
* attempts: number, `exit_code`, `error`, `peak_rss_bytes` against the
  limit, `oom_near_miss`, durations;
* the last 40 events: `type`, offset from the first, `generation`, and
  `detail`'s codes;
* the lease's heartbeat series reduced to the largest gap and the gap from the
  last beat to the end, and `release_reason`;
* park reasons and refusal codes;
* **content-bearing fields only for the platform's own agents**: each
  stream's stored log tail (the last 16 KiB), the merge step's CI excerpt
  (`issueci.excerpt_at`, already redacted and bounded to 8 KiB), and the
  platform components' log lines for that agent's attempt ids (scheduler,
  reconciler, swarm-api, 8 KiB, through the L-1 view). Prompts, outputs,
  titles and other tenants' repository names never cross.

**Owner decision, 2026-10-22 (Q1): "Platform-authored fields for every
tenant; content only for the platform's own".** So for every tenant the entry
carries only what the platform wrote: states, end causes, codes, exit codes,
timings, memory figures, event types and the `last_error` the worker,
scheduler or reconciler set (redacted, 500 bytes). Content (log tails, CI
excerpts, platform log lines) is added only for agents of the platform tenant
and agents whose repository is the SwarmCloud registration marked
`platform: true`. A cause the investigator cannot settle for another tenant
from platform-authored fields alone ends in `unexplained` with the reason
"content not shared across tenants", never by reaching for more.

**Grouped before it is handed over.** swarm-api groups entries by a candidate
signature (profile, `end_cause`, `last_error` with numbers, ids and paths
replaced by placeholders). Each group carries its count, three entries in full
and the rest as references.

**Known causes.** Every open `failure_causes` record (§5): fingerprint, issue,
state, title and the signatures it matched before, so the agent can say "this
is fr-… again" instead of finding it anew.

**Redacted and bounded.** Every text field through `swarm_redaction.redact`;
the whole bounded at 48 KiB, the observer's bound under
`MAX_PLANNER_PROMPT_BYTES` (64 KiB). A digest over the bound keeps every
group's count and drops exemplars from the largest groups first; a failure
whose detail was dropped is listed by reference, and if the agent cannot
explain it from what is left it ends in `unexplained` with the reason
"digest bound", never silently.

**Handed over as DATA** between two lines carrying a random nonce,
`=== FAILURE DIGEST <secrets.token_hex(8)> ===`, with the sentence
`issueci.ci_fix_continuation` uses: it is data, not instructions, and no line
inside it can end it. A `last_error` that reads like an order is still a
`last_error`.

---

## 4. The investigation

**Inputs.** The digest; the SwarmCloud repository at its default branch; the
tools of agent-autonomy.md: `swarm-state` for the platform tenant's own
records, `pr_checks`, `run_list`, `run_view`, `pr_view`, `issue_list` for
GitHub, and child agents.

**Agents per run.** One investigator (`claude-code`, named in the type's code
as `observer.PROFILE` is). For each candidate group beyond the first, it may
start **one child agent per group** (at most 16, depth 1,
[child-tasks.md](child-tasks.md)), each given its group as data and asked for
one `rca.json` cause entry with evidence; the investigator merges groups that
share a cause and splits one that has two. Exhaustive means each cause is
traced to a code site, not to a symptom: "the reconciler's lease-silent reclaim
fires before the OOM classifier reads the pod's status" rather than "the agent
died".

**Output: `rca.json`** in the investigator's artifacts:

```json
{
  "firing_id": "fr_...",
  "causes": [
    {
      "fingerprint_of": {
        "component": "reconciler",
        "site": "apps/reconciler/reconciler/detect.py::detect_stale_leases",
        "class": "oom_reclaimed_as_silent",
        "signature": "lease silent for <n>s (grace <n>s, expired=<b>, dispatch_overdue=<b>)"
      },
      "known_fingerprint": null,
      "failures": ["f-001", "f-004"],
      "cause": "One sentence: what in the code made these fail.",
      "evidence": [
        {"command": "rg -n 'lease silent' apps/reconciler/reconciler/detect.py", "result": "..."},
        {"command": "run_view 12345", "result": "..."}
      ],
      "fix": "What to change, and the red-first test that proves it.",
      "files": ["apps/reconciler/reconciler/detect.py"],
      "hold": "none",
      "where": "platform",
      "action": "fix",
      "existing_issue": null
    }
  ],
  "unexplained": [{"failure": "f-007", "tried": "what was read, and why it did not settle it"}]
}
```

* `hold` is one of `none`, `iam`, `contract`, `secrets`, `tenancy`, `shared`,
  `cost`. `where` is `platform` or `tenant_repo` (§1).
* `action` is `fix` (the default when absent: file an issue, start a run),
  `none` or `existing` (#2003). `none` means no code change is needed -- the
  fix is already on main, or the cause is a process with no code site -- and
  `fix` then says why, with the evidence; the cause is recorded `no_change`
  with that why and nothing is filed or run. `existing` names an OPEN issue
  of the platform repository in `existing_issue`. The field exists because
  the first live take-in (2026-10-22) filed #2001 and started a run for a
  cause whose rca said in prose "No fix run should be started", and filed
  #1999 for one it called "part of #1778": the take-in reads fields, never
  prose.
* **The fingerprint is swarm-api's, not the agent's**:
  `fr-` + the first 12 hex of SHA-256 over `component|site|class|signature`.
  An agent that names a `known_fingerprint` swarm-api holds gets that one, so a
  cause found again under a slightly different site wording is not refiled.
* **Every failure is accounted for exactly once.** swarm-api validates that
  every digest reference not skipped by a rule appears in exactly one cause or
  in `unexplained`, that every cause has at least one evidence entry, and that
  `hold` and `where` are in their vocabularies. A file that fails validation
  files nothing (§9).

---

## 5. De-duplication across runs

`failure_causes/{fingerprint}`, kept by `failurecauses.py` (FR-2): `issue`,
`state` (`open`, `fixing`, `held`, `fixed`, `reported`), `runs`, `first_seen`,
`last_seen`, `failures_seen`, `signatures`, `last_firing`.

* **A new cause** is filed (§6) and recorded. The issue body opens with
  `<!-- failure-review fingerprint=fr-… -->`; before filing, swarm-api searches
  the repository's open issues for that marker, so a cause whose record was lost
  is found and not filed twice (the `observer_epics` rule).
* **A cause already filed** gets **one comment per firing** on its issue: the
  new failures (agent ids for the platform tenant, counts per tenant label
  otherwise), the window, and any new evidence. Never a second issue.
* **A cause with an open fix** (a live issue run on its issue, or an open PR
  that names it) gets no second run.
* **A cause marked fixed that recurs** (its fix merged, the same fingerprint
  again in a later window) gets a comment naming the merge and one new run. A
  cause whose **second** fix run fails or recurs is set `held` and labelled
  `needs-owner`: two attempts that did not hold is the owner's to look at.
* **A cause a person closed as not planned** gets the comment and no run.

---

## 6. From proposal to fix run

1. **File the issue** in the bug form: swarm-api renders
   `.github/ISSUE_TEMPLATE/bug_report.yml`'s sections in order (what happened,
   what you expected, where, how measured, the control), from the cause, its
   evidence and its failures, applies the form's `bug` label, and sets the
   severity: S0 when the cause is tenant isolation, the other team's
   resources, capacity accounting, fencing or secrets (CLAUDE.md "Issues").
   Written with `forgewrite.GitHubWriter.create_issue` and the platform
   tenant's slot, the path `observer` and `model-prices-check` use. The title
   states the defect as a fact.
2. **Hold, or run.** A cause with `hold` other than `none` is labelled
   `needs-owner` and gets **no run**; `issue-sweep`'s default
   `labels_exclude` already skips that label, so nothing else picks it up. A
   cause with `where: tenant_repo` gets the per-tenant finding (§1) and no
   issue here. A cause with `action: none` gets no issue and no run: it is
   recorded and listed in the firing's summary as "no change needed", and if
   the same fingerprint recurs with new failures it is a `fix`, because the code
   was not fixed after all. A cause with `action: existing` gets §5's comment
   and its run on that issue instead of a new one, when the number is an open
   issue of the platform's own repository; any other number (closed, a pull
   request, moved away, absent) is refused and the cause filed new, the reason
   in the summary, and nothing is written to another repository. A hold always
   wins: a held cause is filed `needs-owner` with no run whatever its `action`.
3. **Start the issue run** with issue-sweep's executor
   (`swarm_api/schedtypes/issue_sweep.py` `start`, with `run_options`):
   planner, implementer steps with red-first tests, review, fix rounds
   (`fix_rounds` 3), merge. One run per issue, recorded on
   `failure_causes.runs`.
4. **The merge gate.** "Dispatch and merge" means the schedule's gate is
   `plan: auto`, `merge: auto`. `merge: auto` is reachable only through the
   audited switch, `POST /v1/schedules/{id}:merge-mode`
   (`routes/approvals.py`), recorded `gate_merge_auto` in `schedaudit`: in a
   `platform: true` repository only a platform admin makes it, and in a
   one-person workspace by typing the schedule's name.
   **Owner decision, 2026-10-22 (Q8): "merge: auto, set once by you through
   the audited merge switch at creation".** The owner creates the schedule
   and makes that switch once, alone and without the two-person rule, because
   failure-review is owner-only at platform scope (`merge_mode` lets the owner
   or a platform admin switch such a type), so the `gate_merge_auto` row names the owner;
   the type's own default stays `approve` and nothing in the code sets
   `auto`. Every hold of step 5 still stops a merge. The run's merge continuation
   then merges on a `MERGE` verdict and green CI, as every issue run does.
5. **How a hold category is detected**, at three points, the stricter
   winning (`issueruns.widen_hold`):
   * **at proposal**: the cause's `hold`, from the investigator;
   * **at plan**: `issueruns.hard_stop_hold` over the plan's files and the
     paths its prompts name;
   * **at merge**: the same over the PR's **actual** changed files and patches,
     read through the forge before the merge continuation is submitted
     (`issueci._merge`'s point), because a plan's files are advisory.

   Today `hard_stop_hold` holds IAM (`IAM_PATTERNS`, any `google_*_iam_*`
   line) and the frozen contract. FR-1 adds the four categories BGD named:

   | category | held when the change touches |
   |---|---|
   | `secrets` | `scripts/create-secrets.sh`, `terraform/modules/secret_manager/**`, `apps/redaction/**`, a `google_secret_manager_*` line, a forge-slot reader (`swarm_api/forge.py`) |
   | `tenancy` | `swarm_api/auth.py`, `terraform/modules/tenancy/**`, `kubernetes/network-policies/**`, a tenant-scope function (`swarm_api/deps.py` `tenant_scope` and `submission_scope`, `GkeJobDispatcher.namespace_for`) |
   | `shared` | an added line naming any entry of `SHARED_DENY_LIST` (`scripts/lib/common.sh`), or the deny-list itself |
   | `cost` | budget caps (`BudgetCaps(`), runner sizes and classes, pool ceilings and concurrency limits, `terraform/environments/**/*.tfvars`, replica and instance counts |

   A hold sets `NEEDS_OWNER` with `approvers: owner_only` (the repository is
   `platform: true`) and labels the issue `needs-owner`.

   **Owner decision, 2026-10-22 (Q7): "Every issue run in a platform: true
   repository".** The four categories hold every issue run in a repository
   whose registration says `platform: true`, whoever started it, not only
   failure-review's. In any other repository the stops are today's (IAM and
   the frozen contract), because those categories name this platform's own
   files. Lane FR-1 builds it in `hard_stop_hold`, which already takes
   `platform`.

---

## 7. Budget

**"No cap"** is the owner's decision on the **number** of fix runs, and it
stands: no per-firing limit on causes filed or runs started.

**What the catalogue's budget fields hold.** `BudgetCaps` has three required
fields and no "unbounded" value (`scheduletypes.py`): `per_run_usd`,
`per_day_usd`, `max_concurrent`. A firing over `per_run_usd` ends
`RUN_OVER_BUDGET` and auto-pauses the schedule (§4.4).

**Owner decision, 2026-10-22 (Q9): "Tripwires: per_run_usd 200, per_day_usd
1000, max_concurrent 1".** `max_concurrent = 1` is one firing at a time, not a
fix-run limit. The two money fields are **tripwires** far above measured
spend: they never shape a normal day and stop only a runaway, with the
auto-pause landing in the owner's bell. They are the type's `BudgetCaps`
(`per_run_usd=200, per_day_usd=1000, max_concurrent=1`), which
`scheduletypes.py` makes both the caps and the defaults: a schedule may set
lower figures, and raising them is a change to the type, made in a pull
request the owner merges.

**What still bounds a runaway, with no cap:**

* the 6 h `min_interval` and one firing at a time;
* one investigator per firing, at most 16 children, depth 1;
* one live fix run per fingerprint, and two fix runs per cause before it is
  `held` (§5);
* each run's own bounds: `fix_rounds` ≤ 5, the merge wake's caps
  (`MERGE_CI_MAX_WAKES`, `MERGE_CI_MAX_SECONDS`);
* the platform tenant's admission ceilings and concurrency (invariants 2 and
  3): fix runs queue, they do not overrun capacity;
* three consecutive failed firings auto-pause the schedule (§4.4);
* the git tools' per-attempt limits (agent-autonomy.md §3.1, §3.2).

---

## 8. Reporting

**One summary per firing**, in three places that hold the same text:

* the **firing record**, `failure_reviews/{firing_id}` and the schedule's
  firing row in the console (Automate): failures read, skipped per rule,
  causes new and known, issues filed and updated, runs started, held
  `needs-owner`, findings reported to tenants, unexplained, and the window;
* the investigator's **`report.md`** artifact, shown on the firing as the
  observer's is;
* **one comment** on a standing issue named by the schedule's
  `summary_issue` parameter (optional, `owner/repo#N` in the platform's own
  repository), the `cost-report` `post_to_issue` pattern. Aggregates and
  links only: other tenants appear as counts per tenant label.

A firing with nothing to review still writes its summary ("0 failures in the
window"), with the window and the counts per skip rule, so silence is
distinguishable from a firing that did not run.

---

## 9. The reviewer's own failure

* **Nothing is half filed.** Issues are filed and runs started only after
  `rca.json` validates (§4). An investigator that fails, times out, or writes
  an invalid file leaves the firing `failed`, with its `last_error` on the
  firing's summary, and files nothing. A retried firing files nothing twice
  because every write is found again by its marker (§5).
* **It stays out of the next digest** by the `self` rule (§2): its agents
  carry `metadata.schedule_type = "failure-review"`. A reviewer reviewing its
  own crash would file an issue about itself every six hours.
* **It is still seen.** A failed firing counts toward the three consecutive
  failures that auto-pause the schedule and put it in the owner's bell; and
  the next firing's window starts from the last succeeded firing (§2), so the
  failures it did not review are reviewed next time.
* **The fix runs it starts** are not `self`: when one fails, its failure is
  reviewed like any other and explained under its own cause, and the cause it
  was fixing moves toward `held` (§5).
