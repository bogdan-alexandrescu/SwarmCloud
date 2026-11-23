### `inputs`

ONE `swarm_inputs` call. Report each item as `<workflow_id> · <step_id> ·
opened <opened_at> · expires <expires_at>`, newest first, and say that any
member of the tenant may answer any of them with `/sc answer <wf_id>
<step_id>`. An empty list is an answer: nothing waits for a person.
`truncated: true` means more wait than were listed.

### `answer <wf_id> <step_id> [file ...]`

ONE `swarm_workflow_input` call with `{workflow_id, step_id}`, and `files`
only when the developer named files to attach. The bridge shows the
developer the step's prompt, what it stages and every proposed file (name,
bytes, sha256) in this host's own form, and sends only what they type and
the files they tick. NEVER pass a verdict, findings, answers or text: the
tool takes none and refuses a call that carries one, because an answer an
agent wrote is not a person's. Report the reply as it came: who answered,
`via`, `files_sent`, and `files_not_sent` when the developer left the box
unticked. A refusal that names the console link means this host cannot ask
its human: give the link and stop; never offer to answer for them. A reply
that says the step was already answered is final (the first answer wins);
a correction is a new round in the workflow, not a second answer.

### A fix loop that waits for you

A workflow's `fix-loop` step (docs/design/fix-loop.md) runs review-fix
rounds until a review says MERGE. Its row, `/sc status` and the band's agent
note read `round k of N` (round k of N), then the half running (`fix
running`, `re-review running`), or `waiting for you: <phase>`, or `MERGE
after k rounds`. Each round is an ordinary agent, shown as its own step
`review-fix:<k>` or `re-review:<k>`. A loop with a round running holds
nothing and is never flagged.

When its rounds are spent without a MERGE (`fix_loop_exhausted`), a fix
changed nothing (`fix_changed_nothing`), a round failed or was refused
(`round_failed`, `round_refused`) or a verdict did not read
(`review_unreadable`), the loop parks HUMAN_REQUIRED for a person. `check`
flags it STUCK with that phase, the round counter and the last verdict's
finding count; the band counts it with what waits on you; and its row ends
with the command. Show the developer the phase and the counter, then ask
which they want -- never pick for them:

* add a round: ONE `swarm_fix_loop {workflow_id, action: "add_round"}`, with
  `rounds` when they ask for more than one and `note` (at most 4 KiB) only
  with guidance THEY wrote for the next fix. Any member may; a loop never
  runs more than 10 rounds in all.
* stop: ONE `swarm_fix_loop {workflow_id, action: "stop"}`. The loop ends
  FAILED `fix_loop_exhausted`, and a merge step behind it refuses
  `verdict_not_merge`, leaving the pull request open.

`swarm_fix_loop` CHANGES STATE: call it only on the developer's word. Never
`human-ready` a loop (the API refuses it). Left alone, the park expires after
7 days and the loop ends FAILED `fix_loop_exhausted`.

### Answers given here are recorded on the task

A task's agent, or a workflow step's, may ask the person questions (its
`questions.json`: a row's result's `questions`, a run's result's
`owner_questions`, `swarm_check`'s `owner_questions`, `swarm_result`'s
`owner_questions`). Whenever a run's result or a check carries
`owner_questions`, the session puts EACH one to the person in that turn,
before any other report, with AskUserQuestion -- the question, its options
as given, the recommended one marked -- and records the pick with
`swarm_task_answer`; it never answers one itself and never leaves them in a
result or a log line alone. When the person answers one of them in this session --
with AskUserQuestion or in chat -- record each answer on that question's task
with ONE `swarm_task_answer` call per question:
`{task_id, question_index, choice?, text?}`. `question_index` is the
question's 0-based place in the task's questions. `choice` is the task's own
option label, exactly as served, when the person picked one of them; `text`
is the person's words, or what they chose when it was none of the options.
The bridge adds `via: claude-code` and this session's id, so the console
shows the answer as given in Claude Code instead of leaving it unanswered.
Many answers at once (the person's decision to close finished tasks' questions as moot, or a lane's questions recorded at fold time): ONE `swarm_task_answers {entries: [{task_id, question_index, choice?, text?}]}` call (at most 200; short replies; each refused entry is its own line).

Only the person's own pick, verbatim, from a question this session asked
them; never an answer the agent chose itself, a recommended option they did
not pick, or a paraphrase of what they might want. A question nobody
answered is left unanswered. Report a refusal as it came: a 403 means the
caller is neither the task's submitter nor an admin (or is a viewer), and the
answer stays where it was given -- say so and give the task's console link.
