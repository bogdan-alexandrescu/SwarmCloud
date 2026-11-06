#!/usr/bin/env bash
# Publish a SwarmCloud release to the release home, record its manual test, and
# promote it to the stable channel (DAYONE-RELEASE-1; docs/design/day-one-plan.md
# sections 7.2 steps 5-6 and 7.3; D22).
#
#   scripts/release/publish.sh --bundle DIR --source-images build/images-<env>.json [--dry-run] [--ci]
#   scripts/release/publish.sh --record-test V --record tested.json [--dry-run]
#   scripts/release/publish.sh --promote V [--dry-run]
#   ... [--project swarmcloud-releases]   (D23: had `swarmcloud` been taken)
#
# A publish is confirmed by a PERSON, one of two ways. At a terminal, by typing
# V. Or, since BGD's "Move publishing into CI now" (2026-10-30, DAYONE-RELEASE-CI,
# which ends D22's "CI holds no grant in the release home"), by dispatching
# .github/workflows/publish-release.yml with V and approving its `release`
# environment: that run calls --bundle ... --ci, and the version typed into
# the dispatch form, handed over as SWARM_PUBLISH_VERSION, stands for the typed
# V. --ci is refused unless GITHUB_ACTIONS is `true` and SWARM_PUBLISH_VERSION
# is exactly the bundle's version; nothing else (SWARM_ASSUME_YES least of
# all) answers the confirmation. --record-test and --promote take no --ci: the
# stable channel moves only by a person's typed act. DIR is what
# scripts/release/assemble.sh wrote; --source-images is the build record it
# was assembled from, which says where CI's registry holds each digest.
#
# --bundle: publish release V (read from the bundle) under releases/V/:
#   1. checks, before anything is written: every file matches SHA256SUMS and
#      SHA256SUMS lists every file; marketplace.json names plugin.zip by its
#      absolute release URL and digest; every accepted CVE has an expiry that has
#      not passed (lesson 27); every image's digest in images.json is the one
#      the build record holds; releases/V/ does not exist yet (a release is
#      never overwritten);
#   2. asks for V, typed (SWARM_ASSUME_YES never answers it);
#   3. copies each image BY DIGEST into us-docker.pkg.dev/<project>/swarmcloud
#      with gcrane, tagged V, checks the copy's digest, and signs that digest
#      with cosign and the KMS key (gcpkms://.../swarmcloud-release/cosign);
#   4. signs SHA256SUMS with the same key BEFORE any image is copied, and
#      refuses unless `cosign verify-blob` passes with the release_key.pub in
#      the bundle's own swarm_install wheel, the key every install pins (audit
#      3, A11); then uploads everything under
#      releases/V/ (no object overwritten), SHA256SUMS LAST, so a release cut
#      short has no SHA256SUMS and no installer reads it. The bundle's
#      third-party/ tree (the Lightpanda image's Corresponding Source, AGPL
#      section 6(d)) goes instead to the bucket ROOT, third-party/..., the URL
#      the image's SOURCE notice names, before SHA256SUMS: one copy for every
#      release with that Lightpanda, an object already there left as it is;
#   5. once all of that has succeeded, every publish: the root install.sh
#      (Cache-Control: no-cache). It is channel-agnostic: it reads
#      channels/<SWARM_CHANNEL>, so a candidate's is what `curl .../install.sh |
#      SWARM_CHANNEL=rc sh` runs, before any promotion (audit finding N10);
#   6. for a candidate (YYYY.MM.N-rc.N) only: channels/rc.marketplace.json (the
#      release's marketplace, for a deployment on the rc channel), then LAST
#      channels/rc (one line: V), both Cache-Control: no-cache, each written as
#      exactly that object whatever channels/rc/ prefix exists (A5), and
#      channels/rc read back: it must hold V. install.sh reads channels/rc under
#      SWARM_CHANNEL=rc (DAYONE-RELEASE-2).
#   It never touches channels/stable or the root marketplace.json.
#
# --record-test V --record FILE: the manual install-and-upgrade test of
#   section 7.3, written once to releases/V/TESTED. FILE is JSON: {version: V,
#   previous: <version or null>, tested_at, project, result: "pass", steps:
#   [{name, result: "pass"}, ...]}. A record that is not a pass is refused.
#
# --promote V: refused unless releases/V/TESTED records a pass for V. Then the
#   root marketplace.json (plugin `sc` at V, the archive releases/V/plugin.zip
#   by absolute URL and the digest V's SHA256SUMS holds for it), the root
#   install.sh (V's), and LAST channels/stable (one line: V, read back), each
#   with Cache-Control: no-cache, so nobody reads yesterday's channel for an hour.
#
# --dry-run prints every write and runs none; the reads still run.
#
# Exit codes: 0 done, 1 refused or failed, 2 bad arguments.

set -euo pipefail

# Never wait on a prompt nobody can see. On a project where an API is not yet
# enabled, `gcloud ... describe` asks "enable it and retry? (y/N)" and blocks
# on stdin; the first real run of this script against swarmcloud-prod hung there
# for ten minutes (2026-10-30). With prompts off it fails at once, which reads
# as "not there yet", and the enable below runs.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
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
RELEASE_PROJECTS=("swarmcloud-prod" "swarmcloud-releases")
PROJECT="swarmcloud-prod"
LOCATION="us"
REPOSITORY="swarmcloud"
BUCKET="gs://swarmcloud"
KEYRING="swarmcloud-release"
KEY="cosign"

MODE=""; BUNDLE=""; SOURCE_IMAGES=""; VERSION=""; RECORD=""; DRY_RUN=0; CI_RUN=0

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
    --ci)            CI_RUN=1; shift ;;
    -h|--help)       usage ;;
    *) err "unknown argument: $1"; usage ;;
  esac
done
[[ -n "${MODE}" ]] || usage
[[ "${MODE}" != publish || -n "${SOURCE_IMAGES}" ]] || { err "--bundle needs --source-images"; usage; }
[[ "${MODE}" != record || -n "${RECORD}" ]] || { err "--record-test needs --record"; usage; }

# --ci: the dispatched, approved publish-release run is the confirmation. Only
# for a publish, and only inside GitHub Actions; refused before anything runs.
if [[ "${CI_RUN}" -eq 1 ]]; then
  [[ "${MODE}" == publish ]] \
    || die "refusing --ci with --${MODE/record/record-test}: recording the manual test and promoting stay a person's typed act"
  [[ "${GITHUB_ACTIONS:-}" == "true" ]] \
    || die "refusing --ci outside GitHub Actions (GITHUB_ACTIONS is not true): at a terminal, publish without --ci and type the version"
  [[ -n "${SWARM_PUBLISH_VERSION:-}" ]] \
    || die "refusing --ci: SWARM_PUBLISH_VERSION, the version typed into the publish-release dispatch, is not set"
fi

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

# A channel is objects directly under channels/: channels/<ch>, one line, the
# version (what install.sh and preflight read), and for a channel other than
# stable channels/<ch>.marketplace.json (what `swarm install` adds for it).
# Nothing is ever written under a channels/<ch>/ prefix.
CHANNELS="${BUCKET}/channels"

# put_channel_object FILE NAME: write FILE as exactly the object channels/NAME,
# uncached. `gcloud storage cp FILE gs://.../channels/rc` is not that: when no
# object channels/rc exists but an object under channels/rc/ does, gcloud
# storage takes the destination for a folder and writes channels/rc/<FILE's
# name> (audit 3, A5: 2026.10.1-rc.1's line landed as channels/rc/rc, beside
# channels/rc/marketplace.json). A destination ending in "/" is always a
# folder, and gcloud names the object by the source's own name, so a file
# named NAME copied to channels/ is channels/NAME whatever prefixes exist.
put_channel_object() {
  local source="$1" name="$2" staged="${WORK}/channel-objects/$2"
  mkdir -p "${WORK}/channel-objects"
  cp -- "${source}" "${staged}"
  dim "$([[ "${DRY_RUN}" -eq 1 ]] && printf 'would write' || printf 'writing') ${CHANNELS}/${name}"
  write gcloud storage cp --cache-control=no-cache "${staged}" "${CHANNELS}/"
}

# check_channel NAME VERSION: channels/NAME, read back, holds exactly VERSION.
check_channel() {
  local name="$1" version="$2" held
  [[ "${DRY_RUN}" -eq 0 ]] || return 0
  held="$(gcloud storage cat "${CHANNELS}/${name}" 2>/dev/null)" \
    || die "cannot read ${CHANNELS}/${name} back: an installer on that channel cannot find ${version}"
  [[ "${held}" == "${version}" ]] \
    || die "${CHANNELS}/${name} holds '${held}', not ${version}: an installer on that channel does not install ${version}"
  ok "${CHANNELS}/${name} reads back as ${version}"
}

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
  # The plugin's digest is the one V's published SHA256SUMS holds: the root
  # marketplace is added by URL, so it names plugin.zip absolutely (N9).
  gcloud storage cat "${release}/SHA256SUMS" >"${WORK}/SHA256SUMS" 2>/dev/null \
    || die "cannot read ${release}/SHA256SUMS"
  local archive_sha256
  archive_sha256="$(awk '$2 == "plugin.zip" { print $1 }' "${WORK}/SHA256SUMS")"
  [[ "${archive_sha256}" =~ ^[0-9a-f]{64}$ ]] \
    || die "release ${VERSION} has no plugin.zip in its SHA256SUMS: a marketplace added by URL cannot install its plugin; assemble and publish the next version"
  require_cmd python3
  python3 "${HERE}/marketplace.py" marketplace --version "${VERSION}" \
    --sha256 "${archive_sha256}" --out "${WORK}/marketplace.json"
  printf '%s\n' "${VERSION}" >"${WORK}/stable"
  [[ "${DRY_RUN}" -eq 1 ]] || confirm "Promote ${VERSION}: move ${CHANNEL}, ${BUCKET}/marketplace.json and ${BUCKET}/install.sh to it." "${VERSION}"
  write gcloud storage cp --cache-control=no-cache "${WORK}/marketplace.json" "${BUCKET}/marketplace.json"
  write gcloud storage cp --cache-control=no-cache "${release}/install.sh" "${BUCKET}/install.sh"
  # LAST: an installer that reads the new channel finds everything it names.
  put_channel_object "${WORK}/stable" stable
  check_channel stable "${VERSION}"
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

# The bundle's marketplace is exactly what marketplace.py writes for V and the
# bundle's plugin.zip: an archive source by absolute URL and digest, which a
# marketplace added by URL resolves (a relative path does not; N9).
check_marketplace() {
  [[ -f "${BUNDLE}/plugin.zip" ]] \
    || die "the bundle has no plugin.zip: a marketplace added by URL cannot install its plugin; assemble it with this checkout's scripts/release/assemble.sh"
  require_cmd python3
  python3 "${HERE}/marketplace.py" marketplace --version "${VERSION}" \
    --sha256 "$(sha256_of "${BUNDLE}/plugin.zip")" --out "${WORK}/marketplace.json" \
    || die "cannot write the marketplace of ${VERSION}"
  cmp -s "${WORK}/marketplace.json" "${BUNDLE}/marketplace.json" \
    || die "the bundle's marketplace.json does not name ${VERSION}'s plugin.zip by its release URL and digest; assemble it with this checkout's scripts/release/assemble.sh"
}

# The key every install pins is the release_key.pub inside this bundle's own
# engine wheel (steps/bootstrap.py RELEASE_KEY), so that is the key the new
# signature must verify with (audit 3, A11): a KMS key that is not the pinned
# one would fail every install, and a published release is never overwritten.
extract_release_key() {
  local wheels=("${BUNDLE}"/swarm_install-*.whl)
  [[ "${#wheels[@]}" -eq 1 && -f "${wheels[0]}" ]] \
    || die "the bundle has no single swarm_install wheel to read release_key.pub from; assemble it with scripts/release/assemble.sh"
  python3 - "${wheels[0]}" "${WORK}/release_key.pub" <<'PY' \
    || die "the bundle's $(basename -- "${wheels[0]}") holds no swarm_install/release_key.pub: no install of it could verify a signature"
import sys
import zipfile

try:
    with zipfile.ZipFile(sys.argv[1]) as wheel:
        key = wheel.read("swarm_install/release_key.pub")
except (KeyError, zipfile.BadZipFile):
    sys.exit(1)
if b"BEGIN PUBLIC KEY" not in key:
    sys.exit(1)
with open(sys.argv[2], "wb") as out:
    out.write(key)
PY
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
  # The dispatch's version must be the bundle's, exactly: a run dispatched for
  # one version never publishes another.
  if [[ "${CI_RUN}" -eq 1 && "${SWARM_PUBLISH_VERSION}" != "${VERSION}" ]]; then
    die "refusing --ci: SWARM_PUBLISH_VERSION is '${SWARM_PUBLISH_VERSION}', the bundle is ${VERSION}; nothing was written"
  fi
  check_marketplace
  check_cves
  check_images
  extract_release_key
  require_cmd gcrane cosign
  local release; release="$(RELEASE "${VERSION}")"
  if object_exists "${release}/"; then
    die "release ${VERSION} is already published (${release}/); a release is never overwritten -- assemble the next version"
  fi
  step "Publishing SwarmCloud ${VERSION} to ${release}/ and ${REGISTRY}"
  if [[ "${CI_RUN}" -eq 1 ]]; then
    ok "confirmed by the dispatch: SWARM_PUBLISH_VERSION is ${VERSION}, approved in the release environment"
  elif [[ "${DRY_RUN}" -eq 0 ]]; then
    confirm "Publish ${VERSION}: copy and sign every image into ${REGISTRY}, upload the bundle to ${release}/." "${VERSION}"
  fi

  # SHA256SUMS is signed first and the signature checked with the wheel's
  # pinned key, the way an install checks it, before any image is copied or
  # any object written. The public key travels with the release too (design
  # 6.1); the engine trusts the copy pinned in its wheel, so a tampered bucket
  # cannot swap both.
  cosign public-key "--key=${KMS_KEY}" >"${WORK}/cosign.pub" </dev/null
  write cosign sign-blob --yes "--key=${KMS_KEY}" --output-signature "${WORK}/SHA256SUMS.sig" \
    --bundle "${WORK}/SHA256SUMS.cosign.bundle" "${BUNDLE}/SHA256SUMS" </dev/null
  local verify=(cosign verify-blob --key "${WORK}/release_key.pub" --signature "${WORK}/SHA256SUMS.sig"
    --insecure-ignore-tlog=true "${BUNDLE}/SHA256SUMS")
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    dim "would run: ${verify[*]}"
  elif ! "${verify[@]}" </dev/null >"${WORK}/verify.log" 2>&1; then
    redact <"${WORK}/verify.log" >&2
    die "the new SHA256SUMS signature does not verify with the release_key.pub in the bundle's swarm_install wheel: ${KMS_KEY} is not the key installs pin; nothing was written"
  else
    ok "SHA256SUMS's signature verifies with the release key the wheel pins"
  fi

  # cosign pushes each image's signature through the registry's own API with
  # docker's credentials, not the Google keychain gcrane copies with: on
  # 2026-10-31 the second release candidate's publish copied agent-runtime-base
  # into the release registry, then had its signature refused as an
  # "Unauthenticated request". A docker config of this run's own names gcloud's
  # credential helper for the registry's host, so cosign signs as the same
  # account gcloud acts as, no token passes through this script, and the
  # runner's ~/.docker/config.json is left alone.
  export DOCKER_CONFIG="${WORK}/docker"
  mkdir -p "${DOCKER_CONFIG}"
  printf '{"credHelpers": {"%s": "gcloud"}}\n' "${REGISTRY%%/*}" >"${DOCKER_CONFIG}/config.json"
  [[ "${DRY_RUN}" -eq 1 ]] || require_cmd docker-credential-gcloud

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

  write gcloud storage rsync --recursive --no-clobber --exclude='^(SHA256SUMS$|third-party/)' "${BUNDLE}" "${release}"
  write gcloud storage cp --no-clobber "${WORK}/cosign.pub" "${release}/cosign.pub"
  write gcloud storage cp --no-clobber "${WORK}/SHA256SUMS.sig" "${release}/SHA256SUMS.sig"
  if [[ "${DRY_RUN}" -eq 1 || -f "${WORK}/SHA256SUMS.cosign.bundle" ]]; then
    write gcloud storage cp --no-clobber "${WORK}/SHA256SUMS.cosign.bundle" "${release}/SHA256SUMS.cosign.bundle"
  fi
  # The Corresponding Source of the AGPL software the images carry
  # (assemble.sh; docs/design/lightpanda-runtime.md 6.4) at the bucket root,
  # where the image's SOURCE notice points, so it answers before the release
  # does. Its path names the upstream version, so a later release with the
  # same Lightpanda finds it there and writes no second copy; --no-clobber
  # keeps a concurrent publish from replacing it.
  local rel
  while IFS= read -r rel <&4; do
    if object_exists "${BUCKET}/${rel}"; then
      ok "${BUCKET}/${rel} is already published, by an earlier release; left as it is"
      continue
    fi
    write gcloud storage cp --no-clobber "${BUNDLE}/${rel}" "${BUCKET}/${rel}"
  done 4< <(if [[ -d "${BUNDLE}/third-party" ]]; then cd -- "${BUNDLE}" && find third-party -type f | LC_ALL=C sort; fi)
  # LAST: a release cut short above has no SHA256SUMS, so no installer reads it.
  write gcloud storage cp --no-clobber "${BUNDLE}/SHA256SUMS" "${release}/SHA256SUMS"
  # Every publish puts install.sh at the root: it reads the channel it is asked
  # for, so it is the same for stable and rc, and an rc's install (which its
  # promotion needs, as TESTED) must not wait for a promotion (N10).
  write gcloud storage cp --cache-control=no-cache "${BUNDLE}/install.sh" "${BUCKET}/install.sh"
  # A candidate moves the rc channel, after the release it names is complete.
  # A final release moves no channel here: stable moves only by --promote.
  if [[ "${VERSION}" == *-rc.* ]]; then
    printf '%s\n' "${VERSION}" >"${WORK}/rc"
    # The rc channel's marketplace, for a deployment that follows rc (N10).
    put_channel_object "${BUNDLE}/marketplace.json" rc.marketplace.json
    put_channel_object "${WORK}/rc" rc
    check_channel rc "${VERSION}"
    [[ "${DRY_RUN}" -eq 1 ]] || ok "${CHANNELS}/rc names ${VERSION}: SWARM_CHANNEL=rc installs it"
  fi
  ok "published ${VERSION}$([[ "${DRY_RUN}" -eq 0 ]] || printf ' (dry run: nothing written)')"
  info "next: the manual test (plan section 7.3), then --record-test ${VERSION} --record <file>, then --promote ${VERSION}"
}

case "${MODE}" in
  publish) publish ;;
  record)  record_test ;;
  promote) promote ;;
esac
