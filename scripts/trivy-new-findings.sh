#!/usr/bin/env bash
# trivy-new-findings: fail a pull request only on the Trivy findings it adds.
#
#   scripts/trivy-new-findings.sh gate [--base <rev>]
#       Scan this checkout and, when --base names a commit, the same tree at
#       that commit (a detached worktree), then compare. No --base, or an empty
#       one (a push to main, the weekly schedule), compares with nothing: every
#       finding fails, which is what a gate on main should do.
#
#   scripts/trivy-new-findings.sh compare <base.json> <head.json>
#       Compare two `trivy fs --format json` reports.
#
# WHY. Measured on PR 1186 (2026-10-12, #1194): editing Dockerfile FROM lines
# made the PR's scan report "new" alerts that already existed on main,
# re-attributed to the lines the PR edited. A finding's LINE is not its
# identity -- a version bump moves it, an unrelated edit above it moves it. Its
# identity here is (class, target, rule id, package): the same CVE in the same
# package of the same lockfile or Dockerfile is the same finding, whatever line
# or installed version it is reported at. A finding absent from the base
# branch's scan of the same target is new, and only new findings fail the PR.
# Everything the scan finds is still uploaded as SARIF by the workflow, so
# what the base already had stays visible in the Security tab; it simply is
# not this PR's to fix.
#
# The scan is the gate's as before: vulnerabilities and secrets, HIGH and
# CRITICAL, fixable only (--ignore-unfixed: a failure nobody can fix teaches
# people to ignore the gate).
#
# It prints one line per new finding, tab-separated -- target, rule id,
# package (`-` for none), severity, title -- and never a secret's matched text,
# then the counts it compared on stderr, so an empty result is visibly a
# comparison and not a scan that did not run.
#
#   exit 0  no new finding
#   exit 1  at least one new finding (listed)
#   exit 2  a report could not be read, or the scan could not run

set -euo pipefail

# This script touches no project: it compares trivy reports of the checkout. It says so, so common.sh
# (no default project, DAYONE-DESAGA-2) does not stop on an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

usage() { awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0" >&2; exit 2; }

# The comparison. Rule ids: a vulnerability's VulnerabilityID, a secret's
# RuleID, a misconfiguration's AVDID (else ID; passes are not findings).
# shellcheck disable=SC2016  # jq program, not shell
FINDINGS_JQ='
def findings:
  [ .Results[]? as $r
    | ( ($r.Vulnerabilities // [])[]
          | {target: $r.Target, rule: .VulnerabilityID, pkg: (.PkgName // ""), severity: .Severity, title: (.Title // "")} ),
      ( ($r.Secrets // [])[]
          | {target: $r.Target, rule: .RuleID, pkg: "", severity: .Severity, title: (.Title // "")} ),
      ( ($r.Misconfigurations // [])[] | select(.Status != "PASS")
          | {target: $r.Target, rule: (.AVDID // .ID), pkg: "", severity: .Severity, title: (.Title // "")} ) ];
def key: [.target, .rule, .pkg] | tojson;
($base[0] | findings) as $b
| ($head[0] | findings) as $h
| (reduce ($b[] | key) as $k ({}; .[$k] = true)) as $known
| [ $h[] | select($known[key] | not) ] | unique_by(key) as $new
| "#counts head \($h | length), base \($b | length), new \($new | length)",
  ( $new[] | [.target, .rule, (if .pkg == "" then "-" else .pkg end), .severity, .title] | @tsv )
'

compare() {
  local base="$1" head="$2" out status
  local f
  for f in "${base}" "${head}"; do
    jq -e 'type == "object"' "${f}" >/dev/null 2>&1 || { err "${f} is not a Trivy JSON report"; return 2; }
  done
  out="$(mktemp "${TMPDIR:-/tmp}/trivy-new.XXXXXX")"
  if ! jq -rn --slurpfile base "${base}" --slurpfile head "${head}" "${FINDINGS_JQ}" >"${out}"; then
    rm -f "${out}"
    err "could not compare ${base} with ${head}"
    return 2
  fi
  status=0
  while IFS= read -r line; do
    case "${line}" in
      "#counts "*) info "trivy findings: ${line#\#counts }" ;;
      *) printf '%s\n' "${line}"; status=1 ;;
    esac
  done <"${out}"
  rm -f "${out}"
  return "${status}"
}

# scan DIR OUT [EXTRA...] -- the gate's scan of DIR as JSON.
scan() {
  local dir="$1" out="$2" bin
  shift 2
  bin="$(trivy_bin)" || { err "trivy not found; install it or set SWARM_TRIVY"; return 2; }
  "${bin}" fs --scanners vuln,secret --severity HIGH,CRITICAL --ignore-unfixed \
    --format json --output "${out}" --exit-code 0 --no-progress "$@" "${dir}"
}

gate() {
  local base_rev="" work status
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --base) base_rev="${2-}"; shift 2 ;;
      --base=*) base_rev="${1#*=}"; shift ;;
      *) usage ;;
    esac
  done
  work="$(mktemp -d "${TMPDIR:-/tmp}/trivy-gate.XXXXXX")"
  # shellcheck disable=SC2064  # expand now: ${work} is local
  trap "git worktree remove --force '${work}/base' >/dev/null 2>&1 || true; rm -rf '${work}'" EXIT

  scan . "${work}/head.json" || { err "the scan of this checkout failed"; return 2; }

  if [[ -z "${base_rev}" ]]; then
    info "no base commit: every finding is new"
    printf '{"Results":[]}\n' >"${work}/base.json"
  else
    # A shallow PR checkout does not hold the base commit; fetch just it.
    if ! git cat-file -e "${base_rev}^{commit}" 2>/dev/null; then
      git fetch --no-tags --depth=1 origin "${base_rev}" >/dev/null 2>&1 \
        || { err "could not fetch base commit ${base_rev}"; return 2; }
    fi
    git worktree add --detach "${work}/base" "${base_rev}" >/dev/null 2>&1 \
      || { err "could not check out base commit ${base_rev}"; return 2; }
    info "base ${base_rev}: scanning the same tree there"
    # The vulnerability DB was fetched by the first scan; the base is judged
    # against the same one.
    scan "${work}/base" "${work}/base.json" --skip-db-update \
      || { err "the scan of base commit ${base_rev} failed"; return 2; }
  fi

  status=0
  compare "${work}/base.json" "${work}/head.json" || status=$?
  case "${status}" in
    0) ok "no HIGH/CRITICAL fixable finding is new against ${base_rev:-nothing}" ;;
    1) err "the findings above are new against ${base_rev:-nothing}; fix or upgrade them (the Security tab has every finding)" ;;
  esac
  return "${status}"
}

case "${1:-}" in
  compare) [[ $# -eq 3 ]] || usage; compare "$2" "$3" ;;
  gate) shift; gate "$@" ;;
  *) usage ;;
esac
