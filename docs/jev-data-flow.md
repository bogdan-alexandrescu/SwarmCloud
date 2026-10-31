# Jev data flow: what a Jev call may carry, and the code and test that hold each rule

**Read 2026-10-18 at `9c5726f`** (main), for the security pass (#476) and
#1336; rule 4 and rule 6 updated the same day by lane JEV-SCRUB, which
threaded the agent profile's registered secrets into Jev's config and
amended §5.4 to say where the audit lands. Every `path::Symbol` below is checked by
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
  J1 in `contextpack.prepare` before the agent starts, J4 in the gate
  (`prepublish.Gate._advise`) after it has exited, J2/J3/J5 in
  `runners/cliagent._jev_post_steps` after it has exited. The agent's process
  tree does not exist yet or no longer runs while these calls are made.
* **The worker process** (`agent_worker.judge.attempt.setup`, inside
  `graphstage.prepare`) makes **no call**. It reads
  `workspace_settings/{tenant}` and the repository's registration, then
  writes `<attempt>/judge/config.json` (with the agent profile's registered
  secret names, never their values) and the ledger, or nothing when Jev is
  off.
* **Not the agent, today.** `register.register_jev_tool` (the agent's
  `jev_decide` MCP tool, §5.1's owner override) exists but no runner calls it
  (§5.6, "Not built here"). When it is wired, the tool process is the agent's
  child and runs the same client, sanitizer and ledger.
* **Never swarm-api.** swarm-api reads `plan-advisory.json` (the J2/J5
  result) and never holds a Jev client.

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
  attempt, copied to `<attempt>/private/jev-key` (0600) for the config to
  name. Any other attempt reads `no_key` and makes no call.

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
  process's `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY`. Beyond that the call takes
  the attempt's ordinary network egress: no Jev-specific egress rule exists
  in this repository, and this record does not establish which network path
  a deployed worker's traffic takes past the process.

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

**The redaction applied.** Before any send, the launcher
(`swarm-jev-gate.mjs` `guardedFetch` and `scrub`) parses the body and
replaces, in every string and every key at every depth, the workspace key
itself and the value of every secret the agent profile registered
(`secret_env_names`, read from the hook's own environment) with
`<redacted>`; a value shorter than 8 characters is not scrubbed, the same
line the judge's sanitizer draws. That is the redaction: rules 1 to 3 and
5 of the table above (path deny, credential scan, the redaction families,
never-sent keys)
are `JudgeClient`'s and do **not** apply to these hooks, see Known gaps.
`guardedFetch` also refuses any URL but TypeSafe's, and sends to the URL
the worker's config chose (TypeSafe, or the loopback shim) with the
config's key, whatever `JEV_BASE_URL` or `CLAUDE_PLUGIN_OPTION_*` the
environment holds; the launcher removes those and `JEV_BELAY_LOG`, so
neither tool writes a text log.

**Where the audit lands.** Each hook run appends one line of counts to
`private/jev-gates.counts` -- which hook, how many Jev calls, whether it
blocked, how many rules it flagged; never text. After the agent,
`apps/agent-worker/agent_worker/qualitygates.py::finish` sums them, removes
the key file, and `apps/agent-worker/agent_worker/lifecycle.py::Worker._record_quality_gates`
puts them in the attempt's summary under `quality_gates` and records one
RUNNING event, cause `quality_gate`, with the same counts when belay blocked
a stop or enforce flagged a rule. There is no per-call ledger row like rule
6's; TypeSafe's own records are the authority on what was sent.

**Egress.** The same path the jev browser tools' direct calls take: no host
allowlist exists for TypeSafe; `kubernetes/network-policies/allow-egress.yaml`
rule 4 (the internet minus every private range, TCP 443) applies to every
worker pod, and the forwarder's path (`allow-typesafe-fwd.yaml`) to a pod
labelled `swarm-typesafe: inject`. No new rule was needed.

Held by `tests/unit/worker/test_quality_gates.py` (switch, key, config,
hooks, launcher redaction and URL pin, counts and event) and
`tests/unit/worker/test_jev_data_flow.py::test_the_record_states_what_the_lane_hooks_send_and_where_their_audit_lands`.

## Known gaps

* **The lane hooks get only the registered-secret scrub.** jev-enforce sends
  an edited file's text whatever its path, so an edit to a `.env` or `*.pem`
  file the agent writes reaches TypeSafe with only the profile's registered
  secrets and the key replaced; rule 1a's path deny, rule 2's credential
  scan and rule 3's redaction families are `JudgeClient`'s and do not run
  in the launcher. Turning `quality_gates.jev` on accepts that; it is off
  by default.
* **Nothing sets `SWARM_JUDGE_INTERNAL_URL`** (context-quality.md §5.6). The
  scheduler sends `JEV_API_URL` to live-browser attempts only. So an
  opted-in workspace on the default Internal Jev backend reads
  `no_internal_url` and **makes no call**: today the rules above are held by
  tests, not exercised by traffic. Not fixed here.
* **Rule 5a is by key name.** An email address or an environment value
  pasted into free text (an issue body, an excerpt, the agent's `state`) is
  not removed unless rules 2-4 catch it.
* **Rule 5c holds for J3 by a check, and for J1 and J4 by construction**: their
  rows are read from the attempt's checkout, with no separate guard that would
  refuse a path leaving it.

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
