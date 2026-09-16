#!/usr/bin/env bash
# The local development loop. Entry point: `make dev`.
#
# This uses the gcloud Firestore emulator rather than docker-compose, because the
# Docker daemon on the reference workstation is broken and because the emulator
# is the only local thing that behaves like the real transaction semantics the
# admission path depends on. docker-compose.yml exists for operators whose Docker
# works, and is the secondary path.
#
# It starts the emulator, waits for it to answer, seeds the slot pools the
# scheduler needs, and then runs whichever service you asked for with
# FIRESTORE_EMULATOR_HOST set. No cloud credentials are used and nothing touches
# saga-agents-staging.
#
# Usage: scripts/lib/dev.sh [api|scheduler|quota-broker|emulator] [--port 8080]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

TARGET="${1:-api}"
[[ $# -gt 0 ]] && shift
EMULATOR_PORT="${FIRESTORE_EMULATOR_PORT:-8080}"
APP_PORT="${DEV_APP_PORT:-8000}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)     EMULATOR_PORT="$2"; shift 2 ;;
    --app-port) APP_PORT="$2"; shift 2 ;;
    -h|--help)  sed -n '2,15p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd gcloud uv curl jq

EMULATOR_HOST="127.0.0.1:${EMULATOR_PORT}"
EMULATOR_LOG="${BUILD_DIR}/firestore-emulator.log"
DEV_PROJECT="${DEV_PROJECT_ID:-swarm-local}"

if ! gcloud components list --only-local-state --format='value(id)' 2>/dev/null \
     | grep -q 'cloud-firestore-emulator'; then
  warn "the Firestore emulator component is not installed"
  die "install it with: gcloud components install cloud-firestore-emulator"
fi

# The emulator is a Java program. On a machine with no JRE, gcloud's error is
# buried in a log file, so check for it here where it can say what to install.
# Verified 2026-09-15 on the reference workstation: /usr/bin/java is Apple's stub
# that only offers to install a JRE, so `command -v java` is not enough.
if ! java -version >/dev/null 2>&1; then
  err "the Firestore emulator needs a Java 8+ runtime, and this machine has none"
  err "install one with:  brew install --cask temurin"
  err "or use the container path instead:  docker compose up firestore"
  die "cannot start the emulator without a JRE"
fi

emulator_up() { curl -sS -m 2 "http://${EMULATOR_HOST}/" >/dev/null 2>&1; }

EMULATOR_PID=""
stop_emulator() {
  if [[ -n "${EMULATOR_PID}" ]] && kill -0 "${EMULATOR_PID}" 2>/dev/null; then
    info "stopping the emulator (pid ${EMULATOR_PID})"
    kill "${EMULATOR_PID}" 2>/dev/null || true
    wait "${EMULATOR_PID}" 2>/dev/null || true
  fi
}
trap stop_emulator EXIT INT TERM

step "Firestore emulator"
if emulator_up; then
  ok "already running on ${EMULATOR_HOST}"
else
  info "starting on ${EMULATOR_HOST} (log: ${EMULATOR_LOG})"
  gcloud emulators firestore start \
    --host-port="${EMULATOR_HOST}" \
    --database-mode=firestore-native \
    >"${EMULATOR_LOG}" 2>&1 &
  EMULATOR_PID=$!
  for _ in $(seq 1 60); do
    emulator_up && break
    sleep 1
  done
  emulator_up || { tail -n 20 "${EMULATOR_LOG}" >&2; die "the emulator did not start"; }
  ok "emulator ready (pid ${EMULATOR_PID})"
fi

export FIRESTORE_EMULATOR_HOST="${EMULATOR_HOST}"
export PROJECT_ID="${DEV_PROJECT}"
export FIRESTORE_DATABASE="${DEV_FIRESTORE_DATABASE:-(default)}"
export ENVIRONMENT="local"
export ALLOWED_DOMAINS="${ALLOWED_DOMAINS:-saga.xyz}"

# Seed the pools the scheduler expects. Against the emulator there are no
# credentials involved, so this is plain unauthenticated REST.
seed_pool() {
  local name="$1" limit="$2"
  curl -sS -m 5 -X PATCH \
    "http://${EMULATOR_HOST}/v1/projects/${DEV_PROJECT}/databases/${FIRESTORE_DATABASE}/documents/pools/${name}?updateMask.fieldPaths=name&updateMask.fieldPaths=hard_limit&updateMask.fieldPaths=active&updateMask.fieldPaths=enabled" \
    -H 'Content-Type: application/json' \
    -d "{\"fields\":{\"name\":{\"stringValue\":\"${name}\"},\"hard_limit\":{\"integerValue\":\"${limit}\"},\"active\":{\"integerValue\":\"0\"},\"enabled\":{\"booleanValue\":true}}}" \
    >/dev/null 2>&1 || warn "could not seed pool ${name}"
}

step "Seeding local pools"
seed_pool global 20
seed_pool "backend:CLOUD_RUN_JOB" 20
seed_pool "resource:standard" 20
seed_pool "runner:mock" 5
seed_pool "tenant:u-local" 10
ok "pools seeded in the emulator"

# Each service exposes create_app() and deliberately NOT a module-level `app`:
# building one at import time would construct a Firestore client during import,
# which would make every unit test need credentials. So uvicorn gets --factory.
app_module() {
  case "$1" in
    api)          printf 'swarm_api.main:create_app' ;;
    scheduler)    printf 'scheduler.main:create_app' ;;
    quota-broker) printf 'quota_broker.main:create_app' ;;
  esac
}

case "${TARGET}" in
  emulator)
    step "Emulator only"
    ok "FIRESTORE_EMULATOR_HOST=${EMULATOR_HOST}"
    dim "point a service at it with:  export FIRESTORE_EMULATOR_HOST=${EMULATOR_HOST}"
    dim "press Ctrl-C to stop"
    if [[ -n "${EMULATOR_PID}" ]]; then wait "${EMULATOR_PID}"; else
      while emulator_up; do sleep 5; done
    fi
    ;;
  api|scheduler|quota-broker)
    MODULE="$(app_module "${TARGET}")"
    PACKAGE="${MODULE%%.*}"
    step "Running ${TARGET}"
    if ! uv run --project "${REPO_ROOT}" python -c "import ${PACKAGE}" >/dev/null 2>&1; then
      warn "python package '${PACKAGE}' is not importable yet (its track may not have landed)"
      ok "the emulator is up at ${EMULATOR_HOST}; start your service against it manually"
      dim "  export FIRESTORE_EMULATOR_HOST=${EMULATOR_HOST} PROJECT_ID=${DEV_PROJECT}"
      dim "  uv run uvicorn --factory ${MODULE} --reload --port ${APP_PORT}"
      if [[ -n "${EMULATOR_PID}" ]]; then wait "${EMULATOR_PID}"; fi
      exit 0
    fi
    info "uvicorn --factory ${MODULE} on :${APP_PORT} against ${EMULATOR_HOST}"
    uv run --project "${REPO_ROOT}" uvicorn --factory "${MODULE}" --reload --port "${APP_PORT}"
    ;;
  *)
    die "unknown target '${TARGET}'; expected api, scheduler, quota-broker or emulator"
    ;;
esac
