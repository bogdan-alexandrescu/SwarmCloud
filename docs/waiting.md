# Why an agent is waiting: the `waiting` object (#1782)

`GET /v1/tasks/{id}` serves `waiting` on a PARKED, READY or QUEUED agent. It
says why the agent waits, what ends the wait and when the scheduler next looks
at it. Every other state serves `null`. Built by
`apps/swarm-api/swarm_api/waiting_detail.py`.

## Why it exists

During the 2026-10-20/21 CI stall every merge step's page said "waiting for
CI · eligible since 19:46:25" and nothing else, whether its checks were
queued, failing or done. The park events already held the facts: the pull
request's head, each pending check by name, the wakes so far, whether the
attempt was refunded, and the next check time. `waiting` collects those facts
and adds one sentence that sums them up.

## The rules it keeps

* **Recorded facts only.** It reads the task document, its newest 100 events,
  the `waiting_for` pool reading (#362) and, for a dependency or children
  park, one batched read of the tenant's related agents. A fact that was not
  recorded is `null`. Nothing is inferred to fill it.
* **Nothing is fetched from GitHub or a provider at read time.** The CI detail
  is what the merge step recorded at its last check. `last_checked_at` is
  that check's time, not the time of the read.
* **Read-only and tenant-scoped** (invariant 9). `tasks_by_id` and
  `list_tasks` both drop another tenant's document, so a dependency id that
  names one serves `state: null`.
* **Masked.** Every string goes through the agent's own masker
  (`TaskMasking.text`), the same one `last_error` already goes through.
* **CREDENTIAL_MISSING serves names only.** These are the provider, each
  workspace-secret `consumer/slot` and each MCP server's name, taken from an
  allow-list. A recorded mapping is never copied whole, so a value, or any
  part of one, cannot be served.

## Shape

```
waiting = {
  reason,         # the ParkReason, or READY_CAPACITY, QUEUED
  summary,        # one sentence: "CI on PR #1779: 5 checks pending (checkov,
                  #   ci-gate, python (unit), ...); re-checked 2 times; next check 19:46Z"
  since,          # when this wait began, from the events (a merge step's CI clock:
                  #   merge_wait.first_parked_at); null when not recorded
  next_check_at,  # next_eligible_at for the parks where it is a check time; null otherwise
  resumes_when,   # plain words: what ends the wait
  detail: {...},  # per reason, below
  history: [{at, kind, text}],   # newest last, at most 20
  history_read,   # false when no events were read: an empty history is "not read"
  read_at,
}
```

`next_check_at` is null for AWAITING_INPUT, HUMAN_REQUIRED and
CHILDREN_INCOMPLETE. Their parks write the park instant into
`next_eligible_at`, and nothing happens at that time.

**Eligible but not resumed.** Sometimes a timed park's `next_eligible_at` has
passed and the agent is still PARKED. The summary then ends with "eligible
since HH:MMZ, still parked: ...", and the history ends with an `eligible`
entry. Both give the newest `blocked_by` entry that is not the park's own
copy, or "waiting for the scheduler's next pass" when there is none.

## Detail per reason

| reason | detail |
|---|---|
| CI_PENDING | `pull_request {number, url}`, taken from the park's `merge_wait.pull_request`, else from the signed `merge_target.number`. Then `head`, `code`, `pending`, `failed` (when recorded), `wakes`, `attempts_counted` (false when `attempt_refunded`; null when not recorded), `last_checked_at`, `wake_requested_at` |
| READY_CAPACITY | `waiting_for` as #362 serves it, then the binding `pool`, `pool_state`, `active`, `limit`, `units` and `last_pass` (`blocked_by`). `place_in_line` (GET `/v1/tasks/{id}` only): 1 + the READY agents that need the binding pool and come before this one in the scheduler's READY query (priority DESC, then created_at ASC), from ONE count aggregation that ends before the agent's own position; a listing never counts. `place_in_line_basis` names the `pool`, its `scope` (`tenant`: the agent's own tenant only; `shared`: across tenants, and then only the number is served, never which agents or tenants are ahead) and the `order`, and `excludes` what the count does not model: the aging bonus and cross-tenant round-robin of `scheduler/fairness.py` and ties on created_at, so it is the database's line, not the pass's exact order. Null with `place_in_line_unavailable` saying why when nothing was counted: a listing, no live pool reading, no binding pool, a limit below the agent's units, or a failed count (an index missing or still building names the index). A failed count never fails the GET |
| PROVIDER_QUOTA_EXHAUSTED, PROVIDER_COOLDOWN, PROVIDER_OUTAGE | `provider`, `account` (recorded `account_id`, else the latest attempt's), `provider_state`, `source`, `account_pool_reason`, `wait_seconds`, `until` |
| SCHEDULED_RETRY | `last_error` (masked), `attempt` (used), `next_attempt`, `max_attempts`, `next_try_at`, `cause` |
| DEPENDENCY_INCOMPLETE | `waiting_on [{task_id, step_id, state}]` (current states, one batched read), `states_read` |
| CHILDREN_INCOMPLETE | `children [{task_id, state}]`, `unfinished`, `total`, `states_read` |
| AWAITING_INPUT | `who_may_answer {tenant_id, text}`, `opened_at`, `expires_at`, `answer_command` (`/sc answer WF STEP`) |
| HUMAN_REQUIRED | `who_may_answer`, `expires_at` (null: no park records it), `answer_route` (`POST /v1/tasks/{id}/human-ready`), `cause` |
| HUMAN_REQUIRED, a merge held by owner questions (`park_phase: owner_questions`, #2313) | `park_phase`, `questions [{task_id, step_id, question_index, question}]` (each asking step's question; the answer that clears the last one returns the merge to READY on its own), `unreadable`, `who_may_answer {submitted_by, admin, text}` (the answer route's rule: the submitter or an admin). NO `answer_route`: `human-ready` would only re-run the merge into the same park, and the summary says "Waiting for answers to N owner questions before merging (step: n, ...)", never "sign in". A hold with an `unreadable` entry is the exception: the answer route fails closed on it, so `answer_route` (`human-ready`) is served and the summary says to press it once the questions are answered |
| HUMAN_REQUIRED, an agent's blocking owner questions (`park_phase: question_wait`, AU-7) | An answer, not a sign-in (#2859): `waiting_detail._question_wait` reads the wait the way the answer route reads it (`agent_questions.current_wait`, so a stored answer counts only for the attempt that asked) and serves `park_phase`, `questions [{question_index, question}]` (each blocking question still unanswered, its text redacted, on one line and cut to 80 characters), `blocking` (the question indices that block), `unanswered` (the blocking indices with no stored answer), `who_may_answer {submitted_by, admin, text}`, `answer_route` (`POST /v1/tasks/{id}/answers`), `questions_route` (`GET /v1/tasks/{id}/questions`), `console_section` (`#questions`) and `expires_at` (when it was asked + 72 hours, `QUESTION_WAIT_MAX_SECONDS`; null when no ask instant is readable). The summary counts the unanswered blocking questions and quotes the first: "Waiting for the owner's answer to N questions: “...”; answer on its console page (Questions) or in Claude Code with /sc check", never "sign in" (`/sc answer` is a workflow step's await-input form, not this). A record that is not a question wait's shape is served with empty lists and the same how-to. What the question wait itself carries is also served by `GET /v1/tasks/{id}/questions` (`agent_questions`), under `waiting`: `open`, `blocking`, `unanswered`, `asked_at`, `expires_at` and `issue_comment_url`. The owner answers on the agent's console page, from Claude Code (`swarm_task_answer`), or with `POST /v1/tasks/{id}/answers`; who may answer is the submitter or an admin (and, with the role switch on, a `member` or above). The answer that leaves no blocking question unanswered returns the agent to READY in the same transaction, so nobody presses `human-ready`; unanswered at `expires_at`, swarm-api dead-letters it with `last_error: "owner question unanswered"` (the scheduler's human-park sweep skips it). Every blocking question answered while it is still parked (a cancel was asked for, or its last attempt is used) says so, and the scheduler's sweeps end it |
| CREDENTIAL_MISSING | `provider`, `secret_names`, `account_pool`, `forge_connection`, `workspace_secrets [{consumer, slot}]`, `mcp_servers [{name, code}]` |
| BUDGET_EXHAUSTED | `budget` (null: no writer records it today) |
| MANUAL_PAUSE | `paused_by` (null unless recorded), `paused_at` (the park event's time), `error` |

**Why a question wait holds no capacity.** An agent that asks a blocking
question with `await-answers` does not sleep on its lease waiting for the
owner, who may take hours. Its worker checkpoints, parks the agent
PARKED(HUMAN_REQUIRED) with the `question_wait` record in the park's
`blocked_by`, releases the lease and exits (invariant 4). A parked agent
creates no infrastructure demand and counts against no pool (invariants 1
and 3), so a day of unanswered questions costs nothing and blocks no other
agent. The answer does not hand the old attempt back its slot: it returns
the agent to READY, and the next attempt is admitted all-or-nothing under a
new fencing generation (invariants 2 and 5), reading the stored answers
when it starts.

## Where it is served

* `GET /v1/tasks/{id}`: in full.
* `GET /v1/tasks` (both views): `waiting: {reason, summary}` on each row. The
  row is built from its own document and the page's single `waiting_for`
  pool read. No events or related agents are read per row, so a listing's
  CI summary comes from `metadata.merge_wait`.
* `GET /v1/workflows`, `GET /v1/workflows/{id}`: each step serves
  `waiting: {reason, summary}` off its agent, and so does each agent row the
  route serves.
