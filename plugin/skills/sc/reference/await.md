### `sc await` -- hear of finished work without a row

A session that follows no row learns of a finished task at its next `/sc
check`, up to thirty minutes later. Claude Code re-invokes a session when a
background shell command it started exits, so a command that waits for the
first change and then exits is a push path that costs no tokens while it
waits.

When a reply carries `await_in_background` -- `swarm_dispatch`,
`swarm_workflow` and `swarm_check` do, once -- run that exact command with
Bash in the background (`run_in_background: true`), unless one is already
running for the same work. Do not add arguments to it. It is read-only: it
dispatches, cancels and retries nothing.

What ends it, one line per changed item (id, title, state, pull request, the
refusal code and failing checks when known):

* a task finished in any end state, a refused merge included;
* a step waits for a person: a hand-off, owner questions, or a review-and-fix
  loop stopped for one;
* a task's agent asked questions it waits on;
* a task that can no longer be read.

A task already in one of those when it starts is not news. Starting to run is
not news either.

| Exit | Meaning | Then |
|---|---|---|
| 0 | something changed; its lines say what | report each line; for a person, `/sc inputs` or the `owner_questions` of `swarm_result` |
| 4 | the deadline passed with nothing changed | run the same command again in the background |
| 1 | nothing to wait on (no readable id, or `--mine` found nothing running) | say so; do not run it again |
| 2 | a usage error | report it |

The forms are `sc await <task_id|wf_id> ...` and `sc await --mine` (this
session's running work in this project, as `/sc check` scopes it). The
deadline is `--timeout <seconds>`: 1800 by default, never more than 3600. An
id it cannot read is one line saying so, and the rest are still waited on.
It holds one follow call at a time and reads no faster than a row's follow.
