# The per-agent budget: a limit in tokens or dollars that stops a runaway step

Issue #2459. Backend built by lane BUDGET-CAP (2026-10-26). The console's
controls are a separate lane.

## 1. Why

Until #2459 the only limit on an agent was its step's timeout. On 2026-10-26
four steps ran their full 7,200 s; one of them spent two hours with its CPU
pegged and $2.34 of model use, and others moved accounts mid-run on
exhausted quota (#2457). A timeout bounds wall time, not spend: an agent
looping fast spends more in the same two hours than one waiting on a build.

## 2. BGD's decisions (2026-10-26), and where each lives

| Decision | Where |
|---|---|
| A workspace admin sets a per-agent budget, in tokens or dollars, the unit chosen per workspace | `swarm_api/budget.py` (`BudgetStore`), `PUT /v1/workspaces/{tenant_id}/budget` (`routes/budget_settings.py`) |
| A platform admin sets a ceiling no workspace default can exceed | `agent_budget_ceiling` in the platform settings (`store.PLATFORM_SETTINGS_DEFAULTS`), `PUT /v1/admin/settings` |
| A step may set a lower `budget` (`{usd: N}` or `{tokens: N}`), never a higher one | `schemas.StepBudget` on `TaskCreate` and `WorkflowStepCreate`; `budget.refuse_step_above` |
| An attempt's budget is min(step, workspace, ceiling); with mixed units, each limit is checked | `budget.resolve`, called in `SubmissionService._build_task` |
| At 80% the agent is told to finish and publish, and the console can show it | `agent_worker/budget.py` (`BudgetGuard`), `runners/budget_note_hook.py` |
| At 100% the worker stops the agent at its next turn boundary, checkpoints, publishes, and ends FAILED `budget_exceeded` | `runners/budgetwatch.py`, `lifecycle._record_finalised` |
| Off until a workspace admin sets one; nothing changes for a workspace that sets nothing | `resolve` returns None; the dispatch block, child environment and CLI settings are byte for byte what they were |
| Dollars are the `cost_usd` estimate; tokens are the runner's own reports, input plus output plus cache reads | `agent_worker.budget.tokens_of`, `BudgetGuard._price` |

## 3. Resolved at submission, pinned in the signed spec

The budget is resolved once, when the step is submitted, and written into
the typed `Task.budget` -- `{usd?, tokens?}` -- which the step-spec
signature covers at format 6 (contract request 120, applied 2026-10-27 by
lane BUDGET-CONTRACT). Where each limit came from is
`metadata.dispatch.budget_from: {step, workspace, ceiling}`, inside the
signed dispatch block, and a workflow's own document records what each step
asked for as `WorkflowStep.budget`. The worker reads the budget only from the
verified document. A task stored before request 120 carries it in
`metadata.dispatch.budget` (`{usd?, tokens?, from}`) instead; the worker
(`Limits.from_task`) and the served `budget` (`codec.served_budget`) read that
as the fallback for one release.

Why not read the workspace setting in the worker at each attempt:

* **The worker reads no key the signature does not cover.** The workspace
  settings document is one any tenant worker can write
  ([tenant-settings-integrity.md](tenant-settings-integrity.md) §1.3), and
  the `budget` block is signed only since contract request 119, and only
  for a tenant on the MAC cutover. A worker that read it live would enforce
  whatever another tenant's agent wrote wherever it is not.
* **Workers are moving off direct Firestore access** (#1457, design B).
* **It is the platform's pattern.** `timeout_seconds` is pinned at
  submission, and so are `max_additional_repositories` and its budget:
  "a task already submitted keeps what it was pinned with".

So a change to a workspace default or the ceiling reaches the next
submission, not a step already queued. The LIMIT is per attempt: the worker
counts each attempt's own usage, so a continuation of a step that ended
`budget_exceeded` starts from zero against its own resolved budget.

## 4. The ceiling, and what "off" means

* **The ceiling alone turns nothing on.** With no workspace default and no
  step budget there is no budget at all, whatever the ceiling says ("off
  until a workspace admin sets one"). Once either is set, the ceiling is one
  of the three limits in the min. BGD kept this on 2026-10-26 (~22:15Z,
  question 4): a ceiling does not cap a workspace that sets nothing.
* **A ceiling lowered under a default saved earlier still binds**: the min
  takes it at the next submission.
* **Both refusals ship ON** (`refusals.SWITCHES`, `default=True`), by BGD's
  answer of 2026-10-26 (~22:15Z, question 5, "enforce at once") rather than
  the 2026-10-08 rule's report-only start: `budget_above_limit` (a step
  asking for more than the default or the ceiling, 422 naming both) and
  `budget_above_ceiling` (a default above the ceiling, 422 naming the
  ceiling) are served from the first deploy, with no `REFUSAL_*` variable
  and nothing in terraform's `api_refusals`. Report-only lost no limit,
  because the min holds either way; it is on so the caller is told at once
  rather than finding the lower limit in `dispatch.budget` later. The
  switches stay as kill switches: `REFUSAL_BUDGET_ABOVE_LIMIT=off` or
  `REFUSAL_BUDGET_ABOVE_CEILING=off` (through `api_refusals`) lets the
  request through, and the step then runs under the lower limit and the
  default is capped.

## 5. Enforcement: two processes, one stream

The CLI's stream is read by the RUNNER, through the stdout capture's tap
(`cliagent._run_watched`), so a stream past the capture's cap is still read
whole -- the same reason the account channel reads the pipe, not the file.
The runner cannot price anything and cannot write the agent's events; the WORKER
can. So the work splits as the account channel's does:

1. The runner's `BudgetStreamWatcher` writes the usage it has seen to a
   channel file in the worker's `private/` (never `work/`, which a
   checkpoint archives and the agent can write).
2. The worker's `BudgetGuard`, on each slice of the supervision loop, reads
   the channel, prices it and decides.
3. It answers through a signal file: `stop` (100%) is honoured by the runner
   at the next turn boundary -- a top-level `user` event, the tool results
   of a turn in and the next request not yet answered, the boundary the
   account swap uses -- and `kill` (120%) at once.
4. When the runner has exited, the guard folds its usage in: an account
   move or a credential reload starts another runner on the same session,
   and its usage adds to the attempt's.

**Counted.** Tokens: input plus output plus cache reads, the last copy of
each message id (the stream repeats a message once per content block), and
a subagent's messages too, because they cost money. Cache writes are not
counted: the owner's rule names input, output and cache reads. Dollars: a
live run has not reported its cost (claude-code prints `total_cost_usd`
only in its last event), so the running figure is the per-model usage priced
by swarm-api (`POST /v1/attempts/{id}/spend-estimate`, `spendprice`) -- the
same estimate a stopped run's `cost_usd` is -- at most every 20 s. The
worker holds no price table (#1481). A dollar figure swarm-api cannot give
(no route, a model it has no price for) counts as nothing: the token limit,
if any, still binds, and the log says the figure was not priced.

**Telling the agent at 80%.** A headless claude-code run takes no new
message while it runs. The one place text reaches it mid-run is a
PostToolUse hook's `additionalContext`, shown with the next tool result, so
the worker writes a note and `budget_note_hook.py` hands it on -- once,
because the hook renames the note before it reads it. `codex exec` has no
such mechanism: the warning event says `told: false`.

**The event.** `EventType.BUDGET_WARNING` (contract request 121), with
`detail` `{cause: "budget_warning", percent, told, limit, used}`. Before the
type it was written as `running` with the same detail; the API's one event
decoder (`codec.stored_event_type`) reads that form as `budget_warning` for
one release, so every reader of the type sees one vocabulary.

**The end.** The runner raises at once after a budget stop, so no finish or
repair pass spends more. The lifecycle's `_record_finalised` checkpoints and
publishes exactly as it does for a timed-out step (the publish is withheld
only from a run that finished cleanly and failed a check), then ends the
step FAILED with `last_error` `budget_exceeded: ...` and
`result_summary.budget: {limit, used, warned, end_cause: "budget_exceeded",
stopped: "turn_boundary" | "mid_turn"}`, and `end_cause`
`EndCause.BUDGET_EXCEEDED` (contract request 118). A task ended before the
member was applied carries `end_cause` null and the cause in the summary
alone. The outcome ledger classes the cause `runner_error`, as its text
fallback classed those earlier ends, so its classes keep their meaning. An agent that ends on its own after the stop
was asked, before it reached a boundary, has finished: it is judged as any
finished run, not as a budget stop.

## 6. What it does not do

* **codex is not stopped mid-run today.** The `codex` profile runs `codex
  exec` without `--json`, so its stdout is prose and carries no usage. The
  meter reads codex's `turn.completed` usage events when a stream carries
  them. BGD answered "switch codex to `--json`" on 2026-10-26 (question 6);
  a follow-up lane makes that switch after this lands. Until then a codex step's budget is pinned and shown, and enforced by
  nothing.
* **A profile with no CLI stream** (mock, generic, the worker actions) has
  nothing to enforce from, and gets no guard.
* **No park.** A budget stop is FAILED, not parked: `ParkReason.
  BUDGET_EXHAUSTED` (contract request 39) still names a park nothing writes.

## 7. Frozen contract requests

118 `EndCause.BUDGET_EXCEEDED`; 119 a `budget` block in
`SETTINGS_MAC_FIELDS`; 120 `Task.budget` / `WorkflowStep.budget`; 121
`EventType.BUDGET_WARNING`. BGD ACCEPTED all four on 2026-10-26 (~22:15Z);
lane BUDGET-CONTRACT applied them on 2026-10-27 and switched the readers and
writers over, keeping the old forms readable for one release. Each is in
[contract-change-requests.md](../contract-change-requests.md).

## 8. Invariants

* **1**: a budget holds no capacity; a stopped step is FAILED and its lease
  released in the same write.
* **4**: nothing sleeps; the guard is a file stat per slice of the loop
  that already runs.
* **5**: the end is written under the attempt's generation, like every
  worker end; the budget files are the attempt's own.
* **9**: the ceiling is the platform's, in `control/settings`; the step's
  budget is inside its own signed spec; the workspace default is its own
  document's, signed by request 119.
* **10**: a caller names a number in a unit, which may only lower a limit,
  as `timeout_seconds` does; never an image, command, resource or backend.
* **2, 3, 6, 7, 8** are untouched.
