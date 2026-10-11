#!/usr/bin/env bash
set -euo pipefail
export LD_LIBRARY_PATH=/tmp/libroot/usr/lib/x86_64-linux-gnu:/tmp/libroot/lib/x86_64-linux-gnu
export FONTCONFIG_FILE=/tmp/fonts.conf
export PLAYWRIGHT_BROWSERS_PATH=/workspace/att_34e54b44ddfd49f2a67d/work/.cache/ms-playwright
export SWARM_ARTIFACTS_DIR=${SWARM_ARTIFACTS_DIR:-/workspace/att_34e54b44ddfd49f2a67d/artifacts}
cd "$(dirname "$0")"
node probe.mjs "$1" 2>&1 | grep -v "^\s*-\|Fontconfig"
