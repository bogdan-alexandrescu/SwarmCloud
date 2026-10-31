# Worker images: what each carries, and what it costs every start

Four images run agents. Each is built from the repository root by
`scripts/build-images.sh`, promoted by digest by `scripts/push-images.sh`, and
scanned (trivy, HIGH and CRITICAL) before promotion and weekly after it.

| image | built from | carries | runs |
|---|---|---|---|
| `agent-runtime-base` | `python:3.11-slim-bookworm` by digest | the worker, Node, the agent CLIs (Claude Code, codex), the agent toolbox (gh, gcloud, kubectl, terraform, checkov, trivy, shellcheck, make, the docker CLI, the CodeRabbit CLI; tofu only with `INSTALL_TOFU=1`, tflint only with `INSTALL_TFLINT=1`) | every profile but `browser`, `claude-code-browser`, `indexer` and `claude-code-lightpanda`: `mock`, `generic`, `claude-code`, `claude-code-large`, `codex`, `merge`, `post-verdict`, `claude-code-review` |
| `agent-runtime-browser` | `agent-runtime-base` by digest | Playwright and Chromium | `browser` and `claude-code-browser` (contract request 67, accepted by the owner 2026-10-11), both on GKE Autopilot |
| `agent-runtime-indexer` | `agent-runtime-base` by digest | the repository index's toolchain: the tree-sitter extractor `swarm-repo-index`, the shard writer `swarm-repo-graph`, the Go toolchain, gopls (compiled from module source, #661), pyright and typescript-language-server | `indexer` (contract request 48, accepted by the owner 2026-10-05) |
| `agent-runtime-lightpanda` | `agent-runtime-base` by digest | the Lightpanda 1.0.2 headless browser, at `/opt/swarm/lightpanda/lightpanda`, and its licence notices; no Chromium | `claude-code-lightpanda` (contract request 127, accepted by the owner 2026-10-31), on GKE Autopilot ([below](#the-agent-runtime-lightpanda-image-contract-request-127)) |

The three derived images are built only after the base has finished in the same
run (`build_after()` in `scripts/build-images.sh`), from a `cloudbuild.yaml`
beside their Dockerfile that pulls `agent-runtime-base:<tag>` and passes its
digest as `BASE_IMAGE`. The indexer's and the Lightpanda image's Dockerfiles
have no default `BASE_IMAGE` and refuse one without a digest.

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
agent, the index run, so it moved.

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
| `claude-code-browser` | `GKE_AUTOPILOT` | none: it runs the browser image, for the same `/dev/shm` (contract request 67) |
| `claude-code-large` | `GKE_AUTOPILOT` | none: born on GKE (contract request 113, 2026-10-22), `claude-code` on the `large` class (8 CPU, 32 GiB) with a 14,400 s timeout, so it has no rollback Job |
| `claude-code-lightpanda` | `GKE_AUTOPILOT` | none: born on GKE (contract request 127, 2026-10-31), `claude-code` on the `agent-runtime-lightpanda` image and the `standard` class, so it has no rollback Job |
| `mock` | `CLOUD_RUN_JOB` | one per tenant (no provider) |
| `generic` | `GKE_AUTOPILOT` | none used: since contract request 64 (2026-10-10, #939 option A); each tenant's old Job (one per tenant, no provider) is kept idle as the rollback |
| `indexer` | `GKE_AUTOPILOT` | none used: since contract request 63 (2026-10-10, the canary for #939); each tenant's old Job (one per tenant that registers `anthropic`) is kept idle as the rollback |
| `merge` | `GKE_AUTOPILOT` | none used: since contract request 66 (2026-10-10, #939 option A); each tenant's old Job (one per tenant that registers `git`, contract request 47) is kept idle as the rollback. Until then every MERGE verdict paid a Cloud Run start (#748) |
| `codex` | `GKE_AUTOPILOT` | none used: since contract request 65 (2026-10-10, #939 option A); each tenant's old Job (one per tenant that registers `openai`) is kept idle as the rollback; the profile is disabled (`available=False`) |
| `post-verdict` | `CLOUD_RUN_JOB` | no Job (`profiles_without_a_job`); disabled |
| `claude-code-review` | `CLOUD_RUN_JOB` | no Job (`profiles_without_a_job`); retired |
| `await-input` | `CLOUD_RUN_JOB` | no Job (`profiles_without_a_job`): a `control_plane` profile that nothing runs on any backend, so the backend is the catalogue's statement only (contract requests 93 and 94, docs/design/human-steps.md); available since request D (contract request 105, HW-ENABLE) |
| `fix-loop` | `CLOUD_RUN_JOB` | no Job (`profiles_without_a_job`): the second `control_plane` profile, on the same terms as `await-input` (contract request 124, docs/design/fix-loop.md); shipped unavailable until FL-ACCEPT passes |

So `mock` is the one enabled profile still started on Cloud Run: it reaches no
internet, so #939's option A left it there, and `post-verdict` and
`claude-code-review` stay because each was designed to run as an account of its
own, which a GKE pod cannot be (contract request 66). `scripts/warm-jobs.sh`
warms every per-tenant worker Job it lists: mock's, and the idle rollback Jobs
of claude-code, indexer, generic, codex and merge until they are removed.

## The `indexer` profile (contract request 48)

Accepted by the owner 2026-10-05. `indexer` was `claude-code` in every field
but its name and its image: the same runner, resource class, backend,
timeouts, provider, secrets and inputs. Contract request 53 moved
`claude-code` to GKE Autopilot (2026-10-08); contract request 63 (owner,
2026-10-10) moved `indexer` after it, as the canary for #939: a new Cloud Run
instance's internet path opens a median 20.2 s after start against GKE's
1.17 s, through the same NAT, and every index run clones. Its tenants' Cloud
Run Jobs stay, idle, as the rollback (`cloud_run_fallback_profiles` in
`terraform/infra/locals.tf`). swarm-api submits index runs as
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

**terraform-ls is back in the image (2026-10-20, lane INDEX-LANGS, part of
#1693).** It was out from 2026-10-05, when its source build answered 0
references in the LSP self-test. Like gopls it is compiled from module source
with the golang.org/x modules trivy flagged raised (#661), and its module zip
is checked against a sha256 pinned in the Dockerfile; HashiCorp's prebuilt
zip is not used. The build's `--lsp-self-test` now requires an `lsp` edge for
HCL, as for Python, TypeScript, JavaScript and Go, so a terraform-ls that
resolves nothing fails the build instead of shipping.

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

## The `agent-runtime-lightpanda` image (contract request 127)

`images/agent-runtime-lightpanda/Dockerfile` is `agent-runtime-base` by
digest plus one layer, for the `claude-code-lightpanda` profile
([design](design/lightpanda-runtime.md) §5.D and §6.4; which profile to pick
is [browser-qa.md §2](browser-qa.md#2-which-one-for-which-job)).

**Why its own image.** Lightpanda is AGPL-3.0-only. Whoever ships the binary
conveys it, and so owes its notices and its source. In `agent-runtime-base`
or `agent-runtime-browser` that duty, and 76 MB, would ride along in every
agent of those profiles, most of which never start Lightpanda. In an image of
its own only the agents that asked for Lightpanda carry it, and an image no
profile named would be built but never dispatched.

**What it carries, and the pins.**

* `ARG LIGHTPANDA_VERSION=1.0.2`, `LIGHTPANDA_SHA256_AMD64`,
  `LIGHTPANDA_SHA256_ARM64`, `LIGHTPANDA_COMMIT` (the commit the tag points
  at) and `LIGHTPANDA_SOURCE_SHA256` (the tag's source archive), in full in
  the Dockerfile. The binary and the source archive are read from the build's
  tool mirror and `sha256sum -c`-checked; an architecture with no pin fails
  the build. `images/agent-runtime-base/tool-mirror.sha256` lists all three
  objects at the same sha256s, and `tests/unit/worker/test_lightpanda_image.py`
  and `tests/unit/scripts/test_build_mirror_switch.py` hold the two equal.
* The binary at `/opt/swarm/lightpanda/lightpanda`, root-owned and 0755, so
  the agent's uid cannot replace what the runner starts.
* `ENV LIGHTPANDA_DISABLE_TELEMETRY=true` and `LIGHTPANDA_DISABLE_CORE_DUMP=1`.
  Telemetry is on upstream by default and would report from inside a
  tenant's pod; a core dump of a page process could hold page data. An image
  `ENV` does not reach the agent's CLI (its environment is built from scratch),
  so the runner sets both again on every Lightpanda process it starts
  (`FIXED_ENV` in `apps/agent-worker/agent_worker/lightpanda.py`).
* No Chromium. The base has none and this image adds none, so a
  `claude-code-lightpanda` agent cannot start one by accident.
* A build-time self-test, `selftest.py` (bind-mounted, not shipped), run as
  the agent's uid in one RUN: the binary's owner and mode, `lightpanda
  version` equal to `LIGHTPANDA_VERSION`, the two switches, the notices below,
  no Chromium anywhere in the image, and the `claude` CLI on `PATH`.

**The licence duty, and how it is met.**

1. **Notices in the image** (AGPL §4), under `/usr/share/doc/lightpanda/`,
   each root-owned 0644: `LICENSE` and `LICENSING.md` taken from the tag's own
   source archive (nothing of Lightpanda's source is kept in this
   repository), `FONTS-LICENSE` for the fonts the binary embeds,
   `THIRD_PARTY` (beside the Dockerfile: the components statically linked
   into the binary, each with its SPDX licence) and `SOURCE`, which names the
   tag, the upstream commit, the Corresponding Source's URL and its sha256.
   The design listed `LICENSE`, `SOURCE` and `THIRD_PARTY`; the build adds
   `LICENSING.md` and `FONTS-LICENSE`.
2. **The Corresponding Source, offered by whoever conveys** (§6(d)). A link to
   GitHub is not enough, because the obligation stays with the distributor.
   `scripts/release/assemble.sh` stages the tag's source archive
   (`lightpanda_source_stage` in `scripts/lib/build-mirror.sh`, checked
   against the Dockerfile's sha256) and `scripts/release/publish.sh` puts it
   at the release bucket's root,
   `https://storage.googleapis.com/swarmcloud/third-party/lightpanda/1.0.2/source.tar.gz`,
   the URL `SOURCE` names. It stays there for as long as the image is offered.
3. **Unmodified**, so §13's network clause never applies: SwarmCloud runs
   the release binary as is.
4. **Not linked.** The runner starts it as a separate process and talks to it
   over CDP and MCP stdio, so §5's aggregate clause keeps the AGPL off
   SwarmCloud's own MIT code.

**Build, scan and promotion.** The image is in `ALL_TARGETS` of
`scripts/build-images.sh` and `scripts/push-images.sh`, built after the base
like the other derived images, trivy-scanned by `push-images.sh` before
promotion like every target, and in `RUNNER_IMAGES` of `scripts/warm-jobs.sh`.
`terraform/infra` pins its digest through the profile's `image` in
`locals.tf`. **The weekly scan does not cover it yet**: the `for image in`
list in `.github/workflows/security.yml` has no `agent-runtime-lightpanda`,
and that workflow line is held for the owner (design §6.7, lane L7).

**Updating it.** One PR changes the Dockerfile's ARGs and the three mirror
rows together (`scripts/lib/build-mirror.sh` refuses a disagreement between
them). Bump when SwarmCloud needs a fix, not on Lightpanda's one-to-three-week
cadence: iframes and the screenshot crash are the two to watch. Accept a bump
on the self-test, the probe tests and a rerun of the design's `bench.py`.

## Where an agent's caches go (CACHE-SCRATCH)

**Why.** On 2026-10-15 three claude-code agents were evicted twice each with
`Usage of EmptyDir volume "workspace" exceeds the limit "5Gi"` and were not
retried: task_41ae250f and task_c6a07695 (CI-fix rounds on PR 1338) and
task_44c61c77 (BROWSER-CLASS implement). The 5 GiB workspace volume held the
clone and the outputs, and also every cache the agent's tools fill, because the
agent's HOME (`work/`) and TMPDIR were both inside it: uv's cache, npm's cache,
pip's, terraform's providers, and every pytest/xdist temp tree. A disk
`emptyDir`'s `sizeLimit` is not a filesystem size -- the write succeeds and
kubelet evicts the whole pod on its next scan, with no checkpoint and no park.

**What the worker does now** (`apps/agent-worker/agent_worker/cachedirs.py`).
Before the runner starts it sets `UV_CACHE_DIR`, `npm_config_cache`,
`PIP_CACHE_DIR`, `TF_PLUGIN_CACHE_DIR`, `XDG_CACHE_HOME` and `TMPDIR` to
directories under one per-attempt cache directory, and the CLI and generic
runners carry them on to the agent by exact name. HOME stays `work/`: the CLI's
own state is the agent's and is checkpointed with it.

* **GKE.** The pod has a `cache` volume of its own, mounted at `/cache`. The
  dispatcher names it to the worker as `SWARM_CACHE_ROOT`; the attempt's
  caches are `/cache/<attempt>/`. A full cache is the cache volume's limit,
  never the workspace's. Since CACHE-PVC (owner decision 2026-10-15) it is a
  **20 GiB pd-balanced network disk**: a generic ephemeral volume on the
  `swarm-cache-pd-balanced` StorageClass
  (`kubernetes/storage/cache-storage-class.yaml`), ReadWriteOnce, created by
  the ephemeral-volume controller with the pod and deleted with it
  (`CACHE_STORAGE_CLASS`, `CACHE_VOLUME_GIB` in
  `apps/scheduler/scheduler/dispatch.py`). **Why:** CACHE-SCRATCH's first
  cache was a 2 GiB local emptyDir, and task_247c87a8714c4977921a was evicted
  on it twice (`EmptyDir volume "cache" exceeds 2Gi`); Autopilot caps a pod's
  ephemeral storage at 10 GiB and the split was already 10. A PVC is not
  ephemeral-storage, so a full cache is now ENOSPC in the tool that filled it,
  not an eviction, and its 2 local GiB went back to `/tmp` (3, 1 GiB above the
  checkpoint cap) and HOME. **The trade:** the disk's create-and-attach is on
  every GKE start's critical path, warm or cold
  ([browser-startup-speed.md](browser-startup-speed.md) §11.1, unmeasured as of
  the change), and a disk lives as long as its pod, which a finished Job keeps
  for an hour, so the tenant quota admits one claim per Job. `uv` cannot hard-link
  a venv from a cache on another volume, so it copies (`UV_LINK_MODE=copy`):
  the checkout's `.venv` is the size it was, and what leaves the workspace is
  everything else the caches hold -- npm's tarballs, downloads and builds,
  terraform's providers, and all of TMPDIR.
* **Cloud Run.** Every volume is a memory tmpfs and the provider cannot give a
  Job a second disk, so there is no cache volume and no `SWARM_CACHE_ROOT`.
  The cache directory is `<attempt>/cache/`, inside the workspace tmpfs, where
  it counts honestly against the same limit, and caches are kept small:
  `UV_NO_CACHE=1` and `PIP_NO_CACHE_DIR=1`. npm has no no-cache mode, so its
  cache stays, in that tmpfs, counted.

**Never checkpointed, never uploaded, never carried over.** The cache directory
is outside `work/` (a sibling on Cloud Run, another volume on GKE), so neither
the checkpoint's walk of `work/` nor the artifact upload's walk of `artifacts/`
reaches it. It is emptied when the attempt's workspace is created and removed
when it is destroyed. Checkpointing (invariant 8) covers `work/` exactly as
before; the `TOOL_CACHES` exclusions in `checkpoint.py` still apply to
anything a tool writes under HOME regardless.

## CodeRabbit

`agent-runtime-base` carries the CodeRabbit CLI (CODERABBIT-IMAGE, part of
#2442; owner decision 2026-10-26), so every image built on it has it too:
`claude-code`, `codex`, `claude-code-browser` and the rest. A lane whose
repository's rules ask for a local CodeRabbit review on a risky change runs
it itself, in its checkout, before it finishes:

```bash
coderabbit review --agent              # structured findings, one JSON object a line
coderabbit review --agent --base main  # against a named base
```

Nothing runs it for the lane. The platform does not start a review, read
its findings or gate a pull request on them: the CLI is a tool, like `gh`.

**What is installed.** The vendor's 0.9.1 linux/amd64 release, pinned and
sha256-checked in `images/agent-runtime-base/Dockerfile` (which records where
the checksum came from, the size, and the scan), root-owned at
`/opt/coderabbit/coderabbit`. `coderabbit` on PATH is a short launcher,
`images/agent-runtime-base/coderabbit`, and it exists because the CLI takes
its API key **only** as `--api-key` on `review` (its `--help`: "there is no
API-key environment variable"). `coderabbit auth login` would store the key
under HOME, which in an attempt is the checkpointed work directory, so the
launcher never logs in: it reads the key on every call and hands it to that
one process. It also switches off the CLI's self-update (the pinned binary
cannot be replaced, and should not be) and its telemetry.

**The key is a workspace secret, never in the image or the repository.** A
workspace admin sets it, in the console, under Settings › Workspace ›
**Secrets** (docs/design/workspace-secrets.md §5):

1. **Add** a secret named exactly `coderabbit-api-key`, kind **API key**,
   the value pasted into the form (an Agentic API key from CodeRabbit's
   settings). The value goes into the form only: no chat, prompt, `/sc`
   command or MCP tool takes it, though `/sc` can mint a one-time link to
   the form.
2. **Grant** it to each runner profile that should be able to review, in the
   profile's `files` slot: `profile:claude-code`, and `profile:codex` or
   `profile:claude-code-browser` if their lanes review too. It takes effect
   at the next attempt.

`scripts/create-secrets.sh --stdin` is **not** the route for this key: it
stores a tenant's provider key or one of the platform's own secrets, has no
CodeRabbit option, and nothing releases a platform secret to a lane. The
workspace secret is the only way the key reaches an agent.

**How it reaches the CLI.** Before the agent starts, the worker asks swarm-api
for the secrets granted to this attempt's profile (`agent_worker.secretrelease`)
and writes each to a 0600 file under its own `private/` directory, never
checkpointed and never uploaded, listed by name in the manifest at
`$SWARM_SECRETS_MANIFEST`. Every value is registered with the worker's
redaction the moment it arrives, before any file is written; the same set
scrubs every log line and every artifact the worker uploads, and the
publish-time scan refuses a diff that contains it. When the agent runs
`coderabbit review`, the launcher finds the manifest entry named
`coderabbit-api-key` in slot `files`, reads that one file and starts the CLI
with `--api-key`. The CLI's environment never holds the key, nor the manifest's
path. The key is in that process's argv while it runs, which only this pod's
processes can read (the agent's own uid, which can read the file anyway).
`private/secrets/` is deleted when the attempt ends.

**Without the key** -- no secret of that name, or not granted to this profile
-- the manifest has no such entry. Nothing fails at start: `coderabbit review`
runs keyless, the launcher prints one line naming the secret and slot it looked
for, and the CLI reports that it is not signed in. A lane should say so in its
report rather than treat a review it could not run as clean.

**Why the `files` slot and not a `coderabbit` consumer.** The consumer
catalogue (profiles, `RUNTIME_TOOLS`, components) is the frozen contract
(`swarm_common.profiles`, requests 80-82), and a runtime tool is matched
against a profile's signed `browser_tool` input, which `claude-code` and
`codex` do not have. The `files` slot every agent profile already declares
carries the key with the same release, redaction and removal, so no contract
change was needed. A dedicated consumer, if the owner wants one, is a
contract request (the CODERABBIT-IMAGE PR names it).

## Adding to a worker image

Measure what you add with `scripts/image-sizes.sh --layers` after it ships, and
`--diff` it against the release before. A tool one kind of agent needs belongs
in an image that agent's profile runs, not in agent-runtime-base. If the tool
lands in the base, every agent start pays for it.
