# Merge trains: how a workspace's pull requests land in batches

Lane TRAIN-WS, owner decision 2026-10-14: *"the merge train should be a
setting users manage on their own workspace and admins on all workspaces"*.
Code: `apps/swarm-api/swarm_api/mergesettings.py` (the setting),
`apps/swarm-api/swarm_api/mergetrain.py` (the coordinator),
`apps/agent-worker/agent_worker/mergetrain.py` (the merge step's entry), and
the train branch in `agent_worker.merge._with_forge`.

## 1. Why

A base branch that requires its checks green on the merged result lets one
pull request land per CI run. The merge slot (docs/merge-step.md, merge race
#295) made that safe -- one merge step per base at a time -- but it is still
one full CI run per landing. With a wave of lanes finishing at once, the
queue is as long as the wave times the CI time.

A merge train tests N ready pull requests TOGETHER: one CI run on the head
that merges all of them, and if it is green, each lands on its own. With N=4
and a mostly-green wave, that is a quarter of the CI runs. When a train is red,
bisection finds the pull request that broke it, and only that one is ejected;
the others still land.

It is a per-workspace choice, not a platform default. A train changes how
work lands: a pull request waits for its batch, and a red neighbour costs it
a re-test. So direct stays the default and each workspace owner opts in.

## 2. Two trains, and which is which

| | the repository-level train (PR 1294) | the platform train (this document) |
|---|---|---|
| what | `scripts/merge-train.sh`, a GitHub Actions workflow | `swarm_api.mergetrain`, run by SwarmCloud |
| repositories | THIS repository only | any repository registered in a workspace |
| credential | the merge App's token | the workspace's own forge credential (`swarm-tenant-<t>-git`), as the merge step uses |
| when | on its own schedule, for `ready`-labelled PRs | when a merge step in a `train` workspace has its PR green and ready |
| status | stays, for this repository's own CI, until migrated | the general product feature |

The platform train ports the repository-level train's rules: select, judge,
bisect by halves, eject a single culprit, land by squash pinned to the
tested head, then check the base's tree is the tested tree. When this
repository's own workspace turns `train` on and its lanes are merged by
SwarmCloud's merge step, the repository-level workflow becomes redundant and
can be retired. That migration is a separate decision.

**PR 1294's source was not readable from the lane that wrote this.**
`main` was at `75766d2` and did not contain it. The rules here follow its
description in the lane brief, and its test cases were rebuilt from that
description (`tests/unit/control_plane/test_merge_train_rules.py`). When
PR 1294 is on main, compare its cases with that file.

## 3. The setting

### 3.1 What a workspace chooses

Settings > Workspace > Merging (the console screen is lane TRAIN-WS-UI):

| field | values | default | why the default |
|---|---|---|---|
| `mode` | `direct` \| `train` | `direct` | what every workspace did before; a train changes how work lands, so the owner opts in |
| `train_size` | 1..10 | 4 | the owner's default: most of the CI saving, and a red train finds one culprit in at most two more trains |
| `max_train_age_minutes` | 15..1440 | 90 | the owner's default, longer than a full CI run; under 15 no real CI settles, and over a day is a stuck train, not a slow one |
| per repository | `inherit` \| `direct` \| `train` | `inherit` | a repository can opt out of (or into) the workspace's mode |

The **effective mode** of a repository is its override when it has one, the
workspace's mode otherwise (`mergesettings.effective_mode`). The workspace
settings are stored in `workspace_settings/{tenant_id}` (field `merge`, beside JUDGE-SELECT's `judgment_backend`). The override is the
registration's `merge_mode` field (`repositories/{repo_id}`), and `inherit`
is stored as null. Neither is in the frozen contract:
`Tenant` has no field for it, and contract change request 78 asks for one.

### 3.2 Who may change it

    GET /v1/workspaces/{tenant_id}/merge-settings
    PUT /v1/workspaces/{tenant_id}/merge-settings
        {"mode": "train", "train_size": 4, "max_train_age_minutes": 90,
         "repositories": {"repo_<16 hex>": "inherit" | "direct" | "train"}}

Every PUT field is optional, and a PUT that names nothing is refused 422.
`{tenant_id}` is the workspace: `u-<user>` for a personal workspace, the
group's slug for a group one.

| caller | GET | PUT |
|---|---|---|
| the owner of a personal workspace (its `kind: user` principal) | yes | yes |
| a platform admin, any workspace | yes | yes |
| a member who is neither | yes | 403 `merge_settings_owner_required` |
| anyone else | 404 | 404 |

**A group workspace has no admin role of its own in this API yet.** The
same is true of `readable_by_tasks` (repositories.py). So a group
workspace's "admins" are the platform's. When a group-admin role exists,
`mergesettings.authorize` is the one place to add it.

**The 403 is a switched refusal and ships report-only** (refusals.py, owner
decision 2026-10-08). Until `REFUSAL_MERGE_SETTINGS_OWNER_REQUIRED=on`, a
member's change goes through, is logged as the refusal it would have been,
and is audited under that member's name. Turn it on once the console offers
the change only to those who may make it.

**The 404 is NOT switched.** A caller who is not a member of the workspace
can neither read nor change it, whatever the switch says. Another workspace's
settings are not served and their existence is not confirmed (invariant 9).
The 404 is the established `not_found`, the answer for any resource of
another tenant. A repository id in the body that is not this workspace's
registration is the same 404, and nothing is written.

### 3.3 The audit

Every change is ONE `admin_audit` entry, written in the same transaction as
the change:

    {action: "workspace_merge_settings_set", target_tenant_id, by, at,
     detail: {tenant_id, from: {...changed fields...}, to: {...}}}

`from`/`to` name only what changed, overrides as `{repo_id: mode | null}`.
A PUT that changes nothing writes nothing and audits nothing.

## 4. How a train runs

### 4.1 Lanes

A lane is one (workspace, repository, base branch), with at most one train
running in it. Its document `merge_train_lanes/{lane_id}` holds:

- the running train;
- the stack of untested halves left by bisection;
- `seq`;
- the last train's outcome.

The coordinator runs on the per-tenant `merge_wake` Cloud Scheduler tick
(`POST /v1/admin/merges/wake`, every minute), after the CI wake and the CI
fixer's post-back. Those use the same tenant token and writer.

**Nothing runs between ticks.** A waiting train is documents plus a branch
on GitHub, and its steps are PARKED holding no lease (invariant 1). A
workspace with no entries costs two empty queries per tick.

### 4.2 The rules

The rules are pure functions, ported from PR 1294:

- **select**: up to `train_size` ready pull requests, oldest registration
  first. The top untested half rides first, and a pull request rides once.
- **judge**: the base's required checks at the composed head
  (`forgechecks.evaluate`, the CI wake's own reading) are green, red or
  pending. Pending for longer than `max_train_age_minutes` is `timed_out`,
  which is treated as red.
- **on failure**:
  - A train of one ejects it.
  - A train of several ejects nobody, and every rider keeps its place in the
    queue. Its first half (rounded up) rides next, then its second half. A
    half that is red is split again.
  - With one culprit in N, about log2(N) extra trains find it and only it is
    ejected. Two culprits in eight take 11 trains. Re-forming the whole
    queue after each ejection took 16, which is worse than landing them one
    at a time, and is why the halves are kept on a stack.

### 4.3 One train, tick by tick

1. **Compose.** An entry is eligible when its step is PARKED on
   `CI_PENDING` with `merge_wait.code == merge_train_waiting`, at the same
   pull request and head, in the entry's repository.
   - A step that ended is dropped (`step_ended`).
   - A step that has not parked yet waits for the next tick.

   Each selected pull request is re-read. A merged one, a closed one, one
   whose head moved or one whose base changed is RETURNED to its step.

   The lane is then **claimed** in a transaction before anything is written
   to GitHub, so two overlapping ticks never compose two trains. After the
   claim:
   - A branch `swarm-train/<lane>-<seq>` is made at the base's head.
   - Each head is merged into it in order (`POST /merges`). A pull request
     that **conflicts with the train ahead of it** is RETURNED (see §5.3),
     and after `MAX_RETURNS` (3) returns it is ejected.
   - A **draft** pull request from the branch makes the base's
     pull-request checks run on the composed head. The draft is never
     merged itself.
2. **Test.** Each tick reads the checks at the composed head and judges
   them.
3. **Land (green).** Each pull request is squash-merged in order, pinned
   (`sha`) to the head it registered and was tested at. Its own title
   (`(#N)` appended) is the commit title, and the step's provenance is the
   message.
   - A refusal RETURNS that pull request and puts the rest back in the
     queue.
   - After the last landing, the base's tree is compared with the tested
     tree. The result is recorded on the lane as `last.tree_verified`.
   - A mismatch is logged. It cannot be undone, and it means something
     else landed on the base meanwhile.
4. **Fail (red or timed out).** The failure plan is applied. Then the draft
   is closed and the branch deleted, as after a landing.

If a tick dies mid-compose, its claim is abandoned after 10 minutes and the
riders are queued again. A riding entry that belongs to no running train is
queued again on the next tick.

### 4.4 What a base's protection does to a train

- **"Require branches to be up to date"** (strict required checks) makes
  GitHub refuse the second squash of a train: the base moved under it. That
  pull request is RETURNED (`land_refused`) and the rest are re-queued. The
  train then lands one pull request per CI run, which is no better than
  direct mode. A refused landing counts toward `MAX_RETURNS`, so a pull
  request is ejected after three rather than bouncing forever.
  **Use direct mode (the merge slot) on such a base**, unless the forge
  credential may bypass the policy, as PR 1294's App token does for this
  repository.
- **Required reviews** refuse a landing exactly as they refuse the direct
  merge.

## 5. The merge step in train mode

### 5.1 The mode is signed at submission

When swarm-api appends (or honours) a workflow's merge step, it reads the
effective mode for the workflow's repository (`SubmissionService._merge_mode`).
When the mode is `train`, it writes `merge_target.merge_mode: "train"`
inside the signed dispatch block. Direct mode writes nothing, so a workspace
that never chose trains stores exactly the block it stored before.

A setting changed later reaches later workflows. A step already submitted
lands the way it was submitted.

### 5.2 Register and park

The step does everything it does in direct mode, in the same order:

- verify the verdict;
- check the head, the base and the conflicts;
- spend its CI and conflict **fix rounds** (SPEED-D2), which are untouched
  by train mode;
- see its own required checks green.

Then, instead of taking the merge slot and calling merge, it writes its
entry in `merge_train_entries/{mte_…}`, keyed by (tenant, task). The write
is a fenced transaction, so a superseded attempt writes nothing
(invariant 5). The step then parks `CI_PENDING` with
`merge_train_waiting`:

- The wait is counted on its own bound (`train_waits`, 200 wakes), off the
  CI clock, so a long queue neither spends its attempts nor reads as
  `checks_timeout`.
- The CI wake tick skips these parks.
- The coordinator marks the park when the entry concludes, and the
  15-minute fallback instant covers a lost mark.

### 5.3 How the step ends

| entry | the step's next attempt |
|---|---|
| `landed`, the PR merged at its head | **SUCCEEDED**, `merged_through_train`: comments its provenance on the PR and closes the issues it closes, as a direct merge does |
| `ejected` (`train_red`, `train_timed_out`, `train_conflict`, `step_ended`) | **FAILED** `train_ejected`, the train's reason and failing checks in the message |
| `returned` (`train_conflict`, `head_moved`, `land_refused`, `already_merged`, `closed`, `base_changed`) | re-runs its gate from the top, so a conflict with the new base goes to a **conflict fix round** and a closed or merged PR ends as in direct mode; then registers again, keeping its place. `train_conflict` and `land_refused` count toward `MAX_RETURNS` (3), after which the entry is ejected instead |
| `queued` / `riding` | parks again |

## 6. Invariants

1. A waiting train and its parked steps hold no lease and no pool count.
   Only the per-tenant tick reads them.
2. and 3. Untouched: a merge step is admitted like any other task.
4. The step parks; it never sleeps through the train.
5. The entry write is fenced on the attempt. The coordinator's entry and
   lane writes are transactions guarded on state and head, so a step that
   re-registered at a new head wins.
9. Every query filters on the tenant and every document is checked again.
   The token is the tenant's own, read once per tick and never logged. Both
   the entry and the task are tenant-writable, so a forged entry can at
   worst ask the coordinator to land a green pull request of that tenant's
   own repository with that tenant's token. That is something its worker
   identity, holding the same token, can already do.
10. The settings body is three scalars and a map of registration ids to one
    of three words. No image, command or resource spec is accepted.

## 7. What this does not do yet

- **The console screen** (Settings > Workspace > Merging) is lane
  TRAIN-WS-UI. Until it ships, the API above is the way to change the
  setting.
- **A group-admin role** (§3.2).
- **Migrating this repository** from the PR 1294 workflow to the platform
  train (§2).
- **Contract change request 78**: the setting on `Tenant` and a park reason
  of its own.
