### `check`: the rule table and every field of the reply

READ-ONLY: it reports and attaches, and never cancels, retries, releases or
re-dispatches anything (owner, 2026-10-14: "report only"). Two calls:

1. ONE `swarm_check` call with no arguments: it reads the running workflows
   and single tasks that this Claude Code session started -- another
   session's work in the same tenant, and untagged work, is left out
   (`session.left_out` counts it; never report it as stuck) -- and that clone
   THIS checkout's `origin` repository (`repository: "all"` for every repository; `tenants:
   "all"` for every tenant you may choose; `stale_minutes` moves the 20-minute
   window). Report `flagged` FIRST, STUCK before STALE as it is ordered, one
   line each that starts with the workspace: `[<workspace>] <workflow_id or
   task_id> · <kind> · <reason>`, then the `evidence` that supports it (the
   heartbeat and last event, the age and whether it is exact or "at least",
   the binding pool and where it was read), its console link as served, and
   its `next` -- a `/sc status <id>` to run, or the PR link to open. The rules:

   | `rule` | flags | is NOT flagged |
   |---|---|---|
   | `no_heartbeat` | RUNNING/STARTING with no heartbeat and no event for more than the window | a beat or an event inside it |
   | `waiting_for_capacity` | READY/QUEUED longer than the window, with the pool that binds it | a task that entered the state recently. It holds nothing while it waits (invariant 1): report it as waiting, not as broken |
   | `parked` | HUMAN_REQUIRED (only a person promotes it); CI_PENDING over 90 min; DEPENDENCY_INCOMPLETE whose dependency failed; a fix loop parked HUMAN_REQUIRED on any phase (see "A fix loop that waits for you") | any other park reason: a quota window or a retry backoff is the platform waiting by design; a fix loop with a round running (FIX_ROUNDS_RUNNING), whatever its age -- the 90-minute rule is CI_PENDING's alone |
   | `merge_train_stalled` | a merge step waiting on the merge train (CI_PENDING, code `merge_train_waiting`) more than 60 min, aged from the train's start the API serves (`waiting.detail.train_since`), else from the first train park of its unbroken run (`evidence.in_state_since`, exact or "at least"; `evidence.age_source` says which); the train boards only pull requests whose checks are all green | a train park under 60 min; a train park never counts toward the CI_PENDING 90 min. Name `evidence.failed_checks` (the PR's failed or cancelled checks at its head, read from GitHub's public API); when it is null, say the checks were NOT read (`checks_unread_because` says why). `next` is the PR link |
   | `merge_refused` | a merge step FAILED with `head_moved`, `merge_conflict` or `checks_failed`, and its PR is still open | any other failure, and a PR that has since MERGED or CLOSED (the check reads the PR's state from GitHub's public API). When `pull_request_state` is null, say the PR's state was NOT read (its `pull_request_state_unread_because` says why), and give its link |
   | `state_disagrees` | a workflow whose stored state and derived state disagree on whether it is finished (the stale RUNNING cache) | a stored QUEUED under a derived RUNNING: the cache lags between reads |
   | `awaiting_input` | STUCK only when an AWAITING_INPUT step is still parked more than two ticks (2 min) past its `expires_at`: the expiry sweep is not running | a wait inside its window, or just past it: listed under `awaiting_input`, below |
   | `deploy_behind` | the deployment is behind the default branch, which has held a commit it lacks more than 60 min. The deployed commit is `GET /v1/version`'s `git_sha`, lifted to the newest successful release run on the default branch after it (an incremental build reuses unchanged images; `evidence.release_reaches` says so); behind is aged from the OLDEST commit it lacks. One item for the check, naming no task: report the deployed commit and its age, the commits behind, and the release run's conclusion and failed jobs; a field reading 'not read: ...' was NOT read -- say so, never "no failed jobs"; `next` is the release run | behind 60 min or less; a deployment that is current; a deployment GitHub does not know in this repository (built from another one: not compared). A read that fails is under `unchecked` as "not read", with its reason: say the deployment was NOT checked, never that it is current |
   | `plugin_files_missing` | this session's sc plugin version folder, or its workflows/run.js, is gone (a plugin update removed the folder this session loaded): titled launches fail and the plugin's agent types are lost. One item for the session, naming no task; `next` is `type /reload-plugins in this session` | both present; a bridge started with no plugin root (`SWARM_SC_PLUGIN_ROOT` unset: the repo-local bridge, or one started by hand); another version folder beside it is never checked, since only a reload gives the session its agents back |

   `awaiting_input` (beside `flagged`, never inside it) lists the steps
   waiting for a member of their tenant to answer, as information and NOT as
   stuck: one line each, `[<workspace>] <workflow_id> · <step_id> · <reason>`
   ("waits for a member of eng since 14:02 UTC, expires in 3 h 0 m"), and its
   `next` -- `/sc answer <wf_id> <step_id>` or its console link. A waiting
   step holds no lease and no capacity (invariant 1). HUMAN_REQUIRED stays a
   STUCK `parked` flag.

   `owner_questions` (beside `flagged`, never inside it, and never counted as
   stuck or stale) lists this session's tasks in this project, finished in
   the last 7 days, whose agent asked the owner questions (its
   `questions.json`) that have no answer yet: each entry's `workspace`,
   `workflow` and `step_id` (or a single task's `title`), `task_id`,
   `console`, `open` (how many) and the open `questions` whole -- each
   `{index, question, options, recommended, context}`. It is how a question
   from a row nobody watched end, or one that ended before a compaction, is
   raised again. When it holds any, put EACH question to the person in this
   turn, BEFORE any other report, with AskUserQuestion: the question, its
   options as given (label and description), the `recommended` one marked
   "(recommended)", its `context` beside it. Then record each pick with
   `swarm_task_answer {task_id, question_index: <its index>, choice}` (see
   [Answers given here are recorded on the task](#answers-given-here-are-recorded-on-the-task)).
   Never pick an answer yourself, and never leave the questions only in the
   check's answer. A question whose read failed is under `unchecked` with
   its reason: say so and give its console link.

   `unchecked` lists what was not checked and why -- say so, never call it
   fine. `repository: null` means the check was not scoped (`repository_note`
   says why). Nothing flagged is an answer: say "nothing stuck or stale in
   <repository>" with the count of running workflows and tasks.
2. Then attach what has no row: ONE `swarm_workflow_launch` call with
   `{attach: "all", repository: <the check's repository, or "all" when it
   was null>, new_only: true, exclude: [<the workflow and task ids of every
   [SwarmCloud] row you can see live in this session>]}`, and one Workflow
   call per entry of its `launches`, exactly as `attach --all` launches them
   -- one summary row per workflow at the default `rows: summary`, or, with
   `per-step`, every `rows 1-8`, `rows 9-16`... copy it lists.
   `new_only` leaves out every workflow and task this session was already
   handed a row for, and `exclude` what you can see, so nothing is attached
   twice -- with one exception: a step or single task whose row ENDED
   because its task parked waiting on a person (the owner's questions, a
   hand-off; archived issue 2441) gets a NEW row once it has left PARKED, for that step
   alone (a `rows 1-N` copy naming it), and only once; one still parked
   stays left out; `already_attached` names them, `repository_unread` names any whose
   repository could not be read, with the `/sc attach <id>` for each. Empty
   `launches` is an answer: every running workflow of the project already has
   its rows. `/sc attach <wf_id>` by hand always attaches, whatever was
   attached before -- that is how a row that was closed comes back.
   Each `rowless_tasks` entry is a running task this session dispatched whose row never started: say its `line` and make its Workflow call (`launch`) now.
3. When step 2 launched at least one run, the mandatory final step:
   [Arming the watch](#arming-the-watch), with its reply's `arm_watch`.
   `nothing_running` in the check's answer means nothing of the project runs:
   say so in one line, and LEAVE the watch armed -- a check every interval
   is one read, and the next workflow is watched without anyone re-arming
   it. A check never cancels its own job; only `watch off` does.
