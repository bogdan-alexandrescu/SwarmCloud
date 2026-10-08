# SwarmCloud

A zero-idle, multi-tenant platform for running AI coding agents on Google Cloud.

Agents run as ephemeral cloud jobs instead of on a laptop. Work that is queued,
blocked on provider quota, or waiting on a dependency costs nothing: it is a row
in Firestore, not a sleeping container. When nothing is runnable, agent compute
is zero.

The deployed instance is **SwarmCloud**, at `swarm.saga.xyz`, behind
Identity-Aware Proxy. It builds most of this repository: a pull request whose
commit says *"Made by the worker for task …"* was written by a SwarmCloud agent,
reviewed by another, and merged through the pipeline described below.

> **Status, read from `main` on 2026-10-07.** Agents run end to end: a task is
> authenticated, attributed to a tenant, admitted, dispatched to a Cloud Run Job,
> runs Claude Code or Codex against a cloned repository, checkpoints, and
> publishes a pull request as the person who dispatched it. Issue runs, the CI
> fixer and the workflow merge step are built. Onboarding is designed and only
> partly built. See [Current state](#current-state) for what is built, what is
> proven live, and what is planned.

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
holds a lease. A worker that hits a long provider wait checkpoints, parks,
releases its lease and exits; it does not sleep.

The same rule extends upward. A workflow is a graph of tasks; an issue run is a
workflow planned from a GitHub issue; a merge step that waits for CI parks the
same way a quota wait does. Every layer waits as a Firestore row.

## Architecture

```
 console (swarm.saga.xyz) ─┐
 sc plugin / swarm CLI ────┼─▶ swarm-api ──▶ Firestore ──▶ scheduler ──▶ Cloud Run Jobs
 swarm-mcp bridge ─────────┘   (Cloud Run)   tasks          (admission)   (agents, 0→N→0)
                                  │          leases               │
                                  │          pools                └────▶ GKE Autopilot
                                  │          workflows, runs              (browser)
                                  │          repositories
                                  ▼                │
                               GitHub        quota-broker    reconciler
                               (forge)       (accounts,      (repairs leaks,
                                              provider quota) stalled workflows)
```

**Control plane** — `swarm-api`, `swarm-scheduler`, `swarm-quota-broker` and
`swarm-reconciler`, Cloud Run services at `min-instances=0`, plus the
`swarm-ui` console. Per-tenant Cloud Scheduler ticks advance issue runs, wake
merge steps, poll registered repositories for index runs and sweep workflow
rollups. **Execution plane** — Cloud Run Jobs for `mock`, `generic`,
`claude-code`, `codex` and the repository `indexer`; GKE Autopilot for
`browser` alone. The full picture is in
[`docs/architecture.md`](docs/architecture.md).

**Clients.** One Python package, `apps/swarm-mcp`, ships three commands: `swarm`
(the full CLI), `swarm-mcp` (an MCP bridge for Claude Code and other MCP
clients) and `sc` (status, sign-in and issue runs). The `sc` Claude Code plugin
in [`plugin/`](plugin/) wraps them as `/sc` commands, skills and agents; setup is
in [`docs/plugin-setup.md`](docs/plugin-setup.md).

### Decisions worth knowing

**Cloud Run Jobs is the primary backend, not Kubernetes.** The requirement was no
preemption, no OOM kills, no restarts. Cloud Run has no nodes, no autoscaler and
no node upgrades, so there is far less that can kill a job mid-run. The owner
reaffirmed this on 2026-10-01 over a plan to make GKE the only backend, which
was never built. `browser` runs on GKE because Chromium needs a large
`/dev/shm`. No GPU or >32 GiB profile exists.

**Spot is disabled everywhere, and "Spot preferred" is impossible here, not just
undesirable.** Spot Pods cannot use GKE Autopilot's extended run time, so Spot
and "no preemption" are mutually exclusive. We chose no preemption.

**`requests == limits`, no bursting.** Bursting past a request is exactly what
gets a container OOM-killed under node pressure.

**Resource classes were measured, not guessed.** One working Claude Code lane on
the reference machine was `claude` 1.5 GB + `pytest` 0.8 GB + node/tsx 0.2 GB ≈
2.5 GiB, so the classes are roughly double that. The worker exports peak RSS and
peak disk per runner profile so the numbers get corrected from production.

| class | vCPU | memory | workspace (inside memory) |
|---|---|---|---|
| `standard` | 4 | 8 GiB | ~4 GiB |
| `browser` | 8 | 16 GiB | ~8 GiB |
| `large` | 8 | 32 GiB | ~16 GiB |

**Workspaces are memory-backed tmpfs, carved out of the class's memory — not
extra disk.** Cloud Run's disk-backed ephemeral storage is Preview and the
Terraform provider cannot express it (`empty_dir.medium` accepts only `MEMORY`).
So the deployment runs on the fully-GA path, which **does** support live
migration; the Preview disk would have disabled it.

**Checkpointing is still mandatory and periodic.** Live migration covers
infrastructure moves, not the interruptions that actually end attempts here: a
quota park-and-exit, a cancellation, a reconciler reclaim of a stale generation,
an ordinary crash. Checkpoints make those cost minutes instead of the whole
attempt. Since #637 they leave out build directories, upload only what changed
after an attempt's first, and back off while the tree is unchanged.

**Concurrency counts from `LEASED`, and capacity is reserved all-or-nothing.**
Admission reserves every applicable pool — global, tenant, resource class,
runner profile, backend and, for a provider, the provider and the tenant's share
of it — in one Firestore transaction or none of them, so a slow start cannot
oversubscribe and a half-reserved task cannot strand a slot. A pool with no
`hard_limit` is refused as `POOL_LIMIT_UNSET` (#374) rather than read as
unlimited.

**Every attempt carries a fencing generation.** A worker whose generation is
stale exits without running the agent and without touching the lease. The
reconciler fences, releases and repairs a dead worker in one transaction (#560),
and records a superseded attempt's end in the same transaction (#630).

**Callers pick a `runner_profile` by name and nothing else.** Images, commands,
resource specs and backend parameters are never accepted from a caller.
Workflow specs are signed, and prod enforces the signature (#355).

**Release images are built once and promoted by digest.** A pull request that
changes an image's inputs builds it on the GitHub runner with no Google identity
and pushes nothing (#650); every image installs third-party packages from
`uv.lock` by hash (#663).

### Multi-tenancy

A tenant is a **Google group**, resolved through Cloud Identity, with a personal
fallback tenant (`u-<user>`) for anyone in no registered group. Each tenant gets
its own service account, its own provider keys and git token in Secret Manager,
its own GCS prefix, its own Cloud Run Job resources, its own slot pools and its
own registered repositories. Admission round-robins across tenants so one tenant
cannot starve another.

Tenant resolution checks membership **per registered group**:
`searchTransitiveGroups` returns 403 in this project, so the API never
enumerates a caller's groups, and every Cloud Identity call carries
`x-goog-user-project`. See [`docs/multi-tenancy.md`](docs/multi-tenancy.md).

**A person in several tenants picks one per request (#447).** The API reads
`X-Swarm-Tenant`, which selects among the caller's *verified* memberships and
never grants one (`403 tenant_not_member` otherwise). The console shows a
switcher when `/v1/tenants/mine` lists more than one tenant; the CLI takes
`swarm --tenant <id>` or `SWARM_TENANT`; the MCP bridge reads `SWARM_TENANT` at
start and lists choices with `swarm_tenants`. Browser-fetched downloads carry
`?tenant=`, checked the same way (#617).

Authentication is a Google ID token restricted to a hosted domain. There is no
shared platform bearer token: a shared secret carries no identity, and without
identity there is no tenant to attribute a task to.

**Firestore has no per-database IAM boundary.** IAM Conditions are not evaluated
on Firestore's data plane, and server SDKs bypass Security Rules, so any swarm
identity can reach any Firestore database in the project. Today there is only
one. [`docs/security.md`](docs/security.md) documents this rather than claiming
it solved.

**A forge token is never written to this repository**, a tfvars file, a Job
environment or a log. It lives only in Secret Manager, stored with
`scripts/create-secrets.sh --stdin`, and the worker reads it at runtime. Every
request carrying it goes through one opener that follows no redirect (#645).

## How work flows

**A task** is one agent run: a runner profile, an input, a timeout. It moves
`SUBMITTED → QUEUED → READY → LEASED → DISPATCHED → STARTING → RUNNING`, may sit
`PARKED` on a quota wait, a dependency or a CI wait, and ends `SUCCEEDED`,
`FAILED`, `CANCELLED` or `DEAD_LETTERED`; only the four from `LEASED` to
`RUNNING` hold capacity. A finished task releases its dependants and
runs one admission pass at once through a `task_finished` wake, with the
1-minute tick kept as the safety net (#636).

**A workflow** is a signed graph of steps ([`docs/workflows.md`](docs/workflows.md)).
The common shape is **implement → review → fix, gated on the review's verdict**:
the review writes `verdict.json` with `MERGE` or `NOT_YET`. On `MERGE` no fix
agent runs and the implementer's work is published as the pull request (#644);
on `NOT_YET` a fix agent works through the blockers and majors. An unreadable
verdict is not a `MERGE`. Minor findings are filed once each on the tenant's
findings epic (#638). A step with `allow_empty_diff` succeeds on an empty diff
and skips the steps that needed a change. The console's **Decision card** shows,
for a verdict-gated step, the rule, the verdict, the findings by severity, what
happened instead and the inputs staged (#805).

**An issue run** turns a GitHub issue into a merged pull request
([`docs/issue-runs.md`](docs/issue-runs.md), #454): a planner reads the issue,
open work and overlapping pull requests and writes a plan; a person approves it
(or `--plan auto` skips the pause); the plan compiles to an `integrate` workflow
whose `depends_on` become parallel stages; the workflow opens a pull request;
red CI gets up to `fix_rounds` fix rounds; and the issue is closed only when the
review marks **every** planned requirement met — otherwise the pull request says
`part of #N`. A build that changes nothing ends `DONE already_on_main`, posts
its verification table on the issue, and closes it only when every requirement
is met (#646). Start one from the console's *Submit from a GitHub issue*,
`/sc run --issue owner/repo#N`, `uv run sc run --issue …`, or the MCP tool
`swarm_run_issue`.

**Merging** has two paths, both on `main`:

* **`auto-merge.yml`** merges a pull request labelled `ready` through GitHub's
  native auto-merge once its required checks are green
  ([`docs/runbooks/merge-app.md`](docs/runbooks/merge-app.md)). The label binds
  to the labelled head: a push that only merges the base keeps `ready` and
  auto-merge and re-arms once at the new head; any other push turns auto-merge
  off with the merge App's token, and the job fails if it stays on (#795). A label that lands while checks
  still run is re-evaluated when they finish (#705). A merged pull request closes
  the issues its closing references name, never a `part of #N` (#621).
* **The workflow merge step** ([`docs/merge-step.md`](docs/merge-step.md), #352)
  merges on the tenant's git token. While CI runs it parks `CI_PENDING` with its
  capacity refunded; a per-tenant `merge_wake` tick marks it once the checks
  settle, with a 15-minute fallback. Behind its base, it has GitHub update the
  branch and re-parks at the new head; red with `merge_fix_rounds` left, it hands
  the pull request to a fix round. A **merge-only workflow** names a pull request
  no workflow opened with `merge_pr {number, head_sha}`, in the tenant's
  registered repository, and merges it only at that head (#790).

## Current state

Everything in this section is on `main` as of 2026-10-07 unless it says
*planned*, with the issue that tracks it.

**The core platform** — authenticated, tenant-attributed, atomically admitted
tasks; dispatch to Cloud Run Jobs and GKE Autopilot; fencing; lease heartbeats
that outlive a CPU-saturated agent, a checkpoint, the egress wait and the clone
(#426, #742); a reconciler that fences and repairs dead workers, cancels an
execution whose cancel is older than 600 s (#627), detects stalled workflows and
promotes ready parked steps (#616), and gives back account holds an attempt did
not release (#380). Task results report cost over every attempt, with the last
attempt as secondary (#681).

**Agents that finish their work.** The claude-code runner refuses background
commands, runs the expected-outputs check and the publish credential scan before
it ends, and resumes its session for up to two repair turns naming what failed
(#618, #624). An agent may ask the owner in `questions.json`; the worker
validates it and the bridge surfaces it (#693). Every published commit names the
person who dispatched the task, not a bot (#764, #765), and an issue run's pull
request is titled by its issue.

**Issue runs, end to end** (#454) — plan, approval, workflow, pull request, CI
fix loop and close, as described [above](#how-work-flows). The first live proof
was issue #808, fixed by worker-built PR #811 and merged on 2026-10-07.

**The CI fixer on red pull requests** (#263). When `application` fails on a
`swarm/<task-id>` branch, `ci-fix.yml` submits a one-step continuation of the
task that wrote it, with the redacted failure excerpt; the fix is pushed onto
the red branch, never forced. Two tries per pull request in the workflow, a
ceiling the API also holds, and each outcome is posted on the pull request
(#728). It can continue an `integrate` workflow's integrator (#754). It was
proven on PR #775. [`docs/ci.md`](docs/ci.md) has the design.

**The merge step and merge-only workflows** (#352) — lanes MS1-MS5 and MS7 are
built: `merge_fix_rounds`, the `CI_PENDING` park and `merge_wake` tick, update
branch and split refusal codes, the merge card on the run and workflow pages,
the plugin rows, and the CI-fix hand-off. `merge_pr` merge-only workflows are
built (#790); the console's submit forms do not offer them yet. *Planned:* MS6,
retiring `auto-merge.yml` after ten clean step merges (#352).

**The repository registry, index, graph and test map**
([`docs/repo-index.md`](docs/repo-index.md), [`docs/git-tokens.md`](docs/git-tokens.md)).
A tenant registers a repository by `owner/repo`, read once from GitHub with its
own token (#589). A git token registry holds tenant, repository and user slots
(#588); a probe reads eight capabilities per token and repository, re-checked
daily, and records SSO enforcement, reached orgs and classic-token refusals
(#593, #620, #794). A per-commit index is built by the `indexer` runner profile
in `agent-runtime-indexer`: a tree-sitter extractor plus a headless LSP pass
(pyright, tsserver, gopls; terraform-ls was left out after its self-test found
no references), incremental from the promoted ancestor's index, polled by a
`repo_index_poll` tick (#596, #641, #720, #604). Graph shards are
content-addressed blobs under a manifest (#603), and
`POST /v1/repositories/{repo_id}/impact` selects the covering tests or the full
suite (#609). The console draws the repository list, detail, Graph and Impact
tabs, the test map grouped by source, Git tokens and the permission matrix.
*Planned:* the `selected-tests` merge gate (RI12) and the planner's use of the
index (RI5).

**The console redesign** (#503). Four sections — Overview; Work (Agents,
Workflows, Runs, Timeline, Repositories, and three Submit forms); Capacity
(Pools, Runtimes, By runner profile, Holders, Accounts, Provider quota); Admin
(Pool limits, Tenants, Platform counts). The agent inspector has a full-height
Logs tab, the picked Details layout, and an in-app diff viewer for
`swarm-work.patch` (#310). The 2026-10-07 full console QA recorded 147 findings
(IDs G1-G4); fixes for the Overview, Agents list, run page, workflows list and
detail, Timeline, repository pages and graph route have landed (#777-#793), and
the rest are tracked under #503.

**CI.** `application.yml` runs the Python unit suite and the vitest suite in four
shards each, its first job skips what a change cannot reach, and `ci-gate` needs
the jobs rather than polling for them (#803, #643).
`terraform.yml` runs fmt, validate, tflint, `terraform test` and checkov;
`security.yml` scans the filesystem, secrets, IaC, policy and images.
`main` is protected by strict up-to-date required checks; GitHub refuses a merge
queue on a user-owned repository (#756).

**Release.** `release.yml` deploys on push to `main`, environment-gated. A dev
plan that changes IAM is held, as the saved plan and its sha256, for the owner's
review in the `dev-iam` environment and applied unchanged; it never re-plans and
never runs on prod (#268). After verifying the new digests the release runs one
`--self-test` execution of each Cloud Run worker job, so the 30-59 s first-run
image import is not paid by a real task (#363). Release acceptance runs as the
smoke tenant against a private sandbox repository, never this public one (#628).

**Onboarding** (#780, [`docs/onboarding.md`](docs/onboarding.md)). Designed: a
SwarmCloud GitHub App with user access tokens, one resumable six-step state
machine read by both the console and the `sc` plugin, and a failed connection
parked at admission. Built so far: the token probe's SSO evidence and a refused
registration that names its likely cause (#794). *Planned* (#780): the
register picker that pages through every repository and accepts a typed
`owner/repo` (OB0), the onboarding state API (OB1), the GitHub App
infrastructure (OB2), and the console and plugin flows after them.

**Still true and not solved.** Firestore has no per-database IAM boundary (see
[Multi-tenancy](#multi-tenancy)). Cloud Run job start takes minutes, most of it
Cloud Run's own provisioning (see below).

## What we learned

Lessons that change how one runs or extends SwarmCloud.

* **Cloud Run job start is Cloud Run's provisioning, not ours.** On 2026-10-07,
  `ResourcesAvailable → Started` was 90-95% of the 2-3.5 minutes from
  `DISPATCHED` to `STARTING`; image size and our configuration were not the
  cause. Earlier data agrees: across 400 executions, shape, secrets and egress
  setting left the p50 unchanged
  ([incident](docs/incidents/2026-09-25-worker-startup-network.md) §5).
* **A warm GKE node starts the same image in about a second.** A GKE Autopilot
  probe on 2026-10-07 started the worker image in ~1 s on a warm node and 82 s
  on a fresh one — the case for warm capacity if start latency matters.
* **The clone was waiting on one TCP connect.** 96% of clone time was a single
  connect stalling on SYN backoff; probing the forge's egress path from process
  start and pinning git to the probe's peer cut the clone connect median from
  ~38 s to ~7 s (#721, #734, #758).
* **Strict up-to-date `main` means one merge per CI cycle**, so CI wall time is
  the throughput lever. On 2026-10-07, 82% of the time between merges was CI
  (24 merges in 5 h 47 m); sharding was the answer (#803,
  [`docs/ci.md`](docs/ci.md#the-merge-pipelines-wall-time)).
* **A merge pipeline needs one labeller, a head binding that tolerates base
  updates, and re-evaluation on every check.** A disable that silently failed
  let 17 of 24 pull requests merge in one day at a head nobody labelled (#795);
  a label added while checks ran was refused once and never re-read (#705).
* **A lane costs what its scope costs.** Cost is turns × context; 773 agents
  re-read 8.9 billion cached tokens in a day ([`CLAUDE.md`](CLAUDE.md#lanes-how-an-agent-works-here)).
  Keep a fix lane to about six findings.
* **Verify a claim against `main` before acting on it.** Several issues were
  already fixed when picked up, which is why an issue run that changes nothing
  ends `already_on_main` instead of failing (#646).

## Quick start

```bash
cp .env.example .env          # set PROJECT_ID, REGION
gcloud auth login && gcloud auth application-default login

make bootstrap                # TF state bucket, APIs
make infra                    # VPC, Firestore, IAM, Cloud Run, jobs
make build && make push       # images via Cloud Build (never local Docker)
make deploy
make smoke
make register-tenant GROUP=<group-email>
```

`make up` does the first six in one go. In normal operation `release.yml` does
the build, infrastructure and deploy on every push to `main`.

Submitting work:

```bash
./scripts/api.sh POST /tasks \
  '{"runner_profile":"mock","input":{"prompt":"hello swarm"},"timeout_seconds":300}'
```

Or install the `sc` plugin in Claude Code
(`/plugin marketplace add bogdan-alexandrescu/SwarmCloud`, then
`/plugin install sc@swarmcloud`, then `uv run sc login`) and use `/sc` — see
[`docs/plugin-setup.md`](docs/plugin-setup.md).

An ID token is the *only* input from which the API derives tenant identity, so
whoever holds one is that person's tenant for the next hour — their provider
keys, their GCS prefix, their budget. `scripts/api.sh` builds the header with a
shell builtin and hands it to `curl -K -` on stdin, so it never appears in argv.
The obvious one-liner, `curl -H "Authorization: Bearer $(gcloud auth
print-identity-token)"`, publishes the token to every process on the machine
through `/proc/<pid>/cmdline` and writes it into shell history — the same
objection that stops `create-secrets.sh` taking a key as an argument. See
[`docs/security.md`](docs/security.md#handling-an-id-token-on-the-operator-side),
which also covers why `API_AUDIENCE` is worth setting.

`make destroy` is **label-scoped**: it aborts if the plan would delete anything
lacking `managed-by=swarm-terraform`, because the target project is shared.

## Development

Tests, builds and deployments run in CI. A laptop here authors code and opens
pull requests; [`CLAUDE.md`](CLAUDE.md#before-you-say-you-are-finished) says why.

```bash
make test                     # offline: unit, terraform test, guard self-tests, contract parity
make lint                     # shellcheck, doc links, terraform fmt/validate, tflint, manifests
make status                   # one screen: jobs, agents, queues, leases, pools, quota
uv run python scripts/dev/drive.py state      # dump live control-plane state
uv run python scripts/dev/drive.py drain      # one scheduler pass, locally
uv run python scripts/dev/drive.py reconcile  # one reconciliation pass
```

`make test` needs no credentials and no emulator; CI runs it, sharded. A
SwarmCloud agent runs the unit tests for its area and then
`scripts/changed-guards.sh`, which runs the repo-wide guards and every test that
reads a changed path (#642).

`scripts/dev/drive.py` runs the real scheduler and reconciler in-process against
the real Firestore. Deploying to diagnose costs minutes per iteration; this
costs seconds, and it is how most of the integration bugs in this repository
were found.

Deployments use Workload Identity Federation and a manual environment approval —
no downloadable service-account keys exist.

## Documentation

How it works: [`architecture`](docs/architecture.md) ·
[`concurrency`](docs/concurrency.md) ·
[`quota management`](docs/quota-management.md) ·
[`checkpointing`](docs/checkpointing.md) ·
[`execution backends`](docs/execution-backends.md) ·
[`multi-tenancy`](docs/multi-tenancy.md) · [`security`](docs/security.md) ·
[`account holders`](docs/account-holders.md) · [`outcomes`](docs/outcomes.md)

Building on it: [`workflows`](docs/workflows.md) ·
[`issue runs`](docs/issue-runs.md) · [`merge step`](docs/merge-step.md) ·
[`repository index`](docs/repo-index.md) · [`git tokens`](docs/git-tokens.md) ·
[`onboarding`](docs/onboarding.md) (design) ·
[`plugin setup`](docs/plugin-setup.md) · [`console`](docs/web-ui/README.md)

Running it: [`ci`](docs/ci.md) · [`acceptance`](docs/acceptance.md) ·
[`operations`](docs/operations.md) · [`cost control`](docs/cost-control.md) ·
[`troubleshooting`](docs/troubleshooting.md) · [`testing`](docs/testing.md) ·
[`benchmarks`](docs/benchmarks.md) · [`runbooks`](docs/runbooks/) ·
[`incidents`](docs/incidents/)

[`CONTRACT.md`](CONTRACT.md) holds the invariants every component must respect;
changes to it are requested in
[`docs/contract-change-requests.md`](docs/contract-change-requests.md).

Bugs, fixes and improvements are filed through the
[issue forms](.github/ISSUE_TEMPLATE/); [`CLAUDE.md`](CLAUDE.md#issues) has the
rules for filing, batching and closing them.

## License

MIT — see [LICENSE](LICENSE).
