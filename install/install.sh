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
if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  fail "sha256sum or shasum is needed to check the download"
fi

DIR="$(mktemp -d)"
trap 'rm -rf "${DIR}"' EXIT INT TERM

# uv, when it is missing: the pinned release binary from astral-sh/uv's GitHub
# release, checked against the SHA-256 below before it is unpacked, into
# ~/.local/bin. Never a remote script piped to a shell. Python is never a
# prerequisite: `uv tool install` below fetches a managed one. The digests are
# the release's own uv-<target>.tar.gz.sha256 files for 0.13.6, read
# 2026-10-30; Linux takes the static musl build, which runs on any libc.
UV_VERSION="0.13.6"
UV_SHA256_X86_64_APPLE_DARWIN="63ba17fdda55ad1a230d4b37fc6017ffa10c23688c83818bd3bfdf914c652a02"
UV_SHA256_AARCH64_APPLE_DARWIN="60fbd5c8a1ed681845eea28d9d622fcde03c51e91a43465b64a1e17db1ed3472"
UV_SHA256_X86_64_UNKNOWN_LINUX_MUSL="5d61f26b6c7213779eda90edc6971ea6b0b796bfb8d49a574c8ad69b59838548"
UV_SHA256_AARCH64_UNKNOWN_LINUX_MUSL="819692e404d23e5aae6bc44c0e260f1f5492fd1910ab4693b2c650f926dbe800"

install_uv() {
  case "$(uname -s)/$(uname -m)" in
    Darwin/x86_64) target="x86_64-apple-darwin"; want="${UV_SHA256_X86_64_APPLE_DARWIN}" ;;
    Darwin/arm64) target="aarch64-apple-darwin"; want="${UV_SHA256_AARCH64_APPLE_DARWIN}" ;;
    Linux/x86_64 | Linux/amd64) target="x86_64-unknown-linux-musl"; want="${UV_SHA256_X86_64_UNKNOWN_LINUX_MUSL}" ;;
    Linux/aarch64 | Linux/arm64) target="aarch64-unknown-linux-musl"; want="${UV_SHA256_AARCH64_UNKNOWN_LINUX_MUSL}" ;;
    *) fail "no pinned uv for $(uname -s)/$(uname -m); install uv (https://docs.astral.sh/uv/) and run this again" ;;
  esac
  command -v tar >/dev/null 2>&1 || fail "tar is needed to unpack uv"
  url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz"
  say "uv is not installed: installing the pinned uv ${UV_VERSION} into ${UV_BIN}"
  curl -fsSL -o "${DIR}/uv.tar.gz" "${url}" || fail "cannot download ${url}"
  got="$(sha256 "${DIR}/uv.tar.gz")"
  [ "${got}" = "${want}" ] || fail "uv-${target}.tar.gz does not match its pinned SHA-256 (got ${got}); nothing was installed"
  mkdir -p "${DIR}/uv" "${UV_BIN}"
  tar -xzf "${DIR}/uv.tar.gz" -C "${DIR}/uv" || fail "cannot unpack uv-${target}.tar.gz"
  for tool in uv uvx; do
    [ -f "${DIR}/uv/uv-${target}/${tool}" ] || fail "uv-${target}.tar.gz holds no ${tool}"
    cp "${DIR}/uv/uv-${target}/${tool}" "${UV_BIN}/${tool}.partial"
    chmod 755 "${UV_BIN}/${tool}.partial"
    mv -f "${UV_BIN}/${tool}.partial" "${UV_BIN}/${tool}"
  done
}

UV_BIN="${HOME}/.local/bin"
PERSON_PATH="${PATH}"
if ! command -v uv >/dev/null 2>&1; then
  if [ -x "${UV_BIN}/uv" ]; then
    say "using ${UV_BIN}/uv (it is not on your PATH)"
  else
    install_uv
  fi
  PATH="${UV_BIN}:${PATH}"
  export PATH
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
case ":${PERSON_PATH}:" in
  *":${UV_BIN}:"*) ;;
  *) say "add ${UV_BIN} to your PATH (uv tool update-shell does it), or open a new terminal" ;;
esac
say "next: swarm install --project <project> --env prod --version ${VERSION} --domain <host>"
