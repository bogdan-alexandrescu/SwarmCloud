# Requests against the frozen contract

`apps/common/swarm_common/` is frozen. CLAUDE.md rule 1 is explicit about what
to do when you believe it needs to change: *say so instead of changing it.* This
file is where that is said, so a request survives the session that found it.

Nothing here is applied unless its **Status** line says so. Each entry states the
defect, the proof, what the change would be, and — the part that decides whether
it is worth it — what breaks downstream if it is made, and what is left for
somebody to live with if it is declined.

These are requests for a person to decide. Nothing in this file is a plan.

| # | Request | Status |
|---|---|---|
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
| 26 | `models.py`: the attempt's CPU figures carry no time and their limit no source | open |

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
  The accepted fields carry neither. Request #26 asks for both.

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
`test_model_flag_is_attribution_only.py` holds for the bridge.

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

**Status:** open, recorded 2026-09-25 by the #184 follow-up lane (PR #210),
which applied request #15. A request, not a change. If another branch has
taken 26 by the time this merges, renumber this one.

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

