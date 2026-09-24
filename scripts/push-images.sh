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
# ALL OR NOTHING. Every image is resolved and scanned before any channel tag
# moves; one refusal moves none, and a tag that fails to move part-way puts
# back the ones already moved. The channel and the manifest describe one
# release or the previous one -- never a mix.
#
# Nothing downstream ever deploys a mutable tag. `:dev` exists for humans;
# scripts and Terraform use `image@sha256:...`.
#
# Usage: scripts/push-images.sh [TARGET...] [--tag SHA] [--channel dev] [--scan|--no-scan]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ALL_TARGETS=(agent-runtime-base agent-runtime-browser swarm-api swarm-scheduler swarm-quota-broker swarm-reconciler)
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
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
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
info "images   ${#TARGETS[@]}: ${TARGETS[*]}"

TRIVY_BIN="$(trivy_bin || true)"
if [[ "${SCAN}" -eq 1 && -z "${TRIVY_BIN}" ]]; then
  die "trivy not found but scanning is on; install it or pass --no-scan"
fi

LOOKUP_OUT="$(mktemp "${TMPDIR:-/tmp}/push-lookup.XXXXXX")"
LOOKUP_ERR="$(mktemp "${TMPDIR:-/tmp}/push-lookup-err.XXXXXX")"
trap 'rm -f "${LOOKUP_OUT}" "${LOOKUP_ERR}"' EXIT

# The digest a tag points at, in DIGEST; empty when no version carries the tag.
# Returns non-zero when the registry could not be READ, with gcloud's reason in
# LOOKUP_ERR -- an unreadable registry is not an empty one.
#
# Sets a global rather than printing, because a command substitution runs in a
# subshell and anything it assigns is lost to the caller.
#
# `images list`, not `images describe`: describe reads Container Analysis, so it
# needs containeranalysis.occurrences.list, and without that permission it
# failed with a denial this script used to report as "not found -- build it
# first". The image was there. Someone following that advice rebuilds it and
# gets the same message forever.
#
# The tag is matched EXACTLY, in jq. gcloud's `tags:X` filter is a word match,
# so on its own `tags:dev` also finds `dev-old`, and a 12-character SHA tag also
# finds `pr-<run>-<sha>` -- two digests for one question, where the old code
# silently took whichever came first.
DIGEST=""
digest_for() {
  local image="$1" tag="$2"
  DIGEST=""
  if ! gcloud artifacts docker images list "${image}" \
       --project "${PROJECT_ID}" --include-tags --filter="tags:${tag}" \
       --format=json >"${LOOKUP_OUT}" 2>"${LOOKUP_ERR}"; then
    return 1
  fi
  DIGEST="$(jq -r --arg t "${tag}" '
      [ .[]
        | select((.tags // []) | (if type == "string" then split(",") else . end) | any(.[]; . == $t))
        | .version ]
      | .[0] // ""' "${LOOKUP_OUT}")" || return 1
}

# gcloud's reason, without its "Listing items under ..." chatter.
lookup_error() {
  tr -d '\r' <"${LOOKUP_ERR}" | grep -v '^Listing items' || true
}

# ---------------------------------------------------------------------------
# 1. Resolve every image: the digest to promote, and the digest the channel
#    holds now (so that a promotion that fails part-way can be put back).
# ---------------------------------------------------------------------------
R_NAME=(); R_IMAGE=(); R_DIGEST=(); R_PREV=()
REFUSED=(); REFUSED_WHY=()

refuse() {
  REFUSED+=("$1"); REFUSED_WHY+=("$2")
}

for target in "${TARGETS[@]}"; do
  step "Resolve ${target}"
  image="${IMAGE_REPO}/${target}"

  if ! digest_for "${image}" "${TAG}"; then
    reason="$(lookup_error)"
    # An expired session reaches here too, and "build it first" is the worst
    # possible advice for it -- the rebuild fails the same way, at a different
    # message.
    die_if_auth_failure "${reason}"
    # Say what actually went wrong. A permission problem and a missing image
    # need opposite responses, and only one of them is fixed by rebuilding.
    err "${image}:${TAG}: could not resolve a digest"
    [[ -z "${reason}" ]] || printf '%s\n' "${reason}" | redact | sed -n '1,3s/^/     /p' >&2
    refuse "${target}" "could not resolve :${TAG} (registry unreadable)"
    continue
  fi
  if [[ -z "${DIGEST}" ]]; then
    err "${image}:${TAG} not found in Artifact Registry -- build it first"
    refuse "${target}" ":${TAG} not found in Artifact Registry"
    continue
  fi
  digest="${DIGEST}"
  ok "digest ${digest}"

  # WHAT :CHANNEL POINTS AT NOW. Read before anything moves, because it is the
  # only way to undo a promotion that fails part-way through. Failing to read it
  # refuses the image: a promotion that cannot be undone is not all-or-nothing.
  if ! digest_for "${image}" "${CHANNEL}"; then
    reason="$(lookup_error)"
    die_if_auth_failure "${reason}"
    err "${image}:${CHANNEL}: could not read what the channel points at now, so a failed promotion could not be undone"
    [[ -z "${reason}" ]] || printf '%s\n' "${reason}" | redact | sed -n '1,3s/^/     /p' >&2
    refuse "${target}" "could not read the current :${CHANNEL}"
    continue
  fi
  if [[ -n "${DIGEST}" ]]; then
    dim "  :${CHANNEL} is currently ${DIGEST}"
  else
    dim "  :${CHANNEL} does not exist yet for ${target}"
  fi

  R_NAME+=("${target}"); R_IMAGE+=("${image}"); R_DIGEST+=("${digest}"); R_PREV+=("${DIGEST}")
done

# ---------------------------------------------------------------------------
# 2. Scan EVERY resolved image, all of them, before anything is promoted.
#    Not stopping at the first refusal is deliberate: every image that needs a
#    fix is named in one run, instead of one per release.
# ---------------------------------------------------------------------------
if [[ "${SCAN}" -eq 1 ]]; then
  # --ignorefile carries the accepted-with-an-expiry list. Entries there are
  # dated, so one that outlives its date fails this scan again rather than
  # quietly becoming permanent.
  IGNORE_ARGS=()
  if [[ -f "${REPO_ROOT}/.trivyignore.yaml" ]]; then
    IGNORE_ARGS=(--ignorefile "${REPO_ROOT}/.trivyignore.yaml")
  fi
  for ((i = 0; i < ${#R_NAME[@]}; i++)); do
    target="${R_NAME[$i]}"
    step "Scan ${target} (${SEVERITY})"
    if ! "${TRIVY_BIN}" image --quiet --scanners vuln \
         --severity "${SEVERITY}" --exit-code 1 --ignore-unfixed \
         ${IGNORE_ARGS[@]+"${IGNORE_ARGS[@]}"} \
         "${R_IMAGE[$i]}@${R_DIGEST[$i]}" 2>&1 | redact; then
      err "${target}: trivy found unfixed-excluded ${SEVERITY} vulnerabilities; refusing to promote"
      refuse "${target}" "trivy: fixable ${SEVERITY} findings"
      continue
    fi
    ok "scan clean"
  done
else
  warn "scanning disabled (--no-scan): these digests reach :${CHANNEL} unvetted"
fi

# ---------------------------------------------------------------------------
# The gate. Release run 35969538707 scanned and tagged one image at a time, so
# seven images were on :dev at the new build when trivy refused swarm-ui, and
# :dev described a release nobody built. Now one refusal promotes nothing.
# ---------------------------------------------------------------------------
if [[ "${#REFUSED[@]}" -gt 0 ]]; then
  hr
  err "promoted nothing: :${CHANNEL} still points at the previous release for every image"
  for ((i = 0; i < ${#REFUSED[@]}; i++)); do
    printf '     %-22s %s\n' "${REFUSED[$i]}" "${REFUSED_WHY[$i]}" >&2
  done
  die "refusing to promote a partial release: ${#REFUSED[@]} of ${#TARGETS[@]} image(s) refused (${REFUSED[*]})"
fi

# ---------------------------------------------------------------------------
# 3. Move every channel tag. If one fails, put back the ones already moved.
# ---------------------------------------------------------------------------
# Artifact Registry has no multi-tag transaction, so "all or nothing" here is
# "all, or undo". Undoing a tag that did not exist before this run means
# DELETING it: that is removing what this run created seconds earlier, not
# destroying anything that was there, so it needs no typed confirmation.
MOVED=()

undo_moved() {
  local i image prev stuck=()
  for i in ${MOVED[@]+"${MOVED[@]}"}; do
    image="${R_IMAGE[$i]}"
    prev="${R_PREV[$i]}"
    if [[ -n "${prev}" ]]; then
      if gcloud artifacts docker tags add "${image}@${prev}" "${image}:${CHANNEL}" \
           --project "${PROJECT_ID}" >/dev/null 2>"${LOOKUP_ERR}"; then
        ok "put back ${image}:${CHANNEL} -> ${prev}"
        continue
      fi
    elif gcloud artifacts docker tags delete "${image}:${CHANNEL}" \
           --project "${PROJECT_ID}" --quiet >/dev/null 2>"${LOOKUP_ERR}"; then
      ok "removed ${image}:${CHANNEL} again (it did not exist before this run)"
      continue
    fi
    lookup_error | redact | sed -n '1,2s/^/     /p' >&2
    stuck+=("${R_NAME[$i]}")
  done
  if [[ "${#stuck[@]}" -gt 0 ]]; then
    err "COULD NOT UNDO ${stuck[*]}: :${CHANNEL} now describes a MIXED release. Put each back by hand:"
    for i in ${MOVED[@]+"${MOVED[@]}"}; do
      if [[ -n "${R_PREV[$i]}" ]]; then
        printf '     gcloud artifacts docker tags add %s@%s %s:%s --project %s\n' \
          "${R_IMAGE[$i]}" "${R_PREV[$i]}" "${R_IMAGE[$i]}" "${CHANNEL}" "${PROJECT_ID}" >&2
      else
        printf '     gcloud artifacts docker tags delete %s:%s --project %s\n' \
          "${R_IMAGE[$i]}" "${CHANNEL}" "${PROJECT_ID}" >&2
      fi
    done
  fi
}

step "Promote ${#R_NAME[@]} image(s) to :${CHANNEL}"
for ((i = 0; i < ${#R_NAME[@]}; i++)); do
  image="${R_IMAGE[$i]}"
  # Tagging by digest, not by tag: if someone rebuilt :TAG between the resolve
  # above and now, this still points the channel at the digest that was vetted.
  if ! gcloud artifacts docker tags add "${image}@${R_DIGEST[$i]}" "${image}:${CHANNEL}" \
       --project "${PROJECT_ID}" >/dev/null 2>"${LOOKUP_ERR}"; then
    reason="$(lookup_error)"
    err "${R_NAME[$i]}: could not move ${image}:${CHANNEL}"
    [[ -z "${reason}" ]] || printf '%s\n' "${reason}" | redact | sed -n '1,3s/^/     /p' >&2
    if [[ "${#MOVED[@]}" -gt 0 ]]; then
      warn "putting back the ${#MOVED[@]} channel tag(s) this run already moved"
      undo_moved
    fi
    die "promotion of ${TAG} to :${CHANNEL} failed at ${R_NAME[$i]}; nothing promoted"
  fi
  MOVED+=("${i}")
  ok "${image}:${CHANNEL} -> ${R_DIGEST[$i]}"
done

# ---------------------------------------------------------------------------
# 4. The manifest, written only now that every tag has moved, and renamed into
#    place so a reader never sees half of one.
# ---------------------------------------------------------------------------
entries='[]'
for ((i = 0; i < ${#R_NAME[@]}; i++)); do
  entries="$(jq -c --arg n "${R_NAME[$i]}" --arg i "${R_IMAGE[$i]}" --arg t "${TAG}" \
                  --arg c "${CHANNEL}" --arg d "${R_DIGEST[$i]}" \
    '. + [{name:$n, image:$i, tag:$t, channel:$c, digest:$d, ref:($i + "@" + $d)}]' <<<"${entries}")"
done

DEPLOY_MANIFEST="${BUILD_DIR}/deployed-images-${ENVIRONMENT}.json"
jq -n --arg tag "${TAG}" --arg channel "${CHANNEL}" --arg at "$(iso_now)" \
      --arg env "${ENVIRONMENT}" --argjson images "${entries}" \
   '{tag:$tag, channel:$channel, promoted_at:$at, environment:$env, images:$images}' \
   >"${DEPLOY_MANIFEST}.tmp"
mv -f "${DEPLOY_MANIFEST}.tmp" "${DEPLOY_MANIFEST}"

hr
ok "promoted ${#R_NAME[@]} image(s) to :${CHANNEL}"
info "deploy manifest: ${DEPLOY_MANIFEST}"
