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
| 14 | `models.py`: a sub-agent has nowhere to name its parent | open |
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
| 30 | `identity.py`: a tenant may list service accounts that resolve to it by exact email (#273) | ACCEPTED 2026-09-29 by the owner after three security reviews |
| 32 | `profiles.py`: `browser` and `generic` declare no inputs, so the API bounds them by size alone and the plugin can send them none (#218) | ACCEPTED 2026-09-29 by the owner after three security reviews |
| 33 | `profiles.py` / `models.py`: a merge profile that runs no agent, and two end causes for it (#295) | ACCEPTED 2026-09-29 by the owner, as the design; build gated on #342 |

---

## 1. `identity.py`: the `u-` prefix does not namespace personal tenants

**Status:** open, found 2026-09-18 by an audit agent that correctly declined to
edit the frozen module itself.

### The claim that is false

`apps/common/swarm_common/identity.py:88`:

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

`Attempt` (`apps/common/swarm_common/models.py:196-215`) carries
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
`tests/unit/control_plane/test_dispatch_strategy.py:572-573` pins that
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
`tests/unit/control_plane/test_gke_client_host.py:97`:

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
`tests/unit/worker/test_integrate_strategy.py:86-88`, which asserts the default
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
* `tests/unit/worker/test_integrate_strategy.py:86-88` has to change. It
  currently asserts the wrong vocabulary.

### If it is declined

The carrier vocabulary still has to be reconciled. Both sides are **unfrozen**,
so that can be done today and should be: a dead accessor carrying constants the
writer cannot produce, with a test holding it in place, is a trap set for
whoever wires it up. Everything else keeps working by convention, re-parsed in
three places, with the frozen contract silent about a four-field block that
decides whether a workflow opens one pull request or five.

---

## 7. The workflow rollup has no shared home, so only one service can own it

**Status:** open, found 2026-09-22 while making `Workflow.state` advance at all.

### The problem

Nothing ever wrote `Workflow.state` after submission. It is now derived from the
step tasks and written back, and the derivation lives in
`apps/swarm-api/swarm_api/rollup.py`. It lives there because that is the only
place it CAN live and still be used by more than one service — and it is used by
exactly one.

The scheduler would have been the better home for the write. It already runs on a
guaranteed one-minute clock (`terraform/modules/scheduler/jobs.tf:3`, the safety
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

`apps/common/swarm_common/models.py:344`:

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
repository is an unrelated fixture name at `tests/unit/mcp/test_sc.py:28`. Three
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

* `apps/swarm-api/swarm_api/routes/admin.py:433` — the `overdue_only=1` filter
  is the query an operator runs during a capacity incident to find stuck
  dispatches. It returned an empty list at exactly the moment it was asked.
* `apps/swarm-api/swarm_api/codec.py:355` — `lease_to_api` reported
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

**Status:** open, filed 2026-09-24. A request, not a change. Raised as S1 / B31
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

* `TaskCreate.metadata` (`apps/swarm-api/swarm_api/schemas.py:33`) is a free
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

**Status: ACCEPTED, accepted by the owner 2026-09-29** (recorded on #259),
not yet applied. Recorded 2026-09-29 from #259. If another branch has taken
29 by the time this merges, renumber this one.

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

## 32. `profiles.py`: `browser` and `generic` declare no inputs, so the API bounds them by size alone and the plugin can send them none

**Status: ACCEPTED 2026-09-29 by the owner after three security reviews.**
Nothing here is applied yet; an implementation PR, `part of #218`, carries
this out (see `docs/DEPLOY_STATE.md` or the linked PR for its state — this
entry itself does not track a moving target). It answers #218,
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

**Status: ACCEPTED 2026-09-29 by the owner, as the design; build gated on
#342.** Recorded from #295's design step. Revised 2026-09-29, several times,
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
