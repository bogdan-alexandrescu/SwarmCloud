# Merging without an operator laptop

**Status: PROPOSED 2026-11-12, design only.** The owner chose on 12 November
2026 to build both parts, and to write the design first. Part one closes the
three gaps that still need an operator's laptop. Part two runs a merge train
in SwarmCloud for any registered repository. Nothing described here is built
yet. Each fork below has two or three choices, and one is marked
**recommended** with the reason. §11 lists the build lanes in order.

---

## 1. Decision record

| # | Decision | Recommended choice | Status |
|---|---|---|---|
| D0 | Scope | Both parts; design first (owner, 2026-11-12) | decided |
| D1 | How the merge step proves a follow-up agent's commit | A recorded head with a chain proof, from its signed `publish_to_pr` (§3.2 A) | proposed |
| D2 | What happens to the `ready` label | The drop stays; the merge step re-boards at the accepted head (§3.4 A) | proposed |
| D3 | Where a workflow waits for another's merge | A held-workflow ledger row, released by the merge-wake tick (§4.2 A) | proposed |
| D4 | How the wait is declared | `after_merge` on the workflow spec, by workflow id or pull request number (§4.1) | proposed |
| D5 | Where the SwarmCloud train runs | Extend the existing platform coordinator, `swarm_api.mergetrain` (§6.1 A) | proposed |
| D6 | What the train boards | Merge-step entries, plus `ready` pull requests labelled by someone with write access (§6.2 B) | proposed |
| D7 | Where its settings live | The workspace merge setting, with a per-repository override (§7 A) | proposed |
| D8 | Coexisting with a repository's own train | The repository's own switch wins; one train per base (§8 A) | proposed |
| D9 | Who lands | The workspace's own forge credential, with no ruleset bypass (§9 A) | proposed |

No question is put to the owner. Every fork has a choice the facts below
support. The owner can still pick a different one: each choice says what it
would change.

---

## 2. Today, and the facts this rests on

**The repository-level train.** In this repository the train is a GitHub
Actions workflow running a script. The script boards green `ready` pull
requests (`scripts/merge-train.sh` (`cmd_select() {`)) after a gather wait
(`scripts/merge-train.sh` (`gather_waits() {`)). It reads the base's required
checks from its rules (`scripts/merge-train.sh` (`required_contexts() {`)),
composes `train/<run>` and tests it through a draft train pull request
(`scripts/merge-train.sh` (`cmd_compose() {`)). On red it retries the failed
jobs once (`scripts/merge-train.sh` (`retry_failed_runs() {`)), holds while
the base is red (`scripts/merge-train.sh` (`main_red_gate() {`)), then
bisects (`scripts/merge-train.sh` (`cmd_plan() {`)) and ejects
(`scripts/merge-train.sh` (`eject_pr() {`)). It lands in order, pinned to
each tested head (`scripts/merge-train.sh` (`land_train() {`)). Merging needs
the merge App's token: a merge made with `GITHUB_TOKEN` starts no workflow
(docs/runbooks/merge-app.md).

**The platform train exists.** `swarm_api.mergetrain` is already built
(docs/merge-trains.md). It runs one lane per tenant, repository and base
(`apps/swarm-api/swarm_api/mergetrain.py::lane_id_for`) and is advanced on
the per-tenant merge-wake tick (`apps/swarm-api/swarm_api/mergetrain.py::tick_tenant`,
called from `apps/swarm-api/swarm_api/routes/admin.py::wake_merges`). Its
rules (`select`, `judge`, `bisect`, `on_failure`) are ported from the script.
It compares the tested tree with the base's tree after landing
(`apps/swarm-api/swarm_api/mergetrain.py::_Lane._land`).

It boards only entries that a merge step in `train` mode registered
(`apps/agent-worker/agent_worker/mergetrain.py::register_entry`). Compared
with the script, it has no gather wait, no retry of failed jobs, no hold
while the base is red, and it posts no commit status. Its settings are
`mode`, `train_size` and `max_train_age_minutes`
(`apps/swarm-api/swarm_api/mergesettings.py::MergeSettings`).

**The merge step's head check.** `apps/agent-worker/agent_worker/merge.py::_accept`
accepts exactly these heads:

- the head the workflow pushed (`apps/agent-worker/agent_worker/merge.py::_pushed_head`);
- the heads of its fix rounds (`apps/agent-worker/agent_worker/merge.py::_fix_round_heads`, `apps/agent-worker/agent_worker/merge.py::_ci_fixer_heads`);
- the heads swarm-api recorded for the pull request (`apps/agent-worker/agent_worker/merge.py::_recorded_fix_heads`);
- a pure base merge on top of any of these (`apps/agent-worker/agent_worker/merge.py::_updates_onto`).

A recorded fixer counts only if its **signed** dispatch block `continues` the
branch agent (`apps/agent-worker/agent_worker/merge.py::_fixer_head`).
swarm-api writes those records on its tick
(`apps/swarm-api/swarm_api/cifix.py::record_fix_heads`). Any other head
either ends `superseded_by_person_push` or fails `head_moved` in
`apps/agent-worker/agent_worker/merge.py::_with_forge`.

**Why a follow-up agent is refused (gap 1).** A follow-up agent lands on the
lane's pull request through `publish_to_pr` (`apps/swarm-api/swarm_api/publishtopr.py::DISPATCH_FIELD`).
Its signed block names `{number, task_id}`, not `continues`, so
`_fixer_head` never counts it. Nothing records its head either:
`record_fix_heads` reads only the fix rounds marked at submission.

That leaves the person-push path. `apps/agent-worker/agent_worker/merge.py::_person_lane`
accepts a `publish_to_pr` agent only if it carries none of
`apps/agent-worker/agent_worker/merge.py::LANE_WRITER_MARKS`, and only if
its committer is one of the pull request's people. Even then, the outcome is
`apps/agent-worker/agent_worker/merge.py::_superseded`, a refusal that hands
the merge to a person. Every other case is `head_moved`, with the clause
from `apps/agent-worker/agent_worker/merge.py::_unnamed`: "which is not a
person's plain lane on this branch".

The worker already records what a follow-up proof needs. A `publish_to_pr`
step records the head it started from, `started_head`, in
`apps/agent-worker/agent_worker/lifecycle.py::Worker._publish_git`. It pushes
only a fast-forward of that head (`apps/agent-worker/agent_worker/publishpr.py::head_moved`),
leased on the same sha.

**Why the label drops (gap 1, the label).** On any new head that is not a
pure base merge, `auto-merge.yml` drops `ready`
(`.github/workflows/auto-merge.yml` (`- name: drop the stale ready label and say why`)).
`ready-on-green` survives a push, and the workflow swaps it for `ready` once
the head is green
(`.github/workflows/auto-merge.yml` (`- name: swap ready-on-green for ready`)).
A merge step whose repository runs its own train boards by adding `ready`
itself (`apps/agent-worker/agent_worker/merge.py::_board`). It does this
only when the train switch is on
(`apps/agent-worker/agent_worker/merge.py::REPOSITORY_TRAIN_VARIABLE`).

**Why queued lanes need the operator (gap 2).** `depends_on`
(`apps/swarm-api/swarm_api/schemas.py::WorkflowStepCreate`) orders steps
*within* one workflow. Nothing on `apps/swarm-api/swarm_api/schemas.py::WorkflowCreate`
waits for another workflow's pull request to merge. The one existing wait on
a merge is a child issue run's `after_merged`
(`apps/swarm-api/swarm_api/children.py::ChildIssueRun`). It is a `HELD`
ledger row with no run, workflow or capacity
(`apps/swarm-api/swarm_api/children.py::HELD`). `wake_merges` releases it
(`apps/swarm-api/swarm_api/children.py::ChildService.release_held`) after
the merge-wake tick. docs/lane-queue.md §3 designs a lane-level
`depends_on` that waits for a merge, which is not built.

**Why other repositories need the laptop (gap 3).** A registered repository
(`apps/swarm-api/swarm_api/repositories.py::register`) with no train workflow
gets only the merge step, which merges directly or registers in the platform
train. A pull request that no merge step owns (a person's, or a lane whose
step was refused) has no one to merge it but the laptop.

---

## 3. Part one, gap 1: the merge step accepts a recorded follow-up agent's commit

### 3.1 What "a recorded SwarmCloud follow-up agent" means

A follow-up agent's head is accepted only if all of the following hold:

- It is an agent of this tenant, with a workflow that is not this merge's own.
- Its **signed** dispatch block carries `publish_to_pr` with
  `task_id` equal to the branch agent (the `swarm/<task>` the pull request is
  on) and `number` equal to this pull request.
- Its signed `repository_url` is this merge's repository.
- It verifies against its own signed workflow (`verify_upstream`, as
  `_fixer_head` does).
- It ended `SUCCEEDED`.
- It recorded both a `pushed_head` and the `started_head` it fast-forwarded
  from.

"Recorded" means that swarm-api has written its head into the branch agent's
`pull_request_fix_heads` list with `kind: "follow_up"`. This is the same list
and the same tenant-gated read as a fix round's.

### 3.2 How it proves the commit is that agent's

**A. A recorded head with a chain proof (recommended).** At submission,
`publishtopr.resolve` marks the agent `pr_fix_head: {kind: "follow_up",
state: "pending"}`, as `cifix` marks a fix round. When the agent is terminal,
`record_fix_heads` appends `{task_id, head: pushed_head, started_head,
kind: "follow_up"}`.

The merge step adds a candidate only when the entry's agent passes the
checks in §3.1. A widened `_fixer_head` reads `publish_to_pr.task_id` and
`number` as well as `continues`. The step also requires that the
**`started_head` was itself an accepted head**: the pinned head, a fix
round's head, an earlier follow-up's head, or a pure base merge onto one of
them. The walk is `_accept` run on `started_head`, so no new purity code is
needed.

The chain is what makes this safe. The follow-up fast-forwarded from
`started_head`, so every commit between that head and its push is the
follow-up's own work. An unknown commit someone pushed *before* the
follow-up started fails the walk at `started_head`, and the head stays
`head_moved`.

*Why recommended:* it reuses the record, the read and the purity walk that
already admit fix rounds. Every fact it trusts is either signed or the
forge's own answer. The one new tenant-writable field, `started_head`, can
only narrow what is accepted, because it must itself pass `_accept`.

**B. The commit's last line names the agent.** Accept any worker commit whose
`Made by the worker for task …` line names an agent that passes §3.1. This is
`_person_lane` without the person check. It is cheaper, with no swarm-api
record. But anyone who can push can write that line on any content, and
nothing ties the sha to the agent's push. *Not recommended:* it trusts text
in the commit.

**C. Commits the platform signs.** The worker signs commits with a platform
key that GitHub verifies. This proves that "the platform made it", but not
which agent or which pull request. It also adds a key to rotate and a secret
to every Job. *Not recommended:* it adds a credential and still needs A's
record to name the agent.

### 3.3 What stays refused

Each of these still ends `head_moved`, or `superseded_by_person_push` where
that path applies today:

- a person's push on top of a follow-up's head, which stays superseded (#2505's decision is unchanged);
- a follow-up whose `started_head` is not an accepted head, meaning someone else's commit was already under it;
- a commit on top of a follow-up's head that is neither recorded nor a pure base merge;
- a follow-up of another tenant, another repository, another pull request number, or another branch agent;
- a follow-up that ended `FAILED` or `CANCELLED`, even if it pushed. A failed follow-up's push is the failure-review's to read, not the merge's to land;
- an agent carrying a writer's mark (`LANE_WRITER_MARKS`). These are rounds, recorded and checked as rounds today;
- a follow-up that is still running. On a `head_moved` wake, the step re-parks `follow_up_pending` instead of refusing when the live head's last line names a non-terminal `publish_to_pr` agent for this branch agent. It accepts or refuses once that agent ends. The park holds no lease, no pool count and no merge slot, like `ci_fix_running` (invariant 1). It is bounded by the follow-up agent's own deadline, because a terminal agent ends it.

Accepting the head never skips the checks. The step goes on to read the
required checks at the new head, and waits on `CI_PENDING` as for any other
accepted head.

### 3.4 Keeping or restoring the `ready` label

**A. The drop stays; the merge step re-boards (recommended).** auto-merge.yml
keeps dropping `ready` on any unproven new head, which is the fail-safe.
Once the step accepts the follow-up's head (§3.2) and reads its checks green
with the review's MERGE, it reaches `_board` again and adds `ready` again,
exactly as it does after a fix round's new head. There is no workflow
change, no second proof outside swarm-api, and no laptop.

*Why recommended:* the label is only ever added by the code that holds the
proof. Nothing under `.github/` changes.

**B. auto-merge.yml keeps `ready` for a recorded follow-up.** The disable job
would ask swarm-api whether the new head is accepted. That needs a
credential for swarm-api in the repository's workflow, and the same proof in
two places. *Not recommended.*

**C. swarm-api adds `ready-on-green` when it records the head.** The existing
swap job then adds `ready` once green. This works only where that job
exists, which is this repository behind the merge App, and it labels before
the merge step has accepted the head. *Not recommended* as the mechanism; A
covers every repository.

---

## 4. Part one, gap 2: a workflow that starts when another's pull request merges

### 4.1 How it is declared in a spec

`WorkflowCreate` gains `after_merge`, a list of at most 8 entries. Each entry
is one of:

- `{"workflow_id": "wf_..."}`: a workflow of the **same tenant**. It is satisfied when the pull request its branch agent opened is merged.
- `{"pull_request": N}`: a pull request of the spec's own repository, a number only, as `publish_to_pr` takes it. It is satisfied when GitHub reports it merged.

The submitter sends a name and a number. No image, command or resource
travels with either (invariant 10).

At submission, swarm-api refuses three things, as `publishtopr` and
`issueruns._dependency_path` do: a cycle, another tenant's workflow (refused
in the same words as a missing one, invariant 9), and another repository's
pull request.

**A merge, not a finish.** A predecessor that ended green with its pull
request still open is not enough. Its code is not on the base until it
merges (docs/lane-queue.md §3).

### 4.2 Where the wait lives

**A. A held-workflow ledger row (recommended).** On submission the spec is
validated in full, but **no workflow and no agent is written**. Instead a
`held_workflows/{id}` document records:

- the tenant;
- the submitter's principal;
- the validated spec;
- `after_merge`;
- `state: HELD`.

The merge-wake route releases it on the same per-minute tick that already
releases held child runs (`wake_merges`, after `tick_merge_trains` and
`children.release_held_runs`). A row is checked only once Firestore shows
each predecessor concluded: its merge step `SUCCEEDED` with a merged pull
request, or its train entry `LANDED`. Only a `{"pull_request": N}` entry
costs a GitHub read, and only once per tick.

Release goes through the ordinary submission path, as that submitter: the
spec is validated again, signed and written then. The base it starts from is
the base's head at release, which contains every merge it waited for. A
predecessor that can no longer merge (failed, cancelled, or pull request
closed unmerged) moves the row to `BLOCKED`, which also holds nothing.
Nothing is cancelled in a cascade.

*Why recommended:* it is the `HELD` pattern `children.py` already runs in
production, on the tick that already reads merges. Because nothing is signed
until release, a submitter removed in the meantime starts nothing.

**B. Agents written up-front, `PARKED` on `DEPENDENCY_INCOMPLETE`.** A parked
agent costs nothing either (invariant 1). But the spec is signed at
submission against a base that does not yet hold the predecessor's code. The
scheduler's dependency check would need a cross-workflow merge condition,
and every queued workflow would show agents that may never run. *Not
recommended.*

**C. A GitHub webhook on `pull_request` closed-and-merged.** Release is
immediate rather than up to a minute later. But it adds an ingress that must
authenticate GitHub's signature per repository, and it still needs A's
ledger and a tick as backstop, because webhooks are not guaranteed. *Not
recommended now*; it can be added to A later as a fast path.

### 4.3 What it holds while it waits: nothing

A `HELD` or `BLOCKED` row is a Firestore document. It is not an agent, so it
is never `QUEUED`, `LEASED` or `RUNNING`. It holds no pool count, no lease
and no merge slot, and it makes no pending pod. Only the tick reads it.
Invariant 1 holds because nothing in the `LEASED`…`RUNNING` set exists until
release. Invariants 2 and 3 are untouched, because a released workflow is
admitted like any other.

---

## 5. Part one, gap 3: the other registered repositories

Gap 3 is closed by part two. A repository whose workspace turns the
SwarmCloud train on is merged by it. A repository left in `direct` mode is
merged by its merge step, which already needs no laptop.

---

## 6. Part two: a merge train in SwarmCloud

### 6.1 Where it runs

**A. Extend `swarm_api.mergetrain` (recommended).** The lanes, the pure rules,
the per-tenant tick, the `swarm-train/` branch, the draft pull request, the
pinned squash and the tree check are already built and tested
(tests/unit/control_plane/test_merge_train_coordinator.py). §6.3 adds what
the script has and the coordinator lacks. *Why:* the rules are ported once
and stay in one place. It needs no pod, and a waiting train is two kinds of
Firestore document.

**B. Run `scripts/merge-train.sh` in a SwarmCloud Job per repository.** This
reuses the script as is. But a pod would run for every tick of every
repository, which is an always-on demand that invariant 1 exists to avoid,
and the script's `gh` and Actions assumptions (rerun by workflow) do not
hold outside Actions. *Not recommended.*

**C. GitHub's native merge queue.** It needs no code. It is refused on a
user-owned repository (docs/ci.md, "Merging through the merge queue"), it
cannot bisect by halves, and it varies by plan. *Not recommended* as the
general feature.

### 6.2 Boarding

In each registered repository in `train` mode, with its own train switch off
(§8), the tick boards:

- the merge-step entries it boards today;
- a pull request that carries `ready` and that no merge step registered.

**A. Merge-step entries only** (today). Gap 3 stays open for any pull
request that has no merge step.

**B. Entries plus `ready` pull requests (recommended).** A `ready` pull
request boards only if all of these hold:

- it is open, not a draft, and not a fork;
- it is based on the lane's base;
- it is green at its head with the repository's own required checks (§6.3);
- it was mergeable when last read;
- the **last** `labeled ready` event on its timeline was made by an account
  with `write` or higher on the repository. This is read with the
  collaborator-permission call, so that a triage-only account cannot board
  a pull request.

A merge-step entry still boards first, in registration order. A labelled
pull request boards in label order. *Why recommended:* this is how the
repository-level train already boards, so a repository moved onto the
platform keeps its habit, and it closes gap 3 for human pull requests.

**C. Boarding from the console only.** A person presses *board* on a pull
request. This is explicit, but it is a manual step for every pull request,
which is the laptop's job moved to a browser.

### 6.3 Build, test, bisect, land, post

1. **Compose.** This is today's `_Lane._compose`. The branch is
   `swarm-train/<lane>-<seq>` at the base's head. Each rider's head is merged
   in order, and a DRAFT pull request is opened from it into the base, so
   the repository's own `pull_request` CI runs on the combined tree.
2. **Run the repository's own CI.** The required checks are read on every
   tick from the base's rules and classic protection
   (`apps/swarm-api/swarm_api/forgechecks.py::required_status_checks`, as
   `_Lane._reading` does), never from a stored list, so a change to the
   rules applies on the next tick. There are three additions:
   - **A required check that never starts.** If a required check has no run
     on the composed head `required_start_minutes` (default 15) after
     compose, the train fails `required_check_not_started` and names it. A
     check that skips drafts or filters on branch names is the usual cause,
     and waiting out `max_train_age_minutes` would only hide it.
   - **A required context only the train produces** (the `merge-train`
     status this repository's ruleset requires). The train posts it at each
     rider's head just before landing that rider, as
     `scripts/merge-train.sh` (`land_train() {`) does.
   - **Retry once.** A red train first re-runs its failed jobs once, through
     the Actions re-run API with the landing credential (§9), before
     bisecting. This ports `retry_failed_runs`; a flaky check should not
     eject a culprit-free train.
3. **Bisect on red.** This is today's `on_failure`: a train of one is ejected,
   and a larger one is halved. It also gains the script's **base-red hold**:
   while the base's own required checks are red at its head, a red train is
   held and not bisected, up to `base_red_wait_minutes` (default 60). A red
   base makes every train red.
4. **Land in order.** This is today's `_Lane._land`: each rider is
   squash-merged pinned to its tested head, in train order, and then the
   base's tree is compared with the tested tree.
5. **Post what the rules require, and say why.** On landing, the train posts
   the required context named in step 2, if any. On an ejection it posts one
   comment per head, giving the reason and the failing checks, with a hidden
   marker so that it says so once. It also removes `ready` from a labelled
   pull request. A merge-step entry's step reads its own outcome instead, as
   today. A pull request that cannot board is told why, once per head and
   reason, as `scripts/merge-train.sh` (`tell_skip() {`) does.

---

## 7. Its settings

| Setting | Default | Bounds | Why this default |
|---|---|---|---|
| `train_size` (maximum) | 4 | 1–10 | Today's value. Four riders cost one CI run, and a red one costs about two more to find the culprit. |
| `gather_min` (gather size) | 2 | 1–`train_size` | One rider is direct mode with extra steps. Two halve the CI runs without holding a quiet repository. |
| `gather_wait_minutes` (wait) | 10 | 0–60 | A ready pull request boards after this even if `gather_min` is not reached, so a lone change is never held long. The repository-level train's 6-or-20 suits this repository's volume, not a typical one. |
| `max_train_age_minutes` | 90 | today's | Unchanged. |
| `required_start_minutes` | 15 | 5–60 | §6.3 step 2. |
| `base_red_wait_minutes` | 60 | 0–240 | The script's `MERGE_TRAIN_MAIN_RED_WAIT_MINUTES`. |

**A. The workspace merge setting, plus a per-repository override
(recommended).** `MergeSettings` gains the four new scalars. The repository
document's `merge_mode` override gains the same keys. The setting is read,
written, MAC'd and audited exactly as today (docs/merge-trains.md §3). *Why:*
one place, one audit trail, and it is already who-may-change-it-checked.

**B. A file in the repository** (for example `.swarm/merge-train.json` on
the base). The repository's maintainers own it. But it is read from a branch
anyone with push can change through a pull request, and a train would land
the change to its own settings.

**C. Both, with the file winning.** This has B's weakness plus a second
place to look.

---

## 8. Coexisting with a repository that has its own train

**A. One train per base, and the repository's own switch wins
(recommended).** The merge step already reads the repository variable
`MERGE_TRAIN` (`REPOSITORY_TRAIN_VARIABLE`) and boards the repository's own
train when it is `on`. The SwarmCloud train reads the same variable once per
tick per lane. While it is `on`, the lane composes nothing and reports
`repository_train_on`. Turning the variable off hands the repository to the
platform on the next tick. There is never a moment with two trains
composing on one base. Migrating this repository then needs three steps:
turn the workspace train on, add the `merge-train` context to what the
platform posts, and turn the variable off. *Why:* a single switch that both
sides already read, which needs no `.github/` change.

**B. The platform refuses `train` mode for such a repository.** A settings
write naming it is refused with `repository_train_on` until its own
workflow is retired. This is safe, but the settings write would need a
forge read inside it, and the refusal is stale once the variable changes.

**C. Both trains run, and each boards what the other did not.** They would
race for the same base, and each one's landing makes the other's tested tree
stale. *Not recommended.*

---

## 9. Who may land

**A. The workspace's own forge credential, `swarm-tenant-<tenant>-git`
(recommended).** It is read by name from Secret Manager
(`apps/swarm-api/swarm_api/forge.py::ForgeTokens`), as today's train and
merge step read it. It needs the rights docs/multi-tenancy.md lists ("What
the forge credential must be allowed"): contents read/write, pull requests
read/write, checks read, commit statuses read, and actions read/write.
Posting a required context also needs **commit statuses write**, and the
setting screen states this.

A merge made with it is a person's or an App's, not `GITHUB_TOKEN`'s, so
the base's own workflows start. The train does not rely on GitHub to refuse
a bad merge. It merges a rider only after its own judge has read every
required check green at the tested head, and the merge is pinned to that
head. It should still **not** be a ruleset bypass actor, so that a merge
made outside the train cannot skip the rules either. Reading a ruleset's
bypass list needs admin rights, so the lane cannot check this itself. The
setting screen states the requirement instead. *Why
recommended:* the train lands only what the step would have landed alone,
with the same token (docs/merge-trains.md §6, invariant 9). It adds no new
credential, and the token is never written to the repository, a tfvars
file, a Job or a log.

**B. A SwarmCloud App installation token with write.** Today's installation
tokens are minted read-only, per repository
(`apps/swarm-api/swarm_api/forge_installation.py::InstallationTokens`).
Minting write tokens needs a change to the App's permissions, which every
installer must accept. Merges would then be the App's, not a person's.
Worth it only if workspaces object to a person's token landing; it can
replace A later without changing the train.

**C. The labeller's own connected token.** A pull request labelled by a
person would land as that person. This gives fine attribution, but a train
of four would land with four tokens, and one person's expired connection
would stall the train.

---

## 10. What it costs

- **No pod and no capacity.** The train is a step of the per-tenant
  merge-wake tick in swarm-api, which already runs every minute. A waiting
  train, an entry, a held workflow and a parked step hold no lease and no
  pool count (invariant 1).
- **CI.** There is one run of the repository's own CI per train instead of
  one per pull request. A green train of `train_size` 4 costs a quarter of
  direct mode. A red train costs about log2(N) more runs, plus the one retry.
- **Forge calls.** Each active lane makes about 4 to 8 GitHub calls per tick:
  the pull request list, the check runs and statuses at the composed head,
  and the rules read. That is about 240–480 calls per hour per active lane,
  and an idle lane makes none. Against GitHub's 5,000 per hour per token,
  one workspace token carries about 10 busy lanes, and `MAX_LANES_PER_TICK`
  (20) bounds a tick. The boarding read for `ready` pull requests is one list
  call per lane per tick.
- **Firestore.** A tick reads each tenant's queued and riding entries, its
  lanes and its held workflows: tens of documents per tenant per minute.
- **swarm-api CPU.** This is marginal: seconds per tick per tenant inside
  the existing request.

---

## 11. Build lanes, in order

Each lane pushes its red-first test alone first, then the fix (CLAUDE.md
"Lanes").

| # | Lane | Files | Red-first test | Waits on |
|---|---|---|---|---|
| 1 | Record a follow-up's head | `apps/swarm-api/swarm_api/publishtopr.py`, `apps/swarm-api/swarm_api/cifix.py` | `tests/unit/control_plane/test_follow_up_heads_recorded.py`: a `SUCCEEDED` `publish_to_pr` agent's `pushed_head` and `started_head` appear in the branch agent's `pull_request_fix_heads` as `follow_up`; a `FAILED` one does not | nothing |
| 2 | Accept a recorded follow-up | `apps/agent-worker/agent_worker/merge.py`, `apps/swarm-api/swarm_api/mergewake.py` | `tests/unit/worker/test_merge_follow_up_head.py`: a head a recorded follow-up pushed from an accepted `started_head` is accepted; one from an unproven `started_head`, of another number, or failed, stays `head_moved`; a live one parks `follow_up_pending` | 1 (its record's shape) |
| 3 | Re-board at the accepted head | `apps/agent-worker/agent_worker/merge.py` | `tests/unit/worker/test_merge_follow_up_reboards.py`: after 2's acceptance and green checks, `_board` labels `ready` once at the new head | 2 |
| 4 | Held workflows (`after_merge`) | `apps/swarm-api/swarm_api/schemas.py`, `apps/swarm-api/swarm_api/heldworkflows.py` (new), `apps/swarm-api/swarm_api/routes/admin.py` | `tests/unit/control_plane/test_held_workflows.py`: a spec with `after_merge` writes no agent; the tick releases it after the predecessor merges, blocks it after a close, and refuses a cycle and another tenant's id | nothing (parallel with 1) |
| 5 | Gather settings | `apps/swarm-api/swarm_api/mergesettings.py`, `apps/swarm-api/swarm_api/mergetrain.py` | `tests/unit/control_plane/test_merge_train_gather_platform.py`: one rider waits until `gather_wait_minutes`; `gather_min` riders board at once; malformed values read as defaults | nothing |
| 6 | Rules parity | `apps/swarm-api/swarm_api/mergetrain.py`, `apps/swarm-api/swarm_api/forgechecks.py` | `tests/unit/control_plane/test_merge_train_parity.py`: a red train retries once before bisecting; a red base holds; a never-started required check fails `required_check_not_started`; a train-only context is posted before each landing | 5 |
| 7 | Board `ready` pull requests | `apps/swarm-api/swarm_api/mergetrain.py` | `tests/unit/control_plane/test_merge_train_board_label.py`: a `ready` pull request labelled by a write collaborator boards; one labelled by a triage account, a draft or a fork does not | 5 |
| 8 | Coexistence and the landing check | `apps/swarm-api/swarm_api/mergetrain.py` | `tests/unit/control_plane/test_merge_train_coexist.py`: with the repository's `MERGE_TRAIN` on, the lane composes nothing and reports `repository_train_on`; a rider whose required checks were not read green at its tested head is never merged | 7 |
| 9 | Settings screen fields | `apps/swarm-ui` (the Merging settings screen, lane TRAIN-WS-UI) | that screen's test for the four new fields | 5 |
| 10 | Migrate this repository | docs/merge-trains.md, docs/runbooks/merge-train.md | the runbook's dry run on the platform train (no unit test: an operator step) | 1–8, and the owner's go |

Every lane runs offline unit tests only (no cloud credentials, no emulator).
A change to admission, dispatch or reconciliation is not among them. Lanes
2, 4 and 8 touch the merge and release paths, so each one also runs
`make smoke concurrency-test race-test` against a deployed environment
before it is called done.

---

## 12. Invariants

1. A held workflow, a `follow_up_pending` park, a waiting train and its
   entries are Firestore documents, not demand (§4.3, §3.3).
2. and 3. Untouched: everything released or woken is admitted as today.
4. Every wait is a park or a ledger row; no worker sleeps through one.
5. The new `_accept` candidates are read inside the fenced attempt, and every
   label and merge stays behind the fencing recheck.
9. Every new read filters on the tenant and re-checks the document. A
   follow-up of another tenant reads as no candidate, and an `after_merge`
   naming another tenant's workflow reads as missing.
10. `after_merge` and `publish_to_pr` carry ids and numbers only. The new
    settings are bounded scalars.

**Frozen contract.** Nothing here needs a change to
`apps/common/swarm_common/`. `after_merge` lives on swarm-api's request
schema and in the held row. `follow_up` is a value of an existing metadata
field. `follow_up_pending` is a `merge_wait.code` under the existing
`CI_PENDING` park. If the owner wants `after_merge` typed on the frozen
`Workflow`, that is a request for docs/contract-change-requests.md, not part
of these lanes.
