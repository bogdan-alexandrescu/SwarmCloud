---
description: SwarmCloud cluster state — accounts, capacity, agents, trouble
argument-hint: "[accounts|agents|capacity|trouble|check|watch [interval]|layout [rows|pane|band]|task <id>|runs|run show <run>|plan show <run>|schedules|schedules show <name>|approvals|mcp-servers|roles]"
allowed-tools:
  - Bash(sc)
  - Bash(sc overview:*)
  - Bash(sc accounts:*)
  - Bash(sc agents:*)
  - Bash(sc capacity:*)
  - Bash(sc task:*)
  - Bash(sc trouble:*)
  - Bash(sc whoami:*)
  - Bash(sc runs:*)
  - Bash(sc run show:*)
  - Bash(sc plan show:*)
  - Bash(sc schedules)
  - Bash(sc schedules --json)
  - Bash(sc schedules show:*)
  - Bash(sc schedules preview:*)
  - Bash(sc approvals)
  - Bash(sc approvals --json)
  - Bash(sc mcp-servers:*)
  - Bash(sc roles)
  - Bash(sc roles --json)
  - Bash(sc roles list:*)
  - Bash(swarm doctor:*)
---

Run the requested view and show the operator its output:

```bash
sc $ARGUMENTS
```

With no arguments that is the overview. Other views: `accounts`, `agents`,
`capacity`, `task <id>`, `trouble`, `whoami`, and for issue runs (#454)
`runs`, `run show <run>` and `plan show <run>`.

If the arguments are `check`, `watch [interval]` or `watch off`, do **not**
run them here: `sc` has no such view. They are the `sc` skill's verbs -- one
`swarm_check` read and the attach of this project's running workflows, and
the CronCreate schedule `swarm_check_schedule` hands back -- so follow that
skill's `check` and `watch` sections.

If the arguments are `layout`, `layout rows`, `layout pane` or `layout band`,
do **not** run them here: `sc` has no such view. It is the `sc` skill's toggle
between the three layouts (its "Layouts" section; `layout band` saves and
queues no attach, since the band starts no rows): `/sc layout rows` or
`/sc layout pane` is one `swarm_layout {layout}` call, which saves the choice
for this project; bare `/sc layout` is `swarm_layout {}`, which says which is
set and saves nothing. After `/sc layout rows` or `/sc layout pane` saves,
then queue the re-attach: do the skill's `attach --all` in this same turn, so
rows in the new layout start beside the old ones, which keep running until
they end -- say both. With Claude Code's function hooks on, the plugin's pane
module answers `/sc layout` before this file is read, and the same run goes
on as `/sc attach --all` (one chat turn).

If the arguments are `attach --all --project`, it is the SessionStart hook's
ask, at session start only: the skill's `attach --all` with `project: true`
(its "Session start" section). `/sc check` and the watch stay this session's.

If the arguments are `inputs` or `answer <wf_id> <step_id> [file ...]`, do
**not** run them here: `sc` has no such view. `inputs` is one `swarm_inputs`
read of the workflow steps waiting for a person's answer; `answer` is
`swarm_workflow_input`, which asks the operator for the answer in this host's
own form and sends only what they type there and the files they tick. Follow
the `sc` skill's `inputs` and `answer` sections, and never pass the answer
yourself: the tool refuses a call that carries one.

When the person answers, in this session, a question a task's or a step's
agent asked -- from a row's result, `swarm_result`'s `owner_questions` or a
`questions.json` -- record each answer on the task with `swarm_task_answer`
`{task_id, question_index, choice?, text?}`, so the console shows it as
answered in Claude Code: `choice` is the task's own option label when they
picked one, `text` their words or what they chose. Only the person's own pick,
verbatim -- never an answer you chose yourself, and never one they did not
give. Follow the `sc` skill's "Answers given here" section.

If the arguments are `run --issue ...` or `plan approve|edit|reject ...`, do
**not** run them here: they create an issue run or act on its plan, so they
are not views. Use the `sc` skill's `run --issue` and `plan` verbs, which say
what they submit first and approve only the plan digest the operator was
shown, or tell the operator the command (`sc plan approve <run>` prints
the plan and asks them to type `approve`). `--auto-merge` merges the run's
pull request with the `merge` step (#295, enabled by contract request 47); the
API refuses it only while the `merge` profile is disabled, and prints why.

For schedules and the approval inbox (docs/schedules.md §7.3) the views are
`sc schedules` (the console's table), `sc schedules show <name>`
(one schedule and its last 10 firings), `sc schedules preview "<cron>"`
(the cron in words and its next slots) and `sc approvals` (what waits
for a person: firings, plans, merges, holds). `sc approvals` is also how
a session learns what is waiting: read it when the operator asks, or when
`sc schedules` shows a pending count -- there is no session loop to run.

If the arguments are `schedules new|pause|resume|run ...` or
`approvals approve|reject ...`, do **not** run them here: they create a
schedule, stop or start one, fire one, or decide an approval, so they are not
views and this command is not granted them. Use the MCP tools
(`swarm_schedule_create`, `swarm_schedule_pause`, `swarm_schedule_resume`,
`swarm_schedule_run_now`, `swarm_schedule_approve`, `swarm_schedule_reject`),
which say what they write first and approve only the digest the operator was
shown, or tell the operator the command: `sc approvals approve <id>` prints
the item and its digest and asks them to type `approve`. A schedule names a
type from the catalogue (`swarm_schedule_types`); no image or command is
accepted.

`sc mcp-servers` lists the MCP servers this workspace registered: the
names a claude-code or codex step may put in `inputs.mcp_servers`, each one's
auth mode (`dispatch` means a dispatch must bring that server's
`mcp_credentials` entry), allowed profiles and tools, and the egress addresses
its operator allowlists. It only reads. There is no `sc` command and no MCP tool
that registers a server or stores its credential: an admin registers one in the
console (Config › MCP servers). If the operator wants a server
registered, tell them that; never ask them for a credential to store.

`sc roles` (or `sc roles list`) shows the roles of the workspace
this session acts in: its default role, the people with an override, your own
role with where it came from and how you reach the workspace (`via`
`platform_admin` means you are acting as a platform admin in a group you are
not in, and every action there is audited), what each role permits, and the
recent role events. It only reads. `sc whoami` shows the same role on
its `role` line.

If the arguments are `roles set|clear|default ...`, do **not** run them: they
change who may act in the workspace, so this command is not granted them and
no MCP tool does it either. Tell the operator the command to run in their own
terminal -- `sc roles set <email> <role>`, `sc roles clear <email>` or
`sc roles default <viewer|member>` -- which asks them to type the email and the
role back at the terminal and sends nothing otherwise; or point them at the
console (Config › People and roles). A refusal (`role_admin_required`,
`role_unchanged`, ...) is printed with the server's code and sentence; report
it as it came.

If the arguments are `login`, `logout` or `context ...`, do **not** run them.
They are not views: they change the operator's own sign-in, or which cluster
every later call reaches, so they are theirs to run. Tell them the command
(`sc login` opens a browser and waits for them) and stop. This command
is granted each view by name, not `sc` as a whole, for exactly that reason, so
running one of those would stop at a permission prompt anyway.

If the arguments are `key set ...` or `keys set ...`, do **not** run them.
They store a TypeSafe key, and the key never passes through this session: tell
the operator to run `sc key set typesafe` (`--tenant` for the tenant's
key, `--stdin` to pipe it) in their own terminal, where it asks for the key at
a hidden prompt and prints only `stored: version N` -- or use the `sc` skill's
`keys set` verb, which hands them a one-time console link instead. Never ask
for the key.

Then read the output using the rules in the `sc` skill, which matter more than
the numbers themselves:

- a bare percentage (`12%`) is a measurement
- `~12%` is the last measurement and is **too old to trust** — say "projected"
  or "as of <the reading's age>", never quote it as the current figure
- `—` means **not measured**. It is not zero and it is not "fine"
- a section that says it could not be read is a finding, not an absence

Exit codes, and they mean the same thing on every view:

- **0** — everything the view printed was read, and nothing is down
- **1** — something the view is about could not be read. That includes `sc`
  printing nothing at all because it could not connect. It is an alert about
  the path to the cluster, not about the cluster. Report it as "I could not
  reach it, and this is why" — never as "SwarmCloud is down".
  `swarm doctor` prints which door it used and what that door takes,
  and the `sc` skill's table says what each refusal means
- **3** — it was all read, and something is **down**

One exception, and it is deliberate: a deployment whose swarm-api has no
`/v1/accounts` route is not a broken cluster, so `sc` and `sc trouble` report
it as a note and still exit 0. `sc accounts` exits 1, because that view is
about the pool and has nothing else to show.
