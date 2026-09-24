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
# ALL OR NOTHING, and exactly where that stops being true.
#
#   * Every image is resolved and scanned before any channel tag moves, and
#     one refusal moves none.
#   * Artifact Registry has no multi-tag transaction, so the moves themselves
#     are "all, or undo": a move that fails part-way puts back the tags this
#     run already moved.
#   * The deploy manifest is written only after every tag has moved, so IT
#     never describes a mix.
#   * The CHANNEL can. If the undo itself fails -- the usual cause is the same
#     expired session that failed the move -- :channel holds some new digests
#     and some old ones, and the run's last line says MIXED, names them, and
#     the lines above it give the command that puts each one back. If the job
#     is killed between two moves (a cancelled run, a lost runner), nothing
#     runs to undo or to say so. Either way a `skip_build` redeploy, which
#     rebuilds its manifest from :channel, would deploy the mix: re-run the
#     promotion first.
#
# Nothing downstream ever deploys a mutable tag. `:dev` exists for humans;
# scripts and Terraform use `image@sha256:...`.
#
# WHICH DIGESTS. By default each image's digest is whatever `:<tag>` points at
# when this runs. With --manifest it is the digest the BUILD RECORDED in that
# build manifest (build/images-<env>.json), and the tag is not consulted at
# all. The release uses --manifest, because it promotes the build CI made for
# its commit (docs/ci.md): a tag can be moved by a later build of the same
# commit, and what is scanned and promoted must be what that build produced.
# Every recorded digest is confirmed to be in Artifact Registry before anything
# moves, and a manifest built for another environment is refused outright --
# swarm-ui bakes its environment into the bundle when it is compiled.
#
# --scan-only DOES EVERYTHING ABOVE EXCEPT MOVE. It resolves and confirms
# every digest and scans every one, refuses exactly what a promotion would
# refuse, and then stops: no channel tag moves and no deploy manifest is
# written. release.yml runs it BEFORE the approval (owner decision,
# 2026-09-24, docs/ci.md): scanning is read-only, so a prod reviewer approves
# a set that has already passed, and moving :prod waits for the approval.
# The promotion after the approval scans again -- the approval may come hours
# later, against a newer vulnerability database.
#
# Usage: scripts/push-images.sh [TARGET...] [--tag SHA] [--channel dev] [--scan|--no-scan|--scan-only]
#        scripts/push-images.sh --manifest build/images-dev.json --channel dev --scan
#        scripts/push-images.sh --manifest build/images-prod.json --channel prod --scan-only

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
SCAN_ONLY=0
NO_SCAN_FLAG=0
SEVERITY="${TRIVY_SEVERITY:-HIGH,CRITICAL}"
FROM_MANIFEST=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)       TAG="$2"; shift 2 ;;
    --channel)   CHANNEL="$2"; shift 2 ;;
    --scan)      SCAN=1; shift ;;
    --no-scan)   SCAN=0; NO_SCAN_FLAG=1; shift ;;
    --scan-only) SCAN_ONLY=1; shift ;;
    --manifest)  FROM_MANIFEST="$2"; shift 2 ;;
    -h|--help)   sed -n '2,55p' "$0"; exit 0 ;;
    -*)          die "unknown flag: $1" ;;
    *)           TARGETS+=("$1"); shift ;;
  esac
done

# A scan-only run that does not scan checks nothing and would still exit 0 --
# the pre-approval step would pass a set nobody looked at. Refused whichever
# order the flags came in. SCAN_IMAGES=0 in the environment does not count:
# the flag asked for a scan, explicitly.
if [[ "${SCAN_ONLY}" -eq 1 ]]; then
  if [[ "${NO_SCAN_FLAG}" -eq 1 ]]; then
    die "--scan-only with --no-scan would check nothing and move nothing; pass one of them"
  fi
  SCAN=1
fi

require_cmd gcloud jq

if [[ -n "${FROM_MANIFEST}" ]]; then
  # -------------------------------------------------------------------------
  # Promote what a build RECORDED. Everything checkable without the registry is
  # checked here, before a single call is made.
  # -------------------------------------------------------------------------
  [[ -f "${FROM_MANIFEST}" ]] || die "no build manifest at ${FROM_MANIFEST}"
  jq -e 'type == "object" and ((.images // null) | type) == "array"' "${FROM_MANIFEST}" >/dev/null 2>&1 \
    || die "${FROM_MANIFEST} is not a build manifest (it has no images array)"
  BUILT_FOR="$(jq -r '.environment // ""' "${FROM_MANIFEST}")"
  if [[ "${BUILT_FOR}" != "${CHANNEL}" ]]; then
    die "${FROM_MANIFEST} was built for '${BUILT_FOR:-no environment}' and this promotes to :${CHANNEL}. swarm-ui bakes its environment into the bundle when it is compiled, so those images would tell every ${CHANNEL} user they are on ${BUILT_FOR:-an unknown environment}; build for ${CHANNEL} instead"
  fi
  BUILT_TAG="$(jq -r '.tag // ""' "${FROM_MANIFEST}")"
  [[ -n "${BUILT_TAG}" ]] || die "${FROM_MANIFEST} records no tag"
  if [[ -n "${TAG}" && "${TAG}" != "${BUILT_TAG}" ]]; then
    die "${FROM_MANIFEST} records build ${BUILT_TAG}, not ${TAG}"
  fi
  TAG="${BUILT_TAG}"
  if [[ "${#TARGETS[@]}" -eq 0 ]]; then
    while IFS= read -r line; do
      [[ -n "${line}" ]] && TARGETS+=("${line}")
    done < <(jq -r '.images[] | .name // empty' "${FROM_MANIFEST}")
  fi
  [[ "${#TARGETS[@]}" -gt 0 ]] || die "${FROM_MANIFEST} lists no images"
  # Artifact Registry's own coordinates for IMAGE_REPO, parsed from it rather
  # than rebuilt from REGION and PROJECT_ID: the version confirmed must be in
  # the repository whose tags are about to move.
  if [[ "${IMAGE_REPO}" =~ ^([a-z0-9-]+)-docker\.pkg\.dev/([^/]+)/([^/]+)$ ]]; then
    REG_LOCATION="${BASH_REMATCH[1]}"
    REG_PROJECT="${BASH_REMATCH[2]}"
    REG_REPOSITORY="${BASH_REMATCH[3]}"
  else
    die "IMAGE_REPO '${IMAGE_REPO}' is not <location>-docker.pkg.dev/<project>/<repository>, so a recorded digest cannot be confirmed in it"
  fi
else
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
fi

step "Promotion plan"
info "tag      ${TAG}"
info "digests  $([[ -n "${FROM_MANIFEST}" ]] && echo "as recorded in ${FROM_MANIFEST}" || echo "what :${TAG} points at now")"
info "channel  ${CHANNEL}"
info "repo     ${IMAGE_REPO}"
info "scan     $([[ "${SCAN}" -eq 1 ]] && echo "trivy, fail on ${SEVERITY}" || echo "disabled")"
info "images   ${#TARGETS[@]}: ${TARGETS[*]}"
if [[ "${SCAN_ONLY}" -eq 1 ]]; then
  info "mode     scan only: every check a promotion makes, and no channel tag moves"
fi

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

# The digest FROM_MANIFEST records for image $1, in DIGEST, once Artifact
# Registry has confirmed it holds exactly that version. Refuses the image and
# returns non-zero otherwise.
#
# `versions describe`, not `docker images describe`: the latter reads Container
# Analysis and needs containeranalysis.occurrences.list (see digest_for). This
# is Artifact Registry alone. Measured against the live registry on
# 2026-09-24 (gcloud 483.0.0): a present version exits 0 with its resource
# name; an absent one exits 1 with "NOT_FOUND: Requested entity was not found".
recorded_digest() {
  local target="$1" image="${IMAGE_REPO}/$1" rows recorded reason
  DIGEST=""
  rows="$(jq --arg n "${target}" '[.images[] | select(.name == $n)] | length' "${FROM_MANIFEST}")"
  if [[ "${rows}" -ne 1 ]]; then
    err "${target}: the build manifest lists it ${rows} times, not once"
    refuse "${target}" "listed ${rows} times in the build manifest"
    return 1
  fi
  recorded="$(jq -r --arg n "${target}" '.images[] | select(.name == $n) | .image // ""' "${FROM_MANIFEST}")"
  DIGEST="$(jq -r --arg n "${target}" '.images[] | select(.name == $n) | .digest // ""' "${FROM_MANIFEST}")"
  if [[ "${recorded}" != "${image}" ]]; then
    err "${target}: the build manifest records ${recorded:-no image}, not ${image}"
    refuse "${target}" "recorded in ${recorded:-no repository}, not ${IMAGE_REPO}"
    return 1
  fi
  if [[ ! "${DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    err "${target}: the build manifest records '${DIGEST}', which is not a sha256 digest"
    refuse "${target}" "recorded digest is not a sha256 digest"
    return 1
  fi
  if gcloud artifacts versions describe "${DIGEST}" --package="${target}" \
       --repository="${REG_REPOSITORY}" --location="${REG_LOCATION}" --project="${REG_PROJECT}" \
       --format='value(name)' >"${LOOKUP_OUT}" 2>"${LOOKUP_ERR}"; then
    return 0
  fi
  reason="$(lookup_error)"
  die_if_auth_failure "${reason}"
  if grep -q 'NOT_FOUND' "${LOOKUP_ERR}"; then
    err "${image}@${DIGEST}: the build recorded it, and Artifact Registry does not have it"
    refuse "${target}" "recorded digest is not in Artifact Registry"
  else
    # Unreadable is not absent: a denial here and a deleted image need opposite
    # responses, and only one of them is fixed by building again.
    err "${image}@${DIGEST}: could not confirm it is in Artifact Registry"
    [[ -z "${reason}" ]] || printf '%s\n' "${reason}" | redact | sed -n '1,3s/^/     /p' >&2
    refuse "${target}" "could not confirm the recorded digest (registry unreadable)"
  fi
  return 1
}

for target in "${TARGETS[@]}"; do
  step "Resolve ${target}"
  image="${IMAGE_REPO}/${target}"

  if [[ -n "${FROM_MANIFEST}" ]]; then
    recorded_digest "${target}" || continue
  elif ! digest_for "${image}" "${TAG}"; then
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
  elif [[ -z "${DIGEST}" ]]; then
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

# --scan-only stops here, after every check and before the first move. Nothing
# below this line runs, so no tag moves, nothing needs putting back, and no
# deploy manifest is written for the apply to mistake for a promotion.
if [[ "${SCAN_ONLY}" -eq 1 ]]; then
  hr
  ok "checked ${#R_NAME[@]} image(s): every one would be promoted to :${CHANNEL}; nothing moved (--scan-only)"
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. Move every channel tag. If one fails, put back the ones already moved.
# ---------------------------------------------------------------------------
# Artifact Registry has no multi-tag transaction, so "all or nothing" here is
# "all, or undo". Undoing a tag that did not exist before this run means
# DELETING it: that is removing what this run created seconds earlier, not
# destroying anything that was there, so it needs no typed confirmation.
MOVED=()
# Names of the images the undo could NOT put back, and whether any failure in
# this phase was a dead session. Globals, because undo_moved is called
# directly, never in a command substitution, and its caller reads both.
UNDO_STUCK=()
AUTH_FAILED=0

# Did the gcloud call whose stderr is in LOOKUP_ERR fail on authentication?
# Remembered rather than acted on: die_if_auth_failure EXITS, and exiting here
# would skip the put-back of tags this run already moved -- and the printing
# of the commands that put them back by hand if it cannot.
note_auth_failure() {
  if gcloud_auth_failure "$(lookup_error)"; then AUTH_FAILED=1; fi
}

# Put every tag this run moved back where it was. Returns non-zero when any
# could not be, with their names in UNDO_STUCK and, for exactly those, the
# command that puts each back by hand.
undo_moved() {
  local i image prev stuck_rows=()
  UNDO_STUCK=()
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
    note_auth_failure
    lookup_error | redact | sed -n '1,2s/^/     /p' >&2
    UNDO_STUCK+=("${R_NAME[$i]}")
    stuck_rows+=("${i}")
  done
  [[ "${#stuck_rows[@]}" -gt 0 ]] || return 0

  err "COULD NOT UNDO ${UNDO_STUCK[*]}: :${CHANNEL} now describes a MIXED release. Put each back by hand:"
  for i in "${stuck_rows[@]}"; do
    if [[ -n "${R_PREV[$i]}" ]]; then
      printf '     gcloud artifacts docker tags add %s@%s %s:%s --project %s\n' \
        "${R_IMAGE[$i]}" "${R_PREV[$i]}" "${R_IMAGE[$i]}" "${CHANNEL}" "${PROJECT_ID}" >&2
    else
      printf '     gcloud artifacts docker tags delete %s:%s --project %s\n' \
        "${R_IMAGE[$i]}" "${CHANNEL}" "${PROJECT_ID}" >&2
    fi
  done
  return 1
}

step "Promote ${#R_NAME[@]} image(s) to :${CHANNEL}"
for ((i = 0; i < ${#R_NAME[@]}; i++)); do
  image="${R_IMAGE[$i]}"
  # Tagging by digest, not by tag: if someone rebuilt :TAG between the resolve
  # above and now, this still points the channel at the digest that was vetted.
  if ! gcloud artifacts docker tags add "${image}@${R_DIGEST[$i]}" "${image}:${CHANNEL}" \
       --project "${PROJECT_ID}" >/dev/null 2>"${LOOKUP_ERR}"; then
    reason="$(lookup_error)"
    note_auth_failure
    err "${R_NAME[$i]}: could not move ${image}:${CHANNEL}"
    [[ -z "${reason}" ]] || printf '%s\n' "${reason}" | redact | sed -n '1,3s/^/     /p' >&2

    # THE LAST LINE MUST MATCH THE REGISTRY. It used to be "nothing promoted"
    # unconditionally -- including when the put-back had just failed and the
    # channel held some images from each release.
    undone=1
    if [[ "${#MOVED[@]}" -gt 0 ]]; then
      warn "putting back the ${#MOVED[@]} channel tag(s) this run already moved"
      undo_moved || undone=0
    fi
    cause=""
    if [[ "${AUTH_FAILED}" -eq 1 ]]; then
      err "the gcloud session is not usable -- this is authentication, not a registry fault"
      dim "locally: gcloud auth login && gcloud auth application-default login; in CI: re-run the job"
      cause=" (authentication: the gcloud session is not usable)"
    fi
    if [[ "${undone}" -eq 0 ]]; then
      die "promotion of ${TAG} to :${CHANNEL} failed at ${R_NAME[$i]}${cause}, and COULD NOT UNDO ${UNDO_STUCK[*]}: :${CHANNEL} is MIXED -- put those back with the commands above before anything reads :${CHANNEL}"
    fi
    die "promotion of ${TAG} to :${CHANNEL} failed at ${R_NAME[$i]}${cause}; nothing promoted"
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
