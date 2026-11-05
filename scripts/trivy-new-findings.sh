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
#   scripts/trivy-new-findings.sh accept-list <out.yaml>
#       Write the accept list the gate reads (below, gate_ignores) to
#       <out.yaml>, for the workflow's SARIF scan to pass as --ignorefile:
#       the code-scanning alerts then honour the same entries, and the same
#       expiry, as the gate (BGD, 2026-11-05, #2688). Prints the count kept.
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

# The accept list the gate reads (SWARM_TRIVY_IGNOREFILE overrides it for the
# tests): the image scans' .trivyignore.yaml (lib/image-scan.sh), but only its
# entries scoped to GATE_ACCEPTED_PATH.
GATE_ACCEPTED_PATH="apps/swarm-speech/uv.lock"

# gate_ignores IN OUT -- write to OUT the entries of IN whose paths name
# GATE_ACCEPTED_PATH and nothing else and whose expired_at is after today,
# each as written; print how many. BGD, 2026-11-05 (#2688): the repository
# gate accepts swarm-speech's pinned lockfile findings, and every other
# acceptance in the list -- the agent CLIs', the vendor Go binaries' -- stays
# the image scans' alone, as it was before the gate read the list at all. An
# entry with no paths would match every target, so it is never copied, nor
# one that also names another path. An expired entry is dropped here too, not
# left to trivy alone. A filtered copy, never a second hand-kept list. awk,
# because the runner's python3 has no YAML module and there is no yq: the
# list is one level of `- id:` entries, at any indent, with `paths:` as a
# block or a flow list, which is all this reads.
gate_ignores() {
  local in="$1" out="$2"
  awk -v want="${GATE_ACCEPTED_PATH}" -v today="$(date -u +%Y-%m-%d)" -v out="${out}" '
    function unquote(s) { gsub(/^[[:space:]]+|[[:space:]]+$/, "", s); gsub(/^["'\'']|["'\'']$/, "", s); return s }
    function path(s) { s = unquote(s); if (s == "") return; if (s == want) named++; else other++ }
    function flush(   i) {
      if (n > 0 && section != "" && named > 0 && other == 0 && expiry > today) {
        if (printed != section) { print section ":" >out; printed = section }
        for (i = 1; i <= n; i++) print entry[i] >out
        kept++
      }
      n = 0; named = 0; other = 0; expiry = ""; inpaths = 0
    }
    BEGIN { kept = 0; dash = -1; printf "" >out }
    /^[[:space:]]*(#.*)?$/ { next }
    /^[A-Za-z_]/ { flush(); section = $0; sub(/:.*/, "", section); dash = -1; next }
    {
      body = $0; sub(/[[:space:]]+#.*$/, "", body)
      indent = match(body, /[^ ]/) - 1; body = substr(body, indent + 1)
      item = (body ~ /^- /)
      # The section'"'"'s first list item sets the column every entry starts at.
      if (item && dash < 0) dash = indent
      if (item && indent == dash) { flush(); body = substr(body, 3); item = 0 }
      else if (n == 0) next
      entry[++n] = $0
      if (item) { if (inpaths) path(substr(body, 3)); next }
      if (body !~ /^[A-Za-z_]+:/) next
      inpaths = 0
      if (body ~ /^paths:/) {
        sub(/^paths:[[:space:]]*/, "", body)
        if (body ~ /^\[/) {
          gsub(/^\[|\]$/, "", body)
          k = split(body, items, ",")
          for (i = 1; i <= k; i++) path(items[i])
        } else { inpaths = 1 }
      }
      if (body ~ /^expired_at:/) { sub(/^expired_at:/, "", body); expiry = unquote(body) }
    }
    END { flush(); print kept }
  ' "${in}"
}

# accept_list OUT -- write gate_ignores' copy of the accept list
# (SWARM_TRIVY_IGNOREFILE, else .trivyignore.yaml) to OUT and print how many
# entries it kept. No list, or nothing kept, writes `{}`: an empty accept list
# trivy still parses, so a caller can always pass OUT as --ignorefile.
accept_list() {
  local out="$1" accept accepted=0
  accept="${SWARM_TRIVY_IGNOREFILE:-${REPO_ROOT}/.trivyignore.yaml}"
  if [[ -f "${accept}" ]]; then
    accepted="$(gate_ignores "${accept}" "${out}")" \
      || { err "could not read the accept list ${accept}"; return 2; }
    info "accepting ${accepted} unexpired entries of ${accept} scoped to ${GATE_ACCEPTED_PATH}; no other"
  else
    info "no accept list at ${accept}: accepting nothing"
  fi
  if [[ "${accepted}" == "0" ]]; then
    printf '{}\n' >"${out}" || { err "could not write ${out}"; return 2; }
  fi
  printf '%s\n' "${accepted}"
}

# scan DIR OUT [EXTRA...] -- the gate's scan of DIR as JSON. trivy 0.76 never
# loads an accept list by itself; the gate passes its own with --ignorefile.
scan() {
  local dir="$1" out="$2" bin
  shift 2
  bin="$(trivy_bin)" || { err "trivy not found; install it or set SWARM_TRIVY"; return 2; }
  "${bin}" fs --scanners vuln,secret --severity HIGH,CRITICAL --ignore-unfixed \
    --format json --output "${out}" --exit-code 0 --no-progress "$@" "${dir}"
}

gate() {
  local base_rev="" work status accepted ignore=()
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

  # Both sides read this checkout's list, so an acceptance is judged once.
  accepted="$(accept_list "${work}/gate.trivyignore.yaml")" || return 2
  [[ "${accepted}" == "0" ]] || ignore=(--ignorefile "${work}/gate.trivyignore.yaml")

  scan . "${work}/head.json" ${ignore[@]+"${ignore[@]}"} || { err "the scan of this checkout failed"; return 2; }

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
    scan "${work}/base" "${work}/base.json" --skip-db-update ${ignore[@]+"${ignore[@]}"} \
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
  accept-list) [[ $# -eq 2 ]] || usage; accept_list "$2" ;;
  *) usage ;;
esac
