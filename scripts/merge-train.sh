#!/usr/bin/env bash
# merge-train: test several `ready` pull requests together on main, and land
# each as its own squash once the combination is green.
#
# WHY THIS EXISTS. Owner decision 2026-10-14 ("A1 Merge train": keep main a
# tested tree). Ruleset 24160219 (`main-protection`) has strict up-to-date on,
# so a green `ready` pull request waits for its branch to be brought up to date
# with main and its CI to run again before GitHub merges it: a median 17
# minutes after green, p90 173, and 2.93 CI runs per pull request (measured
# 2026-10-14). GitHub's merge queue would fix that, and is refused on a
# user-owned repository (docs/ci.md, "Merging through the merge queue"). The
# train is the same idea, run by a workflow: .github/workflows/merge-train.yml
# runs `tick` when a train's or a pull request's CI finishes, when `ready` is
# added, and every five minutes as the fallback; docs/runbooks/merge-train.md
# is how the owner turns it on.
#
# ONE TICK.
#   * Every `ready` pull request the train passes over for a reason that will
#     not clear by itself (a failed or cancelled check at its head, a
#     conflict, its closing keywords) is told why, once per head and reason.
#   * A train in flight (an open pull request from `train/<id>`) is judged at
#     its head: pending waits, unless it is older than
#     MERGE_TRAIN_MAX_AGE_MINUTES, when it is abandoned; green lands (unless
#     the deploy-starvation hold below holds it); red is bisected.
#   * No train in flight: up to MERGE_TRAIN_MAX_PRS (default 4) open pull
#     requests that are green at head, labelled `ready`, mergeable, not drafts,
#     not forks and aimed at main board a new one, oldest first, each still
#     passing main's closing-references and dependency checks (the same two
#     scripts auto-merge.yml's gate runs).
#
# WHAT A TRAIN IS. `train/<run id>-<attempt>` = origin/main plus each boarding
# pull request squashed in order, one commit each, titled `<title> (#N)`. A
# pull request that conflicts with one ahead of it is dropped from that train
# with a comment, and stays `ready`. The branch is opened as a DRAFT pull
# request into main with the train App's token, so application.yml,
# security.yml and terraform.yml run their `pull_request` jobs on it: exactly
# the checks ruleset 24160219 requires, at the train's head. Not a `push`
# trigger on `train/**`: a push runs application.yml's `build` job, which mints
# a Google identity that the workload identity pool grants to refs/heads/main
# alone (terraform/bootstrap/wif.tf, github_allowed_refs), so every train would
# fail at `build` and so at ci-gate.
#
# LANDING. Green, the train lands every pull request through GitHub's own
# squash merge (`gh pr merge --squash --match-head-commit`), in train order,
# with the subject auto-merge.yml uses, `<title> (#N)`. Not a fast-forward of
# main to the train's head, for two reasons: the ruleset's `pull_request` rule
# refuses any direct push to main (the merge App is no bypass actor), and a
# squash pushed to main does not contain the pull request's head commit, so
# GitHub would never mark the pull request merged -- it would have to be
# closed by hand, losing its "merged" state, auto-merge.yml's close-issues job
# and every closing keyword. Each squash GitHub makes is the same three-way
# merge, onto the same main, that composed the train, so main's tree after the
# last one is the train's tested tree; the tick checks that, and says so loudly
# if it is not. Before the first merge it also checks main has not moved and
# each pull request's head is still the one tested; if either changed, nothing
# lands and the next train is built from scratch. A merge GitHub fails
# transiently (a network error, a 502, 503 or 504, a rate limit) is retried,
# three attempts in all, reading the pull request again before each so one
# whose merge went through is counted once (merge_one); a refusal stops the
# landing, and what had not landed boards a new train. It posts a `merge-train`
# commit status on each head it lands, so the ruleset can require that context
# (the runbook's change) and nothing reaches main without a green train.
#
# RED. A train of one is ejected: a comment linking the failing job, and
# `ready` removed. A larger train is halved: the first half (the larger, when
# odd) boards the next train at once, and the second is held back as suspects,
# which board the train after it. So a culprit is found in log2(N) trains and
# the innocent land on the way.
#
# THE DEPLOY-STARVATION HOLD (issue 3336). A green train does not land while
# nothing has deployed for MERGE_TRAIN_DEPLOY_RECENT_MINUTES (60) and main's
# newest `build images` job is in progress: landing would cancel that build
# (#2438), as it cancelled every one from 21:04Z on 4 November. It lands once
# that job ends or after MERGE_TRAIN_DEPLOY_HOLD_MINUTES (25), holds once per
# train, and says so once on the train (deploy_hold).
#
# DRY RUN. With vars.MERGE_TRAIN anything but `on`, or a dispatch with
# dry_run, a train is built, pushed and judged, and nothing else: no landing,
# no ejection, no comment or label on any pull request but the train's own,
# and no next train. That is how the owner sees a train go green before
# turning strict up-to-date off (docs/runbooks/merge-train.md).
#
# Subcommands. The first six are pure -- files in, lines out, no network --
# and tests/unit/scripts/test_merge_train.py runs them:
#
#   select --prs <gh pr list json> --required <names file>
#          [--allow-unknown-mergeable | --recheck-unknown]
#       Who may board. Prints `<N>\t<head sha>\t<title>` per pull request,
#       oldest first. --recheck-unknown (needs GH_REPO) re-reads an otherwise
#       eligible UNKNOWN pull request with `gh pr view`: the one network read.
#   skips --prs <gh pr list json>
#       Who will not board until someone acts: a `ready` pull request with a
#       check at its head that finished bad, or a conflict. `skip\t<N>\t<head>
#       \t<key>\t<why>`, then `item\t<N>\t<key>\t<line>` per check. The tick
#       comments once per head and key, under a hidden marker (tell_skip).
#   judge --check-runs <json> --statuses <json> --required <names file> [--workflow-runs <json>]
#       A head's checks, each by the latest run of each event (the Actions
#       runs give each check suite's event): `verdict\t<green|red|pending>`, then
#       `failed\t<name>\t<url>` per failed check.
#   plan --verdict <green|red> --train "<N...>" --suspects "<N...>"
#       What follows a judged train: `eject=`, `next=`, `suspects=`.
#   compose --repo <dir> --base <sha> --prs <tsv: N, head sha, title>
#       Builds the train in a scratch worktree of <dir>: `included\t<N>\t...`,
#       `dropped\t<N>\t<sha>\t<why>`, then `head\t<sha>`.
#   state-render key=value...  |  state-get --body <file> --key <key>
#       The state block a train pull request's body carries.
#   tell --prs <gh pr list json>
#       That telling step alone (GH_REPO, GH_TOKEN, RUN_URL).
#   hold --train <N> --body <file> [--dry-run]
#       The deploy-starvation hold alone, for green train <N> whose body is
#       <file>: `hold\t<why>` or `merge\t<why>` (GH_REPO, GH_TOKEN, RUN_URL).
#   land --base <sha> --head <sha> --prs "<N:sha>..." --id <train id>
#       The landing alone, for a green train (land_train): `rc\t<0|1|2>`,
#       `landed\t<N...>`, `reason\t<why>` (GH_REPO, GH_TOKEN, APP_TOKEN,
#       REPO_DIR, RUN_URL). A transient merge failure is retried (merge_one).
#   tick
#       One step of the train, as above. Needs GH_REPO, GH_TOKEN (the
#       workflow's token: reads, comments, labels, statuses), APP_TOKEN (the
#       merge App's: the push, the train pull request, the merges, so each
#       starts workflows as GITHUB_TOKEN events do not), REPO_DIR (a clone of
#       the repository, main checked out), RUN_ID, RUN_ATTEMPT, RUN_URL,
#       EVENT_NAME, MERGE_TRAIN, DRY_RUN_INPUT, and on `workflow_run`
#       WORKFLOW_RUN_HEAD_BRANCH (the finished run's head branch: a `train/`
#       one only judges). Neither token is ever printed.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# The commit status context the train posts on each head it lands, and which
# the runbook's ruleset change requires. Never itself a check the train waits
# for: it is posted after the train is green.
TRAIN_CONTEXT="merge-train"
TRAIN_PREFIX="train/"
STATE_OPEN="<!-- merge-train-state"
STATE_CLOSE="-->"
# Four, the owner's brief: a red train of four is found in two more trains.
MAX_PRS="${MERGE_TRAIN_MAX_PRS:-4}"
# A pull request GitHub is still computing the mergeability of is read this
# many times in all, this many seconds apart: GitHub computes it in a few
# seconds once asked, and a longer wait holds the whole tick. One still
# UNKNOWN after that is left for the next tick, five minutes on.
MERGEABLE_READS="${MERGE_TRAIN_MERGEABLE_READS:-3}"
MERGEABLE_WAIT_SECONDS="${MERGE_TRAIN_MERGEABLE_WAIT_SECONDS:-3}"
# Ninety minutes: ci-gate gives up on terraform.yml at 90 (CI_GATE_WAIT in
# application.yml), so a train still pending after that is not going to go
# green by waiting, and every `ready` pull request behind it is held.
MAX_AGE_MINUTES="${MERGE_TRAIN_MAX_AGE_MINUTES:-90}"
# THE DEPLOY-STARVATION HOLD (issue 3336; deploy_hold). Twenty-five minutes:
# `build images` runs about 19 (measured 5 November 2026), so a build already
# in progress when the train goes green finishes inside it, and a hung one
# holds the train for no longer than one of its own CI runs takes.
HOLD_MINUTES="${MERGE_TRAIN_DEPLOY_HOLD_MINUTES:-25}"
# Sixty minutes: a push normally deploys within one (its build starts 18 to 44
# minutes after the push and runs about 19), so a main with no deploy in the
# last hour is one whose builds are being cancelled, not one that is slow.
DEPLOY_RECENT_MINUTES="${MERGE_TRAIN_DEPLOY_RECENT_MINUTES:-60}"
# How many of main's newest release.yml runs are read for the last deploy, one
# jobs read each: ten cover the last few hours of merges. Only a train that is
# green while main's build is in progress reads them, once, so the hold costs
# at most 12 reads per train and 2 per held tick, against the workflow token's
# 1,000 an hour for the repository.
RELEASE_LOOK="${MERGE_TRAIN_RELEASE_LOOK:-10}"
# A merge GitHub fails transiently (a timeout, a reset connection, a 502, 503
# or 504, a rate limit) is tried again after each of these waits, in seconds:
# three attempts in all. On 5 November 2026 one i/o timeout on a train's first
# merge stopped a green train of five, and the same five ran every check again
# in a new one: about 40 minutes for one network blip. The waits sum to 40
# seconds, so a blip passes and a real outage still stops the tick quickly.
MERGE_RETRY_WAITS="${MERGE_TRAIN_MERGE_RETRY_WAITS:-10 30}"

# The header, up to the first line that is not a comment: a hand-kept line
# range drifts every time the header grows.
usage() { awk 'NR > 1 { if (!/^#/) exit; print }' "$0" | sed 's/^# \{0,1\}//'; }

is_number() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }
is_sha() { [[ "$1" =~ ^[0-9a-f]{40}$ ]]; }

# A space-separated list of pull request numbers, possibly empty.
is_number_list() { [[ -z "$1" || "$1" =~ ^[1-9][0-9]*( [1-9][0-9]*)*$ ]]; }

# ---------------------------------------------------------------------------
# select
# ---------------------------------------------------------------------------

required_json() {
  # The required names file as a JSON array; refuses an empty one, because
  # "every required check passed" over no checks is true of anything.
  local file="$1" json
  [[ -f "${file}" ]] || die "no required checks file: ${file}"
  json="$(jq -Rs 'split("\n") | map(select(length > 0)) | unique' "${file}")"
  [[ "$(jq 'length' <<<"${json}")" -gt 0 ]] || die "no required checks: a train or a pull request could never be judged green"
  printf '%s' "${json}"
}

# How select and skips read a pull request from `gh pr list`, written once.
# `candidate`: a pull request the train serves at all -- `ready`, aimed at
# main, not a draft, not a fork, not a train (needs --arg prefix). `checks`:
# the status rollup holds check runs (status + conclusion) and commit statuses
# (state) side by side; each is reduced to name / done / result / url. `bad`
# is a result that will not turn green until something is re-run or pushed.
# shellcheck disable=SC2016
ROLLUP_JQ='
    def good: . as $c | ["SUCCESS", "SKIPPED", "NEUTRAL"] | index($c) != null;
    def bad: . as $c | ["FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
                        "STARTUP_FAILURE", "STALE", "ERROR"] | index($c) != null;
    def candidate:
      .baseRefName == "main" and .isDraft == false and .isCrossRepository == false
      and any(.labels[]?; .name == "ready")
      and ((.headRefName | startswith($prefix)) | not);
    def checks:
      [ (.statusCheckRollup // [])[]
        | if .__typename == "StatusContext"
          then {name: .context, done: ((.state // "") as $s | $s != "PENDING" and $s != "EXPECTED"),
                result: (.state // ""), url: (.targetUrl // "")}
          else {name: .name,
                done: (.status == "COMPLETED"
                       or ((.conclusion // "") != "" and (.completedAt // "") != "")),
                result: (.conclusion // ""), url: (.detailsUrl // "")}
          end ];
'

reread_mergeable() {
  # The single-pull-request read, which makes GitHub compute a mergeability
  # the list reports as UNKNOWN. Prints MERGEABLE, CONFLICTING or UNKNOWN; a
  # read that fails counts as UNKNOWN, so the pull request waits a tick.
  local number="$1" reads=0 state=""
  while [[ "${reads}" -lt "${MERGEABLE_READS}" ]]; do
    [[ "${reads}" -eq 0 ]] || sleep "${MERGEABLE_WAIT_SECONDS}"
    reads=$((reads + 1))
    state="$(gh pr view "${number}" --repo "${GH_REPO}" --json mergeable --jq .mergeable </dev/null 2>/dev/null)" || state=""
    [[ "${state}" == "UNKNOWN" || -z "${state}" ]] || break
  done
  printf '%s\n' "${state:-UNKNOWN}"
}

cmd_select() {
  local prs="" required="" unknown=false recheck=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --prs) [[ $# -ge 2 ]] || die "--prs needs a value"; prs="$2"; shift 2 ;;
      --required) [[ $# -ge 2 ]] || die "--required needs a value"; required="$2"; shift 2 ;;
      --allow-unknown-mergeable) unknown=true; shift ;;
      --recheck-unknown) recheck=true; shift ;;
      *) die "select: unknown argument: $1" ;;
    esac
  done
  [[ -f "${prs}" ]] || die "select: no --prs file"
  [[ "${unknown}" == false || "${recheck}" == false ]] \
    || die "select: --allow-unknown-mergeable and --recheck-unknown are exclusive"
  [[ "${recheck}" == false || -n "${GH_REPO:-}" ]] || die "select: --recheck-unknown needs GH_REPO"
  local req
  req="$(required_json "${required}")"
  # The status rollup holds check runs (status + conclusion) and commit
  # statuses (state) side by side; each is reduced to name / done / result.
  # Green: nothing that finished failed, required or not (auto-merge.yml's gate
  # item 5), and every required name finished well. A check still running
  # that is not required does not hold it (#815).
  # A check run is done when its status is COMPLETED, OR when it carries a
  # conclusion together with a completedAt, whatever its status says -- as
  # `judge` reads it (BGD 2026-10-23, CHECK-STUCK-2, part of #2031): GitHub
  # left PR 2006's `secret scan` IN_PROGRESS with a conclusion. A commit
  # status keeps its own reading.
  # --allow-unknown-mergeable also boards a pull request whose mergeability
  # GitHub has not recomputed yet (UNKNOWN, as every open one is for a while
  # after a train lands): only for a bisection's named half, which compose
  # checks for conflicts itself. CONFLICTING never boards.
  # --recheck-unknown (TRAIN-BOARDS-UNKNOWN, part of #2688; epic #3179): right
  # after a landing the list reads every open pull request UNKNOWN, and a tick
  # boarding only MERGEABLE boarded nothing. Each otherwise eligible UNKNOWN
  # one is read once more on its own (reread_mergeable) and boards only on
  # MERGEABLE; one still UNKNOWN is left for the next tick.
  local keep_unknown="${unknown}"
  [[ "${recheck}" == false ]] || keep_unknown=true
  local n sha mergeable title
  jq -r --argjson required "${req}" --arg prefix "${TRAIN_PREFIX}" --argjson unknown "${keep_unknown}" "${ROLLUP_JQ}"'
    def green:
      checks as $c
      | ([ $c[] | select(.done and (.result | bad)) ] | length == 0)
        and all($required[]; . as $r | any($c[]; .name == $r and .done and (.result | good)));
    [ .[]
      | select(candidate)
      | select(.mergeable == "MERGEABLE" or ($unknown and .mergeable == "UNKNOWN"))
      | select((.title | test("^\\s*\\[swarm\\] task_"; "i")) | not)
      | select(green)
    ]
    | sort_by(.number)
    | .[]
    | "\(.number)\t\(.headRefOid)\t\(.mergeable)\t\(.title | gsub("[\\t\\r\\n]"; " "))"
  ' "${prs}" | while IFS=$'\t' read -r n sha mergeable title; do
    if [[ "${recheck}" == true && "${mergeable}" == "UNKNOWN" ]]; then
      mergeable="$(reread_mergeable "${n}")"
      if [[ "${mergeable}" != "MERGEABLE" ]]; then
        info "#${n} does not board: its mergeability reads ${mergeable} on a second read"
        continue
      fi
    fi
    printf '%s\t%s\t%s\n' "${n}" "${sha}" "${title}"
  done
}

# ---------------------------------------------------------------------------
# skips
# ---------------------------------------------------------------------------

cmd_skips() {
  # The `ready` pull requests that do not board for a reason that will not
  # clear by itself, read from the same list and with the same rules as
  # select: a check at the head that finished bad (failed, cancelled, timed
  # out...), whose re-run or a new push is what clears it; or a conflict with
  # main. A check still running, or a train that is full, clears by itself
  # and is not one. Per reason, `skip\t<N>\t<head>\t<key>\t<why>`, then
  # `item\t<N>\t<key>\t<markdown line>` per check behind it. <key> is the
  # reason as the hidden marker carries it: `check:<names>` or `conflict`.
  local prs=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --prs) [[ $# -ge 2 ]] || die "--prs needs a value"; prs="$2"; shift 2 ;;
      *) die "skips: unknown argument: $1" ;;
    esac
  done
  [[ -f "${prs}" ]] || die "skips: no --prs file"
  jq -r --arg prefix "${TRAIN_PREFIX}" "${ROLLUP_JQ}"'
    def one_line: gsub("[\\t\\r\\n]"; " ");
    def said: {FAILURE: "failed", ERROR: "failed", CANCELLED: "was cancelled",
               TIMED_OUT: "timed out", ACTION_REQUIRED: "needs an action",
               STARTUP_FAILURE: "failed to start", STALE: "went stale"}[.] // "failed";
    [ .[] | select(candidate) | select((.title | test("^\\s*\\[swarm\\] task_"; "i")) | not) ]
    | sort_by(.number)
    | .[]
    | . as $pr
    | ( [ checks[] | select(.done and (.result | bad)) ]
        | group_by(.name) | map(.[-1]) ) as $red
    | ( if ($red | length) > 0 then
          ("check:" + ([ $red[].name | gsub("[^A-Za-z0-9._()/-]"; "_") ] | join(","))) as $key
          | "skip\t\($pr.number)\t\($pr.headRefOid)\t\($key)\t"
            + ([ $red[] | "`\(.name | one_line)` \(.result | said)" ] | join(", ")),
            ( $red[] | "item\t\($pr.number)\t\($key)\t- [`\(.name | one_line)`](\(.url)): \(.result | said)" )
        else empty end ),
      ( if $pr.mergeable == "CONFLICTING" then
          "skip\t\($pr.number)\t\($pr.headRefOid)\tconflict\tit conflicts with main"
        else empty end )
  ' "${prs}"
}

# ---------------------------------------------------------------------------
# judge
# ---------------------------------------------------------------------------

cmd_judge() {
  local check_runs="" statuses="" required="" workflow_runs=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check-runs) [[ $# -ge 2 ]] || die "--check-runs needs a value"; check_runs="$2"; shift 2 ;;
      --workflow-runs) [[ $# -ge 2 ]] || die "--workflow-runs needs a value"; workflow_runs="$2"; shift 2 ;;
      --statuses) [[ $# -ge 2 ]] || die "--statuses needs a value"; statuses="$2"; shift 2 ;;
      --required) [[ $# -ge 2 ]] || die "--required needs a value"; required="$2"; shift 2 ;;
      *) die "judge: unknown argument: $1" ;;
    esac
  done
  [[ -f "${check_runs}" && -f "${statuses}" ]] || die "judge: needs --check-runs and --statuses files"
  if [[ -n "${workflow_runs}" && ! -f "${workflow_runs}" ]]; then
    die "judge: no --workflow-runs file: ${workflow_runs}"
  fi
  local req
  req="$(required_json "${required}")"
  # REST spells conclusions in lower case. The combined status is the latest
  # state per context. The check-runs listing is NOT one run per name:
  # GitHub's REST docs say only that `filter=latest`, the default, "returns
  # the most recent check runs", filtering "by their completed_at timestamp"
  # (https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference),
  # and a check that ran in two check suites at one head -- a push and a
  # pull_request run, or a workflow run started again -- is listed once per
  # suite: PR 1966 listed ci-gate twice at 5b25774, (failure, success) (#1999).
  # So a check is decided here by the latest run (highest id) of EACH event
  # that ran it (BGD 2026-10-22, CHECK-RULE-2, part of #1999), as the worker's
  # merge step, swarm-api and auto-merge.yml's gate 5 decide it: a re-run
  # replaces the earlier runs of its own event, never another's. The event is
  # the one that made the run's check suite, from --workflow-runs (GET
  # actions/runs?head_sha=, optional); a suite no Actions run names is its own
  # group, so without the file each check suite is judged on its own.
  # A run has finished when its status is completed, OR when it carries a
  # conclusion together with a completed_at, whatever its status says (BGD
  # 2026-10-23, CHECK-STUCK, part of #2031): GitHub left PR 2006's `secret
  # scan` at in_progress with conclusion success and a completed_at. A run
  # with no conclusion stays pending; the worker's `check_run_finished` and
  # auto-merge.yml's gates 5-6 state the same rule.
  # A latest run that finished `cancelled` is neither bad nor good (#1778,
  # BGD released bundle H09, 2026-10-27): someone stopped it, so a required
  # one leaves the verdict pending until its re-run answers, and a red
  # latest run of another event still makes the verdict red. The worker's
  # `cancelled_run` and swarm-api's `forgechecks.cancelled_run` say the same.
  jq -rn --slurpfile cr "${check_runs}" --slurpfile st "${statuses}" \
    --slurpfile wf "${workflow_runs:-/dev/null}" --argjson required "${req}" '
    def good: . as $c | ["success", "skipped", "neutral"] | index($c) != null;
    def bad: . as $c | ["failure", "timed_out", "action_required",
                        "startup_failure", "stale", "error"] | index($c) != null;
    def finished: .status == "completed"
                  or ((.conclusion // "") != "" and (.completed_at // "") != "");
    ( [ ($wf[0].workflow_runs // [])[]
        | select(.check_suite_id != null and (.event // "") != "")
        | {key: (.check_suite_id | tostring), value: .event} ]
      | from_entries ) as $events
    | ( [ ($cr[0].check_runs // []) | to_entries[]
          | .value + {position: .key,
                      group: ($events[(.value.check_suite.id // "" | tostring)]
                              // "check suite \(.value.check_suite.id // "")")} ]
        | group_by([.name, .group])
        | map(max_by([(.id // 0), .position]))
        | sort_by(.position) ) as $latest
    | ( [ $latest[]
        | {name, done: finished, result: (.conclusion // ""),
           url: (.html_url // .details_url // "")} ]
    + [ ($st[0].statuses // [])[]
        | {name: .context, done: (.state != "pending"), result: (.state // ""),
           url: (.target_url // "")} ] ) as $all
    | [ $all[] | select(.done and (.result | bad)) ] as $failed
    | ( if ($failed | length) > 0 then "red"
        elif all($required[]; . as $r | any($all[]; .name == $r and .done and (.result | good))) then "green"
        else "pending" end ) as $verdict
    | "verdict\t\($verdict)",
      ( $failed[] | "failed\t\(.name | gsub("[\\t\\r\\n]"; " "))\t\(.url)" )
  '
}

# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

cmd_plan() {
  local verdict="" train="" suspects="" have_train=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --verdict) [[ $# -ge 2 ]] || die "--verdict needs a value"; verdict="$2"; shift 2 ;;
      --train) [[ $# -ge 2 ]] || die "--train needs a value"; train="$2"; have_train=true; shift 2 ;;
      --suspects) [[ $# -ge 2 ]] || die "--suspects needs a value"; suspects="$2"; shift 2 ;;
      *) die "plan: unknown argument: $1" ;;
    esac
  done
  [[ "${have_train}" == "true" && -n "${train}" ]] || die "plan: --train names no pull request"
  is_number_list "${train}" || die "plan: --train is not pull request numbers: ${train}"
  is_number_list "${suspects}" || die "plan: --suspects is not pull request numbers: ${suspects}"
  local -a members=()
  read -r -a members <<<"${train}"
  local count="${#members[@]}"
  case "${verdict}" in
    green)
      # The held-back half boards next, on its own, so a culprit in it is
      # still found by halving.
      printf 'eject=\nnext=%s\nsuspects=\n' "${suspects}"
      ;;
    red)
      if [[ "${count}" -eq 1 ]]; then
        printf 'eject=%s\nnext=%s\nsuspects=\n' "${members[0]}" "${suspects}"
      else
        local half=$(((count + 1) / 2)) i first="" rest=""
        for ((i = 0; i < count; i++)); do
          if [[ "${i}" -lt "${half}" ]]; then
            first="${first:+${first} }${members[${i}]}"
          else
            rest="${rest:+${rest} }${members[${i}]}"
          fi
        done
        printf 'eject=\nnext=%s\nsuspects=%s\n' "${first}" "${rest}${suspects:+ ${suspects}}"
      fi
      ;;
    *) die "plan: --verdict is green or red, not '${verdict}'" ;;
  esac
}

# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------

cmd_compose() {
  local repo="" base="" prs=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo) [[ $# -ge 2 ]] || die "--repo needs a value"; repo="$2"; shift 2 ;;
      --base) [[ $# -ge 2 ]] || die "--base needs a value"; base="$2"; shift 2 ;;
      --prs) [[ $# -ge 2 ]] || die "--prs needs a value"; prs="$2"; shift 2 ;;
      *) die "compose: unknown argument: $1" ;;
    esac
  done
  [[ -d "${repo}" ]] || die "compose: no --repo directory"
  is_sha "${base}" || die "compose: --base is not a commit sha: ${base}"
  [[ -f "${prs}" ]] || die "compose: no --prs file"

  local scratch wt msg log
  scratch="$(mktemp -d)"
  wt="${scratch}/train"
  msg="${scratch}/message"
  log="${scratch}/merge.log"
  # A worktree of its own: the checkout this script runs from (main's
  # scripts/) is never touched, and nothing of a pull request is executed --
  # git merges its trees, and no hook of the repository's runs.
  git -C "${repo}" worktree add -q --detach "${wt}" "${base}" >"${log}" 2>&1 \
    || { cat "${log}" >&2; die "compose: could not check out ${base}"; }
  local -a g=(git -C "${wt}" -c core.hooksPath=/dev/null -c commit.gpgsign=false
    -c user.name=merge-train -c user.email=merge-train@users.noreply.github.com)

  local n sha title
  while IFS=$'\t' read -r n sha title || [[ -n "${n:-}" ]]; do
    [[ -n "${n}" ]] || continue
    is_number "${n}" || die "compose: not a pull request number: ${n}"
    if ! is_sha "${sha}" || ! "${g[@]}" cat-file -e "${sha}^{commit}" 2>/dev/null; then
      printf 'dropped\t%s\t%s\t%s\n' "${n}" "${sha}" "its head commit is not in the repository"
      continue
    fi
    if "${g[@]}" merge --squash "${sha}" >"${log}" 2>&1; then
      if "${g[@]}" diff --cached --quiet; then
        "${g[@]}" reset -q --hard HEAD
        printf 'dropped\t%s\t%s\t%s\n' "${n}" "${sha}" "nothing left to merge: main already has it"
        continue
      fi
      printf '%s (#%s)\n' "${title}" "${n}" >"${msg}"
      "${g[@]}" commit -q --no-verify -F "${msg}"
      printf 'included\t%s\t%s\t%s\n' "${n}" "${sha}" "${title}"
    else
      "${g[@]}" reset -q --hard HEAD
      "${g[@]}" clean -q -fd
      printf 'dropped\t%s\t%s\t%s\n' "${n}" "${sha}" "it conflicts with a pull request ahead of it in the train, or with main"
    fi
  done <"${prs}"
  printf 'head\t%s\n' "$("${g[@]}" rev-parse HEAD)"
  git -C "${repo}" worktree remove --force "${wt}" >/dev/null 2>&1 || true
  rm -rf "${scratch}"
}

# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

# `hold` (`<epoch>:<sha>`: when the train began holding, and for which build of
# main) is written only by deploy_hold, so a body without it reads as a train
# that never held; every other key is required.
STATE_KEYS="id base head started dry_run prs suspects hold"

state_valid() {
  local key="$1" value="$2"
  case "${key}" in
    id) [[ "${value}" =~ ^[0-9]+-[0-9]+$ ]] ;;
    base | head) is_sha "${value}" ;;
    started) [[ "${value}" =~ ^[0-9]+$ ]] ;;
    dry_run) [[ "${value}" == "true" || "${value}" == "false" ]] ;;
    prs) [[ "${value}" =~ ^[1-9][0-9]*:[0-9a-f]{40}( [1-9][0-9]*:[0-9a-f]{40})*$ ]] ;;
    suspects) is_number_list "${value}" ;;
    hold) [[ "${value}" =~ ^[0-9]+:[0-9a-f]{40}$ ]] ;;
    *) return 1 ;;
  esac
}

cmd_state_render() {
  local pair key value
  printf '%s\n' "${STATE_OPEN}"
  for pair in "$@"; do
    key="${pair%%=*}"
    value="${pair#*=}"
    [[ " ${STATE_KEYS} " == *" ${key} "* ]] || die "state-render: unknown key: ${key}"
    state_valid "${key}" "${value}" || die "state-render: ${key} is not valid: ${value}"
    printf '%s=%s\n' "${key}" "${value}"
  done
  printf '%s\n' "${STATE_CLOSE}"
}

cmd_state_get() {
  local body="" key=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --body) [[ $# -ge 2 ]] || die "--body needs a value"; body="$2"; shift 2 ;;
      --key) [[ $# -ge 2 ]] || die "--key needs a value"; key="$2"; shift 2 ;;
      *) die "state-get: unknown argument: $1" ;;
    esac
  done
  [[ -f "${body}" ]] || die "state-get: no --body file"
  [[ " ${STATE_KEYS} " == *" ${key} "* ]] || die "state-get: unknown key: ${key}"
  local block value count
  block="$(awk -v open_mark="${STATE_OPEN}" -v close_mark="${STATE_CLOSE}" '
    { sub(/\r$/, "") }
    $0 == open_mark { inside = 1; blocks++; next }
    inside && $0 == close_mark { inside = 0; next }
    inside { print }
    END { if (blocks != 1) exit 3 }' "${body}")" || die "state-get: the body carries no single merge-train state block"
  count="$(printf '%s\n' "${block}" | grep -c "^${key}=" || true)"
  [[ "${count}" -eq 1 ]] || die "state-get: ${key} appears ${count} times in the state block"
  value="$(printf '%s\n' "${block}" | sed -n "s/^${key}=//p")"
  state_valid "${key}" "${value}" || die "state-get: ${key} is not a value the train writes: ${value}"
  printf '%s\n' "${value}"
}

# ---------------------------------------------------------------------------
# tick: everything that talks to GitHub
# ---------------------------------------------------------------------------

# The merge App's token, only for the calls that must start workflows or that
# the workflow's own token may not make. Never echoed: it reaches gh and git
# through the environment alone.
gh_app() { GH_TOKEN="${APP_TOKEN}" gh "$@"; }

git_app_push() {
  # A credential helper that reads APP_TOKEN from the environment when git
  # asks, so the token is in no argument, no URL and no remote.
  # shellcheck disable=SC2016
  git -C "${REPO_DIR}" -c credential.helper= \
    -c 'credential.helper=!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=${APP_TOKEN}"; }; f' \
    push -q "https://github.com/${GH_REPO}.git" "$@"
}

comment_on() {
  local number="$1" file="$2"
  gh pr comment "${number}" --repo "${GH_REPO}" --body-file "${file}" >/dev/null
}

summary() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$*" >>"${GITHUB_STEP_SUMMARY}"
  fi
  info "$*"
}

hash_list() {
  # "101 102" -> "#101, #102"
  local out="" n
  for n in $1; do
    out="${out:+${out}, }#${n}"
  done
  printf '%s' "${out}"
}

required_contexts() {
  # Main's required checks, the way auto-merge.yml's gate reads them: the
  # effective rules (rulesets) and any classic protection, each name once,
  # less the train's own status, which it posts only after it is green.
  local out="$1" rules branch
  rules="${WORK}/rules.json"
  branch="${WORK}/branch.json"
  gh api "repos/${GH_REPO}/rules/branches/main" --paginate >"${rules}"
  gh api "repos/${GH_REPO}/branches/main" >"${branch}"
  {
    jq -r '(.protection.required_status_checks.contexts // [])[],
           ((.protection.required_status_checks.checks // [])[] | .context)' "${branch}"
    jq -rs 'add // [] | .[]
            | select(.type == "required_status_checks")
            | (.parameters.required_status_checks // [])[] | .context' "${rules}"
  } | awk -v self="${TRAIN_CONTEXT}" 'length && $0 != self' | sort -u >"${out}"
  [[ -s "${out}" ]] || die "main has no required status checks: a train could never be judged green"
}

list_open_prs() {
  gh pr list --repo "${GH_REPO}" --state open --base main --limit 200 \
    --json number,title,body,headRefOid,headRefName,baseRefName,isDraft,isCrossRepository,mergeable,labels,statusCheckRollup \
    >"$1"
}

finish_train() {
  # Close a train's pull request with a comment, and delete its branch.
  local number="$1" branch="$2" text="$3" body
  body="${WORK}/finish-${number}.md"
  printf '%s\n\n(scripts/merge-train.sh, run %s)\n' "${text}" "${RUN_URL}" >"${body}"
  comment_on "${number}" "${body}"
  gh pr close "${number}" --repo "${GH_REPO}" >/dev/null
  if [[ "${branch}" == "${TRAIN_PREFIX}"* ]]; then
    gh_app api -X DELETE "repos/${GH_REPO}/git/refs/heads/${branch}" >/dev/null 2>&1 \
      || warn "could not delete ${branch}"
  fi
  summary "train #${number} closed: ${text}"
}

eject_pr() {
  local number="$1" train_id="$2" failed="$3" body
  body="${WORK}/eject-${number}.md"
  {
    echo "**Ejected from the merge train, and \`ready\` removed.** Train ${train_id} -- main plus this pull request alone -- failed:"
    echo
    printf '%s\n' "${failed}"
    echo
    echo "With nothing else in the train, the failure is this change's, or its combination with main as it is now. Fix it, then add \`ready\` again; if the failure was a flake, add \`ready\` again as it is."
    echo "(scripts/merge-train.sh, run ${RUN_URL})"
  } >"${body}"
  comment_on "${number}" "${body}"
  # REST, not `gh pr edit`: that one fails on this repository's classic
  # Projects GraphQL field and changes nothing.
  gh api -X DELETE "repos/${GH_REPO}/issues/${number}/labels/ready" >/dev/null 2>&1 \
    || warn "could not remove ready from #${number}"
  summary "ejected #${number}"
}

# The hidden marker on a "does not board" comment, and the sentence after it
# that names the reason. agent_worker.merge reads both (REPOSITORY_TRAIN_SKIP,
# REPOSITORY_TRAIN_SKIP_HEADLINE) to put the reason on the merge step's
# waiting line; tests/unit/worker holds them equal.
SKIP_MARKER="<!-- merge-train-skip"
SKIP_HEADLINE="**The merge train skips it: "

tell_skip() {
  # Tells a `ready` pull request why the train passes over it, ONCE per head
  # commit and reason: the comment carries `<!-- merge-train-skip head=<sha>
  # reason=<key> -->`, and a comment already carrying that marker means it was
  # told. A new head, or a new reason at the same head, is told again. Never
  # fails the tick: a comment that cannot be read or written is a warning, and
  # an unreadable thread is not commented on, so nothing is said twice.
  # $1 N, $2 head, $3 key, $4 why (one line), $5 a file of detail lines.
  local number="$1" head="$2" key="$3" why="$4" details="$5" marker seen body clears
  marker="${SKIP_MARKER} head=${head} reason=${key} -->"
  seen="${WORK}/seen-${number}.txt"
  if ! gh api "repos/${GH_REPO}/issues/${number}/comments" --paginate --jq '.[].body' \
    >"${seen}" 2>/dev/null </dev/null; then
    warn "could not read #${number}'s comments: not telling it why it does not board"
    return 0
  fi
  if grep -qF -- "${marker}" "${seen}"; then
    return 0
  fi
  case "${key}" in
    check:*)
      clears="What clears it: re-run that job from its run page if the failure was not this change's (a network timeout, a flake), or push a fix if it was. The train boards it on its next tick once every check at its head has finished green." ;;
    conflict)
      clears="What clears it: merge main into the branch (or rebase it onto main), resolve the conflict and push. The train boards it once its new head is green." ;;
    *)
      clears="What clears it: edit the body so it closes only what this change finishes (write \`part of #N\` for the rest). The train reads it again on its next tick." ;;
  esac
  body="${WORK}/skip-${number}.md"
  {
    printf '%s\n' "${marker}"
    printf '%s%s.**\n\n' "${SKIP_HEADLINE}" "${why}"
    if [[ -s "${details}" ]]; then
      cat -- "${details}"
      echo
    fi
    echo "At head \`${head}\`. ${clears} It stays \`ready\`; this is said once per head commit and reason."
    echo "(scripts/merge-train.sh, run ${RUN_URL})"
  } >"${body}"
  if ! comment_on "${number}" "${body}"; then
    warn "could not tell #${number} why it does not board"
    return 0
  fi
  summary "#${number} told why it does not board: ${why}"
}

tell_skipped() {
  # Every reason `skips` finds in the open pull request list ($1), each told
  # once (tell_skip). A dry run tells nothing: the caller does not call it.
  local prs="$1" skips="${WORK}/skips.tsv" reasons="${WORK}/skip-reasons.tsv"
  local kind n head key why told=0
  cmd_skips --prs "${prs}" >"${skips}"
  awk -F'\t' '$1 == "skip"' "${skips}" >"${reasons}"
  while IFS=$'\t' read -r kind n head key why; do
    awk -F'\t' -v n="${n}" -v k="${key}" '$1 == "item" && $2 == n && $3 == k { print $4 }' \
      "${skips}" >"${WORK}/skip-items.md"
    info "#${n} does not board: ${why}"
    tell_skip "${n}" "${head}" "${key}" "${why}" "${WORK}/skip-items.md"
    told=$((told + 1))
  done <"${reasons}"
  info "${told} ready pull request(s) pass over for a reason that will not clear by itself"
}

guards_pass() {
  # Main's closing-references and dependency checks, as auto-merge.yml's gate
  # runs them. 0 boards; 2 is a refusal the gate has already commented on;
  # anything else could not tell, and does not board either. A closing-keyword
  # refusal (2) does not clear by itself, so the pull request is told, once
  # at its head (tell_skip), unless $3 says this is a dry run; a read that
  # could not tell, or a dependency not landed yet, may clear by itself.
  local number="$1" head="$2" dry="$3" here rc refusal
  here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  refusal="${WORK}/closing-${number}.md"
  rc=0
  "${here}/check-closing-references.sh" --pr "${number}" --repo "${GH_REPO}" >"${refusal}" 2>/dev/null || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    info "#${number} does not board: its closing keywords name an open pull request, or that could not be read (exit ${rc})"
    if [[ "${rc}" -eq 2 && "${dry}" != "true" ]]; then
      tell_skip "${number}" "${head}" closing-references \
        "its closing keywords would close an open pull request" "${refusal}"
    fi
    return 1
  fi
  "${here}/check-pr-dependencies.sh" --pr "${number}" --repo "${GH_REPO}" >/dev/null 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    info "#${number} does not board: something it depends on has not landed, or that could not be read (exit ${rc})"
    return 1
  fi
}

start_train() {
  # $1 dry (true|false); $2 an explicit list to board in that order (a
  # bisection), or empty for the pool; $3 the suspects to carry.
  local dry="$1" explicit="$2" suspects="$3"
  local eligible="${WORK}/eligible.tsv" chosen="${WORK}/chosen.tsv"
  : >"${chosen}"
  local n sha title count=0
  if [[ -n "${explicit}" ]]; then
    cmd_select --prs "${WORK}/prs.json" --required "${WORK}/required.txt" --allow-unknown-mergeable >"${eligible}"
    for n in ${explicit}; do
      awk -F'\t' -v n="${n}" '$1 == n' "${eligible}" >>"${chosen}"
    done
  else
    cmd_select --prs "${WORK}/prs.json" --required "${WORK}/required.txt" --recheck-unknown >"${eligible}"
    while IFS=$'\t' read -r n sha title; do
      [[ "${count}" -lt "${MAX_PRS}" ]] || break
      guards_pass "${n}" "${sha}" "${dry}" || continue
      printf '%s\t%s\t%s\n' "${n}" "${sha}" "${title}" >>"${chosen}"
      count=$((count + 1))
    done <"${eligible}"
  fi
  if [[ ! -s "${chosen}" ]]; then
    if [[ -n "${explicit}" ]]; then
      summary "none of $(hash_list "${explicit}") can board any more; building from every ready pull request instead"
      start_train "${dry}" "" ""
      return
    fi
    summary "no ready pull request is green at its head: no train"
    return 0
  fi

  git -C "${REPO_DIR}" fetch -q --no-tags origin "+refs/heads/main:refs/merge-train/main"
  local base fetched="${WORK}/fetched.tsv"
  base="$(git -C "${REPO_DIR}" rev-parse refs/merge-train/main)"
  : >"${fetched}"
  while IFS=$'\t' read -r n sha title; do
    if git -C "${REPO_DIR}" fetch -q --no-tags origin "+refs/pull/${n}/head:refs/merge-train/pr/${n}" \
      && [[ "$(git -C "${REPO_DIR}" rev-parse "refs/merge-train/pr/${n}")" == "${sha}" ]]; then
      printf '%s\t%s\t%s\n' "${n}" "${sha}" "${title}" >>"${fetched}"
    else
      info "#${n} moved since it was read: not in this train"
    fi
  done <"${chosen}"

  local composed="${WORK}/composed.tsv"
  cmd_compose --repo "${REPO_DIR}" --base "${base}" --prs "${fetched}" >"${composed}"
  local head included="" pairs="" dropped_lines=""
  head="$(awk -F'\t' '$1 == "head" { print $2 }' "${composed}")"
  local kind why
  while IFS=$'\t' read -r kind n sha why; do
    case "${kind}" in
      included)
        included="${included:+${included} }${n}"
        pairs="${pairs:+${pairs} }${n}:${sha}"
        ;;
      dropped)
        dropped_lines="${dropped_lines}- #${n}: ${why}"$'\n'
        ;;
    esac
  done <"${composed}"

  local id="${RUN_ID}-${RUN_ATTEMPT:-1}"
  if [[ "${dry}" != "true" ]]; then
    while IFS=$'\t' read -r kind n sha why; do
      [[ "${kind}" == "dropped" ]] || continue
      local note="${WORK}/dropped-${n}.md"
      {
        echo "**Not in merge train ${id}:** ${why}. It stays \`ready\` and is tried again in a later train, once what is ahead of it has landed."
        echo "(scripts/merge-train.sh, run ${RUN_URL})"
      } >"${note}"
      comment_on "${n}" "${note}"
    done <"${composed}"
  fi
  if [[ -z "${included}" ]]; then
    summary "every chosen pull request dropped out: no train"$'\n'"${dropped_lines}"
    return 0
  fi

  local branch="${TRAIN_PREFIX}${id}"
  git_app_push "${head}:refs/heads/${branch}"
  local body="${WORK}/train-body.md"
  {
    echo "A merge train: main at \`${base}\` plus each pull request below squashed in order. **Never merge this pull request.** \`.github/workflows/merge-train.yml\` judges its checks and lands each pull request on its own once they are green, then closes this one."
    echo
    for n in ${included}; do echo "- #${n}"; done
    if [[ -n "${dropped_lines}" ]]; then
      echo
      echo "Dropped:"
      printf '%s' "${dropped_lines}"
    fi
    if [[ -n "${suspects}" ]]; then
      echo
      echo "Bisecting: $(hash_list "${suspects}") held back, to board after this train."
    fi
    if [[ "${dry}" == "true" ]]; then
      echo
      echo "**Dry run:** this train is judged and closed; nothing lands and no pull request is told anything."
    fi
    echo
    cmd_state_render "id=${id}" "base=${base}" "head=${head}" "started=$(date +%s)" \
      "dry_run=${dry}" "prs=${pairs}" "suspects=${suspects}"
  } >"${body}"
  # The App's token: a pull request opened with the workflow's own token
  # starts no workflow, and the train would never be tested.
  gh_app pr create --repo "${GH_REPO}" --draft --base main --head "${branch}" \
    --title "Merge train ${id}: $(hash_list "${included}")" --body-file "${body}" >/dev/null
  summary "train ${id} started on ${base}: $(hash_list "${included}")${suspects:+ (held back: $(hash_list "${suspects}"))}"
}

merge_failure_transient() {
  # Whether gh's error ($1, a file) is GitHub or the network failing for a
  # moment rather than GitHub refusing: a rate limit, a network error, or an
  # HTTP 502, 503 or 504. Any other 4xx, and every message this does not
  # know (a conflict, a missing required check, the head moved), is a refusal.
  local err="$1"
  if grep -Eiq 'rate limit|HTTP 429' "${err}"; then
    return 0
  fi
  if grep -Eq 'HTTP 4[0-9][0-9]' "${err}"; then
    return 1
  fi
  grep -Eiq 'timeout|timed out|connection reset|connection refused|TLS handshake|unexpected EOF|HTTP 50[234]|50[234] (Bad Gateway|Service Unavailable|Gateway Time-?out)' "${err}"
}

merge_one() {
  # Squash-merges pull request $1 at head $2 under subject $3. A transient
  # failure (merge_failure_transient) is retried after each wait in
  # MERGE_RETRY_WAITS, and before each retry the pull request is read again:
  # one that merged meanwhile (the merge went through and only its reply was
  # lost) is landed, never merged twice, and one that closed or moved stops.
  # Returns 0 when it landed; else 1 with LAND_REASON set. Each retry is
  # logged in the run with its reason.
  local n="$1" sha="$2" subject="$3"
  local err="${WORK}/merge.err" view="${WORK}/merge-view.json"
  local attempt=1 attempts wait why
  # shellcheck disable=SC2086 # MERGE_RETRY_WAITS is a list of numbers.
  set -- ${MERGE_RETRY_WAITS}
  attempts=$(($# + 1))
  while :; do
    if gh_app pr merge "${n}" --repo "${GH_REPO}" --squash --match-head-commit "${sha}" \
      --subject "${subject}" 2>"${err}" </dev/null; then
      return 0
    fi
    why="$(head -c 500 "${err}" | tr '\n' ' ')"
    if ! merge_failure_transient "${err}"; then
      # After a lost reply, GitHub refuses the retry of a merge that went
      # through: that one has landed.
      if [[ "${attempt}" -gt 1 ]] && gh pr view "${n}" --repo "${GH_REPO}" --json state >"${view}" 2>/dev/null </dev/null \
        && jq -e '.state == "MERGED"' "${view}" >/dev/null; then
        summary "#${n} had merged although GitHub's reply to an earlier attempt was lost: counted as landed, not merged again"
        return 0
      fi
      LAND_REASON="GitHub refused to merge #${n}: ${why}"
      return 1
    fi
    # Read again even after the last attempt: a merge whose reply was lost
    # has landed, and saying it did not would be wrong.
    if [[ "${attempt}" -lt "${attempts}" ]]; then
      wait="$1"
      shift
      echo "::warning::merging #${n} failed transiently (attempt ${attempt} of ${attempts}): ${why}; reading it again and retrying in ${wait}s"
      summary "merge of #${n}, attempt ${attempt} of ${attempts}, failed transiently: ${why}; retrying in ${wait}s"
      sleep "${wait}"
    fi
    if gh pr view "${n}" --repo "${GH_REPO}" --json state,headRefOid >"${view}" 2>/dev/null </dev/null; then
      if jq -e '.state == "MERGED"' "${view}" >/dev/null; then
        summary "#${n} had merged although GitHub's reply to attempt ${attempt} was lost: counted as landed, not merged again"
        return 0
      fi
      if ! jq -e --arg sha "${sha}" '.state == "OPEN" and .headRefOid == $sha' "${view}" >/dev/null; then
        LAND_REASON="#${n} changed while its merge was being retried (closed, or pushed to), after: ${why}"
        return 1
      fi
    else
      # Not read: the merge is still tried. --match-head-commit refuses a
      # moved head, and GitHub refuses one already merged (read above).
      why="${why}; reading #${n} again failed too"
    fi
    if [[ "${attempt}" -ge "${attempts}" ]]; then
      LAND_REASON="GitHub could not merge #${n} in ${attempts} attempts, each failing transiently, the last with: ${why}"
      return 1
    fi
    attempt=$((attempt + 1))
  done
}

land_train() {
  # Lands a green train. Sets LANDED to the pull requests it merged. Returns 0
  # when every one landed, 1 when nothing landed because something changed
  # under the train, 2 when it stopped part-way.
  local base="$1" head="$2" pairs="$3" id="$4"
  LANDED=""
  git -C "${REPO_DIR}" fetch -q --no-tags origin "+refs/heads/main:refs/merge-train/main"
  local main
  main="$(git -C "${REPO_DIR}" rev-parse refs/merge-train/main)"
  if [[ "${main}" != "${base}" ]]; then
    LAND_REASON="main moved from ${base} to ${main} after the train was built, so the tree it tested is not the one these merges would make"
    return 1
  fi
  local pair n sha view="${WORK}/view.json"
  for pair in ${pairs}; do
    n="${pair%%:*}"
    sha="${pair#*:}"
    gh pr view "${n}" --repo "${GH_REPO}" --json state,headRefOid,labels >"${view}"
    if ! jq -e --arg sha "${sha}" '.state == "OPEN" and .headRefOid == $sha and any(.labels[]; .name == "ready")' \
      "${view}" >/dev/null; then
      LAND_REASON="#${n} changed after the train was built (closed, pushed to, or no longer \`ready\`)"
      return 1
    fi
  done
  for pair in ${pairs}; do
    n="${pair%%:*}"
    sha="${pair#*:}"
    gh api -X POST "repos/${GH_REPO}/statuses/${sha}" \
      -f state=success -f "context=${TRAIN_CONTEXT}" -f "target_url=${RUN_URL}" \
      -f "description=Green in merge train ${id}, on main at ${base:0:12}" >/dev/null
  done
  local title
  for pair in ${pairs}; do
    n="${pair%%:*}"
    sha="${pair#*:}"
    gh pr view "${n}" --repo "${GH_REPO}" --json title >"${view}"
    title="$(jq -r .title "${view}")"
    merge_one "${n}" "${sha}" "${title} (#${n})" || return 2
    LANDED="${LANDED:+${LANDED} }${n}"
  done
  # The point of the train: main's tree is the one that was tested.
  git -C "${REPO_DIR}" fetch -q --no-tags origin "+refs/heads/main:refs/merge-train/main"
  local now_tree train_tree
  now_tree="$(git -C "${REPO_DIR}" rev-parse "refs/merge-train/main^{tree}")"
  train_tree="$(git -C "${REPO_DIR}" rev-parse "${head}^{tree}")"
  if [[ "${now_tree}" != "${train_tree}" ]]; then
    LAND_REASON="every pull request landed, but main's tree (${now_tree}) is not the train's tested tree (${train_tree}): something else merged meanwhile, or GitHub's squash merged differently"
    echo "::warning::${LAND_REASON}"
    return 0
  fi
  LAND_REASON=""
  return 0
}

last_deploy() {
  # When a release on main last ran its deploy job successfully: sets
  # LAST_DEPLOY (epoch, empty when none of RELEASE_LOOK runs did) and
  # LAST_DEPLOY_AT (what to call it). Returns 1 when the API could not be read.
  LAST_DEPLOY=""
  LAST_DEPLOY_AT=""
  local runs="${WORK}/hold-releases.json" jobs="${WORK}/hold-release-jobs.json" run status found
  gh api "repos/${GH_REPO}/actions/workflows/release.yml/runs?branch=main&per_page=${RELEASE_LOOK}" \
    >"${runs}" 2>/dev/null </dev/null || return 1
  while IFS=$'\t' read -r run status; do
    # A run that has not started has run no job.
    case "${status}" in queued | pending | waiting | requested) continue ;; esac
    gh api "repos/${GH_REPO}/actions/runs/${run}/jobs?per_page=100" >"${jobs}" 2>/dev/null </dev/null || return 1
    # `deploy (<environment>)` on a push, as release.yml names it.
    found="$(jq -r '[(.jobs // [])[]
        | select((.name == "deploy" or (.name | startswith("deploy ("))) and .conclusion == "success" and .completed_at != null)
        | .completed_at] | sort | last // empty' "${jobs}")" || return 1
    if [[ -n "${found}" ]]; then
      LAST_DEPLOY="$(jq -rn --arg t "${found}" '$t | fromdateiso8601')" || return 1
      LAST_DEPLOY_AT="${found}"
      return 0
    fi
  done < <(jq -r '(.workflow_runs // [])[] | [(.id | tostring), (.status // "-")] | @tsv' "${runs}")
  LAST_DEPLOY_AT="before $(jq -r '[(.workflow_runs // [])[].created_at] | min // "any release on main"' "${runs}") (none of main's last ${RELEASE_LOOK} releases deployed)"
  return 0
}

deploy_hold() {
  # THE DEPLOY-STARVATION HOLD (issue 3336, 5 November 2026). Main's
  # application runs share one group with cancel-in-progress (#2438), so each
  # merge cancels the previous commit's running `build images` job and its
  # release ends green, superseded. With the build starting 18 to 44 minutes
  # after its push and running about 19, and a train landing every 20 to 40
  # minutes, every build was cancelled: nothing deployed from 21:04Z on 4
  # November. So a green train does not merge while BOTH hold:
  #   * no release on main ran its deploy job successfully in the last
  #     DEPLOY_RECENT_MINUTES;
  #   * main's newest application.yml push run has a `build images` job IN
  #     PROGRESS (a build not started is cancelled for free: never held for).
  # It merges on a later tick once that job has finished (either way), or
  # HOLD_MINUTES after the hold began, and it holds once per train: a hold is
  # recorded in the train's state as `hold=<epoch>:<sha of the build>`, and a
  # build of another commit is never held for again. A hold is said once, in
  # the train's own comments. Anything it cannot read, or a hold it cannot
  # record (the bound would not survive the tick), merges, as before the hold.
  #
  # $1 the train's pull request, $2 its body, $3 its `hold` ("" when it never
  # held), $4 dry (true: say what it would hold; write and comment nothing).
  # Sets HOLD (true|false) and HOLD_WHY.
  local number="$1" body="$2" held="$3" dry="$4" now since="" held_sha=""
  HOLD=false
  HOLD_WHY=""
  now="$(date +%s)"
  if [[ -n "${held}" ]]; then
    since="${held%%:*}"
    held_sha="${held#*:}"
    if [[ $((now - since)) -ge $((HOLD_MINUTES * 60)) ]]; then
      HOLD_WHY="held $(((now - since) / 60)) minutes for main's build of ${held_sha:0:12}, past the ${HOLD_MINUTES}-minute bound: merging"
      return 0
    fi
  fi
  local runs="${WORK}/hold-build-runs.json" jobs="${WORK}/hold-build-jobs.json" run sha build
  if ! gh api "repos/${GH_REPO}/actions/workflows/application.yml/runs?branch=main&event=push&per_page=1" \
    >"${runs}" 2>/dev/null </dev/null; then
    HOLD_WHY="could not read main's application.yml runs: not holding"
    warn "${HOLD_WHY}"
    return 0
  fi
  run="$(jq -r '(.workflow_runs // [])[0].id // empty' "${runs}")"
  sha="$(jq -r '(.workflow_runs // [])[0].head_sha // empty' "${runs}")"
  if [[ -z "${run}" ]] || ! is_sha "${sha}"; then
    HOLD_WHY="main has no application.yml push run: nothing to hold for"
    return 0
  fi
  if ! gh api "repos/${GH_REPO}/actions/runs/${run}/jobs?per_page=100" >"${jobs}" 2>/dev/null </dev/null; then
    HOLD_WHY="could not read the jobs of main's application.yml run ${run}: not holding"
    warn "${HOLD_WHY}"
    return 0
  fi
  # Matched as ci-built-images.sh matches it: by name, or name + " / ".
  build="$(jq -r '[(.jobs // [])[] | select(.name == "build images" or (.name | startswith("build images / ")))]
                  | if length == 0 then "absent" else .[0].status // "absent" end' "${jobs}")"
  if [[ "${build}" != "in_progress" ]]; then
    if [[ -n "${held}" ]]; then
      HOLD_WHY="main's build of ${held_sha:0:12} has finished: merging"
    else
      HOLD_WHY="main's newest build (${sha:0:12}) is not in progress (${build}): nothing to hold for"
    fi
    return 0
  fi
  if [[ -n "${held}" ]]; then
    if [[ "${held_sha}" != "${sha}" ]]; then
      HOLD_WHY="this train already held once, for main's build of ${held_sha:0:12}, and never holds twice: merging"
      return 0
    fi
    HOLD=true
    HOLD_WHY="still holding for main's build of ${sha:0:12}, $(((now - since) / 60)) of ${HOLD_MINUTES} minutes"
    return 0
  fi
  if ! last_deploy; then
    HOLD_WHY="could not read release.yml's runs on main: not holding"
    warn "${HOLD_WHY}"
    return 0
  fi
  if [[ -n "${LAST_DEPLOY}" && $((now - LAST_DEPLOY)) -lt $((DEPLOY_RECENT_MINUTES * 60)) ]]; then
    HOLD_WHY="a release deployed at ${LAST_DEPLOY_AT}, within the last ${DEPLOY_RECENT_MINUTES} minutes: not holding"
    return 0
  fi
  HOLD_WHY="holding the merge so main's build of ${sha:0:12} can finish and deploy; nothing has deployed since ${LAST_DEPLOY_AT}"
  if [[ "${dry}" == "true" ]]; then
    HOLD=true
    return 0
  fi
  # Record it first: a hold the next tick cannot see has no bound.
  local edited="${WORK}/hold-body.md"
  awk -v open_mark="${STATE_OPEN}" -v close_mark="${STATE_CLOSE}" -v line="hold=${now}:${sha}" '
    { sub(/\r$/, "") }
    $0 == open_mark { inside = 1; print; next }
    inside && /^hold=/ { next }
    inside && $0 == close_mark { inside = 0; print line }
    { print }' "${body}" >"${edited}"
  if ! jq -n --rawfile b "${edited}" '{body: $b}' \
    | gh api -X PATCH "repos/${GH_REPO}/pulls/${number}" --input - >/dev/null 2>&1; then
    HOLD_WHY="could not record a hold in train #${number}'s state, so its ${HOLD_MINUTES}-minute bound would not survive the tick: not holding"
    warn "${HOLD_WHY}"
    return 0
  fi
  local note="${WORK}/hold-${number}.md"
  {
    echo "**Green, and ${HOLD_WHY}.** Each merge cancels main's running \`build images\` job (#2438), so merging now would cancel this one too, as every one has been cancelled since then (issue 3336)."
    echo
    echo "This train merges on a later tick once that job has finished, or ${HOLD_MINUTES} minutes from now, whichever comes first; it holds only once."
    echo "(scripts/merge-train.sh, run ${RUN_URL})"
  } >"${note}"
  comment_on "${number}" "${note}" || warn "could not say on #${number} why it holds"
  HOLD=true
  return 0
}

handle_train() {
  local number="$1" train_on="$2"
  local body="${WORK}/train-${number}.md" branch head_now
  jq -r --argjson n "${number}" '.[] | select(.number == $n) | .body // ""' "${WORK}/prs.json" >"${body}"
  branch="$(jq -r --argjson n "${number}" '.[] | select(.number == $n) | .headRefName' "${WORK}/prs.json")"
  head_now="$(jq -r --argjson n "${number}" '.[] | select(.number == $n) | .headRefOid' "${WORK}/prs.json")"

  local id base head started dry pairs suspects
  if ! id="$(cmd_state_get --body "${body}" --key id 2>/dev/null)" \
    || ! base="$(cmd_state_get --body "${body}" --key base 2>/dev/null)" \
    || ! head="$(cmd_state_get --body "${body}" --key head 2>/dev/null)" \
    || ! started="$(cmd_state_get --body "${body}" --key started 2>/dev/null)" \
    || ! dry="$(cmd_state_get --body "${body}" --key dry_run 2>/dev/null)" \
    || ! pairs="$(cmd_state_get --body "${body}" --key prs 2>/dev/null)" \
    || ! suspects="$(cmd_state_get --body "${body}" --key suspects 2>/dev/null)"; then
    finish_train "${number}" "${branch}" "Abandoned: its body carries no state block the train wrote."
    return 0
  fi
  if [[ "${head_now}" != "${head}" ]]; then
    finish_train "${number}" "${branch}" "Abandoned: \`${branch}\` was pushed to after the train built it (${head} -> ${head_now}), so its checks are not the train's."
    return 0
  fi
  # Off means off: a train started before MERGE_TRAIN went off lands nothing.
  if [[ "${train_on}" != "true" ]]; then
    dry=true
  fi
  local train_prs="" pair
  for pair in ${pairs}; do train_prs="${train_prs:+${train_prs} }${pair%%:*}"; done

  local cr="${WORK}/check-runs.json" st="${WORK}/status.json" verdict_file="${WORK}/verdict.tsv"
  gh api "repos/${GH_REPO}/commits/${head}/check-runs?per_page=100" >"${cr}"
  gh api "repos/${GH_REPO}/commits/${head}/status?per_page=100" >"${st}"
  # The event of each check suite, read only when some check ran more than
  # once at the head, as auto-merge.yml's gate 5 reads it (CHECK-RULE-2). A
  # read GitHub refuses (no `actions: read`) leaves no events, and each
  # check suite is then judged on its own: never looser.
  local wf="${WORK}/workflow-runs.json"
  printf '{"workflow_runs": []}\n' >"${wf}"
  if jq -e '[(.check_runs // [])[].name] | group_by(.) | any(length > 1)' "${cr}" >/dev/null; then
    gh api "repos/${GH_REPO}/actions/runs?head_sha=${head}&per_page=100" >"${wf}" 2>/dev/null \
      || printf '{"workflow_runs": []}\n' >"${wf}"
  fi
  cmd_judge --check-runs "${cr}" --statuses "${st}" --workflow-runs "${wf}" \
    --required "${WORK}/required.txt" >"${verdict_file}"
  local verdict failed
  verdict="$(awk -F'\t' '$1 == "verdict" { print $2 }' "${verdict_file}")"
  failed="$(awk -F'\t' '$1 == "failed" { printf "- [%s](%s)\n", $2, $3 }' "${verdict_file}")"

  local age=$(($(date +%s) - started))
  case "${verdict}" in
    pending)
      if [[ "${age}" -gt $((MAX_AGE_MINUTES * 60)) ]]; then
        finish_train "${number}" "${branch}" "Abandoned: still pending after $((age / 60)) minutes, past the ${MAX_AGE_MINUTES}-minute limit. $(hash_list "${train_prs}") stay \`ready\` and board a new train."
        [[ "${dry}" == "true" ]] || start_train false "" ""
      else
        summary "train ${id} ($(hash_list "${train_prs}")) is still running its checks, $((age / 60)) minutes in"
      fi
      ;;
    green)
      # The deploy-starvation hold (deploy_hold), before anything lands.
      local held=""
      held="$(cmd_state_get --body "${body}" --key hold 2>/dev/null)" || held=""
      deploy_hold "${number}" "${body}" "${held}" "${dry}"
      if [[ "${dry}" == "true" ]]; then
        local would=""
        [[ "${HOLD}" != "true" ]] || would=" A live train would first have held its merge: ${HOLD_WHY}."
        finish_train "${number}" "${branch}" "Green. **Dry run:** nothing landed. A live train would have landed $(hash_list "${train_prs}"), each squashed under its own title.${would}"
        return 0
      fi
      if [[ "${HOLD}" == "true" ]]; then
        summary "train ${id} ($(hash_list "${train_prs}")) is green; ${HOLD_WHY}"
        return 0
      fi
      [[ -z "${HOLD_WHY}" ]] || info "${HOLD_WHY}"
      local rc=0
      land_train "${base}" "${head}" "${pairs}" "${id}" || rc=$?
      case "${rc}" in
        0)
          finish_train "${number}" "${branch}" "Green, and landed: $(hash_list "${LANDED}"), one squash each.${LAND_REASON:+ **Warning:** ${LAND_REASON}}"
          ;;
        1)
          finish_train "${number}" "${branch}" "Green, but nothing landed: ${LAND_REASON}. The next train is built from scratch."
          ;;
        *)
          finish_train "${number}" "${branch}" "Green, and landed only $(hash_list "${LANDED}") before stopping: ${LAND_REASON}. The rest stay \`ready\` and board a new train."
          echo "::error::merge train ${id} stopped part-way: ${LAND_REASON}"
          ;;
      esac
      local next=""
      if [[ "${rc}" -eq 0 ]]; then
        next="$(cmd_plan --verdict green --train "${train_prs}" --suspects "${suspects}" | sed -n 's/^next=//p')"
      fi
      list_open_prs "${WORK}/prs.json"
      start_train false "${next}" ""
      [[ "${rc}" -ne 2 ]] || return 1
      ;;
    red)
      if [[ "${dry}" == "true" ]]; then
        finish_train "${number}" "${branch}" "Red. **Dry run:** nothing ejected and nothing bisected. Failing:"$'\n\n'"${failed}"
        return 0
      fi
      local plan eject next held
      plan="$(cmd_plan --verdict red --train "${train_prs}" --suspects "${suspects}")"
      eject="$(sed -n 's/^eject=//p' <<<"${plan}")"
      next="$(sed -n 's/^next=//p' <<<"${plan}")"
      held="$(sed -n 's/^suspects=//p' <<<"${plan}")"
      if [[ -n "${eject}" ]]; then
        eject_pr "${eject}" "${id}" "${failed}"
        finish_train "${number}" "${branch}" "Red, with #${eject} alone: ejected. Failing:"$'\n\n'"${failed}"
      else
        finish_train "${number}" "${branch}" "Red. Bisecting: $(hash_list "${next}") board the next train, $(hash_list "${held}") are held back. Failing:"$'\n\n'"${failed}"
      fi
      list_open_prs "${WORK}/prs.json"
      start_train false "${next}" "${held}"
      ;;
    *) die "judge said '${verdict}'" ;;
  esac
}

cmd_tell() {
  # The tick's telling step alone, over a list read already: each `ready`
  # pull request `skips` finds is told why it does not board, once per head
  # and reason. Needs GH_REPO, GH_TOKEN (reads and comments) and RUN_URL.
  local prs=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --prs) [[ $# -ge 2 ]] || die "--prs needs a value"; prs="$2"; shift 2 ;;
      *) die "tell: unknown argument: $1" ;;
    esac
  done
  [[ -f "${prs}" ]] || die "tell: no --prs file"
  : "${GH_REPO:?GH_REPO is required}"
  : "${RUN_URL:?RUN_URL is required}"
  WORK="$(mktemp -d)"
  trap 'rm -rf "${WORK}"' EXIT
  tell_skipped "${prs}"
}

cmd_hold() {
  # The deploy-starvation hold alone, for one green train (deploy_hold):
  # prints `hold\t<why>` or `merge\t<why>`. Needs GH_REPO, GH_TOKEN (reads,
  # the train's body and its comment) and RUN_URL. --dry-run writes nothing.
  local number="" body="" dry=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --train) [[ $# -ge 2 ]] || die "--train needs a value"; number="$2"; shift 2 ;;
      --body) [[ $# -ge 2 ]] || die "--body needs a value"; body="$2"; shift 2 ;;
      --dry-run) dry=true; shift ;;
      *) die "hold: unknown argument: $1" ;;
    esac
  done
  is_number "${number}" || die "hold: --train is not a pull request number: ${number}"
  [[ -f "${body}" ]] || die "hold: no --body file"
  : "${GH_REPO:?GH_REPO is required}"
  : "${RUN_URL:?RUN_URL is required}"
  hold_knobs_valid
  WORK="$(mktemp -d)"
  trap 'rm -rf "${WORK}"' EXIT
  local held=""
  held="$(cmd_state_get --body "${body}" --key hold 2>/dev/null)" || held=""
  deploy_hold "${number}" "${body}" "${held}" "${dry}"
  if [[ "${HOLD}" == "true" ]]; then
    printf 'hold\t%s\n' "${HOLD_WHY}"
  else
    printf 'merge\t%s\n' "${HOLD_WHY}"
  fi
}

cmd_land() {
  # The landing step alone, for one green train (land_train), as the tick
  # runs it: prints its return code, what landed and why it stopped.
  local base="" head="" pairs="" id=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --base) [[ $# -ge 2 ]] || die "--base needs a value"; base="$2"; shift 2 ;;
      --head) [[ $# -ge 2 ]] || die "--head needs a value"; head="$2"; shift 2 ;;
      --prs) [[ $# -ge 2 ]] || die "--prs needs a value"; pairs="$2"; shift 2 ;;
      --id) [[ $# -ge 2 ]] || die "--id needs a value"; id="$2"; shift 2 ;;
      *) die "land: unknown argument: $1" ;;
    esac
  done
  is_sha "${base}" || die "land: --base is not a commit sha: ${base}"
  is_sha "${head}" || die "land: --head is not a commit sha: ${head}"
  [[ "${pairs}" =~ ^[1-9][0-9]*:[0-9a-f]{40}( [1-9][0-9]*:[0-9a-f]{40})*$ ]] || die "land: --prs is not a list of <N>:<sha>: ${pairs}"
  [[ -n "${id}" ]] || die "land: no --id"
  : "${GH_REPO:?GH_REPO is required}"
  : "${APP_TOKEN:?APP_TOKEN (the merge App token) is required}"
  : "${REPO_DIR:?REPO_DIR is required}"
  : "${RUN_URL:?RUN_URL is required}"
  retry_knobs_valid
  WORK="$(mktemp -d)"
  trap 'rm -rf "${WORK}"' EXIT
  local rc=0
  land_train "${base}" "${head}" "${pairs}" "${id}" || rc=$?
  printf 'rc\t%s\nlanded\t%s\nreason\t%s\n' "${rc}" "${LANDED}" "${LAND_REASON}"
}

retry_knobs_valid() {
  [[ "${MERGE_RETRY_WAITS}" =~ ^([0-9]+( [0-9]+)*)?$ ]] \
    || die "MERGE_TRAIN_MERGE_RETRY_WAITS is not a list of seconds: ${MERGE_RETRY_WAITS}"
}

hold_knobs_valid() {
  is_number "${HOLD_MINUTES}" || die "MERGE_TRAIN_DEPLOY_HOLD_MINUTES is not a positive number: ${HOLD_MINUTES}"
  is_number "${DEPLOY_RECENT_MINUTES}" || die "MERGE_TRAIN_DEPLOY_RECENT_MINUTES is not a positive number: ${DEPLOY_RECENT_MINUTES}"
  is_number "${RELEASE_LOOK}" || die "MERGE_TRAIN_RELEASE_LOOK is not a positive number: ${RELEASE_LOOK}"
}

cmd_tick() {
  : "${GH_REPO:?GH_REPO is required}"
  : "${GH_TOKEN:?GH_TOKEN is required}"
  : "${APP_TOKEN:?APP_TOKEN (the merge App token) is required}"
  : "${REPO_DIR:?REPO_DIR is required}"
  : "${RUN_ID:?RUN_ID is required}"
  : "${RUN_URL:?RUN_URL is required}"
  is_number "${MAX_PRS}" || die "MERGE_TRAIN_MAX_PRS is not a positive number: ${MAX_PRS}"
  is_number "${MAX_AGE_MINUTES}" || die "MERGE_TRAIN_MAX_AGE_MINUTES is not a positive number: ${MAX_AGE_MINUTES}"
  hold_knobs_valid
  retry_knobs_valid
  WORK="$(mktemp -d)"
  trap 'rm -rf "${WORK}"' EXIT

  local train_on=false dry=true
  if [[ "${MERGE_TRAIN:-off}" == "on" ]]; then
    train_on=true
  fi
  if [[ "${train_on}" == "true" && "${DRY_RUN_INPUT:-false}" != "true" ]]; then
    dry=false
  fi

  required_contexts "${WORK}/required.txt"
  list_open_prs "${WORK}/prs.json"
  # Before anything boards: a `ready` pull request the train will pass over
  # on every tick until someone acts is told so, once per head and reason
  # (4 November 2026: one sat eight hours behind a failed check, unsaid).
  if [[ "${dry}" != "true" ]]; then
    tell_skipped "${WORK}/prs.json"
  fi
  local trains="${WORK}/trains.txt"
  jq -r --arg prefix "${TRAIN_PREFIX}" '
    [ .[] | select((.headRefName | startswith($prefix)) and .isCrossRepository == false) ]
    | sort_by(.number) | .[].number' "${WORK}/prs.json" >"${trains}"
  local count newest n
  count="$(grep -c . "${trains}" || true)"
  if [[ "${count}" -gt 0 ]]; then
    # One train at a time. The workflow's concurrency group already holds
    # that; a second open train (opened by hand, or left by a run that died
    # between two steps) is closed, and the newest is the one judged.
    newest="$(tail -n 1 "${trains}")"
    while read -r n; do
      [[ "${n}" == "${newest}" ]] && continue
      finish_train "${n}" "$(jq -r --argjson n "${n}" '.[] | select(.number == $n) | .headRefName' "${WORK}/prs.json")" \
        "Abandoned: more than one train was open, and #${newest} is the newest."
    done <"${trains}"
    handle_train "${newest}" "${train_on}"
    return
  fi
  # A train branch's own CI finishing only ever judges its train. Any other
  # `workflow_run` (a pull request's CI finishing green, `ready` added through
  # auto-merge's run) is one of the events that can make a pull request
  # boardable, so with no train open it starts one, as a schedule tick does:
  # that is exactly when it matters. The branch comes from the workflow's
  # env:, never interpolated into a shell line.
  local event="${EVENT_NAME:-schedule}"
  if [[ "${event}" == "workflow_run" && "${WORKFLOW_RUN_HEAD_BRANCH:-}" == "${TRAIN_PREFIX}"* ]]; then
    summary "no train in flight"
    return 0
  fi
  case "${event}" in
    workflow_dispatch)
      start_train "${dry}" "" ""
      ;;
    *)
      if [[ "${train_on}" == "true" ]]; then
        start_train "${dry}" "" ""
      else
        summary "MERGE_TRAIN is not on: no train started"
      fi
      ;;
  esac
}

main() {
  local sub="${1:-}"
  [[ $# -gt 0 ]] && shift
  case "${sub}" in
    select) cmd_select "$@" ;;
    skips) cmd_skips "$@" ;;
    tell) cmd_tell "$@" ;;
    hold) cmd_hold "$@" ;;
    land) cmd_land "$@" ;;
    judge) cmd_judge "$@" ;;
    plan) cmd_plan "$@" ;;
    compose) cmd_compose "$@" ;;
    state-render) cmd_state_render "$@" ;;
    state-get) cmd_state_get "$@" ;;
    tick) cmd_tick "$@" ;;
    -h | --help | "") usage ;;
    *) die "unknown subcommand: ${sub} (see --help)" ;;
  esac
}

main "$@"
