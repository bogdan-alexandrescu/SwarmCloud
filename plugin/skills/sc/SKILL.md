---
name: sc
description: The SwarmCloud front door, `/sc [verb]`. With no verb, show and interpret SwarmCloud cluster state — the subscription account pool and its 5-hour/7-day quota windows, pool ceilings and which pool binds each runner profile, agents running and queued, and what is wrong right now. `status <id>` reads one workflow or task once; `workflows` lists your tenant's workflows still running in SwarmCloud; `attach <workflow_id>` re-attaches live rows to a workflow still running in SwarmCloud and submits nothing, and `attach --all` does that for every running workflow of your tenant (at most 100 followed, closest to finish first, the plugin's `max_live_rows`) and gives each of your running single tasks — dispatched with swarm_dispatch, in no workflow — one row (at most 100), every run's and row's expanded details naming the workspace it runs in, who started it and its LLM account; `run <spec>` submits a workflow spec through /sc:swarmcloud and says so before it does; `run --issue owner/repo#N` plans a GitHub issue (one planner task) and `plan show|approve|edit|reject <run>` reads or acts on that plan, approving only the digest the developer was shown — `run` and `plan approve|edit|reject` are the verbs that write. `check` reads which of this project's SwarmCloud workflows and tasks are stuck or stale (swarm_check, read-only) and then gives every running workflow of the project that has no row in this session its live rows; `watch [interval]` runs that `check` every interval (default 30m, at least 5m) with Claude Code's CronCreate and remembers it for the project; the watch also arms itself (CronList first, never a second job) whenever this session attaches to or starts SwarmCloud work, and `watch off` stops it and keeps it off for the project until `watch` again. `build-board` refreshes this project's local build board (`.claude/build-board/index.html`, git-ignored, never committed) and opens it in the browser. `keys` says whether a TypeSafe key is set for you and your tenant and which one a task would use; `keys set <provider> [--tenant]` gives the developer a one-time console link to paste the key into themselves — the key never passes through this session. `roles` shows the roles of the workspace this session acts in (read-only); a role is changed only by the developer, typed at their own terminal. Use when asked "is my TypeSafe key set", "set my TypeSafe key", "what is the swarm doing", "how much quota is left", "why is my task queued", "is anything broken", "which account is nearly full", "where is my workflow", "what workflows are running", "show my workflow's rows again", before dispatching a long batch, to run a spec, or to plan and run a GitHub issue ("work on issue #N", "approve the plan").
argument-hint: "[status <wf_id|task_id> | workflows | attach <wf_id> | attach --all | check | inputs | answer <wf_id> <step_id> [file ...] | build-board | watch [interval] | watch off | run <spec path|JSON> | run --issue owner/repo#N | runs | plan show|approve|edit|reject <run> | keys | keys set <provider> [--tenant] | roles]"
arguments:
  - verb
  - target
allowed-tools:
  - Bash(uv run sc)
  - Bash(uv run sc overview:*)
  - Bash(uv run sc accounts:*)
  - Bash(uv run sc agents:*)
  - Bash(uv run sc capacity:*)
  - Bash(uv run sc task:*)
  - Bash(uv run sc trouble:*)
  - Bash(uv run sc whoami:*)
  - Bash(uv run sc config:*)
  - Bash(uv run sc debug:*)
  - Bash(uv run sc workflows:*)
  - Bash(uv run sc runs:*)
  - Bash(uv run sc run show:*)
  - Bash(uv run sc plan show:*)
  - Bash(uv run sc roles)
  - Bash(uv run sc roles --json)
  - Bash(uv run sc roles list:*)
  - Bash(sc)
  - Bash(sc overview:*)
  - Bash(sc accounts:*)
  - Bash(sc agents:*)
  - Bash(sc capacity:*)
  - Bash(sc task:*)
  - Bash(sc trouble:*)
  - Bash(sc whoami:*)
  - Bash(sc config:*)
  - Bash(sc debug:*)
  - Bash(sc workflows:*)
  - Bash(sc runs:*)
  - Bash(sc run show:*)
  - Bash(sc plan show:*)
  - Bash(sc roles)
  - Bash(sc roles --json)
  - Bash(sc roles list:*)
  - Bash(open /*/.claude/build-board/index.html)
  - Bash(xdg-open /*/.claude/build-board/index.html)
  - Bash(uv run swarm doctor:*)
  - Bash(uv run swarm profiles:*)
  - CronCreate
  - CronList
  - CronDelete
---

# sc — SwarmCloud cluster state, and the front door

This skill reads only, except `run`, which submits and says so first,
`plan approve|edit|reject`, which act on an issue run's plan only when the
developer asks for exactly that, `answer`, which sends a workflow step
only what the developer types into this host's own form, and
`swarm_task_answer`, which records on a task an answer the developer gave
here to its agent's question. `build-board` writes one local, git-ignored
page and nothing in SwarmCloud.

## The verb

`/sc` (or `/sc:sc` when another command already claims `/sc`) reads its verb
from the first word of `$ARGUMENTS` and its target from the rest. One verb,
one action — never a second one the developer did not ask for:

| `$ARGUMENTS` | What happens | Writes? |
|---|---|---|
| no verb (empty) | today's cluster state: `uv run sc`, read by the rules below | no |
| `status <wf_id\|task_id>` | one status read of that workflow or task | no |
| `workflows` | lists your tenant's workflows still running in SwarmCloud, marking `[this session]` the ones this Claude Code session started | no |
| `attach <wf_id>` | re-attaches live rows to a workflow running in SwarmCloud | no |
| `attach --all` | live rows for every running workflow THIS Claude Code session started (at most 100), and one per running single task it dispatched (at most 100); `max_live_rows` moves both, 1-200 | no |
| `check` | which of THIS SESSION's workflows and tasks in THIS PROJECT are stuck or stale, stuck first, each with its reason, evidence, console link and next command: `swarm_check`. Then live rows for every running workflow and single task of the project that has none in this session | no (attaching submits nothing) |
| `watch [interval]` | a `check` every interval (default `30m`, at least `5m`) in this session, through Claude Code's CronCreate, remembered for this project so the next session re-arms it: `swarm_check_schedule`. It also arms itself when this session attaches or starts work (see "Arming the watch") | schedules a prompt in this session; nothing in SwarmCloud |
| `build-board` | refreshes this project's build board, `.claude/build-board/index.html`, and opens it in the browser: `swarm_build_board` | writes the local page only (git-ignored); nothing in SwarmCloud |
| `inputs` | the workflow steps of your tenant waiting for a person's answer: `swarm_inputs` | no |
| `answer <wf_id> <step_id> [file ...]` | the developer answers that step in this host's own form: `swarm_workflow_input`. You never write the answer | **yes**, what the developer typed |
| `watch off` | cancels this session's watch and saves "off" for the project, so nothing arms it again until `watch` | no |
| `run <spec path\|JSON>` | submits a workflow spec, and says so first | **yes** |
| `run --issue owner/repo#N [--plan auto] [--auto-merge] [--fix-rounds N]` | plans a GitHub issue (one planner task), and says so first | **yes** |
| `runs` | lists your tenant's issue runs | no |
| `plan show <run>` | shows a run's plan and its digest | no |
| `plan approve\|edit\|reject <run>` | acts on that plan, with the digest the developer was shown | **yes** |
| `keys` | whether a TypeSafe key is set for you and for your tenant, and which one your tasks would use: `swarm_keys`. Never a value | no |
| `keys set <provider> [--tenant]` | a one-time console link where the developer pastes the key themselves: `swarm_key_set_link`. The key never passes through this session | mints a link; stores nothing |
| `roles` | the roles of the workspace this session acts in: its default, the overrides, your own role and how you reach it, the recent role events: `swarm_roles` | no |
| `roles set\|clear\|default ...` | not run here: tell the developer the `sc roles` command for their own terminal (see `roles`) | no (the developer's, typed) |
| a view name (`accounts`, `agents`, `capacity`, `trouble`, `task <id>`, `whoami`, `config`, `debug <id>`) | that view, as in "Which view" below | no |

Anything else: say which verbs exist and run nothing. `/sc:swarmcloud` keeps
working unchanged; these verbs are a shorter way in, not a replacement.

### `status <wf_id|task_id>`

ONE read, then report it. An id beginning `wf_` is a workflow: call
`swarm_workflow_status` with it once and report the workflow's `state` (null
means it was not derived — say that, never quote `stored_state`) and each
step's state. Any other id is a task: call `swarm_status` with
`task_ids: [<id>]` once and report its state. Start the line with the
workspace the reply names, `[<workspace>] <id> · <state>` (the reply's
`workspace`; leave it out only when the reply has none -- never fill it in
from the session's tenant). A waiting task's reply carries `waiting_line` --
why it waits, in the API's own words, with its next check when known: print
it after the state, `[<workspace>] <id> · <state> · <waiting_line>`, e.g.
`[eng@saga.xyz] task_31c3… · PARKED · CI on PR #1779: 5 checks pending; next
check 02:46Z`, and nothing more of `waiting` (its detail and history are the
console page's). No `waiting_line` -- a task not waiting, or an API older
than it -- is the line without it. An error is reported verbatim:
an id the API does not have and another tenant's id read the same. Never poll;
a second look is the developer's next `/sc status`. Both tools are the sc
plugin's own read tools and work outside a checkout; they are not pre-granted
here -- this skill's grants mark it as the checkout-bound view surface -- so
the session may ask once before the first read.

### Every run is titled after its workflow

A SwarmCloud workflow run here is titled `SC · <name> · N steps` (at most
150 characters), not `swarmcloud`: <name> is the spec's `title`, else its
`label` cut at a word, else the title or label SwarmCloud stored, else the
workflow id, cut to fit, never the step count. A run of single tasks is
titled `SC · <label> · task` (several: `SC · <first label> +N · N tasks`).
NO NAME CARRIES A WORKSPACE -- not the run's, not a row's (owner,
2026-10-14: "workflows in here should not have the [Engineering] or
[eng@saga.xyz] prefix in front of the names"). The run's DESCRIPTION starts
with its DETAILS, one line, six fields in this order:
`Workspace: <workspace> (<tenant>) · Started by: <submitted_by> · Profiles: <runner profiles> · LLM: <pool account> (<provider>), model <model> · Browser tool: <browser_tool> · Session: <claude_session>`
-- <workspace> is the workspace (tenant) the API's document says it runs in,
by its display name when the API gives one, and <tenant> its tenant id
(`Workspace: <tenant> · ` alone when there is no display name); `Started by`
is the `submitted_by` the API stores (an email, or the principal a key acts
as); `LLM` is the pool account each step's attempt runs on, by the label its
owner gave it, and the model; `Browser tool` is a `claude-code-browser`
step's tool (`auto` when it named none, `not used` when no step runs that
profile); `Session` is the Claude Code session tag, `this session (<id>)`
when it is this one. Each field says `not reported` when the API serves
none -- `Workspace: not reported` included -- and the workspace stays first,
so clipping a long description never drops it. Nothing credential-shaped is
ever shown: a value that looks like a key is `withheld`. A run started
before an account was assigned says `LLM: not assigned yet`; each of its
rows then says `LLM: <account>` once, on its first progress line after the
assignment. Every row INSIDE the run is `[SwarmCloud] <name> · stage <n> ·
<step>` (a single task: `[SwarmCloud] <label> · task`) with its console
link, and shows ITS OWN details line when expanded (the last line of its
prompt, `details: ...`); every result object and every listing line names
the workspace. A run's name is fixed when it is launched, so `run` and `attach`
first call `swarm_workflow_launch`, which writes a copy of the
`/sc:swarmcloud` script with that name and returns its `script_path`, and
then launch THAT copy with the Workflow tool, `{scriptPath: <script_path>,
args: ...}`. If `swarm_workflow_launch` answers an error, say so in one line
and run the `/sc:swarmcloud` workflow by name with the same args: the rows are
the same, only the run's title is the generic one.

### `attach <wf_id>`

Call `swarm_workflow_launch` with `{attach: "<wf_id>"}`, then run the script it
returned with the args it returned — `{attach: <wf_id>}` plus the `title`,
`workspace` and `details` (each step row's details line) it chose, nothing else. It submits nothing: it reads the workflow once, reports
its finished steps once, starts a live row for every unfinished step, and ends
with the workflow's state. This is how a session that restarted gets its rows
back for a workflow that kept running in SwarmCloud. An unknown or
other-tenant id ends with the API's error; report it verbatim.
By hand it attaches ANY workflow your tenant may read, this session's or
not: when the reply carries `not_this_session` (`this_session: false`), say
in one line that the workflow is not this session's -- another Claude Code
session started it, or it carries no session tag -- and that `check` and
`attach --all` leave it out. It is attached all the same.
Last, the mandatory final step: [Arming the watch](#arming-the-watch), with
the reply's `arm_watch` -- also when the workflow has already finished.

### `workflows`

ONE read of the caller's tenant's workflows that are not finished — every
one whose state is not `SUCCEEDED`, `FAILED`, `CANCELLED` or `DEAD_LETTERED`,
closest to finish first. Call `swarm_workflows` once (the sc plugin's own read tool; it
works outside a checkout); in a checkout `uv run sc workflows` prints the
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

ONLY THIS CLAUDE CODE SESSION'S WORK (owner, 2026-10-14): every submission
the plugin makes is tagged with the session that made it, and `attach --all`
attaches only workflows and single tasks tagged with this Claude Code
session -- never another session's in the same tenant or workspace, and
never untagged work (submitted before tagging, or by another client). The
reply's `session.left_out` counts what was left out and why; say it in one
line when it is not zero, with `/sc attach <wf_id>` for one by hand.
`session.session_id: null` means the bridge could not tell which session it
serves: then nothing is a candidate, and `session.scope` says so -- report it.

ONE CLAUDE CODE RUN PER SWARMCLOUD WORKFLOW, never one run over several: each
workflow keeps its own step count, as the console shows it. Call
`swarm_workflow_launch` once with `{attach: "all"}`. It lists the caller's
tenant's running workflows once, keeps this session's and, for the 100 closest to finish (the plugin's
`max_live_rows` option, held to 1-200), grouped by workspace, writes one
titled script each -- named `SC · <name> · N steps`, its description
starting with its details, `Workspace: <workspace> (<tenant>) · Started by: ...` -- then make one Workflow call per entry of its `launches`,
`{scriptPath: <its script_path>, args: <its args>}` — each attaches its
workflow exactly as `attach <wf_id>` does: finished steps reported once, a
live row for every unfinished step; every row's label carries its task's
console link as served, from the moment the row starts, whatever the step's
state. More than that come back in `not_followed`, each with its
`workspace` and the `/sc attach <wf_id>` that follows it, and are not
launched — every row is an agent of its own in this session, so the cap
stays a cap.

**SCROLLING IS CLAUDE CODE'S, NOT THE PLUGIN'S.** The /workflows panel is
Claude Code's own UI: this plugin cannot add scrolling to it, and cannot make
it show more lines. What the plugin does is keep 100 rows scannable: every row
is ONE compact line (`[SwarmCloud] <name> · stage <n> · <step>
· <console link>`), in a total order -- closest to finish first (steps done of the total, then
the current step's age, then the id), grouped by workspace -- so two
attaches of the same state list the same order. Say this
plainly when someone asks for scrolling.
SINGLE TASKS (#830): your running tasks in no workflow get ONE more entry in
`launches`, whose args are `{attach_tasks: [...]}` — launch it like the
others; it starts one `[SwarmCloud]` row per task, titled from the task's
label, with its workspace and the rest of its details shown when the row is expanded, each following its task until it
finishes (at most 100, `max_live_rows`; the rest come back in `not_followed`). `single_tasks` says how many there were;
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

### `check`

READ-ONLY: it reports and attaches, and never cancels, retries, releases or
re-dispatches anything (owner, 2026-10-14: "report only"). Two calls:

1. ONE `swarm_check` call with no arguments: it reads the running workflows
   and single tasks that this Claude Code session started -- another
   session's work in the same tenant, and untagged work, is left out
   (`session.left_out` counts it; never report it as stuck) -- and that clone
   THIS checkout's `origin` repository (`repository: "all"` for every repository; `tenants:
   "all"` for every tenant you may choose; `stale_minutes` moves the 20-minute
   window). Report `flagged` FIRST, STUCK before STALE as it is ordered, one
   line each that starts with the workspace: `[<workspace>] <workflow_id or
   task_id> · <kind> · <reason>`, then the `evidence` that supports it (the
   heartbeat and last event, the age and whether it is exact or "at least",
   the binding pool and where it was read), its console link as served, and
   its `next` -- a `/sc status <id>` to run, or the PR link to open. The rules:

   | `rule` | flags | is NOT flagged |
   |---|---|---|
   | `no_heartbeat` | RUNNING/STARTING with no heartbeat and no event for more than the window | a beat or an event inside it |
   | `waiting_for_capacity` | READY/QUEUED longer than the window, with the pool that binds it | a task that entered the state recently. It holds nothing while it waits (invariant 1): report it as waiting, not as broken |
   | `parked` | HUMAN_REQUIRED (only a person promotes it); CI_PENDING over 90 min; DEPENDENCY_INCOMPLETE whose dependency failed | any other park reason: a quota window or a retry backoff is the platform waiting by design |
   | `merge_refused` | a merge step FAILED with `head_moved`, `merge_conflict` or `checks_failed`, and its PR is still open | any other failure, and a PR that has since MERGED or CLOSED (the check reads the PR's state from GitHub's public API). When `pull_request_state` is null, say the PR's state was NOT read (its `pull_request_state_unread_because` says why), and give its link |
   | `state_disagrees` | a workflow whose stored state and derived state disagree on whether it is finished (the stale RUNNING cache) | a stored QUEUED under a derived RUNNING: the cache lags between reads |
   | `awaiting_input` | STUCK only when an AWAITING_INPUT step is still parked more than two ticks (2 min) past its `expires_at`: the expiry sweep is not running | a wait inside its window, or just past it: listed under `awaiting_input`, below |

   `awaiting_input` (beside `flagged`, never inside it) lists the steps
   waiting for a member of their tenant to answer, as information and NOT as
   stuck: one line each, `[<workspace>] <workflow_id> · <step_id> · <reason>`
   ("waits for a member of eng since 14:02 UTC, expires in 3 h 0 m"), and its
   `next` -- `/sc answer <wf_id> <step_id>` or its console link. A waiting
   step holds no lease and no capacity (invariant 1). HUMAN_REQUIRED stays a
   STUCK `parked` flag.

   `unchecked` lists what was not checked and why -- say so, never call it
   fine. `repository: null` means the check was not scoped (`repository_note`
   says why). Nothing flagged is an answer: say "nothing stuck or stale in
   <repository>" with the count of running workflows and tasks.
2. Then attach what has no row: ONE `swarm_workflow_launch` call with
   `{attach: "all", repository: <the check's repository, or "all" when it
   was null>, new_only: true, exclude: [<the workflow and task ids of every
   [SwarmCloud] row you can see live in this session>]}`, and one Workflow
   call per entry of its `launches`, exactly as `attach --all` launches them.
   `new_only` leaves out every workflow and task this session was already
   handed a row for, and `exclude` what you can see, so nothing is attached
   twice; `already_attached` names them, `repository_unread` names any whose
   repository could not be read, with the `/sc attach <id>` for each. Empty
   `launches` is an answer: every running workflow of the project already has
   its rows. `/sc attach <wf_id>` by hand always attaches, whatever was
   attached before -- that is how a row that was closed comes back.
3. When step 2 launched at least one run, the mandatory final step:
   [Arming the watch](#arming-the-watch), with its reply's `arm_watch`.
   `nothing_running` in the check's answer means nothing of the project runs:
   say so in one line, and LEAVE the watch armed -- a check every interval
   is one read, and the next workflow is watched without anyone re-arming
   it. A check never cancels its own job; only `watch off` does.

### `build-board`

This project's build board (#1773): one local HTML page of every lane of
the work the developer submitted against this project's repository -- its
state, pull request and what it waits on, grouped by feature -- and a
"Waiting on someone" list. It lives at `<project>/.claude/build-board/`,
beside a `.gitignore` of `*`, so it is never committed, and it belongs to no
Claude account. The bridge already refreshes it after every dispatch, check
and attach, and as rows see work move; this verb refreshes it now and shows
it.

1. Make ONE `swarm_build_board` call (no arguments). It reads, renders and
   writes the page and answers `{path, written_at, counts, waiting}`.
2. Open the returned `path` -- the absolute path, exactly as answered -- with
   `open <path>` on macOS or `xdg-open <path>` elsewhere (the session's
   platform says which). Those two commands, for that one file, are all this
   verb runs; if the open fails, give the path for the developer to open.
3. Report in ONE line: the path, `written_at`, and the `counts` (lanes,
   merged/done, in flight, queued, needs you) with `waiting`.

A project with no work yet gets an empty board that says "No lanes yet": it
is a board, not an error -- open it and report zero lanes. An `error` in the
answer is a failed refresh: say it in one line and, when `path` is not null,
that the previous page is still there to open. Nothing here is written to
SwarmCloud; no other command is run.

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

### Answers given here are recorded on the task

A task's agent, or a workflow step's, may ask the person questions (its
`questions.json`: a row's result's `questions`, `swarm_result`'s
`owner_questions`). When the person answers one of them in this session --
with AskUserQuestion or in chat -- record each answer on that question's task
with ONE `swarm_task_answer` call per question:
`{task_id, question_index, choice?, text?}`. `question_index` is the
question's 0-based place in the task's questions. `choice` is the task's own
option label, exactly as served, when the person picked one of them; `text`
is the person's words, or what they chose when it was none of the options.
The bridge adds `via: claude-code` and this session's id, so the console
shows the answer as given in Claude Code instead of leaving it unanswered.

Only the person's own pick, verbatim, from a question this session asked
them; never an answer the agent chose itself, a recommended option they did
not pick, or a paraphrase of what they might want. A question nobody
answered is left unanswered. Report a refusal as it came: a 403 means the
caller is neither the task's submitter nor an admin (or is a viewer), and the
answer stays where it was given -- say so and give the task's console link.

### `watch [interval]`

A `check` every interval, in this session. Call `swarm_check_schedule` with
`{interval: "<interval>"}` (none given: `30m`; below `5m` it is raised to
`5m`; an interval a cron expression cannot say, such as `45m`, is rounded and
the reply says so). It saves the preference for this project and schedules
nothing itself: first remove any older watch -- CronList, then CronDelete
every job whose prompt is `/sc check` -- then make the reply's `arm_with`
call, CronCreate `{cron, prompt: "/sc check", recurring: true}`, and say the
interval, the cron and that it runs only while this session is open and idle
(a recurring job also ends after 7 days). If CronCreate cannot be called here,
tell the developer to run the reply's `without_cron` command instead, which
for the default is `/loop 30m /sc check`, and do not claim a watch is armed.
The plugin's SessionStart hook reads the saved preference and asks each new
session in this project to run `/sc watch` again, until `watch off`.

Every firing checks and attaches only what this Claude Code session started
(see `check`): a watch never reports, or attaches, another session's work.

The watch arms itself, too (owner, 2026-10-14): whenever this session attaches to or
starts SwarmCloud work -- `run`, `run --issue` once its workflow exists,
a task sent with `swarm_dispatch` (one or `tasks`), `attach <wf_id>` (a
finished workflow too), `attach --all` and a `check` that attached something --
the tool's reply carries `arm_watch`, and that verb ends with
[Arming the watch](#arming-the-watch). So does a new session in which work this
session started is running (a resumed one): the SessionStart hook asks it to
arm the watch beside the attach. `/sc watch` by hand is still how to pick another interval.

### Arming the watch

The final step of every verb that attached or started work, and MANDATORY:
the reply's `arm_watch` says how, and it is null when there is nothing to arm
-- nothing was attached, no workflow exists yet, or `/sc watch off` turned the
project's watch off (`arm_watch_off` then says so: say it in one line and arm
nothing). The verbs: `run` (including a run that ended `SUBMISSION_UNKNOWN`
or `SUBMITTED_UNVERIFIED` -- work may be running, and the result's own
`arm_watch` says to arm with `swarm_workflow_launch`'s; only `NOT_SUBMITTED`
sent nothing), `run --issue` once its workflow exists, every `swarm_dispatch`
(one task or `tasks`: the reply carries `arm_watch` beside its `rows`),
`attach <wf_id>` even of a workflow that has already finished, `attach
--all`, and a `check` that attached something.
Otherwise:

1. CronList. If a job whose prompt is `/sc check` already exists, the watch
   is armed in this session: stop here -- never a second job, and do not
   replace it (that is `/sc watch`'s to do, by hand).
2. Call `swarm_check_schedule` with `arm_watch.save_with`'s args (the saved
   interval, else `30m`), which remembers it for the project.
3. Make the `arm_watch` call: CronCreate with its `args`, `{cron, prompt:
   "/sc check", recurring: true}`, and say its `then_say` in one line.

If CronCreate cannot be called here, say `swarm_check_schedule`'s
`without_cron` command (`/loop 30m /sc check`) once and do not claim a watch is armed. Once armed,
later verbs in this session find it at step 1 and make no call.

### `watch off`

Call `swarm_check_schedule` with `{off: true}`, then CronList and CronDelete
every job whose prompt is `/sc check`. Say how many were cancelled; a
`/loop ... /sc check` started by hand stops with Esc, which only the
developer can press. The off is SAVED for the project, not forgotten: no
attach, run or new session arms the watch again until `/sc watch`, and every
`arm_watch` is null meanwhile.

### `keys`

ONE call to `swarm_keys` (`{provider: "typesafe"}`), and show its `text`:
whether the developer's own key is set, whether their selected tenant's is,
and which one a task they submit there would use — their own key wins over
the tenant's. Status only: `set`, `version`, who set it and when. There is no
value to show and never a reason to ask for one. `available: false` on their
own key means they have no ready personal workspace to keep it in: say so,
and that an admin can set a tenant key instead. `consumed_by` names what
reads the key: `jev`, the jev-browser tools of a live-browser task, which send
page text to TypeSafe. When it is empty, do not describe a key as in use.

### `keys set <provider> [--tenant]`

**THE KEY NEVER PASSES THROUGH THIS SESSION** (owner, 2026-10-14): not in
chat, not as a tool argument, not in a command line. Never ask the developer
for it, and if they paste one anyway, do not repeat it, do not pass it to any
tool or command, and tell them to rotate it at TypeSafe, since it is now in
this transcript.

`<provider>` is `typesafe`, the only one; anything else: say so and stop.
Call `swarm_key_set_link` with `{provider: "typesafe", scope: "user"}` — or
`scope: "tenant"` with `--tenant`, which sets the selected tenant's key and
needs an admin. It answers only `url` and `expires_at`. Give the developer
the `url` and tell them to **open it and paste the key into the console's
form**; it works once, for ten minutes, and only in their own console
session. Then `/sc keys` shows the result. A `null` url comes with a `note`:
show it verbatim. An error is the API's: report it verbatim (a 404 on `user`
means no ready personal workspace; a 403 on `tenant` means not an admin).

If the console answers "keys can't be saved yet", the deployment cannot store
keys until its owner applies the KEYS-IAM bootstrap role; say exactly that,
and do not retry. In a terminal, the developer can instead run
`sc key set typesafe [--tenant]` themselves: it asks for the key at a hidden
prompt and prints only `stored: version N`. Tell them the command; never run
it — it is not granted to this skill, and it would put the key in front of
you.

### `roles`

ONE call to `swarm_roles`, and show its `text`: the workspace this session
acts in, your own role there with its `source` (`override`, `default`,
`personal`, `platform_admin`) and `via`, the workspace's default role, every
person with an override, what each role permits, and the recent role and
platform-admin events. `swarm_tenants` carries the same `role` and `via` for
every workspace you may choose. `via: platform_admin` means you are acting as a
platform admin in a group workspace you are not in: say that every action
there is recorded in its audit log.

A `viewer` sees everything and changes nothing; a `member` submits, approves,
answers and cancels; an `admin` also manages secrets, MCP servers, provider
keys, the workspace's settings and its roles. A refusal such as
`role_member_required` is that rule, not a fault: report it verbatim.

**NO TOOL CHANGES A ROLE, AND YOU NEVER DO** (owner answer Q11). If the
developer wants one changed, tell them the command to run in their own
terminal — `sc roles set <email> <role>`, `sc roles clear <email>` (back to
the default) or `sc roles default <viewer|member>` — which asks them to type
the email and the role back at the terminal and sends nothing otherwise; or
the console's Settings › People. It needs `admin` in that workspace
(`can_manage`). Never run it: it is not granted to this skill, and a
confirmation it could read from you would not be the developer's.

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
   workflow"). Run that script with the spec OBJECT as its args — never
   a bare path, and never the JSON as a string: a path makes a haiku agent
   retype the spec, which is how long specs came back altered and were
   refused. Given a file under the checkout, the args are `{spec: <the spec
   object>, spec_path: "<its absolute path>"}`; given JSON text, or a file
   outside the checkout, `{spec: <the spec object>}`. Either way add
   `details: <the reply's row_details>` beside them, so each step's row
   shows its own details when expanded.
   `spec_path` beside the object is a reference, not the spec: the bridge
   reads those exact bytes for the submission and checks them against the
   digest of the object, so nothing is retyped and a spec that changed on the
   way is refused, not submitted.
4. Report what the workflow returned: its `workflow_id`, its state, and any
   `error` verbatim — `NOT_SUBMITTED`, `SUBMISSION_UNKNOWN`,
   `SUBMITTED_UNVERIFIED` and `FOLLOW_REFUSED` each say what to do next.
5. Last, the mandatory final step, once a spec was submitted -- every outcome
   but `NOT_SUBMITTED`, `SUBMISSION_UNKNOWN` and `SUBMITTED_UNVERIFIED`
   included:
   [Arming the watch](#arming-the-watch), with `swarm_workflow_launch`'s
   `arm_watch` (`swarm_workflow`'s reply carries the same).

### `run --issue owner/repo#N`

Plans a GitHub issue and, once the plan is approved, runs it (#454). A planner
task reads the issue and the repository's open issues and pull requests and
writes a plan; the approved plan compiles into one workflow that opens a pull
request, whose CI the platform reads and fixes for up to `--fix-rounds` rounds
(3 by default). The issue gets a plan comment and one status comment, kept up
to date by the platform with the tenant's own forge credential — never one
this session supplies.

1. Say, before anything is submitted: "about to plan SwarmCloud issue run for
   `<owner/repo#N>`: one planner task, no capacity held by the plan until it is
   approved". Then call `swarm_run_issue` with `issue`, and `plan_approval:
   "auto"`, `auto_merge: true`, `fix_rounds` or `model_class` only when the
   developer gave `--plan auto`, `--auto-merge`, `--fix-rounds` or
   `--model-class <class>` (the planner's model class; see "Model classes"
   below). The terminal equivalent,
   `uv run sc run --issue <owner/repo#N> --follow`, is the developer's to run
   (this skill is not granted it); it prints one row per step as it moves.
2. `--auto-merge` merges the run's pull request with the `merge` step (#295,
   enabled by contract request 47). The API refuses it only while the `merge`
   profile is disabled, and then nothing is created: report that refusal
   plainly, in its own words, and offer the same run without `--auto-merge`.
3. Report the run's `id` and `state`. A `required` run (the default) stops at
   `PLANNED`, holding no capacity, until the developer approves — go on to
   `plan show`. Once the run is `RUNNING` (or `FIXING`, a CI fix round), its
   answer carries `attach_with`: run `/sc attach <workflow_id>` for live rows.
   Once a workflow exists, the reply's `arm_watch` is set too: end with
   [Arming the watch](#arming-the-watch) (the attach would arm it anyway, and
   step 1 there makes sure only once).

### Model classes

A step picks the model it runs by **class**, by name (#1435): `opus` (the
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

### `runs`

ONE read: call `swarm_runs` (in a checkout, `uv run sc runs`) and report each
run's id, issue, state and pull request as served. Never poll.

### `plan show <run>`

ONE read: call `swarm_run` with `run_id` (in a checkout,
`uv run sc plan show <run>`), and show the developer the WHOLE plan — summary,
requirements, overlaps with work in flight, every step's title, files, tests,
estimate and prompt — and its `plan_digest`. The prompts are what will run with
the tenant's forge token; they are what is being approved.

### `plan approve|edit|reject <run>`

THE DIGEST IS THE APPROVAL. Approve only a plan the developer was SHOWN, with
the `plan_digest` of that showing: if the plan was not shown in this
conversation, do `plan show` first and wait for the developer to say approve.
Never read the run and approve in one go.

- `approve`: `swarm_plan_approve` with `run_id` and the shown `plan_digest`.
  The workflow is submitted; report its `attach_with`.
- `edit`: build the whole replacement plan from the developer's instructions,
  show it, then `swarm_plan_edit` with `run_id`, the `plan_digest` of the plan
  that was edited, and `plan`. The run stays `PLANNED` with a new digest, which
  must be shown and approved in its turn.
- `reject`: `swarm_plan_reject` with `run_id`, the developer's `reason` and the
  shown `plan_digest`. The run ends `REJECTED`.

A `plan_changed` refusal means the plan is no longer the one shown, and
nothing was done: show the new plan and ask again — never resend with the new
digest unseen. `invalid_plan` means the edit was refused and the run kept its
plan. Report every error verbatim. In a terminal the developer runs
`sc plan approve <run>` themselves; it prints the plan and asks them to type
`approve`. This skill is not granted those commands.

## The views

Every `sc` VIEW is read-only towards the cluster. A view never writes to it,
never refreshes an account's credential and never cancels anything, so every
view below is always safe to run. The one part of `sc` that writes is
`sc account ...`, which changes an account in the pool; this skill is not
granted it (see the last section).

It reads **the developer's own deployment**, not one this plugin knows about:
whatever they configured at install (`/plugin configure sc@swarmcloud`), or a
context they added. `uv run sc whoami` prints which deployment, from where,
and who they are on it. `sc login`, `sc logout` and `sc context` change the
developer's own sign-in and which cluster every later call reaches — they are
theirs to run, so **tell them the command; never run it yourself**. `sc login`
opens a browser and waits for them.

This skill is granted each **view** by name, not `sc` as a whole. The sign-in
and context commands live under the same `sc` prefix, so a grant for all of
`sc` would let this session sign the developer out or move every later
dispatch to another cluster without asking.

## Which view

| Question | Command |
|---|---|
| what is the swarm doing? | `uv run sc` (or `uv run sc overview`) |
| how much quota is left? which account? | `uv run sc accounts` |
| why is my task queued? what is running? | `uv run sc agents` |
| what is the real ceiling? | `uv run sc capacity` |
| what did this agent produce? | `uv run sc task <id>` |
| is anything broken? | `uv run sc trouble` |
| which deployment, and who am I on it? | `uv run sc whoami` |
| what is my role here, and who holds which? | `uv run sc roles` |
| which endpoint, tenant, dispatch target and plugin version? | `uv run sc config` |
| why did this one task fail? | `uv run sc debug <id>` |
| which of my workflows are still running? | `uv run sc workflows` |
| what may I actually run? | `uv run swarm profiles` |

`sc config` shows where the dispatching tools send work — the session
`target` (cloud, local or hybrid) and which setting chose it — beside the
endpoint, the tenant, the plugin's version and the bridge's. A field it could
not read says `not read:` and why; report that, never a blank.

`sc debug <id>` is one task's diagnosis: its state and profile, every attempt
(generation, start and end, exit code, error), its last error, its newest
events and the tail of its newest attempt's log — all from the API, which
masks them, with the counts it masked. A line reading `withheld` is a string
the API did not say it masked, so it is not shown; say so, do not go looking
for it. A section that says `not read:` failed on its own; the rest of the
report is still good.

## Changing an account is the developer's to do

`uv run sc account pause <label>`, `resume`, `drain`, `remove` and `add` WRITE
to the shared account pool, and this skill is not granted them. Tell the
developer the command and let them run it: `drain` stops new assignments and
moves running agents to another account at their next turn boundary; `remove`
asks them to type the account's label back; `add` opens a browser for them to
sign in to Claude and asks them to paste a code, which only they can do.

The task view says what the agent produced before its code: each artifact by
name and size, the runner's own summary, the exit code, the duration, and the
inputs staged into it from upstream steps. "Artifacts are uploaded when the
attempt ends" means the task has not finished, not that it produced nothing.
Reading an artifact's content is the delegate skill's job, through the API,
which redacts it at read time; this skill lists and never fetches.

Add `--json` for the numbers, `--width N` to force a column count, `--ascii`
for a terminal without the bar glyphs. Put them **after** the view's name —
`uv run sc accounts --json`, `uv run sc overview --json` — because that is
what this skill is granted. A flag written before the view's name works too,
but asks the developer first.

`uv run swarm profiles` is the odd one out, listed here because it is read-only
and because it is the question people ask next. It reads the frozen catalogue
rather than the cluster, so it makes **no network call** and still answers when
nothing else does — which is exactly when someone is guessing at a profile name
because the API is unreachable. It shows no image and no command, deliberately:
a caller names a profile and the execution details follow from the name.

## Reading the marks — this is the part that matters

Three marks, and they are the same three `cs status` uses:

| Mark | Means | How to report it |
|---|---|---|
| `12%` | measured, recent enough to trust | quote it |
| `~12%` | the **last** measurement, too old to trust | say "projected", or give the reading's age. Never quote it as current |
| `—` | **not measured** | say "unknown". Never say zero, never say "fine" |

**Never turn an em dash into a number.** "This account has used none of its
quota" and "nobody has asked this account how much quota it has used" are
different claims, and collapsing them turns a dead poller into a healthy-looking
pool. If the operator needs the real figure and it is stale, say so and stop —
do not estimate it.

A section that reports it could not be read is a **finding**, not an absence.
Report it as such: "the account pool could not be read — utilisation is
unknown", not silence.

## Reading capacity

`BINDS ON` is the pool that will actually refuse the next task of that profile.
Admission is all-or-nothing across every pool a profile requires, so the ceiling
a profile feels is the **tightest** of them — not the global one, which is the
one people quote. `ROOM` is how many more **agents** of that profile fit before
the binding pool says no.

`UNITS` is that pool's capacity **units** in use out of its limit — not agents.
An agent takes its resource class's units, and the note under the table says
how many each class takes, read from the catalogue: a `browser` agent is more
than one unit, so `4/10` beside a `ROOM` of 3 is two browser agents, not four.
On a shared pool `UNITS` counts every tenant's work. The header's `tightest:`
names the profile with the fewest agents still to fit, and its pool's units.

A pool absent from the list is unlimited by construction and shows `∞`. A pool
whose `enabled` flag did not arrive shows `? not reported` rather than "open" —
a paused pool rendered as open is the one error this column exists to prevent.

`ROOM` is `—` whenever **any** required pool could not be graded — one that did
not report its ceiling, or did not report whether it is paused. Admission is
all-or-nothing, so one unknown pool makes the real room unknown, and it could
be 0. Do not fill that dash in from the other pools' numbers: they are the
pools that did not object.

## Reading accounts

Columns are `cs status`'s: `ACCOUNT | 5H USED | 7D USED | CLEARS | STATE`. Every
window percentage is **% used**, on every surface — the console and `sc` share
one polarity. `CLEARS` is when the **binding** window — the fullest one — rolls
over.

States: `available`, `paused`, `draining` (takes no new agents), `reauth needed`
(the credential is dead; only the quota-broker can fix it).

Every account in this pool is a **Claude subscription**. There is no other kind
and no choice to make about one, so there is nothing to ask the operator here.

Never ask for, echo, or store an account's credential. `sc` cannot show one:
the API shape carries no key material, not even a token's length, and `--json`
goes through the same allow-list the screen does. Rotating a credential
**revokes the previous one**, so quota-broker is the platform's single writer
for them — not `sc`, not swarm-api, and not this session.

## Exit codes

They are the machine-readable half of this surface, and every view uses them
the same way:

| Code | Means | Report it as |
|---|---|---|
| 0 | everything printed was read, nothing is down | healthy |
| 1 | something this view is about could not be read | "I could not read X" — an alert about the path to the cluster |
| 3 | it was all read, and something is **down** | "the cluster is broken, here is what" |

1 and 3 are different claims. A cluster nobody can reach is not a cluster that
is down, and reporting either as the other sends someone to the wrong place.

## When sc cannot connect

`sc` exits 1 and prints nothing to stdout, on purpose: a screen of dashes would
look like a reading. Run `uv run swarm doctor` — it reports which deployment it
resolved and from where, which auth tier this machine is on and what that tier
can reach, which is almost always the real answer.

**`sign-in required for <context>: run sc login`** is the commonest answer on a
new machine and is not a fault: the deployment takes a signed-in developer and
none is signed in. Tell the developer to run `uv run sc login` themselves (a
browser window opens), then try again. Do not treat it as the cluster being
unreachable, and do not look for another credential to use instead.

### "I cannot reach it" is not "it is down" — say the first one

This is the single most likely wrong report from this plugin, so it is worth
being exact. On a **team** deployment the API is behind IAP at a load balancer,
and `uv run swarm doctor` prints which door it used and what it presents:

```
front door  https://swarm.saga.xyz  (IAP; takes an OAuth ACCESS token)
api         UNREACHABLE
            GET /v1/tenants/me -> 401: ... Error code 900
```

Read the refusal, because the three of them mean three different things:

| What comes back | What it means |
|---|---|
| **401, IAP error code 900** | IAP did not accept the token at all. A gcloud *user* credential cannot pass: the deployment's IAP uses a Google-managed OAuth client, which admits only allowlisted programmatic clients. The developer signs in with `uv run sc login` (the deployment's Desktop OAuth client); CI sets `SWARM_IMPERSONATE_SA`. A 401 **after** `sc login` means the deployment has not allowlisted its own client — an operator's one-time step |
| **403 that NAMES the caller** | IAP **authenticated** you and the principal is not on the list. One `roles/iap.httpsResourceAccessor` grant away — `frontend_iap_members` in `terraform/bootstrap/terraform.tfvars`, which the owner applies and CI does not |
| **an HTML 404** | the wrong ADDRESS, not a missing route. Cloud Run's `*.run.app` hostname refuses everyone outside the VPC and renders the refusal as 404 |

None of those is the cluster being down. **Say "I cannot reach the API from
here, and this is why", never "SwarmCloud is broken"** — and if someone needs
the numbers now, the console at the front-door host is signed in to the same
API in a browser, where the session cookie is a credential this bridge does not
have and must not go looking for.

Exit code 1 is exactly this case, and exit code 3 is the other one. That is the
whole reason they are different numbers.

If accounts specifically are unreadable while everything else works, this
deployment's swarm-api has no `/v1/accounts` route: the pool is **unknown from
here**, not empty, and the cluster is otherwise fine — `sc` and `sc trouble`
say so as a note and still exit 0. There is no environment variable that makes
`sc` try a second host, and asking for one would be asking it to send a bearer
token minted for swarm-api somewhere else. An operator on the VPC who wants to
read quota-broker directly points `SWARM_API_URL` at it, so the token is minted
for the host that receives it.
