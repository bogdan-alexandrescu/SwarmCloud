# Raising a throttled tenant

**When:** a tenant's tasks queue behind `provider:<p>:tenant:<t> full` while
the pool's `hard_limit` and the quota's `configured_hard_max` are both well
above what is running, because one or more 429s halved the tenant's AIMD
target and it has not climbed back. You have fixed, or decided to accept, what
caused the 429s, and you want the tenant back at a chosen concurrency now.

**Who:** a platform admin. The route is not open to pool admins or tenants.

## Why the target is low, and why it stays low

The quota broker runs AIMD per (provider, tenant)
([quota-management.md §2](../quota-management.md#2-aimd),
`apps/quota-broker/quota_broker/aimd.py`):

* **one 429 halves** `adaptive_target`, immediately;
* the target rises by **1 per 20 consecutive successes**, and any 429 restarts
  the run;
* the per-tenant pool's effective limit is the minimum of its `hard_limit`,
  `adaptive_target` and `quota_derived_limit`
  (`SlotPool.effective_limit`).

That asymmetry is the design: being wrong upwards costs a wall of 429s. Its
price is the case that asked for this runbook. On 2026-10-14, `u-bogdan` had
pool `hard_limit` 40 and `configured_hard_max` 50, but one 429 at 09:47Z had
taken the target to 3. Getting from 3 to 40 by additive increase takes
37 × 20 = 740 clean calls — and with only 3 slots, few calls happen. The owner
lent the tenant two more accounts, which AIMD cannot know about, and wanted the
target at 40.

`PUT /v1/admin/limits/provider/<p>/tenant/<t>` does not help: it sets the
pool's `hard_limit`, and the target caps below it. `set_hard_max` does not
help either: it only ever clamps the target **down**. Until QUOTA-RESET there
was no way to raise the target at all, by design. This route is the one,
deliberately narrow, exception.

## What the reset does, and does not do

`PUT /v1/admin/quota/{provider}/{tenant_id}/adaptive-target` on swarm-api
(`apps/swarm-api/swarm_api/routes/admin.py`), which proxies to the broker's
`PUT /v1/quota/{provider}/{tenant_id}/adaptive-target`. The broker is the only
writer of quota documents, so swarm-api never writes one itself.

| Body field | Meaning |
|---|---|
| `target` (required) | The new target. **Clamped** into `[min_target, configured_hard_max]` by `aimd._clamp` — a request above the hard max gets the hard max, and the response says so (`reset.clamped`). |
| `reason` (required) | Why, in your words. It is the audit record's point. |
| `clear_cooldown` (default `false`) | Also retire a live cooldown and reopen the window. Without it a raised target under a cooldown still caps the pool at 0 until the cooldown expires. |

It **does**: set `adaptive_target`; restart the success run and the rate-limit
run from it; write the quota document, the per-tenant pool's
`adaptive_target` and `quota_derived_limit`, and an audit record in **one
Firestore transaction**; count it in
`swarm_quota_admin_changes_total{action="adaptive_target_reset"}`.

It **does not**: change `configured_hard_max` (that is
`PUT /v1/quota/{p}/{t}/hard-max` on the broker); change the pool's
`hard_limit` (that is `/v1/admin/limits/provider/{p}/tenant/{t}`); leave
`DISABLED` (that is `/v1/admin/providers/{p}/enabled`); or pin anything.

It **refuses** (HTTP 409, `quota_exhausted` from the broker, `conflict` from
swarm-api) a tenant whose state is `EXHAUSTED` unless `clear_cooldown` is
`true`: the provider said the window is spent, and a raised target that opens
nothing would read as if it had. Overruling the provider is a second decision;
make it explicitly or wait for the window to reopen.

## Do it

1. Read the state first, and note it — it is stale the moment you read it:

   ```bash
   scripts/api.sh GET '/admin/quota?tenant_id=u-bogdan' \
     | jq '.quota[] | select(.provider=="anthropic")
           | {state, configured_hard_max, adaptive_target, effective_limit,
              cooldown_until, last_429_at, pool: .feeds_pool.effective_limit}'
   ```

   If `configured_hard_max` is below what you want, the reset clamps to it.
   If the pool's `hard_limit` is, raise that too
   (`PUT /admin/limits/provider/anthropic/tenant/u-bogdan`): the lease takes
   the lower of the two.

2. Reset. `scripts/api.sh` is curl with the token on stdin rather than in
   argv — do not hand-build the `Authorization` header (see its header for
   why). It exits non-zero on a non-2xx, so a 409 stops a runbook script.

   ```bash
   scripts/api.sh PUT /admin/quota/anthropic/u-bogdan/adaptive-target \
     '{"target": 40, "reason": "lent two more accounts after the 09:47Z 429"}'
   ```

   Add `"clear_cooldown": true` only if the tenant is cooling down or
   `EXHAUSTED` and you have reason to believe the provider window is open
   again.

3. Check the answer: `quota.adaptive_target`, `reset.from_target`,
   `reset.to_target`, `reset.clamped`, and `reset.to_effective_limit`. Then
   re-run step 1: `feeds_pool.effective_limit` is what admission enforces.

## AIMD will halve it again on the next 429

This is a reset, not a pin. The next 429 halves the new target (40 → 20), a
run of `exhaustion_threshold` consecutive 429s makes the tenant `EXHAUSTED`,
and additive increase resumes from wherever that leaves it. If the tenant
keeps getting 429s after a reset, the reset was wrong: the cause is upstream
(the provider's own limit for that key or account) and raising the target
again only buys another wall of 429s. Look at the provider-side limit, or
lower `configured_hard_max` so AIMD never proposes more than the provider
will take.

## The audit trail

Every reset writes one document to the `quota_audit` collection, in the same
transaction as the change:

`action`, `provider`, `tenant_id`, `requested_by` (the admin's email, from
swarm-api's verified token), `caller` (the identity the broker verified —
swarm-api's service account), `reason`, `requested_target`, `from_target`,
`to_target`, `clamped`, `configured_hard_max`, `from_state`, `to_state`,
`cleared_cooldown`, `from_effective_limit`, `to_effective_limit`, `at`.

The broker also logs one line per reset with the same fields under
`quota_audit`. A refused reset writes nothing and changes nothing.
