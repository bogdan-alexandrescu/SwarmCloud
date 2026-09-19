# Requests against the frozen contract

`apps/common/swarm_common/` is frozen. CLAUDE.md rule 1 is explicit about what
to do when you believe it needs to change: *say so instead of changing it.* This
file is where that is said, so a request survives the session that found it.

Nothing here has been applied. Each entry states the defect, the proof, what the
change would be, and — the part that decides whether it is worth it — what
breaks downstream if it is made.

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

**Status:** open, raised 2026-09-19 while scoping the SwarmCloud web UI.

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

