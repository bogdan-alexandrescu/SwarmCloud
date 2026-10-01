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
  Reaffirmed by owner decision 2026-10-01, over `docs/BUILD_PROMPT_V2.md`'s
  plan to make GKE the only backend (which was never built): `mock`, `generic`,
  `claude-code` and `codex` run on Cloud Run Jobs and `browser` alone on GKE
  Autopilot (`apps/common/swarm_common/profiles.py:1073`, `RUNNER_PROFILES`),
  routed by `BackendRouter.for_backend`
  (`apps/scheduler/scheduler/dispatch.py:1696`). No GPU or >32 GiB profile
  exists: `ResourceClass` refuses one at import
  (`apps/common/swarm_common/profiles.py:65`). Why: Cloud Run Jobs has no nodes
  to upgrade, repair or compact, so far fewer things can end a running agent
  (`docs/architecture.md` §5); the browser runner is on GKE because Chromium
  needs a large `/dev/shm`.
- "Pod" in the invariants above means the container an attempt runs in, on
  either backend: a Cloud Run Job execution's task for the four Cloud Run
  profiles, a Kubernetes pod for `browser`. Pods are not the universal unit of
  execution here, and every invariant binds both backends equally.
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

## Verified operational constraint — Cloud Identity group resolution

Tested live against saga-agents-staging on 2026-09-15. Two calls, two different outcomes:

    groups:lookup?groupKey.id=eng@saga.xyz                        WORKS
    groups/{id}/memberships:checkTransitiveMembership             WORKS
    groups/-/memberships:searchTransitiveGroups                   403 Error(4013)
                                                 "Insufficient permissions to retrieve memberships"

So the API MUST NOT resolve a caller's tenant by enumerating the groups they belong to.
Instead, iterate the admin-registered tenant groups and check membership in each:

    GET https://cloudidentity.googleapis.com/v1/{group_id}/memberships:checkTransitiveMembership
        ?query=member_key_id == '<caller email>'
    -> {"hasMembership": true|false}

This needs NO org-level IAM grant, is least-privilege (the platform only ever learns about
groups an admin deliberately registered), and requires no change to the frozen contract:
`identity.resolve_tenant(principal, group_priority)` already takes the admin-ordered group
list, so the API simply populates `Principal.groups` from these per-group checks.

Every Cloud Identity call REQUIRES the header `x-goog-user-project: <project_id>`. Without
it the call fails 403 SERVICE_DISABLED naming gcloud's shared client project as consumer,
which looks like a permissions problem but is not.

Cache membership results per (caller, group) with a short TTL; checkTransitiveMembership is
a network round trip on a request path that must stay fast.

---

**Correction (workspace storage).** This platform does NOT use Cloud Run ephemeral
disk. The Terraform google provider cannot express it (`empty_dir.medium` accepts
only `"MEMORY"`), so workspaces are memory-backed tmpfs and the deployment runs on
the fully-GA path, which DOES support live migration.

Mandatory periodic checkpointing therefore remains required, but for different
reasons than originally written: a worker can still lose its attempt to a quota
park-and-exit, a cancellation, a reconciler reclaim of a stale generation, or an
ordinary crash. Checkpointing is what makes any of those cost minutes instead of
the whole attempt. Do not relax it on the grounds that live migration is now
available — migration covers infrastructure moves, not the application-level
interruptions above.
