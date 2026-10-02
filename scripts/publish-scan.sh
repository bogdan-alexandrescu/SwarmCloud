#!/usr/bin/env bash
# publish-scan: run the worker's publish credential scan over what this
# checkout adds, before the worker does.
#
#   scripts/publish-scan.sh [--base <rev>]
#
# OWNER DECISION, 2026-10-02. The worker refuses to publish a diff whose ADDED
# lines look like a credential, and it decides only after the agent is done:
# three of six SwarmCloud lanes lost finished work there. This wraps
# `python -m agent_worker.publish_scan`, which runs the worker's OWN scanner,
# predicate and path tiers (it imports them; nothing is restated here or
# there), so a clean result here is the worker's answer for the same diff --
# except for a task's registered secrets, which only the worker knows.
#
# It scans the git work tree you run it from, against `--base`, else the
# merge-base of HEAD with origin/main, else HEAD; untracked files count, since
# the worker commits them. It prints `path:line rule` for each hit and never
# the value.
#
#   exit 0  nothing credential-shaped is added
#   exit 1  at least one hit; fix each named line (build a fake token at
#           runtime, do not re-add an unchanged secret-shaped NAME=value line)
#   exit 2  the scan could not run (unknown base, not a git work tree)
#
# SwarmCloud agents run this before they finish. A laptop lane leaves it to CI.

set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# `swarm_python` prints either one path or `uv run --project <root> python`.
read -r -a py <<<"$(swarm_python)"

exec "${py[@]}" -m agent_worker.publish_scan "$@"
