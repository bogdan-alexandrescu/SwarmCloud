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

**Every byte in `agent-runtime-base` is paid on every agent start.** A Cloud
Run Job execution pulls its image before the worker's first line runs, and
claude-code is the profile almost every step of every workflow runs. The
dispatched-to-starting p50 for claude-code on Cloud Run, from the 2026-10-05
history analysis, was:

| days | p50 | what had landed on main (not what was deployed: see the bisect below) |
|---|---|---|
| 09-24 | 71 s | — |
| 09-30 .. 10-02 | ~115 s | the agent toolbox (9e2ba44, 2026-10-01 03:44 PDT) |
| 10-04, 10-05 | 166 s, 168 s | the repo-index toolchain: the extractor (c716822, 10-04 22:44 PDT), the shard writer (2a9aa39, 10-04 23:28), the LSP servers (539b5b5, 10-05 01:42) |

GKE Autopilot pods start in 17 s. Each three-step lane pays three starts:
about 8 minutes, or 18% of a lane's wall time.

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
toolchain is not supported; the step is investigated in #667. The move still
stands, because it keeps +188 MB out of every agent start.

The toolbox stays in the base. Agents use those tools in ordinary claude-code
work (BUILD_PROMPT_V2 §2.12). The repo-index toolchain is used by one kind of
task, the index run, so it moved.

**Before and after.** agent-runtime-base goes back to the toolbox-only
726-728 MB the bisect measured, instead of the 916 MB the toolchain made it;
the toolchain's sources and pins moved unchanged, less terraform-ls (below).
Confirm it on the first release of this change with
`scripts/image-sizes.sh --since 2026-10-05`.

## The `indexer` profile (contract request 48)

Accepted by the owner 2026-10-05. `indexer` is `claude-code` in every field
but its name and its image: the same runner, resource class, backend,
timeouts, provider, secrets and inputs. swarm-api submits index runs as
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
