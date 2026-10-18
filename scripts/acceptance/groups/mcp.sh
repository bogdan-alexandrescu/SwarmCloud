#!/usr/bin/env bash
# Acceptance group `mcp`: a task's agent queries its caller's registered MCP
# server through the worker's relay, and the per-run credential stays out of
# the agent's reach (#1433; docs/design/registered-mcp-servers.md section 6,
# lane MCP-ACCEPT).
#
# ONE claude-code TASK, ONE DOOR PROBE AND ONE CALL WITHOUT A CREDENTIAL:
#
#   door   the dispatch naming a server nobody registered, with a reserved
#          metadata key (lib.sh acc_door's rule), so nothing is created
#          whether the door refuses the name or not.
#   probe  the registered URL called with no credential: Cloud Run must
#          refuse it 403 (the echo server's own refusal is 401, so a 401 is
#          a public service).
#   task   claude-code naming the registered echo server with a per-run
#          credential (mcp_credentials, auth mode `dispatch`): a Google ID
#          token minted as the acceptance identity. Its prompt has it call
#          `whoami` once, save the answer as an artifact, and plant a canary
#          in its output, an artifact and a workspace file.
#
# Its peak in the smoke tenant is one task.
#
# THE SERVER IS THE ECHO SERVER (owner decision Q10, 2026-10-17): the
# dev-only Cloud Run service terraform/infra/mcp_test_server.tf deploys from
# tests/acceptance/mcp_echo_server/server.py. An operator REGISTERS it once
# in the smoke tenant -- registration is an admin's act (Q1), and swarm-verify
# is no admin -- as
#
#   POST /v1/mcp-servers  {"name": "acc-echo", "url": "<terraform output
#        mcp_test_server.url>", "auth": {"mode": "dispatch"},
#        "allowed_profiles": ["claude-code"]}
#
# and this group only reads it. No registration, a disabled one, or the
# feature switched off (SWARM_MCP_SERVERS, every submission refused
# `mcp_unavailable`) is a SKIP naming the reason, never a pass.
#
# NOTHING IS PUBLIC (owner decision 2026-10-18, lane MCP-ACCEPT-B). The echo
# server's one invoker is swarm-verify, the identity this suite runs as
# (terraform/infra/mcp_test_server.tf), so the per-run credential is the
# suite's OWN Google ID token, audience the service's URL: exactly what a
# caller sends for its own server. The judge asks the metadata server for it;
# nothing is impersonated and no token-creator grant exists. A token it cannot
# get leaves no task and the checks that needed it FAIL naming why.
# MCP_INVOKER is the identity the token must name: SWARM_ACCEPTANCE_MCP_INVOKER,
# or swarm-verify's address in this project.
#
# THE CREDENTIAL IS NEVER A SHELL VALUE. tests/acceptance/test_caller_mcp.py
# mints it into a 0600 file in this run's work directory and writes the
# request body beside it; the body goes to swarm-api as `--data-binary @file`
# (not api_post, whose body is a curl argument) and is deleted at once. The
# judge reads the minted file to search for the value; acc_cleanup removes
# the directory.
#
# THIS FILE OWNS THE TASK; THE PYTHON OWNS THE JUDGEMENT. What the task left
# -- its document, events, attempts, attach rows, the four log streams and
# the transcript of every attempt, every artifact, every checkpoint archive
# (unredacted gzip), and whether its leases are released -- is saved under
# one directory, and the judge writes one JSON line per check, reported here
# in this file's order.

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused. Checked in context
# through run.sh -x.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

#: The verdict keys tests/acceptance/test_caller_mcp.py writes (its CHECKS),
#: in the order of mcp_checks below.
MCP_KEYS=(credential_seen unregistered_refused no_leak worker_gone unauthenticated_refused)
MCP_JUDGE="tests/acceptance/test_caller_mcp.py"
#: The name the echo server is registered under in the smoke tenant.
MCP_SERVER="${SWARM_ACCEPTANCE_MCP_SERVER:-acc-echo}"
#: The one identity the echo server admits, whose ID token is the credential.
MCP_INVOKER="${SWARM_ACCEPTANCE_MCP_INVOKER:-swarm-verify@${PROJECT_ID}.iam.gserviceaccount.com}"
#: The streams GET /v1/tasks/{id}/logs serves (swarm_api.inspect.LOG_STREAMS),
#: and the most 1 MiB pages read of one before the read is called unread.
MCP_LOG_STREAMS=(stdout stderr agent_stdout agent_stderr)
MCP_LOG_PAGES=64

mcp_checks() {
  cat <<'EOF'
mcp: a claude-code task calls its caller's registered MCP server through the relay, and the server sees the per-run credential
mcp: the same dispatch naming an unregistered server is refused before admission
mcp: the credential is in no log route, artifact, checkpoint or event of the task, and a planted canary is in every one
mcp: after the attempt the worker has exited and the workspace it left holds no copy of the credential
mcp: the registered server called without the credential is refused 403 by Cloud Run
EOF
}

# _mcp_post DIR KIND -> POSTs DIR/KIND-body.json to /v1/tasks, deletes it,
# and leaves the answer in DIR/KIND-answer.json; prints the HTTP status
# (000 when no answer came). The Authorization header arrives on stdin, as
# acc_raw_artifact sends it; the body from the file, never in argv.
_mcp_post() {
  local dir="$1" kind="$2" code
  if ! code="$(auth_config "$(api_credential)" | curl -sS -m 120 -K - -o "${dir}/${kind}-answer.json" \
      -w '%{http_code}' -X POST -H "X-Swarm-Tenant: ${ACC_TENANT}" -H "Content-Type: application/json" \
      --data-binary @"${dir}/${kind}-body.json" "$(api_url)${API_PREFIX}/tasks")"; then
    code="000"
  fi
  rm -f "${dir}/${kind}-body.json"
  printf '%s' "${code}"
}

# _mcp_body DIR KIND [EXTRA ARGS...] -> the judge writes DIR/KIND-body.json.
_mcp_body() {
  local dir="$1" kind="$2"
  shift 2
  python3 "${REPO_ROOT}/${MCP_JUDGE}" body --dir "${dir}" --kind "${kind}" --server "${MCP_SERVER}" \
    --run-id "${ACC_RUN_ID}" --metadata "$(acc_metadata)" "$@"
}

# _mcp_door DIR -> DIR/door.json: {status, code}. A door that let the name
# through and created a task anyway is cancelled here and reported created.
_mcp_door() {
  local dir="$1" code id err
  ACC_CHECK="mcp: door"
  if ! _mcp_body "${dir}" door; then
    jq -nc '{status: "not sent", code: "the judge wrote no body"}' >"${dir}/door.json"
    return 0
  fi
  code="$(_mcp_post "${dir}" door)"
  if [[ "${code}" =~ ^2 ]]; then
    id="$(jq -r '.id // .task_id // .task.id // empty' <"${dir}/door-answer.json" 2>/dev/null || true)"
    [[ -z "${id}" ]] || cancel_all "${id}"
    jq -nc --arg s "created:${id:-?}" '{status: $s, code: ""}' >"${dir}/door.json"
  else
    err="$(acc_error_code <"${dir}/door-answer.json")"
    jq -nc --arg s "${code}" --arg c "${err}" '{status: $s, code: $c}' >"${dir}/door.json"
  fi
  rm -f "${dir}/door-answer.json"
}

# _mcp_submit DIR -> DIR/submit.json {status, code, task} and DIR/task; the
# task is recorded for cleanup in THIS shell (not a command substitution).
_mcp_submit() {
  local dir="$1" code id="" err=""
  ACC_CHECK="mcp: task"
  if ! _mcp_body "${dir}" task --repository-url "${ACC_REPOSITORY_URL}" --ref "${ACC_REF}"; then
    jq -nc '{status: "not sent", code: "the judge wrote no body", task: ""}' >"${dir}/submit.json"
    : >"${dir}/task"
    return 0
  fi
  code="$(_mcp_post "${dir}" task)"
  if [[ "${code}" =~ ^2 ]]; then
    id="$(jq -r '.id // .task_id // .task.id // empty' <"${dir}/task-answer.json" 2>/dev/null || true)"
  else
    err="$(acc_error_code <"${dir}/task-answer.json")"
    t_info "mcp: the dispatch was refused HTTP ${code} ${err:-}"
  fi
  rm -f "${dir}/task-answer.json"
  if [[ -n "${id}" ]]; then
    ACC_TASKS+=("${id}")
    t_info "task ${id}"
  fi
  jq -nc --arg s "${code}" --arg c "${err}" --arg t "${id}" '{status: $s, code: (if $c == "" then null else $c end), task: $t}' \
    >"${dir}/submit.json"
  printf '%s\n' "${id}" >"${dir}/task"
}

# _mcp_raw PATH OUT -> PATH's bytes in OUT, unredacted where the route serves
# them so (a checkpoint archive is gzip); 0 on HTTP 200.
_mcp_raw() {
  local path="$1" out="$2" code
  if ! code="$(auth_config "$(api_credential)" | curl -sS -m 300 -K - -o "${out}" -w '%{http_code}' \
      -H "X-Swarm-Tenant: ${ACC_TENANT}" "$(api_url)${API_PREFIX}${path}")"; then
    return 1
  fi
  [[ "${code}" == "200" ]]
}

# _mcp_logs DIR TASK ATTEMPT -> every page of each stream as one JSON line in
# DIR/logs/ATTEMPT.STREAM.jsonl, and the transcript. A read that fails
# leaves its file absent: the judge calls that unread, never clean.
_mcp_logs() {
  local dir="$1/logs" task="$2" attempt="$3" stream file page offset next pages
  mkdir -p "${dir}"
  for stream in "${MCP_LOG_STREAMS[@]}"; do
    file="${dir}/${attempt}.${stream}.jsonl"
    page="${dir}/page.json"
    : >"${file}"
    offset=0
    pages=0
    while :; do
      if ! api_fetch "/tasks/${task}/logs?attempt_id=${attempt}&stream=${stream}&offset=${offset}&limit_bytes=1048576" "${page}"; then
        rm -f "${file}"
        break
      fi
      jq -c '.streams[0] // {status: "missing"}' "${page}" >>"${file}"
      next="$(jq -r '.streams[0].next_offset // empty' "${page}")"
      pages=$(( pages + 1 ))
      [[ -n "${next}" && "${next}" != "${offset}" ]] || break
      if [[ "${pages}" -ge "${MCP_LOG_PAGES}" ]]; then
        jq -nc --arg n "${MCP_LOG_PAGES}" '{status: ("longer than " + $n + " pages")}' >>"${file}"
        break
      fi
      offset="${next}"
    done
    rm -f "${page}"
  done
  api_fetch "/tasks/${task}/transcript?attempt_id=${attempt}&include_raw=true" "${dir}/transcript.${attempt}.json" \
    || rm -f "${dir}/transcript.${attempt}.json"
}

# _mcp_artifacts DIR TASK -> DIR/artifacts/N.bin per artifact, names.json
# listing {name, file, http}, and answer.txt (the whoami answer).
_mcp_artifacts() {
  local dir="$1/artifacts" task="$2" name i=0 rows
  mkdir -p "${dir}"
  if ! api_fetch "/tasks/${task}/artifacts?limit=500" "${dir}/list.json"; then
    rm -f "${dir}/list.json"
    return 0
  fi
  rows="${dir}/names.jsonl"
  : >"${rows}"
  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    ACC_HTTP=""
    acc_raw_artifact "${task}" "${name}" "${dir}/${i}.bin" 2>/dev/null || rm -f "${dir}/${i}.bin"
    jq -nc --arg n "${name}" --arg f "${i}.bin" --arg h "${ACC_HTTP:-000}" '{name: $n, file: $f, http: $h}' >>"${rows}"
    if [[ "${name}" == "mcp-answer.json" && -f "${dir}/${i}.bin" ]]; then
      cp "${dir}/${i}.bin" "${dir}/answer.txt"
    fi
    i=$(( i + 1 ))
  done < <(jq -r '.artifacts[]?.name // empty' "${dir}/list.json")
  jq -s . "${rows}" >"${dir}/names.json"
  t_info "mcp: ${i} artifact(s) read"
}

# _mcp_checkpoints DIR TASK -> DIR/checkpoints.json and each archive as
# DIR/checkpoints/<id>.tgz.
_mcp_checkpoints() {
  local dir="$1" task="$2" cid attempt n=0
  mkdir -p "${dir}/checkpoints"
  if ! api_fetch "/tasks/${task}/checkpoints?limit=100" "${dir}/checkpoints.json"; then
    rm -f "${dir}/checkpoints.json"
    return 0
  fi
  while IFS=$'\t' read -r cid attempt; do
    [[ "${cid}" =~ ^[A-Za-z0-9._-]+$ ]] || continue
    _mcp_raw "/tasks/${task}/checkpoints/${cid}/content?attempt_id=${attempt}" "${dir}/checkpoints/${cid}.tgz" \
      || rm -f "${dir}/checkpoints/${cid}.tgz"
    n=$(( n + 1 ))
  done < <(jq -r '.checkpoints[]? | [.checkpoint_id // "", .attempt_id // ""] | @tsv' "${dir}/checkpoints.json")
  t_info "mcp: ${n} checkpoint(s) listed"
}

# _mcp_save DIR TASK -> everything the judge reads about the task.
_mcp_save() {
  local dir="$1" task="$2" attempt detail ok
  [[ -n "${task}" ]] || return 0
  task_doc "${task}" >"${dir}/doc.json" 2>/dev/null || rm -f "${dir}/doc.json"
  if task_events "${task}" | jq -s . >"${dir}/events.json" 2>/dev/null; then :; else rm -f "${dir}/events.json"; fi
  api_fetch "/tasks/${task}/attempts" "${dir}/attempts.json" || rm -f "${dir}/attempts.json"
  if fs_query mcp_attach_releases "$(fs_field_filter task_id EQUAL "$(jq -nc --arg v "${task}" '{stringValue:$v}')")" 50 \
      | jq -c "${FS_JQ} doc" | jq -s . >"${dir}/mcp-rows.json" 2>/dev/null; then :; else rm -f "${dir}/mcp-rows.json"; fi
  if [[ -f "${dir}/attempts.json" ]]; then
    while IFS= read -r attempt; do
      [[ "${attempt}" =~ ^[A-Za-z0-9._-]+$ ]] || continue
      _mcp_logs "${dir}" "${task}" "${attempt}"
    done < <(jq -r '.attempts[]?.attempt_id // empty' "${dir}/attempts.json")
  fi
  _mcp_artifacts "${dir}" "${task}"
  _mcp_checkpoints "${dir}" "${task}"
  if detail="$(acc_leases_released "${task}")"; then ok=true; else ok=false; fi
  jq -nc --argjson ok "${ok}" --arg d "${detail}" '{ok: $ok, detail: $d}' >"${dir}/leases.json"
}

run_mcp() {
  step "Acceptance: mcp (server ${MCP_SERVER}; repository ${ACC_REPOSITORY_URL} @ ${ACC_REF})"
  local dir task="" state="" names=() i line key verdict reason status
  local empty='{}'
  dir="${ACC_WORK}/mcp"
  mkdir -p "${dir}"
  chmod 700 "${dir}"
  while IFS= read -r line; do
    names+=("${line}")
  done < <(mcp_checks)
  [[ "${#names[@]}" -eq "${#MCP_KEYS[@]}" ]] \
    || die "mcp: ${#names[@]} checks listed, ${#MCP_KEYS[@]} verdict keys"

  printf '%s\n' "${MCP_SERVER}" >"${dir}/server"
  python3 "${REPO_ROOT}/${MCP_JUDGE}" mint --dir "${dir}" || die "mcp: the judge could not mint the run's values"

  # The registration, read only. Anything but a 200 leaves no file, and the
  # judge skips naming the status.
  if api_fetch "/mcp-servers/${MCP_SERVER}" "${dir}/registration.json" 2>/dev/null; then
    status="200"
  else
    status="${API_STATUS:-000}"
    rm -f "${dir}/registration.json"
  fi
  printf '%s\n' "${status}" >"${dir}/registration.status"

  _mcp_door "${dir}"
  if [[ -f "${dir}/registration.json" ]]; then
    python3 "${REPO_ROOT}/${MCP_JUDGE}" probe --dir "${dir}" || warn "mcp: the judge could not probe ${MCP_SERVER}"
  fi
  if [[ -f "${dir}/registration.json" ]] \
      && [[ "$(jq -r '.server.enabled == true and .server.auth.mode == "dispatch"' "${dir}/registration.json")" == "true" ]]; then
    python3 "${REPO_ROOT}/${MCP_JUDGE}" token --dir "${dir}" --invoker "${MCP_INVOKER}" \
      || warn "mcp: the judge could not mint the ID token"
    if [[ "$(jq -r '.minted == true' "${dir}/token.json" 2>/dev/null || echo false)" == "true" ]]; then
      _mcp_submit "${dir}"
    else
      t_info "mcp: no ID token as ${MCP_INVOKER}: $(jq -r '.reason // "token.json was not written"' "${dir}/token.json" 2>/dev/null | redact || true); no task is submitted"
      jq -nc '{status: "not sent", code: null, task: ""}' >"${dir}/submit.json"
      : >"${dir}/task"
    fi
  else
    t_info "mcp: ${MCP_SERVER} is not an enabled dispatch registration in ${ACC_TENANT} (HTTP ${status}); no task is submitted"
    jq -nc '{status: "not sent", code: null, task: ""}' >"${dir}/submit.json"
    : >"${dir}/task"
  fi
  task="$(cat "${dir}/task")"
  if [[ -n "${task}" ]]; then
    state="$(acc_settle "${task}" "${ACC_TIMEOUT}")" || true
  fi
  printf '%s\n' "${state}" >"${dir}/state"
  _mcp_save "${dir}" "${task}"

  rm -f "${dir}/results.jsonl"
  python3 "${REPO_ROOT}/${MCP_JUDGE}" judge --dir "${dir}" --results "${dir}/results.jsonl" \
    || warn "mcp: the judge exited non-zero"

  for (( i = 0; i < ${#names[@]}; i++ )); do
    key="${MCP_KEYS[i]}"
    acc_check "${names[i]}"
    line="$(jq -c --arg k "${key}" 'select(.check == $k)' "${dir}/results.jsonl" 2>/dev/null | head -n 1 || true)"
    verdict="$(jq -r '.verdict // empty' <<<"${line:-$empty}" 2>/dev/null || true)"
    reason="$(jq -r '.reason // empty' <<<"${line:-$empty}" 2>/dev/null | redact || true)"
    case "${verdict}" in
      PASS) acc_pass "${reason}" "${task}" ;;
      SKIP) acc_skip "${reason}" "${task}" ;;
      FAIL) acc_fail "${reason}" "${task}" ;;
      *) acc_fail "the judge wrote no verdict for this check" "${task}" ;;
    esac
  done
  # The minted values go now, not only at acc_cleanup: nothing after this
  # line needs them.
  rm -f "${dir}/secrets.json"
  [[ -z "${task}" ]] || cancel_all "${task}"
}
