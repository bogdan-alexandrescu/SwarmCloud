# Requests against the frozen contract

`apps/common/swarm_common/` is frozen. CLAUDE.md rule 1 is explicit about what
to do when you believe it needs to change: *say so instead of changing it.* This
file is where that is said, so a request survives the session that found it.

Nothing here is applied unless its **Status** line says so. Each entry states the
defect, the proof, what the change would be, and — the part that decides whether
it is worth it — what breaks downstream if it is made, and what is left for
somebody to live with if it is declined.

These are requests for a person to decide. Nothing in this file is a plan.

| 1 | `identity.py`: the `u-` prefix does not namespace personal tenants | open |
| 2 | `models.py`: `Attempt` records memory and disk, but not tokens or cost | APPLIED 2026-09-19 |
| 3 | `models.py`: a typed `input_from` on `Task` | open |
| 4 | A typed artifact-manifest entry | open |
| 5 | `gke_api_host` in a shared module (`swarm_common/kube.py`) | open |
| 6 | A typed dispatch block | open |
| 7 | `models.py`/`states.py`: the workflow rollup has no shared home | open |
| 8 | `states.py`: `Workflow.state` reuses `TaskState`; a workflow vocabulary | open |
| 9 | `models.py`: `Lease.dispatch_overdue` excluded the leases it names | APPLIED 2026-09-22 |
| 10 | `profiles.py`: `requires_preview_disk` names a feature this platform does not use | open |
| 11 | `profiles.py`: a runner profile had no way to be turned off | applied |
| 12 | `models.py`: the retry cap had no shared home, so one path forgot it | applied |
| 13 | `models.py`: `Attempt` does not record which pool account it ran on | open |
| 14 | `models.py`: a sub-agent has nowhere to name its parent | APPLIED 2026-10-02 (accepted by the owner 2026-10-02, decision OD-B15-1, with the cancel and capacity rules of its amendment; built as docs/design/child-tasks.md phase 2) |
| 15 | `models.py`: `Attempt` records memory, disk and spend, but not CPU | ACCEPTED 2026-09-25 (owner, on #184), applied in PR #210 |
| 16 | `identity.py`: the tenant namespace name, `sanitize_name` included, has two copies | open |
| 17 | `states.py`: a cancel that is only requested is recorded as `cancelled` (incident CR-1) | ACCEPTED 2026-09-24 (the owner's "#13"), applied in PR #44 |
| 18 | `profiles.py`: `RunnerProfile.command` is the lifecycle's child argv, never a container command (incident CR-2) | ACCEPTED 2026-09-24 (the owner's "#14"), rename applied in PR #44 |
| 19 | `states.py`: no `EventType` says "the reconciler evicted this execution" | open |
| 20 | `models.py`: `Workflow.on_step_failure` is a bare `str`, and its vocabulary is stated four times | open |
| 21 | `states.py`: the worker's exit codes have no shared home, and the reconciler now acts on one | open |
| 22 | `profiles.py`: whether a runner profile can run on a pool account is stated by the worker and restated by the scheduler | open |
| 23 | `models.py`: a task's end has no typed cause, so the outcome ledger classifies `last_error` text | ACCEPTED 2026-09-25 (#185, decision 9), applied in PR #217 |
| 24 | `profiles.py`: whether a profile's cost is declared rather than measured is named outside the catalogue | ACCEPTED 2026-09-25 (#185, decision 9), applied in PR #217 |
| 25 | `profiles.py`: a runner profile cannot declare the inputs a caller may send it, so the bridge names the mock's by profile | ACCEPTED 2026-09-25 (owner, on #142), applied in PR #213; both amendments confirmed by the owner 2026-09-26: `inputs=None` for `browser` and `generic` (#218), and the bounded park counted by the task's `attempt_count` rather than the state file |
| 26 | `models.py`: the attempt's CPU figures carry no time and their limit no source | ACCEPTED 2026-09-26 (owner, on #184), applied in PR #229 |
| 27 | `identity.py`: `_slug`'s docstring still sizes tenant ids for the `swarm-t-` prefix that no longer exists | ACCEPTED 2026-09-28 by the owner on #245, applied in PR #245 |
| 29 | `models.py`: `EndCause` gains `PUBLISH_REFUSED` (#259) | APPLIED 2026-10-02 (accepted by the owner 2026-09-29 on #259), functionality wave 3, lane B46 |
| 30 | `identity.py`: a tenant may list service accounts that resolve to it by exact email (#273) | ACCEPTED 2026-09-29 by the owner after three security reviews |
| 31 | `models.py`: `WorkflowStep` cannot record a step's verdict gate or its `builds_on` | open |
| 32 | `profiles.py`: `browser` and `generic` declare no inputs, so the API bounds them by size alone and the plugin can send them none (#218) | ACCEPTED 2026-09-29 by the owner after three security reviews, applied by #345 |
| 33 | `profiles.py` / `models.py`: a merge profile that runs no agent, and two end causes for it (#295) | APPLIED 2026-10-01 (accepted by the owner 2026-10-01; #364 amendment item 1 applied with it) |
| 34 | `models.py` / `specsign.py`: a step's spec is signed by swarm-api and verified by every worker (#342) | ACCEPTED 2026-09-29 by the owner after three security reviews, applied in PR #353 (code) and #354 (Terraform) |
| 35 | `profiles.py` / `models.py`: the `post-verdict` worker-action profile, and its own end causes (part of #295) | APPLIED 2026-10-01 (accepted by the owner 2026-10-01) |
| 36 | `profiles.py`: the `claude-code-review` profile, and a typed `never_restore_checkpoint` (part of #295) | APPLIED 2026-10-01 (accepted by the owner 2026-10-01) |
| 37 | `config.py` / `admission.py`: the lease's dispatch deadline is 300 s, shorter than a slow cold start plus the worker's startup read (#401) | ACCEPTED 2026-09-30 by the owner, applied by this PR (#404) |
| 38 | `states.py` / `admission.py`: a pool with no `hard_limit` is refused as "set to 0" (#374) | accepted by the owner 2026-10-05 (recorded on #374), IMPLEMENTED 2026-10-05 (functionality wave 7, lane CR38) |
| 39 | `states.py` / `models.py`: `ParkReason.BUDGET_EXHAUSTED` names a park nothing writes, because there are no budgets (owner, 2026-10-01) | open |
| 40 | `states.py`: an agent awaiting its children has no park reason, and `DEPENDENCY_INCOMPLETE` would be promoted at once (filed in request 14's amendment) | APPLIED 2026-10-02 (accepted by the owner 2026-10-02 with request 14) |
| 41 | `models.py`: a child cancelled because of its parent has no end cause (filed in request 14's amendment) | APPLIED 2026-10-02 (accepted by the owner 2026-10-02 with request 14) |
| 42 | `specsign.py`: the signed step spec does not cover a child's parent (filed in request 14's amendment) | APPLIED 2026-10-02 (accepted by the owner 2026-10-02 with request 14) |
| 43 | `identity.py`: the tenant worker service account's name has no public home (filed in request 14's amendment) | APPLIED 2026-10-02 (accepted by the owner 2026-10-02 with request 14) |
| 44 | `states.py`: `account_assigned` and `account_released` ride on `RUNNING` and `LEASE_RELEASED` (functionality wave 1, lane B4) | open |
| 45 | `profiles.py`: the catalogue does not say which runner profiles report a cost (filed with #72) | open |
| 46 | `models.py`: a pull-request step that published nothing has no end cause (functionality wave 3, lane B46) | open |
| 48 | `profiles.py`: no runner profile runs `agent-runtime-indexer`, so index runs cannot reach the repo-index toolchain (filed with #625, functionality wave 8, lane IMG) | open |
| 49 | `states.py`: a merge step waiting for its pull request's checks has no park reason (docs/merge-step.md 2026-10-06 request (A), lane MS1) | accepted by the owner 2026-10-06 (#352), to be applied by lane MS2 |
| 50 | `profiles.py` / `models.py`: retire the disabled `single-pr` catalogue entries (docs/merge-step.md 2026-10-06 request (B), lane MS1) | open; removal decided by the owner 2026-10-06 for a cleanup lane |
| 51 | `models.py`: `Attempt` does not type `checkpoint_sha256`, the digest a retry binds its restore to (#350, part of S0 #347) | proposed |

---

## 1. `identity.py`: the `u-` prefix does not namespace personal tenants

**Status:** open, found 2026-09-18 by an audit agent that correctly declined to
edit the frozen module itself.

### The claim that is false

`apps/common/swarm_common/identity.py::tenant_id_for_user`:

```python
def tenant_id_for_user(email: str) -> str:
    """Personal fallback tenant, namespaced so it cannot collide with a group."""
    return _slug(email, prefix="u-")
```

It can collide with a group, because the group path adds no prefix at all:

```python
def tenant_id_for_group(group_email: str) -> str:
    return _slug(group_email)
```

`_slug` appends a disambiguating digest only when the slug is *lossy* or *too
long*. A group whose local part is already a clean, short string starting with
`u-` is neither, so it passes through untouched:

| principal | function | tenant id |
|---|---|---|
| `u-eng@saga.xyz` (a group) | `tenant_id_for_group` | `u-eng` |
| `eng@saga.xyz` (a user) | `tenant_id_for_user` | `u-eng` |

Same GSA, same secret prefix, same GCS prefix, same namespace — the cross-tenant
merge invariant 9 exists to prevent. `store.py:232` compounds it by deriving
`kind = "user" if tenant_id.startswith("u-") else "group"`, which assumes
exactly this cannot happen.

### Why it is not yet exploitable, and why that is temporary

`TENANT_GROUPS` was never set by terraform, so `resolve_tenant()` had no groups
to check and there were no registered groups at all. The collision needs one.

That changed on 2026-09-18: `TENANT_GROUPS` is now wired (see
`terraform/infra/locals.tf`). The collision is reachable from the moment a group
whose local part begins with `u-` is registered. No such group exists today, and
nothing stops one being added — `u-` reads naturally as an abbreviation
(`u-eng` for "unified eng"), which is what makes this a trap rather than a
curiosity.

### The requested change

In `_slug`, force the digest branch when a **group** slug would begin with `u-`,
reserving that namespace for personal tenants. Equivalently: give group slugs
their own required prefix. Either makes the docstring's claim true.

### What it would cost — the reason this is a request and not a patch

**Tenant ids are not just identifiers; they are the names of live
infrastructure.** A tenant id appears in a GSA name, a Kubernetes namespace, a
Secret Manager secret name, and a GCS prefix. Re-deriving them means the
platform stops finding the resources it already provisioned.

For the change as scoped, only groups starting with `u-` change id, and there
are none — so the migration is empty *today*. That is precisely why it is worth
doing now and expensive later: the same change made after such a group exists is
a data migration across four services.

Whoever applies it should also add the collision to `tests/unit` as a case, not
merely as a regression guard: `tenant_id_for_group("u-eng@saga.xyz") !=
tenant_id_for_user("eng@saga.xyz")` is the property, and it is currently false.

### The unfrozen half, which does not need this request

Separately from the collision, `_assert_principal_matches` — the only check that
would catch two identities sharing a tenant id — runs *only* inside
`ensure_tenant`, reached only via `SubmissionService.tenant_for()`. The write
paths call it. `list_tasks`, `get_task`, `cancel_task`, `list_events`,
`list_artifacts`, `list_workflows`, `get_workflow` and `cancel_workflow` do not;
they filter Firestore by the raw `tenant_id` string.

That is in `apps/swarm-api/`, which is not frozen, and it is the half that turns
a collision into a cross-tenant **read and cancel**. It should be fixed whether
or not this request is accepted, and the comment at `routes/tasks.py:3-6` —
"there is no request a caller can construct that reads another tenant's task" —
should not be restored until it is true.

---

## 2. `models.py`: `Attempt` records memory and disk, but not tokens or cost

**Status:** APPROVED by the platform owner and APPLIED, 2026-09-19.

The five fields below are now on `Attempt`, and
`agent_worker.control.record_spend` populates them from the summary
`_usage_summary` extracts before truncation. A field the runner did not report
is omitted rather than written as zero -- `None` means "not reported" and `0`
means "cost nothing", and a mock task is genuinely the second while an
unparseable result is the first.

The original request follows, unchanged, as the record of what was asked for.

### What is missing

`Attempt` (`apps/common/swarm_common/models.py::Attempt`) carries
`peak_rss_bytes` and `peak_disk_bytes`. It carries no token count and no cost.

So the platform records precisely how much MEMORY an agent used and nothing at
all about what it spent -- on a platform whose entire cost is tokens.

### Why this surfaced now

A per-user activity view ("how many agents, tokens and jobs has each engineer
used") cannot be assembled. Two separate obstacles, one now fixed:

1. **The numbers were being thrown away.** `lifecycle.py` wrapped the runner
   result in `_truncate_json(..., 8000)`, which replaces an oversized result
   with a preview *string*. Long runs exceed 8000 characters, so the attempts
   that consumed the most tokens were exactly the ones whose token counts were
   discarded. Fixed on the unfrozen side: `_usage_summary()` now extracts
   `input_tokens`, `output_tokens`, cache counts, `thinking_tokens`,
   `total_cost_usd`, `num_turns` and the model list *before* truncation, into
   the free-form runner summary.

2. **There is still nowhere typed to put them.** The values now reach Firestore
   inside an untyped dict. Nothing can query them, no index can cover them, and
   every reader must know the shape by convention.

### The requested change

Add to `Attempt`:

```python
input_tokens: int | None = None
output_tokens: int | None = None
cache_read_input_tokens: int | None = None
cache_creation_input_tokens: int | None = None
cost_usd: float | None = None
```

Optional, defaulting to `None`, so every existing document remains valid and no
migration is required. `None` means "not reported", which is genuinely different
from zero -- a mock task reports nothing and did not cost nothing by accident.

### What it costs, and what it buys

**Costs:** five fields on the busiest document in the system, and a small write
on every attempt teardown.

**Buys:** cost and token usage become queryable per tenant, per profile and over
time. Today that question can only be answered by reading one GCS object per
attempt, which is correct and does not survive volume.

### Related work that is NOT part of this request

`submitted_by` is not filterable (`store.py:381-411` accepts `state`,
`workflow_id` and `runner_profile` only) and no Firestore index covers it
(`terraform/modules/firestore/indexes.tf`). Per-ENGINEER breakdown needs that
index regardless of this request. Both are unfrozen and can proceed
independently.

---

## 3. `models.py`: a typed `input_from` on `Task`

**Status:** open, recorded 2026-09-21. Raised in 5c99c79 ("input_from works;
work can be routed between agents for the first time") as one of "two
frozen-contract REQUESTS, raised rather than made" -- and then recorded in a
commit message, which is not a place anybody looks for an open decision.

**Update 2026-09-25 (#151):** failure (a) below, a plain `POST /v1/tasks`
carrying `metadata.input_from` past every DAG check, is closed on the unfrozen
side. The owner decided that the key is reserved, like `dispatch`.
`reject_reserved_metadata` now refuses it from callers with 422
`invalid_dispatch`, on a task, a batch and a workflow's own `metadata`, whatever
its value, and creates nothing. Workflow expansion is the only writer, and a
step's own `input_from` is the only declaration a caller can make, so the
`validate_dag` filename checks #64 added (PR #65) see every declaration that
reaches the worker. `metadata.expected_outputs`, the other key workflow
expansion writes (#149), is reserved by the same function. The request for a
typed field is still open. It never depended on (a) alone: the five spellings
below remain, and the worker still re-validates a free-form dict.

### What is asked for

A field on `Task`, mirroring the one `WorkflowStep` already has:

```python
#: upstream TASK id -> artifact filename staged into this task's workspace.
input_from: dict[str, str] = field(default_factory=dict)
```

`WorkflowStep.input_from` is already typed (`models.py:333`) and is keyed by
upstream STEP id. `Task` has no equivalent, so the same declaration -- rewritten
to task ids, which is the form the worker can actually resolve -- travels in
`task.metadata`.

### What is restated today, and where

One idea, five spellings:

| where | type | keyed by |
|---|---|---|
| `swarm_common/models.py:333` `WorkflowStep.input_from` | `dict[str, str]` | step id |
| `swarm-api/schemas.py:80` `WorkflowStepCreate.input_from` | `dict[str, str]` | step id |
| `swarm-api/validation.py` `StepSpec.input_from` | `Mapping[str, str]` | step id (was a tuple of ids with no filenames until #64) |
| `task.metadata["input_from"]` | untyped | **task id** |
| `swarm-ui/src/types.ts:1227` `WorkflowStep.input_from` | `string \| null` | -- |

**Written by exactly one place.** `swarm-api/service.py:389-392`, inside
`submit_workflow`, translating step ids to the task ids it has just minted:

```python
if source.input_from:
    task.metadata[INPUT_FROM_METADATA_KEY] = {
        step_task_id[src]: filename for src, filename in source.input_from.items()
    }
```

**Read by exactly one place.** `agent-worker/agent_worker/inputs.py:56`
(`METADATA_KEY = "input_from"`) and `declared_inputs()` at `:109-154`, called
from `lifecycle.py:875-905` (`_stage_declared_inputs`). `declared_inputs` then
re-validates every key and every value at runtime -- the key is a non-empty
string, the value is a non-empty string, no two entries share a destination --
because the contract types `metadata` as `dict[str, Any]` and says nothing about
what is inside it. That defensiveness is correct and would still be wanted; the
request is about the fifth row of that table. It was also about the submission
path that was never validated at all, a caller's own `metadata.input_from`, and
that half is closed (#151, below).

`StepSpec.input_from` was a *tuple of ids* because the dependency rule needs to
know only which step a file comes from. That shape is also why the API could not
see two parents staging one filename, a mistake that surfaced only after both
parents had run. Since #64 it carries the filenames too, and `validate_dag`
refuses that collision and any absolute or traversing filename at submission.

### Why: the failure it prevents

**(a) The DAG rule is enforced on one submission path and not the other.**
`validation.py:373` states the rule -- "every `input_from` source is also an
upstream dependency" -- and `:412-425` enforces it, so a step cannot stage an
artifact from a step that may never have run. That check lives in the workflow
path. Meanwhile `reject_reserved_metadata` (`validation.py:242-255`) reserves
`dispatch` and nothing else, and
`tests/unit/control_plane/test_dispatch_strategy.py::test_reject_reserved_metadata_allows_everything_else` pins that
`input_from` is deliberately allowed through:

```python
reject_reserved_metadata({"unit": "payments", "input_from": {"x": "y"}})
```

`_build_task` then copies caller metadata verbatim (`service.py:205`,
`metadata = dict(spec.metadata)`). So a plain `POST /v1/tasks` carrying
`metadata.input_from` is stored as written and honoured by the worker, having
passed none of the DAG checks. A workflow's own `metadata.input_from` took the
same route onto every step that declared no `input_from` of its own, which
always included the root steps. #64 (PR #65) briefly checked that value's
filenames at submission (`validate_workflow_input_from_metadata`), but the task
ids it named still carried no dependency edge. That check was removed when #151
reserved the key, below: there is no valid workflow-level value left to check.

Be precise about what that is and is not. It is **not** a tenant escape:
`inputs.fetch_upstream_task` (`inputs.py:226-254`) refuses a task belonging to
another tenant, and `inputs.artifact_key` (`:327-352`) builds the object key
from *this* attempt's tenant, so a cross-tenant artifact is unreachable by
construction. It is an **ordering** bypass inside one tenant: the caller names
any of their own tasks, with no dependency edge and no guarantee the upstream
ran. A typed field gives the API one field to validate on every path, instead of
one path validating a key the other path waves through.

**Closed on the unfrozen side, 2026-09-25 (#151).** The paragraphs above describe
the code before that date. The owner chose to refuse the key from callers rather
than validate it for a standalone task. `reject_reserved_metadata` now reserves
`input_from` alongside `dispatch`, so a `POST /v1/tasks` (or a batch) carrying it
gets 422 `invalid_dispatch` and nothing is created. A workflow whose own
`metadata` carries it is refused the same way, whatever the value, which also
closes the path by which that value reached every root step verbatim. That
refusal runs before `validate_dag`, so such a workflow answers
`invalid_dispatch`, never `invalid_dag`, even when its declaration would also
have broken the #64 filename rules. #65's workflow-level filename check and the
tests that pinned a well-formed, `{}` or `null` workflow-level value as
accepted went with it; its step-level checks are unchanged. The worker's
checks stay as defence in depth, not as the only guard on any submission path.
The test at
`test_dispatch_strategy.py` quoted above no longer pins `input_from` as allowed.
`tests/unit/control_plane/test_input_from_is_reserved.py` holds the refusal, the
empty store after it, and workflow expansion still writing the key.

**(b) The UI's copy is already wrong.** `codec.workflow_to_api:486` serves
`"input_from": s.input_from`, a `dict[str, str]`. `swarm-ui/src/types.ts:1227`
declares `input_from: string | null`, and the fixture at `api.ts:1185-1191`
builds it that way too, so the mock agrees with the wrong shape. Nothing renders
the field today, which is the only reason this has cost nothing -- a seam
declared at both ends with nothing in the middle.

### What it would break if accepted

* **No document migration.** `Task.to_firestore()` is `asdict(self)`, so new
  documents gain one key; `codec.task_from_dict` builds from named fields, so an
  older reader ignores it and an older document takes the default.
* **The rollout is the risky part, and it has a specific failure.** A worker
  image older than the API reads `metadata` only. If `service.py` stops writing
  `metadata["input_from"]` the moment the typed field exists, that worker sees
  no declaration, stages nothing, and runs the agent on a prompt written as
  though the file is there -- the exact silent-wrong-answer outcome
  `inputs.py`'s module docstring says must never happen. So: add the field, have
  the worker prefer it and fall back to metadata, ship both for a deploy window,
  and only then stop writing metadata.
* **`check-contract-parity.sh` gains nothing automatically.** It compares shell
  and jq to Python, and there is no shell restatement of this field.

### If it is declined

The worker keeps re-validating a free-form dict, which is defensible and is done
well today. Two things then need doing on the **unfrozen** side regardless, and
neither needs this request:

* `reject_reserved_metadata` should reserve `input_from`, or `_build_task`
  should validate it for a standalone task. **Done** the first way, by owner
  decision on #151 (2026-09-25): the key is refused from callers on every path
  that creates a task, and only workflow expansion writes it.
* `swarm-ui/src/types.ts` should declare `input_from` as
  `Record<string, string>`. **Done**, along with the fixture in `api.ts` that
  built it as a bare string, and `check-contract-parity.sh` section 5 now
  asserts the declaration against the frozen annotation -- so "nothing will tell
  anybody", which is what this bullet used to end on, is no longer true. Neither
  change touches the frozen module, which is why neither needed this request.

---

## 4. A typed artifact-manifest entry

**Status:** open, recorded 2026-09-21. Raised alongside #3 in 5c99c79: "a typed
artifact-manifest entry so check-contract-parity.sh can hold the producer and
the consumer together. Today they agree by convention, which is why the consumer
re-validates every key at runtime."

### The shape, and its single writer

`agent-worker/agent_worker/lifecycle.py:2110`, inside `_upload_outputs`:

```python
artifacts.append({"name": rel, "bytes": size, "uri": self.store.uri(key)})
```

placed into `result_summary` at `:2126-2134` beside `artifact_bytes` (int) and,
when the attempt hit its cap, `artifacts_skipped` (`list[str]`, truncated to 50).
`name` is a path relative to the artifacts directory, so it may contain
separators; `bytes` is `path.stat().st_size`; `uri` is whatever the store
rendered (`gs://...` in production, `file://...` locally).

### Every place that parses it

1. **`agent-worker/agent_worker/inputs.py:280-324`** -- `artifact_reference`.
   Reads `name`, `uri`, `bytes`. This is the one that resolves a declared input,
   and the one with the defect below.
2. **`swarm-mcp/swarm_mcp/patches.py:92-95`** -- `patch_uri`. Reads `name` and
   `uri` only, matching `result_summary.git.patch` against the manifest so a
   patch that failed to upload yields `None` rather than a link to nothing.
3. **`swarm-ui/src/AgentDetail.tsx:1439-1441`** -- shape-checks
   `summary.artifacts` is an array before casting, then `:1550-1556` renders
   `name`, `bytes` and `uri`, and `:1760` matches `git.patch` against `name`
   again.
4. **`swarm-ui/src/types.ts:345-349`** -- `ArtifactRef { name: string; bytes:
   number; uri: string }`. The cast at `AgentDetail.tsx:1440` checks "is it an
   array" and nothing further, so `bytes` is declared non-optional and is not.

A fifth thing shares the name and not the shape:
`agent-worker/agent_worker/runners/base.py:172` writes
`"artifacts": [<filename>, ...]` -- a list of **strings** -- into the runner's
`result.json`. It is not copied into `result_summary` (`lifecycle.py:697-707`
takes `status`, `summary`, `output` and `metrics` only), so the two do not
collide today. They are two identically-named fields with incompatible shapes in
one codebase, which is the ambiguity a named type removes.

### The defect nearby, and whether typing would have prevented it

`inputs.py:298-307`:

```python
size = entry.get("bytes")
return ArtifactReference(
    key=artifact_key(...),
    uri=uri,
    size_bytes=int(size) if isinstance(size, int) and not isinstance(size, bool) else 0,
)
```

Measured directly against that function (`uv run python`, five hand-built
manifest entries, nothing in the repository changed):

```
  bytes: int     -> size_bytes = 900000000
  bytes absent   -> size_bytes = 0
  bytes: str     -> size_bytes = 0
  bytes: float   -> size_bytes = 0
  bytes: True    -> size_bytes = 0
```

`stage_inputs:432-437` then sums `reference.size_bytes` and refuses the attempt
if the total exceeds `max_total_bytes`; the actual download at `:445` returns the
real size and is never compared to anything. So an entry whose `bytes` is
absent, a string, a float or a bool contributes **zero** to the cap and is then
fetched in full into a memory-backed tmpfs workspace. The comment at `:427-431`
says exactly what that cap is for: "an OOM kill several minutes into an agent
run is a far worse diagnosis than a refusal at setup."

**Would typing have prevented it? Half of it.**

* The half a type fixes: with `bytes` a required int at the boundary, the `0`
  has nowhere to come from. "Not reported" becomes a parse failure naming the
  upstream task and the file, instead of a silent zero -- and this module has
  already decided, in its own docstring, that a declared input which cannot be
  resolved fails the attempt rather than degrading quietly.
* The half it does not: the cap is computed from **declared** sizes and never
  re-checked against what arrived. A manifest that under-reports -- a truncated
  upload, a hand-edited document, a future writer that rounds -- walks through
  the same door with a perfect type in place. The fix that holds is a running
  total during pass 4 that aborts when the bytes actually downloaded exceed
  `max_total_bytes`.

That second half is **unfrozen**, lives in `inputs.py`, and should be fixed
whether or not this request is accepted. This entry must not become the reason
it waits.

### What it would break if accepted

* **Scope matters.** `Task.result_summary` and `Attempt.result_summary` are
  `dict[str, Any] | None`. Typing the whole summary would mean enumerating and
  freezing every key `lifecycle.py` writes (`git`, `logs`, `runner`,
  `checkpoint`, `restored_from`, `inputs`, `artifact_bytes`,
  `artifacts_skipped`) -- a far larger change than this request. The cheap,
  useful version is a dataclass for the **entry** alone, which nothing is forced
  to store and which every parser can construct from.
* **Strict parsing of history is plausible but unproven.** There has only ever
  been one writer and its shape has not changed, so every existing entry should
  carry all three keys. That has **not** been checked against production data,
  and it must be before anything parses strictly -- otherwise this request turns
  an under-counted cap into a failed attempt on an old artifact.
* `check-contract-parity.sh` could then hold the TypeScript `ArtifactRef` to it,
  except that it does not read TypeScript at all. See the closing section.

### If it is declined

`artifact_reference` keeps re-validating every key, which is the right thing to
do with an untyped dict, and `patches.py` and `AgentDetail.tsx` keep their own
defensive checks. The size-cap bypass above is fixed anyway, on the unfrozen
side. The cost is that four readers stay in agreement by convention, and the
fifth -- `ArtifactRef`, which claims `bytes: number` -- already is not.

---

## 5. `gke_api_host` in a shared module (proposed: `swarm_common/kube.py`)

**Status:** open, recorded 2026-09-21. c43844a ends its GKE-host section with
"the contract request to give it one owner is recorded rather than made." It was
not recorded anywhere a person would find it. This entry is that record.

### What is duplicated

`scheduler/dispatch.py:507-549` and `reconciler/backends.py:459-500`. Strip the
docstrings and comments and the two are byte-identical -- thirteen lines:

```python
raw = (endpoint or "").strip()
if not raw:
    return ""
scheme, separator, rest = raw.partition("://")
if not separator:
    rest = raw
elif scheme.lower() != "https":
    raise ValueError(
        "GKE_ENDPOINT must be a bare host or an https:// URL; "
        f"{scheme}:// would send the bearer token in clear text"
    )
rest = rest.rstrip("/")
if not rest:
    raise ValueError(f"GKE_ENDPOINT is not a usable host: {endpoint!r}")
return f"https://{rest}"
```

Verified by diffing the two comment-stripped bodies: no output. The docstrings
differ deliberately -- each names the other copy and states its *own* caller's
failure mode, which is the more useful half of what is written there.

### The claim behind the request, verified

The claim is that `apps/common` is the only directory both images carry. It
holds. Those are the only two `COPY apps/...` lines in either file:

| image | copies |
|---|---|
| `images/swarm-scheduler/Dockerfile:59-60` | `apps/common/`, `apps/scheduler/` |
| `images/swarm-reconciler/Dockerfile:61-62` | `apps/common/`, `apps/reconciler/` |

Each then builds exactly two wheels from those two directories
(`:65-66` and `:67-68` respectively) and installs its own service. So neither
image contains the other's package, neither can import the other, and
`swarm_common` is the only place a single copy could live inside both.

`apps/common/pyproject.toml` declares `dependencies = []` and
`packages = ["swarm_common"]`, so a stdlib-only module added there ships in both
images and adds no dependency to either. This function needs nothing but
`str.partition`.

### Why: the failure it prevents

The two copies **did** disagree, and the direction of the disagreement is the
point. The reconciler added the scheme; the scheduler did not. So every GKE
dispatch failed on a URL urllib3 could not route -- and it failed *after*
admission had already taken the lease. Invariant 3 counts concurrency from
`LEASED`, so the slot is held by a task committed to a backend that will never
start it. The reconciler, which is what would notice, was reading the same
cluster through the copy that worked.

What holds them together today is
`tests/unit/control_plane/test_gke_client_host.py::test_the_two_copies_agree_on_every_input`:

```python
assert scheduler_gke_api_host(endpoint) == reconciler_gke_api_host(endpoint)
```

That is a real guard and it does work. But it works only because the *test*
process can import both services, which neither *image* can -- the file says so
itself at lines 10-14. One test, importing two packages that never run in the
same process, is the entire structural connection between two copies of a
security-relevant string function. Delete the test and the copies are free.

### What it would break if accepted

* **It is an addition, not a change.** No existing type moves, no stored
  document changes, no wire format changes. That makes it the cheapest request
  in this file and the one with the least to argue about.
* Both call sites become imports. The two docstrings must move to the call
  sites rather than be lost with the duplicate: "the dispatch fails after the
  lease is taken" and "the reconciler goes blind on the GKE backend" are
  different facts about different callers, and both are worth keeping.
* The equality test should be **replaced, not deleted**: one test of the shared
  function, plus whatever already asserts that each service configures its
  client from it.
* CLAUDE.md rule 1 says never edit the files in that directory. Adding a file is
  not editing one, but it is still a change to the frozen package, which is
  exactly why this is a request and not a commit.

### If it is declined

Nothing breaks today, and the copies agree. The cost is that the only thing
standing between the next editor of either copy and a repeat of that outage is
one test file in a directory neither service builds -- and that the reasoning
for the duplication now lives in two docstrings that have to be kept accurate by
hand.

---

## 6. A typed dispatch block

**Status:** open, recorded 2026-09-21, from the defect fixed in 1cfdf57
("`integrate` promised ONE pull request and opened one per step").

> **Half of this was done on 2026-09-22, as this entry recommended.** "If it is
> declined" below says the carrier vocabulary has to be reconciled regardless
> because both sides are unfrozen. It has been: `_dispatch_carrier` now accepts
> `("checkpoints", "branches")` and defaults to `checkpoints`, and
> `tests/unit/worker/test_integrate_strategy.py` no longer pins `"patches"`.
> `tests/unit/worker/test_dispatch_contract_parity.py` imports swarm-api's own
> `DISPATCH_CARRIERS`, `DISPATCH_STRATEGIES` and `DispatchOptions.to_metadata()`
> and asserts the worker's parsers agree with them — which is the "something for
> a parity check to assert" this entry asked for, obtained without unfreezing
> anything. **The request itself stays open**: the vocabularies are still typed
> out twice, still in components that cannot import each other, and only a test
> now stops them drifting. That test lives in the unit suite, so it protects the
> repository and not a worker image built from an older commit.

### What is restated, and where

**swarm-api writes it.** `validation.py:186-229` resolves `DispatchOptions`
(`strategy`, `carrier`, `role`, `integrates`) and `to_metadata()` at `:222-229`
renders it into `task.metadata["dispatch"]` (`DISPATCH_METADATA_KEY`, `:175`).
The accepted vocabularies are `DISPATCH_STRATEGIES` (`:155`) and
`DISPATCH_CARRIERS` (`:161`).

**Two things read it back.**

* `codec.dispatch_of` (`:101-120`) -- for API callers. Fills absent fields with
  the API's defaults so a task predating the feature reads as
  `collect`/`checkpoints` rather than null, and `codec.workflow_dispatch`
  (`:122-149`) rolls that up to the workflow.
* `agent-worker/lifecycle.py:1611-1675` -- for the worker. Five accessors:
  `_dispatch_block`, `_dispatch_strategy`, `_dispatch_role`,
  `_dispatch_integrates`, `_dispatch_carrier`.

So three parsers, not two. `dispatch_of` and the worker deliberately disagree
about defaults, and that is correct -- one reports what a caller should *see*,
the other picks what is safe to *do*. What is not correct is the vocabulary.

### The drift that is already there

Measured by importing both sides:

```
swarm-api writes carrier in ('checkpoints', 'branches'), default 'checkpoints'

worker _dispatch_carrier body:
    value = self._dispatch_block().get("carrier")
    if not isinstance(value, str):
        return "patches"
    value = value.strip().lower()
    return value if value in ("patches", "branches") else "patches"
```

`checkpoints` -- the value on every task swarm-api has ever written that did not
ask for branches -- is not in the worker's accepted set, so `_dispatch_carrier()`
returns `"patches"` for it. And `"patches"` is a value swarm-api cannot produce:
`_accepted_value` (`validation.py:233-240`) refuses it at submission with the
accepted list in the error. Two sides of one field, and neither can say a word
the other accepts as the default.

**Today this costs nothing, and the reason matters.** `_dispatch_carrier` has no
caller in production code. Its only references are its own definition and
`tests/unit/worker/test_integrate_strategy.py::test_carrier_defaults_to_checkpoints_and_reads_branches`, which asserts the default
is `"patches"` -- pinning the disagreement into the suite rather than catching
it. 1cfdf57 says as much about the other end: "`carrier: "branches"` forced
`needs_repository` at submission and then changed no behaviour at all."

That is the shape of the two defects this repository fixed today: a seam built
at both ends with nothing in the middle, and a test that passes because it
agrees with the wrong side. The first change that wires the carrier up gets a
worker that reads every default dispatch as a carrier the API cannot name.

### Why: what a shared type prevents

Not the four-field read -- the worker reads all four now. It prevents the
vocabularies drifting apart again, which they already have. Concretely, a
`DispatchBlock` in the frozen package, owning the accepted values, would mean:

* one list of strategies and one list of carriers, imported by both sides
  instead of typed out twice;
* something for `check-contract-parity.sh` to assert. It covers nothing about
  dispatch today: its four checks are `SlotPool.effective_limit`,
  `Tenant.secret_name()`, the `TaskState` arrays, and the tenant-id budget;
* an explicit home for the worker's tolerance rules, which are **correct and
  must survive**. An unknown strategy reading as `collect` and an unknown role
  as `contributor` is the newer-control-plane/older-worker rule: in that
  disagreement the safe reading is the one that pushes nothing and opens
  nothing. That reasoning is currently in two docstrings and nowhere else.

### What it would break if accepted

* **Scope decides the risk.** A `DispatchBlock` dataclass plus a
  `from_metadata()` classmethod changes no stored document -- the block stays
  where it is, inside `metadata`. A typed *field* on `Task` would, and it
  carries the same rollout trap as #3 in the opposite direction: a worker older
  than the API would see no dispatch block, every `integrate` step would read as
  `collect`, and the workflow would run to completion publishing nothing. That
  is the failure 1cfdf57 fixed, arriving from the other side.
* **The tolerance rules must be part of the type's written contract.** If the
  next implementer makes `from_metadata()` strict, a version skew stops being a
  conservative downgrade and becomes a failed attempt.
* `tests/unit/worker/test_integrate_strategy.py::test_carrier_defaults_to_checkpoints_and_reads_branches` has to change. It
  currently asserts the wrong vocabulary.

### If it is declined

The carrier vocabulary still has to be reconciled. Both sides are **unfrozen**,
so that can be done today and should be: a dead accessor carrying constants the
writer cannot produce, with a test holding it in place, is a trap set for
whoever wires it up. Everything else keeps working by convention, re-parsed in
three places, with the frozen contract silent about a four-field block that
decides whether a workflow opens one pull request or five.

### A fifth field, 2026-09-28 (#263): `continues`

The CI fixer needed a step to push to an EXISTING swarm branch rather than its
own, and it was added the way this entry's "if it is declined" path allows:
inside the block, with no change to `apps/common/swarm_common/`. So the block
now has five fields, and this request covers all five.

* **What it is.** A task id. swarm-api writes it only on the one step of a
  `direct-pr` workflow submitted with `continues_task`, after checking the task
  is the caller's own, was itself `direct-pr`, and is in the same repository,
  and after resolving a chain of continuations to its root
  (`swarm_api/continuation.py`). The worker derives `<prefix><id>` from it,
  clones that branch and pushes onto it (`agent_worker/continuation.py`).
* **Why not a typed field.** The same reason as the other four, and the same
  rollout trap in its mildest form: a worker older than the API ignores the
  key, pushes `swarm/<its own id>` and opens a second pull request. That is
  wrong but not unsafe -- no branch is overwritten, and `push_branch` never
  forces either way.
* **The parity it relies on** is a test, as for the others:
  `tests/unit/worker/test_continue_swarm_branch.py` builds the block with
  swarm-api's own `DispatchOptions(continues=new_id("task")).to_metadata()`
  and asserts the worker derives the branch from it, and that the worker's
  task-id pattern accepts every id `new_id("task")` mints. The pattern is
  restated in both components because neither can import the other;
  `swarm_common.models` exporting it beside `new_id` is the frozen-contract
  change that would remove the copy. **Requested, not made.**

`codec.dispatch_of` does not serve `continues`: the workflow create response
echoes it as `dispatch.continues_task`, and adding it to every task's
`dispatch` would change a response shape several clients hold exactly.

---

## 7. The workflow rollup has no shared home, so only one service can own it

**Status:** open, found 2026-09-22 while making `Workflow.state` advance at all. The periodic caller this request was one way to get is built without it (D17, 2026-10-02): a Cloud Scheduler job per registered tenant calls `POST /v1/admin/workflows/rollup` as the dedicated `swarm-rollup-sweeper` account, which holds `roles/run.invoker` on swarm-api and nothing else and which swarm-api admits to that route alone (`terraform/modules/scheduler/jobs.tf` `workflow_rollup`, `terraform/infra/main.tf` `rollup_sweeper_invokes_api`, `swarm_api.auth.ROLLUP_SWEEPER_ROUTES`). What remains open is the shared home for the derivation.

### The problem

Nothing ever wrote `Workflow.state` after submission. It is now derived from the
step tasks and written back, and the derivation lives in
`apps/swarm-api/swarm_api/rollup.py`. It lives there because that is the only
place it CAN live and still be used by more than one service — and it is used by
exactly one.

The scheduler would have been the better home for the write. It already runs on a
guaranteed one-minute clock (`terraform/modules/scheduler/jobs.tf` (`resource "google_cloud_scheduler_job" "safety_tick"`), the safety
tick into the wake topic) and it already owns "state advances over time". It
cannot have the derivation, because:

```
images/swarm-scheduler/Dockerfile:59-60   COPY apps/common/  apps/scheduler/
images/swarm-api/Dockerfile:59-60         COPY apps/common/  apps/swarm-api/
```

Each image installs the frozen contract plus its own package. `swarm-scheduler`
cannot `import swarm_api` and `swarm-api` cannot `import scheduler`; a
cross-package import would be an `ImportError` in production and nowhere else,
which is the failure `apps/swarm-api/pyproject.toml` already carries a comment
about for `google-cloud-storage`.

So the choice was: one implementation in the API, or two implementations with a
cron. Two was refused, because the second copy would be compared against the
first by the drift check this feature also ships — and the check would then be
reporting the two copies drifting rather than the data drifting, which is worse
than no check.

### What the change would be

A new module in the frozen package — `swarm_common/rollup.py` — holding the pure
derivation only:

* the terminal-severity ranking (`DEAD_LETTERED > FAILED > CANCELLED > SUCCEEDED`)
* the pending precedence (`READY > PARKED > QUEUED > SUBMITTED`)
* `derive(readings) -> WorkflowRollup`
* the `UNKNOWN` sentinel and the rule that an incomplete read produces it

It defines no new dataclass field and changes no existing type. It is a pure
function over `TaskState` and a small result object, so it adds no dependency to
any image that does not already have `swarm_common`.

### What it buys

The scheduler's `_promote_dependencies` sweep (`scheduler/loop.py:472`) could then
do the write on the existing one-minute tick, and the API would derive for
display from the same function. One rule, two callers, nothing to drift.

### What breaks if it is made

Nothing imports it yet, so nothing breaks. The API's `swarm_api/rollup.py` would
keep the impure half — the store reads, the write-back, the metric — and import
the pure half instead of defining it.

### What is left to live with if it is declined

What ships today, which is not broken but is narrower than it should be:

* the write-back fires on every workflow READ, so the stored copy converges for
  any workflow anybody looks at — which is most of them, because the web UI polls
  `GET /v1/workflows?limit=100`;
* `POST /v1/admin/workflows/rollup` converges the rest on demand;
* **there is no periodic caller for that route.** A workflow nobody lists and
  nobody sweeps keeps a stale STORED state indefinitely. Every READER still sees
  the truth, because every read derives; it is the *queryable* copy that lags,
  which is the half the write exists for. Closing that needs either this request
  or a Cloud Scheduler job against the admin route, which is Track C.

---

## 8. `Workflow.state` reuses `TaskState`; a workflow needs its own vocabulary

**Status:** open, found 2026-09-22.

### The claim that is imprecise

`apps/common/swarm_common/models.py::Workflow.state`:

```python
@dataclass
class Workflow:
    ...
    state: TaskState
```

A workflow is not a task, and the task vocabulary is the one available to
describe it. Most of it transfers cleanly — SUCCEEDED, FAILED, CANCELLED,
DEAD_LETTERED, RUNNING, READY, PARKED and QUEUED all mean at the workflow level
what they mean at the task level. Two things do not:

* **There is no word for a partial failure.** A workflow whose steps are
  SUCCEEDED, FAILED and CANCELLED derives FAILED, and "FAILED" is not wrong — but
  it cannot distinguish "every step failed" from "four of five succeeded".
  `rollup.counts` carries that today, and a caller filtering on state alone
  cannot see it.
* **`LEASED`, `DISPATCHED` and `STARTING` are meaningless for a workflow.** They
  describe one task's relationship to one lease. The derivation collapses all
  four capacity-holding states into RUNNING for exactly that reason, so three of
  the twelve values are unreachable for a workflow and nothing says so.

### What the change would be

A `WorkflowState` enum in `swarm_common/states.py` and `Workflow.state` retyped
to it:

```python
class WorkflowState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PARKED = "PARKED"
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_FAILED = "PARTIALLY_FAILED"   # some steps succeeded, some did not
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
```

### Why it was NOT done as part of the work that found it

`Workflow` is in the frozen contract, and the existing vocabulary can express
everything the platform currently needs to say. Deriving into `TaskState` needed
no contract change and is what shipped. This is the request, not a plan.

### What breaks if it is made

More than request #7, and it should be costed before anyone agrees:

* `codec.workflow_from_dict` decodes the stored field with `TaskState(...)`.
  Every workflow document written before the change holds `"QUEUED"`, which is
  in both enums, so the read survives — but any document holding `LEASED`,
  `DISPATCHED`, `STARTING` or `DEAD_LETTERED` would stop decoding. Nothing writes
  those today; nothing guarantees nothing ever did.
* `apps/swarm-ui/src/types.ts` widens `Workflow.state` to `string` already, so
  the UI tolerates a new value without a typecheck — which is the same property
  that let it tolerate a wrong one. `check-contract-parity.sh` does not cover
  TypeScript (see the surfaces table at the end of this file).
* `PARTIALLY_FAILED` is a genuinely new concept and needs a decision about
  whether it is terminal for the purposes of "list my failed workflows".

### What is left to live with if it is declined

The derivation stays inside `TaskState`, `rollup.counts` carries the partial
picture, and a caller wanting "mostly succeeded" reads the counts rather than the
state. That is what ships today and it is honest; it is just less queryable than
a named state would be.
---

## Why these requests keep arising

Four of the requests above -- #3, #4, #6 and #7 -- are one situation: a value
with a single definition and several readers, in components that cannot import
each other. #5 is the same situation with a function instead of a value, and #7
is the same situation with a RULE instead of a value -- which is the sharpest
form of it, because the thing that would notice the two copies drifting is the
drift check that one of the copies exists to feed.

---

## 10. `profiles.py`: `requires_preview_disk` names a feature this platform does not use

**Status: open.** Raised while sweeping the documentation for the retracted
workspace-storage story, because the docs were not the only place it survived.

### What is there

```python
#: Cloud Run ephemeral disk is a Preview feature and, per Google's docs,
#: disables live migration -- which is why mandatory checkpointing exists.
requires_preview_disk: bool = False
```

`ResourceClass.requires_preview_disk`, `swarm_common/profiles.py`.

### Why it is a defect and not merely tidy-up

Three separate things, in increasing order of cost:

1. **Nothing sets it and nothing reads it.**
   `grep -rn requires_preview_disk` over the whole repository returns exactly
   one line: the declaration. No resource class passes it, no dispatcher
   branches on it, no test asserts it. It is a field whose every value is the
   default.
2. **Its docstring states a retracted premise, inside the frozen module.**
   The correction at the end of `CONTRACT.md` and of `CLAUDE.md` says this
   platform does NOT use Cloud Run ephemeral disk -- the Terraform google
   provider cannot express it, `empty_dir.medium` accepts only `"MEMORY"` -- so
   workspaces are memory-backed tmpfs on the fully-GA path, **with** live
   migration. The comment above therefore contradicts the contract it lives in,
   and it is the copy a reader is most likely to trust, because it sits beside
   the numbers.
3. **The clause "which is why mandatory checkpointing exists" is the dangerous
   half.** If the stated reason for invariant 8 is a feature the platform does
   not use, the invariant looks obsolete -- and the one conclusion nobody may
   draw is that the checkpoint interval can be relaxed. The real reasons are a
   quota park-and-exit, a cancellation, a reconciler reclaim of a stale
   generation and an ordinary crash; live migration covers none of them.
   `docs/checkpointing.md` now says so at the top of the file, but this comment
   still says the other thing.

### The requested change

Either:

* **remove the field** and its comment, since nothing sets or reads it; or
* **keep the field** (as a placeholder for the day the Preview feature becomes
  expressible) and rewrite the comment to say that it is unused today, that
  nothing may branch on it until a provider can express the feature, and that
  invariant 8 does not depend on it.

Removing it is cleaner. Keeping it is defensible only if somebody intends to
revisit the feature, which is a product decision rather than an engineering one.

### What it would break if accepted

Nothing detectable. There is no reader, so no decoder, no manifest, no test and
no Terraform mirror changes. `make test` covers the catalogue through
`tests/terraform/catalogue_mirror/`, which mirrors name, cpu, memory, disk,
units and the runner fields -- not this flag.

### If it is declined

The comment should still be corrected even if the field stays, and that is the
part that actually matters: a retracted premise stated inside the frozen module,
next to the reason for an invariant, is how the invariant gets argued away in six
months. Every doc that told the old story has now been corrected in place --
`README.md`, `docs/architecture.md`, `docs/checkpointing.md`,
`docs/cost-control.md`, `docs/concurrency.md`, `docs/execution-backends.md` --
so this comment and one other line are what is left.

**The other line is `CONTRACT.md` invariant 8 itself**, which still reads
"Mandatory periodic checkpointing. Cloud Run ephemeral disk is Preview and
disables live migration, so checkpoints are what make interruption survivable."
The correction is 50 lines further down the same file, which is better than
nothing and worse than the invariant being true where it is stated. Restating
invariant 8 on its real grounds -- park-and-exit, cancellation, reclaim, crash --
is a second request, made here rather than taken, because `CONTRACT.md` is the
document these requests are addressed to.

---

## Why these requests keep arising

Three of the requests above -- #3, #4 and #6 -- are one situation: a value with a
single definition and several readers, in components that cannot import each
other. #5 is the same situation with a function instead of a value. (#10 is not:
it is a stale comment on a field nothing uses, and it is here only because the
field is inside the frozen module.)

`CONTRACT.md` permits a restatement where it is unavoidable, and
`docs/architecture.md` pays for the one it permits with a test. There are three
restatement surfaces in this repository. Two are paid for.

| surface | what holds it to the Python | run by |
|---|---|---|
| shell / jq | `scripts/lib/check-contract-parity.sh` -- four checks | `make test` (Makefile:164), and the `shell` job in `.github/workflows/application.yml` |
| Terraform's runner catalogue | `tests/terraform/catalogue.tftest.hcl` with `tests/terraform/catalogue_mirror/` | `make tf-test` |
| TypeScript | `scripts/lib/check-contract-parity.sh` section 5 -- nine checks | the same |

Verified rather than assumed, because an earlier report asserted it. **The
TypeScript row was `nothing` when that was written, and the two drifts the table
below recorded are why.** Both have since been fixed and the surface is now
covered; the paragraphs are kept because the reasoning is what justifies the
check rather than a second route.

* `grep -c 'tsc\|typescript\|swarm-ui' scripts/lib/check-contract-parity.sh
  Makefile` -> `0` and `0` **at the time**. The parity script read no `.ts` file;
  its four checks were `SlotPool.effective_limit`, `Tenant.secret_name()`, the
  four `TaskState` arrays, and the tenant-id length budget.
* `grep -rc 'tsc\|npm \|typecheck\|swarm-ui' .github/workflows/` -> `0` in all
  four workflow files. **Still true**, and it does not need to change: section 5
  is pure Python and `re`, reads `types.ts` as text, and needs no node toolchain
  on the machine or in CI.
* `apps/swarm-ui/package.json` does define `"typecheck": "tsc -b --noEmit"`.
  Nothing in this repository runs it.

And running it would not help. `tsc` checks `types.ts` against itself; it has no
way to know what `swarm_common` says. Only a comparison, or a route, can. Section
5 is the comparison: it reads the literals out of `types.ts` with regular
expressions and asserts them against the imported frozen modules, and it
**refuses** rather than skips when a literal is not where it expects -- because a
parity check that passes because it could not find what it compares reports an
agreement it never established.

What that cost, measured before the check existed:

| in `apps/swarm-ui/src/types.ts` | frozen source | state |
|---|---|---|
| `RESOURCE_UNITS`: standard 1, browser 2, large 4 | `profiles.RESOURCE_CLASSES` units 1 / 2 / 4 | agreed. Hand-copied; **now asserted** |
| `TaskState` union, 12 values | `states.TaskState`, 12 values | agreed; **now asserted** |
| `ParkReason` union, 8 values | `states.ParkReason`, 8 values | agreed; **now asserted** |
| `CONCURRENCY_STATES`, `TERMINAL_STATES` | the frozen frozensets | agreed; **now asserted** |
| `REAL_STATES` + `NEVER_WRITTEN` | must partition `TaskState` | agreed; **now asserted** |
| `PoolKind`, `POOL_FAMILY_ORDER`, `poolKind`'s switch | the families `models.pool_names_for` emits | agreed; **now asserted** |
| `QuotaState.state`: `'HEALTHY' \| 'COOLDOWN' \| 'EXHAUSTED' \| 'DISABLED' \| string` | `models.ProviderState`: `AVAILABLE`, `THROTTLED`, `EXHAUSTED`, `COOLDOWN`, `UNKNOWN`, `DISABLED` | **was drifted; FIXED** -- the field now reuses `ProviderStateName`, which is asserted |
| `WorkflowStep.input_from`: `string \| null` | `codec.workflow_to_api` serves `dict[str, str]` | **was drifted; FIXED** to `Record<string, string>`, and asserted |
| `ResourceClassSpec` | served by `GET /v1/resource-classes` | **not copied, deliberately** |

The `QuotaState` row is worth spelling out, because it is the one that shows why
a typechecker would not have helped. There is no provider state called `HEALTHY`
anywhere in this platform -- the only other occurrence of the word in the
repository is an unrelated fixture name at `tests/unit/mcp/test_sc.py::HEALTHY`. Three
states that *do* exist (`AVAILABLE`, `THROTTLED`, `UNKNOWN`) were missing from
the union. And the trailing `| string` widens the whole thing back to `string`,
so even a typechecker that ran would have reported nothing: the union documented
a vocabulary rather than enforcing one, and the vocabulary it documented was
wrong. The `| string` is kept -- a value the server adds must still decode and
render as itself -- and the vocabulary half is now a named type with an assertion
behind it.

The last row is the remedy the repository found first and used best. The comment
on `ResourceClassSpec` gives this exact reasoning for **not** copying the
resource-class numbers:

> `check-contract-parity.sh` asserts that shell and jq restatements of the
> frozen catalogue still match the Python; it does not cover TypeScript, so a
> copy here would drift the first time a class is resized and nothing would
> notice.

So somebody had already worked this out once, wrote a route instead of a copy,
and the two drifted rows above are what happened in the places where that was
not done. **The second clause of that comment is no longer true** -- the check
does cover TypeScript now -- but the decision it justified is still the better
one, and it stands: a route beats an asserted copy, because an asserted copy
still has to be edited in two places.

Which gives the shape of the answer to all of these:

* where every reader is Python, a shared type ends it -- requests #3, #4, #5, #6;
* where one reader is jq, Terraform or TypeScript, a parity check ends it, and
  all three exist;
* a route that serves the value is better than any of them where the value can
  be served, because nothing has to be kept in step at all. That is what
  `GET /v1/resource-classes` and `GET /v1/runtimes` are, and it is the remedy to
  reach for first.

**A parity check is not a substitute for a shared type.** It catches drift after
it is written, at `make test` rather than in review, and it cannot catch drift in
a value that is restated somewhere it does not read. The requests above are still
requests.

---

## 9. `models.py`: `Lease.dispatch_overdue` excluded the leases it names

**Status: ACCEPTED and applied 2026-09-22, by the owner's decision.**

### The claim that was false

```python
def dispatch_overdue(self, now=None) -> bool:
    """Admitted but never started. The reconciler reclaims these."""
    return self.state is TaskState.LEASED and (now or utcnow()) > self.dispatch_deadline
```

`mark_dispatched` (`apps/scheduler/scheduler/store.py`) writes `DISPATCHED` to
the lease the moment the backend *accepts* the create call — long before a
container runs. So `state is TaskState.LEASED` was False for essentially every
lease whose dispatch was in flight, which is exactly the population the method
names. Measured on `task_b5dc2568713a40158851` on 2026-09-22: still False 301
seconds after admission.

### What it cost

* `apps/swarm-api/swarm_api/routes/admin.py::list_leases` — the `overdue_only=1` filter
  is the query an operator runs during a capacity incident to find stuck
  dispatches. It returned an empty list at exactly the moment it was asked.
* `apps/swarm-api/swarm_api/codec.py::lease_to_api` — `lease_to_api` reported
  `dispatch_overdue: false` on every lease the API served.
* `apps/swarm-ui/src/Overview.tsx` — narrowed again with
  `dispatch_state === 'LEASED' &&`, a second copy of a guard that had already
  emptied the set.

### Why no test caught it

The only test that touched the predicate asserted the KEY was present and never
its VALUE:

```python
for key in ("released", "expired", "dispatch_overdue"):
    assert key in body
```

A predicate is not covered by a test that checks it was spelled correctly. The
suite stayed green for weeks with the flag permanently False.

### What was applied

The `state is LEASED` guard was removed from the helper, the stale comment in
`codec.py` defending it was corrected, and the duplicate guard in `Overview.tsx`
was deleted. A `heartbeat_at is None` conjunct was deliberately NOT added: the
reconciler needs that distinction because it decides whether to fence a
generation, while this method answers the narrower question of whether the
deadline has passed.

Three tests were added in
`tests/unit/control_plane/test_leases_and_attempts_read_path.py`, and the fix
was proved by mutation — restoring the guard turns
`test_a_dispatched_lease_past_its_deadline_is_overdue` red.

---

## 11. `profiles.py`: a runner profile had no way to be turned off

**Status: ACCEPTED and applied 2026-09-23, by the owner's decision.** This edits
`apps/common/swarm_common/`, which is frozen; it is recorded here as such.

### What was asked

"disable codex support for now. just focus on support and deep integration with
claude."

### Why a flag and not a deletion

`RunnerProfile` had no availability concept at all, so the only way to stop a
profile being dispatched was to remove it from `RUNNER_PROFILES`. That would
have:

* stranded anything already queued against it — nothing was, which was checked,
  but the mechanism should not depend on that being true;
* broken the catalogue entry that four existing `codex` task documents in
  `saga-agents-staging` still name, making those runs unreadable rather than
  merely unrepeatable;
* made re-enabling a revert rather than a word, for something explicitly asked
  for "for now".

So `available: bool = True` and `disabled_reason: str = ""` were added, with
`__post_init__` refusing a profile that is disabled without a reason. A caller
told only that a known profile was refused has nothing to act on.

### The distinction that matters

`validate_runner_profile` now separates **unknown** from **known but refused**.
Collapsing them sends a caller hunting for a typo that is not there: `codex` is
spelled correctly, exists, and will not run. The 422 carries `disabled: true`
and the reason.

The suggestion list in that error is now the AVAILABLE set, not the whole
catalogue — offering a profile that would also be refused is not a suggestion.

### What is deliberately NOT narrowed

`known_providers()` still reads the whole catalogue, so `openai` stays
registrable. A tenant already holds `swarm-tenant-eng-openai`; narrowing this
would make that secret unregisterable, unrotatable and undeletable through the
route that owns it. Disabling a runner must not strand a credential someone has
to be able to clean up.

### Why codex specifically

A twenty-step load test on 2026-09-23 dispatched four codex steps across all
five profiles. All four failed with `openai refused the credential`. The
tenant's `swarm-tenant-eng-openai` holds a single version written 2026-09-16
that the provider rejects. Nine `claude-code` and four `mock` steps in the same
run succeeded.

### Proved by

Three mutations, each caught: re-enabling codex reddens two named guard tests;
skipping the availability check reddens
`test_a_disabled_profile_is_refused_as_disabled_not_as_unknown`; and disabling
without a reason raises at import.

The route-to-type parity tests caught the rest — `/v1/runtimes` serves both new
fields, `types.ts` had to match field-for-field, and
`test_the_screen_reads_every_field_the_route_publishes` then obliged the
Runtimes screen to actually render the disabled state and its reason rather
than accept a field it ignored.

---

## 12. `models.py`: the retry cap had no shared home, so one path forgot it

**Status: ACCEPTED and applied 2026-09-23.** This edits
`apps/common/swarm_common/`, which is frozen, and is recorded here as such.

### The outage

`task_d18d8d8b044d469cb43c` -- the browser step of a twenty-step workflow --
reached **83 attempts against a `max_attempts` of 3**, re-dispatching roughly
every 30 seconds for hours on `gke_create_job_failed`. It was still climbing
when an operator stopped it by hand. Its `current_generation` had reached 83,
so every one of those attempts burned a fencing generation as well as a lease.

### Why

TWO paths return a task to READY and only one enforced the cap.

`reconciler/store.py` checked it, on the path that REPAIRS a task to READY.
`scheduler/store.py`'s `return_to_ready_after_failed_dispatch` -- reached
whenever a dispatch fails after the lease is taken -- wrote
`state: READY` unconditionally.

That function is not careless. Its docstring reasons at length about retry
PRESSURE: "a backend that is refusing one dispatch is usually about to refuse
the next, and a tight retry would burn the whole run's lease budget on one
broken task", and it applies a 30-second backoff for exactly that reason. It
spaced the attempts out and never counted them. A backoff is not a cap.

### The change

`retries_exhausted(attempt_count, max_attempts)` in `swarm_common.models`, plus
`Task.retries_exhausted()` delegating to it. Both call sites now use it.

A FREE FUNCTION as well as a method because the two enforcement points do not
both hold a `Task`: the reconciler decides inside a Firestore transaction from
the raw document, where constructing one would mean decoding a task to read two
integers.

### Why it belongs in the frozen contract

The scheduler and the reconciler are separate images and cannot import each
other. A rule they both need therefore has exactly one home that is not a
restatement, and `scripts/lib/check-contract-parity.sh` exists because
restatements drift. This one did worse than drift: the second copy was never
written.

### Proved by

Two mutations, each caught by named tests. Reinstating the original bug
(`exhausted = False` on the dispatch path) reddens
`test_a_task_at_its_cap_fails_instead_of_retrying` and
`test_a_terminal_task_is_not_given_a_retry_time`. An off-by-one (`>` for `>=`)
reddens four.

The tests pin BOTH paths deliberately, because pinning one is what produced the
bug.

### Also fixed on the way past

A task sent to FAILED by this path was getting a `next_eligible_at`, which
promises a retry that is not coming and renders in the console as a scheduled
attempt. It now gets a `completed_at` and no eligibility time.

### Correction, 2026-09-24: the shared predicate was right and the scheduler still fed it the wrong number

The predicate is correct and unchanged. The scheduler's caller was wrong. It
passed `task.attempt_count` from the READY snapshot the drain loop had read.
`acquire_lease_in_transaction` increments the count in the **document** and
never in that object, so every check was one attempt behind, and
`max_attempts` 3 bought four attempts. `task_b568a623be8645eb87c6` ended FAILED
at 4/3 on 2026-09-24.

"Proved by" above did not catch this because the tests handed the store a Task
already carrying the post-increment count, which is a number the production
caller never has. `return_to_ready_after_failed_dispatch` now reads the count
from the document inside a transaction, as the reconciler always did. Its tests
now feed it what the loop feeds it, and one drives the real drain loop from
zero. Nothing in `swarm_common` changed; this note exists so the "Proved by"
section above is not read as covering the caller.

---

## 13. `models.py`: `Attempt` does not record which pool account it ran on

**Status:** open, filed 2026-09-24. A request, not a change: nothing under
`apps/common/swarm_common/` is edited. Raised in
[`docs/web-ui/redesign-v2.md`](web-ui/redesign-v2.md) §6 S6 ("request, not
proposal"), which asked for it to be filed here if pursued.

### The claim, made precise

S6 says "nothing assigns an account to an attempt. `Lease` and `Attempt` carry
no account field". The second sentence is true: `Lease` (`models.py:115-135`)
and `Attempt` (`models.py:242-281`) have no account field. The first is not
quite, and the difference is the reason for this request rather than an
argument against it:

* **The worker does record it -- as an untyped event.** `_lease_account` in
  `apps/agent-worker/agent_worker/lifecycle.py` emits `RUNNING` with
  `detail = {"cause": "account_assigned", "account_id": ..., "provider": ...}`,
  and `control.emit` stamps the event with the attempt id, lease id and
  generation. A rejected account emits `RETRYING` with
  `{"cause": "account_unreadable", "account_id": ...}`.
* **The broker records a HOLD, not a history.** `acquire_hold`
  (`apps/quota-broker/quota_broker/main.py`) counts the assignment against the
  account under an `assignment_id`, and the hold expires and is pruned by the
  quota sweep. Nothing there survives the attempt.

### Why an event is not enough

* **"Which attempts ran on account X"** -- S6's question, i.e. which agent spent
  this subscription's quota -- is a read of every task's `events` subcollection,
  filtered on `detail.cause` and `detail.account_id` in client code. The two
  collection-group indexes on `events` (`terraform/modules/firestore/indexes.tf`,
  `events-tenant-at`, `events-task-at`) do not cover a field inside `detail`,
  and `detail` is a map whose shape is a convention.
* **One attempt can be offered more than one account.** `_reject_account` hands
  back an account whose secret it cannot read and asks again with it excluded.
  The events record every offer; which account the agent RAN on is "the last
  `account_assigned` not followed by an `account_unreadable` for the same id" --
  an inference every reader would re-derive, and the first one to get it wrong
  attributes spend to an account that never ran the agent.
* **Events are declared an audit trail, not a record.** The `events_ttl` field
  policy expires them on `expires_at`. The worker's `emit` sets no `expires_at`
  today, so these particular events do not expire -- but the policy says what
  events are for, and attribution is not that.
* **Spend is already on `Attempt`** (request #2, applied). Attribution on the
  same document makes "spend per account" one query rather than a join through
  untyped event detail.

### The requested change

Add to `Attempt`:

```python
#: The pool account this attempt's agent RAN on: the last assignment it kept,
#: not every one it was offered. None means "not recorded" -- an older attempt,
#: or a worker that never reached account selection.
account_id: str | None = None
#: "account_pool" or "tenant_secret". None means "not recorded". Separate from
#: account_id because "no account" has two meanings that want different
#: readings: the pool is not how this tenant runs, or nobody wrote it down.
credential_source: str | None = None
```

**Single writer: the worker**, at the two places that already decide it --
`_lease_account` when an account is kept and `_decline_pool` when the tenant
secret is used -- through `control.py`'s attempt document, the way
`record_spend` writes spend (merge, tenant-stamped).

**Not on `Lease`.** The lease is capacity, taken by the scheduler before any
account is chosen; the account is chosen afterwards, by the worker. A lease
field would be one the scheduler writes empty and never knows.

### What it would break if accepted

* **No existing document.** Both fields are optional and default to `None`, so
  nothing needs migrating -- the same shape as request #2.
* **Every reader must read `None` as "not recorded", never as "tenant secret".**
  That is the rule #2 set for spend, and `credential_source` exists so that
  nobody has to infer it from an absent `account_id`.
* **Indexes (Track C).** `attempts.account_id` equality is covered by Firestore's
  automatic single-field index; "an account's attempts, newest first" needs a
  composite `(account_id, created_at)` in `terraform/modules/firestore/indexes.tf`.
* **Fencing (invariant 5).** An attempt document is keyed by its own
  `attempt_id`, so a stale worker can only write its own attempt, never a newer
  one. The write should still go through the same path as `record_spend`, not a
  new one.
* **The TypeScript restatement.** `types.ts` gains the two fields, and
  `scripts/lib/check-contract-parity.sh` section 5 should hold them.

### If it is declined

S6 stays unanswerable except by scanning event subcollections, and the console
cannot show "this account's attempts" without an N+1 over tasks. What is left to
live with is the event: its `detail` shape (`cause`, `account_id`, `provider`)
becomes the contract of record by default, and should then be written down as
one, in `docs/`, so that every reader applies the same "last kept assignment"
rule.

---

## 14. `models.py`: a sub-agent has nowhere to name its parent

**Status:** APPLIED 2026-10-02. ACCEPTED 2026-10-02 (owner decision OD-B15-1), WITH the rules in its amendment: accepted by the owner 2026-10-02, when they chose to build child tasks (D15). `Task.parent_task_id` and `Task.parent_attempt_id` are in `apps/common/swarm_common/models.py`, set only by swarm-api's children route (`apps/swarm-api/swarm_api/children.py`).

Filed 2026-09-24 as a request. The cancellation and capacity rules it asked to
be decided with the field are in the amendment at the end of this entry. Raised as S1 / B31
in [`docs/web-ui/ui-audit-and-build-prompt.md`](web-ui/ui-audit-and-build-prompt.md):
"Write the request; do not build a fake hierarchy from `depends_on`."

### What was asked for, and what exists

The brief asked for "workflows of agents **and sub-agents**". A workflow step
is a `Task` with `workflow_id`, `step_id` and `depends_on` (`models.py:172-205`).
There is no parent/child relationship between tasks anywhere -- not in the
frozen model, not in the API, not in the UI.

`depends_on` is not one. It is ORDER: a step runs after the steps it names have
succeeded. A dependency is not a parent -- the step that produced your input did
not create you, cannot cancel you, and is not waiting on you -- which is why S1
forbids inferring a hierarchy from it.

### How a sub-agent would be created today, and what would be lost

The worker gives the agent its own identity: `SWARM_TASK_ID`, `SWARM_ATTEMPT_ID`
and `SWARM_TENANT_ID` are in the child environment it builds
(`agent_worker/lifecycle.py`, the `base` env). An agent that submitted work of
its own could therefore name itself. There is nowhere typed to put that name:

* `TaskCreate.metadata` (`apps/swarm-api/swarm_api/schemas.py::TaskCreate.metadata`) is a free
  `dict`. A child could carry `metadata.parent_task_id` by convention -- the
  same shape request #3 records for `input_from`.
* A convention in `metadata` is **unvalidated**: any caller can claim any
  parent, including a task in another tenant (invariant 9), and a UI drawing a
  tree from it would draw whatever a caller typed.
* It is **unindexed**: "this task's children" is a scan.

**Not verified here, and part of the cost:** that an agent inside a worker can
reach the API at all. The child environment carries no API address and no
credential for it (the same `base` env), so sub-agents also need a submission
path. That is not a contract change and is not requested here; it is named so
the field is not mistaken for the whole feature.

### The requested change

Add to `Task`:

```python
#: The task whose agent submitted this one from inside a running attempt.
#: None for work a person, a client or a workflow submitted.
parent_task_id: str | None = None
#: The parent's ATTEMPT. A parent can be retried, and the children of attempt 1
#: and attempt 2 are different work -- the second may re-create the first's.
parent_attempt_id: str | None = None
```

**Part of the request, not a detail: the API SETS these; a caller never
supplies them.** A caller-supplied parent is invariant 10's shape (a caller
naming something the platform should decide) and invariant 9's hazard (a
parent in another tenant). The API resolves the caller's identity already;
the parent is whatever attempt that identity is currently running, or nothing.

Listing children needs `GET /v1/tasks?parent_task_id=` -- a filter in
`swarm-api/store.py` and a composite `(tenant_id, parent_task_id, created_at)`
index. Both are unfrozen (Tracks A and C); they are listed so the full cost is
visible.

### What it would break if accepted

* **No existing document.** Optional, default `None`.
* **Cancellation has to be decided WITH the field, not after it.** Does
  cancelling a parent cancel its children? The field decides nothing, but the
  first screen that draws the tree will be asked, and an answer arrived at by
  whoever writes that screen is the wrong place for it.
* **Capacity (invariants 1-4).** Each child holds its own lease. A parent that
  WAITS on its children holds a slot they may need -- on a narrow pool, a
  deadlock -- and it sleeps through a long wait, which invariant 4 forbids.
  Agents can already do this today by submitting and polling; naming the
  relationship will invite it. Whatever accepts this request should also say
  what a parent is allowed to do while its children run.
* **The workflow rollup** (request #7) must not count a step's children as
  steps.
* **The TypeScript restatement.** `types.ts` gains the two fields, and
  `check-contract-parity.sh` section 5 should hold them.

### If it is declined

"Sub-agents" stays at **does not exist** in the brief's table, and the console
says so. There is no honest partial: a hierarchy inferred from `depends_on` is
refused by S1, and one read from a `metadata` convention would be a tree any
caller can draw.

### Amendment, 2026-10-02: accepted with the cancellation and capacity rules (B15)

The owner's decisions of 2026-10-02 settle what this entry left open. The full
design, with every flow, failure case, route and limit, is
[`docs/design/child-tasks.md`](design/child-tasks.md); this amendment records
what the request now consists of, so the entry stays the contract of record
for the field. **Nothing under `apps/common/swarm_common/` is changed by this
amendment.** The phase-2 pull request that applies it must add its own
acceptance line here, because `scripts/lib/check-frozen-contract.sh` credits
only a line the same pull request adds.

**Citations corrected.** The entry above cites `models.py:172-205` for the
workflow fields; they are `apps/common/swarm_common/models.py::Task.workflow_id`,
`Task.step_id` and `Task.depends_on` today (`Task` is
`apps/common/swarm_common/models.py::Task`). The "base env" it
names is `_build_child_env` (`apps/agent-worker/agent_worker/lifecycle.py::Worker._build_child_env`),
with the three ids at `apps/agent-worker/agent_worker/lifecycle.py::Worker._build_child_env.base`.
`TaskCreate.metadata` is still `apps/swarm-api/swarm_api/schemas.py::TaskCreate.metadata`.

**OD-B15-1 -- the field is accepted as requested**, both fields optional,
default `None`, and set only by swarm-api from the submitting attempt. Nothing
about the two fields changes. What the acceptance adds is the rules below,
which CR 14 itself said must be decided with the field.

**OD-B15-2 -- the submission path.** The agent never holds a credential. It
writes a request into a spool in its workspace; its worker validates it and
calls `POST /v1/tasks/{parent_task_id}/children`, which accepts only the
tenant's worker service account (derived from the tenant id, never read from
`tenants/<id>`) AND an attempt proof: a signature made with an Ed25519 key the
worker generated in its non-dumpable heap and registered with swarm-api BEFORE
the agent was spawned. An agent can mint its tenant's token from the metadata
server ([security.md](security.md#cloud-metadata-abuse)), and it can read the
whole container environment at `/proc/1/environ`, because `tini` is PID 1 and
is not non-dumpable (`apps/agent-worker/agent_worker/hardening.py` (`WHAT IT DOES NOT COVER. The container's environment is also PID 1's (tini's),`)). So
nothing that authorises a submission travels through the environment, a
mounted file or `work/` and stays valid while the agent runs. The scheduler
passes only a one-use registration nonce, which the worker spends between the
fence and `STARTING -> RUNNING`; registration is first-wins per generation, is
refused once the task is `RUNNING`, and is attested by swarm-api with its own
key (`swarm-child-key`, held only by `swarm-scheduler` and `swarm-api`) at a
document id no tenant identity can compute. A worker without memory protection
spends the nonce on a tombstone and offers no child path. The route re-reads
the parent and its lease in one transaction and creates nothing unless the
lease is this attempt's live one. `parent_task_id` and `parent_attempt_id`
come from the attested registration, never from the body; `POST /v1/tasks`
keeps refusing them. The design states the one residual (a registration that
could not be made at startup) in
[§3.2](design/child-tasks.md#32-what-makes-only-the-worker-true-the-attempt-key).

**OD-B15-3 -- Capacity: what a parent may do while its children run.** It may
run, and submit. It may not wait while holding its lease: the agent gets no
channel that reports a running child's progress, and the only way to learn a
child's result is `await`, which checkpoints, parks the parent with
`ParkReason.CHILDREN_INCOMPLETE` (request 40), releases its lease and exits.
The scheduler promotes it when every child is terminal and every succeeded
child's outputs are written; the resumed worker stages them before the agent
starts. So:

* invariant 1: an awaiting parent is `PARKED` and costs nothing; children cost
  nothing until leased;
* invariant 2: each child acquires its own lease all-or-nothing through the
  unchanged admission; no gang reservation for a fan-out;
* invariant 3: children count from `LEASED` against the same pools; the slot
  the parent gives back by awaiting is the one its children can use, so the
  narrow-pool deadlock this entry warned of cannot form;
* invariant 4: no in-worker wait for a child of any length.

The await refunds the attempt admission counted, up to
`max_child_await_resumes` (4), so waiting is not failing. Depth is 1; fan-out
is capped at 16 children per task across all its attempts. An agent that exits
0 with live children is awaited anyway: a parent never succeeds over running
children.

**OD-B15-4 -- Cancellation.** Cancelling a parent cancels its non-terminal
children: the API's cancel cascades at once, and a scheduler sweep guarantees
it. A parent that ends `FAILED` or `DEAD_LETTERED` with live children has them
cancelled too. A retried or fenced parent keeps them: they belong to the task,
and the next attempt finds them. An awaiting parent past
`child_await_max_seconds` (one day) has its outstanding children cancelled and
is then promoted. A child's end never cascades upward. Every cascade is
tenant-scoped, and the API never releases capacity: a running child is flagged
and stops through the ordinary cancel path (invariant 1). A cascaded child
ends with `EndCause.CHILD_CASCADE` (request 41).

**The workflow rollup** (request 7) does not count a child: a child has no
`workflow_id`, even when its parent is a workflow step.

**The TypeScript restatement** stands as requested: `types.ts` gains the two
fields, and `check-contract-parity.sh` section 5 holds them, together with the
values requests 40 and 41 add.

The acceptance needed four more changes to the frozen contract. Each exists
only because this one was accepted, and each is filed as its own request:
[40](#40-statespy-an-agent-awaiting-its-children-has-no-park-reason-and-dependency_incomplete-would-be-promoted-at-once),
[41](#41-modelspy-a-child-cancelled-because-of-its-parent-has-no-end-cause),
[42](#42-specsignpy-the-signed-step-spec-does-not-cover-a-childs-parent) and
[43](#43-identitypy-the-tenant-worker-service-accounts-name-has-no-public-home).

---

## 15. `models.py`: `Attempt` records memory, disk and spend, but not CPU

**Status: ACCEPTED — accepted by the owner on #184, 2026-09-25, as amended
(four fields), and applied in PR #210.** The decision, in the owner's words
([#184, 2026-09-25](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/184#issuecomment-5840698104)): "Contract request #15 is approved: typed
`Attempt` fields `cpu_seconds`, `peak_cpu_cores`, `mean_cpu_cores` and
`cpu_limit_cores`. They replace the interim heartbeat-event path." Raised
2026-09-24 by the worker-broker lane, which measured CPU without it and
stopped at the frozen line. What was applied, and what it replaced, is under
[Applied](#applied-2026-09-25-pr-210) at the end of this entry.

### What is there now, without the contract

`agent_worker/metrics.py` had no CPU reading at all, so "requested vs used"
(docs/web-ui/redesign-v2.md section 6, S5) had a memory row and could not have
a CPU row. It now measures CPU on every attempt -- cgroup v2 `cpu.stat` when the
container has one, the runner's process tree in /proc otherwise, the kernel's
reaped-children total as the last resort -- and reports:

| field | meaning |
|---|---|
| `cpu_seconds` | CPU time consumed while the attempt's runners ran, summed over every runner it started |
| `peak_cpu_cores` | the busiest sampling interval of any of those runners, in cores |
| `mean_cpu_cores` | total `cpu_seconds` over the total runner time it was measured across |
| `cpu_source` | `cgroup` (the whole container), `proc` (the runner's tree) or `rusage` |

Every figure is the ATTEMPT's. An attempt restarts its runner in place after
a short rate limit or a reloaded credential. Each runner has its own sampler,
and `metrics.combine_usage` puts the runners back together. The first cut of
this reported only the last runner. The same fix also corrected
`peak_rss_bytes` / `peak_disk_bytes` / `oom_near_miss` on the attempt
document, which each runner used to overwrite with its own figure.

It reaches three places, none of them typed or indexable:

* the `attempt resource usage` log line (`LoggingMetricsExporter`);
* Cloud Monitoring, as `cpu_time_ms`, `peak_cpu_millicores` and
  `mean_cpu_millicores` -- which swarm-api cannot read (it holds no
  `roles/monitoring.viewer`);
* every HEARTBEAT event, as a CUMULATIVE `cpu_seconds`, so two consecutive
  events give utilisation over the span between them. This is the only
  per-attempt CPU figure a reader of the API can reach today. It covers every
  runner the attempt has started, so an in-place restart continues the series
  instead of resetting it. The span between two events can include a retry
  wait in which no runner ran, and utilisation computed over that span counts
  the wait as idle time.

### Why it stopped there

The one place a per-attempt figure belongs is the attempt document, next to
`peak_rss_bytes` -- and `record_resource_usage` writes only the keys the frozen
`Attempt` declares, for the reason `record_spend` gives: an untyped field on the
busiest document is one no index can reach and every reader must know by
convention.

### The requested change

Add to `Attempt`, beside `peak_rss_bytes`:

```python
cpu_seconds: float | None = None
peak_cpu_cores: float | None = None
```

Optional, defaulting to `None`, so every existing document remains valid and
nothing migrates. `None` means NOT MEASURED -- a macOS local run has a total and
no peak, and an attempt fenced before its runner started has neither -- which
is different from an agent that used no CPU. `mean_cpu_cores` is deliberately
not requested. `cpu_seconds` over the attempt's own `started_at`/`completed_at`
gives an approximation of it. That approximation is lower than the worker's
figure, because the attempt's span also covers setup, the clone and any retry
wait, where the worker divides by runner time only. For a sizing question
("how much of the reserved CPU did the agent use") the lower figure is the one
that matters, since the reservation is held for the whole span.

### What breaks if it is made

Nothing reads these yet. `agent_worker.control.record_resource_usage` would
gain two keys; `swarm_api.codec.attempt_from_dict`/`attempt_to_api` would each
gain two lines, and `test_api_contract_shapes.py` pins the latter, so the
serialiser cannot drop them silently. `apps/swarm-ui/src/types.ts` restates
`Attempt` by hand and would need the two fields added there too --
`check-contract-parity.sh` does not cover TypeScript.

### What is left to live with if it is declined

A CPU row in "requested vs used" can still be drawn, from the last HEARTBEAT
event of each attempt. It is up to one heartbeat period stale at the end of a
run (the last event before exit is not the exit), it is not queryable across
attempts, and a "CPU per resource class over the last week" report means
reading every attempt's event stream.

### Amendment, 2026-09-25 (#184): add the mean and the limit

**Status:** accepted with the request, 2026-09-25 (see the status line above).
Raised by the #184 backend lane.

**What changed.** The owner decided on #184 that Details shows CPU as the
PEAK and the MEAN cores of the runtime's CPU limit, beside memory and
workspace. The request above deliberately left `mean_cpu_cores` out; it is now
asked for, together with the limit it is a fraction of. The requested change
becomes four fields on `Attempt`, beside `peak_rss_bytes`:

```python
cpu_seconds: float | None = None
peak_cpu_cores: float | None = None
mean_cpu_cores: float | None = None
cpu_limit_cores: float | None = None
```

`mean_cpu_cores` is the worker's figure -- CPU-seconds over RUNNER wall time,
so setup, the clone and retry waits are excluded -- not an approximation from
`started_at`/`completed_at`. `cpu_limit_cores` is what the kernel enforces
(cgroup v2 `cpu.max`), or, where that reads `max` or cannot be read, the
catalogue cpu of the class the container was SIZED with
(`scheduler.dispatch.resource_class_for(task, profile)`, not the profile's own
class). None means not measured throughout, never zero.

**The interim, shipped without the contract (#184).** The worker now puts
every figure on its HEARTBEAT events as flat keys in `detail`
(`agent_worker.metrics.heartbeat_cpu_fields`): the existing `cpu_seconds` and
`cpu_source`, plus `peak_cpu_cores`, `mean_cpu_cores` (kept current while the
runner runs, not only at exit), `cpu_wall_seconds`, `cpu_limit_cores`,
`cpu_limit_source` (`cgroup` or `resource_class`) and `final`. The periodic
reading stays on every fifth heartbeat; one more, with `final: true`, is
emitted when each runner is reaped, and never by an attempt that has been
fenced. `GET /v1/tasks/{id}/attempts?include=usage` serves the newest reading
per attempt from ONE descending read of the task's events
(`swarm_api.attempt_usage`), with a status that says what the reading is
(`final`, `live`, `last_reading`, `never_ran`, `absent`, `beyond_window`,
`unread`). It works; it is not queryable across attempts, and it costs an
events read per request, which is why it is opt-in.

**What changes if this is accepted.** `control.record_resource_usage` writes
the four fields at each runner's end (the same combined, attempt-wide figures
the final HEARTBEAT carries); `codec.attempt_from_dict` / `attempt_to_api`
read and serve them (`test_api_contract_shapes.py` pins the serialiser);
`include=usage` prefers the typed fields for a `final` reading and reads
events only for a live one. `apps/swarm-ui/src/types.ts` restates `Attempt`
by hand and needs the same four fields.

### Applied, 2026-09-25 (PR #210)

The owner's decision went further than the amendment's last paragraph: the
typed fields REPLACE the interim path, so nothing reads the events for a live
reading either.

* **The contract.** `Attempt` gains the four fields, `float | None = None`,
  at the end of the class rather than beside `peak_rss_bytes`, so no
  positional construction changes meaning. Nothing else in
  `apps/common/swarm_common` changed.
* **The worker** writes them on its own attempt document
  (`control.record_cpu_usage`, from `metrics.attempt_cpu_fields`) with each
  periodic reading, which is every fifth heartbeat, and when each runner is
  reaped. They are the attempt's combined figures. A key that was not measured
  is left out of the merge, never written as null.
* **Removed:** the interim keys on the HEARTBEAT event (`peak_cpu_cores`,
  `mean_cpu_cores`, `cpu_wall_seconds`, `cpu_limit_cores`,
  `cpu_limit_source`, `final`), the `final` HEARTBEAT emitted when a runner
  was reaped, `swarm_api.attempt_usage`, and `include=usage`. The HEARTBEAT
  carries exactly what it did before #188. `cpu_seconds` and `cpu_source`
  stay, because `reconciler.progress` reads them.
* **The API** serves the four fields on every attempt row
  (`codec.attempt_from_dict` / `attempt_to_api`, pinned by
  `test_api_contract_shapes.py` and `test_attempt_cpu_fields.py`).
* **The UI** restates them on `AttemptRow`, as optional fields, where a
  missing key means an API older than the fields.
  `test_ui_api_field_contract.py` holds them both ways.
  `scripts/lib/check-contract-parity.sh` restates no `Attempt` field and did
  not change.
* **Attempts from before the typed fields.** Attempts that ran between #188's
  deploy and this one carry their CPU only on HEARTBEAT events. A read-only
  look at dev at 23:16 UTC on 2026-09-25 found one. Attempts from before #188
  carry their cpu-seconds there too, with no cores. The UI keeps a legacy
  reader for both over the drawer's own event page, and the server keeps
  none. `docs/agent-output.md` says why.
* **What the four fields cannot say.** The interim reading carried its time
  (`measured_at`, `age_seconds`) and the limit's source (`cpu_limit_source`).
  The accepted fields carry neither. Request #26 asks for both; the owner
  accepted it on 2026-09-26 and PR #229 applied it.

---

## 16. `identity.py`: the tenant namespace name, `sanitize_name` included, has two copies

**Status:** open, recorded 2026-09-24 by the reconciler-gke lane (PR #32). The
owner asked for the reconciler to name tenant namespaces "through the SAME
namespace-naming helper the dispatcher uses". It cannot import that helper, so
this is the request that would make that literally true.

### What is duplicated

The rule the dispatcher uses to name a tenant's namespace when the tenant
document records none:

* `apps/scheduler/scheduler/dispatch.py`: `GkeJobDispatcher.namespace_for`
  returns `sanitize_name(template.format(tenant=tenant_id))`, with
  `namespace_template = "swarm-tenant-{tenant}"`.
* `apps/reconciler/reconciler/backends.py`: `GkeBackend.namespace_for`
  returns `sanitize_name(prefix + tenant_id)`, with
  `ReconcilerConfig.namespace_prefix = "swarm-tenant-"`. `sanitize_name` here
  is a copy of the scheduler's. The comment-stripped bodies are identical, and
  both import the character class from `swarm_common.identity._TENANT_SAFE`.

Both prefer the tenant document's recorded `namespace`. That precedence is also
stated twice.

### Why it cannot be one copy today

The same reason as request 5. `images/swarm-reconciler/Dockerfile` copies
`apps/common/` and `apps/reconciler/`, and `images/swarm-scheduler/Dockerfile`
copies `apps/common/` and `apps/scheduler/`. Neither image contains the other's
package, so `swarm_common` is the only place a single copy could live in both.

### How the copies drifted before the copy existed

Until PR #32, the reconciler named the namespace with `detect.sanitised`. That
function reproduces only the character-class half of `sanitize_name`. It has no
63-character truncation, no hash of the full name, and no `s` prefix for a name
that does not start with a letter. The only test pinning the two used short
tenant ids, so it could not see the difference.

The difference was latent, not observed:

* `identity._slug` caps a tenant id at 11 characters.
* `scripts/register-tenant.sh` refuses anything longer.
* A live attempt is read at the namespace its own `execution_name` records.

### What holds the copies together now

`tests/unit/control_plane/test_reconciler_gke_namespaced.py`:

* `test_the_reconciler_and_the_dispatcher_sanitise_names_identically` runs both
  `sanitize_name` copies over a corpus that reaches every branch.
* `test_the_reconciler_names_a_long_tenant_namespace_exactly_as_the_dispatcher_does`
  pins the two `namespace_for` methods on ids up to 200 characters.
* `test_the_reconciler_names_a_tenant_namespace_exactly_as_the_dispatcher_does`
  pins the recorded-namespace precedence.

`scripts/lib/check-contract-parity.sh` section 6 holds the prefix to the
template. As with request 5, these checks work only because the test process
can import both services, which neither image can.

### The requested change

Add a stdlib-only function to `swarm_common/identity.py`, next to
`_TENANT_SAFE`, which it already depends on:

```python
def k8s_name(*parts: str, max_length: int = 63) -> str: ...        # today's sanitize_name
def tenant_namespace(tenant_id: str, recorded: str | None = None,
                     template: str = "swarm-tenant-{tenant}") -> str: ...
```

Both services would call it. Two decisions go with it:

* **The exception type.** Today the scheduler raises `DispatchError(code=
  "invalid_resource_name")` and the reconciler raises `ValueError`. A shared
  function would raise `ValueError`, and the dispatcher would wrap it.
* **Whether `detect.sanitised` should use `k8s_name` too.** `sanitised` matches
  label-derived ids back to document ids, and labels also pass through
  `sanitize_name` with the 63-character cap. Task and attempt ids are 25
  characters, so the cap is unreachable there today.

### What it would break if accepted

It adds functions and changes none, so no stored document and no wire format
changes. There is also a third copy, in shell: `tenant_namespace` in
`scripts/lib/common.sh`, which `register-tenant.sh` calls. It is the bare
prefix plus the id, with no sanitising. That is correct only because
`register-tenant.sh` first refuses any id that is not a clean slug of 11
characters or fewer. It would stay a restatement, and `check-contract-parity.sh`
section 6 would still hold its prefix.

### What is left to live with if it is declined

Two copies, and three tests that pin them over inputs longer than any id in
use. If the rule changes, both copies must be edited. If only one is edited,
the tests fail rather than production.

---

## 17. `states.py`: a cancel that is only requested is recorded as `cancelled`

**Status: ACCEPTED — accepted by the owner in session, 2026-09-24 — and applied
in PR #44.** This edits `apps/common/swarm_common/states.py`, which is frozen,
and is recorded here as such. The owner accepted it as "#13" together with
request 18 as "#14": those were the session's numbers for the two incident
requests, not this file's. **Requests 13 and 14 in this file (the pool account
on `Attempt`, a sub-agent's parent) are NOT accepted by this and are still
open.**

Recorded 2026-09-24 from incident `wf_ebb3ab2d65664707a559` (where it was filed
as CR-1). Found while the incident's stuck tasks were read event by event. It
did not cause that incident.

### What was applied

* `EventType.CANCEL_REQUESTED = "cancel_requested"`, documented in `states.py`
  as NOT terminal, next to `CANCELLED`, which is documented as "the task
  reached CANCELLED".
* `swarm_api.store.request_cancel` writes `CANCEL_REQUESTED` when it only sets
  the flag and `CANCELLED` only for the transition it makes itself (SUBMITTED,
  QUEUED, READY, PARKED to CANCELLED). `detail.phase` is still written on both,
  so a reader of the old discriminator keeps working.
* The terminal `cancelled` for a task that held capacity was already written by
  whoever finishes it, and is unchanged: the worker (`control.finish`), the
  reconciler (F-3, `repair.py`), the scheduler (`SchedulerStore.cancel`).
* **History is read, not rewritten.** Events stored before this change keep
  `type: cancelled, phase: cancel_requested`. `swarm_api.codec.stored_event_type`
  (called by `event_from_dict`, the decoder behind every event route) serves
  exactly that shape as `cancel_requested`, with the detail as stored, so a
  served legacy request is the same shape as a new one. A `cancelled` with
  `phase: cancelled` or with no phase is a real cancel and is untouched. No
  migration was written: this lane writes no live data, and the read is one
  line where a migration is a write to every task's history.
* Four readers can see the raw legacy row, and each applies the same reading to
  it. Only one reads Firestore directly: `scripts/load-test.sh`, through
  `testlib.sh`'s `task_events`. The other three read through the API. They
  carry their own copy because they can meet a `swarm-api` image from before
  this change, which serves the legacy row as stored:
  * `swarm_mcp.follow.event_type`, called by `swarm_follow` (`_event_row`) and
    `swarm tail` (`cli.cmd_tail`);
  * `scripts/benchstat.py`, whose one collector, `bench-dispatch.sh`, reads
    `GET /v1/tasks/{id}/events`. (Corrected 2026-09-24: this record first said
    benchstat reads Firestore directly. It does not.)
  * the console's `apps/swarm-ui/src/events.ts`, used by the Timeline's
    end-of-history check and its type labels.
* Tests: `tests/unit/control_plane/test_cancel_request_is_not_a_cancel.py`,
  plus additions to `test_request_cancel_is_transactional.py`,
  `tests/unit/mcp/test_follow_cursor.py`, `tests/unit/scripts/test_benchstat.py`
  and `apps/swarm-ui/src/__tests__/cancel.request.timeline.test.tsx`. Every
  copy of the legacy reading except `load-test.sh`'s jq is pinned by a test
  that feeds it the raw row. For the plugin, two tests run the real application
  with its decoder put back to the pre-change `EventType(data["type"])`: through
  the current API, which already reads the legacy shape, removing either call
  site stayed green.

### What is still true after it (the breakage the request predicted)

The breakage predicted below is real and was not engineered away. A
`swarm-api` image from before this change decodes a stored event with
`EventType(data["type"])`, and `cancel_requested` is not in its enum. So when
such an image serves a page of `GET /v1/tasks/{id}/events` that holds a
`cancel_requested` event, it raises `ValueError` inside `_keyset_page`. There is
no per-row handling, so the whole page is a 500. `swarm-api` is the only
decoder of stored events; the scheduler, reconciler and worker never decode
one.

That happens in two situations, and only the first one ends by itself:

* **During the rollout.** An instance still on the old image serves the page
  after a new instance has written the event. This lasts until the rollout
  completes.
* **After a rollback past this change.** The documented recovery for a broken
  control-plane service is to deploy the previous manifest
  ([operations.md §7](operations.md#7-deploying-a-change),
  [disaster-recovery.md §3](disaster-recovery.md#3-a-control-plane-service-is-broken)).
  The `cancel_requested` events written since the deploy are permanent. A
  pre-change image fails on them for as long as it serves, on every task that
  was cancelled while it held capacity. The console Timeline, `swarm_follow`
  and `swarm tail` then report that task's history as unreadable, during an
  incident, which is when it is needed most. Reverting this change has the
  same effect. (Corrected 2026-09-24: this record first said the crash "lasts
  one rollout of `swarm-api`", which holds only if nobody rolls back.)

Both runbooks now say this: do not roll `swarm-api` back to a build from before
this change, and do not revert it. Roll forward instead, and check a rollback
target with `git grep CANCEL_REQUESTED <tag> -- apps/common/swarm_common/states.py`.

Not done, because it is the owner's choice: shipping the reader (the enum
member and `stored_event_type`) as a release of its own before the writer
(`request_cancel`). That would make a one-step rollback after the writer safe.
It would not make a rollback past the reader safe, so the runbook constraint
is needed either way.

### The claim that is false

`EventType.CANCELLED` (`states.py`, value `"cancelled"`) is read as "this task
is cancelled". The UI treats it as one of the four terminal events
(`AgentDetail.tsx`, `TERMINAL_EVENTS`: "a terminal task has one by
construction"). It is also written when nothing has been cancelled.

`swarm_api.store.request_cancel` handles a task that holds capacity (LEASED,
DISPATCHED, STARTING, RUNNING) by setting `cancel_requested = true` and nothing
else, because the worker or the reconciler has to release the lease. It still
appends an event of type `CANCELLED`, and only a detail field,
`phase: "cancel_requested"`, distinguishes it from a real cancel.

### Measured

check-2, check-3, check-4 and check-5 of `wf_ebb3ab2d65664707a559` each carry a
`cancelled` event from the cancel POSTs at 07:40:20–07:40:31Z, with
`from_state: DISPATCHED` and `phase: cancel_requested`. At 08:55:11Z all four
were still `DISPATCHED`, with leases unreleased. For more than an hour, every
consumer that reads the event type saw four cancelled tasks, and the task
documents said otherwise.

### The requested change

Add `EventType.CANCEL_REQUESTED = "cancel_requested"`. `request_cancel` emits it
when it only sets the flag, and keeps `CANCELLED` for the immediate transition
it makes itself (SUBMITTED, QUEUED, READY, PARKED to CANCELLED). The terminal
`cancelled` for a task that held capacity then comes from whoever actually
finishes it:

* the worker (`control.py`, state-to-event map);
* the scheduler (`SchedulerStore.cancel`);
* the reconciler, once incident item F-3 lands.

### What it would break if accepted

* **It is additive to the enum.** Every writer of existing values is unchanged.
* **An old reader crashes on the new value.** `swarm_api.codec.event_from_dict`
  decodes a stored event with `EventType(data["type"])`. An API instance on the
  previous image that lists the events of a task holding a `cancel_requested`
  event raises `ValueError` for that page. `swarm-api` is both the only writer
  and a reader, so the exposure is one rolling deploy. It is still a window in
  which a task's event page can fail. (Corrected after acceptance: a rollback
  to an image from before the change reopens it, for as long as the rollback
  lasts. See "What is still true after it" above.)
* **History keeps the old shape.** Events already written stay
  `type: cancelled, phase: cancel_requested`. A reader that wants the truth
  about old tasks must keep reading `detail.phase`, unless a one-off migration
  rewrites them. Nothing here proposes that migration.
* The UI's `TERMINAL_EVENTS` and any timeline grouping need to show the new type
  as a non-terminal marker. That is a UI change in `apps/swarm-ui`.

### If it is declined

The phase discriminator stays the only signal, so every consumer that treats
`cancelled` as terminal must also check `detail.phase`. The UI's
terminal-event check is one such consumer today. A consumer that forgets
reports a cancelled task that still holds capacity, which is the situation this
incident's operators were in.

---

## 18. `profiles.py`: `RunnerProfile.command` is the lifecycle's child argv, never a container command

**Status: ACCEPTED — accepted by the owner in session, 2026-09-24 — in its
stronger form, and applied in PR #44.** This edits
`apps/common/swarm_common/profiles.py`, which is frozen, and is recorded here as
such. The owner's "#14" (see request 17's status for the numbering): the field
is RENAMED to `runner_argv` and documented as the argv the worker lifecycle
starts as its child, never a container command.

Recorded 2026-09-24 from incident `wf_ebb3ab2d65664707a559` (where it was filed
as CR-2). The code defect it describes is fixed outside the frozen package, in
`apps/scheduler/scheduler/dispatch.py`. This request is about the field that
made the defect easy to write.

### What was applied

* `RunnerProfile.command` is now `RunnerProfile.runner_argv`, with the comment
  proposed below on the field itself (plus a note of the old name and why it
  changed). Values are unchanged.
* Every reader moved: `agent_worker.lifecycle._runner_argv`,
  `swarm_api.runnerinputs.runner_module`, the comments in
  `scheduler/dispatch.py`, and the tests listed below plus
  `test_runtime_catalogue.py`, `test_runtimes_screen.py`,
  `test_dispatch_manifests.py` and `tests/unit/mcp/test_profiles.py` (whose
  leak list named `command` and now names `runner_argv`, with an assertion
  that every name on it is a real field).
* `tests/unit/control_plane/test_runner_argv_is_the_lifecycles_child.py` pins
  that no field is called `command`, that every profile's `runner_argv` names a
  real runner module, and that the warning stays on the field.
* Not a wire change and not a data change, as predicted: no route serialises
  the field and no stored document holds it. `scripts/lib/check-contract-parity.sh`
  restates nothing about it (checked).

### What happened

`RunnerProfile.command` is `("python", "-m", "agent_worker.runners.<x>")`, the
argv of the RUNNER. The worker lifecycle starts it as a supervised child
(`agent_worker.lifecycle._runner_argv`), and `swarm_api.runnerinputs` reads its
last element to name the runner module. Nothing in `profiles.py` says so. A
field named `command` on the object that describes what a container runs reads
as a container command. Both dispatchers used it that way:

* `GkeJobDispatcher._manifest` set `"command": list(profile.command)`;
* `CloudRunJobDispatcher._build_job` set `command=list(profile.command)`.

A container `command` replaces the image ENTRYPOINT, which is the lifecycle.
Every GKE pod therefore ran the bare runner, with no fencing (invariant 5), no
checkpoints (invariant 8), no heartbeat and no lease release. Five leases held
every browser slot until an operator intervened. The Cloud Run copy was latent
only because Terraform's Jobs leave `command` unset. The prohibition existed in
exactly one place, a comment in `kubernetes/worker-templates/worker-job.yaml`
that the code never read.

### The requested change

The minimum is a comment on the field:

```python
#: The argv of the RUNNER, which the worker lifecycle starts as a supervised
#: CHILD (agent_worker.lifecycle._runner_argv). NEVER a container command:
#: both worker images' ENTRYPOINT is the lifecycle (`tini -- python -m
#: agent_worker`), and a container `command` replaces it, switching off
#: fencing, cancel, heartbeat, checkpointing and lease release at once. A
#: dispatcher names the profile in RUNNER_PROFILE and sets no command.
command: tuple[str, ...]
```

The optional, stronger change is to rename the field to `runner_argv`, so that
`list(profile.command)` cannot be written by someone reaching for a container
command.

### What it would break if accepted

* **The comment breaks nothing.**
* **The rename touches every reader:**
  * `agent_worker/lifecycle.py` (`_runner_argv`);
  * `swarm_api/runnerinputs.py` (`runner_module`);
  * `tests/unit/worker/test_runners.py`;
  * `tests/unit/control_plane/test_workflow_step_input_surface.py`.

  It is not a wire change. The runtimes catalogue
  (`swarm_api/routes/platform.py`) serialises the profile field by field and
  does not include `command`. No stored document holds the field either, since
  profiles are code, so there is no data migration.

### If it is declined

The guard is the tests added with the fix. For every profile, on both backends,
`tests/unit/control_plane/test_dispatch_manifests.py` asserts that neither
dispatcher sets `command` or `args`.
`tests/unit/worker/test_kubernetes_manifests.py` asserts that the dispatcher's
Job and the rendered YAML agree field by field. Those catch a reintroduction.
Nothing warns the next reader of `profiles.py` before they write it.

---

## 19. `states.py`: no `EventType` says "the reconciler evicted this execution"

**Status:** open, recorded 2026-09-24 by the browser-eviction lane. The owner
asked for browser pods that are stuck without progress, or left running after
their task finished, to be evicted "with an event naming the reason". The event
is written. Its type is borrowed.

### What is there now, without the contract

The reconciler's two GKE eviction rules (`apps/reconciler/reconciler/detect.py`,
runbook `docs/runbooks/browser-eviction.md`) write these events:

* **`stuck_no_progress`.** The reconciler only fences in that pass, so
  `generation_fenced`, with `detail.finding` and `detail.reason`, is accurate.
  The rules that finish the job on a later pass write their usual
  `lease_released`, then `ready`, `failed` or `cancelled`.
* **`left_running`.** The task is already terminal, and nothing is fenced. The
  event is `generation_fenced` with `detail.phase: "left_running"`. That type is
  the nearest the frozen `EventType` offers: the reconciler stopped an
  execution of this generation that had no right to run. It is still a
  borrowed word. A reader of the task timeline sees "generation fenced" after
  "succeeded" and must open `detail` to learn it was an eviction.

`EventType` cannot be extended from outside: `swarm_api.codec` parses every
stored event with `EventType(data["type"])`, so writing an unknown type string
would break the read path for that task.

### The requested change

Add one member to `swarm_common/states.py`:

```python
class EventType(str, Enum):
    ...
    EXECUTION_EVICTED = "execution_evicted"
```

The `left_running` rule would write it instead of `generation_fenced`. The
stuck rule would keep `generation_fenced`, because fencing is what it does.

### What breaks if it is made

Nothing stored. Old events keep their types. The costs:

* **An old reader crashes on the new value.** This is the same window request
  17 describes. `swarm_api.codec` decodes with `EventType(data["type"])`, so an
  API instance on the previous image fails the event page of any task holding
  an `execution_evicted` event until the rolling deploy finishes. Request 17
  and this one both add a member to `EventType`, so they are cheapest applied
  together, in one release.
* **The UI.** `AgentDetail.tsx` and `AttemptTimeline.tsx` render `e.type` as
  text, so the new type shows as its own name. `TERMINAL_EVENTS` does not list
  it, which is correct, because an eviction is not a terminal transition.
* **Closed-list consumers.** Anything that assumes the list of types is closed
  needs a case for the new one. None was found in `apps/`.

### What is left to live with if it is declined

The `generation_fenced` events with `phase: left_running` stay as they are.
They are accurate about what was stopped, but not about why. The pass report
and the `evicted a GKE job` log line name the kind exactly either way.

---

## 20. `models.py`: `Workflow.on_step_failure` is a bare `str`, and its vocabulary is stated four times

**Status:** open, recorded 2026-09-24 by the lane that made the scheduler honour
the field (branch `lane/on-step-failure`). It was filed as 19 on that branch,
and renumbered 20 when main took 19 for the browser-eviction request.

### What is true today

`Workflow.on_step_failure: str = "fail_workflow"   # or "continue"`. The comment
is the only place in the frozen contract that names the second value. Nothing
constrains the first. The vocabulary is therefore stated separately by each
component that needs it:

* `swarm_api.schemas.WorkflowCreate.on_step_failure`:
  `Literal["fail_workflow", "continue"]`, the only validation;
* `swarm_mcp.server.TOOLS` (`swarm_workflow`): a JSON-schema `enum` and
  `default`;
* `scheduler.loop.FAIL_WORKFLOW`: the one value the scheduler acts on;
* the frozen dataclass default itself.

Until 2026-09-24 the scheduler did not read the field, so a disagreement
between these copies cost nothing. It does now. A rename of `fail_workflow` in
the API's Literal that missed `scheduler/loop.py` would make the scheduler
ignore the setting again, silently, which is the defect that change fixed.
That is the mirrored-value failure this repository has already had three
outages from.

### The requested change

Add to `states.py` (or `models.py`):

```python
class OnStepFailure(str, Enum):
    """What a FAILED or DEAD_LETTERED step does to the rest of its workflow.

    CANCELLED is not a failure under either value: a stopped step takes only its
    own dependents.
    """
    #: Cancel every step of the workflow that has not started.
    FAIL_WORKFLOW = "fail_workflow"
    #: Cancel only the transitive dependents of the failed step.
    CONTINUE = "continue"
```

and type the field `on_step_failure: OnStepFailure = OnStepFailure.FAIL_WORKFLOW`.
The API's Literal, the MCP enum and the scheduler constant are then derived from
`OnStepFailure` rather than restated.

### What it would break if accepted

* **The stored value does not change.** A `str` Enum's value is the same string,
  so no workflow document needs migrating.
* **Every constructor of `Workflow` still type-checks at runtime,** because
  dataclasses do not enforce annotations. A caller passing a plain string keeps
  working until it is changed to pass the enum.
* **`asdict(workflow)` would carry the enum member, not the string.**
  `swarm_api.codec.workflow_to_firestore` must write `.value` explicitly, as it
  already does for `state`. Otherwise the Firestore client receives an Enum,
  and whether it serialises a `str` subclass unchanged has not been verified.
* **Decoding must map an unknown stored value to something.** Today the API
  echoes whatever string is stored. `OnStepFailure(value)` would raise. The
  decoder needs an explicit rule, and the scheduler's current rule (anything
  other than `fail_workflow` leaves only the dependency rule in force) is the
  conservative one, because a cancel cannot be undone.

### If it is declined

`tests/unit/control_plane/test_on_step_failure.py::test_the_policy_vocabulary_agrees_everywhere_it_is_stated`
holds the four copies together: the API's Literal, the MCP enum and default,
the frozen default and the scheduler's constant. It catches a rename in any one
of them in CI. It does not stop a fifth copy from being written somewhere it
does not look.

---

## 21. `states.py`: the worker's exit codes have no shared home, and the reconciler now acts on one

**Status:** open, recorded 2026-09-25 by the lane that made exit 78
non-retryable (branch `lane/cannot-start-handling`). If another branch has
taken 21 by the time this merges, renumber this one.

### What is true today

The worker's exit codes are `agent_worker.errors.ExitCode`. Until 2026-09-25 no
other component read them. The owner decided on 2026-09-25 that a worker that
cannot start exits 78 and that 78 is non-retryable. So the reconciler now reads
a finished execution's exit code (a Cloud Run task's
`last_attempt_result.exit_code`, a GKE pod's `state.terminated.exitCode`). On
78 it fails the task at once, with no retry. The same change split a second
code off 78: 69, "a dependency was unavailable before the runner", which the
reconciler must go on retrying.

The reconciler's image carries `apps/common/` and `apps/reconciler/` only, so
it cannot import `agent_worker`. The number is therefore stated twice:

* `agent_worker.errors.ExitCode.CONFIG = 78`, which the worker exits with;
* `reconciler.detect.WORKER_EXIT_CANNOT_START = 78`, which the reconciler
  fails a task on.

`tests/unit/worker/test_worker_cannot_start.py::test_the_reconcilers_78_is_the_workers_78`
holds the two together in CI.

Since #198 the reconciler acts on every OTHER code too, but only to requeue,
and without naming any of them: an execution that ended with anything but 78
while its task was still DISPATCHED or STARTING is fenced, released and
requeued in the pass that sees it (`reconciler.detect.detect_ended_at_startup`),
with the code quoted in `last_error`. That rule needs no second number, so it
adds no copy. It does depend on 78 being the only code that must NOT be
requeued, which is the same fact this request would put in one place. That is the mirrored-value arrangement this
repository has had three outages from. Here the failure would be quiet in both
directions. If the worker's number moved and the reconciler's did not, a worker
that cannot start would go back to being retried until its attempts ran out.
If the reconciler's moved onto a code the worker uses for something else, such
as 69 or 75, tasks that should be retried or parked would be failed.

### The requested change

Add the worker's exit contract to `swarm_common/states.py`, beside the other
vocabularies both sides of the platform act on:

```python
class WorkerExit(IntEnum):
    """What a worker process's exit status means to the platform."""
    OK = 0
    FAILED = 1
    #: A dependency was unavailable before the runner. Retried.
    UNAVAILABLE = 69
    GENERATION_FENCED = 70
    CANCELLED = 71
    PARKED = 75
    TIMEOUT = 76
    #: The worker cannot start, and another attempt would fail the same way.
    #: The reconciler fails the task without a retry.
    CANNOT_START = 78
    TENANT_MISMATCH = 79
```

`agent_worker.errors.ExitCode` and `reconciler.detect.WORKER_EXIT_CANNOT_START`
would then both be derived from it, and the parity test deleted.

### What it would break if accepted

* **Nothing stored.** Exit codes are not persisted as names. The attempt
  document's `exit_code` is the integer, and it stays the integer.
* **`ExitCode.CONFIG` is referenced by that name** in the worker and its tests.
  Keeping `CONFIG = WorkerExit.CANNOT_START` as an alias avoids a rename in
  the same change.
* **143** (128 + SIGTERM, `startup.EXIT_INTERRUPTED`) is a signal convention,
  not a platform decision, and could stay where it is.

### If it is declined

The parity test stays and does its job for these two copies. A third reader of
exit codes (a UI badge, a smoke check, an alert on 78s) would have to restate
the number again, and would need its own parity test to be safe.

---

## 22. `profiles.py`: whether a runner profile can run on a pool account is stated by the worker and restated by the scheduler

**Status:** open, recorded 2026-09-25 by the lane that made admission and
dispatch ask one question (branch `lane/pool-credential-admission`, #169). If
another branch has taken 22 by the time this merges, renumber this one.

### What is true today

A pool account is a Claude subscription. Its token fills one variable,
`CLAUDE_CODE_OAUTH_TOKEN`, and a runner profile can run on an account only if
its `secrets` declare that name. `claude-code` does. `browser` does not.

The frozen catalogue says nothing about this. It lists the names, and the
meaning of one of them is decided outside it:

* `agent_worker.accountlease.ACCOUNT_TOKEN_ENV`, which the worker checks before
  it asks the broker for an account (`lifecycle._lease_account`);
* `scheduler.credentials.SUBSCRIPTION_TOKEN_ENV`, which admission checks before
  it lets a tenant with no key of its own through on the pool.

The scheduler's image does not carry the worker, so it cannot import the
worker's constant. `tests/unit/worker/test_pool_credential_parity.py` holds the
two together. It compares the constants, and it also runs the real worker's
credential resolution for every profile in the catalogue against the
scheduler's `credential_for`.

If they drifted, the failure would be quiet in both directions:

* the scheduler expects the pool and the worker does not ask it: a keyless
  tenant's task is admitted, a container starts, the worker parks it on
  CREDENTIAL_MISSING, and the credential sweep promotes it again on the next
  drain;
* the worker would ask and the scheduler does not expect it: a tenant the pool
  serves is parked at admission and never runs.

### The requested change

Add to `profiles.py`:

```python
#: The variable a Claude subscription token fills. A profile whose `secrets`
#: name it can run on an account from the pool; one that does not, cannot.
SUBSCRIPTION_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


@dataclass(frozen=True)
class RunnerProfile:
    ...

    @property
    def runs_on_a_pool_account(self) -> bool:
        return self.provider is not None and SUBSCRIPTION_TOKEN_ENV in self.secrets
```

`agent_worker.accountlease.ACCOUNT_TOKEN_ENV` and
`scheduler.credentials.SUBSCRIPTION_TOKEN_ENV` would then both be derived from
it. The worker's `_lease_account` and the scheduler's `credential_for` would
both ask `profile.runs_on_a_pool_account`, and the parity test could shrink to
the behavioural half.

### What it would break if accepted

* **Nothing stored.** No document carries the name.
* **`ACCOUNT_TOKEN_ENV` is imported by that name** in the worker and its tests.
  Keeping it as an alias of the new constant avoids a rename in the same
  change.
* **The UI's catalogue mirror** (`apps/swarm-ui/src/types.ts`, section 5 of
  `docs/mirrored-values.md`) mirrors `RunnerProfile` fields, not properties, so
  a property adds nothing it has to follow.

### If it is declined

The parity test stays and does its job for these two copies. A third reader,
for example a UI hint that says which profiles a lent account can run, would
have to restate the name again and would need its own parity test.

---

## 23. `models.py`: a task's end has no typed cause, so the outcome ledger classifies `last_error` text

**Status: ACCEPTED — accepted by the owner on 2026-09-25, in the decisions
comment on #185 (item 9: "Contract requests 23 ... and 24 ... are accepted") —
and applied in PR #217.** This edits `apps/common/swarm_common/models.py`,
which is frozen, and is recorded here as such. Recorded 2026-09-25 by the lane
that built `GET /v1/outcomes` (branch `lane/outcomes-api`, #185).

### What was applied

* `EndCause(str, Enum)` in `models.py`, beside `Task`, with the request's ten
  values and ONE MORE, `INPUTS_UNAVAILABLE = "inputs_unavailable"`. It is the
  same comment's item 4: the worker refusing to stage a declared input
  (`agent_worker.errors.InputUnavailable`) is its own class, and a typed cause
  that could not say so would have sent those tasks back to the text. 10 of
  dev's 13 "runner errors" on 2026-09-25 were exactly that.
  `CANCELLED_PARENT` is kept as requested: it is what item 2 (split "after a
  cancel" from "after a failure") needs a writer to say.
* `Task.end_cause: EndCause | None = None`, written by `to_firestore` as its
  string value. None on a success, on every task that has not ended, and on
  every document written before this change.
* Every terminal writer records it beside `completed_at`, deciding it where the
  terminal state is decided:
  * the worker (`control.finish`, `control.fail_retryably`; the lifecycle
    passes TIMEOUT, OUTPUTS_MISSING, INPUTS_UNAVAILABLE, CANNOT_START for a
    78, RUNNER_ERROR, or CANCEL_REQUESTED). `finish` writes the field on EVERY
    terminal state, None included; "runner stopped on SIGTERM" without a
    requested cancel is the one end no value names, and carries None;
  * the reconciler (`ControlStore.repair_task_state`, inside its transaction):
    CANCEL_REQUESTED when the flag picks CANCELLED, else `failed_cause` --
    CANNOT_START for an exit-78 finding, LOST_WORKER for a requeue downgraded
    on spent attempts;
  * the scheduler: `return_to_ready_after_failed_dispatch` (DISPATCH_FAILED, or
    CANCEL_REQUESTED), `cancel` (a REQUIRED keyword: CANCEL_REQUESTED, or
    FAILED_PARENT / CANCELLED_PARENT by `loop._parent_cause` from each
    parent's OWN END -- a CANCELLED parent that is a failure's cascade or was
    swept passes a failure down, and a failure wins -- or, for a parent
    cancelled before the field existed with no flag, None, which the ledger
    splits by the chain) and `cancel_if_not_started` (WORKFLOW_SWEEP);
  * the API's cancel, only when that write ends the task (a pending task): a
    flag on a task holding capacity ends nothing and records nothing.
* `swarm_api.outcomes` reads `end_cause` first and falls back to its text
  classifier only for a task without one. `DERIVE_VERSION` and
  `CLASSIFIER_VERSION` went 1 -> 2, and a stored day is re-derived when EITHER
  differs (the classifier's version had been written and never read).
* **Corrected in review, before release (the review of #217):** the cascade
  split first read only the direct parents' STATES, in both the scheduler and
  the ledger's fallback. The dependency rule is transitive, so every step two
  or more hops below a FAILED one was "after a cancel" nobody made. Both now
  read each parent's own end, and the ledger follows untyped cascades up the
  chain (`outcomes._read_cascade_ancestors`).

### Proved by

`tests/unit/control_plane/test_outcomes_end_cause.py` (the enum, the field, the
classifier, the split, the versions), `tests/unit/control_plane/
test_end_cause_writers.py` (scheduler and API), `tests/unit/worker/
test_end_cause_worker.py` and `tests/unit/worker/test_end_cause_reconciler.py`,
each pushed red before the change (PR #217 names the runs).

The request as it was filed follows, unchanged.

### What is true today

The Timeline's "Why tasks failed" and "Why tasks were cancelled" cards need one
fixed class per ended task. `Task` carries `state` and a free-text
`last_error`, and nothing else about why the task ended. So
`swarm_api.outcomes.classify_failure` and `cancel_cause` infer the class from
text written by four writers the API image does not carry:

* `agent_worker/lifecycle.py` (timeouts, runner errors);
* `agent_worker/expected_outputs.py` (missing outputs);
* `reconciler/detect.py` and `reconciler/repair.py` (could not start, lost
  worker);
* `scheduler/store.py` and `scheduler/loop.py` (dispatch failures, cascade
  cancels, the fail_workflow sweep).

`tests/unit/control_plane/test_outcomes_classifier_parity.py` pins every pattern
to its writer's source, so a reworded message turns CI red. That is still a
restatement of every writer's words, in a fifth place.

One case is not recoverable from the text at all. "an upstream workflow step
did not succeed" is written when a parent is FAILED, DEAD_LETTERED **or
CANCELLED**. So a cancel caused by a person's cancel reads the same as one
caused by a failure.

### The requested change

Add to `models.py`:

```python
class EndCause(str, Enum):
    """Why a task reached its terminal state. Written by the terminal writer."""
    TIMEOUT = "timeout"
    CANNOT_START = "cannot_start"
    LOST_WORKER = "lost_worker"
    OUTPUTS_MISSING = "outputs_missing"
    DISPATCH_FAILED = "dispatch_failed"
    RUNNER_ERROR = "runner_error"
    CANCEL_REQUESTED = "cancel_requested"
    FAILED_PARENT = "failed_parent"
    CANCELLED_PARENT = "cancelled_parent"
    WORKFLOW_SWEEP = "workflow_sweep"
```

Add `end_cause: EndCause | None = None` to `Task`, set by the four terminal
writers beside `completed_at`. `classify_failure` would then read the field
first and fall back to the text only for tasks that ended before it existed.

### What it would break if accepted

* **Nothing stored.** The field is optional, and an old document decodes with
  None.
* **Every terminal writer changes**: the worker's `control.finish`, the
  reconciler's `repair_task_state`, and the scheduler's cancel and
  dispatch-failure paths. They are separate images, so the field would be
  written by some before others during a rollout. The text fallback covers
  that window.
* **`DERIVE_VERSION` in `swarm_api.outcomes` must be bumped**, so that stored
  days are re-derived with the new classes.

### If it is declined

The parity test stays and does its job. The cascade-cancel overclaim stays
unless the ledger reads each cancelled step's parents at derive time, which is
the recommended option on #185's open question.

---

## 24. `profiles.py`: whether a profile's cost is declared rather than measured is named outside the catalogue

**Status: ACCEPTED — accepted by the owner on 2026-09-25, in the decisions
comment on #185 (item 9) — and applied in PR #217.** This edits
`apps/common/swarm_common/profiles.py`, which is frozen, and is recorded here as
such. Recorded 2026-09-25 by the lane that built `GET /v1/outcomes` (branch
`lane/outcomes-api`, #185).

### What was applied

* `RunnerProfile.cost_declared: bool = False`, set True on `mock`, exactly as
  requested.
* `swarm_api.outcomes.DECLARED_COST_PROFILES` is derived from the catalogue;
  the module names no profile. `tests/unit/control_plane/
  test_outcomes_end_cause.py` holds both, and that the source names none.
* NOT served on `/v1/runtimes`, and NOT added to the UI's `RunnerProfile`: the
  UI reads the declared set from `GET /v1/outcomes` (`groups.rows[].declared_cost`
  and `totals.cost.declared.profiles`), so it has no copy to follow. The
  catalogue mirror in `types.ts` is not field-for-field (it carries neither
  `secrets_any_of` nor `supports_checkpoint`), so the "would gain a field to
  follow" below did not arise.

The request as it was filed follows, unchanged.

### What is true today

The Timeline marks cost that a runner **declares**, rather than cost measured
from a provider bill: `mock` reports $0.00 on purpose. That lets a reader tell a
deliberate zero from a real one. The catalogue has no field for it, so
`swarm_api.outcomes.DECLARED_COST_PROFILES = frozenset({"mock"})` names it, and
`test_outcomes_classifier_parity.py` holds every entry to a real profile.

### The requested change

Add `cost_declared: bool = False` to `RunnerProfile`, and set it True on
`mock`. `DECLARED_COST_PROFILES` would then be derived from the catalogue.

### What it would break if accepted

* **Nothing stored.** No document carries the name.
* **The UI's catalogue mirror** (`apps/swarm-ui/src/types.ts`, section 5 of
  `docs/mirrored-values.md`) mirrors `RunnerProfile` field for field. The
  parity check would require the new field there as well.

### If it is declined

The single named set stays, held to the catalogue by its test.

---

## 25. `profiles.py`: a runner profile cannot declare the inputs a caller may send it, so the bridge names the mock's by profile

**Status: ACCEPTED by the owner on 2026-09-25 and applied the same day** (branch
`lane/mock-inputs-contract`). This edits `apps/common/swarm_common/`, which is
frozen, and is recorded here as such. The owner's comment on #142
([issuecomment-5840939054](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/142#issuecomment-5840939054)):
"contract request 25 is accepted: `RunnerProfile.inputs` goes in the frozen
catalogue and the API enforces it for every caller, not only the bridge." What
was applied, and the one place it departs from the text below, is under
*Applied* at the end of this entry. **That departure is an amendment the owner
has NOT approved**: the field is typed `Mapping | None`, not the `Mapping` the
request asked for, and for the two profiles left `None` the API does not
enforce a declaration for every caller. It is recorded under *Amendment
awaiting the owner* below, and the decision is
[#218](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/218). A
second change awaits the owner's confirmation too: the bounded park is counted
by the task's `attempt_count`, not by the state-file counter the acceptance
named, because a failed checkpoint lost that one. It is under *Fixed after the
second review of #213*, at the end.

Recorded 2026-09-25 by the plugin lane that delivered #142 (branch
`lane/plugin-cli-0.5.2`). Numbered 25 because #196 (the outcomes API) takes 23
and 24.

### What is true today

A runner reads its task's `input`. The mock reads `sleep_seconds`, `steps`,
`fail`, `artifact_text` and more (`agent_worker/runners/mock.py`); the CLI
runners read `input.prompt` and `input.model` (`runners/cliagent.py`). The
frozen catalogue says nothing about which keys a profile reads, or which of
them a caller may set.

#142 asked for the plugin to send the mock's test knobs, so a mock step can be
caught RUNNING and cancelled, or fail or park on purpose (the park is withheld;
see below). Invariant 10 is not
at stake -- these are data, never an image, a command, a resource spec or a
backend -- but an input means something only to the runner that reads it:
`input.model` would select the model a `claude-code` agent runs, which is the
contract change `tests/unit/mcp/test_model_flag_is_attribution_only.py` exists
to stop. So the gate has to be per profile.

With nothing in the catalogue to read, the bridge gates on the profile NAME,
in one place: `DECLARED_INPUTS` in `apps/swarm-mcp/swarm_mcp/profiles.py`,
which declares eight keys for `mock` and nothing for any other profile.
`tests/unit/mcp/test_runner_inputs.py` holds every name there to
`RUNNER_PROFILES` and every key to a `payload` read in the runner's source,
which it finds from the profile's own `runner_argv`.

Two of #142's asks are **not** declared, and why is what a catalogue field
would have to carry too. `quota_exhausted` (and `retry_after_seconds`, which
only shapes it) would let a caller start something they cannot stop: the mock
raises its rate limit on every attempt, with no count, and a park does not
spend an attempt, so the task parks and resumes until it is cancelled. A
bounded park needs a counter in the mock (a `quota_exhausted_times`, kept in
its state file the way `credential_revoked_times` keeps its own), which is a
worker change. And `exit_code` is declared with values refused inside its
range -- 0, 77, 78 and 143 -- because the worker reads those as a success, a
rate limit, a refused credential and a cancellation; a failure on purpose that
exits with one is not a failure. The API does not check
input keys at all -- `validate_input_size` bounds the size -- so a caller that
does not go through the bridge can still send anything.

### The requested change

Add to `profiles.py`:

```python
@dataclass(frozen=True)
class RunnerInput:
    kind: str                     # number | integer | boolean | string | filename
    minimum: float | None = None
    maximum: float | None = None
    means: str = ""
    #: Values inside the bounds that are refused anyway, each with what the
    #: platform would read it as (the mock's exit codes 77, 78 and 143).
    refused: tuple[tuple[Any, str], ...] = ()


@dataclass(frozen=True)
class RunnerProfile:
    ...
    #: The keys of `input`, besides `prompt`, a caller may set for this
    #: profile. Empty for every profile that takes only a prompt.
    inputs: Mapping[str, RunnerInput] = field(default_factory=dict)
```

and give `mock` the eight entries the bridge declares today. The bridge would
then read `profile.inputs` and delete its table, and the API could refuse an
undeclared key at submission, for every caller, with the same rule.

### What it would break if accepted

* **Nothing stored.** No document carries the field; tasks keep their `input`.
* **The UI's catalogue mirror** (`apps/swarm-ui/src/types.ts`, section 5 of
  `docs/mirrored-values.md`) would gain a field to follow, or state that it
  does not.
* **An API that refuses undeclared keys** would refuse a caller that sends one
  today and has it ignored. That is the point, and it is a behaviour change to
  announce, not to slip in.

### If it is declined

The bridge's table stays, gated on the profile name, with its two parity tests.
Anything that is not the bridge -- the web UI's New Workflow screen, a script
posting to `/v1/tasks` -- keeps sending whatever `input` it likes, and a new
profile that should take inputs takes none from the plugin until someone edits
the table.

### Applied, 2026-09-25

Accepted by the owner on #142 (the comment quoted under *Status*), together
with "a bounded park for the mock (a `quota_exhausted_times` counter in its
state file, like `credential_revoked_times`), after which `quota_exhausted` and
a bounded `retry_after_seconds` join the declared inputs."

**In `swarm_common/profiles.py`**, as requested: `RunnerInput` (kind, bounds,
`means`, `refused`) and `RunnerProfile.inputs`. Beside them, so that the rule
has one home as well as the data: `INPUT_KINDS`, `InputRefused` (carrying the
key, every refused key and the bound) and `check_inputs(profile, raw)`, which
swarm-api and the bridge both call. `RunnerInput.check` refuses NaN and
Infinity, which `json.loads` reads and which every bound comparison lets
through. `RunnerProfile.inputs` is frozen into a read-only mapping and excluded
from the hash; `prompt` cannot be declared, because every profile takes it.

**The mock declares ten keys**: the eight the bridge declared, plus
`quota_exhausted` (boolean) and `retry_after_seconds` (integer 1..3600).
`exit_code` stays 1..255 except 77, 78 and 143. Still not declared: `spend`,
`provider`, `credential_revoked_times`, `credential_detail`, `quota_detail`,
`reset_at`. `agent_worker/runners/mock.py` parks one attempt: as first
applied, it recorded `quota_exhausted_times` and the parking attempt's id in
`mock_state.json` before it raised, the park's checkpoint carried the file
forward, and the next attempt ran. A retry in place is the same attempt and is
refused again. The count has since moved to the task's `attempt_count`,
because a failed checkpoint lost it; see *Fixed after the second review*
below.

**`claude-code` and `codex` declare nothing**, so `input.model` is refused from
every caller, which is the attribution-only rule
`test_model_flag_is_attribution_only.py` holds for the bridge. (Since #226,
2026-09-26, the CLI runners do not read `input.model` either: the model is the
profile's Job's `MODEL`, set once in Terraform, and the worker drops a stored
`input.model` with a WARNING, which covers `browser` and `generic` too.)

**The one departure: `browser` and `generic` are `inputs=None`, NOT DECLARED
YET, and the API bounds them by size alone, as it bounded every profile
before.** The text above says "Empty for every profile that takes only a
prompt", and neither does: the browser runner refuses an input with neither
`url` nor `actions`, and the generic runner cannot start without `command`,
the name of an entry in its own catalogue. Declaring nothing for them would
have refused every task they run, the smoke suite's GKE row included. The
"What it would break" section above assumed an undeclared key is "ignored"
today; for these two runners it is the work. So which keys they declare, with
which bounds, is open, and it is the owner's to decide
([#218](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/218)):

* `browser` reads `url`, `actions` (a list of objects, which `INPUT_KINDS` has
  no kind for), `timeout_ms`, `launch_timeout_ms`, `viewport_width`,
  `viewport_height`, `user_agent`, `extract_text` and `screenshot`;
* `generic` reads `command`, `paths` (a list), `target`, `working_directory`
  and the four limits in `runners/limits.py`. A declared input named `command`
  would read as invariant 10 relaxed although it names a catalogue entry, not
  an argv.

**swarm-api** refuses an undeclared key, or a declared key out of its bounds,
with 422 `invalid_input` at `POST /v1/tasks`, the batch and every workflow
step, before anything is created (`validation.validate_runner_input`). The
bridge's `DECLARED_INPUTS` and `InputSpec` are deleted; it reads the catalogue.

**Downstream, changed in the same PR because each became a restatement the
API now enforces:** the operational scripts sent `message`, `index` and
`run_id`, which no runner read, and now send the prompt; the Submit form
offered `model` to the CLI profiles and `timeout_seconds` to every profile,
and now offers neither to a declared profile. Section 13 of
`scripts/lib/check-contract-parity.sh` holds both to the declaration. The UI's
catalogue mirror (`apps/swarm-ui/src/types.ts`) does not follow the field:
nothing serves it to the browser yet, which the Submit form's comment records
as a request.

### Amendment awaiting the owner: `inputs` may be `None`

**Not approved.** Recorded 2026-09-25 after the review of #213, which found
it applied without being named as a change to what the owner accepted.

| | as accepted | as applied |
|---|---|---|
| the type | `inputs: Mapping[str, RunnerInput] = field(default_factory=dict)` | `inputs: Mapping[str, RunnerInput] \| None = field(default_factory=dict, hash=False)` |
| what it can mean | a declaration: some keys, or none ("Empty for every profile that takes only a prompt") | three things: some keys; none, so the prompt only (`claude-code`, `codex`); or **not declared yet** (`None`: `browser`, `generic`) |
| what the API enforces | the declaration, for every caller | the declaration, for every caller, where there is one; for `None`, the input's size alone |

**What `None` does, and where that is decided.** `swarm_common.profiles.
check_inputs(profile, raw)` is the one place: for `None` it hands `raw` back
unchecked, and `validate_input_size`, which every submission runs first,
bounds it. `validate_runner_input` asks that function for every profile and
keeps no branch of its own. Before the review it did: it returned early for
`None` while `check_inputs` refused every key, so the one rule and the API
answered the same question oppositely.
`tests/unit/control_plane/test_runner_inputs_by_declaration.py` now holds the
API's answer equal to the rule's for every available profile.

**The bridge sends a `None` profile nothing.** That is the bridge's own send
policy, not a copy of the rule: it sends a key only when a declaration names
it, and it types `--input` by the declared kind. Letting it send a browser
task's `actions` is #218's third question.

**What would retire the amendment**, whichever way #218 goes: declare
`browser` and `generic` (which needs a list kind, and a decision on a key
named `command`), and put the field back to `Mapping` with no `None`; or
approve `None` as it stands, and record that approval here with the owner's
comment.

### Fixed after the second review of #213, 2026-09-25

Four engineering defects the review found, fixed in the same PR. None of them
decides what `browser` or `generic` declare; that is still #218.

**A declared number has both bounds, inside the range Firestore stores.**
`steps`, `sleep_seconds` and `cpu_burn_seconds` were declared with a floor
only, and Python reads a JSON integer at any length, so `steps: 10**30` passed
every check and failed when the task was written: a 500 at the store, not a
422 at the door. `RunnerInput.__post_init__` now refuses a `number` or
`integer` declared without both `minimum` and `maximum`, and an `integer`
whose bounds leave the signed 64-bit range. That range is two new names in
`profiles.py`, `INT64_MIN` and `INT64_MAX`, which swarm-api imports rather
than restates. The field types are unchanged from what was accepted; what
changed is which values of them the catalogue will hold. The mock's ceilings:
`sleep_seconds` and `cpu_burn_seconds` one hour (past the mock's 600 s timeout
on purpose, so the timeout path can be exercised), `steps` a thousand (each
step is a file every later checkpoint carries). A refusal repeats at most
forty characters of the value it refuses. swarm-api's `validate_storable`
also refuses, with 422 and the path, an integer anywhere in an input or a
task's metadata that Firestore could not encode, which is the only number
check a profile not declared yet gets.

**Where the bounded park's count lives. A CHANGE TO WHAT THE OWNER WROTE, NOT
YET CONFIRMED BY THE OWNER.** The acceptance above reads "a bounded park for
the mock (a `quota_exhausted_times` counter in its state file, like
`credential_revoked_times`)". Applied that way, the count reached the next
attempt only through the park's checkpoint, and `Worker._checkpoint` logs a
failed upload and parks anyway, as it must for a real provider. So a park
whose checkpoint failed was followed by another, for as long as uploads kept
failing: the bound held only while checkpoints did. The count is now the
task's `attempt_count`, which admission increments in the lease's own
transaction. The lifecycle writes it into every runner's input beside
`attempt_id`, assigned rather than defaulted so no caller can set it, and the
mock parks while it is 1: the task's first attempt, and no other. A mock
started without it refuses to simulate the park and fails, which spends an
attempt that `max_attempts` bounds. The bound itself, one park, is the
owner's and is unchanged; so is the declaration's own wording, "park the first
attempt". The other option the review named, failing retryably when the
park's checkpoint fails, was not taken: it would change the park path for
every runner, and a real provider that is refusing is still refusing on the
retry. `tests/unit/worker/test_mock_bounded_park.py::test_the_bound_holds_when_the_parks_checkpoint_fails`
makes every checkpoint of the parking attempt fail and requires the next
attempt to finish.

**The prose copies of the bounds are generated.** `docs/workflows.md` and
`plugin/README.md` each carry a table of the mock's inputs between
`runner-inputs:mock` markers, rendered from `RunnerInput.describe()` and
`means`. `tests/unit/mcp/test_runner_input_prose.py` holds both equal to the
catalogue, fails when a sentence in either, or in the delegate skill, states a
number about a declared numeric key outside the table, and requires every
example input to pass `check_inputs`.

**The Submit form no longer offers `browser` a key its runner never reads.**
`timeout_seconds` is read through `runners/limits.py` by generic and the CLI
runners; the browser runner starts no child through it.
`tests/unit/control_plane/test_submit_offers_only_what_runners_read.py` holds
every offer to a profile not declared yet to a `payload` read in its runner.

---

## 26. `models.py`: the attempt's CPU figures carry no time and their limit no source

**Status: ACCEPTED — accepted by the owner on #184, 2026-09-26, and applied in
PR #229.** The decision, in the owner's words
([#184, 2026-09-26](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/184#issuecomment-5841716442)):
"Contract request #26 is accepted: typed `cpu_measured_at` and
`cpu_limit_source` on Attempt." Recorded 2026-09-25 by the #184 follow-up
lane (PR #210), which applied request #15. What was applied is under
[Applied](#applied-2026-09-26-pr-229) at the end of this entry.

### What is true today

Request #15's four fields are on `Attempt`, and the worker rewrites them with
each periodic reading while a runner runs. So on a running attempt they are
a live reading. The interim path they replaced carried two more facts, and
the accepted fields carry neither:

* **When the figures were measured.** The HEARTBEAT reading had its event's
  `at`, and `include=usage` served `measured_at` and `age_seconds`. Details
  drew `latest heartbeat 20s ago`, aged on the server's clock (the #187
  review's fix). Now it can only say `live reading · age not recorded`. A
  worker that has stopped writing looks exactly like one that wrote a second
  ago. The drawer's liveness badge is the only thing that says otherwise, and
  it reads a different document.
* **Where the limit came from.** `cpu_limit_source` said `cgroup` (the
  container's `cpu.max`) or `resource_class` (the catalogue cpu of the class
  it was sized with). Details now names the class when the limit equals that
  class's cpu, and otherwise says `reported limit`. Nobody has read what Cloud
  Run's `cpu.max` holds (#188, "not verified"), so this is not academic.

### The requested change

Add to `Attempt`, beside the four:

```python
cpu_measured_at: datetime | None = None   # when the four were last written
cpu_limit_source: str | None = None       # "cgroup" | "resource_class"
```

Both optional and None by default, so nothing migrates. `cpu_measured_at` is
the worker's clock at the reading, and the API would serve an age computed
against its own `read_at`, as #188 did.

### What it would break if accepted

Nothing stored. `control.record_cpu_usage` would write two more keys,
`codec` would read and serve them, `AttemptRow` would declare them, and
Details would draw the age again. A string for the source is a vocabulary
that would want a home (compare request #20).

### If it is declined

Details keeps `age not recorded` and `reported limit`. A stalled worker's CPU
figures are not dated, and a limit is never attributed to the kernel.

### Applied, 2026-09-26 (PR #229)

* **The contract.** `Attempt` gains `cpu_measured_at: datetime | None = None`
  and `cpu_limit_source: str | None = None`, at the end of the class, after
  request #15's four. Nothing else in `apps/common/swarm_common` changed.
* **The worker** writes them with the four. `metrics.attempt_cpu_fields`
  takes the limit's source (`CPU_LIMIT_SOURCES`: `cgroup` or
  `resource_class`) and puts it beside a limit only. `control.record_cpu_usage`
  stamps `cpu_measured_at` with the worker's clock on every write that carries
  a figure, and never writes a time, a limit or a source with no figure beside
  it. The periodic reading (every fifth heartbeat) is now written even when the
  figures did not move, because its time is what says the worker is still
  reading; the reap and exit writes still skip an unchanged reading.
* **The API** decodes and serves both on every attempt row, and serves
  `cpu_reading_age_seconds`, the reading's age against the route's own
  `read_at` (both attempt routes now carry `read_at`), clamped at zero. It is
  declared computed in `test_api_contract_shapes.py`.
* **The UI** restates the two fields and the age on `AttemptRow`, optional.
  Details' CPU strip says `live reading · 20s ago` (and `last written · 3m
  ago` for an attempt that ended with no recorded finish), and the ceiling's
  source is `cgroup limit`, the class's name, or `class limit`, from the typed
  source. `test_ui_api_field_contract.py` and
  `test_artifacts_tab_field_contract.py` hold the fields both ways.
* **Attempts from before the change** carry neither field. They keep the
  legacy words, `age not recorded` and `reported limit`, and the legacy
  heartbeat reader stays, as the owner decided on #184.
* The source is a bare string, as this entry said it would be. Its vocabulary
  is stated once in the worker (`metrics.CPU_LIMIT_SOURCES`) and read by the
  UI's `limitSource`; compare request #20.


## 27. `identity.py`: `_slug`'s docstring still sizes tenant ids for the `swarm-t-` prefix that no longer exists

**Status: ACCEPTED, accepted by the owner 2026-09-28 on #245 and applied in
PR #245.** Recorded 2026-09-28 by the #176 lane.

### What is true today

`apps/common/swarm_common/identity.py`, `_slug`'s docstring, LENGTH bullet:
"A long group name yields an id no GCP service account can be named for,
because `swarm-t-<id>` must fit in 30 characters." The code under it is right:
`_MAX_TENANT_ID` is computed from `_GSA_PREFIX = "swarm-agent-worker-"`, and the
comment on `_GSA_PREFIX` itself says the `swarm-t-` prefix "no longer exists".
So the frozen module contradicts itself about which identity the cap is for.

That contradiction is how #176 survived: the quota broker's comment said
register-tenant.sh writes `swarm-t-<tenant>`, and accepted it as a worker
identity, long after nothing created one. The broker no longer accepts it; this
docstring is the one live statement left in the repository that `swarm-t-<id>`
is the name a tenant's worker must fit.

### The requested change

Docstring only: `swarm-t-<id>` becomes `swarm-agent-worker-<id>` in that
sentence. No code, no value, no type.

### What it would break if accepted

Nothing. No code reads a docstring, and `check-contract-parity.sh` compares
`_GSA_PREFIX`, not prose.

### If it is declined

The frozen module keeps telling its reader that tenant ids are sized for an
identity nothing creates, and the next restatement copied from that sentence
reintroduces #176.

### Applied, 2026-09-28 (PR #245)

`_slug`'s LENGTH docstring bullet now reads `swarm-agent-worker-<id>` in place
of `swarm-t-<id>`, matching `_GSA_PREFIX` and the comment already on it.


## 28. `profiles.py`: claude-code and codex declare an `issue` runner input

**Status: ACCEPTED, accepted by the owner 2026-09-28** (recorded on #265), and
applied by the #265 change the same day (see "Applied" below). Recorded
2026-09-28 from #265. If another branch has taken 28 by the time this merges,
renumber this one.

### What is true today

A runner profile declares the inputs a caller may send (#213, contract
request 25). `claude-code` and `codex` declare none, so a caller can only put
an issue's text into the prompt itself. Every brief in the 2026-09-27/28 wave
restated its issue by hand, and #247's brief lost the two screens its issue
named as the reproduction.

### The requested change

`claude-code` and `codex` declare one input, `issue`: a positive integer
naming an issue in the step's own `repo`. No other key is added and no type
changes. The worker fetches that issue's title, body and comments read-only
with the tenant's forge credential, writes them to the workspace as
`issue.md`, and names the file in the prompt. The fetch is the worker's; the
declared input is only the number.

### What it would break if accepted

Nothing that exists: `mock` is the only profile that declares inputs today
(#213), and neither `claude-code` nor `codex` does, so every current
submission stays valid. The API's declared-inputs check (#213) starts
accepting `issue` for these two profiles and still refuses everything else.

### Invariants

- **Invariant 10.** The number is data, and the issue text the worker fetches is
  data for the agent, never an image, a command, a resource spec or a backend
  parameter.
- **#219.** The forge token never reaches the workspace or the agent.
- **Invariant 9.** The fetch uses the step's own tenant's credential against the
  step's own repository.

### Applied, 2026-09-28 (#265)

Exactly the requested change, and nothing else in `apps/common/swarm_common/`:
`profiles.py` gives `claude-code` and `codex` one declared input, `issue`, a
`RunnerInput("integer", minimum=1, maximum=999_999)`. No other key, type or
profile changed.

- **The ceiling.** The request says "a positive integer" and a declared integer
  needs both bounds (#213). 999999 is the largest bound `RunnerInput.describe()`
  prints exactly: it formats six significant digits, so `2**31 - 1` would be
  served as "2.14748e+09", a bound the check does not apply.
- **The API** (`validation.validate_runner_input`) accepts `issue` for these
  two profiles through the declaration, as it does every declared key, and
  refuses it with 422 `invalid_input` on a submission with no
  `repository_url`: the issue is the task's repository's, and there would be
  nothing to fetch it from.
- **The worker** (`agent_worker/issue.py`) fetches the issue and its comments
  read-only after the credentials step, writes `work/issue.md`, and fails the
  attempt `INPUTS_UNAVAILABLE` before the agent starts when it cannot. The
  runner names the file in the prompt (`runners/cliagent.py`).
- **Invariant 10.** The number is data, and so is the text: nothing in the
  worker reads the issue for anything it does.
- **#219.** The token is read by `Worker._git_token`, the clone's path, and
  goes only into the request's `Authorization` header; a redirect to another
  host is refused rather than followed with it.
- **Invariant 9.** The fetch is against the task's own repository with its own
  tenant's credential.

## 29. `models.py`: `EndCause` gains `PUBLISH_REFUSED`

**Status: APPLIED 2026-10-02** (accepted by the owner 2026-09-29, recorded
on #259; applied by functionality wave 3, lane B46, exactly as requested
below). Recorded 2026-09-29 from #259.

**As applied.** `EndCause.PUBLISH_REFUSED = "publish_refused"`, appended
after `VERDICT_FAILED` so no existing value moved. The worker writes it from
both call sites, `_fail_for_final_tree_leak` and `_fail_for_refused_title`;
both stay retryable, so the cause is the task's once its attempts are spent.
`swarm_api/outcomes.py` gains the failure class `publish_refused` ("publish
refused"), and `DERIVE_VERSION` moves to 5 so stored days are derived again.
The UI's `FailureClassKey`, its ledger fixture and a new `END_CAUSES` mirror
in `types.ts` gain it too. Section 5 of `scripts/lib/check-contract-parity.sh`
now holds that mirror to the enum.

**Not covered by this cause: the forge refusing a pull request.** The
definition below is "the worker refused to publish", and a pull request the
forge refuses (GitHub's "No commits between") is not the worker refusing.
That case, and a pull-request step that changed nothing, are written as
`OUTPUTS_MISSING` meanwhile. Their own cause is request 46.

### What is true today

The worker refuses to publish in two ways that fail the attempt retryably
(#259): the branch it would push adds a credential ("the final tree adds a
credential in <file>; remove it"), or the agent's `pr-title.txt` is present
and unusable (a task id, or attribution; since 2026-09-29 a mention is
neutralised with a zero-width joiner, never refused). Neither has an end cause
of its own. `EndCause` is in the frozen `swarm_common`, so the worker writes
`RUNNER_ERROR` for the first and `OUTPUTS_MISSING` for the second
(`agent_worker.lifecycle._fail_for_final_tree_leak`,
`_fail_for_refused_title`). The outcome ledger then counts a platform refusal
as the runner's error or as a missing output, and neither is what happened.

### The requested change

One new member, `EndCause.PUBLISH_REFUSED = "publish_refused"`: the worker
refused to publish -- a credential in the final tree, or an unusable
`pr-title.txt`. The worker writes it from both call sites above. swarm-api's
outcome classes (`swarm_api/outcomes.py`) and the UI's fixture gain the
class with it.

### What it would break if accepted

Nothing that exists: a new enum value. A reader that does not know it falls
back to its text classifier, as for any task written before `end_cause`
existed. The outcome ledger's cause-to-class map and the UI's class list
must add it in the same change, or those tasks read as unclassified.

### Invariants

- **Secrets.** The cause names the refusal, never the value; the attempt's
  error names the file, never its content.
- **Invariant 1.** A refused attempt that goes back to READY holds no
  capacity, exactly as any retryable failure.
---

## 30. `identity.py`: a tenant may list service accounts that resolve to it by exact email

**Status: ACCEPTED 2026-09-29 by the owner after three security reviews.**
Decided in principle by the owner on 2026-09-28 for #273 (the red-CI fixer);
this entry went through three rounds of security re-review on 2026-09-29 --
the WIF/deployer-IAM correction, the default-deny scope and IAP-subject
correction, and the required-keyword `submitted_by` correction, each recorded
in its own addendum below -- before the owner accepted it as it now reads.
Nothing here is applied YET: `identity.py`, `auth.py`, `settings.py`,
`variables.tf` and `locals.tf` are unchanged by the pull request that adds
this entry; implementation is tracked separately (`part of #273`). Recorded
2026-09-29. Numbered 30 because 27 and 29 are taken on open branches (29
twice); if another branch has taken 30 by the time this merges, renumber this
one.

**2026-09-29, after a security review: accept with changes.** The owner made
four decisions, folded into this entry below and marked where they land:
the account stays in `saga-agents-staging` as an accepted risk (**Who can
mint the account's token**, below); a listed account's rights are narrowed
to continuation-only (**5. Scope: continuation-only rights**); the deployer
loses `roles/iam.workloadIdentityPoolAdmin` outright (#334) and, since an IAM
condition cannot scope `roles/iam.serviceAccountAdmin` at all (IAM resources
expose no `resource.name` to a condition), instead loses that role
project-wide in favour of project-wide `roles/iam.serviceAccountCreator` plus
a per-account `serviceAccountAdmin` grant on each account `terraform/infra`
manages, excluding `swarm-ci-fix` and `swarm-tf-deployer` (end of **Who can
mint the account's token**, corrected 2026-09-29 after PR #334); and the
reviewer's minors are folded into the diffs below. Status stays
**PROPOSED** — this is
what re-review checks against, not an acceptance.

**2026-09-29, second re-review pass: "NOT YET," three more corrections.**
(1) The per-route scope list in item 5 was incomplete -- it missed
`routes/accounts.py` (8 routes, some writing), `POST
/v1/tenants/me/credentials`, `GET /v1/attempts`, `GET /v1/outcomes`, and the
checkpoint-content routes, which took no `AuthContext` at all and so had no
seam for the old design to attach to. Item 5 is now a DEFAULT-DENY check
inside `current_auth` against an explicit `CONTINUATION_ROUTES` allow-list
(the `POOL_ADMIN_ROUTES` pattern), and the `submitted_by` filter moved into
`Store.get_task`/`Store.list_tasks` themselves rather than being restated at
each read route; `/v1/stats`, `/v1/capacity`, `/v1/providers` and
`/v1/tenants/me` are now explicitly classified out, `/v1/resource-classes`
and `/v1/runtimes` explicitly in. (2) The `uid` pin (item 3/4) compared
`principal.subject` against Terraform's bare unique id, but IAP -- which
`ci-fix.yml` actually calls through -- prefixes `sub` with
`"accounts.google.com:"`; item 4 now strips that prefix once, for both
paths, before `Principal` is built, and notes that `email_verified`
defaults to `True` on the IAP path regardless, so decision 4's "explicit
True" check is a bearer-path protection only. (3) The stolen-token reach
(**Isolation analysis**, "What an attacker who steals its token gets") is
restated: `continues_task` carries an ARBITRARY, caller-chosen prompt, not a
constrained fix, and the account can read back what that prompt produced --
an exfiltration path, not merely an unwanted commit -- repeatable without
limit, because the owner declined a continuation cap (decision 3,
2026-09-29). Recorded as an accepted reach, not a mitigated one.

**2026-09-29, round 3: "NOT YET" on one mechanical point, folded in without
another owner round.** The `submitted_by` filter (item 5) was still OPT-IN:
`get_task`, `list_tasks`, `get_workflow` and `list_workflows` defaulted
`submitted_by=None`, so a route that reaches a task through a layer round 2
did not personally thread the parameter through -- the artifact service,
`ctx.inspection`, the transcript or answer service, or one not yet written --
would keep compiling and keep serving, silently unfiltered. `submitted_by` is
now a REQUIRED keyword with no default on all four store methods: a missed
call site is a `TypeError` at the call and a type-check error before that,
not a silent pass-through. `test_continuation_scope_is_narrow.py` is extended
to drive every task/workflow-scoped route in `CONTINUATION_ROUTES` at a task
another `eng` member submitted and assert 404, so a route added to the
allow-list later without its filter wired through fails on the day it is
added.

**2026-09-29, implementation correction: the "is not a task in your tenant"
wording throughout item 5 and its tests was wrong.** Checked directly against
`apps/swarm-api/swarm_api/store.py` on `main` while implementing this entry
(`part of #273`): `Store.get_task` today raises `NotFound(f"task {task_id!r}
not found")` for BOTH a genuinely missing task and another tenant's task --
never "is not a task in your tenant". That phrase belongs to a DIFFERENT code
path: #273's own `resolve_continuation` wraps its `continues_task` lookup in
a `DispatchOptionError` carrying that exact sentence, but it never touches
`Store.get_task`'s own message. Every place below that quoted "is not a task
in your tenant" as `Store.get_task`'s (and therefore the new `submitted_by`
filter's) refusal text has been corrected to the words the store actually
uses, "task 'X' not found" -- the invariant this entry cares about (SAME
words for "not yours" as for "not found" and "cross-tenant", so the filter is
not a new enumeration oracle) is unaffected; only the literal quoted string
was wrong. Every place describing #273's own `continues_task` message is
unchanged and was already correct.

### What is true today

`.github/workflows/ci-fix.yml` (#273) federates as
`swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com` and submits a
one-step `claude-code` workflow with `continues_task` set to the red swarm
task. `resolve_continuation` (`apps/swarm-api/swarm_api/continuation.py` on
#273) looks the task up with `store.get_task(tenant_id, requested)` and answers
"is not a task in your tenant" for another tenant's task exactly as for a
missing one. The `swarm/` pull requests belong to `eng`, which resolves from
the Google group `eng@saga.xyz` (`terraform/environments/dev/dev.tfvars`,
`tenants.eng`, `directory_group = true`).

So the fixer's account has to resolve to `eng`, and today there are exactly
two ways a caller does:

- **Group membership.** `Authenticator._from_claims` asks Cloud Identity
  `groups_for(email, tenant_groups)`, puts the answers in `Principal.groups`,
  and the frozen `resolve_tenant` returns the first registered group the
  caller is in. Adding the account to `eng@saga.xyz` would work, and the owner
  rejected it: that group is a company group, and every grant it holds
  anywhere in saga.xyz -- Drive, other GCP projects, other applications that
  authorise by it -- would go to a bot whose only job is to comment on pull
  requests and submit fix steps.
- **Being the tenant's principal.** A `kind = "user"` tenant whose principal is
  the account's own email (as `u-sw-c90291` is for `swarm-verify`). That is a
  tenant of its own, not `eng`, so it owns none of the tasks it must continue.

Without either, the account is also refused before tenant resolution: its
domain is `saga-agents-staging.iam.gserviceaccount.com`, not `saga.xyz`, so
`assert_allowed_domain` answers 403 unless it is in `ALLOWED_USERS`. And if it
were admitted through `ALLOWED_USERS`, `groups_for` would ask Cloud Identity
about a service-account address on every request, a lookup that can only
answer "no" and whose failure is fatal by design (503).

### The requested change

**1. Terraform: one new optional field per tenant.** In
`terraform/infra/variables.tf`, `var.tenants` gains

```hcl
    # Service accounts that resolve to THIS tenant besides its principal, by an
    # exact match on the email the caller's verified token carries. For a bot
    # that must act as the tenant without joining the tenant's group, which
    # would hand it every grant the group holds across the company (contract
    # request 30). Bare emails of user-managed accounts in this project; never
    # a human, never an admin, never under two tenants -- see the validations.
    service_accounts = optional(list(string), [])
```

and `dev.tfvars` would set, once #273's account exists:

```hcl
  eng = {
    ...
    service_accounts = ["swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com"]
  }
```

**2. How it reaches swarm-api.** `terraform/infra/locals.tf` renders one new
environment variable on swarm-api beside `TENANT_GROUPS`. **Revised from the
first draft (reviewer minor): a JSON list of objects, not a JSON object keyed
by email**, because Python's `json.loads` on an object keyed by email would
silently keep the LAST of two duplicate keys rather than refuse them -- fine
for what Terraform renders today (its own for-expression already errors on a
duplicate key at plan time), not fine for `TENANT_SERVICE_ACCOUNTS` set by
hand, which is exactly the case `settings.py`'s own startup check exists for.
A list carries duplicates through unchanged, so the Python-side check
(below) is the one thing that can refuse them, not an accident of which
representation the object literal happens to pick:

```hcl
locals {
  # Every listed service account across every tenant, lower-cased and deduped
  # for the data source below -- a `for_each` key must be unique even though
  # the SAME email under two tenants is refused by validation, not by this.
  listed_service_accounts = toset(flatten([
    for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]
  ]))
}

# The account's unique id (`sub` on the token it presents), pinned beside its
# email so a service account deleted and recreated under the same address --
# GCP lets the account id be reused -- does not inherit the old one's tenant.
# Reviewer minor.
data "google_service_account" "listed" {
  for_each   = local.listed_service_accounts
  project    = var.project_id
  account_id = split("@", each.value)[0]
}

      # Every listed service account -> its tenant's kind and principal, and
      # its own unique id. A list, not an object: see above.
      TENANT_SERVICE_ACCOUNTS = jsonencode(flatten([
        for t, v in var.tenants : [
          for sa in v.service_accounts : {
            email     = lower(sa)
            kind      = v.kind
            principal = lower(v.principal)
            uid       = data.google_service_account.listed[lower(sa)].unique_id
          }
        ]
      ]))
```

JSON rather than `_csv`, because each entry carries four values now and a
delimiter scheme invented for it would be a second format to get wrong.
`directory_group` is deliberately not consulted: a listed account never needs
a directory lookup, so it resolves to a `kind = "group"` tenant whose group
cannot be read (as `smoke`'s cannot) exactly as to one whose group can.

`apps/swarm-api/swarm_api/settings.py` gains

```python
    #: Service accounts a tenant lists besides its principal (contract request
    #: 30), from TENANT_SERVICE_ACCOUNTS. Refused at startup, not at request
    #: time, when an entry is not a user-managed service account in THIS
    #: project, is listed more than once, sits under two tenants, is missing
    #: its unique id, or is also in admin_users, admin_pool_users or
    #: secret_admin_principals: a revision that would resolve one wrongly must
    #: not start serving.
    tenant_service_accounts: tuple[TenantMember, ...] = ()
```

parsed by a `_tenant_members("TENANT_SERVICE_ACCOUNTS")` helper that
`json.loads` the value as a **list** (empty or absent is `()`), and for each
entry: lower-cases `email`, `kind` and `principal`; requires `kind` in
`{"group", "user"}`, a non-empty `principal` and a non-empty `uid`; requires
`email` to match `identity.SERVICE_ACCOUNT_EMAIL` **and** to end with
`@{self.project_id}.iam.gserviceaccount.com` -- the second check is what a
bare regex on the shape cannot do, and is why a Google-managed service agent
(`service-<number>@gcp-sa-<api>.iam.gserviceaccount.com`) can never match: its
domain never ends with this project's id, however the regex is written
(reviewer minor). It then **refuses a duplicate email itself**, by building
the tuple through a plain loop that checks membership before appending rather
than relying on `dict`/`json` key semantics to catch it (the other half of the
reviewer minor above): two entries for the same email raise `ValueError`
whether or not they agree on tenant. It raises `ValueError` on any of the
refusals above, including the ones already caught at `terraform plan` --
belt and suspenders for a hand-set environment variable Terraform never saw.
A startup `ValueError` fails the new Cloud Run revision's readiness, so the
previous revision keeps serving.

**3. How `resolve_tenant` uses it.** The frozen change is the diff below:
a `TenantMember` type, a `SERVICE_ACCOUNT_EMAIL` expression, a
`tenant_member_for()` matcher, and a third, defaulted parameter on
`resolve_tenant`. The match is:

- **exact and case-insensitive on email, exact on the account's unique id**:
  `email.strip().lower() == member.email` (already lower-cased once, on
  `TenantMember`, not at every comparison -- reviewer minor) **and**
  `principal.subject == member.uid`. The second half is new: pinning the
  token's `sub` claim beside the listed email is what stops a service account
  that was deleted and recreated under the same address -- GCP allows the
  account id to be reused -- from silently inheriting the old account's
  tenant the moment its email is presented again. An email match with a
  mismatched `uid` is treated as no match at all, exactly like an unlisted
  account, and falls through to today's refusal (wrong domain, not in
  `ALLOWED_USERS`);
- **matched with `re.fullmatch`, not `re.match`** (reviewer minor: `match`
  with a trailing `$` in the pattern still accepts a string with a trailing
  newline, which `fullmatch` does not; a compiled anchor is not a substitute
  for asking the regex engine to consume the whole string);
- **only on a user-managed service-account address**
  (`<id>@<project>.iam.gserviceaccount.com`); a listed entry of any other
  shape never matches even if it got past Terraform and settings. The
  project-id pin that excludes a Google-managed service agent is enforced at
  startup in `settings.py`, not in this regex -- `identity.py` is frozen and
  has no notion of which project it is running in, so the regex alone would
  still accept `service-1234@gcp-sa-x.iam.gserviceaccount.com` under a
  project literally named `gcp-sa-x`; only `settings.py`'s `endswith` check
  closes that (reviewer minor, see item 2);
- **on the email and subject the token verified**: `_from_claims` passes the
  `email` and `sub` claims of a Google ID token that `verify_oauth2_token`
  accepted with `email_verified` not false, or of an IAP assertion
  `IapAssertionVerifier` accepted. Nothing the caller sends in a body, a
  header or a query is read;
- **before any group lookup**: a match returns the listing tenant's id,
  derived by `tenant_id_for_group(principal)` for `kind="group"` or
  `tenant_id_for_user(principal)` for `kind="user"`, from the tenant's own
  kind and principal, so the account and the group it stands beside cannot
  name two different tenants. An entry whose `kind` is neither now **raises**
  `AuthError` instead of silently falling through to the `user` branch
  (reviewer minor) -- `settings.py` already refuses that shape at startup, so
  reaching it here means the belt-and-suspenders check caught something the
  suspenders should already have caught, and that is worth surfacing as a 401
  rather than quietly guessing.

With the default `service_accounts=()` every existing caller of
`resolve_tenant` behaves exactly as today.

**4. `auth.py`, the non-frozen half.** **Corrected 2026-09-29: the `sub` claim
needs normalising before it reaches `tenant_member_for`, and the first draft
of this entry did not do that.** `_from_claims` reads it today at line 335:

```python
        subject = str(claims.get("sub", ""))
```

For a bearer ID token that is already the bare unique id Terraform's
`data.google_service_account.unique_id` renders. For an **IAP assertion it is
not**: `IapAssertionVerifier.verify` documents it in so many words --
*"IAP puts the verified identity in `email`, and `sub` carries a stable
`accounts.google.com:<id>` rather than a bare subject"* (`auth.py`, in the
class docstring). **`ci-fix.yml` calls swarm-api through IAP**, like every
other caller of this Cloud Run service that is not inside the VPC, so the
listed path's `uid` pin -- compared as `principal.subject == member.uid` in
`tenant_member_for` -- would never match the real account's own token as
first drafted: the claim it would compare carries a twelve-character prefix
Terraform's rendered `uid` never has. Fixed at the same line, before
`Principal` is built, since this is a fact about the claims format and not
about listing:

```python
        subject = str(claims.get("sub", ""))
        # IAP prefixes a stable "accounts.google.com:" (IapAssertionVerifier,
        # above); a bearer ID token's `sub` carries no such prefix. Stripped
        # here, once, for BOTH paths, so `principal.subject` is the bare id
        # either way and `tenant_member_for`'s `uid` comparison (item 3) does
        # not need to know which path produced it -- normalising per-path
        # inside the comparison would be the second copy of this rule,
        # findable only by reading both call sites at once.
        subject = subject.removeprefix("accounts.google.com:")
```

`Principal.subject` had no reader anywhere in the codebase before this
change (`grep -rn "\.subject\b" apps/swarm-api apps/common` -- none), so this
is safe to normalise unconditionally rather than only inside the new listed
path: nothing existing depended on the prefixed form.

Then, as the first thing after reading `email` and `subject`:

```python
        member = tenant_member_for(email, subject, self._settings.tenant_service_accounts)
        if member is not None:
            # Reviewer minor: the listed path requires an EXPLICIT True, not
            # merely "not False" -- the weaker rule the bearer path applies
            # everywhere else (GoogleTokenVerifier.verify). A listed account
            # skips both Cloud Identity passes below, so this claim is the
            # only outside confirmation of the identity left; treating an
            # absent claim as acceptable here would remove it.
            if claims.get("email_verified") is not True:
                raise AuthError("listed service account requires a verified email claim")
            principal = Principal(
                email=email,
                subject=subject,
                domain=email.rsplit("@", 1)[1],
                groups=(),
            )
            log.info("tenant member %s resolved by listing", email)
            return AuthContext(
                principal=principal,
                tenant_id=resolve_tenant(
                    principal,
                    self._settings.tenant_groups,
                    service_accounts=self._settings.tenant_service_accounts,
                ),
                is_admin=False,
                tenant_principal=member.principal,
                admin_unresolved=False,
                is_pool_admin=False,
                tenant_member=email,
                # Decision 2: a listed account's rights, below.
                member_scope="continuation",
            )
```

That is: the listing **admits** the account (it does not also go in
`ALLOWED_USERS`, so there is one place that says it may call), it **skips both
Cloud Identity passes** (no tenant-group lookup and no admin-group lookup, so a
directory outage cannot 503 it and it can never pick up admin through a group
it happens to be in), and it is **never an admin or a pool admin**. An
unlisted service account takes today's path unchanged.

**Said plainly, because it changes what the `email_verified is not True` check
(above) is actually worth for `ci-fix.yml`: on the IAP path that check is
inert.** `IapAssertionVerifier.verify` already does `claims.setdefault
("email_verified", True)` (`auth.py:211`) before `_from_claims` ever sees the
claims, because IAP "only ever forwards identities it has already
authenticated" -- so by the time the new listed-path check runs, the claim is
always `True` for an IAP caller, whatever IAP itself received. The check is
real and load-bearing on the **bearer** path, where `GoogleTokenVerifier`
applies the weaker "not False" rule and an absent claim would otherwise pass
through unchanged; on the **IAP** path it can never fire, because IAP has
already made the decision this check exists to double-check. Since
`ci-fix.yml` calls through IAP, decision 4's "listed path requires an
explicit True" is, for this account specifically, a bearer-path-only
protection that happens to also be written for a caller that never takes the
bearer path. It stays in the diff because a future listed account might.

**A second, non-frozen fix folded in here (reviewer minor: "a duplicate-listing
`AuthError` maps to 401/403, not 500").** `Authenticator._authenticate_bearer`
today calls `self._from_claims(claims)` unwrapped at its tail, unlike the IAP
branch of `authenticate()`, which already catches `AuthError` from
`_from_claims` and converts it to `Unauthenticated`. `main.py` registers a
global `@app.exception_handler(AuthError)` that maps an uncaught one to 401
today, so this is not observed as a live 500 -- but it is the one place in
`auth.py` that relies on that global handler instead of converting its own
error, which is exactly the asymmetry `tenant_member_for`'s new
"listed under more than one tenant" `AuthError` (a case this change
introduces) would go through on the bearer path. Made symmetric with the IAP
branch:

```python
        try:
            return self._from_claims(claims)
        except Forbidden:
            raise
        except AuthError as exc:
            raise Unauthenticated(str(exc)) from None
```

**5. Scope: continuation-only rights, DEFAULT-DENY (decision 2, 2026-09-29;
redesigned 2026-09-29 after re-review found the per-route list incomplete).**
A listed account's tenant membership is real -- it is `eng`, with `eng`'s GSA,
secrets, prefix and namespace -- but its RIGHTS within that tenant are not a
human member's. `AuthContext` gains

```python
    #: "" for an ordinary member (a human, or -- unlisted -- today's only
    #: kind of caller): every route behaves exactly as today. "continuation"
    #: for a listed service account (decision 2, 2026-09-29): it may submit a
    #: `continues_task` workflow and read what IT submitted, and nothing else.
    #: Not a kind of admin and not read by `require_admin` -- `is_admin` and
    #: `is_pool_admin` already answer that, independently, and stay False for
    #: every listed account regardless of this field.
    member_scope: str = ""
```

**What the first draft of this entry got wrong.** It named a list of routes
to gate: `create_task`, `create_task_batch`, `cancel_task`, `cancel_workflow`,
and a `submitted_by` filter on the task/workflow read routes. Re-review found
that list incomplete by construction -- it was built by reading `tasks.py` and
`workflows.py` and stopping, not by enumerating every router the app
includes. Missed, all of them reachable by a continuation-scoped caller under
that design because they depend on bare `current_auth` and were simply never
looked at:

- **`routes/accounts.py`, all eight routes** (`routes/accounts.py:157-369`):
  `GET /v1/accounts` (list, read-only). `POST /v1/accounts/authorize` only
  starts a sign-in (no write). The other six WRITE: `POST /v1/accounts`
  (register a credential), `POST /v1/accounts/exchange` (redeem a sign-in
  and register the account), `POST /v1/accounts/{id}/refresh` (rotate the
  credential now), `PUT /v1/accounts/{id}/lending`, `PUT
  /v1/accounts/{id}/state` (pause/drain), `DELETE /v1/accounts/{id}`. A
  stolen token could register a credential into `eng`'s pool, pause or
  delete one of `eng`'s accounts, or change who it lends to.
- **`POST /v1/tenants/me/credentials`** (`routes/tenants.py:54`): the one
  route in the service that accepts key material. A stolen token could
  overwrite `eng`'s provider API key with one the attacker controls.
- **`GET /v1/attempts` and `GET /v1/outcomes`**: both tenant-wide by design
  (`list_attempts`'s docstring: "Attempts across every TASK of the caller's
  tenant"), not task-scoped, so neither goes through `Store.get_task` and
  neither can be narrowed by a `submitted_by` filter the way the task routes
  can -- there is no task in the call to filter.
- **The checkpoint-content routes** (`routes/checkpoints.py:53-118`): `GET
  .../checkpoints/{checkpoint_id}/files`, `.../files/{path}`, `.../content`.
  These three don't even declare `auth: AuthContext` -- only `tenant_id: str =
  Depends(tenant_scope)` -- so there was **no seam** for a per-route
  `member_scope` check to attach to in the first draft's design at all; the
  gap was not a missed line, it was a missing parameter.

**The fix: default-deny in `current_auth` itself, not an opt-in gate each
route remembers to add.** `tenant_scope` already depends on `current_auth`
(every route above does, transitively, including the checkpoint ones -- that
part was never the gap), so putting the check there closes every route in one
place, present and future, the same way `admin_auth`/`POOL_ADMIN_ROUTES`
already gates the admin surface:

```python
# auth.py, beside POOL_ADMIN_ROUTES

#: Every route a CONTINUATION-SCOPED account (member_scope="continuation") may
#: call, as (HTTP method, route template) -- an ALLOW-LIST, same reasoning as
#: POOL_ADMIN_ROUTES: a route defaults to CLOSED for this scope until named
#: here, where opening it is a decision visible in review, rather than
#: defaulting OPEN until someone notices and closes it. This is what the first
#: draft's per-route list should have been from the start.
#: tests/unit/control_plane/test_continuation_scope_is_narrow.py holds this
#: set equal to the decided one and sweeps every route in every router
#: against it, the same shape as test_pool_admin_is_narrow.py.
CONTINUATION_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/workflows"),  # only WITH continues_task -- see item 4
    ("GET", "/v1/tasks"),
    ("GET", "/v1/tasks/{task_id}"),
    ("GET", "/v1/tasks/{task_id}/events"),
    ("GET", "/v1/tasks/{task_id}/attempts"),
    ("GET", "/v1/tasks/{task_id}/artifacts"),
    ("GET", "/v1/tasks/{task_id}/artifacts/content"),
    ("GET", "/v1/tasks/{task_id}/artifacts/raw"),
    ("GET", "/v1/tasks/{task_id}/checkpoints"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files/{path:path}"),
    ("GET", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/content"),
    ("GET", "/v1/tasks/{task_id}/logs"),
    ("GET", "/v1/tasks/{task_id}/transcript"),
    ("GET", "/v1/tasks/{task_id}/answer"),
    ("GET", "/v1/tasks/{task_id}/input"),
    ("GET", "/v1/workflows"),
    ("GET", "/v1/workflows/{workflow_id}"),
    ("GET", "/v1/resource-classes"),  # static catalogue, no tenant data
    ("GET", "/v1/runtimes"),          # static catalogue, no tenant data
})


def require_continuation_route(
    ctx: AuthContext, route: tuple[str, str] | None
) -> AuthContext:
    """A continuation-scoped caller may reach only CONTINUATION_ROUTES.

    An ordinary member (`member_scope == ""`) returns immediately -- this
    changes nothing for anyone but a listed account. `route=None` (no route
    matched) fails closed, same as `require_admin`.
    """
    if not ctx.member_scope:
        return ctx
    if route is not None and route in CONTINUATION_ROUTES:
        return ctx
    method, path = route if route else ("?", "unmatched")
    raise Forbidden(f"a continuation-scoped account may not call {method} {path}")
```

```python
# deps.py, current_auth -- after ctx.authenticator.authenticate(...) succeeds,
# before the rate limiter (an account refused the route gets no limiter
# consequence for the request it was never allowed to make)
    path = getattr(request.scope.get("route"), "path", None)
    route = (request.method.upper(), path) if path else None
    require_continuation_route(auth, route)
```

This is the SAME dependency every route already calls to authenticate at
all -- directly, or through `tenant_scope`/`admin_auth`, both of which
`Depends(current_auth)` themselves -- so a route added tomorrow with no idea
this scope exists is closed to it by default, not open until someone
remembers to gate it. `routes/accounts.py`, `routes/tenants.py`'s credential
route, `/v1/attempts`, `/v1/outcomes`, `/v1/stats`, `/v1/capacity`,
`/v1/providers`, `POST /v1/tasks`, `POST /v1/tasks/batch`, every `/{id}/cancel`
route and every `/v1/admin/*` route are refused **because they are absent from
CONTINUATION_ROUTES**, not because each was individually taught to check
`member_scope` -- the fix for the missed routes and the fix for the routes
this entry already named are the same fix.

**`/v1/stats`, `/v1/capacity`, `/v1/providers`, `/v1/tenants/me`, classified
explicitly, per re-review:** none is in `CONTINUATION_ROUTES`. All four are
tenant-wide informational reads the fixer's job -- submit a continuation, read
what it submitted -- does not need; `/v1/tenants/me` is otherwise harmless
(no secrets, only the caller's own principal and the tenant's declared
providers) but is left out on the same "nothing not needed for the job"
principle, not because it is dangerous. `/v1/resource-classes` and
`/v1/runtimes` ARE included: both are the platform's static, non-tenant
catalogue (`platform.py`'s own docstrings: "no store read, no tenant filter"),
so listing them costs nothing and a client would otherwise have no way to
interpret `resource_class`/`runtime` names it already receives back on the
tasks it can read.

**`POST /v1/workflows`** stays in `CONTINUATION_ROUTES`, but
`SubmissionService.submit_workflow`, right beside its existing
`resolve_continuation` call (#273), still refuses when
`auth.member_scope == "continuation" and spec.continues_task is None`: being
on the route allow-list only means the ROUTE is reachable, not that every
request to it succeeds. Everything `resolve_continuation` already checks --
caller's own tenant, `direct-pr`, one step, no `repository_ref` -- still
applies unchanged on top of this.

**The `submitted_by` filter lives in the store's task lookup, not
route-by-route, per re-review.** One shared dependency derives it:

```python
# deps.py
def submission_scope(auth: AuthContext = Depends(current_auth)) -> str | None:
    """`None` for an ordinary member (unfiltered within the tenant, today's
    behaviour); the caller's own email for a continuation-scoped one. ONE
    function, so every read route derives this the same way instead of each
    restating `auth.email if auth.member_scope == "continuation" else None` --
    which is what the first draft did, and how the checkpoint-content routes,
    which never even took an `AuthContext`, were missed: there was nowhere
    that repeated expression could have been written for them.
    """
    return auth.email if auth.member_scope else None
```

Every route in `CONTINUATION_ROUTES` that reads a task or workflow now also
declares `submitted_by: str | None = Depends(submission_scope)` and passes it
down to `Store.get_task`/`Store.list_tasks`/`Store.get_workflow`/
`Store.list_workflows`, which gain the parameter and do the filtering
**once, in the store**.

**`submitted_by` is a REQUIRED keyword, with no default, on all four --
corrected 2026-09-29, round 3 of re-review.** The first draft gave it
`submitted_by: str | None = None`, which is opt-in: a route or service
that reaches a task through a layer that never learned about this change --
the artifact service, `ctx.inspection`, the transcript and answer services,
`Store`'s OWN internal helpers that call `self.get_task(...)` before doing
something else -- keeps compiling, keeps passing its existing tests, and
keeps returning the task, silently, to a caller the filter was supposed to
refuse. `Store.get_task` alone has two such internal callers today
(`store.py:961` and `:1051`, a tenant check and an artifact-manifest read
that each call `self.get_task(tenant_id, task_id)` with no third argument),
and `Store.get_workflow` has a third (`store.py:1194`) -- none of them
security-relevant today, all three exactly the shape a missed thread-through
would take. A default makes that shape free to write; removing the default
makes it a `TypeError` at the call, and a `reportCallIssue`/missing-argument
error at type-check, before it ever reaches a test:

```python
# store.py
def get_task(self, tenant_id: str, task_id: str, *, submitted_by: str | None) -> Task:
    task = ...  # unchanged lookup and tenant check
    if submitted_by is not None and task.submitted_by != submitted_by:
        # SAME words as the existing cross-tenant refusal, corrected 2026-09-29:
        # `Store.get_task` today (main, checked directly) raises
        # `NotFound(f"task {task_id!r} not found")` for BOTH a genuinely
        # missing task and another tenant's task -- NOT "is not a task in
        # your tenant", which is #273's own `resolve_continuation` wrapper
        # message for `continues_task` specifically, a different code path.
        # An earlier draft of this entry quoted the wrong one. Not a new
        # enumeration oracle: a continuation-scoped caller cannot distinguish
        # "not yours" from "not in your tenant" from "does not exist" any
        # more than a cross-tenant caller can today.
        raise NotFound(f"task {task_id!r} not found")
    return task
```

`list_tasks`, `get_workflow` and `list_workflows` gain the identical
`*, submitted_by: str | None` (no default) and the identical check, so all
four fail the same way when a call site forgets the argument. **Every
existing call site in the codebase passes `submitted_by=None` explicitly**
(today's unfiltered behaviour, made an explicit decision rather than an
implicit default) -- verified by `grep -rln
"\.get_task(\|\.list_tasks(\|\.get_workflow(\|\.list_workflows(" --include="*.py" .`
against this branch: `apps/swarm-api/swarm_api/{store.py, rollup.py,
outcomes.py, inspect.py, routes/admin.py, routes/tasks.py,
routes/workflows.py}`, `apps/reconciler/reconciler/backends.py`, and
`tests/unit/control_plane/{test_scheduler_writes_are_guarded.py,
test_workflow_state_rollup.py}` -- fifteen call sites in `apps/swarm-api`
alone outside `store.py`'s own internals, plus the reconciler's. Every one of
them is a full-member or internal (non-tenant-scope) read today, so
`submitted_by=None` is a no-op change to their behaviour and a REQUIRED
statement of that fact, not a new one to justify per site.

**This is what closes the checkpoint-content gap, and closes it the way
round 2 intended but did not enforce.** Those three routes gain
`submitted_by: str | None = Depends(submission_scope)` and thread it through
`service.list_files(tenant_id, task_id, submitted_by=submitted_by, ...)` ->
`CheckpointContent` -> `InspectionService`'s own `Store.get_task` call. Round
2 already proposed this thread-through; round 3's fix is that `Store.get_task`
itself now REFUSES to compile or run without the keyword arriving from
somewhere, so a fourth service layer discovered later -- the artifact
service, the transcript service, the answer service, or one not yet
written -- cannot reach a task by forgetting the argument; it can only reach
one by supplying it, correctly or not, and supplying it wrong is a much
narrower way to fail than supplying nothing at all.

### What `tenant_principal` becomes for such a caller

**The tenant's principal, not the caller's email**: `eng@saga.xyz` for the
fixer, exactly what a human member of `eng` gets from `_tenant_principal`.
It has to be. `Service.tenant_for` passes it to `Store.ensure_tenant`, and
`Service.scope_for` to `Store.assert_tenant_scope`, and both compare it with
the principal stored on the `eng` tenant document to refuse a caller whose
tenant id belongs to a different principal. The account's own email there
would make every request it sends a collision refusal -- and if it were ever
the first caller to create the document, it would record the bot as `eng`'s
principal. The `secret_admin_principals` check in `tenant_for` is unaffected,
because it reads that same tenant principal, and the listing refuses an
account in `secret_admin_members` separately (below).

The caller's own identity is still `principal.email`, which is what
`submitted_by` is built from. `member_scope` (item 5, above) is a separate,
narrower gate layered on top of `tenant_id`/`tenant_principal`: the tenant
says WHICH secrets, GSA and prefix a caller gets; the scope says WHICH of the
tenant's routes it may use. A future listed account with fuller rights would
set `member_scope=""` and get everything a human member gets -- this entry
proposes `"continuation"` for `swarm-ci-fix` specifically, not a ceiling on
the mechanism.

### Audit: which member submitted a task

Three records, none a frozen-type change; extended so submit, cancel and
continue are recorded the same way rather than only the first (decision 4,
"audit records the acting member ... consistently"):

- **`Task.submitted_by` and `Workflow.submitted_by`** are already the verified
  email (`service.py` sets both from `ctx.email`), so a fixer task already
  carries `swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com`, and the
  outcome ledger's `submitted_by` grouping separates the bot's tasks from the
  humans' in `eng` with no further change. This covers submit AND continue --
  a continuation is a normal `Store.create_tasks` call for its one step task,
  so it already gets both fields with no extra code.
- **`AuthContext.tenant_member`** (new, in `auth.py`, default `""`) is the
  listed email when the tenant came from a listing. The `SUBMITTED` event that
  `Store.create_tasks` writes gains `detail["submitted_by"]` and
  `detail["tenant_member"]` -- `"service_account"` for a listed caller, absent
  otherwise -- so the event trail says, per task, not only who submitted it
  but that the tenant was assigned by a listing rather than by a group. The
  sign-in log line above records the same per request, with no token material
  (the email is already what every other `auth.py` log line names).
- **`Store.request_cancel`**, called from `cancel_task` with `by=auth.email`
  already, gains the same `tenant_member` parameter `create_tasks` gets, so
  the event it writes carries `detail["tenant_member"] = "service_account"`
  under the identical rule. Today only a full member can reach `cancel_task`
  at all -- `("POST", "/v1/tasks/{task_id}/cancel")` is absent from
  `CONTINUATION_ROUTES` (item 5), so `current_auth` itself refuses a
  continuation-scoped caller before the route body, let alone the store, runs
  -- so this is written for consistency with any future listed account whose
  scope is not `"continuation"`, not for `swarm-ci-fix` itself -- a
  `member_scope` that changes must not also have to remember to re-wire the
  audit trail for the one action it newly permits.

### Validation

Every rule is enforced at plan time in `terraform/infra/variables.tf`, and again
at swarm-api startup in `settings.py` for anything Terraform cannot see (an
environment variable set by hand). The variable already requires Terraform
`>= 1.9.0` (`versions.tf`), which is what lets a validation on `var.tenants`
read `var.admin_users`; `secret_admin_members` already validates against
`var.tenants` the same way.

1. **Only a user-managed service account in this project, never a human.**

   ```hcl
   validation {
     condition = alltrue(flatten([
       for t, v in var.tenants : [
         for sa in v.service_accounts :
         can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", sa))
         && endswith(sa, "@${var.project_id}.iam.gserviceaccount.com")
       ]
     ]))
     error_message = "tenants.*.service_accounts takes bare, lower-case emails of user-managed service accounts in this project (<id>@<project>.iam.gserviceaccount.com): never a person, a group, a domain, a pattern, a serviceAccount: member or a Google-managed account."
   }
   ```

   The expression is the one #273's `ci_fix_service_account` validation
   already uses. It refuses a human (no Workspace domain ends in
   `.iam.gserviceaccount.com`), a `serviceAccount:` member, a wildcard, and
   the Google-managed accounts every workload in a project can run as
   (`<number>-compute@developer.gserviceaccount.com`,
   `<project>@appspot.gserviceaccount.com`). The project pin is this entry's
   addition, and one the owner must accept: who can mint a token for an
   account is decided by IAM in that account's project, and only this
   project's IAM is reviewed here.

2. **Under one tenant only.**

   ```hcl
   validation {
     condition = length(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])) == length(distinct(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])))
     error_message = "a service account may be listed under ONE tenant: it decides the account's secrets, GCS prefix and namespace, and two listings would make that depend on rendering order."
   }
   ```

   The same rule is enforced two more times, not three as the first draft of
   this entry said: `TENANT_SERVICE_ACCOUNTS` renders as a JSON **list**, not
   an object keyed by email (item 2, above, reviewer minor), so a duplicate
   there is no longer a `locals.tf` plan-time error by construction -- it is
   `settings.py` that refuses one explicitly at startup (by membership check,
   not by relying on `dict`/`json` key collapsing), and `tenant_member_for`
   that raises `AuthError` rather than pick one if a caller somehow reaches
   this layer with two live entries for the same email. Terraform's own
   `var.tenants` validation above is still the first and cheapest of the
   three: it never lets the plan apply with a duplicate in the first place.

3. **Never an admin, in any admin list.**

   ```hcl
   validation {
     condition = length(setintersection(
       toset(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])),
       toset(concat(
         [for u in var.admin_users : lower(u)],
         [for u in var.admin_pool_users : lower(u)],
         [for m in var.secret_admin_members : lower(replace(m, "serviceAccount:", ""))]
       ))
     )) == 0
     error_message = "a tenant's service account may not also be in admin_users, admin_pool_users or secret_admin_members: a listing gives exactly one tenant's rights, and an admin entry would add every tenant's."
   }
   ```

   `admin_groups` cannot be checked here, because whether a group contains the
   account is a fact in the directory, not in Terraform. That is why
   `auth.py` skips the admin-group pass for a listed caller and sets
   `is_admin=False` unconditionally: membership of an admin group does not
   make a listed account an admin, by construction rather than by validation.

4. **Never a tenant's own principal.**

   ```hcl
   validation {
     condition = length(setintersection(
       toset(flatten([for t, v in var.tenants : [for sa in v.service_accounts : lower(sa)]])),
       toset([for t, v in var.tenants : lower(v.principal)])
     )) == 0
     error_message = "a service account that is a tenant's principal already has a tenant; listing it under another would give it two."
   }
   ```

   This is what stops `swarm-verify` (principal of `u-sw-c90291`) being listed
   under `eng`.

**The account's unique id is not a `validation` block** -- it is not user
input, it is read live from `data.google_service_account.listed` (item 2,
above) at plan time, from whatever account currently holds that email. If the
email is later deleted and recreated, the next `terraform apply` reads the
NEW account's unique id and renders that; nothing here freezes the old one.
The pin's job is narrower than a validation: it stops a stale, already-issued
token's `sub` claim (from the old account) from matching a listing that now
names a different one, in the window between the account being recreated and
the next successful apply -- not to catch a recreation that Terraform itself
already reflects.

Every refusal happens at `terraform plan`, before anything is applied, and
`tests/terraform` asserts each one (below).

### Isolation analysis (invariant 9)

**A listed account gets exactly its tenant's rights, narrowed further to
continuation only, and nothing else.** It resolves to one `tenant_id` and one
`tenant_principal`, the same pair a human member of that tenant gets, so every
tenant-scoped check downstream -- `get_task(tenant_id, ...)`,
`assert_tenant_scope`, the tenant's GSA, secrets, GCS prefix and namespace --
applies to it unchanged. On top of that, `member_scope` (below) cuts it down
to a fraction of what a human member of `eng` can do. It gets no admin (the
flag is forced false and the lists refuse it), no pool admin, no second
tenant (one listing, never a principal), and no personal tenant (a listed
account never falls through to `u-...`). The change grants it **no GCP IAM at
all**: it does not touch the tenant's GSA, its secrets or its bucket, and the
account's own roles stay exactly what #273's `ci_fix.tf` and
`frontend_iap_members` give it. Compared with the rejected alternative
(joining `eng@saga.xyz`), it gets none of that group's grants outside this
platform, and, after decision 2 below, far less than a member's grants inside
it too.

**What an attacker who steals its token gets (M2), restated honestly after
re-review -- the first draft of this section understated the reach, and the
owner has now reviewed and accepted the reach as stated here, not the earlier,
narrower one.** For as long as the token lives -- an ID token or IAP assertion
expires in at most an hour, and an access token minted by federation the
same, though "who can mint it" is wider than the token's own lifetime, see
below -- and constrained by `member_scope = "continuation"` (item 5, above):

- **Submit `continues_task` for ANY `eng` `direct-pr` task that still exists,
  with an ARBITRARY PROMPT.** `resolve_continuation` (#273) constrains the
  SHAPE of a continuation -- caller's own tenant, `direct-pr` strategy, one
  step, no `repository_ref`, the continued task's own repository -- and
  constrains NOTHING about the step's own instructions: that field is
  ordinary caller-supplied task input, read exactly as any other task's is.
  A continuation is not "reapply the same fix"; it is "run a new agent
  session, with a prompt of the attacker's choosing, against that repository,
  using `eng`'s forge token to push whatever it produces to that PR's
  branch." Cannot spend `eng`'s capacity on a genuinely NEW task (no existing
  `direct-pr` PR to attach to) and cannot touch another tenant's task --
  `resolve_continuation` and the scope check both still refuse those -- but
  neither of those is much of a ceiling: any `eng` repository with an open
  `swarm/` pull request is reachable, for whatever prompt the attacker sends.
- **Exfiltrate private repository content through the agent's own
  artifacts.** The account may read `GET /v1/tasks/{id}/artifacts`,
  `/logs`, `/transcript` and `/answer` for tasks it itself submitted (item 5).
  A continuation it submits is such a task. So the arbitrary prompt above can
  instruct the agent to copy the contents of any file the checked-out
  repository holds -- not only files related to the original red build --
  into a commit message, an artifact, or its own transcript, and the
  attacker reads it back through the same allowed routes. This is not a
  hypothetical composition of two separately-acceptable capabilities: it is
  the direct consequence of "arbitrary prompt with `eng`'s push access" plus
  "read what I submitted" existing on the same account.
- **No cap on how many times, and no cap on spend.** The owner considered and
  DECLINED a limit on continuation count or cost for this account (decision
  3, 2026-09-29). So the two capabilities above are not "one bounded
  incident" -- they can be repeated against every `eng` repository with an
  open `direct-pr` PR, for as long as the token can be used or re-minted (see
  **Who can mint the account's token**, below), each repetition spending real
  agent-provider cost against `eng`'s subscription and real capacity against
  `eng`'s `max_active` ceiling (invariant 2 still gates CONCURRENT capacity;
  nothing gates the NUMBER of sequential continuations over time). **Read the
  tasks it itself submitted** is otherwise the account's only read reach: it
  cannot list or read a human `eng` member's other work, and it cannot
  cancel anything, its own tasks included.

**`.github/workflows/auto-merge.yml` remains a real mitigation, but of a
narrower thing than the reach above.** A push to a pull request's branch
fires `synchronize`, which the workflow's `remove-stale-ready` job treats as
"this head changed" -- it turns auto-merge off and removes the `ready` label
unconditionally, label-adding events excepted
(`.github/workflows/auto-merge.yml` lines ~237-270). So a malicious commit
pushed this way cannot merge ITSELF: the pull request it lands on drops out
of the auto-merge queue the moment the push lands, and needs a human to
re-review and re-label it `ready`. **That bounds only the worst
CODE-SHIPPING outcome.** It does nothing for the exfiltration path above --
reading a secret back through the agent's own artifacts needs no merge at
all -- and nothing for the spend -- an agent session costs money and capacity
whether or not its commit ever merges. Restated so this is not read as more
protective than it is: it is one mitigation against one of three
consequences, not a bound on the reach as a whole.

**This is accepted, not mitigated, by owner decision on 2026-09-29 (decision
3).** The owner reviewed this restated reach -- arbitrary-prompt push access
plus artifact-based exfiltration plus unbounded repetition and spend -- and
chose not to add a continuation cap. What actually bounds this account is the
scope in item 5 (it is `continues_task`-or-nothing, and read-your-own-only)
and the token-lifetime and who-can-mint analysis below, not a limit on how
many times or how expensively the account can act within that scope.

Every task it submits still says so in `submitted_by` and in its `SUBMITTED`
event (and, after decision 4's audit fix, every cancel and continuation it
performs says so too), so what it did is enumerable afterwards, after the
fact. It is not other tenants, not the admin surface, not the provider key or
forge token AS MATERIAL (#219 keeps both out of the workspace -- the
exfiltration path above is through repository content the agent reads and
reports, not through reading the credential itself), and not saga.xyz outside
this platform.

### Who can mint the account's token

**#273's `terraform/bootstrap/ci_fix.tf` binds `roles/iam.workloadIdentityUser`
on the account to exactly one principalSet,**
`attribute.job_workflow_ref/bogdan-alexandrescu/SwarmCloud/.github/workflows/ci-fix.yml@refs/heads/main`.
GitHub sets `job_workflow_ref` from the workflow file the job actually runs,
so another workflow -- in this repository on main, on another branch, or in a
fork -- presents a different value and is refused; the pool provider's
`attribute_condition` separately refuses any other repository and any ref
outside `github_allowed_refs`, and never `refs/pull/*`. `workflow_run` runs
the default branch's copy, so a pull request cannot change the file it runs
under. **That is a real limit on an external caller who holds nothing else in
this project:** minting a token that way means first getting a change to
`ci-fix.yml` merged to `main`.

**It is not a limit on the project's own IAM, and the first draft of this
entry overstated it as one.** Measured on 2026-09-29 as bogdan@saga.xyz,
`saga-agents-staging` IAM already lets several principals mint the account's
token without touching `ci-fix.yml` at all:

- **`roles/iam.serviceAccountAdmin`** includes `iam.serviceAccounts.setIamPolicy`
  on every service account in the project, so a holder can bind
  `roles/iam.serviceAccountTokenCreator` on `swarm-ci-fix` to any principal,
  themselves included, and mint a token directly -- no workflow run, no WIF
  pool involved. Held by: the deployer (`swarm-tf-deployer`), `bogdan@`,
  `facu@`, `konstantin@`.
- **`roles/iam.workloadIdentityPoolAdmin`** can edit the pool provider's
  `attribute_condition` and attribute mapping, so a holder can widen the
  principalSet the binding above already trusts to admit a different
  workflow, ref or repository -- defeating the "only `ci-fix.yml` on `main`"
  guarantee from the inside rather than around it. Held by: the deployer,
  `bogdan@`.
- **`roles/owner`** can do both of the above and everything else in the
  project. Held by: `bogdan@`, `emanuel@`.

**The owner accepted this as a known risk on 2026-09-29, not as a gap for
this PR to close.** Every principal listed already holds project-level trust
for other reasons -- the deployer runs Terraform against this project;
`bogdan@`, `facu@`, `konstantin@` and `emanuel@` administer it. What this PR
narrows is the ACCOUNT's own reach once a token is minted (decision 2,
continuation-only rights, above) and the DEPLOYER's standing roles (next
paragraph) -- not who else in the project could, in principle, mint the
token; that is accepted, not mitigated, here.

**The deployer's two roles, corrected (2026-09-29, after this entry's first
draft): `roles/iam.serviceAccountAdmin` cannot be scoped by an IAM
condition.** IAM resources do not expose `resource.name` to a condition at
all: *"the condition `resource.name.endsWith == devResource` never grants
access to any IAM resource because IAM resources don't provide the resource
name"* ([conditions attribute reference](https://docs.cloud.google.com/iam/docs/conditions-attribute-reference)),
and `iam.googleapis.com` is absent from the resource-service table in
[conditions-resource-attributes](https://docs.cloud.google.com/iam/docs/conditions-resource-attributes).
A `resource.name` condition on this role would revoke it outright, not narrow
it, and the deployer's first service-account change after applying one would
403. `terraform/bootstrap/deployer_conditions.tf` (#334) records this in the
same words, as the reason the role stays **unscoped** in that PR.

**`roles/iam.workloadIdentityPoolAdmin` IS removed, in #334, and needs no
condition:** `terraform/infra` names no pool or provider of its own -- it
only names GKE's pool as a string inside a service-account binding's member
-- so the role was pure standing reach, dropped outright rather than scoped.

**For `roles/iam.serviceAccountAdmin`, the owner decided the shape that
actually works, recorded in #334's `docs/ci.md` on 2026-09-29 but not yet
applied by that PR's own diff -- a further bootstrap change:** remove the
deployer's project-wide `roles/iam.serviceAccountAdmin`; grant it
project-wide `roles/iam.serviceAccountCreator` instead (which needs no
per-resource name and stays project-wide by necessity, same as
`serviceusage.serviceUsageAdmin` above); and grant `serviceAccountAdmin`
**per account**, one resource-level binding on each `swarm-*` service account
`terraform/infra` manages, **excluding `swarm-ci-fix` and `swarm-tf-deployer`
themselves** (both bootstrap-managed, outside `terraform/infra`'s remit) --
the shape #23 already gave IAP admin, applied here to service accounts.
Until that lands, the deployer keeps its project-wide `serviceAccountAdmin`
and remains part of the accepted risk above; once it lands, the deployer can
still create and administer the `swarm-*` accounts `terraform/infra` manages,
but can no longer touch `swarm-ci-fix`'s IAM policy at all -- narrower than
"scoped", closed for that one account specifically.

**The residual, stated plainly: this narrows the deployer, not the humans
who separately hold these roles.** `bogdan@`, `facu@` and `konstantin@` hold
`roles/iam.serviceAccountAdmin` as individually granted principals, not
through the deployer, and neither #334 nor the per-account change above
touches those grants. So after both land, **`facu@`, `konstantin@` and
`emanuel@` (`roles/owner`) still can mint `swarm-ci-fix`'s token** --
an accepted risk, for the reason given above: they already hold project-level
trust for other reasons, and this entry narrows the account's own reach and
the deployer's standing access, not every human administrator's.

The remaining preconditions this entry's WIF analysis still depends on:

- the account has **no user-managed keys** (`gcloud iam service-accounts keys
  list --iam-account=swarm-ci-fix@...` lists only system-managed ones), since a
  key mints tokens with no workflow involved;
- `ci-fix.yml` never runs pull-request code in a step that can read the
  federated credential. On #273's head it checks out `main` only ("NOT the
  branch being fixed: its code runs in the fix step's container, never here")
  and runs only when the head repository is this one. That has to stay true,
  because a `workflow_run` job that executes the head's code with the token
  present hands the token to whoever wrote the pull request.

### The diff proposed for `identity.py`

Not applied. Generated against `main` at `d4a2352`. **Revised 2026-09-29 to
fold in the reviewer's minors** (`fullmatch`; `TenantMember` normalises once;
a `uid` pinned beside the email; an unknown `kind` raises) -- this supersedes
the diff this entry's first draft posted.

```diff
--- a/apps/common/swarm_common/identity.py
+++ b/apps/common/swarm_common/identity.py
@@ -8,12 +8,20 @@
 A TENANT IS A GOOGLE GROUP. `eng@saga.xyz` is one tenant whose members share a
 quota budget, provider keys and artifacts. A user in no mapped group falls back
 to a personal tenant so nobody is ever hard-blocked from the platform.
+
+A TENANT MAY ALSO LIST SERVICE ACCOUNTS (contract request 30). A service
+account is not a Workspace principal, and making it a member of the tenant's
+group would hand it every grant that group holds across the company. So a
+listed account resolves to its tenant by an EXACT match on the email AND the
+unique id its verified token carries, before any group is consulted -- never
+by a pattern, a prefix or a domain.
 """
 
 from __future__ import annotations
 
 import hashlib
 import re
+from collections.abc import Iterable
 from dataclasses import dataclass
 
 
@@ -29,6 +37,70 @@
     groups: tuple[str, ...] = ()
 
 
+#: A user-managed service account: `<account id>@<project id>.iam.gserviceaccount.com`,
+#: both halves 6-30 characters as GCP names them. Google-managed accounts
+#: (`<number>-compute@developer.gserviceaccount.com`, `<project>@appspot...`)
+#: do NOT match on purpose: every workload in a project can run as those, so
+#: listing one would put the whole project in the tenant. A human address
+#: cannot match either -- no Workspace domain ends in `.iam.gserviceaccount.com`.
+#: This regex alone does not pin the PROJECT: `settings.py` does that at
+#: startup with an `endswith` check this frozen module has no project id to
+#: perform itself -- see contract request 30, item 2. terraform/infra/variables.tf
+#: validates `tenants.*.service_accounts` with the same expression, and
+#: scripts/lib/check-contract-parity.sh holds the two equal.
+SERVICE_ACCOUNT_EMAIL = re.compile(
+    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
+)
+
+
+@dataclass(frozen=True)
+class TenantMember:
+    """A service account a tenant lists besides its group or user principal.
+
+    `kind` and `principal` are the TENANT's, not the account's: the tenant id
+    is derived from them by the same function that derives it for a human
+    member, so a listed account and the group it stands beside can never name
+    two different tenants.
+
+    Every field is normalised ONCE, here, rather than at each comparison --
+    the earlier draft of this type left `email`/`kind`/`principal` as given
+    and called `.strip().lower()` at every call site instead, which is
+    exactly the kind of repetition that lets one site be missed.
+    """
+
+    email: str       # swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com
+    kind: str        # "group" | "user" -- the tenant's kind
+    principal: str   # eng@saga.xyz -- the tenant's principal
+    uid: str         # the account's OAuth2 unique id (the token's `sub`
+                     # claim), from Terraform's
+                     # data.google_service_account.unique_id -- pinned beside
+                     # the email so an account deleted and recreated under
+                     # the same address, which GCP permits, does not inherit
+                     # the old one's tenant.
+
+    def __post_init__(self) -> None:
+        object.__setattr__(self, "email", self.email.strip().lower())
+        object.__setattr__(self, "kind", self.kind.strip().lower())
+        object.__setattr__(self, "principal", self.principal.strip().lower())
+        object.__setattr__(self, "uid", self.uid.strip())
+
+
+def tenant_member_for(
+    email: str, subject: str, members: Iterable[TenantMember]
+) -> TenantMember | None:
+    """The listed member whose email AND unique id (`sub`) both match, or None.
+
+    Exact equality on both, nothing else. An entry that is not a user-managed
+    service-account address is never matched even when listed, so a
+    misconfiguration that names a human cannot route that human around their
+    group. An email match with no matching `uid` is treated as no match at
+    all -- the caller falls through to today's refusal exactly as an unlisted
+    account would, rather than being told why. An address listed under two
+    tenants is refused rather than resolved by order: the tenant decides
+    which secrets and which GCS prefix a caller gets, and "whichever was
+    rendered first" is not a decision.
+    """
+    wanted = email.strip().lower()
+    if not SERVICE_ACCOUNT_EMAIL.fullmatch(wanted):
+        return None
+    found = [m for m in members if m.email == wanted and m.uid == subject]
+    tenants = {(m.kind, m.principal) for m in found}
+    if len(tenants) > 1:
+        raise AuthError("a service account is listed under more than one tenant")
+    return found[0] if found else None
+
+
 _TENANT_SAFE = re.compile(r"[^a-z0-9-]+")
 
 
@@ -98,14 +166,33 @@
     return domain
 
 
-def resolve_tenant(principal: Principal, group_priority: tuple[str, ...]) -> str:
+def resolve_tenant(
+    principal: Principal,
+    group_priority: tuple[str, ...],
+    service_accounts: tuple[TenantMember, ...] = (),
+) -> str:
     """Pick the caller's tenant.
 
-    `group_priority` is the admin-ordered list of group emails that map to
-    tenants. First match wins, so a user in several mapped groups lands
+    A caller whose verified email AND subject match a listed service account
+    resolves to the tenant that lists it, FIRST and regardless of
+    `principal.groups`: the listing is an admin's explicit statement about
+    this one identity, and a group membership (which a service account can
+    hold) must not be able to move it somewhere else.
+
+    Otherwise `group_priority` is the admin-ordered list of group emails that
+    map to tenants. First match wins, so a user in several mapped groups lands
     deterministically in the same tenant on every request -- which matters
     because the tenant determines which secrets and which GCS prefix they get.
     """
+    member = tenant_member_for(principal.email, principal.subject, service_accounts)
+    if member is not None:
+        if member.kind == "group":
+            return tenant_id_for_group(member.principal)
+        if member.kind == "user":
+            return tenant_id_for_user(member.principal)
+        # settings.py already refuses this shape at startup; reaching it here
+        # means that check was bypassed (a hand-set env var, a test double) --
+        # surfaced rather than guessed at.
+        raise AuthError(f"a listed tenant member names an unknown kind {member.kind!r}")
+
     member_of = {g.lower() for g in principal.groups}
     for group in group_priority:
         if group.lower() in member_of:
```

### What it would break if accepted

Nothing that exists. `resolve_tenant`'s new parameter is keyword-defaulted to
`()`, so `auth.py`'s current call, `test_group_resolution.py` and any other
caller behave exactly as today until `TENANT_SERVICE_ACCOUNTS` is non-empty.
`AuthContext.member_scope` is likewise defaulted to `""`, and
`require_continuation_route` (item 5) returns its argument immediately
whenever `ctx.member_scope` is falsy -- which is every existing caller, since
nothing sets it today -- so `current_auth` gaining this check changes nothing
for a human member or an unlisted service account, on any route, existing or
future. It is EVERY route that newly runs this check, not routes that opt
in (the design item 5 replaced was the opt-in shape, and re-review is why it
was replaced); the change in observable behaviour is confined entirely to a
caller whose `member_scope` is non-empty, and there are none until
`TENANT_SERVICE_ACCOUNTS` lists one. No stored document changes shape: `Task`,
`Workflow`, `Tenant` and `TaskEvent` are untouched (the audit fields are
`detail` keys and `AuthContext` fields outside the contract). `docs/ci.md` on
#273 says the fixer's account must be "admitted (`allowed_users`) and a
member of that tenant's Google group"; that sentence changes to naming
`tenants.eng.service_accounts` and to saying its rights are continuation-only.

### If it is declined

The fixer's account can resolve to `eng` only by joining `eng@saga.xyz`, which
the owner has rejected, or not at all, in which case #273's continuation is
refused with "is not a task in your tenant" on every red pull request and the
fixer can only comment.

### The tests that would prove it

Unit, no credentials and no emulator:

- `tests/unit/control_plane/test_group_resolution.py` (`resolve_tenant`,
  `tenant_member_for`):
  - a listed account resolves to `eng` when `principal.email` AND
    `principal.subject` both match the listing's `email`/`uid`, with
    `groups=()`, and to `tenant_id_for_user(principal)` when the listing
    tenant is `kind="user"`;
  - `SWARM-CI-FIX@SAGA-AGENTS-STAGING.IAM.GSERVICEACCOUNT.COM` resolves the
    same (case-insensitive on email; `uid` still matches exactly);
  - **an email match with a mismatched `uid` does NOT resolve to `eng`** --
    falls through exactly as an unlisted account would (the recreated-account
    case the `uid` pin exists for);
  - **`fullmatch`, not `match`:** an email with a trailing newline
    (`"swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com\n"`) does not
    match, where the earlier `.match()` with `$` in the pattern would have;
  - the listing wins over groups: the same account with
    `groups=("other@saga.xyz",)` and `other@saga.xyz` first in priority still
    resolves to `eng`;
  - no pattern and no domain: `swarm-ci-fix2@...`, `x-swarm-ci-fix@...`,
    `swarm-ci-fix@other-project.iam.gserviceaccount.com` and
    `swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com.evil.example`
    each fall through to today's rule;
  - a human address listed as a member (`bogdan@saga.xyz`) never matches;
  - an account listed under two tenants raises `AuthError`; listed twice
    under the same tenant (same `kind`/`principal`) resolves;
  - **a `TenantMember` with `kind="workspace"` (anything but `"group"` or
    `"user"`) raises `AuthError` from `resolve_tenant`** rather than
    resolving as `kind="user"`;
  - `TenantMember(" Eng@Saga.xyz ".strip(), " GROUP ", ...)`-style
    constructor arguments come out normalised once (`__post_init__`), so two
    `TenantMember`s built from differently-cased/whitespaced input compare
    equal on `email`/`kind`/`principal`;
  - **`tenant_member_for` does no IAP-prefix stripping of its own:** a
    `principal.subject` of `"accounts.google.com:12345"` against a listed
    `uid` of `"12345"` does NOT match here -- proving the frozen function
    does exact comparison only, and that the `"accounts.google.com:"` strip
    (item 4) has to happen in `auth.py`, before this function is ever called,
    not be expected of it;
  - with `service_accounts` omitted, every existing case in the file gives
    today's answer (the file's current cases, unchanged, are this test).
- a new `tests/unit/control_plane/test_tenant_service_accounts.py`
  (`Authenticator` with `StaticTokenVerifier`):
  - a listed account gets `tenant_id="eng"`, `tenant_principal="eng@saga.xyz"`,
    `is_admin=False`, `is_pool_admin=False`, `admin_unresolved=False`,
    `tenant_member=<its email>`, **`member_scope="continuation"`**;
  - a caller with no listing gets `member_scope=""`, including every case
    `test_group_resolution.py` already covers for a human;
  - it is admitted with `allowed_domains=("saga.xyz",)` and empty
    `ALLOWED_USERS`, and an unlisted service account is still 403;
  - the `MembershipResolver` is a fake that raises `GroupLookupError` on any
    call, and `admin_groups` is non-empty: the listed account still
    authenticates (no lookup made, no 503), and is still not an admin;
  - `email_verified: False` is still 401 (the general rule);
  - **`email_verified` ABSENT (not `False`, simply missing) is 401 for the
    listed path on the BEARER path specifically** -- the stricter, listed-only
    rule (decision 4) -- while an unlisted bearer caller with the claim absent
    still succeeds, proving the two paths are not accidentally the same code;
  - **on the IAP path, the same listed caller with `email_verified` absent
    from the raw assertion still authenticates.** This is not a gap in the
    check -- `IapAssertionVerifier.verify` already forces
    `claims.setdefault("email_verified", True)` (`auth.py:211`) before
    `_from_claims` runs, so the listed-path check never observes an absent
    claim on this path at all. The test exists to SAY so, not to catch a
    bug: it is the proof that decision 4's "explicit True" rule is a
    bearer-path protection that happens to be inert for `ci-fix.yml`, which
    calls through IAP;
  - **the IAP path and the bearer path resolve to the SAME `AuthContext`
    (including `member_scope`) from DIFFERENT-SHAPED `sub` claims** -- a
    bearer token's bare `"12345"` and an IAP assertion's
    `"accounts.google.com:12345"` both resolve to the listed account when its
    `uid` is `"12345"`, proving the `"accounts.google.com:"` strip (item 4)
    actually closes the gap it exists for, not merely that both paths agree
    when fed identical input;
  - **a duplicate listing (`tenant_member_for` raising `AuthError`) comes back
    401, not 500,** on both the bearer path and the IAP path -- the symmetric
    `try`/`except` decision 4 adds to `_authenticate_bearer`;
  - `continues_task` naming an `eng` task is accepted, naming a `u-bogdan`
    task is refused "is not a task in your tenant";
  - **`CONTINUATION_ROUTES` is swept against every route in every router the
    app includes, and extended in round 3 to a second assertion per
    task/workflow-scoped route** (`tests/unit/control_plane/test_continuation_scope_is_narrow.py`,
    the same shape as `test_pool_admin_is_narrow.py`):
      - **reachability:** every route NOT in the set 403s a continuation-scoped
        caller with an otherwise-valid request, and every route IN the set
        does not 403 for that reason. This is the test that would have caught
        the first draft's gap, because it does not require anyone to have
        thought of `routes/accounts.py` -- it walks the app's own route table;
      - **ownership, round 3's addition:** for every route in the swept set
        whose path names a `{task_id}` or `{workflow_id}`, the fixture creates
        ONE task/workflow submitted by a different `eng` member and drives
        that route at it with the listed caller's credentials -- every one
        404s ("task 'X' not found", the existing cross-tenant wording --
        corrected 2026-09-29, see item 5's `Store.get_task` snippet). This is
        the assertion that
        catches a missed `submitted_by` thread-through directly, rather than
        relying on someone naming the right route by hand: it walks
        `CONTINUATION_ROUTES` itself, so a route added to the allow-list
        later without its filter wired through fails this test on the day it
        is added, not on the day someone thinks to ask;
  - named explicitly, because re-review found them missing: **all eight
    `routes/accounts.py` routes** (list, register, authorize, exchange,
    refresh, lending, state, delete), **`POST /v1/tenants/me/credentials`**,
    **`GET /v1/attempts`**, **`GET /v1/outcomes`**, **`GET /v1/stats`**,
    **`GET /v1/capacity`**, **`GET /v1/providers`**, **`GET /v1/tenants/me`**
    each 403 for the listed caller with a request that would succeed for an
    ordinary `eng` member;
  - **`GET /v1/resource-classes` and `GET /v1/runtimes`** succeed for the
    listed caller (static catalogue, no tenant data, explicitly allow-listed);
  - **`POST /v1/tasks`, `POST /v1/tasks/batch`, `POST /v1/tasks/{id}/cancel`,
    `POST /v1/workflows/{id}/cancel`** each 403 for the listed caller and
    succeed for an ordinary `eng` member with an identical request otherwise
    -- covered by the sweep above too, and named individually because they
    are the routes decision 2 was written about first;
  - **`POST /v1/workflows` with no `continues_task`** 403s for the listed
    caller and succeeds for an ordinary `eng` member -- `submit_workflow`'s
    own check, since being in `CONTINUATION_ROUTES` only opens the route, not
    every request to it;
  - **`GET /v1/tasks/{id}` for a task `eng`'s human member submitted** 404s
    ("task 'X' not found", the existing cross-tenant wording) for the listed
    caller, and **the same
    call for a task the listed caller itself submitted** succeeds -- proving
    the `submitted_by` filter is per-caller, not per-tenant. Also covered by
    the sweep's ownership assertion above; kept as its own named case because
    it is the one this entry's examples build on elsewhere;
  - **`GET /v1/tasks`** for the listed caller returns only tasks it submitted,
    even when `eng` has others (a LIST assertion, which the sweep's
    single-task ownership check does not itself make -- `Store.list_tasks`
    filtering is exercised here specifically);
  - **`GET /v1/tasks/{id}/checkpoints/{checkpoint_id}/files` (and `/files/{path}`,
    `/content`) for a checkpoint of a task `eng`'s human member submitted**
    404s for the listed caller, and the same call for the listed caller's OWN
    task succeeds -- the case that had no seam at all before `submission_scope`
    was threaded into `checkpoints.py`, and now also caught by the sweep's
    ownership assertion since these three are members of `CONTINUATION_ROUTES`;
  - a new `tests/unit/control_plane/test_store_task_scope_is_required.py`:
    calling `store.get_task(tenant_id, task_id)` (two positional arguments,
    no `submitted_by`) raises `TypeError: get_task() missing 1 required
    keyword-only argument: 'submitted_by'`, and the same for `list_tasks`,
    `get_workflow`, `list_workflows` -- the test that proves the signature
    change itself, independent of any route, so it stays red even if every
    route-level test above were somehow satisfied by an accident of fixture
    setup;
  - the created task's `submitted_by` is the account's email and its
    `SUBMITTED` event carries `tenant_member="service_account"`; the same is
    true of a `CANCELLED` event when a FULL member (not the listed caller,
    which cannot reach cancel -- absent from `CONTINUATION_ROUTES`) cancels
    while resolving through a listing, and of a continuation's own
    `SUBMITTED` event.
- settings: `TENANT_SERVICE_ACCOUNTS` parses as a **list**; `ApiSettings.from_env`
  raises `ValueError` for a human address, a Google-managed service agent
  under a project-shaped id (`service-123@gcp-sa-x.iam.gserviceaccount.com`
  where `x` happens to look like a project id, refused by the `endswith`
  pin, not by the regex), an account from a DIFFERENT project, a duplicate
  email (two list entries, same or different tenant -- refused by the
  explicit membership check, not by relying on `dict`/`json` key collapse), a
  missing or empty `uid`, a `kind` outside `{group, user}`, and an entry also
  in `ADMIN_USERS`, `ADMIN_POOL_USERS` or `SECRET_ADMIN_PRINCIPALS`; absent or
  `[]` is `()`.

Terraform, `tests/terraform/tenancy.tftest.hcl`, each refusal a `run` with
`expect_failures = [var.tenants]`: an account under two tenants; a human
address; `serviceAccount:`-prefixed; a Google-managed compute account; an
account in another project; one in `admin_users`, in `admin_pool_users`, in
`secret_admin_members`; one that is another tenant's principal. `data.
google_service_account.listed` is overridden with a mocked `unique_id` via
`override_data` in the `.tftest.hcl` file, so these runs need no real GCP
call. And one passing `run` asserting the rendered `TENANT_SERVICE_ACCOUNTS`
equals
`[{"email":"swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com","kind":"group","principal":"eng@saga.xyz","uid":"<mocked>"}]`
-- a **list**, revised from the object-keyed-by-email shape the first draft
of this entry proposed (reviewer minor) -- and `[]` when no tenant lists one.

Parity: `scripts/lib/check-contract-parity.sh` gains a check that the
expression in the `variables.tf` validation equals
`identity.SERVICE_ACCOUNT_EMAIL.pattern`, so the plan-time rule and the
runtime rule cannot drift apart. It does not and cannot check the project-id
pin, which lives only in `variables.tf` and `settings.py` (`identity.py` has
no project id to compare against) -- those two are checked against each
other by the settings test above instead.

Each test is proved red first in CI, per CLAUDE.md: the tests land alone on
the implementing branch and must fail there before the change lands on
`<branch>-fix`.

### Invariants

- **Invariant 9.** Analysed above: a listed account is one more member of one
  tenant, with that tenant's GSA, secrets, prefix and namespace and nothing
  else -- and, after decision 2 (2026-09-29), fewer of that tenant's ROUTES
  than a human member, not merely the same rights routed through a bot.
- **Invariant 10.** Unaffected: the listing is operator configuration in
  Terraform; nothing a caller sends chooses a tenant.
- **Invariant 2 (all-or-nothing capacity).** Unaffected in mechanism -- a
  continuation still reserves capacity through the same transaction every
  other task does -- but worth stating given decision 3: nothing here caps
  HOW MANY times a continuation-scoped account can go through that
  transaction over time, only how many it can hold `LEASED` at once (`eng`'s
  `max_active`, invariant 3). The owner accepted that as part of the same
  no-cap decision, not as a separate gap.
- **CONTRACT.md "Tenant = Google group".** Refined, not reversed: a tenant is
  still a group (or a user), and its listed accounts are members by
  declaration rather than by directory.
Applied by #343 (accepted by the owner 2026-09-29).
---

## 31. `models.py`: `WorkflowStep` cannot record a step's verdict gate or its `builds_on`

**Status:** open, recorded 2026-09-29 from #264. If another branch has taken
31 by the time this merges, renumber this one.

### What is true today

#264 added two workflow step fields, `when` (`{"step", "verdict_in"}`: run
this step's agent only on an upstream review's verdict) and `builds_on` (an
upstream step whose pushed branch this step clones). They are accepted by
`WorkflowStepCreate`, checked by `validation.validate_step_routing`, and
stored where the worker reads them: `task.metadata["dispatch"]`, as
`verdict_gate: {"task_id", "verdict_in"}` and `builds_on: <task id>`, keyed by
task id the way `integrates` is. That block is already reserved, so no new
reserved key was needed and nothing frozen was edited.

The frozen `WorkflowStep` has no field for either, so the workflow document
does not record them. `GET /v1/workflows/{id}` shows a gated step exactly as
it shows an ungated one; a reader has to open the step's task to see the gate
(`GET /v1/tasks/{id}`, `dispatch.verdict_gate`). The declaration as the caller
wrote it, keyed by step id, is not stored anywhere.

### The requested change

Two optional fields on `WorkflowStep`, both defaulting to "absent", so every
stored workflow reads back unchanged:

```python
#: {"step": upstream step_id, "verdict_in": [...]} -- run this step's agent
#: only on those verdicts (#264).
when: dict[str, Any] | None = None
#: An upstream step_id whose pushed branch this step's checkout starts from.
builds_on: str | None = None
```

And, with them, `REVIEW_VERDICTS = ("MERGE", "NOT_YET")` in `models.py`, which
is today written out twice (`swarm_api.validation.REVIEW_VERDICTS` and
`agent_worker.verdict.REVIEW_VERDICTS`) and held equal only by
tests/unit/worker/test_verdict_gate.py.

### What it would break if accepted

Nothing stored: both fields default to absent. `SubmissionService.submit_workflow`
would copy them onto the `WorkflowStep` it already builds, and the workflow
codec and the UI's step rows would start showing them.

### If it is declined

The gate stays visible only on the task, and the verdict vocabulary stays in
two copies behind a parity test. That test lives in the unit suite, so it
protects the repository, not a worker image built from an older commit.
---

## 32. `profiles.py`: `browser` and `generic` declare no inputs, so the API bounds them by size alone and the plugin can send them none

**Status: ACCEPTED 2026-09-29 by the owner after three security reviews.**
**Applied by #345** (accepted by the owner 2026-09-29), `part of #218` (see `docs/DEPLOY_STATE.md` or the linked
PR for its state — this entry itself does not track a moving target). It
answers #218,
which contract request 25 left open: which inputs `browser` and `generic`
declare, and with which bounds. Originally numbered 29, because 28 was the
last entry on `main` when this branch was cut and 27 appears nowhere on
`main`. **Renumbered to 32 on 2026-09-29**, after a security review found
four other open PRs had each numbered their own new entry 29 for the same
reason: #259, #314, #304 and #316. Resolved as #259 keeps 29, #314 becomes
30, #304 becomes 31, this entry (#218) becomes 32, and #316 becomes 33.

### What is true today

Measured from `main` at `d4a2352` on 2026-09-29.

**Both profiles are `inputs=None`**, and `check_inputs` hands their input
back unchecked, so swarm-api bounds it by size (`validate_input_size`) and by
Firestore's integer range (`validate_storable`) and nothing else. The owner
confirmed that on 2026-09-26 as the interim (request 25's table row). It was
meant to last until #218 was decided.

**It has already stopped work.** #220 found on 2026-09-28 that every browser
task dispatched through the `sc` plugin failed after it started, with
"browser runner needs input.url or at least one action". The bridge sends a
key only when a declaration names it (`swarm_mcp/profiles.py`, `check_inputs`),
and `browser` declares nothing. So no caller of the plugin can give a browser
task the one thing it cannot run without, and the refusal arrives after
admission, after a lease and a GKE pod start.

**What the size bound lets through, and where it fails instead.** Every item
below is a value the API accepts today that the runner then fails on, or
quietly reads differently:

* `timeout_ms: 0` and `launch_timeout_ms: 0`. Playwright reads 0 as "no
  timeout", so one `wait_for` on a selector that never appears holds the
  attempt, and its `browser` capacity (2 units), for the profile's full
  5400 s.
* `timeout_ms: "abc"`. The runner calls `int()` on it, raises `ValueError` and
  fails the attempt after admission.
* An action without the field its type needs: `{"type": "click"}` raises
  `KeyError` at `action["selector"]`, also after admission.
* `{"type": "wait", "seconds": 600}`. The runner clamps it to 60 with
  `min(..., 60.0)` and does not report the clamp.
* A `goto` to `http://10.0.0.1/`. The runner accepts any http(s) URL with a
  host. The worker's NetworkPolicy drops the packets, and Dataplane V2 drops
  without replying, so the action waits out `timeout_ms` and fails as a
  timeout. Nothing in the failure says the address was refused.
* `generic` with no `command`. It is admitted, leased and started, and then
  fails with "input.command is required".
* `generic` limits. `timeout_seconds: 0` is read as "not asked" and ignored.
  `timeout_seconds: 99999` is clamped to the platform's ceiling, with a
  WARNING in the worker's log that the caller never sees.
* Keys the worker fills with `setdefault` (`task_id`, `attempt_id`,
  `repository`, `resumed_from_checkpoint`, `lifecycle.py` around line 860).
  For these two profiles a value the caller sent takes precedence over the
  worker's. Neither runner reads those keys, so nothing is affected today.
  A declaration closes the gap anyway, because an undeclared key is refused
  at the door.

### What each runner reads

Read again from the runners' source on 2026-09-29. These tables list every
key `body()` reads from `ctx.payload`, and nothing else.
`generic.py` refuses `argv`, `script`, `env`, `command_line` and `shell` by
name. Once the profile declares, the API refuses those at 422 like any other
undeclared key.

**`browser`** (`agent_worker/runners/browser.py`):

| key | kind | bounds | required | runner's default, and why this bound |
|---|---|---|---|---|
| `url` | `url` (new) | http or https, public host (see question 2) | no; `url` or `actions` | none. The runner turns it into a leading `goto`. |
| `actions` | `list` (new) of `object` (new) | 0..200 entries | no; `url` or `actions` | `[]`. 200 is the runner's `MAX_ACTIONS`. |
| `timeout_ms` | integer | 1..300000 | no | 30000. Must be at least 1 because 0 disables Playwright's timeout. Five minutes is the most one action may wait. |
| `launch_timeout_ms` | integer | 1..180000 | no | 60000. The same reason for 1. A launch still waiting at three minutes means the pod is short of `/dev/shm`. |
| `viewport_width` | integer | 320..3840 | no | 1280. The viewport, and a full-page screenshot of it, are held in the pod's memory. |
| `viewport_height` | integer | 240..2160 | no | 900 |
| `user_agent` | string | none | no | Chromium's own; `""` also means Chromium's |
| `extract_text` | boolean | none | no | true: the final page's text is kept as `page.txt` |
| `screenshot` | boolean | none | no | true: a final full-page `final.png` is kept |

Each element of `actions` is an object whose `type` selects one of eight
fixed shapes. A key that the selected shape does not name is refused:

| `type` | fields (required in bold) | runner's defaults |
|---|---|---|
| `goto` | **`url`** (`url`), `wait_until` (string, one of `load`, `domcontentloaded`, `networkidle`, `commit`) | `wait_until` `load` |
| `click` | **`selector`** (string) | |
| `fill` | **`selector`** (string), `text` (string) | `text` `""` |
| `press` | **`selector`** (string), `key` (string) | `key` `Enter` |
| `wait_for` | **`selector`** (string), `timeout_ms` (integer 1..300000) | the task's `timeout_ms` |
| `wait` | `seconds` (number 0..60) | 1. Refusing a value above 60 replaces the runner's silent clamp. |
| `screenshot` | `name` (`filename`), `full_page` (boolean) | `screenshot-NNN.png`, `true` |
| `extract` | `selector` (string), `name` (`filename`) | `body`, `extract-NNN.txt` |

The runner writes both `name` fields through `RunnerContext.artifact_path`,
which keeps only the last path segment. That is the rule the existing
`filename` kind already states. The runner lower-cases `type`, so `"Goto"`
runs today. The declaration matches `type` exactly.

**`generic`** (`agent_worker/runners/generic.py`, `runners/limits.py`):

| key | kind | bounds | required | runner's default, and why this bound |
|---|---|---|---|---|
| `command` | string | one of `make`, `npm-build`, `npm-ci`, `npm-test`, `pytest`, `uv-sync` | **yes** | none. The runner fails without it. The list is `GENERIC_COMMANDS`. |
| `paths` | `list` (new) of `argument` (new) | 0..32 entries | no | none, so pytest runs the whole suite. 32 is `GenericCommand.max_arguments`. Only `pytest` reads it. |
| `target` | `argument` (new) | the runner's argument rule | no | `all`. Only `make` reads it. |
| `working_directory` | `argument` (new) | the runner's argument rule | no | the workspace root |
| `timeout_seconds` | number | 1..3600 | no | the profile's `timeout_seconds` (3600), which the worker exports as the ceiling |
| `grace_seconds` | number | 1..20 | no | `WorkerConfig.termination_grace_seconds` (20) |
| `max_stdout_bytes` | integer | 1..33554432 | no | `WorkerConfig.max_stdout_bytes` (32 MiB) |
| `max_stderr_bytes` | integer | 1..8388608 | no | `WorkerConfig.max_stderr_bytes` (8 MiB) |

**The four limits may only lower the platform's own.** Each declared ceiling
is the value the worker exports by default, so a request above it gets a 422
at submission. Today the runner accepts it and clamps it. The runner still
clamps to whatever the attempt's worker exports, because an operator may set
that lower through the environment. The minimum is 1 because the runner reads
0 or less as "not asked". `argument` is exactly what the runner's
`_check_argument` accepts: `^[A-Za-z0-9._][A-Za-z0-9._\-/]{0,255}$` and no
`..`. Whether the path exists and lies inside the workspace stays the
runner's check, because only the runner has the workspace.

### Question 1 (#218): `RunnerInput.kind` has no kind for a list

**Recommendation: add one `list` kind, whose bounds are its length and whose
elements are a declared `RunnerInput` (`items`).** It reuses the fields that
already exist. `minimum` and `maximum` become the length bounds and are
required, as they are for a number, for the same reason: without them a list
is unbounded until the task write fails. Each element is checked by the same
`check`, under the key `actions[3]`, so a refusal names the element that
failed.

For `paths` that is the whole addition, plus an `argument` kind for the
element. `argument` is the generic runner's argument rule, added as a kind for
the same reason `filename` is one: a regex field would be a second, more
general feature that one rule does not need. `target` and
`working_directory` use it as well.

`actions` needs one more addition, because its elements are objects in eight
shapes. The smallest form that still checks each element is **an `object`
kind with `variants`**: a mapping from the value of `type` to the fields that
shape takes. Each field is a `RunnerInput`, which gains `required`. A shape
refuses keys it does not name. It is not general: the discriminator is always
`type`, and there is no nesting beyond what `items` and `variants` compose.
That is enough for every input any runner reads today. Two further fields
serve keys that are not lists. `choices` (string only) covers `command` and
`wait_until`. `required` is also read for a profile's top-level keys, so a
`generic` task with no `command` is refused at the door.

**Alternatives considered, and why they were not chosen:**

* An opaque `json` kind, bounded by size. That is the current behaviour under
  another name. It checks no element, so a missing `selector` is still found
  after admission.
* A `browser_action` kind. It would put one runner's vocabulary into the kind
  list as code instead of as data, and the next runner with a list of shapes
  would need another kind.
* A full JSON Schema. It is far larger than any runner needs, and a caller
  could no longer read it from `describe()`.

**What `required` cannot say:** the browser runner needs `url` *or*
`actions`. This request leaves that check with the runner. That is a
decision for the owner, listed below.

### The `url` rule: does it need an allow-list or a scheme restriction?

**Not one of #218's three questions.** #218 asks three things, quoted
verbatim below under Question 2 and Question 3; whether `url` needs a scheme
or host restriction is not among them. It is this entry's own addition,
because declaring `url` as a kind at all raises it. A revision of this entry
mislabelled it "Question 2" and cited it as "#218's second question" in the
diff's own docstring; that was wrong on both counts and is fixed below and in
the diff.

**Recommendation: a scheme restriction and a public-host rule, checked at
submission. No host allow-list, and not https-only.** The check sits in one
place, `url_refusal` in the frozen module. It serves `url` and every `goto`
action's `url`. swarm-api, the bridge and the runner's `_check_url` all call
it.

**What the browser can reach today.** The pod's egress is
`kubernetes/network-policies/allow-egress.yaml`. It allows cluster DNS, the
metadata server at 169.254.169.254, the Private Google Access VIPs
(199.36.153.4/30 and 199.36.153.8/30) and the public internet. It excepts
every private range: RFC 1918, link-local, CGNAT and the cluster's pod and
service ranges. So the browser can reach:

1. **The GKE metadata server.** The pod needs it for Workload Identity, and
   the policy's own header says a NetworkPolicy cannot close it for one
   process in a pod. It mints the tenant GSA's token. What protects that
   token from a browser is that the server answers `/computeMetadata/v1/`
   only with a `Metadata-Flavor: Google` header. A navigation does not send
   that header. A cross-origin `fetch()` from page script that sets it
   triggers a CORS preflight, and the metadata server does not answer the
   preflight in a way that permits the request. The preflight behaviour is
   **not measured here**. The applying change should measure it with a
   browser task against the live cluster.
2. **The Google API VIPs.** These are authenticated APIs, so they are useless
   without a token.
3. **The public internet.** That is the browser profile's purpose. The
   policy's header says it "has to fetch whatever page the task names, which
   cannot be an allow-list".

**Why the submission check cannot be the SSRF control.** It sees only the
URL the caller typed. It cannot see redirects (a public page can answer
`302` to `http://169.254.169.254/...`), the page's subresources, requests
its script makes, or what a name resolves to when the pod asks. That last gap
includes DNS rebinding, and names such as `svc.namespace` that the pod's
search path expands into the cluster. Resolving the name at submission does
not close it, because the pod resolves it again later. **The control is the
network, plus the header requirement.** A check at the door is defence in
depth.

**What the door check still buys:**

* A private address becomes a 422 with the reason, instead of a timeout that
  never says the address was dropped.
* A URL with `user:password@` is never stored with the task, served by
  the API or shown in the UI. The runner would also echo it
  back as `final_url`.
* An address the rule can actually see, and that is not global, becomes an
  error at submission instead of a silent pass. **This is narrower than the
  first draft of this entry claimed** ("addresses that parse as public but
  are not become errors"): a security review on 2026-09-29 found that claim
  false, because the first draft's rule let the address it was supposed to
  catch avoid being parsed as that address at all -- see the four bypasses
  below, each of which reached `""` (accepted) under the original diff.

**A security review on 2026-09-29 found the rule below, as it stood, refused
by a different notion of "URL" than the browser that opens it.** Chromium
parses to the WHATWG URL Standard; `urlsplit` parses to RFC 3986. The two
disagree on backslash, on percent-encoding inside a host, and on non-ASCII
characters, and every disagreement is a bypass here: `url_refusal` sees a
value it does not recognise as the metadata host and returns `""`, while
Chromium normalises the same value to that host and opens it.

* `169.254.169.254\@example.com` -- `urlsplit` does not treat `\` as a netloc
  terminator (RFC 3986 does not, either), so the whole string, backslash
  included, becomes `parsed.hostname`. It fails no check the first draft
  had, and passes. Chromium treats `\` exactly like `/` in an authority, so
  it opens `169.254.169.254` with an ignored path.
* `metadata.google.interna%6cl` (`%6c` is a lower-case `l`) -- `urlsplit`
  does **not** percent-decode a host; `.hostname` returns the literal string
  with the `%6c` still in it, so the `.internal` suffix check never matches
  it and the loop over `_URL_REFUSED_SUFFIXES` passes. A browser host-parses
  it as the WHATWG standard requires (percent-decode, then IDNA), which
  yields `metadata.google.internal`, and opens it.
* `metadata.google.ｉｎｔｅｒｎａｌ` (full-width Unicode letters, U+FF49 etc.)
  and `169254169254｡` (U+3002 IDEOGRAPHIC FULL STOP, which IDNA/UTS46
  maps to `.`) -- neither is refused by anything in the first draft, which
  checked only ASCII punctuation and a handful of named suffixes. A
  browser's IDNA/UTS46 host processing folds full-width Unicode to its ASCII
  equivalent before it resolves, so both open the address they spell out
  once normalised.
* `kubernetes.default.svc` and `swarm-api.swarm-system.svc` -- refused today
  only by the "single-label host" rule below, and both have three labels, so
  they passed it. GKE's pod `resolv.conf` sets `ndots:5`: any name with
  fewer than five dots is tried against every search domain
  (`<namespace>.svc.cluster.local`, `svc.cluster.local`, `cluster.local`,
  ...) before the absolute name, and Kubernetes' own DNS resolves the
  `<service>.<namespace>.svc` short form through exactly that path. A
  single-label check does not see either name as a risk; the cluster's
  resolver does. **Revision history on this one bypass, because it changed
  twice:** the first draft's single-label rule missed it (three labels). A
  second draft (2026-09-29) replaced the single-label rule with "fewer than
  two dots," which caught `kubernetes.default.svc` (only by widening the net
  far past what the bypass needed) but ALSO refused every bare apex domain a
  browser task might target (`github.com`, one dot). **The owner declined
  that trade on 2026-09-29** and asked for the narrow fix instead: keep the
  single-label rule, and add `.svc` to the refused-suffix list next to
  `.internal`, `.local` and `.localhost`. That is what ships below. It also
  does not pretend to close the general `ndots:5` gap for an arbitrary
  short name -- see the paragraph after the rule for what does.

**The fix is one check before anything else runs, not four patches.** Every
bypass above turns on `url_refusal` accepting a host character, or a host
shape, that Chromium's own parser would not read as plain ASCII. So the
rule now normalises nothing and instead refuses anything that is not
already plain ASCII host syntax, before the rest of the checks run: a
backslash anywhere in the URL, and a host that is not `[a-z0-9.-]+` once
lower-cased -- which is refused whether it got there by a literal
backslash, a literal percent sign, or a literal non-ASCII character, so
those three bypasses close with one rule instead of three. The alternative
this entry considered -- IDNA/UTS46-normalise the host, then require
`[a-z0-9.-]+` of the *normalised* form, so a legitimately internationalised
domain still passes -- was not chosen: it accepts more real hosts, but it
also means the two representations (raw and normalised) must be kept in
step with whatever `agent_worker/runners/browser.py`'s own Chromium build
does, forever. Refusing raw non-ASCII outright means a caller whose target
is a real IDN sends the ASCII (punycode) form, which every such site
already answers to.

The rule, as written in the diff, now refuses, **in this order**:

* a URL over 2048 characters;
* a space, a control character, or any character outside 0x20-0x7E anywhere
  in the URL -- this alone is what closes the full-width and U+3002 bypass,
  because neither is ASCII;
* a backslash anywhere in the URL;
* any scheme but `http` and `https`;
* userinfo (`user:password@`);
* a host that, once lower-cased, is not `[a-z0-9.-]+` -- this is what closes
  the percent-encoded-host bypass, because `%` is not in that class, and it
  is what would also close a raw non-ASCII host if the character check above
  had not already caught it;
* a host whose last label does not start with a letter (`http://2852039166/`
  and `http://0xa9.254.169.254/`, which a browser reads as 169.254.169.254
  and `ipaddress` parses as neither; no public suffix starts with a digit);
* a host with an empty label (`a..b`, or a label produced by a leading or
  doubled `.`) -- added 2026-09-29 (minor, folded into this revision): the
  character-class check does not catch it, because `.` is itself allowed,
  and nothing downstream would otherwise notice a zero-length label;
* a host with a label made only of hyphens (`http://---.com/`) -- added
  2026-09-29 (minor): the previous message for this case ("ends in a
  number") was wrong and confusing, since a hyphen is not a digit; it now
  says plainly that an all-hyphen label is not a valid domain label;
* a host whose last label does not start with a letter, with two distinct
  messages now instead of one: a digit-led last label (`http://2852039166/`,
  `http://0xa9.254.169.254/`, which a browser reads as 169.254.169.254 and
  `ipaddress` parses as neither; no public suffix starts with a digit) says
  so; anything else that is not a letter (a hyphen, after the all-hyphen
  case above is already out of the way) says which character it found;
* **a single label** (`kubernetes`, `metadata`) -- restored 2026-09-29. A
  second draft of this rule (2026-09-29) had widened this to "fewer than two
  dots," to also catch the `.svc` bypass below; the owner declined that
  trade the same day, because it refused every bare apex domain
  (`github.com`, `example.com`) a browser task's own purpose is to reach.
  The single-label form is restored, unchanged from the first draft, and
  costs nothing a real page address would ever need;
* any name under `.internal`, `.local`, `.localhost` or **`.svc`** (new
  2026-09-29, for `kubernetes.default.svc` / `swarm-api.swarm-system.svc` --
  see the bypass write-up above), checked after the ASCII and single-label
  rules, so a suffix check can no longer be the only thing standing between
  a bypass and a pass;
* a host that is an IP address and, after unwrapping an IPv4-mapped IPv6
  address or one of five other IPv6 forms that embed an IPv4 address --
  NAT64 well-known (`64:ff9b::/96`, RFC 6052), **NAT64 local-use
  (`64:ff9b:1::/48`, RFC 8215, new 2026-09-29)**, 6to4 (`2002::/16`, RFC
  3056), **SIIT / "IPv4-translated" (`::ffff:0:0:0/96`, RFC 6052 section
  2.1, new 2026-09-29)** and **IPv4-compatible, deprecated but still a
  parseable literal (`::/96`, new 2026-09-29)** -- down to the IPv4 address
  each embeds, is not global by this module's own explicit list of refused
  networks (below) -- **not `ipaddress.*.is_global`**, whose own membership
  changed between Python 3.11 and 3.13 (the CGNAT block and `192.0.0.0/24`
  both moved category more than once across that span). An explicit, named
  list is what a reviewer can diff against RFC 1918, 5735, 4291 and 6890
  directly, and it does not move under this module on a Python upgrade the
  platform did not choose for this reason;
* the two Google VIP ranges, which are global addresses by any definition
  but are refused anyway (see above).

**This is not the general `ndots:5` fix, and does not claim to be.** The
`.svc` suffix closes the one specific, named bypass above -- a fixed string,
not an open-ended shape -- but GKE's search path can in principle try any
name under five dots against every search domain, and nothing in this
function sees what the pod's resolver eventually does with a name it
accepted. What actually closes that gap: the worker's NetworkPolicy (which
does not depend on what a name resolves to at all), the pod's own DNS
config -- **tracked as #341: set `ndots:1` and drop search domains**, which
removes the search-path trial rather than trying to enumerate every name
shape it could produce -- and the planned `context.route` guard (see
*Preconditions for the applying PR*). This entry's door check is one layer
among those three, not a substitute for the other two.

**Underscore.** A host carrying one (`a_b.example.com`) is refused by the
same `[a-z0-9.-]+` character class as any other character outside it, with
no separate rule needed: RFC 952/1035 never allowed an underscore in a
hostname label, so refusing it costs nothing a valid page address needs.

**Required for the applying PR: a test table that runs every refusal above,
and every bypass this review found, through a real WHATWG URL parser, not
only `urlsplit`.** A test that constructs its expected value with `urlsplit`
and checks `url_refusal` against that same `urlsplit` output proves nothing
about Chromium; it is the rule re-checking itself in a mirror. The applying
PR's test table parses each case with a WHATWG-conformant parser -- for
example Node's own `URL` (the runtime the plugin already ships with), driven
from the test as a subprocess, or a maintained Python WHATWG binding if one
is vendored -- and asserts `url_refusal`'s verdict agrees with what that
parser resolves the host to, for every case in this section, including the
four original bypasses, the `.svc` bypass, and all six embedded-IPv4 forms
(IPv4-mapped, NAT64 well-known, NAT64 local-use, 6to4, SIIT, IPv4-compatible).

**Why not https-only, decided.** The scheme is not the SSRF vector. The
metadata server and every private address are plain http either way, and
the rule above refuses them by host. The runner already sets
`ignore_https_errors=False`, so an https page with a bad certificate still
fails. Refusing http would refuse legitimate plain-http targets and close no
path. **The owner confirmed on 2026-09-29: no https-only rule.** It is not
listed as an open decision below any more.

**IDN, decided.** A caller whose real target is an internationalised domain
sends its ASCII (punycode) form; this entry does not add a separate IDN
allowance or normalisation path. **The owner confirmed on 2026-09-29:
accepted as written**, alongside declining the https-only rule above.

**Why no allow-list.** A platform-wide list contradicts the profile's purpose
and the NetworkPolicy's stated design. A per-tenant allow-list is tenant
configuration, not catalogue. If it is ever wanted, it belongs with the
tenant's registration, not in `RunnerInput`.

**Not in this request, and recommended as its own worker issue:** a
request-level guard in the runner, `context.route("**/*", ...)`, that aborts
any request, including redirects and subresources, whose host fails
`url_refusal`. It still cannot see DNS answers. That makes it the second
layer behind the network, not a replacement for it. **File it as its own
worker issue rather than folding it into this request**, because
`context.route` is the only layer in this entire path that sees a redirect
or a page's subresources; the door check above never does, whichever way its
own bugs are fixed. Until that issue lands, a page reachable at a refused URL
that itself redirects, or loads a subresource from one, is not caught by
anything this request adds.

### Question 2 (#218): a key named `command`

**#218's own text**, read directly on 2026-09-29 (an earlier draft of this
entry could not read the issue and reconstructed this question from where
this repository quotes it; that reconstruction is replaced here): "A
declared input NAMED `command` reads as invariant 10 relaxed, although it
names a catalogue entry, not an argv. `FORBIDDEN_CALLER_FIELDS` in
`swarm_api/validation.py` and `_NEVER` in
`tests/unit/mcp/test_runner_inputs.py` both list `command`." Request 25
recorded the same concern in its own words before #218 was filed: "a
declared input named `command` would read as invariant 10 relaxed although
it names a catalogue entry, not an argv."

**Recommendation: keep the name, and narrow the guard rather than remove
it.** `command` is what every existing caller sends, including the example
in `docs/workflows.md`, the Submit form and the runner's own error text. The
declaration makes the difference #218 asks about visible instead of hiding
it: `choices` is the closed list of catalogue names, and `describe()`
renders it as `string, one of make | npm-build | ...`. A reader sees a menu,
not an argv. Renaming it to `catalogue_command` would break every caller to
address something a reviewer can already see.

**What this means for the two guards #218 names, checked directly against
their source on 2026-09-29:**

* **`_NEVER` in `tests/unit/mcp/test_runner_inputs.py` (line 52) lists
  `command`, and `test_only_the_mock_declares_inputs_and_no_declaration_
  names_execution_detail` (line ~314) asserts, for **every** profile in
  `RUNNER_PROFILES`, that none of its declared keys intersect `_NEVER`. That
  is invariant 10's own test, and it is written to fail the moment any
  profile declares `command` -- this request must say so explicitly, because
  the diff below makes it fail as written. **This entry does not ask for
  `command` to be removed from `_NEVER`.** It asks for one narrow, named
  exemption: a declared key named `command` is allowed **only** for the
  `generic` profile, and **only** when its `RunnerInput` is `kind="string"`
  with `choices` exactly equal to `GENERIC_COMMANDS`. Any other profile
  declaring `command`, any other kind, or a `command` whose `choices` is not
  exactly that closed list stays refused by the same guard it is refused by
  today. The applying PR narrows the test's assertion to express that
  exemption in code, rather than deleting `command` from `_NEVER` or
  weakening the assertion for every profile.
* **`FORBIDDEN_CALLER_FIELDS` in `swarm_api/validation.py` (line 41) also
  lists `command`, but is unaffected by this request and needs no change.**
  It refuses fields on the **top-level task body** (`image`, `command`,
  `resources`, and the rest of invariant 10's list, checked in
  `swarm_api/main.py` before a runner profile is even resolved). A
  `generic` task's declared `command` lives under `input`, a separate
  namespace `FORBIDDEN_CALLER_FIELDS` does not read and has never read --
  `mock`'s existing declarations already put keys under `input` today
  without needing a change there. Declaring `input.command` for `generic`
  touches neither this list nor its call site.

### Question 3 (#218): should the plugin's bridge send inputs to a profile that has not declared?

**#218's own text**, read directly on 2026-09-29: "Once declared, the
plugin's bridge reads the same field, so `swarm_dispatch` could send a
browser task's `actions` for the first time. That is a new plugin
capability, not only a narrowing." An earlier draft of this entry could not
read #218 and reconstructed this question instead from the places in this
repository that quote it -- request 25's amendment ("Letting it send a
browser task's `actions` is #218's third question"), the header of
`apps/swarm-mcp/swarm_mcp/profiles.py`, and `check_inputs`'s docstring. Read
directly, the issue's wording matches all three; no further check against
the issue's text is needed.

**Recommendation: no. Keep the send policy, and declare instead.** This
request fixes #220's failure by giving `browser` a declaration. No bridge
exception is needed. The bridge sends a key only when a declaration names it,
because the declaration gives `--input` a kind to type the value by, and it
lets the bridge refuse a bad value before anything travels. Letting it send
unchecked keys to a `None` profile would forward, from the plugin, exactly the
unchecked input this request exists to close.

Once `browser` declares, `swarm_dispatch`'s `inputs` object carries `url`
and `actions` as JSON with no change. `swarm dispatch --input` needs one line:
`parse_input_flags` keeps `string` and `filename` values as typed text, and
must keep `url` and `argument` values the same way. Otherwise
`--input target=123` would be parsed as the number 123 and refused. A list is
already read as JSON (`--input 'actions=[{"type":"screenshot"}]'`).

With the diff below applied there is no `None` profile left, so the policy
stops being a live question.

### The requested change

The exact diff against `apps/common/swarm_common/profiles.py` at `d4a2352`.
**Not applied.** Revised 2026-09-29 after a security review; this version
re-verified with `git apply --check` against the current file (still
`d4a2352`; nothing has touched `profiles.py` since) and with a standalone
run of `url_refusal` and `check_inputs` against every case this entry names,
including the four bypasses -- both done outside this repository's own test
suite, since nothing here runs `pytest`. The applying PR still owes the real
test files listed under *Downstream restatements* and the WHATWG-parser
table above; this only proves the diff parses, applies and behaves as this
entry claims.

* **Kinds:** `url`, `argument`, `list`, `object`, and **`header`** (added in
  this revision, for `user_agent`): printable ASCII only, bounded like a
  list, by length.
* **New `RunnerInput` fields:** `choices`, `required`, `items`, `variants`.
  `required` on a list's `items` is now refused in `__post_init__`: an
  element of a list is always present, so the flag had nothing to say on one.
* **`string` may now also give a `maximum`** (a length bound in characters,
  `minimum` defaulting to 0), for `selector`, `text` and `key`: 4 KiB each,
  the runner's own content otherwise unconstrained.
* **A shared rule:** `url_refusal`, rewritten in this revision to refuse
  before parsing rather than after: a backslash or non-ASCII character
  anywhere, and a host that is not `[a-z0-9.-]+` once lower-cased, checked
  before the scheme, the dot count or the suffix list. See the entry's prose
  above for the four bypasses this closes and the explicit refused-network
  lists that replace `is_global`.
* **`browser` and `generic` declare** the tables above. `generic`'s
  `command` is `RunnerProfile.__post_init__`'s one exemption from `_NEVER`,
  enforced in code, not only in prose: a string whose `choices` are exactly
  `_GENERIC_COMMANDS`, for `generic` only.
* **`generic`'s `timeout_seconds` ceiling is read off the profile itself**
  (`_GENERIC_PROFILE.timeout_seconds`, built before `_GENERIC_INPUTS` and
  attached to `RUNNER_PROFILES["generic"]` with `dataclasses.replace`),
  not restated as a second literal.
* **`required` is enforced at the top level** by `check_inputs`.
* **The field returns to the type request 25 was accepted with.**
  `RunnerProfile.inputs` becomes `Mapping[str, RunnerInput]` with no `None`,
  and `check_inputs` loses its `None` branch. That retires the amendment.
* **`describe()` prints whole-number bounds in full** (`1..33554432`, not
  `:g`'s `1..3.35544e+07`). The mock's bounds render unchanged.
* **The article** reads "a url".

CR 28 (`issue` for `claude-code` and `codex`), accepted and not yet applied
on `main`, touches neither these lines nor these kinds.

```diff
--- a/apps/common/swarm_common/profiles.py
+++ b/apps/common/swarm_common/profiles.py
@@ -19,12 +19,15 @@
 
 from __future__ import annotations
 
+import ipaddress
 import math
+import re
 from collections.abc import Mapping
-from dataclasses import dataclass, field
+from dataclasses import dataclass, field, replace
 from enum import Enum
 from types import MappingProxyType
 from typing import Any
+from urllib.parse import urlsplit
 
 
 class Backend(str, Enum):
@@ -98,13 +101,35 @@
 # choose the model a `claude-code` agent runs. So what may be sent is declared
 # per profile, here, once. swarm-api refuses anything else at submission and
 # the plugin's bridge refuses it sooner; both read this, and neither keeps a
-# table of its own. Two profiles, `browser` and `generic`, have not declared
-# yet (#218), and the API bounds what is sent to them by size alone.
+# table of its own. Every profile declares: `browser` and `generic` last, by
+# contract request 32 (#218), which added the kinds their inputs needed.
 
 #: The kinds an input can be. `filename` is a bare file name with no
 #: directory: the worker keeps only the last path segment of an artifact name
 #: (`RunnerContext.artifact_path`), so `../x` would quietly become `x`.
-INPUT_KINDS = ("number", "integer", "boolean", "string", "filename")
+#:
+#: Added by contract request 32 (#218), for the two runners whose work IS
+#: their input:
+#:
+#: * `url`: an http or https URL a browser may open. See `url_refusal`.
+#: * `argument`: a value the generic runner appends to a catalogue argv, or
+#:   runs in -- `_ARGUMENT_SAFE` and the `..` refusal in
+#:   `agent_worker/runners/generic.py`, restated because the catalogue cannot
+#:   import the worker. Existence and "inside the workspace" stay the
+#:   runner's: only it has the workspace.
+#: * `list`: a JSON array. Its BOUNDS ARE ITS LENGTH, both required, as a
+#:   number's are; every element is `items`, and `items` may not itself be
+#:   `required` -- an element of a list is always present, so that flag on an
+#:   element has nothing to say.
+#: * `object`: a JSON object in one of the fixed shapes `variants` names,
+#:   chosen by its `type` key. A key its shape does not name is refused, as a
+#:   key a profile does not declare is.
+#: * `header`: a string that is, or could become, an HTTP header value:
+#:   printable ASCII only (0x20-0x7E), which by construction rules out CR and
+#:   LF and every other control character, so a caller cannot use it to
+#:   inject a second header. `maximum` is required, as it is for a list: the
+#:   bound is the string's length in characters.
+INPUT_KINDS = ("number", "integer", "boolean", "string", "filename", "url", "argument", "list", "object", "header")
 
 #: The signed 64-bit range, which is what Firestore stores an integer in.
 #: Python reads a JSON integer of any length, so an integer input whose bounds
@@ -113,16 +138,282 @@
 INT64_MIN = -(2**63)
 INT64_MAX = 2**63 - 1
 
+#: `argument`: what `generic._ARGUMENT_SAFE` accepts, restated because the
+#: catalogue cannot import the worker. No leading dash, so no value becomes a
+#: flag; no leading slash; 256 characters at most.
+_ARGUMENT = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._\-/]{0,255}")
+
+#: The longest URL a browser task may name. Chromium's own limit is far
+#: larger; a task's URL is stored with the task and shown with it, and past
+#: this it is not a page address but a payload.
+_URL_MAX_CHARS = 2048
+
+#: A host, once `urlsplit` has extracted it and this module has lower-cased
+#: it, must be exactly this: letters, digits, `.` and `-`. Nothing else is
+#: plain ASCII host syntax, so this single check is what closes a
+#: percent-encoded host (`interna%6cl`), a host carrying a literal backslash,
+#: and a raw non-ASCII host at once -- see `url_refusal` and the entry's own
+#: prose for the three bypasses a security review found here on 2026-09-29.
+_HOST_CHARS = re.compile(r"[a-z0-9.-]+")
+
+#: Every network `url_refusal` refuses a *global* IPv4 address in, besides
+#: RFC 1918 and the rest of the ranges `ipaddress` itself would already
+#: refuse as non-global. Chosen as an explicit, named list instead of relying
+#: on `ipaddress.IPv4Address.is_global`, because that property's own
+#: membership is not pinned across the versions this platform runs: the
+#: CGNAT block (100.64.0.0/10) and 192.0.0.0/24 have both changed category
+#: in `ipaddress` between Python 3.11 and 3.13. An explicit list is what a
+#: reviewer can diff against RFC 1918, 5735 and 6890 directly, and it does
+#: not move under this module on a Python upgrade the platform did not make
+#: for this reason.
+_URL_REFUSED_V4_NETWORKS = (
+    ipaddress.ip_network("0.0.0.0/8"),  # "this network" (RFC 791)
+    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
+    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT, RFC 6598
+    ipaddress.ip_network("127.0.0.0/8"),  # loopback
+    ipaddress.ip_network("169.254.0.0/16"),  # link-local, and the metadata server
+    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
+    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
+    ipaddress.ip_network("192.0.2.0/24"),  # documentation (TEST-NET-1)
+    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
+    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
+    ipaddress.ip_network("198.51.100.0/24"),  # documentation (TEST-NET-2)
+    ipaddress.ip_network("203.0.113.0/24"),  # documentation (TEST-NET-3)
+    ipaddress.ip_network("224.0.0.0/4"),  # multicast
+    ipaddress.ip_network("240.0.0.0/4"),  # reserved
+    ipaddress.ip_network("255.255.255.255/32"),  # limited broadcast
+)
+
+#: The IPv6 equivalent of `_URL_REFUSED_V4_NETWORKS`, for a literal IPv6 host
+#: that is neither IPv4-mapped nor a NAT64/6to4 embedding (both unwrapped to
+#: an IPv4 address and checked against the list above instead; see
+#: `_embedded_v4`).
+_URL_REFUSED_V6_NETWORKS = (
+    ipaddress.ip_network("::1/128"),  # loopback
+    ipaddress.ip_network("::/128"),  # unspecified
+    ipaddress.ip_network("100::/64"),  # discard-only, RFC 6666
+    ipaddress.ip_network("2001:db8::/32"),  # documentation
+    ipaddress.ip_network("fc00::/7"),  # unique local
+    ipaddress.ip_network("fe80::/10"),  # link-local
+    ipaddress.ip_network("ff00::/8"),  # multicast
+)
+
+#: Private Google Access, which the worker's NetworkPolicy opens
+#: (kubernetes/network-policies/allow-egress.yaml, rule 3). These are global
+#: addresses by any definition, so they are refused explicitly rather than by
+#: `is_global`: the pod can reach them, but they are authenticated Google
+#: APIs, useless to a browser task without a token, and not what "the public
+#: internet" is meant to include.
+_URL_REFUSED_NETWORKS = (
+    ipaddress.ip_network("199.36.153.4/30"),
+    ipaddress.ip_network("199.36.153.8/30"),
+)
+
+#: Every IPv6 range that EMBEDS an IPv4 address, so a refused v4 address
+#: reachable through any of them would otherwise pass
+#: `_URL_REFUSED_V6_NETWORKS` unseen. IPv4-mapped (`::ffff:a.b.c.d`) is
+#: handled separately by `ipaddress.IPv6Address.ipv4_mapped`; the rest are
+#: unwrapped by `_embedded_v4`:
+#: * `64:ff9b::/96` -- NAT64, RFC 6052 (well-known prefix).
+#: * `64:ff9b:1::/48` -- NAT64, RFC 8215 (local-use prefix; the embedding
+#:   follows RFC 6052 section 2.2's PL48 layout: 48-bit prefix, 16 bits of
+#:   v4, an 8-bit zero field, 16 more bits of v4, 40-bit suffix).
+#: * `2002::/16` -- 6to4, RFC 3056.
+#: * `::ffff:0:0:0/96` -- "IPv4-translated", RFC 6052's SIIT form
+#:   (`::ffff:0:a.b.c.d`; note the extra `:0:` before the address, which is
+#:   what distinguishes it from IPv4-mapped).
+#: * `::/96` -- IPv4-compatible, deprecated (RFC 4291 says so; RFC 6540 says
+#:   not to originate or accept it) but still a parseable literal
+#:   (`::a9fe:a9fe`), so still refused here explicitly rather than assumed
+#:   gone.
+_URL_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
+_URL_NAT64_LOCAL_PREFIX = ipaddress.ip_network("64:ff9b:1::/48")
+_URL_6TO4_PREFIX = ipaddress.ip_network("2002::/16")
+_URL_SIIT_PREFIX = ipaddress.ip_network("::ffff:0:0:0/96")
+_URL_IPV4_COMPATIBLE_PREFIX = ipaddress.ip_network("::/96")
+
+#: Names that are never a public page: GKE's metadata server is
+#: `metadata.google.internal`; `.local`/`.localhost` never leave the host or
+#: the cluster (`cluster.local`); and `.svc` is the short, no-FQDN-suffix
+#: form Kubernetes' own DNS resolves for a Service
+#: (`<service>.<namespace>.svc`, e.g. `kubernetes.default.svc`), which the
+#: pod's search path completes to `.svc.cluster.local` for any name under
+#: five dots (`ndots:5`) -- added 2026-09-29, after the owner found the
+#: dot-count rule this replaced still admitted it. See `url_refusal`'s
+#: docstring for what actually closes the general search-path gap; this
+#: suffix closes only the specific, well-known `.svc` shorthand.
+_URL_REFUSED_SUFFIXES = (".internal", ".local", ".localhost", ".svc")
+
 #: How much of a refused value a refusal repeats. A caller who sent three
 #: hundred digits needs the bound, not the digits back.
 _SHOWN_VALUE_CHARS = 40
 
 
+def _bound(value: float) -> str:
+    """`33554432`, not `3.35544e+07`: a caller copies a bound, and `:g` rounds it."""
+    return str(int(value)) if float(value).is_integer() else f"{value:g}"
+
+
+def _embedded_v4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
+    """The IPv4 address a NAT64, 6to4, SIIT or IPv4-compatible address
+    embeds, or None. IPv4-mapped is handled by the caller
+    (`address.ipv4_mapped`) and never reaches here."""
+    packed = address.packed
+    if address in _URL_NAT64_PREFIX:
+        return ipaddress.IPv4Address(packed[12:16])
+    if address in _URL_NAT64_LOCAL_PREFIX:
+        # RFC 6052 section 2.2, PL48: prefix(6 bytes) + v4-hi(2) + u(1, zero)
+        # + v4-lo(2) + suffix(5). The `u` byte at packed[8] is skipped.
+        return ipaddress.IPv4Address(bytes([packed[6], packed[7], packed[9], packed[10]]))
+    if address in _URL_6TO4_PREFIX:
+        return ipaddress.IPv4Address(packed[2:6])
+    if address in _URL_SIIT_PREFIX:
+        return ipaddress.IPv4Address(packed[12:16])
+    if address in _URL_IPV4_COMPATIBLE_PREFIX:
+        return ipaddress.IPv4Address(packed[12:16])
+    return None
+
+
+def url_refusal(value: str) -> str:
+    """Why `value` is not a URL a browser task may open, or "" when it is.
+
+    One home for the rule, so swarm-api, the plugin's bridge and the browser
+    runner (`_check_url`, which today checks the scheme and that there is a
+    host) give one answer. Not one of #218's three questions -- declaring
+    `url` as a kind at all raises it; see the entry's prose in
+    docs/contract-change-requests.md for why, and for four bypasses a
+    security review found and closed here on 2026-09-29.
+
+    THIS IS NOT THE SSRF CONTROL, AND CANNOT BE. It sees the URL a caller
+    typed, never the page's redirects, its subresources, its script's
+    requests, or what a name resolves to when the pod asks. The control is the
+    network: the worker's NetworkPolicy drops every private range but the
+    metadata server, and the metadata server answers only a request carrying
+    `Metadata-Flavor: Google`, which a navigation does not send. What this
+    buys is a 422 naming the reason, instead of a task that waits out
+    `timeout_ms` against an address the network silently drops (Dataplane V2
+    drops; the sender sees a timeout), and a URL with a password in it that
+    is never stored with the task, served by the API or shown in the UI.
+
+    Refuses rather than normalises. Every check below runs on the raw string
+    or on `urlsplit`'s own view of it: a host that is not already plain ASCII
+    (`[a-z0-9.-]+` once lower-cased) is refused outright rather than
+    IDNA/UTS46-normalised and re-checked, because a normalising rule has to
+    track whatever WHATWG host-parsing Chromium does, forever, while a
+    refusing rule only has to be a subset of what Chromium accepts. A caller
+    whose real target is an internationalised domain sends its ASCII
+    (punycode) form, which the site already answers to -- the owner accepted
+    this as the rule on 2026-09-29 (no separate IDN allowance).
+
+    An underscore is refused by the same `[a-z0-9.-]+` check as any other
+    character outside that set, with no separate rule: RFC 952/1035 do not
+    allow one in a hostname label at all (some internal DNS -- SRV records,
+    `_service._proto.name` -- uses one anyway, which is one more reason a
+    browser task should not be handed a host carrying one).
+
+    WHAT THIS DOES NOT DO, as of the owner's decision on 2026-09-29: it does
+    not refuse a host by dot count. An earlier revision refused any non-IP
+    host with fewer than two dots, which caught `kubernetes.default` and
+    `swarm-api.swarm-system` but also every bare apex domain a browser task
+    might legitimately target (`github.com`, `example.com` both have exactly
+    one dot) -- for a browser profile, refusing those is refusing the
+    profile's main use. It was also incomplete on its own terms: a three-label
+    name ending in `.svc` (`kubernetes.default.svc`) has two dots and was
+    never caught by it either. The general gap -- GKE's `ndots:5` pod
+    resolver tries every search domain before the absolute name for anything
+    under five dots -- is NOT closed by any check in this function, and
+    cannot be from here: it sees the string the caller sent, never what the
+    pod's resolver does with it. The real controls are the worker's
+    NetworkPolicy (which does not depend on what a name resolves to), the
+    pod's own DNS config (tracked as #341: set `ndots:1` and drop search
+    domains, which removes the search-path trial entirely rather than
+    guessing at every name shape it could produce), and the planned
+    `context.route` guard (see the entry's *Preconditions*). What this
+    function still refuses is the single-label case (`kubernetes`,
+    `metadata`), which resolves only through the cluster's search path and
+    is never a real page address, and the specific `.svc` shorthand below,
+    which is the one case named in the review that is also a fixed, known
+    string rather than an open-ended shape.
+    """
+    if len(value) > _URL_MAX_CHARS:
+        return f"it is longer than {_URL_MAX_CHARS} characters"
+    if any(ch < " " or ch == "\x7f" or ord(ch) > 0x7E for ch in value):
+        return "it contains a space, a control character or a non-ASCII character"
+    if "\\" in value:
+        return "it contains a backslash, which a browser treats as a host or path separator"
+    try:
+        parsed = urlsplit(value)
+        parsed.port  # noqa: B018 -- raises ValueError on a port out of range
+    except ValueError:
+        return "it is not a URL"
+    if parsed.scheme not in ("http", "https"):
+        return "only http and https are opened"
+    if parsed.username is not None or parsed.password is not None:
+        return "it carries credentials, which would be stored with the task and shown with it"
+    host = (parsed.hostname or "").rstrip(".")
+    if not host:
+        return "it has no host"
+    # An IP LITERAL IS CHECKED BEFORE THE HOST-CHARACTER RULE, not after: an
+    # IPv6 literal's `hostname` is unbracketed and colon-bearing
+    # (`64:ff9b::808:808`), which `_HOST_CHARS` never matches, and `ipaddress`
+    # itself already rejects a backslash, a percent sign or a non-ASCII
+    # character in an address -- there is nothing left for a second charset
+    # check to catch there.
+    try:
+        address = ipaddress.ip_address(host)
+    except ValueError:
+        address = None
+    if address is None:
+        if not _HOST_CHARS.fullmatch(host):
+            return (
+                "its host is not letters, digits, '.' and '-' once lower-cased -- a "
+                "browser's own host parsing accepts more than this, including a "
+                "percent-encoded or backslash-bearing host, and this module refuses "
+                "rather than reproduces it"
+            )
+        labels = host.split(".")
+        if any(label == "" for label in labels):
+            return "its host has an empty label ('..' in it, or it starts or ends with '.')"
+        if any(label.strip("-") == "" for label in labels):
+            return "its host has a label made only of hyphens, which is not a valid domain label"
+        # A browser reads a host whose last label is a number (`2852039166`,
+        # `0xa9.254.169.254`) as an IPv4 address in another notation, which
+        # `ip_address` does not parse. No public suffix starts with a digit.
+        if labels[-1][:1].isdigit():
+            return "its host ends in a number, which a browser reads as an address"
+        if not labels[-1][:1].isalpha():
+            return f"its host's last label starts with {labels[-1][:1]!r}, not a letter"
+        # SINGLE LABEL ONLY, not a general dot-count rule: see the docstring
+        # above for why the broader rule this replaced was both too costly
+        # (it refused bare apex domains) and still incomplete.
+        if len(labels) < 2:
+            return "its host is a single label, which only the cluster's search path resolves"
+        if host.endswith(_URL_REFUSED_SUFFIXES):
+            return "its host is a cluster or node-local name"
+        return ""
+    if isinstance(address, ipaddress.IPv6Address):
+        if address.ipv4_mapped is not None:
+            address = address.ipv4_mapped
+        else:
+            embedded = _embedded_v4(address)
+            if embedded is not None:
+                address = embedded
+    if isinstance(address, ipaddress.IPv4Address):
+        if any(address in net for net in _URL_REFUSED_V4_NETWORKS) or any(
+            address in net for net in _URL_REFUSED_NETWORKS
+        ):
+            return "its host is not a public address"
+    elif any(address in net for net in _URL_REFUSED_V6_NETWORKS):
+        return "its host is not a public address"
+    return ""
+
+
 class InputRefused(ValueError):
     """An input a profile does not accept, and why.
 
     `key` is the first key refused and `keys` every one; `expected` is the
-    declared bound a value broke, or None when the key is not declared at all.
+    declared bound a value broke, or None when the key is not declared at all
+    or is required and was not sent.
     The message names the key and the bound, and never repeats a value longer
     than it has to.
     """
@@ -165,18 +456,73 @@
     #: the platform would read it as instead -- the mock's exit codes 77, 78
     #: and 143. Pairs, not a dict, so the declaration stays hashable.
     refused: tuple[tuple[Any, str], ...] = ()
+    #: A `string` must be one of these, when any are named: a name from a
+    #: catalogue the runner owns, such as the generic runner's commands.
+    choices: tuple[str, ...] = ()
+    #: The caller must send this key. Read for a profile's keys and for an
+    #: object's fields. Refused on a list's `items`: an element of a list is
+    #: always present, so `required` on it has nothing to say.
+    required: bool = False
+    #: What each element of a `list` must be.
+    items: RunnerInput | None = None
+    #: An `object`'s shapes: the value of its `type` key, to the fields that
+    #: shape takes besides `type`. Excluded from the hash, as
+    #: `RunnerProfile.inputs` is; frozen read-only below.
+    variants: Mapping[str, Mapping[str, RunnerInput]] | None = field(default=None, hash=False)
 
     def __post_init__(self) -> None:
         if self.kind not in INPUT_KINDS:
             raise ValueError(f"input kind {self.kind!r} is not one of {INPUT_KINDS}")
         numeric = self.kind in ("number", "integer")
-        if not numeric and (self.minimum is not None or self.maximum is not None or self.refused):
-            raise ValueError(f"a {self.kind} input has no bounds; only a number or an integer does")
-        if numeric and (self.minimum is None or self.maximum is None):
+        #: `list` and `header` MUST give both bounds, like a number: without
+        #: them a list or a header string is unbounded until the task write
+        #: fails. `string` MAY give a `maximum` -- a length bound in
+        #: characters, for keys such as `selector`, `text` and `key`, whose
+        #: content the runner does not otherwise constrain -- and when it
+        #: does, an omitted `minimum` defaults to 0 rather than being
+        #: required, because most bounded strings have no meaningful floor.
+        strictly_bounded = numeric or self.kind in ("list", "header")
+        length_boundable = strictly_bounded or self.kind == "string"
+        if not length_boundable and (self.minimum is not None or self.maximum is not None):
             raise ValueError(
+                f"a {self.kind} input has no bounds; only a number, a list, a header or a "
+                "string does"
+            )
+        if not numeric and self.refused:
+            raise ValueError(f"a {self.kind} input refuses no values; only a number does")
+        if strictly_bounded and (self.minimum is None or self.maximum is None):
+            raise ValueError(
                 f"a {self.kind} input declares both a minimum and a maximum; without "
-                "one, a JSON number of any size passes and fails at the store instead"
+                "one, a JSON number -- or a string, for a header -- of any size passes "
+                "and fails at the store instead"
             )
+        if self.kind == "string" and self.maximum is not None and self.minimum is None:
+            object.__setattr__(self, "minimum", 0.0)
+        if self.kind in ("list", "header") and not (
+            float(self.minimum).is_integer() and float(self.maximum).is_integer() and self.minimum >= 0
+        ):
+            raise ValueError(f"a {self.kind}'s bounds are its length: whole numbers, from 0")
+        if self.choices and self.kind != "string":
+            raise ValueError(f"a {self.kind} input has no choices; only a string does")
+        if (self.kind == "list") != isinstance(self.items, RunnerInput):
+            raise ValueError("a list input names what its elements are, and nothing else does")
+        if self.kind == "list" and self.items is not None and self.items.required:
+            raise ValueError(
+                "a list's items are always present; `required` on them is refused"
+            )
+        if (self.kind == "object") != bool(self.variants):
+            raise ValueError("an object input names its shapes, and nothing else does")
+        if self.variants:
+            frozen: dict[str, Mapping[str, RunnerInput]] = {}
+            for shape, fields in self.variants.items():
+                for name, declared in fields.items():
+                    if name == "type" or not isinstance(declared, RunnerInput):
+                        raise ValueError(
+                            f"shape {shape!r}: field {name!r} must be a RunnerInput, "
+                            "and `type` is the shape's name, not a field"
+                        )
+                frozen[shape] = MappingProxyType(dict(fields))
+            object.__setattr__(self, "variants", MappingProxyType(frozen))
         for bound in (self.minimum, self.maximum):
             if bound is not None and not math.isfinite(bound):
                 raise ValueError("an input's bounds must be finite numbers")
@@ -197,10 +543,16 @@
     def describe(self) -> str:
         """`integer 1..255 except 77, 78, 143`: the kind and the bound, as a caller reads it."""
         # `__post_init__` gives a number both bounds and anything else neither.
+        if self.kind == "list" and self.items is not None:
+            return f"list of {_bound(self.minimum)}..{_bound(self.maximum)}, each {self.items.describe()}"
+        if self.variants:
+            return f"object, `type` one of {' | '.join(self.variants)}"
         if self.minimum is not None and self.maximum is not None:
-            text = f"{self.kind} {self.minimum:g}..{self.maximum:g}"
+            text = f"{self.kind} {_bound(self.minimum)}..{_bound(self.maximum)}"
         else:
             text = self.kind
+        if self.choices:
+            text += f", one of {' | '.join(self.choices)}"
         if self.refused:
             text += " except " + ", ".join(str(value) for value, _ in self.refused)
         return text
@@ -208,7 +560,7 @@
     def check(self, key: str, value: Any) -> Any:
         """`value`, normalised (an integral float becomes an int), or InputRefused."""
         expected = self.describe()
-        article = "an" if expected[0] in "aeiou" else "a"
+        article = "an" if expected[0] in "aeio" else "a"  # "a url"
         wanted = f"input {key!r} must be {article} {expected}"
 
         def refuse(detail: str = "") -> InputRefused:
@@ -245,18 +597,64 @@
             if not isinstance(value, bool):
                 raise refuse(" (true or false)")
             return value
+        if self.kind == "list":
+            if not isinstance(value, list):
+                raise refuse()
+            if not self.minimum <= len(value) <= self.maximum:
+                raise refuse(f" -- it has {len(value)} entries")
+            # Named by position, so a refusal says WHICH element: `actions[3].url`.
+            return [self.items.check(f"{key}[{index}]", item) for index, item in enumerate(value)]
+        if self.kind == "object":
+            if not isinstance(value, Mapping):
+                raise refuse()
+            shape = value.get("type")
+            if not isinstance(shape, str) or shape not in self.variants:
+                raise refuse()
+            fields = self.variants[shape]
+            unknown = sorted(set(value) - set(fields) - {"type"})
+            if unknown:
+                raise refuse(f" -- a {shape!r} takes {sorted(fields) or 'no field'}, not {unknown}")
+            missing = sorted(name for name, spec in fields.items() if spec.required and name not in value)
+            if missing:
+                raise refuse(f" -- a {shape!r} needs {missing}")
+            checked = {
+                name: fields[name].check(f"{key}.{name}", value[name])
+                for name in sorted(value)
+                if name != "type"
+            }
+            return {"type": shape, **checked}
         if not isinstance(value, str):
             raise refuse()
         if self.kind == "filename" and (
             not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value
         ):
             raise refuse(" -- a bare file name, with no directory")
+        if self.kind == "argument" and (not _ARGUMENT.fullmatch(value) or ".." in value):
+            raise refuse(
+                " -- letters, digits, '.', '_', '-' and '/', starting with none of '-' or "
+                "'/', at most 256 characters, and no '..'"
+            )
+        if self.kind == "url":
+            reason = url_refusal(value)
+            if reason:
+                raise refuse(f" -- {reason}")
+        if self.kind == "header":
+            if not (self.minimum <= len(value) <= self.maximum):
+                raise refuse(f" -- it has {len(value)} characters")
+            if any(ch < " " or ch == "\x7f" or ord(ch) > 0x7E for ch in value):
+                raise refuse(" -- printable ASCII only (0x20-0x7E), which rules out CR and LF")
+        if self.kind == "string" and self.maximum is not None and not (
+            self.minimum <= len(value) <= self.maximum
+        ):
+            raise refuse(f" -- it has {len(value)} characters")
+        if self.choices and value not in self.choices:
+            raise refuse()
         return value
 
 
-def _frozen_inputs(declared: Mapping[str, RunnerInput] | None) -> Mapping[str, RunnerInput] | None:
+def _frozen_inputs(declared: Mapping[str, RunnerInput]) -> Mapping[str, RunnerInput]:
     """A read-only copy, so no caller can widen a profile's declaration in place."""
-    return None if declared is None else MappingProxyType(dict(declared))
+    return MappingProxyType(dict(declared))
 
 
 @dataclass(frozen=True)
@@ -330,29 +728,39 @@
     #: That is what closes `input.model` on `claude-code`, which its runner
     #: would pass as `--model`.
     #:
-    #: NONE MEANS NOT DECLARED YET, and then only the input's size is bounded,
-    #: as it was for every profile before this field existed. It exists for
-    #: the runners whose work IS their input -- `browser` cannot start without
-    #: `url` or `actions`, `generic` without `command` -- where an empty
-    #: declaration would refuse every task they run. What they declare is the
-    #: owner's open question #218, recorded under contract request 25 in
-    #: docs/contract-change-requests.md as an amendment awaiting approval:
-    #: the request as accepted typed this field `Mapping`, with no None.
+    #: THERE IS NO "NOT DECLARED YET". Until contract request 32 (#218) this
+    #: field could be None, for `browser` and `generic`, and then only the
+    #: input's size was bounded -- an amendment to request 25 the owner
+    #: confirmed on 2026-09-26 as the state until #218 was decided. Both
+    #: declare now, so the type is the one request 25 was accepted with.
     #:
     #: Excluded from the hash: a mapping is not hashable, and a profile's
     #: identity is its name.
-    inputs: Mapping[str, RunnerInput] | None = field(default_factory=dict, hash=False)
+    inputs: Mapping[str, RunnerInput] = field(default_factory=dict, hash=False)
 
     def __post_init__(self) -> None:
-        if self.inputs is not None:
-            for key, declared in self.inputs.items():
-                if not isinstance(declared, RunnerInput):
-                    raise ValueError(f"runner {self.name}: input {key!r} is not a RunnerInput")
-                if key == "prompt":
-                    raise ValueError(
-                        f"runner {self.name}: `prompt` is every profile's input and is not declared"
-                    )
-            object.__setattr__(self, "inputs", _frozen_inputs(self.inputs))
+        for key, declared in self.inputs.items():
+            if not isinstance(declared, RunnerInput):
+                raise ValueError(f"runner {self.name}: input {key!r} is not a RunnerInput")
+            if key == "prompt":
+                raise ValueError(
+                    f"runner {self.name}: `prompt` is every profile's input and is not declared"
+                )
+            if key == "command" and self.name != "generic":
+                raise ValueError(
+                    f"runner {self.name}: `command` is invariant 10's own guard "
+                    "(_NEVER, FORBIDDEN_CALLER_FIELDS); only `generic` is exempted, "
+                    "and only for its closed catalogue -- see contract request 32"
+                )
+            if key == "command" and self.name == "generic" and (
+                declared.kind != "string" or set(declared.choices) != set(_GENERIC_COMMANDS)
+            ):
+                raise ValueError(
+                    f"runner {self.name}: `command` may only be a string whose choices "
+                    "are exactly GENERIC_COMMANDS -- the one exemption contract request "
+                    "32 asks _NEVER to carry, not a general licence to declare it"
+                )
+        object.__setattr__(self, "inputs", _frozen_inputs(self.inputs))
         if not self.available and not self.disabled_reason:
             raise ValueError(
                 f"runner {self.name}: a disabled profile must say why. A caller "
@@ -450,6 +858,194 @@
 }
 
 
+#: A wait the browser runner hands Playwright, in milliseconds. FROM 1, NOT 0:
+#: Playwright reads a timeout of 0 as "no timeout", so a 0 here would let one
+#: action wait out the whole 5400 s attempt. FIVE MINUTES at most: a selector
+#: that has not appeared in five minutes is not coming, and the attempt's own
+#: timeout is the ceiling above that.
+_BROWSER_WAIT_MS = {"minimum": 1, "maximum": 300_000}
+
+#: `selector`, `text` and `key` may carry arbitrary Unicode (a CSS selector, a
+#: page's own text, a key combination), so they are `string` with a length
+#: bound rather than `header`, which is ASCII-only. 4 KiB: far past any real
+#: selector or typed text, and small enough that a caller who sent this much
+#: sent a payload, not a selector.
+_BROWSER_TEXT_MAX = 4096
+
+#: The browser runner's actions (`agent_worker/runners/browser.py`, `body`),
+#: one shape per `type`, each field as the runner reads it. `type` is matched
+#: exactly: the runner lower-cases it, so `"Goto"` runs today and is refused
+#: here. The restatement is held to the runner's source by a test, as the
+#: mock's keys are (tests/unit/mcp/test_runner_inputs.py).
+_BROWSER_ACTION = RunnerInput(
+    "object",
+    means="one step of the run, in the shape its `type` names",
+    variants={
+        "goto": {
+            "url": RunnerInput("url", required=True, means="the page to open"),
+            "wait_until": RunnerInput(
+                "string",
+                choices=("load", "domcontentloaded", "networkidle", "commit"),
+                means="when the load counts as done; default load",
+            ),
+        },
+        "click": {
+            "selector": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
+                means="the element to click",
+            )
+        },
+        "fill": {
+            "selector": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
+                means="the field to fill",
+            ),
+            "text": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX,
+                means="what to type into it; default empty",
+            ),
+        },
+        "press": {
+            "selector": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
+                means="the element to press a key in",
+            ),
+            "key": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX,
+                means="the key; default Enter",
+            ),
+        },
+        "wait_for": {
+            "selector": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
+                means="the element to wait for",
+            ),
+            "timeout_ms": RunnerInput(
+                "integer", **_BROWSER_WAIT_MS, means="how long to wait; default the task's timeout_ms"
+            ),
+        },
+        # 0..60: the runner clamps above 60 with `min(..., 60.0)`, so a larger
+        # value is refused rather than quietly shortened.
+        "wait": {"seconds": RunnerInput("number", minimum=0, maximum=60, means="how long to pause; default 1")},
+        "screenshot": {
+            "name": RunnerInput("filename", means="the artifact's file name; default by position"),
+            "full_page": RunnerInput("boolean", means="the whole page, not the viewport; default true"),
+        },
+        "extract": {
+            "selector": RunnerInput(
+                "string", minimum=0, maximum=_BROWSER_TEXT_MAX,
+                means="the element whose text is kept; default body",
+            ),
+            "name": RunnerInput("filename", means="the artifact's file name; default by position"),
+        },
+    },
+)
+
+#: The browser runner's inputs. NEITHER `url` NOR `actions` IS REQUIRED ALONE:
+#: the runner needs one or the other, which `required` cannot say, so that
+#: refusal stays the runner's (contract request 32, *Owner's decisions*).
+_BROWSER_INPUTS: dict[str, RunnerInput] = {
+    "url": RunnerInput("url", means="opened first, before any action"),
+    # 200: the runner's MAX_ACTIONS.
+    "actions": RunnerInput(
+        "list", minimum=0, maximum=200, items=_BROWSER_ACTION, means="run in order, after `url`"
+    ),
+    "timeout_ms": RunnerInput(
+        "integer", **_BROWSER_WAIT_MS, means="how long any one action may take; default 30000"
+    ),
+    # Three minutes: Chromium starts in seconds, and a launch still waiting at
+    # three minutes is a pod short of /dev/shm, not a slow start.
+    "launch_timeout_ms": RunnerInput(
+        "integer", minimum=1, maximum=180_000, means="how long Chromium may take to start; default 60000"
+    ),
+    # Up to 4K. The viewport is rendered in the pod's memory, and a full-page
+    # screenshot of it is written to the workspace, which is memory too.
+    "viewport_width": RunnerInput("integer", minimum=320, maximum=3840, means="pixels; default 1280"),
+    "viewport_height": RunnerInput("integer", minimum=240, maximum=2160, means="pixels; default 900"),
+    # `header`, not `string`: this value is sent as the User-Agent HTTP
+    # header verbatim, so it must be printable ASCII -- a caller could
+    # otherwise inject a second header through it. 512: real User-Agent
+    # strings run under 300 characters; past 512 it is not a browser
+    # signature.
+    "user_agent": RunnerInput(
+        "header", minimum=0, maximum=512, means="the User-Agent sent; default Chromium's"
+    ),
+    "extract_text": RunnerInput("boolean", means="keep the final page's text as page.txt; default true"),
+    "screenshot": RunnerInput("boolean", means="keep a final full-page screenshot; default true"),
+}
+
+#: The generic runner's catalogue (`agent_worker/runners/generic.py`,
+#: `GENERIC_COMMANDS`), restated because the catalogue cannot import the
+#: worker, and held to it by a test, as `_ARGUMENT` is.
+_GENERIC_COMMANDS = ("make", "npm-build", "npm-ci", "npm-test", "pytest", "uv-sync")
+
+#: Built without `inputs` first, so `_GENERIC_INPUTS` below can read this
+#: profile's OWN `timeout_seconds` for its `timeout_seconds` input's ceiling
+#: instead of restating the number as a second literal. `RunnerProfile` sets
+#: no `timeout_seconds` for `generic`, so this is the class default (3600) --
+#: reading it here, rather than writing `3600` again, is what keeps the two
+#: from drifting if a future change gives `generic` its own value.
+_GENERIC_PROFILE = RunnerProfile(
+    name="generic",
+    image="agent-runtime-base",
+    resource_class="standard",
+    backend=Backend.CLOUD_RUN_JOB,
+    runner_argv=("python", "-m", "agent_worker.runners.generic"),
+    provider=None,
+)
+
+#: The generic runner's inputs (`agent_worker/runners/generic.py`).
+#:
+#: `command` IS A NAME, NOT AN ARGV: `choices` is `_GENERIC_COMMANDS`, the
+#: runner's own `GENERIC_COMMANDS` restated, whose argv are constants in the
+#: runner. Declaring it reads as invariant 10 relaxed and is not -- see
+#: contract request 32, Question 2 (#218) -- and `RunnerProfile.__post_init__`
+#: enforces the one exemption `_NEVER` (tests/unit/mcp/test_runner_inputs.py)
+#: is asked to carry: `command` declared as a string whose `choices` are
+#: exactly `_GENERIC_COMMANDS`, for `generic` only.
+#:
+#: THE FOUR LIMITS MAY ONLY LOWER THE PLATFORM'S (`runners/limits.py`). Each
+#: ceiling here is the value the worker exports by default -- the profile's
+#: `timeout_seconds`, and `WorkerConfig`'s grace and output caps -- so a
+#: request above it is refused at the door instead of accepted and clamped.
+#: The runner still clamps to what the attempt's worker exports, which an
+#: operator may have set lower. From 1: the runner reads 0 or less as "not
+#: asked", so a 0 was accepted and meant nothing.
+_GENERIC_INPUTS: dict[str, RunnerInput] = {
+    "command": RunnerInput(
+        "string",
+        required=True,
+        choices=_GENERIC_COMMANDS,
+        means="the platform catalogue entry to run; the platform owns its argv",
+    ),
+    # 32: `GenericCommand.max_arguments`. Read for `pytest` only.
+    "paths": RunnerInput(
+        "list",
+        minimum=0,
+        maximum=32,
+        items=RunnerInput("argument", means="an existing path inside the workspace"),
+        means="pytest only: what to run; default everything",
+    ),
+    "target": RunnerInput("argument", means="make only: the target; default all"),
+    "working_directory": RunnerInput(
+        "argument", means="a directory inside the workspace to run in; default the workspace"
+    ),
+    "timeout_seconds": RunnerInput(
+        "number", minimum=1, maximum=_GENERIC_PROFILE.timeout_seconds,
+        means="lowers the command's wall clock",
+    ),
+    "grace_seconds": RunnerInput(
+        "number", minimum=1, maximum=20, means="lowers the wait between SIGTERM and SIGKILL"
+    ),
+    "max_stdout_bytes": RunnerInput(
+        "integer", minimum=1, maximum=32 * 1024 * 1024, means="lowers the stdout kept"
+    ),
+    "max_stderr_bytes": RunnerInput(
+        "integer", minimum=1, maximum=8 * 1024 * 1024, means="lowers the stderr kept"
+    ),
+}
+
+
 RUNNER_PROFILES: dict[str, RunnerProfile] = {
     "mock": RunnerProfile(
         name="mock",
@@ -463,18 +1059,10 @@
         checkpoint_interval_seconds=30,
         inputs=_MOCK_INPUTS,
     ),
-    "generic": RunnerProfile(
-        name="generic",
-        image="agent-runtime-base",
-        resource_class="standard",
-        backend=Backend.CLOUD_RUN_JOB,
-        runner_argv=("python", "-m", "agent_worker.runners.generic"),
-        provider=None,
-        # NOT DECLARED YET. The runner cannot start without `input.command`,
-        # the NAME of an entry in its own catalogue, so an empty declaration
-        # would refuse every task it runs. See RunnerProfile.inputs.
-        inputs=None,
-    ),
+    # Built from `_GENERIC_PROFILE` (declared above, alongside `_GENERIC_INPUTS`,
+    # so the input's own `timeout_seconds` ceiling can read this profile's
+    # `timeout_seconds` instead of restating it).
+    "generic": replace(_GENERIC_PROFILE, inputs=_GENERIC_INPUTS),
     "claude-code": RunnerProfile(
         name="claude-code",
         image="agent-runtime-base",
@@ -528,10 +1116,7 @@
         provider="anthropic",
         secrets=("ANTHROPIC_API_KEY",),
         timeout_seconds=5400,
-        # NOT DECLARED YET. The runner cannot start without `input.url` or
-        # `input.actions`, so an empty declaration would refuse every task it
-        # runs, the smoke suite's GKE row included. See RunnerProfile.inputs.
-        inputs=None,
+        inputs=_BROWSER_INPUTS,
     ),
 }
 
@@ -556,23 +1141,10 @@
 
     `raw` holds the keys BESIDES the prompt. Returns them normalised (an
     integral float for an integer input becomes an int), or raises
-    InputRefused: for every key the profile does not declare, naming them all,
-    or for the first declared key whose value is out of its bounds, naming the
-    bound.
-
-    NOT DECLARED YET IS DECIDED HERE, AND ONLY HERE. A profile whose inputs are
-    not declared yet (`inputs is None`: `browser` and `generic`, open with the
-    owner on #218) has no declaration to check a key against, so `raw` comes
-    back as it was sent and only its size bounds it, which the caller that
-    measures the size enforces (`validate_input_size` in swarm-api). The review
-    of #213 found this decided twice and differently: this function refused
-    every key while the API returned before asking and accepted every key, so
-    the one rule and the API gave opposite answers for the same profile. The
-    plugin's bridge sending such a profile nothing is its own send policy, not
-    this rule (#218's third question).
+    InputRefused: for every key the profile does not declare, naming them all;
+    for every required key `raw` does not send, naming them all; or for the
+    first declared key whose value is out of its bounds, naming the bound.
     """
-    if profile.inputs is None:
-        return dict(raw)
     declared = profile.inputs
     unknown = sorted(set(raw) - set(declared))
     if unknown:
@@ -590,4 +1162,11 @@
                 f"so {unknown} cannot be sent"
             )
         raise InputRefused(message, key=unknown[0], keys=tuple(unknown))
+    missing = sorted(key for key, spec in declared.items() if spec.required and key not in raw)
+    if missing:
+        raise InputRefused(
+            f"runner profile {profile.name!r} needs {missing} in its input",
+            key=missing[0],
+            keys=tuple(missing),
+        )
     return {key: declared[key].check(key, raw[key]) for key in sorted(raw)}
```

### What it would break if accepted

* **Nothing stored.** Tasks keep their `input`. The check runs at submission
  only.
* **Callers that send what the declaration refuses** would get 422
  `invalid_input` for input the API accepts today. That is the point, and it
  is a behaviour change to announce. Every case below fails or misbehaves at
  the runner today. The only exceptions are an upper-case `type`, a number
  sent as a string, and a limit above the ceiling: today those are coerced or
  clamped, and they would now be refused.
  * `generic` with no `command`, or a `command` outside the catalogue.
  * An action with an upper-case `type`, a missing `selector` or `url`, or a
    key its shape does not take.
  * `timeout_ms`, `launch_timeout_ms` or a viewport sent as a string or out
    of bounds, and `seconds` above 60.
  * A `goto` to a private, metadata, cluster or credentialled URL.
  * A `generic` limit of 0 or less, or above its ceiling.
* **In-repository callers were checked on 2026-09-29.**
  * `scripts/lib/testlib.sh` `profile_input` sends `browser` a `screenshot`
    action with `name` and `full_page` and `extract_text: false`. This
    passes the proposed catalogue.
  * `scripts/prove-gke-dispatch.sh --url https://example.com` passes.
  * `docs/workflows.md`'s example step `{"command": "pytest"}` passes.
  * `profile_input`'s default branch sends `generic` only a prompt. It would
    now be refused at 422 instead of failing at the runner. No script
    submits `generic` through that branch today.
* **Downstream restatements that must follow in the applying PR:**
  * The bridge: `parse_input_flags`'s string kinds (question 3). Its
    `not_yet` wording for a `None` profile is deleted.
  * swarm-api: `validation.validate_runner_input`'s and `runnerinputs.py`'s
    docstrings, which describe `None`. There is no code branch to remove;
    the rule decides.
  * The worker: `browser._check_url` calls `url_refusal`. The runner may keep
    its own checks, because a task written before this change still reaches
    it.
  * The Submit form's `SUGGESTED` for `browser` and `generic`
    (`apps/swarm-ui/src/Submit.tsx`). Today it is a runner census. Section 13
    of `scripts/lib/check-contract-parity.sh` would then hold it to the
    declaration, as it does for the declared profiles.
  * The generated tables. `docs/workflows.md` and `plugin/README.md` gain
    `runner-inputs:browser` and `runner-inputs:generic` blocks, and
    `test_runner_input_prose.py` needs a rendering for `list` and `object`.
  * The tests that key on `inputs is None`.
    `test_runner_inputs.py::_UNDECLARED` skips itself ("#218 is settled").
    `test_runner_inputs_by_declaration.py` and
    `test_submit_offers_only_what_runners_read.py` assert the `None`
    behaviour and are rewritten for the declarations.
  * `apps/swarm-ui/src/types.ts`, if it ever mirrors `inputs`. Today it does
    not.
* **Restatements this creates, each needing a parity test** of the kind
  `test_runner_inputs.py` already runs for the mock:
  * `choices` for `command`, against `GENERIC_COMMANDS`;
  * the `argument` pattern, against `_ARGUMENT_SAFE`;
  * 200, against `MAX_ACTIONS`, and 32, against `max_arguments`;
  * the eight action shapes and their fields, against the `if kind ==`
    chain in `browser.py`;
  * 60, against the `wait` clamp;
  * the four limit ceilings, against `WorkerConfig`'s defaults and the
    profile's `timeout_seconds`.

  The catalogue cannot import the worker, so each of these is a copy, the
  same kind of copy as the mock's exit codes.

### If it is declined

`browser` and `generic` stay `None`. The API goes on bounding them by size,
and every failure listed under *What is true today* goes on arriving after
admission, having spent a lease and a pod start. The plugin still cannot
dispatch a working browser task (#220). The only way around that without a
declaration is a bridge exception for `None` profiles, and question 3
recommends against it. A private-address `goto` still times out instead of
being refused, and a URL with a password is still stored with its task.

### Preconditions for the applying PR

Three things this entry does not itself change, because they are code, not
the frozen module, and this request is docs-only. The applying PR does not
merge without them:

* **Measure against the live cluster that page script cannot use the
  metadata token endpoint, before it merges.** The CORS-preflight claim
  above ("a cross-origin `fetch()` ... triggers a CORS preflight, and the
  metadata server does not answer the preflight in a way that permits the
  request") is reasoned from the metadata server's documented behaviour, not
  measured from this cluster. Run a `browser` task against the live GKE pod
  that attempts exactly that `fetch()` and confirm the token never reaches
  the page before this entry's SSRF posture is relied on for anything.
* **The worker re-runs `check_inputs` on the payload it reads.** Confirmed
  on 2026-09-29: nothing under `apps/agent-worker` calls `check_inputs`
  today; only swarm-api (`validation.py`) and the plugin's bridge
  (`swarm_mcp/server.py`, `workflows.py`) do, both at submission. A task
  document written before this declaration existed, or reached by a future
  bypass of the submission check, is never re-checked at the point that
  actually runs it. The applying PR adds that second call in the worker's
  own dispatch path, so a bad `input` is refused twice, not once.
* **The runner's `_ARGUMENT_SAFE` uses `fullmatch`, not `match`.** Confirmed
  on 2026-09-29: `agent_worker/runners/generic.py` line 71 defines
  `_ARGUMENT_SAFE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._\-/]{0,255}$")`
  and line 181 calls `_ARGUMENT_SAFE.match(text)`. Without `re.MULTILINE`,
  `$` matches at the end of the string **or immediately before a trailing
  newline**, so `"safe\n"` passes `.match()` today though it should not: a
  value the runner appends to an argv, or writes as a working directory,
  carrying a trailing newline. `.fullmatch()` has no such exception. This is
  the runner's own code, not `apps/common/swarm_common/profiles.py`
  (`_ARGUMENT.fullmatch(value)` there already uses `fullmatch`, and was
  correct on this point already); it is listed here because it is the same
  class of bug this entry's URL work found, in the sibling check.

**Neither of the two remaining layers is a precondition of this request,
because both are already filed and neither is code this entry touches:**

* **The pod's DNS config is #341** ("Browser worker pods resolve short
  names through the cluster search path, so `kubernetes.default` reaches
  cluster services by DNS"). Set `ndots:1` and drop search domains, which
  removes the search-path trial for every short name at once, rather than
  this entry's door check trying to enumerate which short names are unsafe.
  This is the fix the owner pointed to on 2026-09-29 when declining the
  broader host-dot-count rule: `url_refusal`'s job is a 422 with a clear
  reason for the names it CAN recognise as unsafe from the string alone
  (single-label, `.svc`); closing the general case is #341's job, in the
  pod spec, not this module's.
* **A runner-side `context.route` guard is not filed as part of this
  request either.** It is the only layer anywhere in this path that sees a
  redirect or a subresource; the door check above never does, however its
  own bugs are fixed. File it as its own worker issue, so this entry is not
  blocked on worker-side Playwright work.

Together with the worker's NetworkPolicy, these three -- #341, the
`context.route` guard, and the network policy already deployed -- are what
actually keep the pod from reaching a cluster-internal service by name. This
entry's `url_refusal` is a fourth, outermost layer: a fast, clear refusal at
submission for the shapes it can recognise, not the mechanism any of this
relies on for correctness.

### Invariants

* **Invariant 10.** Every declared key is data. `command` is a name from a
  closed list whose argv the platform owns, and the ONE EXEMPTION `_NEVER`
  (`tests/unit/mcp/test_runner_inputs.py`) is asked to carry for it is
  narrow and enforced in code (`RunnerProfile.__post_init__`): `generic`
  only, and only a string whose `choices` are exactly `_GENERIC_COMMANDS`.
  `FORBIDDEN_CALLER_FIELDS` (`swarm_api/validation.py`) is unaffected -- it
  guards the top-level task body, a different namespace declared `input`
  keys never touch. `paths`, `target` and `working_directory` are appended
  after a fixed argv and can never become a flag. The four limits can only
  lower the platform's own. Nothing declared is an image, a command line, a
  resource spec or a backend parameter, and `argv`, `script`, `env`,
  `command_line` and `shell` become refusals at the door.
* **Invariant 9.** `url_refusal` refuses the cluster's own names it can
  parse as such, and a global address it can identify in the ranges this
  module lists. **It does not refuse every private address by construction**
  -- a security review on 2026-09-29 found the first draft's version of this
  claim false, because a host it failed to parse the way a browser does
  could avoid every check that followed. The revised rule refuses non-ASCII,
  backslash and any host that is not plain `[a-z0-9.-]+` before any of the
  address logic runs, which is what makes the address logic trustworthy
  again; it is still not the SSRF control (see above), and per-tenant
  isolation stays the network's job regardless.
* **Invariant 1.** A refusal at submission creates no task. So a task that
  is bound to fail no longer takes a lease, which is less infrastructure
  demand, never more.
* **Invariant 7.** Unchanged. Nothing here sizes a pod.

### Owner's decisions

1. **Accept the three new kinds** (`list` with `items`, `object` with
   `variants`, and `argument`), the `url` kind, and the `choices` and
   `required` fields. Or name a smaller shape.
2. **The either/or on `browser`.** A task with neither `url` nor `actions`
   passes the declaration and fails at the runner. Options:
   * leave it with the runner (this request);
   * add a profile-level `requires_one_of` to the catalogue;
   * make `actions` required with a minimum of 1 and drop `url`, which
     breaks every current caller of `url`.
3. **The `url` rule, revised 2026-09-29, DECIDED the same day.** Accept the
   scheme restriction and the public-host rule as rewritten above (refuse
   before parsing, explicit refused-network lists in place of `is_global`,
   the single-label rule kept, `.svc` added to the refused suffixes, six
   IPv4-in-IPv6 embedding forms unwrapped), with no allow-list. **The owner
   answered all three sub-decisions on 2026-09-29:**
   * **the host-dot-count question, resolved: DROP the "fewer than two
     dots" rule.** It refused apex domains (`github.com`, `example.com`),
     the browser profile's main targets, while still admitting
     `kubernetes.default.svc` and `swarm-api.swarm-system.svc` (GKE's
     `ndots:5` search path applies to any name under five dots, so a
     two-label rule was never going to close that on its own terms either).
     The single-label rule is restored, and `.svc` is added to the refused
     suffixes; the general search-path gap is #341's job, not this
     module's (see *Preconditions for the applying PR*);
   * **https-only: no.** Recorded above ("Why not https-only, decided");
   * **IDN: accepted as written**, i.e. punycode only, no separate
     normalisation path. Recorded above ("IDN, decided").
   A runner-side `context.route` guard is filed as its own worker issue, not
   part of this request (recommended, see *Preconditions*).
4. **The bounds.**
   * `timeout_ms` 1..300000;
   * `launch_timeout_ms` 1..180000;
   * viewport up to 3840×2160;
   * `wait.seconds` 0..60;
   * `user_agent`: printable ASCII (0x20-0x7E), 0..512 characters (new in
     this revision, closing a CRLF-header-injection gap the first draft left
     open, since `user_agent` had no bound at all);
   * `selector`, `text` and `key`: 0..4096 characters each (new in this
     revision, for the same reason);
   * the `generic` limits at the worker's default ceilings: refused above
     them at the door, still clamped to the attempt's actual ceiling by the
     runner; `timeout_seconds`'s ceiling is read off `RunnerProfile.
     timeout_seconds` itself rather than restated as a literal (new in this
     revision).
5. **Exact matching of an action's `type`**, which refuses the `"Goto"` the
   runner accepts today.
6. **Keep the key name `command`, with the narrow `_NEVER` exemption this
   revision adds** (`generic` only, only a string whose `choices` are
   exactly `_GENERIC_COMMANDS`, enforced in `RunnerProfile.__post_init__`,
   not only asserted in prose) -- or decline the exemption and rename the
   key, which breaks every existing caller to avoid a guard this revision
   already narrows to the one case invariant 10 does not mean to forbid.
7. **Retire the `None` amendment.** This returns `RunnerProfile.inputs` to
   `Mapping` with no `None`, the type request 25 was accepted with. The
   alternative is to keep `| None` for a future profile, which leaves the
   branch in `check_inputs`.
8. **Question 3 (#218).** Keep the bridge's send-only-what-is-declared
   policy (recommended). #218's own text, read directly on 2026-09-29,
   confirms this is its third question; no further check is needed.

---

## 33. `profiles.py` / `models.py`: a merge profile that runs no agent, and two end causes for it

**Superseded in part 2026-10-06 (lane MS0, part of #352):** the App-shaped half -- `provider="git-merge"`, the merge App, its own Job account -- was already replaced by request 47; the [2026-10-06 revision](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs) keeps everything else this request applied (`WorkerAction`, `RunnerProfile.worker_action`, the `merge` entry, `MERGE_REFUSED`/`MERGE_FAILED`) as the merge step's own vocabulary, and adds new refusal codes inside `result_summary.merge.refusal`, not new end causes.

**Status: ACCEPTED 2026-09-29 by the owner, as the design; build gated on
#342. APPLIED 2026-10-01, the build accepted by the owner on 2026-10-01**
(functionality wave 3, lane M1). The owner decided that #295 is built now
and stays disabled for every tenant until #342 is enforced and the owner
creates the review and merge Apps. Applied as written below: `WorkerAction`,
`RunnerProfile.worker_action` with both `__post_init__` refusals, the `merge`
entry and `MERGE_REFUSED`/`MERGE_FAILED`. The entry also carries
`available=False` and a `disabled_reason` naming #295 and #342, so nothing
can dispatch it. Terraform creates no Job for it (`profiles_without_a_job`,
`terraform/infra/locals.tf`) until its own service account lands. Recorded
from #295's design step. Revised 2026-09-29, several times,
against a security review's rounds (B1, B2, M1–M5 and their minors, then B1
corrected repeatedly against a joint review with CR 34, ending with R8
recorded open rather than closed); see [merge-step.md](merge-step.md)'s own
revision note for the changes, which are almost entirely outside this
request's own frozen-contract surface — `worker_action`, the `merge` profile
and the two end causes are unchanged by the review. The design is
[merge-step.md](merge-step.md). Nothing under `apps/common/swarm_common/`
has been edited. **The owner accepted this request's `profiles.py`/
`models.py` shape as the design to build against — acceptance of the design
is not yet the build:** `merge` (and the `post-verdict`/`claude-code-review`
requests below) must not be enabled for any tenant until #342 (signed step
specs) ships, per merge-step.md §0 consequence 4 and §10. Renumbered from 29
to 33 on 2026-09-29: #259 is 29, #314 is 30, #304 is 31, #315 is 32. Since
then, `main`'s own numbering moved independently — its own request 29
(`EndCause` gains `PUBLISH_REFUSED`) is unrelated content, and request 31 is
not present on `main` at all — but 33 was never taken by anything else, so
no further renumbering was needed on merge.

**Pointer, not a request of its own (round-3 re-review, 2026-09-29): the
design's `post-verdict` and `claude-code-review` catalogue entries are
separate, not-yet-filed frozen-contract requests, tracked in
[merge-step.md](merge-step.md) §10 (build items 2–3) and §6a (the pinned
`VERDICT_REFUSED`/`VERDICT_FAILED` end causes), not folded into this one.**
This request stays scoped to what its heading says — the `merge` profile —
because `post-verdict` and `claude-code-review` each need their own
`### What it would break if accepted` analysis once filed, the same way this
one has its own. This design also now depends on S0 issue #342, signed step
specs, which every step's worker (not only `merge`'s or `post-verdict`'s)
must verify before running. **Corrected, joint review with CR 34 (#344),
2026-09-29: an earlier draft of this paragraph said #342 "is not a change to
a frozen type" and so did not belong in this file. That was wrong.** Signing
and verifying a step's canonical spec needs somewhere on the frozen `Task`
or `WorkflowStep` shape to carry the signature (or an equivalent frozen
type), which is exactly the kind of change this file exists to record; #342
is tracked as its own S0 issue for the security decision and the build plan,
but its frozen-contract surface — whatever field or type CR 34 ends up
needing — belongs here too, as its own numbered request once CR 34 states
precisely what it is. This entry does not attempt to state it first; see
[merge-step.md](merge-step.md) §0 consequence 4 and §7 T14/R7 for why this
design cannot be enabled without it regardless.

### What is true today

A workflow cannot merge its own pull request. Every `RunnerProfile` names a
runner that the lifecycle starts as a supervised child
(`runner_argv`, contract request 18), and every runner runs an agent or a
command. The only credential-bearing thing a worker does after its runner exits
is `_publish_git`, which runs under the tenant's service account in the same
container the agent just ran in.

An agent can mint that service account's token from the metadata server
([security.md](security.md#cloud-metadata-abuse)). So a merge credential
readable by the worker that publishes is readable by every agent of the tenant
([merge-step.md](merge-step.md) §0). The contract's own platform decision says
where a step's identity can differ: Cloud Run sets the service account on the
Job, and there is one Job per tenant per **profile**. A merge that no agent can
reach therefore needs a profile of its own.

`EndCause` (contract request 23) has no value that says a merge was refused or
failed. Recording one as `RUNNER_ERROR` would be false, since no runner ran.
Recording it as None would send the outcome ledger back to classifying text.

### The requested change

In `profiles.py`:

```python
class WorkerAction(str, Enum):
    """A platform action the WORKER performs instead of starting a runner."""
    MERGE = "merge"


@dataclass(frozen=True)
class RunnerProfile:
    ...
    #: When set, the lifecycle performs this action itself and starts no
    #: runner child, so no agent ever runs under this profile's Job identity.
    #: `runner_argv` must then be empty, and it must be non-empty otherwise.
    worker_action: WorkerAction | None = None
```

`__post_init__` refuses `worker_action` with a non-empty `runner_argv`, and
refuses `runner_argv=()` without a `worker_action`.

A catalogue entry:

```python
"merge": RunnerProfile(
    name="merge",
    image="agent-runtime-base",
    resource_class="standard",
    backend=Backend.CLOUD_RUN_JOB,
    runner_argv=(),
    worker_action=WorkerAction.MERGE,
    # The credential is never mounted: its secret is read by the worker at
    # merge time, as `swarm-<tenant>-merge`, the Job's own service account.
    # `provider` is what parks the step CREDENTIAL_MISSING, at no cost, for a
    # tenant that has not registered one, and keeps Terraform from creating a
    # merge Job for that tenant.
    provider="git-merge",
    secrets=(),
    timeout_seconds=600,
    inputs={},
),
```

In `models.py`, two `EndCause` values, written only by the worker:

```python
MERGE_REFUSED = "merge_refused"   # a condition for merging was not met; nothing changed on the forge
MERGE_FAILED = "merge_failed"     # the merge was allowed, and the forge did not do it
```

The specific reason (`verdict_not_merge`, `checks_pending`, `head_moved`, ...)
goes in `result_summary.merge.refusal`, a worker vocabulary and not a frozen
one, just as `publish_reason` is today. [merge-step.md](merge-step.md) §6 lists
every code and its cause.

### What it would break if accepted

* **Nothing stored.** `worker_action` defaults to None, so every existing
  profile is unchanged. Old task documents never carry the new causes.
* **Every consumer of `runner_argv` must learn that it can be empty.** Today
  that is `lifecycle._runner_argv` and the dispatchers, which set no command
  (request 18). The lifecycle must branch on `worker_action` before it builds
  an argv.
* **Terraform's `job_matrix`** creates a `merge` Job for each tenant whose
  providers include `git-merge`. That Job has to run as the tenant's merge
  service account, not `worker_service_accounts[tenant]`, and that is a
  Track C change. Until it lands, the Job must not exist: a merge Job running
  as the tenant's worker account is the hole this request exists to close.
* **Every restatement of the catalogue** has to follow: the plugin's bridge,
  `swarm_profiles`, the UI's profile list, and the parity checks that hold the
  shell and jq copies to the Python (`check-contract-parity.sh`). A profile
  with no agent should appear as such, not as a runner.
* **The outcome ledger** (`swarm_api.outcomes`) gains two classes, and its
  `DERIVE_VERSION` is bumped so stored days are re-derived.

### If it is declined

A merge can be done only by something that is not a workflow step:
`auto-merge.yml` with a human's `ready`, or an operator. The owner's chain
then stops at proof. The two alternatives [merge-step.md](merge-step.md) §1
compares both put the merge credential where an agent, or another tenant, can
reach it. They are listed there as rejected, not as fallbacks.

### Invariants

- **Invariants 1–3.** The merge step is an ordinary task: admitted in the same
  transaction, parked and costing nothing until its parents succeed, and
  counted from `LEASED`.
- **Invariant 4.** A pending check is a refusal, not a wait. A long forge
  rate-limit fails the attempt retryably with `next_eligible_at`, and the
  worker does not sleep through it.
- **Invariant 5.** The worker checks its generation at start and again
  immediately before the merge call. A stale merge worker never touches the
  forge.
- **Invariant 6, 7.** On-demand `standard`; `requests == limits`.
- **Invariant 8.** Checkpointing stays on. The workspace is empty, and the
  merge is idempotent on its pinned sha, so a lost attempt costs one re-read.
- **Invariant 9.** The merge credential's only accessor is the tenant's own
  merge service account. No agent runs as that account, and no other tenant's
  Job can.
- **Invariant 10.** A caller names `merge` and sends `input: {}`. The
  repository, the pull request and the sha all come from the workflow's own
  steps, never from the caller.
- **#219.** The credential is read only by the merge worker, only at merge
  time. It is never in the workspace, a file, an environment variable, argv or
  a log, and it is revoked in a `finally`.

### Amendment (proposed 2026-09-30, #364)

**Status: ACCEPTED — accepted by the owner 2026-10-01. Item 1 APPLIED 2026-10-01**
(functionality wave 3, lane M1, in the same change as request 33):
`validation.known_providers()` leaves out every `worker_action` profile's
provider (`APP_CREDENTIAL_PROVIDERS`: `git-merge`, `git-review`), and both
credential routes call it, so neither route file was edited and neither
accepts the two. **Items 2 and 3 are not applied yet** (Terraform, lane M2);
until they are, no Job and no tenant `providers` entry may name either
provider. Found by the
security review of #351 (CR 35/36), filed as issue #364. This amendment does
not touch this request's frozen-contract surface (`profiles.py`/`models.py`
are unchanged by it); it closes a gap in how the design it names is *built*,
so it is recorded here rather than as a separate numbered request. It leaves
the Status line above as is — that line is about the `profiles.py`/`models.py`
shape, which is still accepted as designed.

`git-merge` must never end up bound to the tenant's ordinary worker service
account, and the same is true of `git-review` (contract request 35, revised
on #351). Nothing enforces that yet outside `register-tenant.sh`
(merge-step.md §10 item 5):

* The API credential routes accept whatever the catalogue lists as a
  provider — `POST /me/credentials` (`routes/tenants.py`) and the admin
  route (`routes/admin.py`) — so once `git-merge`/`git-review` are catalogue
  providers, a tenant (or an operator, on the admin route) can register one
  against the worker account through the ordinary credential path, with
  nothing in that path aware that these two providers are different.
* `terraform/modules/secret_manager` has no per-provider accessor override.
  Its `iam_binding` is authoritative and today reads `members=[cfg.accessor]`
  (`main.tf:17-28,109-116`), always the tenant's worker account
  (`terraform/infra/main.tf` (`accessor      = cfg.accessor`)). Left as is, the next `terraform apply`
  after `git-merge`/`git-review` join the catalogue both binds the worker
  account to the secret and **removes** a separately-granted merge or review
  service account, since `iam_binding` (not `iam_member`) replaces the whole
  membership list.

The build of contract request 33 (and of `post-verdict`/`claude-code-review`,
merge-step.md §10 items 2–3) is gated on all of the following, in addition to
what merge-step.md §10 already lists:

1. The API credential routes (`routes/tenants.py`'s `POST /me/credentials`,
   and `routes/admin.py`'s admin route) refuse to register or bind
   `git-merge` or `git-review` to a tenant's worker account, the same way
   `register-tenant.sh` refuses (merge-step.md §10 item 5).
2. `terraform/modules/secret_manager` gains a per-provider accessor
   override, so that for `git-merge` and `git-review` the merge or review
   service account is the **sole** `secretAccessor` member — not an addition
   to `cfg.accessor`, a replacement of it for those two providers — closing
   the removal hazard in `main.tf:17-28,109-116` above.
3. Both providers are excluded from `refresh_secrets` and from the
   refresher's own grants (its `versionAdder` role and its `-refresh` twin
   secret), so the subscription-refresh path never touches either
   credential.

See merge-step.md §10 item 5's own note on this (added 2026-09-30, #364) and
issue #364 for the full finding.

---

## 34. `models.py` / a new `specsign.py`: a step's spec is signed by swarm-api and verified by every worker

**Status: ACCEPTED 2026-09-29 by the owner after three security reviews.**
Applied by #353 (accepted by the owner 2026-09-29): `swarm_common/specsign.py`
and the three signature fields and `EndCause.SPEC_SIGNATURE_INVALID` in
`swarm_common/models.py`; the Terraform half by #354.
Recorded from #342 (S0), which the round-3 security re-review of #316
(contract request 33, the merge step) found the same day. This entry went
through three rounds of joint review with contract request 33 on 2026-09-29 --
the initial mechanism decision, the checkpoint-restore/shared-signer/GKE
review, and the checkpoint-residual/end-cause/post-verdict-mechanism
correction, each recorded in this entry as it was made -- before the owner
accepted it as it now reads. The owner decided the mechanism on 2026-09-29, in
a joint review with contract request 33: swarm-api signs each step's canonical
spec at
submission with a Cloud KMS asymmetric key that only `swarm-api`'s service
account holds `signer` on; every worker verifies the signature against the
spec it fetched before it starts an agent or a worker action, and refuses a
mismatch with a typed end cause; worker-action steps (`post-verdict` and
`merge`, contract request 33) also verify the upstream specs they rely on.
The key stays in the shared `saga-agents-staging` project (the owner's
decision, 2026-09-29, **not** a dedicated project), and workers trust every
`ENABLED` version of it. Both are accepted residuals, stated honestly in **Key
management** and **The threat model** rather than designed away: a project
owner or editor of the shared project can already reach `swarm-api`'s signing
identity, and a KMS admin's new version is trusted the moment it ships. This
entry is what that decision needs from the frozen contract, and the
design around it that the frozen fields only make sense inside. Nothing under
`apps/common/swarm_common/` has been edited. Numbered 34 because 29 to 33 are
taken on open branches (#259, #314, #304, #315, #316); if another branch has
taken 34 by the time this merges, renumber this one.

### What is true today

A step's instructions are whatever its task document says when its worker
reads it, and the document is writable by every agent of the tenant.

* **The worker trusts the document unsigned.** `Lifecycle._prepare`
  (`apps/agent-worker/agent_worker/lifecycle.py`, step 4) calls
  `self.control.fetch_task()` and builds the runner's `input.json` from
  `task.get("input")`. The same dict is kept as `self._task` and is where the
  worker later reads `repository_url` and `repository_ref` (`_maybe_clone`),
  `metadata.input_from` (`inputs.py`), `metadata.expected_outputs`
  (`expected_outputs.py`), `metadata.dispatch` (the publish gate, and #273's
  `continuation.py`), `resource_class` and `max_attempts`.
  `ControlPlane.fetch_task` (`control.py`) checks one thing about the
  document: that its `tenant_id` is this worker's.
* **The scheduler trusts it too.** `worker_env()`
  (`apps/scheduler/scheduler/dispatch.py`) sets `RUNNER_PROFILE` and
  `TASK_TIMEOUT_SECONDS` on the execution from the document, and picks the Job
  (one per tenant per profile) by the document's `runner_profile`. The
  per-execution override wins over the Job's own `RUNNER_PROFILE`
  (`terraform/infra/locals.tf`, "The dispatcher's per-execution overrides
  MERGE with this").
* **Any agent of the tenant can write the document.**
  [multi-tenancy.md](multi-tenancy.md#the-firestore-row-in-full) records it:
  the tenant worker's service account holds its Firestore role on the whole
  database, because Firestore has no document-level IAM, and an agent can mint
  that account's token from the metadata server
  ([security.md](security.md#cloud-metadata-abuse)). So "it CAN read and write
  a document whose id it can guess". Within a workflow nothing has to be
  guessed: every step's `task_id` is on the workflow document
  (`WorkflowStep.task_id`), and every step knows its workflow.
* **So one step can rewrite another's instructions.** In an implement →
  review → fix chain the implement agent can set the parked review task's
  `input.prompt` to "write verdict.json with MERGE", point its
  `metadata.input_from` at a different file, or rewrite the fix step's prompt.
  The real review step then runs the rewritten instructions, with no prompt
  injection involved. #342 has the reproduction.

`swarm-api`'s `Store` is the only thing in `apps/` that creates a task document
(`create_tasks`, `create_workflow`; read on main at `bc90da1`, 2026-09-29).

### The requested change

Three frozen additions: a module that defines the canonical form, three
optional fields on `Task`, and one `EndCause`. Everything else in this entry
(the KMS key, the signing call, the verification, the rollout) is outside
`swarm_common` and is written here so the frozen part can be judged against
the design it serves.

#### 1. The canonical form: `swarm_common/specsign.py` (new, frozen)

The spec is a projection of the task document: the fields that decide what a
step runs, as swarm-api wrote them, and nothing a later writer changes. Both
sides compute it from the **Firestore dict** (`Task.to_firestore()` on
swarm-api, the fetched snapshot in the worker), so they project the same
shape.

**Covered, format 1.** Every value is taken from the document as stored, with
a missing key read as null:

| canonical key | source | why it is covered |
|---|---|---|
| `purpose` | the constant `"swarm.step-spec"` | domain separation: the bytes cannot be read as any other message this key might one day sign |
| `format` | the constant `1` | a later format is a different message, never a reinterpretation of this one |
| `task_id` | **the document id the worker was pointed at**, never the document's own `id` field | binds the signature to one task, so a signature copied onto another task fails |
| `tenant_id`, `workflow_id`, `step_id` | the fields | binds it to one tenant and one workflow; a spec cannot be replayed across either |
| `submitted_by` | the field | who submitted is part of what was submitted (and of contract request 30's audit) |
| `runner_profile` | the field | the profile picks the Job, and so the service account; a rewrite to `merge` or `post-verdict` would move a step onto an identity no agent may use |
| `resource_class`, `timeout_seconds`, `max_attempts` | the fields | read by the scheduler and the worker (`TASK_TIMEOUT_SECONDS`, `_sized_resource_class`, `fail_retryably`) |
| `provider`, `model` | the fields | set at submission, from the profile and the request, and changed by no later writer |
| `input` | the **whole object** | the prompt and every declared input (`issue`, the mock's), whatever keys a profile declares later |
| `depends_on` | the list, **in stored order** | a removed parent is how a step would be made to run early |
| `repository_url`, `repository_ref` | the fields | what is cloned, and where a pull request is opened |
| `metadata` | exactly the three keys `dispatch`, `input_from`, `expected_outputs`, each the stored value or null | the platform's own blocks. `dispatch` carries `strategy`, `carrier`, `role`, `integrates`, #273's `continues`, and contract request 33's `pr_role`, `merges` and `verdict_source` (`post-verdict`'s own dispatch-block pointer at the review task it reads, `{review: <review task id>}`, added in that entry's round-5 re-review so `post-verdict` never has to trust `input_from`'s tenant-writable staging for the one read this whole design depends on); `input_from` is the task-id-keyed map `submit_workflow` writes; `expected_outputs` is what `record_expected_outputs` writes on an upstream step |

**Not covered, on purpose.** `state`, `park_reason`, `blocked_by`,
`current_lease_id`, `current_generation`, `attempt_count`,
`next_eligible_at`, `cancel_requested`, `started_at`, `completed_at`,
`last_error`, `result_summary`, `latest_checkpoint`, `end_cause` and
`updated_at`: every one is written after submission by the scheduler, the
reconciler, the worker or a cancel, so a signature over any of them would
break on the first legitimate write. `created_at` binds nothing `task_id`
does not, and a timestamp is the one value whose round trip through Firestore
(microseconds, a zone) would have to be specified for no gain. `priority` is admission's
ordering within the tenant and no worker reads it. `metadata.startup_refunds`
is the reconciler's. Every other `metadata` key is the caller's own free-form
label, which no worker acts on. **The rule that keeps this true:** a metadata
key the worker starts reading is added to `SIGNED_METADATA_KEYS` in the same
change, with a format bump; a test holds the two together (**Tests**, below).

**Serialisation: RFC 8785 (JSON Canonicalization Scheme), implemented inside
`specsign.py` with no dependency.** `swarm_common` declares
`dependencies = []`, and the `sc` plugin installs it from a git tag
(`apps/common/pyproject.toml`), so it cannot take the `rfc8785` package. The
value domain is small enough to implement exactly: null, booleans, strings,
integers, finite doubles, arrays and string-keyed maps, which is everything
Firestore returns for a document swarm-api wrote from JSON. The implementation
is held to RFC 8785's own test vectors and to the `rfc8785` package as a
test-only oracle (**Tests**). Three values have no canonical form, and swarm-api
refuses them at submission with 422 `invalid_input` **before** anything is
signed or stored: an integer outside ±(2^53 − 1) (JCS numbers are IEEE doubles;
today `validate_storable` admits up to 2^63 − 1), a non-finite float, and a
string holding a lone surrogate. This is a narrowing of what the API accepts,
and says so.

**Hash: SHA-256** over the UTF-8 bytes. The key signs that digest
(`EC_SIGN_P256_SHA256`, below).

The proposed module, whole. It was run on 2026-09-29, from a scratch copy
outside the repository, against RFC 8785's number vectors (Appendix B) and its
§3.2.2 and §3.2.3 examples, and against the `rfc8785` package (0.1.4) on
20,000 generated values, with no difference. The first draft sorted members by
their *escaped* names and put `"\r"` after `"1"`; §3.2.3's example is the
vector that caught it, which is why it is named in **Tests**.

```python
"""The canonical form of a step's spec, signed by swarm-api and verified by the worker.

Contract request 34 (#342). A tenant's agents can write any task document of
their tenant (docs/multi-tenancy.md, "The Firestore row, in full"), so a
worker must not run a spec swarm-api did not sign. This module is the ONE
definition of which fields that signature covers and how they become bytes:
swarm-api signs `spec_digest(canonical_step_spec(task.to_firestore(),
task_id=task.id))`, and the worker verifies the same digest over the document
it fetched. Two copies of this projection would be two opinions about what
is signed. No cryptography lives here: signing is swarm-api's (Cloud KMS) and
verification is the worker's.

The encoding is RFC 8785 (JCS), implemented for exactly the values a
Firestore document written from JSON can hold, with no dependency: this
package is installed from a git tag by the plugin and declares none.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any, Mapping

#: The canonical form's version. A change to the covered fields or the encoding
#: is a NEW format, never an edit to this one: a signature is over a format.
SPEC_FORMAT = 1

#: Domain separation. The bytes of a step spec cannot be read as another message.
SPEC_PURPOSE = "swarm.step-spec"

#: The keys of `Task.metadata` that swarm-api writes and the worker acts on.
#: Every other metadata key is the caller's label or another writer's record.
SIGNED_METADATA_KEYS = ("dispatch", "input_from", "expected_outputs")

#: JCS numbers are IEEE doubles, so an integer is exact only within this bound.
MAX_SAFE_INTEGER = 2**53 - 1


class SpecNotCanonical(ValueError):
    """A value in the spec has no canonical form (see `jcs`)."""


def canonical_step_spec(doc: Mapping[str, Any], *, task_id: str) -> bytes:
    """The bytes the signature covers, from a task document as stored.

    `task_id` is the id the document was READ BY (the worker's TASK_ID, the
    id swarm-api minted), never the document's own `id` field.
    """
    metadata = doc.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise SpecNotCanonical("metadata is not a map")
    spec = {
        "purpose": SPEC_PURPOSE,
        "format": SPEC_FORMAT,
        "task_id": task_id,
        "tenant_id": doc.get("tenant_id"),
        "workflow_id": doc.get("workflow_id"),
        "step_id": doc.get("step_id"),
        "submitted_by": doc.get("submitted_by"),
        "runner_profile": doc.get("runner_profile"),
        "resource_class": doc.get("resource_class"),
        "timeout_seconds": doc.get("timeout_seconds"),
        "max_attempts": doc.get("max_attempts"),
        "provider": doc.get("provider"),
        "model": doc.get("model"),
        "input": doc.get("input"),
        "depends_on": doc.get("depends_on"),
        "repository_url": doc.get("repository_url"),
        "repository_ref": doc.get("repository_ref"),
        "metadata": {key: metadata.get(key) for key in SIGNED_METADATA_KEYS},
    }
    return jcs(spec)


def spec_digest(canonical: bytes) -> bytes:
    """SHA-256 of the canonical bytes: what the KMS key signs."""
    return hashlib.sha256(canonical).digest()


def jcs(value: Any) -> bytes:
    """RFC 8785 serialisation of `value`, as UTF-8 bytes."""
    return _encode(value, "$").encode("utf-8")


def _encode(value: Any, path: str) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise SpecNotCanonical(f"{path} is an integer outside +/-(2**53 - 1)")
        return str(value)
    if isinstance(value, float):
        return _number(value, path)
    if isinstance(value, str):
        return _string(value, path)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(v, f"{path}[{i}]") for i, v in enumerate(value)) + "]"
    if isinstance(value, Mapping):
        items = []
        for key in value:
            if not isinstance(key, str):
                raise SpecNotCanonical(f"{path} has a key that is not a string")
            _string(key, path)  # refuses a lone surrogate before it is sorted
            items.append(key)
        # RFC 8785 3.2.3: members sorted by the UTF-16 code units of their RAW
        # names -- not their escaped form, which would put "\r" after "1".
        items.sort(key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(
            _string(k, path) + ":" + _encode(value[k], f"{path}.{k}") for k in items
        ) + "}"
    raise SpecNotCanonical(f"{path} is a {type(value).__name__}, which JSON has no form for")


def _string(text: str, path: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SpecNotCanonical(f"{path} holds a lone surrogate") from exc
    # json.dumps with ensure_ascii=False escapes exactly what RFC 8785 3.2.2.2
    # does: '"', '\\', the five short forms, and \u00xx (lowercase) for the
    # rest of U+0000..U+001F. Everything else is written as itself.
    return json.dumps(text, ensure_ascii=False)


def _number(x: float, path: str) -> str:
    # RFC 8785 3.2.2.3: ECMAScript's Number.prototype.toString.
    if not math.isfinite(x):
        raise SpecNotCanonical(f"{path} is not a finite number")
    if x == 0:
        return "0"
    if x < 0:
        return "-" + _number(-x, path)
    # repr() is the shortest round-tripping decimal, as ECMAScript requires.
    _, digits_t, exponent = Decimal(repr(x)).as_tuple()
    digits = "".join(map(str, digits_t))
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    digits = stripped
    k = len(digits)
    n = exponent + k
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    e = n - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return mantissa + "e" + ("+" if e >= 0 else "-") + str(abs(e))
```

#### 2. `models.py`: three fields on `Task` and one `EndCause` (exact diff against `bc90da1`, not applied)

```diff
--- a/apps/common/swarm_common/models.py
+++ b/apps/common/swarm_common/models.py
@@ -182,7 +182,8 @@
     Each value is written by exactly one kind of writer, beside `completed_at`:
 
       * the worker (`agent_worker.control`): TIMEOUT, OUTPUTS_MISSING,
-        INPUTS_UNAVAILABLE, CANNOT_START, RUNNER_ERROR, CANCEL_REQUESTED;
+        INPUTS_UNAVAILABLE, CANNOT_START, RUNNER_ERROR, CANCEL_REQUESTED,
+        SPEC_SIGNATURE_INVALID;
       * the reconciler (`repair_task_state`): LOST_WORKER, CANNOT_START,
         CANCEL_REQUESTED;
       * the scheduler: DISPATCH_FAILED, FAILED_PARENT, CANCELLED_PARENT,
@@ -209,6 +210,17 @@
     its own class, not a runner error -- 10 of dev's 13 "runner errors" on
     2026-09-25 were exactly that. CANCELLED_PARENT is the request's own, and
     what decision item 2 (split "after a cancel" from "after a failure") needs.
+
+    SPEC_SIGNATURE_INVALID is contract request 34 (#342): the worker refused
+    to run a spec swarm-api did not sign -- a signature that does not verify
+    over the fetched document, a missing one outside the rollout window, or a
+    key version that is not the platform's. A worker-action step's OWN spec
+    failing this check is this cause too; an UPSTREAM step's spec failing it
+    is contract request 33's own end cause instead (that entry names it; this
+    one does not), with reason "upstream:<task id>:<why>" -- the same reason
+    shape this cause uses for its own failures. Never retried: another
+    attempt reads the same document. Every occurrence is either a tenant's
+    agent rewriting a step or a platform bug, and both causes are alerted on.
     """
 
     TIMEOUT = "timeout"
@@ -222,6 +234,7 @@
     FAILED_PARENT = "failed_parent"
     CANCELLED_PARENT = "cancelled_parent"
     WORKFLOW_SWEEP = "workflow_sweep"
+    SPEC_SIGNATURE_INVALID = "spec_signature_invalid"
 
 
 @dataclass
@@ -263,6 +276,21 @@
     #: every task that has not ended, on a success, and on anything written
     #: before 2026-09-25 -- an old document decodes exactly as it did.
     end_cause: EndCause | None = None
+    #: swarm-api's signature over this task's canonical step spec (contract
+    #: request 34, #342): standard base64 of the DER ECDSA signature that Cloud
+    #: KMS returns for `specsign.spec_digest(specsign.canonical_step_spec(...))`.
+    #: Written once, in the write that creates the document, and never after.
+    #: None on every task written before swarm-api signed; the worker refuses
+    #: such a task outside the rollout window.
+    spec_signature: str | None = None
+    #: The FULL resource name of the key version that made `spec_signature`:
+    #: projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>/cryptoKeyVersions/<n>.
+    #: Written by the same write. A tenant can rewrite it, so the worker trusts
+    #: it only as a lookup key among the versions the platform published.
+    spec_key_version: str | None = None
+    #: `specsign.SPEC_FORMAT` at signing. Read to choose the projection; it is
+    #: also inside the signed bytes, so a rewrite to another format fails.
+    spec_format: int | None = None
 
     def retries_exhausted(self) -> bool:
         """This task has used its last attempt. See `retries_exhausted`."""
```

**Re-verified against current `main`, 2026-09-29.** The `SPEC_SIGNATURE_INVALID`
docstring above was reworded after the joint review, to separate own-spec from
upstream-spec failures (**Verification in the worker**, below), which changed
this hunk's line counts twice over. The original PR's "Checked" note that this
diff patched cleanly onto `bc90da1` predated both rewordings and this
hunk-count fix. Re-run here: `git apply --check` against `apps/common/swarm_common/models.py`
at `origin/main` (`e7c18ac`, 2026-09-29) exits 0, and the patched file parses
(`ast.parse`). The first re-run, before this fix, failed with `error: corrupt
patch` — the second hunk's header claimed 16 new lines where the body had 17
(a one-line miscount left over from the rewording), which is corrected above.

`to_firestore` needs no change: `asdict` writes the three fields as they are.
`WorkflowStep` and `Workflow` are unchanged (see **What signing does not
close**, the workflow document).

#### 3. Key management (Terraform; Track C's files, named here, not written)

* **`cloudkms.googleapis.com`** joins the service list in
  `terraform/infra/main.tf`. `project_services` already refuses
  `disable_on_destroy = true`, so enabling it can never disable it for the
  other team.
* **One key ring and one key per environment, and this MUST hold.** `google_kms_key_ring`
  `swarm-<env>-specs` in `var.region` (`us-central1`), and in it
  `google_kms_crypto_key` `step-spec` with `purpose = "ASYMMETRIC_SIGN"`,
  `version_template { algorithm = "EC_SIGN_P256_SHA256", protection_level =
  "SOFTWARE" }` and `labels = { managed-by = "swarm-terraform", ... }`.
  P-256 because verification is fast and the signature is 70-odd bytes of DER
  in every task document. **The canonical form carries no `environment`
  field** (section 1's table): nothing in the signed bytes says "dev" or
  "prod". A key shared across environments would let a spec signed by dev's
  swarm-api verify against prod's worker, so `SPEC_SIGNING_KEY` and every
  version in `SPEC_VERIFY_KEYS` must name that environment's key ring and no
  other's; this is an operational rule Terraform's naming enforces (one ring
  per `<env>`), not something the canonicaliser can check.
* **`managed-by=swarm-terraform`, and the one resource that cannot carry it.**
  The crypto key carries the label. **A key ring has no labels at all** in
  Cloud KMS or the google provider, so `google_kms_key_ring` has to be added
  to `scripts/lib/unlabelable-types.json`, checked against
  `terraform providers schema -json` for the pinned provider the way every
  entry there was. The IAM members below are `google_kms_crypto_key_iam_member`,
  which is unlabelable for the same reason as every other `*_iam_member` and
  needs its own entry. **Neither a key ring nor a key can be deleted** in
  Cloud KMS: `terraform destroy` removes them from state and schedules the
  key's versions for destruction, and the names stay taken. So `make destroy`
  followed by a fresh apply collides with the old names; the recreate path
  needs an `import` block, and this is written next to the resources.
* **Who holds what.** Nobody holds `roles/cloudkms.signerVerifier`.

  | identity | role, on the key only | why |
  |---|---|---|
  | `swarm-api` | `roles/cloudkms.signer` (`useToSign` only) | it signs; it never verifies or reads the public key, so `signerVerifier` would be reach it does not use |
  | the CI deployer (`swarm-tf-deployer`) | `roles/cloudkms.publicKeyViewer` and `roles/cloudkms.viewer` | Terraform reads the enabled versions and their public keys at plan time (below); neither role can sign or change IAM |
  | every worker (`swarm-agent-worker-<tenant>`, and contract request 33's `swarm-<tenant>-merge` and `swarm-<tenant>-post-verdict`) | **none** (recommended; decision 4) | the public keys reach it in its Job's environment |

  The key ring, the key and the one `signer` binding live in
  `terraform/bootstrap`, applied by the owner directly, not in
  `terraform/infra` (decision 4). That keeps the CI deployer
  (`swarm-tf-deployer`) off `setIamPolicy` on the key, so **the CI pipeline**
  cannot grant itself `signer`. The binding names `swarm-api` by its email,
  `swarm-api@<project>.iam.gserviceaccount.com`, since the account itself is
  created by `terraform/infra` (in `modules/iam`).

  **This is not a boundary against the project itself, and the owner accepted
  that rather than designing it away (2026-09-29): the key stays in the shared
  `saga-agents-staging` project** (CLAUDE.md rule 2 -- another team's project,
  not a dedicated one). `roles/editor` on that project carries
  `iam.serviceAccounts.actAs` and `run.services.update`, so any project Owner
  or Editor -- not only this platform's operators -- can redeploy
  `swarm-api`'s Cloud Run service to run code of their choosing as its service
  account, which holds `signer`, and sign anything through it without ever
  calling `setIamPolicy` on the key. An Owner can also `setIamPolicy` the key,
  the ring or the project directly, bootstrap's separation notwithstanding.
  **This residual holds against the platform team and the other team sharing
  the project. It does not hold against tenant agents:** the table above gives
  no tenant or worker account any role on this key, ever, so a tenant's own
  agent -- the actor #342 is about -- has no path to a signature through KMS
  IAM at all; its only avenue is still the one this entry closes, rewriting an
  already-signed document. Stated again in **The threat model**.
* **Trusted versions.** A worker trusts **every `ENABLED` version** in
  `SPEC_VERIFY_KEYS` (or reachable through `GetPublicKey`, under the runtime
  alternative), with no separate allow-list of "which versions swarm-api is
  actually using." **Accepted residual, stated plainly:** anyone who can
  create or import a version on this key -- a KMS admin, or `swarm-tf-deployer`
  running `terraform apply` against `terraform/bootstrap` -- gets that version
  trusted by every worker from the next release on, whether or not swarm-api
  ever signs with it. Narrowing trust to "the version `SPEC_SIGNING_KEY_VERSION`
  currently names" was considered and rejected: it would break rotation step 2
  (the entry two bullets below), where N and N+1 must both verify before
  swarm-api cuts over.
* **How workers get the public keys, and how they are cached (recommended).**
  `terraform/infra` reads the key's versions with the
  `google_kms_crypto_key_versions` data source, keeps those in state
  `ENABLED` (the data source's attributes, the state and the PEM, are to be
  checked against `terraform providers schema -json` for the pinned
  provider, as the unlabelable types are).

  **On Cloud Run**, it renders one worker environment variable on every Job,
  `SPEC_VERIFY_KEYS`: a JSON map `{<full version name>: <PEM public key>}`.
  The Job's environment **is** the cache: fixed for the life of the execution,
  refreshed by every release, and needing no network call, no quota and no IAM
  at attempt start. Beside it, `SPEC_SIGNING_KEY` names the crypto key.
  `worker_env()` must never override either (a test holds the scheduler's
  override keys to an allow-list that excludes them).

  **On GKE the same map is not inlined into the pod's `env:`** (the owner's
  decision, 2026-09-29 review): a browser pod's manifest is template-rendered
  by `GkeJobDispatcher._manifest` from
  `kubernetes/worker-templates/worker-job-browser.yaml`, not written once by
  Terraform, and the tenant's own worker KSA runs in the same per-tenant
  namespace as that Job (`kubernetes/namespaces/tenant-namespace.yaml`) --
  putting the JSON map inline is one placeholder away from a tenant-writable
  copy the way `worker_env()`'s per-execution overrides already are, and is
  exactly the large-value-in-a-template problem that file's own
  `SWARM_ARTIFACTS_DIR` comment warns about for a far smaller value. Instead,
  `terraform/infra` renders a `kubernetes_config_map`,
  `swarm-spec-verify-keys` (the same `{version: PEM}` shape, plus
  `SPEC_SIGNING_KEY`), **in each tenant's namespace**, applied by Terraform's
  own identity. `GkeJobDispatcher._manifest` mounts it **read-only** at
  `/etc/swarm/spec-verify-keys` on the `browser` profile's Job in place of an
  `env:` entry.

  **The RBAC fact that matters is a write grant's absence, not a read grant.**
  A volume mount is fetched by the kubelet with the node's own credentials,
  never the pod's `ServiceAccount` token -- and that token is not even mounted
  on these pods (`automountServiceAccountToken: false`,
  `kubernetes/rbac/worker-rbac.yaml`'s deliberately empty `swarm-worker`
  Role, "a worker needs nothing from the Kubernetes API"). So this ConfigMap
  needs no `get`/`list`/`watch` rule added to that Role, and none is added --
  the pod has no token to make such a call with regardless. What has to be
  true, and is checked (**Tests**, below), is that no `RoleBinding` in the
  tenant namespace grants the tenant's worker KSA (or any subject bound to the
  `swarm-worker` Role) `update`, `patch` or `delete` on `configmaps`; only
  Terraform's deployer identity, via its own `kubectl`/provider credentials,
  ever writes it. `worker_env()` must never set a `SPEC_VERIFY_KEYS` /
  `SPEC_SIGNING_KEY` environment entry on a GKE Job either, alongside the
  Cloud Run names (the same allow-list test, extended to the GKE dispatcher).
* **The runtime alternative (decision 4).** The worker calls
  `GetPublicKey` on the version the document names, after the prefix check
  below, holds the PEM in memory for the attempt (one attempt is one process,
  so there is nothing longer-lived to cache in), and runs inside
  `startup_budget()`. That needs `roles/cloudkms.publicKeyViewer` on the key
  for every worker account, granted from `terraform/infra` and from
  `register-tenant.sh` for script-registered tenants, which puts
  `setIamPolicy` on the key back in the deployer's hands. It gives instant
  revocation (below) at that price. **Not verified:** whether `GetPublicKey`
  refuses a DISABLED version. If it does not, this alternative also needs
  `roles/cloudkms.viewer` on the key so the worker can read the version's
  state, and the live API has to answer that before it is chosen.
* **Rotation.** Cloud KMS does not rotate asymmetric keys on a schedule, so
  rotation is by hand and in this order: (1) create version N+1 (bootstrap, or
  `gcloud kms keys versions create`); (2) release, so every Job's
  `SPEC_VERIFY_KEYS` holds N and N+1; (3) point swarm-api's
  `SPEC_SIGNING_KEY_VERSION` (a full version name, because an asymmetric key
  has no primary version) at N+1 and release; (4) once no non-terminal task
  names version N, disable it and release. Several enabled versions verify at
  once; that is what makes steps 2 to 4 safe.
* **Revocation.** Disable the version. With the recommended rendering, every
  task signed by it is refused from the next release on; with the runtime
  alternative, from the next attempt start. A signing identity that was
  compromised could sign anything by submitting it through the API anyway, so
  revocation is about closing the leak, and a release is fast enough for that.
* **`AsymmetricSign` quota, not verified.** Every task and every workflow step
  is one `AsymmetricSign` call at submission (one per step, decision 2), so the
  platform's submission rate is Cloud KMS's request rate for this key. The
  default Cloud KMS asymmetric-sign quota is per project and per region and is
  shared with anything else in `saga-agents-staging` calling `AsymmetricSign`
  in `us-central1` -- including, potentially, the other team's own use of KMS
  in that project. **Not verified here:** the project's current quota value,
  its current headroom against the other team's usage, and the platform's
  peak submission rate against it. Before `enforce` is the default (section
  6), this needs a check against `gcloud services quota list` (or the Cloud
  KMS quota page) for this project, and a quota increase request filed ahead
  of the rollout if headroom is thin -- a KMS `RESOURCE_EXHAUSTED` here is a
  503 on every submission (this section's "Fails closed"), not a soft
  degradation.

#### 4. Signing in swarm-api

* **Where.** In `SubmissionService.submit_tasks` and `submit_workflow`
  (`apps/swarm-api/swarm_api/service.py`), after every write to the task has
  been made and immediately before `self._store.create_tasks` /
  `create_workflow`. For a workflow that is after the loop that writes
  `metadata.input_from` and after `record_expected_outputs`, both of which run
  after `_build_task`; signing inside `_build_task` would sign a spec missing
  both.
* **What.** For each task: `canonical_step_spec(task.to_firestore(),
  task_id=task.id)`, SHA-256, then KMS `AsymmetricSign` with `digest.sha256`
  and `digest_crc32c`, checking `verified_digest_crc32c` and the response's
  `name`. The three fields are set on the `Task`, so they are part of the write
  that creates the document, never a second update.
* **Refused before anything is signed.** `SpecNotCanonical` from the
  canonicaliser is 422 `invalid_input`, counted as a rejected submission like
  every other refusal.
* **Fails closed.** If KMS cannot sign, the submission answers 503 and nothing
  is stored. After the cutover (below) swarm-api never writes an unsigned
  task.
* **One sign call per step**, concurrently for a workflow, as the owner
  decided ("each step's canonical spec"). A per-workflow manifest signed once
  was considered (decision 2).
* **New dependency:** `google-cloud-kms` for `swarm-api` only.

#### 5. Verification in the worker

**Where.** In `Lifecycle._prepare`, **immediately after
`self.control.fetch_task()` (step 4, today around line 749)** and before
anything reads the document for what to do: before `_restore_checkpoint`,
before `_maybe_clone` (which reads the tenant's git token through
`_git_token`), before `_stage_declared_inputs`, before `_build_child_env`
(step 6, the provider credential), before `issue.stage_issue` (#265) and so
long before the runner child exists. Nothing between the generation check and
that point reads a spec or a secret: step 2 records the attempt and advances
it to RUNNING, and step 3 makes an empty workspace. It gets a phase of its
own, `verify_spec`, so the phase log proves the order. For a worker-action
profile (contract request 33) the same call runs at the same point, before the
action reads its secret.

**What it checks, in this order, refusing at the first failure.** Corrected
from the first draft of this entry, which put the format check first: an
unsigned legacy task has no `spec_format` either, so checking it before the
legacy rule refused every legacy task with `unknown_format` instead of
admitting it. The presence/legacy check has to run first.

1. `spec_signature` and `spec_key_version` are present, or the legacy rule
   below applies (in which case the task runs and checks 2 to 5 are skipped).
2. `spec_format` is a format this worker knows (1).
3. `spec_key_version` is `<SPEC_SIGNING_KEY>/cryptoKeyVersions/<digits>` and
   is a key of `SPEC_VERIFY_KEYS`. **Checked as a string, before any key is
   used:** the document can name any version of any key in any project, and
   the worker never goes looking for one.
4. `canonical_step_spec(doc, task_id=cfg.task_id)` succeeds, and its digest
   verifies against `spec_signature` with that version's public key
   (`cryptography`, added to the worker's dependencies explicitly).
   **`SpecNotCanonical` is a refusal, not a crash:** the document is writable
   by any agent of the tenant (**What is true today**, above), so a covered
   field can hold a value the canonicaliser rejects (an out-of-range integer,
   a non-finite float, a lone surrogate) without ever having gone through
   swarm-api's submission-time check. `_prepare` catches
   `SpecNotCanonical` from this call specifically and maps it to
   `SpecSignatureInvalid` with reason `not_canonical`, the same way an
   unverified signature is refused; letting it propagate would crash the
   worker process instead of failing the task cleanly.
5. The execution's own environment agrees with the signed spec:
   `RUNNER_PROFILE`, `TENANT_ID`, `TASK_TIMEOUT_SECONDS`, `REPOSITORY_URL` /
   `REPOSITORY_REF` when set, and the Job's own identity against the Job name
   the scheduler derived from `tenant_id` and `runner_profile` (one Job per
   tenant per profile). The scheduler derives `RUNNER_PROFILE`, `TENANT_ID`
   and `TASK_TIMEOUT_SECONDS` from the document, so a rewritten
   `runner_profile` that got a step dispatched onto another profile's Job is
   refused here even though step 4 alone would catch it too.

   **The two backends' Job-identity checks are not equally strong, and this
   entry says so rather than implying otherwise.** On **Cloud Run**,
   `CLOUD_RUN_JOB` is set by the platform itself -- the execution environment,
   not anything the scheduler wrote -- so comparing it catches a scheduler bug
   that dispatched onto the wrong Job while still copying the right
   environment variables across; that check is independent of the scheduler's
   own arithmetic. On **GKE**, there is no such platform-injected value: the
   dispatcher's manifest template sets `RUNNER_JOB_NAME` itself, from the same
   `tenant_id`/`runner_profile` computation that also produces the Job's
   actual `metadata.name` and the other env vars in the same render. **This
   check on GKE only confirms the scheduler agrees with itself within one
   render** -- it catches a copy-paste mismatch inside `GkeJobDispatcher`, not
   an independent confirmation of which Job the pod is actually running in the
   way Cloud Run's does. Closing that gap on GKE would need the worker to read
   its own Job's name from the Kubernetes Downward API (`metadata.name` into
   an env var, which needs no RBAC) rather than from a value the same
   dispatcher wrote twice; not proposed here, since GKE runs only the
   `browser` profile today and a wrong-Job dispatch there still fails step 4
   (`RUNNER_PROFILE` disagreement) or the tenant-namespace boundary itself.

**After it passes, the worker reads only the verified dict.** It already does:
the rest of the attempt reads `self._task`, and the later `fetch_task` calls
(`validate_generation`, `poll`, `_current_state`) read only the state,
generation and cancel flag, none of them covered.

**On a refusal.** A new `SpecSignatureInvalid(WorkerError)`. The lifecycle
closes the startup window and calls `control.finish(state=FAILED,
end_cause=SPEC_SIGNATURE_INVALID, exit_code=ExitCode.FAILED, error=<reason
and task id, no spec content>, result_summary={"spec_check": {"reason": ...,
"task_id": ..., "key_version": ..., "digest": <hex>}})`. **FAILED directly,
whatever attempts are left:** a retry reads the same document. The write is
fenced like every terminal write, so a superseded worker stands down instead.
The reason is a worker vocabulary, not a frozen one (`signature_mismatch`,
`unsigned`, `unknown_format`, `not_canonical`, `foreign_key_version`,
`environment_mismatch`, `upstream:<task id>:<reason>`), the way
`publish_reason` is. The log line is
ERROR and carries the same fields; the spec itself is never logged, since a
prompt can hold anything. The process exits 1.

**Outages are not refusals.** With the recommended rendering, verification
makes no network call. Under the runtime alternative, a KMS `UNAVAILABLE` or a
spent budget is exit 69 (`UNAVAILABLE`: nothing written, the reconciler
retries), a `PERMISSION_DENIED` is exit 78 (`CANNOT_START`: the worker's own
configuration), and only `NOT_FOUND` or a disabled or destroyed version is a
refusal.

**Worker-action steps verify their upstream steps too** (the owner's
decision). Before it reads its credential, contract request 33's
`post-verdict` step fetches `review.json` from the path it computes itself,
using the review task id **named in its own signed `metadata.dispatch.verdict_source`**
(`{review: <review task id>}`, the same way `merge`'s `dispatch.merges` block
names task ids, not through `input_from`). **Corrected from the first draft of
this entry, which said
`post-verdict` uses `input_from`:** contract request 33's joint review with
this entry, 2026-09-29, removed `post-verdict`'s `input_from` on purpose --
the ordinary staging path resolves an artifact's location from the upstream
task's own `result_summary`, a Firestore field the tenant identity writes,
which is exactly the tenant-writable pointer this whole design exists to stop
trusting. `merge` verifies the **union** of every task its own signed spec
names: the `dispatch.merges` block (author, review, **post-verdict**, fix,
proof) and `input_from`. `post-verdict` is in that union because `merge`
trusts what `post-verdict` posted to GitHub every bit as much as it trusts
`review`'s verdict file. Each upstream task must pass checks 2 to 4 above
**with no legacy exception**, and in addition carry the acting step's own
`tenant_id` and `workflow_id` and the role contract request 33's `single-pr`
table gives it: `reader` for `review` and `proof`, `amender` for `fix`,
`author` for the implement step, and **`none`, the same as `merge` itself,
for `post-verdict`** -- it clones nothing, holds no `pr_role`, and only reads
an upstream verdict and posts it. (This entry's earlier draft gave
`post-verdict` role `reader`; contract request 33's own table has always said
`none`, and this entry now agrees with it rather than the other way round.)

**The end causes are deliberately not unified with #342's own (the owner's
decision, 2026-09-29): a worker-action step's own spec failing its own check
is still `SPEC_SIGNATURE_INVALID`,** exactly as for an agent step. **An
upstream task failing this check is contract request 33's own concern, not
this entry's, and keeps that entry's own end causes:** `merge` ends
`EndCause.MERGE_REFUSED`, and `post-verdict` ends `EndCause.VERDICT_REFUSED`
(contract request 33 names and owns both; this entry only requires that an
upstream-spec failure use them, never `SPEC_SIGNATURE_INVALID`). **The owner's
decision, 2026-09-29: both report through the same field this entry uses for
its own refusals,** `result_summary.spec_check.reason`, with the value
`"upstream:<task id>:<why>"` -- the same shape `SPEC_SIGNATURE_INVALID` uses
for its own failures, so a reader (or an alert) does not need two different
field names to find out why a spec check failed. **Decision 9's alert is keyed
on both:** a log-based metric over `end_cause == SPEC_SIGNATURE_INVALID` alone
would miss every worker-action refusal caused by a tampered upstream spec,
since those end `MERGE_REFUSED` or `VERDICT_REFUSED`; the alert matches
`end_cause == SPEC_SIGNATURE_INVALID` **or** (`end_cause` in `{MERGE_REFUSED,
VERDICT_REFUSED}` **and** `result_summary.spec_check.reason` starts with
`"upstream:"`), because both are the same threat -- a spec that does not
verify -- surfacing through two different steps' vocabularies, and `MERGE_REFUSED`
/ `VERDICT_REFUSED` have other, unrelated causes (contract request 33's own
table) that this alert must not also fire on. Verifying an upstream spec proves that the step
the action trusts was submitted as that step of this workflow and still
carries that spec. It does not prove what that step produced (next section).

#### 6. Rollout: legacy unsigned tasks

Every task written before swarm-api signs carries no signature, and some of
those will still be parked or queued when the verifying worker ships. Signing
cannot be retrofitted onto them: re-signing a document as it now stands would
sign whatever an agent already wrote into it.

* **`SPEC_SIGNATURE_MODE`**, on the worker Jobs: `enforce` (**the default**)
  or `legacy`. In `legacy`, an unsigned task runs, with a WARNING log and a
  `spec_check: {"reason": "legacy_unsigned"}` note on its event, **only if the
  document's Firestore `create_time` is earlier than `SPEC_LEGACY_CUTOVER`**
  (an RFC 3339 time, the moment the signing swarm-api revision took all
  traffic). `create_time` is set by Firestore and no client can write it, so
  stripping the signature off a task created after the cutover does not
  qualify it; the tenant's role has no delete, so the document cannot be
  re-created with an earlier one either. `fetch_task` returns the snapshot's
  `create_time` beside the dict for this.
* **The end date is in the code, not only the configuration:**
  `SPEC_LEGACY_UNTIL = 2026-10-20T00:00:00Z` (decision 5). After it the worker
  ignores `legacy`, logs that it did, and enforces. A flag nobody remembers to
  turn off stays off anyway.
* **Before the end date**, an operator counts non-terminal tasks without
  `spec_signature` (a read-only query) and lets them finish or cancels and
  resubmits them; after it, any left fail `SPEC_SIGNATURE_INVALID` with reason
  `unsigned`.
* **Worker-action steps never take the legacy path, for themselves or their
  upstream steps.** Contract request 33 is not built yet, so no legacy
  worker-action task can exist.
* **Order:** (1) the key and its IAM (bootstrap), then the contract fields and
  `specsign.py`; (2) swarm-api signs, and its release time becomes
  `SPEC_LEGACY_CUTOVER`; (3) the worker verifies, with `legacy` and the
  cutover set; (4) `enforce` by the end date at the latest. A worker that
  enforces before swarm-api signs refuses every task, which is why (2) and (3)
  are separate releases.

#### 7. Continuations and retries

* **A retry is another attempt at the same task.** Same document, same spec,
  same signature, nothing re-signed. That is exactly why no field a retry
  changes (`attempt_count`, `current_generation`, `state`, `next_eligible_at`,
  `startup_refunds`) is covered. The scheduler's
  `return_to_ready_after_failed_dispatch` and the reconciler's
  `repair_task_state` write none of the covered fields, and a test keeps it so.
* **A continuation (#273's `continues_task`) is a new submission.** It is a
  `direct-pr` workflow submitted through `submit_workflow`, so its task is
  signed at its own submission, and the continued task id is inside its signed
  `metadata.dispatch.continues`. An agent therefore cannot point a
  continuation at another task's branch. The continued task's own signature is
  never consulted: nothing about the continued task's spec is read, only its
  id, from which the worker derives the branch (`continuation.py`).
* **Contract request 30's continuation-scoped accounts** submit through the
  same code, so their tasks are signed identically and `submitted_by` records
  the account.
* **Any future re-run that creates a new task** goes through swarm-api and is
  signed at its own submission. No path may copy a signature from one task to
  another; it would not verify anyway, because `task_id` is inside the bytes.

#### 8. The threat model

**What signing closes.** A rewrite, by any agent of the tenant (or by anything
holding any worker's Firestore grant), of a step's `input` (prompt and
inputs), `input_from`, `expected_outputs`, dispatch roles, `depends_on`,
profile, class, timeout, attempt cap or repository, **before or between that
step's attempts**. The worker refuses it before any credential is read and
before any agent or action starts. Copying one task's signature onto another,
moving a spec to another workflow or tenant, forging a new task document, and
naming a key the platform did not publish are all refused the same way.

**What signing does not close,** stated plainly because the platform's
exposure is otherwise unchanged
([multi-tenancy.md](multi-tenancy.md#the-firestore-row-in-full)):

* **The restored workspace, a channel the signature does not cover at
  all.** `latest_checkpoint` is in the "not covered" list (section 1) because
  the reconciler and the worker itself write it legitimately mid-attempt. But
  that also means a signed, unmodified, correctly-verifying spec can still
  start an attempt against a workspace restored from a **planted**
  checkpoint: the signature is over the spec, never over what
  `_restore_checkpoint` pulls from GCS before the runner starts. Joint review
  finding, 2026-09-29 (MAJOR): a planted checkpoint gets past this entry's
  signature entirely, because there was never anything here for it to get
  past.

  **Fixed separately, ahead of this entry: S0 #347, closed by PR #348.**
  #348 makes `_restore_checkpoint` accept only the checkpoint this task's own
  earlier attempt recorded on its own attempt document (never
  `find_latest()`'s scan of the task's GCS prefix), and never restores
  anything on a first attempt. **State it as #348's own body states it, not
  more strongly:** this closes the reproduction in #347 (an implement agent
  planting for a parked review step's first attempt) and, on a retry, every
  path limited to the GCS bucket -- a checkpoint added beside the recorded one,
  or the recorded one's archive rewritten in place. **It does not stop an
  agent that can also write Firestore.** Firestore has no document-level IAM
  (`multi-tenancy.md`), so on a retry such an agent can still raise the
  victim task's `attempt_count`, repoint `latest_checkpoint`, and write a
  forged `attempts/<id>` document naming its own planted checkpoint and
  digest -- every check #348 added then passes, because all of it reads
  Firestore fields and documents the tenant identity can write. #348's own
  words: "the complete fix needs a record the tenant cannot write." Signed
  step specs do not supply that record: `latest_checkpoint` and `attempt_count`
  are both explicitly in contract request 34's own "Not covered" list
  (section 1, above), by design -- the scheduler and the worker legitimately
  rewrite both after submission, so signing either would break on the first
  honest write -- and the `attempts/<id>` document is a different Firestore
  collection entirely, never a candidate for this entry's per-task signature
  in the first place. Extending coverage to close this was not the owner's
  decision (decision 7 covers only re-checking a signed *parent's* `state`, a
  different field, for a different attack).

  **The owner's decision, 2026-09-29: this residual is ACCEPTED, not a
  precondition of anything closing.** Firestore-capable planting on a retry
  remains open until workers leave direct Firestore access (the alternative
  **If it is declined** already names, and the same one #347's issue and
  #348's PR both point at), a materially larger change than either #347 or
  this entry. Contract request 34 closes the rewritten-prompt path #342
  reproduced; it was never going to also close a checkpoint-planting path
  that reads and writes fields it deliberately does not sign.
* **Every field that is not covered.** A tenant agent can still write a
  task's `state`, `result_summary`, `cancel_requested`, `latest_checkpoint`
  and the rest. In particular:
  * **It can mark a parked step READY early**, without touching
    `depends_on`, and the scheduler will admit it. The signature still
    verifies, because the spec is unchanged. In contract request 33's chain
    that would let `fix` start before `post-verdict` has posted the review,
    which is the ordering merge-step.md §4.3 relies on to keep `fix`'s agent
    away from `review.json`. Decision 7 adds a start-time re-check of each
    signed parent's state, which forces the attacker to forge those states as
    well; it raises the cost, and does not close it.
  * **It can re-run a finished signed step by rewriting its state.** `state`
    and `end_cause` are both uncovered, so an agent can write a SUCCEEDED or
    FAILED task's `state` back to `READY` (or `QUEUED`, ahead of admission)
    and have the scheduler dispatch a fresh attempt against the same,
    still-verifying spec -- the signature says nothing about how many times a
    step may run. A re-run costs the tenant's own capacity and attempt cap,
    and can re-execute a step whose real effect (a merge, a posted review) has
    already happened and is not naturally idempotent. Contract request 33's
    sha-equality chain and GitHub-side checks are what stop a re-run from
    double-merging or double-posting; this entry stops none of it, because
    `state` is not a field this entry signs.
  * **It can forge an upstream step's `result_summary` or its GCS
    artifacts.** Contract request 33 answers that for the merge with the sha
    equality chain and the GitHub review `post-verdict` posts, not with this
    entry.
* **The workflow document.** `WorkflowStep.input` is a copy for display. No
  worker reads it, so a rewrite of it misleads a person reading the API or the
  UI and changes nothing that runs.
* **Everything outside `tasks/*`:** `tenants/<id>` (whose `service_account`
  and `gcs_prefix` the control plane trusts as configuration), `pools/*`,
  `quota/*` and `leases/*`. These need the fix multi-tenancy.md names, the
  worker off direct Firestore access or one database per tenant.
* **Capacity spent before the refusal.** The scheduler admits on the
  document's `resource_class` before any worker verifies, so a rewritten
  class costs the tenant's own capacity for one attempt (decision 8).
* **Anyone who can sign -- an accepted residual, not a closed one (owner,
  2026-09-29).** `swarm-api`'s own account, which holds `signer`; the release
  deployer, which deploys the Cloud Run revision that runs as it; and, because
  the key stays in the shared `saga-agents-staging` project rather than a
  dedicated one, **any Owner or Editor of that project** -- `roles/editor`
  carries `iam.serviceAccounts.actAs` and `run.services.update`, enough to
  redeploy `swarm-api` running arbitrary code as its own service account, and
  an Owner can `setIamPolicy` the key, the ring or the project directly
  regardless of where the key's Terraform lives. **A KMS admin who creates or
  imports a version** gets it trusted by every worker at the next release,
  without ever signing anything themselves (**Key management**'s "Trusted
  versions", above). Putting the key's own IAM binding in `terraform/bootstrap`
  keeps the *CI deployer* off `setIamPolicy` on the key; it was never a
  boundary against the shared project's own Owners and Editors, and this entry
  no longer implies that it is. **This residual holds against the platform
  team and the other team sharing the project. It does not hold against
  tenant agents:** no tenant or worker account holds any role on this key, so
  a tenant's own agent -- the actor #342 and this entry are about -- has no
  KMS-side path to a valid signature at all.
* **Anything submitted through the API.** A signature says swarm-api accepted
  this spec from an authenticated submitter. It does not say a person meant
  it: a continuation-scoped account (contract request 30) or a stolen ID
  token gets a valid signature on what it submits.
* **Prompt injection** of the step itself (contract request 33's T11).

#### 9. Tests that would prove it

All offline, as CLAUDE.md requires: no KMS, no credentials, no emulator. The
signer in tests is a local P-256 key from `cryptography` behind the same
interface as the KMS signer, and the worker's `SPEC_VERIFY_KEYS` holds its
public key. Each goes to CI red first, in the shape CLAUDE.md gives.

* **`tests/unit/common/test_specsign.py`** (the canonical form).
  RFC 8785's own number and ordering vectors (Appendix B, and the sorting
  example in 3.2.3); a pinned golden vector, one fixed task dict to fixed bytes
  and a fixed SHA-256 hex, so any change to the projection is a visible diff
  that demands a format bump; agreement with the `rfc8785` package as a
  test-only oracle over generated JSON values; and refusal of 2^53, NaN,
  infinity and a lone surrogate.
* **`tests/unit/common/test_specsign_covers.py`** (the rule that keeps the
  field list honest). Every metadata key the worker reads (`inputs.METADATA_KEY`,
  `expected_outputs.METADATA_KEY`, `"dispatch"`, found by an AST scan of
  `agent_worker`) is in `SIGNED_METADATA_KEYS`, and `SIGNED_METADATA_KEYS`
  equals swarm-api's `RESERVED_METADATA_KEYS` minus `startup_refunds`.
* **`tests/unit/control_plane/test_spec_signing_submission.py`** (swarm-api).
  Every task from `submit_tasks` and `submit_workflow` carries the three
  fields, and its signature verifies over its stored form; the signed bytes
  include the task-id-keyed `input_from` and the `expected_outputs`, so a
  signature made before either was written fails this test; a KMS failure is
  503 and the store is never called; each non-canonical value is 422 and KMS
  is never called.
* **`tests/unit/worker/test_spec_signature_worker.py`** (the worker).
  Parameterised over **every covered field**, nested ones included
  (`input.prompt`, `input.issue`, `metadata.dispatch.role`,
  `metadata.dispatch.integrates` reordered, a value of `metadata.input_from`,
  `depends_on` reordered or shortened, `repository_ref`): a rewrite after
  signing ends the task FAILED with `spec_signature_invalid` on attempt 1 of 3,
  and the fakes record **no runner child, no secret read, no git token read,
  no GCS download and no checkpoint restore**. Parameterised over every
  uncovered field (`state`, `attempt_count`, `result_summary`,
  `startup_refunds`, `priority`, a caller metadata key): the task runs. A
  signature copied from another task, a version outside `SPEC_SIGNING_KEY`, a
  version not in `SPEC_VERIFY_KEYS`, a `RUNNER_PROFILE` override that
  disagrees, and (Cloud Run only, where the platform sets `CLOUD_RUN_JOB`
  independently of the scheduler) an execution whose `CLOUD_RUN_JOB` names a
  Job other than the one the scheduler derived from `tenant_id` and
  `runner_profile`, are each refused. A separate GKE case proves the weaker
  claim honestly: a `RUNNER_JOB_NAME` that disagrees with the dispatcher's own
  `tenant_id`/`runner_profile` computation in the same render is refused, but
  a test also documents that this check cannot catch the dispatcher deriving
  the wrong Job **consistently** (setting `RUNNER_JOB_NAME` and the Job's real
  `metadata.name` to the same wrong value), since GKE has no platform-injected
  value to check against. A covered field holding
  a value `canonical_step_spec` rejects (an integer past 2^53 − 1, a
  non-finite float, a lone surrogate -- written directly to Firestore, not
  through swarm-api's submission check) is refused `not_canonical`, and the
  worker process does not crash. **The checks run in the order section 5
  gives**, proven by a legacy-eligible task with no `spec_format` at all
  (never `unknown_format`) alongside an out-of-window unsigned task that
  correctly still gets `unsigned`. The phase log shows `verify_spec` before
  `restore_checkpoint`, `clone`, `stage_inputs`, `credentials` and
  `fetch_issue`.
* **`tests/unit/worker/test_spec_legacy_window.py`.** Unsigned in `enforce`:
  refused. In `legacy` with `create_time` before the cutover: runs, with the
  note. After the cutover: refused. After `SPEC_LEGACY_UNTIL`, with an injected
  clock: refused, and the log says the flag was ignored.
* **Worker-action upstream checks**, in contract request 33's own test files
  when it lands: `post-verdict` and `merge` refuse an upstream task that fails
  verification, is unsigned (in `legacy` mode too), belongs to another
  workflow or carries the wrong role -- **`post-verdict` included** in
  `merge`'s upstream set, with role `none` (matching contract request 33's own
  `single-pr` table); the merge and review credentials are never read in any
  of those cases; and the end cause on such a refusal is `EndCause.MERGE_REFUSED`
  (`merge`) or `EndCause.VERDICT_REFUSED` (`post-verdict`) -- contract request
  33's own causes, never `SPEC_SIGNATURE_INVALID` -- with
  `result_summary.spec_check.reason == "upstream:<task id>:<why>"`. That cause
  is reserved for the acting step's own spec, and a test in this entry's own
  suite asserts the two causes are never confused for the acting step's own
  failures.
* **Writers.** A test that the scheduler's and the reconciler's task updates
  (`return_to_ready_after_failed_dispatch`, `cancel`, `repair_task_state` and
  the rest) write no covered field; that **swarm-api's own writes to an
  existing task** (`Store.request_cancel`/`cancel_workflow`, `store.py`, which
  patch `cancel_requested`, `state` and `completed_at` -- the only writes
  swarm-api itself makes to a task it did not just create -- and any future
  one) also write no covered field --
  the three signed fields are written once, at creation, and never again
  (section 2's diff comment), so this test fails loudly the day a second
  swarm-api write path touches, say, `input` or `metadata.dispatch`; and that
  `worker_env()` never sets `SPEC_VERIFY_KEYS`, `SPEC_SIGNING_KEY`,
  `SPEC_SIGNATURE_MODE` or `SPEC_LEGACY_CUTOVER` **on either dispatcher** --
  Cloud Run's per-execution override and the GKE manifest's template
  substitution both go through the same allow-list check.
* **The ledger.** `test_outcomes_end_cause.py` gains the class, and
  `DERIVE_VERSION` is bumped.
* **Terraform (`tests/terraform`).** The key's purpose and algorithm; its
  `managed-by` label; `swarm-api` is the only member holding `signer`, and no
  tenant or worker account holds `signer` or `signerVerifier`; the Cloud Run
  Jobs' environment carries `SPEC_VERIFY_KEYS` built from enabled versions
  only; `cloudkms.googleapis.com` is enabled; the key ring and the key's IAM
  member are in `unlabelable-types.json`.
* **GKE (`tests/unit/worker/test_kubernetes_manifests.py` and
  `tests/terraform`).** The rendered `swarm-spec-verify-keys` `ConfigMap`
  carries the same `{version: PEM}` map as `SPEC_VERIFY_KEYS`, lives in the
  tenant's own namespace, and is applied by Terraform's identity, not the
  scheduler's per-task render; `worker-job-browser.yaml`'s manifest and the
  dispatcher's rendered Job agree that neither sets `SPEC_VERIFY_KEYS` or
  `SPEC_SIGNING_KEY` in `env:` and both mount the ConfigMap read-only.
  **The write-grant check is wider than one `RoleBinding` lookup, because a
  write path could come in by any of several doors:**
  * no `RoleBinding` in the tenant namespace (`kubernetes/rbac/*.yaml`) grants
    `update`, `patch` or `delete` on `configmaps` to the `swarm-worker` Role,
    to any subject bound to it, or **to the tenant's GSA named either way a
    Kubernetes subject can name it** -- by email
    (`swarm-agent-worker-<tenant>@<project>.iam.gserviceaccount.com`) and by
    its numeric `uniqueId` -- since an email-only check here would repeat the
    defect a prior lane already measured elsewhere in this platform: an
    email-only RBAC subject can apply cleanly and bind nothing, which reads as
    a passing test while authorising nobody, but the reverse -- a binding
    keyed on `uniqueId` alone -- would be invisible to a check that only
    greps for the email;
  * no `ClusterRoleBinding` grants it either, cluster-wide, to any subject --
    matching `kubernetes/rbac/worker-rbac.yaml`'s existing invariant that no
    `ClusterRole` or `ClusterRoleBinding` appears anywhere in `kubernetes/` at
    all, which this ConfigMap must not become the first exception to;
  * and, in `tests/terraform`, no project-level IAM binding grants the
    tenant's GSA a `container.*` role or permission (`roles/container.admin`,
    `roles/container.developer`, or a custom role naming
    `container.configMaps.update` or `container.secrets.update`) that would
    let it write the object through the GKE API directly, bypassing in-cluster
    RBAC entirely -- the tenant's GSA holds no `container.*` role today
    (Terraform's `modules/iam`), and this test pins that absence rather than
    assuming it.

  This is the same assertion style `test_kubernetes_manifests.py` already uses
  to hold the empty worker Role empty.
* **Live, in dev, once, after (3) of the rollout.** Submit a two-step workflow,
  rewrite the parked step's `input.prompt` with the tenant worker account's
  own credentials, and see the step end `spec_signature_invalid` with no
  agent started. Worth keeping as a `make smoke` case, since this is the one
  guarantee the entry exists for.

### What it would break if accepted

* **Nothing stored.** The three fields are optional and an old document
  decodes with None. What an unsigned task does is the rollout's question
  (section 6), not the decoder's.
* **Every writer that creates a task document must sign.** Today that is only
  swarm-api's `Store`. The integration tests that seed task documents
  directly in the emulator need the test signer.
* **The API refuses three inputs it accepts today**: integers past 2^53 − 1,
  non-finite numbers and lone surrogates. No declared input's bounds reach
  2^53 (the largest is `issue`'s 999999), so only undeclared profiles' inputs
  (`browser`, `generic`) can be affected.
* **A submission now depends on Cloud KMS.** A KMS outage stops submissions
  (503) instead of accepting unsigned ones; that is the point.
* **Every restatement of `EndCause` follows:** the outcome ledger
  (`swarm_api.outcomes`, with its `DERIVE_VERSION` bumped), the UI's
  `outcomes.ts`, `plugin/README.md` and `docs/workflows.md`.
* **Terraform:** a new API, a key ring and key in bootstrap, two entries in
  `unlabelable-types.json`, Cloud Run worker environment in `terraform/infra`,
  and a per-tenant `swarm-spec-verify-keys` `ConfigMap` (**Key management**,
  above) that `register-tenant.sh` and every existing tenant's namespace need.
* **`kubernetes/`:** `worker-job-browser.yaml` gains a read-only volume mount
  for the ConfigMap; `test_kubernetes_manifests.py` gains the assertion that
  no RoleBinding grants the worker KSA write on it.
* **If contract request 3 is applied** (`input_from` as a `Task` field), the
  projection changes and that is format 2.

### If it is declined

#342 stays open at S0. Every multi-step workflow runs whatever its parked
steps' documents say when they start, and contract request 33's merge step has
no answer to its round-3 blocker: a review step whose prompt an earlier agent
rewrote produces a real, honest-looking verdict that `post-verdict` would post.
The remaining alternative is the one multi-tenancy.md names, taking workers
off direct Firestore access, which closes this and much more at a far larger
cost.

### Invariants

- **Invariants 1 to 3.** Unchanged. Signing happens before the task exists;
  a refusal is an ordinary terminal write that releases the lease in the same
  way as every other.
- **Invariant 4.** No wait is added. With the recommended rendering,
  verification makes no network call; under the alternative the one call is
  inside the startup budget, and an outage is exit 69, not a sleep.
- **Invariant 5.** Verification runs after the generation check, and the
  refusal's terminal write is fenced: a stale worker stands down without
  writing it.
- **Invariants 6 and 7.** Untouched.
- **Invariant 8.** Untouched. A refused attempt has done no work to keep.
- **Invariant 9.** Strengthened within a tenant, which is where #342 lives,
  and across tenants for task documents: the signature binds `tenant_id`, and
  `_assert_tenant` keeps its check. No tenant account gains any KMS role.
- **Invariant 10.** The signature binds exactly what the caller chose by name
  and what swarm-api derived from it. The caller still sends no image,
  command, resource spec or backend parameter, and cannot choose a key.
- **#219.** Verification runs before any secret is read, so a refused step
  never holds the tenant's git token or provider key.

### Owner decisions, each with a recommendation

1. **The covered fields.** Recommended: the table in section 1, with
   `priority` and the caller's own metadata keys left out.
2. **One signature per step, or one per workflow.** Recommended: one per step,
   as decided on 2026-09-29. A per-workflow manifest is one sign call instead
   of N, but every worker would read and verify the whole workflow's specs, and
   a standalone task would need a second shape.
3. **The canonicaliser.** Recommended: RFC 8785 written into `specsign.py`,
   with the `rfc8785` package as a test-only oracle, because `swarm_common`
   takes no dependency.
4. **Where the key lives, and how workers get the public key.** Recommended:
   ring, key and the `signer` binding in `terraform/bootstrap`; public keys
   rendered by `terraform/infra` into the Jobs' `SPEC_VERIFY_KEYS`; no KMS role
   for any worker account; the deployer holds only `publicKeyViewer` and
   `viewer`. The alternative (runtime `GetPublicKey` with `publicKeyViewer` on
   every worker account) revokes faster but returns `setIamPolicy` on the key
   to the deployer.
5. **The legacy window.** Recommended: `enforce` by default, `legacy` only
   for tasks whose Firestore `create_time` is before the cutover, and a hard
   `SPEC_LEGACY_UNTIL` of 2026-10-20.
6. **What a refusal does.** Recommended: FAILED at once, not retried and not
   parked, with `SPEC_SIGNATURE_INVALID` and an alert.
7. **A start-time re-check of parent states for every step with `depends_on`**
   (not only worker actions): each signed parent must be SUCCEEDED, or the
   worker parks the task back as `DEPENDENCY_INCOMPLETE` without running.
   Recommended: yes, as defence in depth, stated as raising the cost of the
   early-release rewrite rather than closing it.
8. **Verification at admission too.** Recommended: not now. The worker is the
   gate; admission's hot path stays one query, and the cost of a rewritten
   class is one attempt of the tenant's own capacity.
9. **An alert on `spec_signature_invalid`.** Recommended: yes, a log-based
   metric and an alert in `terraform/modules/monitoring`, because every
   occurrence is an attack or a bug. **Keyed on both end causes** (**Verification
   in the worker**, above): `end_cause == SPEC_SIGNATURE_INVALID` for an
   acting step's own spec, or (`end_cause` in `{MERGE_REFUSED, VERDICT_REFUSED}`
   and `result_summary.spec_check.reason` starts with `"upstream:"`) for an
   upstream one -- the owner did not unify the end causes themselves, so the
   metric is what ties the two vocabularies to one alert, and the reason
   prefix is what keeps it from also firing on `MERGE_REFUSED` /
   `VERDICT_REFUSED`'s many other, unrelated causes.
10. **`SOFTWARE` or `HSM` protection.** Recommended: `SOFTWARE`. The private
    key never leaves Cloud KMS either way, and the threat here is who may call
    `AsymmetricSign`, which IAM decides, not extraction of the key.

### Addendum 2026-09-29: corrections after acceptance

The accepted text above is left as the owner accepted it. Three statements in
it were found wrong while building it (#353, #354), and one decision was added:

1. **The GKE Job name is derived from the task, not the tenant and profile.**
   Requested change item 5 says the worker compares its identity against "the
   Job name the scheduler derived from `tenant_id` and `runner_profile` (one
   Job per tenant per profile)". That is Cloud Run's rule
   (`swarm-job-<tenant>-<profile>`). `GkeJobDispatcher` creates one Job per
   attempt, named `sanitize_name("swarm", <task id without "task_">,
   <generation>)`, that is `swarm-<hex>-<gen>`
   (`apps/scheduler/scheduler/dispatch.py`), and the worker checks
   `RUNNER_JOB_NAME` against `specverify.gke_job_name(task_id, generation)`.
   The weaker-claim paragraph about GKE still holds: the name and the
   environment come from one render.
2. **`cloudkms.googleapis.com` is enabled in bootstrap, not in
   `terraform/infra/main.tf`.** The key ring and key live in
   `terraform/bootstrap/spec_signing.tf` (#354), which enables the API and adds
   it to `prerequisite_services`; `terraform/infra` only reads the key's
   versions. Section 3's first bullet names the wrong root.
3. **A GKE worker reads the signature mode and the legacy cutover from the
   ConfigMap mount too** (owner decision, 2026-09-29). The text above has the
   GKE mount carrying only `SPEC_VERIFY_KEYS` and `SPEC_SIGNING_KEY`, which
   left a GKE worker in `enforce` from its first day while Cloud Run ran
   `legacy`. The `swarm-spec-verify-keys` ConfigMap rendered by
   `kubernetes/render.py` (#354) carries `SPEC_SIGNATURE_MODE` and
   `SPEC_LEGACY_CUTOVER` as files beside the keys, and the worker reads each
   of the four from its environment first and from
   `/etc/swarm/spec-verify-keys` where the environment has none. GKE therefore
   follows the legacy window exactly as Cloud Run does, including
   `SPEC_LEGACY_UNTIL`.
4. **A worker with no keys at all is CANNOT_START for every task, signed or
   not.** The key check now runs before the legacy rule, so a GKE pod whose
   namespace has no ConfigMap (the volume is `optional`) can neither admit an
   unsigned task through a mode it could not read nor refuse a tenant's task
   as a signature failure: it exits `ExitCode.CONFIG`, the operator's fault,
   loudly.

---

## 35. `profiles.py` / `models.py`: the `post-verdict` worker-action profile, and its own end causes

**Superseded 2026-10-06 (lane MS0, part of #352):** the merge step anchors the verdict on the review's verdict file and green required checks, not on an App's GitHub review, and there is no review App ([2026-10-06 revision](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs), §3). The applied entry stays disabled; retiring it is that revision's request (B), the owner's decision.

**Status: ACCEPTED — accepted by the owner 2026-10-01, as written; APPLIED 2026-10-01**
(functionality wave 3, lane M1): `WorkerAction.POST_VERDICT`, the
`post-verdict` entry (`available=False` until #342 is enforced and the review
App exists) and `VERDICT_REFUSED`/`VERDICT_FAILED`. Its MAJOR 1 companions:
the API routes' refusal is applied (request 33's amendment, item 1); the
`secret_manager` accessor override and the refresh exclusion are not yet
(lane M2). Part of #295. Recorded from
[merge-step.md](merge-step.md)'s design (contract request 33, ACCEPTED by
the owner 2026-09-29 as the design for #295), which pointed at this as a
separate, not-yet-filed request ([merge-step.md](merge-step.md)'s own §10
build item 2, §6a — not contract request 33's; §10 and §6a are merge-step.md's
own sections). The design is merge-step.md §4.3 and §6a. Nothing under
`apps/common/swarm_common/` has been edited.

**This request depends on contract request 33's `WorkerAction` enum and
`RunnerProfile.worker_action` field, which it extends rather than
restates.** If 33 is not yet applied when this is decided, apply 33 first;
this diff assumes it exists.

### What is true today

Contract request 33 gives `merge` a worker-action profile so an App key
never sits in a container that runs an agent. The verdict-anchoring design
in merge-step.md needs the identical isolation one step earlier in the
chain, for a different key: a GitHub PR review posted by a `swarm-review`
App, using the `swarm-tenant-<tenant>-git-review` secret. If that key were
held by the review step's own agent-running container (a `claude-code-review`
profile, contract request 36), the review agent — which reads
attacker-controlled diffs — would share a container with it, exactly the
exposure request 33's own §1.1 already rejects for `merge`. `post-verdict`
is the same shape as `merge`: a worker-action profile that runs no agent,
reads `review.json` (from the review-only-writable GCS prefix, at a path it
derives from its own signed dispatch, not through ordinary `input_from`;
merge-step.md §4.3), and posts the GitHub review.

`EndCause` has no value that says a verdict-posting attempt was refused or
failed, for the same reason request 33 gave `merge` its own pair: recording
one as `RUNNER_ERROR` would be false (no runner ran), and `None` sends the
outcome ledger back to classifying text.

### The requested change

In `profiles.py`, extend the `WorkerAction` enum contract request 33 adds:

```python
class WorkerAction(str, Enum):
    """A platform action the WORKER performs instead of starting a runner."""
    MERGE = "merge"
    POST_VERDICT = "post_verdict"
```

A catalogue entry:

```python
"post-verdict": RunnerProfile(
    name="post-verdict",
    image="agent-runtime-base",
    resource_class="standard",
    backend=Backend.CLOUD_RUN_JOB,
    runner_argv=(),
    worker_action=WorkerAction.POST_VERDICT,
    # The credential is never mounted: its secret is read by the worker at
    # post-time, as `swarm-<tenant>-post-verdict`, the Job's own service
    # account. `provider` is what parks the step CREDENTIAL_MISSING, at no
    # cost, for a tenant that has not registered one, and keeps Terraform
    # from creating a post-verdict Job for that tenant.
    #
    # Listing `git-review` here is not by itself sufficient to keep the
    # ordinary worker account off this secret (security review 2026-09-29,
    # MAJOR 1, NOT_YET) -- see "What it would break if accepted" below for
    # the three companion changes this entry depends on.
    provider="git-review",
    secrets=(),
    timeout_seconds=300,
    inputs={},
),
```

`__post_init__`'s existing refusal (request 33: `worker_action` requires
empty `runner_argv`, and vice versa) already covers this entry; nothing new
is needed there.

In `models.py`, two more `EndCause` values, written only by the worker,
alongside request 33's `MERGE_REFUSED`/`MERGE_FAILED`:

```python
VERDICT_REFUSED = "verdict_refused"   # a condition for posting the review was not met; nothing changed on the forge
VERDICT_FAILED = "verdict_failed"     # the post was allowed, and the forge did not do it
```

The specific reason goes in `result_summary.spec_check.reason` for a signed-
spec failure (`upstream:<task id>:<why>`, CR 34's own field and format — not
a `post-verdict`-only convention) and in `result_summary.verdict.refusal`
for every other refusal code (`verdict_unreadable`, `verdict_prefix_unavailable`,
`forge_unavailable`, `forge_refused`, ...). merge-step.md §6a lists every
code and its cause.

### What it would break if accepted

* **MAJOR 1 (security review 2026-09-29, NOT_YET): the catalogue entry alone
  makes the tenant's ORDINARY worker account a reader of the review App
  key.** `known_providers()` (`apps/swarm-api/swarm_api/validation.py::known_providers`)
  derives its accepted set from the catalogue, so `POST /me/credentials`
  (`apps/swarm-api/swarm_api/routes/tenants.py::put_credential`) and the admin route
  (`apps/swarm-api/swarm_api/routes/admin.py::_check_provider`) both accept `git-review`
  as an ordinary, registrable provider name; `credentials.py`'s
  `_grant_accessor` (202-235) then grants `tenant.service_account` — the
  tenant's ordinary worker account, not `post-verdict`'s — `secretAccessor`
  on it; and `terraform/modules/secret_manager/main.tf` (`resource "google_secret_manager_secret_iam_binding" "accessor"`) writes
  an authoritative `iam_binding` (not an additive `iam_member`) whose
  `members` list is `[cfg.accessor]` for every listed provider
  (`terraform/infra/main.tf` (`accessor      = cfg.accessor`) sets `cfg.accessor` to the worker
  account for every tenant), which on its next apply would also REMOVE any
  post-verdict accessor granted separately. `enable_subscription_refresh`
  compounds it: the refresher's `-refresh` twin grants the same worker
  account `secretVersionAdder` the same way. Left as written, any agent
  running under the tenant's worker account can mint that account's token,
  read the review App's private key out of Secret Manager, and post its own
  `APPROVE` review — the exact App-key theft B1 exists to close
  (merge-step.md §7, T3). **This request is accepted only together with:**
  * the API credential routes (`routes/tenants.py`, `routes/admin.py`) and
    `register-tenant.sh` refusing to bind `git-review` — and `git-merge`,
    see below — to the tenant's ordinary worker account;
  * `secret_manager` gaining a per-provider accessor override, so that for
    these two providers the `iam_binding`'s sole member is the post-verdict
    (respectively, merge) service account, never the worker account; and
  * `git-review` and `git-merge` being excluded from `refresh_secrets` and
    from the refresher's own admin grants, so no `-refresh` twin ever names
    the worker account for either provider.

  Without those three changes, this catalogue entry by itself reopens B1.
  **Contract request 33's `git-merge` has the identical gap** — a separate
  issue is being filed for it.
* **Nothing stored**, the same as request 33: `worker_action` already
  defaults to `None`; this only adds one more enum member and one more
  catalogue entry.
* **Terraform's `job_matrix`** creates a `post-verdict` Job for each tenant
  whose providers include `git-review`, running as `swarm-<tenant>-post-verdict`,
  not `worker_service_accounts[tenant]` and not the `merge` account either —
  a third, distinct identity (merge-step.md §1.3, invariant 9). Until it
  lands, the Job must not exist. **`post-verdict`'s own service account
  carries only the ordinary tenant-wide read every identity gets under
  `tenants/<t>/`** (merge-step.md §4.3, binding 1) — nothing narrower,
  nothing wider, and no `objectUser` grant for its own artifacts or
  checkpoints, since it runs no agent and writes neither.
* **Every restatement of the catalogue** — the plugin's bridge,
  `swarm_profiles`, the UI's profile list, `check-contract-parity.sh` —
  needs the new entry, the same as request 33.
* **The outcome ledger** gains two more classes, and `DERIVE_VERSION` bumps
  again, so stored days are re-derived a second time if this lands after
  request 33 already bumped it once.
* **The review POST's `{owner}/{repo}/{n}`** (`POST
  /repos/{owner}/{repo}/pulls/{n}/reviews`, merge-step.md §4.3) split two
  ways: `{owner}/{repo}` come from the tenant's control-plane record, never
  from a task document, but **`{n}`, the pull request number, comes from the
  author's own tenant-writable `result_summary.git`** (merge-step.md §4.2) —
  a value `post-verdict` does not independently verify. Misdirecting which
  pull request the anchored review lands on therefore needs only a
  Firestore write, not a GCS race; this is part of why R8 stays OPEN
  (merge-step.md §7), not something this request closes.

### If it is declined

The verdict cannot be anchored without an agent-adjacent App key, which
merge-step.md's threat model (§0, §1.3, §4.3) already shows is not safe:
either the key sits with the review agent (rejected once already, the
reason `claude-code-review`, request 36, holds no App key at all) or B1
reverts to the control-plane attestation the owner already declined. The
merge step cannot be enabled for any tenant without this.

### Invariants

- **Invariants 1–3.** `post-verdict` is an ordinary task: admitted in the
  same transaction, parked and costing nothing until `review` succeeds, and
  counted from `LEASED`.
- **Invariant 4.** A forge failure fails the attempt retryably; the worker
  does not sleep through it.
- **Invariant 5.** The worker checks its generation at start and again
  before the post. A stale worker never touches the forge.
- **Invariant 8.** Checkpointing stays on, restore is skipped (the same
  `worker_action`-gated skip request 33 already establishes, merge-step.md
  §1.3); the workspace is empty either way.
- **Invariant 9.** The review credential's only accessor is the tenant's own
  post-verdict service account, and no agent runs as that account —
  **true only once the three companion changes in MAJOR 1 above (the API
  credential routes and `register-tenant.sh` refusing to bind `git-review`
  to the worker account, `secret_manager`'s per-provider accessor override,
  and `git-review`'s exclusion from refresh) are in place.** The catalogue
  entry by itself does not establish this invariant; today, unamended, it
  would also grant the tenant's ordinary worker account the same accessor
  role.
- **Invariant 10.** A caller names `post-verdict` and sends `input: {}`.

---

## 36. `profiles.py`: the `claude-code-review` profile, and a typed `never_restore_checkpoint`

**Superseded 2026-10-06 (lane MS0, part of #352) for the profile, not the field:** the review-only-writable prefix the `claude-code-review` profile served went with the review App ([2026-10-06 revision](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs), §3), so the entry stays disabled and retiring it is that revision's request (B). `never_restore_checkpoint` stays: the lifecycle reads it.

**Status: ACCEPTED — accepted by the owner 2026-10-01, as written; APPLIED 2026-10-01**
(functionality wave 3, lane M1): `RunnerProfile.never_restore_checkpoint`
(default `False`) and the `claude-code-review` entry, `available=False` until
#342 is enforced and the review App exists. The lifecycle does not read the
field yet (merge-step.md §10 item 4a, Track B), which is safe only because
nothing can dispatch the profile. Part of #295. Recorded from
[merge-step.md](merge-step.md)'s design (contract request 33, ACCEPTED by
the owner 2026-09-29 as the design for #295), which pointed at this as a
separate, not-yet-filed request ([merge-step.md](merge-step.md)'s own §10
build item 3, §1.3 and §4.3 — not contract request 33's; §10, §1.3 and §4.3
are merge-step.md's own sections). The design is merge-step.md §1.3 and
§4.3. Nothing under `apps/common/swarm_common/` has been edited.

### What is true today

The `review` step of merge-step.md's chain needs an identity distinct from
the tenant's ordinary worker account, `swarm-agent-worker-<tenant>`, for one
narrow reason: it is the only identity allowed to write `review.json` to the
review-only-writable GCS prefix (`tenants/<t>/verdicts/`, merge-step.md
§4.3), which the ordinary tenant account's own IAM binding excludes. Unlike
`post-verdict` and `merge`, `review` still runs an agent — the same
`claude-code` runner, on the same image, needing the same Anthropic
credential — so it cannot be a `worker_action` profile; it needs its own
ordinary, agent-running profile with its own Job, so Terraform can bind
`swarm-<tenant>-review` to that Job specifically and not to every
`claude-code` step of the tenant.

Separately: merge-step.md §1.3 and §10 (build item 4a) require that
`claude-code-review`, `post-verdict` and `merge` never restore a checkpoint,
on any attempt, not only the first. `post-verdict` and `merge` get this for
free from the existing `worker_action`-gated skip (request 33). A profile
that runs an agent, like `claude-code-review`, is not covered by that
mechanism, and the repository's own house style is against keying behaviour
off `profile.name` in the lifecycle (request 18's history: `command` keyed
off a name once and produced incident wf_ebb3ab2d65664707a559). A typed
field is needed for this profile alone.

### The requested change

In `profiles.py`, add one field to `RunnerProfile`:

```python
@dataclass(frozen=True)
class RunnerProfile:
    ...
    #: When True, the lifecycle never restores a checkpoint for this
    #: profile, on any attempt, not only the first. False (the default)
    #: preserves today's behaviour for every existing profile. `review`
    #: must never resume an agent inside a workspace a previous attempt
    #: left behind -- its whole judgement depends on seeing the checked-out
    #: head honestly (merge-step.md's own threat model, S0).
    never_restore_checkpoint: bool = False
```

**The name was chosen by the owner on 2026-09-30** over the originally
proposed `restore_on_retry=False`, which read as "restore normally on attempt
1, skip only on retries". The field means never restore, on any attempt, for
this profile. Attempt 1 has nothing to restore regardless, so its effect is
only ever visible from attempt 2, but the name does not limit it to retries.
The unusual value, `True`, is the one a reader sees set.

**A retry that finds `review.json` already written at its own path must
refuse the attempt, never adopt the existing object as this attempt's own
output.** Because `never_restore_checkpoint=True` skips workspace restore but says
nothing about the review step's GCS write, and because the platform's own
retry of a failed attempt reuses the *same* path — `f(workflow_id, review
task id)`, stable across attempts of one task (merge-step.md §7's R8
discussion) — a naive worker that treats "object already exists" as "already
done" would adopt whatever is sitting at that path, honest or planted, rather
than treating a pre-existing object as the refusal it is. The `review`
worker's write must be create-only (`objectCreator`, no update, no delete;
merge-step.md §4.3) and a create failure on an existing object must end the
attempt in refusal, not success — this is a requirement on the worker
alongside `never_restore_checkpoint`, not a consequence the field produces by itself,
and does not by itself close R8 (merge-step.md §7), which stays OPEN.

A catalogue entry, identical to `claude-code`'s except its name (and so its
Job and service account) and `never_restore_checkpoint`:

```python
"claude-code-review": RunnerProfile(
    name="claude-code-review",
    image="agent-runtime-base",
    resource_class="standard",
    backend=Backend.CLOUD_RUN_JOB,
    runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
    provider="anthropic",
    secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    secrets_any_of=True,
    timeout_seconds=7200,
    inputs=_CLI_AGENT_INPUTS,
    never_restore_checkpoint=True,
),
```

### What it would break if accepted

* **Nothing stored.** `never_restore_checkpoint` defaults to `False`, so every
  existing profile keeps today's restore behaviour unchanged.
* **The lifecycle's restore path** (`agent_worker.lifecycle`, wherever it
  currently restores unconditionally on a retried attempt) must read
  `profile.never_restore_checkpoint` before restoring, the same way it already
  branches on `worker_action` before building an argv (request 33).
* **Terraform** needs a per-tenant `review` service account and Job, holding
  `roles/storage.objectCreator` on `tenants/<t>/verdicts/` alone (no
  delete), plus the ordinary `objectViewer`/excluding-`objectUser` pair for
  its own artifacts and checkpoints (merge-step.md §4.3, §10). Until it
  lands, `claude-code-review` must not be dispatchable: an agent running
  under the tenant's ordinary account, with no distinct identity, gives up
  exactly the isolation this profile exists for. **How this is enforced,
  stated explicitly (minor, review 2026-09-29 — this was previously left
  unstated):** the same kind of provider gate contract request 35 uses for
  `post-verdict` — `claude-code-review`'s admission additionally requires
  the tenant to have registered `git-review` (the same registration
  `post-verdict` requires), parking a tenant that has not with
  `CREDENTIAL_MISSING` at no cost, even though the credential it actually
  mounts is `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`, not the App key.
  Since `RunnerProfile.provider` is already `"anthropic"` for this entry,
  this needs either a second, unmounted-provider field (e.g.
  `requires_providers`) or an equivalent registration check keyed off the
  tenant's Terraform-rendered review Job existing — a small model extension
  this request does not yet specify further than naming the shape.
* **Submission must refuse dispatching `claude-code-review` or
  `post-verdict` outside a workflow step, or with a null `workflow_id`; for
  `post-verdict` specifically, also without a `verdict_source`** (merge-step.md
  §3's submission refusals) — neither profile is meant to run as a
  caller-dispatched standalone task, and both derive part of their identity
  (which workflow's verdict they anchor, or which review they post) from the
  workflow context alone.
* **Every restatement of the catalogue** — the plugin's bridge,
  `swarm_profiles`, the UI's profile list, `check-contract-parity.sh` —
  needs the new entry and the new field.

### If it is declined

`review` runs on the plain `claude-code` profile, sharing its Job and
service account with `implement`, `fix` and `proof`. Since that shared
account would then need write access to the verdicts prefix for `review` to
do its job, every other step on that profile — and every other tenant
workflow's `claude-code` steps — inherits the same write grant, reopening
the in-chain forgery T3a exists to close (merge-step.md §7). The merge step
cannot be enabled for any tenant without this either.

### Invariants

- **Invariants 1–3, 5, 10.** Unchanged: `claude-code-review` is an ordinary
  agent-running task like `claude-code`, admitted, fenced and named the same
  way.
- **Invariant 8.** Checkpointing stays on; `never_restore_checkpoint=True` is the
  point of this request, not a violation of it — checkpointing and
  restoring are separate steps, and mandatory periodic checkpointing is
  unaffected. What is skipped is resuming a workspace a previous attempt
  left behind.
- **Invariant 9.** `swarm-<tenant>-review`'s only elevated grant relative to
  the ordinary tenant account is the verdicts-prefix write. **MAJOR 2
  (security review 2026-09-29, NOT_YET): this is not "no portable secret an
  agent could exfiltrate and reuse elsewhere," and that claim is withdrawn.**
  A prompt-injected review agent (T11) can mint `swarm-<tenant>-review`'s own
  token from the metadata server and use it for as long as that token is
  valid — long enough, with `objectCreator` on `tenants/<t>/verdicts/`, to
  write a fabricated `review.json` at another workflow's path in the same
  tenant's verdicts subtree, ahead of that workflow's own review, before its
  own `post-verdict` reads it. This is exactly the residual merge-step.md
  records as **R8** (merge-step.md §7, T3b), and the owner has accepted it
  as **OPEN**, not closed by this request. This request narrows what the
  grant can do (create-only, no update, no delete, scoped to one prefix); it
  does not make the grant non-portable, and does not claim to.


---

## 37. `config.py` / `admission.py`: the lease's dispatch deadline is 300 s, shorter than a slow cold start plus the worker's startup read

**Status: ACCEPTED, accepted by the owner 2026-09-30, applied by this PR
(#404).** The owner decided the value (480 s) and that it goes through this
file, on #401, after #402 lengthened the worker's startup read. Numbered 37
because 35 and 36 are taken on an open branch
(`docs/cr35-cr36-merge-step-followons`); if another branch has taken 37 by the
time this merges, renumber this one.

### What is true today

The lease's dispatch deadline is 300 s, stated three times inside the frozen
contract:

* `swarm_common/config.py`, `Settings.dispatch_timeout_seconds: int = 300`;
* `swarm_common/config.py`, `Settings.from_env`, the fallback of
  `DISPATCH_TIMEOUT_SECONDS`, `300`;
* `swarm_common/admission.py`, `AdmissionConfig.dispatch_timeout_seconds: int = 300`.

Admission writes `lease.dispatch_deadline = now + dispatch_timeout_seconds`
(`admission.py`, `acquire_lease_in_transaction`). Where it is read:

* **the scheduler** passes its setting into `AdmissionConfig`
  (`apps/scheduler/scheduler/loop.py`), and into
  `backend_deadline_seconds` for every Cloud Run and GKE dispatch
  (`apps/scheduler/scheduler/dispatch.py`): the backend's hard deadline is the
  task timeout + the dispatch timeout + `WORKER_FINALISE_BUDGET_SECONDS` (300);
* **the reconciler** judges a lease that has never heartbeated by
  `dispatch_deadline` alone (`detect_stale_leases`, `reconciler/detect.py`),
  and `MISSING_EXECUTION_GRACE_SECONDS` defaults to it
  (`reconciler/config.py`);
* **`kubernetes/render.py`** reads the dataclass default for the GKE Job
  templates' `activeDeadlineSeconds`, and takes `--dispatch-timeout-seconds`
  where a deployment overrides it.

No Terraform module sets `DISPATCH_TIMEOUT_SECONDS`; every deployed service
runs on the default. (`grep -rn "dispatch_timeout_seconds\|DISPATCH_TIMEOUT_SECONDS"`,
2026-09-30, over the whole tree.)

### The requested change

`dispatch_timeout_seconds` is 480 s in all three places. Nothing else in the
contract changes: not the field, not the env var, not the lease document.

Why 480. #401 (owner decision 2026-09-30) asked the worker's generation check
to keep asking for about 180 s, because fresh Cloud Run instances were measured
getting no reply from Google APIs for 30 to 90 s after starting, and two
attempts were lost to it under a ~90 s schedule (2026-09-25
`swarm-job-eng-mock-9ngvq`, 2026-09-26 `r7ff9`). #402 built it: the read now
spans ~180 s and its worst case, every try hanging its whole call timeout, is
200 s (`agent_worker.startup.CONTROL_PLANE_READ_WINDOW_SECONDS`). The deadline
counts from dispatch, and the cold starts measured on 2026-09-25 were 103 s and
195 s from dispatch to the worker's first line
(`docs/incidents/2026-09-25-worker-startup-network.md`). At 300 s, a worker
that cold-started in 195 s and met an API that did not answer was fenced by the
reconciler before its own last attempt ended: the retry the owner asked for was
cut off by the platform's own deadline, and the attempt was lost exactly as it
was before #401. 195 + 200 = 395 s; with 60 s of margin for the first heartbeat
after the verdict, and because 195 s is the slower of two samples rather than a
bound, 455 s; 480 is the round figure above it.

`tests/unit/worker/test_control_plane_read_retries.py`
(`test_the_dispatch_deadline_outlasts_the_slowest_cold_start_plus_the_worst_read`)
holds the three numbers to each other, imports the worker's window rather than
restating it, and holds all three frozen statements of the default equal.

### What it would break if accepted

* **A genuinely lost dispatch is detected 3 minutes later.** A Job that was
  never created, an image that never pulled, a worker that died before its first
  heartbeat and whose execution the ended-at-startup rule cannot see: the
  reconciler reclaims each at the deadline, now 480 s instead of 300 s. For
  those 180 s the lease is held, and with it the concurrency slot and the
  capacity units across every pool. **Invariants 1 and 3 still hold** (a
  `LEASED`/`DISPATCHED` task is demand and counts toward concurrency, as it
  must); what changes is how long a dead one is counted. At the 100-agent
  ceiling, a burst of lost dispatches holds its slots up to 8 minutes rather
  than 5. The ended-at-startup rule (#198), on a `*/1` tick, still requeues
  any attempt whose execution has visibly ended within 30 to 90 s of the end,
  so the longer wait is paid only by dispatches that left no ended execution
  behind.
* **The backend's hard deadline grows by 180 s** for every attempt
  (`backend_deadline_seconds`). A wedged lifecycle stops heartbeating and is
  reclaimed by the heartbeat rule long before either deadline, so this is the
  ceiling for a hung pod, not its expected life. The largest task timeout the
  API accepts (86 400 s) plus 480 + 300 s stays far inside Cloud Run's task
  timeout limit.
* **The overview's workflow-stall threshold (600 s, `apps/swarm-ui/src/checks.ts`)**
  stays above the deadline and above the p90 cold start, the two bounds its
  tests hold, but no longer above their sum (about 700 s). It was left at 600
  by this change.
* **The reconciler's `missing_execution_grace_seconds`** defaults to the same
  setting, so an execution missing from the backend listing is also given
  480 s. Its dataclass default in `reconciler/config.py`, which tests that do
  not pass the field use, moves to 480 with it.

### If it is declined

The worker's startup read stays at ~180 s (200 s worst) and a cold start
slower than about 94 s from dispatch (300 - 200 - 6) that meets an API with no
reply is fenced before its verdict. The alternative is shortening #402's
schedule back toward ~90 s, which is what lost the two attempts #401 recorded.

### Invariants

1. Demand is still only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING`; the change
   lengthens how long a dead dispatch is counted, stated above.
2. All-or-nothing reservation: unchanged.
3. Concurrency counts from `LEASED`: unchanged.
5. Fencing: a worker that starts after the deadline is still fenced; the
   deadline is later.

---

## 38. `states.py` / `admission.py`: a pool with no `hard_limit` is refused as "set to 0" (#374)

**Status: ACCEPTED 2026-10-05 by the owner (recorded on #374) and IMPLEMENTED
2026-10-05 (functionality wave 7, lane CR38).** Proposed 2026-10-01
(functionality wave 1, lane B1). What was applied is listed under "As
implemented" at the end of this entry.

### What is true today

`acquire_lease_in_transaction` reads each pool document with
`hard_limit=d.get("hard_limit", 0)` (`swarm_common/admission.py`), so a pool
document with no `hard_limit` key -- written by hand, or by a writer that set
only `enabled` -- is a ceiling of 0. `evaluate_capacity` then refuses through it
with the pool's ordinary reason (`TENANT_LIMIT`, `RESOURCE_CLASS_LIMIT`, ...)
and `limit: 0`, which says an operator set the pool to zero. Nobody did.
`BlockedReason` has no value for "this ceiling was never set".

What #374's fix does without touching the contract: both `pool_from_dict`s
return an `UnsetLimitPool` (a `SlotPool` subclass that adds no field) for such
a document; `pool_to_api` serves its `hard_limit`, `effective_limit` and
`available` as null; `/v1/capacity` analyses its profiles as `unknown`; and
the scheduler, after admission has refused, rewrites that pool's blocker to
`POOL_LIMIT_UNSET` with `limit: null` (`scheduler/loop.py`,
`_name_unset_limits`). That last step is a relabel AFTER the frozen function
has spoken: the string is not a `BlockedReason`, and it depends on the
scheduler re-reading which pools are unset.

### The requested change

* `BlockedReason.POOL_LIMIT_UNSET = "POOL_LIMIT_UNSET"`, in the needs-action
  group (`headroom.NEEDS_ACTION`).
* `evaluate_capacity` (and the transaction's pool read) treat a missing or
  null `hard_limit` as unknown: still a refusal, reported as
  `{"pool": name, "reason": POOL_LIMIT_UNSET, "limit": None, ...}`, checked
  after `MANUAL_PAUSE` (a pause refuses at any limit, so it stays the reason).
* `SlotPool.hard_limit: int | None`, `effective_limit` None when it is None.

### What it would break if accepted

* Every reader of `SlotPool.effective_limit` as an `int` must handle None
  (the API codec, headroom, the scheduler's metrics, `reconciler` pool checks).
* `scheduler/loop.py` `_name_unset_limits` and the `UnsetLimitPool` class in
  both codecs become dead and are removed in the same change.
* `tests/unit/control_plane/test_blocker_groups.py` grows the new member.

### If it is declined

The relabel stays. Its weakness: a blocker recorded by any path other than the
scheduler's drain (none today) would still say `TENANT_LIMIT` at 0.

### Invariants

2. All-or-nothing reservation: unchanged; an unset pool still refuses.
3. Concurrency counts from `LEASED`: unchanged.
7. `requests == limits`: unaffected; this is the pool ceiling, not a pod spec.

### As implemented, 2026-10-05

* `states.py`: `BlockedReason.POOL_LIMIT_UNSET`. `swarm_api/headroom.py` files
  it under `NEEDS_ACTION` (waiting never clears it; somebody sets a limit).
* `models.py`: `SlotPool.hard_limit: int | None`. None makes `effective_limit`
  and `available` None and `has_capacity` False. `SlotPool` lives in
  `models.py`, not `admission.py`/`states.py`; it is the type this request's
  third bullet names, so that is the one edit outside the two modules in the
  title.
* `admission.py`: the transaction reads `d.get("hard_limit")`, so a missing
  key and a stored null are both None; `evaluate_capacity` refuses through
  such a pool with `{"reason": "POOL_LIMIT_UNSET", "limit": None}`, after the
  `MANUAL_PAUSE` check. An explicit 0 is still a 0 with the pool's own reason.
* Removed as dead: `Scheduler._name_unset_limits` (`scheduler/loop.py`), the
  `UnsetLimitPool` class in both codecs and the scheduler codec's string
  `POOL_LIMIT_UNSET`, and `_UnsetLimitReads`/`_PoolSnapshot`
  (`scheduler/store.py`, #601), which hid a stored null from the transaction
  because the old read crashed on it. swarm-api's `hard_limit_known` stays,
  now `pool.hard_limit is not None`, for `service.py`'s `/v1/capacity` filter.
* `swarm_api/headroom.py` `_ceiling`: a None limit bounds headroom at 0, never
  "unbounded" (a paused pool with no limit reaches it).
* The jq restatement `effective_limit` in `scripts/lib/common.sh` returns null
  for an absent or null `hard_limit`, as the model returns None, and
  `scripts/lib/check-contract-parity.sh` holds it there with three
  null-`hard_limit` rows. `status.sh` prints such a pool's LIMIT and HARD as
  `unset`; `resume-swarm.sh` says `POOL_LIMIT_UNSET`, not `hard_limit 0`; the
  over-limit checks in `concurrency-test.sh` and `race-test.sh` treat a null
  limit as admitting nothing, because in jq every number is greater than null.
* Proved by `tests/unit/common/test_pool_limit_unset_contract.py` and the
  updated `tests/unit/control_plane/test_pool_limit_unset.py`.

---

## 39. `states.py` / `models.py`: `ParkReason.BUDGET_EXHAUSTED` names a park nothing writes, because there are no budgets

**Status: open (functionality wave 1, lane B0, 2026-10-01).** Recorded as a
request, per CLAUDE.md rule 1; nothing under `apps/common/swarm_common/` was
edited. Numbered 39 on the assumption that 38 (lane B1) lands first; renumber if
another branch has taken it.

### What is true today

The owner decided on 2026-10-01 that there are NO per-tenant budgets: no
`monthly_budget_usd` enforcement, built or planned, and nothing writes
`PARKED(BUDGET_EXHAUSTED)`. The frozen contract still carries the vocabulary of
the feature that was dropped:

* `ParkReason.BUDGET_EXHAUSTED` (`apps/common/swarm_common/states.py::ParkReason.BUDGET_EXHAUSTED`).
  Nothing writes it, and no scheduler sweep reads it
  (`apps/scheduler/scheduler/loop.py::Scheduler._stop_for_failed_workflow`;
  `tests/unit/control_plane/test_every_park_has_an_unparker.py`
  `test_no_sweep_reads_budget_exhausted` holds the second).
* `BlockedReason.BUDGET_LIMIT` (`apps/common/swarm_common/states.py::BlockedReason.BUDGET_LIMIT`).
  `evaluate_capacity` never produces it; `apps/swarm-api/swarm_api/headroom.py::NEEDS_ACTION`
  lists it in the needs-action group with the advice "raise the budget", which
  there is no way to do.
* `Tenant.monthly_budget_usd` (`apps/common/swarm_common/models.py::Tenant.monthly_budget_usd`). The
  admin route refuses it with a 422
  (`apps/swarm-api/swarm_api/routes/admin.py::set_tenant_limits`), so no write path sets it;
  the codecs still read it from a document if a hand-edit put it there.

Per-attempt cost IS recorded, which is what made the old refusal message
("no cost attribution source") stale: `record_spend`
(`apps/agent-worker/agent_worker/control.py::ControlPlane.record_spend`) writes `cost_usd` onto the
attempt (`apps/common/swarm_common/models.py::Attempt.cost_usd`). The decision is not "budgets
once attribution exists"; it is "no budgets".

Readers outside the contract that name the unused value and would go with it:
`apps/swarm-mcp/swarm_mcp/compact.py::_WAITS_FOR`, `apps/swarm-mcp/swarm_mcp/progress.py::_PARKED_BECAUSE`,
and the UI's park-reason lists in `apps/swarm-ui/src/types.ts` (`:1822`,
`:2088`, `:2109`, `:2134`, `:2196`).

### The requested change

Either of these, at the owner's choice:

* **Remove** `ParkReason.BUDGET_EXHAUSTED`, `BlockedReason.BUDGET_LIMIT` and
  `Tenant.monthly_budget_usd`, with the readers above in the same change; or
* **Keep and document** them: a comment on each in the frozen modules saying the
  value is reserved and never written (owner, 2026-10-01), so a reader of the
  enum does not go looking for the code that sets it.

### What it would break if accepted

* Removal: a Firestore task document that somehow carries
  `park_reason: "BUDGET_EXHAUSTED"` would no longer decode into `ParkReason`.
  None should exist, since nothing has ever written it; a read-only count over
  `tasks` where `park_reason == "BUDGET_EXHAUSTED"` should confirm 0 before
  merging. A tenant document with a hand-written `monthly_budget_usd` would
  decode only if the codecs drop the key.
* Removal: the API's `TenantLimitsRequest.monthly_budget_usd`
  (`apps/swarm-api/swarm_api/schemas.py::TenantLimitsRequest.monthly_budget_usd`) exists only so the refusal can
  explain itself. It would stay, as a refusal of a field the contract no longer
  has, or go, and the refusal would become a plain `extra_forbidden`.
* Removal: the UI and MCP lists lose a member; their tests that enumerate
  `ParkReason` follow.
* Keep-and-document: nothing breaks; it is comments only.

### If it is declined

The value stays, unused, and the docs say so where a reader meets it:
`docs/quota-management.md` §4 marks it never written, `docs/cost-control.md` §2
says budgets are not built and not planned, and `docs/multi-tenancy.md` says why
`monthly_budget_usd` is refused. The UI keeps a label for a state no task can
reach, which costs nothing but a misleading line in a list.

### Invariants

1. Only `LEASED`/`DISPATCHED`/`STARTING`/`RUNNING` create demand: unchanged;
   this park was never reached, so no task leaves or enters one.
2. All-or-nothing reservation: unaffected. `BUDGET_LIMIT` is not a pool.
10. Callers pick a profile by name: unaffected.

---

## 40. `states.py`: an agent awaiting its children has no park reason, and `DEPENDENCY_INCOMPLETE` would be promoted at once

**Status:** APPLIED 2026-10-02, accepted by the owner 2026-10-02 with request 14 (filed with [request 14's amendment](#amendment-2026-10-02-accepted-with-the-cancellation-and-capacity-rules-b15)). `ParkReason.CHILDREN_INCOMPLETE` is in `apps/common/swarm_common/states.py`; the worker's await park writes it and the scheduler's await sweep promotes it (`apps/scheduler/scheduler/loop.py`, `_promote_child_awaits`).

### What is true today

`ParkReason` (`apps/common/swarm_common/states.py::ParkReason`) has no value for "this
task is waiting for tasks it created". The nearest, `DEPENDENCY_INCOMPLETE`
(`apps/common/swarm_common/states.py::ParkReason.DEPENDENCY_INCOMPLETE`), is the scheduler's: its sweep
promotes a task parked on it whose `depends_on` is empty immediately, with
reason `no_dependencies` (`apps/scheduler/scheduler/loop.py::Scheduler._promote_dependencies`). An awaiting
parent has an empty `depends_on`.

### The requested change

```python
#: The task's agent asked to await the child tasks it submitted. Promoted by
#: the scheduler when every child is terminal; see docs/design/child-tasks.md.
CHILDREN_INCOMPLETE = "CHILDREN_INCOMPLETE"
```

### What it would break if accepted

Nothing that exists: no document carries the value. Every exhaustive reading
of `ParkReason` must learn it -- `types.ts` and parity section 5, the MCP
progress sentences (`apps/swarm-mcp/swarm_mcp/progress.py::_PARKED_BECAUSE`) and compact
labels, and the console's blocker copy.

### If it is declined

The await parks on `DEPENDENCY_INCOMPLETE` with a reserved metadata marker, and
the scheduler's empty-`depends_on` promotion learns to skip a marked task.
That works, and it is the hierarchy-from-dependencies S1 forbids in reverse:
every reader of the park reason would show an awaiting parent as "a step it
depends on has not finished yet", which is false, and the marker is in a
document any tenant identity can rewrite.

---

## 41. `models.py`: a child cancelled because of its parent has no end cause

**Status:** APPLIED 2026-10-02, accepted by the owner 2026-10-02 with request 14 (filed with [request 14's amendment](#amendment-2026-10-02-accepted-with-the-cancellation-and-capacity-rules-b15)). `EndCause.CHILD_CASCADE` is in `apps/common/swarm_common/models.py`, written by the API's cascade, the scheduler's sweeps and the worker ending a flagged child; the outcome ledger counts it as its own cancel class, `child_cascade` (DERIVE_VERSION 5). So does every writer that ends a flagged child CANCELLED on its `cancel_requested` -- the worker (`agent_worker.control.cancel_end_cause`), the reconciler's repair (`reconciler.model.cancel_end_cause`), and the scheduler's admission, exhausted-retry and dispatch-failure cancels (`scheduler.children.cancel_end_cause`) -- each from `metadata.child_cascade` with the one rule, held equal by `tests/unit/worker/test_end_cause_reconciler.py` and `tests/unit/control_plane/test_child_tasks_scheduler.py`.

### What is true today

`EndCause` (`apps/common/swarm_common/models.py::EndCause`) has `CANCELLED_PARENT`,
which means an UPSTREAM workflow step was cancelled, and `CANCEL_REQUESTED`,
which the outcome ledger reads as a cancel somebody pressed. A child cancelled
because its parent was cancelled, failed, dead-lettered or out-waited its
deadline is neither.

### The requested change

```python
CHILD_CASCADE = "child_cascade"   # the parent task was cancelled or ended, or its await expired
```

Written by the API's cancel cascade and the scheduler's sweep, and by the
worker or reconciler ending a child flagged by either. The specific reason
(`parent_cancelled`, `parent_ended`, `await_expired`) is event detail, not a
frozen vocabulary -- the same split `MERGE_REFUSED` uses for its refusals.

### What it would break if accepted

Nothing that exists. `EndCause`'s docstring gains the writers above; the
ledger and `types.ts` gain one class.

### If it is declined

Cascaded children end `CANCEL_REQUESTED` with the reason in event detail, and
the ledger counts a parent's failure as a person's cancel -- the confusion
request 23 was accepted to end.

---

## 42. `specsign.py`: the signed step spec does not cover a child's parent

**Status:** APPLIED 2026-10-02, accepted by the owner 2026-10-02 with request 14 (filed with [request 14's amendment](#amendment-2026-10-02-accepted-with-the-cancellation-and-capacity-rules-b15)). `SPEC_FORMAT = 2` and `SPEC_FORMATS = (1, 2)` in `apps/common/swarm_common/specsign.py`; `canonical_step_spec` takes `spec_format`, and the worker verifies each document under the projection its own `spec_format` names. swarm-api signs a task that names no parent at FORMAT 1 (`specsign.signing_format`), so a worker built before format 2 keeps running every non-child task through a rollout that updates swarm-api first; only a child, which needs a worker with the child path anyway, is signed at format 2.

### What is true today

`canonical_step_spec` (`apps/common/swarm_common/specsign.py::canonical_step_spec`) covers the
fields a worker must trust, at `SPEC_FORMAT = 1`
(`apps/common/swarm_common/specsign.py::SPEC_FORMAT`). Any tenant identity can rewrite a
task document whose id it knows ([multi-tenancy.md](multi-tenancy.md)), so a
child's `parent_task_id` could be rewritten to attach it to, or detach it
from, a parent's cancel cascade and await, and nothing would notice.

### The requested change

`parent_task_id` and `parent_attempt_id` inside the canonical spec, under
`SPEC_FORMAT = 2`. Format 1 documents keep verifying under format 1's
projection, which the worker already chooses by `spec_format`.

### What it would break if accepted

The worker must accept both formats during the rollout, as it accepted
unsigned tasks during request 34's. No task written before has the fields, so
none changes its digest.

### If it is declined

The cascade and the sweeps stay tenant-scoped, so a rewrite cannot reach
across tenants; within one tenant a rewritten parent is undetectable, and the
tree the console draws is a claim rather than a fact.

---

## 43. `identity.py`: the tenant worker service account's name has no public home

**Status:** APPLIED 2026-10-02, accepted by the owner 2026-10-02 with request 14 (filed with [request 14's amendment](#amendment-2026-10-02-accepted-with-the-cancellation-and-capacity-rules-b15)). `worker_service_account_id` is in `apps/common/swarm_common/identity.py`; `scripts/lib/check-contract-parity.sh` section 8 holds it to the prefix every restatement is checked against.

### What is true today

The worker service account is `swarm-agent-worker-<tenant>`, stated privately
as `_GSA_PREFIX` (`apps/common/swarm_common/identity.py::_GSA_PREFIX`) and restated by
`scripts/register-tenant.sh` (`GSA_PREFIX="swarm-agent-worker-"`) and
`terraform/modules/tenancy/main.tf` (`sa_account_id = { for t, _ in var.tenants`).
The children route must derive the identity it accepts from the tenant id,
because `tenants/<id>.service_account` is a document any tenant identity can
rewrite.

### The requested change

```python
def worker_service_account_id(tenant_id: str) -> str:
    """`swarm-agent-worker-<tenant_id>`: the account id the tenant's workers run as."""
```

### What it would break if accepted

Nothing; the private constant stays. `check-contract-parity.sh` gains a check
that the shell and Terraform restatements still equal it.

### If it is declined

swarm-api restates the prefix, held equal to `_GSA_PREFIX` by a parity test --
one more copy of a name request 16 already counts two of.

---

## 44. `states.py`: `account_assigned` and `account_released` ride on `RUNNING` and `LEASE_RELEASED`

**Status: open (functionality wave 1, lane B4, 2026-10-01).** Recorded as a
request, per CLAUDE.md rule 1; nothing under `apps/common/swarm_common/` was
edited. Numbered 40 on the assumption that 38 and 39 land first; renumber if
another branch has taken it.

### What is true today

The worker records a subscription account's lifecycle on the task's events
with a `cause` on an event type the frozen `EventType`
(`apps/common/swarm_common/states.py::EventType`) already has:

* `account_assigned` is `EventType.RUNNING` with `cause: "account_assigned"`
  (`Worker` in `apps/agent-worker/agent_worker/lifecycle.py`).
* `account_released` (#380) is `EventType.LEASE_RELEASED` with
  `cause: "account_released"` (`Worker._emit_account_released`).

The API reads both through one range query on the `cause`
(`swarm_api.task_accounts`), so the account view is right. A reader that
renders events by type alone is not: a timeline draws the account's release as
a lease release, and the assignment as a second `running`.

### The requested change

Add `EventType.ACCOUNT_ASSIGNED = "account_assigned"` and
`EventType.ACCOUNT_RELEASED = "account_released"`, and have the worker emit
them, keeping the `cause` for one release so readers of either shape work.

### What it would break if accepted

* Every reader that enumerates `EventType` (the UI's event lists in
  `apps/swarm-ui/src/types.ts`, the MCP renderers) gains two members; their
  tests that enumerate the enum follow.
* `swarm_api.task_accounts` would read both shapes for as long as events
  written before the change are retained.

### If it is declined

The `cause` stays the discriminator, and the docstring on
`_emit_account_released` says why the type is `LEASE_RELEASED`. A generic
timeline keeps drawing the release as a lease release.

### Invariants

5. Fencing: unaffected. `control.emit` stamps the attempt, lease and
   generation either way, and a fenced exit emits neither event.
9. Tenant isolation: unaffected; the event carries the account id, the
   provider and a bool, never a secret's name or payload.

---

## 45. `profiles.py`: the catalogue does not say which runner profiles report a cost

**Status:** open, filed 2026-10-02 with #72. A request, not a change.

### What is true today

`GET /v1/attempts` `coverage` (#72) splits rows without a `cost_usd` into
"a profile that never reports a cost" and "spent and recorded nothing". The
frozen `RunnerProfile` (`apps/common/swarm_common/profiles.py`) does not say
which profiles report one, so swarm-api restates it as
`COST_REPORTING_RUNNERS` (`apps/swarm-api/swarm_api/routes/attempts.py`),
matched against each profile's `runner_argv`, and reads `cost_declared` as
"reports only when the input asks" because the mock is the only declared
profile. A unit test holds the set to modules under
`apps/agent-worker/agent_worker/runners` and pins the mock as the only
declared profile.

### The requested change

```python
    #: Whether an attempt on this profile records `cost_usd`: "always" for the
    #: CLI runners that parse a usage block, "on_input" for a runner that
    #: reports one only when its input carries it (the mock), "never" otherwise.
    reports_cost: Literal["always", "on_input", "never"] = "never"
```

### What it would break if accepted

Nothing that exists. swarm-api drops `COST_REPORTING_RUNNERS` and the
`cost_declared` stand-in and reads the field; the UI's `types.ts` gains it if
the profiles route serves it.

### If it is declined

The restatement stays, held by its test; a new cost-reporting runner module
counts as "never reports" until someone adds it to the set.

---

## 46. `models.py`: a pull-request step that published nothing has no end cause

**Status:** open, filed 2026-10-02 by functionality wave 3, lane B46. A
request, not a change.

### What is true today

Since lane B46 (GUARD 2, owner decision 2026-10-02), the worker ends a step
FAILED, and does not retry it, when the step's job was to open a pull
request and it did not. That covers a `direct-pr` step, the `integrate`
integrator and a `single-pr` author. Either the step ended with no commit
beyond its base, or the forge refused its pull request ("No commits between
main and swarm/..."). `last_error` begins `published_nothing:`
(`agent_worker.lifecycle._published_nothing`). No `EndCause` names this
case, so `_fail_for_published_nothing` writes `OUTPUTS_MISSING`, the closest
existing value: the step's promised deliverable, its pull request, does not
exist. `PUBLISH_REFUSED` (request 29) does not fit, because the worker did
not refuse anything. The outcome ledger therefore counts a lost
implementation (workflow `wf_b9b337e107494c10a416`) together with an
expected file nobody wrote.

### The requested change

```python
    PUBLISHED_NOTHING = "published_nothing"   # a pull-request step ended with nothing beyond its base, or the forge refused its pull request
```

The worker writes it from `_fail_for_published_nothing`. `swarm_api/outcomes.py`
gains the failure class `published_nothing` ("published nothing"), the UI's
`FailureClassKey`, fixture and `END_CAUSES` follow it, and
`DERIVE_VERSION` moves.

### What it would break if accepted

Nothing that exists: a new enum value. A reader that does not know it falls
back to its text classifier, as for any task written before `end_cause`
existed. The parity script's section 5 and
`test_every_end_cause_has_exactly_one_class` turn red until every mirror
follows, which is what they are for.

### If it is declined

These steps stay `outputs_missing`, and `last_error`'s `published_nothing:`
prefix is the only way to count them apart.

### Invariants

- **Secrets.** The refusal is quoted after the worker's scrub; the cause
  names the case, never a value.
- **Invariant 1.** A FAILED step holds no capacity; the lease is released on
  the same finish as any other failure.

---

## 47. `profiles.py`: the `merge` profile is enabled, and reads the tenant's `-git` token

**Status:** ACCEPTED by the owner on 2026-10-04 and applied by functionality
wave 4, lane M1a. The decisions are recorded on #295; the risk acceptance on
#476.

### What was true before

Contract request 33 put `merge` in the catalogue with `provider="git-merge"`:
a GitHub App key, `swarm-tenant-<tenant>-git-merge`, read only by the Job's
own service account `swarm-<tenant>-merge`, so that no container an agent
had run in could ever hold a credential able to land code. The profile was
`available=False` until #342 (signed step specs) was enforced and the owner
had created the merge App. Meanwhile pull requests were merged on the GitHub
side, by `.github/workflows/auto-merge.yml` and the `swarmcloud-merge` App.
That App merged #564 and #562 and left #72 and #560 open, although GitHub
listed both as their closing references (#569).

### The change

```python
    "merge": RunnerProfile(
        ...
        worker_action=WorkerAction.MERGE,
        provider="git",
        secrets=(),
        timeout_seconds=600,
        inputs={},
    ),
```

`available` returns to its default, True; `disabled_reason` is dropped; the
provider is `git`, the tenant's existing forge token. Nothing else in
`swarm_common` changed: `post-verdict` and `claude-code-review` stay disabled
with their reason, and `WorkerAction`, the end causes and every other field
are as request 33 left them.

### Why (the owner's decisions, 2026-10-04)

1. Merging moves off the GitHub-side App into a workflow `merge` step.
2. The step uses the tenant's EXISTING `-git` token. The owner accepted, on
   #476, that an agent holding `-git` can also merge: the token that pushes a
   branch can already do everything the merge App was meant to keep from it,
   so a second identity bought no isolation worth its cost.
3. It works in any repository a workflow runs on, not one fixed by Terraform.
4. It explicitly closes the issues the pull request closes, because the App
   path did not (#569).
5. It is an opt-in final step: a platform default (`merge_by_default`,
   default off) or per job (`metadata.merge`: `on` | `off`).

#342 is closed, so the gate request 33 named is lifted. The rest of
docs/merge-step.md stands except where its "Revised 2026-10-04 (owner)"
section says otherwise.

### What it breaks

* `validation.APP_CREDENTIAL_PROVIDERS` no longer derives `git-merge`.
  `git` stays out of `known_providers()` for a different reason: the forge
  token is registered only by `scripts/register-tenant.sh --add-provider git`
  and stored only with `scripts/create-secrets.sh --stdin` (owner rule,
  2026-09-25), never through the credential routes.
* Terraform's catalogue mirror (`terraform/infra/locals.tf`) follows the
  provider, and the merge Job runs as the tenant's worker account: it is
  removed from the profiles that run as their own account. The
  `swarm-<tenant>-merge` account definition in
  `terraform/modules/service_account_ids` is no longer read for the merge
  Job's identity; removing it is a follow-up outside this lane.
* The App-shaped merge action (`merge.run_merge` over a `dispatch.merges`
  block) is replaced by the `-git` merge over a signed `dispatch.merge_target`
  block. A `single-pr` chain still cannot be submitted: it needs
  `post-verdict`, which stays disabled.

### Invariants

- **Secrets.** The token is read at merge time only, after the reap and every
  check that needs no credential (#219), registered with the log redaction,
  and never written to the workspace, a file, an agent environment, an event,
  a log line or `result_summary`.
- **Invariant 1.** The merge step holds capacity only while LEASED through
  RUNNING; a merge waiting on pending checks fails its attempt retryably and
  waits READY with `next_eligible_at`, at no cost (invariant 4).
- **Invariant 9.** The token is the tenant's own; the merge acts only on the
  workflow's own `repository_url`, which the signed spec covers.
- **Invariant 10.** A caller chooses the step by naming the `merge` profile or
  by `metadata.merge`; nothing a caller sends picks an image, a command or a
  credential.

---

## 48. `profiles.py`: no runner profile runs `agent-runtime-indexer`, so index runs cannot reach the repo-index toolchain

**Status:** accepted by the owner 2026-10-05 (#625), and applied by
functionality wave 8, lane IDX. The owner's decision changed the requested
entry in two ways: the profile is named `indexer`, not `repo-indexer`, and it
is `claude-code` in EVERY field but its name and its image -- so it takes
claude-code's inputs (`issue`), not `inputs={}`. Its pool ceiling is the
`runner_profiles` lookup's default, the global ceiling, as for merge,
post-verdict and claude-code-review; a tfvars entry narrows it. Filed
2026-10-05 with #625 (functionality wave 8, lane IMG); it is the image half
of docs/repo-index.md §6.3 request (B), in the smallest form that restores
what index runs had.

### What was true before

#625 moved the repository index's toolchain out of `agent-runtime-base` into
its own image, `agent-runtime-indexer` (docs/worker-images.md). Every
claude-code start pulled about 133 MB of it, compressed, and only index runs
used it. The image is built, promoted and scanned with the others.

No profile names it. `RunnerProfile.image` in `RUNNER_PROFILES` is the only
map from a profile to its image. `terraform/infra/locals.tf` mirrors it, and
`tests/terraform/catalogue.tftest.hcl` holds the mirror equal. Index runs are
submitted as `claude-code` (`swarm_api.repoindex.INDEXER_PROFILE`), which runs
`agent-runtime-base`. An index run therefore records "not installed in this
image", writes no graph, and an impact query on that index plans every changed
file as `unindexed`.

### The requested change

A profile identical to `claude-code` in everything but its name, its image and
its inputs, so the existing indexer prompt runs on the image that carries the
tools it names:

```python
    #: Index runs only (#625, docs/worker-images.md): claude-code's runner on
    #: the image that carries the repository index's toolchain. Submitted by
    #: swarm-api's index runs, never chosen with a caller's image (invariant 10).
    "repo-indexer": RunnerProfile(
        name="repo-indexer",
        image="agent-runtime-indexer",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
        provider="anthropic",
        secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        secrets_any_of=True,
        timeout_seconds=7200,
        inputs={},
    ),
```

`inputs={}`: an index run's input is its prompt alone, which swarm-api
composes. 7200 s is the largest row of §3.5's budget table (120 minutes). The
agent-free shape that §6.3 (B) describes for P1 and P3, with the tool pass as a
worker-run step, remains a later change and does not depend on this one.

### What it would break if accepted

Nothing that exists. The work that follows acceptance:

* the Terraform mirror gains the entry. `runner_images` then includes
  `agent-runtime-indexer`, `WORKER_IMAGE_REFS` carries its digest, and each
  tenant that registers `anthropic` gets a `repo-indexer` Job running as its
  worker account;
* the entry needs a pool ceiling;
* `INDEXER_PROFILE` becomes `"repo-indexer"`;
* the tests that enumerate `RUNNER_PROFILES` gain the entry.

As applied (lane IDX): `RUNNER_PROFILES["indexer"]`, the `indexer` entry and
its model in `terraform/infra/locals.tf`, `INDEXER_PROFILE = "indexer"`, its
agent-stream rows in the worker and swarm-api, and the UI's fixture and
Submit rows. The catalogue has no internal-only mechanism, so `indexer` is
offered to every caller like `claude-code`; keeping it platform-only would
need a catalogue field, which is a request of its own.

### If it is declined

There are two choices, both measured in docs/worker-images.md:

* Index runs stay on `claude-code` without the extractor or a graph.
* The toolchain goes back into `agent-runtime-base`, and every agent start
  pays for it again.

### Invariants

- **Invariant 10.** swarm-api names the profile for its own index runs. No
  caller sends an image, and the image is named by the catalogue.
- **Invariant 7.** `standard` is `requests == limits`, as for `claude-code`.
- **Invariant 9.** The Job runs as the tenant's own worker account and writes
  the graph under the tenant's own prefix, as index runs do today.
- **Invariants 1-3.** It is an ordinary profile: QUEUED until admitted,
  counted from LEASED.

---

## 49. `states.py`: a merge step waiting for its pull request's checks has no park reason

**Status:** accepted by the owner 2026-10-06 (#352), APPLIED 2026-10-06 by lane MS2. Filed
2026-10-06 by functionality wave 11, lane MS1, as request (A) of
[docs/merge-step.md's 2026-10-06 revision](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs)
("Owner decisions on this plan", decision 1). Lane MS2 applies it, with its
mirrors, and adds the line the frozen-contract guard reads to its own pull
request.

### What is true today

The built merge step waits for CI by failing its attempt retryably: a
required check still queued or in progress ends the attempt, and the step
waits READY for `CHECKS_PENDING_RETRY_SECONDS` before it is admitted again,
at most `MERGE_STEP_MAX_ATTEMPTS` times
(`apps/agent-worker/agent_worker/merge.py`,
`apps/swarm-api/swarm_api/validation.py`). Every wait spends an attempt and a
Job execution, so a slow CI exhausts the step, and a fast one is read only
every five minutes.

None of the existing `ParkReason` members fits a park that waits for a
forge's checks:

* `SCHEDULED_RETRY` is promoted by time alone and counts every wake as an
  attempt, which is today's behaviour;
* `DEPENDENCY_INCOMPLETE` is promoted when the workflow's upstream steps
  end, and a merge step's have already ended;
* `CHILDREN_INCOMPLETE` names child tasks, and the observer tools say so
  (`apps/swarm-mcp/swarm_mcp/progress.py`, `_PARKED_BECAUSE`).

### The requested change

In `apps/common/swarm_common/states.py`, after `CHILDREN_INCOMPLETE`:

```python
    #: A merge step waits for its pull request's checks; promoted by the
    #: scheduler's CI-wait sweep on the wake marker or the fallback instant.
    #: Contract request 49 (docs/merge-step.md, 2026-10-06, request (A)).
    CI_PENDING = "CI_PENDING"
```

### What it would break if accepted

Nothing that exists: no writer emits it until MS2's worker park does. MS2
adds its mirrors with it -- the UI's park list, the MCP plugin's
`_PARKED_BECAUSE` row (MS5), and `scripts/lib/check-contract-parity.sh` --
and the scheduler's `_promote_ci_waits` sweep, which returns the park to
READY on `metadata.merge_wait.wake_requested_at` or on `next_eligible_at`.
A reader that enumerates `ParkReason` and has no row for the new member
shows the raw value until its mirror lands.

### If it is declined

MS2 parks on `SCHEDULED_RETRY` with a `blocked_by` reason of `CI_PENDING`
and no refund, and nothing frozen changes. A slow CI then still spends the
step's attempts, one per wake.

### Invariants

- **Invariants 1 and 3.** A `CI_PENDING` task is PARKED: no lease, no pool
  count, no Job execution. Only admission takes capacity when it is woken.
- **Invariant 4.** This is the point of the request: the step parks,
  releases and exits instead of waiting.
- **Invariant 5.** The park is written in the fenced transaction every park
  uses.

---

## 50. `profiles.py` / `models.py`: retire the disabled `single-pr` catalogue entries

**Status:** open. Filed 2026-10-06 by functionality wave 11, lane MS1, as
request (B) of
[docs/merge-step.md's 2026-10-06 revision](merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs).
The owner decided on 2026-10-06 ("Owner decisions on this plan", decision 4)
that the unused #295 pieces are removed by a separate cleanup lane; this
request is that lane's frozen half, and records the change it makes so that
lane carries its own acceptance line.

### What is true today

Requests 35 and 36 added, for the `single-pr` chain:

* the `post-verdict` and `claude-code-review` runner profiles, both
  `available=False` since 2026-10-04;
* `WorkerAction.POST_VERDICT`;
* `EndCause.VERDICT_REFUSED` and `EndCause.VERDICT_FAILED`.

The 2026-10-04 revision moved the merge onto the tenant's `-git` token and
appended the `merge` step to `integrate` and one-step `direct-pr` workflows
(request 47), and the 2026-10-06 revision superseded the review App and the
GitHub-review verdict anchor these served (merge-step.md, 2026-10-06 §3).
Nothing submits either profile, and nothing writes either end cause.

### The requested change

Remove the two profiles from `RUNNER_PROFILES`, `WorkerAction.POST_VERDICT`
from `profiles.py`, and `VERDICT_REFUSED`/`VERDICT_FAILED` from `EndCause`,
with every mirror: the Terraform catalogue in `terraform/infra/locals.tf` and
its test, the worker's `post-verdict` action, swarm-api's `single-pr`
planning, the outcome ledger's classes and the UI's and plugin's rows.

### What it would break if accepted

A stored task or outcome that names either end cause would no longer parse
as an `EndCause`. None should exist, since neither profile has been
submittable; the cleanup lane reads the store to confirm before it removes
them.

### If it is declined

The entries stay disabled, as they are today, and every reader keeps the
rows for values nothing writes.

### Invariants

- **Invariant 10.** Two fewer profiles a caller can name; neither was
  available.
- **Invariant 9.** The per-tenant review, post-verdict and merge service
  accounts are the Terraform half of the same cleanup (decision 4), decided
  at dev-iam, not here.

---

## 51. `models.py`: `Attempt` does not type `checkpoint_sha256`, the digest a retry binds its restore to

**Status:** proposed, filed 2026-10-07 for #350, the Firestore document-shape
record of #348 (the fix for S0 #347). A request, not a change: nothing under
`apps/common/swarm_common/` is edited by it, and the owner accepts or refuses
it.

### What is true today

Since #348, attempt documents (`attempts/<id>`) carry a field the frozen
`Attempt` does not name: `checkpoint_sha256`, a map of checkpoint id to the
SHA-256 of that checkpoint's archive. Its only name is the worker's
`agent_worker.control.CHECKPOINT_DIGESTS_FIELD`, restated as a key by the test
seeds in `tests/unit/worker/worker_seeds.py`.

* **Who writes it, and when.** Only the worker:
  `ControlPlane.record_checkpoint` adds `{checkpoint_id: archive_sha256}` in
  the same merge-set of the attempt's own document that appends the id to
  `checkpoints`, and only then runs the fenced transaction that moves
  `task.latest_checkpoint` to the new checkpoint. A pointer therefore never
  names a checkpoint whose digest is not yet recorded. A checkpoint recorded
  without a digest adds no entry.
* **Who reads it.** Only the worker of a LATER attempt of the same task:
  `Worker._recorded_checkpoint` reads the document of the attempt that wrote
  the checkpoint `latest_checkpoint` names (`ControlPlane.fetch_attempt`,
  which refuses another tenant's document), and restores only when that
  document is this task's, lists the checkpoint id in `checkpoints`, and
  records the digest the manifest carries. The restore then checks the
  archive's bytes against it (docs/checkpointing.md, "What is restored").
* **Who drops it.** swarm-api's `codec.attempt_from_dict` builds an `Attempt`
  field by field, so it reads the field nowhere and drops it silently -- the
  same shape as the five spend fields this decoder once dropped while the
  worker wrote them correctly (request 2).

### Why

A retry restores only a checkpoint whose archive digest its earlier attempt
recorded. The manifest that also carries the digest sits in the bucket, and
every agent of the tenant can write anywhere under `tenants/<tenant>/`
(docs/security.md, "Which checkpoint is restored is not chosen from the
bucket"). Before #348 a restore took whatever the bucket held under the task's
prefix, and `.claude/` -- settings and hooks, which run code -- travels in
every checkpoint because HOME is `work/`. Binding the restore to a digest in
Firestore is what refuses an archive rewritten in place, even with its
manifest rewritten to match. The field is a security record of S0 weight,
and the frozen contract is where every other component learns what an
attempt document holds.

### The requested change

In `apps/common/swarm_common/models.py`, on `Attempt`, after `checkpoints`:

```python
    #: Checkpoint id -> lowercase hex SHA-256 of that checkpoint's archive.
    #: Written ONLY by the worker's `ControlPlane.record_checkpoint`, in the
    #: same merge as `checkpoints` and before `task.latest_checkpoint` moves;
    #: read ONLY by a later attempt of the same task, which restores a
    #: checkpoint only when its archive matches the digest recorded here (#347).
    #: Empty means "no digest recorded": a document written before #348, or a
    #: checkpoint recorded without one. Its retry starts from an empty
    #: workspace; an empty map is never read as "anything goes".
    checkpoint_sha256: dict[str, str] = field(default_factory=dict)
```

* **Type:** `dict[str, str]`, keyed by checkpoint id (`ckpt-` and five or
  more digits), valued by a 64-character lowercase hex digest.
* **Default:** an empty dict, through `field(default_factory=dict)` as for
  `checkpoints`, so every existing document still decodes.
* **The mirrors that follow it, in the applying change:**
  `CHECKPOINT_DIGESTS_FIELD` becomes `"checkpoint_sha256"` checked against
  the dataclass field rather than a free string (a unit test, or a section of
  `scripts/lib/check-contract-parity.sh`); `codec.attempt_from_dict` reads
  it, keeping only `str -> str` entries. `codec.attempt_to_api` does not
  serve it: no API caller needs it, and leaving it out keeps the response
  shape unchanged. Whether a later change shows it to an operator is a
  separate decision.

No writer or reader changes behaviour. The worker writes and checks exactly
what it does today.

### What it would break if accepted

Nothing that exists. A new optional field with an empty default: every
stored attempt still parses, and no reader that ignores it changes.

**Migration for existing attempt documents.** None is run, on purpose. An
attempt document written before #348 has no `checkpoint_sha256`, decodes with
the empty default, and a retry of that attempt starts from an empty workspace
once, as docs/checkpointing.md already says. A backfill must **not** compute
digests from the bucket: the bucket's current bytes are the value the binding
exists to distrust, and a backfill would bless whatever a planter had already
put there. How tasks that were mid-retry at deploy are treated is the owner's
decision recorded on #348; this request takes no position on it and changes
nothing about it.

### If it is declined

Behaviour is identical: the field stays a worker-private key in
`agent_worker.control`, and the restore keeps working. What remains is that
the frozen `Attempt` under-describes a security-relevant document, that
swarm-api's decoder drops the field without anyone deciding to, and that a
future writer of attempt documents (a reconciler repair, a copy tool) learns
of the field only by reading the worker.

### Invariants

- **Invariant 1.** No state, lease or pool count changes. The field is
  written by a RUNNING attempt that already holds its lease; a QUEUED, PARKED
  or READY task creates no demand by having it.
- **Invariant 2.** Untouched: nothing in admission's all-or-nothing
  transaction reads or writes the attempt document.
- **Invariant 3.** Untouched: concurrency counts from LEASED as before.
- **Invariant 4.** No wait is added. The digest is computed while the
  archive is written, and the restore's check is one point read at startup.
- **Invariant 5.** The digest map is written to the attempt's OWN document;
  the pointer that makes a later attempt read it moves only in the fenced
  transaction. A stale worker can write digests into its own document, but
  cannot repoint `latest_checkpoint`, so no later attempt reads them.
- **Invariants 6 and 7.** Untouched: no Spot, no resource spec.
- **Invariant 8.** Checkpointing stays mandatory and periodic at the same
  cadence; this makes a restored checkpoint trustworthy against a
  bucket-only writer. Its cost is that a retry of a pre-#348 attempt starts
  clean once.
- **Invariant 9.** `fetch_attempt` refuses another tenant's attempt
  document, and the checkpoint must lie in the task's own prefix. Within one
  tenant this stops a bucket-only writer, not one that also writes Firestore,
  which has no document-level IAM (docs/multi-tenancy.md); that half rests on
  signed step specs (#342).
- **Invariant 10.** No caller sends it: no API route accepts the field, and
  the serialiser does not return it.
