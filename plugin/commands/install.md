---
description: Install SwarmCloud into your own Google Cloud project, or resume an install wherever it stopped — the installer engine's face in Claude Code, reading only its JSON events and never a value
argument-hint: "[status | dry-run] [--project <id>] [--domain <host>] [--version <release>] [--region <region>] [--env <name>]"
allowed-tools:
  - Bash(swarm install --json-events:*)
  - Bash(swarm install --status --json:*)
---

Drive the SwarmCloud installer (docs/design/day-one-install.md §2) for the
person who asked. The engine does the work; this command runs it, reads its
events and says what they mean. It never runs a Google Cloud, Terraform or
Kubernetes command itself: every step is the engine's, so every step is
checked, recorded and resumable.

The engine is the `swarm` command's `install` verb, which the release home's
`install.sh` puts on the PATH. If running it says that it needs the
SwarmCloud installer, show that line exactly as it came, with the install
line it names, and stop.

**What it needs.** The Google Cloud project id, always. For a new install
also the hostname the console will answer on (`--domain`), the release
(`--version`) and, if not the default, the region and the environment name.
These are plain names, not secrets: take them from the arguments, or ask for
the missing ones. Never invent one.

1. **Where it stands.** Run `swarm install --status --json --project <P>`
   and show each stage and its state. Exit 0 means every step is done (show
   the ready screen it carries); 3 means something is not met or could not be
   checked, which is not "fine". If the arguments were `status`, stop here.
2. **Dry run**, if the arguments were `dry-run`: run
   `swarm install --json-events --dry-run --project <P>`, show each unmet
   step's plan, and stop. It writes nothing.
3. **Run.** For a new install,
   `swarm install --json-events --project <P> --domain <H> --version <V>`
   (add `--region` and `--env` when given); to resume one, the same line with
   only `--project <P>`: the engine re-checks every finished step and goes on
   at the first that is not met. A stage can wait up to an hour on a
   certificate, so run it in the background (the Bash tool's
   `run_in_background`) and read its output as it arrives. Run one at a time:
   the engine's lease refuses a second.

**Reading the events.** Each line is one JSON object,
`{"v": 1, "event": <kind>, "at": <seconds>, ...}`. Show `stage_start` as a
heading; `step_skip` and `step_done` as one short line each; `step_plan` and
`step_apply` with their step id; a `wait_countdown` with its `reason`, the
seconds left and what is `expected`; a `wait_gave_up` with its `look_at`
exactly; `step_drift` with the step that will repair it; `step_failed` and
`stage_refused` with their `message` word for word, never paraphrased. A
human step (`step_paused` with any other reason) carries the instructions
and the deep link: show them exactly, and when the person says it is done,
run the same line again; the engine's own check decides, never the
person's word. `run_end` carries the `outcome` and the `exit_code`. A line
that is not JSON, or a kind this list does not name, is shown as text. When
the CLI hands off to the console (Admin › Setup), show the URL it prints
exactly: the people part (the GitHub App, the Claude accounts, the first team)
is finished there, and the run goes on waiting for it.

**Exit codes.** 0 done; 1 a step failed, and its `message` says how to go
on; 2 a usage error, so show the usage line and fix the arguments; 3 not met
or not checked; 4 paused on a human step or a wait past its bound, resumed by
running the same line again; 5 refused (a typed confirmation is missing, a
downgrade, or a deny-listed resource). On a 5, show the refusal and stop:
never retry it with another project, another flag or another command.

**A value is never Claude's.** The engine puts no value in any event; it
generates the platform's keys itself and writes them straight to Secret
Manager. A value a person holds is typed into the engine's hidden prompt in
their own terminal, or into a one-time console link, and never into this
chat. Never put one in a flag or an argument. If the person pastes one here,
do not repeat it or use it, and tell them it is now in the transcript and
should be replaced.

**A secret step: `needs_terminal`.** Under Claude Code the engine has no
terminal to prompt on, so a step that needs a value or a typed answer pauses
with a `step_paused` event whose `reason` is `needs_terminal` and whose
`command` is the line to hand on, today `! swarm install`. Give the
person that `command` exactly as it came, and say they type it at the prompt
themselves: the engine then asks at a hidden prompt on their terminal. Never
ask for the value, never read it from anywhere and never pipe it into the
engine. When they say it is done, run the same line again to carry on.

**A refused permission.** If Claude Code's auto mode or a permission rule
refuses the command, do not look for another way to run it: never work
around a refusal, never retry it under another spelling and never run the
engine's steps by hand. Give the person the exact command as a `!` line to
type themselves, for example
`! swarm install --json-events --project <P>`, or ask them to add the narrow
allow rule `Bash(swarm install --json-events:*)` (for the status read,
`Bash(swarm install --status --json:*)`) to their settings, and stop until
they have.
