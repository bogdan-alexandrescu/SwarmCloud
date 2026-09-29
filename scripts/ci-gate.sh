#!/usr/bin/env bash
# ci-gate: one check that passes only when every application.yml and
# terraform.yml run at a commit passed -- the check main's ruleset requires in
# their place.
#
# OWNER DECISION, 2026-09-29. main's ruleset (main-protection, id 24160219)
# requires only security.yml's four checks. application.yml and terraform.yml
# carry a `pull_request` path filter, and GitHub treats a required check whose
# workflow was filtered out as PENDING FOREVER, not as passed -- so none of
# their jobs can be required directly, and nothing required held a pull request
# on its unit tests. This script runs as the `ci-gate` job of ci-gate.yml, on
# every pull request and every push to main, and answers one question for the
# head commit: did everything those two workflows ran for it pass?
#
#   exit 0  every gated workflow the change triggered has a run at this commit
#           that concluded `success`, and every job in it concluded success,
#           skipped or neutral. A workflow the change did not trigger, and
#           that has no run, counts as passed.
#   exit 1  anything else, with the reason on the last line: a job failed; a
#           run was cancelled, timed out, awaits a maintainer's approval or
#           failed to start; a workflow the changed paths trigger never got a
#           run within CI_GATE_APPEAR; a run is still going after
#           CI_GATE_WAIT; or the GitHub API could not be read.
#
# THE RACE AT THE START. ci-gate starts on the same event as the workflows it
# waits for, and often before their runs exist. "No run yet" and "not
# triggered" look identical in the API, so the script computes which gated
# workflows the change SHOULD trigger, from the `paths` lists in the workflow
# files themselves -- read here, never restated (CLAUDE.md: the mirrored copy
# is the defect) -- and waits for those. A workflow it did not predict is still
# judged if a run of it shows up: the gate keeps looking for CI_GATE_SETTLE
# seconds after it starts before it will pass, so a late run the path model
# missed cannot slip by.
#
# WHAT `skipped` MEANS. A job skipped by its own `if:` (application.yml's
# `build images` on a pull request, terraform.yml's `plan`) is listed as
# skipped inside a run that concluded `success`: that is a pass. A run that
# failed to start, or whose jobs were skipped because something they need
# failed, does not conclude `success`, and fails the gate whatever its jobs
# say. So the run's conclusion is the authority and the jobs are its detail.
#
# WHY THE ACTIONS API AND NOT CHECK-RUN NAMES. A check run is named by its
# job's `name:`, which is not unique across workflows and says nothing about
# which workflow reported it, nor whether that workflow started at all. The
# runs endpoint filters by head_sha and event and names each run's workflow
# file, and its `startup_failure` conclusion is exactly "failed to start".
#
# Usage:
#   scripts/ci-gate.sh wait --event pull_request|push --sha <40 hex>
#                           (--base <sha> | --changed FILE) [--repo owner/name]
#   scripts/ci-gate.sh expected --event EVENT --changed FILE
#   scripts/ci-gate.sh paths --workflow FILE --event EVENT
#   scripts/ci-gate.sh workflows
#
# `--base` is the pull request's base sha (the changed files are
# `git diff base...head`, the three-dot diff GitHub's path filter uses) or a
# push's `before` (an all-zero `before` means "unknown": every gated workflow
# with a path filter is then expected). `--changed` takes the list directly.
#
# Needs `gh` authenticated with actions: read (GH_TOKEN) and jq.
# CI_GATE_WAIT (default 5400s) bounds the whole wait, CI_GATE_APPEAR (600s) the
# wait for an expected run to be created, CI_GATE_SETTLE (60s) the least time
# spent looking before a pass, CI_GATE_POLL (60s) the interval between looks,
# CI_GATE_API_TRIES (5) the consecutive failed reads tolerated.

set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# The workflows ci-gate stands for: exactly the ones with a `pull_request` path
# filter. security.yml has none and is required directly. test_ci_gate.py
# fails when a path-filtered workflow is added without being listed here.
GATED_WORKFLOWS="${CI_GATE_WORKFLOWS:-application.yml terraform.yml}"
WORKFLOW_DIR="${CI_GATE_WORKFLOW_DIR:-${REPO_ROOT}/.github/workflows}"
# 90 minutes. The job's own timeout-minutes is 100, so the script says what was
# still pending before GitHub kills it without a word.
WAIT="${CI_GATE_WAIT:-5400}"
# A run is created within seconds of its event; ten minutes is for GitHub
# having a slow day, and for a queue of pull requests arriving at once.
APPEAR="${CI_GATE_APPEAR:-600}"
# One minute of looking before any pass: what lets a run the path model did
# not predict be seen, since runs of one event are created together.
SETTLE="${CI_GATE_SETTLE:-60}"
# 60s. A look is one API call (plus one per newly finished run), and the
# repository's GITHUB_TOKEN budget is 1,000 an hour shared by every workflow;
# ten pull requests gating at once spend about 600 of it at this interval.
POLL="${CI_GATE_POLL:-60}"
API_TRIES="${CI_GATE_API_TRIES:-5}"

usage() { sed -n '2,64p' "$0" | sed 's/^# \{0,1\}//'; }

gated_list() {
  local -a list=()
  read -r -a list <<<"${GATED_WORKFLOWS}"
  [[ ${#list[@]} -gt 0 ]] || die "CI_GATE_WORKFLOWS is empty: there is nothing to gate"
  printf '%s\n' "${list[@]}"
}

# ---------------------------------------------------------------------------
# paths FILE EVENT: what a workflow's trigger for EVENT filters on.
#   none          the workflow does not run on EVENT
#   all           it runs on EVENT with no path filter
#   paths + list  it runs on EVENT when a changed file matches one of these
# Any shape this does not model -- an inline `on:`, an inline paths list,
# paths-ignore, a negated pattern -- is refused, never guessed: a guess either
# waits for a run that never comes or passes without one that did.
# ---------------------------------------------------------------------------
PATHS_AWK="$(cat <<'AWK'
function lead(s) { match(s, /^ */); return RLENGTH }
function unquote(s) {
  if (length(s) >= 2 && ((substr(s, 1, 1) == "\"" && substr(s, length(s), 1) == "\"") ||
                         (substr(s, 1, 1) == "'" && substr(s, length(s), 1) == "'")))
    return substr(s, 2, length(s) - 2)
  return s
}
function keyof(s,   k) { k = s; sub(/[ \t]*:.*$/, "", k); return unquote(k) }
function valof(s,   v) {
  v = s; sub(/^[^:]*:/, "", v); sub(/^[ \t]+/, "", v); sub(/[ \t]+#.*$/, "", v); sub(/[ \t]+$/, "", v)
  return v
}
function refuse(msg) { printf "%s: %s\n", FILENAME, msg > "/dev/stderr"; bad = 1; exit 2 }
BEGIN { in_on = 0; ev_ind = -1; key_ind = -1; mine = 0; found = 0; has_paths = 0; in_paths = 0; n = 0; bad = 0 }
{
  line = $0; sub(/\r$/, "", line)
  if (line ~ /^[ \t]*$/ || line ~ /^[ \t]*#/) next
  ind = lead(line); text = substr(line, ind + 1)
  if (ind == 0) {
    in_on = 0; mine = 0; in_paths = 0
    if (keyof(text) == "on") {
      if (valof(text) != "")
        refuse("its on: key is not a block mapping (" valof(text) "); ci-gate models only the block form")
      in_on = 1
    }
    next
  }
  if (!in_on) next
  if (ev_ind < 0) ev_ind = ind
  if (ind == ev_ind) {
    mine = 0; in_paths = 0; key_ind = -1
    if (keyof(text) == event) {
      found = 1; mine = 1
      v = valof(text)
      if (v != "" && v != "{}" && v != "null" && v != "~")
        refuse("its " event " trigger is written inline (" v "); ci-gate models only the block form")
    }
    next
  }
  if (!mine) next
  if (key_ind < 0) key_ind = ind
  if (ind == key_ind && substr(text, 1, 2) != "- ") {
    in_paths = 0
    k = keyof(text)
    if (k == "paths") {
      has_paths = 1; in_paths = 1
      if (valof(text) != "") refuse("its " event " paths list is written inline; ci-gate reads only a block list")
    } else if (k == "paths-ignore") {
      refuse("its " event " trigger uses paths-ignore, which ci-gate does not model")
    }
    next
  }
  if (in_paths) {
    if (substr(text, 1, 2) != "- ") refuse("unexpected line in its " event " paths list: " text)
    p = substr(text, 3); sub(/^[ \t]+/, "", p); sub(/[ \t]+#.*$/, "", p); sub(/[ \t]+$/, "", p); p = unquote(p)
    if (substr(p, 1, 1) == "!") refuse("its " event " paths list has a negated pattern (" p "), which ci-gate does not model")
    paths[++n] = p
  }
}
END {
  if (bad) exit 2
  if (!found) { print "none"; exit 0 }
  if (!has_paths) { print "all"; exit 0 }
  print "paths"
  for (i = 1; i <= n; i++) print paths[i]
}
AWK
)"

paths_of() {
  local file="$1" event="$2"
  [[ -f "${file}" ]] || die "no workflow file at ${file}"
  awk -v event="${event}" "${PATHS_AWK}" "${file}"
}

# GitHub's filter globs as anchored regexes: `**/` is any run of directories
# (none included), `**` anything, `*` anything but `/`, `?` one character but
# `/`. Everything else is literal. Exit 0 when any changed file matches.
# shellcheck disable=SC2016  # $pats, $files and $res are jq's, expanded by jq
MATCH_JQ='
def glob_re:
  [ scan("\\*\\*/|\\*\\*|\\*|\\?|.") ]
  | map(if . == "**/" then "(?:.*/)?"
        elif . == "**" then ".*"
        elif . == "*" then "[^/]*"
        elif . == "?" then "[^/]"
        elif test("^[A-Za-z0-9_/-]$") then .
        elif . == "\\" or . == "]" or . == "[" or . == "^" then "\\" + .
        else "[" + . + "]" end)
  | "^" + join("") + "$";
($pats | split("\n") | map(select(length > 0) | glob_re)) as $res
| any(($files | split("\n") | map(select(length > 0)))[]; . as $f | any($res[]; . as $r | $f | test($r)))
'

# expected_one WORKFLOW EVENT CHANGED_FILE|"" -> exit 0 when the change
# triggers it. An empty CHANGED_FILE means the change is unknown, and a
# path-filtered workflow is then expected.
expected_one() {
  local wf="$1" event="$2" changed="$3" out kind
  out="$(paths_of "${WORKFLOW_DIR}/${wf}" "${event}")" || exit 2
  kind="$(printf '%s\n' "${out}" | head -n 1)"
  case "${kind}" in
    none) return 1 ;;
    all) return 0 ;;
    paths)
      [[ -n "${changed}" ]] || return 0
      # jq -e: 0 is a match, 1 is none; anything else is jq failing, which
      # must not be read as "not triggered".
      local rc=0
      jq -en --arg pats "$(printf '%s\n' "${out}" | tail -n +2)" --rawfile files "${changed}" "${MATCH_JQ}" \
        >/dev/null || rc=$?
      case "${rc}" in
        0|1) return "${rc}" ;;
        *) die "could not match the changed files against ${wf}'s paths (jq exit ${rc})" ;;
      esac
      ;;
    *) die "unexpected answer from the paths reader for ${wf}: ${kind}" ;;
  esac
}

cmd_workflows() { gated_list; }

cmd_paths() {
  local file="" event=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --workflow) file="$2"; shift 2 ;;
      --event)    event="$2"; shift 2 ;;
      *) die "paths: unknown argument: $1" ;;
    esac
  done
  [[ -n "${file}" && -n "${event}" ]] || die "paths needs --workflow FILE and --event EVENT"
  paths_of "${file}" "${event}"
}

cmd_expected() {
  local event="" changed="" wf
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --event)   event="$2"; shift 2 ;;
      --changed) changed="$2"; shift 2 ;;
      *) die "expected: unknown argument: $1" ;;
    esac
  done
  [[ -n "${event}" && -f "${changed}" ]] || die "expected needs --event EVENT and --changed FILE"
  while IFS= read -r wf; do
    if expected_one "${wf}" "${event}" "${changed}"; then printf '%s\n' "${wf}"; fi
  done < <(gated_list)
}

# ---------------------------------------------------------------------------
# wait: poll the runs at the head sha until every gated workflow is decided.
# ---------------------------------------------------------------------------
fail_with() {
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    printf '::error title=ci-gate::%s\n' "$*" >&2
  fi
  die "$*"
}

summary() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$*" >>"${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
  fi
}

cmd_wait() {
  local event="" sha="" base="" changed="" repo="${GITHUB_REPOSITORY:-}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --event)   event="$2"; shift 2 ;;
      --sha)     sha="$2"; shift 2 ;;
      --base)    base="$2"; shift 2 ;;
      --changed) changed="$2"; shift 2 ;;
      --repo)    repo="$2"; shift 2 ;;
      *) die "wait: unknown argument: $1" ;;
    esac
  done
  case "${event}" in
    pull_request|push) ;;
    *) die "--event must be pull_request or push, not '${event}'" ;;
  esac
  [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] \
    || die "--sha must be a full 40-character commit id, not '${sha}': runs are found by it exactly"
  [[ "${repo}" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] \
    || die "no repository to ask: set GITHUB_REPOSITORY or pass --repo owner/name"
  local knob
  for knob in WAIT APPEAR SETTLE API_TRIES; do
    [[ "${!knob}" =~ ^[0-9]+$ ]] || die "CI_GATE_${knob} must be a whole number, not '${!knob}'"
  done
  [[ "${POLL}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "CI_GATE_POLL must be a number of seconds, not '${POLL}'"
  require_cmd gh jq

  local work
  work="$(mktemp -d "${TMPDIR:-/tmp}/swarm-ci-gate.XXXXXX")"
  # shellcheck disable=SC2064  # expand now: work is local to this function
  trap "rm -rf '${work}'" EXIT

  # Which files changed. Unknown (a push with no `before`) leaves CHANGED empty.
  local changed_file=""
  if [[ -n "${changed}" ]]; then
    [[ -f "${changed}" ]] || die "--changed ${changed}: no such file"
    changed_file="${changed}"
  elif [[ -n "${base}" && ! "${base}" =~ ^0+$ ]]; then
    require_cmd git
    changed_file="${work}/changed.txt"
    local range="${base}...${sha}"
    [[ "${event}" == push ]] && range="${base}..${sha}"
    git -C "${REPO_ROOT}" -c core.quotepath=false diff --name-only --no-renames "${range}" >"${changed_file}" \
      || fail_with "could not list the files changed in ${range}; the checkout needs fetch-depth: 0"
  fi

  local -a wfs=() expected=() state=() detail=()
  local wf
  while IFS= read -r wf; do
    wfs+=("${wf}")
    if expected_one "${wf}" "${event}" "${changed_file}"; then expected+=(1); else expected+=(0); fi
    state+=("pending"); detail+=("")
  done < <(gated_list)
  # gated_list runs in a subshell here, so its own refusal cannot stop us.
  [[ ${#wfs[@]} -gt 0 ]] || die "no workflows to gate (CI_GATE_WORKFLOWS='${GATED_WORKFLOWS}')"

  info "ci-gate for ${sha} (${event}) in ${repo}"
  if [[ -n "${changed_file}" ]]; then
    dim "  $(grep -c . "${changed_file}" || true) changed file(s)"
  else
    dim "  changed files unknown: every path-filtered workflow is expected"
  fi
  local i
  for i in "${!wfs[@]}"; do
    if [[ "${expected[$i]}" == 1 ]]; then
      dim "  ${wfs[$i]}: triggered by this change -- waiting for its run"
    else
      dim "  ${wfs[$i]}: not triggered by this change -- judged only if a run of it appears"
    fi
  done

  local api_err="${work}/api.err" runs="${work}/runs.json" jobs="${work}/jobs.json"
  local tries=0 started=${SECONDS} elapsed read_ok pending failed id status conclusion url bad njobs
  while true; do
    elapsed=$(( SECONDS - started ))
    read_ok=1
    if ! gh api -H "Accept: application/vnd.github+json" \
        "repos/${repo}/actions/runs?head_sha=${sha}&event=${event}&per_page=100" \
        </dev/null >"${runs}" 2>"${api_err}"; then
      read_ok=0
    fi

    pending=0; failed=0
    if [[ "${read_ok}" == 1 ]]; then
      for i in "${!wfs[@]}"; do
        wf="${wfs[$i]}"
        # "-" for an empty field: tab is IFS whitespace, so `read` would
        # collapse two adjacent tabs and shift every later field left.
        IFS=$'\t' read -r id status conclusion url < <(jq -r --arg p ".github/workflows/${wf}" '
            [ (.workflow_runs // [])[] | select(.path == $p or ((.path // "") | startswith($p + "@"))) ]
            | sort_by(.created_at, .id) | last
            | if . == null then ["-", "-", "-", "-"]
              else [(.id | tostring), (.status // "-"), (.conclusion // "-"), (.html_url // "-")] end
            | @tsv' "${runs}") || { read_ok=0; break; }

        if [[ "${id}" == "-" ]]; then
          if [[ "${expected[$i]}" == 1 ]]; then
            if (( elapsed >= APPEAR )); then
              state[i]="fail"
              detail[i]="the changed paths trigger ${wf}, and no ${event} run of it at ${sha} appeared within ${APPEAR}s"
              failed=1
            else
              state[i]="pending"; detail[i]="no run yet"; pending=1
            fi
          else
            state[i]="pass"; detail[i]="not triggered by the changed paths, and no run at this commit"
          fi
          continue
        fi

        if [[ "${status}" != "completed" ]]; then
          state[i]="pending"; detail[i]="run ${url} is ${status}"; pending=1
          continue
        fi

        if [[ "${conclusion}" == "startup_failure" ]]; then
          state[i]="fail"
          detail[i]="${wf}'s run ${url} failed to start (startup_failure): none of its jobs ran, so none of them passed"
          failed=1
          continue
        fi

        if ! gh api -H "Accept: application/vnd.github+json" \
            "repos/${repo}/actions/runs/${id}/jobs?filter=latest&per_page=100" \
            </dev/null >"${jobs}" 2>"${api_err}"; then
          read_ok=0; break
        fi
        njobs="$(jq '(.jobs // []) | length' "${jobs}")" || { read_ok=0; break; }
        bad="$(jq -r '[ (.jobs // [])[]
                        | select(.status != "completed"
                                 or ((.conclusion // "") | IN("success", "skipped", "neutral") | not))
                        | "\(.name)=\(.conclusion // .status) \(.html_url // "")" ] | join("; ")' "${jobs}")" \
          || { read_ok=0; break; }

        if [[ "${conclusion}" == "success" && -z "${bad}" && "${njobs}" -gt 0 ]]; then
          state[i]="pass"; detail[i]="run ${url} succeeded (${njobs} job(s); skipped jobs were skipped by their own if:)"
        else
          state[i]="fail"
          case "${conclusion}" in
            success)
              if [[ "${njobs}" -eq 0 ]]; then
                detail[i]="${wf}'s run ${url} concluded success with no jobs listed; nothing ran, so nothing passed"
              else
                detail[i]="${wf}'s run ${url} concluded success but its jobs did not: ${bad}"
              fi ;;
            action_required)
              detail[i]="${wf}'s run ${url} concluded action_required: it waits for a maintainer to approve it${bad:+ -- ${bad}}" ;;
            *)
              detail[i]="${wf}'s run ${url} concluded ${conclusion}${bad:+: ${bad}}" ;;
          esac
          failed=1
        fi
      done
    fi

    if [[ "${read_ok}" == 0 ]]; then
      tries=$(( tries + 1 ))
      local reason
      reason="$(redact <"${api_err}" | tr -d '\r' | grep -v '^[[:space:]]*$' | head -n 1 || true)"
      if (( tries >= API_TRIES )); then
        fail_with "could not read the workflow runs at ${sha} after ${tries} tries: ${reason:-no reason given}"
      fi
      warn "could not read the workflow runs (${tries}/${API_TRIES}): ${reason:-no reason given}"
      sleep "${POLL}"
      continue
    fi
    tries=0

    if [[ "${failed}" == 1 || "${pending}" == 0 ]]; then
      if [[ "${failed}" == 0 ]] && (( elapsed < SETTLE )); then
        sleep "${POLL}"
        continue
      fi
      break
    fi

    if (( elapsed >= WAIT )); then
      local still=""
      for i in "${!wfs[@]}"; do
        [[ "${state[$i]}" == pending ]] && still="${still}${still:+; }${wfs[$i]}: ${detail[$i]}"
      done
      summary "ci-gate: still waiting after ${WAIT}s -- ${still}"
      fail_with "still waiting after ${WAIT}s for ${still}"
    fi
    sleep "${POLL}"
  done

  summary "### ci-gate for \`${sha}\` (${event})"
  summary ""
  summary "| workflow | verdict | detail |"
  summary "|---|---|---|"
  local first_fail=""
  for i in "${!wfs[@]}"; do
    log "  ${wfs[$i]}: ${state[$i]} -- ${detail[$i]}"
    summary "| \`${wfs[$i]}\` | ${state[$i]} | ${detail[$i]} |"
    if [[ "${state[$i]}" == fail && -z "${first_fail}" ]]; then first_fail="${detail[$i]}"; fi
  done
  [[ -z "${first_fail}" ]] || fail_with "${first_fail}"
  ok "every gated workflow that ran at ${sha} passed"
}

case "${1:-}" in
  wait)      shift; cmd_wait "$@" ;;
  expected)  shift; cmd_expected "$@" ;;
  paths)     shift; cmd_paths "$@" ;;
  workflows) shift; cmd_workflows "$@" ;;
  -h|--help) usage ;;
  *) usage >&2; die "unknown command '${1:-}': wait, expected, paths or workflows" ;;
esac
