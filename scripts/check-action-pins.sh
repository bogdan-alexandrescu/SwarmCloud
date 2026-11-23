#!/usr/bin/env bash
# check-action-pins: refuse a third-party GitHub Action that is not pinned to
# a full commit SHA.
#
#   scripts/check-action-pins.sh [PATH ...]
#
# WHY THIS EXISTS (audit, 2026-11-21). Every `uses:` in .github/ named a tag
# (`actions/checkout@v5`), and a tag is whatever its owner last pushed to it:
# a moved or compromised tag runs new code in every job that names it, with
# that job's token and its cloud credentials, and nothing in this repository
# changes to show it. A commit SHA cannot move. So every `uses:` outside this
# repository's own actions names the commit, with the version it was resolved
# from in a trailing comment, which is what a reader and an updater read:
#
#     uses: actions/checkout@<40 hex> # v5.1.0
#
# To pin or bump one, resolve the tag to its commit, dereferencing an
# annotated tag:
#
#     git ls-remote https://github.com/<owner>/<repo> 'refs/tags/<tag>' 'refs/tags/<tag>^{}'
#
# and take the `^{}` line when there is one.
#
# WHAT IT ACCEPTS. A local action (`./...`); a container action pinned by
# digest (`docker://<image>@sha256:<64 hex>`); and `<owner>/<repo>[/<path>]@<40
# lowercase hex> # <version>`. Anything else is a finding, printed as
# `path:line: reason: <uses>`: a tag, a branch, a short SHA, a pin with no
# version comment, a container by tag.
#
# WHAT IT READS. Every *.yml and *.yaml under each PATH (a file or a
# directory), default .github/. It reads a `uses:` key in block style only: a
# line whose first token, after an optional `- `, is `uses:`, `"uses":` or
# `'uses':`, with its value on the same line. Any other line, outside a comment
# line, where `uses` is a key (a flow mapping `- {uses: ...}`, a space before
# the colon, a value on a later line) is a finding too, `uses key in a shape
# this check cannot read (use block style)`, whatever it names: a line-reader
# cannot check it, and skipping it would pass it in silence (issue 190). A
# comment line is never read. It prints how many files and `uses:` it read,
# and a run that read no file fails: an empty answer is not a pass (CLAUDE.md,
# "Empty output is not success").
#
# Exit 0 when every `uses:` is pinned, 1 on any finding or when nothing was
# read, 2 on a usage error. It touches no project and no network.
# tests/unit/scripts/test_check_action_pins.py runs it; application.yml's
# quick-checks job runs it beside actionlint.

set -euo pipefail
# This script touches no project: it reads files only. It says so, so
# common.sh does not stop on an unset PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

usage() {
  sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

case "${1:-}" in
  -h|--help) usage ;;
  -*) log "check-action-pins: unknown option $1"; usage ;;
esac

roots=("$@")
[[ ${#roots[@]} -gt 0 ]] || roots=("${REPO_ROOT}/.github")

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-action-pins.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
FILES="${WORK}/files"
: >"${FILES}"
for root in "${roots[@]}"; do
  if [[ -f "${root}" ]]; then
    printf '%s\n' "${root}" >>"${FILES}"
  elif [[ -d "${root}" ]]; then
    find "${root}" -type f \( -name '*.yml' -o -name '*.yaml' \) | LC_ALL=C sort >>"${FILES}"
  else
    die "check-action-pins: ${root}: no such file or directory"
  fi
done

files=0
uses=0
findings=0
# A block-style `uses:` line, key optionally quoted: its value and what follows.
USES_RE='^[[:space:]]*(-[[:space:]]+)?("uses"|'"'uses'"'|uses):[[:space:]]*(.*)$'
# `uses` as a key in any shape, quoted or not: what USES_RE must account for.
ANY_USES_RE='(^|[-{,[:space:]])["'"'"']?uses["'"'"']?[[:space:]]*:'
COMMENT_RE='^[[:space:]]*#'
UNREADABLE="uses key in a shape this check cannot read (use block style)"
SHA_RE='^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$'
DIGEST_RE='^docker://[^[:space:]@]+@sha256:[0-9a-f]{64}$'
VERSION_RE='^#[[:space:]]*v?[0-9]+(\.[0-9]+)*([.+-][0-9A-Za-z.+-]*)?$'

# finding LINE REASON USES -- one finding in the file being read (`where`).
finding() {
  findings=$((findings + 1))
  printf '%s:%s: %s: %s\n' "${where}" "$1" "$2" "$3"
}

while IFS= read -r file; do
  [[ -n "${file}" ]] || continue
  files=$((files + 1))
  where="${file#"${REPO_ROOT}/"}"
  number=0
  # Read on fd 3, so nothing in the loop can read the file by accident.
  while IFS= read -r line <&3 || [[ -n "${line}" ]]; do
    number=$((number + 1))
    [[ ! "${line}" =~ ${COMMENT_RE} ]] || continue
    if [[ ! "${line}" =~ ${USES_RE} ]]; then
      if [[ "${line}" =~ ${ANY_USES_RE} ]]; then
        uses=$((uses + 1))
        finding "${number}" "${UNREADABLE}" "${line#"${line%%[![:space:]]*}"}"
      fi
      continue
    fi
    rest="${BASH_REMATCH[3]}"
    # The value is the first word, quoted or not; the comment is what follows.
    value="${rest%%[[:space:]]*}"
    comment="${rest#"${value}"}"
    comment="${comment#"${comment%%[![:space:]]*}"}"
    value="${value#[\"\']}"
    value="${value%[\"\']}"
    uses=$((uses + 1))
    if [[ -z "${value}" ]]; then
      finding "${number}" "${UNREADABLE}" "${line#"${line%%[![:space:]]*}"}"
      continue
    fi
    case "${value}" in
      ./*) continue ;;
      docker://*)
        [[ "${value}" =~ ${DIGEST_RE} ]] \
          || finding "${number}" "a container action not pinned by its sha256 digest" "${value}"
        continue ;;
    esac
    if [[ ! "${value}" =~ ${SHA_RE} ]]; then
      finding "${number}" "not pinned to a full commit SHA (owner/repo@<40 hex> # <version>)" "${value}"
    elif [[ ! "${comment}" =~ ${VERSION_RE} ]]; then
      finding "${number}" "pinned, but with no '# <version>' comment naming the release it was resolved from" "${value}"
    fi
  done 3<"${file}"
done <"${FILES}"

if [[ "${files}" -eq 0 ]]; then
  die "check-action-pins: no *.yml or *.yaml file under ${roots[*]}: nothing was checked"
fi
if [[ "${findings}" -gt 0 ]]; then
  err "check-action-pins: ${findings} action(s) not pinned to a commit SHA or not in block style, of ${uses} uses: in ${files} file(s)"
  exit 1
fi
ok "check-action-pins: every one of ${uses} uses: in ${files} file(s) is local or pinned to a commit SHA"
