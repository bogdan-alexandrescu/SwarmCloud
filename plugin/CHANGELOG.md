# Changelog: the sc plugin

Each entry is a version of `plugin/.claude-plugin/plugin.json`. The release
tags the swarm-mcp bridge `sc-v<version>` only when that version moves
([docs/ci.md](../docs/ci.md#the-plugins-bridge-tag-is-made-by-the-release)),
so a change to the plugin or its bridge reaches a session only with an entry
here.

## 0.5.75 (2026-10-31)

* `swarm_check` lists a parked task's blocking owner questions first, marked
  `blocking: true` and read again on every check, and names the task as
  parked on its agent's questions instead of on a sign-in, so the question
  that holds the task is the first thing you see (#2860).
* A workflow spec of any size launches through `swarm_workflow_launch`: its
  args carry only the spec's handle, never the spec itself, and
  `swarm_workflow_spec` answers with an outline and, on request, one step's
  prompt in a window (#2875).
* `/sc:install` and `/sc:team` show a wait's `detail` each time the
  installer's countdown sends a new one, and show `wait_failed`, which ends a
  wait at once, with its `detail` word for word.
