<!-- The agent-facing guide to child tasks, exactly as the worker writes it into
$SWARM_CHILDREN/README.md for an attempt that has a child path
(apps/agent-worker/agent_worker/children.py AGENT_GUIDE; the CLI runners add
one prompt line pointing at it). tests/unit/worker/test_child_tasks_worker.py
holds this copy equal to that constant: change both or neither. The design and
its reasons are docs/design/child-tasks.md. -->

# Child tasks: how this agent submits helpers

This task may submit CHILD TASKS: other agents that run in their own
containers, on this task's tenant, while you work. `$SWARM_CHILDREN` is this
directory. There is no network call to make and no credential to use: you
write files here, and the platform's worker beside you submits them.

## Submit a child

Write ONE JSON object per child to `requests/<request_id>.json`, atomically:
write `requests/<request_id>.tmp`, then rename it to `.json`. `<request_id>`
is yours to choose, `[a-z0-9-]{1,64}`, and is the child's idempotency key:
writing the same id again never makes a second child.

    {
      "request_id": "split-tests-2",
      "runner_profile": "claude-code",
      "input": {"prompt": "Write unit tests for src/parser.py"},
      "resource_class": "standard",
      "timeout_seconds": 1800,
      "repository_ref": "main"
    }

`runner_profile` is required and names a profile, never an image or a
command. `input` is the child's input, `{"prompt": ...}` for the CLI agents.
`resource_class`, `provider`, `model`, `timeout_seconds` and `repository_ref`
are optional; nothing else is accepted. A child works on this task's
repository, runs no longer than this task, and is at most 256 KiB as a file.

## Read the answer

Within about ten seconds the worker writes `responses/<request_id>.json` and
removes the request:

    {"task_id": "task_...", "request_id": "split-tests-2"}

or a refusal:

    {"refused": {"code": "child_fan_out_exceeded", "message": "...", "retryable": false}}

`retryable: true` (`api_unavailable`) means write the same request again with
the same `request_id`. Anything else will be refused again as written. Limits:
16 children per task across all its attempts; a child cannot submit children
of its own; about four requests are answered per ten seconds.

## Wait for the children

There is no way to watch a running child. To wait, first write everything you
will need to continue into your working files (your process ends; the
platform checkpoints the work directory and restores it), then create the
empty file `await` here and EXIT 0. The task gives its slot back and sleeps
until every child has ended. Then you are started again with the same input,
and `results/children.json` exists -- that is how you know you are resuming:

    {"listing": "complete",
     "children": [{"task_id": "task_...", "request_id": "split-tests-2",
                   "state": "SUCCEEDED", "end_cause": null,
                   "parent_attempt_id": "att_...", "outputs": "staged",
                   "files": ["task_.../notes.md"]}]}

Each SUCCEEDED child's artifacts are under `results/<task_id>/`; `files` lists
them relative to `results/`. `outputs` is `staged`, `none`, or `unavailable`
with a `reason` (an expired or over-cap artifact: still information, not a
failure). A child that failed or was cancelled is listed with its `state` and
`end_cause`; deciding what to do about it is yours. `listing: unavailable`
means the platform could not list them this time.

If you exit 0 while children are still running, you are made to wait anyway:
a task never succeeds over running children. If you exit non-zero, this
attempt fails as usual and the children keep running for the next attempt.
