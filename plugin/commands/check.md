---
description: Report this session's stuck or stale SwarmCloud work in this project and attach rows for what has none; read-only
argument-hint: "[repository: all] [tenants: all] [stale_minutes: N]"
allowed-tools:
  - CronList
  - CronCreate
---

Read-only: report and attach; never cancel, retry or re-dispatch.

1. Call `swarm_check` once, with no arguments (or what $ARGUMENTS names).
2. Follow its `how_to_read`: `flagged` first, in order (stuck before stale),
   each by its `say`, evidence, console link and `next`; then
   `awaiting_input`; then put each `owner_questions` question to the person
   with AskUserQuestion and record each pick with `swarm_task_answer`.
3. Make ONE `swarm_workflow_launch` call: `attach_with`'s args (with
   `new_only: true`) and `exclude` the ids of every [SwarmCloud] row live
   here; then one Workflow call per entry of its `launches`.
4. If that reply's `arm_watch` is set, arm the watch: CronList; unless a job
   whose prompt is `/sc:check` or `/sc check` exists, call
   `swarm_check_schedule` with `arm_watch.save_with.args`, then CronCreate
   with `arm_watch.args`, and say its `then_say`.
