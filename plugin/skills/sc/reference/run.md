### `run <spec path|JSON>`

The one verb that writes: it spends the shared pool, a step at a time.

1. Load the spec. Given a path, read the file (relative to this session's
   directory) and parse it as JSON; given JSON text, parse that. Text that is
   neither is an error — say so and stop.
2. Say, before anything is submitted: "about to submit SwarmCloud workflow
   `<label or path>`: <n> steps (<step ids>) — they run remotely and spend the
   shared pool". Then run it; do not wait for a reply the developer did not
   ask to give.
3. Call `swarm_workflow_launch` with `{spec_path: "<its absolute path>"}`
   given a file under this session's checkout (the bridge refuses a spec
   file outside it; pass such a file's parsed object as `spec` instead), or
   `{spec: <the spec object>}` given JSON text, and launch
   the `script_path` it returns (see "Every run is titled after its
   workflow"). Run that script with the reply's `args` AS GIVEN: `{spec_handle,
   details, rows, titled_copy}` -- never the spec. `spec_handle` is the file
   the bridge wrote the spec to, `{path, digest}`; the run reads its outline
   with `swarm_workflow_spec` (the bridge checks the digest) and its submit
   row hands `swarm_workflow` that handle, never the spec text, so nothing is
   retyped (SC-LAUNCH-SPEC, archived issue 2034: a brief holding backticks around `${...}`
   came back altered and was refused) and a spec of any size launches
   (archived issue 2875: a 90 KB spec's reply was too long to read). Never drop
   `spec_handle`, never add `spec` beside it, never pass a bare path, and
   never the JSON as a string. `rows` is `summary` (the
   default), which follows the submitted workflow with ONE summary row, or
   `per-step`, a row per step in this one run (Claude Code queues the rows
   past its per-run limit; `/sc attach <wf_id> --per-step` after the run
   would duplicate them, so prefer `summary` for a large spec). A spec that
   changed on the way is refused, not submitted. A spec whose top-level
   `publish_to_pr` names an existing pull request (archived issue 2183: `41`, `"#41"`,
   `"owner/repo#41"` or its URL) pushes its lane onto that pull request's
   branch and opens none; it is sent with no `merge`, and a spec naming it
   beside a merge step or a `merge` other than `"off"` ends `NOT_SUBMITTED`
   with the API's reason. One task onto an existing pull request needs no
   spec: `swarm_dispatch` with `prompt`, `repo`, `strategy: "direct-pr"` and
   `publish_to_pr` (never beside `tasks`).
4. Report what the workflow returned: its `workflow_id`, its state, and any
   `error` verbatim — `NOT_SUBMITTED`, `SUBMISSION_UNKNOWN`,
   `SUBMITTED_UNVERIFIED` and `FOLLOW_REFUSED` each say what to do next.
   When its result carries `owner_questions` -- one entry per step or task
   whose agent asked the owner and has no answer yet, `{step_id or title,
   task_id, workflow_id, console, questions}`, each question `{index,
   question, options, recommended, context}` -- put EACH question to the
   person in that turn, BEFORE any other report, with AskUserQuestion: the
   question, its options as given, the `recommended` one marked
   "(recommended)". Then record each pick with `swarm_task_answer {task_id,
   question_index: <its index>, choice}`. Never pick an answer yourself, and
   never leave them only in the result or in the run's "asks the owner" log
   line. An entry with `questions_unread` was counted but not read: read its
   questions with `swarm_result` (its `owner_questions`) and ask those. The
   same holds for every run this skill launches -- `attach`, `attach --all`,
   a dispatch's rows -- whenever its result carries them.
5. Last, the mandatory final step, once a spec was submitted -- every outcome
   but `NOT_SUBMITTED`, `SUBMISSION_UNKNOWN` and `SUBMITTED_UNVERIFIED`
   included:
   [Arming the watch](#arming-the-watch), with `swarm_workflow_launch`'s
   `arm_watch` (`swarm_workflow`'s reply carries the same).
### A spec says what it delivers: `description` and `feature`

A workflow spec may carry, at its top level beside `title`, a `description`
-- two or three sentences of plain text saying what the work delivers, at
most 600 characters, no HTML or Markdown -- and a `feature`, `{title, url?}`:
the feature it belongs to, a title of at most 120 characters, and optionally
the https github.com issue or pull request of the workflow's OWN repository
that tracks it. The console's workflow page heads with both, so write them
for a person who opens the workflow cold. They are sent as
`metadata.description` and `metadata.feature`; a malformed one is refused
before anything is sent, and a longer description is refused, never cut.
`swarm_dispatch` takes the same optional `description` for one task and for
each entry of `tasks`, sent as `metadata.description` and shown on the
console's agent page. A workflow's `title` and `label` and a single task's
`label` are capped at 30 characters, so a name fits one row of the console: a
longer one is refused before anything is sent, and the detail belongs in
`description`.
A spec should send `about: {purpose, goals, output}` -- why the work exists
(one sentence, at most 300 characters), up to three goals (at most 120 each)
and what lands (one sentence, at most 300) -- sent as `metadata.about` and
checked the same way, each refusal naming the part.
### Runner size

Start a step at `claude-code` (the standard class). Name
`claude-code-medium` (6 CPU, 16 GiB) only for a step that was OOM-killed or
ran out of time there, and `claude-code-large` only for one that failed on
medium: 96% of large attempts fit medium (7 days to 6 November 2026). A
large step is still submitted; relay the answer's `warnings` line.

### Model classes

A step picks the model it runs by **class**, by name (archived issue 1435): `opus` (the
default of every Claude profile: coding and implementation), `fable`
(planning), `sonnet` (summarising, reformatting) or `haiku` (classifying,
extracting). A spec's step names it as `model_class`, beside
`runner_profile`; a step that names none runs `opus`, so implementation stays
on the default and only the mechanical steps need the field. The platform runs
the class's pinned model, which `swarm_profiles` lists under `models`. A
model ID in `model_class`, or a class on a profile that runs no Claude model,
is refused before anything is submitted; a 422 from the API
(`model_class_not_yet_available` until the deployment offers the classes) is
reported in its own words, with the offer of the same spec without the
field. Never choose a class the spec did not: it is the operator's decision,
made in the spec or on the command line.

A review step's gate is the step's `review_gate`: `false` switches it off,
so the review runs however small the change (a mock-up review, say), and a
block of `min_lines`, `risk_paths` and `requirements_check` sets it. It is
sent as the step's `metadata.review_gate`; a malformed one is refused before
anything is submitted. A step carrying `metadata` itself is refused, like
every key outside the accepted list. Never add or drop a gate the spec did
not name.

A step's work contract is the step's `work_contract`, beside its
`runner_profile`: the document swarm-api decodes, compiles and signs, and
that contract review runs on. It is sent as the step's
`metadata.work_contract`. Its shape is checked first against the frozen
contract (an object, `schema` 1, each level closed to the contract's own
keys, maps and lists where it has them); a malformed one is refused before
anything is submitted, with a message naming the step and the field.
swarm-api still checks every value -- the ids, the catalogues, the tenant,
the limits against the step's -- and its 422 is reported in its own words.
Never write, amend or drop a contract the spec did not carry.

A step's `plugins` names the plugins it runs, `{install_id: {overrides?}}` or `{name: {mode, skills}}`, sent as given.
A step's `plugins_off` lists the workspace's default plugins it does not load, by install id or name, sent as given.
