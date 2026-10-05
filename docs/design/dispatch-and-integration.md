# How work is dispatched, done, saved and merged

A map of every way Claude Code hands work to an agent locally, what SwarmCloud
can do today, and precisely where the two diverge. Written because the goal is
that dispatching to the cluster feels the same as running locally, and "feels
the same" is a claim you can only check against a list.

---

## 1. How Claude Code dispatches work, locally

Four mechanisms, and they compose.

### 1.1 The main loop

The session itself. It edits files in the working directory and commits them.
Everything else is measured against this, because it is what "just running
Claude Code" means.

* **Where work lands:** the working tree, directly.
* **How it is committed:** `git commit`, by the session, when asked.
* **Isolation:** none. There is one tree and one editor of it.

### 1.2 Subagents — the `Agent` tool

`Agent({prompt, subagent_type, model, effort, isolation})`. Runs in the
background; the parent is notified when it finishes and reads its final report.
Several launched in one message run concurrently.

* `subagent_type` selects the agent: `fork` inherits the parent's full context,
  `general-purpose`, `Explore` (read-only search), `Plan`, or a custom type from
  `.claude/agents/*.md`.
* **Where work lands, by default: the SAME working directory.** A subagent's
  edits are simply there afterwards. This is why a subagent feels like an
  extension of the session rather than a separate job.
* `isolation: "worktree"` gives it a fresh git worktree instead — a separate
  checkout, auto-removed if it changed nothing. Documented as expensive and
  reserved for agents that mutate files in parallel and would otherwise
  conflict.
* **How the work is merged:** with a shared tree, there is nothing to merge.
  With a worktree, **merging is manual** — the parent looks at the worktree and
  integrates. Claude Code has no built-in merge step.

### 1.3 Workflows — the `Workflow` tool

A deterministic JavaScript script that orchestrates many subagents:
`agent()`, `parallel()`, `pipeline()`, `phase()`, and `workflow()` for one
level of nesting. Concurrency is capped at `min(16, cpus - 2)`; total agents
per run at 1000.

* `pipeline(items, stage1, stage2, ...)` runs each item through every stage
  independently, with **no barrier between stages** — item A can be in stage 3
  while item B is still in stage 1. `parallel()` is the barrier.
* `schema` forces structured output, validated at the tool layer.
* Resume replays unchanged `agent()` calls from cache.
* **ultracode** is a standing opt-in: author a workflow for every substantive
  task, often several in sequence — understand, design, implement, review —
  with the session reading each result before deciding the next.
* Same landing rules as 1.2: shared tree unless a stage asks for a worktree.

### 1.4 Background processes

`Bash({run_in_background: true})`. Survives across turns, re-invokes the
session on exit. Used for builds, test runs, log tails — not for authoring.

---

## 2. How that work becomes a release, locally

| Path | What happens |
|---|---|
| shared working tree | edits are already in the tree; the session commits |
| worktree isolation | a separate checkout; the session merges by hand |
| a pull request | the session runs `gh pr create`; there is no built-in step |

The important property: **the session is the integrator.** Nothing merges
automatically, and nothing needs to, because the session has the whole
repository in front of it and can resolve a conflict with context.

---

## 3. What SwarmCloud can do today

| Local mechanism | SwarmCloud equivalent | State |
|---|---|---|
| one subagent | `POST /v1/tasks` | works |
| several at once | `POST /v1/tasks/batch` | works |
| a workflow | `POST /v1/workflows` — a DAG with `depends_on` | works |
| `isolation: "worktree"` | every attempt gets its own workspace and its own clone | **always on, not optional** |
| edits land in the tree | they land in a tmpfs that is destroyed | replaced by harvest |
| the session commits | the worker harvests a patch; optionally pushes a branch and opens a PR | works |
| the session merges | — | **missing** |
| passing work between stages | `input_from` on a workflow step | **works** (2026-09-21) |

### The three gaps, precisely

1. ~~**`input_from` is declared and never honoured.**~~ **CLOSED 2026-09-21.**
   It was declared and never honoured: the API validated it, the service
   recorded it in task metadata, the codec returned it, and no worker code read
   it. The worker now stages each declared artifact from the upstream task's
   successful attempt into the workspace before the agent starts, and refuses
   the attempt — naming the upstream task and the filename — when a promised
   input cannot be found. Starting an agent without an input it was promised is
   how it silently produces the wrong thing.

2. **There is no integrator.** Locally the session merges. In the cluster
   nothing does: each attempt produces a patch in its own GCS prefix, and they
   never meet.

3. **The merge strategy is not a choice.** A dispatch cannot say "each agent
   opens its own PR" versus "collect these and have one agent integrate them".
   Today the only behaviour is per-attempt publish, gated on the token's write
   permission.

---

## 4. The shape of the answer

**Decided 2026-09-21 by the owner.** Sections 4.1-4.3 are settled; section 5 is
the build order that follows from them and is not started.

### 4.1 Dispatch scales

The three the owner named, in the vocabulary Claude Code already uses:

| Scale | Local | Cluster |
|---|---|---|
| single agent | one `Agent` call | one task |
| main agent with helpers | a session that spawns subagents | a task permitted to submit child tasks |
| a workflow | the `Workflow` tool | a workflow DAG |

The middle one does not exist in the cluster and is the interesting one: it is
what makes a remote run feel like a local session rather than like a job.

### 4.2 Integration strategies — DECIDED: all three, default `collect`

Per dispatch, chosen at submit time. `collect` is the default so that no
existing dispatch changes behaviour and a deployment whose token is read-only
keeps working exactly as it does today. Turning on write scope stays a decision
somebody makes, rather than one that happens to them because a default moved.

| Strategy | What happens | When it fits |
|---|---|---|
| `direct-pr` | every agent pushes `swarm/<task>` and opens its own PR | independent changes; review per agent |
| `integrate` | a final step depends on all the others, receives their patches, resolves conflicts and opens ONE PR | one feature built by several agents |
| `collect` | patches are harvested and left for a human; nothing is pushed | today's behaviour, and the only one that works with a read-only token |

`integrate` is the one that needs `input_from` to actually work, plus an
integration runner profile.

### 4.3 Where the work is kept between steps

**DECIDED: both, chosen per dispatch** (`carrier: "branches" | "checkpoints"`).

Two options, and they are not equivalent. Carrying both means the integrator --
the one component whose correctness decides what reaches `main` -- has two code
paths through it, and that is the risk this choice accepts deliberately:

* **Feature branches.** Durable, reviewable, and they survive the platform.
  Requires write scope on the token, which is a real widening.
* **Checkpoint tarballs.** Already produced every few minutes, already in GCS,
  already carry `.git` and any local commits. They need a retention rule that
  is demand-driven rather than time-based: today a lifecycle rule expires them
  on a clock, and an integration step that runs a week later would find
  nothing.

**`carrier: branches` IS BUILT** (D13, owner decision 2026-10-01: wire it).
`carrier: checkpoints`, the default, is byte-for-byte what it was. With
`branches`:

* **The checkpoints taken after the runner stops push the step's committed
  work** -- park, cancel, SIGTERM and finish; never the periodic ones (see
  "Periodic checkpoints do not push" below) -- to
  `<git_branch_prefix><task id>`, the branch the publish uses
  (`Worker._push_carrier_branch`). NEVER FORCED: the push is one worker commit
  of the agent's committed tree on top of the branch's current tip
  (`gitops.commit_tree_onto`), so every push fast-forwards the last, across
  checkpoints and attempts. It passes the publish's gates -- the reap, a
  worker-owned repository, the final-tree leak scan, the authorship check,
  `push_branch`'s prefix and protected-branch refusals. A failed push is
  logged and never ends the attempt. The cost of never forcing: when a retry
  cloned a default branch that has moved since the tip was built, its worker
  commit's diff against the tip also shows the default branch's intervening
  changes. The tree is still exactly the work; one commit's diff is noisier.
* **A dependant that starts from its parent's branch already holds the
  parent's work.** If it also declares the parent's `swarm-work.patch` in
  `input_from`, that patch is the same change twice; the worker stages it
  anyway and logs that it is redundant.
* **Periodic checkpoints do not push** (owner decision, 2026-10-02,
  accepting the narrowing of D13's "every checkpoint"). **The git token is
  never held while agent code can run.** The agent shares the worker's uid, so
  while it runs, the credential file and the token-bearing git process would
  be within its reach: a token in hand mid-run is a token the agent can read,
  and with `branches` it is a token with write scope. So a checkpoint taken
  with the runner alive pushes nothing and logs that it did not: the periodic
  ones, and the control-plane-outage one (#70 orders it checkpoint, THEN stop
  the runner). The branch is pushed at the **park, cancel, SIGTERM and finish**
  checkpoints, each taken after the runner has stopped
  (`Worker._carrier_push` returns early while the child is alive). What this
  costs: between those points the branch lags the work by up to the whole run,
  and a worker lost without a SIGTERM (an OOM kill, a node loss) leaves the
  branch at the last push -- the GCS checkpoint still holds the rest. Pushing
  mid-run would need the push to run under a process identity the agent
  cannot reach, which is not built.
* **The finish pushes too**, under every strategy -- a `collect` step pushes
  its branch and opens no pull request -- with the final tree, uncommitted
  work included, as one worker commit on the branch's tip (the agent's commits
  are not replayed one by one under this carrier: a replay writes new shas
  each time and would not fast-forward the checkpoints' pushes). The result
  records `result_summary.branch = {name, head}`.
* **A dependant starts from its parent's branch.** A step with one direct
  parent (`depends_on`) that recorded `result_summary.branch` under the name
  the worker derives from the parent's id clones that branch instead of the
  default one. An integrator merges its parents' branches in step order
  (`integrates`) at its publish, as it already did. A step with several
  parents and no integrator role starts from the default branch.
* **The API refuses `carrier: branches` only without a repository** (422
  `invalid_dispatch`, `detail.missing: "repository_url"`). With one it is
  accepted (201), whatever the token can do: swarm-api reads no tenant's git
  secret and holds no path to one.
* **The worker refuses a token that cannot push, before the agent runs**
  (owner decision, 2026-10-02). After the clone, a `branches` attempt reads
  the tenant's own `swarm-tenant-<tenant>-git` secret and asks the forge with
  it, exactly as the carrier push and the publish do
  (`Worker._carrier_scope_refusal`, `forge.probe_repository`: one
  `GET /repos/{owner}/{repo}`, `permissions.push`). Three answers:
  * **no git credential, or `permissions.push` not `true`**: the task ends
    FAILED at once, whatever attempts are left, with cause
    `forge_read_only` -- the prefix of `last_error` and
    `result_summary.carrier_check.cause` -- and end cause `cannot_start`. Not
    retried: the next attempt reads the same secret and asks the same forge.
    The agent never started, so it cost no agent time and no provider quota;
  * **the forge could not be asked** (a network failure, a 429, a 5xx): the
    attempt fails RETRYABLY with cause `forge_unreachable`, after a 60-second
    delay, bounded by `max_attempts`;
  * **a token that can push**: the attempt proceeds.

  `checkpoints` asks nothing and reads no extra secret. The token is never
  logged, stored or put in the error; the reason is the probe's own words.

  **Why the worker and not the API.** A submit-time 422 would be friendlier --
  the caller hears at once instead of from a failed task -- and B4 first built
  it that way. It required swarm-api to read every tenant's git secret, which
  breaks the rule that exactly one identity may read each secret, the
  tenant's own worker service account
  (`terraform/modules/secret_manager/main.tf`), and made swarm-api depend on
  the worker package. A credential that grants write on a tenant's
  repositories is the last place to add a second reader. The worker already
  reads that secret for the clone and the publish, so the check costs no new
  grant; the price is that a read-only token is reported a few seconds into
  the attempt rather than at submission.

---

## 5. What would have to be built

In dependency order, smallest first:

1. **Honour `input_from`** in the worker: fetch the named artifact from the
   upstream task's GCS prefix and stage it into the workspace. This is the
   unlock; everything in 4.2 depends on it.
2. **An `integrate` runner profile** whose agent applies N patches in order,
   resolves conflicts and opens one PR.
3. **A `strategy` field** on a task or workflow submission, defaulting to
   today's behaviour so nothing changes for an existing caller.
4. **Checkpoint retention by reference** rather than by clock.
5. **Child-task submission** from inside a running agent, for the main-agent
   scale.


---

## 6. Still to map

The owner also asked for a view of **cluster topology: which runtime
environments exist, what each is specialised for, and how they are sized.**
That is a UI surface over the frozen runner-profile catalogue
(`swarm_common.profiles`) plus `RESOURCE_CLASSES`.

**Mapped since, re-checked 2026-10-02.** This section said no route returned
either. Two do now: `GET /v1/runtimes`
(`apps/swarm-api/swarm_api/routes/platform.py::runtimes`) serves every runner profile
with its image, declared and resolved backend, resource class, timeout and
availability, read from the catalogue rather than copied; and
`GET /v1/resource-classes` (`apps/swarm-api/swarm_api/routes/platform.py::resource_classes`)
serves the classes' sizes and units. The console renders the first as
Capacity ▸ Runtimes (`apps/swarm-ui/src/Runtimes.tsx`). Publishing the
catalogue does not weaken invariant 10: a caller still sends only a profile
name, and the route's docstring says why reading what an admin defined is not
supplying one. So a person choosing a `runner_profile` by name can now see what
the names mean, and nothing is left to map here.

## 7. What each decision costs, stated once

| Decision | What it obliges |
|---|---|
| `integrate` exists | the worker must honour `input_from`; an integrate runner profile; a conflict-resolving agent whose output reaches `main` |
| `direct-pr` exists | write scope on the tenant token, which is a real widening |
| `carrier: branches` | the same write scope, earlier in the run (built, D13: pushed at each checkpoint taken after the runner stops, and at the finish; not at the periodic or outage checkpoints; see 4.3) |
| `carrier: checkpoints` | retention driven by whether anything still needs a checkpoint, not by a clock |
| `collect` stays default | nothing; this is today's behaviour |
