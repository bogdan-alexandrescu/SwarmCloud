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
| passing work between stages | `input_from` on a workflow step | **inert, see below** |

### The three gaps, precisely

1. **`input_from` is declared and never honoured.** A workflow step may say it
   wants an upstream step's artifact staged into its workspace. The API
   validates it (`validation.py:205`), the service records it in task metadata
   (`service.py:228`), the codec returns it — and no worker or scheduler code
   reads it. So a step cannot receive the previous step's output, which is
   exactly the primitive an integration step needs.

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
(`swarm_common.profiles`) plus `RESOURCE_CLASSES`, neither of which any route
returns today. It belongs with the control-plane redesign rather than here, and
is listed so it is not lost: a person choosing a `runner_profile` by name
currently has no way to see what the names mean.

## 7. What each decision costs, stated once

| Decision | What it obliges |
|---|---|
| `integrate` exists | the worker must honour `input_from`; an integrate runner profile; a conflict-resolving agent whose output reaches `main` |
| `direct-pr` exists | write scope on the tenant token, which is a real widening |
| `carrier: branches` | the same write scope, earlier in the run |
| `carrier: checkpoints` | retention driven by whether anything still needs a checkpoint, not by a clock |
| `collect` stays default | nothing; this is today's behaviour |
