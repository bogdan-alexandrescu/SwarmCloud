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
                    -> broker writes quota_derived_limit on provider:{p}:tenant:{t}
                        -> scheduler reads the pool and admits fewer tasks
```

The scheduler never asks a provider how it feels. It reads a pool, and the pool
already carries the answer. That separation is what keeps the admission path a
single Firestore transaction with no network calls in it.

### The pool a tenant's 429 may write to

```
A tenant-attributed observation may ONLY lower provider:<p>:tenant:<id>.
The shared provider:<p> pool is capped only when EVERY enabled tenant is stopped.
```

This is an invariant, not a preference, and it is the difference between a
throttle and a denial of service. `pool_names_for` puts `provider:<p>` in
*every* tenant's admission list, so a tenant-derived limit written there halts
tenants that have never touched the provider — and a tenant can reach its own
429 threshold deliberately, by burning its own key into rate limits. That would
be a cheap, deniable cross-tenant outage.

`QuotaService._recompute_provider_pool` enforces it, and two details matter:

* the shared pool is capped only when every state in the roster is a stop state,
  **and** every *enabled tenant* has reported. A tenant with no quota document
  for that provider has never touched it, so the evidence is incomplete and the
  shared pool stays uncapped;
* the condition is also self-sustaining if it fires wrongly. With the shared pool
  at zero nothing runs, so no success can arrive to clear it, and the only way
  out is the platform-only sweep.

Tested by `tests/unit/control_plane/test_aimd.py` under
"the shared provider pool is not a cross-tenant lever" — the same way the
`adaptive_target <= configured_hard_max` clamp is tested rather than asserted.

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
| `CREDENTIAL_MISSING` | tenant has no key for this provider, and no pool account can run the profile for it | admin adds the key, or lends the tenant an account. A worker's park on a task the pool serves also waits for its `next_eligible_at` (below) |

`CREDENTIAL_MISSING` is worth calling out: a tenant that has not registered a key
is **not** an error. The task parks, costs nothing, and starts by itself once an
admin adds the key. The alternative — failing at runtime — burns an attempt, a
container start and a slot to discover something the control plane already knew.

**A key is not the only credential.** A tenant with no key of its own is
admitted when an account in the pool can run the profile for it, and parks only
when none can (#169). One function decides this,
`apps/scheduler/scheduler/credentials.py`. Admission asks it, and so do the
credential sweep that re-readies these parks and the Cloud Run Job's secret
mount. The API no longer judges it at submission. An account can run a profile
for a tenant only when all four of these hold:

* the deployment has a quota broker (`QUOTA_BROKER_URL`);
* the profile takes a subscription token (`CLAUDE_CODE_OAUTH_TOKEN` in its
  `secrets`). `claude-code` does. **`browser` does not**, so no account can run
  it, and a keyless tenant's browser work always needs a key;
* the account is of the profile's provider;
* the account is **owned by or lent to** that tenant. Having a pool does not
  mean the pool serves everyone. A personal tenant nobody has lent an account to
  is served by nothing (`quota_broker.accounts.accounts_serving`, invariant 9).

The park's `parked` event records which of these failed, as
`detail.account_pool`: `no_broker_configured`, `profile_takes_no_subscription`
or `no_accounts_registered`. A spent or paused account is not one of them. That
is the pool's own wait. The worker parks on it as `PROVIDER_QUOTA_EXHAUSTED`,
with `next_eligible_at` set to the broker's reset time, or to a fallback when
the broker has none: 300 seconds for an account nobody has read lately, 900 for
a paused one (`lifecycle._park_no_account`).

**What ends those waits, for a tenant served by a pool account.** Such a tenant
usually has no `provider:{p}:tenant:{t}` pool. Terraform creates that pool only
from a tenant's declared `providers`, and declaring one also creates a Job that
mounts a secret the tenant does not have. Two sweeps handle this case:

* **The prewarm sweep** (section 5) is the only code that returns a
  `PROVIDER_QUOTA_EXHAUSTED`, `PROVIDER_COOLDOWN` or `PROVIDER_OUTAGE` park to
  `READY`. The reconciler has no rule for parked tasks. Without the guard pool,
  the sweep does not promote the task early. It promotes it once
  `next_eligible_at` has passed. Until the #171 review it did not promote it at
  all, so the wait never ended.
* **The credential sweep** handles a `CREDENTIAL_MISSING` park that a worker
  wrote on a task the pool serves. That happens when the worker cannot reach
  the broker, falls back to the tenant's key, and finds none (+1 hour), or when
  the broker refuses the worker or the worker can read no account (the 900-second
  fallback). The sweep waits for that park's `next_eligible_at`. Firestore's
  account list still says the pool serves the tenant, and promoting on that
  alone started a container that could only park, once per drain, for as long
  as the outage lasted. A key registered in the meantime makes the answer the
  tenant's own key, and the sweep promotes that at once. Admission's own parks
  carry no `next_eligible_at`, so this wait does not delay them.

---

## 5. Prewarm

`PARKED` tasks whose cooldown expires within `prewarm_lead_seconds` (120) are
promoted to `READY` early, bounded by `prewarm_max_agents`.

This is free, because `READY` costs nothing (invariant 1). The task is simply
already in the rotation when the window reopens, instead of waiting for the next
scheduler wake to notice.

**Do not disable it.** `ENABLE_QUOTA_PREWARM=false` was documented here as
"strictly no early promotion", but it does more than that. This sweep is also
the only code that returns these parks to `READY` once their instant has passed,
so with it disabled, or with `PREWARM_MAX_AGENTS=0`, a quota-parked task is
never returned. That was found in the #171 review and is not fixed there.

Early promotion needs the task's `provider:{p}:tenant:{t}` pool. That pool's
quota cap is what stops admission until the window actually reopens. A task
without one is promoted only once its `next_eligible_at` has passed, and a park
with no `next_eligible_at` at all is left where it is. See section 4 for why a
tenant that runs on a pool account usually has no such pool.

---

## 6. Operating it

```bash
# Provider health, per tenant, with pool effect
./scripts/status.sh

# Or through the API
./scripts/api.sh GET /providers

# Stop admitting anything for one provider; running work continues
./scripts/pause-swarm.sh --provider anthropic
./scripts/resume-swarm.sh --provider anthropic

# Drain a provider gracefully (limit -> 0, no kills)
./scripts/api.sh POST /admin/providers/anthropic/drain

# Disable/enable a provider entirely
./scripts/api.sh POST /admin/providers/anthropic/enabled '{"enabled": false}'

# Raise the ceiling AIMD is allowed to approach
./scripts/api.sh PUT /admin/limits/provider/anthropic '{"hard_limit": 80}'
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
| Tasks park on their first drain after submit | tenant has no key for the profile's provider, and no pool account can run the profile for it | `park_reason: CREDENTIAL_MISSING`; the `parked` event's `detail.account_pool` says which condition in section 4 failed; `scripts/create-secrets.sh --list` |
| A task that ran on a lent account parks `CREDENTIAL_MISSING` and stays parked for up to an hour | the worker wrote that park, not admission. Either it could not reach the broker and the tenant has no key to fall back to (the event has `provider` and no `account_pool_reason`), or the broker refused it (`account_pool_reason: broker_refused`, usually a missing `run.invoker` grant), or it could read no account's secret (`account_unreadable`) | the credential sweep waits for the park's `next_eligible_at` on purpose (section 4). Fix the cause, or register a key: a key promotes the task on the next drain |
| Provider stuck at a low target | AIMD is still climbing after a 429 storm | `quota/{provider}:{tenant}.success_count` vs `AIMD_SUCCESS_THRESHOLD` |
| Provider never recovers | `state=EXHAUSTED` and nothing clears it | `reset_at` in the future, or the broker tick is not running |
| One tenant's 429s throttle everyone | **a regression, not an expected mode** — section 3 forbids a tenant-derived limit on `provider:X`. Compare `provider:X` and `provider:X:tenant:Y` in `status.sh`; if `provider:X` carries a `quota_derived_limit` while any enabled tenant is healthy, `_recompute_provider_pool` has broken and the AIMD test that covers it should be failing |
| Workers burning CPU while rate-limited | `max_in_worker_retry_delay_seconds` set too high | it should stay well under a typical provider window |
