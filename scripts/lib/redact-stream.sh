#!/usr/bin/env bash
# Filter stdin through the shared redaction rules. Exists so the Makefile's log
# targets can pipe gcloud output through redact() without inlining sed.
set -euo pipefail
# A filter: it touches no project, so sourcing common.sh must neither need one
# nor fetch the never-touch list (a pipe through it must not stop on either).
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
redact
