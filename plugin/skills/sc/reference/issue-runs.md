### `run --issue owner/repo#N`

Plans a GitHub issue and, once the plan is approved, runs it (archived issue 454). A planner
task reads the issue and the repository's open issues and pull requests and
writes a plan; the approved plan compiles into one workflow that opens a pull
request, whose CI the platform reads and fixes for up to `--fix-rounds` rounds
(3 by default). The issue gets a plan comment and one status comment, kept up
to date by the platform with the tenant's own forge credential — never one
this session supplies.

1. Say, before anything is submitted: "about to plan SwarmCloud issue run for
   `<owner/repo#N>`: one planner task, no capacity held by the plan until it is
   approved". Then call `swarm_run_issue` with `issue`, and `plan_approval:
   "auto"`, `auto_merge: true`, `fix_rounds` or `model_class` only when the
   developer gave `--plan auto`, `--auto-merge`, `--fix-rounds` or
   `--model-class <class>` (the planner's model class; see "Model classes"
   below). The terminal equivalent,
   `sc run --issue <owner/repo#N> --follow`, is the developer's to run
   (this skill is not granted it); it prints one row per step as it moves.
2. `--auto-merge` merges the run's pull request with the `merge` step (archived issue 295,
   enabled by contract request 47). The API refuses it only while the `merge`
   profile is disabled, and then nothing is created: report that refusal
   plainly, in its own words, and offer the same run without `--auto-merge`.
3. Report the run's `id` and `state`. A `required` run (the default) stops at
   `PLANNED`, holding no capacity, until the developer approves — go on to
   `plan show`. Once the run is `RUNNING` (or `FIXING`, a CI fix round), its
   answer carries `attach_with`: run `/sc attach <workflow_id>` for live rows.
   Once a workflow exists, the reply's `arm_watch` is set too: end with
   [Arming the watch](#arming-the-watch) (the attach would arm it anyway, and
   step 1 there makes sure only once).
### `runs`

ONE read: call `swarm_runs` (in a terminal, `sc runs`) and report each
run's id, issue, state and pull request as served. Never poll.

### `plan show <run>`

ONE read: call `swarm_run` with `run_id` (in a checkout,
`sc plan show <run>`), and show the developer the WHOLE plan — summary,
requirements, overlaps with work in flight, every step's title, files, tests,
estimate and prompt — and its `plan_digest`. The prompts are what will run with
the tenant's forge token; they are what is being approved.

### `plan approve|edit|reject <run>`

THE DIGEST IS THE APPROVAL. Approve only a plan the developer was SHOWN, with
the `plan_digest` of that showing: if the plan was not shown in this
conversation, do `plan show` first and wait for the developer to say approve.
Never read the run and approve in one go.

- `approve`: `swarm_plan_approve` with `run_id` and the shown `plan_digest`.
  The workflow is submitted; report its `attach_with`.
- `edit`: build the whole replacement plan from the developer's instructions,
  show it, then `swarm_plan_edit` with `run_id`, the `plan_digest` of the plan
  that was edited, and `plan`. The run stays `PLANNED` with a new digest, which
  must be shown and approved in its turn.
- `reject`: `swarm_plan_reject` with `run_id`, the developer's `reason` and the
  shown `plan_digest`. The run ends `REJECTED`.

A `plan_changed` refusal means the plan is no longer the one shown, and
nothing was done: show the new plan and ask again — never resend with the new
digest unseen. `invalid_plan` means the edit was refused and the run kept its
plan. Report every error verbatim. In a terminal the developer runs
`sc plan approve <run>` themselves; it prints the plan and asks them to type
`approve`. This skill is not granted those commands.
