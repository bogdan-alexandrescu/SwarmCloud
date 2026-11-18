#!/usr/bin/env bash
# Pure output parsers for the acceptance suite: text in, a verdict out.
#
# WHY A FILE OF THEIR OWN. The acceptance suite asserts OUTPUTS -- a pytest
# summary line, the lines a patch removes, the order checkpoints were written
# in, the error code of a refusal. Each of those is a parse that can be wrong
# in a way that turns a real failure into a PASS (the house history is full of
# `[[ "" -eq 0 ]]`). So they live here, with no dependency on the platform, on
# common.sh or on credentials, and tests/unit/scripts/test_acceptance_helpers.py
# runs every one of them against inputs that must pass AND inputs that must
# not. Nothing here reads the network or the environment.
#
# Every function reads stdin and/or its arguments, prints its answer, and
# returns non-zero when the thing asked about is not so. bash 3.2 compatible.

set -euo pipefail

# acc_pytest_summary < pytest output -> "passed=N failed=M errors=K"
#
# Read from pytest's LAST summary line ("2 passed in 0.01s", "1 failed, 3
# passed in 0.2s", "1 error in 0.1s"), not from anywhere in the text: a test
# NAME can contain the word "passed". Absent counts are 0. Returns 1 when no
# summary line is found at all -- "no summary" is not "0 failed".
acc_pytest_summary() {
  awk '
    / in [0-9.]+s/ && /(passed|failed|error|errors|skipped|no tests ran)/ { line = $0 }
    END {
      if (line == "") exit 1
      p = 0; f = 0; e = 0
      n = split(line, w, /[ ,=]+/)
      for (i = 2; i <= n; i++) {
        if (w[i] == "passed" && w[i-1] ~ /^[0-9]+$/) p = w[i-1]
        if (w[i] == "failed" && w[i-1] ~ /^[0-9]+$/) f = w[i-1]
        if ((w[i] == "error" || w[i] == "errors") && w[i-1] ~ /^[0-9]+$/) e = w[i-1]
      }
      printf "passed=%d failed=%d errors=%d\n", p, f, e
    }'
}

# acc_diff_files < unified diff -> each file the diff changes, one per line
# (the b/ side, so a rename reports its new name; /dev/null for a deletion is
# reported as the a/ side).
acc_diff_files() {
  awk '
    /^diff --git / {
      a = $3; b = $4
      sub(/^a\//, "", a); sub(/^b\//, "", b)
      print (b == "/dev/null" ? a : b)
    }'
}

# acc_diff_replaces PATH OLD_LINE < unified diff
#
# True only when, WITHIN the section of the diff for PATH, a removed line is
# exactly OLD_LINE and at least one line is added. "The diff touches the bug
# line", made precise: the line is gone and something took its place, in that
# file and not in another one that happens to contain the same text. Leading
# and trailing whitespace of the removed line is compared as written.
acc_diff_replaces() {
  local path="$1" old="$2"
  awk -v path="${path}" -v old="${old}" '
    /^diff --git / {
      a = $3; b = $4; sub(/^a\//, "", a); sub(/^b\//, "", b)
      infile = (a == path || b == path)
      next
    }
    !infile { next }
    /^(\+\+\+|---) / { next }
    /^-/ { if (substr($0, 2) == old) removed = 1; next }
    /^\+/ { added = 1; next }
    END { exit (removed && added) ? 0 : 1 }'
}

# acc_iso_epoch ISO8601 -> integer seconds since the epoch.
#
# Firestore timestamps carry a fraction ("2026-09-29T12:00:00.123456Z"), which
# jq's fromdateiso8601 refuses; the fraction is dropped, an offset of +00:00 is
# read as Z. Returns 1 on anything else rather than printing a guess.
acc_iso_epoch() {
  jq -nr --arg t "$1" '
    $t | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601' 2>/dev/null
}

# acc_checkpoint_ids < task events, one decoded JSON document per line
#   -> the checkpoint ids of `checkpoint_completed` events, in the order they
#      were WRITTEN (by the event's `at`, then its `detail.seq`).
acc_checkpoint_ids() {
  jq -sr '
    map(select(.type == "checkpoint_completed"))
    | sort_by([(.at // ""), (.detail.seq // 0)])
    | .[] | (.detail.checkpoint_id // empty)'
}

# acc_strictly_increasing < ids like ckpt-00003, one per line
#
# True when there are at least two and each numeric suffix is larger than the
# one before. One id is not a sequence, and it is not evidence of periodic
# checkpointing: the final checkpoint alone produces one.
acc_strictly_increasing() {
  awk '
    NF == 0 { next }
    {
      n = $0; sub(/^.*[^0-9]/, "", n)
      if (n == "") { bad = 1; exit }
      n += 0
      if (count > 0 && n <= last) { bad = 1; exit }
      last = n; count++
    }
    END { exit (bad || count < 2) ? 1 : 0 }'
}

# acc_error_code < an API error body -> its `code`, or nothing.
acc_error_code() {
  jq -r 'if type == "object" then (.code // .detail.code // empty) else empty end' 2>/dev/null || true
}

# acc_title_is_fact TASK_ID < a pull-request title
#
# The owner's 2026-09-28 rule: a pull request title names the work, never the
# task id, and the `[swarm] task_` placeholder is not a title. One line, not
# blank.
acc_title_is_fact() {
  local task_id="$1" title lines
  title="$(cat)"
  lines="$(printf '%s' "${title}" | awk 'END { print NR }')"
  [[ -n "${title//[[:space:]]/}" ]] || return 1
  [[ "${lines}" -le 1 ]] || return 1
  case "${title}" in
    *"${task_id}"*|"[swarm]"*|*task_[0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) return 1 ;;
  esac
  return 0
}

# acc_merged_branches < a pull-request body -> the contributor branches an
# integration names as merged ("- merged: `swarm/task_...`"), one per line.
# shellcheck disable=SC2016  # sed pattern below: the backtick and \1 are sed's, not the shell's
acc_merged_branches() {
  sed -nE 's/^- merged: `(swarm\/[A-Za-z0-9_.-]+)`.*$/\1/p'
}
