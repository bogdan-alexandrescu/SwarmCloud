#!/usr/bin/env bash
# ctxq-replay: the context-quality offline replay (lane CTXQ-E) -- §6.1 of
# docs/design/context-quality.md. Replays past lanes at their base commits,
# with and without the context pipeline, and scores the pairs, WITHOUT
# OPENING A PULL REQUEST.
#
# WHY THIS EXISTS. Local context becomes a default only once it measurably
# beats today's runner (§6.3, owner decisions Q8 and Q9). The replay is the
# cheap first measurement: the same brief, at the same base commit, on arm A
# (nothing staged) and arm B (the pipeline), scored by machines. The rules --
# which lanes, which workflow, which arm a run landed on, every score -- live
# in scripts/lib/ctxq_replay.py (tested in tests/unit/scripts/
# test_ctxq_replay.py); this script only reads, submits and pushes.
#
# WHAT IT NEVER DOES. It opens no pull request: every replay is a `collect`
# workflow with `carrier: branches`, whose steps push `swarm/<task>` and open
# nothing. It pushes only `<sha>:refs/heads/replay/<lane>/<arm>`, never
# forced and never a delete, each refspec checked by the helper and again
# here. It cannot pick an arm: whether a step is staged is the stored
# setting's (§4.7, invariant 10), so a replay is labelled by the arm its
# implementer's context-pack.json says it got.
#
# THE STEPS, in order, each resumable over the same --out directory:
#
#   plan     --baseline DIR --out DIR [--signal N] [--controls N]
#            Reads a quality-baseline directory (scripts/quality-baseline.sh
#            --out DIR, lane CTXQ-M) and writes plan.json: the corpus, split
#            into signal and controls by CTXQ-M's definitions, and the pilot
#            (30 signal lanes across G, T, X and P, plus 10 controls). Offline.
#   submit   --out DIR --arm A|B|any [--limit N]
#            For each pilot lane still owed that arm, reads the lane's brief
#            (GET /v1/tasks/<its task>) and submits its replay workflow
#            (POST /v1/workflows). `any` submits under the A/B split and
#            lets the arm fall where it falls; A or B expects the operator to
#            have set the stored setting for that wave. Typed confirmation.
#   collect  --out DIR
#            Reads each submitted workflow, its step tasks, their artifact
#            listings and the three artifacts scoring needs: the implementer's
#            context-pack.json, the review's verdict.json and the score
#            step's score.json. Writes records.json.
#   mirror   --out DIR [--repo owner/name] [--git-dir DIR] [--dry-run]
#            Fetches each slot's implementer head, writes its diff against the
#            lane's base (diffs/) and the merged PR's (merged/), then pushes
#            each head to replay/<lane>/<arm>. Typed confirmation.
#   score    --out DIR [--json]
#            The paired report: CI-equivalent red, historic-red recurrence,
#            review NOT_YET, similarity to the merged change, cost and time,
#            McNemar's exact p, and §8 Q8's gate for the 150-pair run.
#
# Needs `jq`; `submit` and `collect` need swarm-api (an operator credential;
# SWARM_API_TENANT picks the tenant) and `curl`; `mirror` needs `git` with
# push access to the repository, and `gh` when --repo is not given.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

usage() { sed -n '2,52p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

COMMAND="${1:-}"
[[ $# -eq 0 ]] || shift
OUT=""
BASELINE=""
ARM=""
SIGNAL=30
CONTROLS=10
LIMIT=0
REPO="${GH_REPO:-}"
GIT_DIR_ARG=""
DRY_RUN=0
SCORE_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) [[ $# -ge 2 ]] || die "--out needs a directory"; OUT="$2"; shift 2 ;;
    --baseline) [[ $# -ge 2 ]] || die "--baseline needs a directory"; BASELINE="$2"; shift 2 ;;
    --arm) [[ $# -ge 2 ]] || die "--arm needs A, B or any"; ARM="$2"; shift 2 ;;
    --signal) [[ $# -ge 2 ]] || die "--signal needs a number"; SIGNAL="$2"; shift 2 ;;
    --controls) [[ $# -ge 2 ]] || die "--controls needs a number"; CONTROLS="$2"; shift 2 ;;
    --limit) [[ $# -ge 2 ]] || die "--limit needs a number"; LIMIT="$2"; shift 2 ;;
    --repo) [[ $# -ge 2 ]] || die "--repo needs owner/name"; REPO="$2"; shift 2 ;;
    --git-dir) [[ $# -ge 2 ]] || die "--git-dir needs a directory"; GIT_DIR_ARG="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --json) SCORE_ARGS+=(--json); shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

case "${COMMAND}" in
  plan|submit|collect|mirror|score) ;;
  -h|--help|help) usage; exit 0 ;;
  "") usage; die "name a step: plan, submit, collect, mirror or score" ;;
  *) die "unknown step: ${COMMAND} (see --help)" ;;
esac
[[ -n "${OUT}" ]] || die "--out DIR is required: every step reads and writes the same directory"
[[ "${SIGNAL}" =~ ^[0-9]+$ ]] || die "--signal must be a whole number, got: ${SIGNAL}"
[[ "${CONTROLS}" =~ ^[0-9]+$ ]] || die "--controls must be a whole number, got: ${CONTROLS}"
[[ "${LIMIT}" =~ ^[0-9]+$ ]] || die "--limit must be a whole number, got: ${LIMIT}"
require_cmd jq

# `swarm_python` prints either one path or `uv run --project <root> python`.
read -r -a PY <<<"$(swarm_python)"
HELPER="${REPO_ROOT}/scripts/lib/ctxq_replay.py"
mkdir -p "${OUT}"
WORK="$(cd "${OUT}" && pwd)"
REFSPEC_RE='^[0-9a-f]{40}:refs/heads/replay/[1-9][0-9]*/[AB]$'

# One read, redirected to a file rather than captured with $(...): a command
# substitution is a SUBSHELL, so API_STATUS would never reach this shell and
# every response would read as a success.
api_read() {
  local out="$1" path="$2"
  api_request GET "${API_PREFIX}${path}" >"${out}"
}

api_failed() {
  err "$1 answered HTTP ${API_STATUS}"
  redact <"$2" >&2 || true
}

# --- plan ---------------------------------------------------------------------
if [[ "${COMMAND}" == "plan" ]]; then
  [[ -n "${BASELINE}" ]] || die "plan needs --baseline DIR (scripts/quality-baseline.sh --out DIR)"
  [[ -f "${BASELINE}/lanes.json" ]] || die "${BASELINE}/lanes.json is missing: record a baseline first"
  step "Planning the pilot from ${BASELINE}"
  "${PY[@]}" "${HELPER}" plan --baseline "${BASELINE}" --signal "${SIGNAL}" --controls "${CONTROLS}" \
    >"${WORK}/plan.json.tmp"
  mv "${WORK}/plan.json.tmp" "${WORK}/plan.json"
  info "$(jq -r '"\(.corpus | length) lane(s) read: \([.corpus[] | select(.signal)] | length) signal, \([.corpus[] | select(.signal | not)] | length) control(s), \([.corpus[] | select(.replayable | not)] | length) not replayable"' "${WORK}/plan.json")"
  info "$(jq -r '.pilot | "pilot: \(.signal | length) signal lane(s), \(.controls | length) control(s); short by \(.short.signal) signal, \(.short.controls) control(s)"' "${WORK}/plan.json")"
  jq -r '.pilot.pairs[] | "  #\(.lane) \(.group) \(.classes | join(","))"' "${WORK}/plan.json" | redact >&2
  exit 0
fi

[[ -f "${WORK}/plan.json" ]] || die "${WORK}/plan.json is missing: run the plan step first"

# --- submit -------------------------------------------------------------------
if [[ "${COMMAND}" == "submit" ]]; then
  case "${ARM}" in A|B|any) ;; *) die "submit needs --arm A, B or any" ;; esac
  require_cmd curl
  mkdir -p "${WORK}/briefs" "${WORK}/specs"
  step "Lanes still owed arm ${ARM}"
  "${PY[@]}" "${HELPER}" owed --dir "${WORK}" --arm "${ARM}" >"${WORK}/owed.txt"
  : >"${WORK}/to-submit.txt"
  lanes=0
  while read -r lane count; do
    [[ -n "${lane}" ]] || continue
    if [[ "${LIMIT}" -gt 0 && "${lanes}" -ge "${LIMIT}" ]]; then
      break
    fi
    task_id="$(jq -r --argjson n "${lane}" '.corpus[] | select(.number == $n) | .task_id' "${WORK}/plan.json")"
    if [[ ! -s "${WORK}/briefs/${lane}.json" ]]; then
      if ! api_read "${WORK}/briefs/${lane}.json.tmp" "/tasks/${task_id}"; then
        api_failed "GET ${API_PREFIX}/tasks/${task_id} (#${lane}'s brief)" "${WORK}/briefs/${lane}.json.tmp"
        rm -f "${WORK}/briefs/${lane}.json.tmp"
        warn "#${lane}: its brief was not read; it is not submitted"
        continue
      fi
      mv "${WORK}/briefs/${lane}.json.tmp" "${WORK}/briefs/${lane}.json"
    fi
    if ! "${PY[@]}" "${HELPER}" spec --dir "${WORK}" --lane "${lane}" --arm "${ARM}" \
        >"${WORK}/specs/${lane}.json"; then
      warn "#${lane}: no workflow spec could be written; it is not submitted"
      continue
    fi
    jq -e '.strategy == "collect"' "${WORK}/specs/${lane}.json" >/dev/null \
      || die "#${lane}: the spec is not a collect workflow, so it could open a pull request; refusing"
    printf '%s %s\n' "${lane}" "${count}" >>"${WORK}/to-submit.txt"
    lanes=$((lanes + 1))
  done <"${WORK}/owed.txt"
  total="$(awk '{ n += $2 } END { print n + 0 }' "${WORK}/to-submit.txt")"
  info "${lanes} lane(s), ${total} workflow(s) of 3 steps each to submit"
  [[ "${total}" -gt 0 ]] || { ok "nothing is owed arm ${ARM}"; exit 0; }
  confirm "Submit ${total} replay workflow(s) (${lanes} lane(s), arm ${ARM}); each runs 3 agent steps and opens no pull request." "REPLAY"

  submitted=0
  while read -r lane count; do
    i=0
    while [[ "${i}" -lt "${count}" ]]; do
      i=$((i + 1))
      if ! api_request POST "${API_PREFIX}/workflows" "$(cat "${WORK}/specs/${lane}.json")" \
          >"${WORK}/submit.response"; then
        api_failed "POST ${API_PREFIX}/workflows (#${lane})" "${WORK}/submit.response"
        exit 2
      fi
      workflow_id="$(jq -r '.workflow.id // .id // empty' "${WORK}/submit.response")"
      [[ -n "${workflow_id}" ]] || die "#${lane}: the submission answered no workflow id"
      jq -cn --argjson lane "${lane}" --arg arm "${ARM}" --arg id "${workflow_id}" \
        --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{lane: $lane, intended_arm: $arm, workflow_id: $id, submitted_at: $at}' \
        >>"${WORK}/submissions.jsonl"
      submitted=$((submitted + 1))
      dim "#${lane}: ${workflow_id}"
    done
  done <"${WORK}/to-submit.txt"
  ok "${submitted} workflow(s) submitted; run collect once they finish"
  exit 0
fi

# --- collect ------------------------------------------------------------------
if [[ "${COMMAND}" == "collect" ]]; then
  require_cmd curl
  [[ -s "${WORK}/submissions.jsonl" ]] || die "nothing was submitted from ${WORK}: run submit first"
  mkdir -p "${WORK}/workflows" "${WORK}/tasks" "${WORK}/artifacts"
  step "Reading the submitted workflows"
  workflows=0
  while read -r workflow_id; do
    [[ -n "${workflow_id}" ]] || continue
    if ! api_read "${WORK}/workflows/${workflow_id}.json.tmp" "/workflows/${workflow_id}"; then
      api_failed "GET ${API_PREFIX}/workflows/${workflow_id}" "${WORK}/workflows/${workflow_id}.json.tmp"
      rm -f "${WORK}/workflows/${workflow_id}.json.tmp"
      continue
    fi
    mv "${WORK}/workflows/${workflow_id}.json.tmp" "${WORK}/workflows/${workflow_id}.json"
    workflows=$((workflows + 1))
  done < <(jq -r '.workflow_id' "${WORK}/submissions.jsonl")
  info "${workflows} workflow(s) read"

  step "Reading each finished step's task and artifact listing"
  tasks=0
  while read -r task_id; do
    [[ -n "${task_id}" ]] || continue
    mkdir -p "${WORK}/artifacts/${task_id}"
    if api_read "${WORK}/tasks/${task_id}.json" "/tasks/${task_id}" \
        && api_read "${WORK}/artifacts/${task_id}/listing.json" "/tasks/${task_id}/artifacts"; then
      tasks=$((tasks + 1))
    else
      api_failed "reading task ${task_id}" "${WORK}/tasks/${task_id}.json"
      rm -f "${WORK}/tasks/${task_id}.json" "${WORK}/artifacts/${task_id}/listing.json"
    fi
  done < <("${PY[@]}" "${HELPER}" tasks --dir "${WORK}")
  info "${tasks} finished task(s) read"

  step "Reading context-pack.json, verdict.json and score.json"
  artifacts=0
  while read -r task_id name; do
    [[ -n "${task_id}" ]] || continue
    if api_read "${WORK}/artifacts/${task_id}/${name}.response" \
        "/tasks/${task_id}/artifacts/content?name=${name}&limit_bytes=1048576"; then
      artifacts=$((artifacts + 1))
    else
      api_failed "reading ${name} of ${task_id}" "${WORK}/artifacts/${task_id}/${name}.response"
      rm -f "${WORK}/artifacts/${task_id}/${name}.response"
    fi
  done < <("${PY[@]}" "${HELPER}" wanted --dir "${WORK}")
  info "${artifacts} artifact(s) read"

  "${PY[@]}" "${HELPER}" records --dir "${WORK}" >"${WORK}/records.json"
  info "$(jq -r '"\(length) submission(s): \([.[] | select(.done)] | length) finished, \([.[] | select(.observed_arm == "A")] | length) on arm A, \([.[] | select(.observed_arm == "B")] | length) on arm B"' "${WORK}/records.json")"
  exit 0
fi

# --- mirror -------------------------------------------------------------------
if [[ "${COMMAND}" == "mirror" ]]; then
  require_cmd git
  [[ -f "${WORK}/records.json" ]] || die "${WORK}/records.json is missing: run collect first"
  if [[ -z "${REPO}" ]]; then
    require_cmd gh
    REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" \
      || die "no --repo and gh resolves no repository here"
  fi
  GITDIR="${GIT_DIR_ARG:-${WORK}/git}"
  if [[ ! -d "${GITDIR}/objects" ]]; then
    git init -q --bare "${GITDIR}"
    git --git-dir="${GITDIR}" remote add origin "https://github.com/${REPO}.git"
  fi
  mkdir -p "${WORK}/diffs" "${WORK}/merged"

  step "Fetching each slot's head, its base and the merged PR, from ${REPO}"
  "${PY[@]}" "${HELPER}" slots --dir "${WORK}" >"${WORK}/slots.txt"
  WANT=()
  while read -r lane _ _ base head; do
    WANT+=("${base}" "${head}" "+refs/pull/${lane}/head:refs/pr/${lane}")
  done <"${WORK}/slots.txt"
  [[ "${#WANT[@]}" -gt 0 ]] || die "no lane has a finished replay with a known arm: nothing to mirror"
  git --git-dir="${GITDIR}" fetch -q --filter=blob:none origin "${WANT[@]}" </dev/null \
    || die "git could not fetch the replay heads from ${REPO}"

  step "Writing each replay's diff, and each merged PR's"
  diffs=0
  while read -r lane _ workflow_id base head; do
    git --git-dir="${GITDIR}" diff --no-renames --name-only "${base}" "${head}" \
      >"${WORK}/diffs/${workflow_id}.files" </dev/null
    git --git-dir="${GITDIR}" diff --no-renames -U0 "${base}" "${head}" \
      >"${WORK}/diffs/${workflow_id}.patch" </dev/null
    if [[ ! -f "${WORK}/merged/${lane}.patch" ]]; then
      # Only the lane's own files: a merge from main brought the rest.
      FILES=()
      while read -r path; do
        [[ -n "${path}" ]] && FILES+=("${path}")
      done < <(jq -r --argjson n "${lane}" '.corpus[] | select(.number == $n) | (.merged_files // [])[]' \
        "${WORK}/plan.json")
      git --git-dir="${GITDIR}" diff --no-renames -U0 "${base}" "refs/pr/${lane}" -- \
        ${FILES[@]+"${FILES[@]}"} >"${WORK}/merged/${lane}.patch" </dev/null
    fi
    diffs=$((diffs + 1))
  done <"${WORK}/slots.txt"
  info "${diffs} replay diff(s) written"
  "${PY[@]}" "${HELPER}" records --dir "${WORK}" >"${WORK}/records.json"

  "${PY[@]}" "${HELPER}" refspecs --records "${WORK}/records.json" >"${WORK}/refspecs.txt"
  SPECS=()
  while read -r spec; do
    [[ -n "${spec}" ]] || continue
    [[ "${spec}" =~ ${REFSPEC_RE} ]] || die "refusing to push ${spec}: not a replay branch refspec"
    SPECS+=("${spec}")
  done <"${WORK}/refspecs.txt"
  info "${#SPECS[@]} replay branch(es) to push, all under refs/heads/replay/"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '%s\n' ${SPECS[@]+"${SPECS[@]}"}
    exit 0
  fi
  [[ "${#SPECS[@]}" -gt 0 ]] || { ok "nothing to push"; exit 0; }
  confirm "Push ${#SPECS[@]} replay/<lane>/<arm> branch(es) to ${REPO}. No pull request is opened." "MIRROR"
  pushed=0
  refused=0
  for spec in "${SPECS[@]}"; do
    if git --git-dir="${GITDIR}" push -q origin "${spec}" </dev/null 2>"${WORK}/push.err"; then
      pushed=$((pushed + 1))
    else
      refused=$((refused + 1))
      warn "${spec##*:} was not pushed:"
      redact <"${WORK}/push.err" >&2 || true
    fi
  done
  ok "${pushed} replay branch(es) pushed, ${refused} refused"
  [[ "${refused}" -eq 0 ]] || exit 2
  exit 0
fi

# --- score --------------------------------------------------------------------
[[ -f "${WORK}/records.json" ]] || die "${WORK}/records.json is missing: run collect first"
hr
status=0
"${PY[@]}" "${HELPER}" score --dir "${WORK}" ${SCORE_ARGS[@]+"${SCORE_ARGS[@]}"} 2>&1 \
  | redact || status=$?
exit "${status}"
