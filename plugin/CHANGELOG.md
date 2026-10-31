# Changelog: the sc plugin

Each entry is a version of `plugin/.claude-plugin/plugin.json`. The release
tags the swarm-mcp bridge `sc-v<version>` only when that version moves
([docs/ci.md](../docs/ci.md#the-plugins-bridge-tag-is-made-by-the-release)),
so a change to the plugin or its bridge reaches a session only with an entry
here.

## 0.5.76 (2026-10-31)

* The bridge's catalogue holds `claude-code-lightpanda` (contract request
  127, #2918): claude-code's runner on the `agent-runtime-lightpanda` image,
  a headless browser that runs JavaScript and draws no pixels, on the
  `standard` class with no live view. `swarm_dispatch` and a `swarm_workflow`
  step accept it by name instead of refusing it as "no runner profile called
  'claude-code-lightpanda'", and `swarm_profiles` lists it from the bridge's
  own copy when the platform's `/v1/runtimes` cannot be reached, as
  `swarm profiles` always does.
* The plugin README describes `claude-code-lightpanda` and its inputs,
  `issue` and `mcp_servers`, as it does `claude-code`'s.

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
