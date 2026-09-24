# Seamless delegation: running a session's agents in SwarmCloud

**The goal, as stated by the owner on 2026-09-22:** *"most of the time a
developer will spend will still be in front of claude code cli in a terminal
and not on a web page ... we need to start making a lot more progress on the
direct integration with claude cli to make it almost seamless and almost not
visible that the workflows running in claude cli are not running agents locally
but in the cloud."*

That is the bar: **a developer should not be able to tell.** Not "a convenient
remote API" — indistinguishable from local subagents, except that it scales
past one machine and does not consume the local session's context.

## What already exists, and is good

`apps/swarm-mcp` is a real bridge, not a stub. Twelve tools:

| Tool | What it does |
|---|---|
| `swarm_dispatch` | "Run an agent in SwarmCloud instead of locally." Returns a task id immediately. |
| `swarm_wait` | Block until terminal, return commits, files changed, patch uri, PR. |
| `swarm_status` | State of one or more tasks, immediately. |
| `swarm_result` | What a task produced — and when there is no PR, **why**, with six distinct reasons. |
| `swarm_apply` | Apply a task's changes into a local tree, `--3way` always, so a conflict leaves ordinary markers instead of failing atomically. |
| `swarm_integrate` | Several agents' work onto ONE branch, applied in order so each patch lands on the accumulated result. |
| `swarm_cancel` | Cancel running tasks; work stays checkpointed and harvestable. |
| `swarm_overview` / `accounts` / `capacity` / `agents` / `trouble` | Read-only cluster state. |

`swarm_apply`'s three-way discipline and `swarm_result`'s six-reason absence
explanation are the right instincts: they are the difference between a bridge a
developer trusts and one they check by hand every time.

## The three gaps between this and "cannot tell the difference"

### G1 — The bridge cannot run a workflow at all

`apps/swarm-mcp/swarm_mcp/server.py` contains the string "workflow" **zero**
times. Every multi-step feature this platform exists for — fan-out, `depends_on`,
`input_from` artifact staging, the rollup, `on_step_failure` — is unreachable
from a Claude Code session. A developer can dispatch N independent agents and
join them by hand, which is precisely the manual bookkeeping the platform was
built to remove.

This is the single largest gap. The API already has it: `POST /v1/workflows`,
`GET /v1/workflows/{id}`, `POST /v1/workflows/{id}/cancel`. Verified working on
2026-09-22 — `wf_5e5ad3b6f7da4299a839`, five parallel steps and a join, all six
succeeded, `input_from` staged five artifacts into the join's workspace.

**Needed:** `swarm_workflow` (submit a DAG), and workflow-aware `status`/`result`
that report per-step state and the DERIVED rollup, never the stored `state`
field — which, as of 2026-09-22, still reads `QUEUED` for a workflow whose six
steps have all succeeded.

### G2 — Nothing can follow a run

`cmd_tail` exists and works (`cli.py:144`, "follow logs and events until every
task finishes"), but it is a CLI subcommand and **not** an MCP tool. The model
cannot call it. `swarm_agents`' own description admits the hole: *"Returns
immediately; it does not follow anything. For live output ..."* — and then the
sentence points somewhere the model cannot go.

The consequence is exactly the un-seamless experience: dispatch, then silence,
then a result. A local subagent shows progress; a remote one must too.

**Needed:** a streaming or incremental-poll tool that returns new events and log
lines since a cursor, so a session can narrate progress the way it does for
local work. Note the constraint in `client.py:25` — a long-lived `tail` inside a
subprocess dies; design for resumable cursors, not a held connection.

### G3 — No skill tells the session to delegate

The only skill is `plugin/skills/sc`, and it is deliberately read-only
(`allowed-tools: Bash(uv run sc:*)`, "It never writes, never refreshes a
credential and never cancels anything"). That is a good skill for what it is.

But nothing instructs a session on **when** to run work remotely, so delegation
happens only when the developer explicitly asks for it every single time. That
is the opposite of invisible.

**Needed:** a second skill whose job is the delegation decision. It has to answer,
without being asked: when is remote right (many independent units; work that
would flood the local context; anything long enough that the developer wants
their terminal back), when is local right (one small edit; anything needing the
local filesystem or uncommitted state), and what to do on failure — because a
remote agent that dies must degrade to something the developer can act on, not
a dead task id.

## The honesty constraint

Seamless must not mean hidden. Three things a developer must always be able to
see, because getting them wrong costs money or trust:

1. **That it is remote.** Not loudly, but recoverably — the session should be
   able to say which work is running where when asked.
2. **What it cost.** Remote work spends the tenant's subscription quota. The run
   on 2026-09-22 cost $0.0937 for one join step. A local subagent spends the
   developer's own session budget; a remote one spends a shared pool, and a
   surprise there is much worse than a surprise locally.
3. **Where the work landed.** `swarm_apply` writes into the working tree. A
   developer must never discover a three-way merge they did not ask for.

## Order of work

1. **G1**, because it unlocks the platform's actual feature.
2. **G2**, because without it G1 is a black box for minutes at a time.
3. **G3** last — the skill should be written against tools that already exist,
   not against tools it hopes for.
