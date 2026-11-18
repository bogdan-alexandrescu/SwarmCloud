#!/usr/bin/env bash
# Starts Voice it's speech server on the speech machine
# (docs/design/voice-it-own-speech.md §5.2, §5.3). Run by swarm-speech.service
# as root on the HOST, at every boot and after every exit of the container.
#
# 1. Reads the instance's metadata, which swarm-api sets on the stopped machine
#    before each start (§11.4): the image by digest, swarm-api's service
#    account email (the one caller the server admits) and the models.
# 2. Pulls the image if it is not here yet. While it pulls, port 8080 is
#    closed; swarm-api, which set the new digest itself, shows "updating
#    speech server" until the socket answers (§5.3).
# 3. Keeps the running image and the one before it, and removes every other
#    swarm-speech image, so the 150 GB disk never fills with old versions.
# 4. Runs the container in the foreground with the GPU, so systemd sees it
#    exit and restarts it (Restart=always), and its stdout reaches the journal
#    for the machine's logging agent.
#
# Prints no metadata value except the image reference: the models JSON and the
# caller's email are passed to the container, never echoed. No token is
# handled here: the pull authenticates through gcloud's credential helper as
# the machine's own service account (roles/artifactregistry.reader, §6 I3).
set -euo pipefail

readonly METADATA="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
readonly ROOT="/var/lib/swarm-speech"
readonly HISTORY="${ROOT}/state/images"
readonly NAME="swarm-speech"
# The image runs as this uid (apps/swarm-speech/Dockerfile); it owns the mounted directories.
readonly UID_GID="10001"

attr() {
  curl -fsS --retry 5 --retry-all-errors --retry-delay 2 -H "Metadata-Flavor: Google" "${METADATA}/$1"
}

image="$(attr swarm-speech-image)"
caller="$(attr swarm-speech-caller)"
models="$(attr swarm-speech-models)"

# Only an Artifact Registry swarm-speech image pinned by digest is ever run.
if [[ ! "${image}" =~ ^[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+/swarm-speech@sha256:[0-9a-f]{64}$ ]]; then
  echo "swarm-speech-image is not an Artifact Registry swarm-speech image pinned by digest" >&2
  exit 1
fi
repository="${image%@*}"
registry="${image%%/*}"

install -d -m 0750 -o "${UID_GID}" -g "${UID_GID}" "${ROOT}" "${ROOT}/models" "${ROOT}/state"

if ! docker image inspect "${image}" >/dev/null 2>&1; then
  echo "pulling ${image}"
  gcloud auth configure-docker "${registry}" --quiet >/dev/null 2>&1
  docker pull --quiet "${image}"
fi

# The running image first, then the one it replaces.
previous=""
if [[ -f "${HISTORY}" ]]; then
  previous="$(head -n 1 "${HISTORY}")"
fi
if [[ "${previous}" != "${image}" ]]; then
  printf '%s\n%s\n' "${image}" "${previous}" > "${HISTORY}.tmp"
  mv "${HISTORY}.tmp" "${HISTORY}"
fi
keep_previous="$(sed -n 2p "${HISTORY}")"
while read -r present; do
  if [[ "${present}" != "${image}" && "${present}" != "${keep_previous}" ]]; then
    echo "removing ${present}"
    docker rmi "${present}" >/dev/null 2>&1 || true
  fi
done < <(docker images --no-trunc --format '{{.Repository}}@{{.Digest}}' "${repository}")

docker rm --force "${NAME}" >/dev/null 2>&1 || true
exec docker run --rm --name "${NAME}" --gpus all \
  --publish 8080:8080 \
  --volume "${ROOT}:${ROOT}" \
  --log-opt max-size=50m --log-opt max-file=2 \
  --env SWARM_SPEECH_CALLER="${caller}" \
  --env SWARM_SPEECH_MODELS="${models}" \
  --env PORT=8080 \
  "${image}"
