# Cost control

The structural claim this platform makes:

> Queued work costs nothing. Only `LEASED`, `DISPATCHED`, `STARTING` and
> `RUNNING` create infrastructure demand.

Ten thousand `QUEUED` tasks are ten thousand Firestore documents — cents per
month. The same backlog expressed as pending Kubernetes pods is a cluster
autoscaler buying nodes to satisfy scheduling requests that will not be satisfied
for hours. That difference is the single largest cost decision in the design, and
it is why "never use pending pods as a backlog" is invariant 1 rather than a
guideline.

---

## 1. Where money actually goes

| Line | Driver | Control |
|---|---|---|
| Agent compute | concurrent agents x wall clock x class | slot pools, timeouts, right-sizing |
| Provider tokens | what the agent does | usually the biggest line; provider pools, recorded per attempt (no budgets, §2) |
| Control plane | Cloud Run min-instances and request volume | scale to zero where latency allows |
| GCS | checkpoints + artifacts | lifecycle rules, retention days |
| Firestore | documents + reads | aggregation queries instead of scans |
| Networking | NAT egress for clones and provider calls | few static IPs; keep registry in-region |
| Cloud Build | image builds | only on merge and manual runs |

For a fleet of coding agents, **provider tokens usually exceed all cloud costs
combined**. Optimise the number of agent-hours before optimising the price of an
agent-hour.

### Provider tokens: the prompt-cache TTL (#323)

**Today no TTL is set: Claude Code's default is in force.** Nothing below is
built. The section records a measurement, the owner's decision not to act on it
yet, and where the knob would go, so the question is not re-measured from
scratch. The full working, with the per-task table, is in
[issue #323](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/323).
`tests/unit/scripts/test_prompt_cache_ttl_doc.py` fails if the runner starts
setting a TTL while this section still says the default is in force.

**What was measured** (read 2026-09-29, tenant `eng`, 47 of the 50 newest
terminal claude-code tasks; the other 3 had no `structured_output`). Prices are
Opus 5.5 list per MTok: cache write $5 at 5m and $8 at 1h, cache read $0.20,
output $20.

| | $ | share |
|---|--:|--:|
| 1h cache writes | 35.16 | 35% |
| cache reads | 34.19 | 34% |
| output | 30.53 | 30% |
| 5m cache writes (one subagent) | 0.64 | 0.6% |
| uncached input | 0.01 | 0.0% |
| **total** | **100.53** | |
| at 5m, no cache expiry (upper bound) | 87.35 | saving 13.1% |
| at 5m, gap-adjusted | 91.23 | saving 9.3% |

The largest single line was cache writes at the 1h rate, larger than output.
The 1h TTL pays only when a request follows the previous one by more than
5 minutes. Over 1,574 main-chain requests, 5 did, one in each of 5 tasks. 4 of
those 5 tasks would have cost *more* at 5m, because the prefix is re-written
after each gap. The gap-adjusted row charges that re-write.

Per-task totals come from the result's `modelUsage`, not its top-level `usage`
block. `usage` covers only the last init/success pair, so a task the worker
resumed is under-counted. `task_9be4128488d342208947` was the one case where the
two disagreed.

**Why the main chain gets 1h.** Claude Code requests 1h for the main
conversation only under a Claude subscription within plan usage. It requests 5m
for subagents, and 5m everywhere under API-key billing
([Claude Code docs](https://code.claude.com/docs/en/prompt-caching#choose-the-ttl-yourself)).
The runner hands the child `CLAUDE_CODE_OAUTH_TOKEN` when the tenant's
credential is a subscription token (`cliagent._credential_env`), and the
observed split, main chain 1h and subagent 5m, matches the subscription column.
On a subscription the dollars above are notional (`costBasis: "list"`). What
is actually consumed is plan quota.

**Quota cannot settle it.** Under 1h, a median task drew about 0.3% of a 5h
window and the p90 about 1.6%. That rests on one `eng:team` reading, so treat it
as medium confidence. The data cannot separate a 1h write's quota draw from a
5m write's, because every main-chain write in it was 1h. No amount of history
from this population can split the two rates. The binding constraint was not
the 5h window but the shared seats' weekly window, driven by use outside
SwarmCloud.

**The decision.** Owner, 2026-09-29: no TTL change. The controlled A/B that
would answer the quota question is parked as not worth two 5h windows. It is
about 30 tasks per arm on one account with a fresh 5h window, the arms differing
only in the TTL, sampling `sc accounts --json` every 5 minutes. The method and
commands are in the issue. Any of these would reopen it:

- the tenant moves to API-key billing. Then 5m is Claude Code's default anyway
  and the dollars are real;
- the weekly window stops binding, so the 5h window is what limits;
- Anthropic documents how a 1h write is weighted against plan quota.

**The knob, if it is reopened.** It is platform-set only. Invariant 10 means a
caller never supplies env, args or settings, so a per-profile or per-stage TTL
comes from the profile catalogue. There are three routes, cheapest first:

1. Add `"promptCacheTtl": "5m"` to the settings dict in
   `apps/agent-worker/agent_worker/runners/claude_code.py::write_headless_settings`.
   The runner already writes that file and passes it as `--settings` after any
   `CLAUDE_CODE_ARGS`. That needs no passthrough change and no restated argv.
   (#323's measurement comment says the child has no settings file. That is
   wrong: `HOME` is a fresh `work/` with no user settings, but this
   platform-written file is passed on every run.)
2. Add `CLAUDE_CODE_PROMPT_CACHE_TTL` to `cliagent._PLAIN_PASSTHROUGH` and set
   it on the claude-code Job in `terraform/infra/locals.tf`. The child's
   environment is an allow-list, so setting it on the Job alone does not reach
   `claude`. This route is the one to take for a per-profile value.
3. Set `CLAUDE_CODE_ARGS` to the default argv plus
   `--settings '{"promptCacheTtl":"5m"}'`. This needs no worker change, but it
   replaces the whole default argv, so every default flag has to be restated.
   It also puts a second `--settings` before the platform's. How the CLI
   combines two `--settings` was not checked.

The settings key and the env var need Claude Code v2.1.242 or later.
`images/agent-runtime-base/Dockerfile` pins `CLAUDE_CODE_VERSION=2.1.283`.
`subagentPromptCacheTtl` / `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` govern
subagents separately. `FORCE_PROMPT_CACHING_5M=1` forces 5m for both and wins
over everything; `ENABLE_PROMPT_CACHING_1H=1` requests 1h for both.

---

## 2. Ceilings you actually own

Every pool is a spend ceiling with a unit of measure:

```
global                         total agents
tenant:<id>                    one team's share
resource:<class>               how many expensive shapes at once
provider:<p>                   provider spend rate, platform-wide
provider:<p>:tenant:<t>        provider spend rate, per key
backend:<b>                    Cloud Run vs GKE split
```

A rough monthly ceiling:

```
max_monthly_cloud_cost ≈ global_limit x avg_class_hourly x utilisation x 730
```

With `global = 100`, `standard` at roughly $0.20/hour and 40% utilisation, the
cloud side lands near $5.8k/month. The provider side is separate and typically
larger.

**There are no per-tenant dollar budgets. They are not built and not planned**
(owner decision, 2026-10-01). A tenant's spend is bounded by the pools above —
`max_active` and `capacity_units` — which the scheduler enforces on every
admission:

```bash
./scripts/api.sh PUT /admin/tenants/eng/limits \
    '{"max_active": 25, "capacity_units": 50}'
```

What exists, and what does not:

* **Per-attempt cost IS recorded.** The worker's `record_spend`
  (`apps/agent-worker/agent_worker/control.py::ControlPlane.record_spend`), called from every exit by
  `_record_spend` (`apps/agent-worker/agent_worker/lifecycle.py::Worker._record_spend`), writes
  the runner's token counts and `cost_usd` onto the attempt
  (`apps/common/swarm_common/models.py::Attempt.cost_usd`). It is the provider cost the runner
  reports — `total_cost_usd` from the CLI's result — not the cloud bill, and a
  cost the runner did not report is omitted, not written as zero. The attempts
  list reports how many rows carry one
  (`apps/swarm-api/swarm_api/routes/attempts.py::spend_coverage`), because a sum over
  partially reported rows is a lower bound that looks like a total.
* **`monthly_budget_usd` is refused with a 422**
  (`apps/swarm-api/swarm_api/routes/admin.py::set_tenant_limits`). Storing it would echo a
  number back with a 200 and enforce nothing, and an admin would believe they
  had a spend control.
* **`PARKED(BUDGET_EXHAUSTED)` is never written.** The value stays in the frozen
  `ParkReason` enum (`apps/common/swarm_common/states.py::ParkReason.BUDGET_EXHAUSTED`) because the enum
  is frozen, not because anything uses it; no sweep reads it either
  (`apps/scheduler/scheduler/loop.py::Scheduler._stop_for_failed_workflow`). Removing it is a request, not an
  edit: [contract-change-requests.md](contract-change-requests.md) entry 39.

The constraint anyone revisiting this would face: `record_spend` writes when an
attempt ends, so a budget fed by it could only refuse the *next* admission after
the money was spent, and a tenant at `max_active: 25` could overshoot by up to
25 attempts' worth. Concurrency and capacity units bound spend *before* it
happens, at admission, which is why they are the controls this platform keeps.

---

## 3. Spot is off, and why that is not a missed saving

Spot would cut compute by 60–91%, and it is disabled platform-wide.

The reason is specific and verified: **Spot Pods cannot use GKE Autopilot
extended run time.** Extended run time is what stops Autopilot evicting a
long-running pod for consolidation. So "Spot preferred" and "no preemption" are
mutually exclusive — not awkward together, mutually exclusive.

The frozen `RunnerProfile.__post_init__` raises at import if a profile sets
anything but `ON_DEMAND_ONLY`, so this cannot be re-enabled by a config change.

The saving is also smaller than it looks for this workload. A two-hour agent run
preempted at 90 minutes costs the on-demand price of 90 minutes **plus** the
provider tokens for 90 minutes of work, and those tokens are the expensive part.
Re-running them at a 70% compute discount is not a saving.

---

## 4. The Preview-disk tension

**Retracted. This platform does not use Cloud Run ephemeral disk, so the tension
this section described does not exist.** The section is kept under its original
heading because other documents link to it and because the conclusion it reached
— that checkpointing is not optional and every attempt pays for it — is still
correct, for different reasons.

What was written here: Cloud Run ephemeral (second-generation) disk is
**Preview**, enabling it **disables live migration**, and Cloud Run was chosen
partly for having fewer ways to interrupt a long job, so the feature reintroduced
one.

What is actually deployed: the Terraform google provider cannot express that
feature — `empty_dir.medium` accepts only `"MEMORY"` — so workspaces are
memory-backed tmpfs on the fully-GA path, which **does** support live migration.
The reliability requirement that drove the Cloud Run choice is better served than
by the feature we set out to use.

Checkpointing stays mandatory, and the bill below is unchanged. Live migration
covers **infrastructure** moves; it does nothing about the application-level
interruptions that actually end attempts here — a quota park-and-exit, a
cancellation, a reconciler reclaim of a stale generation, an ordinary crash. Do
not read "live migration is available now" as a reason to lengthen the interval;
see [checkpointing.md](checkpointing.md#the-tension-stated-plainly).

The cost consequence is that checkpointing is not optional, so every attempt pays
for it:

```
checkpoints per hour  = 3600 / 120 = 30
cost per checkpoint   ≈ compress + upload (typically a few hundred MB)
storage               ≈ retained checkpoints x archive size x retention days
```

Since #637 (2026-10-05) the second line is the cost of an attempt's FIRST
checkpoint only. The history analysis that day measured 247 GB uploaded, a
median of 66 MB every 120 s, of which 1.25% was ever restored. Now dependency
and build directories are left out, every later checkpoint uploads only what
changed since the one before, and a periodic checkpoint of an unchanged tree
uploads nothing while its interval backs off to 10 minutes. The final, park,
cancellation and interruption checkpoints are still always written.
[checkpointing.md](checkpointing.md#incremental-archives-after-the-first) has
the details.

That is a real, recurring line on the bill, and the alternative is losing whole
attempts — including their provider tokens — to a park, a cancellation, a
reclaim or a crash. Paying 30 uploads an hour to avoid re-running two hours of an
agent is the cheaper side of the trade, but it is a trade, not a free win. Keep
`artifact_retention_days` tight (14 in dev, 180 in prod) and let lifecycle rules
do the rest.

---

## 5. Timeouts are a cost control

An agent that hangs costs money until something stops it:

| Setting | Default | Cost if wrong |
|---|---|---|
| `RunnerProfile.timeout_seconds` | 600–7200 | a hung agent bills to the ceiling |
| `dispatch_timeout_seconds` | 480 | a slot reserved for an execution that never started, for up to 8 minutes (it was 5 until contract request 37) |
| `lease_timeout_seconds` | 120 | a dead worker's slot, until the reconciler sweeps |
| `max_in_worker_retry_delay_seconds` | 45 | **the expensive one** — see below |

`max_in_worker_retry_delay_seconds` is the rule that stops workers being billed
to wait. Anything longer than 45 seconds means checkpoint, park, release the
lease, exit. Forty workers sleeping through a 20-minute rate-limit window is
13 agent-hours of nothing; the same forty tasks parked cost zero.

Raising this value is the easiest way to accidentally multiply the bill.

---

## 6. Cheap control plane

* `swarm-api` scales to zero when idle; set `min-instances: 1` only if cold-start
  latency on the first submission actually matters to you.
* `swarm-scheduler` **must** scale to zero — it is event-driven and bounded by
  design. A min-instance scheduler is a process being paid to not run.
* `swarm-quota-broker` and `swarm-reconciler` are tick-driven; zero minimum.
* The 1-minute safety tick is one tiny request per minute — deliberately cheap
  insurance against a lost Pub/Sub nudge.

`service_max_instances` in the tfvars caps the other direction, so a traffic spike
cannot turn into an unbounded Cloud Run bill.

---

## 7. Watching and reacting

```bash
./scripts/status.sh                    # active agents, per pool, right now
make logs                              # recent control-plane activity

# Everything: stop admitting, keep running work
./scripts/pause-swarm.sh
./scripts/pause-swarm.sh --all --drain # and wait for RUNNING to reach zero

# Surgical
./scripts/pause-swarm.sh --tenant eng
./scripts/pause-swarm.sh --provider anthropic
./scripts/resume-swarm.sh              # re-enables exactly what was paused
```

`resume-swarm.sh` re-enables only what `pause-swarm.sh` recorded, never
"everything" — a pool may have been paused separately by an operator draining a
bad tenant or by the quota broker, and a blanket enable would silently undo that
decision.

Terraform creates budget-shaped alerts (`create_alerts`, `alert_emails`) for
control-plane 5xx, failing job executions, a stopped safety tick, dead-lettered
wake messages and tasks exhausting every attempt. The last one matters for cost:
tasks dead-lettering in volume means work is being paid for repeatedly and thrown
away.

---

## 8. Costly mistakes, in the order they happen

1. **Raising `max_in_worker_retry_delay_seconds`** so workers "just wait it out".
   This converts parked (free) time into billed idle time across the whole fleet.
2. **Raising `global` without raising provider caps.** More agents contend for the
   same provider quota, so each one runs slower while billing the same — you pay
   more for the same throughput.
3. **Long timeouts on exploratory profiles.** A 2-hour timeout on a task that
   should take 10 minutes bills the full 2 hours when it hangs.
4. **Retry storms.** `max_attempts` x a task that always fails is a multiplier on
   both compute and tokens. Watch the dead-letter alert.
5. **Artifacts without lifecycle rules.** Checkpoints every 120 s, never expired,
   grows without bound.
6. **Leaving `make dev` pointed at a real project.** The local loop uses the
   Firestore emulator precisely so experimentation costs nothing.
