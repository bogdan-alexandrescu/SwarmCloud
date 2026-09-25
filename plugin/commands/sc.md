---
description: SwarmCloud cluster state — accounts, capacity, agents, trouble
argument-hint: "[accounts|agents|capacity|trouble|task <id>]"
allowed-tools:
  - Bash(uv run sc)
  - Bash(uv run sc overview:*)
  - Bash(uv run sc accounts:*)
  - Bash(uv run sc agents:*)
  - Bash(uv run sc capacity:*)
  - Bash(uv run sc task:*)
  - Bash(uv run sc trouble:*)
  - Bash(uv run sc whoami:*)
  - Bash(uv run swarm doctor:*)
---

Run the requested view and show the operator its output:

```bash
uv run sc $ARGUMENTS
```

With no arguments that is the overview. Other views: `accounts`, `agents`,
`capacity`, `task <id>`, `trouble`, `whoami`.

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
