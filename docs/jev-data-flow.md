# Jev data flow: what a Jev call may carry, and the code and test that hold each rule

**Read 2026-10-18 at `9c5726f`** (main), for the security pass (#476) and
#1336; rule 4 and rule 6 updated the same day by lane JEV-SCRUB, which
threaded the agent profile's registered secrets into Jev's config and
amended §5.4 to say where the audit lands. "When a call may be made" added
2026-11-02 at `5117e12` by lane JEV-JUDGMENTS-RUN, which made the judgments
run (the decision API's address, the signed opt-in, the setup order, the
monthly cap and the per-agent count). Every `path::Symbol` below is checked by
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
`apps/agent-worker/agent_worker/judge/client.py::JudgeClient.decide`. Before
anything is sent, it:

1. validates the questions, and refuses (raises, sends nothing) a question
   whose own text is credential-shaped or holds a registered secret;
2. runs `apps/agent-worker/agent_worker/judge/sanitize.py::sanitize_state`
   over the whole `state`, which applies rules 1 to 5 to every string at
   every depth;
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
| **The per-agent count.** At setup the ledger starts with the calls and tokens of the agent's settled attempts (their `jev_usage` rows, this tenant's only), created as already settled so they are never charged twice: 8 calls and 120k tokens per agent, not per attempt. | `apps/agent-worker/agent_worker/judge/attempt.py::_carried`, `apps/agent-worker/agent_worker/judge/budget.py::FirestoreMonthlyLedger.task_spend`, `apps/agent-worker/agent_worker/judge/budget.py::Ledger.create` | `tests/unit/worker/test_jev_judgments_run.py::test_a_tasks_second_attempt_counts_the_first_attempts_calls` |
| **The monthly cap.** With `code_intelligence.jev_monthly_cap_tokens` set, setup reads the month's total (`jev_usage/<tenant>_<YYYY-MM>`, UTC); a call that would take the month past the cap is `skipped:budget_monthly`. | `apps/agent-worker/agent_worker/judge/attempt.py::_carried`, `apps/agent-worker/agent_worker/judge/budget.py::FirestoreMonthlyLedger.used`, `apps/agent-worker/agent_worker/judge/budget.py::Ledger.admit` | `tests/unit/worker/test_jev_judgments_run.py::test_a_month_over_its_cap_skips_with_budget_monthly_and_changes_nothing`, `tests/unit/worker/test_jev_judgments_run.py::test_with_no_cap_the_month_is_not_read` |
| **The settle.** After the agent, before any terminal write, the lifecycle settles the attempt: in ONE transaction, fenced by the generation through the control plane's own `_fenced_task` (invariant 5), the attempt's row `jev_usage/<tenant>_<task>_g<generation>` (`kind: attempt`, its calls and tokens) and the month's total are added to. A superseded attempt writes nothing; a failed settle is logged by type, left unsettled, and changes no output. | `apps/agent-worker/agent_worker/lifecycle.py::Worker._settle_jev`, `apps/agent-worker/agent_worker/judge/attempt.py::settle`, `apps/agent-worker/agent_worker/judge/budget.py::settle_attempt`, `apps/agent-worker/agent_worker/judge/budget.py::FirestoreMonthlyLedger.settle_attempt` | `tests/unit/worker/test_jev_judgments_run.py::test_settle_writes_one_jev_usage_row_per_attempt_and_the_month`, `tests/unit/worker/test_jev_judgments_run.py::test_a_stale_generation_settles_nothing`, `tests/unit/worker/test_jev_judgments_run.py::test_the_lifecycle_settles_the_attempt_after_the_agent` |

## The rules

"Test" names a unit test that fails when the cited enforcement is removed.
Each was checked on 2026-10-18 by removing that enforcement in a scratch run
(a monkeypatched module, never a committed edit) and watching the test go red;
the mutations are listed under the table.

| § 5.4 | Rule | Enforced by | Test that fails when it is removed | Audit line |
|---|---|---|---|---|
| 1a | **Path deny**, in the client: no content from `.env*`, `*.pem`, `*.key`, `*.p12`, `*.tfvars`, `*.tfstate*`, anything under `secrets/` or `credentials/`, or the repository's `hard_stop_paths`. An object with such a `path`/`file` keeps its identity keys only (`i`, `kind`, `path`, `file`, `symbol`, `name`, `line`, `step_id`): the path name, never content. | `apps/agent-worker/agent_worker/judge/sanitize.py::is_denied_path`, `apps/agent-worker/agent_worker/judge/sanitize.py::DENIED_NAME_GLOBS`, `apps/agent-worker/agent_worker/judge/sanitize.py::DENIED_DIRECTORIES`, `apps/agent-worker/agent_worker/judge/sanitize.py::IDENTITY_KEYS`, `apps/agent-worker/agent_worker/judge/sanitize.py::_Walker.mapping` | `tests/unit/worker/test_judge_sanitize.py::test_a_denied_path_sends_its_name_only`, `tests/unit/worker/test_judge_sanitize.py::test_a_hard_stop_path_is_denied_like_a_secret_path`, `tests/unit/worker/test_judge_client.py::test_a_credential_in_the_state_never_reaches_jev`, `tests/unit/worker/test_jev_wiring.py::test_j1_drops_a_credential_shaped_excerpt_and_sends_names_only_for_denied_paths` | `denied` is counted on the returned `Decision`, not in the row; the row's `bytes` is the size of what was sent after the deny |
| 1b | **Path deny, J3 call site.** `diff_hunk` is a top-level string, so `review_post_step` sends `""` for a denied or hard-stop `file`; `code_window` sends `{path}` alone for a denied path. | `apps/agent-worker/agent_worker/judge/poststeps.py::review_post_step`, `apps/agent-worker/agent_worker/judge/poststeps.py::code_window` | `tests/unit/worker/test_jev_wiring.py::test_j3_never_sends_the_hunk_of_a_denied_path`, `tests/unit/worker/test_jev_wiring.py::test_j3_never_sends_the_hunk_of_a_hard_stop_path` | as 1a |
| 1c | **Path deny, J4 call site.** `changed` rows carry no `path`, so `jev_test_gap` blanks the hunk of a denied or hard-stop path, and `_test_excerpt` reads no denied test file. | `apps/agent-worker/agent_worker/prepublish.py::jev_test_gap`, `apps/agent-worker/agent_worker/prepublish.py::_test_excerpt` | `tests/unit/worker/test_jev_wiring.py::test_j4_sends_a_hard_stop_paths_symbols_by_name_only`, `tests/unit/worker/test_jev_wiring.py::test_j4_sends_a_denied_directorys_symbols_by_name_only` | as 1a |
| 2 | **Credential scan, then drop.** Every string (and every object key) goes through the publish gate's predicate `lifecycle._credential_in` at its strictest tier (the path given is not a test path). A hit removes the whole string from its object or list: **dropped, never masked**, so a candidate survives as path and symbol only. | `apps/agent-worker/agent_worker/judge/sanitize.py::is_flagged`, `apps/agent-worker/agent_worker/lifecycle.py::_credential_in`, `apps/agent-worker/agent_worker/judge/sanitize.py::_Walker.value` | `tests/unit/worker/test_jev_data_flow.py::test_the_credential_scan_alone_drops_a_headerless_key` (new: random key material no redaction rule knows), `tests/unit/worker/test_judge_sanitize.py::test_a_credential_shaped_excerpt_is_dropped_not_masked` | `dropped` on the `Decision`; nothing of the dropped string anywhere |
| 3 | **Redaction rules.** Every string also goes through `swarm_redaction.rules.redact` (`RULES`, the families `redact()` in `scripts/lib/common.sh` mirrors). Any match counts, **including one glued to a word**, which the publish predicate skips; the string is dropped, not the masked text sent. | `apps/agent-worker/agent_worker/judge/sanitize.py::is_flagged`, `apps/redaction/swarm_redaction/rules.py::redact` | `tests/unit/worker/test_jev_data_flow.py::test_the_redaction_rules_alone_drop_a_token_glued_to_a_word` (new) | as 2 |
| 4 | **The agent's registered secrets** (the values `agent_worker.redact.scrub_text` scrubs). **Enforced.** The code is stricter than scrubbing: it does not call `scrub_text` and send the scrubbed text; a string holding a registered value is dropped whole, like rule 2. The values come from three places: the backend's own key (always); the runner's credential variables and sensitive passthrough (`_jev_client`); and **the agent profile's `RunnerProfile.secrets`**, whose names the lifecycle hands to `graphstage.prepare`, which hands them to `judge.attempt.setup` and on to `register.prepare`, which writes them (names only) into `<attempt>/judge/config.json` as `secret_env_names`; `JevServer.from_config` (behind `judge.attempt.load` in the runner, and the `jev_decide` tool) reads their values from the process's environment. So a profile secret that is not credential-shaped, which rules 2 and 3 pass, is still dropped. | `apps/agent-worker/agent_worker/judge/sanitize.py::is_flagged`, `apps/agent-worker/agent_worker/judge/client.py::JudgeClient.decide`, `apps/agent-worker/agent_worker/lifecycle.py::Worker._graph_context_env`, `apps/agent-worker/agent_worker/graphstage.py::prepare`, `apps/agent-worker/agent_worker/judge/attempt.py::setup`, `apps/agent-worker/agent_worker/judge/register.py::prepare`, `apps/agent-worker/agent_worker/judge/attempt.py::load`, `apps/agent-worker/agent_worker/judge/mcp.py::JevServer.from_config`, `apps/agent-worker/agent_worker/runners/cliagent.py::_jev_client`, `apps/agent-worker/agent_worker/redact.py::scrub_text` | `tests/unit/worker/test_jev_profile_secrets.py::test_a_profile_secret_inside_a_j1_excerpt_is_dropped_before_the_jev_call` (new, JEV-SCRUB: a non-credential-shaped profile secret inside a J1 excerpt, through setup, load and rerank; red before the threading), `tests/unit/worker/test_jev_profile_secrets.py::test_without_the_names_the_same_excerpt_is_sent` (the control), `tests/unit/worker/test_jev_profile_secrets.py::test_graphstage_hands_the_profile_secret_names_to_jev_setup`, `tests/unit/worker/test_jev_profile_secrets.py::test_the_lifecycle_passes_the_task_profiles_secrets_to_graphstage`, `tests/unit/worker/test_judge_sanitize.py::test_a_registered_secret_drops_the_string_it_is_in`, `tests/unit/worker/test_jev_data_flow.py::test_the_runner_client_drops_the_runners_own_key`, `tests/unit/worker/test_judge_mcp.py::test_jev_decide_scrubs_the_tool_process_own_secrets` | as 2 |
| 5a | **Never sent: author identities and emails, environment variables, the agent's transcript.** The keys `author`, `authors`, `email`, `emails`, `user`, `login`, `env`, `environ`, `environment`, `transcript` are removed at any depth, in any case. | `apps/agent-worker/agent_worker/judge/sanitize.py::NEVER_SENT_KEYS`, `apps/agent-worker/agent_worker/judge/sanitize.py::_Walker.mapping` | `tests/unit/worker/test_jev_data_flow.py::test_every_never_sent_key_is_removed_at_any_depth_and_any_case` (new: one case per key, spelled out), `tests/unit/worker/test_judge_sanitize.py::test_environment_and_identity_keys_are_never_sent` | none |
| 5b | **Never sent: the in-flight view beyond titles and file paths.** J1 never sends an `in_flight` or `cannot_push` row; J5 sends each in-flight lane as `{i, title, files}` and nothing else. | `apps/agent-worker/agent_worker/contextpack/rank.py::rerank`, `apps/agent-worker/agent_worker/contextpack/retrieve.py::ALWAYS`, `apps/agent-worker/agent_worker/judge/shapes.py::j5_duplicate` | `tests/unit/worker/test_jev_wiring.py::test_j1_sends_the_top_sixty_and_never_in_flight_or_cannot_push`, `tests/unit/worker/test_jev_data_flow.py::test_j5_sends_an_in_flight_lane_as_title_and_files_only` (new) | none |
| 5c | **Never sent: another repository's content.** J3's code window is read only from a path that resolves inside the attempt's checkout; anything else is sent as `{path}`. J1's rows come from the attempt's own checkout and index (`retrieve.tracked_files`), and J4's tests from the gate's repository. | `apps/agent-worker/agent_worker/judge/poststeps.py::code_window`, `apps/agent-worker/agent_worker/judge/poststeps.py::_inside` | `tests/unit/worker/test_jev_data_flow.py::test_j3_sends_a_file_outside_the_checkout_by_path_only` (new) | none |
| 6 | **Audit, no payload.** Each call -- sent, failed, malformed or skipped -- appends one row to the attempt's ledger, never the state, the questions, the answer or the key. | `apps/agent-worker/agent_worker/judge/client.py::JudgeClient.decide`, `apps/agent-worker/agent_worker/judge/budget.py::Ledger.record`, `apps/agent-worker/agent_worker/judge/budget.py::MAX_AUDIT_ROWS`, `apps/agent-worker/agent_worker/judge/register.py::prepare` | `tests/unit/worker/test_jev_data_flow.py::test_the_audit_row_holds_counts_never_the_payload_the_answer_or_the_key` (new: success and failure, with a TypeSafe key), `tests/unit/worker/test_judge_client.py::test_a_good_answer_is_returned_and_counted` | the row itself: see below |

The mutations each test was proven against (2026-10-18, scratch monkeypatch
runs, nothing committed): `is_denied_path` returning `False` (1a: 16 tests
red); `DENIED_DIRECTORIES` emptied (1a, 1c: 3 red); `poststeps.is_denied_path`
returning `False` (1b: both J3 tests red); `_credential_in` returning `None`
(2); `_rules_redact` finding nothing (3); `is_flagged` ignoring the secrets it
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
| `input_tokens` | `usage.input_tokens` from the answer; `0` when there was none |
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
  (`judge.attempt.settle`).
* **Not the agent, today.** `register.register_jev_tool` (the agent's
  `jev_decide` MCP tool, §5.1's owner override) exists but no runner calls it
  (§5.6, "Not built here"). When it is wired, the tool process is the agent's
  child and runs the same client, sanitizer and ledger.
* **Never swarm-api.** swarm-api reads `plan-advisory.json` (the J2/J5
  result) and never holds a Jev client.

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

## Which key

Chosen by the workspace's judgment backend (`judge.settings.resolve_backend`):

* **Internal Jev** (the default) and **Internal Laya**: **no credential at
  all.** No `Authorization` header is sent, and `state` is sent
  JSON-stringified because the decision API takes a string.
* **TypeSafe**, on GKE with the forwarder on (`SWARM_TYPESAFE_INJECT`): the
  bearer is the worker's loopback shim's **local token**, read from
  `private/jev-typesafe.shim`. The workspace key never enters the pod; the
  forwarder adds it.
* **TypeSafe** otherwise: the workspace's key, read only from the
  `private/jev-typesafe.key` the worker already wrote for a live-browser
  attempt (since JEV-JUDGMENTS-RUN, before the judge's setup reads it),
  copied to `<attempt>/private/jev-key` (0600) for the config to name, or,
  under the signed workflow switch `jev_judgments: on`, the workspace's key
  resolved for the step. Any other attempt reads `no_key` and makes no call.

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
  a TypeSafe-backed live browser, quality gates on, or a `jev_judgments: on`
  step on the TypeSafe backend, whenever the pod is not labelled
  `swarm-typesafe: inject`.

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
neither tool writes a text log. The publish gate's own predicate
(`lifecycle._credential_in`, rule 2 of the table above) is not asked here:
it lives in the worker package, whose import would cost each hook seconds.

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
a stop or enforce flagged a rule. There is no per-call ledger row like rule
6's; TypeSafe's own records are the authority on what was sent.

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

* **The outbound filter's drop counts reach no summary yet.** They are in
  each line of `private/jev-gates.counts` and the browser's log, but
  `apps/agent-worker/agent_worker/qualitygates.py::finish` sums only calls,
  blocks and flags, and nothing hands the browser a `SWARM_JEV_BROWSER_COUNTS`
  file or the worker's `hard_stop_paths`; those are the worker's files, asked
  for in the lane's PR.
* **A Cloud Run Job judges on TypeSafe only.** The decision API's egress is
  a cluster Service, so a claude-code or codex attempt routed to Cloud Run
  gets no Internal URL and an internal backend reads `no_internal_url` there.
* **The state routes carry no Jev usage.** A tenant switched to them holds no
  Firestore client: with a monthly cap set its month counts as spent (every
  call `skipped:budget_monthly`), its earlier attempts are not carried, and
  nothing is settled.
* **Only an attempt that reaches the harvest is settled.** A park, a crash or
  a cancellation ends before `_settle_jev`, so that attempt's calls (at most
  J1's, which run before the agent) are neither charged to the month nor
  carried to the next attempt.
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
