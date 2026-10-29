---
description: Add a team to your SwarmCloud deployment from a Google group — the installer's `team add`, read through its JSON events; the team's git credential goes through a one-time console link, never through Claude
argument-hint: "add <group-email>"
allowed-tools:
  - Bash(swarm team add --json-events:*)
---

Add a team for the person who asked, through the installer's `team add`
(docs/design/day-one-install.md §2.1), the same path as the console's team
creation: the group is probed, the team registered, its namespace, quota and
worker identity applied, and a smoke agent run. This command runs it and
reads its events; it never registers anything itself.

**What it needs.** The Google group's email, a plain name: take it from the
arguments, or ask for it. Run
`swarm team add --json-events <group>`. If it exits 2, show the usage line it
printed exactly: it names what is missing (for example `--project`), so add
that plain name and run it again. If the installer on this machine has no
`team` verb, or running it says that it needs the SwarmCloud installer, show
that line and the install line it names exactly, and stop. Run it in the
background (the Bash tool's `run_in_background`), because the smoke agent
waits for a pool account, and read its output as it arrives.

**Reading the events.** Each line is one JSON object,
`{"v": 1, "event": <kind>, "at": <seconds>, ...}`, the same events as
`/sc:install`: show `step_done` and `step_skip` as one short line each, a
`wait_countdown` with its `reason`, the seconds left and what is `expected`,
and `step_failed`, `stage_refused` and `wait_gave_up` with their `message`
and `look_at` word for word. `run_end` carries the `outcome` and the
`exit_code`: 0 the team is ready and its smoke is green; 1 a step failed and
its `message` says how to go on; 3 not met or not checked; 4 paused, resumed
by running the same line again; 5 refused, so show it and stop, never retry it
with another group or flag. A line that is not JSON is shown as text.

**A value is never Claude's.** The team's git credential is written only
through the one-time console link the engine prints: show the link exactly
and tell the person to open it in their browser, where the page stores the
value server-side. Never put a value in a flag or an argument. If the person
pastes one here, do not repeat it or use it, and tell them it is now in the
transcript and should be replaced.

**A step that needs a terminal: `needs_terminal`.** A step that needs a
value or a typed answer pauses with a `step_paused` event whose `reason` is
`needs_terminal` and whose `command` is the line to hand on, today
`! swarm install --resume`. Give the person that `command` exactly as it
came, to type at the prompt themselves: the engine then asks at a hidden
prompt on their terminal. Never ask for the value, never read it from
anywhere and never pipe it into the engine. When they say it is done, run the
same line again to carry on.

**A refused permission.** If Claude Code's auto mode or a permission rule
refuses the command, never work around the refusal, never retry it under
another spelling and never run the team's steps by hand. Give the person the
exact command as a `!` line to type themselves,
`! swarm team add --json-events <group>`, or ask them to add the narrow allow
rule `Bash(swarm team add --json-events:*)` to their settings, and stop until
they have.
