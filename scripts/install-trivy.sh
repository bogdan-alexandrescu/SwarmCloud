#!/usr/bin/env bash
# Installs trivy at the version CI pins (TRIVY_VERSION) into a directory, from the
# release tarball on GitHub, checked against the sha256 recorded below.
#
#   TRIVY_VERSION=0.74.0 scripts/install-trivy.sh <directory>
#
# WHY NOT TRIVY'S OWN contrib/install.sh. From v0.74.0 that script downloads the
# tarball from get.trivy.dev, and on 3 November 2026 (from about 21:55 universal
# time) that name stopped resolving: every CI and release job that installed
# trivy failed with curl exit 6 or 7 before it scanned anything (the security
# workflow's trivy jobs, the build check's image scan, the release's image scan
# and promotion). The same tarball is a release asset on GitHub, which the
# runners already reach, and pinning its sha256 here means a changed or
# substituted asset is refused instead of installed.
#
# BUMPING THE PIN: change TRIVY_VERSION where the workflows set it and add that
# version's two rows to TRIVY_SHA256 below, copied from the release's
# trivy_<version>_checksums.txt. A version with no row is refused, never
# installed unchecked.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# "<version> <asset> <sha256>", one row per asset this script may install.
TRIVY_SHA256=(
  "0.74.0 trivy_0.74.0_Linux-64bit.tar.gz 2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a"
  "0.74.0 trivy_0.74.0_Linux-ARM64.tar.gz b94ce1976bbf3c15b514b605ee88be7c6d94a29be2302847ff01cb794d47aad5"
)

[[ $# -eq 1 ]] || die "usage: TRIVY_VERSION=<version> $0 <directory>"
dest="$1"
version="${TRIVY_VERSION:-}"
[[ -n "${version}" ]] || die "TRIVY_VERSION is not set; the workflow that calls this pins it"

case "$(uname -m)" in
  x86_64 | amd64) arch="64bit" ;;
  aarch64 | arm64) arch="ARM64" ;;
  *) die "no trivy release asset for this machine ($(uname -m))" ;;
esac
[[ "$(uname -s)" == "Linux" ]] || die "this installs the Linux release; CI runners are Linux"
asset="trivy_${version}_Linux-${arch}.tar.gz"

want=""
for row in "${TRIVY_SHA256[@]}"; do
  read -r row_version row_asset row_sha <<<"${row}"
  if [[ "${row_version}" == "${version}" && "${row_asset}" == "${asset}" ]]; then
    want="${row_sha}"
  fi
done
[[ -n "${want}" ]] || die "no sha256 recorded for ${asset}: add the release's checksum row to TRIVY_SHA256 in $0"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
url="https://github.com/aquasecurity/trivy/releases/download/v${version}/${asset}"
info "downloading ${asset}"
curl -sSfL --retry 5 --retry-all-errors --connect-timeout 20 -o "${work}/${asset}" "${url}"

got="$(sha256sum "${work}/${asset}" | cut -d' ' -f1)"
[[ "${got}" == "${want}" ]] || die "${asset}: sha256 ${got} is not the recorded ${want}; refusing to install it"

mkdir -p "${dest}"
tar -xzf "${work}/${asset}" -C "${work}" trivy
install -m 0755 "${work}/trivy" "${dest}/trivy"
"${dest}/trivy" --version
