#!/usr/bin/env bash
# image-sizes: every released digest of a worker image, with its compressed
# size, its layers and the change from the release before it, read from this
# environment's Artifact Registry. READ-ONLY.
#
#   scripts/image-sizes.sh [--image NAME] [--since YYYY-MM-DD] [--layers]
#   scripts/image-sizes.sh [--image NAME] --diff OLD NEW
#
#   --image NAME   the image (default agent-runtime-base); one path segment of
#                  ${IMAGE_REPO}, e.g. agent-runtime-indexer
#   --since DATE   only releases created on or after DATE (UTC, as the
#                  registry's createTime compares)
#   --layers       under each release, every layer: its size, its digest and
#                  the Dockerfile instruction that made it (the image config's
#                  history), so a jump reads as the instruction behind it
#   --diff OLD NEW the bisect step: the layers NEW has that OLD has not (+),
#                  the ones it dropped (-), and the net change. Each of OLD
#                  and NEW is a tag (a commit sha, `dev`) or a sha256 digest
#
# WHY IT EXISTS (#625). Claude-code container start on Cloud Run Jobs went from
# a 71 s p50 on 2026-09-24 to ~115 s on 09-30..10-02 and 166-168 s on
# 10-04/10-05, while GKE pods start in 17 s. The suspect was agent-runtime-base
# growing, and nothing listed what each release weighed, so the suspicion
# could not be checked against a release. The default output is that list, one
# row per release, oldest first; the row whose delta_mb jumps is the release a
# growth arrived in, and `--diff` on it and the row before names the layers.
#
# WHAT IT MEASURES. compressed_mb is the sum of the linux/amd64 manifest's
# layer sizes: the gzip bytes a node pulls, which is what an image's pull time
# follows. A multi-platform index is resolved to its linux/amd64 manifest --
# the one Cloud Run and GKE run -- never summed across platforms. registry_mb
# is Artifact Registry's own imageSizeBytes for the same digest, printed beside
# it as a cross-check. Two releases share a layer when they share its DIGEST;
# a layer rebuilt with identical content but a new digest shows as dropped and
# re-added, which `--diff` prints as such rather than hiding.
#
# READ-ONLY. One `gcloud artifacts docker images list`, one access token, and
# GETs on the Docker Registry v2 API (a manifest, and an image config for
# --layers and --diff). Nothing is written, tagged or deleted. It needs
# roles/artifactregistry.reader on the repository and nothing more; the
# SwarmCloud worker account does not hold it (measured 2026-10-05:
# PERMISSION_DENIED on artifactregistry.versions.list), so this runs from an
# operator's shell or a CI job that already reads the registry.
#
# The token reaches curl on stdin (`-K -`, common.sh's auth_config), never on
# its argv, and nothing here prints it. Error bodies pass through redact.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

IMAGE="agent-runtime-base"
SINCE=""
LAYERS=0
DIFF_OLD=""
DIFF_NEW=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)  [[ $# -ge 2 ]] || die "--image needs a name"; IMAGE="$2"; shift 2 ;;
    --since)  [[ $# -ge 2 ]] || die "--since needs a date"; SINCE="$2"; shift 2 ;;
    --layers) LAYERS=1; shift ;;
    --diff)   [[ $# -ge 3 ]] || die "--diff needs OLD and NEW (a tag or a sha256 digest each)"
              DIFF_OLD="$2"; DIFF_NEW="$3"; shift 3 ;;
    -h|--help) sed -n '2,46p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

# One path segment of this environment's repository, so nothing here can be
# pointed at another repository or project through the name.
[[ "${IMAGE}" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || die "--image must be one image name such as agent-runtime-base, not ${IMAGE}"
if [[ -n "${SINCE}" && ! "${SINCE}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  die "--since must be YYYY-MM-DD, not ${SINCE}"
fi

require_cmd gcloud curl jq awk

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-image-sizes.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM

REGISTRY_URL="https://${IMAGE_HOST}/v2/${PROJECT_ID}/${ARTIFACT_REGISTRY}/${IMAGE}"
MANIFEST_ACCEPT="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"

mb() { awk -v b="$1" 'BEGIN { printf "%.1f", b / 1000000 }'; }
signed_mb() { awk -v b="$1" 'BEGIN { printf "%+.1f", b / 1000000 }'; }

# --- the listing -----------------------------------------------------------
#
# Normalised to one entry per digest, oldest first. `version` is the digest
# (its last path segment, in case a gcloud release spells the full resource
# name, as `tags list` does -- see lib/image-refs.sh); `tags` arrives as a list
# or as a ", "-joined string depending on the gcloud release, and both read
# the same here.
if ! gcloud artifacts docker images list "${IMAGE_REPO}/${IMAGE}" --include-tags --format=json \
    >"${WORK}/listing.raw" 2>"${WORK}/listing.err"; then
  redact <"${WORK}/listing.err" | head -n 5 >&2
  die "could not list ${IMAGE_REPO}/${IMAGE}; reading it needs roles/artifactregistry.reader on ${ARTIFACT_REGISTRY}"
fi
jq --arg since "${SINCE}" '
  [ .[]
    | { digest: (.version | split("/") | last),
        created: (.createTime // ""),
        tags: ((.tags // []) | if type == "string" then (split(",") | map(gsub("^\\s+|\\s+$"; ""))) else . end
               | map(split("/") | last) | map(select(. != ""))),
        registry_bytes: ((.metadata.imageSizeBytes // "") | tostring) } ]
  | group_by(.digest)
  | map(.[0] + { tags: (map(.tags) | add | unique) })
  | map(select($since == "" or .created >= $since))
  | sort_by(.created, .digest)' "${WORK}/listing.raw" >"${WORK}/releases.json" \
  || die "the registry listing for ${IMAGE} is not the JSON array gcloud documents"

RELEASES="$(jq 'length' "${WORK}/releases.json")"
[[ "${RELEASES}" -gt 0 ]] || die "no released digest of ${IMAGE} in ${IMAGE_REPO}${SINCE:+ since ${SINCE}}"

# One token for the whole run, minted in THIS shell: access_token caches it in
# _ACCESS_TOKEN, which a command substitution would throw away and re-mint.
access_token >/dev/null

# registry_get PATH ACCEPT OUT -- GET ${REGISTRY_URL}/PATH into OUT, or die
# naming PATH and the status. -L for blobs: Artifact Registry answers a blob
# with a redirect to storage, and curl does not carry a -K header to another
# host.
registry_get() {
  local path="$1" accept="$2" out="$3" code
  code="$(auth_config "${_ACCESS_TOKEN}" \
    | curl -sS -L -m 60 -K - -H "Accept: ${accept}" -o "${out}" -w '%{http_code}' \
        "${REGISTRY_URL}/${path}" 2>"${WORK}/curl.err")" || code="000"
  if [[ "${code}" != "200" ]]; then
    { redact <"${WORK}/curl.err"; head -c 300 "${out}" 2>/dev/null | redact; echo; } | head -n 5 >&2
    die "the registry answered ${code} for ${IMAGE}/${path}"
  fi
}

# manifest_of DIGEST OUT -- the linux/amd64 image manifest of DIGEST.
manifest_of() {
  local digest="$1" out="$2" platform
  registry_get "manifests/${digest}" "${MANIFEST_ACCEPT}" "${out}"
  if jq -e 'has("manifests")' "${out}" >/dev/null; then
    platform="$(jq -r '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest][0] // empty' "${out}")"
    [[ -n "${platform}" ]] || die "${IMAGE}@${digest} is an index with no linux/amd64 manifest"
    registry_get "manifests/${platform}" "${MANIFEST_ACCEPT}" "${out}"
  fi
  jq -e '(.layers | type) == "array"' "${out}" >/dev/null \
    || die "${IMAGE}@${digest} has no layer list; it is not an image manifest"
}

# layers_of DIGEST OUT -- one JSON array of {digest, size, created_by}: the
# manifest's layers paired, in order, with the config history entries that are
# not empty_layer. A history that does not pair one-to-one is reported as such
# rather than mislabelled.
layers_of() {
  local digest="$1" out="$2" config
  manifest_of "${digest}" "${WORK}/manifest.json"
  config="$(jq -r '.config.digest' "${WORK}/manifest.json")"
  registry_get "blobs/${config}" "application/vnd.oci.image.config.v1+json, application/vnd.docker.container.image.v1+json" "${WORK}/config.json"
  jq --slurpfile config "${WORK}/config.json" '
    .layers as $layers
    | [ ($config[0].history // [])[] | select(.empty_layer != true) | (.created_by // "") ] as $made
    | [ range(0; $layers | length) as $i
        | { digest: $layers[$i].digest, size: $layers[$i].size,
            created_by: (if ($made | length) == ($layers | length)
                         then ($made[$i] | gsub("[\\t\\n\\r]+"; " ") | .[0:160])
                         else "(the image history does not pair with its layers)" end) } ]' \
    "${WORK}/manifest.json" >"${out}"
}

print_layers() {
  jq -r '.[] | "\(.size)\t\(.digest[0:19])\t\(.created_by)"' "$1" \
    | while IFS=$'\t' read -r size digest made; do
        printf '    %8s  %s  %s\n' "$(mb "${size}")" "${digest}" "${made}"
      done
}

# resolve REF -- the digest a tag or digest names among the listed releases.
resolve() {
  local ref="$1" digest
  digest="$(jq -r --arg ref "${ref}" '[.[] | select(.digest == $ref or (.tags | index($ref)))][0].digest // empty' \
    "${WORK}/releases.json")"
  [[ -n "${digest}" ]] || die "${ref} is neither a tag nor a digest of a listed ${IMAGE} release"
  printf '%s' "${digest}"
}

# --- --diff: the bisect step -------------------------------------------------
if [[ -n "${DIFF_OLD}" ]]; then
  old="$(resolve "${DIFF_OLD}")"
  new="$(resolve "${DIFF_NEW}")"
  layers_of "${old}" "${WORK}/old.json"
  layers_of "${new}" "${WORK}/new.json"
  printf 'diff %s %s (%s) to %s (%s)\n' "${IMAGE}" "${DIFF_OLD}" "${old}" "${DIFF_NEW}" "${new}"
  jq -r --slurpfile old "${WORK}/old.json" '
      ($old[0] | map(.digest)) as $had
      | .[] | select(.digest as $d | $had | index($d) | not)
      | "+\t\(.size)\t\(.digest[0:19])\t\(.created_by)"' "${WORK}/new.json" >"${WORK}/delta.tsv"
  jq -r --slurpfile new "${WORK}/new.json" '
      ($new[0] | map(.digest)) as $has
      | .[] | select(.digest as $d | $has | index($d) | not)
      | "-\t\(.size)\t\(.digest[0:19])\t\(.created_by)"' "${WORK}/old.json" >>"${WORK}/delta.tsv"
  while IFS=$'\t' read -r sign size digest made; do
    printf '%s %8s MB  %s  %s\n' "${sign}" "$(mb "${size}")" "${digest}" "${made}"
  done <"${WORK}/delta.tsv"
  old_total="$(jq '[.[].size] | add // 0' "${WORK}/old.json")"
  new_total="$(jq '[.[].size] | add // 0' "${WORK}/new.json")"
  printf 'net  %s MB (%s MB -> %s MB)\n' "$(signed_mb "$((new_total - old_total))")" \
    "$(mb "${old_total}")" "$(mb "${new_total}")"
  info "$(wc -l <"${WORK}/delta.tsv" | tr -d ' ') layers differ between ${DIFF_OLD} and ${DIFF_NEW}"
  exit 0
fi

# --- the list ------------------------------------------------------------------
printf 'created\tdigest\ttags\tcompressed_mb\tlayers\tdelta_mb\tregistry_mb\n'
previous=""
visited=0
jq -r '.[] | [.created, .digest, (.tags | join(",")), .registry_bytes] | @tsv' "${WORK}/releases.json" \
  >"${WORK}/releases.tsv"
while IFS=$'\t' read -r created digest tags registry_bytes; do
  layers_file="${WORK}/layers-${visited}.json"
  if [[ "${LAYERS}" -eq 1 ]]; then
    layers_of "${digest}" "${layers_file}"
  else
    manifest_of "${digest}" "${WORK}/manifest.json"
    jq '[.layers[] | {digest, size}]' "${WORK}/manifest.json" >"${layers_file}"
  fi
  total="$(jq '[.[].size] | add // 0' "${layers_file}")"
  count="$(jq 'length' "${layers_file}")"
  delta="$(signed_mb "$((total - ${previous:-${total}}))")"
  registry_mb="-"
  [[ "${registry_bytes}" =~ ^[0-9]+$ ]] && registry_mb="$(mb "${registry_bytes}")"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${created}" "${digest}" "${tags:--}" "$(mb "${total}")" \
    "${count}" "${delta}" "${registry_mb}"
  [[ "${LAYERS}" -eq 1 ]] && print_layers "${layers_file}"
  previous="${total}"
  visited=$((visited + 1))
done <"${WORK}/releases.tsv"

# The count, so a run that visited one release cannot read as a sweep of all.
[[ "${visited}" -eq "${RELEASES}" ]] || die "listed ${RELEASES} releases of ${IMAGE} but measured ${visited}"
info "${visited} releases of ${IMAGE} measured from ${IMAGE_REPO}"
