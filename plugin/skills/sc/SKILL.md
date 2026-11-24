---
name: sc
description: The SwarmCloud front door, `/sc [verb]`. With no verb, cluster state -- the account pool and its 5-hour/7-day quota windows, pool ceilings and which pool binds each runner profile, agents running and queued, what is wrong. `status <id>`, `workflows`, `attach <wf_id>` and `attach --all` read and follow workflows and single tasks as live rows; `check` reports this session's stuck or stale work (`/sc:check`) and `watch [interval]` / `watch off` schedule it; `run <spec>`, `run --issue owner/repo#N` and `plan show|approve|edit|reject <run>` submit and act on plans (the verbs that write); `inputs`, `answer`, `build-board`, `layout`, `keys`, `keys set`, `secrets list|link` and `roles`. Use when asked "is my TypeSafe key set", "set my TypeSafe key", "what is the swarm doing", "how much quota is left", "why is my task queued", "is anything broken", "which account is nearly full", "where is my workflow", "what workflows are running", "show my workflow's rows again", before dispatching a long batch, to run a spec, or to plan and run a GitHub issue ("work on issue #N", "approve the plan").
argument-hint: "[status <wf_id|task_id> | workflows | attach <wf_id> | attach --all | attach --all --project | check | inputs | answer <wf_id> <step_id> [file ...] | build-board | watch [interval] | watch off | layout [rows|pane] | run <spec path|JSON> | run --issue owner/repo#N | runs | plan show|approve|edit|reject <run> | keys | keys set <provider> [--tenant] | secrets list [scope] [--repo owner/repo] | secrets link <scope> [name] [--repo owner/repo] [--actions [--env name]] | roles]"
arguments:
  - verb
  - target
allowed-tools:
  - verb
  - target
  - Bash(sc)
  - Bash(sc overview:*)
  - Bash(sc accounts:*)
  - Bash(sc agents:*)
  - Bash(sc capacity:*)
  - Bash(sc task:*)
  - Bash(sc trouble:*)
  - Bash(sc await:*)
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
  - Bash(swarm doctor:*)
  - Bash(swarm profiles:*)
  - Read
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
| no verb (empty) | today's cluster state: `sc`, read by the rules below | no |
| `status <wf_id\|task_id>` | one status read of that workflow or task | no |
| `workflows` | lists your tenant's workflows still running in SwarmCloud, marking `[this session]` the ones this Claude Code session started | no |
| `attach <wf_id>` | re-attaches live rows to a workflow running in SwarmCloud | no |
| `attach --all` | live rows for every running workflow THIS Claude Code session started AND every one still running for THIS PROJECT (this checkout's origin repository) that another session started (at most 100), and one per such running single task (at most 100); `max_live_rows` moves both, 1-200 | no |
| `attach --all --project` | what the SessionStart hook asks for: the same as the plain `attach --all` (`project: true` is still accepted and means the same). See "Session start" | no |
| `check` | which of THIS SESSION's workflows and tasks in THIS PROJECT are stuck or stale, stuck first, each with its reason, evidence, console link and next command: `swarm_check`. Then live rows for every running workflow and single task of the project that has none in this session | no (attaching submits nothing) |
| `watch [interval]` | a `check` every interval (default `30m`, at least `5m`) in this session, through Claude Code's CronCreate, remembered for this project so the next session re-arms it: `swarm_check_schedule`. It also arms itself when this session attaches or starts work (see "Arming the watch") | schedules a prompt in this session; nothing in SwarmCloud |
| `build-board` | refreshes this project's build board, `.claude/build-board/index.html`, and opens it in the browser: `swarm_build_board` | writes the local page only (git-ignored); nothing in SwarmCloud |
| `inputs` | the workflow steps of your tenant waiting for a person's answer: `swarm_inputs` | no |
| `answer <wf_id> <step_id> [file ...]` | the developer answers that step in this host's own form: `swarm_workflow_input`. You never write the answer | **yes**, what the developer typed |
| `watch off` | cancels this session's watch and saves "off" for the project, so nothing arms it again until `watch` | no |
| `layout [rows\|pane\|band]` | how this project follows SwarmCloud work: `/sc layout rows` (today's /workflows rows), `/sc layout pane` (the SwarmCloud pane plus ONE quiet row per workflow) or `/sc layout band` (a strip above the prompt and a summary line below it, and no rows at all); bare, says which is set: `swarm_layout`. See "Layouts" | saves a local preference; nothing in SwarmCloud |
| `run <spec path\|JSON>` | submits a workflow spec, and says so first | **yes** |
| `run --issue owner/repo#N [--plan auto] [--auto-merge] [--fix-rounds N]` | plans a GitHub issue (one planner task), and says so first | **yes** |
| `runs` | lists your tenant's issue runs | no |
| `plan show <run>` | shows a run's plan and its digest | no |
| `plan approve\|edit\|reject <run>` | acts on that plan, with the digest the developer was shown | **yes** |
| `keys` | whether a TypeSafe key is set for you and for your tenant, and which one your tasks would use: `swarm_keys`. Never a value | no |
| `keys set <provider> [--tenant]` | a one-time console link where the developer pastes the key themselves: `swarm_key_set_link`. The key never passes through this session | mints a link; stores nothing |
| `secrets list [workspace\|personal\|repository] [--repo owner/repo]` | Config › Secrets' names and status for one scope (default `workspace`): `swarm_secrets`. Never a value | no |
| `secrets link <workspace\|personal\|repository> [name] [--repo owner/repo] [--actions [--env name]]` | a one-time console link where the developer types a secret's value themselves: `swarm_secret_set_link`, or `swarm_actions_secret_link` with `--actions`. No tool takes a value | mints a link; stores nothing |
| `roles` | the roles of the workspace this session acts in: its default, the overrides, your own role and how you reach it, the recent role events: `swarm_roles` | no |
| `roles set\|clear\|default ...` | not run here: tell the developer the `sc roles` command for their own terminal (see `roles`) | no (the developer's, typed) |
| a view name (`accounts`, `agents`, `capacity`, `trouble`, `task <id>`, `whoami`, `config`, `debug <id>`) | that view, as in "Which view" below | no |

Anything else: say which verbs exist and run nothing. `/sc:swarmcloud` keeps
working unchanged; these verbs are a shorter way in, not a replacement.

### Each verb's rules are in its reference file

This file holds the verb table, `check`, `watch` and arming the watch. Every
other verb's rules live in a file under `reference/` in this skill's base
directory: before acting on that verb, Read its file (and only its file), and
follow it exactly as if it were written here.

| Verb | Read first |
|---|---|
| no verb, a view name, exit codes, "cannot connect" | `reference/views.md` |
| `status` | `reference/status.md` |
| `workflows`, `attach <wf_id>`, `attach --all [--project]`, the SessionStart ask | `reference/attach.md`, then `reference/titling.md` before any Workflow call |
| `run <spec>` | `reference/run.md`, then `reference/titling.md` |
| `run --issue`, `runs`, `plan show\|approve\|edit\|reject` | `reference/issue-runs.md` |
| `inputs`, `answer`, a fix loop that waits, answering an agent's question | `reference/inputs.md` |
| `build-board` | `reference/build-board.md` |
| `layout [rows\|pane\|band]` | `reference/layouts.md` |
| `keys`, `keys set` | `reference/keys.md` |
| `secrets list`, `secrets link` | `reference/secrets.md` |
| `roles`, `roles set\|clear\|default` | `reference/roles.md` |
| the full `check` rule table and every field of its reply | `reference/check.md` |
| a reply's `await_in_background`: the background wait that ends on news | `reference/await.md` |

### `check`

READ-ONLY: it reports and attaches, and never cancels, retries, releases or
re-dispatches anything (owner, 2026-10-14: "report only"). `check` is the
plugin's `/sc:check` command, and `/sc check` routes to it: follow that
command. In short:

1. ONE `swarm_check` call with no arguments. It reads only the running work
   that this Claude Code session started in THIS checkout's `origin`
   repository; another session's work is counted in `session.left_out` and
   never reported as stuck. Its reply carries the reading rules itself:
   `how_to_read` once, and a `say` and `next` on every flagged item. Report
   `flagged` first, in its order (STUCK before STALE), each by its `say`,
   evidence, console link and `next`; then `awaiting_input`, as information
   and never as stuck; then put each `owner_questions` question to the person
   with AskUserQuestion and record each pick with `swarm_task_answer`.
   `unchecked` was NOT checked: say so, never call it fine.
2. ONE `swarm_workflow_launch` call with the reply's `attach_with` args
   (`attach: "all"`, `new_only: true`) and `exclude`: the ids of every
   [SwarmCloud] row live in this session; then one Workflow call per entry of
   its `launches`. Each `rowless_tasks` entry: say its `line` and make its
   Workflow call (`launch`).
3. When step 2 launched at least one run, the mandatory final step:
   [Arming the watch](#arming-the-watch), with its reply's `arm_watch`.
   `nothing_running` means nothing of the project runs: say so in one line,
   and LEAVE the watch armed. A check never cancels its own job; only
   `watch off` does.

The rule table, the fields and what each one is NOT are in
`reference/check.md`.

### `watch [interval]`

A `check` every interval, in this session. Call `swarm_check_schedule` with
`{interval: "<interval>"}` (none given: `30m`; below `5m` it is raised to
`5m`; an interval a cron expression cannot say, such as `45m`, is rounded and
the reply says so). It saves the preference for this project and schedules
nothing itself: first remove any older watch -- CronList, then CronDelete
every job whose prompt is `/sc:check` or `/sc check` (a watch armed before
0.5.88) -- then make the reply's `arm_with`
call, CronCreate `{cron, prompt: "/sc:check", recurring: true}`, and say the
interval, the cron and that it runs only while this session is open and idle
(a recurring job also ends after 7 days). If CronCreate cannot be called here,
tell the developer to run the reply's `without_cron` command instead, which
for the default is `/loop 30m /sc:check`, and do not claim a watch is armed.
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
(one task or `tasks`: the reply carries `arm_watch` beside its `rows`,
whose mode is the attach's: its own `rows` argument, else the project's layout, else the plugin's `rows`),
`attach <wf_id>` even of a workflow that has already finished, `attach
--all`, and a `check` that attached something.
Otherwise:

1. CronList. If a job whose prompt is `/sc:check` or `/sc check` (armed
   before 0.5.88, and still working) already exists, the watch
   is armed in this session: stop here -- never a second job, and do not
   replace it (that is `/sc watch`'s to do, by hand).
2. Call `swarm_check_schedule` with `arm_watch.save_with`'s args (the saved
   interval, else `30m`), which remembers it for the project.
3. Make the `arm_watch` call: CronCreate with its `args`, `{cron, prompt:
   "/sc:check", recurring: true}`, and say its `then_say` in one line.

If CronCreate cannot be called here, say `swarm_check_schedule`'s
`without_cron` command (`/loop 30m /sc:check`) once and do not claim a watch is armed. Once armed,
later verbs in this session find it at step 1 and make no call.

### `watch off`

Call `swarm_check_schedule` with `{off: true}`, then CronList and CronDelete
every job whose prompt is `/sc:check` or `/sc check`. Say how many were
cancelled; a `/loop ... /sc:check` started by hand stops with Esc, which only the
developer can press. The off is SAVED for the project, not forgotten: no
attach, run or new session arms the watch again until `/sc watch`, and every
`arm_watch` is null meanwhile.
