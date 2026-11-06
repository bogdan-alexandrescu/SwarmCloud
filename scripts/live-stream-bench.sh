#!/usr/bin/env bash
# The live view, measured end to end: gate -> relay -> viewer
# (LIVE-STREAM-SPEED, part of #2688).
#
#   scripts/live-stream-bench.sh [--seconds 600] [--link-mbps 8] [--control]
#                                [--chromium] [--json-dir DIR]
#
# WHY. On 2026-11-02 the owner watched browser agents and saw the live views
# connect slowly, reconnect every 5-17 s and move at under a frame a second.
# This runs the real relay (swarm_live), the real gate tunnel and RFB
# session (agent_worker.livegate) and a viewer that speaks as noVNC does, and
# prints, for today's lossless Tight and for the gate's adaptive JPEG
# (SWARM_LIVE_JPEG_QUALITY), the time to first frame, frames and bytes a
# second while a 1440x987 page moves, and the reconnects over the run. The
# targets: first frame under 2 s, at least 10 frames a second, no reconnect
# in 10 minutes. scripts/live_stream_bench.py says what it measures and what
# it cannot see.
#
# --chromium draws the page with a real Chromium on a real Xvfb (both are in
# agent-runtime-browser); without it the page is the gate bench's synthetic
# document with a moving box. Nothing here reaches a cloud project, and no
# key or ticket is printed: they are made at runtime inside the helper.
set -euo pipefail

# This script touches no project: it measures the relay on this machine. It says so, so
# common.sh (no default project, DAYONE-DESAGA-2) does not stop on an unset
# PROJECT_ID.
SWARM_NO_PROJECT=1
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SECONDS_TO_WATCH=600
LINK_MBPS=0
CONTROL=()
CHROMIUM=0
JSON_DIR=""
#: The JPEG quality the comparison runs the switch at; 0 is always run first.
JPEG_QUALITY="${SWARM_LIVE_JPEG_QUALITY:-70}"
#: Pillow, as the browser image pins it (images/agent-runtime-browser/Dockerfile).
PILLOW_PIN="pillow==13.0.0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --seconds) SECONDS_TO_WATCH="$2"; shift 2 ;;
    --link-mbps) LINK_MBPS="$2"; shift 2 ;;
    --control) CONTROL=(--control); shift ;;
    --chromium) CHROMIUM=1; shift ;;
    --json-dir) JSON_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) err "unknown argument: $1"; exit 2 ;;
  esac
done

UV="$(prefer_local_bin uv)"
SOURCE=(--source animate)
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/live-stream-bench.XXXXXX")"
PIDS=()
cleanup() {
  local pid
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "${pid}" 2>/dev/null || true; done
  rm -rf "${SCRATCH}"
}
trap cleanup EXIT

if [[ "${CHROMIUM}" == 1 ]]; then
  XVFB="$(command -v Xvfb || true)"
  BROWSER="$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)"
  if [[ -z "${XVFB}" || -z "${BROWSER}" ]]; then
    err "--chromium needs Xvfb and Chromium on PATH (the agent-runtime-browser image has both)"
    exit 2
  fi
  # A page that never stops moving: a box sliding and turning, and a counter.
  cat >"${SCRATCH}/page.html" <<'HTML'
<!doctype html><meta charset="utf-8"><title>live-stream-bench</title>
<style>
  body { margin: 0; font: 15px/1.5 sans-serif; background: #fff; color: #24292f }
  p { max-width: 860px; margin: 16px 40px }
  .box { position: absolute; top: 300px; left: 360px; width: 720px; height: 400px;
         background: linear-gradient(120deg, #0969da, #8250df, #bf3989, #fb8500);
         background-size: 400% 400%; animation: slide 3s linear infinite alternate }
  .dot { position: absolute; top: 160px; width: 80px; height: 80px; border-radius: 40px;
         background: #1a7f37; animation: run 2s ease-in-out infinite alternate }
  @keyframes slide { from { background-position: 0% 50% } to { background-position: 100% 50% } }
  @keyframes run { from { left: 40px } to { left: 1300px } }
</style>
<p id="t"></p><div class="box"></div><div class="dot"></div>
<script>
  const t = document.getElementById('t'); let n = 0
  setInterval(() => { t.textContent = 'frame ' + (++n) + ' ' + 'lorem ipsum '.repeat(40) }, 50)
</script>
HTML
  DISPLAY_NO=97
  "${XVFB}" ":${DISPLAY_NO}" -screen 0 1440x987x24 -fbdir "${SCRATCH}" -nolisten tcp >/dev/null 2>&1 &
  PIDS+=("$!")
  for _ in $(seq 1 50); do [[ -e "${SCRATCH}/Xvfb_screen0" ]] && break; sleep 0.1; done
  DISPLAY=":${DISPLAY_NO}" "${BROWSER}" --no-first-run --no-default-browser-check --disable-gpu \
    --user-data-dir="${SCRATCH}/profile" --window-position=0,0 --window-size=1440,987 --kiosk \
    "file://${SCRATCH}/page.html" >/dev/null 2>&1 &
  PIDS+=("$!")
  sleep 3
  SOURCE=(--source "xvfb:${SCRATCH}/Xvfb_screen0")
fi

run_one() {
  local quality="$1" json=()
  if [[ -n "${JSON_DIR}" ]]; then
    mkdir -p "${JSON_DIR}"
    json=(--json "${JSON_DIR}/live-stream-jpeg${quality}.json")
  fi
  info "live stream: ${SECONDS_TO_WATCH}s, JPEG quality ${quality} (0 is lossless only), link ${LINK_MBPS} Mbit/s"
  (cd "${REPO_ROOT}" && "${UV}" run --quiet --with "${PILLOW_PIN}" python scripts/live_stream_bench.py \
    --seconds "${SECONDS_TO_WATCH}" --jpeg "${quality}" --link-mbps "${LINK_MBPS}" \
    "${SOURCE[@]}" ${CONTROL[@]+"${CONTROL[@]}"} ${json[@]+"${json[@]}"})
}

run_one 0
if [[ "${JPEG_QUALITY}" != 0 ]]; then
  run_one "${JPEG_QUALITY}"
fi
