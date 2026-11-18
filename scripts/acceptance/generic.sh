#!/usr/bin/env bash
# `run.sh --only generic`, under a name verify-remote.sh can address (see mock.sh).
set -euo pipefail
exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/run.sh" --only generic "$@"
