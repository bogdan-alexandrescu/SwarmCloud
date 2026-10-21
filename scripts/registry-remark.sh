#!/usr/bin/env bash
# Give every digest that carries the OLD promotion mark the NEW one. One-off.
#
# The promotion mark was `<channel>-promoted-<tag>` and is now
# `promoted-<channel>-<tag>` (promotion_mark_prefix, lib/common.sh; #1886).
# Artifact Registry's cleanup policies match tags by PREFIX, and the registry
# must keep `:dev` and `:prod` with no age limit but promoted images for 30
# days; a keep rule on the prefix `dev` also matched every `dev-promoted-*`
# mark, so nothing promoted would ever have aged out.
#
# push-images.sh writes only the new form from now on, but every digest
# promoted before the rename carries only the old one. deploy.sh reads both
# until this has run (promoted_earlier, lib/deploy.sh). For every version in
# `swarm-images` carrying `<channel>-promoted-<tag>`, this adds
# `promoted-<channel>-<tag>` to the SAME digest.
#
# IT ONLY ADDS. It never removes a tag, and it never moves one: `gcloud
# artifacts docker tags add` re-points a tag that already exists, so a new
# mark that already names ANOTHER digest of the same image is reported and
# left exactly where it is, for a person to read. One that already names the
# same digest is done and skipped, so a second run, or a run after a partial
# one, writes only what is missing. The only writer of new marks meanwhile is
# push-images.sh, and it marks the CURRENT release's tag, which no old mark
# names, so it cannot collide with anything this plans.
#
# Only `swarm-images`. ARTIFACT_REGISTRY and IMAGE_REPO (lib/common.sh) are
# overridable, and this refuses before reading anything if either names
# another repository; a listed image outside it is refused the same way.
#
# Dry run by default: it prints how many marks it would add and the first 20
# `<image>:<new mark> -> <digest>` pairs. `--apply` writes them. The operator
# runs it once, after REGISTRY-MARKS merges; after that a later change can
# drop the old form from deploy.sh.
#
# Exit: 0 when every planned mark exists (dry run: when the plan was read);
# 1 when the registry could not be read, a mark could not be added, or a
# conflict was left alone.
#
# Usage: scripts/registry-remark.sh [--apply]
set -euo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

APPLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)   APPLY=1; shift ;;
    -h|--help) sed -n '2,39p' "$0"; exit 0 ;;
    *)         die "unknown argument: $1 (usage: scripts/registry-remark.sh [--apply])" ;;
  esac
done

# The one repository this may touch.
REMARK_REPOSITORY="swarm-images"
if [[ "${ARTIFACT_REGISTRY}" != "${REMARK_REPOSITORY}" ]]; then
  die "ARTIFACT_REGISTRY is '${ARTIFACT_REGISTRY}'; this re-marks ${REMARK_REPOSITORY} and no other repository"
fi
if [[ ! "${IMAGE_REPO}" =~ ^[a-z0-9-]+-docker\.pkg\.dev/[^/]+/${REMARK_REPOSITORY}$ ]]; then
  die "IMAGE_REPO is '${IMAGE_REPO}', not <location>-docker.pkg.dev/<project>/${REMARK_REPOSITORY}; this re-marks ${REMARK_REPOSITORY} and no other repository"
fi
require_cmd gcloud jq

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-remark.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

step "Promotion marks in ${IMAGE_REPO}"
info "mode     $([[ "${APPLY}" -eq 1 ]] && echo apply || echo 'dry run (pass --apply to write)')"

# </dev/null: a gcloud that prompted would otherwise wait on the terminal.
if ! gcloud artifacts docker images list "${IMAGE_REPO}" --project "${PROJECT_ID}" \
     --include-tags --format=json </dev/null >"${WORK}/images.json" 2>"${WORK}/list.err"; then
  die_if_auth_failure "$(cat "${WORK}/list.err")"
  grep -v '^Listing items' "${WORK}/list.err" | redact | head -n 3 | sed 's/^/     /' >&2 || true
  die "could not list ${IMAGE_REPO}; nothing was planned and nothing written"
fi

# Every tag, one row each: package, version, tag. gcloud has spelled `tags`
# both as a list and as one comma-joined string (see push-images.sh,
# digest_for); both are read.
jq -r '
    .[]
    | .package as $p | .version as $v
    | (.tags // []) | (if type == "string" then split(",") else . end)
    | .[] | select(. != "")
    | [$p, $v, .] | @tsv' "${WORK}/images.json" >"${WORK}/tags.tsv"

VERSIONS="$(jq 'length' "${WORK}/images.json")"
OUTSIDE="$(awk -F'\t' -v r="${IMAGE_REPO}/" 'index($1, r) != 1 { n++ } END { print n + 0 }' "${WORK}/tags.tsv")"
if [[ "${OUTSIDE}" -ne 0 ]]; then
  die "${OUTSIDE} listed tag(s) are on images outside ${IMAGE_REPO}; refusing to plan over them"
fi

# Candidates: each old mark, and the new mark it becomes. Both prefixes come
# from lib/common.sh, so this follows the mark wherever it is defined. The
# channel is the word before `-promoted-` (dev, prod); a tag whose prefix
# does not round-trip through legacy_promotion_mark_prefix is not a mark.
: >"${WORK}/candidates.tsv"
while IFS=$'\t' read -r package version tag; do
  [[ "${tag}" =~ ^([a-z0-9]+)-promoted-(.+)$ ]] || continue
  channel="${BASH_REMATCH[1]}"
  rest="${BASH_REMATCH[2]}"
  [[ "${tag}" == "$(legacy_promotion_mark_prefix "${channel}")${rest}" ]] || continue
  printf '%s\t%s\t%s\t%s\n' "${package}" "${version}" "${tag}" \
    "$(promotion_mark_prefix "${channel}")${rest}" >>"${WORK}/candidates.tsv"
done <"${WORK}/tags.tsv"

# Join the candidates against every tag the registry holds. A tag is unique
# within one image, so (package, tag) names at most one version.
#   plan.tsv      package, version, new mark      -- to add
#   present.tsv   package, version, new mark      -- already on that digest
#   conflict.tsv  package, version, old, new, the version the new one names
#   invalid.tsv   package, version, old, new      -- not a valid tag
awk -F'\t' -v OFS='\t' -v dir="${WORK}" '
    FNR == NR { at[$1 SUBSEP $3] = $2; next }
    {
      key = $1 SUBSEP $4
      if ($4 !~ /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/ || length($4) > 128) {
        print $1, $2, $3, $4 > (dir "/invalid.tsv")
      } else if (!(key in at)) {
        if (!(key in planned)) { print $1, $2, $4 > (dir "/plan.tsv"); planned[key] = $2 }
      } else if (at[key] == $2) {
        print $1, $2, $4 > (dir "/present.tsv")
      } else {
        print $1, $2, $3, $4, at[key] > (dir "/conflict.tsv")
      }
    }' "${WORK}/tags.tsv" "${WORK}/candidates.tsv"
for f in plan present conflict invalid; do
  touch "${WORK}/${f}.tsv"
done

count() { wc -l <"$1" | tr -d '[:space:] '; }
PLANNED="$(count "${WORK}/plan.tsv")"
PRESENT="$(count "${WORK}/present.tsv")"
CONFLICTS="$(count "${WORK}/conflict.tsv")"
INVALID="$(count "${WORK}/invalid.tsv")"
OLD_MARKS="$(count "${WORK}/candidates.tsv")"

info "read ${VERSIONS} version(s); ${OLD_MARKS} carry an old <channel>-promoted- mark"
{
  printf '%s promotion mark(s) to add (%s already present, %s conflicting, %s invalid)\n' \
    "${PLANNED}" "${PRESENT}" "${CONFLICTS}" "${INVALID}"
  awk -F'\t' 'NR <= 20 { printf "  %s:%s -> %s\n", $1, $3, $2 }' "${WORK}/plan.tsv"
  if [[ "${PLANNED}" -gt 20 ]]; then
    printf '  ... and %s more\n' "$((PLANNED - 20))"
  fi
} | redact

# Never moved: a person decides what a mark already on another digest means.
if [[ "${CONFLICTS}" -gt 0 ]]; then
  warn "${CONFLICTS} new mark(s) already name another digest and are left as they are:"
  awk -F'\t' '{ printf "     %s:%s names %s; %s carries %s\n", $1, $4, $5, $2, $3 }' "${WORK}/conflict.tsv" \
    | redact >&2
fi
if [[ "${INVALID}" -gt 0 ]]; then
  warn "${INVALID} old mark(s) would become an invalid tag and are left as they are:"
  awk -F'\t' '{ printf "     %s:%s (from %s on %s)\n", $1, $4, $3, $2 }' "${WORK}/invalid.tsv" | redact >&2
fi

if [[ "${APPLY}" -ne 1 ]]; then
  ok "dry run: nothing written; run again with --apply to add ${PLANNED} mark(s)"
  exit 0
fi

FAILED=0
ADDED=0
while IFS=$'\t' read -r package version mark; do
  if gcloud artifacts docker tags add "${package}@${version}" "${package}:${mark}" \
       --project "${PROJECT_ID}" </dev/null >/dev/null 2>"${WORK}/add.err"; then
    ADDED=$((ADDED + 1))
    continue
  fi
  # A dead session fails every remaining add the same way; stop and say so.
  # What was added stays added, and a rerun skips it.
  die_if_auth_failure "$(cat "${WORK}/add.err")"
  warn "${package}:${mark} -> ${version}: could not be added" 2>&1 | redact >&2
  grep -v '^Listing items' "${WORK}/add.err" | redact | head -n 2 | sed 's/^/     /' >&2 || true
  FAILED=$((FAILED + 1))
done <"${WORK}/plan.tsv"

ok "added ${ADDED} of ${PLANNED} promotion mark(s); removed none"
if [[ "${FAILED}" -gt 0 || "${CONFLICTS}" -gt 0 || "${INVALID}" -gt 0 ]]; then
  die "${FAILED} failed, ${CONFLICTS} conflicting, ${INVALID} invalid: deploy.sh must keep reading the old mark until these are resolved"
fi
ok "every old promotion mark in ${IMAGE_REPO} has its promoted-<channel>-<tag> form; deploy.sh's read of the old one can now be removed"
