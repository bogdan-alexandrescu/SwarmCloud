# Worker images: what each carries, and what it costs every start

Three images run tasks. Each is built from the repository root by
`scripts/build-images.sh`, promoted by digest by `scripts/push-images.sh`, and
scanned (trivy, HIGH and CRITICAL) before promotion and weekly after it.

| image | built from | carries | runs |
|---|---|---|---|
| `agent-runtime-base` | `python:3.11-slim-bookworm` by digest | the worker, Node, the agent CLIs (Claude Code, codex), the agent toolbox (gh, gcloud, kubectl, terraform, checkov, shellcheck, make, the docker CLI) | every profile but `browser`: `mock`, `generic`, `claude-code`, `codex`, `merge`, `post-verdict`, `claude-code-review` |
| `agent-runtime-browser` | `agent-runtime-base` by digest | Playwright and Chromium | `browser` (GKE Autopilot) |
| `agent-runtime-indexer` | `agent-runtime-base` by digest | the repository index's toolchain: the tree-sitter extractor `swarm-repo-index`, the shard writer `swarm-repo-graph`, the Go toolchain, gopls, terraform-ls, pyright and typescript-language-server | **no profile yet** (contract request 48, below) |

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

| days | p50 | what had landed in agent-runtime-base |
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
| | terraform-ls 0.39.0 | 30.8 | 43 |
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

**The bisect, as far as the artifacts take it.** Two additions explain the two
steps:

* The toolbox (~202 MB) explains 71 s → ~115 s.
* The repo-index toolchain (~133 MB) explains ~115 s → ~167 s.

The second explanation has one unresolved hole. The repo-index commits landed
late on 10-04 Pacific, which is 10-05 UTC. If the analysis buckets days in UTC,
10-04's 166 s predates them and something else moved that day. If it buckets
in Pacific, 10-04 holds at most 75 minutes of starts with the toolchain.
`scripts/image-sizes.sh` over the released digests settles it, from any shell
that holds `roles/artifactregistry.reader`. Run it with no arguments to get the
size of every release, then run `--diff` on the release before the jump and the
release that brought it.

The toolbox stays in the base. Agents use those tools in ordinary claude-code
work (BUILD_PROMPT_V2 §2.12). The repo-index toolchain is used by one kind of
task, the index run, so it moved.

**Before and after.** agent-runtime-base loses the 132.8 MB of layers above
(409 MB unpacked); the toolchain's sources and pins are unchanged. The base's
absolute compressed size before and after is not stated here. It needs a
registry read, and the first release of this change is where to take it
(`scripts/image-sizes.sh --since 2026-10-05`). The start-time effect is
measured after release, against the table above.

## The gap until request 48 is accepted

Index runs are submitted as `claude-code` (`swarm_api.repoindex.INDEXER_PROFILE`),
and `claude-code` runs agent-runtime-base. Until a profile runs
agent-runtime-indexer, an index run finds no `swarm-repo-index`. It then does
what its prompt already says to do: it computes the mechanical fields with git
and the file tree, records `"extractor": {"ran": false, "reason": "not
installed in this image"}`, and writes no graph. An impact query on a
graph-less index treats every changed file as `unindexed` (§4.3a of
[repo-index.md](repo-index.md)). That is coarser, never wrong.

The image map lives only in the frozen catalogue (`RunnerProfile.image` in
`apps/common/swarm_common/profiles.py`, mirrored by `terraform/infra/locals.tf`),
so the profile that runs this image is a frozen-contract change: request 48 in
[contract-change-requests.md](contract-change-requests.md). Once it is accepted,
the remaining steps are:

1. Add the catalogue entry and its Terraform mirror. The image is already
   built and promoted.
2. Set `INDEXER_PROFILE = "repo-indexer"`.

## Adding to a worker image

Measure what you add with `scripts/image-sizes.sh --layers` after it ships, and
`--diff` it against the release before. A tool one kind of task needs belongs
in an image that task's profile runs, not in agent-runtime-base. If the tool
lands in the base, every agent start pays for it.
