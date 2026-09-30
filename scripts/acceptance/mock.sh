#!/usr/bin/env bash
# `run.sh --only mock`, under a name verify-remote.sh can address: it runs
# `scripts/<target>.sh` in the swarm-verify job and passes no arguments, so
# each acceptance group gets a file of its own (and a job execution, with its
# own 30-minute timeout).
set -euo pipefail
exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/run.sh" --only mock "$@"
