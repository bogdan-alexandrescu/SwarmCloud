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
# ONE BUILD PER COMMIT, and this script is the one path to it. application.yml
# runs it on every push to main and uploads the manifest it writes; release.yml
# runs it with --reuse-ci, which fetches THAT manifest instead of building
# (scripts/lib/ci-built-images.sh, and docs/ci.md for the decision):
#
#   --reuse-ci only       use what CI built for this commit, waiting for it if
#                         it is still building; fail if CI never built it
#   --reuse-ci or-build   the same, but build here when CI never built this
#                         commit for this environment -- a release dispatched
#                         by hand, or any prod release (swarm-ui bakes its
#                         environment in, and CI builds for dev)
#
# Either way the result is build/images-<env>.json, so what follows cannot tell
# a reused build from a fresh one -- and a fresh one is this same script, with
# the same tag, not a second copy of it.
#
# Usage: scripts/build-images.sh [TARGET...] [--tag SHA] [--parallel N]
#                                [--create-repo] [--async] [--digests-only]
#        scripts/build-images.sh --reuse-ci only|or-build
#        TARGET defaults to every image the repo knows how to build.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ALL_TARGETS=(agent-runtime-base agent-runtime-browser swarm-api swarm-scheduler swarm-quota-broker swarm-reconciler swarm-ui swarm-verify)
TARGETS=()
TAG=""
CREATE_REPO=0
ASYNC=0
DIGESTS_ONLY=0
REUSE_CI=""

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
# cannot be submitted until agent-runtime-base has FINISHED, so the critical
# path is two builds long whatever the bound; four slots fit the other six
# images inside that path, and eight would only finish at the same time.
PARALLELISM="${BUILD_PARALLELISM:-4}"
# How often the scheduler looks for finished builds. A build takes minutes.
POLL_INTERVAL="${BUILD_POLL_INTERVAL:-2}"
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
    -h|--help)     sed -n '2,39p' "$0"; exit 0 ;;
    -*)            die "unknown flag: $1" ;;
    *)             TARGETS+=("$1"); shift ;;
  esac
done

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
[[ "${#TARGETS[@]}" -gt 0 ]] || TARGETS=("${ALL_TARGETS[@]}")
[[ "${PARALLELISM}" =~ ^[1-9][0-9]*$ ]] \
  || die "--parallel (or BUILD_PARALLELISM) must be a positive whole number, not '${PARALLELISM}'"

# NOTE: the only bash on the reference workstation is 3.2.57 (Apple ships no
# newer one). Empty arrays are therefore always expanded as ${arr[@]+"${arr[@]}"},
# because plain "${arr[@]}" on an empty array is an unbound-variable error under
# `set -u` in 3.2. No associative arrays, no mapfile, no ${var,,} anywhere.
# The scheduler below cannot use bash 4.3's "wait for any child" either, so a
# finished build is found by the status file it writes when it exits.

require_cmd gcloud jq
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
# Reuse what CI built for this commit, rather than build it a second time.
# ---------------------------------------------------------------------------
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
      hr
      ok "reused CI's build of ${COMMIT}: no Cloud Build submitted"
      info "manifest: ${BUILD_DIR}/images-${ENVIRONMENT}.json"
      exit 0 ;;
    3)
      # The same build CI would have made: same targets, same tag, same
      # manifest. Nothing below knows it was reached this way.
      info "building ${COMMIT} here, as CI would have: every image, tag ${TAG}" ;;
    *)
      exit "${reuse_rc}" ;;
  esac
fi

# Advisory only. The check that can actually stop a bad image is the one made
# before every submission below -- see the comment there for why a single check
# here is not enough.
if git_dirty; then
  warn "working tree is dirty; ${TAG} will not reproduce from git alone"
fi

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

# Find the build recipe for a target. Track B owns images/ and the service
# Dockerfiles, so both layouts are accepted rather than assumed.
find_recipe() {
  local target="$1" candidate
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

# The one image that must FINISH building before another may be SUBMITTED.
#
# images/agent-runtime-browser/cloudbuild.yaml pulls agent-runtime-base:<tag>
# in its first step and builds FROM that digest, so submitting it before the
# base has been pushed fails with manifest-unknown. When builds ran one at a
# time this was satisfied by ALL_TARGETS happening to list the base first --
# which a targeted `build-images.sh agent-runtime-browser agent-runtime-base`
# did not, and which concurrency would not either.
#
# Stated here, once. tests/unit/scripts/test_build_images_concurrency.py reads
# every recipe for images it pulls and fails if this function disagrees, so a
# new `FROM <another target>` cannot be added without it.
build_after() {
  case "$1" in
    agent-runtime-browser) printf '%s' "agent-runtime-base" ;;
    *) : ;;
  esac
}

# A Dockerfile with no cloudbuild.yaml gets a generated one. It is written to
# build/ and kept, so a failed build can be reproduced exactly.
generate_config() {
  local dockerfile="$1" image="$2" out="$3" target="${4:-}"

  # PER-TARGET BUILD ARGS. Only swarm-ui takes one, and it has to be a BUILD
  # arg rather than a Cloud Run env var: Vite inlines `import.meta.env.VITE_*`
  # when the bundle is compiled, so by the time a container starts, a static
  # bundle has already decided what environment it thinks it is in.
  #
  # ENVIRONMENT is exported by lib/common.sh, so this carries dev to a dev
  # build and prod to a prod one without a second place to keep in step.
  local extra_build_args=""
  if [[ "${target}" == "swarm-ui" ]]; then
    extra_build_args="      - --build-arg
      - VITE_SWARM_ENV=${ENVIRONMENT}
"
  fi

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
  - name: gcr.io/cloud-builders/docker
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
${extra_build_args}      - .
images:
  - ${image}:${TAG}
YAML
}

SUBMIT_ARGS=(--project "${PROJECT_ID}" --region "${CLOUDBUILD_REGION}")
if [[ -n "${CLOUDBUILD_SERVICE_ACCOUNT:-}" ]]; then
  SUBMIT_ARGS+=(--service-account="projects/${PROJECT_ID}/serviceAccounts/${CLOUDBUILD_SERVICE_ACCOUNT}")
fi
[[ "${ASYNC}" -eq 1 ]] && SUBMIT_ARGS+=(--async)

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
T_NAME=(); T_CONFIG=(); T_SUBS=(); T_STATE=(); T_PID=(); T_START=(); T_NOTE=()

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
      info "${target}: using checked-in ${path}"
      ;;
    dockerfile)
      config="${BUILD_DIR}/cloudbuild-${target}.yaml"
      generate_config "${path}" "${image}" "${config}" "${target}"
      info "${target}: generated $(basename "${config}") for ${path}"
      ;;
  esac

  T_NAME+=("${target}"); T_CONFIG+=("${config}"); T_SUBS+=("${subs}")
  T_STATE+=(pending); T_PID+=(""); T_START+=(0); T_NOTE+=("")
done

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

launch_build() {
  local i="$1"
  local target="${T_NAME[$i]}" config="${T_CONFIG[$i]}" subs="${T_SUBS[$i]}"
  local log="${LOG_DIR}/${T_NAME[$i]}.log" status="${LOG_DIR}/${T_NAME[$i]}.status"
  local sub_args=()
  [[ -z "${subs}" ]] || sub_args=(--substitutions "${subs}")
  rm -f "${log}" "${status}" "${status}.tmp"

  # The subshell's LAST act is to write its exit status, renamed into place so
  # the scheduler never reads half a file. stdin is closed: a background job
  # that reads the terminal is stopped by it.
  (
    submit_rc=0
    gcloud builds submit "${REPO_ROOT}" \
      "${SUBMIT_ARGS[@]}" \
      --config "${config}" \
      ${sub_args[@]+"${sub_args[@]}"} \
      </dev/null 2>&1 | redact >"${log}" || submit_rc=$?
    printf '%s\n' "${submit_rc}" >"${status}.tmp"
    mv -f "${status}.tmp" "${status}"
  ) &
  T_PID[i]=$!
  T_START[i]="$(date +%s)"
  T_STATE[i]=running
  info "${target}: submitted to Cloud Build (${CLOUDBUILD_REGION}), tag ${TAG}; output in ${log#"${REPO_ROOT}/"}"
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

reap_finished() {
  local i rc status
  for ((i = 0; i < ${#T_NAME[@]}; i++)); do
    [[ "${T_STATE[$i]}" == running ]] || continue
    status="${LOG_DIR}/${T_NAME[$i]}.status"
    if [[ -f "${status}" ]]; then
      rc="$(tr -d '[:space:]' <"${status}")"
      wait "${T_PID[$i]}" 2>/dev/null || true
      finish_build "${i}" "${rc}"
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

# Background jobs IGNORE SIGINT when job control is off, which it is in a
# script, so without this a Ctrl-C would end the scheduler and leave every
# in-flight `gcloud builds submit` polling on its own. Stopping them locally
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

if [[ "${ASYNC}" -eq 1 ]] && row_of agent-runtime-base >/dev/null && row_of agent-runtime-browser >/dev/null; then
  # --async returns once a build is QUEUED, so "the base has finished" cannot be
  # waited for. Said up front rather than discovered in the browser build log.
  warn "--async: agent-runtime-browser is submitted once agent-runtime-base is QUEUED, not built; it fails unless agent-runtime-base:${TAG} already exists"
fi

if [[ "${#T_NAME[@]}" -gt 0 ]]; then
  step "Build ${#T_NAME[@]} image(s), at most ${PARALLELISM} at a time"
fi

ABORT=""
while [[ "${#T_NAME[@]}" -gt 0 ]]; do
  reap_finished

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
for target in ${BUILT[@]+"${BUILT[@]}"}; do
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
  printf '  %-22s %s\n' "${target}" "${digest}" >&2
  entries="$(jq -c --arg n "${target}" --arg i "${image}" --arg t "${TAG}" --arg d "${digest}" \
    '. + [{name:$n, image:$i, tag:$t, digest:$d, ref:($i + "@" + $d)}]' <<<"${entries}")"
done

if [[ "${#MISSING[@]}" -gt 0 ]]; then
  die "no manifest written: ${#MISSING[@]} of ${#BUILT[@]} image(s) built but unreadable (${MISSING[*]-}). The images may well exist; what is not true is that this run can describe them, and a manifest that omits them would deploy the previous revision of each without saying so."
fi

jq -n --arg tag "${TAG}" --arg at "$(iso_now)" --arg env "${ENVIRONMENT}" \
      --arg commit "${COMMIT}" --argjson images "${entries}" \
   '{tag:$tag, commit:$commit, built_at:$at, environment:$env, images:$images}' >"${MANIFEST}"

hr
ok "built ${#BUILT[@]} image(s) at tag ${TAG}"
info "manifest: ${MANIFEST}"
