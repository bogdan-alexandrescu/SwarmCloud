#!/usr/bin/env bash
# Build container images with Cloud Build. Never with the local Docker daemon.
#
# Two independent reasons, both fatal to the local path:
#   * this workstation is arm64 and every target (Cloud Run Jobs, GKE Autopilot)
#     is amd64, so a locally built image either fails to start or silently runs
#     under emulation;
#   * the local Docker daemon on the reference machine is broken.
#
# Images are tagged with the immutable git SHA only. Promotion to a channel tag
# (:dev, :prod) is push-images.sh's job, so that "what is deployed" is always a
# digest that was actually built, and never a tag someone moved by hand.
#
# Builds are submitted CONCURRENTLY, at most --parallel at a time (default 4).
# Every image is attempted even when another fails, and the run ends by naming
# every image that failed. Each build's output is kept in
# build/build-logs/<tag>/<image>.log and printed with the image's name on every
# line, so interleaved builds stay attributable.
#
# Every submit is `--async`, and ONE loop reads the status of every build in
# flight, every BUILD_STATUS_INTERVAL seconds (default 15). A 429
# RESOURCE_EXHAUSTED from Cloud Build -- on a submit or a status read -- is
# waited out with exponential backoff, never reported as a failed image
# (CB-429-RETRY, read_build_states below).
#
# ONE BUILD PER COMMIT, and this script is the one path to it. application.yml
# runs it on every push to main and uploads the manifest it writes; release.yml
# runs it with --reuse-ci, which fetches THAT manifest instead of building
# (scripts/lib/ci-built-images.sh, and docs/ci.md for the decision):
#
#   --reuse-ci only       use what CI built for this commit, waiting for it if
#                         it is still building; fail if CI never built it.
#                         A digest CI's record reused from an earlier commit
#                         whose image inputs this commit has changed is built
#                         here (both modes): what is promoted is compared with
#                         the commit that built each digest, never HEAD~1
#   --reuse-ci or-build   the same, but build here when CI never built this
#                         commit for this environment -- a release dispatched
#                         by hand, or any prod release (swarm-ui bakes its
#                         environment in, and CI builds for dev)
#
# Either way the result is build/images-<env>.json, so what follows cannot tell
# a reused build from a fresh one -- and a fresh one is this same script, with
# the same tag, not a second copy of it.
#
# A PULL REQUEST BUILDS WITHOUT PUSHING (#650, owner decision 2026-10-05).
# Main's build is the one above; a pull request that changes an image's inputs
# runs two more modes of this same script, so the build it checks is the build
# main will make:
#
#   --affected-by FILE    print the images a list of changed paths (FILE, or -
#                         for stdin) reaches, one per line. Offline: no gcloud.
#                         An image's inputs are its recipe's directory, every
#                         COPY/ADD source its Dockerfile names (read by
#                         dockerfile_copy_sources, lib/common.sh -- never
#                         listed by hand), and the two files that filter every
#                         build's context (.dockerignore, .gcloudignore). An
#                         image built FROM another (build_after) is reached
#                         through it.
#   --inputs              print those inputs as `<image><TAB><pattern>` rows.
#   --build-only          build in Cloud Build and push NOTHING: no `images:` in
#                         any config, no registry tag, no manifest, no
#                         Artifact Registry call. The Dockerfiles' RUN steps --
#                         `swarm-repo-index --self-test`, `--lsp-self-test` and
#                         the rest -- run exactly as on main.
#   --build-only --local  the same build on THIS machine's docker, with
#                         `docker buildx build`, and no gcloud at all. This is
#                         what a pull request runs (application.yml
#                         `build-check`): the owner chose not to give pull
#                         requests a Google identity, because code on any
#                         branch could then run Cloud Build in the shared
#                         project. The two reasons above against a local build
#                         are this workstation's; the GitHub runner is amd64
#                         and its daemon works. Images built FROM another are
#                         built after it, FROM the digest this run exported to
#                         a local OCI layout -- never pulled -- and every build
#                         is uncached, so every RUN self-test runs. Nothing is
#                         pushed, loaded, tagged in a registry or logged in to.
#
# BUILDS READ THE PROJECT'S MIRRORS, NOT GHCR.IO, DOCKER HUB OR GITHUB
# (OPS-MIRROR, lib/build-mirror.sh, docs/runbooks/build-mirror.md). Every
# FROM, and the `# syntax=` frontend, is pulled through a remote Artifact
# Registry repository of the project being built for, which this script
# passes (mirror_build_args_yaml; no Dockerfile names a project); agent-runtime-base's
# release files come from gs://${BUILD_MIRROR_BUCKET}, copied in by a
# `tool-mirror` step this script puts first in that image's Cloud Build, and
# a missing object fails that step by name. `--local` reads upstream instead,
# against the same sha256 list, because a pull request has no identity to
# read either mirror with.
#
# MAIN REBUILDS ONLY WHAT A COMMIT CHANGED (owner decision 2026-10-08,
# observer proposal H; docs/ci.md "Main builds only the images a commit
# changed"). Measured that day: every push to main rebuilt all 9 images in
# 9.0-10.4 min, and a uv.lock change alone reached 7 of 9. application.yml
# now runs
#
#   --incremental PREV    PREV is the newest record CI made of an ancestor
#                         commit (scripts/lib/ci-built-images.sh --previous),
#                         or `none`. An image is rebuilt iff --affected-by's
#                         closure reaches it from `git diff <the commit its
#                         previous digest was built from> HEAD`; every other
#                         image keeps its previous digest, which gains this
#                         commit's tag (`gcloud artifacts docker tags add`).
#                         So every digest in the manifest carries this tag,
#                         and push-images.sh, image-refs.sh and deploy.sh
#                         cannot tell a reused image from a rebuilt one. Each
#                         manifest entry says which it is: `reused`,
#                         `built_from` (the commit whose build made the digest)
#                         and `built_at`, and `build_args`, the values it
#                         bakes in; an image whose baked values differ from
#                         its previous digest's is rebuilt whatever the diff.
#                         Its `/version` and OCI revision label name
#                         `built_from`, not this commit (docs/ci.md).
#   --full-build REASON   rebuild every image anyway, recording REASON.
#
#   A uv.lock change rebuilds a Python image only if that image's own pruned
#   `uv export` line (read from its Dockerfile) exports something different at
#   the two commits. Everything is rebuilt -- the build before this change --
#   when: build logic changed since a reused digest was built (this script,
#   lib/common.sh, lib/ci-built-images.sh, application.yml); there is no
#   ancestor record, or it cannot be trusted; a reused digest is older than
#   BUILD_REUSE_MAX_AGE_DAYS (default 7), so base-image patches arrive; the
#   working tree is dirty; --full-build; or the environment is prod. An image
#   whose Dockerfile upgrades OS packages is also rebuilt, on its own, by every
#   build and every release (APT_REFRESH, below).
#
# Usage: scripts/build-images.sh [TARGET...] [--tag SHA] [--parallel N]
#                                [--create-repo] [--async] [--digests-only]
#        scripts/build-images.sh --incremental PREV|none [--full-build REASON]
#        scripts/build-images.sh --reuse-ci only|or-build
#        scripts/build-images.sh --build-only [TARGET...] [--tag SHA] [--parallel N]
#        scripts/build-images.sh --build-only --local [TARGET...] [--tag SHA]
#        scripts/build-images.sh --affected-by FILE|-
#        scripts/build-images.sh --inputs
#        TARGET defaults to every image the repo knows how to build.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/build-mirror.sh
source "${REPO_ROOT}/scripts/lib/build-mirror.sh"

# swarm-egress (EGRESS-BUILD, owner decision 2026-10-14) is the browser egress
# forwarder, apps/swarm-egress/Dockerfile: beside its app as swarm-live's
# recipe is, so find_recipe() finds it and image_inputs() reads its COPY
# lines (apps/common, apps/redaction, apps/swarm-egress) like any other.
# terraform/infra/egress.tf renders swarm_egress_manifest only once
# image_refs has its digest, and image_refs is written from what this builds
# and push-images.sh promotes: an image missing here is a forwarder dev's
# enable_browser_egress could never deploy. It upgrades OS packages, so it is
# rebuilt by every build (upgrades_os_packages).
#
# swarm-typesafe-fwd (SEC-INJECT-B, part of #1347) is the TypeSafe forwarder,
# apps/swarm-typesafe-fwd/Dockerfile, beside swarm-egress for the same
# reasons: terraform/infra/typesafe_fwd.tf renders swarm_typesafe_fwd_manifest
# only once image_refs has its digest (docs/design/workspace-secrets.md §3.5).
#
# swarm-graph (GS-INFRA, part of #1384) is the shared graph service,
# apps/swarm-graph/Dockerfile (lane GS-SVC), beside the forwarder for the
# same reasons: terraform/infra/graph_service.tf renders swarm_graph_manifest
# only once image_refs has its digest (docs/design/graph-service.md §2.1). It
# copies apps/agent-worker in, so a worker change rebuilds it too.
#
# swarm-github-hooks (GS-HOOKS, part of #1384) is the public GitHub push
# receiver, apps/swarm-github-hooks/Dockerfile, beside swarm-graph for the same
# reasons: terraform/infra/github_hooks.tf deploys the Cloud Run service only
# once image_refs has its digest (docs/design/graph-service.md §6.2). It
# copies apps/common and apps/redaction in, and nothing of the worker.
ALL_TARGETS=(agent-runtime-base agent-runtime-base-nocodex agent-runtime-browser agent-runtime-indexer swarm-api swarm-scheduler swarm-quota-broker swarm-reconciler swarm-ui swarm-verify swarm-live swarm-egress swarm-typesafe-fwd swarm-graph swarm-github-hooks workspace-apply)

# The target whose recipe an image is built from. Every image is its own,
# except agent-runtime-base-nocodex: agent-runtime-base's Dockerfile built
# again with INSTALL_CODEX=0 (target_build_args), the base agent-runtime-browser
# is built FROM (BROWSER-NO-CODEX, owner decision 2026-10-13,
# docs/browser-startup-speed.md §2.3). codex is 129.7 MB in the base's npm
# layer, and a derived image cannot drop an inherited layer, so the browser
# needs a base built without it. Its own image name, not a second tag of
# agent-runtime-base, so the build record, the promotion, the size and
# duplicate-layer gates (scripts/image-sizes.sh) and terraform's image_refs --
# all keyed by image name -- cover it as the image it is. Its recipe directory
# is the base's, so it reads the base's inputs and is rebuilt with it.
recipe_target() {
  case "$1" in
    agent-runtime-base-nocodex) printf '%s' "agent-runtime-base" ;;
    *) printf '%s' "$1" ;;
  esac
}

# Find the build recipe for a target. Track B owns images/ and the service
# Dockerfiles, so both layouts are accepted rather than assumed.
find_recipe() {
  local target candidate
  target="$(recipe_target "$1")"
  for candidate in \
    "images/${target}/cloudbuild.yaml" \
    "apps/${target}/cloudbuild.yaml"; do
    if [[ -f "${REPO_ROOT}/${candidate}" ]]; then printf 'config\t%s' "${candidate}"; return 0; fi
  done
  for candidate in \
    "images/${target}/Dockerfile" \
    "apps/${target}/Dockerfile" \
    "docker/${target}/Dockerfile"; do
    if [[ -f "${REPO_ROOT}/${candidate}" ]]; then printf 'dockerfile\t%s' "${candidate}"; return 0; fi
  done
  return 1
}

# The images that must wait for another to FINISH building before they may be
# SUBMITTED.
#
# images/agent-runtime-browser/cloudbuild.yaml pulls
# agent-runtime-base-nocodex:<tag> in its first step and builds FROM that
# digest, so submitting it before that base has been pushed fails with
# manifest-unknown. images/agent-runtime-indexer/cloudbuild.yaml does the same
# with agent-runtime-base:<tag> (#625). When builds ran one at a
# time this was satisfied by ALL_TARGETS happening to list the base first --
# which a targeted `build-images.sh agent-runtime-browser agent-runtime-base`
# did not, and which concurrency would not either.
#
# Stated here, once. tests/unit/scripts/test_build_images_concurrency.py reads
# every recipe for images it pulls and fails if this function disagrees, so a
# new `FROM <another target>` cannot be added without it.
build_after() {
  case "$1" in
    agent-runtime-browser) printf '%s' "agent-runtime-base-nocodex" ;;
    agent-runtime-indexer) printf '%s' "agent-runtime-base" ;;
    *) : ;;
  esac
}

# ---------------------------------------------------------------------------
# What each image is built from, and which images a change reaches (#650).
# ---------------------------------------------------------------------------
# Every function here is offline: --affected-by and --inputs run in the
# pull-request job BEFORE it authenticates, to decide whether to.

# The Dockerfile a recipe builds. A checked-in cloudbuild.yaml builds the one
# beside it (both of today's do); a recipe that has none is refused, not
# guessed, because its inputs could not be read.
recipe_dockerfile() {
  local recipe path
  recipe="$(find_recipe "$1")" || return 1
  path="${recipe#*$'\t'}"
  case "${recipe%%$'\t'*}" in
    dockerfile) printf '%s' "${path}" ;;
    config)
      [[ -f "${REPO_ROOT}/${path%/*}/Dockerfile" ]] || return 1
      printf '%s' "${path%/*}/Dockerfile" ;;
  esac
}

# One glob per line: the paths whose change can change this image.
#   * the recipe's own directory -- Dockerfile, cloudbuild.yaml, nginx.conf,
#     repo-index/, whatever sits beside them;
#   * every COPY/ADD source its Dockerfile names, read by
#     dockerfile_copy_sources (lib/common.sh). A directory source covers what
#     is under it; a glob source is kept as the glob it is. swarm-verify's
#     recipe assembles its own context from the same repo-relative paths its
#     Dockerfile copies, so those sources are repository paths too;
#   * .dockerignore and .gcloudignore, which decide what every build sees.
# `*` in these globs crosses `/`, as bash's [[ == ]] matches it: an input
# over-read builds one image too many, an input under-read builds one too few.
image_inputs() {
  local target="$1" recipe dockerfile sources src
  recipe="$(find_recipe "${target}")" \
    || die "no Dockerfile or cloudbuild.yaml found for ${target}"
  dockerfile="$(recipe_dockerfile "${target}")" \
    || die "${target}: ${recipe#*$'\t'} has no Dockerfile beside it, so what it copies in cannot be read"
  printf '%s\n' "$(dirname -- "${recipe#*$'\t'}")/*" .dockerignore .gcloudignore
  sources="$(dockerfile_copy_sources "${REPO_ROOT}/${dockerfile}")" \
    || die "${dockerfile}: a COPY or ADD above cannot be mapped to repository paths, so a change to what it copies would not build ${target}"
  while IFS= read -r src; do
    [[ -n "${src}" ]] || continue
    src="${src#./}"
    case "${src}" in
      ""|.)     printf '%s\n' '*' ;;
      */)       printf '%s\n' "${src}*" ;;
      *[*?[]*)  printf '%s\n' "${src}" ;;
      *)
        if [[ -d "${REPO_ROOT}/${src}" ]]; then printf '%s\n' "${src}/*"
        elif [[ -e "${REPO_ROOT}/${src}" ]]; then printf '%s\n' "${src}"
        else printf '%s\n' "${src}" "${src}/*"
        fi ;;
    esac
  done <<<"${sources}"
}

in_list() {
  local want="$1" item
  shift
  for item in "$@"; do
    if [[ "${item}" == "${want}" ]]; then return 0; fi
  done
  return 1
}

# The images a list of changed paths (one per line, in FILE) reaches, one per
# line in ALL_TARGETS order: those whose inputs a path matches, and then every
# image built FROM one of those (build_after), since it is built on the change.
affected_images() {
  local file="$1" f t p pats prereq grew
  local changed=() affected=()
  while IFS= read -r f || [[ -n "${f}" ]]; do
    f="${f#./}"
    if [[ -n "${f}" ]]; then changed+=("${f}"); fi
  done <"${file}"
  for t in "${ALL_TARGETS[@]}"; do
    pats="$(image_inputs "${t}")" || exit 1
    while IFS= read -r p; do
      for f in ${changed[@]+"${changed[@]}"}; do
        # shellcheck disable=SC2053 # the right side is a glob, on purpose
        if [[ "${f}" == ${p} ]]; then affected+=("${t}"); break 2; fi
      done
    done <<<"${pats}"
  done
  grew=1
  while [[ "${grew}" -eq 1 ]]; do
    grew=0
    for t in "${ALL_TARGETS[@]}"; do
      prereq="$(build_after "${t}")"
      if [[ -n "${prereq}" ]] && in_list "${prereq}" ${affected[@]+"${affected[@]}"} \
         && ! in_list "${t}" ${affected[@]+"${affected[@]}"}; then
        affected+=("${t}"); grew=1
      fi
    done
  done
  for t in "${ALL_TARGETS[@]}"; do
    if in_list "${t}" ${affected[@]+"${affected[@]}"}; then printf '%s\n' "${t}"; fi
  done
}

TARGETS=()
TAG=""
CREATE_REPO=0
ASYNC=0
DIGESTS_ONLY=0
REUSE_CI=""
BUILD_ONLY=0
LOCAL_BUILD=0
AFFECTED_BY=""
LIST_INPUTS=0
INCREMENTAL=""
FULL_BUILD_REASON=""
# 1 when --incremental was set by --reuse-ci, to check the record CI made.
REUSE_CHECK=0

# HOW MANY BUILDS AT ONCE, and why the default is 4 rather than "all of them".
#
# Release run 35969538707 spent nineteen minutes (07:28:10 -> 07:46:51) in
# this script submitting eight builds one after another, about three minutes
# each: Cloud Build reported ~2m of building per image, and the rest was the
# source upload and gcloud's polling. Nothing about any of those builds waited
# on another except one pair (see build_after below).
#
# Bounded because saga-agents-staging is a SHARED project: its Cloud Build
# concurrency quota is shared with the other team in it, so a release should
# take what it needs and no more. And 4 is what it needs. agent-runtime-browser
# and agent-runtime-indexer cannot be submitted until agent-runtime-base has
# FINISHED, so the critical path is two builds long whatever the bound; four
# slots fit the other seven images inside that path (workspace-apply, #847,
# made it seven), and the two derived images share the slots the base frees,
# so ten images would only finish at the same time with more.
PARALLELISM="${BUILD_PARALLELISM:-4}"
# How often the scheduler looks for finished submits. Local and free: it reads
# the status file each backgrounded submit writes, and calls no API.
POLL_INTERVAL="${BUILD_POLL_INTERVAL:-2}"
# HOW OFTEN CLOUD BUILD IS ASKED, in whole seconds (0 = every scheduler tick,
# which only the tests use). Main's image build failed 27 times on 2026-10-20
# and 2026-10-21 on `429 RESOURCE_EXHAUSTED` for
# cloudbuild.googleapis.com/get_requests: the per-minute status-read quota of
# saga-agents-staging, a project SHARED with another team that draws on the
# same quota. Each image was a blocking `gcloud builds submit`, which polls its
# own build while it streams the log, so four images were four pollers, per
# commit, and every main commit runs its own build. Now one `builds list
# --ongoing` per round covers every build in flight, and a build is described
# (and its log fetched) once, when it leaves that list. 15 s against a build of
# minutes costs at most 15 s of a build's wall-clock time.
STATUS_INTERVAL="${BUILD_STATUS_INTERVAL:-15}"
# A 429 is retried this many attempts in all, after BACKOFF, 2*BACKOFF, ...
# seconds (capped at BACKOFF_MAX), each scaled by a random 50-100% so that four
# builds of one commit -- and the next commit's build -- do not retry in step.
# The default schedule waits about 5+10+20+40+80 s: past a quota minute twice.
QUOTA_RETRIES="${BUILD_QUOTA_RETRIES:-6}"
QUOTA_BACKOFF="${BUILD_QUOTA_BACKOFF:-5}"
QUOTA_BACKOFF_MAX="${BUILD_QUOTA_BACKOFF_MAX:-120}"
# A build whose status stays unreadable on a 429 for this many rounds in a row
# is failed, named as unreadable rather than broken, so a quota that never
# comes back cannot hold the job until its own timeout.
QUOTA_ROUNDS="${BUILD_QUOTA_ROUNDS:-10}"
# A pause between two submissions started in the same pass. Every gcloud
# process opens the same sqlite credential cache under ~/.config/gcloud, and
# several opening it in the same instant is how parallel gcloud reports
# "database is locked". Two seconds against a three-minute build is free.
SUBMIT_STAGGER="${BUILD_SUBMIT_STAGGER:-2}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)         TAG="$2"; shift 2 ;;
    --parallel)    PARALLELISM="$2"; shift 2 ;;
    --create-repo) CREATE_REPO=1; shift ;;
    --async)       ASYNC=1; shift ;;
    # Read digests back for a tag that is ALREADY built and write the manifest,
    # without rebuilding. Exists because the manifest can be lost while the
    # images are perfectly fine -- a session that expires mid-build loses every
    # digest read-back but not the six images Cloud Build already pushed. Before
    # this, recovering a lost manifest meant rebuilding everything to regenerate
    # a JSON file.
    --digests-only) DIGESTS_ONLY=1; shift ;;
    --reuse-ci)    REUSE_CI="$2"; shift 2 ;;
    --build-only)  BUILD_ONLY=1; shift ;;
    --local)       LOCAL_BUILD=1; shift ;;
    --affected-by) AFFECTED_BY="$2"; shift 2 ;;
    --inputs)      LIST_INPUTS=1; shift ;;
    --incremental) INCREMENTAL="$2"; shift 2 ;;
    --full-build)  FULL_BUILD_REASON="$2"; shift 2 ;;
    # The header, up to the first line that is not a comment: a hand-kept
    # line range drifted every time the header grew (#888 box 92).
    -h|--help)     awk 'NR > 1 { if (!/^#/) exit; print }' "$0"; exit 0 ;;
    -*)            die "unknown flag: $1" ;;
    *)             TARGETS+=("$1"); shift ;;
  esac
done

# --affected-by and --inputs answer a question about every image and build
# nothing, so nothing else on the command line can mean anything to them.
if [[ -n "${AFFECTED_BY}" || "${LIST_INPUTS}" -eq 1 ]]; then
  MODE="--inputs"
  [[ -z "${AFFECTED_BY}" ]] || MODE="--affected-by"
  [[ "${#TARGETS[@]}" -eq 0 && -z "${TAG}" && -z "${REUSE_CI}" \
     && "${BUILD_ONLY}${LOCAL_BUILD}${DIGESTS_ONLY}${ASYNC}${CREATE_REPO}" == 00000 \
     && -z "${INCREMENTAL}${FULL_BUILD_REASON}" \
     && ( -z "${AFFECTED_BY}" || "${LIST_INPUTS}" -eq 0 ) ]] \
    || die "${MODE} reads every image's inputs and builds nothing; it takes no target and no other flag"
  if [[ "${LIST_INPUTS}" -eq 1 ]]; then
    for target in "${ALL_TARGETS[@]}"; do
      pats="$(image_inputs "${target}")" || exit 1
      while IFS= read -r p; do printf '%s\t%s\n' "${target}" "${p}"; done <<<"${pats}"
    done
    exit 0
  fi
  CHANGED_LIST="${AFFECTED_BY}"
  [[ "${CHANGED_LIST}" != - ]] || CHANGED_LIST=/dev/stdin
  [[ -r "${CHANGED_LIST}" ]] || die "--affected-by: cannot read ${AFFECTED_BY}"
  affected_images "${CHANGED_LIST}"
  exit 0
fi

# --build-only never reads or writes a registry: everything that does is
# refused with it rather than quietly ignored.
if [[ "${BUILD_ONLY}" -eq 1 ]]; then
  [[ -z "${REUSE_CI}" && "${DIGESTS_ONLY}" -eq 0 && "${ASYNC}" -eq 0 && "${CREATE_REPO}" -eq 0 ]] \
    || die "--build-only pushes nothing and reads no registry; it cannot be combined with --reuse-ci, --digests-only, --async or --create-repo"
fi
[[ "${LOCAL_BUILD}" -eq 0 || "${BUILD_ONLY}" -eq 1 ]] \
  || die "--local builds without pushing on this machine's docker, so it is a mode of --build-only; pass both"

# --reuse-ci takes CI's build WHOLE, at the tag CI gives it. A narrower or
# retagged request is a different build, and quietly building that instead of
# reusing is how the release came to build every image twice.
if [[ -n "${REUSE_CI}" ]]; then
  case "${REUSE_CI}" in
    only|or-build) ;;
    *) die "--reuse-ci takes 'only' or 'or-build', not '${REUSE_CI}'" ;;
  esac
  [[ "${#TARGETS[@]}" -eq 0 ]] \
    || die "--reuse-ci takes the whole build CI made for this commit; it cannot be narrowed to ${TARGETS[*]}"
  [[ -z "${TAG}" ]] \
    || die "--reuse-ci tags a build from the commit, exactly as CI does; drop --tag ${TAG}"
  [[ "${DIGESTS_ONLY}" -eq 0 && "${ASYNC}" -eq 0 ]] \
    || die "--reuse-ci cannot be combined with --digests-only or --async"
fi
# --incremental is main's build of EVERY image, some of them by re-tagging the
# digest an earlier build made. Anything that narrows, retags, defers or
# skips the push is a different build.
if [[ -n "${INCREMENTAL}" ]]; then
  [[ "${#TARGETS[@]}" -eq 0 ]] \
    || die "--incremental decides which images to rebuild itself; it cannot be narrowed to ${TARGETS[*]}"
  [[ -z "${REUSE_CI}" && "${BUILD_ONLY}${DIGESTS_ONLY}${ASYNC}" == 000 ]] \
    || die "--incremental cannot be combined with --reuse-ci, --build-only, --digests-only or --async"
fi
[[ -z "${FULL_BUILD_REASON}" || -n "${INCREMENTAL}" ]] \
  || die "--full-build REASON says why an --incremental build rebuilds everything; without --incremental every image is built anyway"
[[ "${#TARGETS[@]}" -gt 0 ]] || TARGETS=("${ALL_TARGETS[@]}")
[[ "${PARALLELISM}" =~ ^[1-9][0-9]*$ ]] \
  || die "--parallel (or BUILD_PARALLELISM) must be a positive whole number, not '${PARALLELISM}'"
[[ "${STATUS_INTERVAL}" =~ ^[0-9]+$ ]] \
  || die "BUILD_STATUS_INTERVAL must be a whole number of seconds, not '${STATUS_INTERVAL}'"
[[ "${QUOTA_RETRIES}" =~ ^[1-9][0-9]*$ && "${QUOTA_ROUNDS}" =~ ^[1-9][0-9]*$ ]] \
  || die "BUILD_QUOTA_RETRIES and BUILD_QUOTA_ROUNDS must be positive whole numbers"
[[ "${QUOTA_BACKOFF}" =~ ^[0-9]+([.][0-9]+)?$ && "${QUOTA_BACKOFF_MAX}" =~ ^[0-9]+([.][0-9]+)?$ ]] \
  || die "BUILD_QUOTA_BACKOFF and BUILD_QUOTA_BACKOFF_MAX must be numbers of seconds"

# NOTE: the only bash on the reference workstation is 3.2.57 (Apple ships no
# newer one). Empty arrays are therefore always expanded as ${arr[@]+"${arr[@]}"},
# because plain "${arr[@]}" on an empty array is an unbound-variable error under
# `set -u` in 3.2. No associative arrays, no mapfile, no ${var,,} anywhere.
# The scheduler below cannot use bash 4.3's "wait for any child" either, so a
# finished submit is found by the status file it writes when it exits.

if [[ "${LOCAL_BUILD}" -eq 1 ]]; then require_cmd docker jq; else require_cmd gcloud jq; fi
TAG="${TAG:-$(git_sha)}"
CLOUDBUILD_REGION="${CLOUDBUILD_REGION:-${REGION}}"
BUILD_TIMEOUT="${BUILD_TIMEOUT:-3600s}"
BUILD_MACHINE="${BUILD_MACHINE:-E2_HIGHCPU_8}"
# The full commit the build is made from, recorded in the manifest. A release
# that reuses CI's build checks this against the commit it is releasing, so the
# record -- not the run it was found in -- vouches for its own provenance.
# Empty outside a git checkout, which a reuse then refuses.
COMMIT="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || true)"


# ---------------------------------------------------------------------------
# --incremental: which images this commit changed, offline (git and jq only).
# ---------------------------------------------------------------------------
# One row per image of ALL_TARGETS, in parallel indexed arrays (bash 3.2):
# P_ACTION is build or reuse, P_WHY says why, and a reused row carries the
# digest, the commit it was built from and when.
P_NAME=(); P_ACTION=(); P_WHY=(); P_DIGEST=(); P_FROM=(); P_AT=()
PREV_COMMIT=""

# The files whose change changes HOW every image is built, not what goes into
# one: a change to any of them since a reused digest was built rebuilds all.
BUILD_LOGIC=(scripts/build-images.sh scripts/lib/common.sh scripts/lib/ci-built-images.sh .github/workflows/application.yml)
# A reused digest older than this is rebuilt, so the patches its base images
# (python:3.11-slim, node, nginx...) have shipped since arrive within a week
# even on a part of the tree nobody touches.
REUSE_MAX_AGE_DAYS="${BUILD_REUSE_MAX_AGE_DAYS:-7}"

# PER-TARGET BUILD ARGS, as YAML list items for a docker step's args. swarm-ui's has to be a BUILD
# arg rather than a Cloud Run env var: Vite inlines `import.meta.env.VITE_*`
# when the bundle is compiled, so by the time a container starts, a static
# bundle has already decided what environment it thinks it is in.
#
# ENVIRONMENT is exported by lib/common.sh, so this carries dev to a dev
# build and prod to a prod one without a second place to keep in step.
# Defined here, above plan_incremental, because baked_build_args reads it.
#
# agent-runtime-base-nocodex is agent-runtime-base's Dockerfile (recipe_target)
# with INSTALL_CODEX=0, and this is the ONE place that argument is passed: the
# base, and so every runner profile but the browser's, keeps codex.
target_build_args() {
  case "$1" in
    swarm-ui)
      printf '%s\n' "      - --build-arg" "      - VITE_SWARM_ENV=${ENVIRONMENT}" ;;
    agent-runtime-base-nocodex)
      printf '%s\n' "      - --build-arg" "      - INSTALL_CODEX=0" ;;
  esac
}

# THE OS-PACKAGE LAYER IS REBUILT BY EVERY BUILD AND EVERY RELEASE (owner
# decision 2026-10-14, "force a fresh apt layer", then "every release").
# Releases 38535875750 (2c7bbea5) and 38540997006 (cc6353d0) refused
# workspace-apply over curl/libcurl4t64 8.14.1-2+deb13u5 (CVE-2026-8286, HIGH,
# fixed in +deb13u7). Its Dockerfile already runs `apt-get upgrade`; the image
# was simply never BUILT again. No input of it had changed, so main's
# incremental build re-tagged the digest made from 346f2725 on 2026-10-13 --
# before Debian shipped the fix -- and the release's reuse check, which
# compares inputs only, promoted it to the scan. A pinned base plus `apt-get
# upgrade` is only as fresh as the moment the RUN ran, and nothing in the tree
# changes when Debian publishes a fix.
#
# So APT_REFRESH names THIS build: the commit and the GitHub run (id and
# attempt), or the commit and the time outside Actions. Every image whose
# recipe upgrades OS packages (upgrades_os_packages, read from its Dockerfile,
# never listed here) is built with --build-arg APT_REFRESH=<it>, which its
# apt RUN line names so no layer cache can serve an older upgrade, and the
# manifest records it as the image's `apt_refresh`. plan_incremental, and so
# the release's reuse check, reuses such a digest only when its recorded
# apt_refresh IS this build's, which a new run never matches: every main build
# and every release rebuilds them, and the images built FROM them (build_after).
# An image that upgrades nothing (swarm-verify) is still reused. The owner
# chose this knowing the cost, about 7 extra minutes per release. The scan
# stays strict: nothing is ignored or pinned.
#
# BUILD_APT_REFRESH overrides the value: the tests pin it so a record they
# write counts as this build's, and an operator can pin it to repeat a build.
if [[ -n "${BUILD_APT_REFRESH:-}" ]]; then
  APT_REFRESH="${BUILD_APT_REFRESH}"
elif [[ -n "${GITHUB_RUN_ID:-}" ]]; then
  APT_REFRESH="${COMMIT:0:12}-run${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"
else
  APT_REFRESH="${COMMIT:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
fi
APT_REFRESH="${APT_REFRESH#-}"
[[ "${APT_REFRESH}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
  || die "BUILD_APT_REFRESH must be letters, digits, '.', '_' and '-', not '${APT_REFRESH}'"

# True when image $1's Dockerfile runs `apt-get upgrade` or `apk upgrade` (a
# RUN line, not a comment). agent-runtime-base-nocodex reads the base's
# Dockerfile, workspace-apply the one beside its checked-in cloudbuild.yaml.
#
# ONE awk, NO PIPE. The first version was `grep -v '^#' | grep -Eq`: grep -q
# exits at its first match, the writer then dies of SIGPIPE, and under
# pipefail the function answered "no" for exactly the long Dockerfiles
# (agent-runtime-base, workspace-apply) -- CI run 38545464833.
upgrades_os_packages() {
  local dockerfile
  dockerfile="$(recipe_dockerfile "$1")" || return 1
  awk '
    /^[[:space:]]*#/ { next }
    /(apt-get|apk)[[:space:]]+(-[^[:space:]]+[[:space:]]+)*upgrade([[:space:]]|$)/ { found = 1 }
    END { exit !found }
  ' "${REPO_ROOT}/${dockerfile}"
}

# The targets upgrades_os_packages answers yes for, as a JSON array.
os_upgrading_targets_json() {
  local t
  for t in "${ALL_TARGETS[@]}"; do
    if upgrades_os_packages "${t}"; then printf '%s\n' "${t}"; fi
  done | jq -R . | jq -sc .
}

# APT_REFRESH as YAML list items for a docker step's args, for an image that
# upgrades OS packages; nothing for any other. Not one of target_build_args:
# it changes every build, and the reuse decision reads it from the record's
# `apt_refresh` (plan_incremental), not as a baked value whose change would
# read as "its baked build args changed".
apt_build_args() {
  if upgrades_os_packages "$1"; then
    printf '%s\n' "      - --build-arg" "      - APT_REFRESH=${APT_REFRESH}"
  fi
}

# The same for a checked-in recipe, which is submitted as it is: Cloud Build
# substitutions `_MIRROR_DOCKER_HUB=...,_MIRROR_GHCR=...,_BUILDKIT_SYNTAX=...`
# (no leading comma) for its `docker build` to pass on. Every recipe here sets
# ALLOW_LOOSE, so one it does not reference is ignored.
recipe_mirror_subs() {
  local df name repo syntax out=""
  df="$(recipe_dockerfile "$1")" || die "$1: no Dockerfile beside its recipe to pass the build mirrors to"
  for name in "${MIRROR_BUILD_ARGS[@]}"; do
    repo="$(mirror_repository "${name}")"
    out+="${out:+,}_${name}=${repo}"
  done
  syntax="$(mirror_syntax_ref "${df}")"
  if [[ -n "${syntax}" ]]; then out+=",_BUILDKIT_SYNTAX=${syntax}"; fi
  printf '%s' "${out}"
}

# target_build_args' values, one KEY=VALUE a line: what an image bakes in
# beyond GIT_SHA and BUILD_TIME, and what the manifest records per image as
# `build_args`. Read from target_build_args' own output, so an arg added there
# is compared without a second list to keep in step.
#
# The reuse decision compares these with the previous record's, so a reused
# digest is never one built with a different value (#888 box 91). No file
# change marks a value that comes from outside the tree -- a GitHub variable,
# say -- so without the comparison an image baked with the old value would be
# reused until BUILD_REUSE_MAX_AGE_DAYS. Today the only one is ENVIRONMENT,
# which the record's own environment check already covers; the comparison is
# for the next one, whatever feeds it.
baked_build_args() {
  target_build_args "$1" | sed -n 's/^      - \([A-Za-z_][A-Za-z0-9_]*=.*\)$/\1/p'
}

# baked_build_args as a JSON array, the form the manifest records.
baked_build_args_json() {
  baked_build_args "$1" | jq -R . | jq -sc .
}

plan_row() {
  local want="$1" i
  for ((i = 0; i < ${#P_NAME[@]}; i++)); do
    if [[ "${P_NAME[$i]}" == "${want}" ]]; then printf '%s' "${i}"; return 0; fi
  done
  return 1
}

plan_full() {
  local t
  FULL_BUILD_REASON="$1"
  P_NAME=(); P_ACTION=(); P_WHY=(); P_DIGEST=(); P_FROM=(); P_AT=()
  for t in "${ALL_TARGETS[@]}"; do
    P_NAME+=("${t}"); P_ACTION+=(build); P_WHY+=("full build: $1")
    P_DIGEST+=(""); P_FROM+=(""); P_AT+=("")
  done
}

# The changed paths among image $1's inputs, one per line, from the list in $2.
matched_inputs() {
  local pats p f
  pats="$(image_inputs "$1")" || exit 1
  while IFS= read -r f || [[ -n "${f}" ]]; do
    [[ -n "${f}" ]] || continue
    while IFS= read -r p; do
      # shellcheck disable=SC2053 # the right side is a glob, on purpose
      if [[ "${f}" == ${p} ]]; then printf '%s\n' "${f}"; break; fi
    done <<<"${pats}"
  done <"$2"
}

# The arguments of image $1's `uv export` line, one per line, read from its
# Dockerfile (test_service_image_install.py holds that line). Fails when the
# Dockerfile has none, or one this cannot repeat word for word.
uv_export_args() {
  local dockerfile line word words=()
  dockerfile="$(recipe_dockerfile "$1")" || return 1
  line="$(awk '
      !on && /^[[:space:]]*RUN[[:space:]].*uv export/ { on = 1 }
      on {
        cont = ($0 ~ /\\[[:space:]]*$/)
        text = $0
        sub(/\\[[:space:]]*$/, "", text)
        printf "%s ", text
        if (!cont) exit
      }' "${REPO_ROOT}/${dockerfile}")"
  [[ "${line}" == *"uv export"* ]] || return 1
  line="${line#*uv export}"
  line="${line%%>*}"
  read -r -a words <<<"${line}"
  [[ "${#words[@]}" -gt 0 ]] || return 1
  for word in "${words[@]}"; do
    [[ "${word}" =~ ^[A-Za-z0-9_.=-]+$ ]] || return 1
    printf '%s\n' "${word}"
  done
}

# A hash of what image $2's `uv export` line exports at commit $1, comments
# dropped (uv writes the command it ran into the first lines).
uv_export_hash() {
  local rev="$1" target="$2" dir args=() a rc=0
  while IFS= read -r a; do args+=("${a}"); done < <(uv_export_args "${target}")
  [[ "${#args[@]}" -gt 0 ]] || return 1
  dir="${PLAN_DIR}/uv-${rev}"
  if [[ ! -d "${dir}" ]]; then
    mkdir -p "${dir}"
    git -C "${REPO_ROOT}" show "${rev}:pyproject.toml" >"${dir}/pyproject.toml" 2>/dev/null || return 1
    git -C "${REPO_ROOT}" show "${rev}:uv.lock" >"${dir}/uv.lock" 2>/dev/null || return 1
  fi
  (cd "${dir}" && uv export "${args[@]}" 2>/dev/null </dev/null) >"${dir}/export.${target}" || rc=$?
  [[ "${rc}" -eq 0 && -s "${dir}/export.${target}" ]] || return 1
  grep -v '^#' "${dir}/export.${target}" | git hash-object --stdin
}

# `build<TAB>why` when image $1, built from $2, takes a path listed in $3;
# `reuse<TAB>why` when it does not. uv.lock alone is narrowed to the image's
# own export.
rebuild_why() {
  local target="$1" from="$2" diff_list="$3" hits others before after
  hits="$(matched_inputs "${target}" "${diff_list}")"
  if [[ -z "${hits}" ]]; then
    printf 'reuse\tno input changed'; return 0
  fi
  others="$(grep -vxF uv.lock <<<"${hits}" || true)"
  if [[ -n "${others}" ]]; then
    printf 'build\tchanged: %s' "$(head -n 3 <<<"${others}" | paste -sd ' ' -)"
    [[ "$(wc -l <<<"${others}" | tr -d ' ')" -le 3 ]] || printf ' (and %s more)' "$(( $(wc -l <<<"${others}" | tr -d ' ') - 3 ))"
    return 0
  fi
  if ! command -v uv >/dev/null 2>&1; then
    printf 'build\tuv.lock changed, and without uv its own export cannot be compared'; return 0
  fi
  if ! before="$(uv_export_hash "${from}" "${target}")" || ! after="$(uv_export_hash HEAD "${target}")"; then
    printf 'build\tuv.lock changed, and its own uv export could not be read at both commits'; return 0
  fi
  if [[ "${before}" != "${after}" ]]; then
    printf 'build\tuv.lock changed what its own uv export installs'
  else
    printf 'reuse\tuv.lock changed, but not what its own uv export installs'
  fi
}

plan_incremental() {
  local prev="$1" t i row digest from at img why prereq grew age_ok logic diff_list prev_args args prev_apt
  if [[ -n "${FULL_BUILD_REASON}" ]]; then plan_full "${FULL_BUILD_REASON}"; return 0; fi
  if [[ "${ENVIRONMENT}" == prod ]]; then plan_full "prod is always built whole"; return 0; fi
  if [[ "${prev}" == none || ! -f "${prev}" ]]; then
    plan_full "no previous build of an ancestor commit was found"; return 0
  fi
  if ! jq -e 'type == "object" and ((.images // null) | type) == "array"' "${prev}" >/dev/null 2>&1; then
    plan_full "${prev} is not a build manifest"; return 0
  fi
  PREV_COMMIT="$(jq -r '.commit // ""' "${prev}")"
  if [[ "$(jq -r '.environment // ""' "${prev}")" != "${ENVIRONMENT}" ]]; then
    plan_full "the previous build was not built for ${ENVIRONMENT}"; return 0
  fi
  if ! [[ "${COMMIT}" =~ ^[0-9a-f]{40}$ ]]; then
    plan_full "this is not a git checkout, so what changed cannot be read"; return 0
  fi
  if ! [[ "${PREV_COMMIT}" =~ ^[0-9a-f]{40}$ ]] \
     || ! git -C "${REPO_ROOT}" merge-base --is-ancestor "${PREV_COMMIT}" "${COMMIT}" 2>/dev/null; then
    plan_full "the previous build's commit '${PREV_COMMIT}' is not an ancestor of ${COMMIT} in this checkout"; return 0
  fi
  if git_dirty; then
    plan_full "the working tree is dirty, so git cannot say what changed"; return 0
  fi
  [[ "${REUSE_MAX_AGE_DAYS}" =~ ^[0-9]+$ ]] \
    || die "BUILD_REUSE_MAX_AGE_DAYS must be a whole number of days, not '${REUSE_MAX_AGE_DAYS}'"

  for t in "${ALL_TARGETS[@]}"; do
    # A digest is the previous record's; the commit and time it was BUILT are
    # the image's own when the previous build reused it too, so a chain of
    # reuses still diffs from, and ages from, the build that made the digest.
    IFS=$'\t' read -r img digest from at prev_args prev_apt < <(jq -r --arg n "${t}" '
        . as $r | [ .images[] | select(.name == $n) ] as $m
        | if ($m | length) != 1 then ["-", "-", "-", "-", "null", "-"]
          else [ ($m[0].image // "-"), ($m[0].digest // "-"),
                 ($m[0].built_from // $r.commit // "-"), ($m[0].built_at // $r.built_at // "-"),
                 ($m[0].build_args // null | tojson), ($m[0].apt_refresh // "-") ] end
        | @tsv' "${prev}")
    if [[ "${img}" != "${IMAGE_REPO}/${t}" ]]; then
      plan_full "the previous build records ${t} as ${img}, not ${IMAGE_REPO}/${t}"; return 0
    fi
    if ! [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      plan_full "the previous build records no sha256 digest for ${t}"; return 0
    fi
    if ! [[ "${from}" =~ ^[0-9a-f]{40}$ ]] \
       || ! git -C "${REPO_ROOT}" merge-base --is-ancestor "${from}" "${COMMIT}" 2>/dev/null; then
      plan_full "${t}'s digest was built from '${from}', which is not an ancestor of ${COMMIT} in this checkout"; return 0
    fi
    # A digest built FROM THIS COMMIT is this commit's own build -- what a
    # release finds in CI's record -- not a reuse, so the age limit, which is
    # about how long a reused digest may go without base-image patches, is
    # not asked of it. Its OS packages are refreshed by the APT_REFRESH rule
    # below. (Before every release ran this plan, such a record was promoted
    # without it; asking its age made a record older than a week rebuild every
    # image, swarm-verify included.)
    age_ok=yes
    if [[ "${from}" != "${COMMIT}" ]]; then
      age_ok="$(jq -rn --arg at "${at}" --argjson days "${REUSE_MAX_AGE_DAYS}" '
          try (if (now - ($at | fromdateiso8601)) <= ($days * 86400) then "yes" else "no" end) catch "unreadable"')"
    fi
    case "${age_ok}" in
      yes) ;;
      no) plan_full "${t}'s digest was built at ${at}, more than ${REUSE_MAX_AGE_DAYS} days ago, so base-image patches are due"; return 0 ;;
      *)  plan_full "${t}'s build time '${at}' cannot be read, so its age is unknown"; return 0 ;;
    esac
    diff_list="${PLAN_DIR}/diff-${from}"
    if [[ ! -f "${diff_list}" ]]; then
      # --no-renames: a rename is its old path AND its new one, so moving a
      # file out of an image's inputs reaches that image too.
      git -C "${REPO_ROOT}" diff --no-renames --name-only "${from}" "${COMMIT}" >"${diff_list}.tmp" \
        || die "git could not list what changed between ${from} and ${COMMIT}"
      mv -f "${diff_list}.tmp" "${diff_list}"
    fi
    for logic in "${BUILD_LOGIC[@]}"; do
      if grep -qxF -- "${logic}" "${diff_list}"; then
        plan_full "${logic} changed since ${from:0:12}, and it decides how every image is built"; return 0
      fi
    done
    why="$(rebuild_why "${t}" "${from}" "${diff_list}")"
    # A record written before the manifest named build args says nothing; an
    # image that bakes none has nothing to disagree with, one that bakes any
    # is rebuilt once, and records them from then on. A digest built from
    # this commit was built by this script with this commit's args, so an
    # unrecorded set is not a reason to build it again.
    args="$(baked_build_args_json "${t}")"
    if [[ "${prev_args}" == null && "${args}" != '[]' && "${from}" != "${COMMIT}" ]]; then
      why=$'build\tits baked build args were not recorded'
    elif [[ "${prev_args}" != null && "$(jq -c . <<<"${prev_args}")" != "${args}" ]]; then
      why=$'build\tits baked build args changed'
    fi
    # A digest whose OS packages were upgraded by another build carries
    # whatever Debian or Alpine had not fixed yet then (APT_REFRESH, above).
    if [[ "${why%%$'\t'*}" == reuse && "${prev_apt}" != "${APT_REFRESH}" ]] && upgrades_os_packages "${t}"; then
      why=$'build\t'"it upgrades OS packages, and every build and release refreshes them (APT_REFRESH=${APT_REFRESH}, the digest's ${prev_apt})"
    fi
    P_NAME+=("${t}"); P_DIGEST+=("${digest}"); P_FROM+=("${from}"); P_AT+=("${at}")
    P_ACTION+=("${why%%$'\t'*}"); P_WHY+=("${why#*$'\t'} since ${from:0:12}")
  done

  # An image built FROM a rebuilt one is rebuilt on it (build_after), as
  # --affected-by closes over it.
  grew=1
  while [[ "${grew}" -eq 1 ]]; do
    grew=0
    for ((i = 0; i < ${#P_NAME[@]}; i++)); do
      [[ "${P_ACTION[$i]}" == reuse ]] || continue
      prereq="$(build_after "${P_NAME[$i]}")"
      [[ -n "${prereq}" ]] || continue
      row="$(plan_row "${prereq}")" || continue
      if [[ "${P_ACTION[$row]}" == build ]]; then
        P_ACTION[i]=build; P_WHY[i]="built FROM ${prereq}, which is rebuilt"; grew=1
      fi
    done
  done
}

# ---------------------------------------------------------------------------
# Reuse what CI built for this commit, rather than build it a second time.
# ---------------------------------------------------------------------------
# Below plan_incremental, because a reused record is checked with it.
#
# A RECORD IS PROMOTED ONLY IF EVERY DIGEST IN IT WAS BUILT FROM THIS COMMIT'S
# INPUTS (lane REL-SKIP, part of #1194; owner-approved 2026-10-12). CI's
# record of an incremental build names some digests it re-tagged rather than
# built, each with the `built_from` commit that made it. Those are checked
# here against THIS commit, image by image, by the same plan main's build
# uses: `git diff <built_from> <this commit>` over each image's inputs. So
# what a release promotes is compared with what made each digest -- never
# with HEAD~1, the previous release, or which releases were cancelled,
# superseded or failed in between. Release 38273482069 (f816e09b, 2026-10-12)
# failed, and nothing at the release side asked whether the digests on offer
# matched the commit. A digest whose inputs this commit has changed is built
# here, by --incremental with CI's record as the previous build: exactly the
# stale images and the ones built FROM them, every other digest kept. That
# is a build CI got wrong, not the duplicate --reuse-ci exists to avoid, so
# it is made under `only` too. A record whose every image was built from this
# commit (a full build, or one written before records named `built_from`)
# needs no git at all.
if [[ -n "${REUSE_CI}" ]]; then
  [[ "${COMMIT}" =~ ^[0-9a-f]{40}$ ]] \
    || die "--reuse-ci needs a git checkout: CI's build is found by the commit it was made from"
  if_absent=fail
  if [[ "${REUSE_CI}" == or-build ]]; then if_absent=build; fi
  reuse_rc=0
  "${SWARM_LIB_DIR}/ci-built-images.sh" --sha "${COMMIT}" --environment "${ENVIRONMENT}" \
    --out "${BUILD_DIR}/images-${ENVIRONMENT}.json" --if-absent "${if_absent}" || reuse_rc=$?
  case "${reuse_rc}" in
    0)
      # ...and only when every image that upgrades OS packages was built by
      # THIS run (APT_REFRESH): a release rebuilds them otherwise, below.
      if jq -e --arg c "${COMMIT}" --arg a "${APT_REFRESH}" --argjson up "$(os_upgrading_targets_json)" '
           all(.images[]; (.built_from // $c) == $c)
           and all(.images[]; (.name as $n | $up | index($n)) == null or .apt_refresh == $a)' \
           "${BUILD_DIR}/images-${ENVIRONMENT}.json" >/dev/null; then
        hr
        ok "reused CI's build of ${COMMIT}: every image was built from it, no Cloud Build submitted"
        info "manifest: ${BUILD_DIR}/images-${ENVIRONMENT}.json"
        exit 0
      fi
      # The check needs the history back to each `built_from`; a release's
      # checkout is one commit deep. Without it plan_incremental cannot tell
      # an ancestor and rebuilds everything, which is correct, only slower.
      if [[ "$(git -C "${REPO_ROOT}" rev-parse --is-shallow-repository 2>/dev/null || true)" == true ]]; then
        info "this checkout is shallow; fetching the history each reused digest is checked against"
        git -C "${REPO_ROOT}" fetch --quiet --unshallow </dev/null 2>&1 | redact >&2 \
          || warn "could not fetch the history, so every image is rebuilt"
      fi
      cp -f "${BUILD_DIR}/images-${ENVIRONMENT}.json" "${BUILD_DIR}/ci-images-${ENVIRONMENT}.json"
      INCREMENTAL="${BUILD_DIR}/ci-images-${ENVIRONMENT}.json"
      REUSE_CHECK=1
      info "CI's build of ${COMMIT} reuses digests built from earlier commits; checking each against this commit's inputs" ;;
    3)
      # The same build CI would have made: same targets, same tag, same
      # manifest. Nothing below knows it was reached this way.
      info "building ${COMMIT} here, as CI would have: every image, tag ${TAG}" ;;
    *)
      exit "${reuse_rc}" ;;
  esac
fi

if [[ -n "${INCREMENTAL}" ]]; then
  require_cmd git
  step "Incremental build"
  PLAN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarm-incremental.XXXXXX")"
  trap 'rm -rf "${PLAN_DIR}"' EXIT
  plan_incremental "${INCREMENTAL}"
  rm -rf "${PLAN_DIR}"
  trap - EXIT
  if [[ -n "${FULL_BUILD_REASON}" ]]; then
    info "rebuilding every image: ${FULL_BUILD_REASON}"
  else
    info "previous build: ${PREV_COMMIT}"
  fi
  TARGETS=()
  for ((i = 0; i < ${#P_NAME[@]}; i++)); do
    printf '  %-22s %-7s %s\n' "${P_NAME[$i]}" "${P_ACTION[$i]}" "${P_WHY[$i]}" >&2
    [[ "${P_ACTION[$i]}" == reuse ]] || TARGETS+=("${P_NAME[$i]}")
  done
  if [[ "${REUSE_CHECK}" -eq 1 ]]; then
    if [[ "${#TARGETS[@]}" -eq 0 ]]; then
      # CI's record is promoted exactly as fetched: nothing below runs.
      rm -f "${INCREMENTAL}"
      hr
      ok "reused CI's build of ${COMMIT}: every reused digest was built from inputs this commit still has, no Cloud Build submitted"
      info "manifest: ${BUILD_DIR}/images-${ENVIRONMENT}.json"
      exit 0
    fi
    warn "CI's record of ${COMMIT} offers ${#TARGETS[@]} digest(s) this release rebuilds -- inputs this commit has changed, or OS packages every release refreshes (APT_REFRESH): ${TARGETS[*]}. Building them here; nothing stale is promoted."
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      echo "**Images:** CI's record of \`${COMMIT:0:12}\` reused digests built before this commit changed their inputs; this run rebuilt ${TARGETS[*]}." \
        >>"${GITHUB_STEP_SUMMARY}"
    fi
  fi
fi

# Advisory only. The check that can actually stop a bad image is the one made
# before every submission below -- see the comment there for why a single check
# here is not enough.
if git_dirty; then
  warn "working tree is dirty; ${TAG} will not reproduce from git alone"
fi

# --build-only pushes nothing, so it has no registry to find: the first call
# it makes to Google is the build itself.
if [[ "${BUILD_ONLY}" -eq 1 ]]; then
  step "Build only"
  ok "nothing is pushed: no registry is read, tagged or written, and no manifest is kept"
else
  step "Artifact Registry"
  # stderr captured rather than discarded: the failure that matters most here is
  # an expired session, and discarding it turned that into "the repository does
  # not exist. Run 'make infra' first" -- advice that cannot work, for a
  # repository that was there all along.
  REPO_ERR=""
  if REPO_ERR="$(gcloud artifacts repositories describe "${ARTIFACT_REGISTRY}" \
       --project "${PROJECT_ID}" --location "${REGION}" --format='value(name)' 2>&1 >/dev/null)"; then
    ok "repository ${IMAGE_REPO}"
  else
    # Exits here if the session is dead, so nothing below can misreport it.
    die_if_auth_failure "${REPO_ERR}"
    if [[ "${CREATE_REPO}" -eq 1 ]]; then
      info "creating Artifact Registry repository ${ARTIFACT_REGISTRY}"
      gcloud artifacts repositories create "${ARTIFACT_REGISTRY}" \
        --project "${PROJECT_ID}" --location "${REGION}" \
        --repository-format=docker \
        --description="Agent swarm images" \
        --labels="managed-by=swarm-bootstrap,component=images"
      ok "created ${IMAGE_REPO}"
    else
      err "Artifact Registry repository ${ARTIFACT_REGISTRY} does not exist in ${REGION}."
      [[ -z "${REPO_ERR}" ]] || printf '%s\n' "${REPO_ERR}" | head -n 2 | sed 's/^/     /' >&2
      die "run 'make infra' first, or re-run with --create-repo."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# --incremental: the previous digest of every unchanged image gains this tag.
# ---------------------------------------------------------------------------
# BEFORE any build is submitted: agent-runtime-browser's and -indexer's
# recipes pull agent-runtime-base:<tag>, so a derived image rebuilt on a
# reused base finds the base under this commit's tag. In ALL_TARGETS order,
# base first, so a base whose re-tag fails is rebuilt and so is everything
# built FROM it. A re-tag that fails for any reason but a dead session
# rebuilds that image instead: a full build is always a correct answer.
REUSED=()
if [[ -n "${INCREMENTAL}" ]]; then
  for ((i = 0; i < ${#P_NAME[@]}; i++)); do
    [[ "${P_ACTION[$i]}" == reuse ]] || continue
    target="${P_NAME[$i]}"
    prereq="$(build_after "${target}")"
    if [[ -n "${prereq}" ]] && row="$(plan_row "${prereq}")" && [[ "${P_ACTION[$row]}" == build ]]; then
      P_ACTION[i]=build; P_WHY[i]="built FROM ${prereq}, which is rebuilt"
      TARGETS+=("${target}")
      continue
    fi
    # A digest built from this commit was pushed as :${TAG} by that build (a
    # release reusing CI's record of its own commit): there is nothing to
    # re-tag, and the read-back below still checks :${TAG} names it.
    if [[ "${P_FROM[$i]}" == "${COMMIT}" ]]; then
      ok "${target}: reused ${P_DIGEST[$i]}, built from this commit as :${TAG}"
      REUSED+=("${target}")
      continue
    fi
    tag_err=""
    if tag_err="$(gcloud artifacts docker tags add "${IMAGE_REPO}/${target}@${P_DIGEST[$i]}" \
         "${IMAGE_REPO}/${target}:${TAG}" --project "${PROJECT_ID}" </dev/null 2>&1 >/dev/null)"; then
      ok "${target}: reused ${P_DIGEST[$i]} (built from ${P_FROM[$i]:0:12}), now also :${TAG}"
      REUSED+=("${target}")
    else
      die_if_auth_failure "${tag_err}"
      warn "${target}: could not tag ${P_DIGEST[$i]} as :${TAG}, so it is rebuilt instead"
      [[ -z "${tag_err}" ]] || printf '%s\n' "${tag_err}" | redact | head -n 2 | sed 's/^/     /' >&2
      P_ACTION[i]=build; P_WHY[i]="its previous digest could not be tagged :${TAG}"
      TARGETS+=("${target}")
    fi
  done
  # Built in ALL_TARGETS order, as a full build is.
  ordered=()
  for target in "${ALL_TARGETS[@]}"; do
    if in_list "${target}" ${TARGETS[@]+"${TARGETS[@]}"}; then ordered+=("${target}"); fi
  done
  TARGETS=(${ordered[@]+"${ordered[@]}"})
  info "${#TARGETS[@]} image(s) to build, ${#REUSED[@]} reused"
fi

# The Cloud Build step that stages the tool mirror (lib/build-mirror.sh), for
# a build of any of the Dockerfiles given that bind-mounts it -- or nothing.
# A pull request's local build stages it from upstream instead
# (build_locally), because it can read no bucket, so it gets no step.
mirror_step_for() {
  local df
  [[ "${LOCAL_BUILD}" -eq 0 ]] || return 0
  for df in "$@"; do
    if dockerfile_uses_tool_mirror "${df}"; then
      tool_mirror_step_yaml
      return
    fi
  done
}

# A Dockerfile with no cloudbuild.yaml gets a generated one. It is written to
# build/ and kept, so a failed build can be reproduced exactly.
#
# A Dockerfile that bind-mounts the tool mirror is built with BuildKit
# explicitly: `RUN --mount` is BuildKit's, and the legacy builder refuses it.
generate_config() {
  local dockerfile="$1" image="$2" out="$3" target="${4:-}" mirror_step mirror_args buildkit=""
  mirror_step="$(mirror_step_for "${dockerfile}")" || exit 1
  mirror_args="$(mirror_build_args_yaml "${dockerfile}")" || exit 1
  if dockerfile_uses_tool_mirror "${dockerfile}"; then
    buildkit="    env:
      - DOCKER_BUILDKIT=1"
  fi
  # --build-only lists no `images:`, so Cloud Build pushes nothing; the image
  # it builds lives and dies on the build worker.
  local push_block="images:
  - ${image}:${TAG}"
  [[ "${BUILD_ONLY}" -eq 0 ]] || push_block="# --build-only: no images, so nothing is pushed."

  cat >"${out}" <<YAML
# Generated by scripts/build-images.sh on $(iso_now). Do not edit; edit the
# Dockerfile or add a checked-in cloudbuild.yaml next to it instead.
timeout: ${BUILD_TIMEOUT}
options:
  machineType: ${BUILD_MACHINE}
  # CLOUD_LOGGING_ONLY keeps builds working when the legacy Cloud Build bucket
  # is absent and when a user-specified build service account is in use.
  logging: CLOUD_LOGGING_ONLY
steps:
${mirror_step}
  - name: gcr.io/cloud-builders/docker
${buildkit}
    args:
      - build
      - --platform
      - linux/amd64
      - -f
      - ${dockerfile}
      - -t
      - ${image}:${TAG}
      - --build-arg
      - GIT_SHA=${TAG}
      - --build-arg
      - BUILD_TIME=$(iso_now)
${mirror_args}
$(target_build_args "${target}")
$(apt_build_args "${target}")
      - .
${push_block}
YAML
}

# --build-only, for an image built FROM another: build both, in ONE Cloud
# Build, the second FROM the first. Main's recipe for agent-runtime-browser
# pulls agent-runtime-base-nocodex:<tag> from the registry, which a build-only
# run never pushed -- and building the base here is also the point: a pull request that
# breaks the base's self-tests is caught by either image's build. The second
# image takes the first as BASE_IMAGE, as the checked-in recipe passes it.
generate_chain_config() {
  local target="$1" prereq="$2" out="$3" base_df target_df base_image mirror_step base_mirror_args target_mirror_args
  base_df="$(recipe_dockerfile "${prereq}")" \
    || die "${target} is built FROM ${prereq}, which has no Dockerfile to build it from here"
  target_df="$(recipe_dockerfile "${target}")" \
    || die "${target}: no Dockerfile beside its recipe to build without pushing"
  mirror_step="$(mirror_step_for "${base_df}" "${target_df}")" || exit 1
  base_mirror_args="$(mirror_build_args_yaml "${base_df}")" || exit 1
  target_mirror_args="$(mirror_build_args_yaml "${target_df}")" || exit 1
  base_image="${LOCAL_IMAGE}/${prereq}:${TAG}"
  cat >"${out}" <<YAML
# Generated by scripts/build-images.sh --build-only on $(iso_now). Pushes
# nothing: there is no images: list. ${target} is built FROM ${prereq}, so
# both are built here, the second FROM the first.
timeout: ${BUILD_TIMEOUT}
options:
  machineType: ${BUILD_MACHINE}
  logging: CLOUD_LOGGING_ONLY
steps:
${mirror_step}
  - id: build-${prereq}
    name: gcr.io/cloud-builders/docker
    env:
      - DOCKER_BUILDKIT=1
    args:
      - build
      - --platform
      - linux/amd64
      - -f
      - ${base_df}
      - -t
      - ${base_image}
      - --build-arg
      - GIT_SHA=${TAG}
      - --build-arg
      - BUILD_TIME=$(iso_now)
${base_mirror_args}
$(target_build_args "${prereq}")
$(apt_build_args "${prereq}")
      - .
  - id: build-${target}
    name: gcr.io/cloud-builders/docker
    env:
      - DOCKER_BUILDKIT=1
    args:
      - build
      - --platform
      - linux/amd64
      - -f
      - ${target_df}
      - -t
      - ${LOCAL_IMAGE}/${target}:${TAG}
      - --build-arg
      - BASE_IMAGE=${base_image}
      - --build-arg
      - GIT_SHA=${TAG}
      - --build-arg
      - BUILD_TIME=$(iso_now)
${target_mirror_args}
$(target_build_args "${target}")
$(apt_build_args "${target}")
      - .
YAML
}

# --build-only, for a checked-in recipe: the same file with its push removed.
# A top-level `images:` (what Cloud Build pushes) or `artifacts:` (what it
# uploads) block is dropped whole, up to the next top-level key; every step is
# kept, so the build is the one main makes.
strip_push() {
  awk '
    /^[A-Za-z_][A-Za-z0-9_]*:/ { skip = ($0 ~ /^(images|artifacts):/) }
    !skip { print }
  ' "$1" >"$2"
}

# The last word on --build-only, read from the config actually submitted:
# whatever wrote it, a config that would push is never submitted.
refuse_if_pushes() {
  local config="$1" target="$2"
  if grep -Eq '^(images|artifacts):' "${config}" \
     || grep -Eq '(^|[^[:alnum:]_])docker[[:space:]]+push([^[:alnum:]_]|$)|^[[:space:]]*-[[:space:]]+push[[:space:]]*$' "${config}"; then
    die "${target}: --build-only would submit ${config#"${REPO_ROOT}/"}, which pushes an image; refusing"
  fi
}

SUBMIT_ARGS=(--project "${PROJECT_ID}" --region "${CLOUDBUILD_REGION}")
if [[ -n "${CLOUDBUILD_SERVICE_ACCOUNT:-}" ]]; then
  SUBMIT_ARGS+=(--service-account="projects/${PROJECT_ID}/serviceAccounts/${CLOUDBUILD_SERVICE_ACCOUNT}")
fi
# Where the source tarball goes. Unset, gcloud uses <project>_cloudbuild. Set,
# it is a bucket a narrower identity can be given objects in without the
# project's own staging bucket.
if [[ -n "${CLOUDBUILD_SOURCE_STAGING_DIR:-}" ]]; then
  SUBMIT_ARGS+=(--gcs-source-staging-dir="${CLOUDBUILD_SOURCE_STAGING_DIR}")
fi
# ALWAYS --async: the submit returns once the build is created, and
# read_build_states watches every build in flight from one loop. A blocking
# submit polls its own build for as long as it runs -- the per-build polling
# that spent the shared project's get_requests quota (CB-429-RETRY). The
# script's own --async flag only stops it watching.
SUBMIT_ARGS+=(--async)

# --build-only tags each image with this name, on the build worker only. It
# names no registry host, and no config built from it lists an image to push.
LOCAL_IMAGE="swarm-build-only"

MANIFEST="${BUILD_DIR}/images-${ENVIRONMENT}.json"
BUILT=()
SKIPPED=()

if [[ "${DIGESTS_ONLY}" -eq 1 ]]; then
  [[ -n "${TAG}" ]] || die "--digests-only needs an explicit --tag: it describes images that already exist, and the tag is the only thing that says which ones"
  info "reading back digests for ${TAG} without rebuilding"
  BUILT=("${TARGETS[@]}")
  # Emptied so the build phase below is skipped without restructuring it.
  # Expanded with the ${arr[@]+"${arr[@]}"} guard everywhere, because a plain
  # "${arr[@]}" on an empty array is an unbound-variable error under `set -u`
  # in bash 3.2.
  TARGETS=()
fi

# ---------------------------------------------------------------------------
# Recipes. Resolved for EVERY target before anything is submitted, so a target
# with no recipe fails the run before it has spent a build on the others.
# ---------------------------------------------------------------------------
#
# One row per image, in parallel indexed arrays (bash 3.2 has no associative
# ones). T_STATE is pending -> running -> ok | failed, or pending -> skipped
# for an image that was never submitted.
# T_RETRIED is 1 once an image has been rebuilt after a pull that got no answer.
# T_BUILD_ID is the Cloud Build id of a running image once its submit returned
# (empty while it is still submitting); T_UNREAD counts the status rounds in a
# row a 429 kept it from being read.
T_NAME=(); T_CONFIG=(); T_SUBS=(); T_STATE=(); T_PID=(); T_START=(); T_NOTE=(); T_RETRIED=()
T_BUILD_ID=(); T_UNREAD=()

# Row of an image in T_NAME, or failure if it is not part of this run.
row_of() {
  local want="$1" i
  for ((i = 0; i < ${#T_NAME[@]}; i++)); do
    if [[ "${T_NAME[$i]}" == "${want}" ]]; then printf '%s' "${i}"; return 0; fi
  done
  return 1
}

for target in ${TARGETS[@]+"${TARGETS[@]}"}; do
  if row_of "${target}" >/dev/null; then continue; fi   # named twice
  if ! recipe="$(find_recipe "${target}")"; then
    # FAIL, do not skip. A warning here let `make build` report success having
    # built nothing for swarm-api, swarm-scheduler and swarm-quota-broker, while
    # terraform/infra deployed those exact tags -- so the first sign of trouble
    # was Cloud Run reporting "Image not found" long after the build "passed".
    err "no Dockerfile or cloudbuild.yaml found for ${target}"
    dim "looked in images/${target}/, apps/${target}/, docker/${target}/"
    die "every target in ALL_TARGETS must be buildable; add a recipe or remove it from the list"
  fi
  kind="${recipe%%$'\t'*}"
  path="${recipe#*$'\t'}"
  image="${IMAGE_REPO}/${target}"
  [[ "${BUILD_ONLY}" -eq 0 ]] || image="${LOCAL_IMAGE}/${target}"

  if [[ "${BUILD_ONLY}" -eq 1 ]]; then
    # An image another one in this run is built FROM is built inside that
    # one's build (generate_chain_config), not a second time on its own.
    inside=""
    for other in "${TARGETS[@]}"; do
      if [[ "$(build_after "${other}")" == "${target}" ]]; then inside="${other}"; break; fi
    done
    if [[ -n "${inside}" ]]; then
      info "${target}: built inside ${inside}'s build, which is built FROM it"
      continue
    fi
    mkdir -p "${BUILD_DIR}/build-only"
    config="${BUILD_DIR}/build-only/cloudbuild-${target}.yaml"
    subs=""
    prereq="$(build_after "${target}")"
    if [[ -n "${prereq}" ]]; then
      generate_chain_config "${target}" "${prereq}" "${config}"
      info "${target}: generated $(basename "${config}") building ${prereq} and then ${target}, pushing neither"
    elif [[ "${kind}" == config ]]; then
      strip_push "${REPO_ROOT}/${path}" "${config}"
      subs="_IMAGE=${image},_TAG=${TAG},_TARGET=${target}"
      if upgrades_os_packages "${target}"; then subs+=",_APT_REFRESH=${APT_REFRESH}"; fi
      if [[ "${LOCAL_BUILD}" -eq 0 ]]; then
        mirror_subs="$(recipe_mirror_subs "${target}")" || exit 1
        subs+=",${mirror_subs}"
      fi
      info "${target}: checked-in ${path}, without its push"
    else
      generate_config "${path}" "${image}" "${config}" "${target}"
      info "${target}: generated $(basename "${config}") for ${path}, without a push"
    fi
    refuse_if_pushes "${config}" "${target}"
    T_NAME+=("${target}"); T_CONFIG+=("${config}"); T_SUBS+=("${subs}")
    T_STATE+=(pending); T_PID+=(""); T_START+=(0); T_NOTE+=(""); T_RETRIED+=("")
    T_BUILD_ID+=(""); T_UNREAD+=(0)
    continue
  fi

  config=""
  # Cloud Build REJECTS a substitution key the template never references, so the
  # two recipe kinds must be submitted differently:
  #   config      a checked-in cloudbuild.yaml, which parameterises itself with
  #               ${_IMAGE} / ${_TAG} / ${_DOCKERFILE} and needs them supplied
  #   dockerfile  a config generate_config() wrote, with every value already
  #               inlined -- passing substitutions here fails the submit with
  #               "key ... is not matched in the template"
  subs=""
  case "${kind}" in
    config)
      config="${REPO_ROOT}/${path}"
      # NOT _DOCKERFILE: for this kind `path` IS the cloudbuild.yaml, and passing
      # it made docker try to parse the YAML as a Dockerfile ("unknown
      # instruction: TIMEOUT:"). The config declares its own _DOCKERFILE default.
      subs="_IMAGE=${image},_TAG=${TAG},_TARGET=${target}"
      if upgrades_os_packages "${target}"; then subs+=",_APT_REFRESH=${APT_REFRESH}"; fi
      mirror_subs="$(recipe_mirror_subs "${target}")" || exit 1
      subs+=",${mirror_subs}"
      info "${target}: using checked-in ${path}"
      ;;
    dockerfile)
      config="${BUILD_DIR}/cloudbuild-${target}.yaml"
      generate_config "${path}" "${image}" "${config}" "${target}"
      info "${target}: generated $(basename "${config}") for ${path}"
      ;;
  esac

  T_NAME+=("${target}"); T_CONFIG+=("${config}"); T_SUBS+=("${subs}")
  T_STATE+=(pending); T_PID+=(""); T_START+=(0); T_NOTE+=(""); T_RETRIED+=("")
  T_BUILD_ID+=(""); T_UNREAD+=(0)
done

# ---------------------------------------------------------------------------
# --build-only --local: the same build-only configs, on this machine's docker.
# ---------------------------------------------------------------------------
# What a pull request runs (application.yml `build-check`, #650). The owner
# chose NOT to give pull requests a Google identity (2026-10-05): a pull
# request's checkout chooses what runs, so whatever that identity could do --
# run Cloud Build in the shared project -- anyone able to push a branch could
# do. So the runner builds, and it holds nothing to push with.
#
# NOTHING IS RESTATED. Every config above -- generate_config's, a checked-in
# recipe with its push stripped, generate_chain_config's base-then-derived
# pair -- is exactly what --build-only would submit to Cloud Build, and
# refuse_if_pushes has already read it. LOCAL_REPLAY runs it step by step:
#
#   * a `docker build` step of the docker builder becomes
#     `docker buildx build` with the step's own arguments, --no-cache (a
#     cached RUN is a self-test that did not run) and an explicit output that
#     is never a registry: `type=cacheonly`, or, for an image a later step is
#     built FROM, `type=oci` into build/build-only/local/. The later step then
#     takes BASE_IMAGE=<name>@<digest>, resolved by a `--build-context
#     <name>@<digest>=oci-layout://...` -- no pull, no registry, and the
#     indexer's refusal of a BASE_IMAGE without a digest still holds. A base
#     exported once is reused by the next chain in the run, never rebuilt;
#   * any other step (swarm-verify's context assembly) runs in its own image
#     with the checkout at /workspace, as the invoking user, with no docker
#     socket -- so no step can push or pull an image itself;
#   * a docker-builder step that is not a build (push, pull, login, tag), and
#     any config that asks for a secret, are refused.
#
# The RUN steps each build ran are READ from its Dockerfile into
# build/build-only/local-report.md, which the job puts in its summary. They
# are the self-tests main's Cloud Build runs, because they are the same
# Dockerfile steps: `swarm-repo-index --self-test`, `--lsp-self-test`,
# `swarm-repo-graph --self-test`, the base's agent-worker and toolbox checks,
# the browser's Chromium launch.
#
# One build at a time: they share one runner's disk and CPUs, and the derived
# images wait for the base anyway.
LOCAL_REPLAY="$(cat <<'PY'
import json, os, re, shutil, subprocess, sys
import yaml

config, repo, target, builder, state, report, why_file = sys.argv[1:8]
DOCKER = "gcr.io/cloud-builders/docker"
# BUILD_ONLY_SCAN (C3, owner decision 2026-10-14): a command run as
# `<command> <oci-layout> <image>` on every image this run builds, as soon as
# it is built; a non-zero exit fails the build. application.yml's
# `build-check` passes scripts/scan-image-layout.sh, the scan the release's
# promotion runs, so a fixable HIGH/CRITICAL finding in a changed image is
# seen on the pull request rather than at the release. Unset: no scan, and a
# leaf image is exported nowhere, as before.
SCAN = os.environ.get("BUILD_ONLY_SCAN", "")
if SCAN and not os.path.isabs(SCAN):
    SCAN = os.path.join(repo, SCAN)


def fail(msg):
    with open(why_file, "w") as fh:
        fh.write(msg)
    print(f"{target}: {msg}", flush=True)
    sys.exit(1)


with open(config) as fh:
    doc = yaml.safe_load(fh) or {}
steps = doc.get("steps") or []
if "availableSecrets" in doc or "secrets" in doc or any("secretEnv" in s for s in steps):
    fail(f"{config} asks for a secret, and a local build-only run is given none; refusing")
if "images" in doc or "artifacts" in doc:
    fail(f"{config} pushes or uploads; refusing")

subs = {k: str(v) for k, v in (doc.get("substitutions") or {}).items()}
subs.update(json.loads(os.environ["LOCAL_SUBS"]))


def sub(value):
    def one(m):
        if m.group(1) not in subs:
            fail(f"{config} uses ${{{m.group(1)}}}, which a local build has no value for")
        return subs[m.group(1)]
    return re.sub(r"(?<!\$)\$\{(_[A-Z0-9_]+)\}", one, str(value)).replace("$$", "$")


def workspace(path):
    return repo + path[len("/workspace"):] if path == "/workspace" or path.startswith("/workspace/") else path


def flag(args, name):
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == name]


def safe(ref):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", ref)


def repo_of(ref):
    name = ref.split("@", 1)[0]
    return name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name


UPSTREAMS = json.loads(os.environ.get("MIRROR_UPSTREAMS_JSON") or "{}")


def upstream_args(dockerfile, args):
    """This runner can read neither build mirror (scripts/lib/build-mirror.sh),
    so each MIRROR_* argument names the registry it stands in for, and a
    `# syntax=` frontend named through a mirror is replaced, by
    BUILDKIT_SYNTAX, with the same reference -- digest and all -- upstream."""
    given = {a.split("=", 1)[0] for a in flag(args, "--build-arg")}
    out = []
    for name, m in UPSTREAMS.items():
        if name not in given:
            out += ["--build-arg", f"{name}={m['upstream']}"]
    if "BUILDKIT_SYNTAX" not in given and os.path.isfile(dockerfile):
        with open(dockerfile) as fh:
            syntax = re.match(r"#\s*syntax\s*=\s*(\S+)", fh.readline())
        parts = syntax.group(1).split("/", 3) if syntax else []
        for m in UPSTREAMS.values():
            if len(parts) == 4 and parts[0].endswith("-docker.pkg.dev") and parts[2].endswith(m["suffix"]):
                out += ["--build-arg", f"BUILDKIT_SYNTAX={m['upstream']}/{parts[3]}"]
    return out


def run_steps(dockerfile):
    """Each RUN instruction's line number and first line, read as Docker
    reads the file: continuations joined, heredoc bodies skipped."""
    out, cont, heredoc = [], False, None
    if not os.path.isfile(dockerfile):
        return None
    with open(dockerfile) as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if heredoc is not None:
                if line.lstrip("\t") == heredoc:
                    heredoc = None
                continue
            if not cont and re.match(r"\s*RUN\s", line, re.I):
                out.append((n, line.strip()))
            m = re.search(r"<<-?['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
            if m and not line.lstrip().startswith("#"):
                heredoc = m.group(1)
            cont = line.endswith("\\") and not line.lstrip().startswith("#") or (cont and line.lstrip().startswith("#"))
    return out


def stream(cmd, cwd, sid):
    print(f"step {sid}: {' '.join(cmd[:3])}", flush=True)
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, errors="replace")
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    return proc.wait()


parsed = []
for i, step in enumerate(steps):
    name, args = sub(step.get("name", "")), [sub(a) for a in step.get("args") or []]
    sid = str(step.get("id") or f"step-{i}")
    is_build = name.startswith(DOCKER) and not step.get("entrypoint") and args[:1] == ["build"]
    if name.startswith(DOCKER) and not step.get("entrypoint") and not is_build:
        fail(f"step {sid} runs `docker {' '.join(args[:1])}`, which is not a build; a local build-only run builds and does nothing else")
    parsed.append((sid, step, name, args, is_build))

# The tags a later step is built FROM: those are exported, the rest are not.
from_refs = set()
for _, _, _, args, is_build in parsed:
    if is_build:
        for arg in flag(args, "--build-arg"):
            if arg.startswith("BASE_IMAGE="):
                from_refs.add(arg.split("=", 1)[1])

exported = {}
section = [f"### {target}", ""]
for sid, step, name, args, is_build in parsed:
    cwd = os.path.join(repo, sub(step.get("dir", "")))
    if not is_build:
        cmd = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
               "-v", f"{repo}:/workspace", "-w", "/workspace/" + sub(step.get("dir", ""))]
        for env in step.get("env") or []:
            cmd += ["-e", sub(env)]
        if step.get("entrypoint"):
            cmd += ["--entrypoint", sub(step["entrypoint"])]
        cmd += [name, *args]
        if stream(cmd, repo, sid) != 0:
            fail(f"step {sid} failed")
        section.append(f"- step `{sid}` ran in `{name}`")
        continue

    rest = [workspace(a) for a in args[1:]]
    tag = (flag(rest, "-t") or flag(rest, "--tag") or [""])[0]
    extra = []
    for j, arg in enumerate(rest):
        if rest[j - 1] == "--build-arg" and arg.startswith("BASE_IMAGE="):
            ref = arg.split("=", 1)[1]
            if ref not in exported:
                fail(f"step {sid} is built FROM {ref}, which no earlier step of this run exported")
            layout, digest = exported[ref]
            pinned = f"{repo_of(ref)}@{digest}"
            rest[j] = f"BASE_IMAGE={pinned}"
            extra += ["--build-context", f"{pinned}=oci-layout://{layout}@{digest}"]
    dockerfile = (flag(rest, "-f") or flag(rest, "--file") or [os.path.join(rest[-1], "Dockerfile")])[0]
    dockerfile = dockerfile if os.path.isabs(dockerfile) else os.path.join(cwd, dockerfile)
    rest = rest[:-1] + upstream_args(dockerfile, rest) + rest[-1:]

    if tag in from_refs:
        layout = os.path.join(state, safe(tag) + ".oci")
        meta = os.path.join(state, safe(tag) + ".json")
        if os.path.exists(os.path.join(state, safe(tag) + ".failed")):
            fail(f"step {sid}: {tag} already failed to build earlier in this run")
        if os.path.exists(meta):
            with open(meta) as fh:
                exported[tag] = (layout, json.load(fh)["containerimage.digest"])
            print(f"{tag}: built earlier in this run; reusing {exported[tag][1]}", flush=True)
            section.append(f"- step `{sid}`: `{tag}` was built earlier in this run (its steps are listed there)")
            continue
        out = ["--output", f"type=oci,dest={layout},tar=false", "--metadata-file", meta]
    elif SCAN and tag:
        # Scanned below, so exported -- to a layout of its own, removed after the scan.
        layout = os.path.join(state, safe(tag) + ".scan.oci")
        out = ["--output", f"type=oci,dest={layout},tar=false"]
    else:
        out = ["--output", "type=cacheonly"]
    cmd = ["docker", "buildx", "build", "--builder", builder, "--progress=plain", "--no-cache",
           "--provenance=false", *out, *extra, *rest]
    rc = stream(cmd, cwd, sid)
    if rc != 0:
        if tag in from_refs:
            open(os.path.join(state, safe(tag) + ".failed"), "w").close()
        fail(f"step {sid} failed (exit {rc})")
    if tag in from_refs:
        with open(meta) as fh:
            exported[tag] = (layout, json.load(fh)["containerimage.digest"])
    if SCAN and tag:
        print(f"{tag}: scanning with {SCAN}", flush=True)
        scan_rc = stream([SCAN, layout, tag], cwd, sid)
        if tag not in from_refs:
            shutil.rmtree(layout, ignore_errors=True)
        if scan_rc != 0:
            section.append(f"- step `{sid}`: **`{tag}` was refused by the image scan** (exit {scan_rc}); its log says which findings")
            fail(f"step {sid}: {tag} built, and the image scan refused it (exit {scan_rc}): a release would refuse it too")
        section.append(f"- step `{sid}`: `{tag}` passed the image scan the release runs")
    shown = os.path.relpath(dockerfile, repo)
    steps_ran = run_steps(dockerfile)
    if steps_ran is None:
        section.append(f"- step `{sid}` built `{shown}`, which is gone after the build, so its RUN steps are not listed")
        continue
    section.append(f"- step `{sid}` built `{shown}`, running its {len(steps_ran)} RUN step(s):")
    section += [f"  - `{shown}:{n}` `{text[:160]}`" for n, text in steps_ran]

with open(report, "a") as fh:
    fh.write("\n".join(section) + "\n\n")
PY
)"

build_locally() {
  local i target log why state report python builder rc failed=() notes=()
  local prereq t df syntax need_mirror=0
  python="${SWARM_PYTHON:-python3}"
  "${python}" -c 'import yaml' 2>/dev/null \
    || die "--local reads each build config with PyYAML; ${python} cannot import yaml (set SWARM_PYTHON to one that can)"
  docker buildx version >/dev/null 2>&1 \
    || die "--local builds with docker buildx, which this docker does not have"
  # A docker-container builder: the docker driver cannot export an OCI
  # layout, which is how a derived image is built FROM a base never pushed.
  builder="${BUILD_ONLY_BUILDER:-swarm-build-only}"
  if ! docker buildx inspect "${builder}" >/dev/null 2>&1; then
    docker buildx create --name "${builder}" --driver docker-container --bootstrap >/dev/null \
      || die "could not create the buildx builder ${builder}"
  fi
  state="${BUILD_DIR}/build-only/local"
  report="${BUILD_DIR}/build-only/local-report.md"
  log="${BUILD_DIR}/build-logs/${TAG}/local"
  rm -rf "${state}"
  mkdir -p "${state}" "${log}"
  printf '%s\n\n' "Built at ${TAG} with docker buildx, uncached, pushing nothing. Every RUN step listed ran and passed unless its image is marked FAILED." >"${report}"

  # The tool mirror, from upstream: this runner can read no bucket. The same
  # files at the same sha256 the Cloud Build step checks (lib/build-mirror.sh),
  # and nothing is built if one fails.
  for target in "${T_NAME[@]}"; do
    prereq="$(build_after "${target}")"
    for t in "${target}" ${prereq:+"${prereq}"}; do
      df="$(recipe_dockerfile "${t}")" || continue
      if dockerfile_uses_tool_mirror "${df}"; then need_mirror=1; fi
    done
  done
  if [[ "${need_mirror}" -eq 1 ]]; then
    tool_mirror_stage_upstream
  fi

  step "Build ${#T_NAME[@]} image build(s) here, one at a time, pushing nothing"
  for ((i = 0; i < ${#T_NAME[@]}; i++)); do
    target="${T_NAME[$i]}"
    why="${state}/${target}.why"
    info "${target}: building $(basename "${T_CONFIG[$i]}") with docker buildx"
    rc=0
    # A checked-in recipe's mirror substitutions (recipe_mirror_subs) name
    # the upstream registries here, and its frontend as the Dockerfile
    # writes it: this runner reads no mirror.
    syntax=""
    if df="$(recipe_dockerfile "${target}")"; then syntax="$(dockerfile_syntax_ref "${df}")"; fi
    LOCAL_SUBS="$(jq -cn --arg i "${LOCAL_IMAGE}/${target}" --arg t "${TAG}" --arg n "${target}" --arg a "${APT_REFRESH}" \
      --arg s "${syntax}" --argjson u "${MIRROR_UPSTREAMS_JSON}" \
      '{_IMAGE: $i, _TAG: $t, _TARGET: $n, _APT_REFRESH: $a, _BUILDKIT_SYNTAX: $s}
       + ($u | to_entries | map({("_" + .key): .value.upstream}) | add)')" \
      MIRROR_UPSTREAMS_JSON="${MIRROR_UPSTREAMS_JSON}" \
      "${python}" -c "${LOCAL_REPLAY}" "${T_CONFIG[$i]}" "${REPO_ROOT}" "${target}" "${builder}" \
        "${state}" "${report}" "${why}" 2>&1 \
      | redact | tee "${log}/${target}.log" | awk -v p="[${target}] " '{ print p $0; fflush() }' >&2 \
      || rc=$?
    if [[ "${rc}" -eq 0 ]]; then
      ok "${target}: built, nothing pushed"
    else
      failed+=("${target}")
      notes+=("${target} ($( [[ -s "${why}" ]] && cat "${why}" || printf 'exit %s' "${rc}"))")
      printf '### %s: FAILED\n\n%s\n\n' "${target}" "$( [[ -s "${why}" ]] && cat "${why}" || printf 'exit %s' "${rc}")" >>"${report}"
      err "${target}: FAILED; output in ${log#"${REPO_ROOT}/"}/${target}.log"
    fi
  done
  hr
  if [[ "${#failed[@]}" -gt 0 ]]; then
    die "${#failed[@]} of ${#T_NAME[@]} image build(s) failed: ${notes[*]}"
  fi
  ok "built ${#T_NAME[@]} image build(s) at ${TAG} and pushed nothing: no registry, no login, no manifest"
}

if [[ "${LOCAL_BUILD}" -eq 1 ]]; then
  build_locally
  exit 0
fi

# ---------------------------------------------------------------------------
# No credential file goes up with the source.
# ---------------------------------------------------------------------------
# `gcloud builds submit "${REPO_ROOT}"` uploads the checkout. In release.yml
# and application.yml, google-github-actions/auth has already written
# gha-creds-<16 hex>.json into it (it writes to $GITHUB_WORKSPACE on purpose,
# so later steps can read it), and that file is a live credential for as long
# as the job runs. Nothing kept it out: there was no .gcloudignore, and the one
# gcloud generates follows .gitignore, which did not list it. So it went up
# with every build's source.
#
# .gcloudignore now excludes it. THIS is what makes losing that rule a refused
# build instead of a leaked credential, and it asks gcloud itself what it would
# upload -- so it checks the rule gcloud applies, not a restatement of it.
#
# It runs only when such a file exists, because `meta list-files-for-upload`
# is documented as internal ("may change or disappear without notice"). Where
# there is no credential to leak, a vanished command must not stop a build;
# where there is one, a listing that cannot be produced is a refusal.
if [[ "${#T_NAME[@]}" -gt 0 ]]; then
  CRED_FILES=()
  while IFS= read -r found; do
    [[ -n "${found}" ]] && CRED_FILES+=("${found#"${REPO_ROOT}/"}")
  done < <(find "${REPO_ROOT}" -name .git -prune -o -type f -name 'gha-creds-*.json' -print)

  if [[ "${#CRED_FILES[@]}" -gt 0 ]]; then
    step "Build source"
    UPLOAD_ERR="${BUILD_DIR}/upload-check.err"
    if ! UPLOAD_LIST="$(gcloud meta list-files-for-upload "${REPO_ROOT}" 2>"${UPLOAD_ERR}")"; then
      err "gcloud could not list what \`builds submit\` would upload:"
      redact <"${UPLOAD_ERR}" | sed -n '1,3s/^/     /p' >&2
      die "refusing to submit: cannot establish that ${CRED_FILES[*]} (a workflow credential file) stays out of the build source"
    fi
    LEAKED=()
    for cred in "${CRED_FILES[@]}"; do
      if grep -qxF -- "${cred}" <<<"${UPLOAD_LIST}"; then LEAKED+=("${cred}"); fi
    done
    if [[ "${#LEAKED[@]}" -gt 0 ]]; then
      err "the build source would include a workflow credential file; see the gha-creds-*.json rule in .gcloudignore"
      die "refusing to submit: the build source would upload ${LEAKED[*]}"
    fi
    ok "credential file ${CRED_FILES[*]} is in the checkout and excluded from the upload"
  fi
fi

# ---------------------------------------------------------------------------
# Submit, at most PARALLELISM at a time.
# ---------------------------------------------------------------------------

LOG_DIR="${BUILD_DIR}/build-logs/${TAG}"
mkdir -p "${LOG_DIR}"

count_state() {
  local want="$1" n=0 s
  for s in ${T_STATE[@]+"${T_STATE[@]}"}; do
    if [[ "${s}" == "${want}" ]]; then n=$((n + 1)); fi
  done
  printf '%s' "${n}"
}

elapsed() {
  local now secs
  now="$(date +%s)"
  secs=$((now - $1))
  printf '%dm%02ds' $((secs / 60)) $((secs % 60))
}

# The Cloud Build id from a submit's output, when it got far enough to have one.
build_id_in() {
  [[ -f "$1" ]] || return 0
  awk 'match($0, /\/builds\/[0-9a-f-]+]/) { print substr($0, RSTART + 8, RLENGTH - 9); exit }' "$1"
}

# Print one build's output with its image's name on every line. On GitHub
# Actions a successful build's output is folded into a group; a failure's is
# left open, because that is the one someone came to read. Prefixing also keeps
# any line of build output from being read by the runner as a workflow command.
show_log() {
  local target="$1" log="$2" fold="$3"
  if [[ ! -s "${log}" ]]; then dim "[${target}] (no output)"; return 0; fi
  if [[ "${fold}" == fold && "${GITHUB_ACTIONS:-}" == "true" ]]; then
    printf '::group::%s build output\n' "${target}" >&2
  fi
  awk -v p="[${target}] " '{ print p $0 }' "${log}" >&2
  if [[ "${fold}" == fold && "${GITHUB_ACTIONS:-}" == "true" ]]; then
    printf '::endgroup::\n' >&2
  fi
}

# Whether gcloud's output in a file is Cloud Build refusing on QUOTA: the 429
# RESOURCE_EXHAUSTED that main's image build failed on 27 times on 2026-10-20
# and 2026-10-21 (cloudbuild.googleapis.com/get_requests, shared with the other
# team in saga-agents-staging). gcloud prints the HTTP status and the API's
# status name; either is enough, and nothing else prints them.
quota_exhausted() {
  [[ -s "$1" ]] || return 1
  grep -Eq -e 'RESOURCE_EXHAUSTED' -e '"code": *429' -e "'status': '429'" -e 'HTTP(/[0-9.]+)? 429' "$1"
}

# Seconds to wait before attempt $1+1: QUOTA_BACKOFF doubled per attempt,
# capped at QUOTA_BACKOFF_MAX, scaled by a random 50-100%. $2 salts the random
# number, because a backgrounded subshell may start from its parent's seed and
# builds retrying in step would meet the next quota minute together.
quota_delay() {
  awk -v b="${QUOTA_BACKOFF}" -v m="${QUOTA_BACKOFF_MAX}" -v a="$1" \
      -v r="$(( (RANDOM + ${2:-0} * 7919) % 32768 ))" \
      'BEGIN { d = b * 2 ^ (a - 1); if (d > m) d = m; printf "%.2f", d * (0.5 + r / 65534) }'
}

# Run a gcloud READ ($4...) with stdout to $2 and stderr to $3, retrying a 429
# with backoff and jitter up to QUOTA_RETRIES attempts, each logged with its
# attempt number. Any other failure returns at once with gcloud's exit status,
# as it always did; a 429 that outlasts every attempt returns non-zero too, and
# the caller -- which can tell the two apart with quota_exhausted "$3" -- leaves
# that build for the next round rather than failing it.
gcloud_quota_retry() {
  local label="$1" out="$2" errf="$3" attempt=1 rc delay
  shift 3
  while :; do
    rc=0
    "$@" >"${out}" 2>"${errf}" </dev/null || rc=$?
    if [[ "${rc}" -eq 0 ]] || ! quota_exhausted "${errf}"; then return "${rc}"; fi
    if [[ "${attempt}" -ge "${QUOTA_RETRIES}" ]]; then
      warn "${label}: still 429 RESOURCE_EXHAUSTED after ${attempt} attempts"
      return "${rc}"
    fi
    delay="$(quota_delay "${attempt}" 0)"
    warn "${label}: Cloud Build answered 429 RESOURCE_EXHAUSTED (attempt ${attempt}/${QUOTA_RETRIES}); retrying in ${delay}s"
    sleep "${delay}"
    attempt=$((attempt + 1))
  done
}

launch_build() {
  local i="$1"
  local target="${T_NAME[$i]}" config="${T_CONFIG[$i]}" subs="${T_SUBS[$i]}"
  local log="${LOG_DIR}/${T_NAME[$i]}.log" status="${LOG_DIR}/${T_NAME[$i]}.status"
  local sub_args=()
  [[ -z "${subs}" ]] || sub_args=(--substitutions "${subs}")
  rm -f "${log}" "${log}.attempt" "${status}" "${status}.tmp"
  T_BUILD_ID[i]=""
  T_UNREAD[i]=0

  # The subshell's LAST act is to write its exit status, renamed into place so
  # the scheduler never reads half a file. stdin is closed: a background job
  # that reads the terminal is stopped by it.
  #
  # The submit is --async (SUBMIT_ARGS), so this is the upload and the create,
  # and the build is watched by read_build_states. A submit refused with a 429
  # is submitted again after a backoff, up to QUOTA_RETRIES attempts -- unless
  # its output already names a build id: then Cloud Build created the build
  # before something answered 429, a second submit would build the image
  # twice, and the build is watched instead.
  (
    attempt=1
    while :; do
      submit_rc=0
      gcloud builds submit "${REPO_ROOT}" \
        "${SUBMIT_ARGS[@]}" \
        --config "${config}" \
        ${sub_args[@]+"${sub_args[@]}"} \
        </dev/null 2>&1 | redact >"${log}.attempt" || submit_rc=$?
      cat "${log}.attempt" >>"${log}"
      if [[ "${submit_rc}" -eq 0 ]] || ! quota_exhausted "${log}.attempt"; then break; fi
      if [[ -n "$(build_id_in "${log}.attempt")" ]]; then submit_rc=0; break; fi
      if [[ "${attempt}" -ge "${QUOTA_RETRIES}" ]]; then
        warn "${target}: the submit was still refused with 429 RESOURCE_EXHAUSTED after ${attempt} attempts"
        break
      fi
      delay="$(quota_delay "${attempt}" "${i}")"
      warn "${target}: the submit was refused with 429 RESOURCE_EXHAUSTED (attempt ${attempt}/${QUOTA_RETRIES}); submitting again in ${delay}s"
      sleep "${delay}"
      attempt=$((attempt + 1))
    done
    rm -f "${log}.attempt"
    printf '%s\n' "${submit_rc}" >"${status}.tmp"
    mv -f "${status}.tmp" "${status}"
  ) &
  T_PID[i]=$!
  T_START[i]="$(date +%s)"
  T_STATE[i]=running
  info "${target}: submitted to Cloud Build (${CLOUDBUILD_REGION}), tag ${TAG}; output in ${log#"${REPO_ROOT}/"}"
}

# Whether a failed build's output shows a pull that NEVER GOT AN ANSWER.
#
# Owner decision, 2026-09-30, after the build images job failed one image with
#   Get "https://ghcr.io/v2/.../manifests/sha256:..": dial tcp ...:443: i/o timeout
# while pulling its base image: an image whose build failed because a registry
# never answered is rebuilt ONCE; every other failure fails as it did.
#
# WHY ONLY THESE STRINGS. Each is Go's net package (docker and buildkit are Go)
# reporting that the connection itself failed -- the dial did not complete
# (`dial tcp`, `i/o timeout`) or the TLS handshake did not (`TLS handshake
# timeout`). `connection reset` is deliberately left out: a reset can follow a
# connection that succeeded, and the owner's rule (2026-09-30) is to retry only
# when no answer came back at all. None of these can come
# from a registry that answered: a missing tag is `manifest unknown`, a refused
# pull is `denied`/`unauthorized`, and a broken Dockerfile is a RUN step's exit
# code. Rebuilding those would spend a second build to be told the same thing
# and would report a real defect minutes later than it could have.
pull_got_no_answer() {
  [[ -s "$1" ]] || return 1
  grep -qF -e 'i/o timeout' -e 'dial tcp' -e 'TLS handshake timeout' "$1"
}

finish_build() {
  local i="$1" rc="$2" why="${3:-}"
  local target="${T_NAME[$i]}" log="${LOG_DIR}/${T_NAME[$i]}.log"
  local took id
  took="$(elapsed "${T_START[$i]}")"
  id="$(build_id_in "${log}")"
  # A status that is not a number is a failure. `[[ x -eq 0 ]]` would read a
  # non-number as the arithmetic value 0 and call it a success.
  [[ "${rc}" =~ ^[0-9]+$ ]] || rc=1
  if [[ "${rc}" -ne 0 && -z "${T_RETRIED[$i]}" && -z "${ABORT}" ]] \
    && pull_got_no_answer "${log}"; then
    # The rebuild tarballs the working directory again, so the dirty-tree
    # guard every submission passes is passed by this one too.
    if git_dirty && [[ -z "${ALLOW_DIRTY_BUILD:-}" ]]; then
      why="${why:+${why}, }not rebuilt after a pull with no answer: the working tree went dirty"
    else
      T_RETRIED[i]=1
      warn "${target}: failed after ${took} on a pull that got no answer (exit ${rc}${id:+, build ${id}}); rebuilding it once"
      show_log "${target}" "${log}" open
      mv -f "${log}" "${log%.log}.first-attempt.log"
      launch_build "${i}"
      return 0
    fi
  fi
  if [[ "${rc}" -eq 0 ]]; then
    T_STATE[i]=ok
    ok "${target}: built in ${took}${id:+ (build ${id})}"
    show_log "${target}" "${log}" fold
  else
    T_STATE[i]=failed
    T_NOTE[i]="exit ${rc}${why:+, ${why}}${id:+, build ${id}}; output in ${log#"${REPO_ROOT}/"}"
    err "${target}: FAILED after ${took} (${T_NOTE[$i]})"
    show_log "${target}" "${log}" open
  fi
}

# Whether any build has been created and is waiting on read_build_states.
watching_any() {
  local i
  for ((i = 0; i < ${#T_NAME[@]}; i++)); do
    if [[ "${T_STATE[$i]}" == running && -n "${T_BUILD_ID[$i]}" ]]; then return 0; fi
  done
  return 1
}

# A submit came back with exit status $2. A refused one is a failed build, as
# it always was; with the script's own --async the build counts as done once it
# is created; otherwise the build it created is watched from here on.
submit_returned() {
  local i="$1" rc="$2" id
  if [[ "${rc}" != 0 || "${ASYNC}" -eq 1 ]]; then
    finish_build "${i}" "${rc}"
    return 0
  fi
  id="$(build_id_in "${LOG_DIR}/${T_NAME[$i]}.log")"
  if [[ -z "${id}" ]]; then
    finish_build "${i}" 1 "the submit printed no build id, so its build cannot be watched"
    return 0
  fi
  if ! watching_any; then NEXT_STATUS_AT=$(( $(date +%s) + STATUS_INTERVAL )); fi
  T_BUILD_ID[i]="${id}"
  info "${T_NAME[$i]}: build ${id} created; its status is read every ${STATUS_INTERVAL}s with the others"
}

reap_finished() {
  local i rc status
  for ((i = 0; i < ${#T_NAME[@]}; i++)); do
    [[ "${T_STATE[$i]}" == running && -z "${T_BUILD_ID[$i]}" ]] || continue
    status="${LOG_DIR}/${T_NAME[$i]}.status"
    if [[ -f "${status}" ]]; then
      rc="$(tr -d '[:space:]' <"${status}")"
      wait "${T_PID[$i]}" 2>/dev/null || true
      submit_returned "${i}" "${rc}"
    elif ! kill -0 "${T_PID[$i]}" 2>/dev/null; then
      # Gone. It may have written its status between the two checks above; if
      # so, the next pass reads it. If not, it was killed before it could.
      if [[ -f "${status}" ]]; then continue; fi
      rc=0
      wait "${T_PID[$i]}" 2>/dev/null || rc=$?
      [[ "${rc}" -ne 0 ]] || rc=1
      finish_build "${i}" "${rc}" "the submit exited without reporting a status"
    fi
  done
}

# Append build $1's Cloud Build log to its log file, redacted, once it has
# ended: a blocking submit streamed it, an --async one does not, and a failure
# is read there -- by the operator, and by pull_got_no_answer.
fetch_build_log() {
  local i="$1"
  local log="${LOG_DIR}/${T_NAME[$i]}.log" out="${LOG_DIR}/.build-log.out" errf="${LOG_DIR}/.build-log.err"
  if gcloud_quota_retry "${T_NAME[$i]}: build ${T_BUILD_ID[$i]} log" "${out}" "${errf}" \
       gcloud builds log "${T_BUILD_ID[$i]}" --project "${PROJECT_ID}" --region "${CLOUDBUILD_REGION}"; then
    redact <"${out}" >>"${log}"
  else
    printf '(the build log could not be read: %s)\n' "$(redact <"${errf}" | sed -n 1p)" >>"${log}"
  fi
  rm -f "${out}" "${errf}"
}

# A round in which a 429 kept build $1's status from being read. It is not a
# failure -- the build is running on Cloud Build regardless -- until QUOTA_ROUNDS
# such rounds in a row, when the run stops waiting and says the status, not the
# build, is what could not be had.
status_unread() {
  local i="$1"
  T_UNREAD[i]=$(( T_UNREAD[i] + 1 ))
  if [[ "${T_UNREAD[$i]}" -ge "${QUOTA_ROUNDS}" ]]; then
    finish_build "${i}" 1 "its status stayed unreadable on 429 RESOURCE_EXHAUSTED for ${QUOTA_ROUNDS} rounds; the build itself may yet succeed"
  fi
}

# ONE status read for every build in flight, at most every STATUS_INTERVAL
# seconds (CB-429-RETRY). `builds list --ongoing` names every QUEUED or WORKING
# build in the region; a watched build it does not name is described once,
# which says how it ended (or that it is still being created), and its log is
# fetched. A 429 is retried by gcloud_quota_retry and, if it outlasts that,
# leaves the build for the next round. Any other error is what it was when the
# blocking submit hit it: the build's status cannot be had, and it fails --
# except the list, which is only a shortcut, and falls back to describing each
# build this round.
read_build_states() {
  local i id target state listed=1 now
  local out="${LOG_DIR}/.status.out" errf="${LOG_DIR}/.status.err"
  watching_any || return 0
  now="$(date +%s)"
  [[ "${now}" -ge "${NEXT_STATUS_AT}" ]] || return 0

  if ! gcloud_quota_retry "Cloud Build status" "${out}" "${errf}" \
       gcloud builds list --project "${PROJECT_ID}" --region "${CLOUDBUILD_REGION}" \
         --ongoing --format='value(id)'; then
    if quota_exhausted "${errf}"; then
      listed=quota
    else
      die_if_auth_failure "$(cat "${errf}")"
      warn "could not list the ongoing builds; describing each one this round:"
      redact <"${errf}" | sed -n '1,2s/^/     /p' >&2
      listed=0
    fi
  fi

  for ((i = 0; i < ${#T_NAME[@]}; i++)); do
    [[ "${T_STATE[$i]}" == running && -n "${T_BUILD_ID[$i]}" ]] || continue
    id="${T_BUILD_ID[$i]}"
    target="${T_NAME[$i]}"
    if [[ "${listed}" == quota ]]; then status_unread "${i}"; continue; fi
    if [[ "${listed}" == 1 ]] && grep -qxF -- "${id}" "${out}"; then T_UNREAD[i]=0; continue; fi
    if ! gcloud_quota_retry "${target}: build ${id} status" "${out}.one" "${errf}.one" \
         gcloud builds describe "${id}" --project "${PROJECT_ID}" --region "${CLOUDBUILD_REGION}" \
           --format='value(status)'; then
      if quota_exhausted "${errf}.one"; then status_unread "${i}"; continue; fi
      die_if_auth_failure "$(cat "${errf}.one")"
      finish_build "${i}" 1 "its status could not be read: $(redact <"${errf}.one" | sed -n 1p)"
      continue
    fi
    T_UNREAD[i]=0
    state="$(tr -d '[:space:]' <"${out}.one")"
    case "${state}" in
      SUCCESS)
        fetch_build_log "${i}"
        finish_build "${i}" 0 ;;
      QUEUED|WORKING|PENDING|STATUS_UNKNOWN|"") ;;
      *)
        fetch_build_log "${i}"
        finish_build "${i}" 1 "Cloud Build reports ${state}" ;;
    esac
  done
  rm -f "${out}" "${errf}" "${out}.one" "${errf}.one"

  # From the whole second the round ENDED in, plus one: `date +%s` rounds down,
  # so without it two rounds could land milliseconds apart across a second.
  now="$(date +%s)"
  if [[ "${STATUS_INTERVAL}" -gt 0 ]]; then
    NEXT_STATUS_AT=$(( now + STATUS_INTERVAL + 1 ))
  else
    NEXT_STATUS_AT="${now}"
  fi
}

# Background jobs IGNORE SIGINT when job control is off, which it is in a
# script, so without this a Ctrl-C would end the scheduler and leave every
# in-flight `gcloud builds submit` uploading on its own. Stopping them locally
# does NOT cancel the builds: Cloud Build runs them to completion regardless,
# which is said rather than left to be discovered.
on_interrupt() {
  local i
  for ((i = 0; i < ${#T_NAME[@]}; i++)); do
    [[ "${T_STATE[$i]}" == running ]] || continue
    pkill -TERM -P "${T_PID[$i]}" 2>/dev/null || true
    kill -TERM "${T_PID[$i]}" 2>/dev/null || true
    warn "${T_NAME[$i]}: stopped watching; the Cloud Build itself carries on (gcloud builds list --ongoing --region ${CLOUDBUILD_REGION})"
  done
  exit 130
}
trap on_interrupt INT TERM

if [[ "${ASYNC}" -eq 1 ]]; then
  # --async returns once a build is QUEUED, so "the base has finished" cannot be
  # waited for. Said up front, for every image this run builds FROM another it
  # also builds (agent-runtime-browser and agent-runtime-indexer, #625), rather
  # than discovered in that image's build log.
  for async_target in ${T_NAME[@]+"${T_NAME[@]}"}; do
    async_base="$(build_after "${async_target}")"
    if [[ -n "${async_base}" ]] && row_of "${async_base}" >/dev/null; then
      warn "--async: ${async_target} is submitted once ${async_base} is QUEUED, not built; it fails unless ${async_base}:${TAG} already exists"
    fi
  done
fi

if [[ "${#T_NAME[@]}" -gt 0 ]]; then
  step "Build ${#T_NAME[@]} image(s), at most ${PARALLELISM} at a time"
fi

ABORT=""
NEXT_STATUS_AT=0
while [[ "${#T_NAME[@]}" -gt 0 ]]; do
  reap_finished
  read_build_states

  launched=0
  for ((i = 0; i < ${#T_NAME[@]}; i++)); do
    [[ "${T_STATE[$i]}" == pending ]] || continue
    target="${T_NAME[$i]}"

    if [[ -n "${ABORT}" ]]; then
      T_STATE[i]=skipped; T_NOTE[i]="${ABORT}"
      continue
    fi

    prereq="$(build_after "${target}")"
    if [[ -n "${prereq}" ]] && p="$(row_of "${prereq}")"; then
      case "${T_STATE[$p]}" in
        ok) ;;
        failed|skipped)
          T_STATE[i]=skipped
          T_NOTE[i]="not submitted: it is built FROM ${prereq}, which did not build"
          err "${target}: ${T_NOTE[$i]}"
          continue ;;
        *) continue ;;   # the prerequisite is still pending or running
      esac
    fi

    [[ "$(count_state running)" -lt "${PARALLELISM}" ]] || continue

    # RE-CHECKED BEFORE EVERY SUBMISSION, and it stops every submission after
    # it. `gcloud builds submit` tarballs the working DIRECTORY, not a commit,
    # and the submissions of one run are spread over minutes. A single check
    # before the first proves nothing about a later one: on 2026-09-22 this
    # script started against a clean tree at 23:20, a merge began at 23:21:20,
    # and swarm-ui's tarball went up at 23:28:23 carrying half-merged sources
    # under a tag naming a commit that did not contain them. The build failed,
    # which was luck -- a green one would have published an image whose tag was
    # a lie, and nothing downstream could have detected it.
    if git_dirty && [[ -z "${ALLOW_DIRTY_BUILD:-}" ]]; then
      ABORT="not submitted: the working tree went dirty during the run, so ${IMAGE_REPO}/<image>:${TAG} would be tagged with a commit it does not contain. Re-run from a clean tree, or set ALLOW_DIRTY_BUILD=1 to build uncommitted work on purpose"
      err "${ABORT}"
      T_STATE[i]=skipped; T_NOTE[i]="${ABORT}"
      continue
    fi

    if [[ "${launched}" -gt 0 ]]; then sleep "${SUBMIT_STAGGER}"; fi
    launch_build "${i}"
    launched=$((launched + 1))
  done

  pending="$(count_state pending)"
  running="$(count_state running)"
  if [[ "${pending}" -eq 0 && "${running}" -eq 0 ]]; then break; fi
  if [[ "${running}" -eq 0 && "${launched}" -eq 0 ]]; then
    # Nothing in flight and nothing startable: only a cycle in build_after can
    # do that. Refuse rather than spin.
    die "${pending} image(s) can never be submitted: build_after() orders them after each other"
  fi
  sleep "${POLL_INTERVAL}"
done

FAILED_BUILDS=()
for ((i = 0; i < ${#T_NAME[@]}; i++)); do
  case "${T_STATE[$i]}" in
    ok)      BUILT+=("${T_NAME[$i]}") ;;
    skipped) SKIPPED+=("${T_NAME[$i]}") ;;
    *)       FAILED_BUILDS+=("${T_NAME[$i]}") ;;
  esac
done

if [[ "${#FAILED_BUILDS[@]}" -gt 0 || "${#SKIPPED[@]}" -gt 0 ]]; then
  hr
  err "not every image built, so no manifest is written:"
  for ((i = 0; i < ${#T_NAME[@]}; i++)); do
    [[ "${T_STATE[$i]}" != ok ]] || continue
    printf '     %-22s %s\n' "${T_NAME[$i]}" "${T_NOTE[$i]}" >&2
  done
  summary="${#FAILED_BUILDS[@]} of ${#T_NAME[@]} image build(s) failed"
  [[ "${#FAILED_BUILDS[@]}" -eq 0 ]] || summary="${summary}: ${FAILED_BUILDS[*]}"
  [[ "${#SKIPPED[@]}" -eq 0 ]] || summary="${summary}; not submitted: ${SKIPPED[*]}"
  die "${summary}"
fi

if [[ "${BUILD_ONLY}" -eq 1 ]]; then
  hr
  ok "built ${#BUILT[@]} image build(s) at ${TAG} and pushed nothing: no registry tag, no manifest"
  exit 0
fi

if [[ "${ASYNC}" -eq 1 ]]; then
  hr
  ok "submitted ${#BUILT[@]} build(s) asynchronously; digests are recorded by the next non-async run"
  exit 0
fi

step "Digests"
# This loop USED TO discard stderr and `continue` on an empty digest, which cost
# a real deploy on 2026-09-18: the operator's gcloud session expired during a
# 15-minute build, all six read-backs failed identically, and the script warned
# six times, wrote a manifest with an EMPTY images array, printed
# "ok built 6 image(s)" and exited 0. push-images.sh and deploy.sh consume that
# manifest, so a successful build produced an artifact that promised six images
# and listed none.
#
# Two rules now, and the second matters as much as the first:
#   1. stderr is CAPTURED, so a dead session is named as a dead session --
#      die_if_auth_failure is used correctly 140 lines above this, for the
#      repository probe, and was simply never applied here;
#   2. a digest that cannot be read is FATAL. There is no useful partial
#      manifest: one built image missing from it is a service that silently
#      keeps its old revision on the next deploy.
DIGEST_OUT="$(mktemp "${TMPDIR:-/tmp}/swarm-digest.XXXXXX")"
trap 'rm -f "${DIGEST_OUT}"' EXIT INT TERM

entries='[]'
MISSING=()
BUILT_AT="$(iso_now)"
for target in ${BUILT[@]+"${BUILT[@]}"} ${REUSED[@]+"${REUSED[@]}"}; do
  image="${IMAGE_REPO}/${target}"
  digest=""
  digest_err=""
  # stdout to a file, stderr into the substitution: a command substitution runs
  # in a subshell, so anything assigned inside one is lost to the caller.
  # `artifacts docker images describe` is NOT used here, though it is the
  # obvious command and was used until 2026-09-18. It returns vulnerability data
  # alongside the digest, so it calls Container Analysis and needs
  # `containeranalysis.occurrences.list` -- which the operator running a build
  # does not necessarily hold. The denial it produces names a permission that
  # has nothing to do with images, on a project id rendered as an opaque number,
  # and the old code turned that into "no digest could be read back".
  #
  # `tags list` answers the only question being asked -- which digest does this
  # tag point at -- from Artifact Registry alone.
  #
  # AND THE TAG IS MATCHED EXACTLY, in jq, because gcloud's `tag:X` filter is a
  # WORD match. Until 2026-09-24 application.yml's `build` job built every
  # image a second time on a push to main, tagged `pr-<run>-<sha>`, so
  # `tag:<sha>` found that build too -- and those tags are still in the
  # registry, so the exact match stays.
  # This read `value(version)` and `tr -d '[:space:]'`, which glued the two
  # digests into one: release run 35972131246 wrote all eight manifest entries
  # as `sha256:<release build>sha256:<pr build>`. The JSON carries full resource
  # paths, so the tag and the digest are the last segment of each.
  if digest_err="$(gcloud artifacts docker tags list "${image}" \
       --project "${PROJECT_ID}" --filter="tag:${TAG}" --format=json \
       2>&1 >"${DIGEST_OUT}")"; then
    digest="$(jq -r --arg t "${TAG}" '
        [ .[] | select((.tag // "" | split("/") | last) == $t)
              | (.version // "" | split("/") | last) ]
        | unique | if length == 1 then .[0] else "" end' "${DIGEST_OUT}" 2>/dev/null || true)"
  else
    # Exits here if the session is dead, so nothing below can misreport it as a
    # missing image.
    die_if_auth_failure "${digest_err}"
  fi
  if [[ -z "${digest}" ]]; then
    err "${target}: built, but its digest could not be read back"
    [[ -z "${digest_err}" ]] || printf '%s\n' "${digest_err}" | head -n 2 | sed 's/^/     /' >&2
    MISSING+=("${target}")
    continue
  fi
  # A reused image is described by the build that MADE its digest: the next
  # incremental build diffs from that commit and ages from that time.
  built_from="${COMMIT}"; built_at="${BUILT_AT}"; reused=false
  if in_list "${target}" ${REUSED[@]+"${REUSED[@]}"}; then
    row="$(plan_row "${target}")"
    if [[ "${digest}" != "${P_DIGEST[$row]}" ]]; then
      err "${target}: :${TAG} points at ${digest}, not the reused ${P_DIGEST[$row]}"
      MISSING+=("${target}")
      continue
    fi
    built_from="${P_FROM[$row]}"; built_at="${P_AT[$row]}"; reused=true
  fi
  printf '  %-22s %s%s\n' "${target}" "${digest}" "$([[ "${reused}" == false ]] || printf ' (reused, built from %s)' "${built_from:0:12}")" >&2
  # A reused digest's args equal these: plan_incremental rebuilt it otherwise.
  entries="$(jq -c --arg n "${target}" --arg i "${image}" --arg t "${TAG}" --arg d "${digest}" \
    --arg from "${built_from}" --arg at "${built_at}" --argjson reused "${reused}" \
    --argjson args "$(baked_build_args_json "${target}")" \
    --argjson apt "$(if upgrades_os_packages "${target}"; then jq -n --arg a "${APT_REFRESH}" '$a'; else echo null; fi)" \
    '. + [{name:$n, image:$i, tag:$t, digest:$d, ref:($i + "@" + $d),
           built_from:$from, built_at:$at, reused:$reused, build_args:$args, apt_refresh:$apt}]' <<<"${entries}")"
done

if [[ "${#MISSING[@]}" -gt 0 ]]; then
  die "no manifest written: ${#MISSING[@]} of $(( ${#BUILT[@]} + ${#REUSED[@]} )) image(s) built or reused but unreadable (${MISSING[*]-}). The images may well exist; what is not true is that this run can describe them, and a manifest that omits them would deploy the previous revision of each without saying so."
fi

# In ALL_TARGETS order whichever way each image was obtained, so a reader
# (and application.yml's step summary) sees the same list every time.
order="$(printf '%s\n' "${ALL_TARGETS[@]}" | jq -R . | jq -sc .)"
incremental='null'
if [[ -n "${INCREMENTAL}" ]]; then
  incremental="$(jq -cn --arg prev "${PREV_COMMIT}" --arg why "${FULL_BUILD_REASON}" \
    '{previous: (if $prev == "" then null else $prev end), full_build: (if $why == "" then null else $why end)}')"
fi
jq -n --arg tag "${TAG}" --arg at "${BUILT_AT}" --arg env "${ENVIRONMENT}" \
      --arg commit "${COMMIT}" --argjson images "${entries}" --argjson order "${order}" \
      --argjson incremental "${incremental}" '
   {tag:$tag, commit:$commit, built_at:$at, environment:$env,
    images: ($images | sort_by(.name as $n | ($order | index($n)) // 999))}
   + (if $incremental == null then {} else {incremental:$incremental} end)' >"${MANIFEST}"

hr
if [[ "${#REUSED[@]}" -gt 0 ]]; then
  ok "built ${#BUILT[@]} image(s) and reused ${#REUSED[@]} at tag ${TAG}: ${REUSED[*]}"
else
  ok "built ${#BUILT[@]} image(s) at tag ${TAG}"
fi
info "manifest: ${MANIFEST}"
