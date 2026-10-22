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
#
# A TEAM SLOT (docs/design/workspace-kinds.md §4.6). A group workspace's slot
# is `swarm-tenant-<g>-git` with its `-refresh` twin, made by the same
# forge_slot_ensure_pair. A service workspace's is `swarm-tenant-<t>-git`
# ALONE, forge_slot_ensure_single: an App refresh token belongs to a person,
# and a service account is not one, so there is no twin to make or to keep
# the worker off.
#
# THE QA SECRET. qa_secret_ensure, at the end, makes the tenant's empty
# `swarm-tenant-<tenant>-qa` with swarm-api alone bound on it: the same
# create-and-bind for every path that registers a tenant Terraform does not.
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

# forge_slot_bind SLOT WORKER_EMAIL -> 0 the worker reads SLOT; 1 failed.
forge_slot_bind() {
  local slot="$1" email="$2" rc=0
  local member="serviceAccount:${email}" policy="${FORGE_SLOT_WORK}/slot-policy.json"
  "${FORGE_SLOT_PROBE}" "${policy}" gcloud secrets get-iam-policy "${slot}" \
    --project "${PROJECT_ID}" --format=json || rc=$?
  if [[ "${rc}" -eq 0 ]] && iam_policy_binds_member "${policy}" roles/secretmanager.secretAccessor "${member}"; then
    dim "forge slot: ${email} already reads ${slot}; kept"
    return 0
  fi
  if [[ "${FORGE_SLOT_DRY_RUN}" == "1" ]]; then
    info "forge slot: would grant roles/secretmanager.secretAccessor on ${slot} to ${email}"
    return 0
  fi
  [[ "${rc}" -eq 0 ]] || { [[ "${rc}" -eq 2 ]] || "${FORGE_SLOT_SHOW_ERR}"; return 1; }
  "${FORGE_SLOT_CALL}" /dev/null gcloud secrets add-iam-policy-binding "${slot}" --project "${PROJECT_ID}" \
    --member "${member}" --role roles/secretmanager.secretAccessor \
    || { "${FORGE_SLOT_SHOW_ERR}"; return 1; }
  ok "forge slot: granted roles/secretmanager.secretAccessor on ${slot} to ${email}"
}

# forge_slot_ensure_single TENANT SLOT WORKER_EMAIL -> 0 the team slot present
# and the worker bound on it; 1 failed (the reason printed); 3 a secret
# labelled for another tenant (FORGE_SLOT_LABELLED). A service workspace's
# `swarm-tenant-<t>-git`, which has no twin (§4.6).
forge_slot_ensure_single() {
  local tenant="$1" slot="$2" email="$3" rc
  [[ -n "${FORGE_SLOT_CALL}" && -n "${FORGE_SLOT_PROBE}" && -n "${FORGE_SLOT_SHOW_ERR}" \
     && -d "${FORGE_SLOT_WORK}" ]] \
    || { err "forge slot: the caller named no runner (FORGE_SLOT_CALL, _PROBE, _SHOW_ERR, _WORK)"; return 1; }
  [[ "${slot}" == "swarm-tenant-${tenant}-git" ]] \
    || { err "forge slot: ${slot} is not the tenant ${tenant}'s team slot"; return 1; }
  rc=0; forge_slot_present "${slot}" "${tenant}" git || rc=$?
  [[ "${rc}" -eq 0 ]] || return "${rc}"
  forge_slot_bind "${slot}" "${email}"
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

# --- the tenant's QA target credential secret ---------------------------------
#
# `swarm-tenant-<tenant>-qa` (docs/design/visual-qa-any-app.md §2.1, §7):
# terraform/infra/qa_targets.tf declares it for every tenant in var.tenants,
# and a tenant this script registers is not one, so it is made here (BGD,
# 2026-10-22: "register-tenant.sh creates it"). The same shape:
#
#   * EMPTY. swarm-api adds every version from the console's one-time form;
#     no value ever passes through here.
#   * qa_targets.tf's own labels -- component, tenant, swarm-tenant -- and
#     swarm-env, which Terraform's local.labels carries. NOT managed-by=
#     swarm-terraform: destroy.sh and the plan guard read that label as "in
#     Terraform's state", and this secret is not.
#     tests/unit/scripts/test_register_tenant_qa_secret.py reads the labels off
#     qa_targets.tf, so a label moved there fails here.
#   * Pinned to the workload region, as qa_targets.tf's user_managed replicas
#     are: automatic replication would copy a test account's password into
#     regions nothing here runs in.
#   * V-1 secretVersionAdder and V-2 secretAccessor for swarm-api, on this one
#     secret, and NOBODY ELSE: not the tenant's worker, whose agent would then
#     read a password out of Secret Manager itself. Terraform's two bindings
#     are authoritative; here they are added, and a policy naming any other
#     member is reported (return 4), never repaired -- removing a member is
#     not something registration does.
QA_SECRET_COMPONENT="qa-target-credential"
# Set when qa_secret_ensure returns 4: the member that should not be there.
QA_SECRET_FOREIGN=""

# qa_secret_labels TENANT -> the --labels value for the tenant's QA secret.
qa_secret_labels() {
  printf 'managed-by=swarm-register-tenant,swarm-env=%s,component=%s,tenant=%s,swarm-tenant=%s' \
    "${ENVIRONMENT}" "${QA_SECRET_COMPONENT}" "$1" "$1"
}

# qa_secret_present NAME TENANT -> 0 present and this tenant's QA secret, or
# created; 1 failed; 3 present but not this tenant's QA secret
# (FORGE_SLOT_LABELLED says whose). A name does not say whose a secret is
# (register-tenant.sh "whose secret is it?"): swarm-tenant-eng-qa is also
# tenant eng's key for a provider named `qa`, which the worker reads.
qa_secret_present() {
  local name="$1" tenant="$2" rc=0 component
  "${FORGE_SLOT_PROBE}" "${FORGE_SLOT_WORK}/qa.json" \
    gcloud secrets describe "${name}" --project "${PROJECT_ID}" --format=json || rc=$?
  if [[ "${rc}" -eq 1 ]]; then
    if [[ "${FORGE_SLOT_DRY_RUN}" == "1" ]]; then
      info "qa secret: would create ${name} (empty, labelled component=${QA_SECRET_COMPONENT} tenant=${tenant}, in ${REGION})"
      return 0
    fi
    if "${FORGE_SLOT_CALL}" /dev/null gcloud secrets create "${name}" --project "${PROJECT_ID}" \
         --replication-policy user-managed --locations "${REGION}" \
         --labels "$(qa_secret_labels "${tenant}")"; then
      ok "qa secret: created ${name} (empty, labelled component=${QA_SECRET_COMPONENT} tenant=${tenant})"
      return 0
    fi
    # Made between the describe and the create: ALREADY_EXISTS is success
    # when the labels below say it is ours.
    rc=0
    "${FORGE_SLOT_PROBE}" "${FORGE_SLOT_WORK}/qa.json" \
      gcloud secrets describe "${name}" --project "${PROJECT_ID}" --format=json || rc=$?
    [[ "${rc}" -eq 0 ]] || { [[ "${rc}" -eq 2 ]] || "${FORGE_SLOT_SHOW_ERR}"; return 1; }
  fi
  [[ "${rc}" -eq 0 ]] || return 1
  FORGE_SLOT_LABELLED="$(jq -r '.labels.tenant // ""' "${FORGE_SLOT_WORK}/qa.json")"
  component="$(jq -r '.labels.component // ""' "${FORGE_SLOT_WORK}/qa.json")"
  [[ "${FORGE_SLOT_LABELLED}" == "${tenant}" && "${component}" == "${QA_SECRET_COMPONENT}" ]] || {
    [[ "${component}" == "${QA_SECRET_COMPONENT}" ]] || FORGE_SLOT_LABELLED="${FORGE_SLOT_LABELLED:-nobody} (component=${component:-none})"
    return 3
  }
  dim "qa secret: ${name} is present; kept"
}

# qa_secret_ensure TENANT API_EMAIL -> 0 the secret present, empty as far as
# this goes, and swarm-api holding V-1 and V-2 on it; 1 failed (the reason
# printed); 3 a secret of that name that is not this tenant's QA secret
# (FORGE_SLOT_LABELLED); 4 another member holds a role on it
# (QA_SECRET_FOREIGN). Idempotent: a re-run over a complete secret reads and
# changes nothing.
qa_secret_ensure() {
  local tenant="$1" email="$2" rc=0 role
  local name="swarm-tenant-${tenant}-qa" member="serviceAccount:${email}"
  local policy="${FORGE_SLOT_WORK}/qa-policy.json"
  [[ -n "${FORGE_SLOT_CALL}" && -n "${FORGE_SLOT_PROBE}" && -n "${FORGE_SLOT_SHOW_ERR}" \
     && -d "${FORGE_SLOT_WORK}" ]] \
    || { err "qa secret: the caller named no runner (FORGE_SLOT_CALL, _PROBE, _SHOW_ERR, _WORK)"; return 1; }
  qa_secret_present "${name}" "${tenant}" || return $?
  "${FORGE_SLOT_PROBE}" "${policy}" gcloud secrets get-iam-policy "${name}" \
    --project "${PROJECT_ID}" --format=json || rc=$?
  if [[ "${rc}" -eq 1 && "${FORGE_SLOT_DRY_RUN}" == "1" ]]; then
    # A dry run's secret does not exist yet: there is no policy to read.
    info "qa secret: would grant roles/secretmanager.secretVersionAdder and roles/secretmanager.secretAccessor on ${name} to ${email}, and nobody else"
    return 0
  fi
  [[ "${rc}" -eq 0 ]] || { [[ "${rc}" -eq 2 ]] || "${FORGE_SLOT_SHOW_ERR}"; return 1; }
  QA_SECRET_FOREIGN="$(jq -r --arg m "${member}" \
    '[(.bindings? // [])[] | .role as $r | .members[]? | select(. != $m) | "\(.) (\($r))"] | first // ""' \
    "${policy}")"
  [[ -z "${QA_SECRET_FOREIGN}" ]] || return 4
  for role in roles/secretmanager.secretVersionAdder roles/secretmanager.secretAccessor; do
    if iam_policy_binds_member "${policy}" "${role}" "${member}"; then
      dim "qa secret: ${email} already holds ${role} on ${name}; kept"
      continue
    fi
    if [[ "${FORGE_SLOT_DRY_RUN}" == "1" ]]; then
      info "qa secret: would grant ${role} on ${name} to ${email}"
      continue
    fi
    "${FORGE_SLOT_CALL}" /dev/null gcloud secrets add-iam-policy-binding "${name}" --project "${PROJECT_ID}" \
      --member "${member}" --role "${role}" \
      || { "${FORGE_SLOT_SHOW_ERR}"; return 1; }
    ok "qa secret: granted ${role} on ${name} to ${email}"
  done
}
