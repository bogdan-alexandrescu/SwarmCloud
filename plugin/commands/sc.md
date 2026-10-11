---
description: SwarmCloud cluster state — accounts, capacity, agents, trouble
argument-hint: "[accounts|agents|capacity|trouble|task <id>|runs|run show <run>|plan show <run>|schedules|schedules show <name>|approvals]"
allowed-tools:
  - Bash(uv run sc)
  - Bash(uv run sc overview:*)
  - Bash(uv run sc accounts:*)
  - Bash(uv run sc agents:*)
  - Bash(uv run sc capacity:*)
  - Bash(uv run sc task:*)
  - Bash(uv run sc trouble:*)
  - Bash(uv run sc whoami:*)
  - Bash(uv run sc runs:*)
  - Bash(uv run sc run show:*)
  - Bash(uv run sc plan show:*)
  - Bash(uv run sc schedules)
  - Bash(uv run sc schedules --json)
  - Bash(uv run sc schedules show:*)
  - Bash(uv run sc schedules preview:*)
  - Bash(uv run sc approvals)
  - Bash(uv run sc approvals --json)
  - Bash(uv run swarm doctor:*)
---

Run the requested view and show the operator its output:

```bash
uv run sc $ARGUMENTS
```

With no arguments that is the overview. Other views: `accounts`, `agents`,
`capacity`, `task <id>`, `trouble`, `whoami`, and for issue runs (#454)
`runs`, `run show <run>` and `plan show <run>`.

If the arguments are `run --issue ...` or `plan approve|edit|reject ...`, do
**not** run them here: they create an issue run or act on its plan, so they
are not views. Use the `sc` skill's `run --issue` and `plan` verbs, which say
what they submit first and approve only the plan digest the operator was
shown, or tell the operator the command (`uv run sc plan approve <run>` prints
the plan and asks them to type `approve`). `--auto-merge` is visible but
disabled: the API refuses it until #295.

For schedules and the approval inbox (docs/schedules.md §7.3) the views are
`uv run sc schedules` (the console's table), `uv run sc schedules show <name>`
(one schedule and its last 10 firings), `uv run sc schedules preview "<cron>"`
(the cron in words and its next slots) and `uv run sc approvals` (what waits
for a person: firings, plans, merges, holds). `uv run sc approvals` is also how
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

If the arguments are `login`, `logout` or `context ...`, do **not** run them.
They are not views: they change the operator's own sign-in, or which cluster
every later call reaches, so they are theirs to run. Tell them the command
(`uv run sc login` opens a browser and waits for them) and stop. This command
is granted each view by name, not `sc` as a whole, for exactly that reason, so
running one of those would stop at a permission prompt anyway.

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
  `uv run swarm doctor` prints which door it used and what that door takes,
  and the `sc` skill's table says what each refusal means
- **3** — it was all read, and something is **down**

One exception, and it is deliberate: a deployment whose swarm-api has no
`/v1/accounts` route is not a broken cluster, so `sc` and `sc trouble` report
it as a note and still exit 0. `sc accounts` exits 1, because that view is
about the pool and has nothing else to show.
