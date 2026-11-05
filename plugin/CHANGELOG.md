# Changelog: the sc plugin

Each entry is a version of `plugin/.claude-plugin/plugin.json`. The release
tags the swarm-mcp bridge `sc-v<version>` only when that version moves
([docs/ci.md](../docs/ci.md#the-plugins-bridge-tag-is-made-by-the-release)),
so a change to the plugin or its bridge reaches a session only with an entry
here.

## 0.5.80 (2026-11-05)

* A `/sc:swarmcloud` or `swarm_workflow` spec can set a review step's gate
  (part of #2688): a step's `review_gate` is `false`, which switches the gate
  off for that review, or a block of `min_lines`, `risk_paths` and
  `requirements_check`, and is sent as the step's `metadata.review_gate`.
  swarm-api has taken it since the gate shipped, but the bridge refused the
  key, so a spec could not run a mock-up review the gate skipped. A malformed
  block is refused before anything is submitted, by swarm-api's rules
  (`validation.resolve_review_gate`). `metadata` itself is still refused with
  the same sentence: a spec names a runner profile and never sends an image,
  a command or a resource spec (invariant 10).

## 0.5.79 (2026-11-04)

* The bridge survives a plugin update that removes the folder it was started
  from (part of #2688). When `SWARM_SC_PLUGIN_ROOT`'s `workflows/run.js` is
  gone and the root sits in the plugin cache (`<cache>/swarmcloud/sc/<version>`),
  `swarm_workflow_launch` and `swarm_dispatch`'s rows use the newest version
  beside it that has run.js (versions compared as numbers: 0.5.10 is newer
  than 0.5.9), and the reply's `plugin_root_note` names both versions and asks
  for `/reload-plugins`. Only a sibling of the configured root is ever used. On
  4 November an update from 0.5.74 to 0.5.76 had removed a long-running
  session's root, and every titled launch failed while dispatch kept working.
* With no usable sibling the error stays, and now says a plugin update may
  have removed the folder and that `/reload-plugins` is the fix.

## 0.5.78 (2026-11-03)

* Every agent the plugin ships -- `sc:workflow`, `sc:step`, `sc:task`,
  `sc:summary`, `sc:finish`, `sc:wait` and `sc:remote` -- opens with "Chat is
  context, never your task" (#3117): its task is fixed by its prompt and the
  arguments it carries, the person's chat text is context only and never a
  reason to skip, alter or stop the submit, the follow, the dispatch or the
  wait, and a question in chat is the session's to answer. A chat question
  relayed to the inner agents as a "relayed user request" had made two
  reviewed launches end NOT_SUBMITTED and two attach rows stop early.
* The SUBMIT prompt's `request:` line names that framing and says it does not
  win over the computed SUBMIT.

## 0.5.77 (2026-11-02)

* The bridge's catalogue offers `codex` (contract request 130, part of
  #2688): `swarm_dispatch` and a `swarm_workflow` step accept it instead of
  refusing it as disabled, and `swarm_profiles` lists it available. It runs on
  the tenant's OpenAI key, else on a ChatGPT plan leased from the account pool;
  `claude-code` stays the default.
* The `delegate` skill and the plugin README say so, and name `post-verdict`
  as the example of a known profile the catalogue refuses.

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
