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
| Provider tokens | what the agent does | usually the biggest line; per-tenant budgets |
| Control plane | Cloud Run min-instances and request volume | scale to zero where latency allows |
| GCS | checkpoints + artifacts | lifecycle rules, retention days |
| Firestore | documents + reads | aggregation queries instead of scans |
| Networking | NAT egress for clones and provider calls | few static IPs; keep registry in-region |
| Cloud Build | image builds | only on merge and manual runs |

For a fleet of coding agents, **provider tokens usually exceed all cloud costs
combined**. Optimise the number of agent-hours before optimising the price of an
agent-hour.

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

Per-tenant budgets park work rather than failing it:

```bash
./scripts/api.sh PUT /admin/tenants/eng/limits \
    '{"max_active": 25, "capacity_units": 50, "monthly_budget_usd": 2000}'
```

A tenant over budget gets `PARKED(BUDGET_EXHAUSTED)` — free, resumable,
unsurprising — instead of failed tasks someone retries by hand.

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
| `dispatch_timeout_seconds` | 300 | a slot reserved for an execution that never started |
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
