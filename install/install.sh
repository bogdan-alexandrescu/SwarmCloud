#!/bin/sh
# Install the SwarmCloud command line: `swarm` (with the installer's verbs,
# `swarm install | upgrade | uninstall | team | addon`) and `sc`
# (DAYONE-RELEASE-1; design section 6.3, plan R6).
#
#   curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | sh
#   curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | SWARMCLOUD_VERSION=2026.10.1 sh
#   curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | SWARM_CHANNEL=rc sh
#
# Published at the root of the release home by every `scripts/release/publish.sh`
# run, a candidate's included, and by `--promote`. With no SWARMCLOUD_VERSION it installs the release that
# channels/<SWARM_CHANNEL> names: `stable` by default, `rc` for a release
# candidate (day-one decision 5); any other channel is refused. It downloads that release's wheels, checks each against the
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

# Checked before anything is fetched: the value becomes part of a URL.
CHANNEL="${SWARM_CHANNEL:-stable}"
case "${CHANNEL}" in
  stable | rc) ;;
  *) fail "SWARM_CHANNEL='${CHANNEL}' is not a release channel: use stable (the default) or rc" ;;
esac

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
  VERSION="$(curl -fsSL "${HOME_URL}/channels/${CHANNEL}")" || fail "cannot read ${HOME_URL}/channels/${CHANNEL}"
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

# Make `swarm` callable without asking the person to edit anything. uv puts a
# tool's commands in its tool bin directory: $UV_TOOL_BIN_DIR, else
# $XDG_BIN_HOME, else $XDG_DATA_HOME/../bin, else ~/.local/bin (uv's own order).
if [ -n "${UV_TOOL_BIN_DIR:-}" ]; then
  TOOL_BIN="${UV_TOOL_BIN_DIR}"
elif [ -n "${XDG_BIN_HOME:-}" ]; then
  TOOL_BIN="${XDG_BIN_HOME}"
elif [ -n "${XDG_DATA_HOME:-}" ]; then
  TOOL_BIN="${XDG_DATA_HOME}/../bin"
else
  TOOL_BIN="${HOME}/.local/bin"
fi

on_person_path() {
  case ":${PERSON_PATH}:" in
    *":$1:"* | *":$1/:"*) return 0 ;;
  esac
  return 1
}

# The rc file the person's login shell reads for a new terminal: zsh reads
# .zshrc; bash on macOS opens login shells, which read .bash_profile, and
# elsewhere interactive shells, which read .bashrc.
rc_file() {
  case "${SHELL:-}" in
    */zsh | zsh) printf '%s\n' "${ZDOTDIR:-${HOME}}/.zshrc" ;;
    */bash | bash)
      if [ "$(uname -s)" = "Darwin" ]; then
        printf '%s\n' "${HOME}/.bash_profile"
      else
        printf '%s\n' "${HOME}/.bashrc"
      fi
      ;;
    */fish | fish) printf '%s\n' "${XDG_CONFIG_HOME:-${HOME}/.config}/fish/config.fish" ;;
    *) printf '%s\n' "${HOME}/.profile" ;;
  esac
}

# One line, found again by its marker so a second run writes nothing.
RC_MARKER="# swarmcloud: swarm on PATH"
add_to_rc() {
  rc="$(rc_file)"
  if [ -f "${rc}" ] && grep -qF "${RC_MARKER}" "${rc}"; then
    say "${TOOL_BIN} is already put on PATH by ${rc}"
    return 0
  fi
  case "${TOOL_BIN}" in
    *"'"*)
      say "cannot write ${TOOL_BIN} into ${rc} (its name holds a quote); open a new terminal after adding it to PATH"
      return 0
      ;;
  esac
  case "${rc}" in
    *.fish) line="fish_add_path '${TOOL_BIN}' ${RC_MARKER}" ;;
    *) line="export PATH='${TOOL_BIN}':\"\${PATH}\" ${RC_MARKER}" ;;
  esac
  mkdir -p "$(dirname "${rc}")" 2>/dev/null || true
  if printf '\n%s\n' "${line}" >>"${rc}" 2>/dev/null; then
    say "added ${TOOL_BIN} to PATH in ${rc}"
  else
    say "cannot write ${rc}: add ${TOOL_BIN} to your PATH"
  fi
}

# A piped install cannot change the PATH of the shell it was typed in. When
# that PATH already holds a writable directory of the person's own (under
# HOME), swarm and sc are linked there, so this terminal's next command finds
# them; the rc line covers every new terminal.
link_into_person_path() {
  old_ifs="${IFS}"
  IFS=:
  set -f
  # shellcheck disable=SC2086 # split on ':' on purpose, with globbing off
  set -- ${PERSON_PATH}
  set +f
  IFS="${old_ifs}"
  for dir in "$@"; do
    case "${dir}" in
      "${HOME}"/*) ;;
      *) continue ;;
    esac
    if [ ! -d "${dir}" ] || [ ! -w "${dir}" ]; then
      continue
    fi
    linked=""
    for tool in swarm sc; do
      [ -x "${TOOL_BIN}/${tool}" ] || continue
      if [ -e "${dir}/${tool}" ] && [ ! -L "${dir}/${tool}" ]; then
        continue
      fi
      ln -sf "${TOOL_BIN}/${tool}" "${dir}/${tool}" && linked="${linked} ${tool}"
    done
    if [ -n "${linked}" ]; then
      say "linked${linked} into ${dir}, which this terminal's PATH holds"
    fi
    case " ${linked} " in
      *" swarm "*) SWARM_FOUND=1 ;;
    esac
    return 0
  done
}

# How this terminal runs swarm: plain `swarm` when its PATH finds ours, else
# the path installed, so the last line works without a new terminal (audit 3,
# A9: macOS zsh has no directory of the person's own on PATH, and `Next: swarm
# install` there was `command not found`). Under HOME it is shown as ~/...;
# a path with a character the shell would split or expand is single-quoted.
swarm_command() {
  if [ "${SWARM_FOUND}" -eq 1 ]; then
    printf 'swarm\n'
    return 0
  fi
  shown="${TOOL_BIN}/swarm"
  case "${shown}" in
    *[!A-Za-z0-9_./+@%:=,-]*) printf "'%s'\n" "${shown}"; return 0 ;;
  esac
  case "${shown}" in
    "${HOME}"/*) shown="~${shown#"${HOME}"}" ;;
  esac
  printf '%s\n' "${shown}"
}

SWARM_FOUND=0
if on_person_path "${TOOL_BIN}"; then
  SWARM_FOUND=1
else
  add_to_rc
  link_into_person_path
fi

printf '\nNext: %s install\n' "$(swarm_command)" >&2
