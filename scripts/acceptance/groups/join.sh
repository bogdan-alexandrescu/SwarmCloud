#!/usr/bin/env bash
# Acceptance probe `join` (JOIN-DOCS-ACCEPT; docs/design/join-requests.md §6
# row 8 and §8; part of #2325): a person outside a group workspace asks to
# join it through its join link, and a platform admin decides, measured live.
#
#   1. link-on: the admin (ACC_JOIN_ADMIN_SA, a platform admin) turns the test
#      workspace's join link on with a fresh code, and is shown the code once:
#      in the answer's `link` fragment, never again in GET.
#   2. ask: the person (ACC_JOIN_PERSON, NOT in the workspace's group) POSTs
#      the code in a body to /v1/join-requests: 202, a `requested` request.
#   3. approve: the admin approves it. With `platform_memberships` on the
#      answer is `admitted: true`; off, `admitted: false` with the `gcloud`
#      group command, and nobody is admitted.
#   4. selects: on, the person's next request with X-Swarm-Tenant selects the
#      workspace and GET /v1/tenants/mine lists it `via: platform_member`;
#      off, the same request is still 403 tenant_not_member.
#   5. no-group-write: the API's own fresh per-group check (`still_in_group`
#      in GET /v1/workspace/members) does not find the person in the group
#      after the approval; off, the person is still refused. READ ONLY: the
#      probe calls no Cloud Identity API and no gcloud, so it cannot write a
#      group either.
#   6. removal: on, the admin removes the membership and the person's next
#      request is 403 tenant_not_member again. Off there is nothing to remove,
#      so it is not measured.
#   7. link-off: the link is turned off again, and GET says so.
#
# Which path runs is the deployment's: GET /v1/workspace/members says whether
# `platform_memberships` is on. Neither path is a skip of the other.
#
# THE PERSON. The join routes refuse a service account (`_person` in
# routes/join_requests.py: joining is a person's own request), so the asker
# cannot be minted by impersonation as the roles probe's identities are. The
# person's sign-in is an OAuth `authorized_user` credential (client_id,
# client_secret, refresh_token; what `gcloud auth application-default login`
# writes) in the Secret Manager secret ACC_JOIN_PERSON_SECRET, read at run
# time with the job's access token and exchanged at Google's token endpoint
# for an ID token. Its OAuth client must be one the API accepts (API_AUDIENCE,
# or the front door's IAP client). Nothing of it reaches argv, a file other
# than a 0600 one removed at once, or a log; the join code neither.
#
# Until the owner has provisioned the workspace, the person and the secret
# (config in accept.yml's repository variables), every check SKIPs and says
# which setting is missing; a skip is never a pass.
#
# Run in accept.yml's smoke job, after the roles probe, as
# `verify-remote.sh acceptance/groups/join`, so this file is also its own
# entry point; sourced, it only defines the probe.

# Sourced in context (common.sh, testlib.sh, lib.sh) or run on its own below;
# checked on its own, shellcheck sees what those set as unassigned. The
# single-quoted `$e`/`$t`/`$c` are jq's own variables, handed to _join_field.
# shellcheck disable=SC2034,SC2154,SC2016
set -euo pipefail

#: The probe's settings (accept.yml passes them from the repository variables
#: JOIN_TENANT, JOIN_ADMIN_SA, JOIN_PERSON and JOIN_PERSON_SECRET). None is a
#: secret: a tenant id, two addresses and a secret's NAME.
ACC_JOIN_TENANT="${SWARM_ACCEPTANCE_JOIN_TENANT:-}"
ACC_JOIN_ADMIN_SA="${SWARM_ACCEPTANCE_JOIN_ADMIN_SA:-}"
ACC_JOIN_PERSON="${SWARM_ACCEPTANCE_JOIN_PERSON:-}"
ACC_JOIN_PERSON_SECRET="${SWARM_ACCEPTANCE_JOIN_PERSON_SECRET:-}"
#: How long a membership write may take to reach the instance that answers
#: the person's next request: resolution caches a membership read for 15 s
#: (memberships.py), and only the writing instance evicts at once.
ACC_JOIN_SETTLE="${SWARM_ACCEPTANCE_JOIN_SETTLE:-30}"

#: The codes and fields the probe judges by, as swarm_api spells them
#: (auth.TenantNotMember, routes/tenants.VIA_PLATFORM_MEMBER, the approval's
#: group_change.command).
JOIN_NOT_MEMBER="tenant_not_member"
JOIN_VIA_PLATFORM_MEMBER="platform_member"
JOIN_GROUP_COMMAND="gcloud identity groups memberships add"

join_checks() {
  cat <<'EOF'
join: link-on: the admin turns the test workspace's join link on and is shown its code once
join: ask: the person outside the group asks with the code in a body and gets a requested request
join: approve: the admin approves; admitted is true with platform_memberships on, false with the group command off
join: selects: the person's next request selects the workspace (via platform_member), or is still refused with the switch off
join: no-group-write: the approval wrote no Google group: the API's fresh per-group check does not find the person (read only)
join: removal: the admin removes the membership and the person's next request is refused
join: link-off: the join link is turned off again
EOF
}

# join_settings_problem -> returns 0 and prints nothing when the probe can
# run; otherwise prints, on one line, every setting that is missing or
# refused, and returns 1.
join_settings_problem() {
  local problems=() tenant="${ACC_JOIN_TENANT}" admin="${ACC_JOIN_ADMIN_SA}"
  local person="${ACC_JOIN_PERSON}" secret="${ACC_JOIN_PERSON_SECRET}"
  [[ -n "${tenant}" ]] || problems+=("SWARM_ACCEPTANCE_JOIN_TENANT is unset")
  [[ -n "${admin}" ]] || problems+=("SWARM_ACCEPTANCE_JOIN_ADMIN_SA is unset")
  [[ -n "${person}" ]] || problems+=("SWARM_ACCEPTANCE_JOIN_PERSON is unset")
  [[ -n "${secret}" ]] || problems+=("SWARM_ACCEPTANCE_JOIN_PERSON_SECRET is unset")
  if [[ -n "${tenant}" ]]; then
    if [[ ! "${tenant}" =~ ^[a-z0-9][a-z0-9-]*$ || "${tenant}" == u-* ]]; then
      problems+=("SWARM_ACCEPTANCE_JOIN_TENANT must name a group workspace")
    elif [[ "${tenant}" == "${ACC_TENANT:-smoke}" || "${tenant}" == "smoke" ]]; then
      problems+=("SWARM_ACCEPTANCE_JOIN_TENANT may not be the acceptance tenant: its link would be open while every other group runs there")
    fi
  fi
  if [[ -n "${admin}" ]] && { [[ ! "${admin}" =~ ^[a-z0-9-]+@[a-z0-9.-]+\.iam\.gserviceaccount\.com$ ]] || is_shared_resource "${admin}"; }; then
    problems+=("SWARM_ACCEPTANCE_JOIN_ADMIN_SA must be one of this platform's service accounts")
  fi
  if [[ -n "${person}" ]] && { [[ ! "${person}" =~ ^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]+$ ]] || [[ "${person}" == *gserviceaccount.com ]]; }; then
    problems+=("SWARM_ACCEPTANCE_JOIN_PERSON must be one person's lower-case address, not a service account: the join routes refuse a service account")
  fi
  if [[ -n "${secret}" ]] && { [[ ! "${secret}" =~ ^[A-Za-z0-9_-]{1,255}$ ]] || is_shared_resource "${secret}"; }; then
    problems+=("SWARM_ACCEPTANCE_JOIN_PERSON_SECRET must be a Secret Manager secret id of this platform's")
  fi
  [[ "${#problems[@]}" -gt 0 ]] || return 0
  local IFS=';'
  printf '%s\n' "${problems[*]}"
  return 1
}

#: The person's ID token, in this shell's memory only; and what is left to
#: undo (the EXIT trap and _join_link_off_on_exit read them).
JOIN_PERSON_ID=""
JOIN_LINK_SET=0
JOIN_ADMITTED=0

# _join_person_token -> sets JOIN_PERSON_ID from the person's authorized_user
# credential in Secret Manager, or returns 1 with why on stderr. The access
# token and the credential go to curl in 0600 files and through pipes, never
# in argv; the form is built by jq from the secret's payload, in a pipe.
_join_person_token() {
  local token header form rc=0
  token="$(access_token)" || return 1
  header="$(mktemp "${TMPDIR:-/tmp}/swarm-auth.XXXXXX")"
  form="$(mktemp "${TMPDIR:-/tmp}/swarm-auth.XXXXXX")"
  chmod 600 "${header}" "${form}"
  printf '%s %s\n' "${_API_AUTH_PREFIX}" "${token}" >"${header}"
  curl -sS -f -m "${HTTP_TIMEOUT:-30}" -H @"${header}" \
    "https://secretmanager.googleapis.com/v1/projects/${PROJECT_ID}/secrets/${ACC_JOIN_PERSON_SECRET}/versions/latest:access" 2>/dev/null \
    | jq -r '.payload.data | @base64d | fromjson
             | select(.refresh_token and .client_id)
             | "grant_type=refresh_token&client_id=\(.client_id | @uri)&client_secret=\(.client_secret // "" | @uri)&refresh_token=\(.refresh_token | @uri)"' \
      >"${form}" 2>/dev/null || rc=$?
  rm -f "${header}"
  if [[ "${rc}" -ne 0 || ! -s "${form}" ]]; then
    rm -f "${form}"
    err "join: could not read the person's sign-in from secret ${ACC_JOIN_PERSON_SECRET} (the job's identity needs roles/secretmanager.secretAccessor on it, and it must hold an authorized_user credential)"
    return 1
  fi
  JOIN_PERSON_ID="$(curl -sS -f -m "${HTTP_TIMEOUT:-30}" \
    -H "Content-Type: application/x-www-form-urlencoded" --data-binary @"${form}" \
    https://oauth2.googleapis.com/token 2>/dev/null | jq -r '.id_token // empty' 2>/dev/null)" || JOIN_PERSON_ID=""
  rm -f "${form}"
  if [[ -z "${JOIN_PERSON_ID}" ]]; then
    err "join: Google's token endpoint gave no ID token for the person's credential in ${ACC_JOIN_PERSON_SECRET} (revoked or expired refresh token?)"
    return 1
  fi
}

# _join_person NAME METHOD PATH [BODY] [TENANT] -> one API request as the
# person, into ${ACC_WORK}/join.NAME (and .status, 0 when none was made, and
# .err). The body goes to curl from a 0600 file: the ask's carries the code.
_join_person() {
  local name="$1" method="$2" path="$3" body="${4:-}" tenant="${5:-}"
  local out="${ACC_WORK}/join.$1" header data="" response
  local args=()
  printf '0' >"${out}.status"
  : >"${out}"
  header="$(mktemp "${TMPDIR:-/tmp}/swarm-auth.XXXXXX")"
  chmod 600 "${header}"
  printf '%s %s\n' "${_API_AUTH_PREFIX}" "${JOIN_PERSON_ID}" >"${header}"
  args=(-sS -m "${HTTP_TIMEOUT:-30}" -H @"${header}" -w $'\n%{http_code}' -X "${method}")
  [[ -z "${tenant}" ]] || args+=(-H "X-Swarm-Tenant: ${tenant}")
  if [[ -n "${body}" ]]; then
    data="$(mktemp "${TMPDIR:-/tmp}/swarm-auth.XXXXXX")"
    chmod 600 "${data}"
    printf '%s' "${body}" >"${data}"
    args+=(-H "Content-Type: application/json" --data-binary @"${data}")
  fi
  response="$(curl "${args[@]}" "$(api_url)${API_PREFIX}${path}" 2>"${out}.err")" || response=""
  rm -f "${header}" ${data:+"${data}"}
  [[ -n "${response}" ]] || return 0
  printf '%s' "${response##*$'\n'}" >"${out}.status"
  printf '%s' "${response%$'\n'*}" >"${out}"
}

# _join_admin NAME METHOD PATH BODY -> acc_api_as the platform admin, in the
# test workspace, into ${ACC_WORK}/join.NAME.
_join_admin() {
  acc_api_as "${ACC_JOIN_ADMIN_SA}" "${ACC_JOIN_TENANT}" "$2" "$3" "$4" "${ACC_WORK}/join.$1"
}

_join_status() { cat "${ACC_WORK}/join.$1.status" 2>/dev/null || printf '0'; }
# _join_field NAME [JQ ARGS...] FILTER -> the filter over a response;
# nothing when it is not JSON. Never the body itself.
_join_field() {
  local file="${ACC_WORK}/join.$1"
  shift
  jq -r "$@" <"${file}" 2>/dev/null || true
}
# The API's code in a response, and nothing else of it.
_join_code() {
  local code
  code="$(_join_field "$1" 'if type == "object" then (.code // "") else "" end')"
  [[ "${code}" =~ ^[a-z0-9_]*$ ]] || code=""
  printf '%s' "${code}"
}
# Why a request went wrong, from what it printed: one line, redacted.
_join_err() { redact <"${ACC_WORK}/join.$1.err" 2>/dev/null | grep -v '^$' | tail -n 1 | head -c 300 || true; }
_join_title() { join_checks | awk -v n="join: $1:" 'index($0, n) == 1 { print; exit }'; }

# _join_fail_rest REASON CHECK... -> each CHECK FAILs, not measured, for REASON.
_join_fail_rest() {
  local reason="$1" check
  shift
  for check in "$@"; do
    acc_check "$(_join_title "${check}")"
    acc_fail "not measured: ${reason}"
  done
}

# _join_selects NAME -> the status of the person's GET /v1/tenants/me in the
# test workspace; with WANT, retried until it is WANT or ACC_JOIN_SETTLE
# seconds have passed (a membership read is cached for 15 s elsewhere).
_join_selects() {
  local name="$1" want="${2:-}" deadline=$((SECONDS + ACC_JOIN_SETTLE))
  while :; do
    _join_person "${name}" GET /tenants/me "" "${ACC_JOIN_TENANT}"
    [[ -n "${want}" && "$(_join_status "${name}")" != "${want}" && "${SECONDS}" -lt "${deadline}" ]] || break
    sleep 5
  done
  _join_status "${name}"
}

_join_remove() {
  _join_admin "$1" DELETE /workspace/members/person \
    "$(jq -nc --arg e "${ACC_JOIN_PERSON}" --arg r "JOIN-ACCEPT ${ACC_RUN_ID:-}" '{email: $e, reason: $r}')"
}

# What a crash must not leave behind: the person a member, the link on.
_join_link_off_on_exit() {
  if [[ "${JOIN_ADMITTED}" == "1" ]]; then
    _join_remove exit-remove
    if [[ "$(_join_status exit-remove)" =~ ^(2|404) ]]; then
      JOIN_ADMITTED=0
    else
      err "join: ${ACC_JOIN_PERSON} may still hold a platform membership of ${ACC_JOIN_TENANT}: remove it in Workspace › People (HTTP $(_join_status exit-remove) $(_join_err exit-remove))"
    fi
  fi
  if [[ "${JOIN_LINK_SET}" == "1" ]]; then
    _join_admin exit-link PUT /workspace/join-link '{"on":false}'
    if [[ "$(_join_status exit-link)" =~ ^2 ]]; then
      JOIN_LINK_SET=0
    else
      err "join: the join link of ${ACC_JOIN_TENANT} may still be on: turn it off in Workspace › People (HTTP $(_join_status exit-link) $(_join_err exit-link))"
    fi
  fi
  return 0
}

run_join() {
  step "Acceptance: join (join requests, JOIN-DOCS-ACCEPT)"
  local problem="" line="" on="" code="" ref="" status="" row=""
  ACC_CHECK="join"
  if ! problem="$(join_settings_problem)"; then
    while IFS= read -r line; do
      acc_check "${line}"
      acc_skip "not measured: ${problem}"
    done < <(join_checks)
    return 0
  fi
  info "join: test workspace ${ACC_JOIN_TENANT}; platform admin ${ACC_JOIN_ADMIN_SA}; person ${ACC_JOIN_PERSON}"
  local all=(link-on ask approve selects no-group-write removal link-off)

  # 0. The person signs in, the API takes them for a person, and they are
  # outside the workspace. A membership a crashed run left is removed first.
  if ! _join_person_token; then
    _join_fail_rest "the person's ID token could not be minted (above)" "${all[@]}"
    return 0
  fi
  _join_person whoami GET /join-requests/mine
  if [[ "$(_join_status whoami)" != "200" ]]; then
    _join_fail_rest "GET /v1/join-requests/mine as ${ACC_JOIN_PERSON} answered HTTP $(_join_status whoami) $(_join_code whoami) $(_join_err whoami): the API did not take the person's sign-in as a person" "${all[@]}"
    return 0
  fi
  _join_admin members GET /workspace/members ""
  if [[ "$(_join_status members)" != "200" ]]; then
    _join_fail_rest "GET /v1/workspace/members as the admin answered HTTP $(_join_status members) $(_join_code members) $(_join_err members)" "${all[@]}"
    return 0
  fi
  on="$(_join_field members '.platform_memberships == true')"
  info "join: platform_memberships is $([[ "${on}" == "true" ]] && echo on || echo off) in this environment"
  if [[ "$(_join_field members --arg e "${ACC_JOIN_PERSON}" \
      '[(.members // [])[] | objects | select(.email == $e)] | length')" != "0" ]]; then
    info "join: a run before this one left ${ACC_JOIN_PERSON} a member; removing it first"
    _join_remove restore
  fi
  status="$(_join_selects before)"
  if [[ "${status}" != "403" || "$(_join_code before)" != "${JOIN_NOT_MEMBER}" ]]; then
    _join_fail_rest "before asking, ${ACC_JOIN_PERSON} selecting ${ACC_JOIN_TENANT} answered HTTP ${status} $(_join_code before), not 403 ${JOIN_NOT_MEMBER}: the probe needs a person outside the workspace's group" "${all[@]}"
    return 0
  fi

  # 1. The link, on with a fresh code, shown once.
  acc_check "$(_join_title link-on)"
  _join_admin link PUT /workspace/join-link '{"on":true,"rotate":true}'
  status="$(_join_status link)"
  [[ "${status}" =~ ^2 ]] && JOIN_LINK_SET=1
  code="$(_join_field link 'if type == "object" and .on == true then (.link // "" | if test("#") then sub("^[^#]*#"; "") else "" end) else "" end')"
  [[ "${code}" =~ ^[A-Za-z0-9_-]+$ ]] || code=""
  _join_admin link-read GET /workspace/join-link ""
  if [[ -z "${code}" ]]; then
    acc_fail "PUT /v1/workspace/join-link answered HTTP ${status} $(_join_code link) with no code in its link"
    _join_fail_rest "the join link has no code" ask approve selects no-group-write removal
  else
    if [[ "$(_join_field link-read 'type == "object" and .on == true and (has("link") | not)')" == "true" ]]; then
      acc_pass "the link is on and its code was shown once, in the answer's link fragment; GET does not show it"
    else
      acc_fail "GET /v1/workspace/join-link answered HTTP $(_join_status link-read) and did not say on without the code"
    fi

    # 2. The ask, the code in a body (printf is a builtin: no argv).
    acc_check "$(_join_title ask)"
    _join_person ask POST /join-requests "$(printf '{"code":"%s","message":"JOIN-ACCEPT %s"}' "${code}" "${ACC_RUN_ID:-}")"
    code=""
    status="$(_join_status ask)"
    ref="$(_join_field ask 'if type == "object" and .state == "requested" then (.ref // "") else "" end')"
    [[ "${ref}" =~ ^[A-Za-z0-9_-]+$ ]] || ref=""
    if [[ "${status}" =~ ^20[02]$ && -n "${ref}" ]]; then
      acc_pass "HTTP ${status}: a requested request, the code sent in the body only"
    else
      acc_fail "POST /v1/join-requests answered HTTP ${status} $(_join_code ask), not 202 with a requested request"
      _join_fail_rest "the person's request was not made" approve selects no-group-write removal
    fi
  fi

  if [[ -n "${ref}" ]]; then
    # 3. The approval, at the workspace's default role.
    acc_check "$(_join_title approve)"
    _join_admin approve POST "/admin/join-requests/${ref}/approve" '{}'
    status="$(_join_status approve)"
    if [[ "${on}" == "true" ]]; then
      if [[ "${status}" == "200" && "$(_join_field approve '.admitted == true and .group_change == null')" == "true" ]]; then
        JOIN_ADMITTED=1
        acc_pass "200 admitted: true, with no group change to make"
      else
        acc_fail "the approval answered HTTP ${status} $(_join_code approve) admitted $(_join_field approve '.admitted // "absent"'), not 200 admitted: true"
      fi
    else
      if [[ "${status}" == "200" && "$(_join_field approve --arg c "${JOIN_GROUP_COMMAND}" --arg e "${ACC_JOIN_PERSON}" \
          '.admitted == false and ((.group_change.command // "") | startswith($c) and endswith("--member-email=" + $e))')" == "true" ]]; then
        acc_pass "200 admitted: false, and the answer names the group command (${JOIN_GROUP_COMMAND} ... --member-email=${ACC_JOIN_PERSON}) for the group's owner"
      else
        acc_fail "with platform_memberships off the approval answered HTTP ${status} $(_join_code approve), not 200 admitted: false with the group command"
      fi
    fi

    # 4. The person's next request.
    acc_check "$(_join_title selects)"
    if [[ "${on}" == "true" ]]; then
      status="$(_join_selects after 200)"
      _join_person mine GET /tenants/mine
      row="$(_join_field mine --arg t "${ACC_JOIN_TENANT}" '[.[]? | objects | select(.tenant_id == $t) | .via] | first // ""')"
      if [[ "${status}" == "200" && "$(_join_field after '.tenant_id // ""')" == "${ACC_JOIN_TENANT}" && "${row}" == "${JOIN_VIA_PLATFORM_MEMBER}" ]]; then
        acc_pass "200: the header selects ${ACC_JOIN_TENANT}, and GET /v1/tenants/mine lists it via ${JOIN_VIA_PLATFORM_MEMBER}"
      else
        acc_fail "the person selecting ${ACC_JOIN_TENANT} answered HTTP ${status} $(_join_code after), tenants/mine via '${row}': the approval did not admit"
      fi
    else
      status="$(_join_selects after)"
      if [[ "${status}" == "403" && "$(_join_code after)" == "${JOIN_NOT_MEMBER}" ]]; then
        acc_pass "403 ${JOIN_NOT_MEMBER}: with the switch off an approval admits nobody"
      else
        acc_fail "with platform_memberships off the person selecting ${ACC_JOIN_TENANT} answered HTTP ${status} $(_join_code after), not 403 ${JOIN_NOT_MEMBER}"
      fi
    fi

    # 5. No group was written: read only, through the API's own check.
    acc_check "$(_join_title no-group-write)"
    if [[ "${on}" == "true" ]]; then
      _join_admin members-after GET /workspace/members ""
      row="$(_join_field members-after --arg e "${ACC_JOIN_PERSON}" \
        '[(.members // [])[] | objects | select(.email == $e) | (.still_in_group | tostring)] | first // "absent"')"
      case "${row}" in
        false) acc_pass "the API's fresh per-group check does not find ${ACC_JOIN_PERSON} in the group: the membership is platform-side only" ;;
        true) acc_fail "the API's fresh per-group check finds ${ACC_JOIN_PERSON} in the workspace's group after the approval: something wrote the group" ;;
        null) acc_fail "the directory did not answer the per-group check (still_in_group null); not measured" ;;
        *) acc_fail "GET /v1/workspace/members answered HTTP $(_join_status members-after) with no membership for ${ACC_JOIN_PERSON}" ;;
      esac
    elif [[ "$(_join_status after)" == "403" && "$(_join_code after)" == "${JOIN_NOT_MEMBER}" ]]; then
      acc_pass "the person outside the group before the approval is still refused after it: no group was written"
    else
      acc_fail "the person selecting ${ACC_JOIN_TENANT} after the approval answered HTTP $(_join_status after) $(_join_code after)"
    fi

    # 6. Removal ends it.
    acc_check "$(_join_title removal)"
    if [[ "${on}" != "true" ]]; then
      acc_skip "not measured: platform_memberships is off in this environment, so the approval wrote no membership to remove"
    elif [[ "${JOIN_ADMITTED}" != "1" ]]; then
      acc_fail "not measured: the approval admitted nobody"
    else
      _join_remove remove
      status="$(_join_status remove)"
      [[ "${status}" =~ ^2 ]] && JOIN_ADMITTED=0
      if [[ "${status}" =~ ^2 && "$(_join_field remove '.state // ""')" == "removed" ]]; then
        status="$(_join_selects removed 403)"
        if [[ "${status}" == "403" && "$(_join_code removed)" == "${JOIN_NOT_MEMBER}" ]]; then
          acc_pass "removed, and the person's next request is 403 ${JOIN_NOT_MEMBER}"
        else
          acc_fail "removed, and the person selecting ${ACC_JOIN_TENANT} still answered HTTP ${status} $(_join_code removed), not 403 ${JOIN_NOT_MEMBER}"
        fi
      else
        acc_fail "DELETE /v1/workspace/members/person answered HTTP ${status} $(_join_code remove) $(_join_err remove)"
      fi
    fi
  fi

  # 7. The link, off again.
  acc_check "$(_join_title link-off)"
  _join_admin link-off PUT /workspace/join-link '{"on":false}'
  status="$(_join_status link-off)"
  [[ "${status}" =~ ^2 ]] && JOIN_LINK_SET=0
  _join_admin link-final GET /workspace/join-link ""
  if [[ "${status}" =~ ^2 && "$(_join_field link-final '.on == false')" == "true" ]]; then
    acc_pass "the link is off: its code no longer works"
  else
    acc_fail "turning the link off answered HTTP ${status} $(_join_code link-off); GET says on is $(_join_field link-final '.on // "unknown"')"
  fi
}

# On its own: `verify-remote.sh acceptance/groups/join` executes this file.
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
  step "Acceptance: ${PROJECT_ID} / ${ENVIRONMENT} -- the join probe"
  require_platform
  acc_init
  JOIN_FINISHED=0
  # A crash must not leave the person a member or the link on, and must not
  # exit 0.
  # shellcheck disable=SC2317,SC2329  # invoked through the EXIT trap below
  _join_exit() {
    local ec=$?
    _join_link_off_on_exit || true
    acc_cleanup
    if [[ "${JOIN_FINISHED}" != "1" ]]; then
      [[ "${ec}" -ne 0 ]] || ec=1
      err "join: the probe did not finish (exit ${ec})"
    fi
    exit "${ec}"
  }
  trap _join_exit EXIT
  run_join
  JOIN_FINISHED=1
  if t_summary; then exit 0; fi
  exit 1
fi
