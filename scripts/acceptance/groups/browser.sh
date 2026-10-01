#!/usr/bin/env bash
# Acceptance group `browser`: a page RENDERED, proven by its pixels and text.
#
# WHY THIS GROUP EXISTS. Every browser task reported as a success before
# 2026-09-29 screenshotted about:blank. The task SUCCEEDED, the artifact
# uploaded, and nothing looked at the picture. So here real pages are opened
# and the checks read what came back: the text, the title, the final URL and
# the screenshot's PIXELS.
#
# THE PAGES ARE THIRD-PARTY (owner decision on #358, 2026-09-29), because no
# host this repository has serves an .html file as text/html:
# raw.githubusercontent.com and jsDelivr both answer `text/plain` with
# `X-Content-Type-Options: nosniff` (measured 2026-09-29), so Chromium would
# show a fixture's source rather than render it.
#
#   * https://example.com -- the deterministic page. Its body text, its title
#     and its background (#eee under the light scheme headless Chromium uses)
#     are known. NOTE, measured 2026-09-29: the page's visible text no longer
#     contains "Example Domain" -- only its <title> does. The body reads "This
#     domain is for use in documentation examples...". So the title is
#     asserted to be "Example Domain" and the extracted text to carry the body
#     sentence. The page also asks not to be relied on "for testing and
#     monitoring purposes"; one request per dev release is the whole use.
#   * https://www.reddit.com -- a real-world render (the owner's note). No
#     exact text is asserted: final_url is on reddit.com, the title is not
#     empty, and the full-page screenshot is not blank. A bot challenge or a
#     consent wall is a SKIP naming what was seen, never a PASS.
#   * https://httpbin.org/forms/post -- a public test form made for exactly
#     this (httpbin is a request-echo service). `fill` the customer name,
#     `click` the submit button, and the echoed response must carry the name.
#     If httpbin itself is unreachable the check SKIPs saying so: that is a
#     third party's availability, not the platform's.

# Sourced by run.sh after common.sh, testlib.sh and lib.sh: CI shellchecks
# this file on its own too, where the variables those set and the ones this
# file sets for them read as unassigned and unused. Checked in context
# through run.sh -x.
# shellcheck disable=SC2034,SC2154
set -euo pipefail

EXAMPLE_URL="https://example.com/"
EXAMPLE_TITLE="Example Domain"
EXAMPLE_TEXT="This domain is for use in documentation examples"
#: html{background:light-dark(#eee,#222)} -- #eee under the light scheme.
EXAMPLE_BACKGROUND="eeeeee"
REDDIT_URL="https://www.reddit.com/"
FORM_URL="https://httpbin.org/forms/post"

#: Titles and text that mean the page served a wall instead of itself.
BROWSER_WALL_RE='blocked|verify you are human|are you a robot|prove your humanity|just a moment|attention required|access denied|captcha|cloudflare|unusual traffic|consent|accept all cookies|whoa there'

browser_checks() {
  cat <<'EOF'
browser: example.com renders, and its title and text are the page's
browser: example.com's full-page screenshot is not blank and shows its background colour
browser: reddit.com renders a real page (final_url, title, a screenshot that is not blank)
browser: fill and click on httpbin's form produce the echoed name
browser: the door refuses the metadata server, a 10.x address, a .svc host and file:// with a 422
EOF
}

_browser_task() {
  local __bt_var="$1" url="$2" actions="$3"
  acc_submit "${__bt_var}" browser "$(jq -nc --arg u "${url}" --argjson a "${actions}" \
      '{prompt: "acceptance browser", url: $u, actions: $a, extract_text: true, screenshot: false}')" \
    "$(acc_extra '{"max_attempts": 1}')" || printf -v "${__bt_var}" '%s' ""
}

run_browser() {
  step "Acceptance: browser"
  local rc=0 name example="" reddit="" form="" s_example="" s_reddit="" s_form=""

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

  ACC_CHECK="browser: example.com"
  _browser_task example "${EXAMPLE_URL}" '[{"type":"screenshot","name":"full.png","full_page":true}]'
  ACC_CHECK="browser: reddit.com"
  _browser_task reddit "${REDDIT_URL}" '[{"type":"wait","seconds":3},{"type":"screenshot","name":"full.png","full_page":true}]'
  ACC_CHECK="browser: form"
  _browser_task form "${FORM_URL}" "$(jq -nc --arg n "acceptance-${ACC_RUN_ID}" '[
      {type: "fill", selector: "input[name=custname]", text: $n},
      {type: "click", selector: "form button"},
      {type: "wait_for", selector: "pre"}]')"

  if [[ -n "${example}" ]]; then acc_run_to_end s_example "${example}" || s_example="NOT_RUN"; fi
  if [[ -n "${reddit}" ]]; then acc_run_to_end s_reddit "${reddit}" || s_reddit="NOT_RUN"; fi
  if [[ -n "${form}" ]]; then acc_run_to_end s_form "${form}" || s_form="NOT_RUN"; fi

  _browser_check_example "${example}" "${s_example}"
  _browser_check_example_pixels "${example}" "${s_example}"
  _browser_check_reddit "${reddit}" "${s_reddit}"
  _browser_check_form "${form}" "${s_form}"
  _browser_check_door
}

_browser_ran() {
  local task="$1" state="$2"
  if [[ -z "${task}" ]]; then
    acc_fail "not submitted"
    return 1
  fi
  case "${state}" in
    SUCCEEDED) return 0 ;;
    NOT_RUN) acc_skip "not measured: the task did not run (see the reason above)" "${task}"; return 1 ;;
    *) acc_fail "ended ${state}: $(task_field "${task}" '.last_error // "no error"' | redact | tr '\n' ' ' | head -c 300)" "${task}"; return 1 ;;
  esac
}

# _browser_pixels TASK [COLOUR MIN_FRACTION] -> PASS or FAIL on full.png.
_browser_pixels() {
  local task="$1" colour="${2:-}" fraction="${3:-}" png out rc=0 args=()
  png="${ACC_WORK}/full-${task}.png"
  if ! acc_raw_artifact "${task}" "full.png" "${png}"; then
    acc_fail "full.png could not be downloaded (HTTP ${ACC_HTTP:-none})" "${task}"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    acc_skip "not measured: this image has no python3 to decode the PNG with (scripts/acceptance/pngcheck.py is standard-library Python); $(wc -c <"${png}" | tr -d ' ') bytes were downloaded" "${task}"
    return 0
  fi
  [[ -z "${colour}" ]] || args=(--colour "${colour}" --min-colour-fraction "${fraction}")
  out="$(python3 "${ACC_DIR}/pngcheck.py" "${png}" ${args[@]+"${args[@]}"})" || rc=$?
  case "${rc}" in
    0) acc_pass "full.png $(jq -r '"\(.width)x\(.height)"' <<<"${out}"): $(jq -r '.reason' <<<"${out}")" "${task}" ;;
    1) acc_fail "full.png: $(jq -r '.reason' <<<"${out}")" "${task}" ;;
    *) acc_fail "full.png is not a PNG the decoder reads: $(jq -r '.reason // empty' <<<"${out}" 2>/dev/null || printf '%s' "${out}")" "${task}" ;;
  esac
}

_browser_check_example() {
  local task="$1" state="$2" page url
  acc_check "browser: example.com renders, and its title and text are the page's"
  _browser_ran "${task}" "${state}" || return 0
  acc_assert_eq "${EXAMPLE_TITLE}" "$(acc_output "${task}" '.title // ""')" "the page title the runner read" "${task}"
  url="$(acc_output "${task}" '.final_url // ""')"
  if [[ "${url}" == https://example.com* ]]; then
    acc_pass "ended on ${url}" "${task}"
  else
    acc_fail "ended on '${url:-nothing}', not example.com" "${task}"
  fi
  page="$(acc_artifact_text "${task}" "page.txt" || true)"
  if grep -qF "${EXAMPLE_TEXT}" <<<"${page}"; then
    acc_pass "page.txt (extract_text) carries \"${EXAMPLE_TEXT}\"" "${task}"
  else
    acc_fail "page.txt (extract_text) does not carry \"${EXAMPLE_TEXT}\": $(printf '%s' "${page}" | head -c 120 | tr '\n' ' ')" "${task}"
  fi
}

_browser_check_example_pixels() {
  local task="$1" state="$2"
  acc_check "browser: example.com's full-page screenshot is not blank and shows its background colour"
  _browser_ran "${task}" "${state}" || return 0
  # Half the page at least: example.com is a few lines of text on #eee.
  _browser_pixels "${task}" "${EXAMPLE_BACKGROUND}" 0.5
}

_browser_check_reddit() {
  local task="$1" state="$2" title url page wall
  acc_check "browser: reddit.com renders a real page (final_url, title, a screenshot that is not blank)"
  _browser_ran "${task}" "${state}" || return 0
  title="$(acc_output "${task}" '.title // ""')"
  url="$(acc_output "${task}" '.final_url // ""')"
  page="$(acc_artifact_text "${task}" "page.txt" || true)"
  # A wall first: a challenge page renders too, and would pass the rest.
  # The title always; the text only when it is short, as a wall's is -- a
  # real front page is long, and a post title saying "blocked" is not a wall.
  wall="$(printf '%s' "${title}" | grep -oiE "${BROWSER_WALL_RE}" | head -n 1 || true)"
  if [[ -z "${wall}" && "${#page}" -lt 2000 ]]; then
    wall="$(printf '%s' "${page}" | grep -oiE "${BROWSER_WALL_RE}" | head -n 1 || true)"
  fi
  if [[ -n "${wall}" ]]; then
    acc_skip "not measured: reddit.com served a bot challenge or consent wall (saw \"${wall}\"; title \"${title}\")" "${task}"
    return 0
  fi
  if [[ "${url}" =~ ^https://([a-z0-9-]+\.)*reddit\.com(/|$) ]]; then
    acc_pass "ended on ${url}" "${task}"
  else
    acc_fail "ended on '${url:-nothing}', not reddit.com" "${task}"
  fi
  if [[ -n "${title//[[:space:]]/}" ]]; then
    acc_pass "title: ${title}" "${task}"
  else
    acc_fail "the page has no title" "${task}"
  fi
  _browser_pixels "${task}"
}

_browser_check_form() {
  local task="$1" state="$2" page name error
  acc_check "browser: fill and click on httpbin's form produce the echoed name"
  if [[ -n "${task}" && "${state}" == "FAILED" ]]; then
    error="$(task_field "${task}" '.last_error // ""')"
    # httpbin down, slow or unreachable is httpbin's availability, not a result.
    if grep -qiE 'net::ERR|Timeout .*exceeded|50[234]' <<<"${error}"; then
      acc_skip "not measured: ${FORM_URL} did not answer ($(printf '%s' "${error}" | redact | tr '\n' ' ' | head -c 160))" "${task}"
      return 0
    fi
  fi
  _browser_ran "${task}" "${state}" || return 0
  name="acceptance-${ACC_RUN_ID}"
  page="$(acc_artifact_text "${task}" "page.txt" || true)"
  if grep -qE "\"custname\": *\"${name}\"" <<<"${page}"; then
    acc_pass "httpbin echoed custname \"${name}\": the fill reached the field and the click submitted the form" "${task}"
  else
    acc_fail "the echoed response does not carry custname \"${name}\": $(printf '%s' "${page}" | head -c 160 | tr '\n' ' ')" "${task}"
  fi
}

_browser_check_door() {
  local label url input answer
  acc_check "browser: the door refuses the metadata server, a 10.x address, a .svc host and file:// with a 422"
  while IFS='|' read -r label url; do
    [[ -n "${label}" ]] || continue
    input="$(jq -nc --arg u "${url}" '{prompt: "acceptance door", url: $u}')"
    answer="$(acc_door browser "${input}")"
    acc_assert_eq "422 invalid_input" "${answer}" "url: ${label}" ""
    # The same URL where an action carries it, not the start url: a door that
    # checked only `url` would let `goto` through.
    input="$(jq -nc --arg u "${url}" '{prompt: "acceptance door", actions: [{type: "goto", url: $u}]}')"
    answer="$(acc_door browser "${input}")"
    acc_assert_eq "422 invalid_input" "${answer}" "actions[0].url: ${label}" ""
  done <<'EOF'
the metadata server|http://169.254.169.254/computeMetadata/v1/
a 10.x address|http://10.0.0.1/
a .svc host|http://kubernetes.default.svc/
file://|file:///etc/passwd
EOF
}
