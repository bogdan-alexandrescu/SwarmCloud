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
| 15 | `models.py`: `Attempt` records memory, disk and spend, but not CPU | open |
| 16 | `identity.py`: the tenant namespace name, `sanitize_name` included, has two copies | open |
| 17 | `states.py`: a cancel that is only requested is recorded as `cancelled` (incident CR-1) | open |
| 18 | `profiles.py`: `RunnerProfile.command` is the lifecycle's child argv, never a container command (incident CR-2) | open |
| 19 | `states.py`: no `EventType` says "the reconciler evicted this execution" | open |
| 20 | `models.py`: `Workflow.on_step_failure` is a bare `str`, and its vocabulary is stated four times | open |

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
| `swarm-api/validation.py:358` `StepSpec.input_from` | `tuple[str, ...]` | step id, **no filenames** |
| `task.metadata["input_from"]` | untyped | **task id** |
| `swarm-ui/src/types.ts:1227` `WorkflowStep.input_from` | `string \| null` | -- |

**Written by exactly one place.** `swarm-api/service.py:354-357`, inside
`submit_workflow`, translating step ids to the task ids it has just minted:

```python
if source.input_from:
    task.metadata["input_from"] = {
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
request is about the fifth row of that table, and about the submission path that
never gets validated at all.

`StepSpec.input_from` being a *tuple of ids* is not sloppiness: `validate_dag`
needs to know which step a file comes from, not which file. It is listed because
it is a fourth shape somebody has to hold in their head while reading this.

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
passed none of the DAG checks.

Be precise about what that is and is not. It is **not** a tenant escape:
`inputs.fetch_upstream_task` (`inputs.py:226-254`) refuses a task belonging to
another tenant, and `inputs.artifact_key` (`:327-352`) builds the object key
from *this* attempt's tenant, so a cross-tenant artifact is unreachable by
construction. It is an **ordering** bypass inside one tenant: the caller names
any of their own tasks, with no dependency edge and no guarantee the upstream
ran. A typed field gives the API one field to validate on every path, instead of
one path validating a key the other path waves through.

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
  should validate it for a standalone task. Right now the DAG rule is
  enforceable on only one of the two ways a task is created.
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

**Status:** open, raised 2026-09-24 by the worker-broker lane, which measured
CPU without it and stopped at the frozen line.

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

**Status:** open, recorded 2026-09-24 from incident `wf_ebb3ab2d65664707a559`
(where it was filed as CR-1). Found while the incident's stuck tasks were read
event by event. It did not cause that incident.

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
  which a task's event page can fail.
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

**Status:** open, recorded 2026-09-24 from incident `wf_ebb3ab2d65664707a559`
(where it was filed as CR-2). The code defect it describes is fixed outside the
frozen package, in `apps/scheduler/scheduler/dispatch.py`. This request is about
the field that made the defect easy to write.

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
