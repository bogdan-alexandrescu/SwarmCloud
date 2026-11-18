#!/usr/bin/env bash
# The deployment-values guard: finds what ties the repository to a company, a
# person or a live deployment, and never prints what it found
# (docs/design/generic-repository.md section 4; the rules and their reasons are
# in deployment_values.py beside this file).
#
#   scripts/guards/deployment-values.sh --tree            every tracked file
#   scripts/guards/deployment-values.sh --diff <base>     lines added since <base>
#   scripts/guards/deployment-values.sh --text <file>     one file (issue text)
#
# Options: --mode report|enforce|summary (default report: counts per
# directory, exit 0), --patterns-file <file>, --patterns-secret (Secret
# Manager), --archive-refs, --depth <n>. The pattern list comes from
# DEPLOYMENT_PATTERNS when no option names one.
#
# Exit: 0 clean (or report mode), 1 findings, 2 the list is unavailable under
# CI, altered or malformed, 3 no list locally (the name layer did not run).
set -euo pipefail
# A local check: it reads no project unless --patterns-secret asks for one.
SWARM_NO_PROJECT=1
export SWARM_NO_PROJECT
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../lib/common.sh"

SWARM_GCLOUD="$(prefer_local_bin gcloud "${SWARM_GCLOUD:-}" || printf 'gcloud')"
export SWARM_GCLOUD
exec python3 "${SWARM_LIB_DIR}/../guards/deployment_values.py" "$@"
