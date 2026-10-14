#!/usr/bin/env bash
# quality-baseline: re-measure the context-quality baseline (lane CTXQ-M) --
# the §2.6 metrics of docs/design/context-quality.md, over the newest merged
# SwarmCloud lane PRs.
#
# WHY THIS EXISTS. §2.1's baseline (0.45 red runs per PR, 81.3% first-pass,
# 13.3% hand fix-up, ...) was measured once, by hand, with the commands in §9.
# Every later phase of the design is judged against it: the offline replay,
# the A/B and the default-on thresholds (§6). A number nobody can re-measure
# is a number nobody can compare against, so the definitions live in
# scripts/lib/quality_metrics.py (tested in tests/unit/scripts/
# test_quality_metrics.py) and this script only fetches.
#
# WHAT IT READS, all read-only:
#   * GitHub (gh): the last --pages pages of closed PRs and every open PR; the
#     pull_request Actions runs since the window opened, in --run-hours ranges
#     (the list API stops at 1,000 results per query, so a range that holds
#     more is an error, never a silent cut); issues labelled `bug`; comments on
#     wave epics; and the comments of issue-run PRs that closed unmerged.
#   * git: the PR heads and main, history only (--filter=blob:none), into
#     --git-dir. Each lane's own commits are `git log main..refs/pr/N`.
#   * swarm-api, only with --api: runs (M9) and each lane's task (M12); with
#     --repo-id too, the impact answer for each lane PR (M11). The impact route
#     is a POST, and it only reads. Without --api those three print "not
#     measured", which is not zero.
#
# WHAT IT PRINTS. M1-M12, each with how many PRs, runs, commits or issues it
# visited, so an empty read can never pass for a clean one. Every line goes
# through `redact`. It changes nothing on GitHub or in swarm-api.
#
# Usage:
#   scripts/quality-baseline.sh [--limit N] [--pages P] [--repo owner/name]
#                               [--api] [--repo-id ID] [--run-hours H]
#                               [--issue-runs-since ISO] [--operator-author NAME]...
#                               [--git-dir DIR] [--out DIR] [--json]
#
#   --limit N            lane PRs in the window, newest merged first (default 150)
#   --pages P            pages of 100 closed PRs to read (default 3)
#   --repo R             default: GH_REPO, else the repository gh resolves here
#   --api                also read swarm-api (an operator credential; SWARM_API_TENANT picks the tenant)
#   --repo-id ID         with --api: the registered repository, for M11's impact answers
#   --run-hours H        width of each Actions runs query (default 6)
#   --issue-runs-since T M8's window start (default: the lane window's)
#   --operator-author A  M6's operator identity, repeatable (default bogdan-alexandrescu)
#   --git-dir DIR        keep the history fetch here and reuse it (default: a temp dir)
#   --out DIR            keep everything read in DIR (default: a temp dir, removed)
#   --json               print the report as one JSON document
#
# Needs `gh` (authenticated; read access), `git` and `jq`.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

LIMIT=150
PAGES=3
REPO="${GH_REPO:-}"
API=0
REPO_ID=""
RUN_HOURS=6
IR_SINCE=""
GIT_DIR_ARG=""
OUT=""
REPORT_ARGS=()

usage() { sed -n '2,49p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) [[ $# -ge 2 ]] || die "--limit needs a number"; LIMIT="$2"; shift 2 ;;
    --pages) [[ $# -ge 2 ]] || die "--pages needs a number"; PAGES="$2"; shift 2 ;;
    --repo) [[ $# -ge 2 ]] || die "--repo needs owner/name"; REPO="$2"; shift 2 ;;
    --api) API=1; shift ;;
    --repo-id) [[ $# -ge 2 ]] || die "--repo-id needs an id"; REPO_ID="$2"; shift 2 ;;
    --run-hours) [[ $# -ge 2 ]] || die "--run-hours needs a number"; RUN_HOURS="$2"; shift 2 ;;
    --issue-runs-since) [[ $# -ge 2 ]] || die "--issue-runs-since needs an ISO time"; IR_SINCE="$2"; shift 2 ;;
    --operator-author) [[ $# -ge 2 ]] || die "--operator-author needs a name"
      REPORT_ARGS+=(--operator-author "$2"); shift 2 ;;
    --git-dir) [[ $# -ge 2 ]] || die "--git-dir needs a directory"; GIT_DIR_ARG="$2"; shift 2 ;;
    --out) [[ $# -ge 2 ]] || die "--out needs a directory"; OUT="$2"; shift 2 ;;
    --json) REPORT_ARGS+=(--json); shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ "${LIMIT}" =~ ^[1-9][0-9]*$ ]] || die "--limit must be a positive integer, got: ${LIMIT}"
[[ "${PAGES}" =~ ^[1-9][0-9]*$ ]] || die "--pages must be a positive integer, got: ${PAGES}"
[[ "${RUN_HOURS}" =~ ^[1-9][0-9]*$ ]] || die "--run-hours must be a positive integer, got: ${RUN_HOURS}"
[[ -z "${REPO_ID}" || "${API}" -eq 1 ]] || die "--repo-id reads swarm-api: give --api too"
require_cmd gh jq git
[[ "${API}" -eq 0 ]] || require_cmd curl

if [[ -z "${REPO}" ]]; then
  REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" \
    || die "no --repo and gh resolves no repository here"
fi

# `swarm_python` prints either one path or `uv run --project <root> python`.
read -r -a PY <<<"$(swarm_python)"
HELPER="${REPO_ROOT}/scripts/lib/quality_metrics.py"

if [[ -n "${OUT}" ]]; then
  mkdir -p "${OUT}"
  WORK="$(cd "${OUT}" && pwd)"
else
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/quality-baseline.XXXXXX")"
  trap 'rm -rf "${WORK}"' EXIT INT TERM
fi
GITDIR="${GIT_DIR_ARG:-${WORK}/git}"
mkdir -p "${WORK}/commits" "${WORK}/comments"

# --- the PRs, and the lane window ------------------------------------------
step "Reading ${PAGES} page(s) of closed PRs and the open ones in ${REPO}"
: >"${WORK}/prs.stream"
p=1
while [[ "${p}" -le "${PAGES}" ]]; do
  gh api "repos/${REPO}/pulls?state=closed&per_page=100&page=${p}&sort=created&direction=desc" \
    >>"${WORK}/prs.stream" </dev/null
  p=$((p + 1))
done
gh api --paginate "repos/${REPO}/pulls?state=open&per_page=100" >>"${WORK}/prs.stream" </dev/null
jq -s 'add | unique_by(.number)' "${WORK}/prs.stream" >"${WORK}/prs.json"
info "$(jq 'length' "${WORK}/prs.json") PR(s) read"

"${PY[@]}" "${HELPER}" select --limit "${LIMIT}" --pages "${WORK}/prs.json" >"${WORK}/lanes.json"
LANES="$(jq 'length' "${WORK}/lanes.json")"
[[ "${LANES}" -gt 0 ]] || die "no merged swarm/* PR in ${PAGES} page(s): nothing was read, so nothing can be reported"
[[ "${LANES}" -ge "${LIMIT}" ]] || warn "only ${LANES} merged lane PR(s) in ${PAGES} page(s), short of --limit ${LIMIT}: raise --pages"
SINCE="$(jq -r 'map(.created_at) | min' "${WORK}/lanes.json")"
UNTIL="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
info "${LANES} lane PR(s), #$(jq -r 'map(.number) | min' "${WORK}/lanes.json")-#$(jq -r 'map(.number) | max' "${WORK}/lanes.json"), created since ${SINCE}"
printf '%s\n' "${IR_SINCE:-${SINCE}}" >"${WORK}/issue-runs-since.txt"

# --- M1-M3: the Actions runs ------------------------------------------------
step "Reading pull_request runs, ${SINCE} to ${UNTIL}, ${RUN_HOURS} h at a time"
: >"${WORK}/runs.jsonl"
"${PY[@]}" "${HELPER}" windows --since "${SINCE}" --until "${UNTIL}" --hours "${RUN_HOURS}" \
  >"${WORK}/windows.txt"
ranges=0
while read -r range; do
  [[ -n "${range}" ]] || continue
  ranges=$((ranges + 1))
  total="$(gh api "repos/${REPO}/actions/runs?event=pull_request&per_page=1&created=${range}" \
    --jq '.total_count' </dev/null)"
  [[ "${total}" -le 1000 ]] \
    || die "${total} runs in ${range}: the list API stops at 1,000. Lower --run-hours"
  gh api --paginate "repos/${REPO}/actions/runs?event=pull_request&per_page=100&created=${range}" \
    --jq '.workflow_runs[] | {id, name, path, event, status, conclusion, head_branch, created_at}' \
    >>"${WORK}/runs.jsonl" </dev/null
done <"${WORK}/windows.txt"
jq -s 'unique_by(.id)' "${WORK}/runs.jsonl" >"${WORK}/runs.json"
info "${ranges} range(s) read, $(jq 'length' "${WORK}/runs.json") run(s)"

# --- M6, M10: each lane's own commits ---------------------------------------
step "Fetching main and ${LANES} PR head(s), history only"
if [[ ! -d "${GITDIR}/objects" ]]; then
  git init -q --bare "${GITDIR}"
  git --git-dir="${GITDIR}" remote add origin "https://github.com/${REPO}.git"
fi
REFSPECS=("+refs/heads/main:refs/remotes/main")
while read -r n; do
  REFSPECS+=("+refs/pull/${n}/head:refs/pr/${n}")
done < <(jq -r '.[].number' "${WORK}/lanes.json")
git --git-dir="${GITDIR}" fetch -q --filter=blob:none origin "${REFSPECS[@]}" </dev/null \
  || die "git could not fetch the PR heads from ${REPO}"
heads=0
commits=0
while read -r n; do
  if ! git --git-dir="${GITDIR}" rev-parse -q --verify "refs/pr/${n}" >/dev/null; then
    warn "#${n}: its head was not fetched; it is reported as not read"
    continue
  fi
  # --no-renames: rename detection would fetch blobs this history-only clone skipped.
  git --git-dir="${GITDIR}" log --no-renames --name-only \
    --format='%x1e%H%x1f%P%x1f%an%x1f%cI' "refs/remotes/main..refs/pr/${n}" \
    >"${WORK}/commits/${n}.log" </dev/null
  heads=$((heads + 1))
  commits=$((commits + $(grep -c $'\x1e' "${WORK}/commits/${n}.log" || true)))
done < <(jq -r '.[].number' "${WORK}/lanes.json")
info "${heads} head(s) read, ${commits} commit(s) outside main"

# --- M7: bugs and wave-epic comments ----------------------------------------
step "Reading bug issues and wave-epic comments since ${SINCE}"
gh api --paginate "repos/${REPO}/issues?state=all&labels=bug&since=${SINCE}&per_page=100" \
  --jq '.[] | {number, title, body, created_at, labels: [.labels[].name], pull_request}' \
  </dev/null | jq -s 'map(if .pull_request == null then del(.pull_request) else . end)' \
  >"${WORK}/bugs.json"
: >"${WORK}/epic-comments.jsonl"
epics=0
while read -r epic; do
  [[ -n "${epic}" ]] || continue
  epics=$((epics + 1))
  gh api --paginate "repos/${REPO}/issues/${epic}/comments?since=${SINCE}&per_page=100" \
    --jq ".[] | {issue: ${epic}, body, created_at}" >>"${WORK}/epic-comments.jsonl" </dev/null
done < <(gh api --paginate "repos/${REPO}/issues?state=all&labels=epic&since=${SINCE}&per_page=100" \
  --jq '.[] | select(.pull_request == null) | select(.title | test("wave"; "i")) | .number' </dev/null)
jq -s '.' "${WORK}/epic-comments.jsonl" >"${WORK}/epic-comments.json"
info "$(jq 'length' "${WORK}/bugs.json") bug issue(s), ${epics} wave epic(s) with $(jq 'length' "${WORK}/epic-comments.json") comment(s)"

# --- M8: the closing comments of issue-run PRs that closed unmerged ---------
step "Reading the closing comments of issue-run PRs closed unmerged"
closed_runs=0
while read -r n; do
  [[ -n "${n}" ]] || continue
  closed_runs=$((closed_runs + 1))
  gh api --paginate "repos/${REPO}/issues/${n}/comments?per_page=100" \
    --jq '.[] | {body, created_at}' </dev/null | jq -s '.' >"${WORK}/comments/${n}.json"
done < <("${PY[@]}" "${HELPER}" issue-run-prs --pages "${WORK}/prs.json" \
  --since "$(cat "${WORK}/issue-runs-since.txt")")
info "${closed_runs} issue-run PR(s) closed unmerged, comments read"

# --- M9, M11, M12: swarm-api ------------------------------------------------
# One read, redirected to a file rather than captured with $(...): a command
# substitution is a SUBSHELL, so API_STATUS would never reach this shell and
# every response would read as a success.
api_read() {
  local out="$1" path="$2"
  if ! api_request GET "${API_PREFIX}${path}" >"${out}"; then
    return 1
  fi
}

if [[ "${API}" -eq 1 ]]; then
  step "Reading swarm-api runs created since ${SINCE}"
  : >"${WORK}/api-runs.json"
  token=""
  pages=0
  while :; do
    query="/runs?limit=200"
    [[ -z "${token}" ]] || query="${query}&page_token=${token}"
    if ! api_read "${WORK}/page.json" "${query}"; then
      err "GET ${API_PREFIX}${query} answered HTTP ${API_STATUS}"
      redact <"${WORK}/page.json" >&2 || true
      exit 2
    fi
    cat "${WORK}/page.json" >>"${WORK}/api-runs.json"
    printf '\n' >>"${WORK}/api-runs.json"
    pages=$((pages + 1))
    token="$(jq -r '.next_page_token // ""' "${WORK}/page.json")"
    older="$(jq -r --arg since "${SINCE}" '[.runs[] | select(.created_at < $since)] | length' "${WORK}/page.json")"
    [[ -n "${token}" && "${older}" -eq 0 && "${pages}" -lt 50 ]] || break
  done
  info "${pages} page(s), $(jq -s 'map(.runs | length) | add // 0' "${WORK}/api-runs.json") run(s)"

  step "Reading each lane's task"
  : >"${WORK}/tasks.json"
  tasks=0
  unread=0
  while read -r task_id; do
    [[ -n "${task_id}" ]] || continue
    if api_read "${WORK}/page.json" "/tasks/${task_id}"; then
      cat "${WORK}/page.json" >>"${WORK}/tasks.json"
      printf '\n' >>"${WORK}/tasks.json"
      tasks=$((tasks + 1))
    elif [[ "${API_STATUS}" == "404" ]]; then
      unread=$((unread + 1))
    else
      err "GET ${API_PREFIX}/tasks/${task_id} answered HTTP ${API_STATUS}"
      redact <"${WORK}/page.json" >&2 || true
      exit 2
    fi
  done < <(jq -r '.[].head.ref | capture("^swarm/(?<t>task_[A-Za-z0-9]+)").t' "${WORK}/lanes.json")
  info "${tasks} task(s) read, ${unread} not found (another tenant's, or gone): not counted as \$0"

  if [[ -n "${REPO_ID}" ]]; then
    step "Reading the impact answer of each lane PR (repository ${REPO_ID})"
    mkdir -p "${WORK}/impact"
    impacts=0
    failed=0
    while read -r n; do
      if api_request POST "${API_PREFIX}/repositories/${REPO_ID}/impact" "{\"pull_request\": ${n}}" \
          >"${WORK}/impact/${n}.json"; then
        impacts=$((impacts + 1))
      else
        warn "#${n}: impact answered HTTP ${API_STATUS}; left out of M11"
        rm -f "${WORK}/impact/${n}.json"
        failed=$((failed + 1))
      fi
    done < <(jq -r '.[].number' "${WORK}/lanes.json")
    info "${impacts} impact answer(s) read, ${failed} refused"
  fi
fi

# --- the numbers -------------------------------------------------------------
hr
[[ -z "${OUT}" ]] || dim "everything read is kept in ${WORK}"
status=0
"${PY[@]}" "${HELPER}" report --dir "${WORK}" ${REPORT_ARGS[@]+"${REPORT_ARGS[@]}"} 2>&1 \
  | redact || status=$?
exit "${status}"
