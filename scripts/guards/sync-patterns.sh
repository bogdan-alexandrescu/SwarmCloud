#!/usr/bin/env bash
# Publish the deployment pattern list from its one source to its two stores
# (docs/design/generic-repository.md section 1.4):
#
#   source  gs://$TF_STATE_BUCKET/config/deployment-patterns.tsv (private, versioned)
#   ->      the DEPLOYMENT_PATTERNS repository secret (read by the CI guard job)
#   ->      a new version of the swarm-deployment-patterns Secret Manager entry
#           (read by the worker's pre-publish gate)
#
#   scripts/guards/sync-patterns.sh [--repo <owner/name>] [--dry-run]
#
# Both stores get the same bytes: a first line `# sha256 <digest of the rest>`,
# then the source's entries. Any digest line the source already has is
# replaced. The guard prints the digest on every run, so two stores that
# disagree show without a value being printed. The entries are validated
# before anything is written; a malformed source writes nothing.
#
# No typed confirmation: the source object is the truth and keeps every past
# version, so a run overwrites nothing that cannot be written again from it,
# and Secret Manager keeps the previous version too.
#
# The list's values never reach stdout, stderr or a log: the working copy is a
# mode-600 file in a private directory under $TMPDIR, removed on exit.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../lib/common.sh"

SM_SLOT="swarm-deployment-patterns"
GH_SLOT="DEPLOYMENT_PATTERNS"

usage() {
  cat >&2 <<'USAGE'
Usage: scripts/guards/sync-patterns.sh [--repo <owner/name>] [--dry-run]

  --repo <owner/name>  the repository whose secret to set; default
                       $GITHUB_REPOSITORY, else the gh default repository
  --dry-run            read and validate the source, print its digest, write nothing
USAGE
}

REPO="${GITHUB_REPOSITORY:-}"
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)    [[ $# -ge 2 ]] || { usage; exit 2; }; REPO="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         usage; die "unknown argument: $1" ;;
  esac
done

GCLOUD="$(prefer_local_bin gcloud "${SWARM_GCLOUD:-}")" || die "gcloud is not installed; it reads the source and writes the Secret Manager entry"
SOURCE_OBJECT="gs://${TF_STATE_BUCKET}/config/deployment-patterns.tsv"

umask 077
work="$(mktemp -d "${TMPDIR:-/tmp}/swarm-patterns.XXXXXX")"
trap 'rm -rf "${work}"' EXIT

if ! "${GCLOUD}" storage cat "${SOURCE_OBJECT}" >"${work}/source" 2>"${work}/err"; then
  redact <"${work}/err" >&2 || true
  die "cannot read the list's source ${SOURCE_OBJECT}"
fi
digest="$(python3 "${SWARM_LIB_DIR}/../guards/deployment_values.py" stamp --in "${work}/source" --out "${work}/list")" \
  || die "the source ${SOURCE_OBJECT} is malformed (the line is named above); nothing was written"
info "pattern list sha256 ${digest}, from ${SOURCE_OBJECT}"
if [[ "${DRY_RUN}" -eq 1 ]]; then
  info "dry run: nothing written"
  exit 0
fi

GH="$(prefer_local_bin gh "${SWARM_GH:-}")" || die "gh is not installed; it sets the repository secret"
if [[ -z "${REPO}" ]]; then
  REPO="$("${GH}" repo view --json nameWithOwner --jq .nameWithOwner)" || die "no repository: pass --repo <owner/name>"
fi
"${GH}" secret set "${GH_SLOT}" --repo "${REPO}" <"${work}/list" >/dev/null 2>"${work}/err" \
  || { redact <"${work}/err" >&2 || true; die "could not set the ${GH_SLOT} secret on ${REPO}"; }
info "${GH_SLOT} set on ${REPO}"

if ! "${GCLOUD}" secrets describe "${SM_SLOT}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  "${GCLOUD}" secrets create "${SM_SLOT}" --project="${PROJECT_ID}" \
    --replication-policy=user-managed --locations="${REGION}" \
    --labels=managed-by=swarm-sync-patterns >/dev/null 2>"${work}/err" \
    || { redact <"${work}/err" >&2 || true; die "could not create ${SM_SLOT} in ${PROJECT_ID}"; }
fi
"${GCLOUD}" secrets versions add "${SM_SLOT}" --project="${PROJECT_ID}" --data-file="${work}/list" \
  >/dev/null 2>"${work}/err" \
  || { redact <"${work}/err" >&2 || true; die "could not add a version to ${SM_SLOT} in ${PROJECT_ID}"; }
info "${SM_SLOT} has a new version in ${PROJECT_ID}; both stores carry sha256 ${digest}"
