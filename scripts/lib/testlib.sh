#!/usr/bin/env bash
# Shared assertions and platform helpers for the *-test.sh scripts.
#
# Deliberate split of responsibilities:
#
#   * WRITES go through the API, because that is the only supported way to
#     create work and the tests should exercise the real admission path.
#   * OBSERVATIONS read Firestore directly, because Firestore is the
#     authoritative store. Reading state from the same API that is under test
#     would let a broken API report success, and it couples these tests to a
#     response shape rather than to the state machine in CONTRACT.md.

set -euo pipefail

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0
TESTS_SKIPPED=0
FAILED_NAMES=()
SKIPPED_NAMES=()
SUITE_NAME="${SUITE_NAME:-suite}"
SUITE_STARTED="$(date -u +%s)"

t_case() {
  TESTS_RUN=$((TESTS_RUN + 1))
  printf '\n%s[%d] %s%s\n' "${C_BOLD}" "${TESTS_RUN}" "$*" "${C_RESET}" >&2
}

t_pass() {
  TESTS_PASSED=$((TESTS_PASSED + 1))
  printf '%s  PASS%s %s\n' "${C_GREEN}" "${C_RESET}" "$*" >&2
}

t_fail() {
  TESTS_FAILED=$((TESTS_FAILED + 1))
  FAILED_NAMES+=("$*")
  printf '%s  FAIL%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2
}

# A THIRD OUTCOME, because two are not enough and the missing one kept being
# spelled as the wrong one of the other two.
#
# Some properties can only be checked when the deployment happens to carry the
# evidence -- a spend figure exists only once something that reports usage has
# run, and a backend execution can only be compared when one is in flight. The
# honest answer there is "not measured", and the two ways of faking it are both
# worse: a t_pass certifies something nobody looked at (the exact shape that put
# `[[ "" -eq 0 ]]` in this file's history), and a t_fail blames the platform for
# a condition it is entitled to be in.
#
# A skip is NOT a pass. It is counted apart, listed by name in the summary, and
# `t_summary` prints it whether the suite passed or failed so it cannot be read
# past. A caller that needs the check to be mandatory turns skips into failures
# with SUITE_SKIPS_ARE_FAILURES=1 -- which is what CI should do once the
# evidence is guaranteed to exist.
t_skip() {
  if [[ "${SUITE_SKIPS_ARE_FAILURES:-0}" == "1" ]]; then
    t_fail "$* (not measured, and this run requires it)"
    return 0
  fi
  _t_record_skip "$*"
}

# A skip an OPERATOR chose, as opposed to one the evidence forced.
#
# t_skip means "could not be measured this time". A pause is different in
# kind: the owner closed admission to a pool on purpose -- pools/resource:browser
# at hard_limit 0 until the GKE fixes are proven -- so there is nothing to
# measure, and a submission would sit QUEUED for the suite's whole timeout.
#
# Counted and listed exactly like any other skip, so it is NOT a pass and the
# summary names it. It is NOT turned into a failure by SUITE_SKIPS_ARE_FAILURES:
# that switch means "the evidence is guaranteed to exist", and a deliberate
# pause guarantees that it does not. A suite that uses this must still fail a
# run made of nothing but pauses -- smoke-test.sh does, in "Every execution
# backend was exercised".
t_skip_paused() {
  _t_record_skip "$* (paused by operator)"
}

_t_record_skip() {
  TESTS_SKIPPED=$((TESTS_SKIPPED + 1))
  SKIPPED_NAMES+=("$*")
  printf '%s  SKIP%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2
}

t_info() { printf '       %s\n' "$*" >&2; }

# A failure that must STOP the suite, because every assertion after it would be
# measuring something other than what it claims to measure.
#
# `t_fail` is advisory: it records the failure and the script carries on. That
# is right for an assertion about the platform -- one broken property should not
# hide the other nine. It is wrong for a SETUP step, and race-test.sh is where
# the difference bites. Narrowing a pool to one slot is what creates the
# contention the whole suite exists to observe; when it does not happen, the
# remaining cases still run, still sample, and still report
#
#     PASS  peak concurrent leases on runner:mock: 0 <= 1
#
# over a pool whose ceiling is untouched at 20 and which nothing was ever
# racing for. A red summary line does not undo a green assertion: the assertion
# is what a person quotes, and it says the platform was proven not to
# oversubscribe when nothing was measured at all.
#
# So a setup step that fails ends the run here, with the summary printed so the
# cases that did run are still reported. The EXIT trap still fires, which is how
# anything this suite changed gets put back.
t_fatal() {
  t_fail "$*"
  t_info "this is a SETUP failure, not a platform result: every case after it"
  t_info "would measure something other than what it claims to. Stopping here."
  t_summary || true
  exit 1
}

assert_eq() {
  local want="$1" got="$2" what="$3"
  if [[ "${want}" == "${got}" ]]; then
    t_pass "${what}: ${got}"
  else
    t_fail "${what}: expected '${want}', got '${got}'"
  fi
}

# A numeric assertion whose operand was never measured is not a pass.
#
# Both helpers below are reached as `assert_le "$(pool_active global)" N ...`
# (failure-test.sh:162, load-test.sh:150). A command substitution used as an
# ARGUMENT swallows its own exit status -- `set -e` cannot fire there -- so a
# failed read arrived as the empty string, and `[[ "" -le N ]]` is TRUE in
# bash. The assertion passed green having measured nothing.
#
# Guarding the callers is not enough, because the substitution is evaluated
# before this function is entered; the check has to be here, where the value
# actually lands.
_assert_numeric() {
  local value="$1" what="$2" role="$3"
  if [[ -z "${value}" ]]; then
    t_fail "${what}: the ${role} could not be read (empty). Refusing to compare -- an unread value is not a passing one."
    return 1
  fi
  if [[ ! "${value}" =~ ^-?[0-9]+$ ]]; then
    t_fail "${what}: the ${role} is not a number ('${value}'). Refusing to compare."
    return 1
  fi
  return 0
}

assert_le() {
  local value="$1" limit="$2" what="$3"
  _assert_numeric "${value}" "${what}" "measured value" || return 0
  _assert_numeric "${limit}" "${what}" "limit" || return 0
  if [[ "${value}" -le "${limit}" ]]; then
    t_pass "${what}: ${value} <= ${limit}"
  else
    t_fail "${what}: ${value} > ${limit}"
  fi
}

assert_ge() {
  local value="$1" floor="$2" what="$3"
  _assert_numeric "${value}" "${what}" "measured value" || return 0
  _assert_numeric "${floor}" "${what}" "floor" || return 0
  if [[ "${value}" -ge "${floor}" ]]; then
    t_pass "${what}: ${value} >= ${floor}"
  else
    t_fail "${what}: ${value} < ${floor}"
  fi
}

assert_true() {
  if "$@"; then t_pass "$*"; else t_fail "$*"; fi
}

t_summary() {
  local elapsed=$(( $(date -u +%s) - SUITE_STARTED ))
  local name
  hr
  # Printed BEFORE the verdict and on both paths: a green suite that silently
  # measured nothing is the failure mode this counter exists to expose, so the
  # list must not be reachable only through the failure branch.
  if [[ "${TESTS_SKIPPED}" -gt 0 ]]; then
    warn "${SUITE_NAME}: ${TESTS_SKIPPED} check(s) NOT MEASURED -- these are not passes"
    for name in ${SKIPPED_NAMES[@]+"${SKIPPED_NAMES[@]}"}; do
      printf '    ? %s\n' "${name}" >&2
    done
  fi
  if [[ "${TESTS_FAILED}" -eq 0 ]]; then
    ok "${SUITE_NAME}: ${TESTS_PASSED}/${TESTS_RUN} passed in ${elapsed}s"
    return 0
  fi
  err "${SUITE_NAME}: ${TESTS_FAILED} of ${TESTS_RUN} failed in ${elapsed}s"
  for name in ${FAILED_NAMES[@]+"${FAILED_NAMES[@]}"}; do
    printf '    - %s\n' "${name}" >&2
  done
  return 1
}

# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

require_platform() {
  # NOT gcloud. The suites reach Cloud Run and GCS over REST via
  # cloud_run_service_uri and gcs_object_count, so they run in an image with
  # no Cloud SDK -- whose base ships unfixed HIGH/CRITICAL CVEs that `make
  # push` rightly refuses to promote.
  require_cmd jq curl
  # Three answers, not two. This guard used to run `gcloud firestore databases
  # describe` with its stderr discarded, in an image that carries no Cloud SDK
  # on purpose -- so every in-VPC run of this gate died here with "does not
  # exist. Run 'make infra' first" against a database holding live tenants.
  # fs_database_exists is REST now and separates absent from unreadable.
  require_fs_database "run ${SUITE_NAME:-the verification suites}"

  # Not api_reachable(): it runs curl with stderr sent to /dev/null and reads
  # grep's exit status rather than curl's, so a 403 (missing roles/run.invoker),
  # a 401 (expired session) and a genuinely undeployed service all collapse to
  # the same "did not answer" verdict below. Probe directly, the same way
  # api_url() does above it in common.sh, so the die message names the real
  # cause instead of sending every one of those cases at 'make deploy'.
  #
  # api_credential, not id_token: the front door is behind IAP and refuses a
  # Google ID token whatever its audience. And through `-K -`, not argv --
  # this line used to interpolate the token straight into curl's command line,
  # where every process on the box can read it out of ps.
  local ready_err ready_status detail
  ready_err="$(mktemp "${TMPDIR:-/tmp}/swarm-readyz.XXXXXX")"
  if ! ready_status="$(auth_config "$(api_credential)" \
        | curl -sS -m 10 -K - -o /dev/null -w '%{http_code}' \
        "$(api_url)/readyz" 2>"${ready_err}")"; then
    detail="$(cat "${ready_err}")"
    rm -f "${ready_err}"
    die_if_auth_failure "${detail}"
    err "curl could not reach $(api_url)/readyz"
    printf '%s\n' "${detail}" | redact | head -n 3 | sed 's/^/     /' >&2
    die "that is a transport failure, not proof the API is undeployed. Check network access and API_URL before running 'make deploy'."
  fi
  rm -f "${ready_err}"

  case "${ready_status}" in
    2*) ;;
    401|403)
      die "the API at $(api_url)/readyz answered HTTP ${ready_status}. That is an auth/IAM problem -- an expired session, the caller missing roles/run.invoker, or IAP refusing the credential -- not a missing deployment. Do not run 'make deploy' for this."
      ;;
    404)
      # 404 HAS ITS OWN CASE BECAUSE IT IS THE ONE THAT LIES.
      #
      # This gate used to fold it into the catch-all below and print "rule out
      # auth/IAM before running 'make deploy'", which is advice that cannot
      # help: the request never reached the application. A Cloud Run service
      # whose ingress is internal-and-cloud-load-balancing answers every
      # external request 404 from Google's frontend -- /readyz, /v1/anything
      # and a route that does not exist all return the identical 272-byte HTML
      # page. Measured against the live deployment on 2026-09-22, while the
      # same routes served through the load balancer.
      #
      # common.sh's api_url refuses that address now, so reaching here means
      # something set API_URL to it by hand, or the front door itself is
      # serving a 404.
      err "the API at $(api_url)/readyz answered HTTP 404."
      err "A 404 here is almost never a missing route. Cloud Run's frontend answers 404 to"
      err "every external request for a service whose ingress is not 'all' -- so this is the"
      err "wrong ADDRESS, not a broken deployment and not an IAM problem."
      err "Check: is API_URL pinned to the *.run.app hostname? From outside the VPC it cannot"
      err "serve. Unset it and let api_url resolve the load balancer, or set API_HOST."
      die "do not run 'make deploy' for this; nothing about the deployment is wrong."
      ;;
    *)
      die "the API at $(api_url)/readyz answered HTTP ${ready_status}, not 2xx. Rule out auth/IAM before running 'make deploy' -- this was not a transport failure, not a 401/403 and not a 404."
      ;;
  esac
}

# profile_input PROFILE RUN_ID -> the smallest input PROFILE's runner accepts
# AND can complete, as compact JSON.
#
# Most runners start on a prompt alone. `browser` refuses an input with neither `url` nor
# `actions` ("browser runner needs input.url or at least one action",
# apps/agent-worker/agent_worker/runners/browser.py), so the `{message, run_id}`
# every suite used to send fails at the runner with dispatch working perfectly.
# That made the smoke suite's GKE_AUTOPILOT row -- browser is its only profile
# -- a check that could not pass, and `smoke-test.sh --profile browser` a proof
# that could not prove anything.
#
# One screenshot of about:blank: Chromium starts, /dev/shm is large enough, the
# workspace is writable and an artifact uploads, with no dependency on any site
# outside the platform. THAT IS ALL IT PROVES, and the suites label it "Chromium
# starts" for that reason (#357): every browser task that ever succeeded here
# screenshotted about:blank, so a page that never loaded, a renderer that drew
# nothing and a broken text extraction all came back SUCCEEDED. That a page
# loads and renders is t_check_browser_fixture's to prove, below.
#
# THE RUN ID RIDES IN THE PROMPT. It was a key of its own, `run_id`, beside a
# `message` no runner read. Since contract request 25 the API refuses any key
# of `input` the named profile does not declare (RunnerProfile.inputs, 422
# `invalid_input`), and `prompt` is the one key every profile takes. The
# default branch goes to whichever profile a run names, so it carries nothing
# else. Pinned by tests/unit/scripts/test_profile_input.py and by section 13
# of scripts/lib/check-contract-parity.sh.
profile_input() {
  local profile="$1" run_id="$2"
  case "${profile}" in
    browser)
      jq -nc --arg r "${run_id}" '{
        prompt: ("smoke " + $r),
        actions: [{type: "screenshot", name: "proof.png", full_page: false}],
        extract_text: false}'
      ;;
    generic)
      # `command` is REQUIRED since contract request 32 (#218): a generic task
      # with only a prompt is a 422 at the door. `pytest` is the catalogue
      # entry that needs nothing but the image. On an empty workspace it exits
      # 5, no tests collected, so this runs it where there is one test to pass:
      # tests/smoke/generic in this repository, which profile_extra below has
      # the worker clone into work/repo. Its own pytest.ini makes that
      # directory the rootdir. No paths key: the runner checks paths against
      # the workspace but hands them to pytest as written, beside this cwd.
      jq -nc --arg r "${run_id}" '{prompt: ("smoke " + $r), command: "pytest", working_directory: "repo/tests/smoke/generic"}'
      ;;
    *)
      jq -nc --arg r "${run_id}" '{prompt: ("smoke " + $r)}'
      ;;
  esac
}

# ---------------------------------------------------------------------------
# The browser fixture: a page the check writes, loaded and read back (#357)
# ---------------------------------------------------------------------------
#
# The about:blank task above proves Chromium starts. This proves a page LOADS
# and RENDERS: the check writes a page -- one colour filling the viewport and a
# string carrying the run id -- has a browser task open it, and asserts the
# screenshot's PIXELS are that colour and the extracted text holds that string.
# A blank screenshot, another page (an error page, a consent wall) or a stale
# one from an earlier run fails it.
#
# HOW THE CHECK SERVES A PAGE IT WROTE. Not a data: URL: the runner opens http
# and https only (`ALLOWED_SCHEMES`, and `url_refusal` in the frozen catalogue).
# Not this repository: no host it has serves an .html file as text/html
# (scripts/acceptance/groups/browser.sh measured raw.githubusercontent.com and
# jsDelivr on 2026-09-29: text/plain, nosniff). httpbin's /base64/<value>
# answers the base64url-decoded value as text/html, so the URL CARRIES the
# page and nothing outside the URL decides what it shows. httpbin.org is the
# third party the acceptance suite already uses (owner decision on #358);
# SWARM_BROWSER_FIXTURE_BASE points this at a self-hosted httpbin (go-httpbin
# serves the same route) for a deployment whose egress cannot reach it.
#
# tests/unit/scripts/test_browser_fixture_check.py runs every helper here
# offline, against PNGs it writes.

#: The fixture's colour block, RRGGBB. Saturated and far from white, black and
#: example.com's #eee, so neither a blank page nor a different page matches it
#: within pngcheck's tolerance.
BROWSER_FIXTURE_RGB="c2185b"
BROWSER_FIXTURE_TITLE="Swarm acceptance fixture"
#: The block fills the viewport and the text is a few short lines, so the
#: colour covers far more than this; half is what a page that rendered
#: something else as well, or only partly, cannot reach.
BROWSER_FIXTURE_MIN_FRACTION="0.5"

# browser_fixture_text RUN_ID -> the string the page shows and page.txt must
# hold. Per run, so a screenshot or a cached page from another run cannot pass.
browser_fixture_text() { printf 'swarm-fixture-%s' "$1"; }

# browser_fixture_html RUN_ID -> the page.
browser_fixture_html() {
  printf '<!doctype html><html><head><meta charset="utf-8"><title>%s</title>' "${BROWSER_FIXTURE_TITLE}"
  printf '<style>html,body{margin:0;height:100%%;background:#%s}' "${BROWSER_FIXTURE_RGB}"
  printf 'p{margin:0;padding:24px 40px;color:#fff;font:20px sans-serif}</style></head>'
  printf '<body><p>%s</p><p>%s</p></body></html>' "${BROWSER_FIXTURE_TITLE}" "$(browser_fixture_text "$1")"
}

# browser_fixture_url RUN_ID -> the URL that serves the page.
#
# base64url WITH its padding: httpbin decodes with urlsafe_b64decode, which
# refuses unpadded input. `tr -d '\n'` because GNU base64 wraps at 76 columns
# and macOS's does not; `-w` exists on only one of them.
browser_fixture_url() {
  local base="${SWARM_BROWSER_FIXTURE_BASE:-https://httpbin.org/base64/}"
  printf '%s%s' "${base}" "$(browser_fixture_html "$1" | base64 | tr -d '\n' | tr '+/' '-_')"
}

# png_shows_colour FILE RRGGBB MIN_FRACTION -> 0 when FILE is a PNG that is not
# blank and RRGGBB covers at least MIN_FRACTION of it; otherwise 1, with the
# reason on stderr. scripts/acceptance/pngcheck.py does the decoding -- one
# decoder in this repository, standard-library Python, tested against every
# PNG row filter in tests/unit/scripts/test_acceptance_helpers.py.
png_shows_colour() {
  local file="$1" rgb="$2" fraction="$3" out rc=0 reason
  if ! command -v python3 >/dev/null 2>&1; then
    err "png: no python3 to decode ${file} with (scripts/acceptance/pngcheck.py is standard-library Python)"
    return 1
  fi
  out="$(python3 "${REPO_ROOT}/scripts/acceptance/pngcheck.py" "${file}" \
      --colour "${rgb}" --min-colour-fraction "${fraction}")" || rc=$?
  reason="$(jq -r '.reason // empty' <<<"${out}" 2>/dev/null || printf '%s' "${out}")"
  case "${rc}" in
    0) printf '%s\n' "${reason}" >&2; return 0 ;;
    1)
      case "${reason}" in
        blank*) err "png: the screenshot is blank -- ${reason}" ;;
        *) err "png: the fixture colour #${rgb} is missing or too little of the image -- ${reason}" ;;
      esac
      ;;
    *) err "png: ${file} is not a PNG this decoder reads -- ${reason}" ;;
  esac
  return 1
}

# browser_fixture_preflight URL -> 0 when URL answers 200 text/html carrying
# BROWSER_FIXTURE_TITLE, fetched from HERE before any task is submitted.
# Otherwise 1, with FIXTURE_PREFLIGHT_REASON naming the host and what it said.
#
# WHY. The fixture host is a third party (httpbin.org unless
# SWARM_BROWSER_FIXTURE_BASE says otherwise), and both callers gate a release.
# The runner reports an error page as SUCCEEDED, so without this an httpbin
# outage reads as "the page did not render" and turns the release red for a
# reason that is not the platform's. A host that does not serve the fixture is
# therefore a SKIP that names it -- not measured, never a pass -- exactly as
# scripts/acceptance/groups/browser.sh treats httpbin being unreachable. The
# fetch is from the caller's network, not the worker's, so a host that answers
# here and not from the worker still fails below, with the page the runner
# ended on in the message.
FIXTURE_PREFLIGHT_REASON=""
browser_fixture_preflight() {
  local url="$1" host body meta code type rc=0
  host="${url#*://}"
  host="${host%%/*}"
  body="$(mktemp "${TMPDIR:-/tmp}/swarm-fixture-pre.XXXXXX")"
  meta="$(curl -sS -m 20 -o "${body}" -w '%{http_code} %{content_type}' "${url}" 2>/dev/null)" || rc=$?
  code="${meta%% *}"
  type=""
  [[ "${meta}" != *" "* ]] || type="${meta#* }"
  FIXTURE_PREFLIGHT_REASON=""
  if [[ "${rc}" -ne 0 ]]; then
    FIXTURE_PREFLIGHT_REASON="${host} unavailable (curl exit ${rc}), page load not measured"
  elif [[ "${code}" != "200" ]]; then
    FIXTURE_PREFLIGHT_REASON="${host} unavailable (HTTP ${code:-none}), page load not measured"
  elif [[ "${type}" != text/html* ]]; then
    FIXTURE_PREFLIGHT_REASON="${host} answered ${type:-no content type}, not text/html, page load not measured"
  elif ! grep -qF -- "<title>${BROWSER_FIXTURE_TITLE}</title>" "${body}"; then
    FIXTURE_PREFLIGHT_REASON="${host} answered 200 without the fixture's title, page load not measured"
  fi
  rm -f "${body}"
  [[ -z "${FIXTURE_PREFLIGHT_REASON}" ]]
}

# text_file_contains FILE STRING -> 0 when FILE holds STRING, literally.
text_file_contains() {
  [[ -s "$1" ]] && grep -qF -- "$2" "$1"
}

# task_artifact_raw TASK NAME OUTFILE -> the artifact's stored bytes in OUTFILE.
#
# Through the API's raw route, as scripts/acceptance/lib.sh's acc_raw_artifact
# reads them: curl writes the file itself, because a shell variable drops NUL
# bytes and a PNG is full of them. The credential goes on stdin (auth_config),
# never argv. Sets ARTIFACT_HTTP to the status.
ARTIFACT_HTTP=""
task_artifact_raw() {
  local task="$1" name="$2" out="$3" q
  q="$(jq -rn --arg n "${name}" '$n | @uri')"
  ARTIFACT_HTTP=""
  if ! ARTIFACT_HTTP="$(auth_config "$(api_credential)" | curl -sS -m 120 -K - -o "${out}" \
      -w '%{http_code}' "$(api_url)${API_PREFIX}/tasks/${task}/artifacts/raw?name=${q}&disposition=attachment")"; then
    return 1
  fi
  [[ "${ARTIFACT_HTTP}" == "200" ]]
}

# t_check_browser_fixture WHO TIMEOUT [EXTRA_JSON] -> one case: submit the
# fixture task, run it to the end, and assert its title, pixels and text.
# Returns 1 when any assertion failed (each is already recorded), so a caller
# under `set -e` calls it with `|| true`. Sets FIXTURE_TASK_ID and
# FIXTURE_TASK_FINAL (empty when not submitted, or not finished), so a caller
# can check that task's leases came back too.
FIXTURE_TASK_ID=""
FIXTURE_TASK_FINAL=""
t_check_browser_fixture() {
  local who="$1" timeout="$2" extra="${3:-{\}}"
  local run_id text url task final title landed work failed=0
  FIXTURE_TASK_ID=""
  FIXTURE_TASK_FINAL=""
  t_case "${who}: a browser task loads a page the check wrote; its pixels and text come back"
  run_id="$(test_run_id)"
  text="$(browser_fixture_text "${run_id}")"
  url="$(browser_fixture_url "${run_id}")"
  if ! browser_fixture_preflight "${url}"; then
    t_skip "${who}: ${FIXTURE_PREFLIGHT_REASON}"
    return 0
  fi
  # The input is written HERE, as a jq literal on the submit_task line, rather
  # than by a helper: scripts/lib/check-contract-parity.sh reads every
  # submit_task call's input keys against the catalogue's declared inputs, and
  # an input built elsewhere is one it cannot read. The viewport screenshot is
  # fixture.png; extract_text writes page.txt, the text read back below; no
  # final full-page screenshot, because one picture is the evidence.
  if ! task="$(submit_task browser \
      "$(jq -nc --arg r "${run_id}" --arg u "${url}" '{prompt: ("smoke fixture " + $r), url: $u, actions: [{type: "screenshot", name: "fixture.png", full_page: false}], extract_text: true, screenshot: false}')" \
      "${extra}")"; then
    t_fail "${who}: the fixture task could not be submitted; see the API response above"
    return 1
  fi
  # shellcheck disable=SC2034  # read by the scripts that source this library
  FIXTURE_TASK_ID="${task}"
  t_info "${who}: fixture task ${task}, page served by ${SWARM_BROWSER_FIXTURE_BASE:-https://httpbin.org/base64/}"
  if ! final="$(wait_for_state "${task}" "SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED" "${timeout}")"; then
    t_fail "${who}: the fixture task reached no terminal state within ${timeout}s (stuck at ${final})"
    cancel_task "${task}" || t_info "${who}: could not cancel ${task}; cancel it by hand"
    return 1
  fi
  # shellcheck disable=SC2034  # read by the scripts that source this library
  FIXTURE_TASK_FINAL="${final}"
  if [[ "${final}" != "SUCCEEDED" ]]; then
    t_fail "${who}: the fixture task ended ${final}: $(task_field "${task}" '.last_error // "no error recorded"' | redact | head -c 300)"
    return 1
  fi

  title="$(task_field "${task}" '.result_summary.runner.output.title // ""')"
  if [[ "${title}" == "${BROWSER_FIXTURE_TITLE}" ]]; then
    t_pass "${who}: the runner read the fixture's title"
  else
    landed="$(task_field "${task}" '.result_summary.runner.output.final_url // ""' | redact | head -c 160)"
    t_fail "${who}: the page the runner ended on is titled '${title}', not '${BROWSER_FIXTURE_TITLE}' -- it did not load the fixture (final_url: ${landed:-none})"
    failed=1
  fi

  work="$(mktemp -d "${TMPDIR:-/tmp}/swarm-fixture.XXXXXX")"
  if ! task_artifact_raw "${task}" "fixture.png" "${work}/fixture.png"; then
    t_fail "${who}: fixture.png could not be downloaded (HTTP ${ARTIFACT_HTTP:-none})"
    failed=1
  elif png_shows_colour "${work}/fixture.png" "${BROWSER_FIXTURE_RGB}" "${BROWSER_FIXTURE_MIN_FRACTION}" 2>"${work}/png.err"; then
    t_pass "${who}: fixture.png is not blank and shows #${BROWSER_FIXTURE_RGB} ($(tr '\n' ' ' <"${work}/png.err"))"
  else
    t_fail "${who}: $(tr '\n' ' ' <"${work}/png.err")"
    failed=1
  fi
  if ! task_artifact_raw "${task}" "page.txt" "${work}/page.txt"; then
    t_fail "${who}: page.txt (extract_text) could not be downloaded (HTTP ${ARTIFACT_HTTP:-none})"
    failed=1
  elif text_file_contains "${work}/page.txt" "${text}"; then
    t_pass "${who}: page.txt (extract_text) carries '${text}'"
  else
    t_fail "${who}: page.txt (extract_text) does not carry '${text}': $(head -c 160 "${work}/page.txt" | tr '\n' ' ' | redact)"
    failed=1
  fi
  rm -rf "${work}"
  return "${failed}"
}

# profile_extra PROFILE -> what a submission of PROFILE carries BESIDE its
# input, as a compact JSON object to merge into submit_task's EXTRA_JSON.
#
# `{}` for every profile but `generic`, whose smoke fixture is a directory of
# THIS repository (see profile_input): the task clones it. Public, so the clone
# needs no tenant credential (the worker clones anonymously when the tenant
# holds no clone token). The ref is `main`, so a run proves what is merged;
# SWARM_SMOKE_FIXTURE_REPOSITORY and SWARM_SMOKE_FIXTURE_REF point it at a
# fork or a branch. No dispatch block: the default strategy, collect, keeps
# the diff and pushes nothing.
#
# THE FIXTURE IS ECHOED, on stderr (stdout is the JSON the caller merges), with
# where each value came from (#346, #345 review): an override left exported in
# an operator's shell used to make the smoke clone a stale fork or branch with
# nothing in the run to say so. Through `redact`, as any URL an operator
# supplies could carry userinfo.
SMOKE_FIXTURE_REPOSITORY_DEFAULT="https://github.com/bogdan-alexandrescu/SwarmCloud.git"
profile_extra() {
  local profile="$1"
  local url="${SMOKE_FIXTURE_REPOSITORY_DEFAULT}" url_from="the default"
  local ref="main" ref_from="the default"
  case "${profile}" in
    generic)
      if [[ -n "${SWARM_SMOKE_FIXTURE_REPOSITORY:-}" ]]; then
        url="${SWARM_SMOKE_FIXTURE_REPOSITORY}"
        url_from="from SWARM_SMOKE_FIXTURE_REPOSITORY"
      fi
      if [[ -n "${SWARM_SMOKE_FIXTURE_REF:-}" ]]; then
        ref="${SWARM_SMOKE_FIXTURE_REF}"
        ref_from="from SWARM_SMOKE_FIXTURE_REF"
      fi
      info "generic smoke fixture: ${url} (${url_from}) at ref ${ref} (${ref_from})" 2>&1 | redact >&2
      jq -nc \
        --arg u "${url}" \
        --arg r "${ref}" \
        '{repository_url: $u, repository_ref: $r}'
      ;;
    *)
      printf '{}'
      ;;
  esac
}

# submit_task PROFILE [INPUT_JSON] [EXTRA_JSON] -> task id on stdout
submit_task() {
  local profile="$1" input="${2:-{\}}" extra="${3:-{\}}"
  local body response id out
  body="$(jq -nc --arg p "${profile}" --argjson i "${input}" --argjson x "${extra}" \
    '{runner_profile:$p, input:$i} + $x')"
  # api_post is called directly here, NOT as `response="$(api_post ...)"`: that
  # form runs api_post in a subshell, so the API_STATUS it sets dies with the
  # subshell and this function would print whatever API_STATUS held before this
  # call ran -- the exact command-substitution trap CLAUDE.md documents.
  # Redirect to a file instead, as fs_request/api_url already do.
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-submit.XXXXXX")"
  if ! api_post "/tasks" "${body}" >"${out}"; then
    response="$(cat "${out}")"
    rm -f "${out}"
    err "POST ${API_PREFIX}/tasks returned HTTP ${API_STATUS}"
    printf '%s\n' "${response}" | redact >&2
    return 1
  fi
  response="$(cat "${out}")"
  rm -f "${out}"
  id="$(jq -r '.id // .task_id // .task.id // empty' <<<"${response}")"
  [[ -n "${id}" ]] || { err "no task id in the API response"; printf '%s\n' "${response}" | redact >&2; return 1; }
  printf '%s' "${id}"
}

cancel_task() {
  api_post "/tasks/$1/cancel" '{}' >/dev/null
}

# validation_rejected STATUS -- true only when the API's VALIDATOR refused.
#
# The suites prove the API refuses things -- malformed bodies, oversized input,
# a caller-supplied image -- and the proof is a status code. Only two mean the
# request reached validation and was turned away: 422 (`ValidationFailed`, and
# FastAPI's own request-validation answer) and 400 (the `ApiError` base), per
# apps/swarm-api/swarm_api/errors.py and main.py.
#
# NOT "any 4xx". That was the 2026-09-19 fix to "any non-2xx", and it still
# counted 401 (an expired session), 403 (no run.invoker, or IAP refusing the
# credential) and 404 (the wrong address) as the validator having spoken -- so
# the suite passed loudest for a caller the API never evaluated. One definition,
# here, because the three call sites had already drifted apart once:
# smoke-test.sh had `400|422` in one place and a 4xx window in another.
# tests/integration/test_suite_rejection_status.py holds it to exactly this set.
validation_rejected() {
  case "$1" in
    400|422) return 0 ;;
    *)       return 1 ;;
  esac
}

# api_fetch PATH OUTFILE -- a GET whose failure is a failure.
#
# The one correct spelling of `api_get`, wrapped once so no suite has to get it
# right again. Two traps live here:
#
#   * `body="$(api_get ...)"` runs api_get in a SUBSHELL, so the API_STATUS it
#     assigns dies with the subshell and the caller reads whatever status the
#     last in-shell call left behind -- every response then looks like whatever
#     the previous one was. Redirecting to a file keeps the assignment in this
#     shell, which is the same shell the caller is in, because a function is
#     not a subshell.
#   * `api_get ... | jq ...` takes jq's exit status under pipefail, so a 500 with
#     a JSON error body reads as a success with odd-looking data.
#
# Prints the HTTP status and a redacted body on failure, so a caller only has to
# decide what to do about it.
#
# The body is indented by awk, not sed, because awk ends every line it prints.
# swarm-api's JSON error bodies carry no trailing newline, and GNU sed leaves a
# last line exactly as unterminated as it found it -- so the caller's next line
# landed on the end of the body:
#
#     {"code":"unavailable","message":"..."}  FAIL could not read the runner-...
#
# which is a FAIL line that no longer starts with FAIL, in the one place a
# reader scans for it. Caught by tests/integration/test_smoke_backend_matrix.py.
_api_error_body() {
  redact <"$1" | head -n 5 | awk '{ print "       " $0 }' >&2
}

api_fetch() {
  local path="$1" out="$2"
  if api_get "${path}" >"${out}"; then
    return 0
  fi
  err "GET ${API_PREFIX}${path} -> HTTP ${API_STATUS}"
  _api_error_body "${out}"
  return 1
}

# api_send METHOD PATH BODY OUTFILE -- the same discipline for a write.
api_send() {
  local method="$1" path="$2" body="$3" out="$4"
  if api_request "${method}" "${API_PREFIX}${path}" "${body}" >"${out}"; then
    return 0
  fi
  err "${method} ${API_PREFIX}${path} -> HTTP ${API_STATUS}"
  _api_error_body "${out}"
  return 1
}

# submit_workflow BODY_JSON -> the creation response on stdout.
#
# Separate from submit_task because a workflow is the only way to exercise
# `input_from`, which is the seam this platform's headline feature runs through.
# The response carries each step's task id, so the caller never has to guess the
# mapping from step ids to tasks.
submit_workflow() {
  local body="$1" out response
  out="$(mktemp "${TMPDIR:-/tmp}/swarm-wf.XXXXXX")"
  if ! api_post "/workflows" "${body}" >"${out}"; then
    response="$(cat "${out}")"
    rm -f "${out}"
    err "POST ${API_PREFIX}/workflows returned HTTP ${API_STATUS}"
    printf '%s\n' "${response}" | redact >&2
    return 1
  fi
  response="$(cat "${out}")"
  rm -f "${out}"
  if [[ -z "$(jq -r '.workflow.workflow_id // empty' <<<"${response}")" ]]; then
    err "no workflow id in the API response"
    printf '%s\n' "${response}" | redact >&2
    return 1
  fi
  printf '%s' "${response}"
}

# workflow_step_task WORKFLOW_JSON STEP_ID -> the task id that step became.
workflow_step_task() {
  jq -r --arg s "$2" '.workflow.steps[] | select(.step_id == $s) | .task_id // empty' <<<"$1"
}

# Attempts of one task, newest first, straight from Firestore.
#
# `|| return 1` for the reason every other read in this file carries it: these
# are called from inside `if` conditions, where set -e is suppressed, and a
# failed query would otherwise read as "this task never ran".
task_attempts() {
  fs_query attempts "$(fs_field_filter task_id EQUAL "$(jq -nc --arg v "$1" '{stringValue:$v}')")" 50 \
    | jq -c "${FS_JQ} doc" || return 1
}

# Authoritative read, straight from Firestore.
#
# `|| return 1`, not bare `set -e`: wait_for_state below, and wait_until's
# predicates, are called as the condition of an `if` in the *-test.sh scripts
# (`if final="$(wait_for_state ...)"`, `if wait_until ... task_in_state ...`),
# and set -e is suppressed for the whole call tree evaluated as that condition
# -- see common.sh's fs_list for the same note. Without an explicit check here,
# a real Firestore error (403, 500, a dropped connection) is silently read as
# "no matching state yet" and the caller polls it for the full timeout instead
# of failing on the first read.
task_doc() { fs_get "tasks/$1" | jq -c "${FS_JQ} if .fields then doc else null end" || return 1; }
task_state() {
  local doc
  doc="$(task_doc "$1")" || return 1
  printf '%s' "${doc}" | jq -r '.state // "MISSING"'
}
task_field() {
  local doc
  doc="$(task_doc "$1")" || return 1
  printf '%s' "${doc}" | jq -r "$2"
}

# `|| return 1` on every read, for the reason task_doc above already carries
# it -- these three were the ones that did not, and the omission reached the
# assertions.
#
# A failed read makes these print the EMPTY STRING, and `[[ "" -eq 0 ]]` and
# `[[ "" -le N ]]` are both TRUE in bash (confirmed on 3.2.57 and on the
# Alpine bash the verify image ships). The predicates below then answer "the
# pool is drained" and "capacity is back at baseline" having never read
# anything. wait_until calls them as `if "$@"`, which discards the status, so
# nothing downstream could notice either.
#
# The reachable consequence was in race-test.sh:203, polled the instant after
# every cancellation is fired in parallel -- exactly when Firestore is most
# likely to answer 429. One failed read there printed
# `PASS  runner:mock active is 0` and exited 0, certifying that no lease
# survived the cancellation storm without having looked at the pool.
#
# A 401 is no safer despite fs_request routing it through die: the die runs in
# the left-hand side of the pipeline, which is a subshell, so only the subshell
# exits and the false PASS still happens.
pool_doc() { fs_get "pools/$1" | jq -c "${FS_JQ} if .fields then doc else null end" || return 1; }
pool_active() {
  local doc
  doc="$(pool_doc "$1")" || return 1
  # A pool document that does not exist is NOT a drained pool. fs_get opts into
  # 404, and `null | .active // 0` is 0, so an absent pool used to read as
  # empty -- the same conflation, one level down.
  [[ -n "${doc}" && "${doc}" != "null" ]] || return 1
  printf '%s' "${doc}" | jq -r '.active // 0'
}
pool_limit() { pool_doc "$1" | jq -r "${FS_JQ} effective_limit" 2>/dev/null || pool_doc "$1" | jq -r '.hard_limit // 0'; }

# Number of tasks currently holding capacity, from the state machine's own
# definition of CONCURRENCY_STATES.
holding_capacity() {
  local total=0 state n
  for state in LEASED DISPATCHED STARTING RUNNING; do
    # Unchecked, a failed count folded in as 0 and UNDERSTATED the total --
    # the direction that turns a real leak into a pass.
    n="$(fs_count tasks state EQUAL "${state}")" || return 1
    [[ -n "${n}" ]] || return 1
    total=$(( total + n ))
  done
  printf '%s' "${total}"
}

active_leases() {
  local n
  n="$(fs_count_where leases "$(fs_null_filter released_at IS_NULL)")" || return 1
  [[ -n "${n}" ]] || return 1
  printf '%s' "${n}"
}

# Predicates, so wait_until can be given a condition rather than a shell string.
#
# Each captures the read FIRST and fails the predicate when the read failed,
# rather than letting the empty string fall into a numeric comparison that is
# true for every bound. A failed read now means "not satisfied, keep polling",
# and if it persists the wait times out loudly -- which is the honest outcome.
holding_at_most() {
  local n; n="$(holding_capacity)" || return 1
  [[ -n "${n}" ]] || return 1
  [[ "${n}" -le "$1" ]]
}
pool_drained() {
  local n; n="$(pool_active "$1")" || return 1
  [[ -n "${n}" ]] || return 1
  [[ "${n}" -eq 0 ]]
}
leases_at_most() {
  local n; n="$(active_leases)" || return 1
  [[ -n "${n}" ]] || return 1
  [[ "${n}" -le "$1" ]]
}
task_is_terminal() {
  case "$(task_state "$1")" in
    SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED) return 0 ;;
    *) return 1 ;;
  esac
}
task_in_state() {
  local id="$1" wanted="$2" state
  state="$(task_state "${id}")"
  [[ "${state}" =~ ^(${wanted})$ ]]
}

# wait_for_state TASK_ID "A|B|C" TIMEOUT_SECONDS
wait_for_state() {
  local task_id="$1" wanted="$2" timeout="${3:-300}"
  local deadline=$(( $(date -u +%s) + timeout )) state
  while :; do
    # Checked explicitly, not `state="$(task_state ...)"` on its own: callers
    # invoke wait_for_state as an `if` condition
    # (`if final="$(wait_for_state ...)"`), which suppresses set -e for
    # everything evaluated underneath it, including this loop. Without this
    # check a real Firestore error here reads as "not in the wanted state yet"
    # and burns the full timeout before reporting a misleading state.
    if ! state="$(task_state "${task_id}")"; then
      die "could not read the state of task ${task_id}; see the Firestore error above -- this is a failed read, not evidence the task never reached ${wanted}"
    fi
    if [[ "${state}" =~ ^(${wanted})$ ]]; then
      printf '%s' "${state}"
      return 0
    fi
    if [[ "$(date -u +%s)" -ge "${deadline}" ]]; then
      printf '%s' "${state}"
      return 1
    fi
    # INJECTABLE, defaulting to the 2s this has always polled at against a real
    # deployment. The offline suite drives this loop against a fake gcloud
    # where nothing ever moves, so every negative case burns its whole timeout
    # two seconds at a time -- which is where 700 of `make test`'s 790 seconds
    # went. Lowering it changes how OFTEN the state is read and nothing about
    # what is read or concluded, so a fake that answers instantly is entitled
    # to be polled instantly.
    sleep "${SWARM_POLL_INTERVAL_SECONDS:-2}"
  done
}

# wait_until DESCRIPTION TIMEOUT COMMAND...
wait_until() {
  local what="$1" timeout="$2"; shift 2
  local deadline=$(( $(date -u +%s) + timeout ))
  while :; do
    if "$@"; then return 0; fi
    if [[ "$(date -u +%s)" -ge "${deadline}" ]]; then
      t_info "timed out after ${timeout}s waiting for: ${what}"
      return 1
    fi
    sleep 2
  done
}

task_events() {
  fs_list "tasks/$1/events" 200 | jq -c "${FS_JQ} .[] | doc"
}

# Every lease a task has held, one id per line, read from the task's OWN EVENTS.
#
# NOT from the task's `current_lease_id`. The worker clears that field in the
# same write that makes the task terminal or PARKED
# (apps/agent-worker/agent_worker/control.py, finish() and park()), so on a
# finished task it is always null. Every suite that read it misjudged the
# lease: prove-gke-dispatch.sh failed on every successful task, smoke-test.sh
# skipped its lease check without a word, and failure-test.sh printed PASS
# "nothing to release". The scheduler records each lease it grants as a
# top-level `lease_id` on the `lease_acquired` event (scheduler/loop.py), and
# again on `dispatched` (scheduler/store.py mark_dispatched).
#
# Fails on a read that did not answer. An empty answer means the events name
# no lease, which is not the same as "could not tell".
task_lease_ids() {
  local events
  events="$(task_events "$1")" || return 1
  [[ -n "${events}" ]] || return 0
  printf '%s\n' "${events}" \
    | jq -r 'select(.type == "lease_acquired" or .type == "dispatched") | .lease_id // empty' \
    | awk 'NF && !seen[$0]++'
}

# lease_released_at LEASE_ID -> when it was released; "null" while it still
# holds capacity; "missing" when there is no such lease. Fails on a read that
# did not answer.
lease_released_at() {
  local doc
  doc="$(fs_get "leases/$1")" || return 1
  printf '%s' "${doc}" | jq -r "${FS_JQ} if .fields then (doc.released_at // \"null\") else \"missing\" end"
}

# t_check_leases_released TASK_ID WAIT_SECONDS ON_NONE
#
# One PASS for each lease TASK_ID has held that is released, and one FAIL for
# each lease that is not. ON_NONE decides a task whose events name no lease at
# all: "fail" for a task that ran (it was admitted, so it held one), "pass" for
# one that may have failed before admission.
#
# IT WAITS, up to WAIT_SECONDS, while any lease still reads as held. The worker
# writes the terminal state FIRST and releases the lease after that
# (control.finish: transition, record_attempt_end, emit, release_lease). A read
# taken the moment a task reads SUCCEEDED can land in that gap and report a
# leak that is not one. A lease with no document is not waited for, because
# nothing will create it.
#
# Returns 1, after recording a FAIL, when a read did not answer. A failed read
# is not evidence either way, and it must never become a PASS.
t_check_leases_released() {
  local task_id="$1" wait_s="$2" on_none="$3"
  local ids id at held results deadline
  if ! ids="$(task_lease_ids "${task_id}")"; then
    t_fail "could not read the events of ${task_id}, so its leases cannot be named (a failed read, not evidence either way)"
    return 1
  fi
  if [[ -z "${ids}" ]]; then
    if [[ "${on_none}" == "pass" ]]; then
      t_pass "its events record no lease: nothing to release (it never reached admission)"
    else
      t_fail "its events record no lease, so whether capacity was returned cannot be checked"
    fi
    return 0
  fi
  deadline=$(( $(date -u +%s) + wait_s ))
  while :; do
    results=""
    held=0
    while IFS= read -r id; do
      [[ -n "${id}" ]] || continue
      if ! at="$(lease_released_at "${id}")"; then
        t_fail "could not read lease ${id} (a failed read, not evidence either way)"
        return 1
      fi
      if [[ "${at}" == "null" ]]; then
        held=$(( held + 1 ))
      fi
      results="${results}${id} ${at}"$'\n'
    done <<<"${ids}"
    if [[ "${held}" -eq 0 || "$(date -u +%s)" -ge "${deadline}" ]]; then
      break
    fi
    sleep "${SWARM_POLL_INTERVAL_SECONDS:-2}"
  done
  while IFS=' ' read -r id at; do
    [[ -n "${id}" ]] || continue
    case "${at}" in
      null)    t_fail "lease ${id} is still holding capacity after ${wait_s}s of waiting (released_at=null)" ;;
      missing) t_fail "lease ${id} is named on the task's events but does not exist, so its release cannot be shown" ;;
      *)       t_pass "lease ${id} released at ${at}" ;;
    esac
  done <<<"${results}"
  return 0
}

# own_capacity_holders TASK_ID... -> one line per unit of capacity THOSE tasks
# still hold: `ID state STATE` for a task in CONCURRENCY_STATES, `ID lease
# LEASE_ID` for each lease its events name that is not released. Nothing when
# they hold nothing.
#
# WHY NOT holding_capacity. That counts every task in the tenant, and the smoke
# suite runs in `eng`, where the owner's SwarmCloud lanes run at the same time.
# Release 36977343635 (2026-10-02) failed "tasks holding capacity after
# completion: 1 > 0" in the same run that printed the smoke task's own lease
# released: the 1 was a lane that started during the smoke. A baseline does not
# fix that, because the other work moves while the suite runs. Asking only
# about the tasks the suite submitted is exact.
#
# BOTH the state and the leases, because each is half of the record: the
# worker writes the terminal state first and releases the lease after it
# (control.finish), so a terminal task whose lease never came back is the leak
# this exists to catch, and a task still in a holding state is capacity held
# whatever its leases say.
#
# Fails -- never prints nothing -- when given no task, when a read did not
# answer, when a task does not exist, or when a lease its events name does not
# exist: none of those is evidence that capacity came back.
own_capacity_holders() {
  local id state held_state lease leases at
  [[ $# -gt 0 ]] || return 1
  for id in "$@"; do
    [[ -n "${id}" ]] || return 1
    state="$(task_state "${id}")" || return 1
    [[ -n "${state}" && "${state}" != "MISSING" ]] || return 1
    for held_state in "${CONCURRENCY_STATES[@]}"; do
      if [[ "${state}" == "${held_state}" ]]; then
        printf '%s state %s\n' "${id}" "${state}"
      fi
    done
    leases="$(task_lease_ids "${id}")" || return 1
    while IFS= read -r lease; do
      [[ -n "${lease}" ]] || continue
      at="$(lease_released_at "${lease}")" || return 1
      case "${at}" in
        null)       printf '%s lease %s\n' "${id}" "${lease}" ;;
        missing|"") return 1 ;;
      esac
    done <<<"${leases}"
  done
  return 0
}

# Predicate for wait_until: the tasks hold nothing, read and confirmed.
own_capacity_released() {
  local holders
  holders="$(own_capacity_holders "$@")" || return 1
  [[ -z "${holders}" ]]
}

# Everything a test creates is tagged so cleanup can find it again and so a
# human reading Firestore can tell test traffic from real work.
test_run_id() { printf 'test-%s-%s' "${SUITE_NAME}" "$(date -u +%Y%m%d%H%M%S)"; }

cancel_all() {
  local id
  for id in "$@"; do
    [[ -n "${id}" ]] || continue
    cancel_task "${id}" >/dev/null 2>&1 || true
  done
}
