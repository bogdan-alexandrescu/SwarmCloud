#!/usr/bin/env bash
# Runs shellcheck over the runner's cores, and on a pull request only over what the
# change reaches (LANE CI-EARLY, part of #1694, owner decision 2026-10-20).
#
# WHY. One serial `shellcheck -x` over every script was the slowest check on
# 19 of 65 pull-request heads (median 291 s, p90 320 s), whatever the change
# touched. shellcheck analyses each file it is handed as its own root, so the
# set can be split across processes without changing a single finding: each
# file is still checked by `shellcheck -x <file>`, following its own
# `# shellcheck source=` directives, exactly as in the one big call.
#
# WHAT IS CHECKED.
#   * No --changed (main, a push, a queue entry, a dispatch): every file of
#     SET below -- the roots the serial command named, so main still checks
#     everything it did.
#   * --changed FILE (a pull request; FILE lists its changed paths, one a
#     line): the changed files of SET; the files of SET they source, through
#     any chain; and every file of SET that sources a changed file, through any
#     chain. The last is the one that matters: under -x a library is analysed
#     in its callers' context, so removing a variable from common.sh shows up
#     as SC2154 in the scripts that read it, never in common.sh. A file
#     outside SET (kubernetes/cluster-network.sh) is never made a root -- main
#     never checks it as one -- but its callers are.
#     A change to this script, a .shellcheckrc or application.yml checks
#     everything: those change how every file is checked.
#   The edges are the `# shellcheck source=` directives, because they are what
#   -x follows; a `source` line shellcheck cannot resolve is not followed by
#   it either.
#
# Usage:
#   scripts/lib/shellcheck-ci.sh [--changed FILE] [--jobs N] [--list]
#
#   --jobs N   parallel shellcheck processes (default: the runner's cores)
#   --list     print the selected files, one a line, and check nothing
#
# Prints how many files it visited; zero is a pass only when the selection is
# empty, never because a glob failed to expand.

set -euo pipefail
# This script touches no project: it runs shellcheck over the checkout. It
# says so, so common.sh (no default project, DAYONE-DESAGA-2) does not stop on
# an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# The roots of main's check. application.yml's other shell steps walk the
# same globs (tests/unit/scripts/test_shellcheck_ci.py holds the two equal).
SET_GLOBS=("scripts/*.sh" "scripts/lib/*.sh" "scripts/lib/guard-bin/*" "scripts/acceptance/*.sh" "scripts/acceptance/groups/*.sh")

# A change to any of these changes how every file is checked.
EVERYTHING=("scripts/lib/shellcheck-ci.sh" ".shellcheckrc" ".github/workflows/application.yml")

changed_file=""
jobs=""
list_only=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --changed) [ "$#" -ge 2 ] || die "--changed needs a file"; changed_file="$2"; shift 2 ;;
    --jobs) [ "$#" -ge 2 ] || die "--jobs needs a number"; jobs="$2"; shift 2 ;;
    --list) list_only=1; shift ;;
    -h|--help) sed -n '2,36p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if [ -n "${changed_file}" ] && [ ! -r "${changed_file}" ]; then
  die "cannot read the change list ${changed_file}"
fi
if [ -z "${jobs}" ]; then
  # nproc honours a cgroup or affinity limit; macOS has only getconf.
  jobs="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
fi
case "${jobs}" in ''|*[!0-9]*|0) die "--jobs must be a positive number, not '${jobs}'" ;; esac

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

cd "${REPO_ROOT}"

# SET, sorted, repository-relative.
: > "${work}/set"
for pattern in "${SET_GLOBS[@]}"; do
  # Unquoted on purpose: the pattern is the glob to expand.
  # shellcheck disable=SC2206
  matches=(${pattern})
  for f in "${matches[@]}"; do
    if [ -f "$f" ]; then printf '%s\n' "$f" >> "${work}/set"; fi
  done
done
LC_ALL=C sort -u -o "${work}/set" "${work}/set"
[ -s "${work}/set" ] || die "no script matched ${SET_GLOBS[*]}: not the repository root?"

# A path made repository-relative and free of `.` and `..`, or nothing when
# it does not name a file. Directives are relative to the script's own
# directory (source-path=SCRIPTDIR) or, failing that, to the repository root.
resolve() {
  local from="$1" target="$2" dir candidate
  dir="$(dirname -- "${from}")"
  for candidate in "${dir}/${target}" "${target}"; do
    if [ -f "${candidate}" ]; then
      candidate="$(cd -- "$(dirname -- "${candidate}")" && pwd)/$(basename -- "${candidate}")"
      case "${candidate}" in
        "${REPO_ROOT}"/*) printf '%s\n' "${candidate#"${REPO_ROOT}"/}" ;;
      esac
      return 0
    fi
  done
  return 0
}

if [ -z "${changed_file}" ]; then
  cp "${work}/set" "${work}/selected"
  scope="every script main checks"
else
  # Does the change touch how every file is checked?
  everything=0
  while IFS= read -r path || [ -n "${path}" ]; do
    for name in "${EVERYTHING[@]}"; do
      if [ "${path}" = "${name}" ]; then everything=1; fi
    done
  done < "${changed_file}"

  if [ "${everything}" -eq 1 ]; then
    cp "${work}/set" "${work}/selected"
    scope="every script main checks (the change touches how shellcheck runs)"
  else
    # The edges: "<file>\t<file it sources>", for every file of SET.
    : > "${work}/edges"
    while IFS= read -r f; do
      sed -nE 's/^[[:space:]]*#[[:space:]]*shellcheck[[:space:]]+(.*[[:space:]])?source=([^[:space:]]+).*/\2/p' "$f" \
        | while IFS= read -r target; do
            dep="$(resolve "$f" "${target}")"
            if [ -n "${dep}" ]; then printf '%s\t%s\n' "$f" "${dep}" >> "${work}/edges"; fi
          done
    done < "${work}/set"

    grep -v '^[[:space:]]*$' "${changed_file}" | LC_ALL=C sort -u > "${work}/changed" || true
    # Seeds: the changed files that are roots (a deleted one is not a file).
    LC_ALL=C comm -12 "${work}/set" "${work}/changed" > "${work}/selected"

    # Forward: what the selected files source, until nothing is added.
    cp "${work}/selected" "${work}/frontier"
    while [ -s "${work}/frontier" ]; do
      awk -F '\t' 'NR == FNR { want[$0] = 1; next } ($1 in want) { print $2 }' \
        "${work}/frontier" "${work}/edges" | LC_ALL=C sort -u > "${work}/reached"
      LC_ALL=C comm -12 "${work}/set" "${work}/reached" | LC_ALL=C comm -23 - "${work}/selected" > "${work}/frontier"
      LC_ALL=C sort -u -o "${work}/selected" "${work}/selected" "${work}/frontier"
    done

    # Reverse: what sources a changed file (root or not), until nothing is added.
    cp "${work}/changed" "${work}/callees"
    cp "${work}/changed" "${work}/frontier"
    while [ -s "${work}/frontier" ]; do
      awk -F '\t' 'NR == FNR { want[$0] = 1; next } ($2 in want) { print $1 }' \
        "${work}/frontier" "${work}/edges" | LC_ALL=C sort -u | LC_ALL=C comm -23 - "${work}/callees" > "${work}/next"
      mv "${work}/next" "${work}/frontier"
      LC_ALL=C sort -u -o "${work}/callees" "${work}/callees" "${work}/frontier"
    done
    LC_ALL=C comm -12 "${work}/set" "${work}/callees" > "${work}/callers"
    LC_ALL=C sort -u -o "${work}/selected" "${work}/selected" "${work}/callers"
    scope="the changed scripts, what they source and what sources them"
  fi
fi

count="$(grep -c . "${work}/selected" || true)"

if [ "${list_only}" -eq 1 ]; then
  cat "${work}/selected"
  log "shellcheck-ci: ${count} file(s) selected: ${scope}"
  exit 0
fi

if [ "${count}" -eq 0 ]; then
  echo "shellcheck: 0 file(s) to check -- the change reaches no script"
  exit 0
fi
require_cmd shellcheck

# Batches of about a quarter of a worker's share, so a slow file does not
# leave the other workers idle at the end. Each batch writes its own file and
# they are printed after, so findings of parallel batches never interleave.
batch=$(( (count + jobs * 4 - 1) / (jobs * 4) ))
mkdir "${work}/out"
status=0
# The inner script is single-quoted on purpose: it runs in each batch's shell.
# shellcheck disable=SC2016
tr '\n' '\0' < "${work}/selected" \
  | xargs -0 -P "${jobs}" -n "${batch}" \
      bash -c 'out="$(mktemp "$1/batch.XXXXXX")"; shift; exec shellcheck -x "$@" > "${out}" 2>&1' _ "${work}/out" \
  || status=$?
for f in "${work}"/out/batch.*; do
  if [ -s "$f" ]; then cat "$f"; fi
done
echo "shellcheck: ${count} file(s) in batches of ${batch}, ${jobs} at a time (${scope})"
if [ "${status}" -ne 0 ]; then
  echo "::error::shellcheck reported findings or failed (xargs exit ${status})"
  exit 1
fi
