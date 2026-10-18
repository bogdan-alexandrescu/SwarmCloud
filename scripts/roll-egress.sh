#!/usr/bin/env bash
# Roll swarm-egress, the live browser's egress forwarder (StatefulSet
# swarm-system/swarm-egress on the swarm's Autopilot cluster), to the image
# this environment's terraform rendered, wait for the rollout, and prove its
# pods run that digest (docs/browser-egress.md §9.3; lane EGRESS-BUILD, part
# of #1030).
#
#   scripts/roll-egress.sh                                     # dry run: apply.sh's preview
#   scripts/roll-egress.sh --confirm                           # apply, rollout, digest
#   scripts/roll-egress.sh --tf-init --get-credentials --confirm   # what the release runs
#   scripts/roll-egress.sh --confirm --timeout 900             # a slower first volume
#
# WHY. terraform/infra/egress.tf renders `swarm_egress_manifest` with the
# release's swarm-egress digest, and `kubernetes/apply.sh --swarm-system`
# applies it after the live relay -- but nothing waited for the forwarder to
# roll or read back what it runs, so a crash-looping or stale forwarder would
# pass a release while every proxied browser attempt failed closed.
#
# THIS IS scripts/roll-live-relay.sh --component egress, AND NOTHING ELSE. The
# apply (apply.sh's, with its manifest checks), the cluster checks, the
# shared-project deny-list (scripts/lib/common.sh: never agents-staging), the
# rollout wait and the digest read-back live there once, for both
# StatefulSets; a second copy here is how a deny-list check drifts. Every flag
# is passed through. Off (enable_browser_egress false) is a notice and exit 0;
# on with no swarm-egress digest in image_refs is a failure.
#
# apply.sh --swarm-system --confirm needs the live relay's manifest too (it
# creates swarm-system ahead of the policies), so an environment with
# enable_live_browser off cannot roll swarm-egress either; dev has both on.
#
# Exit: 0 swarm-egress runs the rendered digest (or egress is off, or a dry run
# previewed); 1 anything refused, failed, timed out or still on another image.
set -euo pipefail

# This file's own header, not the relay script's.
case "${1:-}" in
  -h|--help) awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0"; exit 0 ;;
esac
exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/roll-live-relay.sh" --component egress "$@"
