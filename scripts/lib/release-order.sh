#!/usr/bin/env bash
# Keep dev moving FORWARD when two release lanes run at once.
#
# WHY THIS EXISTS. release.yml and hotfix.yml both promote, apply and deploy to
# dev, and they are in different concurrency groups on purpose (owner decision
# 2026-10-08, observer proposal H; docs/ci.md, "Hotfix releases"): a hotfix
# must not queue behind a normal release that holds `release-dev` for 100
# minutes p50. So nothing in GitHub stops them overlapping, and three things
# could go wrong without this script:
#
#   1. an OLDER commit's release reaches its apply after a NEWER commit's
#      hotfix applied, and quietly rolls dev back to the older digests;
#   2. the same, one job earlier: an older release's promote moves `:dev`
#      back over a newer one's digests, so a `skip_build` redeploy reads the
#      wrong set;
#   3. two applies of the same root race, and the loser's saved plan is
#      refused as stale -- a red release for nothing.
#
# The rule, stated once and used by BOTH workflows (release-steps.yml calls
# it; release.yml's dev-iam job calls it directly):
#
#   * One lock per environment, gs://$TF_STATE_BUCKET/releases/<env>/apply.lock,
#     created with --if-generation-match=0 -- an atomic "create only if absent"
#     in GCS, so exactly one lane holds it. A lane waits for it (--wait), and
#     a lock older than --stale is a runner that died holding it and is broken.
#   * Two records beside it, releases/<env>/promoted.json and applied.json,
#     each the SHA the last successful promote / apply shipped.
#   * Under the lock, a lane proceeds only if EVERY recorded SHA is an ancestor
#     of (or equal to) the commit it is releasing. Otherwise a newer commit is
#     already out, and this lane is SUPERSEDED: it changes nothing and ends
#     GREEN, because the newer commit contains everything this one had.
#
# WHAT IS DEPLOYED, READ BACK. applied.json is also the answer to "which
# commit does dev run": .github/workflows/accept.yml asks `deployed` for it,
# so acceptance judges exactly that SHA and says so, rather than the commit
# whose release happened to finish last (owner decision 2026-10-08, cut A of
# the release timing report). It takes no lock: it reads one object.
#
# It is not used for prod. A prod release is dispatched by a person at a
# commit they chose, behind `approval (prod)`, and may be a deliberate
# rollback; ordering it here would refuse exactly that. `unless-superseded`
# runs its command unchanged for prod, and the workflows call the other
# subcommands only when the environment is not prod.
#
# The SHAs are compared with `git merge-base --is-ancestor`, so the checkout
# needs main's history (`fetch-depth: 0`). A shallow checkout is refused rather
# than answered: in one, every older commit looks like no ancestor at all.
#
# Usage:
#   release-order.sh lock    --environment dev [--wait SECONDS] [--stale SECONDS]
#       prints `generation=<n>` (append it to $GITHUB_OUTPUT)
#   release-order.sh unlock  --environment dev --generation N
#   release-order.sh check   --environment dev [--sha SHA]
#       prints `proceed` or `superseded` as its last line, and appends
#       superseded=true|false to $GITHUB_OUTPUT when that is set
#   release-order.sh record  --environment dev --what applied|promoted [--sha SHA]
#   release-order.sh unless-superseded --environment ENV [--sha SHA] -- COMMAND...
#       runs COMMAND; if it fails and the release has since been superseded,
#       says so, writes superseded=true to $GITHUB_OUTPUT and exits 0
#   release-order.sh deployed --environment dev
#       prints `sha=<sha>`, `run_id=<id>` and `workflow=<name>` from
#       applied.json (append them to $GITHUB_OUTPUT); no record is an error
#
# --sha defaults to $GITHUB_SHA. The bucket is $TF_STATE_BUCKET (load_env's
# default when unset). Exit 0 = done (for check, the verdict is on stdout);
# exit 1 = could not do it, which fails the step: nothing is applied blind.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

usage() { sed -n '/^# Usage:/,/^# exit 1/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; }

[[ $# -gt 0 ]] || { usage; exit 1; }
COMMAND="$1"; shift

ORDER_ENV=""
ORDER_SHA="${GITHUB_SHA:-}"
ORDER_WHAT=""
ORDER_GENERATION=""
# Half an hour: the longest a lane holds the lock is a plan and an apply
# (~10 minutes measured, under Terraform's own 900 s lock timeout on a hotfix).
ORDER_WAIT="${SWARM_RELEASE_LOCK_WAIT:-1800}"
# An hour: no lane holds the lock for that long unless its runner died holding
# it, and a dead holder must not stop every dev release until someone notices.
ORDER_STALE="${SWARM_RELEASE_LOCK_STALE:-3600}"
# Between two looks at a held lock. Tests set 0.
ORDER_POLL="${SWARM_RELEASE_LOCK_POLL:-15}"
WRAPPED=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment) ORDER_ENV="${2:-}"; shift 2 ;;
    --sha)         ORDER_SHA="${2:-}"; shift 2 ;;
    --what)        ORDER_WHAT="${2:-}"; shift 2 ;;
    --generation)  ORDER_GENERATION="${2:-}"; shift 2 ;;
    --wait)        ORDER_WAIT="${2:-}"; shift 2 ;;
    --stale)       ORDER_STALE="${2:-}"; shift 2 ;;
    --) shift; WRAPPED=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) die "release-order.sh: unknown argument '$1'" ;;
  esac
done

[[ "${ORDER_ENV}" =~ ^[a-z][a-z0-9-]*$ ]] || die "release-order.sh: --environment is required (dev, ...), got '${ORDER_ENV}'"
for n in "${ORDER_WAIT}" "${ORDER_STALE}" "${ORDER_POLL}"; do
  [[ "${n}" =~ ^[0-9]+$ ]] || die "release-order.sh: '${n}' is not a number of seconds"
done

load_env
require_cmd gcloud jq git

# The checkout whose history decides the order: this one. Tests point it at a
# repository they built.
ORDER_GIT="${SWARM_RELEASE_ORDER_CHECKOUT:-${REPO_ROOT}}"
ORDER_PREFIX="gs://${TF_STATE_BUCKET}/releases/${ORDER_ENV}"
LOCK_URL="${ORDER_PREFIX}/apply.lock"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-release-order.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# Who is asking, as the lock and the records say it.
holder() {
  printf '%s run %s attempt %s job %s' "${GITHUB_WORKFLOW:-local}" "${GITHUB_RUN_ID:-$$}" \
    "${GITHUB_RUN_ATTEMPT:-1}" "${GITHUB_JOB:-none}"
}

# gcs_read URL OUT -- 0: the object's JSON metadata is in OUT and its content
# in OUT.body. 1: there is no such object. 2: gcloud could not say (printed).
gcs_read() {
  local url="$1" out="$2" rc=0
  gcloud storage objects describe "${url}" --format=json >"${out}" 2>"${out}.err" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    if gcloud_not_found "$(cat "${out}.err")"; then return 1; fi
    err "could not read ${url}:"
    redact <"${out}.err" | head -n 3 | sed 's/^/     /' >&2
    return 2
  fi
  rc=0
  gcloud storage cat "${url}" >"${out}.body" 2>"${out}.err" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    # Deleted between the two calls: that is "absent", as the next look says.
    if gcloud_not_found "$(cat "${out}.err")"; then return 1; fi
    err "could not read ${url}:"
    redact <"${out}.err" | head -n 3 | sed 's/^/     /' >&2
    return 2
  fi
}

generation_of() {
  jq -r '.generation // .metadata.generation // empty' "$1"
}

require_sha() {
  [[ "${ORDER_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "release-order.sh: --sha (or GITHUB_SHA) must be a full commit SHA, got '${ORDER_SHA}'"
}

require_history() {
  git -C "${ORDER_GIT}" rev-parse --git-dir >/dev/null 2>&1 \
    || die "release-order.sh: ${ORDER_GIT} is not a git checkout; the order is read from its history"
  if [[ "$(git -C "${ORDER_GIT}" rev-parse --is-shallow-repository)" == "true" ]]; then
    die "release-order.sh: this checkout is shallow, so an older commit is indistinguishable from an unrelated one. Check out with fetch-depth: 0."
  fi
  git -C "${ORDER_GIT}" cat-file -e "${ORDER_SHA}^{commit}" 2>/dev/null \
    || die "release-order.sh: ${ORDER_SHA} is not in this checkout"
}

cmd_lock() {
  local body="${WORK}/lock.json" seen="${WORK}/seen" now deadline rc at gen who nonce misses=0
  nonce="$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
  now="$(date +%s)"
  deadline=$((now + ORDER_WAIT))
  while :; do
    now="$(date +%s)"
    jq -n --arg holder "$(holder)" --arg sha "${ORDER_SHA}" --arg nonce "${nonce}" --argjson at "${now}" \
      '{holder: $holder, sha: $sha, nonce: $nonce, at: $at}' >"${body}"
    if gcloud storage cp --no-user-output-enabled --if-generation-match=0 "${body}" "${LOCK_URL}" 2>"${WORK}/cp.err"; then
      rc=0
      gcs_read "${LOCK_URL}" "${seen}" || rc=$?
      [[ "${rc}" -eq 0 ]] || die "took ${LOCK_URL} but could not read it back"
      [[ "$(jq -r '.nonce // empty' "${seen}.body")" == "${nonce}" ]] \
        || die "took ${LOCK_URL} but it now names $(jq -r '.holder // "nobody"' "${seen}.body"); refusing to go on under a lock that is not this lane's"
      gen="$(generation_of "${seen}")"
      [[ "${gen}" =~ ^[0-9]+$ ]] || die "took ${LOCK_URL} but gcloud reported no generation for it"
      ok "took ${LOCK_URL} (generation ${gen}) for $(holder)"
      printf 'generation=%s\n' "${gen}"
      return 0
    fi

    rc=0
    gcs_read "${LOCK_URL}" "${seen}" || rc=$?
    case "${rc}" in
      0)
        misses=0
        at="$(jq -r '.at // 0' "${seen}.body" 2>/dev/null || echo 0)"
        [[ "${at}" =~ ^[0-9]+$ ]] || at=0
        who="$(jq -r '.holder // "an unreadable holder"' "${seen}.body" 2>/dev/null || echo "an unreadable holder")"
        if (( now - at > ORDER_STALE )); then
          gen="$(generation_of "${seen}")"
          warn "${LOCK_URL} has been held by ${who} for $((now - at)) s, longer than ${ORDER_STALE} s: its runner is gone. Breaking it."
          gcloud storage rm --no-user-output-enabled --if-generation-match="${gen}" "${LOCK_URL}" 2>/dev/null \
            || warn "someone else moved ${LOCK_URL} first; looking again"
          continue
        fi
        if (( now >= deadline )); then
          die "${LOCK_URL} is held by ${who} (for $((now - at)) s); waited ${ORDER_WAIT} s. Nothing was changed by this lane."
        fi
        info "${LOCK_URL} is held by ${who}; waiting"
        sleep "${ORDER_POLL}"
        ;;
      1)
        # Not there, yet the create failed: the holder released it in between,
        # or the create failed for a reason of its own. Look a few times.
        misses=$((misses + 1))
        if (( misses >= 5 )); then
          err "could not create ${LOCK_URL}, and it does not exist:"
          redact <"${WORK}/cp.err" | head -n 3 | sed 's/^/     /' >&2
          exit 1
        fi
        ;;
      *) exit 1 ;;
    esac
  done
}

cmd_unlock() {
  [[ "${ORDER_GENERATION}" =~ ^[0-9]+$ ]] || die "release-order.sh unlock: --generation is required"
  if gcloud storage rm --no-user-output-enabled --if-generation-match="${ORDER_GENERATION}" "${LOCK_URL}" 2>"${WORK}/rm.err"; then
    ok "released ${LOCK_URL} (generation ${ORDER_GENERATION})"
    return 0
  fi
  local rc=0 seen="${WORK}/seen"
  gcs_read "${LOCK_URL}" "${seen}" || rc=$?
  case "${rc}" in
    1) warn "${LOCK_URL} was already gone (broken as stale?); nothing to release" ;;
    0)
      if [[ "$(generation_of "${seen}")" == "${ORDER_GENERATION}" ]]; then
        err "could not release ${LOCK_URL}, which this lane still holds:"
        redact <"${WORK}/rm.err" | head -n 3 | sed 's/^/     /' >&2
        exit 1
      fi
      warn "${LOCK_URL} now belongs to $(jq -r '.holder // "another lane"' "${seen}.body"); left alone"
      ;;
    *) exit 1 ;;
  esac
}

# The verdict on stdout; why, on stderr.
cmd_check() {
  require_sha
  require_history
  local what rc rec verdict="proceed" seen="${WORK}/record"
  for what in promoted applied; do
    rc=0
    gcs_read "${ORDER_PREFIX}/${what}.json" "${seen}" || rc=$?
    case "${rc}" in
      0) ;;
      1) info "no ${what} record for ${ORDER_ENV} yet"; continue ;;
      *) exit 1 ;;
    esac
    rec="$(jq -r '.sha // empty' "${seen}.body" 2>/dev/null || true)"
    [[ "${rec}" =~ ^[0-9a-f]{40}$ ]] \
      || die "${ORDER_PREFIX}/${what}.json names no commit SHA; refusing to guess the order. Read it, and remove it only if it is wrong."
    if [[ "${rec}" == "${ORDER_SHA}" ]]; then
      info "${what} ${ORDER_ENV}: ${rec} is this commit"
      continue
    fi
    git -C "${ORDER_GIT}" cat-file -e "${rec}^{commit}" 2>/dev/null \
      || die "${ORDER_PREFIX}/${what}.json names ${rec}, which is not in this checkout's history (main rewritten?). Refusing to guess; read the record and remove it if it is wrong."
    if git -C "${ORDER_GIT}" merge-base --is-ancestor "${rec}" "${ORDER_SHA}"; then
      info "${what} ${ORDER_ENV}: ${rec} is an ancestor of ${ORDER_SHA}"
    else
      warn "${what} ${ORDER_ENV}: ${rec} ($(jq -r '.workflow // "?"' "${seen}.body") run $(jq -r '.run_id // "?"' "${seen}.body")) is not an ancestor of ${ORDER_SHA}: a newer commit is already out"
      verdict="superseded"
    fi
  done
  if [[ "${verdict}" == "superseded" ]]; then
    echo "::notice title=Superseded::a newer commit than ${ORDER_SHA} is already promoted or applied in ${ORDER_ENV}; this release changes nothing there and ends green. That commit contains this one."
  fi
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    if [[ "${verdict}" == "superseded" ]]; then echo "superseded=true"; else echo "superseded=false"; fi >>"${GITHUB_OUTPUT}"
  fi
  # The verdict is the LAST line of stdout (a notice may precede it).
  printf '%s\n' "${verdict}"
}

cmd_record() {
  require_sha
  case "${ORDER_WHAT}" in
    applied|promoted) ;;
    *) die "release-order.sh record: --what must be applied or promoted, got '${ORDER_WHAT}'" ;;
  esac
  local body="${WORK}/record.json"
  jq -n --arg sha "${ORDER_SHA}" --arg env "${ORDER_ENV}" --arg what "${ORDER_WHAT}" \
    --arg workflow "${GITHUB_WORKFLOW:-local}" --arg run_id "${GITHUB_RUN_ID:-}" \
    --arg run_attempt "${GITHUB_RUN_ATTEMPT:-}" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{sha: $sha, environment: $env, what: $what, workflow: $workflow, run_id: $run_id, run_attempt: $run_attempt, at: $at}' >"${body}"
  gcloud storage cp --no-user-output-enabled "${body}" "${ORDER_PREFIX}/${ORDER_WHAT}.json" \
    || die "could not write ${ORDER_PREFIX}/${ORDER_WHAT}.json"
  ok "recorded ${ORDER_WHAT} ${ORDER_ENV} = ${ORDER_SHA}"
}

# The applied record, validated, as GITHUB_OUTPUT lines. An absent record is
# an error, not an empty answer: acceptance of "nothing" would be green.
cmd_deployed() {
  local rc=0 seen="${WORK}/record" sha run_id workflow
  gcs_read "${ORDER_PREFIX}/applied.json" "${seen}" || rc=$?
  case "${rc}" in
    0) ;;
    1) die "${ORDER_PREFIX}/applied.json does not exist: no release has recorded what ${ORDER_ENV} runs, so there is nothing to judge" ;;
    *) exit 1 ;;
  esac
  sha="$(jq -r '.sha // empty' "${seen}.body" 2>/dev/null || true)"
  [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] \
    || die "${ORDER_PREFIX}/applied.json names no commit SHA; refusing to guess what ${ORDER_ENV} runs"
  run_id="$(jq -r '.run_id // empty' "${seen}.body")"
  [[ -z "${run_id}" || "${run_id}" =~ ^[0-9]+$ ]] || run_id=""
  # One line, nothing a GITHUB_OUTPUT reader could take for a second key.
  workflow="$(jq -r '.workflow // empty' "${seen}.body" | tr -cd 'A-Za-z0-9 ._-')"
  workflow="${workflow:0:100}"
  info "${ORDER_ENV} runs ${sha} (applied by ${workflow:-?} run ${run_id:-?})"
  printf 'sha=%s\nrun_id=%s\nworkflow=%s\n' "${sha}" "${run_id}" "${workflow}"
}

cmd_unless_superseded() {
  [[ ${#WRAPPED[@]} -gt 0 ]] || die "release-order.sh unless-superseded: give the command after --"
  if [[ "${ORDER_ENV}" == "prod" ]]; then
    exec "${WRAPPED[@]}"
  fi
  local rc=0 verdict=""
  "${WRAPPED[@]}" || rc=$?
  [[ "${rc}" -ne 0 ]] || return 0
  # The command failed. If a newer commit reached the environment while it
  # ran, that is why, and this release has nothing left to prove.
  # Its own GITHUB_OUTPUT line is not wanted here: this one writes the answer.
  verdict="$(GITHUB_OUTPUT="" cmd_check | tail -n 1)" || return "${rc}"
  if [[ "${verdict}" == "superseded" ]]; then
    echo "::notice title=Superseded::${WRAPPED[0]} failed (exit ${rc}) after a newer commit reached ${ORDER_ENV}; that release proves itself, so this one stops here, green."
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then echo "superseded=true" >>"${GITHUB_OUTPUT}"; fi
    return 0
  fi
  return "${rc}"
}

case "${COMMAND}" in
  lock)   cmd_lock ;;
  unlock) cmd_unlock ;;
  check)  cmd_check ;;
  record) cmd_record ;;
  unless-superseded) cmd_unless_superseded ;;
  deployed) cmd_deployed ;;
  *) die "release-order.sh: unknown command '${COMMAND}' (lock, unlock, check, record, unless-superseded, deployed)" ;;
esac
