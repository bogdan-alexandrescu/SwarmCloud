#!/usr/bin/env bash
# Acceptance probe `roles` (ROLES-ACCEPT; docs/design/workspace-roles.md §6
# row 6 and §8; #1518): what dev's workspace roles do, measured live with two
# identities that are not swarm-verify's own.
#
#   1. The platform admin (ACC_ROLES_ADMIN_SA), NOT in the test workspace's
#      group, selects it through the reach and sets its default role to
#      `viewer`: the grant. A service account cannot hold a person's grant
#      (role_person_required), so the default is the one grant that reaches
#      the viewer identity, and the test workspace is the probe's own.
#   2. The viewer (ACC_ROLES_VIEWER_SA), a member of the group: GET
#      /v1/workspace/roles names it and says `viewer`; POST /v1/tasks is 403
#      role_member_required and creates nothing; GET /v1/tasks is 200.
#   3. The admin sets the default back to `member`: the revocation. Always,
#      once the grant was made, and again on exit if a crash skipped it.
#   4. GET /v1/workspace/roles `audit` holds both rows this run wrote (matched
#      by their reason, which names the run) and a `platform_admin_acted` row
#      written since the probe started.
#   5. The admin selecting another identity's `u-` workspace is 403
#      tenant_not_member: the reach never covers a personal workspace.
#
# The judge, roles_judge, reads only the status and the API's code or audit
# action, and never prints a response body: tests/unit/scripts/
# test_acceptance_roles.py holds it to swarm_api's own codes. Until the
# owner has provisioned the identities and the workspace (config.sh), every
# check SKIPs and says which setting is missing; a skip is never a pass.
#
# Run in accept.yml's smoke job as `verify-remote.sh acceptance/groups/roles`
# (BGD, 2026-10-28: the smoke job, not a new group), so this file is also its
# own entry point; sourced, it only defines the probe.

# Sourced in context (common.sh, testlib.sh, lib.sh) or run on its own below;
# checked on its own, shellcheck sees what those set as unassigned.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

#: The codes and audit actions the probe judges by, as swarm_api spells them
#: (errors.RoleMemberRequired, auth.TenantNotMember, roles.AUDIT_*).
ROLES_MEMBER_REQUIRED="role_member_required"
ROLES_NOT_MEMBER="tenant_not_member"
ROLES_DEFAULT_SET="workspace_default_role_set"
ROLES_ADMIN_ACTED="platform_admin_acted"

roles_checks() {
  cat <<'EOF'
roles: viewer-submit: a viewer's POST /v1/tasks in the roles test workspace is 403 role_member_required
roles: viewer-read: a viewer's GET /v1/tasks there is 200
roles: audit: the viewer grant and its revocation are both in GET /v1/workspace/roles audit
roles: reach: a platform admin outside the workspace's group selects it, and its write is recorded platform_admin_acted
roles: personal: the platform admin selecting another identity's u- workspace is 403 tenant_not_member
EOF
}

# roles_settings_problem -> returns 0 and prints nothing when the probe can
# run; otherwise prints, on one line, every setting that is missing or
# refused, and returns 1.
roles_settings_problem() {
  local problems=() tenant="${ACC_ROLES_TENANT:-}" other="${ACC_ROLES_OTHER_WORKSPACE:-}"
  [[ -n "${tenant}" ]] || problems+=("SWARM_ACCEPTANCE_ROLES_TENANT is unset")
  [[ -n "${ACC_ROLES_VIEWER_SA:-}" ]] || problems+=("SWARM_ACCEPTANCE_ROLES_VIEWER_SA is unset")
  [[ -n "${ACC_ROLES_ADMIN_SA:-}" ]] || problems+=("SWARM_ACCEPTANCE_ROLES_ADMIN_SA is unset")
  [[ -n "${other}" ]] || problems+=("SWARM_ACCEPTANCE_ROLES_OTHER_WORKSPACE is unset")
  if [[ -n "${tenant}" ]]; then
    if [[ ! "${tenant}" =~ ^[a-z0-9][a-z0-9-]*$ || "${tenant}" == u-* ]]; then
      problems+=("SWARM_ACCEPTANCE_ROLES_TENANT must name a group workspace")
    elif [[ "${tenant}" == "${ACC_TENANT:-smoke}" || "${tenant}" == "smoke" ]]; then
      problems+=("SWARM_ACCEPTANCE_ROLES_TENANT may not be the acceptance tenant: every other group submits there")
    fi
  fi
  if [[ -n "${other}" && ! "${other}" =~ ^u-[a-z0-9][a-z0-9-]*$ ]]; then
    problems+=("SWARM_ACCEPTANCE_ROLES_OTHER_WORKSPACE must be a personal (u-) workspace")
  fi
  [[ "${#problems[@]}" -gt 0 ]] || return 0
  local IFS=';'
  printf '%s\n' "${problems[*]}"
  return 1
}

# roles_judge CHECK STATUS BODY [EXTRA] -> one line, "PASS<TAB>reason" or
# "FAIL<TAB>reason". EXTRA is this run's grant reason for `audit` and the
# probe's start (epoch seconds) for `reach`. Reads the code or the audit rows
# out of BODY and never echoes BODY itself.
roles_judge() {
  local check="$1" status="$2" body="$3" extra="${4:-}" code verdict
  code="$(jq -r 'if type == "object" then (.code // "") else "" end' <<<"${body}" 2>/dev/null)" || code=""
  [[ "${code}" =~ ^[a-z0-9_]*$ ]] || code="(unreadable)"
  case "${check}" in
    viewer-submit)
      if [[ "${status}" == "403" && "${code}" == "${ROLES_MEMBER_REQUIRED}" ]]; then
        printf 'PASS\t403 %s: the viewer was refused and nothing was submitted\n' "${code}"
      elif [[ "${status}" =~ ^2 ]]; then
        printf 'FAIL\tthe viewer'"'"'s submission was accepted (HTTP %s): the gate is off or the viewer grant did not take\n' "${status}"
      else
        printf 'FAIL\tHTTP %s %s, not 403 %s\n' "${status}" "${code:-(no code)}" "${ROLES_MEMBER_REQUIRED}"
      fi ;;
    viewer-read)
      if [[ "${status}" == "200" ]]; then
        printf 'PASS\t200: the viewer still reads\n'
      else
        printf 'FAIL\tHTTP %s %s, not 200: a viewer must still read\n' "${status}" "${code:-(no code)}"
      fi ;;
    audit)
      if [[ "${status}" != "200" ]]; then
        printf 'FAIL\tGET /v1/workspace/roles answered HTTP %s %s\n' "${status}" "${code:-(no code)}"
        return 0
      fi
      verdict="$(jq -r --arg a "${ROLES_DEFAULT_SET}" --arg r "${extra}" '
          [(.audit // [])[] | objects | select(.action == $a and (.detail.reason // "") == $r)] as $mine
          | [($mine | any(.detail.to == "viewer")), ($mine | any(.detail.to == "member"))]
          | if .[0] and .[1] then "PASS\tthe grant (default -> viewer) and its revocation (-> member) are both in the audit"
            elif .[0] then "FAIL\tthe grant is in the audit, its revocation is not"
            elif .[1] then "FAIL\tthe revocation is in the audit, the grant is not"
            else "FAIL\tneither this run'"'"'s grant nor its revocation is in the audit" end' \
        <<<"${body}" 2>/dev/null)" || verdict="FAIL	the audit could not be read"
      printf '%s\n' "${verdict}" ;;
    reach)
      if [[ ! "${status}" =~ ^2 ]]; then
        printf 'FAIL\tthe platform admin'"'"'s write in the test workspace answered HTTP %s %s\n' "${status}" "${code:-(no code)}"
        return 0
      fi
      verdict="$(jq -r --arg a "${ROLES_ADMIN_ACTED}" --argjson since "${extra:-0}" '
          [(.audit // [])[] | objects | select(.action == $a)
           | (try (.at | sub("\\.[0-9]+"; "") | sub("(\\+00:00|Z)$"; "Z") | fromdateiso8601) catch 0)
           | select(. >= $since)]
          | if length > 0 then "PASS\tthe write went through the reach and is recorded \($a)"
            else "FAIL\tthe write succeeded with no \($a) row since the probe started" end' \
        <<<"${body}" 2>/dev/null)" || verdict="FAIL	the audit could not be read"
      printf '%s\n' "${verdict}" ;;
    personal)
      if [[ "${status}" == "403" && "${code}" == "${ROLES_NOT_MEMBER}" ]]; then
        printf 'PASS\t403 %s: the reach does not cover a personal workspace\n' "${code}"
      else
        printf 'FAIL\tHTTP %s %s, not 403 %s\n' "${status}" "${code:-(no code)}" "${ROLES_NOT_MEMBER}"
      fi ;;
    *)
      printf 'FAIL\tno judge for check %s\n' "${check}" ;;
  esac
}

#: 1 between the grant and its revocation: _roles_revoke runs again on exit.
ROLES_GRANTED=0
ROLES_REASON=""

# _roles_as viewer|admin TENANT METHOD PATH BODY NAME -> acc_api_as into
# ${ACC_WORK}/roles.NAME (and .status, .err).
_roles_as() {
  local sa
  case "$1" in
    viewer) sa="${ACC_ROLES_VIEWER_SA}" ;;
    admin) sa="${ACC_ROLES_ADMIN_SA}" ;;
    *) die "roles: no identity '$1'" ;;
  esac
  acc_api_as "${sa}" "$2" "$3" "$4" "$5" "${ACC_WORK}/roles.$6"
}

_roles_status() { cat "${ACC_WORK}/roles.$1.status"; }
_roles_body() { cat "${ACC_WORK}/roles.$1" 2>/dev/null || true; }
# The API's code in a response, and nothing else of it.
_roles_code() {
  local code
  code="$(jq -r 'if type == "object" then (.code // "") else "" end' <"${ACC_WORK}/roles.$1" 2>/dev/null)" || code=""
  [[ "${code}" =~ ^[a-z0-9_]*$ ]] || code=""
  printf '%s' "${code}"
}
# Why a request was not made, from what it printed: one line, redacted.
_roles_err() { redact <"${ACC_WORK}/roles.$1.err" 2>/dev/null | grep -v '^$' | tail -n 1 | head -c 300 || true; }

# _roles_default ROLE NAME -> the admin sets the test workspace's default.
_roles_default() {
  _roles_as admin "${ACC_ROLES_TENANT}" PUT /workspace/roles/default \
    "$(jq -nc --arg r "$1" --arg why "${ROLES_REASON}" '{role: $r, reason: $why, via: "cli"}')" "$2"
}

_roles_revoke() {
  [[ "${ROLES_GRANTED}" == "1" ]] || return 0
  _roles_default member revoke
  case "$(_roles_status revoke)" in
    2*) ROLES_GRANTED=0 ;;
    *)
      err "roles: the test workspace ${ACC_ROLES_TENANT} may still default to viewer: the revocation answered HTTP $(_roles_status revoke) $(_roles_err revoke)"
      err "roles: restore it with PUT /v1/admin/workspaces/${ACC_ROLES_TENANT}/roles/default {\"role\": \"member\"} as a platform admin" ;;
  esac
}

# _roles_result CHECK STATUS BODY [EXTRA] -> acc_pass / acc_fail on the judge.
_roles_result() {
  local verdict reason
  IFS=$'\t' read -r verdict reason < <(roles_judge "$@")
  if [[ "${verdict}" == "PASS" ]]; then acc_pass "${reason}"; else acc_fail "${reason}"; fi
}

_roles_title() { roles_checks | awk -v n="roles: $1:" 'index($0, n) == 1 { print; exit }'; }

run_roles() {
  step "Acceptance: roles (workspace roles, ROLES-ACCEPT)"
  local problem="" line started status task check
  ACC_CHECK="roles"
  if ! problem="$(roles_settings_problem)"; then
    while IFS= read -r line; do
      acc_check "${line}"
      acc_skip "not measured: ${problem}"
    done < <(roles_checks)
    return 0
  fi
  ROLES_REASON="ROLES-ACCEPT ${ACC_RUN_ID}"
  started="$(date -u +%s)"
  info "roles: test workspace ${ACC_ROLES_TENANT}; viewer ${ACC_ROLES_VIEWER_SA}; platform admin ${ACC_ROLES_ADMIN_SA}"

  # 1. The grant. A workspace a crashed run left at viewer answers 409
  # role_unchanged: restore it first, so this run's rows are this run's.
  _roles_default viewer grant
  if [[ "$(_roles_status grant)" == "409" ]]; then
    _roles_default member restore
    _roles_default viewer grant
  fi
  status="$(_roles_status grant)"
  if [[ "${status}" =~ ^2 ]]; then
    ROLES_GRANTED=1
  else
    for check in viewer-submit viewer-read audit reach; do
      acc_check "$(_roles_title "${check}")"
      acc_fail "not measured: the platform admin could not make the viewer grant in ${ACC_ROLES_TENANT} (HTTP ${status} $(_roles_code grant) $(_roles_err grant))"
    done
  fi

  if [[ "${ROLES_GRANTED}" == "1" ]]; then
    # 2. The viewer, after checking the API saw the viewer and the grant took.
    _roles_as viewer "${ACC_ROLES_TENANT}" GET /workspace/roles "" whoami
    line="$(jq -r '[(.me.email // "" | ascii_downcase), (.me.role // "")] | @tsv' <<<"$(_roles_body whoami)" 2>/dev/null)" || line=""
    if [[ "${line}" != "$(printf '%s\tviewer' "${ACC_ROLES_VIEWER_SA}" | tr '[:upper:]' '[:lower:]')" ]]; then
      for check in viewer-submit viewer-read; do
        acc_check "$(_roles_title "${check}")"
        acc_fail "not measured: GET /v1/workspace/roles as the viewer answered HTTP $(_roles_status whoami) with '${line//$'\t'/ as }', not ${ACC_ROLES_VIEWER_SA} as viewer $(_roles_err whoami)"
      done
    else
      acc_check "$(_roles_title viewer-submit)"
      _roles_as viewer "${ACC_ROLES_TENANT}" POST /tasks \
        "$(jq -nc --arg p "acceptance ${ACC_RUN_ID} roles viewer-submit" --argjson m "$(acc_metadata)" \
            '{runner_profile: "mock", input: {prompt: $p}, priority: 10, metadata: $m}')" submit
      status="$(_roles_status submit)"
      if [[ "${status}" =~ ^2 ]]; then
        # The gate let a viewer submit: cancel it at once, as the admin.
        task="$(jq -r '.id // .task_id // empty' <<<"$(_roles_body submit)" 2>/dev/null)" || task=""
        [[ -z "${task}" ]] || _roles_as admin "${ACC_ROLES_TENANT}" POST "/tasks/${task}/cancel" '{}' cancel
      fi
      _roles_result viewer-submit "${status}" "$(_roles_body submit)"

      acc_check "$(_roles_title viewer-read)"
      _roles_as viewer "${ACC_ROLES_TENANT}" GET "/tasks?limit=1" "" read
      _roles_result viewer-read "$(_roles_status read)" "$(_roles_body read)"
    fi

    # 3. The revocation, then 4. the audit, read as the viewer (a member again).
    _roles_revoke
    _roles_as viewer "${ACC_ROLES_TENANT}" GET /workspace/roles "" audit
    acc_check "$(_roles_title audit)"
    _roles_result audit "$(_roles_status audit)" "$(_roles_body audit)" "${ROLES_REASON}"
    acc_check "$(_roles_title reach)"
    if [[ "$(_roles_status audit)" == "200" ]]; then
      _roles_result reach "$(_roles_status grant)" "$(_roles_body audit)" "${started}"
    else
      acc_fail "not measured: the audit could not be read (HTTP $(_roles_status audit))"
    fi
  fi

  # 5. Another identity's personal workspace.
  acc_check "$(_roles_title personal)"
  _roles_as admin "${ACC_ROLES_OTHER_WORKSPACE}" GET /tenants/me "" personal
  _roles_result personal "$(_roles_status personal)" "$(_roles_body personal)"
}

# On its own: `verify-remote.sh acceptance/groups/roles` executes this file.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  # shellcheck source-path=SCRIPTDIR
  # shellcheck source=../../lib/common.sh
  source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../../lib/common.sh"
  SUITE_NAME="acceptance"
  # shellcheck source-path=SCRIPTDIR
  # shellcheck source=../../lib/testlib.sh
  source "${REPO_ROOT}/scripts/lib/testlib.sh"
  # shellcheck source-path=SCRIPTDIR
  # shellcheck source=../lib.sh
  source "${REPO_ROOT}/scripts/acceptance/lib.sh"
  step "Acceptance: ${PROJECT_ID} / ${ENVIRONMENT} -- the roles probe"
  require_platform
  acc_init
  ROLES_FINISHED=0
  # A crash between the grant and its revocation must not leave the test
  # workspace at viewer, and must not exit 0.
  # shellcheck disable=SC2317,SC2329  # invoked through the EXIT trap below
  _roles_exit() {
    local ec=$?
    _roles_revoke || true
    acc_cleanup
    if [[ "${ROLES_FINISHED}" != "1" ]]; then
      [[ "${ec}" -ne 0 ]] || ec=1
      err "roles: the probe did not finish (exit ${ec})"
    fi
    exit "${ec}"
  }
  trap _roles_exit EXIT
  run_roles
  ROLES_FINISHED=1
  if t_summary; then exit 0; fi
  exit 1
fi
