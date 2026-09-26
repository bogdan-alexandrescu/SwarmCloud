---
name: delegate
description: Decide whether a unit of work runs in this session or as agents in SwarmCloud, dispatch it, follow it, and bring the result back into the tree. Use when work splits into several independent units, when a task would flood this session's context, when it is long enough that the developer wants their terminal back, when more parallelism is wanted than one machine has, or when asked "run this in the cloud", "run these in parallel", "do it remotely", "what is running remotely", "what has that cost", or "apply what the agent did". Read it before applying any remote agent's patch into a working tree.
allowed-tools:
  - mcp__swarmcloud__swarm_profiles
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
  - mcp__plugin_sc_swarmcloud__swarm_profiles
  - mcp__plugin_sc_swarmcloud__swarm_dispatch
  - mcp__plugin_sc_swarmcloud__swarm_workflow
  - mcp__plugin_sc_swarmcloud__swarm_workflow_status
  - mcp__plugin_sc_swarmcloud__swarm_workflow_result
  - mcp__plugin_sc_swarmcloud__swarm_workflow_cancel
  - mcp__plugin_sc_swarmcloud__swarm_follow
  - mcp__plugin_sc_swarmcloud__swarm_status
  - mcp__plugin_sc_swarmcloud__swarm_wait
  - mcp__plugin_sc_swarmcloud__swarm_result
  - mcp__plugin_sc_swarmcloud__swarm_apply
  - mcp__plugin_sc_swarmcloud__swarm_integrate
  - mcp__plugin_sc_swarmcloud__swarm_cancel
  - mcp__plugin_sc_swarmcloud__swarm_overview
  - mcp__plugin_sc_swarmcloud__swarm_accounts
  - mcp__plugin_sc_swarmcloud__swarm_capacity
  - mcp__plugin_sc_swarmcloud__swarm_agents
  - mcp__plugin_sc_swarmcloud__swarm_trouble
  - Bash(uv run swarm tail:*)
  - Bash(swarm tail:*)
  - Bash(git status:*)
---

> **Why every tool is listed twice.** The same bridge arrives under two names
> depending on how it was registered, and a permission rule is matched, not
> resolved, so the wrong spelling grants nothing and the session behaves as if
> delegation is simply not available.
>
> * `.mcp.json` at the repository root registers it as the project server
>   `swarmcloud`, and its tools are `mcp__swarmcloud__*`. That is the path when
>   this session's working directory is the repository.
> * `plugin/.claude-plugin/plugin.json` registers the same server as part of the
>   plugin, and a plugin's own MCP server is SCOPED: its tools are
>   `mcp__plugin_<plugin>_<server>__<tool>`, so `mcp__plugin_sc_swarmcloud__*`.
>   That is the path when the plugin is installed and the session is anywhere
>   else. Without it, `delegate` was 18 permissions for tools that only existed
>   in one directory on one machine.
>
> `tests/unit/mcp/test_plugin_skills.py` holds the two lists in lockstep and
> derives the scoped prefix from `plugin.json` itself, so renaming the plugin or
> the server key goes red here rather than silently ungranting half the skill.

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
  `repo` argument is optional in the tool and has no default — the terminal's
  `swarm dispatch` falls back to `$SWARM_REPO` when `--repo` is not given, the
  tool does not.

So: before dispatching anything that touches code, say out loud which ref the
agents will see, and check that the work they depend on is on it.

## Dispatching

**Anything with `depends_on` or `input_from` goes through `swarm_workflow`**,
not through a hand-rolled join. Dispatching the units separately and stitching
them together in a later prompt is exactly the hand bookkeeping the platform
exists to remove, and it loses artifact staging entirely --- `input_from` is how
one step's output reaches the next, by GCS reference rather than through a
prompt. The filename is also where the file lands, so a join with several
parents needs each parent to write a **distinct** filename (`scan-A-notes.md`,
`scan-B-notes.md`, not `notes.md` twice). The API refuses a shared name, or an
absolute or `..` path, at submission.

A step's file reaches its dependant only if the upstream agent wrote it to
`$SWARM_ARTIFACTS_DIR`. `./artifacts` in its working directory is a link to that
directory, unless a staged input or a restored checkpoint already has that
name. A `claude-code` or `codex` upstream agent is told which filenames
its dependants stage and that directory's absolute path; other runners are told
nothing, so their prompt has to say it. A file written anywhere else, the
repository included, is never staged. An upstream attempt whose agent finishes
without writing one of those files FAILS, retryably, naming the missing files:
the step runs again, starting with an empty artifacts directory, until it has
used `max_attempts`, and then it FAILS for good and its dependants are
cancelled. A dependant never starts on a step that left its file out.

Per-step state comes from `swarm_workflow_status`, which reports the **derived**
rollup and never a stored `state` field. That distinction is not theoretical: on
2026-09-22 a stored field read `QUEUED` for a workflow whose six steps had ALL
succeeded.


| What you want | Call |
|---|---|
| which profiles may I name? | `swarm_profiles` |
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

`swarm_dispatch` takes `prompt`, `profile`, `repo`, `ref`, `label` and
`inputs`. The profile is a **name** from the frozen catalogue, and the image,
command and resource class come from the name. There is no parameter for an
image and asking for one is not a thing a caller may do.

`inputs` — on `swarm_dispatch` and on each `swarm_workflow` step — carries only
what the named profile **declares**, which `swarm_profiles` lists under
`inputs`. Today that is `mock`'s test knobs: `{"sleep_seconds": 120}` keeps a
mock step RUNNING long enough to cancel, `{"fail": true}` fails it on purpose,
and `{"quota_exhausted": true, "retry_after_seconds": 60}` parks it ONCE on a
simulated rate limit; the attempt after the park runs to the end. Report that
step as parked while it waits, not as failed. `exit_code` takes a failure's
code, but not 77, 78 or 143, which the worker reads as a rate limit, a refused
credential and a cancellation. `claude-code` and `codex` declare none and take
only the prompt. For a profile that declares, a key it does not declare is
refused before anything is dispatched, by the bridge and by the API alike.
`browser` and `generic` have **not declared their inputs yet** (#218): the
bridge sends them none, but the API bounds what any other caller sends them by
size alone, so do not tell anyone their inputs are checked.

**Do not guess the name — call `swarm_profiles`.** It is the catalogue itself,
so it cannot go stale the way a list written into this paragraph can: every
name, whether each one can be dispatched at all, which backend it lands on, how
much cpu and memory it gets, and whether it needs a provider credential. It
takes no arguments, makes no network call, and therefore still answers when the
cluster does not — which is exactly when somebody is guessing at a name.

It deliberately carries **no image and no command**. That is not an oversight
to work around; those are not part of a caller's vocabulary.

A bad name is refused **before anything is dispatched**, by the bridge, with the
catalogue's own words — so the two answers stay apart:

* *there is no runner profile called `claude`* — a typo. The refusal lists the
  real names; pick one and re-dispatch. Nothing was spent.
* *`codex` is refused: …* — a **known** profile that is turned off. Do not go
  hunting for a typo and do not retry it.

**`codex` is DISABLED.** It is still in the catalogue so that existing runs
which name it stay readable, but this platform is focused on Claude and the
provider refused the registered credential — on 2026-09-23 four codex steps of a
twenty-step run failed with "openai refused the credential", each after being
admitted, leased and dispatched. Use `claude-code`. The same refusal now happens
at submission for a whole workflow, so a DAG with one bad step is refused
entire rather than running the other nineteen and failing that one late.

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
* `swarm tail <id> [<id> ...]` in a **background shell** is the thing that
  streams. It takes several ids, so one shell follows a whole batch, and it
  prints a gap header when the published window moved past what it had shown —
  a tailer that stitched two non-adjacent pieces together would print a
  transcript that never happened.

  **Run it exactly as `follow_live_with` spells it; never retype it.** The
  bridge spells every command it hands back for the install it is running
  from: `uv run swarm tail ...` in a checkout of this repository, plain
  `swarm tail ...` where swarm-mcp is `uv tool install`ed, and
  `uv tool run --from 'swarm-mcp @ git+...@sc-v<version>#subdirectory=apps/swarm-mcp' swarm tail ...`
  on a plugin-only install, where there is nothing installed to find. Retyped
  with the wrong prefix it fails — `uv run` outside a checkout answers
  `Failed to spawn: swarm`, a bare `swarm` with nothing installed answers
  `command not found` — and neither is the platform being broken. The reply
  names the tool first, in `follow_with`, because that is the one this session
  can actually call; the background shell is for the developer. This skill's
  permission rules grant only the checkout's and an installed tool's spelling,
  so the long form asks first.

The un-seamless shape to avoid is: dispatch, go silent for eleven minutes,
produce a result. Narrate. A local subagent shows progress and a remote one has
to as well, even if the progress is only "four of five finished, `absentzero`
is still running".

## Before any of that: could this session reach the API at all?

A tool that fails because the bridge never reached the control plane is not a
dispatch that failed, and reporting it as one sends the developer to look at
their prompt, their repository and the shared pool — none of which is involved.

The tell is in the error, and it is unambiguous: `IAP refused this before the
API saw it`, `an HTML 404 from Google's edge`, `Error code 900`, or a 403 that
names a principal. All four mean the request never reached swarm-api, and the
bridge ends a tool error that Google's edge answered, rather than the API, with
the `swarm doctor` command to run, spelled for this install the way
`follow_live_with` is. Run it **exactly as the error
spells it** — never retyped, for the reasons above — and it prints the address
it used and the kind of credential that address takes; report **that**. The
`sc` skill's table says what each refusal means and which one is an IAM grant
away.

Nothing was dispatched, so nothing was spent, so say that too: a developer who
thinks a batch went out and died will not re-run it.

The fifth tell is the most common and the easiest: **`sign-in required for
<context>: run … sc login`**. The deployment the developer configured takes
them signed in as themselves, and they are not yet. Tell them to run the
`sc login` command **exactly as the message spells it** (a browser window
opens) — it is spelled for their install, as `follow_live_with` is — then
retry the same call. It is theirs to run, not this session's — it waits on a
browser — and there is no other credential to go looking for: every dispatch is
meant to run as the developer, on their deployment, and `sc whoami`, spelled
the same way, shows which one that is.

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

Every result also carries `runner_profile` and `backend`, and on a failure those
are the first two things to say. They separate the two kinds of dead agent that
need completely different answers: a `claude-code` task on `CLOUD_RUN_JOB` that
failed is usually about the prompt or the repository, while a `browser` task on
`GKE_AUTOPILOT` that failed is usually about placement. `backend` is the one you
cannot work out from the task — the mapping lives in the frozen catalogue — and
a `null` there means the catalogue does not hold that profile any more, which is
"unknown", never a default. Do not fill it in.

Two more, from outside that list:

* A task in `FAILED` or `DEAD_LETTERED` carries `error` in the result **and a
  `failure` block**, read from the per-attempt record. That block is what turns
  "task failed" into a report: the last attempt's backend, execution name, exit
  code, error and whether it came near an OOM, plus **every earlier attempt's**
  exit code and error. The earlier ones are not available anywhere else —
  `result_summary` is written once at terminal state, so a task that failed
  twice and succeeded on the third try carries only the third attempt's numbers.

  Three readings that are easy to get wrong:

  * **`exit_code: null` is NOT RECORDED, not 0.** Zero means the agent exited
    cleanly, which is the one thing it did not do. Say "not recorded".
  * **`failure.note`** means there are no attempt records at all: the task
    failed *before any agent ran*. Look at admission and dispatch, not at the
    prompt.
  * **`failure.attempts_unreadable`** means the route could not be read. The
    exit code is unknown, not absent — do not report the failure as having no
    exit code.

  `oom_near_miss` is the difference between "make the unit smaller" and "use a
  bigger resource class", and it is invisible in an exit code alone.
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

* Before a batch, read `swarm_capacity`. `ROOM` is how many more **agents** of
  that profile fit before the **binding** pool refuses — and if `ROOM` is an em
  dash, the real room is unknown and could be zero. Do not dispatch a large
  batch against a dash. `UNITS` beside it is the pool's capacity **units**, not
  agents: a `browser` or `large` agent takes more than one — the note under the
  table says how many, from the catalogue — so `4/10` can be two agents.
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

