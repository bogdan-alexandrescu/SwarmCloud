#!/usr/bin/env bash
# Close the attempts a fence, a reclaim or a failed dispatch superseded and
# nobody ever ended (#630). One-off, idempotent, dry run by default.
#
# WHY. Until #630 the reconciler's fence and reclaim, and the scheduler's
# failed-dispatch path, moved the TASK on and left the superseded ATTEMPT with
# no `completed_at` and no `exit_code` for ever: a fenced worker exits without
# writing anything (invariant 5), and a failed dispatch started no container
# to write it. The 2026-10-05 history analysis found 183 of them -- 17 on
# 10-01 and 5 on 10-04, so it was live -- and every figure read from attempts
# counted them as still running: outcomes reported "no exit recorded: 180",
# and a first pass at concurrency read a peak of 202 where the truth was 20.
# The writers now record the end in the same transaction; this closes the
# ones they left before that.
#
# WHAT IT CLOSES. An attempt is closed only when ALL of these hold, read from
# Firestore by this run:
#
#   * it has no `completed_at`;
#   * its task document exists and is at a LATER generation than the attempt
#     (`current_generation > generation`), so the attempt is superseded and
#     nothing may run for it any more -- a worker at a stale generation exits
#     without running its agent;
#   * it has not changed since this run read it: every write carries the
#     attempt's `updateTime` as a precondition (`fs_patch`), so a worker that
#     records its own end in between keeps its account and this write is
#     refused and counted.
#
# An attempt at its task's CURRENT generation is never touched: it is the live
# attempt, or a finished task's unended one, which the reconciler's
# lost_after_finish rule owns (#380). An attempt whose task is gone is left
# and counted.
#
# WHAT IT WRITES. `completed_at`, and -- only where `error` is null -- an
# `error` naming the cause `superseded` and this script. `exit_code` is never
# written: none is known, and a null one says so. `superseded` and not
# fenced/reclaimed/dispatch_failed: which of the three it was cannot be told
# from the documents after the fact, and a cause this run guessed would be a
# figure that is wrong without saying so.
#
# THE END TIME is the moment the attempt was superseded, as near as the
# documents record it, so a closed attempt does not inflate a duration as
# badly as an open one did:
#
#   next_attempt    the earliest created_at of the task's attempts at a later
#                   generation -- the admission that superseded it;
#   lease_released  else its lease's released_at -- the reclaim's release;
#   task_updated    else its task's updated_at, which is no earlier than the
#                   fence that moved the generation past it.
#
# The counts by source are printed, so a run that fell back is visible.
#
# Usage:
#   scripts/backfill-attempt-ends.sh                 # dry run: counts, writes nothing
#   scripts/backfill-attempt-ends.sh --apply         # close, after typing the project id
#
# A second run finds nothing left to close and exits 0 without asking.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# common.sh's confirm() skips the prompt when SWARM_ASSUME_YES is set. This
# rewrites production records, so the project id is always typed at a terminal.
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: closing attempts always needs the project id typed at a terminal"
  unset SWARM_ASSUME_YES
fi

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/backfill-attempt-ends.sh [--apply] [--allow-prod]

  --apply        close the superseded open attempts, after typing the project id;
                 without it, a dry run that counts and writes nothing
  --dry-run      the default, accepted so a command line can say so
  --allow-prod   required when ENVIRONMENT is prod
USAGE
}

APPLY=0
SAID_DRY_RUN=0
ALLOW_PROD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)      APPLY=1; shift ;;
    --dry-run|-n) SAID_DRY_RUN=1; shift ;;
    --allow-prod) ALLOW_PROD=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) usage; die "unknown argument: $1" ;;
  esac
done
if [[ "${APPLY}" -eq 1 && "${SAID_DRY_RUN}" -eq 1 ]]; then
  die "--apply and --dry-run contradict each other; say which one you mean"
fi

case "${FIRESTORE_DATABASE}" in
  ''|'(default)'|default)
    die "refusing the (default) Firestore database; it belongs to the rest of this shared project" ;;
esac
if is_production && [[ "${ALLOW_PROD}" -eq 0 ]]; then
  die "ENVIRONMENT is '${ENVIRONMENT}'; closing production attempts requires --allow-prod"
fi

require_cmd jq curl

#: Documents per listing page. A page size, not a cap: the listing follows
#: its cursor to the end.
FS_PAGE=300
#: How many candidates the dry run names, beside the counts.
SAMPLE=20
#: The head of the `error` this run writes. The cause is the first word, as
#: the reconciler's `fenced:` / `reclaimed:` and the scheduler's
#: `dispatch_failed:` are.
CAUSE="superseded"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-backfill.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

#: A document id this run may put in a path: Firestore ids the platform mints
#: (`new_id`), never a `/` or anything a URL would read differently.
valid_id() {
  case "$1" in
    ''|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-]*) return 1 ;;
  esac
  return 0
}

# plan OUT_FILE -- list every attempt, read each open attempt's task, and
# write one JSON line per OPEN attempt with its class:
#   close | current_generation | task_gone | malformed
plan() {
  local out="$1" raw="${WORK}/attempts.raw" attempts="${WORK}/attempts.jsonl"
  local tasks="${WORK}/tasks.jsonl" task_ids="${WORK}/task-ids.txt"
  local id reply

  # Into a file, never through a command substitution: fs_list's failure is
  # its status, and a pipeline would hand on jq's instead.
  fs_list attempts "${FS_PAGE}" >"${raw}" \
    || die "could not list attempts; NOTHING was concluded or written"
  jq -c -s "${FS_JQ}"'
    add // [] | .[]
    | {_path: (.name | sub("^.*?/documents/"; "")), _update_time: .updateTime} + doc
  ' "${raw}" >"${attempts}"

  jq -r 'select(.completed_at == null) | .task_id // empty | strings' "${attempts}" \
    | sort -u >"${task_ids}"
  : >"${tasks}"
  while IFS= read -r id; do
    valid_id "${id}" || continue
    reply="$(fs_get "tasks/${id}")" || die "could not read tasks/${id}; NOTHING was concluded or written"
    printf '%s' "${reply}" | jq -c "${FS_JQ}"' select(.fields != null) | doc' >>"${tasks}"
  done <"${task_ids}"

  jq -c -n --slurpfile a "${attempts}" --slurpfile t "${tasks}" '
    ($t | map({key: .id, value: .}) | from_entries) as $tasks
    | $a[]
    | select(.completed_at == null)
    | . as $x
    | ($x.generation | if type == "number" then . else null end) as $gen
    | ($tasks[$x.task_id // ""]) as $task
    | {
        path: $x._path, update_time: $x._update_time, attempt_id: $x.id,
        task_id: $x.task_id, lease_id: $x.lease_id, generation: $gen,
        has_error: ($x.error != null),
        task_generation: ($task.current_generation // null),
        task_updated: ($task.updated_at // null),
        next_attempt: (
          if $gen == null then null else
            [ $a[] | select(.task_id == $x.task_id)
                   | select((.generation | type) == "number" and .generation > $gen)
                   | .created_at | strings ] | sort | first
          end)
      }
    | .class = (
        if ($x.task_id | type) != "string" or $gen == null then "malformed"
        elif $task == null then "task_gone"
        elif ((.task_generation | type) == "number" and .task_generation > $gen) then "close"
        else "current_generation" end)
  ' >"${out}"
}

count_class() { jq -s --arg c "$1" '[.[] | select(.class == $c)] | length' "$2"; }

PLAN="${WORK}/plan.jsonl"
plan "${PLAN}"

LISTED="$(wc -l <"${WORK}/attempts.jsonl" | tr -d ' ')"
OPEN="$(wc -l <"${PLAN}" | tr -d ' ')"
TO_CLOSE="$(count_class close "${PLAN}")"
CURRENT="$(count_class current_generation "${PLAN}")"
GONE="$(count_class task_gone "${PLAN}")"
MALFORMED="$(count_class malformed "${PLAN}")"

# The end time of each candidate, and where it came from.
#
# Columns are split on US (\x1f), not TAB: TAB is IFS WHITESPACE to `read`, so
# an empty column -- an attempt with no later one, a lease id that is null --
# collapsed into its neighbour and every column after it shifted by one.
US=$'\037'
ENDS="${WORK}/ends.txt"
: >"${ENDS}"
jq -r 'select(.class == "close")
       | [.path, .update_time, .attempt_id, .task_id, (.generation|tostring),
          (.task_generation|tostring), (.next_attempt // ""), (.lease_id // ""),
          (.task_updated // ""), (if .has_error then "1" else "0" end)]
       | map(tostring | gsub("[\u001f\n]"; " ")) | join("\u001f")' "${PLAN}" \
  >"${WORK}/close.txt"
FROM_NEXT=0
FROM_LEASE=0
FROM_TASK=0
NO_TIME=0
while IFS="${US}" read -r path update_time attempt_id task_id gen task_gen next lease_id task_updated has_error; do
  end="" source=""
  if [[ -n "${next}" ]]; then
    end="${next}" source="next_attempt"
  elif [[ -n "${lease_id}" ]] && valid_id "${lease_id}"; then
    reply="$(fs_get "leases/${lease_id}")" || die "could not read leases/${lease_id}; NOTHING was written"
    end="$(printf '%s' "${reply}" | jq -r "${FS_JQ}"' select(.fields != null) | doc | .released_at // empty | strings')"
    [[ -n "${end}" ]] && source="lease_released"
  fi
  if [[ -z "${end}" && -n "${task_updated}" ]]; then
    end="${task_updated}" source="task_updated"
  fi
  case "${source}" in
    next_attempt)   FROM_NEXT=$((FROM_NEXT + 1)) ;;
    lease_released) FROM_LEASE=$((FROM_LEASE + 1)) ;;
    task_updated)   FROM_TASK=$((FROM_TASK + 1)) ;;
    *)              NO_TIME=$((NO_TIME + 1)); continue ;;
  esac
  printf '%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\n' "${path}" "${update_time}" "${attempt_id}" \
    "${task_id}" "${gen}" "${task_gen}" "${end}" "${source}" "${has_error}" >>"${ENDS}"
done <"${WORK}/close.txt"
CLOSABLE="$(wc -l <"${ENDS}" | tr -d ' ')"

step "superseded open attempts in ${PROJECT_ID}/${FIRESTORE_DATABASE}"
{
  printf '  %-34s %s\n' "attempts listed" "${LISTED}"
  printf '  %-34s %s\n' "open (no completed_at)" "${OPEN}"
  printf '  %-34s %s\n' "to close: superseded" "${TO_CLOSE}"
  printf '  %-34s %s\n' "  end from next_attempt" "${FROM_NEXT}"
  printf '  %-34s %s\n' "  end from lease_released" "${FROM_LEASE}"
  printf '  %-34s %s\n' "  end from task_updated" "${FROM_TASK}"
  printf '  %-34s %s\n' "  left: no end time recorded" "${NO_TIME}"
  printf '  %-34s %s\n' "left: at the current generation" "${CURRENT}"
  printf '  %-34s %s\n' "left: task document gone" "${GONE}"
  printf '  %-34s %s\n' "left: malformed" "${MALFORMED}"
} | redact >&2

if [[ "${CLOSABLE}" -eq 0 ]]; then
  ok "nothing to close"
  exit 0
fi

if [[ "${APPLY}" -eq 0 ]]; then
  info "DRY RUN (the default): nothing was written; --apply closes the ${CLOSABLE} above"
  head -n "${SAMPLE}" "${ENDS}" \
    | awk -F "${US}" '{ printf "  %s  task %s  generation %s < %s  end %s (%s)\n", $3, $4, $5, $6, $7, $8 }' \
    | redact >&2
  if [[ "${CLOSABLE}" -gt "${SAMPLE}" ]]; then
    dim "  ... and $((CLOSABLE - SAMPLE)) more"
  fi
  exit 0
fi

confirm "This writes completed_at on ${CLOSABLE} attempt(s) in ${PROJECT_ID}/${FIRESTORE_DATABASE}." "${PROJECT_ID}"

CLOSED=0
REFUSED=0
while IFS="${US}" read -r path update_time attempt_id task_id gen task_gen end source has_error; do
  mask="completed_at"
  fields="$(jq -nc --arg at "${end}" '{completed_at: {timestampValue: $at}}')"
  if [[ "${has_error}" == "0" ]]; then
    mask="completed_at,error"
    fields="$(jq -nc --arg at "${end}" --arg err \
      "${CAUSE}: closed by scripts/backfill-attempt-ends.sh (#630); task at generation ${task_gen}, end from ${source}" \
      '{completed_at: {timestampValue: $at}, error: {stringValue: $err}}')"
  fi
  # The precondition is the attempt's updateTime as this run listed it: an
  # attempt its worker ended since is refused here, never overwritten.
  if fs_patch "${path}" "${mask}" "${fields}" "${update_time}"; then
    CLOSED=$((CLOSED + 1))
  else
    REFUSED=$((REFUSED + 1))
    warn "did not close ${attempt_id} (task ${task_id}): it changed since this run read it"
  fi
done <"${ENDS}"

# The proof is a second listing, not the writes' own answers.
AFTER="${WORK}/after.jsonl"
plan "${AFTER}"
REMAINING="$(count_class close "${AFTER}")"
{
  printf '  %-34s %s\n' "closed" "${CLOSED}"
  printf '  %-34s %s\n' "refused (changed since read)" "${REFUSED}"
  printf '  %-34s %s\n' "superseded and still open" "${REMAINING}"
} | redact >&2
# A write that answered success and did not land is the one failure this
# must not report as done. Those it refused, or could not date, are counted.
if [[ "${REMAINING}" -gt $((NO_TIME + REFUSED)) ]]; then
  die "${REMAINING} superseded attempt(s) are still open after ${CLOSED} write(s) succeeded; run it again to see which"
fi
if [[ "${REMAINING}" -gt "${NO_TIME}" ]]; then
  warn "${REMAINING} superseded attempt(s) are still open; a second run reads them again"
fi
ok "closed ${CLOSED} superseded attempt(s)"
