#!/usr/bin/env bash
# Stand up the SwarmCloud release home, idempotently (DAYONE-RELHOME-1; design
# docs/design/day-one-install.md section 6.1, plan section 7.1).
#
# The release home is project `swarmcloud-prod` (D25; its display name is
# `swarmcloud`, the id `swarmcloud` being taken; `swarmcloud-releases` had that
# id been taken, D23). It is where every company's install fetches the release,
# and nothing in a deployment reads it after the install has copied the images
# (D21). BGD runs this; nothing in CI holds a grant in that project (D22). It
# creates, in this order, each only if it is not there yet:
#
#   1. the three APIs it needs (Artifact Registry, Cloud KMS, Cloud Storage);
#   2. Artifact Registry repository `swarmcloud` in `us`, format Docker
#      (us-docker.pkg.dev/swarmcloud-prod/swarmcloud);
#   3. bucket gs://swarmcloud in `us`, uniform bucket-level access, versioning;
#   4. KMS key ring `swarmcloud-release` in `us` and, in it, key `cosign`:
#      asymmetric signing, EC P-256 SHA-256, the key cosign signs every image
#      digest and SHA256SUMS with (gcpkms://projects/swarmcloud-prod/locations/us/
#      keyRings/swarmcloud-release/cryptoKeys/cosign).
#
# The repository, the bucket and the key are labelled managed-by=swarm-release.
# A KMS key ring cannot carry a label (the API has no field for one), so the
# ring is the one exception. Neither a key ring nor a key can ever be deleted,
# only its versions destroyed -- which is why an existing key of the wrong
# purpose or algorithm is refused rather than "fixed": the fix is a new key
# name, and that is a decision, not a script's.
#
# Then, with --public and ONLY then, the one IAM change that makes the release
# downloadable by any company without an account (Q4):
#
#   * allUsers as roles/storage.objectViewer on gs://swarmcloud;
#   * allUsers as roles/artifactregistry.reader on the repository.
#
# An org with Domain Restricted Sharing refuses a grant to allUsers unless the
# project carries an exception to iam.allowedPolicyMemberDomains, and making
# that exception is an org admin's act. So before anything else is touched,
# --public reads the EFFECTIVE policy on the project and refuses unless it
# admits every member; an unreadable policy is a refusal, not a pass. Then it
# asks for the project id, typed: SWARM_ASSUME_YES never answers that.
#
# The cosign public key is printed on stdout at the end (it is public, not a
# secret); DAYONE-ENGINE-3 pins it as swarm_install/release_key.pub.
#
# Usage:
#   scripts/release-home.sh --dry-run [--public]  # every call it would make, none made
#   scripts/release-home.sh                       # registry, bucket, key; no public read
#   scripts/release-home.sh --public              # ...and the public read, after the check
#   scripts/release-home.sh --project swarmcloud-releases
#
# Exit codes: 0 done (or nothing to do), 1 refused or failed, 2 bad arguments.

set -euo pipefail

# Never wait on a prompt nobody can see. On a project where an API is not yet
# enabled, `gcloud ... describe` asks "enable it and retry? (y/N)" and blocks
# on stdin; the first real run of this script against swarmcloud-prod hung there
# for ten minutes (2026-10-30). With prompts off it fails at once, which reads
# as "not there yet", and the enable below runs.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# The public grant is never non-interactive. An automation that sets this does
# not get to answer the typed confirmation for this script.
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  warn "ignoring SWARM_ASSUME_YES: the public grant always needs the project id typed"
  unset SWARM_ASSUME_YES
fi

# The two ids the release home may have (D23). Anything else is refused: this
# script makes a bucket and a registry world-readable, and the only project
# where that is the intent is the release home.
RELEASE_PROJECTS=("swarmcloud-prod" "swarmcloud-releases")
PROJECT="swarmcloud-prod"
# One location for all three: `us` is a multi-region in Artifact Registry,
# Cloud Storage and Cloud KMS alike, and the paths in the design are us-*.
LOCATION="us"
REPOSITORY="swarmcloud"
BUCKET="gs://swarmcloud"
KEYRING="swarmcloud-release"
KEY="cosign"
LABEL_KEY="managed-by"
LABEL_VALUE="swarm-release"
LABEL="${LABEL_KEY}=${LABEL_VALUE}"
SERVICES=("artifactregistry.googleapis.com" "cloudkms.googleapis.com" "storage.googleapis.com")
POLICY_CONSTRAINT="iam.allowedPolicyMemberDomains"
PUBLIC_MEMBER="allUsers"
BUCKET_ROLE="roles/storage.objectViewer"
REPOSITORY_ROLE="roles/artifactregistry.reader"

DRY_RUN=0
PUBLIC=0

usage() { sed -n '2,52p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      [[ $# -ge 2 ]] || { err "--project needs a value"; exit 2; }
      PROJECT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --public)  PUBLIC=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown argument: $1"; usage; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Guards, before gcloud is called at all
# ---------------------------------------------------------------------------

# The shared project is the one whose service accounts SHARED_DENY_LIST names
# (<name>@<project>.iam.gserviceaccount.com): read from the one list rather
# than spelling the project id a second time.
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
for name in "${REPOSITORY}" "${BUCKET#gs://}" "${KEYRING}" "${KEY}"; do
  if is_shared_resource "${name}"; then
    die "refusing: ${name} is on SHARED_DENY_LIST (scripts/lib/common.sh)"
  fi
done
release_project=0
for candidate in "${RELEASE_PROJECTS[@]}"; do
  if [[ "${PROJECT}" == "${candidate}" ]]; then release_project=1; fi
done
[[ "${release_project}" -eq 1 ]] \
  || die "refusing project ${PROJECT}: the release home is one of ${RELEASE_PROJECTS[*]} (D23, D25)"

require_cmd gcloud jq

WORK="$(mktemp -d "${TMPDIR:-/tmp}/release-home.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# Every gcloud call goes through one of these two, so a dry run prints every
# call: reads run (they change nothing and decide what would be written),
# writes are printed on stdout and not run.
gc_read() {
  dim "read: gcloud $*"
  gcloud "$@"
}
gc_write() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf 'would run: gcloud %s\n' "$*"
    return 0
  fi
  info "gcloud $*"
  gcloud "$@"
}

# has_label FILE -> 0 when the described resource carries managed-by=swarm-release.
has_label() {
  jq -e --arg k "${LABEL_KEY}" --arg v "${LABEL_VALUE}" '(.labels // {})[$k] == $v' "$1" >/dev/null
}

# ---------------------------------------------------------------------------
# Phase 1: read everything and decide. Nothing is written until every check,
# the org-policy one included, has passed.
# ---------------------------------------------------------------------------

if [[ "${PUBLIC}" -eq 1 ]]; then
  step "Effective ${POLICY_CONSTRAINT} on ${PROJECT}"
  policy="${WORK}/policy.json"
  if ! gc_read resource-manager org-policies describe "${POLICY_CONSTRAINT}" \
      --project="${PROJECT}" --effective --format=json >"${policy}"; then
    die "refusing the public grant: could not read the effective ${POLICY_CONSTRAINT} policy on ${PROJECT}; an unread policy is not an exception"
  fi
  # Admitted: the constraint is not set at all (no listPolicy, so the default,
  # which admits every member), or the exception (allValues ALLOW). Anything
  # else -- an allowed list of customer ids, a denied list, DENY -- refuses
  # allUsers, so the grant would fail half-way; refuse it here instead.
  if jq -e '(.listPolicy // {}) as $l
            | ($l.allValues == "ALLOW")
              or (($l | has("allValues") | not)
                  and (($l.allowedValues // []) | length) == 0
                  and (($l.deniedValues // []) | length) == 0)' "${policy}" >/dev/null; then
    ok "${POLICY_CONSTRAINT} admits ${PUBLIC_MEMBER} on ${PROJECT}"
  else
    err "the effective ${POLICY_CONSTRAINT} policy on ${PROJECT} does not admit ${PUBLIC_MEMBER}:"
    jq -c '.listPolicy // {}' "${policy}" >&2 || true
    die "refusing the public grant until the Q4 org-policy exception exists: an org admin overrides ${POLICY_CONSTRAINT} on project ${PROJECT} alone to allow all values; then run this again"
  fi
fi

step "Release home in ${PROJECT}"

NEED_SERVICES=()
enabled="${WORK}/services.txt"
gc_read services list --enabled --project="${PROJECT}" --format='value(config.name)' >"${enabled}" \
  || : >"${enabled}"
for service in "${SERVICES[@]}"; do
  grep -qx "${service}" "${enabled}" || NEED_SERVICES+=("${service}")
done

repo="${WORK}/repo.json"
REPO_CREATE=0; REPO_LABEL=0
if gc_read artifacts repositories describe "${REPOSITORY}" --location="${LOCATION}" \
    --project="${PROJECT}" --format=json >"${repo}" 2>"${WORK}/repo.err"; then
  jq -e '.format == "DOCKER"' "${repo}" >/dev/null \
    || die "repository ${REPOSITORY} in ${PROJECT} exists but is not a Docker repository ($(jq -r '.format' "${repo}")); not changing it"
  has_label "${repo}" || REPO_LABEL=1
else
  REPO_CREATE=1
fi

bucket="${WORK}/bucket.json"
BUCKET_CREATE=0; BUCKET_UPDATE=()
if gc_read storage buckets describe "${BUCKET}" --project="${PROJECT}" --format=json \
    >"${bucket}" 2>"${WORK}/bucket.err"; then
  # A bucket's location cannot change; gs:// names are global, so a bucket of
  # this name in another location (or another project) is not ours to adopt.
  jq -e --arg l "${LOCATION}" '(.location // "" | ascii_downcase) == $l' "${bucket}" >/dev/null \
    || die "${BUCKET} exists in location $(jq -r '.location' "${bucket}"), not ${LOCATION}; a bucket cannot move, not changing it"
  jq -e '.uniform_bucket_level_access == true' "${bucket}" >/dev/null \
    || BUCKET_UPDATE+=("--uniform-bucket-level-access")
  jq -e '.versioning_enabled == true' "${bucket}" >/dev/null || BUCKET_UPDATE+=("--versioning")
  has_label "${bucket}" || BUCKET_UPDATE+=("--update-labels=${LABEL}")
else
  BUCKET_CREATE=1
  # `buckets create` sets neither versioning nor labels: the update does.
  BUCKET_UPDATE=("--versioning" "--update-labels=${LABEL}")
fi

KEYRING_CREATE=0
gc_read kms keyrings describe "${KEYRING}" --location="${LOCATION}" --project="${PROJECT}" \
  --format=json >"${WORK}/keyring.json" 2>"${WORK}/keyring.err" || KEYRING_CREATE=1

key="${WORK}/key.json"
KEY_CREATE=0; KEY_LABEL=0
if [[ "${KEYRING_CREATE}" -eq 0 ]] && gc_read kms keys describe "${KEY}" --keyring="${KEYRING}" \
    --location="${LOCATION}" --project="${PROJECT}" --format=json >"${key}" 2>"${WORK}/key.err"; then
  jq -e '.purpose == "ASYMMETRIC_SIGN" and .versionTemplate.algorithm == "EC_SIGN_P256_SHA256"' \
    "${key}" >/dev/null \
    || die "key ${KEYRING}/${KEY} exists as $(jq -r '"\(.purpose) \(.versionTemplate.algorithm)"' "${key}"), not ASYMMETRIC_SIGN EC_SIGN_P256_SHA256; a key's purpose and algorithm cannot change and a key cannot be deleted -- choosing a new key name is BGD's decision"
  has_label "${key}" || KEY_LABEL=1
else
  KEY_CREATE=1
fi

# ---------------------------------------------------------------------------
# Phase 2: create what is missing, converge what drifted.
# ---------------------------------------------------------------------------

if [[ ${#NEED_SERVICES[@]} -gt 0 ]]; then
  gc_write services enable "${NEED_SERVICES[@]}" --project="${PROJECT}"
else
  ok "APIs enabled"
fi

if [[ "${REPO_CREATE}" -eq 1 ]]; then
  gc_write artifacts repositories create "${REPOSITORY}" --repository-format=docker \
    --location="${LOCATION}" --project="${PROJECT}" --labels="${LABEL}" \
    --description="SwarmCloud release images, by digest, signed with cosign"
elif [[ "${REPO_LABEL}" -eq 1 ]]; then
  gc_write artifacts repositories update "${REPOSITORY}" --location="${LOCATION}" \
    --project="${PROJECT}" --update-labels="${LABEL}"
else
  ok "repository ${LOCATION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}"
fi

if [[ "${BUCKET_CREATE}" -eq 1 ]]; then
  gc_write storage buckets create "${BUCKET}" --project="${PROJECT}" --location="${LOCATION}" \
    --uniform-bucket-level-access
fi
if [[ ${#BUCKET_UPDATE[@]} -gt 0 ]]; then
  gc_write storage buckets update "${BUCKET}" "${BUCKET_UPDATE[@]}" --project="${PROJECT}"
else
  ok "bucket ${BUCKET}"
fi

if [[ "${KEYRING_CREATE}" -eq 1 ]]; then
  gc_write kms keyrings create "${KEYRING}" --location="${LOCATION}" --project="${PROJECT}"
fi
if [[ "${KEY_CREATE}" -eq 1 ]]; then
  gc_write kms keys create "${KEY}" --keyring="${KEYRING}" --location="${LOCATION}" \
    --project="${PROJECT}" --purpose=asymmetric-signing \
    --default-algorithm=ec-sign-p256-sha256 --protection-level=software --labels="${LABEL}"
elif [[ "${KEY_LABEL}" -eq 1 ]]; then
  gc_write kms keys update "${KEY}" --keyring="${KEYRING}" --location="${LOCATION}" \
    --project="${PROJECT}" --update-labels="${LABEL}"
else
  ok "key ${KEYRING}/${KEY}"
fi

# ---------------------------------------------------------------------------
# Phase 3 (--public only): the public read, after the typed project id.
# ---------------------------------------------------------------------------

if [[ "${PUBLIC}" -eq 1 ]]; then
  step "Public read for ${PUBLIC_MEMBER}"
  bucket_policy="${WORK}/bucket-iam.json"
  repo_policy="${WORK}/repo-iam.json"
  # A resource that does not exist yet (a dry run from empty) has no policy:
  # an empty file reads as "not bound", which is the truth.
  gc_read storage buckets get-iam-policy "${BUCKET}" --project="${PROJECT}" --format=json \
    >"${bucket_policy}" 2>/dev/null || : >"${bucket_policy}"
  gc_read artifacts repositories get-iam-policy "${REPOSITORY}" --location="${LOCATION}" \
    --project="${PROJECT}" --format=json >"${repo_policy}" 2>/dev/null || : >"${repo_policy}"
  GRANT_BUCKET=1; GRANT_REPO=1
  iam_policy_binds_member "${bucket_policy}" "${BUCKET_ROLE}" "${PUBLIC_MEMBER}" && GRANT_BUCKET=0
  iam_policy_binds_member "${repo_policy}" "${REPOSITORY_ROLE}" "${PUBLIC_MEMBER}" && GRANT_REPO=0

  if [[ "${GRANT_BUCKET}" -eq 0 && "${GRANT_REPO}" -eq 0 ]]; then
    ok "${PUBLIC_MEMBER} already reads ${BUCKET} and ${REPOSITORY}"
  else
    if [[ "${DRY_RUN}" -eq 0 ]]; then
      warn "this makes ${BUCKET} and ${LOCATION}-docker.pkg.dev/${PROJECT}/${REPOSITORY} readable by anyone on the internet, without an account"
      confirm "Grant ${PUBLIC_MEMBER} ${BUCKET_ROLE} on ${BUCKET} and ${REPOSITORY_ROLE} on ${REPOSITORY} in ${PROJECT}." "${PROJECT}"
    fi
    if [[ "${GRANT_BUCKET}" -eq 1 ]]; then
      gc_write storage buckets add-iam-policy-binding "${BUCKET}" --member="${PUBLIC_MEMBER}" \
        --role="${BUCKET_ROLE}" --project="${PROJECT}"
    fi
    if [[ "${GRANT_REPO}" -eq 1 ]]; then
      gc_write artifacts repositories add-iam-policy-binding "${REPOSITORY}" --location="${LOCATION}" \
        --project="${PROJECT}" --member="${PUBLIC_MEMBER}" --role="${REPOSITORY_ROLE}"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# The cosign public key, for swarm_install/release_key.pub (ENGINE-3).
# ---------------------------------------------------------------------------

if [[ "${DRY_RUN}" -eq 1 && "${KEY_CREATE}" -eq 1 ]]; then
  dim "the key does not exist yet: no public key to print"
else
  step "cosign public key (gcpkms://projects/${PROJECT}/locations/${LOCATION}/keyRings/${KEYRING}/cryptoKeys/${KEY})"
  gc_read kms keys versions get-public-key 1 --key="${KEY}" --keyring="${KEYRING}" \
    --location="${LOCATION}" --project="${PROJECT}"
fi
ok "release home ${PROJECT} $([[ "${DRY_RUN}" -eq 1 ]] && printf 'planned (dry run)' || printf 'in place')"
