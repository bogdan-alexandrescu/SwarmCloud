#!/usr/bin/env bash
# Acceptance group `browser`: a page RENDERED, proven by its pixels and text.
#
# WHY THIS GROUP EXISTS. Every browser task reported as a success before
# 2026-09-29 screenshotted about:blank. The task SUCCEEDED, the artifact
# uploaded, and nothing looked at the picture. So here a page with known
# content is opened, and the checks read what came back: the extracted text
# must be the page's, the screenshot's pixels must contain the page's
# coloured block, and a form driven by `fill` and `click` must produce the
# text only the page's own script writes.
#
# THE FIXTURE PAGE is tests/acceptance/fixtures/browser/index.html. It has to
# be served as text/html from a public, stable URL, and that URL is
# SWARM_ACCEPTANCE_BROWSER_URL. raw.githubusercontent.com and jsDelivr do NOT
# qualify: both serve an .html file as `text/plain` with
# `X-Content-Type-Options: nosniff` (measured 2026-09-29), so Chromium shows
# the source as text -- no block, no script, no form. Where it is served from
# is the owner's decision (docs/acceptance.md); until it is set, the page
# checks SKIP saying so. The door checks below need no page and always run
# where the door exists.

set -euo pipefail

BROWSER_URL="${SWARM_ACCEPTANCE_BROWSER_URL:-}"
BROWSER_KNOWN_TEXT="swarmcloud-acceptance-known-text-7f3a"
BROWSER_TITLE="SwarmCloud acceptance fixture"
BROWSER_BLOCK_COLOUR="12a150"

browser_checks() {
  cat <<'EOF'
browser: goto the fixture, and extract_text returns its known string
browser: the full-page screenshot is not blank and shows the fixture's block colour
browser: fill and click on the fixture's form produce the text its script writes
browser: the door refuses the metadata server, a 10.x address, a .svc host and file:// with a 422
EOF
}

run_browser() {
  step "Acceptance: browser${BROWSER_URL:+ (fixture ${BROWSER_URL})}"
  local rc=0 name task="" state=""

  # Is the browser profile's input declared on this deployment (contract
  # request 32, #218 / PR #345)? Asked of the door without creating anything
  # (acc_declares): a file:// url is refused as an input where it is declared,
  # and let through to the reserved-metadata refusal where it is not.
  acc_declares browser '{"prompt":"acceptance door probe","url":"file:///etc/passwd"}' || rc=$?
  if [[ "${rc}" != "0" ]]; then
    while IFS= read -r name; do
      acc_check "${name}"
      if [[ "${rc}" == "1" ]]; then
        acc_skip "not measured: this deployment does not declare the browser profile's inputs yet (contract request 32, #218/PR #345)"
      else
        acc_fail "the declaration probe answered neither invalid_input nor invalid_dispatch; see above"
      fi
    done < <(browser_checks)
    return 0
  fi

  if _browser_page_ready; then
    ACC_CHECK="browser: page"
    acc_submit task browser "$(jq -nc --arg u "${BROWSER_URL}" --arg name "acceptance-${ACC_RUN_ID}" '{
        prompt: "acceptance browser fixture",
        url: $u,
        actions: [
          {type: "wait_for", selector: "#known"},
          {type: "extract", selector: "#known", name: "known.txt"},
          {type: "screenshot", name: "full.png", full_page: true},
          {type: "fill", selector: "#name", text: $name},
          {type: "click", selector: "#go"},
          {type: "wait_for", selector: "#result:not(:empty)"},
          {type: "extract", selector: "#result", name: "result.txt"}
        ],
        extract_text: true,
        screenshot: false}')" "$(acc_extra '{"max_attempts": 1}')" || task=""
    if [[ -n "${task}" ]]; then
      acc_run_to_end state "${task}" || state="NOT_RUN"
    fi
    _browser_check_extract "${task}" "${state}"
    _browser_check_screenshot "${task}" "${state}"
    _browser_check_form "${task}" "${state}"
  fi
  _browser_check_door
}

# _browser_page_ready -> 0 when a fixture URL is set and serves HTML; records
# a SKIP for each page check (and returns 1) when it is not.
_browser_page_ready() {
  local reason="" type name headers
  if [[ -z "${BROWSER_URL}" ]]; then
    reason="SWARM_ACCEPTANCE_BROWSER_URL is not set: the fixture page has no host that serves it as text/html (raw.githubusercontent.com and jsDelivr serve text/plain with nosniff, measured 2026-09-29); see docs/acceptance.md"
  else
    # A reachable URL that answers text/plain would render as source and fail
    # every check for a reason that is the fixture's, not the platform's.
    # Unreachable from here is NOT a reason to skip: the swarm-verify job's
    # egress is not the browser pod's, and the task will say what it saw.
    headers="${ACC_WORK}/browser-headers"
    if curl -sS -m 15 -o /dev/null -D "${headers}" "${BROWSER_URL}" 2>/dev/null; then
      type="$(awk -F': *' 'tolower($1) == "content-type" { print tolower($2) }' "${headers}" | tr -d '\r' | tail -n 1)"
      case "${type}" in
        text/html*) ;;
        *) reason="${BROWSER_URL} is served as '${type:-no content type}', not text/html, so Chromium would show its source instead of rendering it" ;;
      esac
    else
      t_info "could not fetch ${BROWSER_URL} from this job; submitting anyway -- the browser pod's egress is its own"
    fi
  fi
  [[ -n "${reason}" ]] || return 0
  while IFS= read -r name; do
    case "${name}" in
      *door*) continue ;;
    esac
    acc_check "${name}"
    acc_skip "not measured: ${reason}"
  done < <(browser_checks)
  return 1
}

_browser_ran() {
  local task="$1" state="$2"
  if [[ -z "${task}" ]]; then
    acc_fail "not submitted"
    return 1
  fi
  case "${state}" in
    SUCCEEDED) return 0 ;;
    NOT_RUN) acc_skip "not measured: the task did not run (see the first page check)" "${task}"; return 1 ;;
    *) acc_fail "ended ${state}: $(task_field "${task}" '.last_error // "no error"' | redact | tr '\n' ' ' | head -c 300)" "${task}"; return 1 ;;
  esac
}

_browser_check_extract() {
  local task="$1" state="$2" known page title url
  acc_check "browser: goto the fixture, and extract_text returns its known string"
  _browser_ran "${task}" "${state}" || return 0
  title="$(acc_output "${task}" '.title // ""')"
  url="$(acc_output "${task}" '.final_url // ""')"
  acc_assert_eq "${BROWSER_TITLE}" "${title}" "the page title the runner read" "${task}"
  if [[ "${url}" == "about:blank" || -z "${url}" ]]; then
    acc_fail "the runner ended on '${url:-nothing}', not the fixture" "${task}"
  else
    acc_pass "ended on ${url}" "${task}"
  fi
  known="$(acc_artifact_text "${task}" "known.txt" || true)"
  acc_assert_eq "${BROWSER_KNOWN_TEXT}" "$(printf '%s' "${known}" | tr -d '\r\n')" "#known, extracted" "${task}"
  page="$(acc_artifact_text "${task}" "page.txt" || true)"
  if grep -qF "${BROWSER_KNOWN_TEXT}" <<<"${page}"; then
    acc_pass "page.txt (extract_text) carries the known string" "${task}"
  else
    acc_fail "page.txt (extract_text) does not carry the known string: $(printf '%s' "${page}" | head -c 120 | tr '\n' ' ')" "${task}"
  fi
}

_browser_check_screenshot() {
  local task="$1" state="$2" png out rc=0
  acc_check "browser: the full-page screenshot is not blank and shows the fixture's block colour"
  _browser_ran "${task}" "${state}" || return 0
  png="${ACC_WORK}/full.png"
  if ! acc_raw_artifact "${task}" "full.png" "${png}"; then
    acc_fail "full.png could not be downloaded (HTTP ${ACC_HTTP:-none})" "${task}"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    acc_skip "not measured: this image has no python3 to decode the PNG with (scripts/acceptance/pngcheck.py is standard-library Python); $(wc -c <"${png}" | tr -d ' ') bytes were downloaded" "${task}"
    return 0
  fi
  out="$(python3 "${ACC_DIR}/pngcheck.py" "${png}" --colour "${BROWSER_BLOCK_COLOUR}" --min-colour-fraction 0.05)" || rc=$?
  case "${rc}" in
    0) acc_pass "$(jq -r '.reason' <<<"${out}")" "${task}" ;;
    1) acc_fail "$(jq -r '.reason' <<<"${out}")" "${task}" ;;
    *) acc_fail "full.png is not a PNG the decoder reads: $(jq -r '.reason // empty' <<<"${out}" 2>/dev/null || printf '%s' "${out}")" "${task}" ;;
  esac
}

_browser_check_form() {
  local task="$1" state="$2" result
  acc_check "browser: fill and click on the fixture's form produce the text its script writes"
  _browser_ran "${task}" "${state}" || return 0
  result="$(acc_artifact_text "${task}" "result.txt" || true)"
  acc_assert_eq "Hello, acceptance-${ACC_RUN_ID}!" "$(printf '%s' "${result}" | tr -d '\r\n')" "#result after fill and click" "${task}"
}

_browser_check_door() {
  local label url input answer
  acc_check "browser: the door refuses the metadata server, a 10.x address, a .svc host and file:// with a 422"
  while IFS='|' read -r label url; do
    [[ -n "${label}" ]] || continue
    input="$(jq -nc --arg u "${url}" '{prompt: "acceptance door", url: $u}')"
    answer="$(acc_door browser "${input}")"
    acc_assert_eq "422 invalid_input" "${answer}" "url: ${label}"
    # The same URL where an action carries it, not the start url: a door that
    # checked only `url` would let `goto` through.
    input="$(jq -nc --arg u "${url}" '{prompt: "acceptance door", actions: [{type: "goto", url: $u}]}')"
    answer="$(acc_door browser "${input}")"
    acc_assert_eq "422 invalid_input" "${answer}" "actions[0].url: ${label}"
  done <<'EOF'
the metadata server|http://169.254.169.254/computeMetadata/v1/
a 10.x address|http://10.0.0.1/
a .svc host|http://kubernetes.default.svc/
file://|file:///etc/passwd
EOF
}
