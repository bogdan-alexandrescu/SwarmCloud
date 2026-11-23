### `attach <wf_id>`

Call `swarm_workflow_launch` with `{attach: "<wf_id>"}`, then run the script it
returned with the args it returned — `{attach: <wf_id>}` plus the `title`,
`workspace` and `details` (each step row's details line) it chose, nothing else. It submits nothing: it reads the workflow once, reports
its finished steps once, starts a live row for every unfinished step, and ends
with the workflow's state. This is how a session that restarted gets its rows
back for a workflow that kept running in SwarmCloud. An unknown or
other-tenant id ends with the API's error; report it verbatim.
ONE ROW OR ONE PER STEP (the plugin's `rows` setting, owner 2026-10-21):
at its default, `summary`, the run follows the whole workflow with ONE
`[SwarmCloud] <name> · N steps` row whose line -- `18 steps: 6 running · 9
queued · 3 done · 0 failed · PR #N` and the console link -- changes only when
the counts do. Under it the row names EVERY step the platform runs on a line
of its own, grouped by the console's stages (`Stage 1`, `Stage 2`... in the
console's order and names), each with its state and console link, written
again only when a step's state changes; the steps SwarmCloud appends
(re-review, merge, CI-fix and conflict-fix rounds) join them as they appear.
A step that fails, waits for a person or asks the owner questions is also
named on its own line. It is the default because it is one agent
instead of N, so it never meets Claude Code's limit of min(16, CPU cores - 2)
agents per run. `per-step` is a row per unfinished step; past that limit the
reply's `launches` holds several copies, `SC ∴ <name> · N steps · rows 1-8`,
`rows 9-16`..., each a run of its own: launch EVERY entry of `launches`, each
with its own args. `/sc attach <wf_id> --per-step` (pass `{attach: "<wf_id>",
rows: "per-step"}`) uses per-step for this one attach, and `--summary` (`rows:
"summary"`) the summary row; `rows` outranks the setting. `attach` is the
workflow id alone: the bridge refuses a flag inside it, so the mode always
goes in `rows`.
By hand it attaches ANY workflow your tenant may read, this session's or
not: when the reply carries `not_this_session` (`this_session: false`), say
in one line that the workflow is not this session's -- another Claude Code
session started it, or it carries no session tag -- and that `check` leaves
it out, and `attach --all` too unless it clones this project's repository. It
is attached all the same.
Last, the mandatory final step: [Arming the watch](#arming-the-watch), with
the reply's `arm_watch` -- also when the workflow has already finished.

### `workflows`

ONE read of the caller's tenant's workflows that are not finished — every
one whose state is not `SUCCEEDED`, `FAILED`, `CANCELLED` or `DEAD_LETTERED`,
closest to finish first. Call `swarm_workflows` once (the sc plugin's own read tool; it
works outside a checkout); in a terminal `sc workflows` prints the
same list, closest to finish first (steps done of the total, then the
current step's age) and grouped by workspace. Report each workflow on
one line that STARTS with its workspace, `[<workspace>] <id> <label>
<state>` (the entry's `workspace`), then its current steps with
their states, its age and its console link, and each current step's console
link (a parked or queued one too), all as served — never build a link.
`complete: false` means the list stopped paging: say so. Its `single_tasks`
are YOUR running tasks in no workflow (sent with `swarm_dispatch`): report
each one's workspace first, then its id, label, state, age and console link
the same way, and
`single_tasks_error` verbatim when they could not be read. None running is an
answer, not an error. This list is the WHOLE tenant: each entry's
`this_session` (`[this session]` in `sc workflows`) marks what this Claude
Code session started; say which. Never poll; offer `/sc attach --all` when
any of this session's run, and `/sc attach <wf_id>` for another one.

### `attach --all`

THIS CLAUDE CODE SESSION'S WORK AND THE PROJECT'S (the owner, 2026-11-09):
every submission the plugin makes is tagged with the session that made it,
and `attach --all` attaches the workflows and single tasks tagged with this
Claude Code session AND every one still running that clones this checkout's
`origin` repository, whichever session started it -- the same as the
session start's `attach --all --project` (`project: true` is still accepted
and means the same). Each entry another session started (or nobody tagged)
is marked `this_session: false`, and the reply's `project.from_other_sessions`
counts them: say in one line how many came from other sessions. Another
repository's work from another session is never attached, nor untagged work
of another repository. The reply's `session.left_out` now counts only those:
`not_this_project` (another repository's) and `untagged`; say it in one line
when it is not zero, with `/sc attach <wf_id>` for one by hand. With no
`origin` naming a repository nothing widens: only this session's work is
attached, `project.note` says why, and `other_session` counts the rest. In
the pane layout (`rows: "pane"`) the attach-all stays this session's: the
pane lists the project's work with no model row. `session.session_id: null`
means the bridge could not tell which session it serves: then only the
project's work is a candidate, and `session.scope` says so -- report it.

ONE CLAUDE CODE RUN PER SWARMCLOUD WORKFLOW, never one run over several: each
workflow keeps its own step count, as the console shows it. Call
`swarm_workflow_launch` once with `{attach: "all"}`. It lists the caller's
tenant's running workflows once, keeps this session's and the project's and, for the 100 closest to finish (the plugin's
`max_live_rows` option, held to 1-200), grouped by workspace, writes one
titled script each -- named `SC ∴ <name> · N steps`, its description
starting with its details, `Workspace: <workspace> (<tenant>) · Started by: ...` -- then make one Workflow call per entry of its `launches`,
`{scriptPath: <its script_path>, args: <its args>}` — each attaches its
workflow exactly as `attach <wf_id>` does: finished steps reported once, a
live row for every unfinished step; every row's label carries its task's
console link as served, from the moment the row starts, whatever the step's
state. More than that come back in `not_followed`, each with its
`workspace` and the `/sc attach <wf_id>` that follows it, and are not
launched — every row is an agent of its own in this session, so the cap
stays a cap. With `rows` at `summary` (the default) each workflow is ONE
summary row, and the cap counts workflows; with `per-step` it counts step
rows across every workflow, and a workflow with more rows than one Claude
Code run shows comes back as several entries, `rows 1-8`, `rows 9-16`... —
launch every entry of `launches`. `/sc attach --all --per-step` passes
`{attach: "all", rows: "per-step"}` (and `--summary`, `rows: "summary"`) for
this one attach-all, whatever the setting; single tasks have one row each in
every mode.

**SCROLLING IS CLAUDE CODE'S, NOT THE PLUGIN'S.** The /workflows panel is
Claude Code's own UI: this plugin cannot add scrolling to it, and cannot make
it show more lines. What the plugin does is keep 100 rows scannable: every row
is ONE compact line (`[SwarmCloud] <name> · stage <n> · <step>
· <console link>`), in a total order -- closest to finish first (steps done of the total, then
the current step's age, then the id), grouped by workspace -- so two
attaches of the same state list the same order. Say this
plainly when someone asks for scrolling.
SINGLE TASKS (archived issue 830): each of your running tasks in no workflow gets ONE entry
of its own in `launches`, whose args are `{attach_tasks: [<that task>]}`, its
run titled `SC · <label>` from that task alone — launch every one like the
others; each starts ONE `[SwarmCloud] <label> · task` row, with its workspace
and the rest of its details shown when the row is expanded, following its
task until it finishes. That holds whatever the `rows` setting (owner
2026-11-04: a summary row over a batch read `0/1` and showed counts, not
lanes);
a task row that answers before its task ends (SUCCEEDED, FAILED, CANCELLED, DEAD_LETTERED) or parks for a person is started again at once, so it never ends on a DISPATCHED task; `max_live_rows` caps the total (at most 100 by default) and the rest
come back in `not_followed`. A copy launched with any task but the one it
was written for is refused, naming the right call: never reuse one. `single_tasks` says how many there were;
`single_tasks_error` means they could not be read — say so verbatim.
`count: 0` and no task launch is an answer: nothing is running. Report each run's `state`
(`NOT_ATTACHED` with the API's error verbatim) and the `not_followed` list.
If `swarm_workflow_launch` answers an error, run the `/sc:swarmcloud`
workflow by name with args `{attach: "all"}`: it lists the running workflows,
keeps only those `swarm_workflows` marks `this_session`, and returns `attach_calls`, the exact `{attach: "<wf_id>", title, workspace}` args of
each — run `/sc:swarmcloud` once per entry with those args — and `task_call`,
the `{attach_tasks: [...]}` args for your running single tasks (null when none
run): run `/sc:swarmcloud` once more with it. This is what the
plugin's SessionStart hook asks a new session to run first when workflows or
single tasks of yours are running. No hook fires on `/reload-plugins`, so
after a reload — or whenever a task started outside this session — run it
by hand; it submits nothing either way. Last, the mandatory final step when
it launched at least one run: [Arming the watch](#arming-the-watch), with the
reply's `arm_watch` (null when nothing was launched).

### Session start: this session's work, and the project's running work

At session start the plugin's SessionStart hook names the SwarmCloud work
still running that this session should track -- and here, as in the plain
`attach --all` (the owner, 2026-11-09), it is WIDER than `check` and the
watch (the owner, 2026-10-25: "at start also try
to run attach and see if there are any workflows that the project its
starting in has any work running in sc that needs to be attached and tracked
here"). It names this Claude Code session's own work AND the work still
running for THIS PROJECT: every workflow and single task that clones this
checkout's `origin` repository, whichever Claude Code session started it.
Another repository's work from another session is never named; with no
`origin` naming a repository nothing widens, and only this session's work is
named.

* In the **rows** layout the hook asks for **`/sc attach --all --project`**:
  one `swarm_workflow_launch` call with `{attach: "all", project: true}`,
  launched exactly as `attach --all` launches (every entry of `launches`,
  then [Arming the watch](#arming-the-watch)). Its `project` says the
  repository and how many came from other sessions (`from_other_sessions`);
  say that in one line. Entries another session started are marked
  `this_session: false`.
* In the **band** layout the strip above the prompt opens at session start
  with this session's AND this project's running work, read with
  `swarm_browse {level: "workflows", project: true}` and no model row, and
  the hook asks for nothing else -- no attach: the band starts no rows (see
  "Layouts").
* In the **pane** layout the pane opens at session start, not read yet (see
  "Layouts"), and its Refresh lists the project's running work beside
  this session's (`swarm_browse {level: "workflows", project: true}`) with
  no model row for it; the hook asks for the plain `/sc attach --all` only
  when this session has work of its own, for its quiet finish rows.

WHICH IS WHICH: the widening is the session start's AND the plain
`/sc attach --all`'s (the owner, 2026-11-09: a person attaching from a
terminal or another session sees the project's work, not a gap that reads as
a bug) -- `{attach: "all"}` and `{attach: "all", project: true}` mean the
same. `/sc check` and the watch stay scoped to this session -- they check and
attach only work this Claude Code session started (owner decision
2026-10-14); the check's attach passes `new_only`, which keeps it so.
