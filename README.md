# agent-swarm-infra

A production control plane for running long-lived AI coding agents on Google
Cloud — multi-tenant, capacity-bounded, and built so that **no agent is ever
killed by the platform** and **queued work costs nothing**.

```
   412 QUEUED   31 PARKED   88 READY      <- costs nothing
     3 LEASED    2 DISPATCHED  4 STARTING  34 RUNNING   <- the entire bill
```

That split is the whole idea. A backlog is Firestore documents, not pending pods
that an autoscaler answers by buying nodes.

---

## Quick start

```bash
make prerequisites      # tools, credentials, APIs, shared-project guards
make bootstrap          # state bucket, .env, terraform init
make tf-plan            # READ THIS -- saga-agents-staging is a SHARED project
make tf-apply
make build push deploy  # Cloud Build -> scan -> promote by digest -> Cloud Run
make smoke              # submit a mock task and prove it runs end to end
```

Then register a tenant and give it a key:

```bash
make register-tenant GROUP=eng@saga.xyz PROVIDERS=anthropic
./scripts/create-secrets.sh --tenant eng --provider anthropic --stdin
make register-tenant GROUP=eng@saga.xyz PROVIDERS=anthropic   # re-run to bind the secret
```

That one command creates the tenant's service account, its IAM conditions, its
Firestore documents, and — through `kubernetes/apply.sh` — its whole namespace
with a default-deny NetworkPolicy, a ResourceQuota and Pod Security Admission
labels. See [multi-tenancy.md](docs/multi-tenancy.md#1-what-a-tenant-owns).

Day to day:

```bash
make status             # cluster, jobs, agents, queues, leases, pools, quota
make logs               # recent control-plane logs, redacted
make help               # every target
```

---

## What it does

Submit a task naming a **runner profile**; the platform finds it capacity, runs
it in an isolated per-tenant sandbox, checkpoints it every two minutes, and
returns artifacts.

```bash
./scripts/api.sh POST /tasks '{
  "runner_profile": "claude-code",
  "repository_url": "https://github.com/acme/widgets",
  "input": {"prompt": "Add tests for the retry path"}}'
```

`scripts/api.sh` calls the API as you, with a Google ID token that never reaches
a command line. `curl -H "Authorization: Bearer $(gcloud auth
print-identity-token)"` is the obvious one-liner and is unsafe: argv is
world-readable through `/proc`, an ID token *is* the tenant identity, and it
lands in shell history. See [security.md](docs/security.md#authentication).

The caller chooses a profile **by name** and nothing else. Images, commands,
resource specs and backends come from a frozen catalogue — which is what stops an
authenticated caller turning the swarm into arbitrary compute.

| Profile | Runs | Class | Backend |
|---|---|---|---|
| `mock` | a no-op, for smoke tests (no API key needed) | standard | Cloud Run Job |
| `generic` | a plain command | standard | Cloud Run Job |
| `claude-code` | Claude Code | standard | Cloud Run Job |
| `codex` | Codex CLI | standard | Cloud Run Job |
| `browser` | Playwright + Chromium | browser | GKE Autopilot |

---

## Design decisions worth knowing up front

These are departures from the original brief, each forced by "no preemption, no
OOM kills, no restarts". [architecture.md](docs/architecture.md#5-deliberate-departures-from-the-original-design)
has the full reasoning.

* **Cloud Run Jobs is the primary backend, not GKE Autopilot.** Cloud Run has no
  nodes, no autoscaler and no node upgrades, so it has far fewer mechanisms that
  can end a running agent. Autopilot is kept for browser, GPU and >32 GiB work.
* **Spot is disabled platform-wide.** Verified: Spot Pods cannot use Autopilot
  extended run time, so "Spot preferred" and "no preemption" are mutually
  exclusive. The frozen catalogue raises at import if a profile tries.
* **requests == limits, no bursting.** Bursting past a request is exactly what
  gets a container OOM-killed under node pressure.
* **Resource classes were measured, not guessed.** One working Claude Code lane
  on the reference machine was claude 1,532 MB + pytest 769 MB + node/tsx 207 MB
  ≈ 2.5 GiB, so `standard` is 8 GiB — roughly 2x measured.
* **A known risk, accepted deliberately:** Cloud Run ephemeral disk is Preview
  and, per Google's docs, **disables live migration** — partially undermining the
  reason Cloud Run was chosen for long jobs. Mandatory 120-second checkpointing
  is the compensation, not a cure. See
  [checkpointing.md](docs/checkpointing.md#the-tension-stated-plainly).
* **Multi-tenant from V1.** A tenant is a Google group, with a personal fallback
  tenant. Per-tenant service account, secrets, GCS prefix and namespace; tenants
  bring their own provider keys.
* **Auth is Google ID tokens restricted to `saga.xyz`. There is no shared
  platform bearer token** — a shared secret carries no identity, and
  multi-tenancy has to start at authentication.

---

## Repository layout

```
apps/
  common/swarm_common/   FROZEN domain contract -- import it, never edit it
  swarm-api/             submission, tenancy, admin surface
  scheduler/             bounded drain loop, fairness, dispatch
  quota-broker/          AIMD provider concurrency control
  reconciler/            detects and repairs control-plane/reality drift
  agent-worker/          the process that runs an agent
terraform/
  bootstrap/             state bucket + GitHub WIF (local state, by necessity)
  infra/                 ONE root; environments are tfvars against it
  modules/               network, iam, firestore, storage, cloud_run, gke, ...
  environments/<env>/    <env>.tfvars
images/                  agent-runtime-base, agent-runtime-browser (digest-pinned)
kubernetes/              namespaces, RBAC, network policies, worker templates
scripts/                 the real operator interface; make targets wrap these
docs/                    see below
tests/                   unit (no cloud) and integration
```

---

## Documentation

| Doc | Read it when |
|---|---|
| [architecture.md](docs/architecture.md) | you want the whole picture and the reasoning |
| [concurrency.md](docs/concurrency.md) | limits, leases, fairness, the admission transaction |
| [quota-management.md](docs/quota-management.md) | 429s, parking, AIMD |
| [checkpointing.md](docs/checkpointing.md) | resume behaviour, and the Preview-disk risk |
| [execution-backends.md](docs/execution-backends.md) | Cloud Run vs GKE, dispatch, adding a backend |
| [multi-tenancy.md](docs/multi-tenancy.md) | tenants, groups, secrets, isolation |
| [security.md](docs/security.md) | the threat model, in full |
| [operations.md](docs/operations.md) | the day-to-day runbook |
| [troubleshooting.md](docs/troubleshooting.md) | something is wrong right now |
| [disaster-recovery.md](docs/disaster-recovery.md) | something is very wrong |
| [scaling.md](docs/scaling.md) | growing the fleet; what binds first |
| [cost-control.md](docs/cost-control.md) | the bill |
| [versions.md](docs/versions.md) | exact pins, and why `$PATH` is not trusted |
| [workflows.md](docs/workflows.md) | task DAGs, the local loop, CI/CD |
| [CONTRACT.md](CONTRACT.md) | **before writing any code** |

---

## Safety notes for this project

`saga-agents-staging` is **shared**. It holds a live GKE cluster
(`agents-staging`), a VPC (`agents-staging-vpc`) and 12 service accounts owned by
other teams (promptlab, crawler, aipipeline, external-secrets,
tournament-digest). Nothing here may touch them.

* `make destroy` runs `terraform plan -destroy -json` and **aborts** unless every
  resource marked for deletion carries `managed-by=swarm-terraform`. Unknown
  unlabelable types are treated as offenders — it fails **closed**. It refuses in
  prod without `--allow-prod`, ignores `SWARM_ASSUME_YES`, requires a typed
  confirmation, and re-verifies afterwards that every shared resource still
  exists. Verify the guard itself with `./scripts/destroy.sh --self-test`.
* `make purge-data` deletes runtime data only, exports Firestore first, and
  refuses the `(default)` database and every deny-listed bucket.
* `scripts/configure-kubectl.sh` writes an **isolated** kubeconfig and refuses to
  fetch credentials for another team's cluster.
* Firestore is the named database `swarm`, never `(default)`.

Workstation specifics that are not optional:

* **Images build with Cloud Build**, never the local Docker daemon — this Mac is
  arm64, the targets are amd64, and the local daemon is broken.
* **kubectl is resolved explicitly.** Older binaries win `$PATH` here and an old
  client silently drops manifest fields it does not understand.
* The scripts are written for **bash 3.2** (what macOS ships) and the interactive
  shell is **zsh**, which does not word-split unquoted variables.

---

## Development

```bash
make dev                # Firestore emulator + seeded pools + the API
make test               # unit tests, terraform tests, guard self-tests. No cloud.
make lint               # shellcheck, doc links, terraform fmt/validate, tflint, manifests
make security           # checkov over terraform and the rendered manifests, trivy
```

`make test` is offline: unit tests, the destroy-guard and plan-guard self-tests,
the frozen-contract parity check, and 86 `terraform test` assertions against a
mock provider. No credentials, no emulator, nothing created.

CI (`.github/workflows/`) authenticates to GCP with Workload Identity
Federation; there are **no downloadable service account keys**. Production
applies require manual approval through a GitHub Environment.

---

## License

Apache-2.0. See [LICENSE](LICENSE).
