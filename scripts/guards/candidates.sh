#!/usr/bin/env bash
# Candidates for the deployment pattern list (docs/design/generic-repository.md
# section 1.5, step 4): every token the guard's shape layer lets through only
# because it does not know it -- each email domain and URL host outside the
# placeholder set, and each capitalised word that appears in prose more than
# five times and never in lower case (so not an English word).
#
#   scripts/guards/candidates.sh
#
# The owner sorts each candidate into the list's source object or into the
# allowlist (scripts/guards/placeholders.json). The candidates ARE deployment
# values, so they go to one new file under $TMPDIR, mode 600, and nowhere
# else: not the tree, not stdout, not a log. This prints the file's path and
# the number of candidates of each kind. It refuses a $TMPDIR inside the
# repository, where a later `git add` could stage the file.
set -euo pipefail
SWARM_NO_PROJECT=1
export SWARM_NO_PROJECT
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../lib/common.sh"

scratch="${TMPDIR:-/tmp}"
[[ -d "${scratch}" ]] || die "TMPDIR ${scratch} is not a directory"
scratch="$(cd -- "${scratch}" && pwd -P)"
tree="$(git rev-parse --show-toplevel 2>/dev/null)" || die "run this inside the repository"
tree="$(cd -- "${tree}" && pwd -P)"
case "${scratch}/" in
  "${tree}/"*) die "TMPDIR ${scratch} is inside the repository; point TMPDIR outside it so the candidates cannot be staged" ;;
esac

out="$(mktemp "${scratch}/deployment-candidates.XXXXXX")"
chmod 600 "${out}"
if ! counts="$(python3 "${SWARM_LIB_DIR}/../guards/deployment_values.py" candidates --out "${out}")"; then
  rm -f "${out}"
  die "the candidate sweep failed; nothing was kept"
fi
info "candidates (${counts}) written to ${out}; sort each into the list's source or the allowlist, then delete the file"
