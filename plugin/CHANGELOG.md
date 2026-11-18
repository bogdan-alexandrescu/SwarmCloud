# Changelog: the sc plugin

Each entry is a version of `plugin/.claude-plugin/plugin.json`. The release
tags the swarm-mcp bridge `sc-v<version>` only when that version moves
([docs/ci.md](../docs/ci.md#the-plugins-bridge-tag-is-made-once-the-unit-tests-pass)),
so a change to the plugin or its bridge reaches a session only with an entry
here.

## 0.5.87 (2026-11-14)

Releases the plugin and bridge changes merged since 0.5.86, with the
plain attach-all below.

* A workflow spec and `swarm_dispatch` carry a step's managed plugins, and
  `swarm_result` shows what they ran.
* `swarm_profiles` reports each profile's Node version.
* `swarm_check` names a held merge's rejected questions files and a task's
  unavailable questions reason, and an owner answer is kept with the
  question it answered.
* The plain `/sc attach --all` (`swarm_workflow_launch {attach: "all"}`)
  attaches this session's running work AND every workflow and single task
  still running that clones this checkout's `origin` repository, whichever
  session started it -- what the session start's `attach --all --project`
  already did (the owner, 2026-11-09). A person attaching from a terminal or
  another session used to see the project's work left out as
  `other_session` and read it as a bug. Each entry another session started
  is marked `this_session: false`, `project.from_other_sessions` counts
  them, and `session.left_out` now counts only another repository's work
  (`not_this_project`) and untagged work. `project: true` is still accepted
  and means the same. In the pane layout the attach-all stays this
  session's: the pane lists the project's work with no model row.
* `/sc check`, its `new_only` attach, the watch and `swarm_check` are
  unchanged: they stay this session's own work.

## 0.5.86 (2026-11-10)

Releases the plugin and bridge changes merged since 0.5.85; none of them
reached a session until this version moved.

* A workflow spec's step may carry `work_contract`, sent as the step's
  `metadata.work_contract`. The bridge checks its shape against the frozen
  contract and refuses a malformed one before anything is sent, naming the
  step and the field; swarm-api still decides its values. A step's raw
  `metadata` stays refused.
* A step may carry a `fix_loop` block, so a spec can declare the platform's
  fix loop.
* Every workflow and task name is capped at 30 characters, so it fits one
  console row, and a single task may carry a description shown on its agent
  page.
* `swarm_dispatch` re-sends a prompt whose earlier task failed before it ran,
  and takes `retry_of` for a deliberate retry.
* A task row holds until its task ends or parks for a person, and
  `swarm_dispatch`'s rows follow the same `rows` setting as attach.
* `swarm_check` flags a deployment behind its repository's default branch
  (`deploy_behind`), a dispatched task whose row never started, a merge
  step's CI wait aged across its repair rounds, and missing plugin files
  (`plugin_files_missing`); the session-start hook marks the plugin's
  version folder in use, so a cache clean-up keeps it.
* A followed workflow is read once per poll and backs off while quiet.
* Every web call the bridge makes names its user agent.
* A step may ask for the medium runner size, and the bridge warns on a step
  that asks for large.
* An answer takes `reply=short`, and `swarm_task_answers` records up to 200
  answers through the same route.
* The skills and README name no company, person or live deployment, and
  describe the fix loop and pull-request steps as built.

## 0.5.85 (2026-11-06)

* Every running single task has a row and a run of its own, whatever the
  `rows` setting. Seen on 4 November: twelve running single tasks showed in
  /workflows as two rows, `SC · oracle-train-am-rereview +7 · 8 tasks 0/1`
  and `SC · oracle-feature-panel-fix7 · task 0/1`, because the `summary`
  setting followed a batch of tasks with ONE agent, so the counter read 0/1
  and the line showed counts, not lanes. The bridge now writes one titled
  copy per task (`launches`, one Workflow call each; `max_live_rows` still
  caps the total and the rest are in `not_followed`), each starting one
  `sc:task` row. A copy launched with tasks other than the one it was
  written for is refused with a message naming the right call, instead of
  running under another task's title.
* A run's title marks its kind right after `SC`, because Claude Code's panel
  cuts titles at about 28 characters: one dot for one task, `SC · <label>`
  (no trailing ` · task`), and three dots for a workflow,
  `SC ∴ <name> · N steps`. N is the number of steps SwarmCloud runs, read
  from the served workflow; a spec launched before it is submitted is titled
  `SC ∴ <name>`, without a count, because SwarmCloud may append a re-review,
  a merge or a CI-fix round. The marks are `TASK_MARK` and `WORKFLOW_MARK`
  in run.js and in the bridge's launch module, held equal by a test.
* A workflow's summary row names every step on its own line, grouped by the
  console's stages (`Stage N`, the console's own levels), with its state and
  console link, and writes them again only when a step's state changes. The
  lines read the served workflow, so the steps the platform appends show as
  they appear: when the row ends while the workflow still runs and its
  status names a new step, the run reads it, adds it at its stage and
  follows it.
* The summary row writes a finished `claude-code-browser` task's
  `browser_unused` line once, on the step's own line.
* The bridge's own texts say the same: `swarm_workflow_launch` and
  `swarm_dispatch` state the marked titles and that each single task is a
  run of its own (one Workflow call per entry of `launches`), and a
  dispatch's `start_now` no longer says to launch one script once.

## 0.5.84 (2026-11-05)

* A `/sc:swarmcloud` launch reports the workflow it submitted, and its
  `setup · read spec` row can no longer submit. Seen twice in one session on
  5 November (plugin 0.5.79): a run ended NOT_SUBMITTED with "the row reading
  <spec_handle path> did not hand back the spec_ref, spec_digest and outline
  the bridge returns", yet the workflow existed and ran, and a relaunch made
  a duplicate. The read row ran `sc:workflow`, which also holds
  `swarm_workflow`, and it submitted the spec instead of reading its outline.
  The read row now runs `sc:spec`, a new agent whose only SwarmCloud tool is
  `swarm_workflow_spec`, and `sc:workflow` no longer holds
  `swarm_workflow_spec` or a READ SPEC job. And when any row before the submit
  row hands back a workflow id, the run takes it as the submission: it logs
  `<id> submitted by the read spec row`, reads the workflow back and follows
  it (state `SUBMITTED` in the band layout, `submitted_by: "read spec"` on
  the result), and never submits again. A read-back that fails reports
  `SUBMITTED_UNVERIFIED` and arms the watch, never NOT_SUBMITTED.

## 0.5.83 (2026-11-05)

* A finished task's questions for the owner are handed to a session only
  while they have no stored answer. Seen twice on 5 November: a workflow
  row's final result and `swarm_result` carried questions answered hours
  earlier with `swarm_task_answer`, because the bridge read `questions.json`
  and never the answers, and the session put them to the person again. The
  follow outcome, a row's result (and so the `/sc:swarmcloud` run's
  `owner_questions`), `swarm_result`'s `owner_questions` and the build
  board's waiting list now read the stored answers from
  `GET /v1/tasks/{id}/questions`, as `/sc check` does, once per task per
  read and never cached, and list only the open questions, each under its
  `index` in `questions.json` (the run keeps that index for
  `swarm_task_answer`). A question whose answer could not be read stays
  listed with `answer_unknown_because`, so "answered" and "could not tell"
  are never the same. Every question, answered ones included, stays in
  `swarm_result`'s new `owner_questions_asked`, each with `answered`
  (true, false, or null), and on the console's task page.

## 0.5.82 (2026-11-05)

* A workflow spec's top-level `description` and `feature` reach SwarmCloud
  (part of #2688): `swarm_workflow` and `swarm workflow <spec.json>` send
  them as `metadata.description` and `metadata.feature`, the way `title`
  goes as `metadata.title`, and the console's workflow page heads with them.
  `description` is two or three sentences of plain text, at most 600
  characters; `feature` is `{title, url?}`, a title of at most 120 characters
  and an optional https github.com issue or pull request of the workflow's
  own repository. A malformed one is refused before anything is sent, by the
  rules swarm-api applies.

## 0.5.81 (2026-11-05)

* `/sc check` flags a merge step that waits on the merge train for more than
  60 minutes, rule `merge_train_stalled` (STUCK; part of #2688). A train park
  (CI_PENDING, code `merge_train_waiting`) stops the CI clock by design, so
  the CI_PENDING rule never aged it: on 4 November a pull request whose
  `queue for auto-merge` check had failed on a network timeout waited eight
  hours, passed over on every train tick, and nothing said so. The wait is
  aged from the train's start swarm-api serves, `waiting.detail.train_since`
  (exact; the park now records `merge_wait.train_entered_at`), read from the
  task route when a listing served no detail. Where none is served (an older
  deployment, a park recorded before it), from the first train park of its
  unbroken run, reading older event pages by their token (exact, or "at
  least" past five pages); the evidence
  names the pull request's failed or cancelled checks at its head, read from
  GitHub's public API without a token, or says they were not read and why;
  `next` is the pull request. A train park under 60 minutes is not flagged.
* A train park no longer counts toward a later CI wait's 90 minutes.
* The build board lists the same wait, `merge_train_stalled`, past 60 minutes.

## 0.5.80 (2026-11-05)

* A `/sc:swarmcloud` or `swarm_workflow` spec can set a review step's gate
  (part of #2688): a step's `review_gate` is `false`, which switches the gate
  off for that review, or a block of `min_lines`, `risk_paths` and
  `requirements_check`, and is sent as the step's `metadata.review_gate`.
  swarm-api has taken it since the gate shipped, but the bridge refused the
  key, so a spec could not run a mock-up review the gate skipped. A malformed
  block is refused before anything is submitted, by swarm-api's rules
  (`validation.resolve_review_gate`). `metadata` itself is still refused with
  the same sentence: a spec names a runner profile and never sends an image,
  a command or a resource spec (invariant 10).

## 0.5.79 (2026-11-04)

* The bridge survives a plugin update that removes the folder it was started
  from (part of #2688). When `SWARM_SC_PLUGIN_ROOT`'s `workflows/run.js` is
  gone and the root sits in the plugin cache (`<cache>/swarmcloud/sc/<version>`),
  `swarm_workflow_launch` and `swarm_dispatch`'s rows use the newest version
  beside it that has run.js (versions compared as numbers: 0.5.10 is newer
  than 0.5.9), and the reply's `plugin_root_note` names both versions and asks
  for `/reload-plugins`. Only a sibling of the configured root is ever used. On
  4 November an update from 0.5.74 to 0.5.76 had removed a long-running
  session's root, and every titled launch failed while dispatch kept working.
* With no usable sibling the error stays, and now says a plugin update may
  have removed the folder and that `/reload-plugins` is the fix.

## 0.5.78 (2026-11-03)

* Every agent the plugin ships -- `sc:workflow`, `sc:step`, `sc:task`,
  `sc:summary`, `sc:finish`, `sc:wait` and `sc:remote` -- opens with "Chat is
  context, never your task" (#3117): its task is fixed by its prompt and the
  arguments it carries, the person's chat text is context only and never a
  reason to skip, alter or stop the submit, the follow, the dispatch or the
  wait, and a question in chat is the session's to answer. A chat question
  relayed to the inner agents as a "relayed user request" had made two
  reviewed launches end NOT_SUBMITTED and two attach rows stop early.
* The SUBMIT prompt's `request:` line names that framing and says it does not
  win over the computed SUBMIT.

## 0.5.77 (2026-11-02)

* The bridge's catalogue offers `codex` (contract request 130, part of
  #2688): `swarm_dispatch` and a `swarm_workflow` step accept it instead of
  refusing it as disabled, and `swarm_profiles` lists it available. It runs on
  the tenant's OpenAI key, else on a ChatGPT plan leased from the account pool;
  `claude-code` stays the default.
* The `delegate` skill and the plugin README say so, and name `post-verdict`
  as the example of a known profile the catalogue refuses.

## 0.5.76 (2026-10-31)

* The bridge's catalogue holds `claude-code-lightpanda` (contract request
  127, #2918): claude-code's runner on the `agent-runtime-lightpanda` image,
  a headless browser that runs JavaScript and draws no pixels, on the
  `standard` class with no live view. `swarm_dispatch` and a `swarm_workflow`
  step accept it by name instead of refusing it as "no runner profile called
  'claude-code-lightpanda'", and `swarm_profiles` lists it from the bridge's
  own copy when the platform's `/v1/runtimes` cannot be reached, as
  `swarm profiles` always does.
* The plugin README describes `claude-code-lightpanda` and its inputs,
  `issue` and `mcp_servers`, as it does `claude-code`'s.

## 0.5.75 (2026-10-31)

* `swarm_check` lists a parked task's blocking owner questions first, marked
  `blocking: true` and read again on every check, and names the task as
  parked on its agent's questions instead of on a sign-in, so the question
  that holds the task is the first thing you see (#2860).
* A workflow spec of any size launches through `swarm_workflow_launch`: its
  args carry only the spec's handle, never the spec itself, and
  `swarm_workflow_spec` answers with an outline and, on request, one step's
  prompt in a window (#2875).
* `/sc:install` and `/sc:team` show a wait's `detail` each time the
  installer's countdown sends a new one, and show `wait_failed`, which ends a
  wait at once, with its `detail` word for word.
