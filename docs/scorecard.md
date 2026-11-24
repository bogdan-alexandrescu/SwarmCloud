# The scorecard

`scripts/scorecard.sh` prints, per week, what agent work cost and how fast it
landed. It is **read-only**: it reads Firestore, the API and GitHub, and
writes nothing anywhere.

```
scripts/scorecard.sh                      # the last 7 days
scripts/scorecard.sh --weeks 4            # a four-week trend, one column per week
scripts/scorecard.sh --since 2026-11-01T00:00:00Z --until 2026-11-15T00:00:00Z
scripts/scorecard.sh --from-file extract.jsonl --no-github --no-verdicts
scripts/scorecard.sh --json
```

## Why it exists

The owner's improvement plan of 2026-11-23 changes three things that each
trade money against time: cheaper model classes for reviews, a stricter
pre-publish gate, and a shorter merge-train wait. Each is to be judged on
measured numbers, before and after, and until this script nothing reported
them. A change that saves review cost but doubles NOT_YET rounds, or a
stricter gate that ends at its budget half the time, is visible only when
both sides of the trade are on one page.

## How it reads, and the rules it keeps

* **Task records** come from one structured query on the `tasks` collection
  (created_at in the window, ordered by created_at then document name, 300 a
  page, a field mask naming only the fields below), through `fs_request` in
  `scripts/lib/common.sh`, whose token reaches curl on stdin and never on a
  command line. `--from-file` reads a saved extract instead: JSON lines of the
  same plain records (`{"id": ..., "state": ..., "result_summary": {...}}`).
* **Review verdicts** are each review and re-review task's `verdict.json`,
  read through the API's tenant-checked, redacted `/artifacts/content` route
  and parsed by the rules of `swarm_api.verdictpublish.parse_verdict`. At most
  `--max-verdicts` (200) are read, newest first; the report says how many were
  sampled of how many.
* **GitHub** is read through `gh`, which spends the operator's hourly quota:
  at most `--max-gh-calls` (40) pages of 100, and the report prints how many
  calls were made. `--no-github` skips it.
* **Not read is never 0.** A source that failed, was switched off or had
  nothing in the week prints `not read: <why>`. A zero would read as a
  measurement nobody made.
* **No workspace is named.** Tenants are counted only as *main workspace*
  (a group tenant), *acceptance* (the acceptance suite's tenant,
  `SWARM_ACCEPTANCE_TENANT`, default `smoke`, or `--acceptance-tenant`) or
  *personal* (a person's own `u-` tenant). No tenant id, workflow id or
  comment text reaches the output, and everything printed goes through
  `redact`.
* **Agent tasks** are tasks whose runner profile runs an agent: the merge
  step, the verdict poster, the indexer, `await-input`, `fix-loop` and `mock`
  are not counted as agent tasks.

## Roles

A task's role is decided in one place, `role_for_step` in
`scripts/lib/scorecard.py`, from its `step_id`. The id is lower-cased and
split on anything that is not a letter or digit, so a lane prefix or a round
suffix (`re-review:2`) does not change it; the first match wins:
`conflict-fix`, `ci-fix`, `re-review`, `fix` (any `fix` part, so
`review-fix:1`), `review`, `planner` (`plan`, `planner`), `build` (`build`,
`implement`, `impl`), and `other` for anything else and for a task with no
step.

## The measures

Each week is seven days ending at `--until`; a task belongs to the week it was
created in, a workflow to the week it merged, a comment or run to the week it
was created.

| Measure | How | Why |
|---|---|---|
| agent tasks, by workspace | count of agent tasks | the volume every other number is divided by |
| succeeded % | SUCCEEDED of the agent tasks that ended | a cheaper setting that fails more is not cheaper |
| cost USD | `attempt_totals.cost_usd_total`, else the runner's `usage.total_cost_usd` | the money side of every trade |
| tokens | cache read, cache creation, output, input, summed from `result_summary.runner.usage` | cache reads dominate cost; a change that breaks caching shows here first |
| per merged PR | cost and tokens over the pull requests GitHub says merged in the week | the unit of delivered work; total cost alone rises with volume |
| cost share by role | each role's share of the week's cost | says where a cheaper model class would act (reviews) and where it would not |
| cost share by model | each `model`'s share | shows a model-class change taking effect |
| turns | median and p90 of `usage.num_turns` | long sessions cost superlinearly: each turn re-reads the context |
| context tokens per turn | (input + cache read + cache creation) / turns, median and p90 | the size of what each turn carries |
| tasks over 100 turns, their share of cost | share of tasks at 100 turns or more, and of the cost on tasks with a turn count | what a turn cap would act on |
| tool calls per attempt, by role | the sum of `result_summary.runner.metrics.tool_calls.families` (every `tool_use` block of the attempt's sessions, a subagent's included; absent when the stream was not read), median, p90 and p99 | what a per-role tool-call limit would act on, over the whole attempt |
| turns per attempt, by role | `usage.num_turns`, median, p90 and p99 | the same for turns |
| tool calls and turns per session, by role | a session stopped at the ceiling: its stop entry's `tool_calls` and `turn` (top-level calls only, as the ceiling counts them); the one session of a one-session attempt: the attempt's numbers; median, p90 and p99 | the number the per-role session limits are tuned from |
| sessions with no numbers of their own | the last session of an attempt that ran several: `metrics.sessions.count` less its stops. Its own numbers are not recorded, so it is counted, not estimated | how much of the per-session distribution is missing, and that it is the sessions that did not hit the ceiling |
| sessions ended at the ceiling %, by role | stops in `result_summary.runner.output.context_ceiling.stops` over `metrics.sessions.count`, for tasks with both; a ceiling record cut with the runner's output (8,000 characters) is not read, never "no stop" | how often today's limits end a session early, per role |
| fresh sessions per attempt, by role | `metrics.sessions.count` - 1 (a handoff's fresh session or a refused first call's restart), median, p90 and p99 | each fresh session re-reads a handoff; many per attempt is a lane looping |
| sub-agent share by role | from `result_summary.runner.usage` (see "Sub-agents" below): cost % is `subagent_cost_usd` over `total_cost_usd`; tokens % is the `model_usage` rows' tokens beyond the parent's own over all their tokens; tasks started is an at-least and an at-most count; per role and `all` | sub-agents were about 15% of cost in the 30 days to 2026-11-23; whether to cap them is decided on this number, per role |
| model time vs wall-clock | summed `usage.duration_api_ms` against summed `result_summary.duration_seconds` | time not spent in the model is tools, tests and waits: the gate and CI show here |
| queue wait | created → started, median and p90, for tasks outside a workflow | admission and capacity; a workflow step's created → started also includes waiting on its parents, so it is printed apart |
| workflows merged, submit → merged | first task's created_at to the merge, median and p90 | the end-to-end number the merge-train change is judged on |
| last review or fix → merged | the last review/fix/re-review/CI-fix/conflict-fix end to the merge | the merge train's own wait, without the agent work before it |
| merge steps refused % | `end_cause == merge_refused` of the merge steps that ended | a refused merge is a round trip someone pays for |
| train ejections, per merged PR, by cause | issue comments starting "Ejected from the merge train"; the cause is the failing checks the comment lists, else the reason in parentheses | ejections are the merge train's cost; the cause says which check to fix |
| review verdicts by model class | MERGE, NOT_YET, SKIPPED, unreadable per `model_class`, and NOT_YET % of MERGE + NOT_YET | a cheaper review class that says NOT_YET more often buys fix rounds |
| release runs, success %, duration | `release.yml` runs: completed, success of success/failure/timed out, run start → last update | the release side of the merge-train wait |
| acceptance pass % | the same for `accept.yml` | the deployed suite's health; a cancelled run decides nothing |
| PR CI duration | pull-request-event runs, run start → last update, median and p90 | what a merge waits on |
| pre-publish gate runs, ended at budget %, guards left to CI | from `result_summary.runner.output.prepublish` (the record `prepublish-checks.md` is written from): at budget when the gate or its second turn was skipped for budget, or a check timed out, was skipped for budget or left guards to CI | the stricter-gate change is judged on how often it runs out of time and what it hands back to CI |
| not published (reason recorded) | agent tasks with `result_summary.git.publish_reason` | work the agent did that never reached a pull request |

## The fields read

`state`, `runner_profile`, `model`, `model_class`, `tenant_id`, `workflow_id`,
`step_id`, `created_at`, `started_at`, `completed_at`, `attempt_count`,
`end_cause`, `attempt_totals.cost_usd_total`, `result_summary.runner.usage`,
`result_summary.runner.output.prepublish`,
`result_summary.runner.output.context_ceiling`,
`result_summary.runner.metrics.sessions`,
`result_summary.runner.metrics.tool_calls`, `result_summary.duration_seconds`,
`result_summary.git.publish_reason` and
`result_summary.git.pull_request.number` (which links a workflow to the pull
request GitHub reports merged; without GitHub, a merge step that succeeded
stands in for the merge time). Nothing else of a task is read: no prompt, no
input, no log.

The numbers are made by `scripts/lib/scorecard.py` (standard library only),
unit-tested offline in `tests/unit/scripts/test_scorecard.py`; the shell only
fetches.

## Sessions: what is and is not recorded

A task carries only its **last** attempt's `result_summary`, so "per attempt"
is per last attempt; an earlier attempt's sessions are not on the task. The
context ceiling (`apps/agent-worker/agent_worker/runners/ceiling.py`) records
a stopped session's own turns and tool calls in its stop entry, but the last
session of an attempt has no entry of its own: its numbers are the attempt's
less the stops', and the two count differently (the attempt's tool calls
include a subagent's; the ceiling's do not), so the scorecard does not
subtract them and reports the session as having no numbers of its own. A
task whose runner the ceiling does not watch carries no `metrics.sessions`
and adds nothing to the session measures. These numbers are what the
per-role session limits are to be tuned from.

## Sub-agents: what is and is not recorded

The worker writes three things into `result_summary.runner.usage`
(`apps/agent-worker/agent_worker/usage.py`, SUBAGENT-SPEND): the summary's own
token counts, which are the **parent** agent's alone; `model_usage`, every
model the session paid for with its counts and cost, sub-agents included; and
`subagent_cost_usd`, the share of `total_cost_usd` the parent's tokens do not
explain. The context ceiling counts a sub-agent's messages too
(`runners/ceiling.py`, `parent_tool_use_id`), but only into the attempt's
totals. Both figures also take in the small model the CLI runs for its own
housekeeping, which cannot be told apart from a sub-agent on that model, so
the shares are a slight overstatement.

No record counts sub-agent **launches**, so "tasks that started any" is two
bounds: **at most** the tasks with any token beyond the parent's (a
housekeeping call counts here too) and **at least** the tasks whose parent's
own model row holds tokens beyond the parent's (a sub-agent on the session's
model; one on another model is missed). A role with tasks in the week but no
`model_usage` on any of them is `not measured`, and a week with none at all is
`not measured` whole: never 0. No cap on sub-agents is enforced; one is to be
decided on this measure.
