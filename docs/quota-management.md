# Provider quota management

The expensive mistake this subsystem exists to prevent:

> A provider rate-limits you, and 40 workers sit in `time.sleep()` waiting for
> the window to reopen — each holding 4 vCPU, 8 GiB and a concurrency slot, all
> billed, all doing nothing.

The rule instead (`CONTRACT.md` invariant 4):

```
wait <= max_in_worker_retry_delay_seconds (45s)  ->  sleep and retry in place
wait >  max_in_worker_retry_delay_seconds        ->  checkpoint, upload, publish
                                                     quota state, PARK with
                                                     next_eligible_at, release
                                                     the lease, EXIT
```

A `PARKED` task is a Firestore document. It costs nothing, for as long as it
takes. When `next_eligible_at` passes, the scheduler promotes it to `READY` and
it competes for capacity again, resuming from its last checkpoint.

**An unknown wait is treated as a long one.** Guessing "probably short" costs a
slot for as long as the guess is wrong; guessing "probably long" costs one
requeue.

---

## 1. Quota state

`quota/{provider}:{tenant_id}` — keyed by both, because tenants bring their own
API keys. One tenant's exhausted Anthropic quota says nothing about another's,
and throttling the provider globally on one tenant's 429 would let any tenant
halt the platform.

| Field | Meaning |
|---|---|
| `state` | `AVAILABLE` / `THROTTLED` / `EXHAUSTED` / `COOLDOWN` / `UNKNOWN` / `DISABLED` |
| `configured_hard_max` | admin ceiling. AIMD may only go **below** it |
| `adaptive_target` | what AIMD currently believes is safe |
| `quota_derived_limit` | derived from the provider's own reported headroom |
| `requests_remaining`, `tokens_remaining`, `reset_at` | as reported by the provider |
| `cooldown_until`, `retry_after_seconds`, `last_429_at` | backoff bookkeeping |
| `success_count`, `rate_limit_count` | AIMD's inputs |

`effective_limit` is `min(hard_max, adaptive_target?, quota_derived_limit?)`, and
is **0** whenever the state is `EXHAUSTED`, `DISABLED` or `COOLDOWN`. Zero
effective limit means the matching slot pool admits nothing, which means the
work stays `READY`/`PARKED` and free.

---

## 2. AIMD

Additive increase, multiplicative decrease — the shape of TCP congestion
control, for the same reason: the safe operating point is unknown, it changes
without warning, and the only feedback is failure.

The asymmetry is the design:

**Increase is slow and conditional.** The target rises by `additive_increase`
(1) only after `success_threshold` (20) consecutive successes with no
intervening 429, and the success run resets on every rate limit. One success
proves nothing. Being wrong in this direction produces a burst of 429s and a
provider that starts throttling everything — far more expensive than a few
minutes of under-use.

**Decrease is fast and unconditional.** A single 429 multiplies the target by
`multiplicative_decrease` (0.5) immediately. Waiting for a second data point
means the second data point arrives as a wall of 429s.

```
       target
         |          .-'|        .-'|
         |      .-'    |    .-'    |
         |  .-'        |.-'        |
         |-'           v           v          <- one 429 each time
         +----------------------------> time
```

### The invariant that must never break

```
adaptive_target <= configured_hard_max, always.
```

`configured_hard_max` is an admin's statement about what the platform is allowed
to do to a provider — a contractual or budget ceiling, not a guess. AIMD may only
ever propose a number below it. All arithmetic goes through one clamping helper
so a new signal cannot be added that forgets to clamp.

### Tuning

| Env var | Default | Effect |
|---|---|---|
| `AIMD_ADDITIVE_INCREASE` | 1 | slots added per successful run |
| `AIMD_MULTIPLICATIVE_DECREASE` | 0.5 | factor applied on a 429 |
| `AIMD_SUCCESS_THRESHOLD` | 20 | successes required before an increase |
| `AIMD_MIN_TARGET` | 1 | never throttle to zero on 429s alone |
| `AIMD_DEFAULT_COOLDOWN_SECONDS` | 30 | when the provider gives no `Retry-After` |
| `AIMD_MAX_COOLDOWN_SECONDS` | 900 | ceiling on backoff |
| `AIMD_EXHAUSTION_THRESHOLD` | 5 | consecutive 429s that mean `EXHAUSTED` |
| `QUOTA_DEFAULT_HARD_MAX` | 50 | ceiling for an unseen (provider, tenant) pair |

---

## 3. How a 429 travels

```
runner child sees 429 (it is the only thing that sees the provider's headers)
    -> writes quota.json into the workspace
        -> worker reads it, decides short-wait vs park
            -> worker POSTs the observation to the quota broker
                -> broker applies AIMD to quota/{provider}:{tenant}
                    -> broker writes quota_derived_limit on the provider pools
                        -> scheduler reads the pool and admits fewer tasks
```

The scheduler never asks a provider how it feels. It reads a pool, and the pool
already carries the answer. That separation is what keeps the admission path a
single Firestore transaction with no network calls in it.

A worker also learns from this: the control plane publishes aggregated provider
state, so one worker discovers that *another* worker on the same tenant key has
already exhausted the quota — and parks instead of finding out the hard way.

---

## 4. Park reasons

From `swarm_common.states.ParkReason`. None of these cost compute:

| Reason | Meaning | Unparked by |
|---|---|---|
| `PROVIDER_QUOTA_EXHAUSTED` | quota spent | `next_eligible_at` / reset |
| `PROVIDER_COOLDOWN` | backing off after 429s | cooldown expiry |
| `PROVIDER_OUTAGE` | provider unreachable | broker marking it available |
| `SCHEDULED_RETRY` | ordinary retry backoff | `next_eligible_at` |
| `DEPENDENCY_INCOMPLETE` | upstream workflow step unfinished | dependency sweep |
| `MANUAL_PAUSE` | an operator paused it | `resume-swarm.sh` |
| `BUDGET_EXHAUSTED` | tenant budget spent | budget reset / admin raise |
| `CREDENTIAL_MISSING` | tenant has no key for this provider | admin adds the key |

`CREDENTIAL_MISSING` is worth calling out: a tenant that has not registered a key
is **not** an error. The task parks, costs nothing, and starts by itself once an
admin adds the key. The alternative — failing at runtime — burns an attempt, a
container start and a slot to discover something the control plane already knew.

---

## 5. Prewarm

`PARKED` tasks whose cooldown expires within `prewarm_lead_seconds` (120) are
promoted to `READY` early, bounded by `prewarm_max_agents`.

This is free, because `READY` costs nothing (invariant 1). The task is simply
already in the rotation when the window reopens, instead of waiting for the next
scheduler wake to notice. Disable with `ENABLE_QUOTA_PREWARM=false` if you want
strictly no early promotion.

---

## 6. Operating it

```bash
# Provider health, per tenant, with pool effect
./scripts/status.sh

# Or through the API
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     "$API/v1/providers"

# Stop admitting anything for one provider; running work continues
./scripts/pause-swarm.sh --provider anthropic
./scripts/resume-swarm.sh --provider anthropic

# Drain a provider gracefully (limit -> 0, no kills)
curl -X POST "$API/v1/admin/providers/anthropic/drain"

# Disable/enable a provider entirely
curl -X POST "$API/v1/admin/providers/anthropic/enabled" -d '{"enabled": false}'

# Raise the ceiling AIMD is allowed to approach
curl -X PUT "$API/v1/admin/limits/provider/anthropic" -d '{"hard_limit": 80}'
```

Verify the behaviour end to end without waiting for a real rate limit:

```bash
make quota-test
```

It drives the quota document directly — the same document the broker owns —
asserts that work parks rather than sleeping, that no compute is held while
parked, and that the task resumes when the window reopens. It restores the
original state on every exit path.

---

## 7. What to look at when it misbehaves

| Symptom | Likely cause | Check |
|---|---|---|
| Tasks park immediately on submit | tenant has no key for the profile's provider | `park_reason: CREDENTIAL_MISSING`; `scripts/create-secrets.sh --list` |
| Provider stuck at a low target | AIMD is still climbing after a 429 storm | `quota/{provider}:{tenant}.success_count` vs `AIMD_SUCCESS_THRESHOLD` |
| Provider never recovers | `state=EXHAUSTED` and nothing clears it | `reset_at` in the future, or the broker tick is not running |
| One tenant's 429s throttle everyone | a provider-wide pool is the binding one, not the per-tenant one | compare `provider:X` and `provider:X:tenant:Y` in `status.sh` |
| Workers burning CPU while rate-limited | `max_in_worker_retry_delay_seconds` set too high | it should stay well under a typical provider window |
