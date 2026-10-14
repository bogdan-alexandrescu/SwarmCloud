#!/usr/bin/env bash
# The image scan's policy, in one place: what `push-images.sh --scan` refuses
# to promote (the release) and what `scan-image-layout.sh` refuses on a pull
# request (application.yml `build-check`, C3, owner decision 2026-10-14).
# Sourced, never executed. Needs common.sh first.
#
# WHY ONE PLACE. The pull request's scan exists to say, before the merge,
# what the release's scan would say after it: on 2026-10-14 a curl CVE in
# Debian blocked every release for hours and was first seen there. Two copies
# of these flags would be two gates that can disagree, which is the one thing
# the earlier one must not do.
#
#   fixable HIGH and CRITICAL findings only (--ignore-unfixed): a finding no
#   package version fixes yet is not something a pull request can act on;
#   .trivyignore.yaml carries the accepted ones, each with an expiry, so an
#   entry that outlives its date fails the scan again.

set -euo pipefail

IMAGE_SCAN_SEVERITY="${TRIVY_SEVERITY:-HIGH,CRITICAL}"

# image_scan_args -- set IMAGE_SCAN_ARGS to `trivy`'s arguments before the
# image: `"${TRIVY_BIN}" "${IMAGE_SCAN_ARGS[@]}" <image or --input layout>`.
image_scan_args() {
  IMAGE_SCAN_ARGS=(image --quiet --scanners vuln --severity "${IMAGE_SCAN_SEVERITY}" --exit-code 1 --ignore-unfixed)
  if [[ -f "${REPO_ROOT}/.trivyignore.yaml" ]]; then
    IMAGE_SCAN_ARGS+=(--ignorefile "${REPO_ROOT}/.trivyignore.yaml")
  fi
}
