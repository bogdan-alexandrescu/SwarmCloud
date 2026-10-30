#!/usr/bin/env bash
# Publish a SwarmCloud release to the release home, record its manual test, and
# promote it to the stable channel (DAYONE-RELEASE-1; docs/design/day-one-plan.md
# sections 7.2 steps 5-6 and 7.3; D22).
#
#   scripts/release/publish.sh --bundle DIR --source-images build/images-<env>.json [--dry-run]
#   scripts/release/publish.sh --record-test V --record tested.json [--dry-run]
#   scripts/release/publish.sh --promote V [--dry-run]
#   ... [--project swarmcloud-releases]   (D23: had `swarmcloud` been taken)
#
# Run by a PERSON with rights in the release home, never by CI: CI holds no
# grant in project `swarmcloud`, so D22's "no IAM change for CI" holds. DIR is
# what scripts/release/assemble.sh wrote (release-public.yml uploads it as a
# workflow artefact); --source-images is the build record it was assembled
# from, which says where CI's registry holds each digest.
#
# --bundle: publish release V (read from the bundle) under releases/V/:
#   1. checks, before anything is written: every file matches SHA256SUMS and
#      SHA256SUMS lists every file; every accepted CVE has an expiry that has
#      not passed (lesson 27); every image's digest in images.json is the one
#      the build record holds; releases/V/ does not exist yet (a release is
#      never overwritten);
#   2. asks for V, typed (SWARM_ASSUME_YES never answers it);
#   3. copies each image BY DIGEST into us-docker.pkg.dev/<project>/swarmcloud
#      with gcrane, tagged V, checks the copy's digest, and signs that digest
#      with cosign and the KMS key (gcpkms://.../swarmcloud-release/cosign);
#   4. signs SHA256SUMS with the same key, and uploads everything under
#      releases/V/ (no object overwritten), SHA256SUMS LAST, so a release cut
#      short has no SHA256SUMS and no installer reads it.
#   It never touches channels/stable or the root marketplace.json.
#
# --record-test V --record FILE: the manual install-and-upgrade test of
#   section 7.3, written once to releases/V/TESTED. FILE is JSON: {version: V,
#   previous: <version or null>, tested_at, project, result: "pass", steps:
#   [{name, result: "pass"}, ...]}. A record that is not a pass is refused.
#
# --promote V: refused unless releases/V/TESTED records a pass for V. Then the
#   root marketplace.json (plugin `sc` at V, from ./releases/V/plugin), the
#   root install.sh (V's), and LAST channels/stable (one line: V), each with
#   Cache-Control: no-cache, so nobody reads yesterday's channel for an hour.
#
# --dry-run prints every write and runs none; the reads still run.
#
# Exit codes: 0 done, 1 refused or failed, 2 bad arguments.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR/..
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

# A publish and a promotion are never non-interactive: an automation that sets
# this does not get to answer the typed version.
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: a publish or a promotion always needs the version typed"
  unset SWARM_ASSUME_YES
fi

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# The release home, as scripts/release-home.sh creates it (D23, D25).
RELEASE_PROJECTS=("swarmcloud" "swarmcloud-releases")
PROJECT="swarmcloud"
LOCATION="us"
REPOSITORY="swarmcloud"
BUCKET="gs://swarmcloud"
KEYRING="swarmcloud-release"
KEY="cosign"

MODE=""; BUNDLE=""; SOURCE_IMAGES=""; VERSION=""; RECORD=""; DRY_RUN=0

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
set_mode() {
  [[ -z "${MODE}" || "${MODE}" == "$1" ]] || { err "--bundle, --record-test and --promote are separate runs"; usage; }
  MODE="$1"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)        [[ $# -ge 2 ]] || usage; set_mode publish; BUNDLE="$2"; shift 2 ;;
    --source-images) [[ $# -ge 2 ]] || usage; SOURCE_IMAGES="$2"; shift 2 ;;
    --record-test)   [[ $# -ge 2 ]] || usage; set_mode record; VERSION="$2"; shift 2 ;;
    --record)        [[ $# -ge 2 ]] || usage; RECORD="$2"; shift 2 ;;
    --promote)       [[ $# -ge 2 ]] || usage; set_mode promote; VERSION="$2"; shift 2 ;;
    --project)       [[ $# -ge 2 ]] || usage; PROJECT="$2"; shift 2 ;;
    --dry-run)       DRY_RUN=1; shift ;;
    -h|--help)       usage ;;
    *) err "unknown argument: $1"; usage ;;
  esac
done
[[ -n "${MODE}" ]] || usage
[[ "${MODE}" != publish || -n "${SOURCE_IMAGES}" ]] || { err "--bundle needs --source-images"; usage; }
[[ "${MODE}" != record || -n "${RECORD}" ]] || { err "--record-test needs --record"; usage; }

# ---------------------------------------------------------------------------
# Guards, before anything is called
# ---------------------------------------------------------------------------

# The shared project is the one whose service accounts SHARED_DENY_LIST names;
# read from the one list rather than spelling the project id a second time.
is_shared_project() {
  local candidate="$1" protected
  for protected in "${SHARED_DENY_LIST[@]}"; do
    [[ "${protected}" == *"@${candidate}.iam.gserviceaccount.com" ]] && return 0
  done
  return 1
}
if is_shared_project "${PROJECT}" || is_shared_resource "${PROJECT}"; then
  die "refusing project ${PROJECT}: it is the shared project or on SHARED_DENY_LIST (scripts/lib/common.sh); the release home is never there"
fi
release_project=0
for candidate in "${RELEASE_PROJECTS[@]}"; do
  [[ "${PROJECT}" != "${candidate}" ]] || release_project=1
done
[[ "${release_project}" -eq 1 ]] \
  || die "refusing project ${PROJECT}: the release home is one of ${RELEASE_PROJECTS[*]} (D23, D25)"

REGISTRY="${LOCATION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}"
KMS_KEY="gcpkms://projects/${PROJECT}/locations/${LOCATION}/keyRings/${KEYRING}/cryptoKeys/${KEY}"

require_cmd gcloud jq

WORK="$(mktemp -d "${TMPDIR:-/tmp}/release-publish.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# Every call that changes the release home goes through `write`: a dry run
# prints it and runs nothing. Reads run either way.
write() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    dim "would run: $*"
    return 0
  fi
  info "$*"
  "$@"
}

release_name_ok() { [[ "$1" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]+(-rc\.[0-9]+)?$ ]]; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# object_exists URL: 0 when the release home holds it.
object_exists() { gcloud storage ls "$1" >/dev/null 2>&1; }

# check_tested FILE VERSION: the record is a pass of exactly VERSION.
check_tested() {
  local file="$1" version="$2" problem
  problem="$(jq -r --arg v "${version}" '
    if type != "object" then "it is not a JSON object"
    elif .version != $v then "it records version \(.version), not \($v)"
    elif .result != "pass" then "its result is \(.result | tostring), not pass"
    elif ((.steps // []) | length) == 0 then "it lists no steps"
    elif ([.steps[] | select(.result != "pass")] | length) > 0 then
      "step(s) \([.steps[] | select(.result != "pass") | .name] | join(", ")) did not pass"
    elif ((.tested_at // "") == "") then "it has no tested_at"
    else "" end' "${file}" 2>/dev/null)" || problem="it is not JSON"
  [[ -z "${problem}" ]] || { err "the test record for ${version} is refused: ${problem}"; return 1; }
}

RELEASE() { printf '%s/releases/%s' "${BUCKET}" "$1"; }

# ---------------------------------------------------------------------------
# --record-test V --record FILE
# ---------------------------------------------------------------------------

record_test() {
  release_name_ok "${VERSION}" || { err "${VERSION} is not a release name"; exit 2; }
  [[ -f "${RECORD}" ]] || die "--record ${RECORD}: no such file"
  local release; release="$(RELEASE "${VERSION}")"
  check_tested "${RECORD}" "${VERSION}" || exit 1
  object_exists "${release}/SHA256SUMS" || die "release ${VERSION} is not published (${release}/SHA256SUMS); publish it first"
  if object_exists "${release}/TESTED"; then
    die "release ${VERSION} already has a test record (${release}/TESTED); a record is written once"
  fi
  [[ "${DRY_RUN}" -eq 1 ]] || confirm "Record the manual test of ${VERSION} in ${release}/TESTED." "${VERSION}"
  write gcloud storage cp --no-clobber "${RECORD}" "${release}/TESTED"
  ok "recorded the manual test of ${VERSION}; promote it with: scripts/release/publish.sh --promote ${VERSION}"
}

# ---------------------------------------------------------------------------
# --promote V: the ONLY place the stable channel moves
# ---------------------------------------------------------------------------

promote() {
  release_name_ok "${VERSION}" || { err "${VERSION} is not a release name"; exit 2; }
  local release; release="$(RELEASE "${VERSION}")"
  local CHANNEL="${BUCKET}/channels/stable"
  object_exists "${release}/SHA256SUMS" || die "release ${VERSION} is not published (${release}/SHA256SUMS)"
  if ! gcloud storage cat "${release}/TESTED" >"${WORK}/TESTED" 2>/dev/null; then
    die "release ${VERSION} has no manual test record (${release}/TESTED); run the install-and-upgrade test of plan section 7.3, then: scripts/release/publish.sh --record-test ${VERSION} --record <file>"
  fi
  check_tested "${WORK}/TESTED" "${VERSION}" || die "release ${VERSION} is not promoted"
  require_cmd python3
  python3 "${HERE}/marketplace.py" marketplace --version "${VERSION}" \
    --source "./releases/${VERSION}/plugin" --out "${WORK}/marketplace.json"
  printf '%s\n' "${VERSION}" >"${WORK}/stable"
  [[ "${DRY_RUN}" -eq 1 ]] || confirm "Promote ${VERSION}: move ${CHANNEL}, ${BUCKET}/marketplace.json and ${BUCKET}/install.sh to it." "${VERSION}"
  write gcloud storage cp --cache-control=no-cache "${WORK}/marketplace.json" "${BUCKET}/marketplace.json"
  write gcloud storage cp --cache-control=no-cache "${release}/install.sh" "${BUCKET}/install.sh"
  # LAST: an installer that reads the new channel finds everything it names.
  write gcloud storage cp --cache-control=no-cache "${WORK}/stable" "${CHANNEL}"
  ok "${VERSION} is the stable release"
}

# ---------------------------------------------------------------------------
# --bundle DIR --source-images FILE
# ---------------------------------------------------------------------------

check_sums() {
  local listed="${WORK}/listed" present="${WORK}/present" digest name bad=0
  [[ -f "${BUNDLE}/SHA256SUMS" ]] || die "${BUNDLE} has no SHA256SUMS; assemble it with scripts/release/assemble.sh"
  while read -r digest name; do
    [[ -n "${name}" ]] || continue
    printf '%s\n' "${name}" >>"${listed}"
    if [[ ! -f "${BUNDLE}/${name}" ]]; then
      err "${name} is in SHA256SUMS but not in the bundle"; bad=1
    elif [[ "$(sha256_of "${BUNDLE}/${name}")" != "${digest}" ]]; then
      err "${name} does not match SHA256SUMS"; bad=1
    fi
  done <"${BUNDLE}/SHA256SUMS"
  (cd -- "${BUNDLE}" && find . -type f ! -name SHA256SUMS | sed 's|^\./||' | LC_ALL=C sort) >"${present}"
  LC_ALL=C sort -o "${listed}" "${listed}"
  if ! diff -q "${listed}" "${present}" >/dev/null; then
    err "the bundle holds files SHA256SUMS does not list: $(comm -13 "${listed}" "${present}" | tr '\n' ' ')"
    bad=1
  fi
  [[ "${bad}" -eq 0 ]] || die "the bundle is refused: it is not what assemble.sh wrote"
}

check_cves() {
  local today problems
  today="$(date -u +%Y-%m-%d)"
  problems="$(jq -r --arg today "${today}" '
    if type != "array" then "cve-acceptances.json is not a list"
    else .[] | select((.expires // "") == "" or .expires < $today)
      | "\(.cve) is accepted \(if (.expires // "") == "" then "with no expiry" else "past its expiry (\(.expires))" end)"
    end' "${BUNDLE}/cve-acceptances.json" 2>/dev/null)" || problems="cve-acceptances.json is not JSON"
  if [[ -n "${problems}" ]]; then
    err "${problems//$'\n'/; }"
    die "the release is refused: an accepted CVE always ends, and an installer refuses one past its expiry (lesson 27)"
  fi
}

check_images() {
  local name digest ref bad=0
  while IFS=$'\t' read -r name digest <&3; do
    [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || { err "${name}: ${digest} is not a digest"; bad=1; continue; }
    ref="$(jq -r --arg n "${name}" '[.images[]? | select(.name == $n) | .ref][0] // ""' "${SOURCE_IMAGES}")"
    if [[ "${ref}" != *"@${digest}" ]]; then
      err "${name}: the build record holds ${ref:-nothing}, not ${digest}"; bad=1
    fi
  done 3< <(jq -r 'to_entries[] | [.key, .value] | @tsv' "${BUNDLE}/images.json")
  [[ "${bad}" -eq 0 ]] || die "the release is refused: images.json and the build record disagree"
}

publish() {
  [[ -d "${BUNDLE}" ]] || die "--bundle ${BUNDLE}: no such directory"
  [[ -f "${SOURCE_IMAGES}" ]] || die "--source-images ${SOURCE_IMAGES}: no such file"
  BUNDLE="$(cd -- "${BUNDLE}" && pwd)"
  check_sums
  VERSION="$(jq -r '.plugin // ""' "${BUNDLE}/compat.json")"
  release_name_ok "${VERSION}" || die "the bundle's compat.json names no release (${VERSION:-nothing})"
  [[ "$(jq -r '.version' "${BUNDLE}/plugin/.claude-plugin/plugin.json")" == "${VERSION}" ]] \
    || die "the bundle's plugin is not at ${VERSION}"
  check_cves
  check_images
  require_cmd gcrane cosign
  local release; release="$(RELEASE "${VERSION}")"
  if object_exists "${release}/"; then
    die "release ${VERSION} is already published (${release}/); a release is never overwritten -- assemble the next version"
  fi
  step "Publishing SwarmCloud ${VERSION} to ${release}/ and ${REGISTRY}"
  [[ "${DRY_RUN}" -eq 1 ]] || confirm "Publish ${VERSION}: copy and sign every image into ${REGISTRY}, upload the bundle to ${release}/." "${VERSION}"

  local name digest source copied
  while IFS=$'\t' read -r name digest <&3; do
    source="$(jq -r --arg n "${name}" '[.images[] | select(.name == $n) | .ref][0]' "${SOURCE_IMAGES}")"
    write gcrane cp "${source}" "${REGISTRY}/${name}:${VERSION}" </dev/null
    if [[ "${DRY_RUN}" -eq 0 ]]; then
      copied="$(gcrane digest "${REGISTRY}/${name}:${VERSION}" </dev/null)"
      [[ "${copied}" == "${digest}" ]] || die "${name}: the copy is ${copied}, not ${digest} (H28)"
    fi
    write cosign sign --yes "--key=${KMS_KEY}" "${REGISTRY}/${name}@${digest}" </dev/null
  done 3< <(jq -r 'to_entries[] | [.key, .value] | @tsv' "${BUNDLE}/images.json")

  # The public key travels with the release too (design 6.1); the engine
  # trusts the copy pinned in its wheel, so a tampered bucket cannot swap both.
  cosign public-key "--key=${KMS_KEY}" >"${WORK}/cosign.pub" </dev/null
  write cosign sign-blob --yes "--key=${KMS_KEY}" --output-signature "${WORK}/SHA256SUMS.sig" \
    --bundle "${WORK}/SHA256SUMS.cosign.bundle" "${BUNDLE}/SHA256SUMS" </dev/null

  write gcloud storage rsync --recursive --no-clobber --exclude='^SHA256SUMS$' "${BUNDLE}" "${release}"
  write gcloud storage cp --no-clobber "${WORK}/cosign.pub" "${release}/cosign.pub"
  write gcloud storage cp --no-clobber "${WORK}/SHA256SUMS.sig" "${release}/SHA256SUMS.sig"
  if [[ "${DRY_RUN}" -eq 1 || -f "${WORK}/SHA256SUMS.cosign.bundle" ]]; then
    write gcloud storage cp --no-clobber "${WORK}/SHA256SUMS.cosign.bundle" "${release}/SHA256SUMS.cosign.bundle"
  fi
  # LAST: a release cut short above has no SHA256SUMS, so no installer reads it.
  write gcloud storage cp --no-clobber "${BUNDLE}/SHA256SUMS" "${release}/SHA256SUMS"
  ok "published ${VERSION}$([[ "${DRY_RUN}" -eq 0 ]] || printf ' (dry run: nothing written)')"
  info "next: the manual test (plan section 7.3), then --record-test ${VERSION} --record <file>, then --promote ${VERSION}"
}

case "${MODE}" in
  publish) publish ;;
  record)  record_test ;;
  promote) promote ;;
esac
