# Worker images: what each carries, and what it costs every start

Three images run tasks. Each is built from the repository root by
`scripts/build-images.sh`, promoted by digest by `scripts/push-images.sh`, and
scanned (trivy, HIGH and CRITICAL) before promotion and weekly after it.

| image | built from | carries | runs |
|---|---|---|---|
| `agent-runtime-base` | `python:3.11-slim-bookworm` by digest | the worker, Node, the agent CLIs (Claude Code, codex), the agent toolbox (gh, gcloud, kubectl, terraform, checkov, trivy, shellcheck, make, the docker CLI; tofu only with `INSTALL_TOFU=1`, tflint only with `INSTALL_TFLINT=1`) | every profile but `browser` and `indexer`: `mock`, `generic`, `claude-code`, `codex`, `merge`, `post-verdict`, `claude-code-review` |
| `agent-runtime-browser` | `agent-runtime-base` by digest | Playwright and Chromium | `browser` (GKE Autopilot) |
| `agent-runtime-indexer` | `agent-runtime-base` by digest | the repository index's toolchain: the tree-sitter extractor `swarm-repo-index`, the shard writer `swarm-repo-graph`, the Go toolchain, gopls (compiled from module source, #661), pyright and typescript-language-server | `indexer` (contract request 48, accepted by the owner 2026-10-05) |

The two derived images are built only after the base has finished in the same
run (`build_after()` in `scripts/build-images.sh`), from a `cloudbuild.yaml`
beside their Dockerfile that pulls `agent-runtime-base:<tag>` and passes its
digest as `BASE_IMAGE`. The indexer's Dockerfile has no default `BASE_IMAGE`
and refuses one without a digest.

## Why the repository index has its own image (#625)

**What the image's size costs a start, and what it does not.** The move was
argued, on 2026-10-05, from image weight: a Cloud Run Job execution pulls its
image before the worker's first line runs, and claude-code was then the profile
almost every step ran, on Cloud Run. Two measurements since have narrowed that.
The registry bisect below found the 10-04/05 step came with no change in the
image's size, and the measurement of 2026-10-07
([below](#where-dispatched---starting-goes-measured-2026-10-07)) found that
90-95 % of a Cloud Run start is Cloud Run's own provisioning, and that size
shows only in the first execution after a new digest. The dispatched-to-starting
p50 for claude-code on Cloud Run, from the 2026-10-05 history analysis, was:

| days | p50 | what had landed on main (not what was deployed, and not the cause: see the bisect and the 2026-10-07 measurement below) |
|---|---|---|
| 09-24 | 71 s | — |
| 09-30 .. 10-02 | ~115 s | the agent toolbox (9e2ba44, 2026-10-01 03:44 PDT); the bisect confirms the deployed base grew 521 → 726 MB |
| 10-04, 10-05 | 166 s, 168 s | the repo-index toolchain landed on main (c716822, 2a9aa39, 539b5b5, 10-04 22:44 .. 10-05 01:42 PDT), but the bisect found no change in the deployed image's size: the step is Cloud Run's `ResourcesAvailable -> Started`, which moved the 27.6 MB swarm-verify job the same way |

GKE Autopilot pods start in 17 s. Each three-step lane paid three starts:
about 8 minutes, or 18% of a lane's wall time. claude-code no longer starts on
Cloud Run ([where each profile starts today](#where-each-profile-starts-today)).

**What each addition weighs.** The registry could not be read from the lane that
did this work. The SwarmCloud worker account has no
`artifactregistry.versions.list`: measured 2026-10-05, `PERMISSION_DENIED`. So
each addition was measured from the exact pinned artifacts its Dockerfile
installs: downloaded, sha256-checked against the Dockerfile's pins, unpacked
as the Dockerfile unpacks them, and gzipped (`gzip -6`, the compression a
layer is pushed with). These are layer sizes, not the registry's figure for
the image:

| addition | layer | gzip MB | unpacked MB |
|---|---|---|---|
| repo-index toolchain | Go 1.27.1, `/usr/local/go` without `test/` | 69.5 | 256 |
| | terraform-ls 0.39.0 (since removed, see below) | 30.8 | 43 |
| | gopls 0.23.0, built as the Dockerfile builds it | 22.1 | 43 |
| | pyright, typescript, typescript-language-server (`npm ci` of the lockfile) | 9.1 | 60 |
| | tree-sitter and its five grammars | 1.3 | 7 |
| | **total** | **132.8** | **409** |
| agent toolbox (10-01) | gcloud 587.0.0 without its bundled Python | 53.6 | 368 |
| | checkov 3.3.17 environment | 51.9 | 183 |
| | terraform 1.16.4 | 35.3 | |
| | docker CLI 29.8.2 | 20.8 | 46 |
| | kubectl 1.36.5 | 17.8 | |
| | gh 2.102.0 | 15.3 | |
| | gke-gcloud-auth-plugin | 4.0 | 10 |
| | shellcheck 0.11.0 | 3.8 | |
| | **total** | **~202** | |
| trivy (2026-10-06, #442) | trivy 0.75.0 | 51.0 | 165 |

**The registry bisect (the operator, 2026-10-05 ~11:05Z, #625).** Run with
`scripts/image-sizes.sh` over 101 releases of agent-runtime-base:

| when | compressed | what |
|---|---|---|
| until 2026-10-01 10:57Z | 521 MB | — |
| 2026-10-01 10:57Z .. 2026-10-05 06:00Z | 726-728 MB | the agent toolbox, +205 MB |
| 518642c2's build, 2026-10-05 09:38Z | 916 MB | the repo-index toolchain, +188 MB; never deployed (the release scan refused it) |

So the toolbox explains 71 s → ~115 s, but **the ~115 s → ~167 s step of
10-04/05 happened with no change in the image's size**: the toolchain never
reached a deployed base. The estimate above that tied that step to the
toolchain is not supported; the step is investigated in #667, and the
measurement of 2026-10-07 below found it in Cloud Run's provisioning. The move
still stands, because it keeps +188 MB out of every start that runs the base.

The toolbox stays in the base. Agents use those tools in ordinary claude-code
work (BUILD_PROMPT_V2 §2.12). The repo-index toolchain is used by one kind of
task, the index run, so it moved.

**Before and after.** agent-runtime-base goes back to the toolbox-only
726-728 MB the bisect measured, instead of the 916 MB the toolchain made it;
the toolchain's sources and pins moved unchanged, less terraform-ls (below).
Confirm it on the first release of this change with
`scripts/image-sizes.sh --since 2026-10-05`. Every promote now also writes
each runner image's compressed size and its delta from the previous release
to the release job's summary (`image-sizes.sh --manifest`, run by
`.github/actions/release-promote`; [ci.md](ci.md#images-are-built-once-per-commit-and-the-release-reuses-them)),
so a later growth shows on the release that brought it.

### Where DISPATCHED -> STARTING goes (measured 2026-10-07)

Measured read-only by the operator on 2026-10-07 (#625's second comment, and
#667): 874 claude-code attempts from 09-30 to 10-07, 497 of them with Cloud
Run execution conditions, against controls of 811 `mock`, 446 swarm-verify and
313 `browser` attempts on GKE Autopilot.

* **90-95 % of DISPATCHED -> STARTING is Cloud Run's own `ResourcesAvailable
  -> Started`**, its instance provisioning, on every day and every job. For
  claude-code over all days, `ResourcesAvailable -> Started` was p50 138 s /
  p90 227 s, against DISPATCHED -> STARTING p50 128 s / p90 212 s. (The two
  populations differ: the first is the 497 executions whose conditions were
  readable.)
* **Every segment of ours is small and flat**, a few seconds at most: dispatch
  call -> execution created -0.2 s, created -> `ResourcesAvailable` 1-2 s,
  `Started` -> the worker's first log 3 s, first log -> STARTING 3 s, STARTING
  -> first heartbeat 0.3 s.
* **Nothing of ours changed on 10-04.** Image size, 4 CPU / 8 GiB, region,
  gen2 and Direct VPC egress were identical on every execution read, and no
  commit to `terraform/`, `images/`, the worker or the scheduler landed between
  10-02 20:37Z and 10-04 05:14Z.
* **The control moved the same way.** swarm-verify (27.6 MB, 1 CPU) went
  through daily p50 76 -> 153 -> 97 -> 191 s, so neither the image's size nor
  its shape is the cause.
* **The slow periods recovered and returned with no release.** The hourly
  create -> `Started` median was 160-227 s from 10-04 ~01h to 10-05 ~12h,
  about 100 s from 10-05 ~14h, and about 180 s again from 10-06 23h. Swings
  of this size also appear on 09-20..22. Executions created 10-60 s apart often
  reach `Started` in the same second; concurrency adds up to ~60 s in bursts.
* **Image size shows in one place only:** the first execution of each job
  after a new digest imports the image in 30-59 s, about 3 % of starts. Every
  later start pays 1-3 s. `scripts/warm-jobs.sh`, which `release.yml` runs
  after the deploy has verified the new digests, pays that import with one
  no-op execution per Cloud Run worker Job (owner decision (2), 2026-10-07).

Confidence: high that the time is in Cloud Run provisioning and that nothing of
ours changed; medium that the cause is Google-side, because that is reached by
elimination and Direct VPC egress cannot be excluded: every control uses it
too. The owner's decision (3) of 2026-10-07 is a support case, drafted for the
owner to file in
[`docs/runbooks/cloud-run-start-support-case.md`](runbooks/cloud-run-start-support-case.md).

This is the start, not the clone: the time to connect and clone after STARTING
(#721) is not part of DISPATCHED -> STARTING.

**What returns claude-code toward 70 s is the backend, not the image.**
claude-code runs on GKE Autopilot since contract request 53, applied
2026-10-08
([`docs/contract-change-requests.md`, request 53](contract-change-requests.md)):
its canary, request 55, ran five real steps at DISPATCHED -> RUNNING p50
~23 s, max 44 s, and `browser` on GKE reaches STARTING in p50 15 s / p90
83 s. The tenants' claude-code Cloud Run Jobs stay, idle, until 2026-10-15 as
the rollback (`cloud_run_fallback_profiles` in `terraform/infra/locals.tf`).
The p50 of claude-code on GKE across ordinary work is to be confirmed with
`scripts/bench-coldstart.sh --profile claude-code`. The profiles that still
start on Cloud Run keep the provisioning time above, and the warm run covers
only their per-digest import.

### Where each profile starts today

Read from `apps/common/swarm_common/profiles.py` (`resolve_backend`) and, for
which Cloud Run profiles have a Job, `job_matrix` and `profiles_without_a_job`
in `terraform/infra/locals.tf`. `tests/unit/scripts/test_cloud_run_start_doc.py`
fails when a profile's backend moves and this table does not.

| profile | backend | Cloud Run Job |
|---|---|---|
| `claude-code` | `GKE_AUTOPILOT` | none used: since contract request 53 (applied 2026-10-08); each tenant's old Job is kept idle until 2026-10-15 as the rollback |
| `browser` | `GKE_AUTOPILOT` | none: Chromium needs the `/dev/shm` GKE gives it |
| `mock` | `CLOUD_RUN_JOB` | one per tenant (no provider) |
| `generic` | `CLOUD_RUN_JOB` | one per tenant (no provider) |
| `merge` | `CLOUD_RUN_JOB` | one per tenant that registers `git` (contract request 47); every MERGE verdict pays a Cloud Run start (#748) |
| `indexer` | `CLOUD_RUN_JOB` | one per tenant that registers `anthropic` (contract request 48) |
| `codex` | `CLOUD_RUN_JOB` | one per tenant that registers `openai`; the profile is disabled (`available=False`) |
| `post-verdict` | `CLOUD_RUN_JOB` | no Job (`profiles_without_a_job`); disabled |
| `claude-code-review` | `CLOUD_RUN_JOB` | no Job (`profiles_without_a_job`); retired |

So `mock`, `generic`, `merge`, `indexer` and `codex` are the profiles still
started on Cloud Run. `scripts/warm-jobs.sh` warms every per-tenant worker Job
it lists: theirs, and the idle claude-code Jobs until they are removed.

## The `indexer` profile (contract request 48)

Accepted by the owner 2026-10-05. `indexer` was `claude-code` in every field
but its name and its image: the same runner, resource class, backend,
timeouts, provider, secrets and inputs. Since contract request 53 moved
`claude-code` to GKE Autopilot (2026-10-08) the backend differs too: `indexer`
stays on `CLOUD_RUN_JOB`. swarm-api submits index runs as
`indexer` (`swarm_api.repoindex.INDEXER_PROFILE`), so an index run finds
`swarm-repo-index`, runs the LSP pass and writes the graph. Terraform mirrors
it (`terraform/infra/locals.tf`), which puts `agent-runtime-indexer` in
`runner_images`: its digest is pinned in `image_refs`, handed to the scheduler
in `WORKER_IMAGE_REFS`, and every tenant that registers `anthropic` gets an
`indexer` Job running as its own worker account.

**It is offered to every caller.** The catalogue has no internal-only
mechanism: `available` is the one flag, and `available=False` would refuse
swarm-api's own index runs too. A caller who submits `indexer` gets a
claude-code agent on a bigger image, by name, with no image or command of
their own (invariant 10). Making it platform-only needs a catalogue field, a
contract change of its own.

**terraform-ls is not in the image.** Its source build answered 0 references
in the LSP self-test (owner decision 2026-10-05), so it stays a
`DISABLED_SERVERS` spec in `lsp/servers.py` until it resolves. gopls is
compiled from module source with the golang.org/x modules trivy flagged
raised (#661); HashiCorp's prebuilt zip is not used.

**trivy joined the base on 2026-10-06 (#442).** BUILD_PROMPT_V2 §2.12 and the
owner's decision on #442 put all eleven toolbox tools in agent-runtime-base;
three were held back on 2026-10-01 because every release of each failed the
promote scan. trivy 0.75.0 scanned clean, so it is in the default build, and
it costs every agent start its 51 MB layer (measured as above: the pinned
tarball, sha256-checked, unpacked, `gzip -6`). tofu and tflint stay held, each
behind its own build argument, `INSTALL_TOFU` and `INSTALL_TFLINT`, so whichever
ships a clean release first can join the default build alone, as trivy did.
Turning both on adds about 52 MB (tofu 1.13.1 34.7, tflint 0.64.0 16.8). Why each still fails the scan (last re-scanned 2026-10-09) is recorded beside
its pin in the Dockerfile.

## Adding to a worker image

Measure what you add with `scripts/image-sizes.sh --layers` after it ships, and
`--diff` it against the release before. A tool one kind of task needs belongs
in an image that task's profile runs, not in agent-runtime-base. If the tool
lands in the base, every agent start pays for it.
