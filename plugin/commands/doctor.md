---
description: Say what is wrong with a SwarmCloud deployment and how to repair it — this machine's tier and reach, and the install's stages read from its state; it writes nothing and never sees a value
argument-hint: "[--project <id>] [--env <name>]"
allowed-tools:
  - Bash(swarm doctor)
  - Bash(swarm install --status --json:*)
---

Diagnose a SwarmCloud deployment for the person who asked. Both halves only
read: nothing here writes, repairs or retries anything.

1. **This machine.** Run `swarm doctor` and show its output as it came: which
   deployment it resolved, which auth tier this machine is on and what that
   tier reaches. A failed check's explanation is the command's own, word for
   word, never paraphrased.
2. **The install**, when the person administers the deployment and has given
   its Google Cloud project id (a plain name; ask for it if it is missing,
   never guess it): run `swarm install --status --json --project <P>` (add
   `--env <name>` when given) and show each stage and its state. Exit 0 means
   every step is done; 3 means something is not met **or could not be
   checked**, which is never "fine"; a step the engine reports as drift names
   the step that repairs it.

**What to say.** For every finding, the step id, its state and the engine's
explanation exactly; then the repair, which is always the installer's own:
`/sc:install` (it re-checks every finished step and repairs drift), or the
command a finding names, handed to the person as it came. The console's
Admin › Setup Health page shows the same findings, with the hourly doctor's
results, once the deployment is up: point at it rather than restating it.
If the status read says it needs the SwarmCloud installer, show that
line and its install line exactly, and do only the first half.

**A value is never Claude's.** Neither half prints a value: the install state
records only that a secret version exists and its number. Never read a
secret, a key or a token to check it, and never ask the person to paste one
here; a missing value is repaired through `/sc:install`, at the engine's
hidden prompt in their own terminal.

**A refused permission.** If Claude Code's auto mode or a permission rule
refuses either command, never work around the refusal, never retry it under
another spelling and never run the probes by hand with other tools. Give the
person the exact command as a `!` line to type themselves, `! swarm doctor`
or `! swarm install --status --json --project <P>`, or ask them to add the
narrow allow rule `Bash(swarm doctor)` or `Bash(swarm install --status --json:*)`
to their settings, and stop until they have.
