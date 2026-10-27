# A workflow step that runs review-fix rounds until a review says MERGE

Status: **design only, lane FIX-LOOP-DESIGN, part of #2509.** No code changes
here. BGD asked for the design first (2026-10-26, about 17:20 PDT in Product
Oracle's session, confirmed on #2509 at about 00:10Z on 2026-10-27). Build
lanes follow BGD's answers to §9. The working name in the issue is
`fix_until_merge`. This note calls the step a **fix loop**, and §9 Q10 asks
BGD which name to ship.

Symbols are cited by name, not by line number, because line numbers move.

---

## 1. Why

Today a workflow fixes a NOT_YET once. If the second review still says
NOT_YET, the workflow is over and a person starts another one.

* **Product Oracle** runs build → review. A NOT_YET means a whole new
  workflow, with a fix lane plus a re-review. Oracle ran 14 workflows on
  2026-10-26, and 5 of them were fix rounds (#2509).
* **SwarmCloud's own lanes** run implement → review → fix → re-review →
  merge. When the re-review still says NOT_YET, the merge step refuses
  `verdict_not_merge` (`agent_worker.merge`, the `target.review` branch of the
  merge run) and leaves the pull request open. CLD then sends a new fix lane
  by hand.

The goal: **one workflow per feature carries its own loop.** It stops on
MERGE, or after a bounded number of rounds, and then waits for a person while
holding nothing.

---

## 2. What exists today, read for this design

### 2.1 The verdict file and the NOT_YET gate

A review writes `verdict.json` into `$SWARM_ARTIFACTS_DIR`. The shape is
`{"verdict": "MERGE" | "NOT_YET", "findings": [...]}`
(`agent_worker.verdict`, module docstring; `REVIEW_VERDICTS`). These keys
ride beside it, and the gate ignores all of them:

* `findings` entries marked `"severity": "minor"`. These are read as
  `MinorFinding` (`verdict.minor_findings`). The gated step files them on the
  tenant's wave epic instead of fixing them (`agent_worker.findings_epic`,
  docs/workflows.md "Minor findings are filed on the tenant's wave epic").
* A separate top-level `minors: [{file, call_site, finding}]` list, which the
  lane queue's review prompt asks for (docs/lane-queue.md §7).
* `requirements` (issue runs, `issueci.evaluate_requirements`).
* `needs_owner` / `open_questions` (#2070, `verdict.owner_question_refs`).
* `p_real` / `triage` annotations (Jev, `verdict.annotate_findings`).

The **gate** is the fix step's signed `metadata.dispatch.verdict_gate`,
`{"task_id": <review>, "verdict_in": ["NOT_YET"]}`
(`verdict.gate_from_dispatch`). The worker reads the staged file with
`verdict.read_verdict`:

* When the verdict is in `verdict_in`, the fix agent runs.
* Otherwise no agent runs, the step publishes, and it records
  `verdict_gate.agent_ran: false` and `skipped_agent: "review verdict MERGE"`.
* An unreadable file fails the attempt with `INPUTS_UNAVAILABLE`. It is read
  as neither MERGE nor NOT_YET (docs/workflows.md "What the worker refuses").

swarm-api parses the same file on its side as
`verdictpublish.parse_verdict`, for the control-plane publish (#748).

### 2.2 Why only one review-fix round compiles

* **docs/workflows.md, "What this does not do":** "One review, one
  conditional fix." Under `integrate`, only the integrator may be gated
  (`invalid_dispatch`, `detail.integrator_step_id`). A second review of the
  fixed branch needs a second gated step, and that step would not be the
  publisher.
* **`issueruns.py`** (the module docstring, "WHAT IS COMPILED, AND THE
  FIX-ROUND CAP"): an issue run's `fix_rounds` (1-5, default 3) "is the cap on
  review-then-fix rounds". The same docstring adds that ONE round is all the
  platform can compile. In practice the number bounds only the CI loop's
  rounds after the pull request opens (`issueci`).
* **`validation.REREVIEW_ROUNDS = 1`.** `on_merge_verdict` appends exactly
  one `re-review` (`validation.rereview_step_for`) and a merge that reads it
  (`merge_step_for`). The comment beside the constant gives the reason: "The
  workflow is a DAG fixed at submission, so a round is a step that exists or
  not."
* **The publisher rule.** `integrate` opens one pull request from the
  integrator. `direct-pr` refuses `when` altogether, because every step
  there opens its own pull request.

### 2.3 How the platform already appends its own rounds

`mergewake` (swarm-api's per-tenant wake tick) runs three kinds of round for a
parked merge step. All three go through one path, `_hand_off` →
`_submit_round`:

1. **Claim.** The round is claimed in the merge agent's `metadata.merge_fix`
   (`rounds`, `conflict_rounds`, `pick_rounds`) under a transaction guard
   (`_claim_round`), so two ticks never submit it twice.
2. **Submit.** The round is submitted as **its own one-step workflow**,
   **as the merge agent's submitter**, and only while that person is still a
   member (`_submitter`, `SubmitterNotMember`). The workflow is a
   `continues_task` continuation on the pull request's branch:
   `issueci.ci_fix_continuation`, `conflict_fix_continuation`,
   `pick_fix_continuation`. These are `direct-pr` with one step,
   `merge: "off"`, no `pr-title.txt` and no closing keyword.
3. **Append.** The round's agent is appended to the merge's workflow as a step
   `ci-fix:<n>` / `conflict-fix:<n>` / `pick-fix:<n>`
   (`_append_round_step`, `round_step_id`). The append is guarded in the same
   way: the workflow must belong to this tenant, must not already have that
   step id, and must hold the merge agent. A caller can never submit a step id
   with a `:` (`WorkflowStepCreate.step_id`'s pattern), so the id alone marks
   a platform round. That is what `swarm_rollup.FIX_ROUND_STEP_RE` /
   `is_fix_round_step` read.
4. **Rollup.** A round counts like any step while it is live or unreadable.
   It does **not** take part in the terminal severity while the workflow has
   steps of its own (`swarm_rollup.derive`'s docstring). So a failed round
   under a merge that later SUCCEEDED leaves the workflow SUCCEEDED, and
   `fix_round_steps` names it.
5. **One writer.** The round's metadata kind is in `cifix.ROUND_KINDS`, so the
   PR-side CI fixer treats it as the pull request's one live writer.
6. **Cancel.** `Store.cancel_workflow` cancels every step that has a
   `task_id`, and that includes appended rounds.
7. **The plugin.** `plugin/workflows/run.js` `followedStepId` strips `:<n>`,
   so a row follows the round workflow's own step (`ci-fix`, `conflict-fix`).

### 2.4 The closest existing loop: the merge step's pick-fix round (PR 2463)

docs/merge-step.md, "An unbuilt pick starts a pick-fix round":

* The merge parks `CI_PENDING`, code `owner_pick_fix_pending`, holding
  nothing.
* The tick claims a round, recording `pull_head` (the head it starts from)
  and `picks`, and submits a claude-code continuation.
* The merge counts a pick as built only under all of these conditions:
  * the round's signed spec verifies against the round's own workflow;
  * its signed `continues` is the root;
  * it SUCCEEDED;
  * it pushed a head other than `pull_head`;
  * its report says `builds_pick: true`.
* **It never loops.** A round that changed nothing, failed, or was refused
  ends the merge with `owner_pick_not_built`. A pick is never sent to a second
  round. `MERGE_PICK_ROUNDS_MAX = 2`.

This design reuses the pick-fix pattern: a park that holds nothing, rounds
claimed under a guard, signed round records, and "changed nothing" measured
by the pushed head.

### 2.5 Constraints already decided by BGD that bind this design

* **swarm-api writes no artifact.** BGD declined the pod-less re-review on
  2026-10-25 ("spin up another pod to re-review and then merge?";
  [no-pod-rereview.md](no-pod-rereview.md)). Every review verdict here
  therefore comes from a review agent's own pod. The loop step publishes no
  verdict file of its own.
* **No fix agent after MERGE** (owner decision 2026-10-05), and **no
  re-review agent after MERGE** (2026-10-10).
* **A UI lane's MERGE counts only from a rendered review** (#2184), and a
  review's runtime is chosen from its diff with `runtime: browser_if_ui`
  (#2322).
* **Per-agent budget** (PR 2466, [agent-budget.md](agent-budget.md)): a limit
  per attempt, resolved at submission and pinned into the signed
  `metadata.dispatch.budget`.

---

## 3. Designs considered

| | Where the loop lives | For | Against |
|---|---|---|---|
| **A (recommended)** | Its own **control-plane step** (`fix-loop` profile, no pod). swarm-api drives it on `task_finished` wakes and appends rounds as `review-fix:<n>` / `re-review:<n>` steps. | Works with or without a merge step, so issue runs (whose workflow has no merge, §5.3) and Oracle's lanes can use it. One step kind, named in the spec, as #2509 asks. It costs no pod between rounds. It reuses `_submit_round` / `_append_round_step` / the rollup rule. | Needs contract requests: a catalogue profile, and a park reason (§9 Q2). The merge step must learn to read the loop's last review (§4.6). |
| B | **Inside the merge step**, as a fourth kind of merge round beside ci/conflict/pick. The merge parks `CI_PENDING` code `review_fix_pending`. | No contract change. It is PR 2463's shape almost line for line. | Only a workflow with a merge step can loop. Issue runs compile `merge: "off"` (`issueruns.py`, the `auto_merge` paragraph) and would get nothing, and the same goes for Oracle lanes that leave the PR for a person. It mixes "is it good" with "merge it". `/sc check` flags `CI_PENDING` over 90 minutes as stuck (`swarm_mcp.check`, rule c), and a three-round loop can take longer than that. |
| C | **Pre-declared**: unroll `max_rounds` fix/re-review pairs into the DAG at submission. | No runtime driver. | Needs gated non-integrator steps under `integrate`, which is exactly the publisher rule in §2.2. Every pair whose gate is shut still costs a pod to republish the verdict (`Worker._republish_staged_verdict`). That is the cost the #2322 audit counted 43 times in one day. |

Also rejected: **creating the round agents inside the same workflow as real
steps** with `depends_on` / `input_from`. That would give staging for free.
But a submitted workflow is fixed in several places: the integrator's
`integrates` list, the spec signature of every step, and the
`invalid_dag` / territory refusals that run at submission. Mutating it after
submission would reopen every one of those checks. The mergewake shape
(separate round workflows plus a pointer step) is the one already proven.

---

## 4. The recommended design

### 4.1 The step in a spec

```json
{"step_id": "loop",
 "runner_profile": "fix-loop",
 "depends_on": ["fix"],
 "fix_loop": {
   "review": "review",
   "fix": "fix",
   "max_rounds": 3,
   "fix_round":    {"runner_profile": "claude-code", "model_class": "opus",
                    "prompt": "optional; the fix step's prompt when absent"},
   "re_review":    {"runner_profile": "claude-code-browser", "model_class": "fable",
                    "runtime": "browser_if_ui",
                    "prompt": "optional; the review step's prompt when absent"}
 }}
```

| field | meaning | why |
|---|---|---|
| `runner_profile: "fix-loop"` | A catalogue profile with `control_plane: true` (the shape `await-input` uses, [human-steps.md](human-steps.md) request B). The scheduler never leases it. | A loop that holds a pod between rounds would break invariant 1. The await-input precedent already gives the scheduler and the readers a way to skip it. |
| `fix_loop.review` | The step id of the review whose verdict starts the loop. It must stage `verdict.json`-style output to a dependant (`input_from`) and be a direct or transitive parent. | It is the loop's round 0. Its prompt and profile are what the re-reviews repeat, as `rereview_step_for` does. |
| `fix_loop.fix` | Under `integrate`: the gated integrator, whose `verdict_gate.task_id` is `review`. Absent under `direct-pr`. | It is the fix of round 1, already in the DAG. The loop starts by judging its head. |
| `max_rounds` | 1-5, **default 3** (§9 Q5). The number of fix agents the loop's workflow may run in total, the declared `fix` counted as round 1. | It matches `MERGE_FIX_ROUNDS_MAX = 5` and the issue run's `fix_rounds` range and default. It is not called `fix_rounds`, because `WorkflowCreate.fix_rounds` already means the merge step's **CI** fix rounds (`validation.resolve_fix_rounds`), and one name must not mean two caps. |
| `fix_round`, `re_review` | Optional. A runner profile **by name**, a `model_class`, and a prompt. Absent, each takes the `fix` / `review` step's own. | Invariant 10: profiles by name, never images, commands or resources. The caller chose these already, and the loop only repeats them. A prompt override lets a round be told "you are round n". The platform adds that preamble either way (§4.4). |

Rules checked at submission (each a 422 naming the step, nothing written):

* `strategy` is `integrate` or `direct-pr`. `collect` publishes nothing to
  loop on.
* Under `integrate`, `fix` is named, is the integrator, and is gated on
  `review` with `verdict_in: ["NOT_YET"]`. The loop `depends_on` it.
* Under `direct-pr`:
  * `fix` is absent;
  * the loop depends on the review;
  * the review `builds_on` the one implementer whose pull request the loop
    works on;
  * the review is declared `allow_empty_diff: true`, so it SUCCEEDS
    `no_change` and opens nothing.

  The worker's behaviour for a no-change `direct-pr` step with
  `allow_empty_diff` was **not verified by this design**. Lane FL-SUBMIT
  checks it first (§8).
* At most one `fix-loop` step per workflow. No step may stage from it
  (`input_from`), because it writes no artifact (§2.5).
* `metadata.merge: "on_merge_verdict"` with a loop: swarm-api appends **only
  the merge**, not the `re-review` (`rereview_step_for` is skipped). The
  merge's signed `merge_target` names the loop (§4.6). The loop's
  `re-review:1` is the re-review `on_merge_verdict` would have run. Without a
  loop, `on_merge_verdict` is unchanged (§9 Q8).
* `publish_to_pr` workflows may loop. Their integrator pushes onto the named
  pull request, and the rounds continue that integrator (§4.3).

The block is stored in the loop agent's signed `metadata.dispatch.fix_loop`,
as `verdict_gate` and `await_input` are. The frozen `WorkflowStep` carries
neither, and `GET /v1/tasks/{id}` serves it in `dispatch`. Nothing in the
frozen contract needs a new field for the block itself.

### 4.2 Round numbering

* **Round k** is fix k followed by `re-review:k`, which judges fix k's pushed
  head.
* **Under `integrate`**, fix 1 is the declared `fix` step. The loop's first
  action is `re-review:1`. Then, on NOT_YET with rounds left, `review-fix:2`
  and `re-review:2`, and so on, until `re-review:<max_rounds>`.
* **Under `direct-pr`**, the review judged the implementer's head. The loop's
  first action is `review-fix:1`, then `re-review:1`, and so on.
* **If the first review says MERGE**, the loop runs nothing.
  * Under `integrate`, the gated fix publishes and the loop ends SUCCEEDED
    with `rounds: 0`, at no pod cost.
  * Under `direct-pr`, the loop ends at once.
* **Most agents a loop can spend:** `max_rounds` fix agents and
  `max_rounds` re-review agents, one of each per round, beyond the first
  review.

### 4.3 How a round is made and where it publishes

Each half of a round is its **own one-step workflow**. swarm-api submits it
as the loop agent's submitter while that person is still a member, the rule in
`mergewake._submitter`. It is then appended to the loop's workflow as a
pointer step. This is `_submit_round` / `_append_round_step` with a new
round kind.

**`review-fix:<k>`: the fix.**

* It is a `continues_task` continuation of the **root**: the integrator
  under `integrate` (including a `publish_to_pr` integrator), or the
  implementer under `direct-pr`. Its shape is `issueci.ci_fix_continuation`'s:
  * `direct-pr`, one step, `merge: "off"`;
  * no `pr-title.txt` or `pr-body.md`;
  * `NO_CLOSING_KEYWORD`;
  * commit on top, never a rebase or force-push;
  * the `resource_class` of the step it continues when the profile matches
    (`mergewake._round_resource_class`).
* It pushes onto the pull request's own branch.
  `continuation.py` already admits both roots.
* Its metadata record is `fix_loop_round:
  {loop_task, round, kind: "fix", start_head, pull_request}`, and that kind is
  added to `cifix.ROUND_KINDS`. The round is then the pull request's one live
  writer, and the PR-side CI fixer stands down while it runs.
* **What it receives**, in its prompt, between two lines carrying a
  `secrets.token_hex` nonce, redacted (the `pick_fix_continuation` fence):
  * **The findings to fix**: the blocker and major `findings` of the verdict
    that sent it, as `verdict.pull_request_lines` renders them, bounded by
    `MAX_FINDINGS` / `MAX_FINDING_CHARS`. Minor findings stay out, as they
    stay out of today's fix (they go to the wave epic). Jev's
    `verify before acting` mark stays on any finding that carries it.
  * **Which round it is**: "review-fix round k of at most N".
  * **The pull request**: number and URL.
  * **The head it starts from** (`start_head`).
  * **Earlier rounds**: one line per earlier round (its verdict and finding
    count). The previous patches are not staged as files. They are the
    branch's history, and the agent reads them with `git log` /
    `git diff <start_head>`.

  Findings travel in the prompt, not as a staged file, because `input_from`
  stages only from a parent in the **same** workflow, and a round is its own
  workflow (§3).

**`re-review:<k>`: the review.**

* It is a one-step **`collect`** workflow with
  `repository_ref = <the 40-hex head fix k pushed>`.
  * It publishes nothing, so it needs no `allow_empty_diff` and cannot open a
    second pull request.
  * It is pinned to the exact commit, so its verdict is about a known head.
    `validation`'s abbreviated-sha refusal shows that full shas are already
    accepted as refs.
* Its profile and prompt are the review's, or the `re_review` override. The
  platform puts a preamble in front, like `REREVIEW_PREAMBLE`:
  * review the whole change against the default branch, not only the fix;
  * here are the previous verdict's findings (fenced as data);
  * write your own `verdict.json` in the same shape;
  * files the original instructions name are not staged here.
* Its metadata record is `fix_loop_round: {loop_task, round, kind:
  "review", head, pull_request}`.
* **The UI rule must still bind it** (§2.5). Today a review is UI-gated only
  when "a dependant stages `verdict.json`" from it and its staged patches or
  `builds_on` list UI paths. A lone `collect` step has neither. So:
  * swarm-api signs the lane's own changed paths into the re-review's
    dispatch block (`rendered_review_paths`). Those are the union of
    `result_summary.git.own_files` over the lane's implementers, its fix, and
    every earlier `review-fix` round.
  * The worker's rendered-review rule reads that list as the patch headers it
    would otherwise read (`agent_worker.verdict`, `UI_PATH_PREFIXES`,
    lane FL-WORKER).
  * `runtime: browser_if_ui` resolves from the same list at submission.

  A list that cannot be built resolves to the browser and to UI-gated, as
  today's failed read does.

**What drives it.** The loop needs no clock. It needs to know when a round's
agent ended. So it hangs off the `task_finished` wake that swarm-api already
receives as `POST /v1/admin/tasks/finished` (the `api_task_finished`
subscription that drives `verdictpublish.on_task_finished`). A new
`fixloop.on_task_finished` runs beside `verdictpublish.on_task_finished`:

* When the ended agent is a `fix_loop_round` of a live loop, or the loop's
  `review` / `fix`, it reads the state and claims the next action.
* The claim is a transaction on the loop agent's
  `metadata.fix_loop_state.rounds`, guarded on `seen` exactly as
  `_claim_round` is.
* A lost push is covered by a sweep in the per-tenant tick that already runs
  `mergewake.wake_tenant`. That sweep reads the PARKED loops of the tenant
  whose last round ended more than 5 minutes ago and handles them the same
  way.

`max_rounds` is read **only from the signed dispatch block**. The `rounds`
list in metadata is tenant-writable, so it is re-capped on every read, as
`mergewake._rounds_asked` re-caps.

### 4.4 What a round reads, and the decision table

After each `re-review:k` ends, swarm-api reads its `verdict.json`:

* through the size-capped, tenant-checked artifact read that `verdictpublish`
  uses for the gated fix's verdict;
* parsed with `verdictpublish.parse_verdict`;
* only after verifying the round's spec signature and its
  `fix_loop_round.loop_task` against the loop's own id.

| what the loop reads | what it does | loop ends as |
|---|---|---|
| `review` says MERGE (round 0) | nothing more | SUCCEEDED, `rounds: 0` |
| `re-review:k` says MERGE | records `final_review: <task>`, `final_head` | **SUCCEEDED** |
| `re-review:k` says NOT_YET, k < `max_rounds` | claims and submits `review-fix:k+1` | stays parked |
| `re-review:k` says NOT_YET, k = `max_rounds` | records the last verdict | **PARKED `HUMAN_REQUIRED`, `park_phase: "fix_loop_exhausted"`** (§4.5) |
| `review-fix:k` SUCCEEDED and pushed a head ≠ `start_head` | submits `re-review:k` at that head | stays parked |
| `review-fix:k` SUCCEEDED and pushed nothing (head = `start_head`) | no re-review: the head the last NOT_YET judged is unchanged, so a review agent would only repeat it | PARKED `HUMAN_REQUIRED`, `park_phase: "fix_changed_nothing"` |
| a round FAILED, was CANCELLED or DEAD_LETTERED (its own retries spent) | records it | PARKED `HUMAN_REQUIRED`, `park_phase: "round_failed"`, naming the agent and its end cause |
| `re-review:k` SUCCEEDED but its verdict does not read (not JSON, no or bad `verdict`, over 256 KiB, missing) | never read as MERGE (it would merge unreviewed work) and never as NOT_YET (it would fix findings that are not there), the §2.1 rule | PARKED `HUMAN_REQUIRED`, `park_phase: "review_unreadable"` |
| the round's submission was refused (submitter left the tenant, or a 422) | recorded on the round as `error` | PARKED `HUMAN_REQUIRED`, `park_phase: "round_refused"` |
| the pull request was merged or closed outside the workflow | read once per wake, the `_ended_outside` idea | SUCCEEDED ("merged outside") or CANCELLED |
| the pull request's head moved to a commit no round pushed (a person pushed) | the next round starts from the new head, and the re-review judges it | as above |

Every `HUMAN_REQUIRED` ending names its reason in the loop's
`result_summary.fix_loop`:

```
{rounds: [...], final_review, final_head, verdict, findings_count, phase}
```

### 4.5 Parked for a person: who resumes it, and how

The loop parks `HUMAN_REQUIRED` with a `park_phase`, the way the merge's
owner-questions hold does (#2070). It holds no lease, no pool count and no
execution. A person may then do one of three things:

1. **Add a round.** `POST /v1/workflows/{id}/fix-loop/rounds`.
   * The default allowance is one more round. A round added past
     `max_rounds` is capped by a platform hard ceiling,
     `FIX_LOOP_ROUNDS_CEILING = 10` (§9 Q4).
   * It records who and when, in a transaction guarded on the park.
   * Then the decision table runs again. After `fix_changed_nothing` or
     `round_failed`, the added round is a fix. After `review_unreadable`, it
     is a re-review of the same head.
   * An optional `note` (at most 4 KiB, redacted) is fenced into the fix's
     prompt as the person's guidance.
2. **Accept the last verdict.** `POST .../fix-loop/end {"outcome": "stop"}`.
   The loop ends FAILED `fix_loop_exhausted`. A merge step behind it then
   refuses `verdict_not_merge`, as today, and the pull request stays open.
3. **Cancel the workflow**, as today. This cancels any live round
   (`Store.cancel_workflow`, §2.3).

`human-ready` refuses a `fix-loop` agent with 409, as it refuses await-input.
A READY control-plane agent would only be parked again by the scheduler.

The park itself expires after 7 days (§9 Q3, BGD 2026-10-27: (c)). The
scheduler's `_expire_human_waits` expires a `park_phase: fix_loop_*` park 7
days after it opened and ends the loop FAILED `fix_loop_exhausted`, as a
person's "stop" would, instead of sparing it as it spares `owner_questions`.

Until it expires, `/sc check` flags the park as STUCK (§6.4).

### 4.6 The merge step reads the loop's last review

The merge cannot stage a verdict file from the loop, which writes none
(§2.5), and its signed `merge_target` is fixed at submission. So
`merge_step_for` signs `merge_target.review_loop = <loop task>` in place of
`merge_target.review`. The worker's merge then does the following:

1. It requires the loop SUCCEEDED, with signed spec verified, profile
   `fix-loop`, and the same workflow.
2. It reads `result_summary.fix_loop.final_review`, and for `rounds: 0` the
   loop's `review`.
3. It verifies that review agent:
   * signed spec, same tenant;
   * its signed `fix_loop_round.loop_task` equals the loop, or it is the
     loop's own `review`;
   * SUCCEEDED.

   It then reads its `verdict.json` through the existing tenant-checked
   upstream read (`ctx.fetch_upstream` / `verdict_mod.read_verdict`).
4. It requires that review's pinned `repository_ref` (or, for round 0, the
   head its gate judged) to equal the head it merges, or the head it already
   pins today.
5. Everything after that is unchanged: MERGE or `verdict_not_merge`, owner
   questions, CI.

The loop's `result_summary` is tenant-writable, so step 3 is the only thing
that makes it safe. A forged `final_review` can name only a review this loop
actually ran, and step 4 refuses any review that did not judge the merged
head.

### 4.7 Invariants

1. **Only LEASED / DISPATCHED / STARTING / RUNNING create demand.** The loop
   step is `control_plane`, never leased, and PARKED throughout. Between
   rounds no agent is live: the next half is submitted only after the previous
   one's `task_finished`. Each half is QUEUED → READY → admitted like any
   agent. Nothing is pre-created as a pending pod.
2. **All-or-nothing reservation.** Each round agent is admitted by the
   scheduler's ordinary transaction. swarm-api writes no lease and no pool
   count, the same rule that was decided for #748.
3. **Concurrency counts from LEASED.** The rounds are ordinary agents, so they
   count exactly as their profile does. A loop at most has one live round
   agent at a time.
4. **Workers never sleep through a long wait.** No worker waits for a round.
   The loop is a parked document, and the round agents are separate
   attempts.
5. **Fencing.** Every round is a new agent with its own lease and generation,
   and its worker fences as every worker does. A stale round attempt exits
   without running. A round's head is accepted only from its signed spec and
   recorded `pushed_head` (the pick-fix rule).
6. **Spot.** No change.
7. **Requests and limits.** No change: round agents take their profile's
   class, or the continued step's class (`_round_resource_class`), never a
   bigger box.
8. **Checkpointing.** Each round agent checkpoints as any agent does. The
   loop needs none: its state is the Firestore document.
9. **Tenant isolation.** Rounds are submitted in the loop's tenant, as its
   submitter, pushing with that tenant's forge credential through the
   continuation rules. The verdict reads are tenant-checked.
10. **Profiles by name.** The loop repeats profiles the caller named. Nothing
    else is accepted.

### 4.8 Cost and budget

* **Per agent.** Each round agent is submitted through
  `SubmissionService._build_task`, so `budget.resolve` pins its own
  min(step, workspace, ceiling) into its dispatch block (PR 2466). A round
  inherits the step `budget` of the step it repeats (`fix` or `review`), so a
  caller's lower limit binds every round, never a higher one.
* **Per loop.** The worst case is bounded by count: `max_rounds × (fix budget
  + review budget)`, and `max_rounds ≤ 5`. The recommendation is **no
  separate loop budget in the first build** (§9 Q6). The loop records each
  round's `cost_usd` and token totals in `fix_loop_state` and the console
  shows the sum. A loop-level `usd` cap is a later lane if the sums show one
  is needed.
* **What it saves.** Today each extra round costs a whole new workflow,
  built by hand: an implementer-less fix lane plus a review, and on Oracle's
  side a new submission. The loop spends exactly the fix and review agents
  of those workflows and none of their wiring.

---

## 5. How SwarmCloud's lanes and Product Oracle would use it

### 5.1 A SwarmCloud lane: implement → review → fix → loop → merge

```json
{"strategy": "integrate",
 "repository_url": "https://github.com/bogdan-alexandrescu/SwarmCloud.git",
 "label": "EXAMPLE-LANE",
 "merge": "on_merge_verdict",
 "steps": [
  {"step_id": "implement", "runner_profile": "claude-code", "model_class": "opus",
   "input": {"prompt": "Implement the change in the brief."}},
  {"step_id": "review", "runner_profile": "claude-code-browser", "runtime": "browser_if_ui",
   "model_class": "fable",
   "depends_on": ["implement"], "builds_on": "implement",
   "input_from": {"implement": "swarm-work.patch"},
   "input": {"prompt": "Review swarm-work.patch. Do not edit files. Write verdict.json: {\"verdict\", \"findings\"}; minors as severity minor."}},
  {"step_id": "fix", "runner_profile": "claude-code", "model_class": "opus",
   "depends_on": ["review"], "builds_on": "implement",
   "input_from": {"review": "verdict.json"},
   "when": {"step": "review", "verdict_in": ["NOT_YET"]},
   "input": {"prompt": "Fix every blocker and major in verdict.json. Change nothing else."}},
  {"step_id": "loop", "runner_profile": "fix-loop", "depends_on": ["fix"],
   "fix_loop": {"review": "review", "fix": "fix", "max_rounds": 3}}
 ]}
```

swarm-api appends `merge`, reading the loop (§4.6). The `re-review` step
that `on_merge_verdict` appends today is not appended. The workflow page
then shows, as rounds run: `re-review:1`, `review-fix:2`, `re-review:2`, and
so on.

### 5.2 A Product Oracle lane: build → review → fix → loop, no merge

Oracle submits as its own service tenant (`u-or-9a0792`,
docs/multi-tenancy.md) and leaves the pull request for a person to merge:

```json
{"strategy": "integrate",
 "repository_url": "https://github.com/<oracle-owner>/<oracle-repo>.git",
 "label": "oracle-build-<feature>",
 "merge": "off",
 "steps": [
  {"step_id": "build", "runner_profile": "claude-code",
   "input": {"prompt": "Build <feature> as specified."}},
  {"step_id": "review", "runner_profile": "claude-code",
   "depends_on": ["build"], "builds_on": "build",
   "input_from": {"build": "swarm-work.patch"},
   "input": {"prompt": "Review the change. Write verdict.json."}},
  {"step_id": "fix", "runner_profile": "claude-code",
   "depends_on": ["review"], "builds_on": "build",
   "input_from": {"review": "verdict.json"},
   "when": {"step": "review", "verdict_in": ["NOT_YET"]},
   "input": {"prompt": "Fix every finding in verdict.json."}},
  {"step_id": "loop", "runner_profile": "fix-loop", "depends_on": ["fix"],
   "fix_loop": {"review": "review", "fix": "fix", "max_rounds": 2,
                "re_review": {"model_class": "sonnet"}}}
 ]}
```

The workflow ends SUCCEEDED with the pull request open and the loop's
`verdict: MERGE`. Or it ends with the loop parked for a person, which Oracle's
client reads from `GET /v1/workflows/{id}` as `phase` and the last findings.
One submission replaces what was up to five workflows on 2026-10-26.

The repository placeholders are Oracle's to fill in. This design does not
name Oracle's repository.

### 5.3 Issue runs

`issueruns` compiles the loop after its gated fix, with `max_rounds =
fix_rounds`. The run's cap then means what its docstring always said, and
"ONE round is what the platform can compile today" stops being true.
`issueci` reads the review for `requirements` and for "merge only when the
verdict is MERGE" through the loop's `final_review` (§4.6's resolution), not
the first review (§9 Q9).

---

## 6. How it shows

### 6.1 The rollup

* `swarm_rollup.FIX_ROUND_STEP_RE` grows to accept the two new round ids:

  ```
  ^(?:(?:ci|conflict|pick|review)-fix|re-review):[1-9][0-9]{0,2}$
  ```

  `re-review` without `:<n>` (the `on_merge_verdict` step,
  `validation.REREVIEW_STEP_ID`) stays a caller-visible ordinary step,
  because the pattern needs the colon.
* The terminal rule already fits: a round is judged by its owner step. For
  merge rounds that is the merge. For loop rounds it is the loop step, whose
  own ending (SUCCEEDED, or FAILED `fix_loop_exhausted`) is the verdict on
  them. A failed `review-fix:2` before a MERGE in `re-review:3` leaves the
  workflow SUCCEEDED.
* While the loop is PARKED and a round runs, the round is live, so the
  workflow is RUNNING (`steps_hold_capacity`). Between halves the loop's
  PARKED makes it the pending state, never "finished".
* The reconciler's `WorkflowView` derives from step ids alone, so it needs
  only the regex.
* The test that holds `mergewake.ROUND_KIND` equal to the pattern
  (`tests/unit/control_plane/test_merge_auto_fix_rounds.py`) learns the
  second source: `fixloop.ROUND_KIND`.

### 6.2 The workflow page (console)

* The loop step's row reads `round k of N`, then the half that is running
  (`re-review running`, `fix running`), then the outcome: `MERGE after k
  rounds`, or `waiting for you: <phase>` with the two actions **Add a
  round** and **Stop**.
* Appended rounds render as the steps they already are, as `ci-fix:<n>`
  rows do, each linking to its own agent.
* The last verdict's findings show beside the loop, using the existing
  verdict view (`pull_request_lines`'s rendering, and the diff viewer's pins
  when findings carry locations).

### 6.3 The sc plugin: rows and band

* `plugin/workflows/run.js` `followedStepId` already strips `:<n>`, so a row
  for `re-review:2` follows the round workflow's step `re-review`. The round
  workflows' step ids must therefore be exactly `review-fix` and
  `re-review`. Lane FL-DRIVE holds that with a test against the constant.
* The loop's own row follows a control-plane agent. It shows the phase and
  the round counter from `result_summary.fix_loop` / `fix_loop_state`, the
  way an await-input row shows its wait (`swarm_mcp.progress`,
  `swarm_mcp.compact`).
* The band's summary counts a parked loop with the "needs you" items, as it
  counts `AWAITING_INPUT`.

### 6.4 `/sc check`

* Rule (c) already flags PARKED `HUMAN_REQUIRED` as STUCK. The loop's
  evidence carries `park_phase`, the last verdict's finding count and the
  round counter. The next command it names is the add-a-round call (a new
  `swarm_fix_loop` tool, or `scripts/api.sh POST
  /workflows/<id>/fix-loop/rounds`).
* A loop parked on a round in flight is the platform working, and is not
  flagged. Only the round agent can be flagged, by its own rules (a) and (b).
* Rule (d) `merge_refused` is unchanged. A merge that refused
  `verdict_not_merge` behind a stopped loop is flagged as today.

### 6.5 The pull request

Round fixes write no PR text (the CI-round rule), so the body's verdict block
written by the integrator goes stale after round 1. swarm-api therefore
keeps **one status comment** on the pull request, edited in place: round k
of N, each verdict, and the finding counts (not the findings, which are an
agent's text and stay in the verdict artifacts). It is written with the
tenant's own token, as the issue run's status comment is
(`issuecomments`). Minor findings keep going to the wave epic, filed by the
step that read them: the integrator for round 1, and the loop for later
rounds, through the same `findings_epic` writer. This is lane FL-DRIVE's to
place, and §9 Q7 asks whether the loop files minors at all.

---

## 7. Frozen contract: requests (BGD applies, lane FL-CONTRACT)

* **A.** A `fix-loop` catalogue profile, `control_plane: true`, shipped
  unavailable until FL-ACCEPT passes, as `await-input` was
  (`profiles.py`, `terraform/infra/locals.tf`).
* **B.** A park reason while rounds run (§9 Q2). The recommendation is a new
  `ParkReason.FIX_ROUNDS_RUNNING`: never promoted by the scheduler, ended by
  swarm-api PARKED → SUCCEEDED / FAILED / CANCELLED, and moved to
  `HUMAN_REQUIRED` with a `park_phase` when a person is needed. All of those
  edges are already in `_ALLOWED`; changing a park reason is a field write on
  a PARKED agent, not a transition. The alternative,
  `HUMAN_REQUIRED` from the start, would make `/sc check` and the console
  call every running loop "waiting for you".
* **C.** None for the block itself: `fix_loop` lives in
  `metadata.dispatch`, inside the signed spec, as `verdict_gate` does
  (contract request 29's precedent).

Each becomes an entry in `docs/contract-change-requests.md`, written by
FL-CONTRACT.

---

## 8. Build lanes (files do not overlap)

| # | lane | files | red-first test it must turn green |
|---|---|---|---|
| 0 | **FL-CONTRACT** (BGD applies) | `apps/common/swarm_common/states.py`, `profiles.py`, `terraform/infra/locals.tf`, `tests/terraform/catalogue.tftest.hcl`, `docs/contract-change-requests.md`, `tests/unit/common/test_fix_loop_contract.py` (new) | `fix-loop` is `control_plane`, unavailable, in `profiles_without_a_job`; the new park reason is in no capacity set; the parity script passes |
| 1 | **FL-ROLLUP** | `apps/workflow-rollup/swarm_rollup/__init__.py`, `apps/reconciler/reconciler/model.py` (only if it restates the pattern), `tests/unit/control_plane/test_merge_auto_fix_rounds.py`, `tests/unit/control_plane/test_fix_loop_rollup.py` (new) | `re-review:2` and `review-fix:3` are rounds and `re-review` is not; a FAILED `review-fix:2` under a SUCCEEDED loop rolls up SUCCEEDED |
| 1 | **FL-SUBMIT** | `apps/swarm-api/swarm_api/schemas.py`, `validation.py`, `service.py`, `tests/unit/control_plane/test_fix_loop_submission.py` (new) | each §4.1 refusal is a 422 naming the step; `max_rounds` 0 and 6 are refused, absent is 3; the block is in the signed dispatch and changes the digest; `on_merge_verdict` with a loop appends a merge with `review_loop` and no `re-review`; first, a measurement of a `direct-pr` no-change review with `allow_empty_diff` |
| 1 | **FL-SCHED** | `apps/scheduler/scheduler/loop.py`, `apps/scheduler/scheduler/store.py`, `tests/unit/control_plane/test_fix_loop_scheduler.py` (new) | a READY `fix-loop` agent is parked, never leased, with any headroom; `_expire_human_waits` expires a `park_phase: fix_loop_*` park after 7 days and ends the loop FAILED `fix_loop_exhausted` (Q3 (c)), and spares one younger than that |
| 2 | **FL-DRIVE** | `apps/swarm-api/swarm_api/fixloop.py` (new), `mergewake.py` (move `_append_round_step` / `_submit_round` to a shared helper inside it, or export them), `cifix.py` (`ROUND_KINDS`), `routes/admin.py` (the finished hook), `routes/fix_loop.py` (new: add-round / end), `main.py`, `tests/unit/control_plane/test_fix_loop_drive.py` (new), `tests/unit/control_plane/test_fix_loop_routes.py` (new) | every row of §4.4's table; two wakes submit one round; `max_rounds` read from the signed block, not the tenant-writable list; a round refused for a submitter who left parks `round_refused`; the round step ids equal `followedStepId`'s expectation |
| 2 | **FL-WORKER** | `apps/agent-worker/agent_worker/merge.py`, `verdict.py` (`rendered_review_paths`), `tests/unit/worker/test_fix_loop_merge.py` (new), `tests/unit/worker/test_fix_loop_rendered.py` (new) | a merge with `review_loop` merges on the final re-review's MERGE at the head it judged; refuses a `final_review` from another loop, a non-SUCCEEDED one, or one that judged another head; a re-review with UI paths in `rendered_review_paths` on `claude-code` reads NOT_YET |
| 3 | **FL-ISSUE** | `apps/swarm-api/swarm_api/issueruns.py`, `issueci.py`, `tests/unit/control_plane/test_issue_run_fix_loop.py` (new) | a compiled plan carries a loop with `max_rounds = fix_rounds`; `Closes #N` and the CI loop's merge read the final review |
| 3 | **FL-UI** | `apps/swarm-ui/src/WorkflowViews.tsx`, `apps/swarm-ui/src/components/StatePill.tsx`, `apps/swarm-ui/src/types.ts`, `apps/swarm-ui/src/__tests__/fixLoop.test.tsx` (new) | the loop row shows round k of N and the two actions on a parked loop; round rows link their agents |
| 3 | **FL-PLUGIN** | `apps/swarm-mcp/swarm_mcp/check.py`, `progress.py`, `compact.py`, `server.py` (the `swarm_fix_loop` tool), `plugin/workflows/run.js`, `plugin/skills/sc/SKILL.md`, `tests/unit/mcp/test_fix_loop_check.py` (new), `plugin/hooks/band.test.ts` | `/sc check` flags an exhausted loop with its phase and the add-round command, and does not flag a loop with a round running |
| 4 | **FL-DOCS** | `docs/workflows.md` ("What this does not do" and a new section), `docs/merge-step.md`, `docs/issue-runs.md`, `plugin/skills/delegate/SKILL.md` | `tests/unit/scripts/` doc guards that already read those files |
| 5 | **FL-ACCEPT** | `scripts/acceptance/groups/workflow.sh` | a mock-profile workflow whose review says NOT_YET twice then MERGE ends SUCCEEDED with `re-review:1`, `review-fix:2`, `re-review:2` appended |

Order: 0 first. Then 1 in parallel. FL-DRIVE needs FL-ROLLUP merged, because
otherwise `_append_round_step` refuses the new ids. FL-ISSUE, FL-UI and
FL-PLUGIN follow FL-DRIVE. FL-DOCS lands last, with what was built.

`mergewake.py` is FL-DRIVE's alone. FL-WORKER owns `agent_worker/merge.py`,
and no other lane touches it.

---

## 9. Questions for BGD

The same questions, with options, are in this lane's `questions.json`.

**Q1. Where the loop lives.**
(a) its own control-plane step, `fix-loop`;
(b) inside the merge step, as a fourth merge round kind;
(c) pre-declared fix/re-review pairs at submission.
**Recommended: (a).** It is the only one that serves issue runs and lanes
without a merge step, and it costs no pod on the MERGE path (§3).

BGD 2026-10-27: (a).

**Q2. The park reason while rounds run** (contract request B).
(a) a new `ParkReason.FIX_ROUNDS_RUNNING`;
(b) `HUMAN_REQUIRED` with a `park_phase: "fix_loop_running"` from the start;
(c) `AWAITING_INPUT` with a phase.
**Recommended: (a).** (b) and (c) tell every reader that a running loop
waits for a person.

BGD 2026-10-27: (a). Contract request B, accepted.

**Q3. When the loop parks for a person, does the park expire?**
(a) no expiry, like the owner-questions hold;
(b) the default 25-hour `HUMAN_REQUIRED` dead-letter;
(c) 7 days, then FAILED `fix_loop_exhausted`.
**Recommended: (a).** It holds nothing, and a person decides.

BGD 2026-10-27: (c), not the recommended (a). What changes because of it:
§4.5's park expiry is no longer "no expiry". The scheduler's
`_expire_human_waits` expires a `park_phase: fix_loop_*` park 7 days after
it opened and ends the loop FAILED `fix_loop_exhausted`, instead of sparing
it as it spares `owner_questions`; FL-SCHED's row in §8 tests that instead.

**Q4. Who may add a round, and how far past `max_rounds`?**
(a) any full member of the tenant, up to a platform ceiling of 10 rounds in
total;
(b) only the workflow's submitter or a tenant admin, ceiling 10;
(c) only a tenant admin, no rounds past `max_rounds`.
**Recommended: (a).** It matches who may cancel a workflow
(`Store.cancel_workflow`), and the ceiling bounds cost.

BGD 2026-10-27: (a).

**Q5. The default `max_rounds`.**
(a) 3, the issue run's `fix_rounds` default;
(b) 2, `LANE_FIX_ROUNDS_DEFAULT`;
(c) 1, today's behaviour unless asked.
**Recommended: (a).** On 2026-10-26 Oracle's 5 fix workflows came from 14
lanes, so most lanes converge in 1-2 rounds, and 3 leaves room.

BGD 2026-10-27: (a).

**Q6. A loop-level spend cap.**
(a) none beyond the per-agent budget times `max_rounds`, and the loop shows
the sum;
(b) an optional `fix_loop.budget: {usd}` that parks the loop
`budget_spent` when the rounds' summed `cost_usd` passes it;
(c) a workspace-level loop cap beside the per-agent one.
**Recommended: (a) now, (b) later if the sums show a need.**

BGD 2026-10-27: (a).

**Q7. Minor findings from rounds after the first.**
(a) the loop files them on the wave epic, as the gated step does for round 1;
(b) dropped after round 1;
(c) kept in the loop's summary only.
**Recommended: (a).** CLAUDE.md says minors go to the wave epic, and later
rounds find them too.

BGD 2026-10-27: (a).

**Q8. `on_merge_verdict` and the loop.**
(a) with a declared loop, append only the merge, reading the loop; without
one, unchanged;
(b) `on_merge_verdict` always adds a loop (default `max_rounds`) when the
integrator is gated;
(c) keep them apart: a loop and `on_merge_verdict` together are refused.
**Recommended: (a).** It changes nothing for workflows that do not ask, and
(b) can follow once loops are proven.

BGD 2026-10-27: (a).

**Q9. Issue runs.**
(a) compile the loop with `max_rounds = fix_rounds`;
(b) leave issue runs at one round;
(c) a separate `review_rounds` field on the run.
**Recommended: (a).** The cap already says it means review-fix rounds
(`issueruns.py`).

BGD 2026-10-27: (a).

**Q10. The name.**
(a) profile `fix-loop`, block `fix_loop`;
(b) `fix-until-merge` / `fix_until_merge`, the issue's working name;
(c) `review-loop` / `review_loop`.
**Recommended: (a).** It is short, and it follows `await-input` /
`await_input`.

BGD 2026-10-27: (a).

---

## 10. What this design did not verify

* **Whether a `direct-pr` review with `allow_empty_diff: true` and no change
  ends SUCCEEDED without opening a pull request.** FL-SUBMIT measures this
  before relying on it. If it does not, `direct-pr` loops are refused in the
  first build and only `integrate` is offered.
* **Whether the worker records `pushed_head` for a continuation that pushed
  nothing in the shape the "changed nothing" row reads.** PR 2463 reads
  `pull_head` against the round's pushed head. FL-DRIVE reuses its read.
* **Whether a `collect` step accepts a 40-hex `repository_ref` end to end**
  (clone, checkout). The API accepts the shape, and the abbreviated-sha
  refusal implies the full one is read. FL-DRIVE's first test measures it.
