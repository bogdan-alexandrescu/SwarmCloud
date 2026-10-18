#!/usr/bin/env bash
# Fill the build mirrors from upstream, or check that they are full.
#
# Image builds read ghcr.io, Docker Hub and GitHub release downloads through
# this project's own mirrors (terraform/infra/build_mirror.tf,
# scripts/lib/build-mirror.sh, docs/runbooks/build-mirror.md), because on
# 2026-10-12 three release builds failed on those fetches inside Cloud Build.
# This is the operator's half of that:
#
#   tools   every file images/agent-runtime-base/tool-mirror.sha256 lists is
#           downloaded from its upstream URL (https://<object>), checked
#           against the sha256 listed there -- the same one the Dockerfile
#           checks -- and copied to gs://${BUILD_MIRROR_BUCKET}/<object>. A
#           file that fails its sha256 is NEVER uploaded. An object already
#           in the bucket is left alone unless --force.
#   images  every reference a Dockerfile pulls through a mirror repository
#           (FROM and `# syntax=`, at its pinned digest) is looked up there.
#           A remote repository fetches a digest from upstream on the first
#           pull; one not yet cached is pulled through it here, through the
#           Docker Registry API (the manifest, its linux/amd64 image and
#           every blob), so the next build is served from the cache even if
#           upstream is down that day.
#
# Run it after `make apply` creates the mirrors, and after every pin bump in a
# Dockerfile or tool-mirror.sha256. A build whose mirror object is missing
# fails, naming the object; it never falls back to upstream.
#
# Usage: scripts/refresh-build-mirror.sh [--check] [--force]
#                                        [--tools-only | --images-only]
#
#   --check        change nothing: name every missing object and every image
#                  not cached, and exit 1 if there is one.
#   --force        upload every tool file again, even if present.
#
# Needs gcloud signed in as someone who can write objects in the mirror bucket
# and read the mirror repositories (the deployer, or an operator), and curl.
# Writes only gs://${BUILD_MIRROR_BUCKET}; nothing is deleted. The access
# token reaches curl on stdin (`-K -`), never on its argv, and nothing here
# prints it.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/build-mirror.sh
source "${REPO_ROOT}/scripts/lib/build-mirror.sh"

CHECK=0
FORCE=0
DO_TOOLS=1
DO_IMAGES=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)       CHECK=1; shift ;;
    --force)       FORCE=1; shift ;;
    --tools-only)  DO_IMAGES=0; shift ;;
    --images-only) DO_TOOLS=0; shift ;;
    -h|--help)     sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ "${DO_TOOLS}${DO_IMAGES}" != "00" ]] || die "--tools-only and --images-only together leave nothing to do"
[[ "${CHECK}" -eq 0 || "${FORCE}" -eq 0 ]] || die "--check changes nothing, so it takes no --force"

require_cmd gcloud curl
[[ "${BUILD_MIRROR_BUCKET}" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || die "BUILD_MIRROR_BUCKET is not a bucket name: ${BUILD_MIRROR_BUCKET}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-build-mirror.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM

PROBLEMS=()

# --- tools -----------------------------------------------------------------

refresh_tools() {
  local rows sha obj url n=0 present=0 uploaded=0
  rows="$(tool_mirror_rows)"
  step "Tool mirror: gs://${BUILD_MIRROR_BUCKET}"
  while read -r sha obj; do
    n=$((n + 1))
    url="gs://${BUILD_MIRROR_BUCKET}/${obj}"
    if [[ "${FORCE}" -eq 0 ]] && gcloud storage objects describe "${url}" >/dev/null 2>"${WORK}/describe.err"; then
      present=$((present + 1))
      dim "present  ${url}"
      continue
    fi
    if [[ "${CHECK}" -eq 1 ]]; then
      err "missing  ${url}"
      PROBLEMS+=("${url} is missing")
      continue
    fi
    if ! tool_mirror_fetch_upstream "${sha}" "${obj}" "${WORK}/tools/${obj}"; then
      PROBLEMS+=("${obj} was not uploaded: its upstream download failed or did not match its sha256")
      continue
    fi
    if ! gcloud storage cp "${WORK}/tools/${obj}" "${url}" >/dev/null 2>"${WORK}/cp.err"; then
      redact <"${WORK}/cp.err" | head -n 3 >&2
      err "could not upload ${url}"
      PROBLEMS+=("${url} could not be uploaded")
      continue
    fi
    rm -f "${WORK}/tools/${obj}"
    uploaded=$((uploaded + 1))
    ok "uploaded ${url} (sha256 ${sha})"
  done <<<"${rows}"
  info "${n} tool file(s): ${present} already present, ${uploaded} uploaded"
}

# --- images ----------------------------------------------------------------

# The reference gcloud describes: NAME@DIGEST when it carries one (gcloud
# takes a tag or a digest, not both), else NAME:TAG.
describable() {
  local ref="$1" name
  if [[ "${ref}" == *@* ]]; then
    name="${ref%@*}"
    [[ "${name##*/}" == *:* ]] && name="${name%:*}"
    printf '%s@%s' "${name}" "${ref##*@}"
  else
    printf '%s' "${ref}"
  fi
}

# registry_get URL ACCEPT OUT -- GET URL into OUT with the access token; dies
# naming URL unless 200. -L for blobs, which Artifact Registry redirects to
# storage (and curl does not carry a -K header to another host).
registry_get() {
  local url="$1" accept="$2" out="$3" code
  code="$(auth_config "${_ACCESS_TOKEN}" \
    | curl -sS -L -m 600 -K - -H "Accept: ${accept}" -o "${out}" -w '%{http_code}' "${url}" \
        2>"${WORK}/curl.err")" || code="000"
  if [[ "${code}" != "200" ]]; then
    { redact <"${WORK}/curl.err"; head -c 300 "${out}" 2>/dev/null | redact; echo; } | head -n 5 >&2
    err "the mirror answered ${code} for ${url}"
    return 1
  fi
}

MANIFEST_ACCEPT="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"

# warm REF -- pull REF through its remote repository: the manifest it names,
# the linux/amd64 image of an index, and that image's config and layers.
warm() {
  local ref="$1" name reference base digest platform
  name="${ref%@*}"
  if [[ "${ref}" == *@* ]]; then
    reference="${ref##*@}"
    [[ "${name##*/}" == *:* ]] && name="${name%:*}"
  else
    reference="${name##*:}"
    name="${name%:*}"
  fi
  # <host>/<project>/<repository>/<image> -> https://<host>/v2/<project>/<repository>/<image>
  base="https://${name%%/*}/v2/${name#*/}"
  registry_get "${base}/manifests/${reference}" "${MANIFEST_ACCEPT}" "${WORK}/manifest.json" || return 1
  if jq -e 'has("manifests")' "${WORK}/manifest.json" >/dev/null; then
    platform="$(jq -r '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest][0] // empty' "${WORK}/manifest.json")"
    [[ -n "${platform}" ]] || { err "${ref} is an index with no linux/amd64 image"; return 1; }
    registry_get "${base}/manifests/${platform}" "${MANIFEST_ACCEPT}" "${WORK}/manifest.json" || return 1
  fi
  while IFS= read -r digest; do
    [[ -n "${digest}" ]] || continue
    registry_get "${base}/blobs/${digest}" "*/*" "${WORK}/blob" || return 1
    rm -f "${WORK}/blob"
  done < <(jq -r '(.config.digest // empty), (.layers[]?.digest // empty)' "${WORK}/manifest.json")
}

refresh_images() {
  local refs ref n=0 cached=0 warmed=0
  refs="$(mirror_image_refs)" || die "could not read the mirror references from the Dockerfiles"
  [[ -n "${refs}" ]] || die "no Dockerfile pulls through a mirror; the references could not be read"
  step "Image mirrors: the remote repositories in terraform/infra/build_mirror.tf"
  while IFS= read -r ref; do
    [[ -n "${ref}" ]] || continue
    n=$((n + 1))
    if gcloud artifacts docker images describe "$(describable "${ref}")" >/dev/null 2>"${WORK}/describe.err"; then
      cached=$((cached + 1))
      dim "cached   ${ref}"
      continue
    fi
    if [[ "${CHECK}" -eq 1 ]]; then
      err "not cached ${ref}"
      PROBLEMS+=("${ref} is not cached")
      continue
    fi
    [[ -n "${_ACCESS_TOKEN}" ]] || access_token >/dev/null
    if warm "${ref}"; then
      warmed=$((warmed + 1))
      ok "pulled through the mirror: ${ref}"
    else
      PROBLEMS+=("${ref} could not be pulled through its mirror")
    fi
  done <<<"${refs}"
  info "${n} image reference(s): ${cached} already cached, ${warmed} pulled through"
}

if [[ "${DO_TOOLS}" -eq 1 ]]; then refresh_tools; fi
if [[ "${DO_IMAGES}" -eq 1 ]]; then
  [[ "${CHECK}" -eq 1 ]] || require_cmd jq
  refresh_images
fi

hr
if [[ "${#PROBLEMS[@]}" -gt 0 ]]; then
  for p in "${PROBLEMS[@]}"; do err "${p}"; done
  die "${#PROBLEMS[@]} problem(s); a build that needs any of them fails until they are fixed (docs/runbooks/build-mirror.md)"
fi
ok "the build mirrors hold everything the Dockerfiles pin"
