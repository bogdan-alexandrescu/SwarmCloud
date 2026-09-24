#!/usr/bin/env bash
# Promote already-built images to the environment channel tag, by DIGEST.
#
# Cloud Build has already pushed the SHA-tagged image by the time this runs, so
# there is nothing to upload. What this script does instead is the part that
# actually matters for a deploy you can reason about:
#
#   * resolve every image to an immutable sha256 digest;
#   * optionally scan it before it is allowed near a channel tag;
#   * attach the channel tag (dev/prod) to that exact digest;
#   * write build/deployed-images-<env>.json, which deploy.sh consumes.
#
# Nothing downstream ever deploys a mutable tag. `:dev` exists for humans;
# scripts and Terraform use `image@sha256:...`.
#
# Usage: scripts/push-images.sh [TARGET...] [--tag SHA] [--channel dev] [--scan|--no-scan]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# The same eight build-images.sh builds. This list had six -- no swarm-ui, no
# swarm-verify -- and it is the fallback whenever build/images-<env>.json is
# absent. That used to cost only an unpromoted image; now terraform deploys
# every image by digest from the manifest this writes and refuses to plan one
# with no digest, so a promotion that silently skips two images is a deploy
# that fails naming them. Better to promote them.
ALL_TARGETS=(agent-runtime-base agent-runtime-browser swarm-api swarm-scheduler swarm-quota-broker swarm-reconciler swarm-ui swarm-verify)
TARGETS=()
TAG=""
CHANNEL="${ENVIRONMENT}"
SCAN="${SCAN_IMAGES:-1}"
SEVERITY="${TRIVY_SEVERITY:-HIGH,CRITICAL}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)     TAG="$2"; shift 2 ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    --scan)    SCAN=1; shift ;;
    --no-scan) SCAN=0; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    -*)        die "unknown flag: $1" ;;
    *)         TARGETS+=("$1"); shift ;;
  esac
done

require_cmd gcloud jq

MANIFEST="${BUILD_DIR}/images-${ENVIRONMENT}.json"
if [[ -z "${TAG}" && -f "${MANIFEST}" ]]; then
  TAG="$(jq -r '.tag // ""' "${MANIFEST}")"
fi
TAG="${TAG:-$(git_sha)}"

if [[ "${#TARGETS[@]}" -eq 0 ]]; then
  if [[ -f "${MANIFEST}" ]]; then
    while IFS= read -r line; do
      [[ -n "${line}" ]] && TARGETS+=("${line}")
    done < <(jq -r '.images[].name' "${MANIFEST}")
  fi
  [[ "${#TARGETS[@]}" -gt 0 ]] || TARGETS=("${ALL_TARGETS[@]}")
fi

step "Promotion plan"
info "tag      ${TAG}"
info "channel  ${CHANNEL}"
info "repo     ${IMAGE_REPO}"
info "scan     $([[ "${SCAN}" -eq 1 ]] && echo "trivy, fail on ${SEVERITY}" || echo "disabled")"

TRIVY_BIN="$(trivy_bin || true)"
if [[ "${SCAN}" -eq 1 && -z "${TRIVY_BIN}" ]]; then
  die "trivy not found but scanning is on; install it or pass --no-scan"
fi

entries='[]'
PROMOTED=0
FAILED=()

for target in "${TARGETS[@]}"; do
  step "Promote ${target}"
  image="${IMAGE_REPO}/${target}"

  # `images describe` would be the obvious call and is the wrong one: it reads
  # Container Analysis, so it needs containeranalysis.occurrences.list, and
  # without that permission it fails with a denial that this script then
  # reported as "not found -- build it first". The image was there. Someone
  # following that advice rebuilds it and gets the same message forever.
  #
  # `images list` resolves the digest straight from Artifact Registry, which is
  # the only thing actually being asked.
  describe_err=""
  digest="$(gcloud artifacts docker images list "${image}" \
    --project "${PROJECT_ID}" --include-tags --filter="tags:${TAG}" \
    --format='value(version)' 2>"${TMPDIR:-/tmp}/push-digest.$$" || true)"
  describe_err="$(tr -d '\r' <"${TMPDIR:-/tmp}/push-digest.$$" | grep -v '^Listing items' || true)"
  rm -f "${TMPDIR:-/tmp}/push-digest.$$"
  digest="$(printf '%s' "${digest}" | head -n1)"
  if [[ -z "${digest}" ]]; then
    # An expired session reaches here too, and "build it first" is the worst
    # possible advice for it -- the rebuild fails the same way, at a different
    # message.
    die_if_auth_failure "${describe_err}"
    if [[ -n "${describe_err}" ]]; then
      # Say what actually went wrong. A permission problem and a missing image
      # need opposite responses, and only one of them is fixed by rebuilding.
      err "${image}:${TAG}: could not resolve a digest"
      printf '%s\n' "${describe_err}" | head -n 3 | sed 's/^/     /'
    else
      err "${image}:${TAG} not found in Artifact Registry -- build it first"
    fi
    FAILED+=("${target}")
    continue
  fi
  ok "digest ${digest}"

  if [[ "${SCAN}" -eq 1 ]]; then
    info "scanning ${target} (${SEVERITY})"
    # --ignorefile carries the accepted-with-an-expiry list. Entries there are
    # dated, so one that outlives its date fails this scan again rather than
    # quietly becoming permanent.
    IGNORE_ARGS=()
    if [[ -f "${REPO_ROOT}/.trivyignore.yaml" ]]; then
      IGNORE_ARGS=(--ignorefile "${REPO_ROOT}/.trivyignore.yaml")
    fi
    if ! "${TRIVY_BIN}" image --quiet --scanners vuln \
         --severity "${SEVERITY}" --exit-code 1 --ignore-unfixed \
         "${IGNORE_ARGS[@]}" \
         "${image}@${digest}" 2>&1 | redact; then
      err "${target}: trivy found unfixed-excluded ${SEVERITY} vulnerabilities; refusing to promote"
      FAILED+=("${target}")
      continue
    fi
    ok "scan clean"
  fi

  # Tagging by digest, not by tag: if someone rebuilt :TAG between the describe
  # above and now, this still points the channel at the digest we vetted.
  gcloud artifacts docker tags add "${image}@${digest}" "${image}:${CHANNEL}" \
    --project "${PROJECT_ID}" >/dev/null
  ok "${image}:${CHANNEL} -> ${digest}"

  entries="$(jq -c --arg n "${target}" --arg i "${image}" --arg t "${TAG}" \
                  --arg c "${CHANNEL}" --arg d "${digest}" \
    '. + [{name:$n, image:$i, tag:$t, channel:$c, digest:$d, ref:($i + "@" + $d)}]' <<<"${entries}")"
  PROMOTED=$((PROMOTED + 1))
done

DEPLOY_MANIFEST="${BUILD_DIR}/deployed-images-${ENVIRONMENT}.json"
jq -n --arg tag "${TAG}" --arg channel "${CHANNEL}" --arg at "$(iso_now)" \
      --arg env "${ENVIRONMENT}" --argjson images "${entries}" \
   '{tag:$tag, channel:$channel, promoted_at:$at, environment:$env, images:$images}' \
   >"${DEPLOY_MANIFEST}"

hr
if [[ "${#FAILED[@]}" -gt 0 ]]; then
  die "promoted ${PROMOTED}, failed: ${FAILED[*]-}"
fi
ok "promoted ${PROMOTED} image(s) to :${CHANNEL}"
info "deploy manifest: ${DEPLOY_MANIFEST}"
