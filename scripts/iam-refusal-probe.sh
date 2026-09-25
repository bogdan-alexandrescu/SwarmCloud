#!/usr/bin/env bash
# Ask, once and on purpose, whether the CI deployer can grant itself a role
# terraform/infra never hands out -- and prove the answer is no (#68).
#
# RUN ONLY BY .github/workflows/iam-refusal-probe.yml, as the deployer, on
# main, dispatched by the owner after step 3 of the order recorded on #80
# (PR #73's targeted bootstrap apply, ~7 minutes of IAM propagation, a
# release). docs/runbooks/iam-refusal-probe.md is the owner's page.
#
# WHAT IT ASKS. After #73, the deployer's roles/resourcemanager.projectIamAdmin
# carries the condition
#
#     api.getAttribute("iam.googleapis.com/modifiedGrantsByRole", []).hasOnly([...])
#
# (terraform/bootstrap/deployer_conditions.tf), whose list is the fifteen
# project roles terraform/infra grants. Nothing in the pipeline ever asks for a
# role off that list, so nothing has shown the refusal side works. This script
# asks for one: `gcloud projects add-iam-policy-binding` of PROBE_ROLE to the
# deployer itself.
#
# WHY roles/browser. It is on neither side of the question:
#   * it is not in deployer_grantable_project_roles, not in deployer_roles, and
#     nothing in terraform/ grants it (tests/unit/scripts/test_iam_refusal_probe.py
#     holds all three), so the condition must refuse it;
#   * if the condition is broken and the grant lands, it gives the deployer
#     nothing it does not already hold. Its six permissions, read with
#     `gcloud iam roles describe roles/browser` on 2026-09-25, are
#     resourcemanager.{projects.get, projects.getIamPolicy, projects.list,
#     folders.get, folders.list, organizations.get}; projectIamAdmin already
#     carries the project reads, and the project has no organisation or folder.
# It is a constant, not a workflow input, so a dispatcher cannot aim the probe
# at roles/owner.
#
# THE VERDICT, per subcommand:
#
#   preflight  READ-ONLY. Refuses to go on unless the verdict can only be the
#              condition's: the conditioned projectIamAdmin binding is live and
#              the unconditioned one is gone, the condition does not list
#              PROBE_ROLE, the deployer holds no PROBE_ROLE binding already (so
#              the revert can never remove one this run did not make), and no
#              OTHER role the deployer holds carries
#              resourcemanager.projects.setIamPolicy (a custom role it cannot
#              read is counted and reported, not a stop; see step 3 below).
#              Sets `ready=true` and `unread=<n>`.
#   grant      The one write. PASSES only on a refusal (see `classify`). A
#              success fails the step and the revert takes it off.
#   revert     Runs on every path after preflight (the workflow gates it on
#              `always()`). Reads the policy; if PROBE_ROLE is on the deployer,
#              removes exactly that binding (--condition=None), reads again to
#              confirm it is gone, and FAILS saying #68 must be reopened.
#   summary    What the run means, in the run summary.
#   classify   OFFLINE. The refusal test on its own, for the unit tests.
#
# NOTHING HERE PRINTS THE PROJECT POLICY. It names every member of a project
# shared with another team, and this repository's Actions logs are public. The
# policy goes to a file under RUNNER_TEMP, is read with jq for counts and role
# names, and is deleted. Every gcloud error that IS printed goes through
# `redact` (scripts/lib/common.sh) first. No access token is ever requested:
# the workflow's auth step sets no token_format, and nothing here calls
# `gcloud auth print-access-token`.
#
# Usage: scripts/iam-refusal-probe.sh preflight|grant|revert|summary
#        scripts/iam-refusal-probe.sh classify EXIT_CODE STDERR_FILE

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# The role asked for. See "WHY roles/browser" above before changing it.
PROBE_ROLE="roles/browser"
# The deployer grant whose condition is under test.
SCOPED_ROLE="roles/resourcemanager.projectIamAdmin"
# The permission a project policy write is checked against. A second role
# carrying it would authorise the grant with no condition evaluated.
SET_PERMISSION="resourcemanager.projects.setIamPolicy"

WORK=""
cleanup() { [[ -n "${WORK}" ]] && rm -rf "${WORK}"; return 0; }
trap cleanup EXIT

workdir() {
  if [[ -z "${WORK}" ]]; then
    WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/iam-refusal-probe.XXXXXX")"
  fi
}

in_actions() { [[ "${GITHUB_ACTIONS:-}" == "true" ]]; }

# A workflow annotation, so the verdict is on the run page and not only in a log.
annotate() {
  local level="$1" title="$2" message="$3"
  if in_actions; then
    printf '::%s title=%s::%s\n' "${level}" "${title}" "${message}"
  fi
}

set_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s=%s\n' "$1" "$2" >>"${GITHUB_OUTPUT}"
  fi
  dim "output ${1}=${2}"
}

step_summary() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$@" >>"${GITHUB_STEP_SUMMARY}"
  fi
  return 0
}

# Print a captured gcloud error, masked, indented, at most eight lines. `sed -n`
# reads its whole input, so nothing upstream dies of SIGPIPE under pipefail.
show_error() {
  redact <"$1" | sed -n '1,8p' | sed 's/^/     /' >&2
}

deployer_member() {
  [[ -n "${DEPLOYER_SA:-}" ]] \
    || die "DEPLOYER_SA is empty: the workflow sets it from vars.GCP_DEPLOY_SA, the account the auth step signed in as. Nothing was attempted."
  printf 'serviceAccount:%s' "${DEPLOYER_SA}"
}

# read_policy FILE -- the project policy (gcloud requests version 3, so
# conditions come back) into FILE, never to the log.
read_policy() {
  local out="$1" errfile rc=0
  workdir
  errfile="${WORK}/get-iam-policy.err"
  gcloud projects get-iam-policy "${PROJECT_ID}" --format=json >"${out}" 2>"${errfile}" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    err "could not read the project policy of ${PROJECT_ID} (gcloud exit ${rc}):"
    show_error "${errfile}"
    return 1
  fi
  if ! jq -e '.bindings | type == "array"' "${out}" >/dev/null 2>&1; then
    err "the project policy read back has no bindings array; refusing to reason about it"
    return 1
  fi
}

# bindings_for POLICY ROLE any|unconditioned|conditioned -- how many bindings of
# ROLE name the deployer.
bindings_for() {
  local policy="$1" role="$2" which="$3" member
  member="$(deployer_member)"
  jq -r --arg m "${member}" --arg r "${role}" --arg w "${which}" '
    [ .bindings[]
      | select(.role == $r and any((.members // [])[]; . == $m))
      | select($w == "any"
               or ($w == "unconditioned" and .condition == null)
               or ($w == "conditioned" and .condition != null)) ]
    | length' "${policy}"
}

# ---------------------------------------------------------------------------
# classify EXIT_CODE STDERR_FILE  ->  granted | refused | error
#
# `refused` is the ONLY verdict that passes, and it needs all of:
#
#   * gcloud exited non-zero;
#   * the error is a permission denial ON THIS PROJECT'S setIamPolicy. gcloud
#     does not print the canonical status. Read in the Cloud SDK
#     (googlecloudsdk/api_lib/util/exceptions.py, HttpErrorPayload.
#     _MakeDescription), an HTTP 403 whose URL names a resource renders as
#
#       [<account>] does not have permission to access projects instance
#       [<project>:setIamPolicy] (or it may not exist): <server message>
#
#     and that sentence is produced for status 403 and no other. HTTP 403 is
#     the one code Google's error model maps to PERMISSION_DENIED (AIP-193).
#     The method is in the sentence because the URL is
#     `v1/projects/<project>:setIamPolicy`, so a 403 on the policy READ that
#     add-iam-policy-binding makes first says `:getIamPolicy]` and is not a
#     refusal of the grant. A gcloud that prints the status literally is also
#     accepted, as `PERMISSION_DENIED` together with `<project>:setIamPolicy`;
#   * nothing in the error says the 403 came from somewhere other than an IAM
#     decision: a disabled API or billing, or a VPC Service Controls perimeter
#     (all three also 403, also PERMISSION_DENIED, and none the condition), or
#     an expired or refused credential.
#
# Everything else -- a credential WIF would not exchange, a network error, a
# 409, a 400 from an organisation policy, gcloud's own refusal to write without
# --condition, an empty stderr, a new wording in a later gcloud -- is `error`,
# and the step FAILS showing the redacted error. A wording change therefore
# costs a re-run after a one-line fix here; it can never read as a pass.
# ---------------------------------------------------------------------------
classify() {
  local rc="$1" file="$2" text
  case "${rc}" in
    ''|*[!0-9]*) printf 'error'; return 0 ;;
  esac
  if [[ "${rc}" -eq 0 ]]; then
    printf 'granted'
    return 0
  fi
  text="$(cat -- "${file}" 2>/dev/null || true)"
  if [[ -z "${text}" ]] || gcloud_auth_failure "${text}"; then
    printf 'error'
    return 0
  fi
  case "${text}" in
    *SERVICE_DISABLED*|*"has not been used in project"*|*"it is disabled"*) printf 'error'; return 0 ;;
    *BILLING_DISABLED*|*"billing account"*) printf 'error'; return 0 ;;
    *vpcServiceControls*|*"organization's policy"*) printf 'error'; return 0 ;;
    *"refreshing your current auth tokens"*|*unauthorized_client*) printf 'error'; return 0 ;;
  esac
  local denial="does not have permission to access projects instance ["
  case "${text}" in
    *"${denial}"*) printf 'refused'; return 0 ;;
  esac
  case "${text}" in
    *PERMISSION_DENIED*)
      case "${text}" in
        *"${PROJECT_ID}:setIamPolicy"*) printf 'refused'; return 0 ;;
      esac
      ;;
  esac
  printf 'error'
}

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
cmd_preflight() {
  require_cmd gcloud jq
  local member policy wide scoped expr title existing roles_file role describe carries n_roles=0 n_unread=0
  member="$(deployer_member)"
  workdir
  policy="${WORK}/policy.json"

  step "Preflight (read-only): can this run's verdict only be the condition's?"
  info "project ${PROJECT_ID}; probe role ${PROBE_ROLE}; the grant under test: ${SCOPED_ROLE}"

  read_policy "${policy}" || { annotate error "probe not run" "could not read the project policy"; die "nothing was attempted"; }

  # 1. The scoped grant is live, alone. With the unconditioned binding still
  #    present, the conditioned one decides nothing ("Conditional role bindings
  #    do not override role bindings with no conditions") and a grant would
  #    SUCCEED for a reason that says nothing about the condition.
  wide="$(bindings_for "${policy}" "${SCOPED_ROLE}" unconditioned)"
  scoped="$(bindings_for "${policy}" "${SCOPED_ROLE}" conditioned)"
  if [[ "${wide}" != "0" ]]; then
    annotate error "probe not run" "the deployer still holds ${SCOPED_ROLE} unconditioned"
    die "the deployer still holds ${SCOPED_ROLE} with NO condition: PR #73's targeted bootstrap apply (step 3 of #80) has not landed, or has been reverted. The condition is not in force, so there is nothing to probe. Nothing was attempted."
  fi
  if [[ "${scoped}" != "1" ]]; then
    annotate error "probe not run" "expected one conditioned ${SCOPED_ROLE} binding, found ${scoped}"
    die "expected exactly one conditioned ${SCOPED_ROLE} binding on the deployer and found ${scoped}. Nothing was attempted."
  fi
  expr="$(jq -r --arg m "${member}" --arg r "${SCOPED_ROLE}" \
    '.bindings[] | select(.role == $r and .condition != null and any((.members // [])[]; . == $m)) | .condition.expression' \
    "${policy}")"
  title="$(jq -r --arg m "${member}" --arg r "${SCOPED_ROLE}" \
    '.bindings[] | select(.role == $r and .condition != null and any((.members // [])[]; . == $m)) | .condition.title // ""' \
    "${policy}")"
  case "${expr}" in
    *'iam.googleapis.com/modifiedGrantsByRole'*hasOnly*) ;;
    *)
      annotate error "probe not run" "the live condition is not the modifiedGrantsByRole one"
      die "the conditioned ${SCOPED_ROLE} binding (title \"${title}\") does not test modifiedGrantsByRole with hasOnly, so it is not the condition #68 asks about. Nothing was attempted."
      ;;
  esac
  case "${expr}" in
    *"\"${PROBE_ROLE}\""*)
      annotate error "probe not run" "the live condition lists ${PROBE_ROLE}"
      die "the live condition lists ${PROBE_ROLE}, so a grant of it would be admitted by design and prove nothing. Nothing was attempted."
      ;;
  esac
  ok "the deployer's ${SCOPED_ROLE} is conditioned (\"${title}\"), with no unconditioned twin, and the condition does not list ${PROBE_ROLE}"

  # 2. The revert removes PROBE_ROLE from the deployer. It must never remove a
  #    binding this run did not make, so none may exist before it.
  existing="$(bindings_for "${policy}" "${PROBE_ROLE}" any)"
  if [[ "${existing}" != "0" ]]; then
    annotate error "probe not run" "the deployer already holds ${PROBE_ROLE}"
    die "the deployer already holds ${PROBE_ROLE} (${existing} binding(s)). This probe would not be able to tell its own grant from that one, and its revert would remove it. Nothing was attempted."
  fi
  ok "the deployer holds no ${PROBE_ROLE} binding"

  # 3. No other role the deployer holds carries the permission a project policy
  #    write is checked against. If one did, a grant would succeed through it
  #    with the condition never evaluated: #68's goal is already unmet (#79 is
  #    one way it happens), and there is no reason to write to the policy to
  #    learn it. Direct bindings only: group memberships and inherited policy
  #    are not visible here, and the project has no organisation or folder
  #    above it.
  #
  #    A CUSTOM role the deployer cannot read is a caveat, not a stop. Reading
  #    one needs iam.roles.get, which leaves the deployer with roleAdmin at
  #    step 4 of #80 (PR #150). A REFUSAL is still proof, because no role
  #    admitted the write; only a GRANT's attribution is weakened, and a grant
  #    fails the run and reopens #68 whichever role let it through. So it is
  #    counted, and said at the grant. A PREDEFINED role is always readable, so
  #    failing to read one is a stop.
  roles_file="${WORK}/deployer-roles.txt"
  jq -r --arg m "${member}" --arg r "${SCOPED_ROLE}" \
    '[.bindings[] | select(any((.members // [])[]; . == $m)) | .role | select(. != $r)] | unique | .[]' \
    "${policy}" >"${roles_file}"
  # gcloud reads from /dev/null inside the loop: a command that reads stdin
  # would otherwise eat the rest of the role list and the loop would end early.
  while IFS= read -r role; do
    [[ -n "${role}" ]] || continue
    n_roles=$((n_roles + 1))
    describe="${WORK}/role-${n_roles}.json"
    case "${role}" in
      roles/*)
        gcloud iam roles describe "${role}" --format=json </dev/null >"${describe}" 2>"${describe}.err" \
          || { show_error "${describe}.err"; annotate error "probe not run" "could not read ${role}"; die "could not read the predefined role ${role} to check it for ${SET_PERMISSION}. Nothing was attempted."; }
        ;;
      "projects/${PROJECT_ID}/roles/"*)
        if ! gcloud iam roles describe "${role##*/}" --project="${PROJECT_ID}" --format=json </dev/null >"${describe}" 2>"${describe}.err"; then
          show_error "${describe}.err"
          warn "could not read the custom role ${role}. If the grant succeeds, it may be through this role rather than the condition."
          n_unread=$((n_unread + 1))
          continue
        fi
        ;;
      *)
        annotate error "probe not run" "unrecognised role ${role}"
        die "the deployer holds ${role}, which this probe cannot read. Nothing was attempted."
        ;;
    esac
    # A describe that did not return a role is "could not check", never "does
    # not carry it".
    carries="$(jq -r --arg p "${SET_PERMISSION}" \
      'if (type == "object" and has("name")) then (any((.includedPermissions // [])[]; . == $p) | tostring) else "unreadable" end' \
      "${describe}" 2>/dev/null)" || carries="unreadable"
    case "${carries}" in
      false) ;;
      true)
        annotate error "probe not run" "${role} also carries ${SET_PERMISSION}"
        die "the deployer also holds ${role}, which carries ${SET_PERMISSION}: a grant would succeed through it with the condition never evaluated (#79 describes one way this happens), so #68's goal is already unmet. Nothing was attempted."
        ;;
      *)
        annotate error "probe not run" "could not read the permissions of ${role}"
        die "gcloud returned no readable role for ${role}, so whether it carries ${SET_PERMISSION} is unknown. Nothing was attempted."
        ;;
    esac
  done <"${roles_file}"
  if [[ "${n_roles}" -eq 0 ]]; then
    annotate error "probe not run" "read no other roles for the deployer"
    die "found no role on the deployer besides ${SCOPED_ROLE}; the release could not run like that, so the policy read is not what it seems. Nothing was attempted."
  fi
  ok "checked $((n_roles - n_unread)) of ${n_roles} other role(s) the deployer holds: none carries ${SET_PERMISSION}"

  set_output unread "${n_unread}"
  set_output ready true
  if [[ "${n_unread}" -eq 0 ]]; then
    step_summary "* preflight: the condition is live and alone, ${PROBE_ROLE} is not on the deployer, and none of its ${n_roles} other roles carries \`${SET_PERMISSION}\`."
  else
    annotate warning "custom roles unread" "${n_unread} custom role(s) on the deployer could not be read; a refusal is still proof, a grant's cause would be uncertain"
    step_summary "* preflight: the condition is live and alone and ${PROBE_ROLE} is not on the deployer. $((n_roles - n_unread)) of ${n_roles} other roles were read and none carries \`${SET_PERMISSION}\`; **${n_unread} custom role(s) could not be read**, so a grant, if one happened, might be through one of them."
  fi
}

# What a grant means. Preflight's `unread` output arrives as PREFLIGHT_UNREAD:
# with every other role read, only the condition can have admitted it.
admitted() {
  if [[ "${PREFLIGHT_UNREAD:-0}" == "0" ]]; then
    printf 'the condition admitted an unlisted role'
  else
    printf 'an unlisted role was admitted by the condition or by one of the %s custom role(s) preflight could not read' "${PREFLIGHT_UNREAD}"
  fi
}

# ---------------------------------------------------------------------------
# grant
# ---------------------------------------------------------------------------
cmd_grant() {
  require_cmd gcloud jq
  local member policy existing errfile rc=0 verdict
  member="$(deployer_member)"
  workdir
  policy="${WORK}/policy.json"
  errfile="${WORK}/grant.err"

  step "Grant: the deployer asks for ${PROBE_ROLE} on ${PROJECT_ID}"

  # Preflight was a separate step. Look again immediately before the write, so
  # the revert's rule -- anything it finds was made by this run -- holds.
  read_policy "${policy}" || { set_output verdict error; die "could not re-read the policy before the write; nothing was attempted"; }
  existing="$(bindings_for "${policy}" "${PROBE_ROLE}" any)"
  if [[ "${existing}" != "0" ]]; then
    set_output conflict true
    annotate error "probe not run" "${PROBE_ROLE} appeared on the deployer after preflight"
    die "${PROBE_ROLE} appeared on the deployer between preflight and now, and this run did not put it there. Nothing was attempted, and the revert will not remove it."
  fi

  # Recorded BEFORE the call, so a run cancelled mid-call still says a write
  # may have happened.
  set_output attempted true

  # --condition=None: the project policy already carries conditions, and gcloud
  # refuses a conditionless add to such a policy in a non-interactive shell
  # ("prohibited in non-interactive mode"), which would be an `error` here, not
  # a refusal. --format=none and >/dev/null: on success gcloud prints the whole
  # new policy.
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="${member}" --role="${PROBE_ROLE}" --condition=None \
    --quiet 2>"${errfile}" || rc=$?

  verdict="$(classify "${rc}" "${errfile}")"
  set_output verdict "${verdict}"

  case "${verdict}" in
    refused)
      ok "REFUSED (gcloud exit ${rc}), as the condition requires. gcloud said:"
      show_error "${errfile}"
      annotate notice "refused" "the deployer's grant of ${PROBE_ROLE} was refused with PERMISSION_DENIED on setIamPolicy"
      step_summary "* grant: **refused** with PERMISSION_DENIED on \`${PROJECT_ID}:setIamPolicy\` (gcloud exit ${rc})."
      ;;
    granted)
      err "GRANTED. The deployer gave itself ${PROBE_ROLE}, a role the condition does not list: $(admitted) and #68 must be reopened. The revert step removes the binding next."
      annotate error "GRANTED -- reopen #68" "$(admitted) (${PROBE_ROLE}); the revert step removes it"
      step_summary "* grant: **GRANTED** -- $(admitted). #68 must be reopened."
      exit 1
      ;;
    *)
      err "neither a refusal nor a grant (gcloud exit ${rc}). This is NOT a pass. gcloud said:"
      show_error "${errfile}"
      annotate error "inconclusive" "gcloud exit ${rc} was not a PERMISSION_DENIED on setIamPolicy; see the grant step"
      step_summary "* grant: **inconclusive** -- gcloud exit ${rc}, not a PERMISSION_DENIED on setIamPolicy. See the grant step's log."
      exit 1
      ;;
  esac
}

# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------
manual_revert_hint() {
  err "Remove it as the owner, then confirm nothing is left:"
  err "  gcloud projects remove-iam-policy-binding ${PROJECT_ID} --member=$(deployer_member) --role=${PROBE_ROLE} --condition=None --format=none"
  err "  (docs/runbooks/iam-refusal-probe.md, \"If the revert did not finish\")"
}

cmd_revert() {
  require_cmd gcloud jq
  local member policy present conditioned attempt rc errfile
  member="$(deployer_member)"
  workdir
  policy="${WORK}/policy.json"
  errfile="${WORK}/remove.err"

  step "Revert: is ${PROBE_ROLE} on the deployer now?"

  if ! read_policy "${policy}"; then
    manual_revert_hint
    annotate error "revert unverified" "could not read the policy to check for ${PROBE_ROLE}"
    step_summary "* revert: **could not read the policy**, so whether ${PROBE_ROLE} is on the deployer is unknown."
    exit 1
  fi
  present="$(bindings_for "${policy}" "${PROBE_ROLE}" unconditioned)"
  conditioned="$(bindings_for "${policy}" "${PROBE_ROLE}" conditioned)"
  set_output found "${present}"

  if [[ "${conditioned}" != "0" ]]; then
    annotate error "not ours" "a CONDITIONED ${PROBE_ROLE} binding names the deployer"
    step_summary "* revert: a **conditioned** ${PROBE_ROLE} binding names the deployer. This probe only ever adds an unconditioned one, so it is not this run's and was not touched."
    die "a CONDITIONED ${PROBE_ROLE} binding names the deployer. This probe adds only an unconditioned one, so it did not make it and will not remove it."
  fi

  if [[ "${present}" == "0" ]]; then
    if [[ "${GRANT_VERDICT:-}" == "granted" ]]; then
      annotate error "contradiction" "gcloud reported the grant succeeded and the policy has no such binding"
      step_summary "* revert: gcloud reported the grant **succeeded**, and the policy read back has no such binding. That cannot be squared; treat #68 as reopened until it is explained."
      die "gcloud reported the grant succeeded, and the policy read back has no ${PROBE_ROLE} binding on the deployer. That is not a pass."
    fi
    ok "no ${PROBE_ROLE} binding names the deployer: nothing to remove"
    step_summary "* revert: the policy read back after the attempt has no ${PROBE_ROLE} binding on the deployer."
    return 0
  fi

  if [[ "${GRANT_CONFLICT:-}" == "true" ]]; then
    annotate error "not ours" "${PROBE_ROLE} appeared on the deployer after preflight and was not removed"
    step_summary "* revert: ${PROBE_ROLE} appeared on the deployer after preflight, before this run wrote anything. Not removed."
    die "${PROBE_ROLE} is on the deployer, and the grant step found it there before writing. This run did not make it and will not remove it."
  fi

  err "${PROBE_ROLE} IS on the deployer. Preflight showed it was not before this run, so this run made it. Removing exactly that binding."
  for attempt in 1 2 3; do
    rc=0
    gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
      --member="${member}" --role="${PROBE_ROLE}" \
      --quiet --format=none >/dev/null 2>"${errfile}" || rc=$?
    if [[ "${rc}" -eq 0 ]]; then
      break
    fi
    warn "remove attempt ${attempt} failed (gcloud exit ${rc}):"
    show_error "${errfile}"
    if [[ "${attempt}" -lt 3 ]]; then
      sleep 10
    fi
  done

  if read_policy "${policy}" && [[ "$(bindings_for "${policy}" "${PROBE_ROLE}" any)" == "0" ]]; then
    set_output removed true
    annotate error "GRANTED -- reopen #68" "$(admitted) (${PROBE_ROLE}); the binding was removed and the policy read back confirms it is gone"
    step_summary "* revert: the binding was **removed**, and the policy read back confirms it is gone. $(admitted): **#68 must be reopened.**"
    die "$(admitted) (${PROBE_ROLE}) and #68 must be reopened. The binding has been removed, and the policy read back confirms it is gone."
  fi

  set_output removed false
  annotate error "REVERT FAILED" "${PROBE_ROLE} may still be on the deployer; remove it by hand"
  step_summary "* revert: **FAILED**. ${PROBE_ROLE} may still be on the deployer. Remove it by hand (docs/runbooks/iam-refusal-probe.md). #68 must be reopened."
  manual_revert_hint
  die "REVERT FAILED: ${PROBE_ROLE} may still be on the deployer, and $(admitted): #68 must be reopened."
}

# ---------------------------------------------------------------------------
# summary -- one line on what the run means, from the step outcomes.
# ---------------------------------------------------------------------------
cmd_summary() {
  local meaning landed="no"
  # The revert's `found` is how many unconditioned PROBE_ROLE bindings named the
  # deployer when it looked: a grant that landed even though gcloud failed.
  case "${REVERT_FOUND:-}" in
    ''|0) ;;
    *) landed="yes" ;;
  esac
  case "${PREFLIGHT_OUTCOME:-}/${GRANT_VERDICT:-}/${REVERT_OUTCOME:-}/${landed}" in
    success/refused/success/no)
      meaning="**PASS.** The deployer asked for \`${PROBE_ROLE}\`, IAM refused it with PERMISSION_DENIED, and the policy read back afterwards has no such binding. One unlisted role is refused under the modifiedGrantsByRole condition. This does not show the roleAdmin route closed (#79)." ;;
    success/granted/*|success/*/*/yes)
      meaning="**FAIL: $(admitted). Reopen #68.** Read the revert step: it says whether the binding was removed." ;;
    success/refused/*)
      meaning="**FAIL.** The grant was refused, and the revert step could not confirm the policy is clean. Read the revert step." ;;
    success/*)
      meaning="**FAIL: inconclusive.** The grant step did not end in a refusal or a grant (verdict \`${GRANT_VERDICT:-none}\`). Read the grant and revert steps." ;;
    *)
      meaning="**NOT RUN.** Preflight did not pass (\`${PREFLIGHT_OUTCOME:-not reached}\`), so nothing was written. Read the first red step." ;;
  esac
  step_summary "" "### IAM refusal probe (#68)" "" "${meaning}"
  printf '%s\n' "${meaning}" >&2
}

main() {
  local cmd="${1:-}"
  shift || true
  case "${cmd}" in
    preflight) cmd_preflight ;;
    grant)     cmd_grant ;;
    revert)    cmd_revert ;;
    summary)   cmd_summary ;;
    classify)
      [[ $# -eq 2 ]] || die "usage: $(basename "$0") classify EXIT_CODE STDERR_FILE"
      classify "$1" "$2"
      printf '\n'
      ;;
    *) die "usage: $(basename "$0") preflight|grant|revert|summary|classify EXIT_CODE STDERR_FILE" ;;
  esac
}

main "$@"
