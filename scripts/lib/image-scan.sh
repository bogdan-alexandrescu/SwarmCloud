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

# The exit code a FINDING gets, and only a finding. trivy exits 1 on its own
# errors -- a vulnerability DB it could not download, a layout it could not
# read -- so with `--exit-code 1` a FATAL DB download read as "found fixable
# vulnerabilities" and sent people looking for a CVE that did not exist
# (#2379). 3 is a code trivy never uses for itself. Any other non-zero exit is
# a scanner error: still a refusal (the gate fails closed), but named as one.
IMAGE_SCAN_FINDINGS_RC=3

# Where the vulnerability DB comes from, tried in this order: Google's mirror
# first, because ghcr.io rate-limits anonymous pulls (TOOMANYREQUESTS) and
# that is the measured way the download fails; ghcr.io, the upstream, second.
IMAGE_SCAN_DB_REPOSITORIES=(mirror.gcr.io/aquasec/trivy-db ghcr.io/aquasecurity/trivy-db)
IMAGE_SCAN_DB_ATTEMPTS=3
# Seconds before the second round, doubled before each later one (2, 4).
IMAGE_SCAN_DB_BACKOFF="${IMAGE_SCAN_DB_BACKOFF:-2}"

# image_scan_args -- set IMAGE_SCAN_ARGS to `trivy`'s arguments before the
# image: `"${TRIVY_BIN}" "${IMAGE_SCAN_ARGS[@]}" <image or --input layout>`.
image_scan_args() {
  IMAGE_SCAN_ARGS=(image --quiet --scanners vuln --severity "${IMAGE_SCAN_SEVERITY}"
    --exit-code "${IMAGE_SCAN_FINDINGS_RC}" --ignore-unfixed)
  if [[ -f "${REPO_ROOT}/.trivyignore.yaml" ]]; then
    IMAGE_SCAN_ARGS+=(--ignorefile "${REPO_ROOT}/.trivyignore.yaml")
  fi
}

# image_scan_db -- download trivy's vulnerability DB into its cache, retried,
# and only then add --skip-db-update to IMAGE_SCAN_ARGS (so call it after
# image_scan_args). Without it every scan downloads the DB itself, once, with
# no retry, and fails on the first rate-limited pull. --skip-db-update is not
# one of the policy flags above because a scan nobody ran this for has no DB
# to fall back on. Each round tries every repository in
# IMAGE_SCAN_DB_REPOSITORIES; IMAGE_SCAN_DB_ATTEMPTS rounds, backing off
# between them. Returns non-zero when no round got a DB: the caller reports
# that as a scanner error and refuses, because an unscanned image is not a
# clean one. Needs TRIVY_BIN.
image_scan_db() {
  local attempt=1 delay="${IMAGE_SCAN_DB_BACKOFF}" repo
  while :; do
    for repo in "${IMAGE_SCAN_DB_REPOSITORIES[@]}"; do
      if "${TRIVY_BIN}" image --quiet --download-db-only --db-repository "${repo}" 2>&1 | redact; then
        IMAGE_SCAN_ARGS+=(--skip-db-update)
        return 0
      fi
      warn "trivy could not download its vulnerability DB from ${repo} (round ${attempt} of ${IMAGE_SCAN_DB_ATTEMPTS})"
    done
    if [[ "${attempt}" -ge "${IMAGE_SCAN_DB_ATTEMPTS}" ]]; then
      err "trivy could not download its vulnerability DB in ${IMAGE_SCAN_DB_ATTEMPTS} rounds from ${IMAGE_SCAN_DB_REPOSITORIES[*]}"
      return 1
    fi
    sleep "${delay}"
    delay=$((delay * 2))
    attempt=$((attempt + 1))
  done
}
