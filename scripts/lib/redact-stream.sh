#!/usr/bin/env bash
# Filter stdin through the shared redaction rules. Exists so the Makefile's log
# targets can pipe gcloud output through redact() without inlining sed.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
redact
