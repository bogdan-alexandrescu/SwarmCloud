# SwarmCloud

A zero-idle, multi-tenant platform for running AI coding agents on Google Cloud.

Agents run as ephemeral cloud jobs instead of on a laptop. Work that is queued,
blocked on provider quota, or waiting on a dependency costs nothing at all: it
is a row in Firestore, not a sleeping container. When there is nothing runnable,
agent compute is zero.

> **Status: the control plane works end to end; the agent runtime does not yet.**
> A submitted task is authenticated, attributed to a tenant, admitted through
> atomic multi-pool concurrency control, and dispatched to a real Cloud Run Job
> execution. The worker container then exits 1. See
> [Current state](#current-state) for exactly what is proven and what is not.

## The idea

Most agent platforms keep a worker alive while it waits — for a rate limit to
reset, for a dependency, for a retry window. That is a container billing you to
sleep. SwarmCloud makes waiting a *durable state* rather than a *running
process*:

```
desired work → durable task state → eligibility + quota check
             → concurrency admission → execution lease → compute demand
```

Infrastructure follows admitted work. It never leads it. A backlog of ten
thousand tasks produces zero pods, because nothing creates compute until it
holds a lease.

## Architecture

```
 client ──▶ Swarm API ──▶ Firestore ──▶ Scheduler ──▶ Cloud Run Jobs
           (Cloud Run)    tasks         (admission)    (agents, 0→N→0)
            min=0         leases              │
                          pools               └────▶ GKE Autopilot
                          quota                      (browser, GPU)
                                          Reconciler
                                       (repairs leaks)
```

**Control plane** — four Cloud Run services at `min-instances=0`, so idle cost is
zero. **Execution plane** — Cloud Run Jobs by default; GKE Autopilot for browser,
GPU and workloads above 32 GiB.

### Decisions worth knowing

**Cloud Run Jobs is the primary backend, not Kubernetes.** The requirement was no
preemption, no OOM kills, no restarts. Cloud Run has no nodes, no autoscaler and
no node upgrades, so there is far less that can kill a job mid-run.

**Spot is disabled everywhere.** Spot Pods cannot use GKE Autopilot's extended run
time, so "Spot preferred" and "no preemption" are mutually exclusive. We chose
no preemption.

**`requests == limits`, no bursting.** Bursting past a request is exactly what
gets a container OOM-killed under node pressure.

**Resource classes were measured, not guessed.** One working Claude Code lane on
the reference machine was `claude` 1.5 GB + `pytest` 0.8 GB + node/tsx 0.2 GB ≈
2.5 GiB, so the classes are roughly double that. The worker exports peak RSS and
peak disk per runner profile so the numbers get corrected from production.

| class | vCPU | memory | workspace |
|---|---|---|---|
| `standard` | 4 | 8 GiB | ~4 GiB |
| `browser` | 8 | 16 GiB | ~8 GiB |
| `large` | 8 | 32 GiB | ~16 GiB |

Workspaces are **memory-backed tmpfs, carved out of the class's memory** — not
extra disk. Cloud Run's disk-backed ephemeral storage is Preview and the
Terraform provider cannot express it (`empty_dir.medium` accepts only `MEMORY`).
The upside is that this path is fully GA and supports live migration, which the
Preview disk explicitly does not.

### Multi-tenancy

A tenant is a **Google group**, resolved through Cloud Identity, with a personal
fallback tenant for anyone in no registered group. Each tenant gets its own
service account, its own provider API keys in Secret Manager, its own GCS
prefix, its own Cloud Run Job resources, and its own slot pools. Admission
round-robins across tenants so one tenant cannot starve another.

Authentication is a Google ID token restricted to a hosted domain. There is no
shared platform bearer token: a shared secret carries no identity, and without
identity there is no tenant to attribute a task to.

## Current state

**Proven on a live deployment:**

- 188 Terraform resources apply cleanly; **zero deletions across every plan**
- Google ID token auth, hosted-domain enforcement, tenant resolution
- **Atomic admission** — `READY → LEASED` reserving every applicable pool in one
  Firestore transaction, or none
- Caller-supplied `image` and `command` rejected — callers pick a runner profile
  by name and nothing else
- **Dispatch** — a real Cloud Run Job execution created from an admitted lease
- Leak recovery — the reconciler reclaims stranded leases and returns slots
- 538 Python tests, 87 Terraform tests

**Not yet working:**

- **The worker container exits 1** with no logs. It is the one service that never
  received the logging fix, so it fails silently for the same reason everything
  else did.
- **No task has reached `SUCCEEDED`.** The platform around the agent works; the
  agent does not run yet.
- **GKE Autopilot path is unexercised.** The reconciler cannot reach a private
  control plane from Cloud Run without authorized-networks work.
- **Firestore has no per-database IAM boundary.** IAM Conditions are not
  evaluated on Firestore's data plane, and server SDKs bypass Security Rules. Any
  swarm identity can reach any Firestore database in the project. Today there is
  only one. See [`docs/security.md`](docs/security.md) — this is documented
  honestly rather than claimed as solved.

## Quick start

```bash
cp .env.example .env          # set PROJECT_ID, REGION
gcloud auth login && gcloud auth application-default login

make bootstrap                # TF state bucket, APIs
make infra                    # VPC, Firestore, IAM, Cloud Run, jobs
make build && make push       # images via Cloud Build (never local Docker)
make deploy
make smoke
```

Submitting work:

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     -H "Content-Type: application/json" \
     -X POST "$API_URL/v1/tasks" \
     -d '{"runner_profile":"mock","input":{"prompt":"hello swarm"},"timeout_seconds":300}'
```

`make destroy` is **label-scoped**: it aborts if the plan would delete anything
lacking `managed-by=swarm-terraform`, because the target project is shared.

## Development

```bash
make test                     # 538 python + 87 terraform tests
make lint                     # tflint, checkov, trivy, shellcheck
uv run python scripts/dev/drive.py state      # dump live control-plane state
uv run python scripts/dev/drive.py drain      # one scheduler pass, locally
uv run python scripts/dev/drive.py reconcile  # one reconciliation pass
```

`scripts/dev/drive.py` runs the real scheduler and reconciler in-process against
the real Firestore. Deploying to diagnose costs about eight minutes per
iteration; this costs seconds, and it is how most of the integration bugs in this
repository were found.

CI runs on every push to `main`: unit and integration tests, shellcheck,
Kubernetes manifest rendering, `terraform fmt/validate/test`, tflint, checkov,
trivy and secret scanning. Deployments require Workload Identity Federation and
a manual environment approval — no downloadable service-account keys exist.

## Documentation

[`architecture`](docs/architecture.md) · [`concurrency`](docs/concurrency.md) ·
[`quota management`](docs/quota-management.md) ·
[`checkpointing`](docs/checkpointing.md) ·
[`multi-tenancy`](docs/multi-tenancy.md) · [`security`](docs/security.md) ·
[`operations`](docs/operations.md) · [`cost control`](docs/cost-control.md) ·
[`troubleshooting`](docs/troubleshooting.md)

[`CONTRACT.md`](CONTRACT.md) holds the invariants every component must respect.

## License

MIT — see [LICENSE](LICENSE).
