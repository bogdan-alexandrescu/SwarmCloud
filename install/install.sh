#!/bin/sh
# Install the SwarmCloud command line: `swarm` (with the installer's verbs,
# `swarm install | upgrade | uninstall | team | addon`) and `sc`
# (DAYONE-RELEASE-1; design section 6.3, plan R6).
#
#   curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | sh
#   curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | SWARMCLOUD_VERSION=2026.10.1 sh
#
# Published at the root of the release home by `scripts/release/publish.sh
# --promote`. With no SWARMCLOUD_VERSION it installs the stable channel's
# release. It downloads that release's wheels, checks each against the
# release's SHA256SUMS, and installs them as ONE uv tool, so `swarm install`
# finds the engine beside the bridge:
#
#   swarm_mcp      the bridge: `swarm`, `sc`, `swarm-mcp`
#   swarm_install  the engine (the Google clients live only here)
#   swarm_common, swarm_redaction
#                  the in-repo libraries both require, from the release
#                  itself: they are not on PyPI, and a bare name would be an
#                  index lookup
#
# The images and SHA256SUMS' signature are verified by the engine, with the
# release key pinned in its wheel, before anything is copied (stage 4).
#
# POSIX sh, not bash: `curl ... | sh` runs it with whatever /bin/sh is (dash on
# Debian and Ubuntu), so there is no pipefail; every pipeline below is a single
# command whose status is checked.
set -eu

HOME_URL="${SWARMCLOUD_HOME_URL:-https://storage.googleapis.com/swarmcloud}"

say() { printf 'swarmcloud: %s\n' "$*" >&2; }
fail() { say "$*"; exit 1; }

command -v curl >/dev/null 2>&1 || fail "curl is needed"
if ! command -v uv >/dev/null 2>&1; then
  fail "uv is needed (https://docs.astral.sh/uv/). Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh -- then run this again"
fi
if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  fail "sha256sum or shasum is needed to check the download"
fi

VERSION="${SWARMCLOUD_VERSION:-}"
if [ -z "${VERSION}" ]; then
  VERSION="$(curl -fsSL "${HOME_URL}/channels/stable")" || fail "cannot read ${HOME_URL}/channels/stable"
  VERSION="$(printf '%s' "${VERSION}" | tr -d '[:space:]')"
fi
case "${VERSION}" in
  [0-9][0-9][0-9][0-9].[0-9][0-9].[0-9]*) ;;
  *) fail "'${VERSION}' is not a SwarmCloud release name (YYYY.MM.N)" ;;
esac
# The wheels' file names carry the PEP 440 form: 2026.01.2 -> 2026.1.2,
# 2026.11.3-rc.2 -> 2026.11.3rc2.
WHEEL_VERSION="$(printf '%s' "${VERSION}" | sed -e 's/^\([0-9]*\)\.0*\([0-9][0-9]*\)\./\1.\2./' -e 's/-rc\./rc/')"
RELEASE="${HOME_URL}/releases/${VERSION}"

DIR="$(mktemp -d)"
trap 'rm -rf "${DIR}"' EXIT INT TERM

say "installing SwarmCloud ${VERSION} from ${RELEASE}"
curl -fsSL -o "${DIR}/SHA256SUMS" "${RELEASE}/SHA256SUMS" || fail "cannot read ${RELEASE}/SHA256SUMS"
for dist in swarm_mcp swarm_install swarm_common swarm_redaction; do
  wheel="${dist}-${WHEEL_VERSION}-py3-none-any.whl"
  curl -fsSL -o "${DIR}/${wheel}" "${RELEASE}/${wheel}" || fail "cannot download ${RELEASE}/${wheel}"
  want="$(awk -v f="${wheel}" '$2 == f { print $1 }' "${DIR}/SHA256SUMS")"
  [ -n "${want}" ] || fail "${wheel} is not in the release's SHA256SUMS"
  got="$(sha256 "${DIR}/${wheel}")"
  [ "${got}" = "${want}" ] || fail "${wheel} does not match SHA256SUMS (got ${got}); nothing was installed"
done

uv tool install --force --python '>=3.11' \
  "swarm-mcp @ file://${DIR}/swarm_mcp-${WHEEL_VERSION}-py3-none-any.whl" \
  --with "swarm-install @ file://${DIR}/swarm_install-${WHEEL_VERSION}-py3-none-any.whl" \
  --with "swarm-common @ file://${DIR}/swarm_common-${WHEEL_VERSION}-py3-none-any.whl" \
  --with "swarm-redaction @ file://${DIR}/swarm_redaction-${WHEEL_VERSION}-py3-none-any.whl" \
  || fail "uv tool install failed (its error is above)"

say "installed SwarmCloud ${VERSION}: swarm and sc"
say "next: swarm install --project <project> --env prod --version ${VERSION} --domain <host>"
