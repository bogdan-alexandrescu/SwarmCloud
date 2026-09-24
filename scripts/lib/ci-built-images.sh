#!/usr/bin/env bash
# The images CI already built for a commit: wait for that build if it is still
# running, fetch the record of what it built, and say plainly when there is
# nothing to reuse.
#
# OWNER DECISION, 2026-09-24. Every push to main built all eight images TWICE:
# application.yml's `build images` job, and release.yml's own build, competing
# for Cloud Build in a project whose build quota is shared with another team --
# and the release waited behind the duplicate. Measured on commit 94ee043:
# application.yml built it 16:33:09-16:41:51 (run 36027980122), and release
# 36027980448 built the same commit again 16:39:24-16:48:17 before it promoted
# anything. Now application.yml builds once per commit on main and uploads its
# build manifest (build/images-<env>.json: every image's digest, the tag, the
# commit and the environment it was built for) as the artifact
# `images-<env>`, and the release promotes exactly those digests.
#
# This script is how a release finds them. It answers one question -- did CI
# build THIS commit for THIS environment, and what did it build? -- and it
# never answers "no" when the truth is "I could not tell":
#
#   exit 0  yes. The build manifest is at --out, and it records this commit
#           and this environment.
#   exit 3  CI never built this commit for this environment, and
#           `--if-absent build` says the caller will build it. That is a
#           release dispatched by hand for a commit application.yml never
#           built, and build-images.sh then builds it exactly as CI would have.
#   exit 1  anything else, with the reason on the last line: CI's build
#           FAILED; it is still running after CI_BUILD_WAIT; CI never built
#           the commit and `--if-absent fail`; the GitHub API could not be
#           read; or the record describes another commit or environment.
#
# A FAILED BUILD IS NOT "NEVER BUILT", even under `--if-absent build`. The
# release would submit the same recipes from the same commit, and the failure
# would come back one Cloud Build later -- the duplicate this exists to remove.
# Re-run the failed job if it was a flake.
#
# "NEVER BUILT" means one of:
#   * no application.yml run for the commit on the branch appeared within
#     CI_BUILD_APPEAR seconds;
#   * its build job was cancelled (a newer push replaced the run while it was
#     still queued) or skipped (the checks it needs failed first);
#   * the build succeeded but recorded nothing for THIS environment. That is
#     every prod release: application.yml builds for dev, and swarm-ui bakes
#     its environment into the bundle when it is compiled (VITE_SWARM_ENV,
#     scripts/build-images.sh), so a dev build is not a prod build. It is also
#     a record older than the artifact's retention.
#
# A pull request's run of the same commit is never reused: it builds nothing
# (its OIDC ref cannot mint a token), and it is not the branch being released.
#
# Usage:
#   scripts/lib/ci-built-images.sh --sha <40 hex> --environment dev --out build/images-dev.json
#                                  [--if-absent fail|build] [--repo owner/name]
#                                  [--workflow application.yml] [--job 'build images']
#                                  [--branch main]
#
# Needs `gh` authenticated for the repository -- GH_TOKEN, with actions: read.
# CI_BUILD_WAIT (default 2700s) bounds the wait for a running build,
# CI_BUILD_APPEAR (default 300s) the wait for its run to appear at all, and
# CI_BUILD_POLL (default 30s) is the interval between looks.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SHA=""
ENV_NAME="${ENVIRONMENT}"
OUT=""
IF_ABSENT="fail"
REPO="${GITHUB_REPOSITORY:-}"
# The workflow and job that build on main. tests/unit/scripts/test_ci_built_images.py
# reads application.yml and fails if no job there answers to this name, so a
# rename cannot turn every release into "never built".
WORKFLOW="${CI_BUILD_WORKFLOW:-application.yml}"
JOB="${CI_BUILD_JOB:-build images}"
BRANCH="${CI_BUILD_BRANCH:-main}"
# 45 minutes. A build is ~9 minutes behind ~2 of checks, and on main the
# application run can be queued behind the previous commit's (its concurrency
# group does not cancel an in-progress run there), so the realistic worst case
# is ~25. Past 45 something is wrong, and the message says what was pending.
WAIT="${CI_BUILD_WAIT:-2700}"
# The run is created within seconds of the push that also started the release;
# five minutes is for GitHub having a slow day, not for anything expected.
APPEAR="${CI_BUILD_APPEAR:-300}"
# 30s: each look is two API calls, and the token's budget is 1,000 an hour for
# the repository. A 45-minute wait at this interval spends under 200.
POLL="${CI_BUILD_POLL:-30}"
# Consecutive failed reads before giving up. A transient 502 is retried; an API
# that stays unreadable is reported as unreadable, never as a missing build.
API_TRIES="${CI_BUILD_API_TRIES:-5}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha)         SHA="$2"; shift 2 ;;
    --environment) ENV_NAME="$2"; shift 2 ;;
    --out)         OUT="$2"; shift 2 ;;
    --if-absent)   IF_ABSENT="$2"; shift 2 ;;
    --repo)        REPO="$2"; shift 2 ;;
    --workflow)    WORKFLOW="$2"; shift 2 ;;
    --job)         JOB="$2"; shift 2 ;;
    --branch)      BRANCH="$2"; shift 2 ;;
    -h|--help)     sed -n '2,60p' "$0"; exit 0 ;;
    *)             die "unknown argument: $1" ;;
  esac
done

[[ "${SHA}" =~ ^[0-9a-f]{40}$ ]] \
  || die "--sha must be a full 40-character commit id, not '${SHA}': runs are found by it exactly"
[[ -n "${OUT}" ]] || die "--out FILE is required"
[[ -n "${ENV_NAME}" ]] || die "--environment is required"
case "${IF_ABSENT}" in
  fail|build) ;;
  *) die "--if-absent takes 'fail' or 'build', not '${IF_ABSENT}'" ;;
esac
[[ "${REPO}" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] \
  || die "no repository to ask: set GITHUB_REPOSITORY or pass --repo owner/name"
for knob in WAIT APPEAR API_TRIES; do
  [[ "${!knob}" =~ ^[0-9]+$ ]] || die "CI_BUILD_${knob} must be a whole number of seconds (or tries), not '${!knob}'"
done
[[ "${POLL}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "CI_BUILD_POLL must be a number of seconds, not '${POLL}'"

require_cmd gh jq

# What application.yml uploads, and the file inside it.
ARTIFACT="images-${ENV_NAME}"
RECORD="images-${ENV_NAME}.json"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-ci-images.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
API_ERR="${WORK}/api.err"

# A red job's last line is the one a reader sees; on Actions it is also raised
# as an annotation, so it shows on the run's summary page without opening logs.
fail_with() {
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    printf '::error title=release images::%s\n' "$*" >&2
  fi
  die "$*"
}

summary() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$*" >>"${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
  fi
}

# gh's reason for the last failed read, one line, credentials masked.
api_reason() {
  redact <"${API_ERR}" | tr -d '\r' | grep -v '^[[:space:]]*$' | head -n 1 || true
}

# gh api PATH > FILE. Non-zero when the API could not be read, with the reason
# in API_ERR. stdin is closed: this runs inside a `while read` over a file.
api() {
  gh api -H "Accept: application/vnd.github+json" "$1" </dev/null >"$2" 2>"${API_ERR}"
}

# ---------------------------------------------------------------------------
# One look at CI. Sets VERDICT and returns 0, or returns non-zero when the API
# could not be read (the caller retries, and never reads that as "absent").
#
#   reuse    the record is at OUT
#   wait     a build of this commit is queued or running
#   none     no run of WORKFLOW for this commit on BRANCH exists yet
#   failed   the newest decided run's build failed
#   unbuilt  the newest decided run never built this commit for ENV_NAME
#
# Runs are read NEWEST FIRST and the first reusable one wins: a successful
# build of this commit is a successful build of this commit, even if a newer
# run of it is still going. Otherwise a pending run is waited for, and only
# when every run has decided does the newest decide the answer.
# ---------------------------------------------------------------------------
VERDICT=""; V_URL=""; V_WHY=""

survey() {
  VERDICT=""; V_URL=""; V_WHY=""
  api "repos/${REPO}/actions/workflows/${WORKFLOW}/runs?head_sha=${SHA}&per_page=100" "${WORK}/runs.json" || return 1
  # "-" for an empty field: tab is IFS whitespace, so `read` would collapse two
  # adjacent tabs and shift every later field left by one.
  jq -r --arg b "${BRANCH}" --arg sha "${SHA}" '
      [ (.workflow_runs // [])[]
        | select(.head_sha == $sha and .head_branch == $b and .event != "pull_request") ]
      | sort_by(.id) | reverse | .[]
      | [ (.id | tostring), (.status // "-"), (.conclusion // "-"), (.html_url // "-") ]
      | @tsv' "${WORK}/runs.json" >"${WORK}/runs.tsv" || return 1
  if [[ ! -s "${WORK}/runs.tsv" ]]; then
    VERDICT="none"
    return 0
  fi

  local run_id run_status run_conclusion run_url state detail url
  local pending=0 pending_url="" pending_why="" first="" first_url="" first_why=""
  while IFS=$'\t' read -r run_id run_status run_conclusion run_url; do
    api "repos/${REPO}/actions/runs/${run_id}/jobs?per_page=100" "${WORK}/jobs.json" || return 1
    # Matched by name, or by name + " / " -- how GitHub names a job that calls a
    # reusable workflow -- so the build can move into one without this missing it.
    IFS=$'\t' read -r state detail url < <(jq -r --arg n "${JOB}" '
        [ (.jobs // [])[] | select(.name == $n or (.name | startswith($n + " / "))) ] as $j
        | [ $j[] | .conclusion // "-" ] as $c
        | if ($j | length) == 0 then ["missing", "-", "-"]
          elif any($j[]; .status != "completed") then
            ["running", ([$j[] | .status] | unique | join(",")), ($j[0].html_url // "-")]
          elif all($c[]; . == "success") then ["success", "success", ($j[0].html_url // "-")]
          elif all($c[]; . == "success" or . == "cancelled" or . == "skipped") then
            ["unbuilt", ($c | unique | join(",")), ($j[0].html_url // "-")]
          else
            ["failed", ($c | unique | join(",")),
             ([$j[] | select(.conclusion != "success")][0].html_url // "-")]
          end
        | @tsv' "${WORK}/jobs.json") || return 1

    case "${state}" in
      success)
        local found=""
        if ! found="$(artifact_state "${run_id}")"; then return 1; fi
        if [[ "${found}" == present ]]; then
          fetch "${run_id}" "${url}" || return 1
          VERDICT="reuse"; V_URL="${url}"
          return 0
        fi
        state=unbuilt
        if [[ "${found}" == expired ]]; then
          detail="its build succeeded, but the ${ARTIFACT} record has expired"
        else
          detail="its build succeeded but recorded nothing for ${ENV_NAME} (no ${ARTIFACT} artifact): it built for another environment"
        fi
        ;;
      running)
        pending=1; pending_url="${url}"; pending_why="${detail}"
        continue ;;
      missing)
        if [[ "${run_status}" != completed ]]; then
          # The run is queued (behind the previous commit's, on main) or its
          # build is waiting on the checks it needs; either way it has not
          # decided yet.
          pending=1; pending_url="${run_url}"; pending_why="run ${run_status}, build job not started"
          continue
        fi
        state=unbuilt; url="${run_url}"
        detail="run ${run_id} finished (${run_conclusion}) without a job named '${JOB}'"
        ;;
      unbuilt) detail="its '${JOB}' job was ${detail} and recorded no build" ;;
      failed)  detail="its '${JOB}' job concluded ${detail}" ;;
    esac

    if [[ -z "${first}" ]]; then
      first="${state}"; first_url="${url}"; first_why="${detail}"
    fi
  done <"${WORK}/runs.tsv"

  if [[ "${pending}" -eq 1 ]]; then
    VERDICT="wait"; V_URL="${pending_url}"; V_WHY="${pending_why}"
  else
    VERDICT="${first}"; V_URL="${first_url}"; V_WHY="${first_why}"
  fi
  return 0
}

# present | expired | absent, for this environment's record in a run.
artifact_state() {
  api "repos/${REPO}/actions/runs/$1/artifacts?name=${ARTIFACT}&per_page=100" "${WORK}/artifacts.json" || return 1
  jq -r --arg a "${ARTIFACT}" '
      [ (.artifacts // [])[] | select(.name == $a) ] as $m
      | if any($m[]; .expired != true) then "present"
        elif ($m | length) > 0 then "expired"
        else "absent" end' "${WORK}/artifacts.json"
}

# Download the record from run $1 and check it describes this commit and this
# environment before it goes anywhere near OUT. A download that fails is an
# unreadable API (returns 1, retried); a record that is WRONG is fatal here and
# now -- rebuilding over it would hide whatever produced it.
fetch() {
  local run_id="$1" job_url="$2" dir="${WORK}/record-$1" record got_commit got_env count
  rm -rf "${dir}"
  mkdir -p "${dir}"
  gh run download "${run_id}" --repo "${REPO}" --name "${ARTIFACT}" --dir "${dir}" \
    </dev/null >/dev/null 2>"${API_ERR}" || return 1
  record="${dir}/${RECORD}"
  if [[ ! -f "${record}" ]]; then
    fail_with "the ${ARTIFACT} artifact of ${job_url} holds no ${RECORD} (it holds: $(cd "${dir}" && find . -type f | sed 's|^\./||' | tr '\n' ' '))"
  fi
  if ! jq -e 'type == "object" and ((.images // null) | type) == "array"' "${record}" >/dev/null 2>&1; then
    fail_with "${RECORD} from ${job_url} is not a build manifest (no images array)"
  fi
  got_commit="$(jq -r '.commit // ""' "${record}")"
  got_env="$(jq -r '.environment // ""' "${record}")"
  count="$(jq '.images | length' "${record}")"
  # PROVENANCE. The run was chosen by its head sha, and this checks the record
  # agrees -- it is the manifest, not the run, that names the digests promoted.
  if [[ "${got_commit}" != "${SHA}" ]]; then
    fail_with "${RECORD} from ${job_url} says it was built from '${got_commit:-no commit at all}', not ${SHA}; refusing to release images whose record does not name this commit"
  fi
  if [[ "${got_env}" != "${ENV_NAME}" ]]; then
    fail_with "${RECORD} from ${job_url} was built for '${got_env:-no environment}', not '${ENV_NAME}'; swarm-ui bakes its environment in at build time, so it is not these images"
  fi
  if [[ "${count}" -eq 0 ]]; then
    fail_with "${RECORD} from ${job_url} lists no images; a build that recorded nothing cannot be released"
  fi
  mkdir -p "$(dirname -- "${OUT}")"
  cp "${record}" "${OUT}.tmp"
  mv -f "${OUT}.tmp" "${OUT}"
  ok "${count} image(s) ${WORKFLOW} built for ${SHA} (${ENV_NAME}): ${job_url}"
}

absent() {
  if [[ "${IF_ABSENT}" == build ]]; then
    warn "CI never built ${SHA} for ${ENV_NAME}: $1"
    info "--if-absent build: the caller builds it"
    summary "**Images:** built by this run -- ${WORKFLOW} never built \`${SHA:0:12}\` for ${ENV_NAME} ($1)."
    exit 3
  fi
  fail_with "CI never built ${SHA} for ${ENV_NAME}: $1. A release on a push promotes the images ${WORKFLOW} built for its commit and never submits a second build of its own. To release this commit anyway, dispatch release.yml for it: a dispatched release builds what CI did not."
}

# ---------------------------------------------------------------------------
# Look until there is an answer.
# ---------------------------------------------------------------------------
step "Images ${WORKFLOW} built for ${SHA} (${ENV_NAME})"
START="$(date +%s)"
FAILED_READS=0
LAST_NOTE=""

while :; do
  rc=0
  survey || rc=$?
  WAITED=$(( $(date +%s) - START ))

  if [[ "${rc}" -ne 0 ]]; then
    FAILED_READS=$((FAILED_READS + 1))
    reason="$(api_reason)"
    warn "could not read the GitHub API (${FAILED_READS} of ${API_TRIES}): ${reason:-no reason given}"
    if [[ "${FAILED_READS}" -ge "${API_TRIES}" ]]; then
      fail_with "could not read ${WORKFLOW}'s runs for ${SHA} after ${API_TRIES} tries (${reason:-no reason given}); an unreadable API is not a missing build, so nothing is reused and nothing is built"
    fi
    sleep "${POLL}"
    continue
  fi
  FAILED_READS=0

  case "${VERDICT}" in
    reuse)
      summary "**Images:** reused from ${V_URL} -- no second Cloud Build."
      exit 0 ;;
    wait)
      note="waiting for ${V_URL} (${V_WHY})"
      if [[ "${note}" != "${LAST_NOTE}" ]]; then info "${note}"; LAST_NOTE="${note}"; fi
      if [[ "${WAITED}" -ge "${WAIT}" ]]; then
        fail_with "timed out after ${WAIT}s waiting for ${WORKFLOW}'s '${JOB}' job for ${SHA}, which is still pending (${V_WHY}): ${V_URL}"
      fi ;;
    none)
      note="no ${WORKFLOW} run for ${SHA} on ${BRANCH} yet"
      if [[ "${note}" != "${LAST_NOTE}" ]]; then info "${note}"; LAST_NOTE="${note}"; fi
      if [[ "${WAITED}" -ge "${APPEAR}" ]]; then
        absent "no ${WORKFLOW} run for it on ${BRANCH} appeared within ${APPEAR}s"
      fi ;;
    failed)
      fail_with "${WORKFLOW}'s build of ${SHA} failed -- ${V_WHY}: ${V_URL}. The release promotes only what that job built, so there is nothing to promote, and building again here would repeat the failure behind a second Cloud Build. Re-run that job if it was a flake; otherwise fix the build and push." ;;
    unbuilt)
      absent "${V_WHY} (${V_URL})" ;;
    *)
      fail_with "internal: no verdict after reading ${WORKFLOW}'s runs for ${SHA}" ;;
  esac
  sleep "${POLL}"
done
