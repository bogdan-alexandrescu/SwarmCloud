# Frozen contract — read before writing any code

`apps/common/swarm_common/` is FROZEN. Import from it; do not redefine its types.
If you believe it needs a change, say so in your report rather than editing it.

    states.py     TaskState(12), CONCURRENCY_STATES(4), TERMINAL_STATES,
                  assert_transition(), ParkReason, BlockedReason, EventType
    models.py     SlotPool, Lease, Task, Attempt, TaskEvent, Tenant,
                  QuotaState, ProviderState, Workflow, WorkflowStep,
                  pool_names_for(), new_id(), utcnow()
    profiles.py   RESOURCE_CLASSES, RUNNER_PROFILES, Backend, resolve_backend()
    admission.py  acquire_lease_in_transaction(), release_lease_in_transaction(),
                  evaluate_capacity(), AdmissionDenied, AdmissionConfig
    config.py     Settings.from_env()
    identity.py   Principal, resolve_tenant(), assert_allowed_domain(), AuthError

## Invariants that must never be violated

1. Only LEASED/DISPATCHED/STARTING/RUNNING create infrastructure demand.
   QUEUED, PARKED and READY cost nothing. Never use pending pods as a backlog.
2. Capacity is reserved ALL-OR-NOTHING across every pool, inside one Firestore
   transaction. Never reserve one pool and fail another.
3. Concurrency counts from LEASED, not RUNNING, so slow starts cannot
   oversubscribe.
4. Workers never sleep through a long provider wait. Longer than
   `max_in_worker_retry_delay_seconds` -> checkpoint, park, release, exit.
5. Every attempt carries a fencing generation. A worker whose generation is
   stale exits WITHOUT running the agent.
6. Spot is disabled platform-wide. Spot Pods cannot use Autopilot extended run
   time, which would break the no-preemption requirement.
7. requests == limits. No bursting; bursting is what gets OOM-killed.
8. Mandatory periodic checkpointing. Cloud Run ephemeral disk is Preview and
   disables live migration, so checkpoints are what make interruption survivable.
9. Per-tenant isolation: own GSA, own secrets, own GCS prefix, own namespace.
   A tenant's provider key must never be reachable from another tenant's pod.
10. API callers pick a runner_profile BY NAME. Never accept images, commands,
    resource specs or backend parameters from a caller.

## Platform decisions

- Project `saga-agents-staging`, region `us-central1`, Firestore database `swarm`
  (NOT `(default)` — shared project).
- Primary backend Cloud Run Jobs. GKE Autopilot only for browser/GPU/>32GiB.
- Auth: Google ID token, hosted domain `saga.xyz`. No shared bearer token.
- Tenant = Google group (`eng@saga.xyz` -> `eng`), personal fallback `u-<user>`.
- Cloud Run sets the service account on the JOB resource and it cannot be
  overridden per execution, so the dispatcher creates per-tenant-per-profile Job
  resources; the reconciler garbage-collects unused ones.
- Scheduler wakes on Pub/Sub, runs a bounded drain loop while admissible work
  exists, then exits. Cloud Scheduler provides a 1-minute safety tick.
- `make destroy` is label-scoped and ABORTS if the plan would delete anything
  lacking `managed-by=swarm-terraform`. The project holds a live GKE cluster
  `agents-staging`, VPC `agents-staging-vpc`, and 12 other service accounts.
