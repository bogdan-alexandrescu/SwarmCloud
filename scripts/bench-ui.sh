#!/usr/bin/env bash
# What the web UI costs in a real browser: paint, load, and every /v1 fetch.
#
# WHY A BROWSER AND NOT curl
# --------------------------
# `scripts/bench-api.sh` measures the API from a shell. That is the server's
# number. It is not what an operator experiences, and the difference is where
# this platform's UI problems live: the Overview screen issues a dozen /v1
# reads, several of them in parallel, some blocked behind others, and the slow
# one decides when the screen is usable. The UI's own data-source strip already
# shows per-route latency -- /v1/stats at 1711ms next to routes under 300ms --
# but it shows the LAST attempt, on screen, to whoever happens to be looking.
# Nothing records it, so nothing can compare it to last week.
#
# The Performance API is used rather than the UI's internal probe registry, for
# two reasons: it needs no change to the app (`apps/` is another track), and it
# measures what the browser did rather than what the app believes it did.
#
# FIXTURES ARE NOT A MEASUREMENT
# ------------------------------
# `npm run dev` with no VITE_LIVE serves fixtures and never touches the
# network, so a fixture run has zero /v1 resource entries. That run measures
# render cost honestly and measures API cost NOT AT ALL -- so the API metrics
# are recorded as not-measured with that reason rather than omitted. Omitting
# them would make a fixture run look like a very fast live one, which is the
# single most likely way this benchmark could lie.
#
# OPERATING THE DRIVER (hard-won; see docs/benchmarks.md)
#   * BROWSE_HEADED=1 and a long BROWSE_IDLE_TIMEOUT go in the ENVIRONMENT.
#     Never pass --headed: the flag writes a configHash and every later call
#     must match it or the daemon restarts underneath you.
#   * headed mode keeps a cookie jar (launchPersistentContext); `launched` mode
#     does not, so a silent fall back to it loses the session.
#   * The driver acts on the ACTIVE tab, and the active tab drifts. This pins
#     one by id and re-pins before every command, and the page-side probe
#     refuses to measure unless location.origin is exactly the target.
#
# COST TO RUN
# -----------
# Free and about a minute: it drives a browser against a UI you are already
# running, creates nothing and submits nothing. The only prerequisite is a
# reachable UI -- `npm run dev` in apps/swarm-ui, or a deployed one.
#
# Usage:
#   scripts/bench-ui.sh [--url http://localhost:5173] [--rounds 3]
#                       [--screen '#overview/now']...
#   scripts/bench-ui.sh --from-json capture.jsonl   # re-analyse a capture

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/benchlib.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/benchlib.sh"

load_env

UI_URL="${SWARM_UI_URL:-http://localhost:5173}"
ROUNDS=3
FROM_JSON=""
SCREENS=()
BROWSE_BIN="${SWARM_BROWSE_BIN:-${HOME}/.claude/skills/gstack/browse/dist/browse}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)       UI_URL="${2%/}"; shift 2 ;;
    --rounds)    ROUNDS="$2"; shift 2 ;;
    --screen)    SCREENS+=("$2"); shift 2 ;;
    --from-json) FROM_JSON="$2"; shift 2 ;;
    -h|--help)   sed -n '2,50p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# The six sections of the redesigned nav, one representative tab each. Listed
# rather than discovered because a screen that stops being reachable must make
# this run RED, and a list read from the app at run time would simply get
# shorter.
if [[ "${#SCREENS[@]}" -eq 0 ]]; then
  SCREENS=(
    "#overview/now"
    "#agents/running"
    "#agents/workflows"
    "#runtimes/catalogue"
    "#pools/pools"
    "#history/timeline"
  )
fi

step "Web UI performance: ${UI_URL}"
bench_init ui

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-bench-ui.XXXXXX")"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT INT TERM

CAPTURE="${BUILD_DIR}/bench-ui-capture-${ENVIRONMENT}.jsonl"

# ---------------------------------------------------------------------------
# Capture, unless re-analysing one that already exists.
if [[ -z "${FROM_JSON}" ]]; then
  [[ -x "${BROWSE_BIN}" ]] || die "no browse driver at ${BROWSE_BIN}; set SWARM_BROWSE_BIN"
  require_cmd curl

  if ! curl -sS -m 10 -o /dev/null "${UI_URL}/"; then
    die "the UI at ${UI_URL} did not answer. That is an unreachable UI, not a slow one."
  fi

  # In the ENVIRONMENT, never as --headed. See the header.
  export BROWSE_HEADED=1
  export BROWSE_IDLE_TIMEOUT="${BROWSE_IDLE_TIMEOUT:-900000}"

  # UNDER build/, NOT under $TMPDIR. The driver refuses to read a file outside
  # its allowed roots ("Path must be within: /private/tmp, <cwd>"), and on a Mac
  # $TMPDIR is /var/folders/... -- so `eval` failed silently, `js` read back the
  # PREVIOUS payload, and the first run of this collector recorded one screen's
  # numbers for all six. The nonce below now catches that class outright; this
  # keeps it from happening in the first place.
  PROBE_DIR="${BUILD_DIR}/bench-ui"
  mkdir -p "${PROBE_DIR}"
  PROBE="${PROBE_DIR}/probe.js"

  # WARM THE DAEMON FIRST. It autostarts on the first command, and that start
  # takes seconds -- during which `eval` does not run and the capture comes
  # back empty. The first screen of the first run was lost to exactly that, and
  # recording the driver's own cold start as the UI's is the kind of number
  # that gets quoted in a meeting.
  "${BROWSE_BIN}" tabs >/dev/null 2>&1 || true

  # One dedicated tab, pinned by id for the whole run. Re-pinned before every
  # command because the daemon is shared and the active tab drifts.
  # `|| true` ON THE PIPELINE, and the check afterwards.
  #
  # `set -o pipefail` is in force, so if the driver is killed -- which happened
  # twice during this collector's development, the daemon being shared with
  # another session -- the pipeline fails, the command substitution fails, and
  # `set -e` ends the script RIGHT HERE with exit 1 and not one word of
  # explanation. That is the same "a failure that renders as nothing" shape the
  # rest of this repository has been hunting, produced by the benchmark meant
  # to catch it. The status is swallowed deliberately so the emptiness below
  # can be reported as the real cause.
  OPENED="$("${BROWSE_BIN}" newtab "${UI_URL}/${SCREENS[0]}" 2>&1 | tail -n 1 || true)"
  TAB_ID="$(printf '%s' "${OPENED}" | sed -n 's/.*Opened tab \([0-9][0-9]*\).*/\1/p')"
  if [[ -z "${TAB_ID}" ]]; then
    err "the browse driver did not open a tab. It said:"
    printf '%s\n' "${OPENED:-（nothing at all — the driver produced no output）}" | sed 's/^/     /' >&2
    die "that is a driver that would not start, not a slow UI. Check that no other session is restarting the shared daemon, then retry."
  fi
  info "pinned tab ${TAB_ID}"

  # Re-pin, and re-OPEN if the id has gone stale.
  #
  # Observed on the first six-round run: the daemon is shared, and partway
  # through, `tab <id>` stopped landing on our page -- the probe's origin guard
  # reported the active tab as http://127.0.0.1:10703 (the driver's own welcome
  # page) and once as `null`. The guard did its job and those captures were
  # recorded as not-measured rather than as the UI. This makes the run recover
  # instead of losing every remaining sample: a tab id that no longer selects
  # is replaced with a fresh one.
  #
  # It deliberately does NOT retry until the numbers look right. A capture that
  # still fails after this is recorded as not-measured and fails the gate.
  repin() {
    if "${BROWSE_BIN}" tab "${TAB_ID}" >/dev/null 2>&1; then return 0; fi
    local reopened new_id
    reopened="$("${BROWSE_BIN}" newtab "${UI_URL}/${SCREENS[0]}" 2>&1 | tail -n 1 || true)"
    new_id="$(printf '%s' "${reopened}" | sed -n 's/.*Opened tab \([0-9][0-9]*\).*/\1/p')"
    if [[ -n "${new_id}" ]]; then
      warn "tab ${TAB_ID} no longer selects; re-pinned as ${new_id}"
      TAB_ID="${new_id}"
    fi
  }

  : >"${CAPTURE}"
  # The counter is unused on purpose: every round measures the same screens
  # and the round number is not part of a sample's identity.
  for _round in $(seq 1 "${ROUNDS}"); do
    for screen in "${SCREENS[@]}"; do
      # ONE RETRY, and no more. A capture can fail for a reason that is the
      # harness's and not the platform's -- the daemon restarting under a
      # shared session, a navigation that had not committed. Retrying once
      # removes that noise. Retrying until it works would remove the signal
      # too, so a second failure is recorded as not-measured and fails the gate
      # exactly as the first would have.
      payload=""
      evalout=""
      for _attempt in 1 2; do
      # A FRESH NONCE PER CAPTURE. See the probe's header: without it a failed
      # `eval` leaves the previous screen's payload on the window and the
      # reader records it as this screen's.
      nonce="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
      sed -e "s|__SWARM_EXPECTED_ORIGIN__|${UI_URL}|" -e "s|__SWARM_NONCE__|${nonce}|" \
        "${REPO_ROOT}/scripts/lib/bench-ui-probe.js" >"${PROBE}"

      repin
      # A UNIQUE QUERY STRING BEFORE THE HASH, so each capture is a genuinely
      # NEW DOCUMENT.
      #
      # This is not a cache-busting nicety, it is the difference between a
      # measurement and a fiction. `goto` to a URL that differs only in its
      # HASH does not reload: the SPA router handles it in place. The first
      # working version of this collector did exactly that and the numbers said
      # so plainly -- `ui.nav.dom_content_loaded` was 83.5ms for all six
      # screens, to the tenth of a millisecond, because all six were reporting
      # the one navigation that really happened. Worse, the resource buffer
      # ACCUMULATES on a reused document, so each screen was credited with
      # every request every earlier screen had made.
      #
      # The nonce is already unique per capture, so it doubles as the query.
      # The app ignores it, the origin is unchanged so the guard still holds,
      # and the hash router still gets its hash.
      "${BROWSE_BIN}" goto "${UI_URL}/?bench=${nonce}${screen}" >/dev/null 2>&1 || true
      "${BROWSE_BIN}" wait --networkidle >/dev/null 2>&1 || true
      repin
      evalout="$("${BROWSE_BIN}" eval "${PROBE}" 2>&1 || true)"
      repin
      raw="$("${BROWSE_BIN}" js 'window.__swarmBench' 2>/dev/null || true)"

      # The driver prints the value with its own decoration; the JSON object is
      # what is wanted. If nothing parseable comes back, that is recorded as a
      # failed capture -- not as a screen with no requests.
      candidate="$(printf '%s' "${raw}" | sed -n 's/^[^{]*\({.*}\)[^}]*$/\1/p' | head -n 1)"
      if [[ -z "${candidate}" ]] || ! printf '%s' "${candidate}" | jq -e . >/dev/null 2>&1; then
        payload=""
        continue
      fi
      # The nonce decides. A payload without THIS capture's nonce is the
      # previous screen's, and accepting it is how every screen ends up
      # carrying one screen's numbers.
      if [[ "$(printf '%s' "${candidate}" | jq -r '.nonce // ""')" != "${nonce}" ]]; then
        payload=""
        evalout="stale payload: the probe did not run on this navigation. The driver said: ${evalout}"
        continue
      fi
      payload="${candidate}"
      break
      done

      if [[ -z "${payload}" ]]; then
        jq -nc --arg s "${screen}" --arg e "${evalout}" \
          '{screen:$s, ok:false, reason:("the page probe produced no payload for this navigation, twice. The driver said: " + $e)}' \
          >>"${CAPTURE}"
        continue
      fi
      printf '%s' "${payload}" | jq -c --arg s "${screen}" '. + {screen:$s}' >>"${CAPTURE}"
    done
  done
  info "capture -> ${CAPTURE}"
else
  [[ -f "${FROM_JSON}" ]] || die "no capture at ${FROM_JSON}"
  CAPTURE="${FROM_JSON}"
  info "re-analysing ${CAPTURE}"
fi

# ---------------------------------------------------------------------------
# Capture -> samples. Everything below is pure translation, which is why
# --from-json exists: it is the half that can be tested without a browser.
[[ -s "${CAPTURE}" ]] || die "the capture is empty; nothing was measured"

while IFS= read -r line; do
  [[ -n "${line}" ]] || continue
  screen="$(printf '%s' "${line}" | jq -r '.screen // "unknown"')"
  labels="$(bench_label screen "${screen}")"

  if [[ "$(printf '%s' "${line}" | jq -r '.ok == true')" != "true" ]]; then
    reason="$(printf '%s' "${line}" | jq -r '.reason // "the probe reported a failure"')"
    for metric in ui.paint.first_contentful ui.nav.dom_content_loaded ui.nav.load ui.fetch; do
      bench_missing "${metric}" ms "${reason}" "${labels}"
    done
    continue
  fi

  for pair in first_contentful_paint_ms:ui.paint.first_contentful \
              nav.dom_content_loaded_ms:ui.nav.dom_content_loaded \
              nav.load_ms:ui.nav.load; do
    path="${pair%%:*}"; metric="${pair##*:}"
    value="$(printf '%s' "${line}" | jq -r ".${path} // empty")"
    if [[ -z "${value}" || "${value}" == "null" ]]; then
      bench_missing "${metric}" ms "the browser reported no ${path} for this screen" "${labels}"
    else
      bench_sample "${metric}" ms "${value}" "${labels}"
    fi
  done

  # A screen that made no /v1 request. On a live UI that is a finding; on a
  # fixture run it is the whole story. Either way it is NOT a fast screen.
  count="$(printf '%s' "${line}" | jq -r '(.api // []) | length')"
  if [[ "${count}" -eq 0 ]]; then
    bench_missing ui.fetch ms \
      "this screen issued no same-origin /v1 request -- a fixture build, or a screen whose reads did not fire" \
      "${labels}"
    continue
  fi

  while IFS= read -r entry; do
    [[ -n "${entry}" ]] || continue
    route="$(printf '%s' "${entry}" | jq -r '.route')"
    rlabels="$(bench_label screen "${screen}" route "${route}")"
    duration="$(printf '%s' "${entry}" | jq -r '.duration // empty')"
    if [[ -z "${duration}" || "${duration}" == "null" ]]; then
      bench_missing ui.fetch ms "the browser reported no duration for ${route}" "${rlabels}"
    else
      bench_sample ui.fetch ms "${duration}" "${rlabels}"
    fi
  done < <(printf '%s' "${line}" | jq -c '(.api // []) | .[]')
done <"${CAPTURE}"

bench_finish
