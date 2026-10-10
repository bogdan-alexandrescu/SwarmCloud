#!/usr/bin/env bash
# A person's forge slot pair: the empty `swarm-tenant-<tenant>-git-u-<hex>`
# slot the tenant's worker reads, and its `-refresh` twin no worker may read.
# Sourced, never executed. Needs common.sh first.
#
# WHY ONE FUNCTION. Two scripts make the pair: register-tenant.sh's A6 for a
# workspace the job builds, and workspace-migrate-record.sh for a tenant
# Terraform made (W9 of #847; owner decision 2026-10-10: the migration creates
# it, so every workspace has the shape A9 verifies). A second copy of the
# create-and-bind would drift from the first, and A9 would then hold one
# script's tenants to the other's shape -- the mismatch W9 was measured on.
#
# WHAT IT MAKES, AND WHAT IT NEVER DOES.
#   * Each secret empty, labelled as swarm-api labels the slots it makes
#     (swarm_api.forgeapp.slot_labels), so swarm-api's later create of the
#     same name finds it and treats ALREADY_EXISTS as success, and A9's
#     `labels.tenant` check reads the tenant back. No version is ever added:
#     the value arrives when the person connects GitHub, and never from here.
#   * roles/secretmanager.secretAccessor for the worker on the SLOT, never on
#     the twin (terraform/bootstrap/forge_user_slots.tf). A worker found on the
#     twin is reported, not repaired.
#   * A present secret labelled for another tenant is reported, not adopted.
#   * Idempotent: a re-run over a complete pair reads and changes nothing.
#
# THE CALLER'S RUNNER. The job runs every call through ws_call / ws_probe (the
# guard's retry and error file); the migration through plain ones. So the
# caller names its own, with ws_call / ws_probe's contracts:
#   FORGE_SLOT_CALL OUT CMD...   0 done; non-zero failed
#   FORGE_SLOT_PROBE OUT CMD...  0 present; 1 absent (NOT_FOUND); 2 could not tell
#   FORGE_SLOT_SHOW_ERR          prints the last failure, redacted
#   FORGE_SLOT_WORK              a private directory for the answers
#   FORGE_SLOT_DRY_RUN=1         read, and print what would be made; make nothing
set -euo pipefail

FORGE_SLOT_CALL="${FORGE_SLOT_CALL:-}"
FORGE_SLOT_PROBE="${FORGE_SLOT_PROBE:-}"
FORGE_SLOT_SHOW_ERR="${FORGE_SLOT_SHOW_ERR:-}"
FORGE_SLOT_WORK="${FORGE_SLOT_WORK:-}"
FORGE_SLOT_DRY_RUN="${FORGE_SLOT_DRY_RUN:-0}"
# Set when forge_slot_ensure_pair returns 3: the tenant the slot is labelled for.
FORGE_SLOT_LABELLED=""

# forge_slot_present NAME TENANT PROVIDER -> 0 present and the tenant's, or
# created; 1 failed (the reason printed); 3 present but labelled for another
# tenant (FORGE_SLOT_LABELLED says whom).
forge_slot_present() {
  local name="$1" tenant="$2" provider="$3" rc=0
  "${FORGE_SLOT_PROBE}" "${FORGE_SLOT_WORK}/slot.json" \
    gcloud secrets describe "${name}" --project "${PROJECT_ID}" --format=json || rc=$?
  case "${rc}" in
    0)
      FORGE_SLOT_LABELLED="$(jq -r '.labels.tenant // ""' "${FORGE_SLOT_WORK}/slot.json")"
      [[ "${FORGE_SLOT_LABELLED}" == "${tenant}" ]] || return 3
      dim "forge slot: ${name} is present; kept"
      return 0 ;;
    1)
      if [[ "${FORGE_SLOT_DRY_RUN}" == "1" ]]; then
        info "forge slot: would create ${name} (empty, labelled tenant=${tenant})"
        return 0
      fi
      if ! "${FORGE_SLOT_CALL}" /dev/null gcloud secrets create "${name}" --project "${PROJECT_ID}" \
             --replication-policy automatic \
             --labels "managed-by=swarm-api,swarm-tenant=${tenant},tenant=${tenant},provider=${provider}"; then
        # Made between the describe and the create (swarm-api's own create,
        # or a second run): ALREADY_EXISTS is success when it is ours.
        "${FORGE_SLOT_PROBE}" "${FORGE_SLOT_WORK}/slot.json" \
          gcloud secrets describe "${name}" --project "${PROJECT_ID}" --format=json \
          || { "${FORGE_SLOT_SHOW_ERR}"; return 1; }
        FORGE_SLOT_LABELLED="$(jq -r '.labels.tenant // ""' "${FORGE_SLOT_WORK}/slot.json")"
        [[ "${FORGE_SLOT_LABELLED}" == "${tenant}" ]] || return 3
        dim "forge slot: ${name} is present; kept"
        return 0
      fi
      ok "forge slot: created ${name} (empty, labelled tenant=${tenant})"
      return 0 ;;
  esac
  return 1
}

# forge_slot_ensure_pair TENANT SLOT TWIN WORKER_EMAIL -> 0 the pair present,
# the worker bound on the slot and not on the twin; 1 failed (the reason
# printed); 3 a secret labelled for another tenant (FORGE_SLOT_LABELLED);
# 4 the worker is bound on the twin.
forge_slot_ensure_pair() {
  local tenant="$1" slot="$2" twin="$3" email="$4" suffix rc
  [[ -n "${FORGE_SLOT_CALL}" && -n "${FORGE_SLOT_PROBE}" && -n "${FORGE_SLOT_SHOW_ERR}" \
     && -d "${FORGE_SLOT_WORK}" ]] \
    || { err "forge slot: the caller named no runner (FORGE_SLOT_CALL, _PROBE, _SHOW_ERR, _WORK)"; return 1; }
  suffix="${slot#swarm-tenant-"${tenant}"-}"
  [[ "${suffix}" != "${slot}" && "${twin}" == "${slot}-refresh" ]] \
    || { err "forge slot: ${slot} / ${twin} is not the tenant ${tenant}'s pair"; return 1; }
  rc=0; forge_slot_present "${slot}" "${tenant}" "${suffix}" || rc=$?
  [[ "${rc}" -eq 0 ]] || return "${rc}"
  rc=0; forge_slot_present "${twin}" "${tenant}" "${suffix}-refresh" || rc=$?
  [[ "${rc}" -eq 0 ]] || return "${rc}"

  local member="serviceAccount:${email}" policy="${FORGE_SLOT_WORK}/slot-policy.json"
  local twin_policy="${FORGE_SLOT_WORK}/twin-policy.json"
  # A dry run's slot may not exist yet; there is no policy to read, and the
  # binding is what --apply would add.
  rc=0
  "${FORGE_SLOT_PROBE}" "${twin_policy}" gcloud secrets get-iam-policy "${twin}" \
    --project "${PROJECT_ID}" --format=json || rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    # The refresh twin is the one secret no worker reads; finding the worker
    # on it is not something this repairs.
    if jq -e --arg m "${member}" 'any((.bindings? // [])[]; any(.members[]?; . == $m))' \
         "${twin_policy}" >/dev/null 2>&1; then
      return 4
    fi
  elif ! [[ "${rc}" -eq 1 && "${FORGE_SLOT_DRY_RUN}" == "1" ]]; then
    [[ "${rc}" -eq 2 ]] || "${FORGE_SLOT_SHOW_ERR}"
    return 1
  fi
  rc=0
  "${FORGE_SLOT_PROBE}" "${policy}" gcloud secrets get-iam-policy "${slot}" \
    --project "${PROJECT_ID}" --format=json || rc=$?
  if [[ "${rc}" -eq 0 ]] && iam_policy_binds_member "${policy}" roles/secretmanager.secretAccessor "${member}"; then
    dim "forge slot: ${email} already reads ${slot}; kept"
    return 0
  fi
  if [[ "${FORGE_SLOT_DRY_RUN}" == "1" ]]; then
    info "forge slot: would grant roles/secretmanager.secretAccessor on ${slot} (never ${twin}) to ${email}"
    return 0
  fi
  [[ "${rc}" -eq 0 ]] || { [[ "${rc}" -eq 2 ]] || "${FORGE_SLOT_SHOW_ERR}"; return 1; }
  "${FORGE_SLOT_CALL}" /dev/null gcloud secrets add-iam-policy-binding "${slot}" --project "${PROJECT_ID}" \
    --member "${member}" --role roles/secretmanager.secretAccessor \
    || { "${FORGE_SLOT_SHOW_ERR}"; return 1; }
  ok "forge slot: granted roles/secretmanager.secretAccessor on ${slot} to ${email}"
}
