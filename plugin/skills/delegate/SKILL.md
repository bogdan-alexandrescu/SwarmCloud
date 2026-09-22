---
name: delegate
description: Decide whether a unit of work runs in this session or as agents in SwarmCloud, dispatch it, follow it, and bring the result back into the tree. Use when work splits into several independent units, when a task would flood this session's context, when it is long enough that the developer wants their terminal back, when more parallelism is wanted than one machine has, or when asked "run this in the cloud", "run these in parallel", "do it remotely", "what is running remotely", "what has that cost", or "apply what the agent did". Read it before applying any remote agent's patch into a working tree.
allowed-tools:
  - mcp__swarmcloud__swarm_dispatch
  - mcp__swarmcloud__swarm_workflow
  - mcp__swarmcloud__swarm_workflow_status
  - mcp__swarmcloud__swarm_workflow_result
  - mcp__swarmcloud__swarm_workflow_cancel
  - mcp__swarmcloud__swarm_follow
  - mcp__swarmcloud__swarm_status
  - mcp__swarmcloud__swarm_wait
  - mcp__swarmcloud__swarm_result
  - mcp__swarmcloud__swarm_apply
  - mcp__swarmcloud__swarm_integrate
  - mcp__swarmcloud__swarm_cancel
  - mcp__swarmcloud__swarm_overview
  - mcp__swarmcloud__swarm_accounts
  - mcp__swarmcloud__swarm_capacity
  - mcp__swarmcloud__swarm_agents
  - mcp__swarmcloud__swarm_trouble
  - Bash(uv run swarm tail:*)
  - Bash(swarm tail:*)
  - Bash(git status:*)
---

# delegate — running this session's work in SwarmCloud

The `sc` skill is read-only and says so. **This one writes.** It dispatches
agents, cancels them, and puts their changes into the developer's working tree.
Every rule below about asking first exists because of that sentence.

The bar it is written to: a developer working in the terminal should not have to
ask for remote execution, and should never be surprised by it. Delegation that
only happens when someone types "run this in the cloud" is the thing this skill
replaces. Delegation nobody was told about is worse than no delegation at all.

## The decision, in one table

| What is in front of you | Where it runs |
|---|---|
| Three or more units that do not read each other's output | **remote**, one agent each |
| One unit whose output would fill this session with transcript | **remote** — the context cost lands there, not here |
| Anything the developer would rather not sit and watch | **remote**, then hand the terminal back |
| More parallel work than this machine will run at once | **remote** — that is the whole point |
| One small edit you can make in a tool call or two | **local** |
| Anything that reads uncommitted work, or the local filesystem | **local** — see below; this is the surprising one |
| Anything needing the developer's own credentials or logins | **local** — the agent runs as its own tenant identity |
| Anything needing git history: blame, bisect, "when did this break" | **local** — the remote clone is `--depth 1` |

Two or more of these can be true at once. When they are, the local row wins:
work that cannot see what it needs does not become right by being parallel.

## The clean-checkout rule — this is the part that matters

**A remote agent gets a fresh, shallow clone. It cannot see uncommitted work.**

`agent_worker/gitops.py:193` clones with `--depth 1 --no-tags --single-branch`
at the ref the dispatch names. Everything follows from that:

* **Uncommitted edits are invisible.** The agent works on what is *pushed*.
  A task premised on a change sitting in the developer's tree will confidently
  do the wrong thing, succeed, and return a patch against the wrong base.
* **A branch that only exists locally is not there.** Push the ref first, or
  dispatch against one that is already on the remote.
* **There is no history.** One commit deep. `git log`, `git blame` and
  `git bisect` have nothing to work with.
* **Dispatching with no `repo` at all clones nothing.** The task still runs and
  still succeeds; `swarm_result` then reports *"this task cloned no repository,
  so there is no code to apply"*, and the work exists only as transcript. The
  `repo` argument is optional in the tool and has no default — the terminal
  `swarm dispatch --repo` falls back to `$SWARM_REPO`, the MCP tool does not.

So: before dispatching anything that touches code, say out loud which ref the
agents will see, and check that the work they depend on is on it.

## Dispatching

**Anything with `depends_on` or `input_from` goes through `swarm_workflow`**,
not through a hand-rolled join. Dispatching the units separately and stitching
them together in a later prompt is exactly the hand bookkeeping the platform
exists to remove, and it loses artifact staging entirely --- `input_from` is how
one step's output reaches the next, by GCS reference rather than through a
prompt.

Per-step state comes from `swarm_workflow_status`, which reports the **derived**
rollup and never a stored `state` field. That distinction is not theoretical: on
2026-09-22 a stored field read `QUEUED` for a workflow whose six steps had ALL
succeeded.


| What you want | Call |
|---|---|
| run one unit remotely | `swarm_dispatch` |
| is it done yet? | `swarm_status` |
| block until it is done, then tell me what it made | `swarm_wait` |
| what did it produce — and if nothing, why | `swarm_result` |
| put one agent's changes in my tree | `swarm_apply` |
| put several agents' work on one branch | `swarm_integrate` |
| stop it | `swarm_cancel` |
| what is running right now | `swarm_agents` |
| is there room for this batch | `swarm_capacity` |
| what is the shared pool at | `swarm_accounts` |
| why did four of them die at once | `swarm_trouble` |

`swarm_dispatch` takes `prompt`, `profile`, `repo`, `ref` and `label`. The
profile is a **name** from the frozen catalogue — `mock`, `generic`,
`claude-code`, `codex`, `browser` — and the image, command and resource class
come from the name. There is no parameter for an image and asking for one is
not a thing a caller may do.

Always pass `label`. It is the short name the console shows, and an operator
looking at eight running agents should not have to open each one to find out
which is which.

Give each agent the whole of its unit. They cannot see each other's trees —
per-tenant isolation is not negotiable — so a prompt that says "continue what
the other agent started" describes something the agent cannot do.

## Following a run

Use **`swarm_follow`** rather than going silent between dispatch and result. It
takes a cursor and hands back the next one, so poll it between turns and narrate
progress the way this session narrates local work. It is a cursor rather than a
held connection because a long-lived tail inside a subprocess dies.

It caps what it returns and SAYS when it capped. A silent truncation reads as
"that was all the output", so pass the cap on to the developer rather than
summarising past it.


An MCP tool returns exactly **once**, so nothing here streams and no tool
pretends to.

* `swarm_wait` blocks and then reports. It spends this session's turn doing
  nothing, so it is right for a minute and wrong for twenty.
* `swarm_wait` with `timeout_seconds: 0` does one pass and returns: "tell me
  what has finished, do not wait". It is the cheap poll, and it names anything
  still running rather than implying it finished.
* `uv run swarm tail <id> [<id> ...]` in a **background shell** is the thing
  that streams. It takes several ids, so one shell follows a whole batch, and
  it prints a gap header when the published window moved past what it had
  shown — a tailer that stitched two non-adjacent pieces together would print a
  transcript that never happened.

The un-seamless shape to avoid is: dispatch, go silent for eleven minutes,
produce a result. Narrate. A local subagent shows progress and a remote one has
to as well, even if the progress is only "four of five finished, `absentzero`
is still running".

## When a remote agent dies — a bare task id is not a report

Anything that reaches the developer must be actionable. `swarm_result` already
does the hard half: when there is no patch it says **why**, and there are six
causes needing six different answers. Surface the reason, never swallow it.

| What `swarm_result` says | What it means, and what to do |
|---|---|
| no result summary was written | The attempt **parked** — its summary is in the event detail. It is not dead; it resumes and will reach a terminal state later. Report it as parked, with the reason, and keep the id |
| this task cloned no repository | The dispatch carried no `repo`. The work happened and is unreachable as a patch. Re-dispatch with `repo` and `ref` |
| the change could not be read from the workspace | The harvest failed, not the agent. Quote the error verbatim — it is the only evidence of what went wrong |
| the diff was N bytes, over the cap, and was discarded | Real work, too large. It was **refused rather than truncated**, on purpose: half a patch applies cleanly and silently loses the rest. Narrow the unit and re-dispatch, or take the branch/PR instead |
| the agent changed nothing in the repository | It ran and decided there was nothing to do, *or* it misread the prompt. `swarm_result`'s commit and insertion counts are both zero either way, so say which one you believe and why |
| no patch was recorded / a publish reason | The publishing half failed or was declined. Quote the reason; it names the remedy |

Two more, from outside that list:

* A task in `FAILED` or `DEAD_LETTER` carries `error` in the result. Quote it.
* **Several failing at once is a platform answer, not an agent answer.** Run
  `swarm_trouble` before re-dispatching: an exhausted quota window, a paused
  pool or an account needing re-auth will fail the retry the same way, and
  re-dispatching into it spends the shared pool to learn nothing.

## How the work comes back

`swarm_apply` downloads one task's patch and applies it with `--3way`. What
that actually does to the developer's repository:

* It writes into the working tree and **commits nothing**.
* The patch covers committed *and* uncommitted *and* untracked work from the
  agent's workspace — the worker records new files with `--intent-to-add` so a
  single diff covers them.
* A conflict is an **outcome, not a failure**: the content lands and ordinary
  conflict markers are left in the files, which are listed in the response.
* `repo` defaults to the **current working directory**.
* It does **not** require a clean tree. `swarm_integrate` does; `swarm_apply`
  does not, and will happily interleave an agent's changes with the
  developer's uncommitted ones.

### Apply without asking only when all of these hold

1. The developer asked for this work to land here — or asked for the change
   itself, and this is simply how it arrives.
2. `git status --porcelain` is empty, or the only dirty files are ones they
   asked the agent to change.
3. It is one task. Several go through `swarm_integrate`, which is a different
   conversation.

### Ask first — with the specific thing you are about to do — when

* the tree is dirty and they have not said to apply on top of it;
* more than one agent's work is coming back;
* nobody named a directory, so `repo` would default to wherever this session
  happens to be;
* the work is large, or nothing like what was asked for. Read `swarm_result`
  before applying, not after.

**`swarm_integrate` moves the developer's branch.** It refuses a dirty tree,
then runs `git checkout -B <branch> [<base>]` and applies each patch in the
order given, so each lands on the accumulated result. That is a branch switch.
Name the branch and the base in the same sentence you propose it, and never run
it as a way of "just having a look".

Afterwards, say what landed: the files, the conflicted ones, and the fact that
nothing was committed. A developer must never discover a three-way merge they
did not ask for.

## The honesty constraint

**Seamless must not mean hidden.** Three things a developer must always be able
to recover, because getting them wrong costs money or trust.

**1. That it is remote.** Not loudly — but when asked "what is running?", the
answer comes from `swarm_agents`, not from memory, and it says these are
running in SwarmCloud rather than here.

**2. What it is spending, and whose.** A local subagent spends the developer's
own session budget. A remote one spends a **shared subscription pool** that
other people and other tenants are also drawing on, and a surprise on a shared
pool is far worse than a surprise locally.

* Before a batch, read `swarm_capacity`. `ROOM` is how many more tasks of that
  profile fit before the **binding** pool refuses — and if `ROOM` is an em
  dash, the real room is unknown and could be zero. Do not dispatch a large
  batch against a dash.
* `swarm_accounts` is how the pool's 5-hour and 7-day windows moved. The marks
  mean exactly what the `sc` skill says they mean, and the one that matters
  here is that **an em dash is "not measured", never zero** — a dead poller
  must not read as a healthy pool.
* **Dollars are not on this surface.** Per-attempt spend is recorded as
  `cost_usd` on the attempt and is reachable from the API and the console; no
  tool in this plugin returns it and no view of `sc` prints it. When asked what
  something cost: give the pool movement, say the per-attempt figure is not
  available from this session and where it is, and **do not estimate one**.
* For scale only, from a measured run on 2026-09-22: one join step of a
  six-step workflow cost **$0.0937**. That is an order of magnitude for one
  step of one workflow. It is not a quote for anything else and must never be
  multiplied into one.

**3. Where the work landed.** Covered above, and it is the rule most easily
lost when things are going well.

