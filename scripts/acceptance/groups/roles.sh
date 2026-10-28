#!/usr/bin/env bash
# Acceptance group `roles` (ROLES-ACCEPT; docs/design/workspace-roles.md §6
# row 6, #1518): the workspace-role gate, measured against the deployment
# rather than the fakes the unit tests use.
#
#   narrow    a platform admin sets the test workspace's DEFAULT role to viewer
#   submit    swarm-verify, now a viewer there, is 403 `role_member_required`
#             on POST /v1/tasks
#   read      and 200 on GET /v1/tasks
#   audit     the narrowing and its revocation are both in GET
#             /v1/workspace/roles `audit`
#   acted     the admin's reach into the workspace (it is not in its group) is
#             recorded there as `platform_admin_acted`
#   personal  the same admin selecting another identity's `u-` workspace is
#             403 `tenant_not_member`: the reach covers group workspaces only
#
# WHY THE DEFAULT, NOT A PERSON'S GRANT. The design names "an identity granted
# viewer". Every identity this suite can act as is a service account, and a
# role write naming one is 403 `role_person_required` (roles.subject_for_write:
# a role is held by a person). A service account in a group workspace holds
# that workspace's default role, so narrowing the default narrows it -- which
# is why the test workspace must be one nothing else works in
# (scripts/acceptance/config.sh), and why the revocation is the default put
# back to member. A person's grant needs a person's credential this suite
# does not have; that is the owner's question in the pull request.
#
# THE TWO IDENTITIES. The viewer is swarm-verify itself (its own ID token, from
# the metadata server). The admin is SWARM_ACCEPTANCE_ROLES_ADMIN_SA, whose ID
# token swarm-verify mints (lib.sh's acc_mint_id_token): swarm-verify is not a
# platform admin and must not become one (the owner reversed that on
# 2026-09-24; verify-remote.sh's header).
#
# Nothing here submits work that runs: the one submission is the refused
# one, and a submission that is wrongly accepted is cancelled at exit.
#
# accept.yml runs it in the tenant-isolation job, as `verify-remote.sh
# acceptance/roles` (scripts/acceptance/roles.sh), report-only until the
# repository variable ROLES_PROBE_REQUIRED is true.

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

#: The API's own words (swarm_api.errors.RoleMemberRequired,
#: swarm_api.auth.TenantNotMember, swarm_api.roles.AUDIT_DEFAULT_SET and
#: AUDIT_PLATFORM_ADMIN_ACTED). tests/unit/scripts/test_acceptance_roles.py
#: holds each one to the Python, so a rename fails there, not live.
ROLES_CODE_MEMBER_REQUIRED="role_member_required"
ROLES_CODE_NOT_MEMBER="tenant_not_member"
ROLES_AUDIT_DEFAULT_SET="workspace_default_role_set"
ROLES_AUDIT_ACTED="platform_admin_acted"

#: How long the viewer waits to see its narrowed role. swarm-api caches a role
#: for ROLE_CACHE_SECONDS (15 by default, settings.role_cache_seconds), and the
#: submit gate reads through that cache.
ROLES_SETTLE_SECONDS="${SWARM_ACCEPTANCE_ROLES_SETTLE_SECONDS:-60}"
#: How far before its own start the probe looks in the audit: the API's clock
#: and this job's are not the same clock.
ROLES_CLOCK_SLACK_SECONDS=120

roles_checks() {
  cat <<'EOF2'
roles: narrow: a platform admin sets the test workspace's default role to viewer
roles: submit: a viewer's POST /v1/tasks is 403 role_member_required
roles: read: a viewer's GET /v1/tasks is 200
roles: audit: the narrowing and its revocation are both in GET /v1/workspace/roles audit
roles: acted: the platform admin's reach into the test workspace is recorded as platform_admin_acted
roles: personal: the platform admin selecting another identity's u- workspace is 403 tenant_not_member
EOF2
}

_roles_title() {
  roles_checks | awk -v n="roles: $1:" 'index($0, n) == 1 { print; exit }'
}

# roles_config_problem -> nothing when the probe can run; otherwise one
# sentence saying what is missing. Always returns 0.
roles_config_problem() {
  if [[ -z "${ACC_ROLES_TENANT}" ]]; then
    echo "SWARM_ACCEPTANCE_ROLES_TENANT is unset: name a group workspace whose group holds swarm-verify and that nothing else works in"
  elif [[ ! "${ACC_ROLES_TENANT}" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "SWARM_ACCEPTANCE_ROLES_TENANT is not a tenant id (lower-case letters, digits and hyphens)"
  elif [[ "${ACC_ROLES_TENANT}" == u-* ]]; then
    echo "SWARM_ACCEPTANCE_ROLES_TENANT is ${ACC_ROLES_TENANT}, a personal workspace: the probe needs a group workspace (a u- workspace's person is its fixed admin)"
  elif [[ "${ACC_ROLES_TENANT}" == "${ACC_TENANT}" ]]; then
    echo "SWARM_ACCEPTANCE_ROLES_TENANT is the acceptance tenant (${ACC_TENANT}), where the other groups submit at once: narrowing it would refuse them"
  elif [[ -z "${ACC_ROLES_OTHER_PERSONAL}" ]]; then
    echo "SWARM_ACCEPTANCE_ROLES_OTHER_PERSONAL is unset: name another identity's personal workspace (u-<someone>)"
  elif [[ ! "${ACC_ROLES_OTHER_PERSONAL}" =~ ^u-[a-z0-9-]+$ ]]; then
    echo "SWARM_ACCEPTANCE_ROLES_OTHER_PERSONAL is ${ACC_ROLES_OTHER_PERSONAL}, not a u- personal workspace"
  elif [[ ! "${ACC_ROLES_ADMIN_SA}" =~ ^[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$ ]]; then
    echo "SWARM_ACCEPTANCE_ROLES_ADMIN_SA is unset or not a service account's email: name a platform admin's service account swarm-verify may mint an ID token for"
  elif api_is_front_door; then
    echo "the API is reached through its front door (IAP), which takes no minted ID token: run the probe inside the VPC (verify-remote.sh acceptance/roles)"
  fi
  return 0
}

# roles_judge CHECK STATUS BODY_FILE [SINCE ADMIN_EMAIL] -> one line,
# "PASS<TAB>reason" or "FAIL<TAB>reason". Pure: it reads the response it is
# given and nothing else, so the unit test runs it over the API's own codes.
# SINCE (YYYY-MM-DDTHH:MM:SS, UTC) bounds the audit rows that count.
roles_judge() {
  local check="$1" status="$2" file="$3" since="${4:-}" admin="${5:-}"
  local code got want verdict="FAIL" reason
  code="$(jq -r 'if type == "object" then (.code // "") else "" end' <"${file}" 2>/dev/null || true)"
  case "${check}" in
    narrow|revoke)
      want="viewer"
      [[ "${check}" == "narrow" ]] || want="member"
      got="$(jq -r 'if type == "object" then (.default_role // "") else "" end' <"${file}" 2>/dev/null || true)"
      if [[ "${status}" == "200" && "${got}" == "${want}" ]]; then
        verdict="PASS" reason="the default is ${want}"
      else
        reason="HTTP ${status}${code:+ ${code}}; expected 200 with default_role ${want}"
      fi
      ;;
    submit)
      if [[ "${status}" == "403" && "${code}" == "${ROLES_CODE_MEMBER_REQUIRED}" ]]; then
        verdict="PASS" reason="403 ${code}"
      elif [[ "${status}" =~ ^2 ]]; then
        reason="HTTP ${status}: a viewer's submission was accepted"
      else
        reason="HTTP ${status}${code:+ ${code}}; expected 403 ${ROLES_CODE_MEMBER_REQUIRED}"
      fi
      ;;
    read)
      if [[ "${status}" == "200" ]]; then
        verdict="PASS" reason="200"
      else
        reason="HTTP ${status}${code:+ ${code}}; expected 200: a viewer reads"
      fi
      ;;
    personal)
      if [[ "${status}" == "403" && "${code}" == "${ROLES_CODE_NOT_MEMBER}" ]]; then
        verdict="PASS" reason="403 ${code}"
      elif [[ "${status}" =~ ^2 ]]; then
        reason="HTTP ${status}: a platform admin reached another identity's personal workspace"
      else
        reason="HTTP ${status}${code:+ ${code}}; expected 403 ${ROLES_CODE_NOT_MEMBER}"
      fi
      ;;
    audit)
      if [[ "${status}" != "200" ]]; then
        reason="GET /v1/workspace/roles: HTTP ${status}${code:+ ${code}}"
      else
        # The newest narrowing to viewer since SINCE, and a revocation (viewer
        # back to member) no older than it. Rows are newest first; `at` is an
        # ISO time, compared to the second.
        reason="$(jq -r --arg a "${ROLES_AUDIT_DEFAULT_SET}" --arg s "${since}" '
          [(.audit // [])[] | select(.action == $a)
            | select(((.at // "") | tostring | .[0:19]) >= $s)] as $rows
          | ([$rows[] | select(.detail.to == "viewer") | .at | tostring] | max) as $n
          | ([$rows[] | select(.detail.from == "viewer" and .detail.to == "member") | .at | tostring] | max) as $r
          | if $n == null then "no narrowing to viewer since \($s)"
            elif $r == null then "the narrowing at \($n) has no revocation after it"
            elif $r >= $n then "ok narrowed at \($n), revoked at \($r)"
            else "the newest revocation (\($r)) is older than the narrowing (\($n))" end' \
          <"${file}" 2>/dev/null || echo "the audit could not be read")"
        if [[ "${reason}" == ok\ * ]]; then
          verdict="PASS" reason="${reason#ok }"
        fi
      fi
      ;;
    acted)
      if [[ "${status}" != "200" ]]; then
        reason="GET /v1/workspace/roles: HTTP ${status}${code:+ ${code}}"
      elif jq -e --arg a "${ROLES_AUDIT_ACTED}" --arg b "${admin}" --arg s "${since}" '
             any((.audit // [])[]; .action == $a and .by == $b
                 and (((.at // "") | tostring | .[0:19]) >= $s))' <"${file}" >/dev/null 2>&1; then
        verdict="PASS" reason="${ROLES_AUDIT_ACTED} by ${admin}"
      else
        reason="no ${ROLES_AUDIT_ACTED} row by ${admin} since ${since}"
      fi
      ;;
    *) reason="unknown check ${check}" ;;
  esac
  printf '%s\t%s\n' "${verdict}" "${reason}"
}

# _roles_verdict CHECK STATUS FILE [SINCE ADMIN] -> acc_pass or acc_fail, and
# returns 0 on PASS.
_roles_verdict() {
  local line verdict reason
  line="$(roles_judge "$@")"
  verdict="${line%%$'\t'*}"
  reason="${line#*$'\t'}"
  if [[ "${verdict}" == "PASS" ]]; then
    acc_pass "${reason}"
    return 0
  fi
  acc_fail "${reason}"
  return 1
}

# _roles_default_body ROLE -> the PUT /v1/workspace/roles/default body.
_roles_default_body() {
  jq -nc --arg r "$1" --arg why "acceptance ${ACC_RUN_ID} ROLES-ACCEPT" '{role: $r, reason: $why, via: "cli"}'
}

# _roles_utc EPOCH -> YYYY-MM-DDTHH:MM:SS. GNU date in the job, BSD on a Mac.
_roles_utc() {
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -r "$1" +%Y-%m-%dT%H:%M:%S
}

run_roles() {
  step "Acceptance: roles (workspace ${ACC_ROLES_TENANT:-unset})"
  local problem title dir admin since deadline role id narrowed=0 revoke_line
  ACC_CHECK="roles"
  problem="$(roles_config_problem)"
  if [[ -n "${problem}" ]]; then
    while IFS= read -r title; do
      acc_check "${title}"
      acc_fail "not measured: ${problem}"
    done < <(roles_checks)
    return 0
  fi

  dir="${ACC_WORK}/roles"
  mkdir -p "${dir}"
  admin="${dir}/admin.token"
  since="$(_roles_utc $(( $(date -u +%s) - ROLES_CLOCK_SLACK_SECONDS )))"
  if ! acc_mint_id_token "${ACC_ROLES_ADMIN_SA}" "${admin}"; then
    while IFS= read -r title; do
      acc_check "${title}"
      acc_fail "not measured: no ID token for the platform admin ${ACC_ROLES_ADMIN_SA} (above)"
    done < <(roles_checks)
    return 0
  fi

  # A run that stopped between its narrowing and its revocation left the
  # default at viewer: put it back first, so this run's narrowing is a write
  # of its own. 409 role_unchanged is the usual answer and is fine.
  acc_api_as "${admin}" "${ACC_ROLES_TENANT}" PUT /workspace/roles/default "${dir}/reset.json" \
    "$(_roles_default_body member)"

  acc_check "$(_roles_title narrow)"
  acc_api_as "${admin}" "${ACC_ROLES_TENANT}" PUT /workspace/roles/default "${dir}/narrow.json" \
    "$(_roles_default_body viewer)"
  if _roles_verdict narrow "${ACC_API_STATUS}" "${dir}/narrow.json"; then
    narrowed=1
    # The viewer sees itself as one once the role cache has turned over.
    deadline=$(( SECONDS + ROLES_SETTLE_SECONDS ))
    while :; do
      acc_api_as "" "${ACC_ROLES_TENANT}" GET /workspace/roles "${dir}/me.json"
      role="$(jq -r '.me.role // empty' <"${dir}/me.json" 2>/dev/null || true)"
      [[ "${role}" != "viewer" && ${SECONDS} -lt ${deadline} ]] || break
      sleep 5
    done
    t_info "swarm-verify's role in ${ACC_ROLES_TENANT}: ${role:-unread}"
  fi

  acc_check "$(_roles_title submit)"
  if [[ "${narrowed}" -eq 1 ]]; then
    acc_api_as "" "${ACC_ROLES_TENANT}" POST /tasks "${dir}/submit.json" \
      "$(jq -nc --arg p "acceptance ${ACC_RUN_ID} roles: a viewer's submission, to be refused" \
          --argjson x "$(acc_extra)" '{runner_profile: "mock", input: {prompt: $p}} + $x')"
    if [[ "${ACC_API_STATUS}" =~ ^2 ]]; then
      id="$(jq -r '.id // .task_id // empty' <"${dir}/submit.json" 2>/dev/null || true)"
      [[ -z "${id}" ]] || SWARM_API_TENANT="${ACC_ROLES_TENANT}" acc_record_task "${id}"
    fi
    _roles_verdict submit "${ACC_API_STATUS}" "${dir}/submit.json" || true
  else
    acc_fail "not measured: the narrowing failed"
  fi

  acc_check "$(_roles_title read)"
  if [[ "${narrowed}" -eq 1 ]]; then
    acc_api_as "" "${ACC_ROLES_TENANT}" GET "/tasks?limit=1" "${dir}/read.json"
    _roles_verdict read "${ACC_API_STATUS}" "${dir}/read.json" || true
  else
    acc_fail "not measured: the narrowing failed"
  fi

  # The revocation, before anything else is read: the workspace's default is
  # put back whatever the checks above concluded.
  acc_api_as "${admin}" "${ACC_ROLES_TENANT}" PUT /workspace/roles/default "${dir}/revoke.json" \
    "$(_roles_default_body member)"
  revoke_line="$(roles_judge revoke "${ACC_API_STATUS}" "${dir}/revoke.json")"
  acc_api_as "" "${ACC_ROLES_TENANT}" GET /workspace/roles "${dir}/roles.json"
  local roles_status="${ACC_API_STATUS}"

  acc_check "$(_roles_title audit)"
  if [[ "${narrowed}" -ne 1 ]]; then
    acc_fail "not measured: the narrowing failed"
  elif [[ "${revoke_line}" != PASS* ]]; then
    acc_fail "the revocation failed (${revoke_line#*$'\t'}): ${ACC_ROLES_TENANT}'s default may still be viewer"
  else
    _roles_verdict audit "${roles_status}" "${dir}/roles.json" "${since}" || true
  fi

  acc_check "$(_roles_title acted)"
  _roles_verdict acted "${roles_status}" "${dir}/roles.json" "${since}" "${ACC_ROLES_ADMIN_SA}" || true

  acc_check "$(_roles_title personal)"
  acc_api_as "${admin}" "${ACC_ROLES_OTHER_PERSONAL}" GET "/tasks?limit=1" "${dir}/personal.json"
  _roles_verdict personal "${ACC_API_STATUS}" "${dir}/personal.json" || true

  rm -f "${admin}"
}
