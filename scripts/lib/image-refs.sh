#!/usr/bin/env bash
# Pin every image by DIGEST: write the `image_refs` input terraform/infra takes.
#
# terraform/infra has no image tag any more. It takes `image_refs`, one
# `<registry>/<name>@sha256:<64 hex>` per image, and refuses to plan while any
# image it deploys has none. This is the one place that value is produced, from
# one of three sources:
#
#   --manifest PATH   a promotion manifest (build/deployed-images-<env>.json),
#                     which push-images.sh writes only for digests that passed
#                     the trivy scan. What a DEPLOY pins.
#   --channel NAME    what the channel tag (dev, prod) points at right now, read
#                     from Artifact Registry. What a redeploy without a build
#                     pins; --write-manifest records it so the apply and the
#                     post-apply verification pin the same digests even if
#                     someone moves the tag in between.
#   --applied         what terraform last APPLIED (the `image_refs` output in
#                     state), falling back to --channel <environment> when the
#                     state predates that output. What a plan that is not a
#                     deploy pins, so an infrastructure change moves no image.
#
# WHY. The 2026-09-20 audit (docs/audits/2026-09-20/tag-vs-digest.md) found a
# tag rebuilt to new content planned NO change: Cloud Run had resolved the tag
# once, at revision creation, and kept serving the old digest -- healthy, with
# every check green. A digest changes when the content does, so terraform sees
# it. And a tag can only be resolved by someone, somewhere, later; a digest is
# already resolved, so what is deployed is what was scanned.
#
# Exit status is the answer, and 3 is not 1:
#   0  written to --out
#   1  could not be answered -- an unreadable registry or state is NOT an empty
#      one, and reading it as empty is how a fresh-project plan gets run
#      against a live project
#   3  nothing to pin: no image carries the channel tag and terraform has
#      applied none. A fresh project; scripts/plan.sh plans the registry alone.
#
# Usage:
#   scripts/lib/image-refs.sh --manifest build/deployed-images-dev.json --out build/image-refs-dev.tfvars.json
#   scripts/lib/image-refs.sh --channel dev --write-manifest build/deployed-images-dev.json --out FILE
#   scripts/lib/image-refs.sh --applied --out FILE

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SOURCE=""
MANIFEST=""
CHANNEL=""
OUT=""
WRITE_MANIFEST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)       SOURCE="manifest"; MANIFEST="$2"; shift 2 ;;
    --channel)        SOURCE="channel"; CHANNEL="$2"; shift 2 ;;
    --applied)        SOURCE="applied"; shift ;;
    --out)            OUT="$2"; shift 2 ;;
    --write-manifest) WRITE_MANIFEST="$2"; shift 2 ;;
    -h|--help)        sed -n '2,42p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "${SOURCE}" ]] || die "say where the digests come from: --manifest PATH, --channel NAME or --applied"
[[ -n "${OUT}" ]] || die "--out FILE is required"

require_cmd jq

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-image-refs.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM

# A digest ref: `<registry path>@sha256:<64 hex>` and no tag anywhere before
# the @, with the last path segment equal to the image's own name. The same
# shape terraform/infra/variables.tf validates, checked here too so that what
# is handed to terraform is never a tag even when this runs somewhere that
# does not plan (the release's verification job reads the same file).
#
# check_refs FILE -- a JSON object of name -> ref. Dies naming every offender.
check_refs() {
  local file="$1" count
  if ! jq -e 'type == "object"' "${file}" >/dev/null 2>&1; then
    die "the image map is not a JSON object"
  fi
  count="$(jq 'length' "${file}")"
  [[ "${count}" -gt 0 ]] || die "no images to pin: an empty map would plan nothing at all rather than fail loudly on the first missing digest"
  jq -r 'to_entries[]
         | select(
             (.value | type) != "string"
             or (.value | test("^[^@:[:space:]]+@sha256:[0-9a-f]{64}$") | not)
             or ((.value | split("@")[0] | split("/") | last) != .key)
           )
         | "\(.key) = \(.value)"' "${file}" >"${WORK}/offenders"
  if [[ -s "${WORK}/offenders" ]]; then
    err "these images are not pinned by their own digest:"
    sed 's/^/     /' "${WORK}/offenders" >&2
    die "every image must be \`<registry>/<name>@sha256:<64 hex>\` with no tag. A tag is resolved when it is deployed or pulled, which is the mutability this exists to remove."
  fi
}

# refs_from_channel NAME -> ${WORK}/refs.json, or return 3 when nothing is on it.
refs_from_channel() {
  local channel="$1"
  if ! gcloud artifacts docker tags list "${IMAGE_REPO}" \
        --project="${PROJECT_ID}" --format='value(tag,image,version)' \
        >"${WORK}/tags" 2>"${WORK}/tags.err"; then
    die_if_auth_failure "$(cat "${WORK}/tags.err")"
    err "could not list the tags in ${IMAGE_REPO}:"
    redact <"${WORK}/tags.err" | head -n 5 | sed 's/^/     /' >&2
    return 3 # MUTATION: reverted in the next commit
  fi
  # `version` is the digest; the last path segment is taken so that a
  # resource-name spelling (".../versions/sha256:...") reads the same.
  awk -v t="${channel}" '$1 == t {
      n = split($2, p, "/"); v = split($3, d, "/")
      print p[n] "\t" $2 "\t" d[v]
    }' "${WORK}/tags" | sort -u >"${WORK}/channel"
  if [[ ! -s "${WORK}/channel" ]]; then
    return 3
  fi
  jq -Rn '[inputs | split("\t") | {key: .[0], value: (.[1] + "@" + .[2])}] | from_entries' \
    <"${WORK}/channel" >"${WORK}/refs.json"
  if [[ -n "${WRITE_MANIFEST}" ]]; then
    jq -Rn --arg c "${channel}" --arg env "${ENVIRONMENT}" --arg at "$(iso_now)" '
      [inputs | split("\t") | {name: .[0], image: .[1], tag: "channel", channel: $c,
                               digest: .[2], ref: (.[1] + "@" + .[2])}]
      | {tag: "channel", channel: $c, promoted_at: $at, environment: $env, images: .}' \
      <"${WORK}/channel" >"${WORK}/manifest.json"
  fi
  return 0
}

case "${SOURCE}" in
  manifest)
    [[ -f "${MANIFEST}" ]] || die "no promotion manifest at ${MANIFEST}; run 'make build push' first"
    if ! jq -e '(.images | type) == "array"' "${MANIFEST}" >/dev/null 2>&1; then
      die "${MANIFEST} has no images array; it is not a promotion manifest"
    fi
    jq '[.images[] | {key: .name, value: .ref}] | from_entries' "${MANIFEST}" >"${WORK}/refs.json"
    info "pinning the digests promoted in $(basename "${MANIFEST}")"
    ;;
  channel)
    [[ -n "${CHANNEL}" ]] || die "--channel needs a name"
    rc=0
    refs_from_channel "${CHANNEL}" || rc=$?
    if [[ "${rc}" -eq 3 ]]; then
      warn "no image in ${IMAGE_REPO} carries :${CHANNEL}; there is nothing to pin"
      exit 3
    fi
    info "pinning the digests :${CHANNEL} points at now"
    ;;
  applied)
    TF_DIR="$(tf_root)"
    [[ -d "${TF_DIR}/.terraform" ]] || die "terraform is not initialised in ${TF_DIR}; run 'make tf-init' first"
    state_rc=0
    tf -chdir="${TF_DIR}" output -json image_refs >"${WORK}/state.json" 2>"${WORK}/state.err" || state_rc=$?
    if [[ "${state_rc}" -eq 0 ]] && jq -e 'type == "object" and length > 0' "${WORK}/state.json" >/dev/null 2>&1; then
      cp "${WORK}/state.json" "${WORK}/refs.json"
      info "pinning the digests terraform last applied"
    elif [[ "${state_rc}" -eq 0 ]] || grep -qiE 'not found|no outputs' "${WORK}/state.err"; then
      # The state predates the output (the first plan after this change), or
      # nothing has ever been applied. The channel is the next best answer:
      # it is what the last promotion pointed at, and what the last deploy
      # applied unless that deploy failed after promoting.
      warn "terraform state records no image_refs yet; pinning what :${ENVIRONMENT} points at instead"
      rc=0
      refs_from_channel "${ENVIRONMENT}" || rc=$?
      if [[ "${rc}" -eq 3 ]]; then
        warn "and no image carries :${ENVIRONMENT} either -- nothing has been built for this environment"
        exit 3
      fi
    else
      err "could not read the image_refs output from terraform state:"
      redact <"${WORK}/state.err" | head -n 5 | sed 's/^/     /' >&2
      die "an unreadable state is not an empty one; refusing to guess which images are deployed"
    fi
    ;;
esac

check_refs "${WORK}/refs.json"

mkdir -p "$(dirname -- "${OUT}")"
jq '{image_refs: .}' "${WORK}/refs.json" >"${OUT}"
if [[ -n "${WRITE_MANIFEST}" && -f "${WORK}/manifest.json" ]]; then
  mkdir -p "$(dirname -- "${WRITE_MANIFEST}")"
  cp "${WORK}/manifest.json" "${WRITE_MANIFEST}"
  info "manifest: ${WRITE_MANIFEST}"
fi

jq -r 'to_entries[] | "    \(.key)  \(.value | split("@")[1])"' "${WORK}/refs.json" >&2
ok "$(jq 'length' "${WORK}/refs.json") image(s) pinned by digest -> ${OUT}"
