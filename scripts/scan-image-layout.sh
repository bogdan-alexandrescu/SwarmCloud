#!/usr/bin/env bash
# Scan one image a pull request built, as the release would scan it.
#
#   scripts/scan-image-layout.sh <oci-layout-dir> <image-name>
#
# C3 (owner decision 2026-10-14, docs/ci.md "A pull request scans the images
# it builds"). `build-images.sh --build-only --local` exports each image it
# builds to an OCI layout when BUILD_ONLY_SCAN names this script, and runs it
# on that layout as soon as the image is built. The flags are the release's
# (scripts/lib/image-scan.sh, which push-images.sh --scan reads too), so a
# fixable HIGH or CRITICAL finding that would stop the release stops the pull
# request instead. Needs trivy at the version release.yml pins (the job
# installs it); no registry, no credential, nothing pushed.
#
#   exit 0  no fixable finding at the policy's severity
#   exit 1  findings (listed above the exit), or trivy could not read the layout
#   exit 2  usage

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/image-scan.sh
source "${REPO_ROOT}/scripts/lib/image-scan.sh"

if [[ $# -ne 2 || ! -d "$1" ]]; then
  err "usage: $(basename "$0") <oci-layout-dir> <image-name>"
  exit 2
fi
layout="$1"
name="$2"

TRIVY_BIN="$(trivy_bin || true)"
[[ -n "${TRIVY_BIN}" ]] || die "trivy not found; the build-check job installs the version release.yml pins"

image_scan_args
step "Scan ${name} (${IMAGE_SCAN_SEVERITY}, fixable only)"
if ! "${TRIVY_BIN}" "${IMAGE_SCAN_ARGS[@]}" --input "${layout}" 2>&1 | redact; then
  err "${name}: trivy found fixable ${IMAGE_SCAN_SEVERITY} vulnerabilities; the release would refuse to promote this image"
  exit 1
fi
ok "${name}: scan clean"
