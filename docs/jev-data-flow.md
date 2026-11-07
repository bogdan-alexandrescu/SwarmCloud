# Jev data flow: what a Jev call may carry, and the code and test that hold each rule

**Read 2026-10-18 at `9c5726f`** (main), for the security pass (#476) and
#1336; rule 4 and rule 6 updated the same day by lane JEV-SCRUB, which
threaded the agent profile's registered secrets into Jev's config and
amended §5.4 to say where the audit lands. "When a call may be made" added
2026-11-02 at `5117e12` by lane JEV-JUDGMENTS-RUN, which made the judgments
run (the decision API's address, the signed opt-in, the setup order, the
monthly cap and the per-agent count). "Jev spend on the state routes" added
2026-11-03 at `272a7fd`, from the code lane AQ-JEV-ACCOUNTING merged (#3111),
when the owner chose a doc follow-up for that lane. Rule 2 and "The order a
send goes through" reworded 2026-11-04, when the owner chose to correct the
wording after the credential check moved into `swarm_redaction.credentials`,
which every Jev call now runs. Every `path::Symbol` below is checked by
`tests/unit/worker/test_jev_data_flow.py`, which fails when a cited function,
name or test no longer exists. The rules are those of
[context-quality.md §5.4](design/context-quality.md#54-what-is-sent-and-what-never-is-even-when-opted-in);
the call sites are those of
[§5.6](design/context-quality.md#56-what-lane-ctxq-jw-built-j1-j5-in-the-pipeline).

## Why this record exists

Since PR 1330, `workspace_settings/{tenant}.code_intelligence.jev` decides
whether repository excerpts go to a ranking service outside the worker. The
owner made that switch owner/admin-only and audited on 2026-10-14 for exactly
that reason. §5.4 states the rules as design prose; a reviewer signing them
off needs to know which code makes each one true and which test goes red the
day someone deletes it. A rule with no failing test is a rule nobody will
notice losing.

## The order a send goes through

Every Jev call, from any call site, goes through
`apps/jev-client/swarm_jev/client.py::JudgeClient.decide`, the shared
package `swarm_jev` that `agent_worker.judge.client` and `.sanitize`
re-export (the same objects) and swarm-api installs. Its scan asks the
publish gate's credential check, `swarm_redaction.credentials`, by default,
so a call from swarm-api is scanned exactly as one from the worker (the
owner, 4 November 2026; before, the worker added the check when
`judge.sanitize` was imported, and swarm-api's calls ran without it).
Before anything is sent, it:

1. validates the questions, and refuses (raises, sends nothing) a question
   whose own text is credential-shaped or holds a registered secret;
2. runs `apps/agent-worker/agent_worker/judge/sanitize.py::sanitize_state`
   over the whole `state`, which applies rules 1 to 5 to every string at
   every depth, asking of each string, in this order, the registered
   secrets (rule 4), the credential check (rule 2) and the redaction rules
   (rule 3);
3. skips the call (`skipped:empty_state`) when nothing survived, and
   (`skipped:oversize`) when the state is past 70,000 characters;
4. reserves a slot in the attempt's ledger (8 calls, 120k input tokens);
5. sends, and writes one audit row (rule 6) whatever happens.

The client's sanitizer can only deny a path it can see: an object carrying a
`path` or `file` key. Two shapes carry content with no path beside it -- J3's
top-level `diff_hunk` and J4's `changed: [{symbol, hunk}]` -- so their call
sites withhold the hunk themselves. Those are rows 1b and 1c.

## When a call may be made

Lane JEV-JUDGMENTS-RUN (the owner chose on 2026-11-02 to build every
improvement proposal in CLD's Jev report). Before it, the rules below were
held by tests and exercised by no traffic: no attempt but a live browser's
had the decision API's address, the opt-in was read unsigned, and the budget
was per attempt with no monthly cap. Every refusal here is a skip or "off":
the attempt's outputs stay byte-identical to the local-only run (§5.6).

| Condition | What holds it | Test that fails when it is removed |
|---|---|---|
| **The signed opt-in.** `code_intelligence.jev` counts only when swarm-api's MAC over the `code_intelligence` block verifies (tenant-settings-integrity.md row 7). The worker holds no settings key, so the scheduler checks the block at dispatch and writes the verdict, `SWARM_JEV_JUDGMENTS` (`true`/`false`, and `SWARM_JEV_JUDGMENTS_REFUSED` with why), into every claude-code and codex job on both backends, as it does the quality gates'. The worker reads anything but `true` -- unsigned, mismatched, SETTINGS_MAC_REQUIRED off, no verdict at all -- as not opted in, and sets nothing up. | `apps/scheduler/scheduler/judgment.py::judgments_verdict`, `apps/scheduler/scheduler/dispatch.py::read_jev_judgments`, `apps/scheduler/scheduler/dispatch.py::jev_judgments_reading`, `apps/agent-worker/agent_worker/judge/attempt.py::unverified`, `apps/agent-worker/agent_worker/judge/attempt.py::setup` | `tests/unit/control_plane/test_judge_address.py::test_unsigned_or_mismatched_is_off`, `tests/unit/control_plane/test_judge_address.py::test_build_router_wires_the_reader_into_both_dispatchers`, `tests/unit/control_plane/test_judge_address.py::test_the_cloud_run_job_carries_the_verdict`, `tests/unit/worker/test_jev_judgments_run.py::test_unsigned_settings_turn_judgments_off` |
| **The decision API's address.** A claude-code or codex pod on GKE whose signed opt-in is on, or whose signed dispatch block says `jev_judgments: on` (never `off`), on `internal-jev` or `internal-laya` with the tailnet egress on, gets `SWARM_JUDGE_INTERNAL_URL` (the backend's `/v1/decide` by the tailnet name), the `swarm-judge: internal` label `kubernetes/network-policies/allow-judge-egress.yaml` selects, and the hostAliases entry: from `jev_dispatch`, the live browser's code path. A Cloud Run Job cannot reach the egress Service and gets no URL: there an internal backend still reads `no_internal_url`. | `apps/scheduler/scheduler/judgment.py::judge_dispatch`, `apps/scheduler/scheduler/dispatch.py::judges_internally`, `apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._with_judge_address`, `apps/agent-worker/agent_worker/judge/settings.py::resolve_backend` | `tests/unit/control_plane/test_judge_address.py::test_an_opted_in_internal_backend_attempt_gets_the_address_and_the_label`, `tests/unit/control_plane/test_judge_address.py::test_no_address_and_no_label_otherwise`, `tests/unit/control_plane/test_judge_address.py::test_the_url_variable_is_the_worker_judges` |
| **The setup order.** `jevkey.prepare` writes `private/jev-typesafe.key` before the graph context runs `judge.attempt.setup`, which reads it (`_read_key`): before, a live-browser attempt on TypeSafe without the forwarder read `no_key`. | `apps/agent-worker/agent_worker/lifecycle.py::Worker._build_child_env`, `apps/agent-worker/agent_worker/judge/attempt.py::_read_key` | `tests/unit/worker/test_jev_judgments_run.py::test_the_typesafe_key_file_exists_before_the_judge_is_set_up` |
| **The per-agent count.** At setup the ledger starts with the calls and tokens of the agent's settled attempts (their `jev_usage` rows, this tenant's only), created as already settled so they are never charged twice: 8 calls and 120k tokens per agent, not per attempt. On the state routes swarm-api's reservation carries the agent's earlier attempts instead ("Jev spend on the state routes"). | `apps/agent-worker/agent_worker/judge/attempt.py::_carried`, `apps/agent-worker/agent_worker/judge/budget.py::FirestoreMonthlyLedger.task_spend`, `apps/agent-worker/agent_worker/judge/budget.py::Ledger.create` | `tests/unit/worker/test_jev_judgments_run.py::test_a_tasks_second_attempt_counts_the_first_attempts_calls` |
| **The monthly cap.** With `code_intelligence.jev_monthly_cap_tokens` set, setup reads the month's total (`jev_usage/<tenant>_<YYYY-MM>`, UTC); a call that would take the month past the cap is `skipped:budget_monthly`. That read is the direct (Firestore) path's; on the state routes the cap is held by swarm-api's reservation, against every other live reservation too. | `apps/agent-worker/agent_worker/judge/attempt.py::_carried`, `apps/agent-worker/agent_worker/judge/budget.py::FirestoreMonthlyLedger.used`, `apps/agent-worker/agent_worker/judge/budget.py::Ledger.admit` | `tests/unit/worker/test_jev_judgments_run.py::test_a_month_over_its_cap_skips_with_budget_monthly_and_changes_nothing`, `tests/unit/worker/test_jev_judgments_run.py::test_with_no_cap_the_month_is_not_read` |
| **The settle.** After the agent, before any terminal write, the lifecycle settles the attempt: in ONE transaction, fenced by the generation through the control plane's own `_fenced_task` (invariant 5), the attempt's row `jev_usage/<tenant>_<task>_g<generation>` (`kind: attempt`, its calls and tokens) and the month's total are added to. A superseded attempt writes nothing; a failed settle is logged by type, left unsettled, and changes no output. This is the direct (Firestore) path. On the state routes the same call (`judge.attempt.settle`, outcome not yet known) sends the ledger's records one per call, and the settlement waits for the attempt's exit ("Jev spend on the state routes"). | `apps/agent-worker/agent_worker/lifecycle.py::Worker._settle_jev`, `apps/agent-worker/agent_worker/judge/attempt.py::settle`, `apps/agent-worker/agent_worker/judge/budget.py::settle_attempt`, `apps/agent-worker/agent_worker/judge/budget.py::FirestoreMonthlyLedger.settle_attempt` | `tests/unit/worker/test_jev_judgments_run.py::test_settle_writes_one_jev_usage_row_per_attempt_and_the_month`, `tests/unit/worker/test_jev_judgments_run.py::test_a_stale_generation_settles_nothing`, `tests/unit/worker/test_jev_judgments_run.py::test_the_lifecycle_settles_the_attempt_after_the_agent` |

## Jev spend on the state routes

Lane AQ-JEV-ACCOUNTING (#3111, merged 2026-11-03). A tenant switched to the
state routes (`SWARM_STATE_VIA_API`) builds no Firestore client, so the direct
path above never ran for it: its calls were never settled, its earlier
attempts were not carried, and with a monthly cap set every call was
`skipped:budget_monthly`, because a month nobody read counts as spent. On
either path the cap was also held to the ledger, a file the agent's user can
edit, read once at start, so two attempts running at once could each spend
the same remainder. swarm-api is now the authority, through three routes
beside the others in `apps/swarm-api/swarm_api/routes/attemptstate.py::_POSTS`.
The worker reaches them only through its signed state call; the agent's own
process cannot sign one, so the worker sends the records after the agent.

| Step | What holds it | Test that fails when it is removed |
|---|---|---|
| **The reservation, at admission (`state/jev-reserve`).** `judge.attempt.setup` asks for it before writing the config. In ONE transaction, fenced by the generation through `AttemptState._fenced` (invariant 5), swarm-api reads the agent's earlier attempt rows (at most 256 generations back; an agent past that is granted nothing, because an unread spend is not a free one), the workspace's cap (`code_intelligence.jev_monthly_cap_tokens`, only as the signed block lets a reader act on it), the month's spend and every other live reservation. It grants the attempt at most what is left of the agent's 8 calls and 120k tokens, its tokens no more than what is left of the month after the other reservations, and nothing when that cannot pay for one call at the measured 2,850 tokens. The body is the tuple alone: the worker names no number, so editing the local ledger cannot raise the grant. Two attempts reserving at once are serialised by the transaction, so they cannot both take the last of the month. A replayed reserve answers the grant as first made. A reserve that fails is Jev off for the attempt, never an unbounded allowance. | `apps/swarm-api/swarm_api/jevaccount.py::JevAccount.reserve`, `apps/swarm-api/swarm_api/jevaccount.py::grant_for`, `apps/swarm-api/swarm_api/jevaccount.py::JevReserveBody`, `apps/swarm-api/swarm_api/jevaccount.py::MAX_GENERATIONS_READ`, `apps/swarm-api/swarm_api/attemptstate.py::AttemptState._fenced`, `apps/agent-worker/agent_worker/judge/account.py::reserve`, `apps/agent-worker/agent_worker/judge/attempt.py::setup` | `tests/unit/control_plane/test_jev_account.py::test_reserve_grants_the_task_allowance_bounded_by_the_month`, `tests/unit/control_plane/test_jev_account.py::test_reserve_takes_no_number_from_the_worker`, `tests/unit/control_plane/test_jev_account.py::test_two_attempts_cannot_both_reserve_the_last_of_the_month`, `tests/unit/control_plane/test_jev_account.py::test_the_cap_is_held_to_what_the_month_already_spent`, `tests/unit/control_plane/test_jev_account.py::test_a_replayed_reserve_grants_nothing_more`, `tests/unit/worker/test_jev_state_accounting.py::test_a_capped_tenant_on_the_state_routes_is_granted_its_month`, `tests/unit/worker/test_jev_state_accounting.py::test_two_concurrent_attempts_cannot_both_spend_the_last_of_the_allowance`, `tests/unit/worker/test_jev_state_accounting.py::test_a_failed_reserve_is_jev_off_never_unbounded` |
| **The grant binds the ledger.** The ledger is built with the grant as its authority: the file's limits can only narrow it, and the counts this process has seen are a floor the file cannot lower. A response that states no usable `usage.input_tokens` is charged what the call was admitted at, never below 2,850, and its row says `tokens_estimated`; `output_tokens` and `served_model` are recorded only when stated, unknown and never 0. | `apps/agent-worker/agent_worker/judge/budget.py::Grant`, `apps/agent-worker/agent_worker/judge/budget.py::Ledger`, `apps/agent-worker/agent_worker/judge/client.py::_usage` | `tests/unit/worker/test_jev_state_accounting.py::test_editing_the_local_ledger_cannot_raise_authority`, `tests/unit/worker/test_jev_state_accounting.py::test_an_invalid_jev_response_cannot_bypass_the_budget`, `tests/unit/worker/test_jev_state_accounting.py::test_a_valid_response_records_its_served_model_and_output_tokens` |
| **One record per call (`state/jev-usage`).** After the agent, `judge.attempt.settle` with no outcome sends every ledger row not yet settled, one record each, keyed by its `call_id`. In one transaction swarm-api writes the call's own document, adds to the attempt's row (`calls`, `input_tokens`, `by_kind`), to the month's total and takes the tokens off the reservation's remainder, renewing its expiry. A record already stored is answered `replayed` and charges nothing, so a resend after a lost answer, or a settle run again, is never a second charge. A `skipped:<reason>` record is an outcome, not a call. Spend past the grant is recorded, never refused, and flagged on the row as `over_grant`. A failure returns everything to the ledger's unsettled count. | `apps/swarm-api/swarm_api/jevaccount.py::JevAccount.record_call`, `apps/swarm-api/swarm_api/jevaccount.py::JevCallBody`, `apps/agent-worker/agent_worker/judge/account.py::call_record`, `apps/agent-worker/agent_worker/judge/account.py::settle`, `apps/agent-worker/agent_worker/judge/budget.py::Ledger.take_unsettled_attempt`, `apps/agent-worker/agent_worker/judge/budget.py::Ledger.restore_unsettled` | `tests/unit/control_plane/test_jev_account.py::test_usage_through_the_state_routes_is_recorded_and_charged_once`, `tests/unit/control_plane/test_jev_account.py::test_a_skip_is_an_outcome_not_a_call`, `tests/unit/control_plane/test_jev_account.py::test_spend_past_the_grant_is_recorded_and_flagged`, `tests/unit/worker/test_jev_state_accounting.py::test_settle_through_the_state_routes_records_one_record_per_call`, `tests/unit/worker/test_jev_state_accounting.py::test_a_replayed_settlement_does_not_charge_twice` |
| **The settlement, on every exit (`state/jev-settle`).** The cleanup's `settle_jev` step, on every exit path and before the workspace holding the ledger is destroyed, settles with how the attempt ended: the committed end read as `succeeded`, `failed` (a dead letter too), `parked`, `cancelled` or `retried`, and `crashed` when no end was committed (a crash, a fence, an exit before any end). The records not yet sent go first, then the settlement: the calls and tokens the ledger counted beyond its kept audit rows, charged to the row and the month under the kind `unattributed`, and the reservation's remainder released. Once: a second settle is `replayed`. So a park, a cancellation and a crash in the worker are charged, not only an attempt that reached the harvest. | `apps/agent-worker/agent_worker/lifecycle.py::Worker._settle_jev_at_exit`, `apps/agent-worker/agent_worker/judge/attempt.py::settle_at_exit`, `apps/agent-worker/agent_worker/judge/account.py::outcome_of`, `apps/agent-worker/agent_worker/judge/account.py::ENDS`, `apps/swarm-api/swarm_api/jevaccount.py::JevAccount.settle`, `apps/swarm-api/swarm_api/jevaccount.py::JevSettleBody` | `tests/unit/control_plane/test_jev_account.py::test_every_ending_settles_and_releases_the_reservation`, `tests/unit/worker/test_jev_state_accounting.py::test_the_park_cancel_and_crash_paths_settle`, `tests/unit/worker/test_jev_state_accounting.py::test_after_the_agent_records_go_and_the_exit_settles_with_the_committed_end`, `tests/unit/worker/test_jev_state_accounting.py::test_calls_beyond_the_kept_audit_settle_as_unattributed_at_the_exit`, `tests/unit/worker/test_jev_settle_at_exit.py::test_cleanup_settles_jev_with_the_committed_end_before_the_workspace_goes`, `tests/unit/worker/test_jev_settle_at_exit.py::test_cleanup_settles_nothing_without_jev`, `tests/unit/worker/test_jev_state_accounting.py::test_the_direct_path_is_not_settled_at_the_exit` |
| **An abandoned attempt.** A worker that dies before settling (the pod killed, the node lost) leaves its reservation held. The agent's next attempt's reserve releases it and settles the dead attempt's row as `abandoned`, with `unreported_calls` and `unreported_tokens` null: its spend beyond the records it sent is UNKNOWN, never 0. What it did record stays on its row and is carried into the agent's limits. A reservation no recorded call renews for 6 hours is released by any reserve, so a worker that is gone cannot starve the workspace's other attempts of the month. | `apps/swarm-api/swarm_api/jevaccount.py::JevAccount.reserve`, `apps/swarm-api/swarm_api/jevaccount.py::ABANDONED`, `apps/swarm-api/swarm_api/jevaccount.py::RESERVATION_TTL` | `tests/unit/control_plane/test_jev_account.py::test_a_crashed_attempt_is_reconciled_by_the_next_reserve` |
| **A superseded attempt's spend.** `state/jev-usage` and `state/jev-settle` are checked against the attempt's own document (`AttemptState._attempt_doc_ok`: 409 when it names another tuple), not fenced on the agent's document, because a cancelled or superseded attempt's spend is still spend. What they write is that attempt's own row, its own call documents, the month's total and its own entry in the reservations: never the agent's document, the lease or another attempt's reservation (invariant 5). The reserve, which grants new spend, is fenced. | `apps/swarm-api/swarm_api/attemptstate.py::AttemptState._attempt_doc_ok`, `apps/swarm-api/swarm_api/jevaccount.py::JevAccount.record_call`, `apps/swarm-api/swarm_api/jevaccount.py::JevAccount.settle` | `tests/unit/control_plane/test_jev_account.py::test_every_ending_settles_and_releases_the_reservation`, `tests/unit/control_plane/test_jev_account.py::test_another_tenants_worker_cannot_record` |
| **The quality hooks' and the browser's calls.** jev-belay, jev-enforce and the live browser call Jev themselves, through the image's launcher, whose `recordedSend` appends one record per call as it is answered (call id, source, kind, backend, requested and served model, input and output tokens, whether the tokens were estimated, outcome; never text) to `private/jev-gates.calls` (the gates' config `calls_file`) or `private/jev-browser.calls` (`SWARM_JEV_BROWSER_CALLS`). At exit, before the settlement, the worker sends each new line through the same `state/jev-usage` with source `quality_hook` or `browser`, whether or not the judgments are on, so the reservation the settlement releases has counted them. A file's sent offset is kept beside it (`.sent`), so a second exit sends only what is new and a lost answer leaves the offset where it was; a symlinked file, a half-written line or a line of another source is not read. These tools are the workspace's hooks, not the judgments' budget: their records are spend reported, never refused. | `apps/agent-worker/agent_worker/judge/account.py::record_tool_calls`, `apps/agent-worker/agent_worker/judge/account.py::TOOL_CALL_FILES`, `apps/agent-worker/agent_worker/qualitygates.py::CALLS_FILE_NAME`, `apps/agent-worker/agent_worker/runners/jev_launch.py::BROWSER_CALLS_FILE_NAME`, `apps/agent-worker/agent_worker/judge/attempt.py::settle_at_exit` | `tests/unit/worker/test_jev_tool_calls.py::test_jev_enforce_writes_one_record_per_call`, `tests/unit/worker/test_jev_tool_calls.py::test_the_browser_filter_records_each_call_with_its_stated_usage`, `tests/unit/worker/test_jev_tool_calls.py::test_a_browser_call_that_got_no_answer_is_recorded_failed_and_still_raises`, `tests/unit/worker/test_jev_tool_calls.py::test_record_tool_calls_sends_each_once_and_only_its_own_source`, `tests/unit/worker/test_jev_tool_calls.py::test_a_lost_answer_leaves_the_offset_so_the_records_go_again`, `tests/unit/worker/test_jev_tool_calls.py::test_a_half_written_line_waits_for_its_newline`, `tests/unit/worker/test_jev_tool_calls.py::test_a_symlinked_calls_file_is_not_read`, `tests/unit/worker/test_jev_tool_calls.py::test_the_tools_calls_are_recorded_at_exit_even_with_the_judgments_off`, `tests/unit/worker/test_jev_tool_calls.py::test_the_tools_records_count_against_the_reservation_before_it_is_released`, `tests/unit/worker/test_jev_settle_at_exit.py::test_cleanup_hands_the_tools_calls_over_even_with_the_judgments_off` |

**What a record carries.** The names and counts in `call_record` and
`JevCallBody`, each field held to a pattern (a call id, an identifier, a model
name, an outcome); a row that does not fit is not sent as a record and its
spend is settled as unrecorded instead. Never a payload, a question, an
answer or a key: none of rule 6's forbidden fields reaches swarm-api, and
another tenant's worker cannot record into this tenant's documents.

**The documents** (collection `jev_usage`, every id derived by swarm-api from
the attempt's tuple):

| Document | `kind` | What it holds |
|---|---|---|
| `<tenant>_<YYYY-MM>` | none | the month's `input_tokens`, as before (UTC) |
| `<tenant>_<task>_g<generation>` | `attempt` | as before (`calls`, `input_tokens`, `by_kind`, `settled_at`), plus `reservation` (the agent's carried spend, the grant, its reason, the cap and the month), `over_grant` when spend passed the grant, and `settlement` (the outcome, the unrecorded calls and tokens, the released tokens; or `abandoned` with its unreported spend null) |
| `<tenant>_<task>_g<generation>_c<call_id>` | `call` | one call: source (`judgment`, `quality_hook`, `browser`), its kind, backend, requested and served model, input and output tokens, `tokens_estimated`, outcome, `recorded_at` |
| `<tenant>_reservations` | `reservations` | `held`: each live reservation by `<task>_g<generation>`, with its remaining tokens and expiry |
| `<tenant>_<task>_g<generation>_reask` | `attempt` | the control plane's re-ask of the attempt's acceptance judgment (`jevaccount.ReaskLedger`): `calls`, `input_tokens`, `settled_at` and `by_kind` under kind `contract_judgment`; each call also has its `_c<call_id>` `call` document, source `contract_reask` |
| `<tenant>_contract_<contract_id>_r<revision>` | `contract_review` | swarm-api's review of a work contract at submission (`jevusage.record_contract_review`): `calls`, `input_tokens`, `settled_at` and `by_kind` under kind `contract_review` |

`swarm_api.jevusage` reads only `kind: attempt` and `kind: contract_review`
rows and the month documents, so the `call` and `reservations` kinds change no
total the usage page serves; the rows' `by_kind` now also carries the hooks'
and the browser's kinds, the re-ask's `contract_judgment` and
`unattributed`. Every row above adds to the same month document, so the
monthly cap holds all of them.

## The rules

"Test" names a unit test that fails when the cited enforcement is removed.
Each was checked on 2026-10-18 by removing that enforcement in a scratch run
(a monkeypatched module, never a committed edit) and watching the test go red;
the mutations are listed under the table.

| § 5.4 | Rule | Enforced by | Test that fails when it is removed | Audit line |
|---|---|---|---|---|
| 1a | **Path deny**, in the client: no content from `.env*`, `*.pem`, `*.key`, `*.p12`, `*.tfvars`, `*.tfstate*`, anything under `secrets/` or `credentials/`, or the repository's `hard_stop_paths`. An object with such a `path`/`file` keeps its identity keys only (`i`, `kind`, `path`, `file`, `symbol`, `name`, `line`, `step_id`): the path name, never content. | `apps/agent-worker/agent_worker/judge/sanitize.py::is_denied_path`, `apps/agent-worker/agent_worker/judge/sanitize.py::DENIED_NAME_GLOBS`, `apps/agent-worker/agent_worker/judge/sanitize.py::DENIED_DIRECTORIES`, `apps/agent-worker/agent_worker/judge/sanitize.py::IDENTITY_KEYS`, `apps/jev-client/swarm_jev/sanitize.py::_Walker.mapping` | `tests/unit/worker/test_judge_sanitize.py::test_a_denied_path_sends_its_name_only`, `tests/unit/worker/test_judge_sanitize.py::test_a_hard_stop_path_is_denied_like_a_secret_path`, `tests/unit/worker/test_judge_client.py::test_a_credential_in_the_state_never_reaches_jev`, `tests/unit/worker/test_jev_wiring.py::test_j1_drops_a_credential_shaped_excerpt_and_sends_names_only_for_denied_paths` | `denied` is counted on the returned `Decision`, not in the row; the row's `bytes` is the size of what was sent after the deny |
| 1b | **Path deny, J3 call site.** `diff_hunk` is a top-level string, so `review_post_step` sends `""` for a denied or hard-stop `file`; `code_window` sends `{path}` alone for a denied path. | `apps/agent-worker/agent_worker/judge/poststeps.py::review_post_step`, `apps/agent-worker/agent_worker/judge/poststeps.py::code_window` | `tests/unit/worker/test_jev_wiring.py::test_j3_never_sends_the_hunk_of_a_denied_path`, `tests/unit/worker/test_jev_wiring.py::test_j3_never_sends_the_hunk_of_a_hard_stop_path` | as 1a |
| 1c | **Path deny, J4 call site.** `changed` rows carry no `path`, so `jev_test_gap` blanks the hunk of a denied or hard-stop path, and `_test_excerpt` reads no denied test file. | `apps/agent-worker/agent_worker/prepublish.py::jev_test_gap`, `apps/agent-worker/agent_worker/prepublish.py::_test_excerpt` | `tests/unit/worker/test_jev_wiring.py::test_j4_sends_a_hard_stop_paths_symbols_by_name_only`, `tests/unit/worker/test_jev_wiring.py::test_j4_sends_a_denied_directorys_symbols_by_name_only` | as 1a |
| 2 | **Credential scan, then drop.** Every string (and every object key) goes through the publish gate's credential check, `swarm_redaction.credentials.is_credential` (the shared package's `credential_in`, which the worker's `lifecycle._credential_in` is), at its strictest tier (the path given is not a test path). Every Jev call runs it, in the worker and in swarm-api alike: `swarm_jev.sanitize` imports it, so no process has to add it. `is_flagged` asks it second, after the registered secrets (rule 4) and before the redaction rules (rule 3). A hit removes the whole string from its object or list: **dropped, never masked**, so a candidate survives as path and symbol only. | `apps/jev-client/swarm_jev/sanitize.py::is_flagged`, `apps/redaction/swarm_redaction/credentials.py::is_credential`, `apps/redaction/swarm_redaction/credentials.py::credential_in`, `apps/agent-worker/agent_worker/judge/sanitize.py::is_flagged`, `apps/agent-worker/agent_worker/lifecycle.py::_credential_in`, `apps/jev-client/swarm_jev/sanitize.py::_Walker.value` | `tests/unit/worker/test_jev_data_flow.py::test_the_credential_scan_alone_drops_a_headerless_key` (new: random key material no redaction rule knows), `tests/unit/worker/test_judge_sanitize.py::test_a_credential_shaped_excerpt_is_dropped_not_masked` | `dropped` on the `Decision`; nothing of the dropped string anywhere |
| 3 | **Redaction rules.** Every string also goes through `swarm_redaction.rules.redact` (`RULES`, the families `redact()` in `scripts/lib/common.sh` mirrors). Any match counts, **including one glued to a word**, which the publish predicate skips; the string is dropped, not the masked text sent. | `apps/agent-worker/agent_worker/judge/sanitize.py::is_flagged`, `apps/redaction/swarm_redaction/rules.py::redact` | `tests/unit/worker/test_jev_data_flow.py::test_the_redaction_rules_alone_drop_a_token_glued_to_a_word` (new) | as 2 |
| 4 | **The agent's registered secrets** (the values `agent_worker.redact.scrub_text` scrubs). **Enforced.** The code is stricter than scrubbing: it does not call `scrub_text` and send the scrubbed text; a string holding a registered value is dropped whole, like rule 2. The values come from three places: the backend's own key (always); the runner's credential variables and sensitive passthrough (`_jev_client`); and **the agent profile's `RunnerProfile.secrets`**, whose names the lifecycle hands to `graphstage.prepare`, which hands them to `judge.attempt.setup` and on to `register.prepare`, which writes them (names only) into `<attempt>/judge/config.json` as `secret_env_names`; `JevServer.from_config` (behind `judge.attempt.load` in the runner, and the `jev_decide` tool) reads their values from the process's environment. So a profile secret that is not credential-shaped, which rules 2 and 3 pass, is still dropped. | `apps/agent-worker/agent_worker/judge/sanitize.py::is_flagged`, `apps/jev-client/swarm_jev/client.py::JudgeClient.decide`, `apps/agent-worker/agent_worker/lifecycle.py::Worker._graph_context_env`, `apps/agent-worker/agent_worker/graphstage.py::prepare`, `apps/agent-worker/agent_worker/judge/attempt.py::setup`, `apps/agent-worker/agent_worker/judge/register.py::prepare`, `apps/agent-worker/agent_worker/judge/attempt.py::load`, `apps/agent-worker/agent_worker/judge/mcp.py::JevServer.from_config`, `apps/agent-worker/agent_worker/runners/cliagent.py::_jev_client`, `apps/agent-worker/agent_worker/redact.py::scrub_text` | `tests/unit/worker/test_jev_profile_secrets.py::test_a_profile_secret_inside_a_j1_excerpt_is_dropped_before_the_jev_call` (new, JEV-SCRUB: a non-credential-shaped profile secret inside a J1 excerpt, through setup, load and rerank; red before the threading), `tests/unit/worker/test_jev_profile_secrets.py::test_without_the_names_the_same_excerpt_is_sent` (the control), `tests/unit/worker/test_jev_profile_secrets.py::test_graphstage_hands_the_profile_secret_names_to_jev_setup`, `tests/unit/worker/test_jev_profile_secrets.py::test_the_lifecycle_passes_the_task_profiles_secrets_to_graphstage`, `tests/unit/worker/test_judge_sanitize.py::test_a_registered_secret_drops_the_string_it_is_in`, `tests/unit/worker/test_jev_data_flow.py::test_the_runner_client_drops_the_runners_own_key`, `tests/unit/worker/test_judge_mcp.py::test_jev_decide_scrubs_the_tool_process_own_secrets` | as 2 |
| 5a | **Never sent: author identities and emails, environment variables, the agent's transcript.** The keys `author`, `authors`, `email`, `emails`, `user`, `login`, `env`, `environ`, `environment`, `transcript` are removed at any depth, in any case. | `apps/agent-worker/agent_worker/judge/sanitize.py::NEVER_SENT_KEYS`, `apps/jev-client/swarm_jev/sanitize.py::_Walker.mapping` | `tests/unit/worker/test_jev_data_flow.py::test_every_never_sent_key_is_removed_at_any_depth_and_any_case` (new: one case per key, spelled out), `tests/unit/worker/test_judge_sanitize.py::test_environment_and_identity_keys_are_never_sent` | none |
| 5b | **Never sent: the in-flight view beyond titles and file paths.** J1 never sends an `in_flight` or `cannot_push` row; J5 sends each in-flight lane as `{i, title, files}` and nothing else. | `apps/agent-worker/agent_worker/contextpack/rank.py::rerank`, `apps/agent-worker/agent_worker/contextpack/retrieve.py::ALWAYS`, `apps/agent-worker/agent_worker/judge/shapes.py::j5_duplicate` | `tests/unit/worker/test_jev_wiring.py::test_j1_sends_the_top_sixty_and_never_in_flight_or_cannot_push`, `tests/unit/worker/test_jev_data_flow.py::test_j5_sends_an_in_flight_lane_as_title_and_files_only` (new) | none |
| 5c | **Never sent: another repository's content.** J3's code window is read only from a path that resolves inside the attempt's checkout; anything else is sent as `{path}`. J1's rows come from the attempt's own checkout and index (`retrieve.tracked_files`), and J4's tests from the gate's repository. | `apps/agent-worker/agent_worker/judge/poststeps.py::code_window`, `apps/agent-worker/agent_worker/judge/poststeps.py::_inside` | `tests/unit/worker/test_jev_data_flow.py::test_j3_sends_a_file_outside_the_checkout_by_path_only` (new) | none |
| 6 | **Audit, no payload.** Each call -- sent, failed, malformed or skipped -- appends one row to the attempt's ledger, never the state, the questions, the answer or the key. | `apps/jev-client/swarm_jev/client.py::JudgeClient.decide`, `apps/agent-worker/agent_worker/judge/budget.py::Ledger.record`, `apps/agent-worker/agent_worker/judge/budget.py::MAX_AUDIT_ROWS`, `apps/agent-worker/agent_worker/judge/register.py::prepare` | `tests/unit/worker/test_jev_data_flow.py::test_the_audit_row_holds_counts_never_the_payload_the_answer_or_the_key` (new: success and failure, with a TypeSafe key), `tests/unit/worker/test_judge_client.py::test_a_good_answer_is_returned_and_counted` | the row itself: see below |

The mutations each test was proven against (2026-10-18, scratch monkeypatch
runs, nothing committed): `is_denied_path` returning `False` (1a: 16 tests
red); `DENIED_DIRECTORIES` emptied (1a, 1c: 3 red); `poststeps.is_denied_path`
returning `False` (1b: both J3 tests red); `_credential_in` returning `None`
(2; since the move, the same mutation is `swarm_jev.sanitize.is_credential`
returning `False`); `_rules_redact` finding nothing (3); `is_flagged` ignoring the secrets it
is given (4: both sanitizer and MCP tests red); `cliagent.collect_secrets`
returning nothing (4, runner); `login` removed from `NEVER_SENT_KEYS` (5a);
`j5_duplicate` copying a lane's other fields (5b); `_inside` always true (5c);
`Ledger.record` given the state (6). Each turned its named test red and no
other new test. Rule 4's profile-secret tests (JEV-SCRUB) were proven the
other way round: they were committed before the threading existed and failed
there (`setup()` and `prepare()` took no `secret_env_names`, and the lifecycle
passed none), and pass with it; `test_without_the_names_the_same_excerpt_is_sent`
shows the passphrase they use does reach Jev when no name is registered.

## What each audit line records

One row per `decide`, written by `JudgeClient.decide` into
`<attempt>/judge/ledger.json` (`register.LEDGER_FILE`, mode 0600, under the
ledger's `flock`, at most 200 rows):

| field | what it is |
|---|---|
| `call_id` | 12 hex characters, random per call; the only thing tying a log line (`jev_unavailable`) to a row |
| `kind` | `J0`-`J5` for the runner's calls, `agent` for the `jev_decide` tool |
| `backend` | `internal-jev`, `internal-laya` or `typesafe`; named in §5.4 step 6 since JEV-SCRUB. A name, never a URL or key |
| `bytes` | the size of the request body actually sent, after rules 1-5; `0` for a skip |
| `input_tokens` | `usage.input_tokens` from the answer; `0` for a skip or a failed call; for an answered call (`ok` or `malformed`) that states no usable count, what the call was admitted at, never below 2,850 (`client._usage`) |
| `tokens_estimated` | `true` when `input_tokens` is that admitted estimate, not the answer's count |
| `output_tokens` | `usage.output_tokens`, only when the answer states it: absent means unknown, never 0 |
| `requested_model`, `served_model` | the model the backend was asked for, and the model the answer says served it (only when stated); names, never a payload |
| `ms` | wall time across the call and its retries |
| `outcome` | `ok`, `failed`, `malformed`, or `skipped:<reason>` (`empty_state`, `oversize`, `breaker_open`, `budget_calls`, `budget_tokens`, `budget_monthly`) |

**Where it lands.** In the ledger, as §5.4 step 6 now says (amended by
JEV-SCRUB; it used to say `context-pack.json`). `pack.json` carries only J1's
`jev_calls` and `jev_input_tokens`
(`apps/agent-worker/agent_worker/contextpack/build.py::_build`), and §5.6
explains why: a pack that changed whenever Jev failed would not be the
local-only pack. The ledger lives in the attempt directory, which the agent
can read and (through `jev_decide`, which runs as the agent's user) rewrite:
it is a cost control and a record, not tamper-evidence. The backend's own
records are the authority on what was sent.

## Where Jev runs

* **The runner process**, for J1-J5. `judge.attempt.load` builds the client
  from `SWARM_JUDGE_CONFIG` (a path, never a key) inside the runner:
  J0 and J1 in `contextpack.prepare` before the agent starts (J0 only for a
  step the local rules leave `implement`, and it sends the brief alone, as
  `{task: {brief}}`, which J1 already sends), J4 in the gate
  (`prepublish.Gate._advise`) after it has exited, J2/J3/J5 in
  `runners/cliagent._jev_post_steps` after it has exited (J3 for a review
  and for a re-review, `contextpack/tasktype.py::classify`). The agent's process
  tree does not exist yet or no longer runs while these calls are made.
* **The worker process** (`agent_worker.judge.attempt.setup`, inside
  `graphstage.prepare`) makes **no call**. It reads
  `workspace_settings/{tenant}`, the scheduler's verdict on it, the
  repository's registration and, when Jev is on, the agent's earlier
  attempts' `jev_usage` rows and the month's total (with a cap set), then
  writes `<attempt>/judge/config.json` (with the agent profile's registered
  secret names, never their values) and the ledger, or nothing when Jev is
  off. After the agent it settles the ledger into `jev_usage`
  (`judge.attempt.settle`). On the state routes it reads none of that
  itself: it asks swarm-api for the attempt's reservation at setup, sends
  one record per call after the agent, and settles at exit however the
  attempt ended (`judge.attempt.settle_at_exit`; "Jev spend on the state
  routes").
* **The agent's `jev_decide` tool, on claude-code steps** (§5.1's owner
  override; wired by lane AGENTS-USE-TOOLS on 2026-11-02, when the owner chose
  that agents must use the tools switched on for them). The claude-code runner
  registers `swarm-jev` only when the worker handed it `SWARM_JUDGE_CONFIG`,
  that is, only for a workspace opted in to Jev judgments under the signed
  setting and the step's `jev_judgments` switch; the entry is
  `register.server_entry` over the worker's own config, so the tool's calls
  go through the same `JudgeClient`, sanitizer (every rule above) and ledger
  as J1-J5 and count against the same per-agent and monthly budget. The entry
  carries Claude Code's `alwaysLoad`, so the tool is in the prompt from the
  session's start, and the prompt has one line saying when to ask it. The
  tool process is the agent's child, running as the agent's user: on the
  TypeSafe backend it reads the key from the file the config names
  (`private/jev-key`, or the shim's local token on GKE), exactly as the lane
  hooks reach theirs, the residual the owner accepted; the key is never in the
  MCP config, the prompt, a log or the summary. Its calls are counted in the
  attempt's `result_summary.runner.metrics.tool_calls` as `jev_decision`.
  Codex steps do not register it.
* **swarm-api, first: the work contract review at submission**
  (JC1, docs/design/agent-quality/jev-contract.md section 4; lane
  CONTRACT-REVIEW-SUBMIT, 4 November 2026).
  `apps/swarm-api/swarm_api/contractreview.py::ContractReviewer.review` asks
  Jev, inside the submission request, which of a work contract's requirements
  no worker could enforce, through the shared client
  (`apps/jev-client/swarm_jev/client.py::JudgeClient.decide`) and its
  sanitizer, with a 15 s deadline and one retry. It runs only while the
  platform's `SWARM_CONTRACT_REVIEW` is `report` or `enforce` (default
  `off`: no call) and the workspace's own signed setting
  `contract_review.enabled` is on -- not `code_intelligence.jev`, because no
  code is sent: the state is `contractjudge.review_state`, the contract's own
  requirement texts by index, its strictness, acceptance, artifact kinds and
  write scope, never the tenant, the owner, the contract id, the repository
  URL or a commit. A requirement whose text the sanitizer would drop
  (credential-shaped, or holding the backend's own key) is not asked about
  at all and is recorded `not_judged`
  (`apps/swarm-api/swarm_api/contractreview.py::unreadable`): Jev never
  answers for a text it did not read. What is kept is the step's signed
  dispatch block's `contract_review`: the digest judged, Jev's closed
  classes, reasons and probabilities by requirement id, the call id and the
  backend's name, never a requirement's text. The backend is the workspace's `judgment_backend`,
  reached through swarm-api's Direct VPC egress and the internal load
  balancer in front of the tailnet egress (`judgment.resolve_call_backend`),
  never TypeSafe in its place. Its calls are admitted under
  `code_intelligence.jev_monthly_cap_tokens` and charged to the month as
  kind `contract_review` (`jevusage.record_contract_review`). A call that
  fails, times out or is over the cap is recorded `not_run` and the contract
  is accepted on the mechanical compile alone. Every other swarm-api read of
  Jev's work stays a file: `plan-advisory.json` (the J2/J5 result).
* **swarm-api, second: the control plane's re-ask of a step's acceptance
  judgment** (JC2 from the control plane, jev-contract.md sections 5.4, 8.2
  and 9; the owner's answers of 3 November 2026).
  `apps/swarm-api/swarm_api/contractreask.py::ForgeJudge` asks Jev again,
  from the scheduled tick's bounded pass (`contractreask.run_pass`, one a
  minute, never a sleep in a request), when the worker's end-of-step
  judgment came back `not_run` or `cannot_judge`: at most three tries over
  30 minutes, then the owner's approval screen. JC2 sends code -- the
  published diff's hunks, read with the repository's read token -- so it
  runs only while the workspace's signed `code_intelligence.jev` is on, and
  through the same shared client and sanitizer, to the workspace's internal
  `judgment_backend` only (`judgment.resolve_call_backend`), never TypeSafe.
  Each call is admitted under `code_intelligence.jev_monthly_cap_tokens`
  after every live reservation and charged to the same month document,
  in a row of its own per attempt (`<attempt row>_reask`, `kind: attempt`)
  under kind `contract_judgment`, which the usage page shows as the
  control plane's re-ask (`jevaccount.ReaskLedger`); beside it, not in place
  of it, the submission review's kind `contract_review`. A call the cap
  refuses is a try without a verdict (`budget_monthly`).

**Which verdict files J3 reads** (owner, 2 November 2026). A review step's
verdict is not always one `verdict.json` with a `findings` list: the first
experiment batch's reviews wrote `code-verdict.json` and `ui-verdict.json`
with `blockers`, `majors` and `minors`, and no finding carried `p_real`.
`poststeps.review_post_step` therefore reads every declared output named
`*verdict.json` and every `*verdict.json` the step wrote, takes a finding from
any of the four lists (a string or an object, its text from `summary` or the
briefs' `problem`), and asks about blockers before majors and minors under the
same five-call cap, so it spends no more of the agent's eight calls than before.
Each finding is sent through `shapes.j3_finding` and the client's sanitizer
like any other; the answer goes onto it as `p_real` and nothing else in the
file changes, `verdict` least of all. The step's summary (`jev.review`) names
how many findings each file had and how many were scored, and names every file
it could not read, which is left byte for byte. One failed call still leaves
every file as the review wrote it.

**What a person can see of it (lane JEV-USAGE-PAGE, 2026-11-02).** The owner
chose that day to build a Jev usage and cost page, because no one could see
how many calls, tokens or dollars Jev used and its price was recorded
nowhere. swarm-api's `GET /v1/jev-usage` (`swarm_api.jevusage`, drawn by the
console's Capacity › Jev usage) sums the selected workspace's `jev_usage`
attempt rows per UTC day and kind over 30 days, with the month's total
against `code_intelligence.jev_monthly_cap_tokens`. It reads counts only --
calls, input and output tokens, and outcomes (`ok`, `failed`,
`skipped:<reason>`) -- so nothing this record forbids sending can reach the
page: every query filters on `tenant_id` and a row filed under another
tenant is dropped again in code (per-tenant isolation), and the task id, the
generation and any field not counted never leave swarm-api. Dollars come from
the platform setting `jev_token_rates` (USD per million tokens, in and out),
which has no default: until a platform admin sets it the page says "rate not
set" and prints no figure, never $0. The worker's settle writes each attempt
row's `settled_at` and its per-kind split (`by_kind: {kind: {calls,
input_tokens, output_tokens, outcomes}}`), summed from its ledger's audit
rows in the same fenced transaction
(`apps/agent-worker/agent_worker/judge/budget.py::split_by_kind`); a row
settled before that is shown as "not split by kind", on the day of its last
write, with its outcomes "not recorded" (null, never 0 ok and 0 failed), and
output tokens stay null where no record states them. On the state routes
each call also has its own record and the row's `by_kind` is added to call
by call, the quality gates' jev-belay and jev-enforce hooks and the Jev
browser included (their kinds beside J0-J5 and `agent`), and calls beyond
the kept audit appear as `unattributed` ("Jev spend on the state routes").
On the direct path the hooks and the browser still record nothing in
`jev_usage` (Known gaps).

## Which key

Chosen by the workspace's judgment backend (`judge.settings.resolve_backend`):

* **Internal Jev** (the default) and **Internal Laya**: **no credential at
  all.** No `Authorization` header is sent, and `state` is sent
  JSON-stringified because the decision API takes a string.
* **TypeSafe**, on GKE with the forwarder on (`SWARM_TYPESAFE_INJECT`): the
  bearer is the worker's loopback shim's **local token**, read from
  `private/jev-typesafe.shim`. The workspace key never enters the pod; the
  forwarder adds it.
* **TypeSafe** otherwise: the workspace's key, from the
  `private/jev-typesafe.key` the worker already wrote for a live-browser
  attempt (since JEV-JUDGMENTS-RUN, before the judge's setup reads it), or,
  on any other step whose judgments run -- the signed opt-in, or the signed
  workflow switch `jev_judgments: on` -- the WORKSPACE's key resolved for
  the step with no submitter, as the quality gates resolve theirs
  (`apps/agent-worker/agent_worker/jevkey.py::resolve_workspace`), copied to
  `<attempt>/private/jev-key` (0600) for the config to name. Until lane
  jev-on-coding-profiles (4 November 2026) the workspace's key was resolved
  only under the switch, so every claude-code and claude-code-large step on
  TypeSafe read `no_key` ("Closed by lane jev-on-coding-profiles"). An
  attempt with no key at all reads `no_key` and makes no call.

In every case the key is in no config, MCP entry, environment variable, log
line or audit row (`Backend.__repr__` omits it; rule 6's test checks the
ledger), and it is itself a registered secret for rule 4, so a state string
holding it is dropped.

## Which egress path

`apps/agent-worker/agent_worker/judge/client.py::urllib_transport`, one
`POST` per call, 10 s timeout, up to two retries:

* **The TypeSafe shim** (`http://127.0.0.1:<port>/v1/systemone`) is opened
  with a proxy-less opener (`client._opener`), so loopback never goes through
  a proxy the environment names. The shim forwards to the TypeSafe forwarder
  Service in the cluster, with the attempt's short-lived ticket; the fence
  (invariant 5) refuses a stale attempt there.
* **TypeSafe direct** (`https://api.typesafe.ai/v1/systemone`) and **the
  Internal URL** (`SWARM_JUDGE_INTERNAL_URL`, the decision API over the
  tailnet) are opened with `urllib.request.urlopen`, which honours the runner
  process's `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY`. The Internal URL is set
  by the scheduler on a GKE pod that judges internally ("When a call may be
  made"), with the `swarm-judge: internal` label that
  `kubernetes/network-policies/allow-judge-egress.yaml` opens the
  decision API's egress Service to, and the hostAliases entry that resolves
  the tailnet name to that Service. TypeSafe direct takes the pod's egress,
  below.

**The TypeSafe egress allowlist** (2026-11-02, the owner's choice to build
every proposal in CLD's Jev report; [the runbook](runbooks/typesafe-egress.md)).
Two GKE FQDNNetworkPolicies name `api.typesafe.ai`, and nothing else in
`kubernetes/` does:

* **The forwarder**, `kubernetes/network-policies/swarm-system-typesafe-fwd.yaml`:
  `swarm-typesafe-fwd`, the one process that holds workspaces' TypeSafe keys,
  is put under egress isolation and reaches DNS, the metadata server, and TCP
  443 to `api.typesafe.ai` and `secretmanager.googleapis.com` by name only.
* **A direct pod**, `kubernetes/network-policies/fqdn/allow-typesafe-direct.yaml`:
  TCP 443 to `api.typesafe.ai` for a tenant pod the dispatcher labelled
  `swarm-jev: typesafe-direct`
  (`apps/scheduler/scheduler/dispatch.py::TYPESAFE_DIRECT_POD_LABEL`, set by
  `apps/scheduler/scheduler/dispatch.py::with_typesafe_direct` and
  `apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._with_judgments_forwarder`):
  a TypeSafe-backed live browser, quality gates on, or a step on the
  TypeSafe backend whose judgments run (the signed opt-in, or
  `jev_judgments: on`; `apps/scheduler/scheduler/dispatch.py::jev_judgments_run`),
  whenever the pod is not labelled `swarm-typesafe: inject`.

A pod labelled `swarm-typesafe: inject` has no TypeSafe allow: it reaches
TypeSafe only through the forwarder, on TCP 8080 (`allow-typesafe-fwd.yaml`).
An unlabelled pod has none either. Held by
`tests/unit/scripts/test_typesafe_egress_allowlist.py` (every FQDN policy
under `kubernetes/` judged against an unlabelled, a forwarded and a direct
pod and the forwarder) and
`tests/unit/control_plane/test_dispatch_typesafe_direct_label.py::test_a_forwarded_pod_has_no_direct_label`.

**What the allowlist does not do**: deny. `allow-egress.yaml` rule 4 still
admits every tenant pod to the public internet on TCP 443, and policies are
allow-only unions, so an unlabelled pod's packets to `api.typesafe.ai` are
not dropped (Known gaps). Both files are applied by the owner by hand
(the runbook); no render applies them yet.

## The lane hooks

Lane JEV-LANES (BGD, 2026-10-31; #642: 25% of lane PRs were red on their
first CI run). A claude-code lane in a workspace whose owner or admin turned
`workspace_settings/{tenant}.quality_gates.jev` on (`swarm_api.qualitygates`,
audited like `code_intelligence.jev`, default off) runs two Claude Code
hooks, pinned in `images/agent-runtime-base/Dockerfile` (jev-belay vendored
at its v0.2.0 commit, jev-enforce from the npm registry by sha512):
**jev-belay** v0.2.0 (a Stop hook) and **jev-enforce** v1.1.1 (a Stop hook
and an Edit/Write hook, `JEV_ENFORCE_MODE=end`). They do **not** go through
`JudgeClient.decide`: they are third-party Node programs. Every hook command
is the image's launcher, `images/agent-runtime-base/jev-gates/bin/swarm-jev-gate.mjs`,
which runs the pinned tool in its own process after replacing `fetch`.

**Who decides, and where the key comes from.** The worker
(`apps/agent-worker/agent_worker/qualitygates.py::prepare`, called from
`apps/agent-worker/agent_worker/lifecycle.py::Worker._quality_gates_env` for
the claude-code runner only) reads the switch; anything but `True` is off,
and off means no key is read, no config is written, no hook is registered
and nothing is sent. On, it resolves the **workspace's** TypeSafe key
(`swarm-tenant-<tenant>-typesafe`, `jevkey.resolve` with no submitter, the
secret the jev browser tools read), writes it to `private/jev-gates.key`
(0600, never checkpointed), and hands the runner `SWARM_JEV_GATES_CONFIG`,
the PATH of a 0600 config naming that file, the counts file, the hooks' data
directory and the profile's registered secret NAMES. The key is in no
environment variable the CLI, its Bash tool or the settings file sees, no
event, no log line and no spec. With the TypeSafe forwarder on, no key is
read at all: the config names the worker's loopback shim file.

**What each hook sends** (one `POST https://api.typesafe.ai/v1/systemone`
per Jev call, `{model, state, questions}`):

| Hook | When it calls Jev | `state` | `questions` |
|---|---|---|---|
| jev-belay, Stop | only when the turn changed files and no test, build or lint run passed since: at most one call | the turn's request (the user's prompt), **the final reply**, the file-change count and the names of the checks run with passed/failed; no tool input, diff or file content | four fixed questions of its own |
| jev-enforce, Stop (`end` mode) | when the final reply or a recorded edit is at least 20 characters and an applicable rule exists | **the final reply** (up to 60,000 characters), then **each edited file's written text** with its path | one per applicable rule: **the rule's text**, from the repository's CLAUDE.md, `.claude/CLAUDE.md` and `CLAUDE.local.md` files |
| jev-enforce, first run in a checkout | once per rule set, cached in the data directory | the rule texts | what each rule constrains |
| jev-enforce, Edit/Write | **never**: in `end` mode it only records the edit's text in the data directory (`private/jev-gates-data`) for the Stop hook | -- | -- |

**The redaction applied: the outbound filter.** Lane
jev-outbound-redaction (owner decision 2026-11-02, CLD's Jev report). Before
any send, the launcher (`swarm-jev-gate.mjs` `guardedFetch`, which runs
`filterBody`) parses the body and applies the worker's rules to every string
and every key at every depth, a matching string **dropped, never masked**, as
`JudgeClient` does:

| # | Rule | Enforced by (`images/agent-runtime-base/jev-gates/bin/swarm-jev-gate.mjs`) | Test that fails when it is removed |
|---|---|---|---|
| G1 | **Path deny.** An object whose `path` or `file` is `.env*`, `*.pem`, `*.key`, `*.p12`, `*.tfvars`, `*.tfstate*`, under `secrets/` or `credentials/`, or one of the repository's hard-stop paths (the config's `hard_stop_paths`, tried repository-relative too) keeps its identity keys only. jev-enforce sends each edit as `{kind, file, text}`, so an edited `.env` goes as its name. The globs, directories and keys are `sanitize.py`'s, held equal. | `isDeniedPath`, `walk`, `DENIED_NAME_GLOBS` | `tests/unit/worker/test_jev_outbound_filter.py::test_an_edited_env_files_text_never_reaches_the_fake_jev_from_jev_enforce`, `tests/unit/worker/test_jev_outbound_filter.py::test_the_repositorys_hard_stop_paths_are_denied_when_the_config_names_them`, `tests/unit/worker/test_jev_outbound_filter.py::test_the_path_deny_and_never_sent_keys_are_the_workers`, `tests/unit/worker/test_jev_outbound_filter.py::test_the_launchers_path_deny_agrees_with_the_workers` |
| G2 | **The redaction rule families, then drop.** A string any rule of `swarm_redaction.rules` would mask is removed from its object or list. The rules are asked of the image's own module (`/opt/venv/bin/python -I`, one batched call per request), not ported: the key/value rule is built on conditional groups (`(?(name)...)`) that a JavaScript `RegExp` cannot spell, and a hand-written second definition would drift. If the rules cannot be asked, nothing is sent. | `credentialFlags`, `filterBody` | `tests/unit/control_plane/test_log_redaction.py::test_the_jev_outbound_filter_flags_exactly_what_the_rules_mask` (the parity fixture, every case), `tests/unit/worker/test_jev_outbound_filter.py::test_a_credential_in_an_edit_is_dropped_and_a_registered_secret_is_still_scrubbed`, `tests/unit/worker/test_jev_outbound_filter.py::test_without_its_redaction_python_the_filter_sends_nothing` |
| G3 | **The key and the registered secrets** (`secret_env_names`, read from the hook's own environment) are replaced with `<redacted>` (`scrub`), first; a value shorter than 8 characters is not scrubbed, the same line the judge's sanitizer draws. | `scrub`, `secretValues` | `tests/unit/worker/test_quality_gates.py::test_the_launcher_sends_only_to_typesafe_scrubbed_and_counted`, `tests/unit/worker/test_jev_outbound_filter.py::test_a_credential_in_an_edit_is_dropped_and_a_registered_secret_is_still_scrubbed` |
| G4 | **Never sent:** `author(s)`, `email(s)`, `user`, `login`, `env`, `environ`, `environment`, `transcript`, removed wherever they appear. | `walk`, `NEVER_SENT_KEYS` | `tests/unit/worker/test_jev_outbound_filter.py::test_the_filter_drops_never_sent_keys_and_credentials_at_every_depth` |
| G5 | **JSON text is filtered as its structure** (the browser's internal backend sends `state` as JSON text). | `walk` | `tests/unit/worker/test_jev_outbound_filter.py::test_a_state_sent_as_json_text_is_filtered_as_its_structure` |

`guardedFetch` also refuses any URL but TypeSafe's, and sends to the URL
the worker's config chose (TypeSafe, or the loopback shim) with the
config's key, whatever `JEV_BASE_URL` or `CLAUDE_PLUGIN_OPTION_*` the
environment holds; the launcher removes those and `JEV_BELAY_LOG`, so
neither tool writes a text log. The publish gate's credential check
(`swarm_redaction.credentials`, rule 2 of the table above) is not asked
here: the launcher's interpreter imports `swarm_redaction.rules` alone. When
this was written the check lived in the worker package, whose import would
cost each hook seconds; it has since moved into `swarm_redaction`, which the
image's interpreter holds, and the hooks have not been changed to ask it.

**The Jev browser takes the same filter.** `images/agent-runtime-browser/jev-browser/bin/jev-browser-mcp`
starts the server as `node --import` of the launcher's `installBrowserFilter`
(the launcher is installed in agent-runtime-base, which the browser image is
built FROM), so the global `fetch` upstream `src/jev.mjs` calls for every Jev
request is the filtered one: page text, element labels and any JSON-text
`state` go through G1 to G5, with the TypeSafe key it was given scrubbed. The
wrapper refuses to start when the launcher is missing, and always sets the
interpreter it asks (`SWARM_JEV_REDACTION_PYTHON`) itself. Held by
`tests/unit/worker/test_jev_outbound_filter.py::test_a_page_holding_a_github_token_never_reaches_the_fake_jev_from_the_browser`
and `tests/unit/worker/test_jev_browser_image.py::test_without_the_outbound_filter_the_wrapper_refuses_to_start`.

**Where the audit lands.** Each hook run appends one line of counts to
`private/jev-gates.counts` -- which hook, how many Jev calls, whether it
blocked, how many rules it flagged, and the outbound filter's `dropped`,
`denied` and `withheld` counts; never text. The browser writes one such line
per Jev call (`hook: "browser"`) to its stderr, which the CLI keeps as the
MCP server's log, and to `SWARM_JEV_BROWSER_COUNTS` when the runner names a
file. After the agent,
`apps/agent-worker/agent_worker/qualitygates.py::finish` sums them, removes
the key file, and `apps/agent-worker/agent_worker/lifecycle.py::Worker._record_quality_gates`
puts them in the attempt's summary under `quality_gates` and records one
RUNNING event, cause `quality_gate`, with the same counts when belay blocked
a stop or enforce flagged a rule. The launcher also appends one record per
Jev call, names and counts only, to `private/jev-gates.calls` (the browser's
to `private/jev-browser.calls`), which the worker sends to swarm-api's
`state/jev-usage` at exit on the state routes, so their spend is in the
attempt's account beside the judgments' ("Jev spend on the state routes").
TypeSafe's own records remain the authority on what was sent.

**Egress.** The same path the jev browser tools' direct calls take
(Which egress path): a gates-on pod without the forwarder is labelled
`swarm-jev: typesafe-direct`, which the TypeSafe allowlist's direct FQDN rule
selects; with the forwarder, the hooks use the loopback shim and the pod's
`swarm-typesafe: inject` path (`allow-typesafe-fwd.yaml`).
`kubernetes/network-policies/allow-egress.yaml` rule 4 (the internet minus
every private range, TCP 443) still applies to every worker pod.

Held by `tests/unit/worker/test_quality_gates.py` (switch, key, config,
hooks, launcher redaction and URL pin, counts and event) and
`tests/unit/worker/test_jev_data_flow.py::test_the_record_states_what_the_lane_hooks_send_and_where_their_audit_lands`.

## Known gaps

* **The filter's counts and hard-stop paths, and what is still open**
  (follow-up lane, owner 2026-11-02). `apps/agent-worker/agent_worker/qualitygates.py::finish`
  now sums the filter's `dropped`, `denied` and `withheld` from the hooks'
  lines and from the browser's `private/jev-browser.counts`, which
  `apps/agent-worker/agent_worker/runners/jev_launch.py::filter_env` names to
  the browser as `SWARM_JEV_BROWSER_COUNTS` with the registered secrets'
  names as `SWARM_JEV_SECRET_ENV_NAMES`, into the summary's `quality_gates`
  as `filter_dropped`, `filter_denied` and `filter_withheld`; and `prepare`
  writes the repository's `hard_stop_paths` into the gates' config
  (`apps/agent-worker/agent_worker/qualitygates.py::hard_stop_paths`, this
  tenant's registration only). Still open: the browser's counts reach the
  summary only on an attempt whose gates run, an internal-backend browser
  (started without `jev_launch.py`) gets neither variable. The lifecycle
  passes the clone's `repository_url` to `prepare`
  (`apps/agent-worker/agent_worker/lifecycle.py::Worker._quality_gates_env`). With the
  forwarder serving the tenant on the TypeSafe backend, a gated claude-code
  pod is labelled `swarm-typesafe: inject` and no `private/jev-gates.key` is
  written (`apps/scheduler/scheduler/dispatch.py::quality_gates_run`).
* **A Cloud Run Job judges on TypeSafe only.** The decision API's egress is
  a cluster Service, so a claude-code or codex attempt routed to Cloud Run
  gets no Internal URL and an internal backend reads `no_internal_url` there.
* **On the direct (Firestore) path, only an attempt that reaches the harvest
  is settled.** A park, a crash or a cancellation ends before `_settle_jev`,
  so that attempt's calls (at most J1's, which run before the agent) are
  neither charged to the month nor carried to the next attempt; and the
  quality hooks' and the browser's per-call records are sent only on the
  state routes (`judge.attempt.settle_at_exit` makes no call without a state
  client). The direct path also still holds the cap to the ledger read once
  at start, so two attempts running at once can each spend the month's last
  remainder. The state routes close all three ("Jev spend on the state
  routes"); the direct path is what a tenant uses until it is switched.
* **No test drives a superseded generation's record or settle.** The routes
  are checked against the attempt's own document and write only its own row,
  calls, reservation entry and the month, which is what the code does; the
  tests cover a cancelled agent and another tenant's worker, not a generation
  the agent has moved past.
* **The opt-in needs SETTINGS_MAC_REQUIRED on.** As for the quality gates,
  the scheduler verifies nothing while that switch is off, so every stored
  `code_intelligence.jev: true` reads as off (`settings_mac_off`) until lane
  ISO-SETTINGS-MAC's re-sign job has signed the documents and the switch is on.
* **Rule 5a is by key name.** An email address or an environment value
  pasted into free text (an issue body, an excerpt, the agent's `state`) is
  not removed unless rules 2-4 catch it.
* **Rule 5c holds for J3 by a check, and for J1 and J4 by construction**: their
  rows are read from the attempt's checkout, with no separate guard that would
  refuse a path leaving it.

* **The TypeSafe allowlist names, it does not deny.** An unlabelled tenant
  pod can still reach `api.typesafe.ai` through `allow-egress.yaml` rule 4:
  NetworkPolicies cannot subtract a host from an allow, and the host's
  addresses are shared Cloudflare ones no `except` may pin. What stops such a
  pod's call doing anything is that it holds no workspace key. Closing it (a
  hostname sinkhole on unlabelled pods, or the general internet behind a
  proxy that refuses the host) is an owner question, in
  [the runbook](runbooks/typesafe-egress.md).

### Closed by lane jev-on-coding-profiles (2026-11-04)

* **Only live-browser steps called Jev.** SwarmCloud's own `jev_usage`
  ledger for 3 November 15:26 to 4 November 19:51 UTC (Engineering) held
  377 calls, every one from a claude-code-browser step, all through
  TypeSafe; 902 of 1,165 attempts settled a reservation and made no call,
  580 of them on claude-code and 116 on claude-code-large. The owner chose
  that day to find why and fix it. Each candidate cause, checked:
  * *The client in the image*: not the cause. `images/agent-runtime-base`
    installs `swarm_jev` and `agent_worker`, and the browser image is built
    `FROM` it; all three profiles run `agent_worker.runners.claude_code`.
  * *The worker's key* (the cause on the worker): `judge.attempt.setup` read
    only the key file `jevkey.prepare` writes for a live browser, and asked
    for the workspace's key only under `jev_judgments: on`. A coding step
    under the signed opt-in alone read `no_key`, so it had no
    `judge/config.json`: no J0 or J1 in the context pack, no J3 on a review,
    no `jev_decide` tool. Its reservation was still made and settled empty,
    which is the 696. Now it resolves the workspace's key whenever its
    judgments run and it has neither the browser's key file nor the
    forwarder's shim (`apps/agent-worker/agent_worker/judge/attempt.py::setup`;
    `tests/unit/worker/test_jev_on_coding_profiles.py`).
  * *The pod's route* (the cause in the GKE render): the forwarder's
    `swarm-typesafe: inject` label and SWARM_TYPESAFE_INJECT=on, or the
    `swarm-jev: typesafe-direct` label, reached a non-browser pod only under
    the switch, never under the signed opt-in, so on a forwarder tenant the
    worker served no shim and the forwarder's ingress did not select the
    pod. Now `apps/scheduler/scheduler/dispatch.py::jev_judgments_run` (the
    signed reading or the switch, never `off`) decides it
    (`tests/unit/control_plane/test_jev_on_coding_profiles.py`).
  * *The opt-in read per profile*: not the cause. The scheduler's verdict
    (`read_jev_judgments`) is read for every profile that carries the Jev
    client, and the Internal backends' address already followed it
    (`judges_internally`).
  * *The decision API's egress and its ingress source ranges*: not the
    cause, and unchanged. Every call in the window went through TypeSafe;
    coding and browser pods share the cluster's pod range and policies, and
    `allow-egress.yaml` rule 4 admits TCP 443 for every tenant pod.

  The budget is unchanged: the coding step's calls go through the same
  ledger, the same per-agent 8 calls and 120k tokens, the same swarm-api
  reservation on the state routes and the same
  `code_intelligence.jev_monthly_cap_tokens`. Which steps call Jev now: on
  every claude-code, claude-code-large and claude-code-browser step of an
  opted-in workspace, J0 for an `implement` step, J1 before the agent, J3 on
  a review, J4 in the gate, J2/J5 after it and the agent's `jev_decide`
  tool; codex steps the same, without the tool. The effect is read in the
  ledger as `jev_usage` attempt rows' calls grouped by the agent's
  `runner_profile` after the release that carries this.

### Closed by lane AQ-JEV-ACCOUNTING (2026-11-03)

* **The state routes carried no Jev usage.** A tenant switched to them held
  no Firestore client: with a monthly cap set its month counted as spent
  (every call `skipped:budget_monthly`), its earlier attempts were not
  carried, and nothing was settled. swarm-api now reserves a bounded
  allowance at admission in one fenced transaction, records each call once
  and settles the attempt: "Jev spend on the state routes".
* **Only an attempt that reached the harvest was settled**, on the state
  routes: a park, a cancellation and a crash in the worker now settle at
  exit with their outcome, and a worker that died is settled `abandoned`,
  its unreported spend unknown, by the agent's next reserve.
* **The quality hooks and the Jev browser recorded nothing in `jev_usage`.**
  On the state routes each of their calls is now a record of its own.

### Closed by lane jev-outbound-redaction (2026-11-02)

* **The lane hooks got only the registered-secret scrub**, so jev-enforce
  could send an edited `.env` or `*.pem` file's text, and **the Jev browser
  sent page text and element labels with no SwarmCloud-side redaction.** Both
  launchers now run every request body through the outbound filter (G1 to G5
  under "The lane hooks").

### Closed by the Jev report's classifier proposals (2026-11-02)

The owner chose on 2026-11-02 to build every improvement proposal in CLD's
Jev report; two were in the classifier that picks a step's type.

* **A re-review got no J3 triage.** `tasktype.classify` typed any step that
  staged `verdict.json` a fix, and a re-review stages the review it re-reads,
  so `_jev_post_steps` never ran J3 over the verdict the re-review wrote. A
  step that stages the patch and the verdict AND owes `verdict.json` in its
  own `expected_outputs` (written from its signed `step_id`,
  `apps/agent-worker/agent_worker/expected_outputs.py::is_review_step`) is now
  a review; a fix, which owes no verdict, stays a fix. Held by
  `tests/unit/worker/test_jev_wiring.py::test_cli_runner_triages_a_re_review_s_own_verdict`
  and `tests/unit/worker/test_contextpack.py::test_a_fix_that_stages_a_verdict_stays_a_fix`.
  J3 sends what it sent for a review: no new data flow.
* **J0 was defined and never called.** `apps/agent-worker/agent_worker/judge/shapes.py::j0_classify` is now
  called by `apps/agent-worker/agent_worker/contextpack/tasktype.py::classify_with_jev`
  from `contextpack.prepare`, only where the local rules fall back to
  `implement` and the attempt has a Jev client. Its state is the brief J1
  already sends, through the same client, sanitizer and ledger (one audit row,
  `kind` J0, counted in the agent's eight calls). Held by
  `tests/unit/worker/test_jev_wiring.py::test_j0_types_an_ad_hoc_step_the_local_rules_left_implement`
  and `tests/unit/worker/test_jev_wiring.py::test_j0_failure_leaves_the_pack_byte_identical_to_local_only`.

### Closed by lane JEV-JUDGMENTS-RUN (2026-11-02)

* **Nothing set `SWARM_JUDGE_INTERNAL_URL`** (context-quality.md §5.6). The
  scheduler sent `JEV_API_URL` to live-browser attempts only, so an opted-in
  workspace on the default Internal Jev backend read `no_internal_url` and
  made no call. A judging claude-code or codex pod on GKE now gets the URL,
  the label and the hostAliases entry: "When a call may be made", second row.
* **The worker read `code_intelligence.jev` unsigned.** It now acts only on
  the scheduler's verdict: first row.
* **The judge's setup ran before `jevkey.prepare`**, so the key file it reads
  did not exist yet: third row.
* **The budget was per attempt and the monthly cap never enforced**:
  `budget.settle` and `FirestoreMonthlyLedger` had no production caller, and
  `attempt.setup` passed no `month_used_tokens` or `task_calls`. Rows four
  to six.

### Closed by lane JEV-SCRUB (2026-10-18)

* **Registered secrets were the runner's own, not the profile's.**
  `attempt.setup` called `register.prepare` without `secret_env_names`, so a
  profile secret that did not look like a credential could reach Jev. The
  lifecycle now passes `profile.secrets` to `graphstage.prepare`, which passes
  them to `attempt.setup` and `register.prepare`: rule 4's row above, held by
  `tests/unit/worker/test_jev_profile_secrets.py`.
* **§5.4 put the audit in `context-pack.json`.** §5.4 step 6 now names the
  ledger, the `backend` field, and why the pack carries only J1's totals
  (§5.6's byte-identical local-only pack): rule 6's row above.
