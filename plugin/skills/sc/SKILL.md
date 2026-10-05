---
name: sc
description: The SwarmCloud front door, `/sc [verb]`. With no verb, show and interpret SwarmCloud cluster state — the subscription account pool and its 5-hour/7-day quota windows, pool ceilings and which pool binds each runner profile, agents running and queued, and what is wrong right now. `status <id>` reads one workflow or task once; `workflows` lists your tenant's workflows still running in SwarmCloud; `attach <workflow_id>` re-attaches live rows to a workflow still running in SwarmCloud and submits nothing, and `attach --all` does that for every running workflow of your tenant (at most 10 followed); `run <spec>` submits a workflow spec through /sc:swarmcloud and says so before it does; `run --issue owner/repo#N` plans a GitHub issue (one planner task) and `plan show|approve|edit|reject <run>` reads or acts on that plan, approving only the digest the developer was shown — `run` and `plan approve|edit|reject` are the verbs that write. Use when asked "what is the swarm doing", "how much quota is left", "why is my task queued", "is anything broken", "which account is nearly full", "where is my workflow", "what workflows are running", "show my workflow's rows again", before dispatching a long batch, to run a spec, or to plan and run a GitHub issue ("work on issue #N", "approve the plan").
argument-hint: "[status <wf_id|task_id> | workflows | attach <wf_id> | attach --all | run <spec path|JSON> | run --issue owner/repo#N | runs | plan show|approve|edit|reject <run>]"
arguments:
  - verb
  - target
allowed-tools:
  - Bash(uv run sc)
  - Bash(uv run sc overview:*)
  - Bash(uv run sc accounts:*)
  - Bash(uv run sc agents:*)
  - Bash(uv run sc capacity:*)
  - Bash(uv run sc task:*)
  - Bash(uv run sc trouble:*)
  - Bash(uv run sc whoami:*)
  - Bash(uv run sc config:*)
  - Bash(uv run sc debug:*)
  - Bash(uv run sc workflows:*)
  - Bash(uv run sc runs:*)
  - Bash(uv run sc run show:*)
  - Bash(uv run sc plan show:*)
  - Bash(sc)
  - Bash(sc overview:*)
  - Bash(sc accounts:*)
  - Bash(sc agents:*)
  - Bash(sc capacity:*)
  - Bash(sc task:*)
  - Bash(sc trouble:*)
  - Bash(sc whoami:*)
  - Bash(sc config:*)
  - Bash(sc debug:*)
  - Bash(sc workflows:*)
  - Bash(sc runs:*)
  - Bash(sc run show:*)
  - Bash(sc plan show:*)
  - Bash(uv run swarm doctor:*)
  - Bash(uv run swarm profiles:*)
---

# sc — SwarmCloud cluster state, and the front door

This skill reads only, except `run`, which submits and says so first, and
`plan approve|edit|reject`, which act on an issue run's plan only when the
developer asks for exactly that.

## The verb

`/sc` (or `/sc:sc` when another command already claims `/sc`) reads its verb
from the first word of `$ARGUMENTS` and its target from the rest. One verb,
one action — never a second one the developer did not ask for:

| `$ARGUMENTS` | What happens | Writes? |
|---|---|---|
| no verb (empty) | today's cluster state: `uv run sc`, read by the rules below | no |
| `status <wf_id\|task_id>` | one status read of that workflow or task | no |
| `workflows` | lists your tenant's workflows still running in SwarmCloud | no |
| `attach <wf_id>` | re-attaches live rows to a workflow running in SwarmCloud | no |
| `attach --all` | live rows for every running workflow of your tenant (at most 10) | no |
| `run <spec path\|JSON>` | submits a workflow spec, and says so first | **yes** |
| `run --issue owner/repo#N [--plan auto] [--auto-merge] [--fix-rounds N]` | plans a GitHub issue (one planner task), and says so first | **yes** |
| `runs` | lists your tenant's issue runs | no |
| `plan show <run>` | shows a run's plan and its digest | no |
| `plan approve\|edit\|reject <run>` | acts on that plan, with the digest the developer was shown | **yes** |
| a view name (`accounts`, `agents`, `capacity`, `trouble`, `task <id>`, `whoami`, `config`, `debug <id>`) | that view, as in "Which view" below | no |

Anything else: say which verbs exist and run nothing. `/sc:swarmcloud` keeps
working unchanged; these verbs are a shorter way in, not a replacement.

### `status <wf_id|task_id>`

ONE read, then report it. An id beginning `wf_` is a workflow: call
`swarm_workflow_status` with it once and report the workflow's `state` (null
means it was not derived — say that, never quote `stored_state`) and each
step's state. Any other id is a task: call `swarm_status` with
`task_ids: [<id>]` once and report its state. An error is reported verbatim:
an id the API does not have and another tenant's id read the same. Never poll;
a second look is the developer's next `/sc status`. Both tools are the sc
plugin's own read tools and work outside a checkout; they are not pre-granted
here -- this skill's grants mark it as the checkout-bound view surface -- so
the session may ask once before the first read.

### Every run is titled after its workflow

A SwarmCloud workflow run here is titled `SwarmCloud · <name> · N steps` (at
most 100 characters), not `swarmcloud`: <name> is the spec's `title`, else
its `label` cut at a word, else the title or label SwarmCloud stored, else the
workflow id. A run's name is fixed when it is launched, so `run` and `attach`
first call `swarm_workflow_launch`, which writes a copy of the
`/sc:swarmcloud` script with that name and returns its `script_path`, and
then launch THAT copy with the Workflow tool, `{scriptPath: <script_path>,
args: ...}`. If `swarm_workflow_launch` answers an error, say so in one line
and run the `/sc:swarmcloud` workflow by name with the same args: the rows are
the same, only the run's title is the generic one.

### `attach <wf_id>`

Call `swarm_workflow_launch` with `{attach: "<wf_id>"}`, then run the script it
returned with the args it returned — `{attach: <wf_id>}` plus the `title`
it chose, nothing else. It submits nothing: it reads the workflow once, reports
its finished steps once, starts a live row for every unfinished step, and ends
with the workflow's state. This is how a session that restarted gets its rows
back for a workflow that kept running in SwarmCloud. An unknown or
other-tenant id ends with the API's error; report it verbatim.

### `workflows`

ONE read of the caller's tenant's workflows that are not finished — every
one whose state is not `SUCCEEDED`, `FAILED`, `CANCELLED` or `DEAD_LETTERED`,
newest first. Call `swarm_workflows` once (the sc plugin's own read tool; it
works outside a checkout); in a checkout `uv run sc workflows` prints the
same list. Report each workflow's id, label, state, its current steps with
their states, its age and its console link, and each current step's console
link (a parked or queued one too), all as served — never build a link.
`complete: false` means the list stopped paging: say so. None running is an
answer, not an error. Never poll; offer `/sc attach --all` when any run.

### `attach --all`

ONE CLAUDE CODE RUN PER SWARMCLOUD WORKFLOW, never one run over several: each
workflow keeps its own step count, as the console shows it. Call
`swarm_workflow_launch` once with `{attach: "all"}`. It lists the caller's
tenant's running workflows once and, for the newest 10, writes one titled
script each; then make one Workflow call per entry of its `launches`,
`{scriptPath: <its script_path>, args: <its args>}` — each attaches its
workflow exactly as `attach <wf_id>` does: finished steps reported once, a
live row for every unfinished step; every row's label carries its task's
console link as served, from the moment the row starts, whatever the step's
state. More than 10 come back in `not_followed`, each with the `/sc attach
<wf_id>` that follows it, and are not launched — every row is an agent of its
own in this session, and past 10 workflows /workflows is no longer readable.
`count: 0` is an answer: nothing is running. Report each run's `state`
(`NOT_ATTACHED` with the API's error verbatim) and the `not_followed` list.
If `swarm_workflow_launch` answers an error, run the `/sc:swarmcloud`
workflow by name with args `{attach: "all"}`: it lists the running workflows
and returns `attach_calls`, the exact `{attach: "<wf_id>", title}` args of
each — run `/sc:swarmcloud` once per entry with those args. This is what the
plugin's SessionStart hook asks a new session to run first when workflows are
running; it submits nothing either way.

### `run <spec path|JSON>`

The one verb that writes: it spends the shared pool, a step at a time.

1. Load the spec. Given a path, read the file (relative to this session's
   directory) and parse it as JSON; given JSON text, parse that. Text that is
   neither is an error — say so and stop.
2. Say, before anything is submitted: "about to submit SwarmCloud workflow
   `<label or path>`: <n> steps (<step ids>) — they run remotely and spend the
   shared pool". Then run it; do not wait for a reply the developer did not
   ask to give.
3. Call `swarm_workflow_launch` with `{spec_path: "<its absolute path>"}`
   given a file, or `{spec: <the spec object>}` given JSON text, and launch
   the `script_path` it returns (see "Every run is titled after its
   workflow"). Run that script with the spec OBJECT as its args — never
   a bare path, and never the JSON as a string: a path makes a haiku agent
   retype the spec, which is how long specs came back altered and were
   refused. Given a file, the args are `{spec: <the spec object>, spec_path:
   "<its absolute path>"}`; given JSON text, `{spec: <the spec object>}`.
   `spec_path` beside the object is a reference, not the spec: the bridge
   reads those exact bytes for the submission and checks them against the
   digest of the object, so nothing is retyped and a spec that changed on the
   way is refused, not submitted.
4. Report what the workflow returned: its `workflow_id`, its state, and any
   `error` verbatim — `NOT_SUBMITTED`, `SUBMISSION_UNKNOWN`,
   `SUBMITTED_UNVERIFIED` and `FOLLOW_REFUSED` each say what to do next.

### `run --issue owner/repo#N`

Plans a GitHub issue and, once the plan is approved, runs it (#454). A planner
task reads the issue and the repository's open issues and pull requests and
writes a plan; the approved plan compiles into one workflow that opens a pull
request, whose CI the platform reads and fixes for up to `--fix-rounds` rounds
(3 by default). The issue gets a plan comment and one status comment, kept up
to date by the platform with the tenant's own forge credential — never one
this session supplies.

1. Say, before anything is submitted: "about to plan SwarmCloud issue run for
   `<owner/repo#N>`: one planner task, no capacity held by the plan until it is
   approved". Then call `swarm_run_issue` with `issue`, and `plan_approval:
   "auto"`, `auto_merge: true` or `fix_rounds` only when the developer gave
   `--plan auto`, `--auto-merge` or `--fix-rounds`. The terminal equivalent,
   `uv run sc run --issue <owner/repo#N> --follow`, is the developer's to run
   (this skill is not granted it); it prints one row per step as it moves.
2. `--auto-merge` is visible but disabled: the API refuses it until #295 and
   nothing is created. Report that refusal plainly, in its own words, and offer
   the same run without `--auto-merge`.
3. Report the run's `id` and `state`. A `required` run (the default) stops at
   `PLANNED`, holding no capacity, until the developer approves — go on to
   `plan show`. Once the run is `RUNNING` (or `FIXING`, a CI fix round), its
   answer carries `attach_with`: run `/sc attach <workflow_id>` for live rows.

### `runs`

ONE read: call `swarm_runs` (in a checkout, `uv run sc runs`) and report each
run's id, issue, state and pull request as served. Never poll.

### `plan show <run>`

ONE read: call `swarm_run` with `run_id` (in a checkout,
`uv run sc plan show <run>`), and show the developer the WHOLE plan — summary,
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

## The views

Every `sc` VIEW is read-only towards the cluster. A view never writes to it,
never refreshes an account's credential and never cancels anything, so every
view below is always safe to run. The one part of `sc` that writes is
`sc account ...`, which changes an account in the pool; this skill is not
granted it (see the last section).

It reads **the developer's own deployment**, not one this plugin knows about:
whatever they configured at install (`/plugin configure sc@swarmcloud`), or a
context they added. `uv run sc whoami` prints which deployment, from where,
and who they are on it. `sc login`, `sc logout` and `sc context` change the
developer's own sign-in and which cluster every later call reaches — they are
theirs to run, so **tell them the command; never run it yourself**. `sc login`
opens a browser and waits for them.

This skill is granted each **view** by name, not `sc` as a whole. The sign-in
and context commands live under the same `sc` prefix, so a grant for all of
`sc` would let this session sign the developer out or move every later
dispatch to another cluster without asking.

## Which view

| Question | Command |
|---|---|
| what is the swarm doing? | `uv run sc` (or `uv run sc overview`) |
| how much quota is left? which account? | `uv run sc accounts` |
| why is my task queued? what is running? | `uv run sc agents` |
| what is the real ceiling? | `uv run sc capacity` |
| what did this agent produce? | `uv run sc task <id>` |
| is anything broken? | `uv run sc trouble` |
| which deployment, and who am I on it? | `uv run sc whoami` |
| which endpoint, tenant, dispatch target and plugin version? | `uv run sc config` |
| why did this one task fail? | `uv run sc debug <id>` |
| which of my workflows are still running? | `uv run sc workflows` |
| what may I actually run? | `uv run swarm profiles` |

`sc config` shows where the dispatching tools send work — the session
`target` (cloud, local or hybrid) and which setting chose it — beside the
endpoint, the tenant, the plugin's version and the bridge's. A field it could
not read says `not read:` and why; report that, never a blank.

`sc debug <id>` is one task's diagnosis: its state and profile, every attempt
(generation, start and end, exit code, error), its last error, its newest
events and the tail of its newest attempt's log — all from the API, which
masks them, with the counts it masked. A line reading `withheld` is a string
the API did not say it masked, so it is not shown; say so, do not go looking
for it. A section that says `not read:` failed on its own; the rest of the
report is still good.

## Changing an account is the developer's to do

`uv run sc account pause <label>`, `resume`, `drain`, `remove` and `add` WRITE
to the shared account pool, and this skill is not granted them. Tell the
developer the command and let them run it: `drain` stops new assignments and
moves running agents to another account at their next turn boundary; `remove`
asks them to type the account's label back; `add` opens a browser for them to
sign in to Claude and asks them to paste a code, which only they can do.

The task view says what the agent produced before its code: each artifact by
name and size, the runner's own summary, the exit code, the duration, and the
inputs staged into it from upstream steps. "Artifacts are uploaded when the
attempt ends" means the task has not finished, not that it produced nothing.
Reading an artifact's content is the delegate skill's job, through the API,
which redacts it at read time; this skill lists and never fetches.

Add `--json` for the numbers, `--width N` to force a column count, `--ascii`
for a terminal without the bar glyphs. Put them **after** the view's name —
`uv run sc accounts --json`, `uv run sc overview --json` — because that is
what this skill is granted. A flag written before the view's name works too,
but asks the developer first.

`uv run swarm profiles` is the odd one out, listed here because it is read-only
and because it is the question people ask next. It reads the frozen catalogue
rather than the cluster, so it makes **no network call** and still answers when
nothing else does — which is exactly when someone is guessing at a profile name
because the API is unreachable. It shows no image and no command, deliberately:
a caller names a profile and the execution details follow from the name.

## Reading the marks — this is the part that matters

Three marks, and they are the same three `cs status` uses:

| Mark | Means | How to report it |
|---|---|---|
| `12%` | measured, recent enough to trust | quote it |
| `~12%` | the **last** measurement, too old to trust | say "projected", or give the reading's age. Never quote it as current |
| `—` | **not measured** | say "unknown". Never say zero, never say "fine" |

**Never turn an em dash into a number.** "This account has used none of its
quota" and "nobody has asked this account how much quota it has used" are
different claims, and collapsing them turns a dead poller into a healthy-looking
pool. If the operator needs the real figure and it is stale, say so and stop —
do not estimate it.

A section that reports it could not be read is a **finding**, not an absence.
Report it as such: "the account pool could not be read — utilisation is
unknown", not silence.

## Reading capacity

`BINDS ON` is the pool that will actually refuse the next task of that profile.
Admission is all-or-nothing across every pool a profile requires, so the ceiling
a profile feels is the **tightest** of them — not the global one, which is the
one people quote. `ROOM` is how many more **agents** of that profile fit before
the binding pool says no.

`UNITS` is that pool's capacity **units** in use out of its limit — not agents.
An agent takes its resource class's units, and the note under the table says
how many each class takes, read from the catalogue: a `browser` agent is more
than one unit, so `4/10` beside a `ROOM` of 3 is two browser agents, not four.
On a shared pool `UNITS` counts every tenant's work. The header's `tightest:`
names the profile with the fewest agents still to fit, and its pool's units.

A pool absent from the list is unlimited by construction and shows `∞`. A pool
whose `enabled` flag did not arrive shows `? not reported` rather than "open" —
a paused pool rendered as open is the one error this column exists to prevent.

`ROOM` is `—` whenever **any** required pool could not be graded — one that did
not report its ceiling, or did not report whether it is paused. Admission is
all-or-nothing, so one unknown pool makes the real room unknown, and it could
be 0. Do not fill that dash in from the other pools' numbers: they are the
pools that did not object.

## Reading accounts

Columns are `cs status`'s: `ACCOUNT | 5H USED | 7D USED | CLEARS | STATE`. Every
window percentage is **% used**, on every surface — the console and `sc` share
one polarity. `CLEARS` is when the **binding** window — the fullest one — rolls
over.

States: `available`, `paused`, `draining` (takes no new agents), `reauth needed`
(the credential is dead; only the quota-broker can fix it).

Every account in this pool is a **Claude subscription**. There is no other kind
and no choice to make about one, so there is nothing to ask the operator here.

Never ask for, echo, or store an account's credential. `sc` cannot show one:
the API shape carries no key material, not even a token's length, and `--json`
goes through the same allow-list the screen does. Rotating a credential
**revokes the previous one**, so quota-broker is the platform's single writer
for them — not `sc`, not swarm-api, and not this session.

## Exit codes

They are the machine-readable half of this surface, and every view uses them
the same way:

| Code | Means | Report it as |
|---|---|---|
| 0 | everything printed was read, nothing is down | healthy |
| 1 | something this view is about could not be read | "I could not read X" — an alert about the path to the cluster |
| 3 | it was all read, and something is **down** | "the cluster is broken, here is what" |

1 and 3 are different claims. A cluster nobody can reach is not a cluster that
is down, and reporting either as the other sends someone to the wrong place.

## When sc cannot connect

`sc` exits 1 and prints nothing to stdout, on purpose: a screen of dashes would
look like a reading. Run `uv run swarm doctor` — it reports which deployment it
resolved and from where, which auth tier this machine is on and what that tier
can reach, which is almost always the real answer.

**`sign-in required for <context>: run sc login`** is the commonest answer on a
new machine and is not a fault: the deployment takes a signed-in developer and
none is signed in. Tell the developer to run `uv run sc login` themselves (a
browser window opens), then try again. Do not treat it as the cluster being
unreachable, and do not look for another credential to use instead.

### "I cannot reach it" is not "it is down" — say the first one

This is the single most likely wrong report from this plugin, so it is worth
being exact. On a **team** deployment the API is behind IAP at a load balancer,
and `uv run swarm doctor` prints which door it used and what it presents:

```
front door  https://swarm.saga.xyz  (IAP; takes an OAuth ACCESS token)
api         UNREACHABLE
            GET /v1/tenants/me -> 401: ... Error code 900
```

Read the refusal, because the three of them mean three different things:

| What comes back | What it means |
|---|---|
| **401, IAP error code 900** | IAP did not accept the token at all. A gcloud *user* credential cannot pass: the deployment's IAP uses a Google-managed OAuth client, which admits only allowlisted programmatic clients. The developer signs in with `uv run sc login` (the deployment's Desktop OAuth client); CI sets `SWARM_IMPERSONATE_SA`. A 401 **after** `sc login` means the deployment has not allowlisted its own client — an operator's one-time step |
| **403 that NAMES the caller** | IAP **authenticated** you and the principal is not on the list. One `roles/iap.httpsResourceAccessor` grant away — `frontend_iap_members` in `terraform/bootstrap/terraform.tfvars`, which the owner applies and CI does not |
| **an HTML 404** | the wrong ADDRESS, not a missing route. Cloud Run's `*.run.app` hostname refuses everyone outside the VPC and renders the refusal as 404 |

None of those is the cluster being down. **Say "I cannot reach the API from
here, and this is why", never "SwarmCloud is broken"** — and if someone needs
the numbers now, the console at the front-door host is signed in to the same
API in a browser, where the session cookie is a credential this bridge does not
have and must not go looking for.

Exit code 1 is exactly this case, and exit code 3 is the other one. That is the
whole reason they are different numbers.

If accounts specifically are unreadable while everything else works, this
deployment's swarm-api has no `/v1/accounts` route: the pool is **unknown from
here**, not empty, and the cluster is otherwise fine — `sc` and `sc trouble`
say so as a note and still exit 0. There is no environment variable that makes
`sc` try a second host, and asking for one would be asking it to send a bearer
token minted for swarm-api somewhere else. An operator on the VPC who wants to
read quota-broker directly points `SWARM_API_URL` at it, so the token is minted
for the host that receives it.
